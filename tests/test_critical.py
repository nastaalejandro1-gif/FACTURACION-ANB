"""
TC-01 Idempotency, TC-02 Authorization, TC-03 Pydantic validation,
TC-04 ToolUseBlock serialization, TC-05 forma_pago enum, TC-06 webhook secret.
"""
import json
import pytest
from pydantic import ValidationError

from models import InvoiceData, ReceptorData, FacturaData, EmisorData


# ---------------------------------------------------------------------------
# TC-03: Pydantic validation blocks bad tool_input
# ---------------------------------------------------------------------------

VALID_INVOICE = {
    "estatus": "confirmado_por_cliente",
    "requiere_revision": False,
    "motivo_revision": "",
    "emisor": {
        "nombre_comercial": "Envoy",
        "razon_social": "DANTE FABRIZIO TORRES IBARRA",
        "rfc": "TOID000104AI5",
        "regimen_fiscal": "621",
        "cp_fiscal": "45019",
    },
    "receptor": {
        "razon_social": "EMPRESA SA DE CV",
        "rfc": "EMP010101AA1",
        "regimen_fiscal": "601",
        "cp_fiscal": "06600",
        "uso_cfdi": "G03",
    },
    "factura": {
        "conceptos": [
            {
                "descripcion": "Servicios de contabilidad",
                "clave_prod_serv": "78101803",
                "cantidad": 1,
                "clave_unidad": "E48",
                "precio_unitario": 5000.0,
            },
        ],
        "monto_antes_impuestos": 5000.0,
        "iva": 800.0,
        "retencion_iva": 85.36,
        "retencion_isr": 62.5,
        "total_estimado": 5652.14,
        "metodo_pago": "PUE",
        "forma_pago": "03",
        "observaciones": "",
    },
}


def test_valid_invoice_passes():
    data = InvoiceData(**VALID_INVOICE)
    assert data.factura.monto_antes_impuestos == 5000.0


def test_negative_monto_rejected():
    bad = dict(VALID_INVOICE)
    bad["factura"] = {**VALID_INVOICE["factura"], "monto_antes_impuestos": -5000.0}
    with pytest.raises(ValidationError) as exc_info:
        InvoiceData(**bad)
    assert "monto_antes_impuestos" in str(exc_info.value)


def test_invalid_rfc_receptor_rejected():
    bad = dict(VALID_INVOICE)
    bad["receptor"] = {**VALID_INVOICE["receptor"], "rfc": "INVALID-RFC"}
    with pytest.raises(ValidationError) as exc_info:
        InvoiceData(**bad)
    assert "RFC inválido" in str(exc_info.value)


def test_rfc_too_short_rejected():
    bad = dict(VALID_INVOICE)
    bad["receptor"] = {**VALID_INVOICE["receptor"], "rfc": "EMP01010"}
    with pytest.raises(ValidationError):
        InvoiceData(**bad)


def test_total_inconsistency_rejected():
    bad = dict(VALID_INVOICE)
    bad["factura"] = {**VALID_INVOICE["factura"], "total_estimado": 9999.0}  # way off
    with pytest.raises(ValidationError) as exc_info:
        InvoiceData(**bad)
    assert "total" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TC-05: forma_pago enum constraint
# ---------------------------------------------------------------------------

def test_forma_pago_natural_language_rejected():
    bad = dict(VALID_INVOICE)
    bad["factura"] = {**VALID_INVOICE["factura"], "forma_pago": "transferencia bancaria"}
    with pytest.raises(ValidationError) as exc_info:
        InvoiceData(**bad)
    assert "Forma de pago" in str(exc_info.value)


def test_forma_pago_valid_code_accepted():
    data = InvoiceData(**VALID_INVOICE)
    assert data.factura.forma_pago == "03"


def test_metodo_pago_invalid_rejected():
    bad = dict(VALID_INVOICE)
    bad["factura"] = {**VALID_INVOICE["factura"], "metodo_pago": "pago diferido"}
    with pytest.raises(ValidationError):
        InvoiceData(**bad)


# ---------------------------------------------------------------------------
# TC-04: ToolUseBlock serialization (simulate SDK object)
# ---------------------------------------------------------------------------

def test_tooluse_block_model_dump_serializable():
    """
    Simulates what happens when claude_client stores response.content in history.
    SDK objects must be converted via model_dump() before json.dumps().
    """
    class FakeBlock:
        type = "tool_use"
        id = "toolu_01"
        name = "generate_invoice_data"
        input = {"estatus": "confirmado_por_cliente"}

        def model_dump(self):
            return {
                "type": self.type,
                "id": self.id,
                "name": self.name,
                "input": self.input,
            }

    block = FakeBlock()
    serialized = block.model_dump() if hasattr(block, "model_dump") else block
    result = json.dumps(serialized)  # must not raise
    assert "tool_use" in result


def test_plain_dict_content_serializable():
    content = [{"type": "text", "text": "Hola"}]
    result = json.dumps(content)
    assert "Hola" in result


# ---------------------------------------------------------------------------
# TC-02: Authorization — /aprobar only for Alejandro
# (unit test of the guard logic, not HTTP)
# ---------------------------------------------------------------------------

def test_alejandro_chat_id_check():
    from config import ALEJANDRO_CHAT_ID

    def is_alejandro(chat_id: str) -> bool:
        return int(chat_id) == ALEJANDRO_CHAT_ID

    assert is_alejandro("999999999") is True
    assert is_alejandro("12345678") is False
    assert is_alejandro("999999998") is False


# ---------------------------------------------------------------------------
# TC-06: Webhook secret validation (HTTP level)
# ---------------------------------------------------------------------------

def test_webhook_rejects_missing_secret():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)

    response = client.post("/webhook", json={"update_id": 1})
    assert response.status_code == 403


def test_webhook_rejects_wrong_secret():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 403


def test_webhook_accepts_correct_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    # Mock process_update so it doesn't actually call anything
    monkeypatch.setattr("main.process_update", lambda update: None)

    client = TestClient(app)
    response = client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# TC-07: /check-pending authentication
# ---------------------------------------------------------------------------

def test_check_pending_rejects_no_secret():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    response = client.post("/check-pending")
    assert response.status_code == 403


def test_check_pending_rejects_wrong_secret():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    response = client.post("/check-pending", headers={"X-Cron-Secret": "wrong"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# TC-13: RFC regex validation
# ---------------------------------------------------------------------------

def test_rfc_valid_pf():
    r = ReceptorData(
        razon_social="Juan Pérez",
        rfc="PERJ800101AB1",
        regimen_fiscal="612",
        cp_fiscal="44100",
        uso_cfdi="G03",
    )
    assert r.rfc == "PERJ800101AB1"


def test_rfc_valid_pm():
    r = ReceptorData(
        razon_social="Empresa SA",
        rfc="EMP010101AA1",
        regimen_fiscal="601",
        cp_fiscal="06600",
        uso_cfdi="G03",
    )
    assert r.rfc == "EMP010101AA1"


def test_rfc_lowercase_normalized():
    r = ReceptorData(
        razon_social="Empresa SA",
        rfc="emp010101aa1",
        regimen_fiscal="601",
        cp_fiscal="06600",
        uso_cfdi="G03",
    )
    assert r.rfc == "EMP010101AA1"


# ---------------------------------------------------------------------------
# TC-14: Ruteo de pendientes — un REP se distingue por uuid_factura_origen
# ---------------------------------------------------------------------------

VALID_REP = {
    "estatus": "confirmado_por_cliente",
    "uuid_factura_origen": "12345678-ABCD-1234-ABCD-123456789012",
    "receptor": VALID_INVOICE["receptor"],
    "fecha_pago": "2026-06-01T12:00:00",
    "forma_pago": "03",
    "monto_pagado": 5652.14,
    "requiere_revision": False,
    "motivo_revision": "",
}


def test_pending_rep_detected_by_uuid_field():
    from models import RepData
    # La regla de main.handle_approval_command: presencia de uuid_factura_origen → REP
    assert "uuid_factura_origen" in VALID_REP
    assert "uuid_factura_origen" not in VALID_INVOICE
    data = RepData(**VALID_REP)
    assert data.monto_pagado == 5652.14


def test_pending_invoice_payload_not_valid_rep():
    from models import RepData
    with pytest.raises(ValidationError):
        RepData(**VALID_INVOICE)


def test_rfc_with_spaces_rejected():
    with pytest.raises(ValidationError):
        ReceptorData(
            razon_social="X",
            rfc="EMP 010101 AA1",
            regimen_fiscal="601",
            cp_fiscal="06600",
            uso_cfdi="G03",
        )
