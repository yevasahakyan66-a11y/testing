import asyncio
import logging

import aiohttp

from config import (
    logger, GEMINI_API_KEY, OPENROUTER_API_KEY,
    GEMINI_MODEL, GEMINI_FALLBACK_MODELS, OPENROUTER_MODEL, AI_TEMPERATURE,
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _sniff_image_mime(data):
    """Определить MIME по magic bytes (для Gemini Vision)."""
    if data:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png'
        if data[:3] == b'\xff\xd8\xff':
            return 'image/jpeg'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'image/webp'
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return 'image/gif'
        if data[:2] == b'BM':
            return 'image/bmp'
    return 'image/png'


class AIError(Exception):
    """Ошибка обоих провайдеров — нечего ответить."""


class AIEngine:
    """Генерация текста с прозрачным фоллбэком Gemini → OpenRouter."""

    def __init__(self):
        self._genai = None
        self._models = None
        self._model_idx = 0
        self._sem = asyncio.Semaphore(2)

    # ── вспомогательное ──

    def _get_genai(self):
        """Лениво инициализирует google-generativeai (sync SDK)."""
        if self._genai is None:
            if not GEMINI_API_KEY:
                return None
            try:
                import google.generativeai as genai
            except Exception as ex:
                logger.warning(f"google-generativeai import failed: {ex}")
                return None
            genai.configure(api_key=GEMINI_API_KEY)
            self._genai = genai
            seen = []
            for name in (GEMINI_MODEL, *GEMINI_FALLBACK_MODELS):
                if name and name not in seen:
                    seen.append(name)
            self._models = tuple(seen)
        return self._genai

    def _gemini_sync(self, prompt, system_prompt, image_bytes):
        genai = self._get_genai()
        if genai is None:
            raise AIError("GEMINI_API_KEY не задан или SDK недоступен.")
        request = []
        if system_prompt:
            request.append(system_prompt)
        if image_bytes:
            request.append({'mime_type': _sniff_image_mime(image_bytes), 'data': image_bytes})
        if prompt:
            request.append(prompt)
        last_err = None
        for idx in range(self._model_idx, len(self._models)):
            model_name = self._models[idx]
            try:
                response = genai.GenerativeModel(model_name).generate_content(request)
            except Exception as ex:
                last_err = ex
                logger.warning(f"Gemini {model_name} недоступен, пробую другую модель: {ex}")
                continue
            text = getattr(response, 'text', None)
            if not text and getattr(response, 'parts', None):
                text = ''.join(p.text for p in response.parts if getattr(p, 'text', None))
            if not text:
                last_err = AIError(f"Gemini ({model_name}) вернул пустой ответ.")
                logger.warning(f"Gemini {model_name} вернул пустой ответ, пробую другую модель.")
                continue
            self._model_idx = idx
            return text.strip()
        if last_err is None:
            last_err = AIError("Gemini: нет доступных моделей.")
        raise AIError(f"Все Gemini-модели недоступны: {str(last_err).splitlines()[0][:150]}") from last_err

    async def _gemini(self, prompt, system_prompt, image_bytes):
        try:
            return await asyncio.to_thread(
                self._gemini_sync, prompt, system_prompt, image_bytes
            )
        except AIError:
            raise
        except Exception as ex:
            logger.warning(f"Gemini error: {ex}")
            raise

    async def _openrouter(self, prompt, system_prompt):
        if not OPENROUTER_API_KEY:
            raise AIError("OPENROUTER_API_KEY не задан.")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": AI_TEMPERATURE,
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.post(_OPENROUTER_URL, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        raise AIError(f"OpenRouter: rate limit (HTTP 429)")
                    if resp.status != 200:
                        body = await resp.text()
                        raise AIError(f"OpenRouter: HTTP {resp.status} — {body[:300]}")
                    data = await resp.json()
        except aiohttp.ClientError as ex:
            raise AIError(f"OpenRouter: сеть недоступна — {ex}")
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as ex:
            raise AIError(f"OpenRouter: неожиданный ответ — {ex}")
        if not text:
            raise AIError("OpenRouter вернул пустой ответ.")
        return text

    async def generate(self, prompt, system_prompt=None, image_bytes=None):
        """Сначала Gemini, при любой ошибке/лимите — транзакция на OpenRouter.

        Возвращает (текст_ответа, имя_провайдера).
        """
        async with self._sem:
            try:
                text = await self._gemini(prompt, system_prompt, image_bytes)
                return text, "Gemini"
            except Exception as gem_ex:
                logger.warning(f"Gemini недоступен, пробую OpenRouter: {gem_ex}")
            try:
                text = await self._openrouter(prompt, system_prompt)
                return text, "OpenRouter"
            except Exception as or_ex:
                logger.warning(f"OpenRouter тоже не сработал: {or_ex}")
                raise AIError(
                    f"AI недоступен: Gemini ({self._fmt(gem_ex)}) | "
                    f"OpenRouter ({self._fmt(or_ex)})"
                ) from or_ex

    @staticmethod
    def _fmt(ex):
        return str(ex).splitlines()[0][:120] if ex else '?'