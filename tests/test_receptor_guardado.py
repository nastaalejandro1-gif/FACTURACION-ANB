"""
Receptores guardados en el directorio de clientes de FacturAPI: se guardan
al timbrar y el bot los busca por nombre para no pedir la CSF otra vez.
Todo mockeado — nunca contra FacturAPI/Anthropic reales.
"""
import json
from decimal import Decimal
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

import claude_client
import facturapi_client
import main
from models import EmisorData, FacturaData, ConceptoItem, InvoiceData, ReceptorData
from tests.test_main_orchestration import CLIENT_PROFILE, RECEPTOR_PF
from tools import TOOL_BUSCAR_RECEPTOR, build_invoice_tools

CUSTOMER_FAPI = {
    "id": "cus_1", "legal_name": "GRUPO ENVOY", "tax_id": "GEN010101AA1",
    "tax_system": "601", "address": {"zip": "06600"}, "default_invoice_use": "G03",
}


def _mock_async_client(monkeypatch, handler):
    monkeypatch.setattr(
        facturapi_client.httpx, "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )


def test_tool_de_busqueda_siempre_se_ofrece():
    assert any(t["name"] == TOOL_BUSCAR_RECEPTOR for t in build_invoice_tools([]))


@pytest.mark.asyncio
async def test_guardar_cliente_nuevo_hace_post(monkeypatch):
    peticiones = []

    def handler(request):
        peticiones.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(201, json={"id": "cus_nuevo"})

    _mock_async_client(monkeypatch, handler)
    await facturapi_client.guardar_cliente(RECEPTOR_PF, "fake-key")

    assert [r.method for r in peticiones] == ["GET", "POST"]
    cuerpo = json.loads(peticiones[1].content)
    assert cuerpo == {
        "legal_name": "Juan Pérez", "tax_id": "PERJ800101AB1", "tax_system": "612",
        "address": {"zip": "44100"}, "default_invoice_use": "G03",
    }


@pytest.mark.asyncio
async def test_guardar_cliente_existente_lo_actualiza(monkeypatch):
    peticiones = []

    def handler(request):
        peticiones.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "cus_9", "tax_id": "PERJ800101AB1"}]})
        return httpx.Response(200, json={"id": "cus_9"})

    _mock_async_client(monkeypatch, handler)
    await facturapi_client.guardar_cliente(RECEPTOR_PF, "fake-key")

    assert [r.method for r in peticiones] == ["GET", "PUT"]
    assert peticiones[1].url.path.endswith("/customers/cus_9")


@pytest.mark.asyncio
async def test_publico_en_general_no_se_guarda(monkeypatch):
    def handler(request):
        raise AssertionError("no debe llamar a FacturAPI para un RFC genérico")

    _mock_async_client(monkeypatch, handler)
    generico = RECEPTOR_PF.model_copy(update={"rfc": "XAXX010101000"})
    await facturapi_client.guardar_cliente(generico, "fake-key")


def test_buscar_clientes_traduce_a_campos_del_receptor(monkeypatch):
    def fake_get(url, params, headers, timeout):
        assert params["q"] == "Envoy"
        return httpx.Response(200, json={"data": [CUSTOMER_FAPI]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(facturapi_client.httpx, "get", fake_get)
    assert facturapi_client.buscar_clientes(" Envoy ", "fake-key") == [{
        "razon_social": "GRUPO ENVOY", "rfc": "GEN010101AA1", "regimen_fiscal": "601",
        "cp_fiscal": "06600", "uso_cfdi": "G03",
    }]


def _respuesta(*bloques, stop_reason):
    return SimpleNamespace(
        content=list(bloques), stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def test_turno_busca_receptor_y_sigue_sin_pedir_csf(monkeypatch):
    monkeypatch.setattr(claude_client, "buscar_clientes", lambda texto, key: [{"rfc": "GEN010101AA1"}])
    respuestas = iter([
        _respuesta(
            SimpleNamespace(type="tool_use", id="tu_1", name=TOOL_BUSCAR_RECEPTOR, input={"texto": "Envoy"}),
            stop_reason="tool_use",
        ),
        _respuesta(SimpleNamespace(type="text", text="Mándame los conceptos"), stop_reason="end_turn"),
    ])
    llamadas = []

    def fake_create(**kwargs):
        llamadas.append(json.loads(json.dumps(kwargs["messages"], default=vars)))
        return next(respuestas)

    monkeypatch.setattr(claude_client.client.messages, "create", fake_create)
    history = []
    mensaje, draft, rep, cancelacion = claude_client.run_conversation_turn(
        CLIENT_PROFILE, [], history, [], user_text="factura a Envoy por 1000",
    )

    assert mensaje == "Mándame los conceptos"
    assert (draft, rep, cancelacion) == (None, None, None)
    tool_result = llamadas[1][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1"
    assert "GEN010101AA1" in tool_result["content"]


def test_busqueda_sin_resultados_o_con_error_pide_csf(monkeypatch):
    monkeypatch.setattr(claude_client, "buscar_clientes", lambda texto, key: [])
    assert "Pide la CSF" in claude_client._resultado_busqueda_receptor(CLIENT_PROFILE, "Nadie")

    def falla(texto, key):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(claude_client, "buscar_clientes", falla)
    assert "Pide la CSF" in claude_client._resultado_busqueda_receptor(CLIENT_PROFILE, "Envoy")


@pytest.mark.asyncio
async def test_si_falla_guardar_receptor_la_factura_se_entrega(monkeypatch):
    enviados = []

    async def fake_send_message(chat_id, text, reply_markup=None):
        enviados.append(text)

    async def fake_send_document(chat_id, data, filename, caption=None):
        enviados.append(filename)

    async def fake_create_invoice(data, key):
        return {"id": "fapi-1", "series": "F", "folio_number": 7}

    async def falla_guardar(receptor, key):
        raise httpx.ConnectError("sin red")

    async def fake_descarga(folio, key):
        return b"archivo"

    monkeypatch.setattr(main.telegram_client, "send_message", fake_send_message)
    monkeypatch.setattr(main.telegram_client, "send_document", fake_send_document)
    monkeypatch.setattr(main, "create_invoice", fake_create_invoice)
    monkeypatch.setattr(main, "guardar_cliente", falla_guardar)
    monkeypatch.setattr(main, "download_pdf", fake_descarga)
    monkeypatch.setattr(main, "download_xml", fake_descarga)
    monkeypatch.setattr(main.sheets_client, "log_to_bitacora", lambda **kw: None)
    async def fake_email(**kwargs):
        pass

    monkeypatch.setattr(main, "send_invoice_email", fake_email)

    invoice = InvoiceData(
        estatus="confirmado_por_cliente",
        emisor=EmisorData(
            nombre_comercial="Sin Culpa", razon_social="Sin Culpa SA de CV",
            rfc="SIN010101AA1", regimen_fiscal="601", cp_fiscal="06600",
        ),
        receptor=RECEPTOR_PF,
        factura=FacturaData(
            conceptos=[ConceptoItem(descripcion="Servicio", clave_prod_serv="78101803", precio_unitario=Decimal("1000"))],
            monto_antes_impuestos=Decimal("1000"), iva=Decimal("160"), tasa_iva=Decimal("0.16"),
            retencion_iva=Decimal("0"), retencion_isr=Decimal("0"), total_estimado=Decimal("1160"),
            metodo_pago="PUE", forma_pago="03",
        ),
    )
    await main._timbre_and_deliver("inv-1", invoice, CLIENT_PROFILE, "555", 1)

    assert "F-7.pdf" in enviados
