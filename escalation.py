"""
Motivos de escalamiento a ANB — enum cerrado.

Reemplaza el booleano `requiere_revision` de libre interpretación que hoy
decide Claude en el prompt. A partir del restructure, solo estos motivos
disparan una revisión manual del despacho; todo lo demás lo resuelve el
motor de cálculo de forma determinista.
"""
from dataclasses import dataclass
from enum import Enum


class EscalationReason(str, Enum):
    CLAVE_PROD_SERV_NUEVA = "clave_prod_serv_nueva"
    VALIDACION_ARITMETICA = "validacion_aritmetica"
    ERROR_TIMBRE_SAT = "error_timbre_sat"
    RFC_INVALIDO = "rfc_invalido"
    RECEPTOR_EXTRANJERO_SIN_RFC = "receptor_extranjero_sin_rfc"
    FACTURA_ORIGEN_EXTERNA = "factura_origen_externa"  # REP de factura hecha en otro programa que no pasa las validaciones de cfdi_xml.problemas_para_rep
    FACTURA_ORIGEN_PDF = "factura_origen_pdf"  # factura externa leída de PDF por Claude: ANB verifica los datos antes de timbrar
    PAGOS_PREVIOS_EXTERNOS = "pagos_previos_externos"  # hubo parcialidades en el otro programa: ANB fija saldo y número de parcialidad
    MONTO_ALTO = "monto_alto"  # decisión de negocio de ANB, no fiscal — ver reglas_fiscales_cliente.monto_maximo_sin_autorizacion


@dataclass(frozen=True)
class EscalationDetail:
    reason: EscalationReason
    detail: str
