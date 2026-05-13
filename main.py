import json
import logging
import uuid
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import sheets_client
import telegram_client
from claude_client import run_conversation_turn
from config import ALEJANDRO_CHAT_ID, CRON_SECRET, TELEGRAM_WEBHOOK_SECRET
from facturapi_client import create_invoice, download_pdf, download_xml
from models import InvoiceData

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="ANB Billing Agent")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    # Security: validate Telegram webhook secret
    if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()
    background_tasks.add_task(process_update, update)
    return {"ok": True}


async def process_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = str(message["chat"]["id"])
    message_id = int(message.get("message_id", 0))

    # Idempotency — skip if already processed
    if sheets_client.is_message_already_processed(message_id):
        logger.info("Mensaje %d ya procesado, ignorando", message_id)
        return

    # Route commands
    text = message.get("text", "")
    if text.startswith("/aprobar") or text.startswith("/rechazar"):
        await handle_approval_command(chat_id, message_id, text)
        return

    # Identify client
    client_profile = sheets_client.get_client_by_canal_id("telegram", chat_id)
    if client_profile is None:
        await telegram_client.send_message(
            chat_id,
            "No encontré tu perfil en el sistema. Contacta a ANB Consultores para activar tu acceso."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ Mensaje de canal_id desconocido: {chat_id}\nMensaje: {text[:200]}"
        )
        return

    # Acquire per-channel lock (prevents concurrent history corruption)
    async with sheets_client.get_channel_lock(chat_id):
        await handle_conversation(client_profile, chat_id, message_id, message)


async def handle_conversation(client_profile, chat_id: str, message_id: int, message: dict) -> None:
    history = sheets_client.load_history(chat_id)

    # Extract text and/or file
    user_text: Optional[str] = message.get("text") or message.get("caption")
    file_bytes: Optional[bytes] = None
    media_type: Optional[str] = None

    try:
        if message.get("document"):
            file_id = message["document"]["file_id"]
            mime = message["document"].get("mime_type", "application/octet-stream")
            if mime == "application/pdf":
                file_bytes = await telegram_client.get_file(file_id)
                media_type = "application/pdf"
            else:
                await telegram_client.send_message(chat_id, "Por favor envía la CSF como PDF o imagen (JPG/PNG).")
                return

        elif message.get("photo"):
            # Take the largest photo
            photo = message["photo"][-1]
            file_bytes = await telegram_client.get_file(photo["file_id"])
            media_type = "image/jpeg"

        if not user_text and not file_bytes:
            await telegram_client.send_message(chat_id, "No entendí tu mensaje. ¿En qué te puedo ayudar?")
            return

        client_message, invoice_data = run_conversation_turn(
            profile=client_profile,
            history=history,
            user_text=user_text,
            file_bytes=file_bytes,
            media_type=media_type,
        )

    except Exception as exc:
        logger.exception("Error en conversación para chat_id %s", chat_id)
        await telegram_client.send_message(chat_id, "Ocurrió un error inesperado. El despacho ha sido notificado.")
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"❌ Error en conversación con {client_profile.nombre_comercial} ({chat_id}):\n{exc}"
        )
        return
    finally:
        # Always save history (even on error, to preserve what we have)
        try:
            sheets_client.save_history("telegram", chat_id, history)
        except Exception:
            logger.exception("Error guardando historial para %s", chat_id)

    await telegram_client.send_message(chat_id, client_message)

    if invoice_data:
        await process_invoice(invoice_data, client_profile, chat_id, message_id)


async def process_invoice(
    invoice_data: InvoiceData,
    client_profile,
    chat_id: str,
    message_id: int,
) -> None:
    invoice_id = str(uuid.uuid4())

    if invoice_data.requiere_revision or client_profile.requiere_revision:
        motivo = invoice_data.motivo_revision or "Perfil del cliente marcado para revisión manual"
        sheets_client.save_pending(
            invoice_id=invoice_id,
            canal="telegram",
            canal_id=chat_id,
            telegram_message_id=message_id,
            invoice_json=invoice_data.model_dump_json(),
            motivo_revision=motivo,
        )
        await telegram_client.send_message(
            chat_id,
            "Tu solicitud de factura está siendo revisada por el despacho. "
            "Te notificaremos en breve. ✅"
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"📋 *Factura pendiente de revisión*\n"
            f"Cliente: {client_profile.nombre_comercial}\n"
            f"ID: {invoice_id}\n"
            f"Motivo: {motivo}\n"
            f"Monto: ${invoice_data.factura.monto_antes_impuestos:,.2f}\n"
            f"Total: ${invoice_data.factura.total_estimado:,.2f}\n\n"
            f"Responde:\n/aprobar {invoice_id}\n/rechazar {invoice_id}"
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id,
            canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc,
            rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=True,
            estado="pendiente_revision",
        )
        return

    # Auto-timbre
    await _timbre_and_deliver(invoice_id, invoice_data, client_profile, chat_id, message_id)


async def _timbre_and_deliver(
    invoice_id: str,
    invoice_data: InvoiceData,
    client_profile,
    chat_id: str,
    message_id: int,
) -> None:
    try:
        result = await create_invoice(invoice_data)
        folio = result.get("id", "")
        pdf_bytes = await download_pdf(folio)
        xml_bytes = await download_xml(folio)
    except httpx.TimeoutException:
        error_msg = "timeout al conectar con FacturAPI"
        logger.error("FacturAPI timeout para %s", invoice_id)
        await telegram_client.send_message(
            chat_id,
            "Hubo un problema al generar tu factura (tiempo de espera agotado). "
            "El despacho ha sido notificado y lo resolveremos pronto."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⏱️ Timeout en FacturAPI\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}"
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
        )
        return
    except httpx.HTTPStatusError as exc:
        error_msg = str(exc)[:500]
        logger.error("FacturAPI error HTTP para %s: %s", invoice_id, error_msg)
        if exc.response.status_code >= 500:
            client_msg = "Error temporal en el sistema de facturación. El despacho lo resolverá pronto."
        else:
            client_msg = "El SAT rechazó la factura. Tu despacho te contactará para resolverlo."
        await telegram_client.send_message(chat_id, client_msg)
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"❌ Error FacturAPI\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}\n{error_msg}"
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
        )
        return

    # Éxito
    await telegram_client.send_document(
        chat_id, pdf_bytes, f"factura_{invoice_id[:8]}.pdf",
        caption=f"✅ Tu factura ha sido timbrada. Folio: {folio}"
    )
    await telegram_client.send_document(
        chat_id, xml_bytes, f"factura_{invoice_id[:8]}.xml"
    )
    sheets_client.log_to_bitacora(
        invoice_id=invoice_id, canal_id=chat_id,
        rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
        monto=invoice_data.factura.monto_antes_impuestos,
        total=invoice_data.factura.total_estimado,
        requirio_revision=False, estado="timbrado", folio_fiscal=folio,
    )
    logger.info("Factura timbrada: %s → folio %s", invoice_id, folio)


# ---------------------------------------------------------------------------
# Approval commands (/aprobar, /rechazar)
# ---------------------------------------------------------------------------

async def handle_approval_command(chat_id: str, message_id: int, text: str) -> None:
    # CRITICAL: only Alejandro can approve/reject
    if int(chat_id) != ALEJANDRO_CHAT_ID:
        logger.warning("Usuario no autorizado intentó /aprobar o /rechazar: %s", chat_id)
        return

    parts = text.strip().split()
    if len(parts) < 2:
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            "Formato: /aprobar {id} o /rechazar {id}"
        )
        return

    command = parts[0].lower()
    invoice_id = parts[1]

    pending = sheets_client.get_pending(invoice_id)
    if not pending:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"No encontré la factura con ID: {invoice_id}")
        return

    client_canal_id = str(pending["canal_id"])

    if command == "/rechazar":
        sheets_client.update_pending_status(pending["row_index"], "rechazado")
        await telegram_client.send_message(
            client_canal_id,
            "Tu solicitud de factura no pudo procesarse. El despacho te contactará para más información."
        )
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"✅ Factura {invoice_id} rechazada.")
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=client_canal_id,
            rfc_emisor="", rfc_receptor="",
            monto=0, total=0,
            requirio_revision=True, estado="rechazado",
        )
        return

    # /aprobar — reconstruct InvoiceData and timbre
    try:
        invoice_data = InvoiceData(**json.loads(pending["invoice_json"]))
    except Exception as exc:
        logger.exception("Error reconstruyendo InvoiceData para %s", invoice_id)
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"Error al leer datos de la factura: {exc}")
        return

    sheets_client.update_pending_status(pending["row_index"], "aprobado")
    await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Timbrando factura {invoice_id}...")
    await _timbre_and_deliver(invoice_id, invoice_data, None, client_canal_id, 0)


# ---------------------------------------------------------------------------
# Cron: check overdue pending invoices
# ---------------------------------------------------------------------------

@app.post("/check-pending")
async def check_pending(x_cron_secret: Optional[str] = Header(None)):
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    overdue = sheets_client.get_overdue_pending(hours=24)
    if not overdue:
        return {"checked": 0}

    for row in overdue:
        invoice_id = row.get("id", "?")
        canal_id = row.get("canal_id", "?")
        motivo = row.get("motivo_revision", "")
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ Factura sin revisión hace más de 24h\n"
            f"ID: {invoice_id}\nCliente canal_id: {canal_id}\nMotivo: {motivo}\n\n"
            f"/aprobar {invoice_id}\n/rechazar {invoice_id}"
        )

    return {"checked": len(overdue)}
