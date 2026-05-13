import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
ALEJANDRO_CHAT_ID = int(os.environ["ALEJANDRO_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = "claude-sonnet-4-6"

FACTURAPI_KEY = os.environ["FACTURAPI_KEY"]
FACTURAPI_BASE_URL = "https://www.facturapi.io/v2"

GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

DESPACHO_ID = os.environ.get("DESPACHO_ID", "ANB-001")
CRON_SECRET = os.environ["CRON_SECRET"]
