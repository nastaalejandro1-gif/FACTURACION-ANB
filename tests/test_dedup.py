"""
Duplicados: un doble clic (o un update que Telegram reenvía) no debe timbrar
ni recalcular dos veces — el pendiente se toma con un UPDATE condicional.
"""
import pytest

import main
from tests.test_main_orchestration import CLIENT_PROFILE, _draft, _patch_common, _pending_anb


@pytest.mark.asyncio
async def test_segundo_clic_de_aprobar_no_vuelve_a_procesar(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_pending", lambda invoice_id: _pending_anb(_draft("78101803")))
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    # El otro clic ya lo tomó: el UPDATE ... WHERE estado='pendiente' no afecta filas.
    monkeypatch.setattr(main.sheets_client, "tomar_pendiente", lambda pid, estado: False)

    async def no_recalcular(*a, **kw):
        raise AssertionError("no debe recalcular ni mandar al cliente otra vez")
    monkeypatch.setattr(main, "_calcular_y_procesar_factura", no_recalcular)

    msg = await main._execute_approval("aprobar", "inv-9")

    assert "ya fue procesada" in msg
    assert saved_pending == []


@pytest.mark.asyncio
async def test_check_pending_manda_un_solo_resumen_y_marca_notificados(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    import json
    filas = [
        {**_pending_anb(_draft("78101803")), "id": "rev-1", "timestamp": "2026-10-01T00:00:00+00:00"},
        {"id": "conf-1", "tipo_aprobacion": "cliente_confirmacion", "canal_id": "555", "canal": "telegram",
         "invoice_json": json.dumps({"emisor": {"id_cliente": ""}}), "timestamp": "2026-10-27T00:00:00+00:00"},
        {"id": "canc-1", "tipo_aprobacion": "cliente_cancelacion", "canal_id": "555", "invoice_json": "{}"},
    ]
    monkeypatch.setattr(main.sheets_client, "limpiar_updates_viejos", lambda: None)
    monkeypatch.setattr(main.sheets_client, "get_overdue_pending", lambda horas: filas)
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    marcados = []
    monkeypatch.setattr(main.sheets_client, "marcar_notificados", lambda ids: marcados.extend(ids))

    resultado = await main.check_pending(x_cron_secret=main.CRON_SECRET)

    a_alejandro = [(t, m) for chat, t, m in sent if str(chat) == str(main.ALEJANDRO_CHAT_ID)]
    assert len(a_alejandro) == 1
    texto, teclado = a_alejandro[0]
    assert "Por revisar (1)" in texto and "no ha confirmado (1)" in texto
    assert "aprobar:rev-1" in str(teclado)
    assert sorted(marcados) == ["canc-1", "conf-1", "rev-1"]
    assert resultado == {"checked": 3}


@pytest.mark.asyncio
async def test_check_pending_sin_nada_nuevo_no_manda_mensaje(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "limpiar_updates_viejos", lambda: None)
    monkeypatch.setattr(main.sheets_client, "get_overdue_pending", lambda horas: [])
    assert await main.check_pending(x_cron_secret=main.CRON_SECRET) == {"checked": 0}
    assert sent == []
