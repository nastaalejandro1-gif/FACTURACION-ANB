import os
from dotenv import load_dotenv

load_dotenv()

# Validate required variables on startup and report which are missing
_REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ALEJANDRO_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "CRON_SECRET",
]
_missing = [v for v in _REQUIRED if not os.environ.get(v)]
if _missing:
    raise RuntimeError(f"Variables de entorno faltantes: {', '.join(_missing)}")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
ALEJANDRO_CHAT_ID = int(os.environ["ALEJANDRO_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Haiku 5.5 (oct 2026): ~17x más barato que Sonnet 4.6 con la misma exactitud
# en la batería de escenarios reales (20/21 vs 14/14; el único error fue una
# clave marcada NUEVA, que escala a ANB en vez de timbrar mal). Para regresar
# a Sonnet sin tocar código: ANTHROPIC_MODEL=claude-sonnet-4-6 en Railway.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low")

FACTURAPI_BASE_URL = "https://www.facturapi.io/v2"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

DESPACHO_ID = os.environ.get("DESPACHO_ID", "ANB-001")
CRON_SECRET = os.environ["CRON_SECRET"]

# Resend (opcional — si no está configurado, el email se omite silenciosamente)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Facturación ANB <facturas@anb-consultores.com>")
