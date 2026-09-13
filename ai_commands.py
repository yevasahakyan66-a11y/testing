import asyncio
import json
import os
import random
import subprocess
import tempfile
import time

from telethon import events
from telethon.tl.types import (
    UpdateMessageReactions, UpdateBotMessageReaction, UpdateBotMessageReactions,
    ReactionEmpty, User, PeerUser,
)

from client import client
from config import (
    logger, MEDIA_DIR, AI_TTS_VOICE, AI_MAX_HISTORY, OWNER_ID,
)
from core import state, db, owner_filter, respond
from downloaders import _HAS_FFMPEG, _FFMPEG_PATH
from ai_engine import AIEngine, AIError

eng = AIEngine()

_me_cache = None


def random_pause():
    return random.uniform(0.4, 1.2)


async def _me():
    global _me_cache
    if _me_cache is None:
        _me_cache = await client.get_me()
    return _me_cache


# ────────────────────────────────────────────────────────────
# Вспомогательное
# ────────────────────────────────────────────────────────────

ROLE_PROMPTS = {
    'девушка': (
        "Ты — девушка владельца бота. Веди себя ласково, заботливо и с лёгким "
        "флиртом. Короткие, живые ответы, эмодзи иногда. Не пиши большими простынями."
    ),
    'враг': (
        "Ты — враг собеседника. Отвечай дерзко, саркастично, провокационно, "
        "но без зашкаливающей агрессии. Ты не поддаёшься на провокации и умеешь защищаться."
    ),
    'друг': (
        "Ты — лучший друг владельца бота. Ответы дружелюбные, поддерживающие, "
        "иногда с лёгким юмором. Искренне интересуешься делами собеседника."
    ),
    'босс': (
        "Ты — босс/начальник владельца. Отвечаешь официально-деловым тоном, "
        "кратко, по делу, без лишних эмоций. Решения принимаешь чётко и уверенно."
    ),
}

REACTION_PROMPTS = {
    "🤔": "Проанализируй текст и найди скрытый смысл, подтекст или потенциальную манипуляцию: «{}»",
    "🤬": "Проанализируй текст на уровень токсичности, пассивной агрессии и «красные флаги»: «{}»",
    "⚡": "Сделай краткую выжимку (TL;DR) этого сообщения в 1-2 предложениях: «{}»",
    "🤓": "Переведи этот текст на русский язык (или на английский, если он уже на русском): «{}»",
    "🤡": "Сделай жесткий, но смешной и саркастичный роаст этого сообщения: «{}»",
}

_ai_cd = {}


def _cooldown(key, sec=3):
    now = time.time()
    if _ai_cd.get(key, 0) > now:
        return int(_ai_cd[key] - now)
    _ai_cd[key] = now + sec
    if len(_ai_cd) > 500:
        cutoff = now - 3600
        for k in [k for k, v in _ai_cd.items() if v < cutoff]:
            _ai_cd.pop(k, None)
    return 0


def _truncate(t, n=3000):
    t = (t or '').strip()
    return t if len(t) <= n else t[:n] + "…"


def _build_transcript(hist, me_id, trunc=150):
    """Собрать переписку в строки «Я: …» / «Он(а): …» (без await в генераторах)."""
    lines = []
    for m in hist:
        who = "Я" if m['sender_id'] == me_id else "Он(а)"
        lines.append(f"{who}: {_truncate(m['text'], trunc)}")
    return "\n".join(lines)


async def _resolve_target(event, mention=None):
    """Вернуть entity собеседника: по @упоминанию → реплаю → если команда в ЛС — сам чат."""
    if mention:
        return await client.get_entity(mention)
    if event.reply_to_msg_id:
        r = await event.get_reply_message()
        if r and r.sender_id:
            try:
                return await client.get_entity(r.sender_id)
            except Exception:
                pass
    chat = await event.get_chat()
    if isinstance(chat, User):
        return chat
    return None


async def _last_text(event):
    if event.reply_to_msg_id:
        r = await event.get_reply_message()
        return (r.sender_id, r.raw_text or '')
    tgt = await _resolve_target(event)
    if tgt:
        hist = db.get_ai_msgs(tgt.id, limit=1)
        if hist:
            return (hist[0]['sender_id'], hist[0]['text'])
    return (None, None)


async def _grab_history(uid, n):
    """Вернуть до n сообщений из базы; при нехватке дотянуть прямо из Telegram."""
    hist = db.get_ai_msgs(uid, limit=n)
    if len(hist) >= n:
        return hist
    pulled = []
    try:
        async for m in client.iter_messages(uid, limit=n):
            if m.raw_text and not m.raw_text.startswith('!'):
                ts = m.date.timestamp() if m.date else time.time()
                db.save_ai_msg(uid, m.sender_id or uid, m.raw_text[:1000], ts=ts)
                pulled.insert(0, {'sender_id': m.sender_id or uid, 'text': m.raw_text[:1000]})
    except Exception as ex:
        logger.debug(f"grab_history: {ex}")
    if len(hist) < n and pulled:
        hist = db.get_ai_msgs(uid, limit=n)
        if not hist:
            hist = pulled
    return hist


def _target_name(ent):
    if not ent:
        return 'пользователь'
    return getattr(ent, 'first_name', None) or getattr(ent, 'title', 'пользователь') or 'пользователь'


def _system_for_auto(user_auto, uid):
    base = ROLE_PROMPTS.get(user_auto['role']) or user_auto['prompt'] or ROLE_PROMPTS['друг']
    lines = [base]
    facts = db.get_ai_facts(uid)
    if facts:
        lines.append("Факты о собеседнике, которые обязательно учитывать:\n- " + "\n- ".join(facts))
    lines.append("Отвечай одним сообщением, естественно, как в переписке.")
    return "\n".join(lines)


def _user_prompt_response(uid, text):
    me = _me_cache
    me_id = me.id if me else OWNER_ID
    hist = db.get_ai_msgs(uid, limit=20)
    lines = ["Переписка с человеком (последние сообщения):"]
    for m in hist:
        who = "Я" if m['sender_id'] == me_id else "Собеседник"
        lines.append(f"{who}: {_truncate(m['text'], 300)}")
    lines.append(f"Собеседник только что написал: {_truncate(text, 500)}")
    lines.append("Ответь на это сообщение.")
    return "\n".join(lines)


_QW = {
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p','х':'[','ъ':']',
    'ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k','д':'l','ж':';','э':"'",
    'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m','б':',','ю':'.','ё':'`',
    'Й':'Q','Ц':'W','У':'E','К':'R','Е':'T','Н':'Y','Г':'U','Ш':'I','Щ':'O','З':'P','Х':'[','Ъ':']',
    'Ф':'A','Ы':'S','В':'D','А':'F','П':'G','Р':'H','О':'J','Л':'K','Д':'L','Ж':':','Э':'"',
    'Я':'Z','Ч':'X','С':'C','М':'V','И':'B','Т':'N','Ь':'M','Б':'<','Ю':'>','Ё':'~',
}


def _swap_layout(text):
    """Инверсия ЙЦУКЕН ↔ QWERTY. Возвращает кандидата с бОльшим числом кириллицы."""
    def corki(s):
        return sum(1 for c in s if 'а' <= c <= 'я' or 'А' <= c <= 'Я' or c in 'ёЁ')

    ru_to_en = {ru: en for ru, en in _QW.items()}
    en_to_ru = {v: k for k, v in _QW.items() if v not in ('[', ']', ';', "'")}
    cand1 = text.translate(str.maketrans(ru_to_en))
    cand2 = text.translate(str.maketrans(en_to_ru))
    base = corki(text)
    best = max([(cand1, corki(cand1)), (cand2, corki(cand2))], key=lambda x: x[1])
    if best[1] > base:
        return best[0]
    return text


async def _send_saved(text, caption=None):
    me = await _me()
    await client.send_message('me', text if not caption else f"{caption}\n\n{text}")


async def _gen(prompt, system=None, image=None):
    try:
        return await eng.generate(prompt, system_prompt=system, image_bytes=image)
    except AIError as ex:
        raise
    except Exception as ex:
        logger.error(f"AI generate error: {ex}")
        raise AIError(f"AI ошибка: {ex}")


def _fmt_aierr(ex):
    return f"❌ {ex}"


# ────────────────────────────────────────────────────────────
# MIDDLEWARE: история + AI-автоответчик в ЛС
# ────────────────────────────────────────────────────────────

@client.on(events.NewMessage(func=lambda e: e.is_private and e.raw_text))
async def ai_private_middleware(event):
    text = event.raw_text.strip()
    if not text or text.startswith('!') or text.startswith('/'):
        return
    peer_id = event.chat_id
    sender_id = event.sender_id or 0
    if peer_id <= 0:
        return
    _poll_bump(peer_id)
    try:
        db.save_ai_msg(peer_id, sender_id, text[:1000])
    except Exception as ex:
        logger.debug(f"ai save: {ex}")

    if event.out:
        return

    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False):
        return
    uid = event.sender_id
    if not uid or uid == OWNER_ID:
        return

    if state.cover_enabled or state.silent_enabled:
        return

    if state.lock_enabled:
        try:
            if uid != (await _me()).id:
                is_contact = db.get_saved(f'_lock_cache_{uid}')
                if is_contact is None:
                    is_contact = '0'
                    try:
                        contact = await client.get_entity(uid)
                        if getattr(contact, 'contact', False):
                            is_contact = '1'
                    except Exception:
                        pass
                    if is_contact == '0':
                        try:
                            common = await client.get_common_chats(uid)
                            if common:
                                is_contact = '1'
                        except Exception:
                            pass
                    db.set_saved(f'_lock_cache_{uid}', is_contact)
                if is_contact == '0':
                    return
        except Exception:
            pass

    if db.is_ai_blocked(uid):
        return
    whitelist = db.get_ai_list('white')
    if whitelist and uid not in whitelist:
        return
    if state.mute_enabled:
        return

    auto = db.get_ai_auto(uid)
    if not auto['enabled']:
        return

    sysp = _system_for_auto(auto, uid)
    prompt = _user_prompt_response(uid, text)

    cd = _cooldown(f'autoreply_{uid}', 15)
    if cd:
        return

    if db.get_ai_prompt_mode(uid):
        try:
            variants, prov = await _gen(
                prompt + "\n\nДополнительно: предложи 3 коротких варианта ответа, каждый на новой строке: 1), 2), 3).",
                system=sysp,
            )
            await _send_saved(
                "1) 2) 3)\n" + variants,
                caption=f"🤖 3 варианта ответа для [{_target_name(sender)}](tg://user?id={uid})",
            )
        except AIError as ex:
            await _send_saved(str(ex), caption="❌ AI prompt mode")
        return

    delay = db.get_ai_delay() or 0
    if delay:
        await asyncio.sleep(min(delay, 30))
    try:
        async with client.action(event.chat_id, 'typing'):
            await asyncio.sleep(0.5 + random_pause())
    except Exception:
        pass

    try:
        answer, prov = await _gen(prompt, system=sysp)
    except AIError as ex:
        await _send_saved(str(ex), caption=f"❌ Автоответ для [{_target_name(sender)}](tg://user?id={uid})")
        return

    try:
        sent = await event.reply(_truncate(answer, 4000))
        if state.ghost_mode or state.shadow_enabled or state.autodel_enabled:
            delay = None
            if state.ghost_mode or state.shadow_enabled:
                delay = 30
            if state.autodel_enabled:
                delay = state.autodel_delay
            if delay is not None and delay > 0:
                await asyncio.sleep(delay)
                try:
                    await sent.delete()
                except Exception:
                    pass
        logger.info(f"🤖 AI автоответ ({prov}) → {uid}")
        db.bump_stat('cmds')
    except Exception as ex:
        logger.warning(f"AI auto reply send failed: {ex}")


# ────────────────────────────────────────────────────────────
# ЭМОДЗИ-ТРИГГЕРЫ (реакции в ЛС)
# ────────────────────────────────────────────────────────────
#
# Сервер НЕ присылает апдейт о реакции, которую владелец ставит сам на
# сообщение собеседника (updateMessageReactions получает автор сообщения).
# Официальные клиенты синхронизируют свои реакции коротким опросом видимых
# сообщений (15–30с). Поэтому основной механизм здесь — опрос
# (_reaction_poll_cycle), а апдейты остаются быстрым дополнительным путём.

_active_privates = {}         # peer_id -> время последней активности (для опроса)
_reaction_poll_started = False
_reaction_done = set()        # (peer_id, msg_id, emoji) — уже обработанные реакции


def _reaction_emoticons(reactions):
    """Из списка Reaction-объектов достать обычные эмодзи (пустые/кастомные — пропускаем)."""
    out = set()
    for r in reactions or ():
        if r is None or isinstance(r, ReactionEmpty):
            continue
        em = getattr(r, 'emoticon', None)
        if em:
            out.add(em)
    return out


def _own_reaction_emojis(reactions):
    """Свои реакции из MessageReactions/списка ReactionCount (по chosen_order).

    Для текущего пользователя server всегда заполняет chosen_order у своей
    реакции — это серверная истина, не зависит от апдейтов и сессии.
    """
    out = set()
    results = getattr(reactions, 'results', None)
    if isinstance(results, (list, tuple)):
        for rc in results:
            if getattr(rc, 'chosen_order', None) is None:
                continue
            em = getattr(getattr(rc, 'reaction', None), 'emoticon', None)
            if em:
                out.add(em)
    return out


def _reaction_add(peer_id, msg_id, emojis):
    """Прогнать эмодзи через общий дедуп; вернуть только новые."""
    new = set()
    for em in emojis:
        key = (peer_id, msg_id, em)
        if key not in _reaction_done:
            _reaction_done.add(key)
            new.add(em)
    if len(_reaction_done) > 4000:
        for key in list(_reaction_done)[:3000]:
            _reaction_done.remove(key)
    return new


async def _process_reaction(peer_id, msg_id, emoji, text):
    prompt = REACTION_PROMPTS.get(emoji, REACTION_PROMPTS['🤔'])
    try:
        ans, prov = await _gen(
            prompt.replace('{}', _truncate(text, 2000)),
            system="Ты — опытный аналитик.",
        )
        me = await _me()
        quoted = _truncate(text, 100)
        await client.send_message(
            me.id,
            f"🔍 **Разбор по реакции {emoji}:**\n"
            f"💬 *Сообщение:* «{quoted}»\n\n"
            f"💡 **Ответ AI:**\n{ans}",
        )
        logger.info(f"🤖 Реакция {emoji} → {peer_id} (#{msg_id})")
    except AIError as ex:
        await _send_saved(str(ex), caption=f"❌ Триггер {emoji}")
    except Exception as ex:
        logger.warning(f"trigger {emoji}: {ex}")


async def _react_to_emojis(peer_id, msg_id, emojis):
    if not emojis:
        return
    try:
        msg = await client.get_messages(peer_id, ids=[msg_id])
        if not msg or msg.out:
            return
        text = (msg.raw_text or '').strip()
        if not text:
            return
    except Exception as ex:
        logger.debug(f"reaction fetch: {ex}")
        return
    for emoji in emojis:
        asyncio.create_task(_process_reaction(peer_id, msg_id, emoji, text))


def _reaction_counts(update):
    """Достать список ReactionCount из обновлений разного вида.

    UpdateMessageReactions.user-side отдаёт объект MessageReactions (обёртку),
    UpdateBotMessageReactions.bot-side — сразу список ReactionCount.
    """
    raw = getattr(update, 'reactions', None)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = (getattr(raw, 'results', None) or getattr(raw, 'reactions', None)) or []
    return list(raw)


async def reactions_plural(update):
    """Быстрый путь: сводное обновление реакций, если оно пришло.

    Основной механизм — короткий опрос (_reaction_poll_cycle): сервер шлёт
    updateMessageReactions только автору сообщения, а не нашим сессиям.
    """
    peer = update.peer
    if not isinstance(peer, PeerUser) or peer.user_id <= 0:
        return
    active = set()
    for rc in _reaction_counts(update):
        if getattr(rc, 'chosen_order', None) is None:
            continue
        em = getattr(getattr(rc, 'reaction', None), 'emoticon', None)
        if em:
            active.add(em)
    new_em = _reaction_add(peer.user_id, update.msg_id, active & set(REACTION_PROMPTS))
    if new_em:
        await _react_to_emojis(peer.user_id, update.msg_id, new_em)


@client.on(events.Raw(types=UpdateMessageReactions))  # user-side: приходит юзерботам
async def reactions_user_plural(update):
    await reactions_plural(update)


@client.on(events.Raw(types=UpdateBotMessageReactions))  # запасной вариант для ботов
async def reactions_bot_plural(update):
    await reactions_plural(update)


@client.on(events.Raw(types=UpdateBotMessageReaction))  # отдельное обновление (для ботов)
async def reactions_bot_single(update):
    peer = update.peer
    if not isinstance(peer, PeerUser) or peer.user_id <= 0:
        return
    actor = getattr(update, 'actor', None)
    if isinstance(actor, PeerUser) and getattr(actor, 'user_id', None) != OWNER_ID:
        return
    active = _reaction_emoticons(getattr(update, 'new_reactions', None))
    new_em = _reaction_add(peer.user_id, update.msg_id, active & set(REACTION_PROMPTS))
    if new_em:
        await _react_to_emojis(peer.user_id, update.msg_id, new_em)


def _poll_bump(peer_id):
    """Отметить ЛС активным — оно будет опрашиваться на реакции."""
    _active_privates[peer_id] = time.time()
    _ensure_reaction_poll()


def _ensure_reaction_poll():
    global _reaction_poll_started
    if _reaction_poll_started:
        return
    _reaction_poll_started = True
    asyncio.create_task(_reaction_poll_loop())
    logger.info("Реакции: короткий опрос активен (каждые ~25 с)")


async def _reaction_poll_loop():
    while True:
        try:
            await _reaction_poll_cycle()
        except asyncio.CancelledError:
            return
        except Exception as ex:
            logger.debug(f"reaction poll: {ex}")
        await asyncio.sleep(25)


async def _reaction_poll_cycle():
    now = time.time()
    for peer_id in list(_active_privates):
        if now - _active_privates[peer_id] > 600:  # неактивен 10 минут — пропускаем
            continue
        try:
            msgs = await client.get_messages(peer_id, limit=20)
        except Exception as ex:
            logger.debug(f"poll {peer_id}: {ex}")
            continue
        for msg in msgs or ():
            if not msg or getattr(msg, 'out', False):
                continue
            text = (msg.raw_text or '').strip()
            if not text:
                continue
            reactions = getattr(msg, 'reactions', None)
            if not reactions:
                continue
            own = _own_reaction_emojis(reactions)
            if not own:
                continue
            new_em = _reaction_add(peer_id, msg.id, own)
            if new_em:
                for emoji in new_em:
                    asyncio.create_task(_process_reaction(peer_id, msg.id, emoji, text))


# ────────────────────────────────────────────────────────────
# КОМАНДЫ: контекст / анализ
# ────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=r'!summary(?:\s+(@\w+))?(?:\s+(\d+))?$', func=owner_filter))
async def summary_cmd(e):
    mention, n = e.pattern_match.group(1), e.pattern_match.group(2)
    tgt = await _resolve_target(e, mention)
    if not tgt:
        await respond(e, "ℹ️ `!summary @ник` — выжимка переписки. Работает в ЛС с этим человеком.")
        return
    n = min(int(n or 20), AI_MAX_HISTORY)
    hist = db.get_ai_msgs(tgt.id, limit=n)
    if len(hist) < 3:
        await respond(e, f"❌ Мало сообщений в базе ({len(hist)}). Используй `!sync @{tgt.username or tgt.id} {n}`.")
        db.bump_stat('cmds')
        return
    transcript = _build_transcript(hist, (await _me()).id, 200)
    msg = await respond(e, f"⏳ Выжимаю суть из {len(hist)} сообщений...")
    try:
        res, prov = await _gen(
            f"Сделай TL;DR переписки в ровно 3 предложениях. Кто собеседник, о чём речь, \
куда движется разговор, вывод:\n\n{transcript}",
            system="Ты делаешь краткие точные выжимки.",
        )
        await msg.edit(f"📌 **Выжимка ({_target_name(tgt)}):**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!vibe(?:\s+(@\w+))?$', func=owner_filter))
async def vibe_cmd(e):
    mention = e.pattern_match.group(1)
    tgt = await _resolve_target(e, mention)
    if not tgt:
        await respond(e, "ℹ️ `!vibe @ник` — анализ настроения. Работает в ЛС с этим человеком.")
        return
    hist = await _grab_history(tgt.id, 100)
    if len(hist) < 3:
        await respond(e, f"❌ Мало сообщений ({len(hist)}). Работает в ЛС с этим человеком — сначала напишите друг другу.")
        db.bump_stat('cmds')
        return
    transcript = _build_transcript(hist[-50:], (await _me()).id, 150)
    msg = await respond(e, "⏳ Считываю настроение...")
    try:
        res, prov = await _gen(
            f"Проанализируй настроение собеседника по переписке. Ответь строго по шаблону:\n"
            f"🎯 Агрессия: X%\n❤️ Симпатия: X%\n📊 Вовлечённость: X%\n"
            f"🧠 Общий тон: …\n💡 Совет: …\n\nПереписка:\n{transcript}",
            system="Ты — аналитик эмоций.",
        )
        body = f"🎭 **Вибрация:** [{_target_name(tgt)}]\n\n{res}"
        await msg.edit(body)
        await _send_saved(body)
        logger.info(f"🌀 vibe {tgt.id} ({prov})")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!liar(?:\s+(@\w+))?$', func=owner_filter))
async def liar_cmd(e):
    mention = e.pattern_match.group(1)
    tgt = await _resolve_target(e, mention)
    if not tgt:
        await respond(e, "ℹ️ `!liar @ник` — поиск противоречий. Работает в ЛС с этим человеком.")
        return
    hist = await _grab_history(tgt.id, AI_MAX_HISTORY)
    if len(hist) < 2:
        await respond(e, f"❌ Мало сообщений ({len(hist)}). Работает в ЛС с этим человеком — сначала напишите друг другу.")
        db.bump_stat('cmds')
        return
    transcript = _build_transcript(hist[-100:], (await _me()).id, 150)
    facts = db.get_ai_facts(tgt.id)
    extra = "\nФакты о человеке:\n- " + "\n- ".join(facts) if facts else ""
    msg = await respond(e, "⏳ Ищу противоречия...")
    try:
        res, prov = await _gen(
            f"Найди противоречия в словах собеседника: что он говорил в разное время, что менялось, \
где расходится с фактами. По каждому пункту дай цитируемую пару. Если противоречий нет — так и скажи.\n\n{transcript}{extra}",
            system="Ты — дотошный детектор противоречий.",
        )
        body = f"🕵️ **Противоречия [{_target_name(tgt)}]:**\n\n{res}"
        await msg.edit(body)
        await _send_saved(body)
        logger.info(f"🕵️ liar {tgt.id} ({prov})")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!sync(?:\s+(@\w+))?(?:\s+(\d+))?$', func=owner_filter))
async def sync_cmd(e):
    mention, n = e.pattern_match.group(1), e.pattern_match.group(2)
    tgt = await _resolve_target(e, mention)
    if not tgt:
        await respond(e, "ℹ️ `!sync @ник [кол-во]` — сохранить историю в базу для ИИ.")
        return
    n = min(int(n or 100), 500)
    msg = await respond(e, f"⏳ Синхронизирую {n} сообщений с {_target_name(tgt)}...")
    saved = 0
    async for m in client.iter_messages(tgt.id, limit=n):
        if m.raw_text and not m.raw_text.startswith('!'):
            db.save_ai_msg(tgt.id, m.sender_id or tgt.id, m.raw_text[:1000], ts=m.date.timestamp() if m.date else time.time())
            saved += 1
            if saved % 50 == 0:
                await asyncio.sleep(0.2)
    await msg.edit(f"✅ Синхронизировано: **{saved}** сообщений с [{_target_name(tgt)}](tg://user?id={tgt.id}).")
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!fact\s+(@\w+)\s+(.+)', func=owner_filter))
async def fact_add_cmd(e):
    mention, text = e.pattern_match.group(1), e.pattern_match.group(2).strip()
    tgt = await client.get_entity(mention)
    db.add_ai_fact(tgt.id, _truncate(text, 300))
    facts = db.get_ai_facts(tgt.id)
    await respond(e, f"🧠 Факт сохранён о [{_target_name(tgt)}](tg://user?id={tgt.id}). Всего фактов: **{len(facts)}**.")
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!facts(?:\s+(@\w+))?$', func=owner_filter))
async def facts_cmd(e):
    mention = e.pattern_match.group(1)
    tgt = await _resolve_target(e, mention)
    if not tgt:
        await respond(e, "ℹ️ `!facts @ник` — факты о человеке.")
        db.bump_stat('cmds')
        return
    facts = db.get_ai_facts(tgt.id)
    if not facts:
        await respond(e, f"📭 Фактов о [{_target_name(tgt)}](tg://user?id={tgt.id}) пока нет. Добавь через `!fact @{tgt.username or tgt.id} текст`.")
    else:
        await respond(e, f"🧠 **Факты о {_target_name(tgt)}:**\n\n" + "\n".join(f"• {f}" for f in facts))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!fact\s+clear\s+(@\w+)', func=owner_filter))
async def fact_clear_cmd(e):
    tgt = await client.get_entity(e.pattern_match.group(1))
    db.clear_ai_facts(tgt.id)
    await respond(e, f"🗑 Факты о {_target_name(tgt)} удалены.")
    db.bump_stat('cmds')


# ────────────────────────────────────────────────────────────
# КОМАНДЫ: редактура и проверка
# ────────────────────────────────────────────────────────────

async def _get_target_msg(e):
    sender_id, text = await _last_text(e)
    if not text:
        return None, "❌ Нет текста. Ответь на сообщение или используй в ЛС."
    return text, None


@client.on(events.NewMessage(pattern=r'!rewrite(?:\s+(.+))?$', func=owner_filter))
async def rewrite_cmd(e):
    style = (e.pattern_match.group(1) or '').strip() or 'вежливо'
    text, err = await _get_target_msg(e)
    if err:
        await respond(e, err)
        db.bump_stat('cmds')
        return
    msg = await respond(e, f"⏳ Переписываю в стиле «{style}»...")
    try:
        res, prov = await _gen(
            f"Перепиши сообщение в стиле «{style}», сохранив смысл. Верни только готовый текст:\n\n{_truncate(text, 2000)}",
            system="Ты — редактор текста.",
        )
        await msg.edit(f"✍️ **Переписано ({style}):**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!fix(?:\s+(.+))?$', func=owner_filter))
async def fix_cmd(e):
    raw = (e.pattern_match.group(1) or '').strip()
    if raw:
        text = raw
    else:
        text, err = await _get_target_msg(e)
        if err:
            await respond(e, err)
            db.bump_stat('cmds')
            return
    swapped = _swap_layout(text)
    msg = await respond(e, "⏳ Правлю...")
    try:
        res, prov = await _gen(
            f"Исправь грамматику, опечатки и раскладку текста. Верни только исправленный текст:\n\n{_truncate(swapped, 2000)}",
            system="Ты — редактор обязан вернуть только итоговый текст без объяснений.",
        )
        await msg.edit(f"🔧 **Исправлено:**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!factcheck$', func=owner_filter))
async def factcheck_cmd(e):
    text, err = await _get_target_msg(e)
    if err:
        await respond(e, err)
        db.bump_stat('cmds')
        return
    msg = await respond(e, "⏳ Проверяю факты...")
    try:
        res, prov = await _gen(
            f"Проверь факты и нестыковки в утверждениях. Где сомнительно — отметь и предложи, как проверить. \
Ответь кратко:\n\n{_truncate(text, 2500)}",
            system="Ты — фактчекер. Подчёркиваешь степень уверенности каждого пункта.",
        )
        await msg.edit(f"🔍 **Фактчек:**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!args$', func=owner_filter))
async def args_cmd(e):
    text, err = await _get_target_msg(e)
    if err:
        await respond(e, err)
        db.bump_stat('cmds')
        return
    msg = await respond(e, "⏳ Куём контраргументы...")
    try:
        res, prov = await _gen(
            f"Дай 3 железных контраргумента на утверждение — логичных, с фактами и без эмоций. Нумеруй:\n\n{_truncate(text, 2000)}",
            system="Ты — мастер дебатов.",
        )
        await msg.edit(f"⚔️ **Контраргументы:**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!tr(?:\s+([a-z]{2}))?$', func=owner_filter))
async def tr_cmd(e):
    lang = (e.pattern_match.group(1) or 'ru').lower()
    text, err = await _get_target_msg(e)
    if err:
        await respond(e, err)
        db.bump_stat('cmds')
        return
    msg = await respond(e, f"⏳ Перевожу на {lang}...")
    try:
        res, prov = await _gen(
            f"Переведи на язык с кодом {lang}. Только перевод, без пояснений:\n\n{_truncate(text, 2500)}",
            system="Ты — профессиональный переводчик.",
        )
        await msg.edit("✅ Переведено → в Избранное.")
        await _send_saved(res, caption=f"🌐 Перевод ({lang})")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!ocr$', func=owner_filter))
async def ocr_cmd(e):
    if not e.reply_to_msg_id:
        await respond(e, "ℹ️ Ответь на картинку/скриншот с текстом: `!ocr`")
        db.bump_stat('cmds')
        return
    r = await e.get_reply_message()
    if not r.media:
        await respond(e, "❌ В ответном сообщении нет картинки.")
        db.bump_stat('cmds')
        return
    msg = await respond(e, "⏳ Распознаю текст...")
    path = os.path.join(MEDIA_DIR, f'ocr_{int(time.time())}.img')
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        down = await r.download_media(file=path)
        if not down:
            await msg.edit("❌ Не удалось скачать картинку.")
            return
        with open(down, 'rb') as f:
            img = f.read()
        res, prov = await _gen(
            "Распознай весь текст с изображения. Если текста нет — так и напиши. Верни только распознанный текст.",
            system="Ты — OCR-движок.",
            image=img,
        )
        if prov == 'OpenRouter':
            await msg.edit("⚠️ OCR работал через резервный провайдер (без зрения) — результат может быть неточным.")
        else:
            await msg.edit(f"📄 **Распознанный текст:**\n\n{res}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    except Exception as ex:
        await msg.edit(f"❌ Ошибка: {ex}")
        logger.warning(f"ocr: {ex}")
    finally:
        for p in (path, down if 'down' in locals() and down else None):
            if not p:
                continue
            try:
                os.remove(p)
            except OSError:
                pass
    db.bump_stat('cmds')


# ────────────────────────────────────────────────────────────
# КОМАНДЫ: медиа (TTS / кружочек)
# ────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=r'!tts(?:\s+(.+))?$', func=owner_filter))
async def tts_cmd(e):
    raw = (e.pattern_match.group(1) or '').strip()
    if not raw:
        raw, err = await _get_target_msg(e)
        if err:
            await respond(e, err)
            db.bump_stat('cmds')
            return
    msg = await respond(e, "⏳ Озвучиваю...")
    tmp = os.path.join(tempfile.gettempdir(), f'tts_{int(time.time()*1000)}.mp3')
    ogg = os.path.join(tempfile.gettempdir(), f'tts_{int(time.time()*1000)}.ogg')
    try:
        import edge_tts
        comm = edge_tts.Communicate(_truncate(raw, 4000), AI_TTS_VOICE)
        await comm.save(tmp)
        if not os.path.exists(tmp):
            await msg.edit("❌ Не удалось синтезировать голос.")
            return
        if _HAS_FFMPEG:
            ffmpeg = _FFMPEG_PATH or 'ffmpeg'
            r = await asyncio.to_thread(
                subprocess.run,
                [ffmpeg, '-y', '-i', tmp, '-c:a', 'libopus', '-b:a', '48k', '-ar', '48000', ogg],
                capture_output=True, timeout=120,
            )
            path = ogg if r.returncode == 0 else tmp
        else:
            path = tmp
        await client.send_file(e.chat_id, path, voice_note=(_HAS_FFMPEG and path == ogg), reply_to=msg.id)
        await msg.delete()
        logger.info(f"🎙 TTS → {e.chat_id}")
    except ImportError:
        await msg.edit("❌ edge-tts не установлен. `pip install edge-tts`")
    except Exception as ex:
        await msg.edit(f"❌ Ошибка TTS: {ex}")
        logger.warning(f"tts: {ex}")
    finally:
        for p in (tmp, ogg):
            try:
                os.remove(p)
            except OSError:
                pass
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!circle$', func=owner_filter))
async def circle_cmd(e):
    if not e.reply_to_msg_id:
        await respond(e, "ℹ️ Ответь на гифку/видео: `!circle`")
        db.bump_stat('cmds')
        return
    if not _HAS_FFMPEG:
        await respond(e, "❌ ffmpeg не установлен — кружочек недоступен.")
        db.bump_stat('cmds')
        return
    r = await e.get_reply_message()
    if not r.media:
        await respond(e, "❌ В ответном сообщении нет медиа.")
        db.bump_stat('cmds')
        return
    msg = await respond(e, "⏳ Конвертирую в кружочек...")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    src = os.path.join(MEDIA_DIR, f'circle_src_{int(time.time()*1000)}')
    out = os.path.join(MEDIA_DIR, f'circle_{int(time.time()*1000)}.mp4')
    try:
        down = await r.download_media(file=src)
        if not down:
            await msg.edit("❌ Не удалось скачать медиа.")
            return
        ffmpeg = _FFMPEG_PATH or 'ffmpeg'
        base = [ffmpeg, '-y', '-i', down,
                '-vf', "crop='min(iw,ih)':min(iw,ih),scale=512:512,setsar=1",
                '-pix_fmt', 'yuv420p', '-an']
        last_err = ''
        ok = False
        enc_opts_map = {
            'libx264': ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28'],
            'libopenh264': ['-c:v', 'libopenh264', '-b:v', '900k'],
            'mpeg4': ['-c:v', 'mpeg4', '-q:v', '5'],
        }
        for enc, enc_opts in enc_opts_map.items():
            r_ = await asyncio.to_thread(
                subprocess.run, base + enc_opts + ['-movflags', '+faststart', out],
                capture_output=True, timeout=180,
            )
            if r_.returncode == 0 and os.path.exists(out):
                ok = True
                break
            last_err = (r_.stderr or b'').decode('utf-8', errors='replace')[-300:]
            try:
                os.remove(out)
            except OSError:
                pass
        if not ok:
            await msg.edit(f"❌ Конвертация не удалась: {last_err}")
            return
        await client.send_file(e.chat_id, out, video_note=True, reply_to=msg.id)
        await msg.delete()
    except Exception as ex:
        await msg.edit(f"❌ Ошибка: {ex}")
        logger.warning(f"circle: {ex}")
    finally:
        for p in (src, out, down if 'down' in locals() and down else None):
            if not p:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except (OSError, TypeError):
                pass
    db.bump_stat('cmds')


# ────────────────────────────────────────────────────────────
# КОМАНДЫ: автоответчик и доступ
# ────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=r'!auto\s+(@\w+)\s+(on|off)(?:\s+(.+))?$', func=owner_filter))
async def auto_cmd(e):
    mention, mode, role = e.pattern_match.group(1), e.pattern_match.group(2), e.pattern_match.group(3)
    tgt = await client.get_entity(mention)
    if mode == 'off':
        db.set_ai_auto(tgt.id, False)
        await respond(e, f"🔕 Автоответчик для [{_target_name(tgt)}](tg://user?id={tgt.id}) **ВЫКЛЮЧЕН**.")
    else:
        role = (role or 'друг').strip()
        if role in ROLE_PROMPTS:
            db.set_ai_auto(tgt.id, True, role=role)
            await respond(e, f"🤖 Автоответчик для [{_target_name(tgt)}](tg://user?id={tgt.id}) ВКЛЮЧЁН.\n🎭 Роль: **{role}**.")
        else:
            db.set_ai_auto(tgt.id, True, role='кастомная', prompt=role)
            await respond(e, f"🤖 Автоответчик для [{_target_name(tgt)}](tg://user?id={tgt.id}) ВКЛЮЧЁН.\n📝 Кастомный промпт: _{role}_")
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!prompt\s+(@\w+)\s+(on|off)$', func=owner_filter))
async def prompt_cmd(e):
    mention, mode = e.pattern_match.group(1), e.pattern_match.group(2)
    tgt = await client.get_entity(mention)
    db.set_ai_prompt_mode(tgt.id, mode == 'on')
    await respond(e, f"💬 Режим подсказок для [{_target_name(tgt)}](tg://user?id={tgt.id}): "
                     f"{'ВКЛЮЧЁН — варианты в Избранное' if mode == 'on' else 'ВЫКЛЮЧЕН'}.")
    db.bump_stat('cmds')


async def _list_cmd(e, list_type, keyword):
    mention = e.pattern_match.group(1)
    if mention:
        tgt = await client.get_entity(mention)
        added = db.toggle_ai_list(tgt.id, list_type)
        if added and list_type == 'white':
            db.toggle_ai_list(tgt.id, 'black')
        if added and list_type == 'black':
            db.toggle_ai_list(tgt.id, 'white')
        state_ = "добавлен в" if added else "удалён из"
        await respond(e, f"{'⛔' if list_type == 'black' else '✅'} [{_target_name(tgt)}](tg://user?id={tgt.id}) {state_} {keyword}.")
    else:
        lst = db.get_ai_list(list_type)
        if not lst:
            await respond(e, f"📭 {keyword.capitalize()} пуст.")
        else:
            names = []
            for uid in lst[:50]:
                try:
                    ent = await client.get_entity(uid)
                    names.append(f"• {_target_name(ent)} (`{uid}`)")
                except Exception:
                    names.append(f"• `{uid}`")
            await respond(e, f"{'⛔' if list_type == 'black' else '✅'} **{keyword.capitalize()} ({len(lst)}):**\n\n" + "\n".join(names))
    db.bump_stat('cmds')


@client.on(events.NewMessage(pattern=r'!blacklist(?:\s+(@\w+))?$', func=owner_filter))
async def blacklist_cmd(e):
    await _list_cmd(e, 'black', 'чёрный список')


@client.on(events.NewMessage(pattern=r'!whitelist(?:\s+(@\w+))?$', func=owner_filter))
async def whitelist_cmd(e):
    await _list_cmd(e, 'white', 'белый список')


# ────────────────────────────────────────────────────────────
# КОМАНДА: квест
# ────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern=r'!quest(?:\s+(.+))?$', func=owner_filter))
async def quest_cmd(e):
    action = (e.pattern_match.group(1) or '').strip()
    chat_id = e.chat_id
    if action.lower() in ('начать', 'новая', 'старт', 'рестарт'):
        db.clear_ai_quest(chat_id)
        state_ = {"story": "", "hp": 10, "step": 0, "done": False, "title": "Приключение"}
    else:
        raw_state = db.get_ai_quest(chat_id)
        state_ = json.loads(raw_state) if raw_state else None
        if state_ is None:
            await respond(e, "ℹ️ Квест не начат. Напиши `!quest начать`.")
            db.bump_stat('cmds')
            return
    state_["step"] = state_.get("step", 0) + 1
    sysp = (
        "Ты — мастер текстового RPG-квеста в Telegram. Веди историю главами, "
        "в конце давай 2-3 варианта действия. Следи за состоянием и дальше от состояния."
    )
    user_prompt = f"Состояние квеста (JSON): {json.dumps(state_, ensure_ascii=False)}\n"
    if action.lower() in ('начать', 'новая', 'старт', 'рестарт'):
        user_prompt += "Создай новый захватывающий квест. Опиши стартовую сцену (3-5 предложений) и дай варианты действий."
    else:
        user_prompt += f"Игрок делает: «{action}». Опиши результат (3-5 предложений), последствия и новые варианты действий."
    msg = await respond(e, "🎮 Квест генерируется...")
    try:
        res, prov = await _gen(user_prompt, system=sysp)
        state_["story"] = _truncate(state_.get("story", "") + "\n" + res, 3000)
        db.set_ai_quest(chat_id, json.dumps(state_, ensure_ascii=False)[:5000])
        await msg.edit(f"🏰 **Квест · шаг {state_['step']}**\n\n{_truncate(res, 3500)}")
    except AIError as ex:
        await msg.edit(_fmt_aierr(ex))
    db.bump_stat('cmds')


# ────────────────────────────────────────────────────────────
# !helpai
# ────────────────────────────────────────────────────────────

_AI_HELP = {
    'контекст': (
        "👤 **КОНТЕКСТ И АНАЛИЗ**\n\n"
        "`!summary @ник [n]` — выжимка переписки в 3 предложениях\n"
        "`!vibe @ник` — настроение (агрессия/симпатия/вовлечённость)\n"
        "`!liar @ник` — противоречия по всей истории\n"
        "`!sync @ник [n]` — сохранить прошлую переписку в базу\n"
        "`!fact @ник [факт]` — факт о человеке · `!facts @ник` — посмотреть\n"
    ),
    'редактура': (
        "✏️ **РЕДАКТУРА**\n\n"
        "`!rewrite [стиль]` — переписать (вежливо/официально/токсично/гопник)\n"
        "`!fix [текст]` — грамматика + раскладка (eng/rus)\n"
    ),
    'проверка': (
        "🔍 **ПРОВЕРКА**\n\n"
        "`!factcheck` — проверка фактов и нестыковок\n"
        "`!args` — 3 контраргумента для спора\n"
        "`!tr [язык]` — перевод в Избранное\n"
    ),
    'медиа': (
        "🎙 **МЕДИА**\n\n"
        "`!tts [текст]` — голосовое сообщение (голос из AI_TTS_VOICE)\n"
        "`!circle` — гифка/видео → видео-кружочек (ответи, затем команда)\n"
        "`!ocr` — текст с картинки (ответи, затем команда)\n"
    ),
    'автоответчик': (
        "🤖 **АВТООТВЕТЧИК**\n\n"
        "`!auto @ник on [роль]` — включить автоответ (девушка/враг/друг/босс/кастом)\n"
        "`!auto @ник off` — выключить\n"
        "`!prompt @ник on/off` — подсказки: 3 варианта ответа в Избранное\n"
        "`!delay [сек]` — пауза перед ответом ИИ\n"
        "`!blacklist @ник` / `!whitelist @ник` — доступ\n"
    ),
    'квест': (
        "🏰 **КВЕСТ**\n\n"
        "`!quest начать` — новый квест\n"
        "`!quest [действие]` — сделать ход\n"
    ),
    'реакции': (
        "🎯 **РЕАКЦИИ (анализ в Избранное)**\n\n"
        "Поставь реакцию на входящее сообщение в ЛС:\n"
        "🤔 — скрытый смысл и манипуляции\n"
        "🤬 — токсичность и «красные флаги»\n"
        "⚡ — TL;DR в 1-2 предложениях\n"
        "🤓 — перевод на русский/английский\n"
        "🤡 — саркастичный роаст\n"
    ),
}


@client.on(events.NewMessage(pattern=r'!helpai(?:\s+(.+))?$', func=owner_filter))
async def helpai_cmd(e):
    arg = (e.pattern_match.group(1) or '').strip().lower()
    if arg == 'all':
        msg_ = "\n\n".join(t for t in _AI_HELP.values())
        msg_ = "🤖 **AI ПОМОЩНИК** — все команды\n\n" + msg_
        if len(msg_) > 4096:
            msg_ = msg_[:4080] + "\n\n⚠️ Обрезано (лимит 4096)"
        await respond(e, msg_)
    elif arg in _AI_HELP:
        await respond(e, _AI_HELP[arg])
    elif arg:
        await respond(e, "❌ Раздел не найден. Доступно: `!helpai контекст`, `редактура`, `проверка`, `медиа`, `автоответчик`, `квест`, `реакции`, `all`.")
    else:
        cats = "\n".join(f"• `{c}` → `!helpai {c}`" for c in _AI_HELP)
        await respond(e, f"🤖 **AI-ПОМОЩНИК**\n\nВыбери раздел:\n{cats}\n\nВсе команды: `!helpai all`")
    db.bump_stat('cmds')