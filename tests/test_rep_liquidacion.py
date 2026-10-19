"""
REP de liquidación: el cliente dice "liquídala con lo que resta" sin dar
monto y el sistema toma el saldo pendiente como monto pagado. Todo mockeado.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

import main
from models import RepDraft
from tools import build_invoice_tools
from tests.test_main_orchestration import CLIENT_PROFILE, REP_DRAFT_RECEPTOR, _patch_common

UUID = "AAAAAAAA-1111-4222-8333-BBBBBBBBBBBB"


def _draft(**kw) -> RepDraft:
    datos = dict(
        estatus="confirmado_por_cliente", uuid_factura_origen=UUID, receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-10-18T12:00:00", forma_pago="03", liquidar_saldo=True,
    )
    datos.update(kw)
    return RepDraft(**datos)


def _patch_rep(monkeypatch, reps_previos):
    sent, saved, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main, "_xml_recientes", {})

    async def fake_search(uuid, key):
        return {"uuid": uuid, "total": 10000.0, "items": []}
    monkeypatch.setattr(main, "search_invoice_by_uuid", fake_search)
    monkeypatch.setattr(main.sheets_client, "get_rep_history", lambda uuid: reps_previos)
    return sent, saved


def test_rep_sin_monto_exige_liquidar():
    with pytest.raises(ValidationError, match="monto_pagado es obligatorio"):
        _draft(liquidar_saldo=False)


def test_tool_de_rep_acepta_liquidar_sin_monto():
    rep_tool = next(t for t in build_invoice_tools([]) if t["name"] == "generate_rep_draft")
    assert "liquidar_saldo" in rep_tool["input_schema"]["properties"]
    assert "monto_pagado" not in rep_tool["input_schema"]["required"]


@pytest.mark.asyncio
async def test_liquidacion_usa_el_saldo_pendiente_como_monto(monkeypatch):
    sent, saved = _patch_rep(monkeypatch, [{"imp_saldo_insoluto": 6500.0, "folio_fiscal": "rep-a"}])

    await main._calcular_y_timbrar_rep("rep-1", _draft(), CLIENT_PROFILE, "555", 1)

    rep = main.RepData.model_validate_json(saved[0]["invoice_json"])
    assert rep.monto_pagado == Decimal("6500.0")
    assert rep.imp_saldo_insoluto == 0
    assert rep.liquidacion is True
    texto = next(t for c, t, _ in sent if c == "555")
    assert "Monto pagado: $6,500.00" in texto
    assert "liquida la factura" in texto


@pytest.mark.asyncio
async def test_liquidacion_sin_pagos_previos_es_el_total(monkeypatch):
    _, saved = _patch_rep(monkeypatch, [])

    await main._calcular_y_timbrar_rep("rep-1", _draft(), CLIENT_PROFILE, "555", 1)

    rep = main.RepData.model_validate_json(saved[0]["invoice_json"])
    assert rep.monto_pagado == Decimal("10000.0")


@pytest.mark.asyncio
async def test_factura_ya_liquidada_no_genera_rep(monkeypatch):
    sent, saved = _patch_rep(monkeypatch, [{"imp_saldo_insoluto": 0, "folio_fiscal": "rep-a"}])

    await main._calcular_y_timbrar_rep("rep-1", _draft(), CLIENT_PROFILE, "555", 1)

    assert saved == []
    assert any("ya está liquidada" in t for c, t, _ in sent if c == "555")


@pytest.mark.asyncio
async def test_pagos_previos_en_liquidacion_recalcula_el_monto_con_el_saldo_de_anb(monkeypatch):
    """Si el cliente avisa que hubo pagos en otro programa, el monto NO se
    queda en el total que se supuso: viaja como liquidación sin monto."""
    sent, saved, _ = _patch_common(monkeypatch)
    rep_data = main.RepData(
        estatus="confirmado_por_cliente", uuid_factura_origen=UUID, receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-10-18T12:00:00", forma_pago="03",
        monto_pagado=Decimal("10000"), imp_saldo_ant=Decimal("10000"), imp_saldo_insoluto=Decimal("0"),
        liquidacion=True, factura_origen_externa={"fuente": "xml", "uuid": UUID, "total": "10000"},
    )
    pending = {
        "id": "rep-1", "estado": "pendiente", "canal_id": "555", "canal": "telegram",
        "invoice_json": rep_data.model_dump_json(), "telegram_message_id": 0,
    }
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a: None)

    await main._execute_client_confirmation("cliente_previos", "rep-1", pending)

    envelope = main.PendingPayload.model_validate_json(saved[0]["invoice_json"])
    assert envelope.rep_draft.liquidar_saldo is True
    assert envelope.rep_draft.monto_pagado is None
    texto_anb = next(t for c, t, _ in sent if c == main.ALEJANDRO_CHAT_ID)
    assert "saldo pendiente (liquidación)" in texto_anb


ORIGEN_EXTERNO = {
    "fuente": "xml", "uuid": UUID, "total": "10000.00", "rfc_emisor": CLIENT_PROFILE.rfc,
    "metodo_pago": "PPD", "moneda": "MXN", "tipo_comprobante": "I", "bases_impuestos": [],
    "advertencias": [], "rfc_receptor": "PERJ800101AB1", "nombre_receptor": "Juan Pérez",
    "regimen_receptor": "612", "cp_receptor": "44100",
}


@pytest.mark.asyncio
async def test_liquidacion_de_factura_externa_sin_saldo_confirmado_va_a_anb(monkeypatch):
    sent, saved = _patch_rep(monkeypatch, [])
    monkeypatch.setattr(main.cfdi_xml, "problemas_para_rep", lambda origen, rfc: [])

    await main._calcular_y_timbrar_rep("rep-1", _draft(), CLIENT_PROFILE, "555", 1, origen_externo=ORIGEN_EXTERNO)

    assert saved[0]["tipo_aprobacion"] == "anb_revision"
    assert "pagos_previos_externos" in saved[0]["motivo_revision"]
    texto_anb = next(t for c, t, _ in sent if c == main.ALEJANDRO_CHAT_ID)
    assert "/aprobar rep-1 saldo=SALDO_PENDIENTE parcialidad=N" in texto_anb


@pytest.mark.asyncio
async def test_aprobar_con_saldo_liquida_la_factura_externa_con_ese_saldo(monkeypatch):
    """/aprobar {id} saldo=4000 parcialidad=3 -> REP por $4,000 (no por el total)."""
    sent, saved = _patch_rep(monkeypatch, [])
    monkeypatch.setattr(main.cfdi_xml, "problemas_para_rep", lambda origen, rfc: [])
    envelope = main.PendingPayload(
        tipo="rep", escalation_reason="pagos_previos_externos", escalation_detail="x",
        rep_draft=_draft(), factura_origen=ORIGEN_EXTERNO,
    )
    monkeypatch.setattr(main.sheets_client, "get_pending", lambda pid: {
        "id": "esc-1", "estado": "pendiente", "tipo_aprobacion": "anb_revision", "canal": "telegram",
        "canal_id": "555", "invoice_json": envelope.model_dump_json(),
    })
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a: None)

    await main._execute_approval("aprobar", "esc-1", saldo=Decimal("4000"), parcialidad=3)

    rep = main.RepData.model_validate_json(saved[-1]["invoice_json"])
    assert saved[-1]["tipo_aprobacion"] == "cliente_confirmacion"
    assert rep.monto_pagado == Decimal("4000.0")
    assert rep.imp_saldo_ant == Decimal("4000")
    assert rep.imp_saldo_insoluto == 0
