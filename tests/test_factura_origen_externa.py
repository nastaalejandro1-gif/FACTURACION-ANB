"""
REP de una factura emitida en OTRO programa (no está en FacturAPI): lectura
del XML (cfdi_xml.py), espera del XML, confirmación con "ya hubo pagos
antes" y aprobación de ANB con saldo/parcialidad. Todo mockeado.
"""
from decimal import Decimal

import pytest

import cfdi_xml
import main
from facturapi_client import _build_related_document_taxes
from models import RepDraft
from tests.test_main_orchestration import CLIENT_PROFILE, REP_DRAFT_RECEPTOR, _patch_common

UUID = "AAAAAAAA-1111-4222-8333-BBBBBBBBBBBB"


def _xml(
    emisor=CLIENT_PROFILE.rfc, metodo="PPD", moneda="MXN", total="10984.00",
    version_ns="http://www.sat.gob.mx/cfd/4", traslado_extra="", timbrado=True,
) -> bytes:
    # 2 conceptos: 5000 + 4000, IVA 16% y retención ISR 1.25% en cada uno.
    timbre = (
        '<cfdi:Complemento><tfd:TimbreFiscalDigital '
        'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" '
        f'Version="1.1" UUID="{UUID.lower()}"/></cfdi:Complemento>'
    ) if timbrado else ""
    concepto = (
        '<cfdi:Concepto ClaveProdServ="80111600" Cantidad="1" Importe="{imp}" ObjetoImp="02">'
        '<cfdi:Impuestos><cfdi:Traslados>'
        '<cfdi:Traslado Base="{imp}" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="{iva}"/>'
        '{extra}</cfdi:Traslados><cfdi:Retenciones>'
        '<cfdi:Retencion Base="{imp}" Impuesto="001" TipoFactor="Tasa" TasaOCuota="0.012500" Importe="{isr}"/>'
        '</cfdi:Retenciones></cfdi:Impuestos></cfdi:Concepto>'
    )
    conceptos = (
        concepto.format(imp="5000.00", iva="800.00", isr="62.50", extra=traslado_extra)
        + concepto.format(imp="4000.00", iva="640.00", isr="50.00", extra="")
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<cfdi:Comprobante xmlns:cfdi="{version_ns}" Version="4.0" Serie="A" Folio="77" '
        f'Fecha="2026-09-01T10:00:00" SubTotal="9000.00" Moneda="{moneda}" Total="{total}" '
        f'TipoDeComprobante="I" MetodoPago="{metodo}">'
        f'<cfdi:Emisor Rfc="{emisor}" Nombre="SIN CULPA" RegimenFiscal="601"/>'
        '<cfdi:Receptor Rfc="DVA010101AA1" Nombre="DISTRIBUIDORA DEL VALLE" '
        'DomicilioFiscalReceptor="06600" RegimenFiscalReceptor="601" UsoCFDI="G03"/>'
        f'<cfdi:Conceptos>{conceptos}</cfdi:Conceptos>'
        '<cfdi:Impuestos TotalImpuestosTrasladados="1440.00" TotalImpuestosRetenidos="112.50"/>'
        f'{timbre}</cfdi:Comprobante>'
    ).encode()


TOTAL_OK = "10327.50"  # 9000 + 1440 - 112.50


# ---------------------------------------------------------------------------
# cfdi_xml
# ---------------------------------------------------------------------------

def test_parse_xml_agrega_bases_por_impuesto_y_tasa():
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK))
    assert origen["uuid"] == UUID
    assert origen["total"] == "10327.50"
    assert origen["serie_folio"] == "A77"
    assert origen["rfc_receptor"] == "DVA010101AA1"
    assert origen["advertencias"] == []
    bases = {(b["type"], b["rate"], b["withholding"]): b["base"] for b in origen["bases_impuestos"]}
    assert bases == {("IVA", "0.16", False): "9000.00", ("ISR", "0.0125", True): "9000.00"}


@pytest.mark.parametrize("xml, mensaje", [
    (b"no soy xml", "no es un XML"),
    (_xml(version_ns="http://www.sat.gob.mx/cfd/3", total=TOTAL_OK), "3.3"),
    (_xml(timbrado=False, total=TOTAL_OK), "no está timbrado"),
])
def test_parse_xml_rechaza_archivos_que_no_sirven(xml, mensaje):
    with pytest.raises(cfdi_xml.CfdiXmlError, match=mensaje):
        cfdi_xml.parse_cfdi_xml(xml)


def test_total_que_no_cuadra_queda_como_advertencia():
    origen = cfdi_xml.parse_cfdi_xml(_xml(total="10984.00"))  # p.ej. impuestos locales
    assert any("no cuadra" in a for a in origen["advertencias"])


def test_iva_exento_queda_como_advertencia():
    exento = '<cfdi:Traslado Base="1" Impuesto="003" TipoFactor="Exento"/>'
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK, traslado_extra=exento))
    assert any("Exento" in a for a in origen["advertencias"])


@pytest.mark.parametrize("kwargs, esperado", [
    ({"metodo": "PUE"}, "no PPD"),
    ({"moneda": "USD"}, "USD"),
    ({"emisor": "OTR010101AA1"}, "no el cliente"),
])
def test_problemas_para_rep(kwargs, esperado):
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK, **kwargs))
    problemas = cfdi_xml.problemas_para_rep(origen, CLIENT_PROFILE.rfc)
    assert any(esperado in p for p in problemas)


def test_factura_externa_valida_no_tiene_problemas():
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK))
    assert cfdi_xml.problemas_para_rep(origen, CLIENT_PROFILE.rfc) == []


def test_related_document_taxes_usa_bases_del_xml_prorrateadas():
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK))
    taxes = _build_related_document_taxes(origen, Decimal("10327.50") / 2)  # medio pago
    iva = next(t for t in taxes if t["type"] == "IVA")
    isr = next(t for t in taxes if t["type"] == "ISR")
    assert iva == {"base": 4500.0, "type": "IVA", "rate": 0.16, "withholding": False}
    assert isr["withholding"] is True and isr["base"] == 4500.0


# ---------------------------------------------------------------------------
# main: saldo y número de parcialidad
# ---------------------------------------------------------------------------

def test_saldo_anterior():
    origen = {"total": "1000", "fuente": "xml"}
    assert main._saldo_anterior(origen, []) == Decimal("1000")
    origen["saldo_inicial"] = "400"  # fijado por ANB: pagos en el otro programa
    assert main._saldo_anterior(origen, []) == Decimal("400")
    assert main._saldo_anterior(origen, [{"imp_saldo_insoluto": 150}]) == Decimal("150")


@pytest.mark.asyncio
async def test_num_parcialidad(monkeypatch):
    async def fake_get_invoice(invoice_id, key):
        assert invoice_id == "rep-facturapi-id"
        return {"complements": [{"data": [{"related_documents": [{"installment": 4}]}]}]}
    monkeypatch.setattr(main, "get_invoice", fake_get_invoice)
    reps = [{"folio_fiscal": "otro"}, {"folio_fiscal": "rep-facturapi-id"}]

    # factura de FacturAPI: se cuentan los REPs del bot, sin llamar a FacturAPI
    assert await main._num_parcialidad({"total": 1}, reps, "k") == 3
    # externa, primer REP del bot: las previas que fijó ANB
    assert await main._num_parcialidad({"fuente": "xml", "parcialidades_previas": 2}, [], "k") == 3
    # externa con REPs del bot: la parcialidad del último, tal como quedó en FacturAPI
    assert await main._num_parcialidad({"fuente": "xml"}, reps, "k") == 5


# ---------------------------------------------------------------------------
# main: flujo
# ---------------------------------------------------------------------------

def _rep_draft():
    return RepDraft(
        estatus="confirmado_por_cliente", uuid_factura_origen=UUID, receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-10-01T12:00:00", forma_pago="03", monto_pagado=5000.0,
    )


@pytest.fixture(autouse=True)
def _sin_xml_recientes(monkeypatch):
    monkeypatch.setattr(main, "_xml_recientes", {})


def _sin_facturapi(monkeypatch):
    async def fake_search(uuid, key):
        return None
    monkeypatch.setattr(main, "search_invoice_by_uuid", fake_search)
    monkeypatch.setattr(main.sheets_client, "get_rep_history", lambda uuid: [])


def _botones(markup):
    return [b["callback_data"] for fila in markup["inline_keyboard"] for b in fila]


@pytest.mark.asyncio
async def test_uuid_desconocido_pide_xml_en_vez_de_rendirse(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    _sin_facturapi(monkeypatch)

    await main._calcular_y_timbrar_rep("rep-1", _rep_draft(), CLIENT_PROFILE, "555", 1)

    assert len(saved_pending) == 1
    assert saved_pending[0]["tipo_aprobacion"] == "esperando_xml_origen"
    assert RepDraft.model_validate_json(saved_pending[0]["invoice_json"]).uuid_factura_origen == UUID
    assert any("XML" in text for chat, text, _ in sent if chat == "555")


@pytest.mark.asyncio
async def test_xml_recibido_retoma_el_rep_con_receptor_del_cfdi(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    _sin_facturapi(monkeypatch)
    esperando = {"id": "esp-1", "invoice_json": _rep_draft().model_dump_json()}
    monkeypatch.setattr(main.sheets_client, "get_pending_esperando_xml", lambda chat: esperando)
    estados = []
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda pid, e: estados.append((pid, e)))

    nota = await main._procesar_xml_factura_origen(CLIENT_PROFILE, "555", 2, _xml(total=TOTAL_OK))

    assert nota is None
    assert ("esp-1", "xml_recibido") in estados
    confirmacion = saved_pending[-1]
    assert confirmacion["tipo_aprobacion"] == "cliente_confirmacion"
    rep_data = main.RepData.model_validate_json(confirmacion["invoice_json"])
    assert rep_data.receptor.razon_social == "DISTRIBUIDORA DEL VALLE"
    assert rep_data.receptor.uso_cfdi == "CP01"
    assert rep_data.imp_saldo_ant == Decimal("10327.50")
    assert rep_data.imp_saldo_insoluto == Decimal("5327.50")
    # la factura viaja en el pendiente: al confirmar no se busca en FacturAPI
    assert rep_data.factura_origen_externa["uuid"] == UUID
    # se ofrece el botón de pagos previos porque se asumió primer pago
    assert any(c.startswith("cliente_previos:") for c in _botones(sent[-1][2]))


@pytest.mark.asyncio
async def test_confirmacion_de_rep_externo_no_busca_en_facturapi(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a, **kw: None)
    monkeypatch.setattr(main.sheets_client, "get_rep_history", lambda uuid: [])

    async def boom(*a, **kw):
        raise AssertionError("FacturAPI no conoce facturas externas")
    monkeypatch.setattr(main, "search_invoice_by_uuid", boom)
    timbrado = []

    async def fake_timbre_rep(invoice_id, rep_data, client_profile, chat_id, key, num_parcialidad, original):
        timbrado.append((num_parcialidad, original))
    monkeypatch.setattr(main, "_timbre_and_deliver_rep", fake_timbre_rep)

    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK))
    rep_data = main.RepData(
        estatus="confirmado_por_cliente", uuid_factura_origen=UUID, receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-10-01T12:00:00", forma_pago="03", monto_pagado=Decimal("5000"),
        imp_saldo_ant=Decimal("10327.50"), imp_saldo_insoluto=Decimal("5327.50"),
        factura_origen_externa=origen,
    )
    pending = {
        "id": "rep-1", "estado": "pendiente", "canal_id": "555", "canal": "telegram",
        "invoice_json": rep_data.model_dump_json(), "telegram_message_id": 0,
    }
    await main._execute_client_confirmation("cliente_si", "rep-1", pending)

    assert timbrado == [(1, origen)]


@pytest.mark.asyncio
async def test_xml_de_otro_emisor_se_rechaza(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)

    nota = await main._procesar_xml_factura_origen(
        CLIENT_PROFILE, "555", 2, _xml(total=TOTAL_OK, emisor="OTR010101AA1")
    )
    assert nota is None
    assert saved_pending == []
    assert main._xml_recientes == {}
    assert any("OTR010101AA1" in text for _, text, _ in sent)


@pytest.mark.asyncio
async def test_xml_antes_del_rep_no_vuelve_a_pedirse(monkeypatch):
    """El cliente manda el XML primero: Claude recibe la nota y, cuando arma
    el REP, el bot usa ese XML en vez de pedirlo otra vez."""
    _, saved_pending, _ = _patch_common(monkeypatch)
    _sin_facturapi(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_pending_esperando_xml", lambda chat: None)

    nota = await main._procesar_xml_factura_origen(CLIENT_PROFILE, "555", 2, _xml(total=TOTAL_OK))
    assert nota.startswith("[Sistema:") and UUID in nota

    await main._calcular_y_timbrar_rep("rep-1", _rep_draft(), CLIENT_PROFILE, "555", 3)
    assert [p["tipo_aprobacion"] for p in saved_pending] == ["cliente_confirmacion"]


@pytest.mark.asyncio
async def test_factura_externa_pue_no_se_timbra(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    _sin_facturapi(monkeypatch)
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK, metodo="PUE"))

    await main._calcular_y_timbrar_rep("rep-1", _rep_draft(), CLIENT_PROFILE, "555", 1, origen_externo=origen)

    assert saved_pending == []
    assert any("no PPD" in text for chat, text, _ in sent if chat == "555")


@pytest.mark.asyncio
async def test_aprobar_pagos_previos_exige_saldo_y_lo_aplica(monkeypatch):
    _patch_common(monkeypatch)
    origen = cfdi_xml.parse_cfdi_xml(_xml(total=TOTAL_OK))
    envelope = main.PendingPayload(
        tipo="rep", escalation_reason="pagos_previos_externos", escalation_detail="x",
        rep_draft=_rep_draft(), factura_origen=origen,
    )
    pending = {
        "id": "esc-1", "estado": "pendiente", "tipo_aprobacion": "anb_revision",
        "canal_id": "555", "canal": "telegram", "invoice_json": envelope.model_dump_json(),
    }
    monkeypatch.setattr(main.sheets_client, "get_pending", lambda pid: pending)
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    estados = []
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda pid, e: estados.append(e))
    reintentos = []

    async def fake_rep(*a, origen_externo=None):
        reintentos.append(origen_externo)
    monkeypatch.setattr(main, "_calcular_y_timbrar_rep", fake_rep)

    # sin saldo: no aprueba
    await main._execute_approval("aprobar", "esc-1")
    assert estados == [] and reintentos == []

    await main.handle_approval_command(
        str(main.ALEJANDRO_CHAT_ID), 1, "/aprobar esc-1 saldo=$6,000.50 parcialidad=3"
    )
    assert estados == ["aprobado"]
    assert len(reintentos) == 1
    aplicado = reintentos[0]
    assert aplicado["uuid"] == UUID
    assert aplicado["saldo_inicial"] == "6000.50"
    assert aplicado["parcialidades_previas"] == 2
    assert aplicado["saldo_confirmado"] is True
