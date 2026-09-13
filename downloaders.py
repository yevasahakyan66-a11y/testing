import asyncio
import base64
import glob
import os
import re
import subprocess
import sys
import time
import urllib.request

import aiohttp
import yt_dlp

from config import MEDIA_DIR, logger, PROXY, PROXY_LIST
from utils import format_bytes

_proxy_list = list(PROXY_LIST)
_single_proxy = PROXY


def _proxy_candidates():
    proxies = [_p for _p in _proxy_list if _p] or ([_single_proxy] if _single_proxy else [])
    if proxies:
        return proxies + [None]
    return [None]


def _with_proxy(opts, proxy):
    if proxy:
        o = dict(opts)
        o['proxy'] = proxy
        return o
    return opts


def _dl_rotate(sync_fn):
    last_ex = None
    for proxy in _proxy_candidates():
        label = proxy or 'прямое подключение'
        try:
            return sync_fn(proxy)
        except Exception as ex:
            last_ex = ex
            logger.warning(f"Загрузка через {label} не удалась: {ex}")
            continue
    if last_ex:
        raise last_ex
    raise ValueError("Не удалось скачать: нет рабочих прокси")

_COOKIES_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'cookies.txt'))

def _ensure_cookies_from_env():
    """Cookie из переменных окружения (приоритет YOUTUBE_COOKIES > COOKIES_B64 > файл)."""
    env_content = os.environ.get('YOUTUBE_COOKIES') or ''
    if env_content.strip():
        with open(_COOKIES_PATH, 'w', encoding='utf-8') as f:
            f.write(env_content.strip() + '\n')
        logger.info("cookies.txt создан из YOUTUBE_COOKIES")
        return True
    b64 = os.environ.get('COOKIES_B64') or ''
    if b64.strip():
        try:
            decoded = base64.b64decode(b64).decode('utf-8', errors='ignore')
            with open(_COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(decoded.strip() + '\n')
            logger.info("cookies.txt создан из COOKIES_B64")
            return True
        except Exception as ex:
            logger.warning(f"Ошибка декодирования COOKIES_B64: {ex}")
    return False

_ensure_cookies_from_env()

_COOKIES_VALID = False
_AUTH_COOKIE_KEYS = ('SID', 'SSID', 'HSID', 'APISID', 'SAPISID', '__Secure-1PAPISID',
                     '__Secure-3PAPISID', '__Secure-3PSID', 'LOGIN_INFO', 'LOGIN')
if os.path.exists(_COOKIES_PATH):
    with open(_COOKIES_PATH, encoding='utf-8', errors='ignore') as f:
        content = f.read()
    if '.youtube.com' in content and ('\tTRUE\t' in content or 'cookie' in content.lower()):
        real_auth = [k for k in _AUTH_COOKIE_KEYS
                     if re.search(r'\t' + re.escape(k) + r'\t', content)]
        _COOKIES_VALID = bool(real_auth)
        lines = [l for l in content.splitlines() if l.strip() and not l.startswith('#')]
        logger.info(f"cookies.txt: {len(lines)} строк, домен youtube — "
                    f"{'OK, авторизация (' + ', '.join(real_auth[:3]) + ')' if real_auth else 'есть, но авторизующие куки НЕ найдены'}")
        if not real_auth and 'abc123' in content:
            logger.warning("cookies.txt похож на шаблон-пример из cookies.txt.example — "
                           "нужен реальный экспорт из браузера")
    else:
        logger.warning("cookies.txt существует, но похож на неверный формат (нужен Netscape)")
else:
    logger.info("cookies.txt не найден — скачивание YouTube может не работать без авторизации")
    logger.info("Задай YOUTUBE_COOKIES в .env или создай cookies.txt из cookies.txt.example")

def _update_ytdlp():
    try:
        old_ver = getattr(yt_dlp.version, '__version__', '???')
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--upgrade', '--quiet', 'yt-dlp'],
            capture_output=True, timeout=60
        )
        result = subprocess.run(
            [sys.executable, '-m', 'yt_dlp', '--version'],
            capture_output=True, text=True, timeout=30
        )
        new_ver = result.stdout.strip() or '???'
        if old_ver != new_ver:
            logger.info(f"yt-dlp обновлён: {old_ver} → {new_ver}")
        else:
            logger.info(f"yt-dlp {old_ver} (актуальная)")
    except Exception as ex:
        logger.warning(f"yt-dlp auto-update failed: {ex}")

_update_ytdlp()

try:
    logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")
except Exception:
    logger.info("yt-dlp version: unknown")

_JS_RUNTIMES = {}
for _rt in ('node', 'deno'):
    try:
        r = subprocess.run([_rt, '--version'], capture_output=True, check=False, timeout=5)
        if r.returncode == 0:
            _JS_RUNTIMES[_rt] = {}
            logger.info(f"JS runtime: {_rt} найден")
    except (FileNotFoundError, OSError):
        pass
if not _JS_RUNTIMES:
    logger.warning("JS runtime не найден (node/deno) — установи для лучшей совместимости с YouTube")
    logger.warning("  apt install nodejs  или  curl -fsSL https://deno.land/install.sh | sh")

def _find_ffmpeg():
    """Найти ffmpeg по имени или абсолютным путям (Railway/nix-лого может не быть в PATH)."""
    try:
        import shutil
        p = shutil.which('ffmpeg')
        if p:
            return p
    except Exception:
        pass
    for cand in (
        '/usr/bin/ffmpeg', '/bin/ffmpeg', '/sbin/ffmpeg',
        '/usr/local/bin/ffmpeg', '/opt/ffmpeg/bin/ffmpeg',
        '/opt/homebrew/bin/ffmpeg',
    ):
        if os.path.exists(cand):
            return cand
    candidates = sorted(glob.glob('/nix/store/*/bin/ffmpeg'), key=os.path.getmtime, reverse=True)
    if candidates:
        return candidates[0]
    return None


_FFMPEG_LOCAL_DIR = os.path.abspath(os.path.join(MEDIA_DIR, 'ffmpeg'))
_FFMPEG_LOCAL_BIN = os.path.join(_FFMPEG_LOCAL_DIR, 'ffmpeg')
_FFPROBE_LOCAL_BIN = os.path.join(_FFMPEG_LOCAL_DIR, 'ffprobe')

def _ffmpeg_static_url():
    """URL статической сборки ffmpeg под текущую платформу (автопредустановка)."""
    import platform
    sysname = (platform.system() or '').lower()
    arch = (platform.machine() or '').lower()
    if sysname in ('linux', ''):
        build = 'amd64' if arch in ('x86_64', 'amd64', '') else 'arm64'
        return (f"https://johnvansickle.com/ffmpeg/releases/"
                f"ffmpeg-release-{build}-static.tar.xz"), 'tar.xz'
    if sysname == 'darwin':
        return ("https://evermeet.cx/ffmpeg/get/mac64/zip"), 'zip'
    if sysname in ('windows', 'win32'):
        return ("https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
                "ffmpeg-master-latest-win64-gpl.zip"), 'zip'
    return None, None

def _download_static_ffmpeg():
    """Предустановка ffmpeg: качаем статический бинарник в MEDIA_DIR/ffmpeg, если система пуста."""
    if os.path.exists(_FFMPEG_LOCAL_BIN):
        return _FFMPEG_LOCAL_BIN
    url, kind = _ffmpeg_static_url()
    if not url:
        return None
    tmp = os.path.join(MEDIA_DIR, f'_ffmpeg_dl.{kind}')
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        logger.info(f"ffmpeg не найден в системе — предустановка: {url}")
        # через yt-dlp? нет, urllib достаточно (Railway has outbound)
        subprocess.run(
            [_PY_EXE, "-m", "pip", "install", "--quiet", "--upgrade", "static-ffmpeg"],
            capture_output=True, timeout=120,
        )
    except Exception as ex:
        logger.warning(f"pip static-ffmpeg failed: {ex}")
    try:
        import static_ffmpeg
        paths = static_ffmpeg.add_paths()  # распакует бинарник в env
        if paths:
            bin_ = paths[0]  # dir с ffmpeg/ffprobe
            first = os.path.join(bin_, 'ffmpeg')
            if not os.path.exists(first):
                first = os.path.join(bin_, 'ffmpeg.exe')
            if os.path.exists(first):
                os.makedirs(_FFMPEG_LOCAL_DIR, exist_ok=True)
                shutil.copy2(first, _FFMPEG_LOCAL_BIN)
                p = os.path.join(bin_, 'ffprobe')
                if os.path.exists(p):
                    shutil.copy2(p, _FFMPEG_LOCAL_BIN.replace('ffmpeg', 'ffprobe') if False else _FFPROBE_LOCAL_BIN)
                logger.info(f"ffmpeg предустановлен: {_FFMPEG_LOCAL_BIN}")
                return _FFMPEG_LOCAL_BIN
    except Exception as ex:
        logger.warning(f"static-ffmpeg extraction failed: {ex}")
    # фоллбэк — ручная загрузка архивов
    try:
        if kind == 'tar.xz':
            import tarfile
            subprocess.run([_PY_EXE, "-m", "pip", "install", "--quiet", "lzma", "xz"] , capture_output=True, timeout=60) if False else None
            # urllib может не знать xz; пробуем через curl/zstd уже в yt-dlp — но проще pip-уже сделано выше
            return _extract_tar_xz(url, tmp)
        elif kind == 'zip':
            import zipfile
            urllib.request.urlretrieve(url, tmp)
            return _extract_zip(tmp)
    except Exception as ex:
        logger.warning(f"ffmpeg manual download failed: {ex}")
    return None

def _extract_tar_xz(url, tmp):
    subprocess.run([_PY_EXE, "-m", "pip", "install", "--quiet", "py7zr"], capture_output=True, timeout=60)
    import urllib.request
    urllib.request.urlretrieve(url, tmp)
    import tarfile
    with tarfile.open(tmp, 'r') as tf:
        os.makedirs(_FFMPEG_LOCAL_DIR, exist_ok=True)
        tf.extractall(_FFMPEG_LOCAL_DIR, filter='data')
    for root, _, files in os.walk(_FFMPEG_LOCAL_DIR):
        if 'ffmpeg' in files and 'ffprobe' in files:
            b = os.path.join(root, 'ffmpeg')
            os.chmod(b, 0o755)
            shutil.move(b, _FFMPEG_LOCAL_BIN)
            shutil.move(os.path.join(root, 'ffprobe'), _FFPROBE_LOCAL_BIN)
            return _FFMPEG_LOCAL_BIN
    return None

def _extract_zip(tmp):
    import zipfile
    with zipfile.ZipFile(tmp) as zf:
        os.makedirs(_FFMPEG_LOCAL_DIR, exist_ok=True)
        zf.extractall(_FFMPEG_LOCAL_DIR)
    for root, _, files in os.walk(_FFMPEG_LOCAL_DIR):
        if 'ffmpeg.exe' in files:
            shutil.move(os.path.join(root, 'ffmpeg.exe'), _FFMPEG_LOCAL_BIN)
        pattern = 'ffprobe.exe'
        if pattern in files:
            shutil.move(os.path.join(root, pattern), _FFPROBE_LOCAL_BIN)
    if os.path.exists(_FFMPEG_LOCAL_BIN):
        return _FFMPEG_LOCAL_BIN
    return None

if not _find_ffmpeg():
    _downloaded = _download_static_ffmpeg()
    _FFMPEG_LOCAL_BIN = _downloaded or _FFMPEG_LOCAL_BIN
_FFMPEG_PATH = _find_ffmpeg() or (os.path.exists(_FFMPEG_LOCAL_BIN) and _FFMPEG_LOCAL_BIN) or None
_HAS_FFMPEG = bool(_FFMPEG_PATH)
if _HAS_FFMPEG:
    try:
        subprocess.run([_FFMPEG_PATH, '-version'], capture_output=True, check=True, timeout=10)
    except Exception:
        _FFMPEG_PATH = None
        _HAS_FFMPEG = False
if _HAS_FFMPEG:
    logger.info(f"ffmpeg: {_FFMPEG_PATH}")
else:
    logger.info("ffmpeg не найден — конвертация видео/аудио недоступна")

_YT_DL_OPTS = {
    'outtmpl': os.path.join(MEDIA_DIR, '%(id)s.%(ext)s'),
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'playlist_items': '1',
    'extractor_retries': 5,
    'fragment_retries': 5,
    'retry_sleep': lambda n: 5 + n * 3,
    'throttledratelimit': 100000,
    'sleep_interval_requests': 2,
    'js_runtimes': _JS_RUNTIMES,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    },
}
if _FFMPEG_PATH:
    _YT_DL_OPTS['ffmpeg_location'] = os.path.dirname(_FFMPEG_PATH)

_AUTH_WALL_HINTS = (
    re.compile(r'sign\s?in\s?to\s?confirm', re.I),
    re.compile(r'confirm you\'?re not a bot', re.I),
    re.compile(r'requires a Google account', re.I),
    re.compile(r'log\s*in\s*(is|be|to)', re.I),
    re.compile(r'required to purchase', re.I),
    re.compile(r'Insufficient permissions', re.I),
    re.compile(r'private video', re.I),
    re.compile(r'HTTP Error 403', re.I),
    re.compile(r'Video unavailable', re.I),
    re.compile(r'авторизац', re.I),
    re.compile(r'требуется вход', re.I),
    re.compile(r'не прошли проверку', re.I),
    re.compile(r'блокирует (?:IP|сервер)', re.I),
)


def _is_auth_wall(ex):
    text = str(ex)
    return any(p.search(text) for p in _AUTH_WALL_HINTS)

_YT_CLIENT_FALLBACKS = [
    {'youtube': {'player_client': ['android']}},
    {'youtube': {'player_client': ['mweb'], 'player_skip': ['hls', 'js']}},
]

_max_file_size = 1500


def set_max_file_size(mb: int):
    global _max_file_size
    _max_file_size = mb


_INVIDIOUS_INSTANCES = [
    'inv.nadeko.net',
    'yewtu.be',
    'invidious.snopyta.org',
    'inv.vern.cc',
    'vid.puffyan.us',
]


def _yt_id(url):
    m = re.search(r'(?:v=|youtu\.be/|list=)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


async def _try_invidious(url, mode):
    video_id = _yt_id(url)
    if not video_id:
        return None
    os.makedirs(MEDIA_DIR, exist_ok=True)
    for instance in _INVIDIOUS_INSTANCES:
        try:
            api_url = f"https://{instance}/api/v1/videos/{video_id}"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(api_url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
            title = data.get('title', video_id) or video_id
            combined = data.get('formatStreams') or []
            adaptive = data.get('adaptiveFormats') or []
            if mode == 'audio':
                picks = adaptive + combined
                best = max((f for f in picks if f.get('url') and (f.get('bitrate') or 0)), key=lambda f: f['bitrate'], default=None)
            else:
                best = max((f for f in combined if f.get('url') and (f.get('height') or 0)), key=lambda f: f['height'], default=None)
                if not best and adaptive:
                    best = max((f for f in adaptive if f.get('url') and (f.get('height') or 0)), key=lambda f: f['height'], default=None)
            if not best:
                continue
            ext = best.get('container', 'mp4')
            path = os.path.join(MEDIA_DIR, f"{video_id}_invidious.{ext}")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.get(best['url']) as resp:
                    if resp.status != 200:
                        continue
                    with open(path, 'wb') as f:
                        while True:
                            chunk = await resp.content.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                logger.info(f"Invidious download OK: {title} ({instance})")
                return path
        except Exception as ex:
            logger.warning(f"Invidious {instance} failed: {ex}")
            continue
    return None


def _resolve_yt_path(ydl, info):
    filepath = None
    if info.get('requested_downloads'):
        filepath = info['requested_downloads'][0].get('filepath')
    if not filepath or not os.path.exists(filepath or ''):
        filepath = ydl.prepare_filename(info)
    if filepath and os.path.exists(filepath):
        return filepath
    video_id = info.get('id')
    if video_id:
        pattern = os.path.join(MEDIA_DIR, f'{video_id}.*')
        matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if matches:
            return matches[0]
    raise ValueError("Файл не найден после загрузки (возможно, проблема с ffmpeg пост-обработкой)")


def _probe_formats(url, opts):
    probe_opts = dict(opts)
    probe_opts.pop('format', None)
    probe_opts['skip_download'] = True
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            logger.info(f"YouTube probe: id={info.get('id', '?')}, "
                        f"title=\"{info.get('title', '?')[:80]}\", "
                        f"channel={info.get('channel', '?')}")
            if info.get('title') or info.get('entries'):
                return info
            logger.warning("Probe вернул пустой результат (возможно, требуется авторизация)")
    except yt_dlp.utils.DownloadError as ex:
        logger.warning(f"Probe failed: {ex}")
    except Exception as ex:
        logger.warning(f"Probe unexpected: {ex}")
    _dl_diag(url)
    return None


def _dl_diag(url):
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'yt_dlp', '--verbose', '--flat-playlist', '-J', '--no-check-certificates', url],
            capture_output=True, text=True, timeout=60
        )
        logger.info(f"yt-dlp CLI stdout: {result.stdout[:500]}")
        logger.info(f"yt-dlp CLI stderr: {result.stderr[:500]}")
    except Exception as ex:
        logger.error(f"yt-dlp CLI diag failed: {ex}")


def _cli_download(url, output_path):
    cmd = [
        sys.executable, '-m', 'yt_dlp',
        '-o', output_path,
        '--no-playlist',
        '--no-check-certificates',
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            matches = sorted(glob.glob(output_path.replace('%(ext)s', '*')), key=os.path.getmtime, reverse=True)
            if matches:
                return matches[0]
            stderr_lower = result.stderr.lower()
            m = re.search(r'\[download\]\s+(.+?)\s+has already been downloaded', result.stderr, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        logger.error(f"CLI download failed (rc={result.returncode}): {result.stderr[:3000]}")
        return None
    except subprocess.TimeoutExpired:
        logger.error("CLI download timed out")
        return None
    except Exception as ex:
        logger.error(f"CLI download error: {ex}")
        return None


def _auth_final_error(last_ex):
    hint = (
        "YouTube требует авторизацию и блокирует сервер. Варианты:\n"
        "  1) Обнови cookies (YOUTUBE_COOKIES в .env, формат Netscape)\n"
        "  2) Укажи рабочий прокси: PROXY=http://лог:пароль@хост:порт или\n"
        "     PROXY_LIST=ip1,ip2,socks5://... (см. .env.example)\n"
        "  3) Смени хостинг / используй домашний IP"
    )
    return f"YouTube: {last_ex}\n\n🔑 {hint}"


def _pick_format_and_download(url, opts, quality, is_audio):
    last_ex = None

    normal_attempts = [opts]
    if _COOKIES_VALID:
        with_cookie = dict(opts)
        with_cookie['cookiefile'] = _COOKIES_PATH
        normal_attempts.append(with_cookie)

    for o in normal_attempts:
        try:
            return _download_with_opts(url, o, quality, is_audio)
        except (yt_dlp.utils.DownloadError, ValueError) as ex:
            last_ex = ex
            logger.warning(f"Попытка не удалась: {ex}")
            if not _is_auth_wall(ex):
                raise

    logger.warning("Стена авторизации: пробую обходные клиенты YouTube (android/mweb)...")
    for fallback in _YT_CLIENT_FALLBACKS:
        client_attempts = []
        o = dict(opts)
        o['extractor_args'] = fallback
        o['socket_timeout'] = 45
        o['extractor_retries'] = 10
        o['fragment_retries'] = 10
        client_attempts.append(o)
        if _COOKIES_VALID:
            oc = dict(o)
            oc['cookiefile'] = _COOKIES_PATH
            client_attempts.append(oc)
        for oa in client_attempts:
            try:
                result = _download_with_opts(url, oa, quality, is_audio)
                logger.info(f"Обходной клиент {fallback['youtube']['player_client']} сработал")
                return result
            except (yt_dlp.utils.DownloadError, ValueError) as ex:
                last_ex = ex
                logger.warning(f"Обходной клиент {fallback['youtube']['player_client']} не сработал: {ex}")

    raise ValueError(_auth_final_error(last_ex))


def _download_with_opts(url, opts, quality, is_audio):
    os.makedirs(MEDIA_DIR, exist_ok=True)

    if not _HAS_FFMPEG:
        probe = _probe_formats(url, opts)
        if probe is None:
            logger.warning("Python API probe failed, trying CLI fallback...")
            ext = 'mp3' if is_audio else 'mp4'
            cli_path = os.path.join(MEDIA_DIR, f'%(id)s.{ext}')
            cli_result = _cli_download(url, cli_path)
            if cli_result:
                return cli_result
            if not _JS_RUNTIMES:
                hint = (
                    "\n\n📦 Установи JS-рантайм для yt-dlp и перезапусти:\n"
                    "  apt install nodejs\n"
                    "  # или: curl -fsSL https://deno.land/install.sh | sh"
                )
            else:
                hint = (
                    "\n\n🔑 YouTube всё ещё блокирует IP. Тогда нужны cookies:\n"
                    "  cookies.txt.example → cookies.txt, экспорт из браузера"
                )
            raise ValueError("YouTube блокирует сервер." + hint)

    if _HAS_FFMPEG and is_audio:
        opts['format'] = 'ba/b'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif _HAS_FFMPEG and not is_audio:
        opts['format'] = 'bv*+ba/b'
        if quality:
            opts['format'] = f'bv*[height<={quality}]+ba/b[height<={quality}]'
        opts['merge_output_format'] = 'mp4'
    else:
        formats_to_try = ['ba', 'b'] if is_audio else ['b', 'bv']
        last_ex = None
        for fmt in formats_to_try:
            opts['format'] = fmt
            try:
                logger.info(f'yt-dlp opts: format={opts.get("format")}, ffmpeg={_HAS_FFMPEG}')
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return _resolve_yt_path(ydl, info)
            except yt_dlp.utils.DownloadError as ex:
                last_ex = ex
                continue
        raise ValueError(f"YouTube: {last_ex}")

    logger.info(f'yt-dlp opts: format={opts.get("format")}, ffmpeg={_HAS_FFMPEG}')
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return _resolve_yt_path(ydl, info)


async def _download_yt_video(url, quality=None):
    def _dl(proxy=None):
        try:
            return _pick_format_and_download(url, _with_proxy(dict(_YT_DL_OPTS), proxy), quality, is_audio=False)
        except yt_dlp.utils.DownloadError as ex:
            raise ValueError(f"YouTube: {ex}")
        except ValueError:
            raise
        except Exception as ex:
            logger.error(f"yt-dlp unexpected error: {ex}", exc_info=True)
            raise ValueError(f"YouTube: {ex}")

    return await asyncio.to_thread(_dl_rotate, _dl)


async def _download_yt_audio(url):
    def _dl(proxy=None):
        try:
            return _pick_format_and_download(url, _with_proxy(dict(_YT_DL_OPTS), proxy), quality=None, is_audio=True)
        except yt_dlp.utils.DownloadError as ex:
            raise ValueError(f"YouTube: {ex}")
        except ValueError:
            raise
        except Exception as ex:
            logger.error(f"yt-dlp unexpected error: {ex}", exc_info=True)
            raise ValueError(f"YouTube: {ex}")

    return await asyncio.to_thread(_dl_rotate, _dl)


async def _download_instagram_video(url):
    def _dl(proxy=None):
        try:
            opts = _with_proxy(dict(_YT_DL_OPTS), proxy)
            opts['format'] = 'b'
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return _resolve_yt_path(ydl, info)
        except yt_dlp.utils.DownloadError as ex:
            raise ValueError(f"Instagram: {ex}")
        except ValueError:
            raise
        except Exception as ex:
            logger.error(f"instagram unexpected error: {ex}", exc_info=True)
            raise ValueError(f"Instagram: {ex}")

    return await asyncio.to_thread(_dl_rotate, _dl)


async def _download_tiktok_video(url):
    def _dl(proxy=None):
        opts = _with_proxy(dict(_YT_DL_OPTS), proxy)
        opts['format'] = 'b'
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = _resolve_yt_path(ydl, info)
                if fp:
                    ext = os.path.splitext(fp)[1].lower()
                    if ext not in ('.m4a', '.mp3', '.aac', '.ogg'):
                        return fp
                    logger.info(f"TikTok: видео нет, только аудио ({fp}) — пробую картинки")
                    return None
        except yt_dlp.utils.DownloadError as ex:
            raise ValueError(f"TikTok: {ex}")
        except Exception as ex:
            logger.warning(f"TikTok video error: {ex}")
        return None

    try:
        fp = await asyncio.to_thread(_dl_rotate, _dl)
    except ValueError:
        fp = None
    if fp:
        return fp

    images = await _scrape_tiktok_images(url)
    if images:
        return images
    raise ValueError("TikTok: видео или карточки не доступны (IP может быть заблокирован)")


async def _scrape_tiktok_images(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'}

    working = None
    html = None
    for proxy in [p for p in _proxy_candidates() if p] or [None]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
                kwargs = {'headers': headers, 'allow_redirects': True}
                if proxy:
                    kwargs['proxy'] = proxy
                async with s.get(url, **kwargs) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text(errors='ignore')
                    working = proxy
                    break
        except Exception as ex:
            logger.warning(f"TikTok page fetch через {proxy or 'напрямую'} не удалась: {ex}")
            continue
    if html is None:
        return None

    candidates = []
    m = re.search(r'<meta[^>]+(?:property|name)="og:image"[^>]+content="([^"]+)"', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="og:image"', html, re.I)
    if m:
        candidates.append(m.group(1).replace('&amp;', '&'))
    for s in re.findall(r'data-e2e="slide_img"[^>]*src="([^"]+)"', html):
        candidates.append(s.replace('&amp;', '&'))
    for s in re.findall(r'<img[^>]+src="([^"]+)"', html):
        candidates.append(s.replace('&amp;', '&'))

    seen, urls = set(), []
    for u in candidates:
        u = u.rstrip(')')
        if not u.startswith('http') or u in seen:
            continue
        seen.add(u)
        low = u.lower()
        if 'tiktokcdn' in low or 'image' in low or low.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            urls.append(u)

    if not urls:
        return None

    os.makedirs(MEDIA_DIR, exist_ok=True)
    paths = []
    for i, u in enumerate(urls[:12], 1):
        ext = os.path.splitext(u.split('?')[0])[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
            ext = '.jpg'
        path = os.path.join(MEDIA_DIR, f'tiktok_{int(time.time() * 1000)}_{i}{ext}')
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s2:
                kwargs = {**headers, 'Referer': 'https://www.tiktok.com/'}
                if working:
                    kwargs['proxy'] = working
                async with s2.get(u, **kwargs) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
            if data:
                with open(path, 'wb') as f:
                    f.write(data)
                if os.path.getsize(path) > 0:
                    paths.append(path)
        except Exception as ex:
            logger.warning(f"TikTok image {i} failed: {ex}")
            continue
    return paths or None


_GEN_OPTS = {
    'outtmpl': os.path.join(MEDIA_DIR, '%(id)s.%(ext)s'),
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'playlist_items': '1',
    'extractor_retries': 3,
    'fragment_retries': 3,
    'retry_sleep': lambda n: 5 + n * 2,
    'sleep_interval_requests': 1,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    },
}
if _FFMPEG_PATH:
    _GEN_OPTS['ffmpeg_location'] = os.path.dirname(_FFMPEG_PATH)


def _is_direct_media(url):
    return bool(re.search(r'\.(mp4|webm|mkv|avi|mov|flv|wmv|mp3|aac|ogg|wav|m4a)($|\?)', url.lower()))


def _find_m3u8_in_html(html):
    urls = re.findall(r'(https?://[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)', html)
    if not urls:
        urls = re.findall(r'["\']([^"\']+\.m3u8[^"\']*)["\']', html)
    return urls


def _resolve_url(base, uri):
    if uri.startswith('http://') or uri.startswith('https://'):
        return uri
    from urllib.parse import urljoin
    return urljoin(base, uri)


async def _download_m3u8_video(m3u8_url, output_path):
    import m3u8
    from urllib.parse import urljoin
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(m3u8_url) as resp:
                if resp.status != 200:
                    return None
                content = await resp.text()
        playlist = m3u8.loads(content)
        seg_urls = []
        if playlist.is_variant:
            top = max(playlist.playlists, key=lambda p: p.stream_info.resolution[1] if p.stream_info.resolution else 0)
            variant_url = _resolve_url(m3u8_url, top.uri)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.get(variant_url) as resp:
                    if resp.status != 200:
                        return None
                    content = await resp.text()
            playlist = m3u8.loads(content)
        for seg in playlist.segments:
            uri = _resolve_url(m3u8_url, seg.uri)
            seg_urls.append(uri)
        if not seg_urls:
            return None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            with open(output_path, 'wb') as f:
                for seg_url in seg_urls:
                    try:
                        async with s.get(seg_url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                f.write(data)
                    except Exception:
                        continue
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as ex:
        logger.warning(f"m3u8 download failed: {ex}")
    return None


async def _download_generic(url, mode, event_edit_func):
    await event_edit_func("🌐 Пробую yt-dlp...")
    try:
        opts = dict(_GEN_OPTS)
        if mode == 'audio':
            if _HAS_FFMPEG:
                opts['format'] = 'ba/b'
                opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
            else:
                opts['format'] = 'ba'
        filename = await asyncio.to_thread(lambda: _dl_generic_sync(url, opts))
        if filename:
            return filename
    except Exception as ex:
        logger.warning(f"yt-dlp generic failed: {ex}")

    if _is_direct_media(url):
        await event_edit_func("📥 Скачиваю напрямую...")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
                async with s.get(url) as resp:
                    if resp.status == 200:
                        ext = os.path.splitext(url.split('?')[0])[1] or '.mp4'
                        path = os.path.join(MEDIA_DIR, f'direct_{int(time.time())}{ext}')
                        with open(path, 'wb') as f:
                            while True:
                                chunk = await resp.content.read(65536)
                                if not chunk:
                                    break
                                f.write(chunk)
                        if os.path.getsize(path) > 0:
                            return path
        except Exception as ex:
            logger.warning(f"Direct download failed: {ex}")

    await event_edit_func("🖼 Пробую скачать как картинку...")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    og_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html, re.IGNORECASE)
                    if og_img:
                        img_url = og_img.group(1).replace('&amp;', '&')
                        ext = os.path.splitext(img_url.split('?')[0])[1] or '.jpg'
                        path = os.path.join(MEDIA_DIR, f'img_{int(time.time())}{ext}')
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s2:
                            async with s2.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    with open(path, 'wb') as f:
                                        async for chunk in img_resp.content.iter_chunked(65536):
                                            f.write(chunk)
                                    if os.path.getsize(path) > 0:
                                        return path
    except Exception as ex:
        logger.warning(f"Image fallback failed: {ex}")

    await event_edit_func("🔍 Ищу m3u8...")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    m3u8_urls = _find_m3u8_in_html(html)
                    for m3u8_url in m3u8_urls:
                        path = os.path.join(MEDIA_DIR, f'm3u8_{int(time.time())}.mp4')
                        result = await _download_m3u8_video(m3u8_url, path)
                        if result:
                            return result
    except Exception as ex:
        logger.warning(f"m3u8 search failed: {ex}")

    return None


def _dl_generic_sync(url, opts):
    last_ex = None
    for fmt_override in [None, 'b', 'best']:
        try:
            if fmt_override:
                opts['format'] = fmt_override
            elif 'format' in opts:
                del opts['format']
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = None
                if info.get('requested_downloads'):
                    filepath = info['requested_downloads'][0].get('filepath')
                if not filepath or not os.path.exists(filepath or ''):
                    filepath = ydl.prepare_filename(info)
                if filepath and os.path.exists(filepath):
                    return filepath
                video_id = info.get('id')
                if video_id:
                    matches = sorted(glob.glob(os.path.join(MEDIA_DIR, f'{video_id}.*')), key=os.path.getmtime, reverse=True)
                    if matches:
                        return matches[0]
        except yt_dlp.utils.DownloadError as ex:
            last_ex = ex
            if 'No video formats' in str(ex):
                continue
            raise
    last_ex_str = str(last_ex) if last_ex else ''

    if 'No video formats' in last_ex_str:
        try:
            with yt_dlp.YoutubeDL({**opts, 'skip_download': True, 'quiet': True, 'no_warnings': True, 'format': None}) as ydl:
                info = ydl.extract_info(url, download=False)
            thumb_url = info.get('thumbnail') or (info.get('entries') or [{}])[0].get('thumbnail')
            if thumb_url:
                ext = os.path.splitext(thumb_url.split('?')[0])[1] or '.jpg'
                path = os.path.join(MEDIA_DIR, f'img_{info.get("id", int(time.time()))}{ext}')
                urllib.request.urlretrieve(thumb_url, path)
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    return path
        except Exception as ex:
            logger.warning(f"Thumbnail download failed: {ex}")

    return None


async def _download_pinterest_pin(url):
    yt_path = await _try_ytdlp_pinterest(url)
    if yt_path:
        return yt_path

    logger.info("No video from yt-dlp, scraping image...")
    originals_url, fallback_url = await _scrape_pinterest_image(url)
    if originals_url:
        try:
            return await _generic_direct_download(originals_url, fallback_736x=fallback_url)
        except ValueError as ex:
            if fallback_url:
                logger.info(f"originals failed ({ex}), trying 736x fallback")
                return await _generic_direct_download(fallback_url)
            raise
    raise ValueError("Pinterest: не удалось найти медиа на странице")


async def _try_ytdlp_pinterest(url):
    def _dl():
        opts = dict(_YT_DL_OPTS)
        opts['format'] = 'bv*+ba/b'
        opts['merge_output_format'] = 'mp4'
        opts['extractor_args'] = {'pinterest': {'video': ['true']}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = _resolve_yt_path(ydl, info)
                if fp:
                    return fp
        except yt_dlp.utils.DownloadError as ex:
            if 'No video formats' in str(ex):
                return None
            logger.warning(f"yt-dlp Pinterest error: {ex}")
            return None
        except Exception as ex:
            logger.warning(f"yt-dlp Pinterest error: {ex}")
        return None
    return await asyncio.to_thread(_dl)


async def _scrape_pinterest_image(url):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, allow_redirects=True) as resp:
            if resp.status != 200:
                return None, None
            html = await resp.text()

    og = re.search(r'<meta[^>]+(?:property|name)="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if not og:
        og = re.search(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="og:image"', html, re.IGNORECASE)
    if og:
        img = og.group(1).replace('&amp;', '&')
        fallback = None
        if '/736x/' in img:
            fallback = img
            img = img.replace('/736x/', '/originals/')
        return img, fallback

    img_urls = re.findall(r'(https://i\.pinimg\.com/(?:originals|736x|1200x|474x|236x|170x)/[a-zA-Z0-9/_-]+\.(?:jpg|jpeg|png|gif|webp))', html)
    if not img_urls:
        return None, None

    seen = set()
    unique = []
    for u in img_urls:
        n = u.rstrip(')')
        if n not in seen:
            seen.add(n)
            unique.append(n)

    for prefix in ('/originals/', '/1200x/', '/736x/', '/474x/', '/236x/', '/170x/'):
        for u in unique:
            if prefix in u:
                if prefix != '/originals/':
                    fallback = u
                    u = u.replace(prefix, '/originals/', 1)
                    return u, fallback
                return u, None
    return unique[0], None


async def _generic_direct_download(url, *, referer='https://www.pinterest.com/', fallback_736x=None):
    ext = os.path.splitext(url.split('?')[0].split('/')[-1])[1]
    if not ext or ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov'):
        ext = '.jpg'
    path = os.path.join(MEDIA_DIR, f'pin_{int(time.time())}{ext}')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Referer': referer,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.get(url, headers=headers) as resp:
            if resp.status == 403 and fallback_736x:
                logger.info("originals blocked (403), fallback to 736x")
                return await _generic_direct_download(fallback_736x, referer=referer)
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status} при скачивании {url}")
            with open(path, 'wb') as f:
                while True:
                    chunk = await resp.content.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    raise ValueError("Файл пуст после скачивания")


_DL_LOCK = asyncio.Lock()


async def run_download(event_edit_func, url, mode='video', quality=None, timeout=600):
    async with _DL_LOCK:
        return await _run_download_impl(event_edit_func, url, mode, quality, timeout)


async def _run_download_impl(event_edit_func, url, mode='video', quality=None, timeout=600):
    logger.info(f"[download] Starting: {url} (mode={mode})")
    try:
        is_instagram = 'instagram.com' in url.lower()
        is_youtube = 'youtube.com' in url.lower() or 'youtu.be' in url.lower()
        is_tiktok = 'tiktok.com' in url.lower()
        is_pinterest = 'pinterest.com' in url.lower() or 'pin.it' in url.lower()
        if is_youtube:
            m = re.match(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]{11})', url)
            if m:
                url = m.group(1)

        if is_pinterest:
            await event_edit_func("📥 Скачиваю из Pinterest...")
            filename = await asyncio.wait_for(
                _download_pinterest_pin(url), timeout=timeout
            )
        elif is_tiktok:
            await event_edit_func("📥 Скачиваю из TikTok...")
            filename = await asyncio.wait_for(
                _download_tiktok_video(url), timeout=timeout
            )
        elif is_instagram:
            await event_edit_func("📥 Скачиваю из Instagram...")
            filename = await asyncio.wait_for(
                _download_instagram_video(url), timeout=timeout
            )
        elif is_youtube:
            if mode == 'audio':
                await event_edit_func("🎵 Скачиваю аудио...")
                filename = await asyncio.wait_for(
                    _download_yt_audio(url), timeout=timeout
                )
            else:
                qual = f"{quality}p" if quality else None
                await event_edit_func(f"📥 Скачиваю видео ({qual or 'авто'})...")
                filename = await asyncio.wait_for(
                    _download_yt_video(url, quality), timeout=timeout
                )
        else:
            return await _download_generic(url, mode, event_edit_func)

        if isinstance(filename, list):
            filename = [p for p in filename if p and os.path.exists(p)]
            if filename:
                logger.info(f"[download] OK: {len(filename)} файл(ов) — {url}")
                return filename
            filename = None
        if filename and os.path.exists(filename):
            size = os.path.getsize(filename)
            if size < 512:
                logger.warning(f"[download] подозрительно маленький файл ({size} байт): {filename}")
                try:
                    os.remove(filename)
                except OSError:
                    pass
                filename = None
            else:
                logger.info(f"[download] OK: {filename} ({format_bytes(size)})")
                return filename
        logger.warning(f"[download] file not found or empty after download: {url}")
        return None
    except asyncio.TimeoutError:
        await event_edit_func("❌ Превышено время ожидания (10 мин).")
        logger.warning(f"Download timeout: {url}")
        return None
    except ValueError as ex:
        logger.warning(f"Download error: {ex}")
        if is_youtube:
            await event_edit_func("🔄 Пробую через Invidious...")
            invidious_path = await _try_invidious(url, mode)
            if invidious_path:
                return invidious_path
        await event_edit_func(f"❌ {ex}")
        return None
    except Exception as ex:
        logger.error(f"Download error: {ex}", exc_info=True)
        await event_edit_func(f"❌ **Ошибка:** {ex}")
        return None


async def send_and_clean(event_edit_func, client, chat_id, filepath, caption=''):
    if not filepath:
        return
    files = filepath if isinstance(filepath, list) else [filepath]
    files = [f for f in files if f and os.path.exists(f)]
    if not files:
        return
    try:
        too_big = [f for f in files if os.path.getsize(f) / 1024 / 1024 > _max_file_size]
    except OSError:
        return
    if too_big:
        await event_edit_func(f"❌ Слишком большой файл (> {_max_file_size} МБ).")
    else:
        await event_edit_func("📤 **Отправляю файл...**")
        sent = []
        failed = []
        for f in files:
            try:
                logger.info(f"send file: {f} ({os.path.getsize(f)} байт)")
            except OSError:
                logger.warning(f"send file: {f} (размер недоступен)")
            try:
                await client.send_file(chat_id, f, caption=caption)
                sent.append(f)
            except Exception as ex:
                logger.warning(f"send as media failed ({f}): {ex}")
                try:
                    await client.send_file(chat_id, f, caption=caption, force_document=True)
                    sent.append(f)
                except Exception as ex2:
                    logger.error(f"send as document failed ({f}): {ex2}")
                    failed.append((f, ex2))
        if failed:
            names = ", ".join(os.path.basename(f) for f, _ in failed)
            await event_edit_func(f"❌ Не удалось отправить файл(ы): {names}")
        elif sent:
            await event_edit_func("✅ Файл(ы) отправлены")
    await asyncio.sleep(5)
    for f in files:
        for _ in range(3):
            try:
                os.remove(f)
                break
            except OSError:
                await asyncio.sleep(1)
