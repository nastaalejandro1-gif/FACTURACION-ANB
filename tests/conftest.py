import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ALEJANDRO_CHAT_ID", "999999999")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
os.environ.setdefault("FACTURAPI_KEY", "test-key")
os.environ.setdefault("CRON_SECRET", "cron-secret-test")
os.environ.setdefault("DESPACHO_ID", "ANB-001")


import pytest


@pytest.fixture(autouse=True)
def _tomar_pendiente_via_update(monkeypatch):
    """En los tests tomar_pendiente siempre gana y pasa por update_pending_status,
    que cada test mockea para registrar los estados."""
    import sheets_client

    def tomar(invoice_id, estado):
        sheets_client.update_pending_status(invoice_id, estado)
        return True
    monkeypatch.setattr(sheets_client, "tomar_pendiente", tomar)
