import os
from dotenv import load_dotenv

load_dotenv()

# Validate required variables on startup and report which are missing
_REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ALEJANDRO_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "GOOGLE_SHEETS_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "CRON_SECRET",
]
_missing = [v for v in _REQUIRED if not os.environ.get(v)]
if _missing:
    raise RuntimeError(f"Variables de entorno faltantes: {', '.join(_missing)}")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
ALEJANDRO_CHAT_ID = int(os.environ["ALEJANDRO_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = "claude-sonnet-4-6"

FACTURAPI_BASE_URL = "https://www.facturapi.io/v2"

GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

DESPACHO_ID = os.environ.get("DESPACHO_ID", "ANB-001")
CRON_SECRET = os.environ["CRON_SECRET"]

# Resend (opcional — si no está configurado, el email se omite silenciosamente)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Facturación ANB <facturas@anb-consultores.com>")
