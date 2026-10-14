"""Анти-удаление сообщений в личных чатах.

Работает всегда при запуске бота. Отключить можно переменной окружения
ANTIDEL=0 (значения 0/false/no/off).

Как работает: Telegram не передаёт содержимое удалённых сообщений, поэтому
входящие ЛС кэшируются как приходят (текст — в БД, медиа — файлом на диск).
Когда собеседник удаляет сообщение, копия пересылается в «Избранное» с
указанием отправителя, после чего запись и файл удаляются из кэша.
Невоспетребованное медиа автоматически чистится через 24 часа.
"""

import asyncio
import difflib
import os
import time

from telethon import events

from client import client
from config import MEDIA_DIR, OWNER_ID, logger
from core import db


ENABLED = os.environ.get('ANTIDEL', '1').strip().lower() not in ('0', 'false', 'no', 'off')
CACHE_DIR = os.path.join(MEDIA_DIR, 'antidel')
RETENTION_SECONDS = 24 * 3600
CLEANUP_INTERVAL = 30 * 60

_cleanup_started = False


# ── инфраструктура ──────────────────────────────────────────

def _ensure_cleanup():
    global _cleanup_started
    if _cleanup_started or not ENABLED:
        return
    _cleanup_started = True
    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            paths = db.purge_cache(int(time.time()) - RETENTION_SECONDS)
            for p in paths:
                _remove_file(p)
            if paths:
                logger.info(f"antidel: очищено {len(paths)} устаревших медиа")
        except asyncio.CancelledError:
            return
        except Exception as ex:
            logger.warning(f"antidel cleanup: {ex}")


def _remove_file(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as ex:
        logger.debug(f"antidel remove {path}: {ex}")


def _esc(s):
    if not s:
        return ''
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _kind(msg):
    if msg.sticker:
        return 'sticker'
    if msg.photo:
        return 'photo'
    if msg.video_note:
        return 'round'
    if msg.voice:
        return 'voice'
    if msg.audio:
        return 'audio'
    if msg.video:
        return 'video'
    if msg.gif:
        return 'gif'
    if msg.document:
        return 'document'
    if msg.poll:
        return 'poll'
    if msg.raw_text:
        return 'text'
    return 'other'


def _display_name(sender):
    if not sender:
        return 'неизвестный'
    name = ' '.join(
        x for x in (getattr(sender, 'first_name', '') or '',
                    getattr(sender, 'last_name', '') or '') if x
    ).strip()
    if not name:
        name = (getattr(sender, 'username', '') or
                getattr(sender, 'title', '') or
                str(getattr(sender, 'id', '?')))
    uname = getattr(sender, 'username', None)
    if uname:
        return f"{name} (@{uname})"
    return f"{name} (id{getattr(sender, 'id', '?')})"


def _header(emoji, action, name, ts):
    when = time.strftime('%d.%m %H:%M', time.localtime(ts or time.time()))
    return f"{emoji} <b>{action}</b> — {_esc(name)}\n🕒 {when}"


def _suffix(kind):
    return {
        'photo': '.jpg', 'video': '.mp4', 'round': '.mp4', 'voice': '.ogg',
        'audio': '.mp3', 'gif': '.mp4', 'sticker': '.webp', 'document': '.bin',
    }.get(kind, '.bin')


async def _download(msg, chat_id):
    os.makedirs(CACHE_DIR, exist_ok=True)
    ext = ''
    if msg.file and getattr(msg.file, 'ext', None):
        ext = msg.file.ext
    if not ext:
        ext = _suffix(_kind(msg))
    path = os.path.join(CACHE_DIR, f"{chat_id}_{msg.id}{ext}")
    try:
        result = await msg.download_media(file=path)
        return result or None
    except Exception as ex:
        logger.warning(f"antidel download {msg.id}: {ex}")
        return None


def _render_diff(old, new):
    if not old:
        return f"<blockquote>{_esc(new)}</blockquote>"
    ol = old.splitlines() or [old]
    nl = new.splitlines() or [new]
    lines = []
    for d in difflib.unified_diff(ol, nl, lineterm='', n=1):
        if d.startswith(('---', '+++', '@@')):
            continue
        if d.startswith('+'):
            lines.append('🟢 ' + _esc(d[1:]))
        elif d.startswith('-'):
            lines.append('🔴 ' + _esc(d[1:]))
        elif d.startswith(' '):
            lines.append('   ' + _esc(d[1:]))
    body = "\n".join(lines) if lines else _esc(new)
    return f"<blockquote>{body}</blockquote>"


async def _send_text(html):
    try:
        await client.send_message('me', html, parse_mode='html')
    except Exception:
        plain = (html.replace('<b>', '').replace('</b>', '')
                     .replace('<blockquote>', '> ').replace('</blockquote>', ''))
        await client.send_message('me', plain)


async def _send_media(path, caption):
    try:
        await client.send_file('me', path, caption=caption, parse_mode='html')
    except Exception as ex:
        logger.warning(f"antidel send_file: {ex}")
        await _send_text(caption + "\n\n⚠️ Медиа не удалось переотправить.")


async def _deliver(kind, text, file_path, header):
    if file_path and os.path.exists(file_path) and kind not in ('text', 'other', 'poll'):
        caption = header
        if text:
            caption += f"\n\n<blockquote>{_esc(text)}</blockquote>"
        await _send_media(file_path, caption)
    else:
        body = text or f"[{kind}]"
        await _send_text(header + f"\n\n<blockquote>{_esc(body)}</blockquote>")


# ── обработчики ─────────────────────────────────────────────

@client.on(events.NewMessage(func=lambda e: e.is_private))
async def antidel_on_message(event):
    if not ENABLED or event.out:
        return
    msg = event.message
    if msg is None or msg.action is not None:
        return
    sender_id = event.sender_id or 0
    if not sender_id or sender_id == OWNER_ID:
        return
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None
    if sender is None or getattr(sender, 'bot', False):
        return

    kind = _kind(msg)
    if kind == 'other':
        return

    text = (msg.raw_text or '').strip()
    name = _display_name(sender)
    chat_id = event.chat_id
    ts = msg.date.timestamp() if msg.date else time.time()

    ttl = getattr(msg.media, 'ttl_seconds', None) if msg.media is not None else None
    if ttl:
        file_path = await _download(msg, chat_id) if kind not in ('text', 'poll') else None
        await _deliver(kind, text, file_path, _header('🔥', 'Одноразовое медиа от', name, ts))
        _remove_file(file_path)
        return

    file_path = None
    if kind not in ('text', 'poll'):
        file_path = await _download(msg, chat_id)

    try:
        db.cache_msg(chat_id, msg.id, sender_id, name, kind, text, file_path, ts=int(ts))
    except Exception as ex:
        logger.warning(f"antidel cache: {ex}")
        _remove_file(file_path)
        return
    _ensure_cleanup()


@client.on(events.MessageDeleted)
async def antidel_on_deleted(event):
    if not ENABLED:
        return
    for msg_id in event.deleted_ids or ():
        rec = db.get_cached_by_msgid(msg_id)
        if not rec:
            continue
        # Если событие пришло из канала/супергруппы (peer известен) и не совпало
        # с чатом записи — это не наш ЛС, пропускаем.
        if event.chat_id is not None and event.chat_id != rec['chat_id']:
            continue
        if rec['chat_id'] <= 0:
            db.del_cached_msg(rec['chat_id'], msg_id)
            continue
        header = _header('🗑', 'Удалённое сообщение от', rec['sender_name'], rec['date'])
        await _deliver(rec['kind'], rec['text'], rec['file_path'], header)
        _remove_file(rec['file_path'])
        db.del_cached_msg(rec['chat_id'], msg_id)


@client.on(events.MessageEdited(func=lambda e: e.is_private))
async def antidel_on_edited(event):
    if not ENABLED or event.out:
        return
    msg = event.message
    if msg is None:
        return
    rec = db.get_cached_msg(event.chat_id, msg.id)
    if not rec:
        return
    old = rec['text'] or ''
    new = (msg.raw_text or '').strip()
    if old == new:
        return
    header = _header('✏️', 'Изменённое сообщение от', rec['sender_name'], rec['date'])
    await _send_text(header + "\n\n" + _render_diff(old, new))
    db.update_cached_text(event.chat_id, msg.id, new)
    _ensure_cleanup()


if ENABLED:
    logger.info("antidel: перехват удалённых сообщений в ЛС активен")
else:
    logger.info("antidel: отключён (ANTIDEL=0)")
