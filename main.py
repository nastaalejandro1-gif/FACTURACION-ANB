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
from facturapi_client import create_invoice, create_rep, download_pdf, download_xml, search_invoice_by_uuid
from models import InvoiceData, RepData
from resend_client import send_invoice_email

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

    try:
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

    except Exception as exc:
        logger.exception("Error no capturado en process_update para chat_id %s", chat_id)
        try:
            await telegram_client.send_message(
                ALEJANDRO_CHAT_ID,
                f"🔴 Error crítico en process_update\nchat_id: {chat_id}\n{type(exc).__name__}: {exc}"
            )
        except Exception:
            pass


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

        client_message, invoice_data, rep_data = run_conversation_turn(
            profile=client_profile,
            history=history,
            user_text=user_text,
            file_bytes=file_bytes,
            media_type=media_type,
        )

    except Exception as exc:
        error_str = str(exc)
        # Historial corrupto (tool_result sin tool_use correspondiente) — limpiar y pedir reintento
        if "tool_use_id" in error_str and "tool_result" in error_str:
            logger.warning("Historial corrupto para %s — limpiando y pidiendo reintento", chat_id)
            history.clear()
            await telegram_client.send_message(
                chat_id,
                "Tuve un problema con nuestra conversación anterior. Ya está resuelto — por favor vuelve a enviar tu mensaje. 🔄"
            )
            return

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
    elif rep_data:
        await process_rep(rep_data, client_profile, chat_id, message_id)


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
    facturapi_key = client_profile.facturapi_key if client_profile else ""

    try:
        result = await create_invoice(invoice_data, facturapi_key)
        folio = result.get("id", "")
        pdf_bytes = await download_pdf(folio, facturapi_key)
        xml_bytes = await download_xml(folio, facturapi_key)
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

    # Éxito — entregar por Telegram y por email
    await telegram_client.send_document(
        chat_id, pdf_bytes, f"factura_{invoice_id[:8]}.pdf",
        caption=f"✅ Tu factura ha sido timbrada. Folio: {folio}"
    )
    await telegram_client.send_document(
        chat_id, xml_bytes, f"factura_{invoice_id[:8]}.xml"
    )
    await send_invoice_email(
        to_email=client_profile.email_factura,
        nombre_comercial=client_profile.nombre_comercial,
        folio=folio,
        pdf_bytes=pdf_bytes,
        xml_bytes=xml_bytes,
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
# REP (Complemento de Pago)
# ---------------------------------------------------------------------------

async def process_rep(
    rep_data: RepData,
    client_profile,
    chat_id: str,
    message_id: int,
) -> None:
    invoice_id = str(uuid.uuid4())
    facturapi_key = client_profile.facturapi_key if client_profile else ""

    if rep_data.requiere_revision:
        motivo = rep_data.motivo_revision or "REP requiere revisión manual"
        sheets_client.save_pending(
            invoice_id=invoice_id,
            canal="telegram",
            canal_id=chat_id,
            telegram_message_id=message_id,
            invoice_json=rep_data.model_dump_json(),
            motivo_revision=motivo,
        )
        await telegram_client.send_message(
            chat_id,
            "Tu complemento de pago está siendo revisado por el despacho. Te notificamos en breve. ✅"
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"📋 *REP pendiente de revisión*\n"
            f"Cliente: {client_profile.nombre_comercial}\n"
            f"ID: {invoice_id}\n"
            f"UUID origen: {rep_data.uuid_factura_origen}\n"
            f"Monto pagado: ${rep_data.monto_pagado:,.2f}\n"
            f"Motivo: {motivo}\n\n"
            f"/aprobar {invoice_id}\n/rechazar {invoice_id}"
        )
        return

    await _timbre_and_deliver_rep(invoice_id, rep_data, client_profile, chat_id, facturapi_key)


async def _timbre_and_deliver_rep(
    invoice_id: str,
    rep_data: RepData,
    client_profile,
    chat_id: str,
    facturapi_key: str,
) -> None:
    # Look up original invoice in FacturAPI to get total and calculate saldo
    original_invoice = await search_invoice_by_uuid(rep_data.uuid_factura_origen, facturapi_key)
    if not original_invoice:
        await telegram_client.send_message(
            chat_id,
            "No encontré la factura original en el sistema. El despacho revisará tu complemento de pago."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ REP: no se encontró UUID {rep_data.uuid_factura_origen} en FacturAPI\n"
            f"Cliente: {client_profile.nombre_comercial}\nMonto: ${rep_data.monto_pagado:,.2f}"
        )
        return

    invoice_total = float(original_invoice.get("total", 0))

    # Determine parcialidad and saldo anterior from previous REPs
    previous_reps = sheets_client.get_rep_history(rep_data.uuid_factura_origen)
    if previous_reps:
        last_rep = previous_reps[-1]
        imp_saldo_ant = float(last_rep.get("imp_saldo_insoluto") or 0)
        num_parcialidad = len(previous_reps) + 1
    else:
        imp_saldo_ant = invoice_total
        num_parcialidad = 1

    try:
        result = await create_rep(rep_data, facturapi_key, num_parcialidad, imp_saldo_ant)
        folio = result.get("id", "")
        imp_saldo_insoluto = result.get("_imp_saldo_insoluto", 0.0)
        pdf_bytes = await download_pdf(folio, facturapi_key)
        xml_bytes = await download_xml(folio, facturapi_key)
    except httpx.TimeoutException:
        await telegram_client.send_message(
            chat_id,
            "Tiempo de espera agotado al generar el complemento. El despacho ha sido notificado."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⏱️ Timeout en REP\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}"
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
            monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
            requirio_revision=False, estado="error", error_detalle="timeout",
            tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        )
        return
    except httpx.HTTPStatusError as exc:
        error_msg = str(exc)[:500]
        await telegram_client.send_message(
            chat_id,
            "El SAT rechazó el complemento de pago. Tu despacho te contactará."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"❌ Error REP FacturAPI\nCliente: {client_profile.nombre_comercial}\n{error_msg}"
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
            monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
            tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        )
        return

    await telegram_client.send_document(
        chat_id, pdf_bytes, f"rep_{invoice_id[:8]}.pdf",
        caption=f"✅ Complemento de pago timbrado. Folio: {folio}"
    )
    await telegram_client.send_document(chat_id, xml_bytes, f"rep_{invoice_id[:8]}.xml")
    sheets_client.log_to_bitacora(
        invoice_id=invoice_id, canal_id=chat_id,
        rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
        monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
        requirio_revision=False, estado="timbrado", folio_fiscal=folio,
        tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        imp_saldo_insoluto=imp_saldo_insoluto,
    )
    logger.info("REP timbrado: %s → folio %s", invoice_id, folio)


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

    # Idempotencia: un /aprobar repetido sobre algo ya procesado timbraría un CFDI duplicado
    estado_actual = str(pending.get("estado", ""))
    if estado_actual != "pendiente":
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"La solicitud {invoice_id} ya fue procesada (estado: {estado_actual}). No se timbró de nuevo."
        )
        return

    client_canal_id = str(pending["canal_id"])

    try:
        payload = json.loads(pending["invoice_json"])
    except Exception as exc:
        logger.exception("Error leyendo invoice_json para %s", invoice_id)
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"Error al leer datos de la solicitud: {exc}")
        return

    # Los REPs en pendientes se distinguen por uuid_factura_origen (campo obligatorio de RepData)
    es_rep = "uuid_factura_origen" in payload

    if command == "/rechazar":
        sheets_client.update_pending_status(pending["id"], "rechazado")
        await telegram_client.send_message(
            client_canal_id,
            "Tu solicitud no pudo procesarse. El despacho te contactará para más información."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"✅ {'REP' if es_rep else 'Factura'} {invoice_id} rechazado."
        )
        sheets_client.log_to_bitacora(
            invoice_id=invoice_id, canal_id=client_canal_id,
            rfc_emisor="", rfc_receptor="",
            monto=0, total=0,
            requirio_revision=True, estado="rechazado",
            tipo="rep" if es_rep else "ingreso",
            uuid_factura_origen=payload.get("uuid_factura_origen", "") if es_rep else "",
        )
        return

    # /aprobar — reconstruct data model and timbre
    try:
        data = RepData(**payload) if es_rep else InvoiceData(**payload)
    except Exception as exc:
        logger.exception("Error reconstruyendo datos para %s", invoice_id)
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"Error al leer datos de la solicitud: {exc}")
        return

    # Retrieve the client profile to get their FacturAPI key
    canal = str(pending.get("canal", "telegram"))
    client_profile = sheets_client.get_client_by_canal_id(canal, client_canal_id)
    if not client_profile:
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ No encontré el perfil del cliente (canal_id: {client_canal_id}). "
            "No se puede timbrar sin la API key de FacturAPI."
        )
        return

    sheets_client.update_pending_status(pending["id"], "aprobado")
    if es_rep:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Timbrando REP {invoice_id}...")
        await _timbre_and_deliver_rep(invoice_id, data, client_profile, client_canal_id, client_profile.facturapi_key)
    else:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Timbrando factura {invoice_id}...")
        await _timbre_and_deliver(invoice_id, data, client_profile, client_canal_id, 0)


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
