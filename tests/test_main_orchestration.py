"""
Tests de la orquestación nueva en main.py: escalamiento vs. confirmación
del cliente, y el chequeo de seguridad de que un cliente no puede aprobar
la factura de otro. Todo mockeado (sheets_client, telegram_client,
facturapi_client) — nunca contra Supabase/Telegram/FacturAPI reales.
"""
from decimal import Decimal

import pytest

import main
from models import ClientProfile, ConceptoDraft, FacturaDraft, InvoiceDraft, ReceptorData

RECEPTOR_PF = ReceptorData(
    razon_social="Juan Pérez", rfc="PERJ800101AB1",
    regimen_fiscal="612", cp_fiscal="44100", uso_cfdi="G03",
)

RECEPTOR_INCONGRUENTE = ReceptorData(
    razon_social="Empresa SA de CV", rfc="EMP010101AA1",  # PM por longitud
    regimen_fiscal="612",  # régimen exclusivo de PF -> choca
    cp_fiscal="06600", uso_cfdi="G03",
)

CLIENT_PROFILE = ClientProfile(
    despacho_id="ANB-001", id_cliente="2", nombre_comercial="Sin Culpa",
    razon_social="Sin Culpa SA de CV", rfc="SIN010101AA1", canal="telegram",
    canal_id="555", email_factura="f@sinculpa.mx", tipo_persona="PM",
    regimen_fiscal="601", cp_fiscal="06600", iva_aplica="SI",
    retencion_iva=0.0, retencion_isr=0.0, ieps_rate=0.0,
    clave_prod_serv_default="78101803", requiere_revision=False,
    notas_fiscales="", activo=True, facturapi_key="fake-key",
)


def _draft(clave: str, clave_propuesta: str = "") -> InvoiceDraft:
    return InvoiceDraft(
        estatus="confirmado_por_cliente",
        receptor=RECEPTOR_PF,
        factura=FacturaDraft(
            conceptos=[ConceptoDraft(
                descripcion="Servicio", cantidad=1, clave_unidad="E48",
                precio_unitario=1000.0, clave_prod_serv=clave,
                clave_prod_serv_propuesta=clave_propuesta,
            )],
            metodo_pago="PUE", forma_pago="03",
        ),
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _patch_common(monkeypatch):
    sent = []

    async def fake_send_message(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(main.telegram_client, "send_message", fake_send_message)

    saved_pending = []

    def fake_save_pending(**kwargs):
        saved_pending.append(kwargs)

    monkeypatch.setattr(main.sheets_client, "save_pending", fake_save_pending)

    bitacora_calls = []
    monkeypatch.setattr(main.sheets_client, "log_to_bitacora", lambda **kw: bitacora_calls.append(kw))

    return sent, saved_pending, bitacora_calls


REGLAS = main.fiscal_engine.FiscalRules(
    iva_aplica=True, tasa_iva=Decimal("0.16"),
    retencion_iva_tasa=Decimal("0"), retencion_isr_tasa=Decimal("0"),
    ieps_tasa=Decimal("0"), claves_con_ieps=frozenset(),
)


@pytest.mark.asyncio
async def test_clave_nueva_escala_a_anb_sin_llamar_fiscal_engine(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("no debería llamarse get_fiscal_rules cuando hay clave NUEVA")
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", boom)

    draft = _draft("NUEVA", clave_propuesta="90111500")
    await main._calcular_y_procesar_factura("inv-1", draft, CLIENT_PROFILE, "555", 1)

    assert len(saved_pending) == 1
    assert saved_pending[0]["tipo_aprobacion"] == "anb_revision"
    assert "clave_prod_serv_nueva" in saved_pending[0]["motivo_revision"]
    # el cliente recibe el mensaje de "en revisión", ANB recibe el de aprobación
    assert any("revisada por el despacho" in text for _, text, _ in sent)
    assert any("Aprobar" in str(markup) for _, _, markup in sent if markup)


@pytest.mark.asyncio
async def test_regimen_incongruente_escala_validacion_aritmetica(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", lambda *a, **kw: REGLAS)

    draft = InvoiceDraft(
        estatus="confirmado_por_cliente", receptor=RECEPTOR_INCONGRUENTE,
        factura=FacturaDraft(
            conceptos=[ConceptoDraft(
                descripcion="Servicio", cantidad=1, clave_unidad="E48",
                precio_unitario=1000.0, clave_prod_serv="78101803",
            )],
            metodo_pago="PUE", forma_pago="03",
        ),
    )
    await main._calcular_y_procesar_factura("inv-2", draft, CLIENT_PROFILE, "555", 1)

    assert len(saved_pending) == 1
    assert saved_pending[0]["tipo_aprobacion"] == "anb_revision"
    assert "validacion_aritmetica" in saved_pending[0]["motivo_revision"]


@pytest.mark.asyncio
async def test_factura_limpia_pide_confirmacion_al_cliente_no_timbra_directo(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_fiscal_rules", lambda *a, **kw: REGLAS)

    timbrada = []
    async def fake_timbre(*a, **kw):
        timbrada.append((a, kw))
    monkeypatch.setattr(main, "_timbre_and_deliver", fake_timbre)

    draft = _draft("78101803")
    await main._calcular_y_procesar_factura("inv-3", draft, CLIENT_PROFILE, "555", 1)

    assert timbrada == []  # NUNCA timbra directo, siempre pasa por confirmación
    assert len(saved_pending) == 1
    assert saved_pending[0]["tipo_aprobacion"] == "cliente_confirmacion"
    assert saved_pending[0]["canal_id_aprobador"] == "555"
    assert any("¿Confirmas" in text for _, text, _ in sent)


@pytest.mark.asyncio
async def test_cliente_no_puede_aprobar_factura_de_otro_cliente(monkeypatch):
    """El hueco de seguridad central: invoice_id conocido no basta, el
    canal_id debe coincidir con canal_id_aprobador guardado en pendientes."""
    answered = []
    async def fake_answer(callback_id, text=""):
        answered.append(text)
    monkeypatch.setattr(main.telegram_client, "answer_callback_query", fake_answer)

    ejecutado = []
    async def fake_execute_confirmation(command, invoice_id, pending):
        ejecutado.append((command, invoice_id))
    monkeypatch.setattr(main, "_execute_client_confirmation", fake_execute_confirmation)

    monkeypatch.setattr(
        main.sheets_client, "get_pending",
        lambda invoice_id: {
            "id": invoice_id, "estado": "pendiente",
            "tipo_aprobacion": "cliente_confirmacion",
            "canal_id_aprobador": "555",  # el cliente legítimo
            "canal_id": "555",
        },
    )

    callback_query = {
        "id": "cb1",
        "message": {"chat": {"id": 999}},  # OTRO chat_id, no 555
        "data": "cliente_si:inv-3",
    }
    await main.handle_callback_query(callback_query)

    assert ejecutado == []  # nunca se ejecutó la aprobación
    assert "No autorizado" in answered


@pytest.mark.asyncio
async def test_cliente_legitimo_si_puede_confirmar(monkeypatch):
    async def fake_answer(callback_id, text=""):
        pass
    monkeypatch.setattr(main.telegram_client, "answer_callback_query", fake_answer)

    ejecutado = []
    async def fake_execute_confirmation(command, invoice_id, pending):
        ejecutado.append((command, invoice_id))
    monkeypatch.setattr(main, "_execute_client_confirmation", fake_execute_confirmation)

    monkeypatch.setattr(
        main.sheets_client, "get_pending",
        lambda invoice_id: {
            "id": invoice_id, "estado": "pendiente",
            "tipo_aprobacion": "cliente_confirmacion",
            "canal_id_aprobador": "555",
            "canal_id": "555",
        },
    )

    callback_query = {
        "id": "cb2",
        "message": {"chat": {"id": 555}},  # el mismo cliente
        "data": "cliente_si:inv-3",
    }
    await main.handle_callback_query(callback_query)

    assert ejecutado == [("cliente_si", "inv-3")]


# ---------------------------------------------------------------------------
# REP también pasa por confirmación del cliente (pedido explícito: "el botón
# de confirmación si hay que ponerlo también a los REPs")
# ---------------------------------------------------------------------------

REP_DRAFT_RECEPTOR = ReceptorData(
    razon_social="Distribuidora del Valle SA de CV", rfc="DVA010101AA1",
    regimen_fiscal="601", cp_fiscal="06600", uso_cfdi="CP01",
)


def _rep_draft(monto_pagado: float = 1000.0):
    from models import RepDraft
    return RepDraft(
        estatus="confirmado_por_cliente",
        uuid_factura_origen="C8171E7E-283A-4AC7-BF7F-5584051E5A9D",
        receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-09-29T12:00:00",
        forma_pago="03",
        monto_pagado=monto_pagado,
    )


@pytest.mark.asyncio
async def test_rep_limpio_pide_confirmacion_al_cliente_no_timbra_directo(monkeypatch):
    sent, saved_pending, _ = _patch_common(monkeypatch)

    async def fake_search(uuid, key):
        return {"total": 5000.0}
    monkeypatch.setattr(main, "search_invoice_by_uuid", fake_search)
    monkeypatch.setattr(main.sheets_client, "get_rep_history", lambda uuid: [])

    timbrado = []
    async def fake_timbre_rep(*a, **kw):
        timbrado.append((a, kw))
    monkeypatch.setattr(main, "_timbre_and_deliver_rep", fake_timbre_rep)

    draft = _rep_draft(monto_pagado=1000.0)
    await main._calcular_y_timbrar_rep("rep-1", draft, CLIENT_PROFILE, "555", 1)

    assert timbrado == []  # nunca timbra directo
    assert len(saved_pending) == 1
    assert saved_pending[0]["tipo_aprobacion"] == "cliente_confirmacion"
    assert saved_pending[0]["canal_id_aprobador"] == "555"
    assert any("complemento de pago" in text.lower() for _, text, _ in sent)


@pytest.mark.asyncio
async def test_confirmacion_cliente_de_rep_reconstruye_fresco_y_timbra(monkeypatch):
    """Al confirmar, se re-busca la factura original y el historial de REPs
    frescos (no se reusa lo calculado al momento de la vista previa)."""
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_client_by_canal_id", lambda canal, cid: CLIENT_PROFILE)
    monkeypatch.setattr(main.sheets_client, "update_pending_status", lambda *a, **kw: None)

    async def fake_search(uuid, key):
        return {"total": 5000.0}
    monkeypatch.setattr(main, "search_invoice_by_uuid", fake_search)
    monkeypatch.setattr(main.sheets_client, "get_rep_history", lambda uuid: [])

    timbrado = []
    async def fake_timbre_rep(invoice_id, rep_data, client_profile, chat_id, facturapi_key, num_parcialidad, original_invoice):
        timbrado.append((invoice_id, rep_data, num_parcialidad, original_invoice))
    monkeypatch.setattr(main, "_timbre_and_deliver_rep", fake_timbre_rep)

    rep_data = main.RepData(
        estatus="confirmado_por_cliente",
        uuid_factura_origen="C8171E7E-283A-4AC7-BF7F-5584051E5A9D",
        receptor=REP_DRAFT_RECEPTOR,
        fecha_pago="2026-09-29T12:00:00", forma_pago="03",
        monto_pagado=Decimal("1000.00"), imp_saldo_ant=Decimal("1000.00"),
        imp_saldo_insoluto=Decimal("0.00"),
    )
    pending = {
        "id": "rep-1", "estado": "pendiente", "canal_id": "555", "canal": "telegram",
        "invoice_json": rep_data.model_dump_json(), "telegram_message_id": 0,
    }
    await main._execute_client_confirmation("cliente_si", "rep-1", pending)

    assert len(timbrado) == 1
    invoice_id, rep_data_out, num_parcialidad, original_invoice = timbrado[0]
    assert invoice_id == "rep-1"
    assert num_parcialidad == 1
    assert original_invoice == {"total": 5000.0}
