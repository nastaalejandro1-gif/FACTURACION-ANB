"""
Cancelación pedida por el cliente: el bot solo ofrece folios propios y
recientes, pide confirmación con botón, y solo el mismo chat puede
confirmarla. Todo mockeado — nunca contra Supabase/Telegram/FacturAPI reales.
"""
import json

import httpx
import pytest

import main
from models import CancelacionDraft
from tools import build_invoice_tools
from tests.test_main_orchestration import CLIENT_PROFILE

FILA_BITACORA = {
    "id": "bit-1", "folio_fiscal": "fapi-1", "estado": "timbrado", "tipo": "ingreso",
    "rfc_receptor": "PERJ800101AB1", "total": 1160.0,
}
FACTURA_FAPI = {
    "id": "fapi-1", "status": "valid", "cancellation_status": "none", "series": "F", "folio_number": 123,
    "total": 1160.0, "date": "2026-10-15T12:00:00Z", "uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
    "customer": {"legal_name": "Juan Pérez", "tax_id": "PERJ800101AB1"},
}


def _patch(monkeypatch, fila=FILA_BITACORA, factura=FACTURA_FAPI):
    sent, saved, estados_pend, estados_bit = [], [], [], []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    async def fake_get_invoice(invoice_id, key):
        return factura

    monkeypatch.setattr(main.telegram_client, "send_message", fake_send)
    monkeypatch.setattr(main.sheets_client, "get_bitacora_por_folio", lambda despacho, canal, folio, rfc_emisor="": fila)
    monkeypatch.setattr(main.sheets_client, "save_pending", lambda **kw: saved.append(kw))
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda pid, e: estados_pend.append(e))
    monkeypatch.setattr(main.sheets_client, "update_bitacora_estado", lambda bid, e, *a: estados_bit.append((bid, e)))
    monkeypatch.setattr(main, "get_invoice", fake_get_invoice)
    return sent, saved, estados_pend, estados_bit


def _pending(motivo="02", canal="555"):
    return {
        "id": "can-1", "estado": "pendiente", "canal": "telegram", "canal_id": canal,
        "canal_id_aprobador": canal, "tipo_aprobacion": "cliente_cancelacion",
        "invoice_json": json.dumps({"folio": "fapi-1", "motivo": motivo, "bitacora_id": "bit-1", "tipo": "ingreso"}),
    }


def test_tool_de_cancelacion_solo_ofrece_folios_recibidos():
    tools = build_invoice_tools(["78101803"], ["fapi-1", "fapi-2"])
    tool = next(t for t in tools if t["name"] == "request_invoice_cancellation")
    assert tool["input_schema"]["properties"]["folio"]["enum"] == ["fapi-1", "fapi-2"]
    assert tool["input_schema"]["properties"]["motivo"]["enum"] == ["02", "03"]


def test_sin_facturas_timbradas_no_hay_tool_de_cancelacion():
    assert all(t["name"] != "request_invoice_cancellation" for t in build_invoice_tools(["78101803"]))


@pytest.mark.asyncio
async def test_solicitud_muestra_datos_y_pide_confirmacion(monkeypatch):
    sent, saved, _, _ = _patch(monkeypatch)

    await main._solicitar_confirmacion_cancelacion(
        CancelacionDraft(folio="fapi-1", motivo="02"), CLIENT_PROFILE, "555", 7
    )

    assert saved[0]["tipo_aprobacion"] == "cliente_cancelacion"
    assert saved[0]["canal_id_aprobador"] == "555"
    chat, texto, markup = sent[-1]
    assert chat == "555"
    assert "F-123" in texto and "$1,160.00" in texto and "Juan Pérez" in texto
    assert "02 – Comprobante emitido con errores sin relación" in texto
    assert "cancelar_si:" in str(markup) and "cancelar_no:" in str(markup)


@pytest.mark.asyncio
async def test_folio_que_no_es_del_cliente_no_se_ofrece(monkeypatch):
    sent, saved, _, _ = _patch(monkeypatch, fila=None)

    await main._solicitar_confirmacion_cancelacion(
        CancelacionDraft(folio="fapi-otro", motivo="02"), CLIENT_PROFILE, "555", 7
    )

    assert saved == []
    assert "No encontré esa factura" in sent[-1][1]


@pytest.mark.asyncio
async def test_factura_ya_cancelada_no_se_ofrece(monkeypatch):
    sent, saved, _, _ = _patch(monkeypatch, factura={**FACTURA_FAPI, "status": "canceled"})

    await main._solicitar_confirmacion_cancelacion(
        CancelacionDraft(folio="fapi-1", motivo="03"), CLIENT_PROFILE, "555", 7
    )

    assert saved == []
    assert "ya está cancelada" in sent[-1][1]


@pytest.mark.asyncio
async def test_confirmar_cancela_y_marca_bitacora(monkeypatch):
    sent, _, estados_pend, estados_bit = _patch(monkeypatch)
    llamadas = []

    async def fake_cancel(folio, motivo, key):
        llamadas.append((folio, motivo))
        return {**FACTURA_FAPI, "status": "canceled"}
    monkeypatch.setattr(main, "cancel_invoice", fake_cancel)

    await main._execute_cancelacion("cancelar_si", "can-1", _pending("03"), CLIENT_PROFILE)

    assert llamadas == [("fapi-1", "03")]
    assert estados_pend == ["aprobado"]
    assert estados_bit == [("bit-1", "cancelado")]
    assert any(c == "555" and "F-123 quedó cancelada" in t for c, t, _ in sent)
    assert any(c == main.ALEJANDRO_CHAT_ID for c, _, _ in sent)


@pytest.mark.asyncio
async def test_cancelacion_que_requiere_aceptacion_del_receptor(monkeypatch):
    sent, _, _, estados_bit = _patch(monkeypatch)

    async def fake_cancel(folio, motivo, key):
        return {**FACTURA_FAPI, "status": "valid", "cancellation_status": "pending"}
    monkeypatch.setattr(main, "cancel_invoice", fake_cancel)

    await main._execute_cancelacion("cancelar_si", "can-1", _pending(), CLIENT_PROFILE)

    assert estados_bit == [("bit-1", "cancelacion_pendiente")]
    assert any("buzón tributario" in t for _, t, _ in sent)


@pytest.mark.asyncio
async def test_error_de_facturapi_avisa_a_anb_y_no_marca_cancelada(monkeypatch):
    sent, _, _, estados_bit = _patch(monkeypatch)

    async def fake_cancel(folio, motivo, key):
        req = httpx.Request("DELETE", "https://x")
        raise httpx.HTTPStatusError("tiene pagos relacionados", request=req, response=httpx.Response(400, request=req))
    monkeypatch.setattr(main, "cancel_invoice", fake_cancel)

    await main._execute_cancelacion("cancelar_si", "can-1", _pending(), CLIENT_PROFILE)

    assert estados_bit == []
    assert any(c == main.ALEJANDRO_CHAT_ID and "tiene pagos relacionados" in t for c, t, _ in sent)


@pytest.mark.asyncio
async def test_decir_no_no_cancela(monkeypatch):
    sent, _, estados_pend, _ = _patch(monkeypatch)

    async def boom(*a):
        raise AssertionError("no debe cancelar")
    monkeypatch.setattr(main, "cancel_invoice", boom)

    await main._execute_cancelacion("cancelar_no", "can-1", _pending(), CLIENT_PROFILE)

    assert estados_pend == ["rechazado_cliente"]


@pytest.mark.asyncio
async def test_doble_clic_no_cancela_dos_veces(monkeypatch):
    _patch(monkeypatch)

    async def boom(*a):
        raise AssertionError("no debe cancelar de nuevo")
    monkeypatch.setattr(main, "cancel_invoice", boom)

    await main._execute_cancelacion("cancelar_si", "can-1", {**_pending(), "estado": "aprobado"}, CLIENT_PROFILE)


@pytest.mark.asyncio
async def test_otro_chat_no_puede_confirmar_la_cancelacion(monkeypatch):
    respuestas = []

    async def fake_answer(cid, text=""):
        respuestas.append(text)
    monkeypatch.setattr(main.telegram_client, "answer_callback_query", fake_answer)
    monkeypatch.setattr(main.sheets_client, "get_pending", lambda pid: _pending(canal="555"))

    async def boom(*a):
        raise AssertionError("no debe ejecutarse")
    monkeypatch.setattr(main, "_execute_cancelacion", boom)

    await main.handle_callback_query({
        "id": "cb-1", "data": "cancelar_si:can-1", "message": {"chat": {"id": 666}},
    })

    assert respuestas == ["No autorizado"]
