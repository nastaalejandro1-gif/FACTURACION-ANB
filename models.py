import re
from typing import Literal
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


class FacturaData(BaseModel):
    concepto: str
    clave_prod_serv: str
    monto_antes_impuestos: float = Field(gt=0, le=10_000_000)
    iva: float = Field(ge=0)
    retencion_iva: float = Field(ge=0)
    retencion_isr: float = Field(ge=0)
    total_estimado: float = Field(gt=0)
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
    def validate_total_consistency(self) -> "FacturaData":
        expected = (
            self.monto_antes_impuestos
            + self.iva
            - self.retencion_iva
            - self.retencion_isr
        )
        if abs(expected - self.total_estimado) > self.monto_antes_impuestos * 0.02:
            raise ValueError(
                f"Total estimado ({self.total_estimado}) no coincide con "
                f"el cálculo ({expected:.2f}). Diferencia > 2%."
            )
        return self


class InvoiceData(BaseModel):
    estatus: Literal["confirmado_por_cliente"]
    requiere_revision: bool
    motivo_revision: str = ""
    emisor: EmisorData
    receptor: ReceptorData
    factura: FacturaData


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
    clave_prod_serv_default: str
    requiere_revision: bool
    notas_fiscales: str
    activo: bool
    facturapi_key: str  # API key de la organización del cliente en FacturAPI
