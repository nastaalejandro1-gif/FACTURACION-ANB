"""
Motor de cálculo fiscal — Claude extrae y clasifica, este módulo calcula.

Puro: sin red, sin `anthropic`, sin `httpx`, sin `supabase`. Solo `decimal`,
tipos de `models` para las firmas de entrada, y `sat_catalogs`/`escalation`.

Ningún monto fiscal (IEPS, IVA, retenciones, total) debe originarse fuera
de este módulo. Todos los montos de entrada/salida son `Decimal`; la
conversión a `float` ocurre solo en la frontera hacia FacturAPI/Supabase,
nunca aquí.

Redondeo: ROUND_HALF_UP a centavo, POR CONCEPTO (no al final) — así
`Σ importes == subtotal` por construcción, igual que hace FacturAPI/el SAT.
Ver Anexo_20_Guia_de_llenado_CFDI 4.0.pdf en la raíz del repo para
confirmar la regla exacta contra 2 casos timbrados en sandbox antes de
depender de esto para clientes reales.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from escalation import EscalationDetail, EscalationReason
from models import ReceptorData
from sat_catalogs import regimen_coincide_con_tipo_persona, tipo_persona_from_rfc

CENTAVO = Decimal("0.01")


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Entrada (lo que Claude extrae del documento/chat)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConceptoExtraido:
    descripcion: str
    cantidad: Decimal
    precio_unitario: Decimal
    clave_unidad: str
    clave_prod_serv: str


@dataclass(frozen=True)
class FiscalRules:
    """Cargado desde la tabla reglas_fiscales_cliente (ver sheets_client.get_fiscal_rules)."""
    iva_aplica: bool
    tasa_iva: Decimal
    retencion_iva_tasa: Decimal
    retencion_isr_tasa: Decimal
    ieps_tasa: Decimal
    claves_con_ieps: frozenset[str]  # claves del catalogo_clave_prod_serv con aplica_ieps=True
    # Facturas por encima de esto -> EscalationReason.MONTO_ALTO. Default
    # solo para no romper construcciones existentes (tests, etc.) — el
    # valor real siempre viene de reglas_fiscales_cliente.
    monto_maximo_sin_autorizacion: Decimal = Decimal("50000")


# ---------------------------------------------------------------------------
# Salida (lo que se timbra)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConceptoCalculado:
    descripcion: str
    clave_prod_serv: str
    clave_unidad: str
    cantidad: Decimal
    precio_unitario: Decimal
    importe: Decimal
    ieps: Decimal
    ieps_tasa: Decimal  # tasa realmente aplicada a este concepto (0 si no gravado)


@dataclass(frozen=True)
class FacturaCalculada:
    conceptos: list[ConceptoCalculado]
    subtotal: Decimal
    ieps: Decimal
    iva: Decimal
    tasa_iva: Decimal
    retencion_iva: Decimal
    retencion_iva_tasa: Decimal
    retencion_isr: Decimal
    retencion_isr_tasa: Decimal
    total: Decimal
    metodo_pago: str
    forma_pago: str
    tipo_persona_receptor: str


@dataclass(frozen=True)
class FiscalCalculationResult:
    factura: Optional[FacturaCalculada] = None
    escalation: Optional[EscalationDetail] = None

    def __post_init__(self) -> None:
        if (self.factura is None) == (self.escalation is None):
            raise ValueError(
                "FiscalCalculationResult debe traer exactamente uno de: factura, escalation"
            )


@dataclass(frozen=True)
class RepCalculado:
    monto_pagado: Decimal
    imp_saldo_ant: Decimal
    imp_saldo_insoluto: Decimal


@dataclass(frozen=True)
class RepCalculationResult:
    rep: Optional[RepCalculado] = None
    escalation: Optional[EscalationDetail] = None

    def __post_init__(self) -> None:
        if (self.rep is None) == (self.escalation is None):
            raise ValueError(
                "RepCalculationResult debe traer exactamente uno de: rep, escalation"
            )


# ---------------------------------------------------------------------------
# Cálculo — factura de ingreso
# ---------------------------------------------------------------------------

def calcular_factura(
    conceptos: list[ConceptoExtraido],
    receptor: ReceptorData,
    reglas: FiscalRules,
    metodo_pago: str,
    forma_pago: str,
    total_documento_fuente: Optional[Decimal] = None,
    omitir_validacion_cruzada: bool = False,
) -> FiscalCalculationResult:
    """
    omitir_validacion_cruzada: se usa SOLO cuando ANB ya revisó manualmente
    una escalación por VALIDACION_ARITMETICA y aprobó proceder de todas
    formas (ver main.py::_calcular_y_procesar_factura). Nunca lo pone
    Claude ni el cliente — es una decisión humana explícita y queda
    registrada en la bitácora. Los montos siempre se calculan igual; lo
    único que se salta son los chequeos de consistencia previos.
    """
    if not conceptos:
        raise ValueError("calcular_factura requiere al menos un concepto")

    # Validación cruzada determinista RFC <-> régimen fiscal (ver sat_catalogs).
    # No es "duda fiscal" del prompt: es el motor detectando una inconsistencia
    # de datos real, mapeada a VALIDACION_ARITMETICA (validación cruzada).
    tipo_persona = tipo_persona_from_rfc(receptor.rfc)
    if not omitir_validacion_cruzada and not regimen_coincide_con_tipo_persona(receptor.regimen_fiscal, tipo_persona):
        return FiscalCalculationResult(escalation=EscalationDetail(
            reason=EscalationReason.VALIDACION_ARITMETICA,
            detail=(
                f"El régimen fiscal {receptor.regimen_fiscal} del receptor no corresponde "
                f"al tipo de persona {tipo_persona} (derivado del RFC {receptor.rfc})."
            ),
        ))

    # tasa_iva se necesita ANTES del loop: el IVA se calcula POR CONCEPTO
    # (igual que el IEPS), no una sola vez sobre el agregado — FacturAPI/SAT
    # calculan y redondean el impuesto de cada línea del CFDI por separado
    # y suman; redondear una sola vez sobre (subtotal+ieps) puede diferir
    # en un centavo de esa suma cuando hay 2+ conceptos (redondeo no es
    # distributivo). Confirmado con un timbrado real: agregado daba
    # iva=308.63, FacturAPI (por línea) daba 308.62.
    tasa_iva = reglas.tasa_iva if reglas.iva_aplica else Decimal("0")

    conceptos_calculados: list[ConceptoCalculado] = []
    iva_total = Decimal("0.00")
    for c in conceptos:
        importe = _redondear(c.cantidad * c.precio_unitario)
        aplica_ieps = c.clave_prod_serv in reglas.claves_con_ieps
        ieps_tasa_concepto = reglas.ieps_tasa if aplica_ieps else Decimal("0")
        ieps_concepto = _redondear(importe * ieps_tasa_concepto) if aplica_ieps else Decimal("0.00")
        iva_concepto = _redondear((importe + ieps_concepto) * tasa_iva)
        iva_total += iva_concepto
        conceptos_calculados.append(ConceptoCalculado(
            descripcion=c.descripcion,
            clave_prod_serv=c.clave_prod_serv,
            clave_unidad=c.clave_unidad,
            cantidad=c.cantidad,
            precio_unitario=c.precio_unitario,
            importe=importe,
            ieps=ieps_concepto,
            ieps_tasa=ieps_tasa_concepto,
        ))

    subtotal = sum((cc.importe for cc in conceptos_calculados), Decimal("0.00"))
    ieps_total = sum((cc.ieps for cc in conceptos_calculados), Decimal("0.00"))
    iva = iva_total

    if tipo_persona == "PM":
        retencion_iva_tasa = reglas.retencion_iva_tasa
        retencion_isr_tasa = reglas.retencion_isr_tasa
        retencion_iva = _redondear(iva * retencion_iva_tasa)
        retencion_isr = _redondear(subtotal * retencion_isr_tasa)
    else:
        # Receptor PF: retenciones siempre en 0, sin excepción.
        retencion_iva_tasa = Decimal("0")
        retencion_isr_tasa = Decimal("0")
        retencion_iva = Decimal("0.00")
        retencion_isr = Decimal("0.00")

    total = subtotal + ieps_total + iva - retencion_iva - retencion_isr

    # El "TOTAL" impreso en una cotización puede ser el subtotal (sin IVA),
    # el total con impuestos trasladados, o el neto ya con retenciones. Los
    # tres son válidos; solo se escala si no coincide con NINGUNO (concepto
    # faltante o mal extraído). Antes se comparaba solo contra el subtotal y
    # escalaba cualquier documento cuyo total incluía IVA.
    if not omitir_validacion_cruzada and total_documento_fuente is not None:
        total_con_traslados = subtotal + ieps_total + iva
        candidatos = (subtotal, total_con_traslados, total)
        if all(abs(c - total_documento_fuente) > CENTAVO for c in candidatos):
            return FiscalCalculationResult(escalation=EscalationDetail(
                reason=EscalationReason.VALIDACION_ARITMETICA,
                detail=(
                    f"El total del documento fuente (${total_documento_fuente}) no coincide "
                    f"con lo calculado de los conceptos extraídos: subtotal ${subtotal}, "
                    f"total con impuestos ${total_con_traslados}, total neto ${total}."
                ),
            ))

    # Decisión de negocio de ANB (no fiscal): facturas por encima de un
    # monto requieren su autorización antes de pasar a confirmación del
    # cliente. omitir_validacion_cruzada=True también salta esto — es la
    # misma señal de "ANB ya lo revisó y aprobó, seguir adelante".
    if not omitir_validacion_cruzada and total > reglas.monto_maximo_sin_autorizacion:
        return FiscalCalculationResult(escalation=EscalationDetail(
            reason=EscalationReason.MONTO_ALTO,
            detail=(
                f"El total de la factura (${total}) supera el máximo sin autorización "
                f"(${reglas.monto_maximo_sin_autorizacion})."
            ),
        ))

    # Regla SAT: PPD siempre forma_pago=99. Se normaliza, no se confía en
    # lo que haya extraído Claude del mensaje del cliente.
    forma_pago_final = "99" if metodo_pago == "PPD" else forma_pago

    factura = FacturaCalculada(
        conceptos=conceptos_calculados,
        subtotal=subtotal,
        ieps=ieps_total,
        iva=iva,
        tasa_iva=tasa_iva,
        retencion_iva=retencion_iva,
        retencion_iva_tasa=retencion_iva_tasa,
        retencion_isr=retencion_isr,
        retencion_isr_tasa=retencion_isr_tasa,
        total=total,
        metodo_pago=metodo_pago,
        forma_pago=forma_pago_final,
        tipo_persona_receptor=tipo_persona,
    )
    return FiscalCalculationResult(factura=factura)


# ---------------------------------------------------------------------------
# Cálculo — REP (complemento de pago)
# ---------------------------------------------------------------------------

def calcular_rep(monto_pagado: Decimal, imp_saldo_ant: Decimal) -> RepCalculationResult:
    if monto_pagado > imp_saldo_ant:
        # Antes se recortaba el saldo a 0 silenciosamente (bug conocido, ver
        # TODOS.md). Un sobrepago es un dato que no cuadra: se escala.
        return RepCalculationResult(escalation=EscalationDetail(
            reason=EscalationReason.VALIDACION_ARITMETICA,
            detail=(
                f"El monto pagado (${monto_pagado}) excede el saldo insoluto anterior "
                f"(${imp_saldo_ant}). Posible sobrepago o error de captura."
            ),
        ))

    imp_saldo_insoluto = _redondear(imp_saldo_ant - monto_pagado)
    return RepCalculationResult(rep=RepCalculado(
        monto_pagado=monto_pagado,
        imp_saldo_ant=imp_saldo_ant,
        imp_saldo_insoluto=imp_saldo_insoluto,
    ))
