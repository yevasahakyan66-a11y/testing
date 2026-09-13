import os
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
PORT = int(os.environ.get('PORT', 8080))
STRING_SESSION = os.environ.get('STRING_SESSION')
OWNER_ID_RAW = os.environ.get('OWNER_ID')
MEDIA_DIR = os.environ.get('MEDIA_DIR', './media')

if not API_ID or not API_HASH:
    raise ValueError("API_ID и API_HASH обязательны. Укажите их в .env")
if not OWNER_ID_RAW:
    raise ValueError("OWNER_ID обязателен. Укажите Telegram ID владельца в .env")
OWNER_ID = int(OWNER_ID_RAW)

PROXY = os.environ.get('PROXY')
PROXY_LIST = [p.strip() for p in os.environ.get('PROXY_LIST', '').split(',') if p.strip()]

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-1.5-flash')
GEMINI_FALLBACK_MODELS = (
    [m.strip() for m in os.environ.get('GEMINI_FALLBACK_MODELS', '').split(',') if m.strip()]
    or ['gemini-2.0-flash', 'gemini-2.5-flash', 'gemini-1.5-pro', 'gemini-pro']
)
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', 'deepseek/deepseek-chat:free')
AI_TEMPERATURE = float(os.environ.get('AI_TEMPERATURE', '0.9'))
AI_TTS_VOICE = os.environ.get('AI_TTS_VOICE', 'ru-RU-SvetlanaNeural')
AI_MAX_HISTORY = int(os.environ.get('AI_MAX_HISTORY', '200'))

MAX_COOLDOWN_ENTRIES = 500
MAX_FILE_SIZE_MB = 1500
