import re
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

RFC_PATTERN = re.compile(r"^[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}$")

# Catálogo SAT — claves válidas de forma_pago
FORMAS_PAGO_VALIDAS = {
    "01", "02", "03", "04", "05", "06", "08",
    "12", "13", "17", "23", "24", "25", "26",
    "27", "28", "29", "30", "31", "99",
}


class EmisorData(BaseModel):
    nombre_comercial: str
    razon_social: str
    rfc: str
    regimen_fiscal: str
    cp_fiscal: str


class ReceptorData(BaseModel):
    razon_social: str
    rfc: str
    regimen_fiscal: str
    cp_fiscal: str
    uso_cfdi: str

    @field_validator("rfc")
    @classmethod
    def validate_rfc(cls, v: str) -> str:
        v = v.upper().strip()
        if not RFC_PATTERN.match(v):
            raise ValueError(f"RFC inválido: '{v}'. Formato esperado: 4 letras + 6 dígitos + 3 alfanuméricos")
        return v


# ---------------------------------------------------------------------------
# Modelos "draft" — lo único que Claude genera: extracción y clasificación.
# Sin montos calculados (iva, retenciones, total): eso lo produce
# fiscal_engine a partir de estos datos. Ver fiscal_engine.py.
# ---------------------------------------------------------------------------

class ConceptoDraft(BaseModel):
    descripcion: str
    cantidad: float = Field(gt=0, default=1.0)
    clave_unidad: str = "E48"  # E48=Servicio (default despachos contables)
    precio_unitario: float = Field(gt=0)
    # Código del catálogo aprobado del cliente, o "NUEVA" si Claude no
    # encontró ninguna clave del catálogo que aplique al concepto.
    clave_prod_serv: str
    clave_prod_serv_propuesta: str = ""  # obligatorio si clave_prod_serv == "NUEVA"

    @model_validator(mode="after")
    def validate_clave_nueva_requiere_propuesta(self) -> "ConceptoDraft":
        if self.clave_prod_serv == "NUEVA" and not self.clave_prod_serv_propuesta:
            raise ValueError(
                "clave_prod_serv='NUEVA' requiere clave_prod_serv_propuesta "
                "(código SAT de 8 dígitos que mejor describe el concepto)."
            )
        return self


class FacturaDraft(BaseModel):
    conceptos: list[ConceptoDraft] = Field(min_length=1)
    metodo_pago: Literal["PUE", "PPD"]
    forma_pago: str
    observaciones: str = ""
    # Total impreso en el documento fuente (cotización), si Claude lo vio.
    # El motor de cálculo lo usa como validación cruzada contra la suma de
    # conceptos — ver EscalationReason.VALIDACION_ARITMETICA.
    total_documento_fuente: Optional[float] = None

    @field_validator("forma_pago")
    @classmethod
    def validate_forma_pago(cls, v: str) -> str:
        if v not in FORMAS_PAGO_VALIDAS:
            raise ValueError(
                f"Forma de pago '{v}' no válida. Use clave SAT: "
                f"03=Transferencia, 04=Tarjeta, 01=Efectivo, etc."
            )
        return v


class InvoiceDraft(BaseModel):
    estatus: Literal["confirmado_por_cliente"]
    receptor: ReceptorData
    factura: FacturaDraft


class RepDraft(BaseModel):
    estatus: Literal["confirmado_por_cliente"]
    uuid_factura_origen: str
    receptor: ReceptorData
    fecha_pago: str
    forma_pago: str
    monto_pagado: float = Field(gt=0)

    @field_validator("forma_pago")
    @classmethod
    def validate_forma_pago_rep(cls, v: str) -> str:
        validas = FORMAS_PAGO_VALIDAS - {"99"}
        if v not in validas:
            raise ValueError(
                f"Forma de pago '{v}' no válida para REP. No puede ser '99'. "
                "Use: 03=Transferencia, 04=Tarjeta, 01=Efectivo, etc."
            )
        return v

    @field_validator("uuid_factura_origen")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        v = v.upper().strip()
        if not re.match(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$", v):
            raise ValueError(f"UUID inválido: '{v}'. Formato esperado: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")
        return v


# ---------------------------------------------------------------------------
# Modelos finales — lo que produce fiscal_engine, listo para timbrar.
# Todos los montos en Decimal. Los validadores de consistencia son EXACTOS
# (no hay tolerancia): si no cuadran, es un bug del motor de cálculo, no
# una discrepancia esperada de un LLM calculando distinto.
# ---------------------------------------------------------------------------

class ConceptoItem(BaseModel):
    descripcion: str
    clave_prod_serv: str
    cantidad: Decimal = Field(gt=0, default=Decimal("1"))
    clave_unidad: str = "E48"
    precio_unitario: Decimal = Field(gt=0)
    ieps: Decimal = Field(ge=0, default=Decimal("0"))
    ieps_tasa: Decimal = Field(ge=0, default=Decimal("0"))  # tasa aplicada (0 si no gravado)


class FacturaData(BaseModel):
    conceptos: list[ConceptoItem] = Field(min_length=1)
    monto_antes_impuestos: Decimal = Field(gt=0, le=Decimal(10_000_000))
    ieps: Decimal = Field(ge=0, default=Decimal("0"))
    iva: Decimal = Field(ge=0)
    tasa_iva: Decimal = Field(ge=0, default=Decimal("0"))
    retencion_iva: Decimal = Field(ge=0)
    retencion_iva_tasa: Decimal = Field(ge=0, default=Decimal("0"))
    retencion_isr: Decimal = Field(ge=0)
    retencion_isr_tasa: Decimal = Field(ge=0, default=Decimal("0"))
    total_estimado: Decimal = Field(gt=0)
    metodo_pago: Literal["PUE", "PPD"]
    forma_pago: str
    observaciones: str = ""

    @field_validator("forma_pago")
    @classmethod
    def validate_forma_pago(cls, v: str) -> str:
        if v not in FORMAS_PAGO_VALIDAS:
            raise ValueError(
                f"Forma de pago '{v}' no válida. Use clave SAT: "
                f"03=Transferencia, 04=Tarjeta, 01=Efectivo, etc."
            )
        return v

    @model_validator(mode="after")
    def validate_monto_conceptos(self) -> "FacturaData":
        suma = sum((c.cantidad * c.precio_unitario for c in self.conceptos), Decimal("0"))
        if suma != self.monto_antes_impuestos:
            raise ValueError(
                f"monto_antes_impuestos ({self.monto_antes_impuestos}) no coincide "
                f"exactamente con la suma de conceptos ({suma}). Esto es un bug del "
                f"motor de cálculo, no una discrepancia esperada."
            )
        return self

    @model_validator(mode="after")
    def validate_ppd_forma_pago(self) -> "FacturaData":
        if self.metodo_pago == "PPD" and self.forma_pago != "99":
            raise ValueError(
                "Para método de pago PPD la forma de pago debe ser '99' (Por Definir). "
                f"Se recibió '{self.forma_pago}'."
            )
        return self

    @model_validator(mode="after")
    def validate_ieps_breakdown(self) -> "FacturaData":
        suma_ieps = sum((c.ieps for c in self.conceptos), Decimal("0"))
        if suma_ieps != self.ieps:
            raise ValueError(
                f"factura.ieps ({self.ieps}) no coincide exactamente con la suma de "
                f"ieps por concepto ({suma_ieps})."
            )
        return self

    @model_validator(mode="after")
    def validate_total_consistency(self) -> "FacturaData":
        expected = (
            self.monto_antes_impuestos
            + self.ieps
            + self.iva
            - self.retencion_iva
            - self.retencion_isr
        )
        if expected != self.total_estimado:
            raise ValueError(
                f"Total estimado ({self.total_estimado}) no coincide exactamente con "
                f"el cálculo ({expected})."
            )
        return self


class InvoiceData(BaseModel):
    estatus: Literal["confirmado_por_cliente"]
    emisor: EmisorData
    receptor: ReceptorData
    factura: FacturaData


class RepData(BaseModel):
    estatus: Literal["confirmado_por_cliente"]
    uuid_factura_origen: str
    receptor: ReceptorData
    fecha_pago: str
    forma_pago: str
    monto_pagado: Decimal = Field(gt=0)
    imp_saldo_ant: Decimal = Field(ge=0)
    imp_saldo_insoluto: Decimal = Field(ge=0)
    # Factura hecha en otro programa (leída de su XML/PDF, ver cfdi_xml.py).
    # Viaja en el pendiente para que al confirmar no se busque en FacturAPI,
    # que no la conoce.
    factura_origen_externa: Optional[dict] = None

    @field_validator("forma_pago")
    @classmethod
    def validate_forma_pago_rep(cls, v: str) -> str:
        validas = FORMAS_PAGO_VALIDAS - {"99"}
        if v not in validas:
            raise ValueError(
                f"Forma de pago '{v}' no válida para REP. No puede ser '99'. "
                "Use: 03=Transferencia, 04=Tarjeta, 01=Efectivo, etc."
            )
        return v

    @field_validator("uuid_factura_origen")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        v = v.upper().strip()
        if not re.match(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$", v):
            raise ValueError(f"UUID inválido: '{v}'. Formato esperado: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")
        return v

    @model_validator(mode="after")
    def validate_saldo_insoluto_consistency(self) -> "RepData":
        expected = self.imp_saldo_ant - self.monto_pagado
        if expected != self.imp_saldo_insoluto:
            raise ValueError(
                f"imp_saldo_insoluto ({self.imp_saldo_insoluto}) no coincide exactamente "
                f"con imp_saldo_ant - monto_pagado ({expected})."
            )
        return self


class ClientProfile(BaseModel):
    despacho_id: str
    id_cliente: str
    nombre_comercial: str
    razon_social: str
    rfc: str
    canal: str
    canal_id: str
    email_factura: str
    tipo_persona: Literal["PF", "PM"]
    regimen_fiscal: str
    cp_fiscal: str
    iva_aplica: str
    retencion_iva: float
    retencion_isr: float
    ieps_rate: float
    clave_prod_serv_default: str
    requiere_revision: bool
    notas_fiscales: str
    activo: bool
    facturapi_key: str  # API key de la organización del cliente en FacturAPI


class PendingPayload(BaseModel):
    """
    Envoltura que se guarda en pendientes.invoice_json cuando el motivo es
    'anb_revision' (escalamiento). Guarda el DRAFT (extracción pre-cálculo,
    nunca montos ya calculados) más el motivo de escalación, para que
    main.py pueda reconstruirlo y volver a intentar el cálculo cuando ANB
    aprueba — ver main.py::_calcular_y_procesar_factura /
    _calcular_y_timbrar_rep.

    Para 'cliente_confirmacion' NO se usa este envoltorio: invoice_json es
    directamente InvoiceData/RepData ya calculado (formato sin cambios
    respecto al que ya usaba /aprobar de ANB).
    """
    tipo: Literal["ingreso", "rep"]
    escalation_reason: str
    escalation_detail: str
    invoice_draft: Optional[InvoiceDraft] = None
    rep_draft: Optional[RepDraft] = None
    # Factura origen emitida fuera de FacturAPI (XML/PDF): sin ella, al
    # aprobar ANB se volvería a pedir el archivo al cliente.
    factura_origen: Optional[dict] = None

    @model_validator(mode="after")
    def validate_shape(self) -> "PendingPayload":
        if self.tipo == "ingreso" and self.invoice_draft is None:
            raise ValueError("tipo='ingreso' requiere invoice_draft")
        if self.tipo == "rep" and self.rep_draft is None:
            raise ValueError("tipo='rep' requiere rep_draft")
        return self
