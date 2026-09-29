"""
_build_facturapi_payload / _build_taxes_for_concepto son puras (sin red) —
se prueban sin mockear httpx.

Cubre: (1) Decimal debe convertirse a float antes de json.dumps (httpx no
sabe serializar Decimal); (2) IVA e IEPS se mandan con la tasa exacta que
calculó fiscal_engine (su base coincide con la que usa FacturAPI); (3) las
RETENCIONES se back-calculan (fiscal_engine las calcula contra una base
distinta a la que usa FacturAPI internamente — ver
facturapi_client._calcular_tasas_retencion_efectivas) de forma que
rate * base_de_FacturAPI, sumado en todos los conceptos, reconstruye
EXACTO el monto que calculó fiscal_engine — no la tasa "semántica" tal
cual. Este último punto se descubrió corriendo un timbrado real contra el
sandbox de FacturAPI: mandar la tasa semántica directo producía una
retención ~6x más grande que la correcta.
"""
import json
from decimal import Decimal

from facturapi_client import (
    _build_facturapi_payload,
    _build_taxes_for_concepto,
    _calcular_tasas_retencion_efectivas,
)
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
    retenciones = _calcular_tasas_retencion_efectivas(invoice_data)
    taxes = _build_taxes_for_concepto(conceptos[0], invoice_data, retenciones)
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
    retenciones = _calcular_tasas_retencion_efectivas(invoice_data)

    taxes_gravado = _build_taxes_for_concepto(conceptos[0], invoice_data, retenciones)
    ieps_tax = next(t for t in taxes_gravado if t["type"] == "IEPS")
    assert ieps_tax["rate"] == 0.08

    taxes_flete = _build_taxes_for_concepto(conceptos[1], invoice_data, retenciones)
    assert not any(t["type"] == "IEPS" for t in taxes_flete)


def test_retenciones_se_back_calculan_para_que_facturapi_reconstruya_el_monto_exacto():
    """
    fiscal_engine calcula retencion_iva = iva_amount * tasa (validado contra
    el fixture histórico del despacho: 800*0.1067=85.36) y retencion_isr =
    subtotal_sin_ieps * tasa — bases DISTINTAS a la que usa FacturAPI
    (precio_concepto + ieps_concepto). Si se manda la tasa "semántica" tal
    cual, FacturAPI calcula un monto totalmente distinto. Este test simula
    exactamente eso: rate * base_facturapi sumado en todos los conceptos
    debe reconstruir el monto que calculó fiscal_engine, no otra cosa.
    """
    conceptos = [
        ConceptoItem(
            descripcion="Botana gravada", clave_prod_serv="50192100",
            cantidad=Decimal("20"), precio_unitario=Decimal("45.00"),
            ieps=Decimal("72.00"), ieps_tasa=Decimal("0.08"),
        ),
        ConceptoItem(
            descripcion="Flete", clave_prod_serv="78101800",
            cantidad=Decimal("1"), precio_unitario=Decimal("200.00"),
        ),
    ]
    invoice_data = _invoice(
        conceptos, iva=Decimal("187.52"), tasa_iva=Decimal("0.16"),
        retencion_iva=Decimal("20.01"), retencion_iva_tasa=Decimal("0.1067"),
        retencion_isr=Decimal("13.75"), retencion_isr_tasa=Decimal("0.0125"),
    )
    retenciones = _calcular_tasas_retencion_efectivas(invoice_data)

    total_ret_iva_reconstruido = Decimal("0")
    total_ret_isr_reconstruido = Decimal("0")
    for c in conceptos:
        base_facturapi = c.cantidad * c.precio_unitario + c.ieps  # como calcula FacturAPI, confirmado en sandbox
        taxes = _build_taxes_for_concepto(c, invoice_data, retenciones)
        ret_iva_tax = next(t for t in taxes if t["type"] == "IVA" and t["withholding"])
        ret_isr_tax = next(t for t in taxes if t["type"] == "ISR" and t["withholding"])
        total_ret_iva_reconstruido += base_facturapi * Decimal(str(ret_iva_tax["rate"]))
        total_ret_isr_reconstruido += base_facturapi * Decimal(str(ret_isr_tax["rate"]))

    # Tolerancia mínima por redondeo de 6 decimales en la tasa (no por
    # aritmética float) — nunca más de un centavo en un monto de este tamaño.
    assert abs(total_ret_iva_reconstruido - invoice_data.factura.retencion_iva) < Decimal("0.01")
    assert abs(total_ret_isr_reconstruido - invoice_data.factura.retencion_isr) < Decimal("0.01")


def test_sin_retenciones_no_se_manda_impuesto_de_retencion():
    conceptos = [ConceptoItem(
        descripcion="Servicio", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("1000.00"),
    )]
    invoice_data = _invoice(conceptos)
    retenciones = _calcular_tasas_retencion_efectivas(invoice_data)
    assert retenciones == (Decimal("0"), Decimal("0"))
    taxes = _build_taxes_for_concepto(conceptos[0], invoice_data, retenciones)
    assert not any(t["withholding"] for t in taxes)


def test_ppd_fuerza_forma_pago_99_en_payload():
    conceptos = [ConceptoItem(
        descripcion="Servicio", clave_prod_serv="78101803",
        cantidad=Decimal("1"), precio_unitario=Decimal("1000.00"),
    )]
    invoice_data = _invoice(conceptos, metodo_pago="PPD", forma_pago="99")
    payload = _build_facturapi_payload(invoice_data)
    assert payload["payment_form"] == "99"
    assert payload["payment_method"] == "PPD"
