"""
Modo despacho: Alejandro y los socios que él autorice facturan desde su chat
a nombre de los clientes (operadores.py + main.py). Todo mockeado.
"""
import json

import pytest

import main
import operadores
from claude_client import build_prompt_operador
from models import ClientProfile, InvoiceData
from tests.test_main_orchestration import REGLAS, _draft, _patch_common

ADMIN = str(main.ALEJANDRO_CHAT_ID)
SOCIO = "777"


def _perfil(id_cliente: str, nombre: str, rfc: str, canal_id: str = "") -> ClientProfile:
    return ClientProfile(
        despacho_id="ANB-001", id_cliente=id_cliente, nombre_comercial=nombre,
        razon_social=f"{nombre} SA de CV", rfc=rfc, canal="telegram",
        canal_id=canal_id, email_factura=f"f@{nombre.lower()}.mx", tipo_persona="PM",
        regimen_fiscal="601", cp_fiscal="06600", iva_aplica="SI",
        retencion_iva=0.0, retencion_isr=0.0, ieps_rate=0.0,
        clave_prod_serv_default="84111500", requiere_revision=False,
        notas_fiscales="", activo=True, facturapi_key=f"key-{id_cliente}",
    )


ENVOY_1 = _perfil("1", "Envoy", "TOID000104AI5", "8997334380")
ENVOY_3 = _perfil("3", "Envoy", "TOID000104AI5", "1126165630")
ANB_4 = _perfil("4", "ANB Consultores", "AUMR0101073N1", ADMIN)
ANB_5 = _perfil("5", "ANB Consultores", "AUMR0101073N1", "5920872743")
SIN_CULPA = _perfil("2", "Sin Culpa", "LOEA962322PX3", "8208800448")


def _clientes(operadores_envoy=(), operadores_sin_culpa=()):
    return [
        (ENVOY_1, list(operadores_envoy)), (ENVOY_3, []), (ANB_4, []), (ANB_5, []),
        (SIN_CULPA, list(operadores_sin_culpa)),
    ]


# ---------------------------------------------------------------------------
# Quién puede facturar como quién
# ---------------------------------------------------------------------------

def test_alejandro_ve_todos_los_clientes_una_vez_por_rfc_con_su_propia_fila():
    emisores = operadores.calcular_emisores(ADMIN, True, _clientes())
    assert [e.id_cliente for e in emisores] == ["4", "1", "2"]  # ANB (la suya), Envoy (canónica), Sin Culpa


def test_socio_sin_permisos_no_esta_en_modo_despacho():
    assert operadores.calcular_emisores(SOCIO, False, _clientes()) is None


def test_socio_solo_ve_los_clientes_que_alejandro_le_dio():
    emisores = operadores.calcular_emisores(SOCIO, False, _clientes(operadores_envoy=[SOCIO]))
    assert [e.id_cliente for e in emisores] == ["1"]


def test_socio_de_anb_ve_todos_los_clientes_sin_permiso_y_conserva_su_fila():
    emisores = operadores.calcular_emisores(ANB_5.canal_id, False, _clientes())
    assert [e.id_cliente for e in emisores] == ["5", "1", "2"]  # ANB (la suya), Envoy, Sin Culpa


def test_cliente_normal_no_es_socio_aunque_tenga_varias_filas():
    assert operadores.calcular_emisores(ENVOY_3.canal_id, False, _clientes()) is None


def test_externo_con_fila_propia_la_conserva_como_emisor():
    externo = ENVOY_3.canal_id
    emisores = operadores.calcular_emisores(externo, False, _clientes(operadores_sin_culpa=[externo]))
    assert {e.id_cliente for e in emisores} == {"3", "2"}


def test_buscar_emisor_sin_acentos_ni_mayusculas_y_por_rfc():
    emisores = [ENVOY_1, ANB_4, SIN_CULPA]
    assert operadores.buscar_emisor("envoy", emisores) == [ENVOY_1]
    assert operadores.buscar_emisor("sin culpa", emisores) == [SIN_CULPA]
    assert operadores.buscar_emisor("aumr0101073n1", emisores) == [ANB_4]
    assert operadores.buscar_emisor("xyz", emisores) == []


def test_mencionados_detecta_el_nombre_completo_en_el_mensaje():
    emisores = [ENVOY_1, ANB_4, SIN_CULPA]
    assert operadores.mencionados("Factúrame a nombre de Envoy a Zepeda por 10,000", emisores) == [ENVOY_1]
    assert operadores.mencionados("factura para Zepeda por 10,000", emisores) == []


def test_perfil_operador_rechaza_emisor_sin_permiso(monkeypatch):
    monkeypatch.setattr(operadores.sheets_client, "get_clientes_activos", lambda: _clientes(operadores_envoy=[SOCIO]))
    assert operadores.perfil_operador(SOCIO, "1").operador_canal_id == SOCIO
    assert operadores.perfil_operador(SOCIO, "2") is None  # Sin Culpa: sin permiso


def test_prompt_operador_lista_emisores_y_exige_seleccionar():
    texto = build_prompt_operador(ENVOY_1, [ENVOY_1, ANB_4])
    assert "id 4 = ANB Consultores (RFC AUMR0101073N1)" in texto
    assert "seleccionar_emisor" in texto
    assert "Emisor: {nombre} ({RFC})" in texto


# ---------------------------------------------------------------------------
# Flujo de factura en modo despacho
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operador_resumen_muestra_emisor_y_guarda_su_id(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", lambda *a, **kw: REGLAS)

    perfil = operadores.como_operador(ENVOY_1, SOCIO)
    await main._calcular_y_procesar_factura("inv-op", _draft("84111500"), perfil, SOCIO, 1)

    assert saved_pending[0]["tipo_aprobacion"] == "cliente_confirmacion"
    assert saved_pending[0]["canal_id_aprobador"] == SOCIO
    assert InvoiceData(**json.loads(saved_pending[0]["invoice_json"])).emisor.id_cliente == "1"
    resumen = sent[-1][1]
    assert "Emisor: Envoy (TOID000104AI5)" in resumen
    assert "Receptor: Juan Pérez (PERJ800101AB1)" in resumen


@pytest.mark.asyncio
async def test_operador_clave_nueva_no_escala_se_guarda_en_catalogo(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", lambda *a, **kw: REGLAS)
    guardadas = []
    monkeypatch.setattr(main.sheets_client, "save_clave_aprobada", lambda **kw: guardadas.append(kw))

    perfil = operadores.como_operador(ENVOY_1, SOCIO)
    await main._calcular_y_procesar_factura("inv-op2", _draft("NUEVA", "80111600"), perfil, SOCIO, 1)

    assert [g["clave_prod_serv"] for g in guardadas] == ["80111600"]
    assert guardadas[0]["id_cliente"] == "1"
    assert saved_pending[0]["tipo_aprobacion"] == "cliente_confirmacion"  # no anb_revision
    assert not any(chat == main.ALEJANDRO_CHAT_ID for chat, _, _ in sent)


@pytest.mark.asyncio
async def test_operador_monto_alto_no_escala_pero_avisa_en_el_resumen(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    reglas = REGLAS.__class__(**{**REGLAS.__dict__, "monto_maximo_sin_autorizacion": REGLAS.tasa_iva * 0})
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", lambda *a, **kw: reglas)

    perfil = operadores.como_operador(ENVOY_1, SOCIO)
    await main._calcular_y_procesar_factura("inv-op3", _draft("84111500"), perfil, SOCIO, 1)

    assert saved_pending[0]["tipo_aprobacion"] == "cliente_confirmacion"
    assert "⚠️ Revisa antes de confirmar" in sent[-1][1]


@pytest.mark.asyncio
async def test_confirmacion_de_operador_timbra_con_el_emisor_y_sin_correo(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a: None)
    monkeypatch.setattr(main.sheets_client, "get_clientes_activos", lambda: _clientes(operadores_envoy=[SOCIO]))

    perfil = operadores.como_operador(ENVOY_1, SOCIO)
    invoice = main._build_invoice_data(
        _draft("84111500"), perfil,
        main.fiscal_engine.calcular_factura(
            [main.fiscal_engine.ConceptoExtraido("Servicio", main.Decimal("1"), main.Decimal("1000"), "E48", "84111500")],
            _draft("84111500").receptor, REGLAS, "PUE", "03",
        ).factura,
    )
    llaves, correos = [], []

    async def fake_create(data, key):
        llaves.append(key)
        return {"id": "fac-1", "series": "F", "folio_number": 7}

    async def fake_download(folio, key):
        return b"x"

    async def fake_doc(*a, **kw):
        pass

    async def fake_guardar(*a, **kw):
        pass

    async def fake_email(**kw):
        correos.append(kw)

    monkeypatch.setattr(main, "create_invoice", fake_create)
    monkeypatch.setattr(main, "download_pdf", fake_download)
    monkeypatch.setattr(main, "download_xml", fake_download)
    monkeypatch.setattr(main, "guardar_cliente", fake_guardar)
    monkeypatch.setattr(main.telegram_client, "send_document", fake_doc)
    monkeypatch.setattr(main, "send_invoice_email", fake_email)

    pending = {
        "id": "inv-op4", "estado": "pendiente", "canal": "telegram", "canal_id": SOCIO,
        "invoice_json": invoice.model_dump_json(), "telegram_message_id": 1,
    }
    await main._execute_client_confirmation("cliente_si", "inv-op4", pending)

    assert llaves == ["key-1"]  # la llave de Envoy, no la del chat
    assert correos == []  # solo a quien la pidió


@pytest.mark.asyncio
async def test_confirmacion_de_operador_sin_permiso_ya_no_timbra(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a: None)
    monkeypatch.setattr(main.sheets_client, "get_clientes_activos", lambda: _clientes())  # permiso quitado

    perfil = operadores.como_operador(ENVOY_1, SOCIO)
    invoice = main._build_invoice_data(
        _draft("84111500"), perfil,
        main.fiscal_engine.calcular_factura(
            [main.fiscal_engine.ConceptoExtraido("Servicio", main.Decimal("1"), main.Decimal("1000"), "E48", "84111500")],
            _draft("84111500").receptor, REGLAS, "PUE", "03",
        ).factura,
    )

    async def boom(*a, **kw):
        raise AssertionError("no debe timbrar")
    monkeypatch.setattr(main, "create_invoice", boom)

    pending = {
        "id": "inv-op5", "estado": "pendiente", "canal": "telegram", "canal_id": SOCIO,
        "invoice_json": invoice.model_dump_json(), "telegram_message_id": 1,
    }
    await main._execute_client_confirmation("cliente_si", "inv-op5", pending)
    assert any("Ya no tienes permiso" in text for chat, text, _ in sent if chat == SOCIO)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permiso_se_guarda_en_la_fila_canonica(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_clientes_activos", lambda: _clientes())
    guardados = []
    monkeypatch.setattr(main.sheets_client, "set_operadores", lambda id_cliente, ops: guardados.append((id_cliente, ops)))

    await main.handle_admin_operadores(f"/permiso {SOCIO} envoy")

    assert guardados == [("1", [SOCIO])]
    assert "ya puede facturar a nombre de: Envoy" in sent[-1][1]


@pytest.mark.asyncio
async def test_quitar_lo_borra_de_todas_las_filas_del_rfc(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    clientes = _clientes(operadores_envoy=[SOCIO])
    clientes[1] = (ENVOY_3, [SOCIO])  # puesto a mano en la otra fila de Envoy
    monkeypatch.setattr(main.sheets_client, "get_clientes_activos", lambda: clientes)
    guardados = []
    monkeypatch.setattr(main.sheets_client, "set_operadores", lambda id_cliente, ops: guardados.append((id_cliente, ops)))

    await main.handle_admin_operadores(f"/quitar {SOCIO} envoy")

    assert sorted(guardados) == [("1", []), ("3", [])]


@pytest.mark.asyncio
async def test_comando_emisor_cambia_y_reinicia_conversacion(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    activo, historial = [], []
    monkeypatch.setattr(main.sheets_client, "set_emisor_activo", lambda chat, id_cliente: activo.append(id_cliente))
    monkeypatch.setattr(main.sheets_client, "save_history", lambda canal, chat, h: historial.append(h))

    await main._comando_emisor(SOCIO, "envoy", [ENVOY_1, SIN_CULPA])

    assert activo == ["1"] and historial == [[]]
    assert "Envoy" in sent[-1][1]


@pytest.mark.asyncio
async def test_comando_emisor_sin_permiso_no_cambia(monkeypatch):
    sent, _, _ = _patch_common(monkeypatch)
    activo = []
    monkeypatch.setattr(main.sheets_client, "set_emisor_activo", lambda chat, id_cliente: activo.append(id_cliente))

    await main._comando_emisor(SOCIO, "sin culpa", [ENVOY_1])

    assert activo == []
    assert "No tienes permiso" in sent[-1][1]
