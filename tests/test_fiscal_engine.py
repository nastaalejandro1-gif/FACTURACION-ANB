"""
Casos dorados del motor de cálculo fiscal — tolerancia CERO.

Asserts de igualdad exacta en Decimal, nunca pytest.approx: si un caso no
cuadra al centavo es un bug del motor, no una discrepancia esperada.
"""
from decimal import Decimal

import pytest

from escalation import EscalationReason
from fiscal_engine import (
    ConceptoExtraido,
    FiscalRules,
    calcular_factura,
    calcular_rep,
)
from models import ReceptorData

RECEPTOR_PF = ReceptorData(
    razon_social="Juan Pérez",
    rfc="PERJ800101AB1",  # 13 chars -> PF
    regimen_fiscal="612",
    cp_fiscal="44100",
    uso_cfdi="G03",
)

RECEPTOR_PM = ReceptorData(
    razon_social="Empresa SA de CV",
    rfc="EMP010101AA1",  # 12 chars -> PM
    regimen_fiscal="601",
    cp_fiscal="06600",
    uso_cfdi="G03",
)

REGLAS_SIN_IEPS_SIN_RETENCION = FiscalRules(
    iva_aplica=True,
    tasa_iva=Decimal("0.16"),
    retencion_iva_tasa=Decimal("0"),
    retencion_isr_tasa=Decimal("0"),
    ieps_tasa=Decimal("0"),
    claves_con_ieps=frozenset(),
)


def test_iva_simple_sin_ieps_sin_retenciones():
    conceptos = [ConceptoExtraido(
        descripcion="Servicios de contabilidad",
        cantidad=Decimal("1"),
        precio_unitario=Decimal("5000.00"),
        clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION,
        metodo_pago="PUE", forma_pago="03",
    )
    assert resultado.escalation is None
    f = resultado.factura
    assert f.subtotal == Decimal("5000.00")
    assert f.ieps == Decimal("0.00")
    assert f.iva == Decimal("800.00")
    assert f.retencion_iva == Decimal("0.00")
    assert f.retencion_isr == Decimal("0.00")
    assert f.total == Decimal("5800.00")


def test_ieps_solo_en_concepto_gravado_no_en_accesorio():
    reglas = FiscalRules(
        iva_aplica=True,
        tasa_iva=Decimal("0.16"),
        retencion_iva_tasa=Decimal("0"),
        retencion_isr_tasa=Decimal("0"),
        ieps_tasa=Decimal("0.08"),
        claves_con_ieps=frozenset({"22101500"}),
    )
    conceptos = [
        ConceptoExtraido(
            descripcion="Botana gravada", cantidad=Decimal("10"),
            precio_unitario=Decimal("20.00"), clave_unidad="H87",
            clave_prod_serv="22101500",
        ),
        ConceptoExtraido(
            descripcion="Flete", cantidad=Decimal("1"),
            precio_unitario=Decimal("150.00"), clave_unidad="E48",
            clave_prod_serv="78101800",
        ),
    ]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, reglas, metodo_pago="PUE", forma_pago="03",
    )
    f = resultado.factura
    assert f.conceptos[0].ieps == Decimal("16.00")
    assert f.conceptos[1].ieps == Decimal("0.00")
    assert f.subtotal == Decimal("350.00")
    assert f.ieps == Decimal("16.00")
    # IVA sobre (subtotal + ieps) = 366.00 * 0.16
    assert f.iva == Decimal("58.56")
    assert f.total == Decimal("424.56")


def test_retenciones_solo_si_receptor_es_pm():
    reglas = FiscalRules(
        iva_aplica=True,
        tasa_iva=Decimal("0.16"),
        retencion_iva_tasa=Decimal("0.10"),
        retencion_isr_tasa=Decimal("0.10"),
        ieps_tasa=Decimal("0"),
        claves_con_ieps=frozenset(),
    )
    conceptos = [ConceptoExtraido(
        descripcion="Honorarios", cantidad=Decimal("1"),
        precio_unitario=Decimal("5000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]

    resultado_pm = calcular_factura(conceptos, RECEPTOR_PM, reglas, "PUE", "03")
    f_pm = resultado_pm.factura
    assert f_pm.retencion_iva == Decimal("80.00")
    assert f_pm.retencion_isr == Decimal("500.00")
    assert f_pm.total == Decimal("5220.00")

    resultado_pf = calcular_factura(conceptos, RECEPTOR_PF, reglas, "PUE", "03")
    f_pf = resultado_pf.factura
    assert f_pf.retencion_iva == Decimal("0.00")
    assert f_pf.retencion_isr == Decimal("0.00")
    assert f_pf.total == Decimal("5800.00")


def test_ppd_fuerza_forma_pago_99_sin_confiar_en_input():
    conceptos = [ConceptoExtraido(
        descripcion="Servicio", cantidad=Decimal("1"),
        precio_unitario=Decimal("1000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION,
        metodo_pago="PPD", forma_pago="03",  # forma_pago incorrecta a propósito
    )
    assert resultado.factura.forma_pago == "99"


def test_redondeo_por_concepto_no_cierra_limpio_a_centavo():
    conceptos = [ConceptoExtraido(
        descripcion="Producto fraccionado", cantidad=Decimal("3"),
        precio_unitario=Decimal("33.335"), clave_unidad="H87",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03",
    )
    # 3 * 33.335 = 100.005 -> ROUND_HALF_UP a 100.01
    assert resultado.factura.conceptos[0].importe == Decimal("100.01")
    assert resultado.factura.subtotal == Decimal("100.01")


def test_escalamiento_total_fuente_no_coincide_con_suma_conceptos():
    conceptos = [ConceptoExtraido(
        descripcion="Servicio", cantidad=Decimal("1"),
        precio_unitario=Decimal("5000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03",
        total_documento_fuente=Decimal("999.00"),
    )
    assert resultado.factura is None
    assert resultado.escalation.reason == EscalationReason.VALIDACION_ARITMETICA


def test_escalamiento_por_regimen_incongruente_con_rfc():
    # RFC de 12 chars (PM) pero régimen fiscal 612 = PF exclusivo.
    receptor_incongruente = ReceptorData(
        razon_social="Empresa SA de CV",
        rfc="EMP010101AA1",
        regimen_fiscal="612",
        cp_fiscal="06600",
        uso_cfdi="G03",
    )
    conceptos = [ConceptoExtraido(
        descripcion="Servicio", cantidad=Decimal("1"),
        precio_unitario=Decimal("1000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, receptor_incongruente, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03",
    )
    assert resultado.factura is None
    assert resultado.escalation.reason == EscalationReason.VALIDACION_ARITMETICA


def test_omitir_validacion_cruzada_permite_regimen_incongruente():
    receptor_incongruente = ReceptorData(
        razon_social="Empresa SA de CV",
        rfc="EMP010101AA1",
        regimen_fiscal="612",
        cp_fiscal="06600",
        uso_cfdi="G03",
    )
    conceptos = [ConceptoExtraido(
        descripcion="Servicio", cantidad=Decimal("1"),
        precio_unitario=Decimal("1000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, receptor_incongruente, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03",
        omitir_validacion_cruzada=True,
    )
    assert resultado.escalation is None
    assert resultado.factura.total == Decimal("1160.00")


def test_omitir_validacion_cruzada_tambien_omite_total_fuente():
    conceptos = [ConceptoExtraido(
        descripcion="Servicio", cantidad=Decimal("1"),
        precio_unitario=Decimal("5000.00"), clave_unidad="E48",
        clave_prod_serv="78101803",
    )]
    resultado = calcular_factura(
        conceptos, RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03",
        total_documento_fuente=Decimal("999.00"),
        omitir_validacion_cruzada=True,
    )
    assert resultado.escalation is None


def test_calcular_factura_requiere_al_menos_un_concepto():
    with pytest.raises(ValueError):
        calcular_factura([], RECEPTOR_PF, REGLAS_SIN_IEPS_SIN_RETENCION, "PUE", "03")


# ---------------------------------------------------------------------------
# REP — parcialidades y sobrepago
# ---------------------------------------------------------------------------

def test_rep_parcialidades_sucesivas_acumulan_saldo_insoluto():
    r1 = calcular_rep(monto_pagado=Decimal("2000.00"), imp_saldo_ant=Decimal("5652.14"))
    assert r1.rep.imp_saldo_insoluto == Decimal("3652.14")

    r2 = calcular_rep(monto_pagado=Decimal("1652.14"), imp_saldo_ant=r1.rep.imp_saldo_insoluto)
    assert r2.rep.imp_saldo_insoluto == Decimal("2000.00")

    r3 = calcular_rep(monto_pagado=Decimal("2000.00"), imp_saldo_ant=r2.rep.imp_saldo_insoluto)
    assert r3.rep.imp_saldo_insoluto == Decimal("0.00")


def test_rep_sobrepago_escala_en_vez_de_recortar_a_cero():
    resultado = calcular_rep(monto_pagado=Decimal("6000.00"), imp_saldo_ant=Decimal("5652.14"))
    assert resultado.rep is None
    assert resultado.escalation.reason == EscalationReason.VALIDACION_ARITMETICA


def test_fiscal_calculation_result_exige_exactamente_uno():
    from fiscal_engine import FiscalCalculationResult
    with pytest.raises(ValueError):
        FiscalCalculationResult(factura=None, escalation=None)


def test_iva_se_calcula_por_concepto_no_sobre_el_agregado():
    """
    Caso real que expuso el bug (auditoría con sandbox real, PM + IEPS +
    retenciones + 3 conceptos): calcular IVA una sola vez sobre
    (subtotal+ieps) daba $308.63, pero FacturAPI — que calcula y redondea
    el IVA de cada concepto por separado y suma — daba $308.62. La
    diferencia de un centavo es exactamente lo que "tolerancia cero"
    prohíbe. El motor debe calcular IVA por concepto igual que el IEPS.
    """
    reglas = FiscalRules(
        iva_aplica=True, tasa_iva=Decimal("0.16"),
        retencion_iva_tasa=Decimal("0.1067"), retencion_isr_tasa=Decimal("0.0125"),
        ieps_tasa=Decimal("0.08"), claves_con_ieps=frozenset({"50192100"}),
    )
    conceptos = [
        ConceptoExtraido(descripcion="Botana gravada A", cantidad=Decimal("15"),
            precio_unitario=Decimal("37.25"), clave_unidad="H87", clave_prod_serv="50192100"),
        ConceptoExtraido(descripcion="Botana gravada B", cantidad=Decimal("8"),
            precio_unitario=Decimal("112.90"), clave_unidad="H87", clave_prod_serv="50192100"),
        ConceptoExtraido(descripcion="Flete", cantidad=Decimal("1"),
            precio_unitario=Decimal("350.00"), clave_unidad="E48", clave_prod_serv="78101800"),
    ]
    resultado = calcular_factura(conceptos, RECEPTOR_PM, reglas, "PPD", "99")
    assert resultado.escalation is None
    f = resultado.factura
    assert f.subtotal == Decimal("1811.95")
    assert f.ieps == Decimal("116.96")
    # 308.62 (por concepto), NO 308.63 (agregado) -- esto es lo que verificó
    # el timbrado real contra FacturAPI.
    assert f.iva == Decimal("308.62")
    assert f.retencion_iva == Decimal("32.93")
    assert f.retencion_isr == Decimal("22.65")
    assert f.total == Decimal("2181.95")


def test_iva_por_concepto_reconstruye_suma_de_lineas_individuales():
    """Verifica explícitamente que el iva agregado == suma de iva por línea,
    calculando cada línea por separado con la misma fórmula que usa el motor."""
    reglas = FiscalRules(
        iva_aplica=True, tasa_iva=Decimal("0.16"),
        retencion_iva_tasa=Decimal("0"), retencion_isr_tasa=Decimal("0"),
        ieps_tasa=Decimal("0"), claves_con_ieps=frozenset(),
    )
    conceptos = [
        ConceptoExtraido(descripcion="A", cantidad=Decimal("15"), precio_unitario=Decimal("37.25"),
            clave_unidad="H87", clave_prod_serv="X"),
        ConceptoExtraido(descripcion="B", cantidad=Decimal("8"), precio_unitario=Decimal("112.90"),
            clave_unidad="H87", clave_prod_serv="X"),
        ConceptoExtraido(descripcion="C", cantidad=Decimal("1"), precio_unitario=Decimal("350.00"),
            clave_unidad="E48", clave_prod_serv="X"),
    ]
    resultado = calcular_factura(conceptos, RECEPTOR_PM, reglas, "PUE", "03")
    f = resultado.factura
    iva_por_linea = [
        (Decimal("15") * Decimal("37.25") * Decimal("0.16")).quantize(Decimal("0.01")),
        (Decimal("8") * Decimal("112.90") * Decimal("0.16")).quantize(Decimal("0.01")),
        (Decimal("1") * Decimal("350.00") * Decimal("0.16")).quantize(Decimal("0.01")),
    ]
    assert f.iva == sum(iva_por_linea, Decimal("0"))
