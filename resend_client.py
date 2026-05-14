import base64
import logging

import httpx

from config import RESEND_API_KEY, RESEND_FROM_EMAIL

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


async def send_invoice_email(
    to_email: str,
    nombre_comercial: str,
    folio: str,
    pdf_bytes: bytes,
    xml_bytes: bytes,
) -> None:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY no configurada — email omitido")
        return
    if not to_email:
        logger.warning("Cliente %s sin email_factura — email omitido", nombre_comercial)
        return

    folio_short = folio[:8] if len(folio) > 8 else folio

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": f"Tu factura está lista — Folio {folio_short}",
        "html": f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #2c3e50;">Tu factura está lista</h2>
            <p>Hola <strong>{nombre_comercial}</strong>,</p>
            <p>Adjuntamos tu CFDI timbrado. Encontrarás dos archivos:</p>
            <ul>
                <li><strong>PDF</strong> — versión imprimible de tu factura.</li>
                <li><strong>XML</strong> — archivo oficial para tus registros contables.</li>
            </ul>
            <p style="color: #7f8c8d; font-size: 13px;">
                Este correo fue generado automáticamente por ANB Consultores.<br>
                Si tienes dudas, responde a este mensaje o contáctanos por Telegram.
            </p>
        </div>
        """,
        "attachments": [
            {
                "filename": f"factura_{folio_short}.pdf",
                "content": base64.standard_b64encode(pdf_bytes).decode(),
            },
            {
                "filename": f"factura_{folio_short}.xml",
                "content": base64.standard_b64encode(xml_bytes).decode(),
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                RESEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            )
        if response.status_code in (200, 201):
            logger.info("Email enviado a %s para folio %s", to_email, folio_short)
        else:
            logger.error("Resend error %s: %s", response.status_code, response.text)
    except Exception:
        logger.exception("Error enviando email a %s", to_email)
