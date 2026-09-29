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
