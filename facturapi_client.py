import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config import FACTURAPI_BASE_URL
from models import InvoiceData, RepData

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


_facturapi_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


def _is_retryable_write(exc: BaseException) -> bool:
    # Para timbres (POST) solo reintentar si la petición NUNCA llegó al servidor.
    # Un ReadTimeout o un 5xx puede significar que FacturAPI YA timbró la factura:
    # reintentar a ciegas crearía un CFDI duplicado ante el SAT.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


_facturapi_retry_write = retry(
    retry=retry_if_exception(_is_retryable_write),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


def _build_facturapi_payload(data: InvoiceData) -> dict:
    taxes = _build_taxes(data)

    items = [
        {
            "quantity": concepto.cantidad,
            "product": {
                "description": concepto.descripcion,
                "product_key": concepto.clave_prod_serv,
                "price": concepto.precio_unitario,
                "tax_included": False,
                "unit_key": concepto.clave_unidad,
                "taxes": taxes,
            },
        }
        for concepto in data.factura.conceptos
    ]

    # SAT rule: PPD always requires forma_pago = 99 (Por Definir)
    forma_pago = "99" if data.factura.metodo_pago == "PPD" else data.factura.forma_pago

    return {
        "customer": {
            "legal_name": data.receptor.razon_social,
            "tax_id": data.receptor.rfc,
            "tax_system": data.receptor.regimen_fiscal,
            "address": {"zip": data.receptor.cp_fiscal},
        },
        "items": items,
        "payment_form": forma_pago,
        "payment_method": data.factura.metodo_pago,
        "use": data.receptor.uso_cfdi,
    }


def _build_taxes(data: InvoiceData) -> list:
    taxes = []

    if data.factura.ieps > 0:
        taxes.append({
            "type": "IEPS",
            "rate": round(data.factura.ieps / data.factura.monto_antes_impuestos, 6),
            "factor": "Tasa",
            "withholding": False,
        })

    if data.factura.iva > 0:
        taxes.append({
            "type": "IVA",
            "rate": 0.16,
            "factor": "Tasa",
            "withholding": False,
        })

    if data.factura.retencion_iva > 0:
        taxes.append({
            "type": "IVA",
            "rate": round(data.factura.retencion_iva / data.factura.monto_antes_impuestos, 6),
            "factor": "Tasa",
            "withholding": True,
        })

    if data.factura.retencion_isr > 0:
        taxes.append({
            "type": "ISR",
            "rate": round(data.factura.retencion_isr / data.factura.monto_antes_impuestos, 6),
            "factor": "Tasa",
            "withholding": True,
        })

    return taxes


@_facturapi_retry_write
async def create_invoice(invoice_data: InvoiceData, facturapi_key: str) -> dict:
    """
    Llama a FacturAPI para timbrar el CFDI.
    facturapi_key: API key de la organización del cliente (viene de Supabase).
    Returns: {"id": "...", "folio_fiscal": "...", "pdf_url": "...", "xml_url": "..."}
    Raises: httpx.HTTPStatusError on 4xx (no retry), httpx.TimeoutException on timeout.
    """
    if not facturapi_key:
        raise ValueError("El cliente no tiene configurada una API key de FacturAPI en Supabase (tabla clientes).")

    payload = _build_facturapi_payload(invoice_data)
    logger.info("FacturAPI payload: %s", payload)
    headers = {"Authorization": f"Bearer {facturapi_key}"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{FACTURAPI_BASE_URL}/invoices",
            json=payload,
            headers=headers,
        )

    if response.status_code == 400:
        logger.warning("FacturAPI rechazó la factura (400): %s", response.text)
        raise httpx.HTTPStatusError(
            f"Error de validación FacturAPI: {response.text}",
            request=response.request,
            response=response,
        )

    response.raise_for_status()
    return response.json()


@_facturapi_retry
async def download_pdf(invoice_id: str, facturapi_key: str) -> bytes:
    headers = {"Authorization": f"Bearer {facturapi_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{FACTURAPI_BASE_URL}/invoices/{invoice_id}/pdf",
            headers=headers,
        )
        response.raise_for_status()
        return response.content


@_facturapi_retry
async def download_xml(invoice_id: str, facturapi_key: str) -> bytes:
    headers = {"Authorization": f"Bearer {facturapi_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{FACTURAPI_BASE_URL}/invoices/{invoice_id}/xml",
            headers=headers,
        )
        response.raise_for_status()
        return response.content


@_facturapi_retry
async def search_invoice_by_uuid(uuid: str, facturapi_key: str) -> Optional[dict]:
    """Busca una factura en FacturAPI por su UUID/folio fiscal. Retorna el objeto o None."""
    headers = {"Authorization": f"Bearer {facturapi_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{FACTURAPI_BASE_URL}/invoices",
            params={"q": uuid},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("data", [])
        for inv in results:
            if str(inv.get("uuid", "")).upper() == uuid.upper():
                return inv
        return None


@_facturapi_retry_write
async def create_rep(
    rep_data: RepData,
    facturapi_key: str,
    num_parcialidad: int,
    imp_saldo_ant: float,
) -> dict:
    """
    Crea un Complemento de Pago (REP) en FacturAPI.
    imp_saldo_ant: saldo antes de este pago (total original si es primer pago).
    """
    if not facturapi_key:
        raise ValueError("El cliente no tiene configurada una API key de FacturAPI.")

    imp_saldo_insoluto = round(imp_saldo_ant - rep_data.monto_pagado, 2)
    if imp_saldo_insoluto < 0:
        imp_saldo_insoluto = 0.0

    payload = {
        "type": "P",
        "customer": {
            "legal_name": rep_data.receptor.razon_social,
            "tax_id": rep_data.receptor.rfc,
            "tax_system": rep_data.receptor.regimen_fiscal,
            "address": {"zip": rep_data.receptor.cp_fiscal},
        },
        "payment_form": rep_data.forma_pago,
        "complemento_pago": {
            "pagos": [{
                "fecha_pago": rep_data.fecha_pago,
                "forma_de_pago_p": rep_data.forma_pago,
                "moneda_p": "MXN",
                "tipo_cambio_p": 1,
                "monto": rep_data.monto_pagado,
                "documentos_relacionados": [{
                    "id_documento": rep_data.uuid_factura_origen,
                    "moneda_dr": "MXN",
                    "tipo_cambio_dr": 1,
                    "metodo_de_pago_dr": "PPD",
                    "num_parcialidad": num_parcialidad,
                    "imp_saldo_ant": imp_saldo_ant,
                    "imp_pagado": rep_data.monto_pagado,
                    "imp_saldo_insoluto": imp_saldo_insoluto,
                }],
            }],
        },
    }

    logger.info("FacturAPI REP payload: %s", payload)
    headers = {"Authorization": f"Bearer {facturapi_key}"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{FACTURAPI_BASE_URL}/invoices",
            json=payload,
            headers=headers,
        )

    if response.status_code == 400:
        logger.warning("FacturAPI rechazó el REP (400): %s", response.text)
        raise httpx.HTTPStatusError(
            f"Error de validación FacturAPI REP: {response.text}",
            request=response.request,
            response=response,
        )

    response.raise_for_status()
    result = response.json()
    result["_imp_saldo_insoluto"] = imp_saldo_insoluto
    return result
