import os

def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

# Telegram Auth
API_ID = _safe_int(os.environ.get("API_ID"), 0)
API_HASH = os.environ.get("API_HASH", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")  # MUST use session string for Render

# Channels
CHANNELS_STR = os.environ.get("CHANNELS", "")
try:
    CHANNELS = [int(x.strip()) for x in CHANNELS_STR.split(",") if x.strip()]
except ValueError:
    CHANNELS = []

OUTPUT_CHANNEL_ID = _safe_int(os.environ.get("OUTPUT_CHANNEL_ID"), 0)

# Wishlink Credentials
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
WISHLINK_REFRESH_TOKEN = os.environ.get("WISHLINK_REFRESH_TOKEN", "")

# Lehlah Credentials
LEHLAH_COOKIE = os.environ.get("LEHLAH_COOKIE", "")

# Admin ID (For DMs / Error Alerts)
ADMIN_ID = _safe_int(os.environ.get("ADMIN_ID"), 0)

# Render specifics
PORT = _safe_int(os.environ.get("PORT"), 10000)
# Use Render's persistent disk path if configured, else fallback to temp local db
DB_PATH = os.environ.get("DB_PATH", "seen_deals.sqlite")

# WhatsApp Bot Config
WA_BOT_URL = os.environ.get("WA_BOT_URL", "https://watsap-bot-nuej.onrender.com")
WA_API_SECRET = os.environ.get("WA_API_SECRET", "my_super_secret_key_123")

