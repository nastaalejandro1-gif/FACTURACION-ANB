import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config import FACTURAPI_BASE_URL
from models import InvoiceData

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


@_facturapi_retry
async def create_invoice(invoice_data: InvoiceData, facturapi_key: str) -> dict:
    """
    Llama a FacturAPI para timbrar el CFDI.
    facturapi_key: API key de la organización del cliente (viene de Sheets).
    Returns: {"id": "...", "folio_fiscal": "...", "pdf_url": "...", "xml_url": "..."}
    Raises: httpx.HTTPStatusError on 4xx (no retry), httpx.TimeoutException on timeout.
    """
    if not facturapi_key:
        raise ValueError("El cliente no tiene configurada una API key de FacturAPI en Google Sheets.")

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


async def download_pdf(invoice_id: str, facturapi_key: str) -> bytes:
    headers = {"Authorization": f"Bearer {facturapi_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{FACTURAPI_BASE_URL}/invoices/{invoice_id}/pdf",
            headers=headers,
        )
        response.raise_for_status()
        return response.content


async def download_xml(invoice_id: str, facturapi_key: str) -> bytes:
    headers = {"Authorization": f"Bearer {facturapi_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{FACTURAPI_BASE_URL}/invoices/{invoice_id}/xml",
            headers=headers,
        )
        response.raise_for_status()
        return response.content
