import logging
from decimal import ROUND_HALF_UP, Decimal
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
    """
    Frontera Decimal -> float/JSON: fiscal_engine y models.py trabajan en
    Decimal para precisión exacta; FacturAPI recibe JSON estándar (httpx no
    sabe serializar Decimal). La conversión a float ocurre SOLO aquí.
    """
    retenciones = _calcular_tasas_retencion_efectivas(data)

    items = [
        {
            "quantity": float(concepto.cantidad),
            "product": {
                "description": concepto.descripcion,
                "product_key": concepto.clave_prod_serv,
                "price": float(concepto.precio_unitario),
                "tax_included": False,
                "unit_key": concepto.clave_unidad,
                "taxes": _build_taxes_for_concepto(concepto, data, retenciones),
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


def _calcular_tasas_retencion_efectivas(data: InvoiceData) -> tuple[Decimal, Decimal]:
    """
    FacturAPI calcula cada impuesto de una línea como `rate * base`, donde
    `base` es SIEMPRE el precio del concepto MÁS cualquier IEPS ya
    trasladado en esa misma línea (confirmado empíricamente contra el
    sandbox: un concepto con IEPS usa base = importe + ieps para el IVA Y
    para las retenciones de esa línea, no solo para el IVA).

    Pero `factura.retencion_iva`/`retencion_isr` (ver fiscal_engine.py) se
    calculan contra bases DISTINTAS: retencion_iva = iva_total * tasa
    (fórmula validada por el despacho — ver el fixture histórico de
    test_critical.py, retencion_iva=85.36 para iva=800.00 = 800*0.1067),
    y retencion_isr = subtotal SIN ieps * tasa. Ninguna de las dos
    coincide con la base que usa FacturAPI.

    Por eso NO se manda la tasa "semántica" (data.factura.retencion_*_tasa)
    directo a FacturAPI — se back-calcula la tasa EFECTIVA que, aplicada a
    la base real que usará FacturAPI (suma de precio+ieps de todos los
    conceptos), reproduce EXACTO el monto ya calculado por fiscal_engine.
    Es el mismo mecanismo que ya usaba este archivo antes del restructure;
    aquí se hace con Decimal en vez de float, y con la base correcta
    (incluye IEPS) en vez de solo el subtotal.
    """
    base_efectiva_total = data.factura.monto_antes_impuestos + data.factura.ieps

    def _tasa_efectiva(monto: Decimal) -> Decimal:
        if monto <= 0:
            return Decimal("0")
        return (monto / base_efectiva_total).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    return (
        _tasa_efectiva(data.factura.retencion_iva),
        _tasa_efectiva(data.factura.retencion_isr),
    )


def _build_taxes_for_concepto(concepto, data: InvoiceData, retenciones: tuple[Decimal, Decimal]) -> list:
    retencion_iva_rate, retencion_isr_rate = retenciones
    taxes = []

    # IEPS e IVA: la tasa que fiscal_engine calculó SÍ coincide con la base
    # que usa FacturAPI (precio del concepto, y precio+ieps respectivamente)
    # — se mandan exactas, sin back-calcular.
    if concepto.ieps > 0:
        taxes.append({
            "type": "IEPS",
            "rate": float(concepto.ieps_tasa),
            "factor": "Tasa",
            "withholding": False,
        })

    if data.factura.iva > 0:
        taxes.append({
            "type": "IVA",
            "rate": float(data.factura.tasa_iva),
            "factor": "Tasa",
            "withholding": False,
        })

    if data.factura.retencion_iva > 0:
        taxes.append({
            "type": "IVA",
            "rate": float(retencion_iva_rate),
            "factor": "Tasa",
            "withholding": True,
        })

    if data.factura.retencion_isr > 0:
        taxes.append({
            "type": "ISR",
            "rate": float(retencion_isr_rate),
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
) -> dict:
    """
    Crea un Complemento de Pago (REP) en FacturAPI.

    rep_data ya trae imp_saldo_ant/imp_saldo_insoluto calculados por
    fiscal_engine.calcular_rep con Decimal — no se recalculan aquí, solo
    se convierten a float en la frontera JSON con FacturAPI.
    """
    if not facturapi_key:
        raise ValueError("El cliente no tiene configurada una API key de FacturAPI.")

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
                "monto": float(rep_data.monto_pagado),
                "documentos_relacionados": [{
                    "id_documento": rep_data.uuid_factura_origen,
                    "moneda_dr": "MXN",
                    "tipo_cambio_dr": 1,
                    "metodo_de_pago_dr": "PPD",
                    "num_parcialidad": num_parcialidad,
                    "imp_saldo_ant": float(rep_data.imp_saldo_ant),
                    "imp_pagado": float(rep_data.monto_pagado),
                    "imp_saldo_insoluto": float(rep_data.imp_saldo_insoluto),
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
    result["_imp_saldo_insoluto"] = float(rep_data.imp_saldo_insoluto)
    return result
