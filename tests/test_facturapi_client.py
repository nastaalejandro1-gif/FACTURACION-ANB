"""
_build_facturapi_payload / _build_taxes_for_concepto son puras (sin red) —
se prueban sin mockear httpx. Cubre especificamente el punto donde
Decimal debe convertirse a float antes de json.dumps (httpx no sabe
serializar Decimal), y que las tasas de impuesto vienen EXACTAS de
FacturaData, no recalculadas por división.
"""
import json
from decimal import Decimal

from facturapi_client import _build_facturapi_payload, _build_taxes_for_concepto
from models import ConceptoItem, EmisorData, FacturaData, InvoiceData, ReceptorData

EMISOR = EmisorData(
    nombre_comercial="Sin Culpa", razon_social="Sin Culpa SA de CV",
    rfc="SIN010101AA1", regimen_fiscal="601", cp_fiscal="06600",
)
RECEPTOR = ReceptorData(
    razon_social="Empresa SA de CV", rfc="EMP010101AA1",
    regimen_fiscal="601", cp_fiscal="06600", uso_cfdi="G03",
)


def _invoice(conceptos, **factura_kwargs):
    defaults = dict(
        conceptos=conceptos,
        monto_antes_impuestos=sum((c.cantidad * c.precio_unitario for c in conceptos), Decimal("0")),
        ieps=sum((c.ieps for c in conceptos), Decimal("0")),
        iva=Decimal("0"), retencion_iva=Decimal("0"), retencion_isr=Decimal("0"),
        metodo_pago="PUE", forma_pago="03",
    )
    defaults.update(factura_kwargs)
    subtotal = defaults["monto_antes_impuestos"]
    ieps = defaults["ieps"]
    iva = defaults["iva"]
    ret_iva = defaults["retencion_iva"]
    ret_isr = defaults["retencion_isr"]
    defaults["total_estimado"] = subtotal + ieps + iva - ret_iva - ret_isr
    factura = FacturaData(**defaults)
    return InvoiceData(estatus="confirmado_por_cliente", emisor=EMISOR, receptor=RECEPTOR, factura=factura)


def test_payload_es_json_serializable_sin_crashear_por_decimal():
    conceptos = [ConceptoItem(
        descripcion="Servicio", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("5000.00"),
    )]
    invoice_data = _invoice(conceptos, iva=Decimal("800.00"), tasa_iva=Decimal("0.16"))
    payload = _build_facturapi_payload(invoice_data)
    json.dumps(payload)  # no debe lanzar TypeError por Decimal no serializable
    assert payload["items"][0]["quantity"] == 1.0
    assert isinstance(payload["items"][0]["quantity"], float)
    assert isinstance(payload["items"][0]["product"]["price"], float)


def test_iva_usa_tasa_exacta_no_recalculada_por_division():
    conceptos = [ConceptoItem(
        descripcion="Servicio", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("5000.00"),
    )]
    invoice_data = _invoice(conceptos, iva=Decimal("800.00"), tasa_iva=Decimal("0.16"))
    taxes = _build_taxes_for_concepto(conceptos[0], invoice_data)
    iva_tax = next(t for t in taxes if t["type"] == "IVA" and not t["withholding"])
    assert iva_tax["rate"] == 0.16


def test_ieps_solo_se_incluye_si_concepto_tiene_ieps():
    conceptos = [
        ConceptoItem(
            descripcion="Gravado", clave_prod_serv="22101500",
            cantidad=Decimal("1"), precio_unitario=Decimal("100.00"),
            ieps=Decimal("8.00"), ieps_tasa=Decimal("0.08"),
        ),
        ConceptoItem(
            descripcion="Flete", clave_prod_serv="78101800",
            cantidad=Decimal("1"), precio_unitario=Decimal("50.00"),
        ),
    ]
    invoice_data = _invoice(conceptos)

    taxes_gravado = _build_taxes_for_concepto(conceptos[0], invoice_data)
    ieps_tax = next(t for t in taxes_gravado if t["type"] == "IEPS")
    assert ieps_tax["rate"] == 0.08

    taxes_flete = _build_taxes_for_concepto(conceptos[1], invoice_data)
    assert not any(t["type"] == "IEPS" for t in taxes_flete)


def test_retenciones_usan_tasa_exacta_transportada():
    conceptos = [ConceptoItem(
        descripcion="Honorarios", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("5000.00"),
    )]
    invoice_data = _invoice(
        conceptos, iva=Decimal("800.00"), tasa_iva=Decimal("0.16"),
        retencion_iva=Decimal("80.00"), retencion_iva_tasa=Decimal("0.10"),
        retencion_isr=Decimal("500.00"), retencion_isr_tasa=Decimal("0.10"),
    )
    taxes = _build_taxes_for_concepto(conceptos[0], invoice_data)
    ret_iva = next(t for t in taxes if t["type"] == "IVA" and t["withholding"])
    ret_isr = next(t for t in taxes if t["type"] == "ISR" and t["withholding"])
    assert ret_iva["rate"] == 0.10
    assert ret_isr["rate"] == 0.10


def test_ppd_fuerza_forma_pago_99_en_payload():
    conceptos = [ConceptoItem(
        descripcion="Servicio", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("1000.00"),
    )]
    invoice_data = _invoice(conceptos, metodo_pago="PPD", forma_pago="99")
    payload = _build_facturapi_payload(invoice_data)
    assert payload["payment_form"] == "99"
    assert payload["payment_method"] == "PPD"
