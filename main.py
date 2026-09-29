import asyncio
import json
import logging
import uuid
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import fiscal_engine
import sheets_client
import telegram_client
import tools
from claude_client import run_conversation_turn
from config import ALEJANDRO_CHAT_ID, CRON_SECRET, TELEGRAM_WEBHOOK_SECRET
from escalation import EscalationReason
from facturapi_client import create_invoice, create_rep, download_pdf, download_xml, search_invoice_by_uuid
from models import (
    ConceptoItem,
    EmisorData,
    FacturaData,
    InvoiceData,
    InvoiceDraft,
    PendingPayload,
    RepData,
    RepDraft,
)
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
    # Los callback_query (botones inline) se rutean antes que los mensajes
    callback_query = update.get("callback_query")
    if callback_query:
        try:
            await handle_callback_query(callback_query)
        except Exception as exc:
            logger.exception("Error no capturado en callback_query")
            try:
                await telegram_client.send_message(
                    ALEJANDRO_CHAT_ID,
                    f"🔴 Error crítico en callback_query\n{type(exc).__name__}: {exc}"
                )
            except Exception:
                pass
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = str(message["chat"]["id"])
    message_id = int(message.get("message_id", 0))

    try:
        # Idempotency — skip if already processed
        if await asyncio.to_thread(sheets_client.is_message_already_processed, chat_id, message_id):
            logger.info("Mensaje %d ya procesado, ignorando", message_id)
            return

        # Route commands
        text = message.get("text", "")
        if text.startswith("/aprobar") or text.startswith("/rechazar"):
            await handle_approval_command(chat_id, message_id, text)
            return

        # Identify client
        client_profile = await asyncio.to_thread(sheets_client.get_client_by_canal_id, "telegram", chat_id)
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

        # /start — reinicio explícito del historial. Los clientes ya lo
        # usaban intuitivamente ("/start", "empieza de nuevo") esperando
        # borrón y cuenta nueva, pero antes no hacía nada especial: el
        # historial seguía creciendo indefinidamente y cada factura nueva
        # pagaba por reenviar TODAS las conversaciones previas ya
        # completadas (medido: ~$0.22 extra por factura en un caso real
        # con 80 mensajes acumulados) — además de arriesgar que Claude se
        # distraiga con contexto de pedidos viejos ya resueltos.
        if text.strip() == "/start":
            await asyncio.to_thread(sheets_client.save_history, "telegram", chat_id, [])
            await telegram_client.send_message(
                chat_id, f"¡Listo! Empezamos de cero. 😊 Bienvenido a **{client_profile.nombre_comercial}** con ANB Consultores."
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
    history = await asyncio.to_thread(sheets_client.load_history, chat_id)
    catalogo = await asyncio.to_thread(
        sheets_client.get_catalogo_claves, client_profile.despacho_id, client_profile.id_cliente
    )

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

        # En hilo aparte: la llamada a Claude tarda segundos (PDFs: hasta un minuto)
        # y bloquearía el procesamiento de TODOS los demás clientes.
        client_message, invoice_draft, rep_draft = await asyncio.to_thread(
            run_conversation_turn,
            profile=client_profile,
            catalogo=catalogo,
            history=history,
            user_text=user_text,
            file_bytes=file_bytes,
            media_type=media_type,
        )

    except Exception as exc:
        error_str = str(exc)
        # Historial corrupto (tool_use/tool_result desparejados — ver el fix
        # de disable_parallel_tool_use en claude_client.py, esto es la red
        # de seguridad por si algo similar vuelve a pasar por otra causa) —
        # limpiar y pedir reintento en vez de dejar al cliente atascado para
        # siempre (el historial roto ya habría quedado guardado).
        if "tool_result" in error_str and "tool_use" in error_str:
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
            await asyncio.to_thread(sheets_client.save_history, "telegram", chat_id, history)
        except Exception:
            logger.exception("Error guardando historial para %s", chat_id)

    await telegram_client.send_message(chat_id, client_message)

    if invoice_draft:
        invoice_id = str(uuid.uuid4())
        await _calcular_y_procesar_factura(invoice_id, invoice_draft, client_profile, chat_id, message_id)
    elif rep_draft:
        invoice_id = str(uuid.uuid4())
        await _calcular_y_timbrar_rep(invoice_id, rep_draft, client_profile, chat_id, message_id)


# ---------------------------------------------------------------------------
# Factura de ingreso — cálculo, escalamiento, confirmación del cliente
# ---------------------------------------------------------------------------

def _build_invoice_data(draft: InvoiceDraft, client_profile, calculada: fiscal_engine.FacturaCalculada) -> InvoiceData:
    emisor = EmisorData(
        nombre_comercial=client_profile.nombre_comercial,
        razon_social=client_profile.razon_social,
        rfc=client_profile.rfc,
        regimen_fiscal=client_profile.regimen_fiscal,
        cp_fiscal=client_profile.cp_fiscal,
    )
    conceptos = [
        ConceptoItem(
            descripcion=cc.descripcion,
            clave_prod_serv=cc.clave_prod_serv,
            cantidad=cc.cantidad,
            clave_unidad=cc.clave_unidad,
            precio_unitario=cc.precio_unitario,
            ieps=cc.ieps,
            ieps_tasa=cc.ieps_tasa,
        )
        for cc in calculada.conceptos
    ]
    factura = FacturaData(
        conceptos=conceptos,
        monto_antes_impuestos=calculada.subtotal,
        ieps=calculada.ieps,
        iva=calculada.iva,
        tasa_iva=calculada.tasa_iva,
        retencion_iva=calculada.retencion_iva,
        retencion_iva_tasa=calculada.retencion_iva_tasa,
        retencion_isr=calculada.retencion_isr,
        retencion_isr_tasa=calculada.retencion_isr_tasa,
        total_estimado=calculada.total,
        metodo_pago=calculada.metodo_pago,
        forma_pago=calculada.forma_pago,
        observaciones=draft.factura.observaciones,
    )
    return InvoiceData(estatus="confirmado_por_cliente", emisor=emisor, receptor=draft.receptor, factura=factura)


async def _calcular_y_procesar_factura(
    invoice_id: str,
    draft: InvoiceDraft,
    client_profile,
    chat_id: str,
    message_id: int,
    omitir_validacion_cruzada: bool = False,
) -> None:
    """
    Calcula la factura con fiscal_engine y decide la ruta:
    - clave_prod_serv nueva sin aprobar -> escalar a ANB.
    - el motor detecta inconsistencia (VALIDACION_ARITMETICA) -> escalar a ANB.
    - todo cuadra -> vista previa con montos exactos al CLIENTE (botones Sí/No).

    omitir_validacion_cruzada=True SOLO cuando ANB ya aprobó una escalación
    por VALIDACION_ARITMETICA y se está recalculando tras esa aprobación
    (ver _execute_approval). Nunca se activa desde el flujo normal.
    """
    if not omitir_validacion_cruzada:
        claves_nuevas = [c for c in draft.factura.conceptos if c.clave_prod_serv == tools.CLAVE_NUEVA]
        if claves_nuevas:
            detalle = "; ".join(
                f"{c.descripcion} -> propuesta {c.clave_prod_serv_propuesta}" for c in claves_nuevas
            )
            envelope = PendingPayload(
                tipo="ingreso",
                escalation_reason=EscalationReason.CLAVE_PROD_SERV_NUEVA.value,
                escalation_detail=detalle,
                invoice_draft=draft,
            )
            await _escalar_a_anb(invoice_id, chat_id, message_id, client_profile, envelope)
            return

    try:
        reglas = await asyncio.to_thread(
            sheets_client.get_fiscal_rules, client_profile.despacho_id, client_profile.id_cliente
        )
    except ValueError as exc:
        logger.error("Cliente %s sin reglas fiscales vigentes: %s", client_profile.id_cliente, exc)
        await telegram_client.send_message(chat_id, "Ocurrió un error inesperado. El despacho ha sido notificado.")
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"🔴 {client_profile.nombre_comercial} no tiene reglas fiscales vigentes en Supabase: {exc}"
        )
        return

    conceptos_extraidos = [
        fiscal_engine.ConceptoExtraido(
            descripcion=c.descripcion,
            cantidad=Decimal(str(c.cantidad)),
            precio_unitario=Decimal(str(c.precio_unitario)),
            clave_unidad=c.clave_unidad,
            clave_prod_serv=c.clave_prod_serv,
        )
        for c in draft.factura.conceptos
    ]
    total_fuente = (
        Decimal(str(draft.factura.total_documento_fuente))
        if draft.factura.total_documento_fuente is not None else None
    )

    resultado = fiscal_engine.calcular_factura(
        conceptos_extraidos, draft.receptor, reglas,
        draft.factura.metodo_pago, draft.factura.forma_pago,
        total_documento_fuente=total_fuente,
        omitir_validacion_cruzada=omitir_validacion_cruzada,
    )

    if resultado.escalation:
        envelope = PendingPayload(
            tipo="ingreso",
            escalation_reason=resultado.escalation.reason.value,
            escalation_detail=resultado.escalation.detail,
            invoice_draft=draft,
        )
        await _escalar_a_anb(invoice_id, chat_id, message_id, client_profile, envelope)
        return

    invoice_data = _build_invoice_data(draft, client_profile, resultado.factura)
    await _solicitar_confirmacion_cliente(invoice_id, invoice_data, client_profile, chat_id, message_id)


async def _escalar_a_anb(
    invoice_id: str, chat_id: str, message_id: int, client_profile, envelope: PendingPayload
) -> None:
    await asyncio.to_thread(
        sheets_client.save_pending,
        invoice_id=invoice_id,
        canal="telegram",
        canal_id=chat_id,
        telegram_message_id=message_id,
        invoice_json=envelope.model_dump_json(),
        motivo_revision=f"[{envelope.escalation_reason}] {envelope.escalation_detail}",
        tipo_aprobacion="anb_revision",
    )
    await telegram_client.send_message(
        chat_id,
        "Tu solicitud está siendo revisada por el despacho. Te notificaremos en breve. ✅"
    )
    tipo_texto = "REP" if envelope.tipo == "rep" else "Factura"
    await telegram_client.send_message(
        ALEJANDRO_CHAT_ID,
        f"📋 {tipo_texto} pendiente de revisión\n"
        f"Cliente: {client_profile.nombre_comercial}\n"
        f"ID: {invoice_id}\n"
        f"Motivo: [{envelope.escalation_reason}] {envelope.escalation_detail}\n\n"
        f"/aprobar {invoice_id}\n/rechazar {invoice_id}",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Aprobar", "callback_data": f"aprobar:{invoice_id}"},
            {"text": "❌ Rechazar", "callback_data": f"rechazar:{invoice_id}"},
        ]]},
    )
    await asyncio.to_thread(
        sheets_client.log_to_bitacora,
        invoice_id=invoice_id,
        canal_id=chat_id,
        rfc_emisor=client_profile.rfc,
        rfc_receptor="",
        monto=0,
        total=0,
        requirio_revision=True,
        estado="pendiente_revision",
        tipo=envelope.tipo,
    )


async def _solicitar_confirmacion_cliente(
    invoice_id: str, invoice_data: InvoiceData, client_profile, chat_id: str, message_id: int
) -> None:
    await asyncio.to_thread(
        sheets_client.save_pending,
        invoice_id=invoice_id,
        canal="telegram",
        canal_id=chat_id,
        telegram_message_id=message_id,
        invoice_json=invoice_data.model_dump_json(),
        motivo_revision="",
        tipo_aprobacion="cliente_confirmacion",
        canal_id_aprobador=chat_id,
    )
    f = invoice_data.factura
    conceptos_texto = "\n".join(
        f"  • {c.descripcion} — {c.cantidad} x ${c.precio_unitario:,.2f}" for c in f.conceptos
    )
    lineas = [
        "📄 Resumen de tu factura:",
        "",
        conceptos_texto,
        "",
        f"Subtotal: ${f.monto_antes_impuestos:,.2f}",
    ]
    if f.ieps > 0:
        lineas.append(f"IEPS: ${f.ieps:,.2f}")
    lineas.append(f"IVA: ${f.iva:,.2f}")
    if f.retencion_iva > 0:
        lineas.append(f"Retención IVA: -${f.retencion_iva:,.2f}")
    if f.retencion_isr > 0:
        lineas.append(f"Retención ISR: -${f.retencion_isr:,.2f}")
    lineas.append(f"Total: ${f.total_estimado:,.2f}")
    lineas.append("")
    lineas.append("¿Confirmas estos datos para timbrar tu factura?")

    await telegram_client.send_message(
        chat_id, "\n".join(lineas),
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Sí, confirmar", "callback_data": f"cliente_si:{invoice_id}"},
            {"text": "❌ No, corregir", "callback_data": f"cliente_no:{invoice_id}"},
        ]]},
    )


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
            f"❌ Error FacturAPI [error_timbre_sat]\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}\n{error_msg}"
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
        )
        return
    except httpx.RequestError as exc:
        # Timeout o error de conexión/DNS: la factura PUDO haberse timbrado en
        # FacturAPI aunque no recibimos respuesta. No reintentar a ciegas.
        error_msg = f"{type(exc).__name__}: {exc}"[:500]
        logger.error("FacturAPI error de red para %s: %s", invoice_id, error_msg)
        await telegram_client.send_message(
            chat_id,
            "Hubo un problema de conexión al generar tu factura. "
            "El despacho ha sido notificado y lo resolveremos pronto."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⏱️ Error de red con FacturAPI\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}\n{error_msg}\n\n"
            f"⚠️ Verifica en FacturAPI si la factura se timbró ANTES de reintentar."
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
        )
        return

    # A partir de aquí la factura YA está timbrada: un fallo en la descarga o
    # entrega NO debe registrarse como error (el cliente reintentaría y duplicaría).
    try:
        pdf_bytes = await download_pdf(folio, facturapi_key)
        xml_bytes = await download_xml(folio, facturapi_key)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("Factura %s timbrada (folio %s) pero falló la descarga: %s", invoice_id, folio, exc)
        await telegram_client.send_message(
            chat_id,
            f"✅ Tu factura fue timbrada (folio: {folio}), pero hubo un problema al "
            "enviarte los archivos. El despacho te los hará llegar."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ Factura {invoice_id} timbrada (folio {folio}) pero falló la descarga del PDF/XML. "
            f"Descárgalos de FacturAPI y envíalos al cliente.\n{type(exc).__name__}: {exc}"
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=invoice_data.emisor.rfc, rfc_receptor=invoice_data.receptor.rfc,
            monto=invoice_data.factura.monto_antes_impuestos,
            total=invoice_data.factura.total_estimado,
            requirio_revision=False, estado="timbrado", folio_fiscal=folio,
            error_detalle="timbrada OK; falló descarga/entrega de PDF y XML",
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
    await asyncio.to_thread(
        sheets_client.log_to_bitacora,
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

async def _calcular_y_timbrar_rep(
    invoice_id: str, draft: RepDraft, client_profile, chat_id: str, message_id: int,
) -> None:
    """
    Busca la factura original + REPs previos, calcula el saldo insoluto con
    fiscal_engine (fresco, justo antes de timbrar — evita depender de un
    cálculo viejo si llegaron más pagos mientras tanto), y timbra o escala.
    """
    facturapi_key = client_profile.facturapi_key if client_profile else ""

    try:
        original_invoice = await search_invoice_by_uuid(draft.uuid_factura_origen, facturapi_key)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("Error buscando factura origen %s: %s", draft.uuid_factura_origen, exc)
        await telegram_client.send_message(
            chat_id,
            "Hubo un problema de conexión al buscar tu factura original. "
            "El despacho ha sido notificado; vuelve a intentarlo en unos minutos."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⏱️ Error de red buscando UUID {draft.uuid_factura_origen} para REP\n"
            f"Cliente: {client_profile.nombre_comercial}\n{type(exc).__name__}: {exc}"
        )
        return
    if not original_invoice:
        await telegram_client.send_message(
            chat_id,
            "No encontré la factura original en el sistema. El despacho revisará tu complemento de pago."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ REP: no se encontró UUID {draft.uuid_factura_origen} en FacturAPI\n"
            f"Cliente: {client_profile.nombre_comercial}\nMonto: ${draft.monto_pagado:,.2f}"
        )
        return

    invoice_total = Decimal(str(original_invoice.get("total", 0)))
    previous_reps = await asyncio.to_thread(sheets_client.get_rep_history, draft.uuid_factura_origen)
    if previous_reps:
        imp_saldo_ant = Decimal(str(previous_reps[-1].get("imp_saldo_insoluto") or 0))
        num_parcialidad = len(previous_reps) + 1
    else:
        imp_saldo_ant = invoice_total
        num_parcialidad = 1

    resultado = fiscal_engine.calcular_rep(
        monto_pagado=Decimal(str(draft.monto_pagado)), imp_saldo_ant=imp_saldo_ant
    )

    if resultado.escalation:
        envelope = PendingPayload(
            tipo="rep",
            escalation_reason=resultado.escalation.reason.value,
            escalation_detail=resultado.escalation.detail,
            rep_draft=draft,
        )
        await _escalar_a_anb(invoice_id, chat_id, message_id, client_profile, envelope)
        return

    rep_data = RepData(
        estatus="confirmado_por_cliente",
        uuid_factura_origen=draft.uuid_factura_origen,
        receptor=draft.receptor,
        fecha_pago=draft.fecha_pago,
        forma_pago=draft.forma_pago,
        monto_pagado=resultado.rep.monto_pagado,
        imp_saldo_ant=resultado.rep.imp_saldo_ant,
        imp_saldo_insoluto=resultado.rep.imp_saldo_insoluto,
    )
    await _solicitar_confirmacion_cliente_rep(invoice_id, rep_data, client_profile, chat_id, message_id)


async def _solicitar_confirmacion_cliente_rep(
    invoice_id: str, rep_data: RepData, client_profile, chat_id: str, message_id: int
) -> None:
    await asyncio.to_thread(
        sheets_client.save_pending,
        invoice_id=invoice_id,
        canal="telegram",
        canal_id=chat_id,
        telegram_message_id=message_id,
        invoice_json=rep_data.model_dump_json(),
        motivo_revision="",
        tipo_aprobacion="cliente_confirmacion",
        canal_id_aprobador=chat_id,
    )
    lineas = [
        "💳 Resumen de tu complemento de pago (REP):",
        "",
        f"Factura original: {rep_data.uuid_factura_origen}",
        f"Monto pagado: ${rep_data.monto_pagado:,.2f}",
        f"Fecha de pago: {rep_data.fecha_pago}",
        f"Saldo insoluto después de este pago: ${rep_data.imp_saldo_insoluto:,.2f}",
        "",
        "¿Confirmas estos datos para timbrar el complemento de pago?",
    ]
    await telegram_client.send_message(
        chat_id, "\n".join(lineas),
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Sí, confirmar", "callback_data": f"cliente_si:{invoice_id}"},
            {"text": "❌ No, corregir", "callback_data": f"cliente_no:{invoice_id}"},
        ]]},
    )


async def _timbre_and_deliver_rep(
    invoice_id: str,
    rep_data: RepData,
    client_profile,
    chat_id: str,
    facturapi_key: str,
    num_parcialidad: int,
    original_invoice: dict,
) -> None:
    try:
        result = await create_rep(rep_data, facturapi_key, num_parcialidad, original_invoice)
        folio = result.get("id", "")
    except httpx.HTTPStatusError as exc:
        error_msg = str(exc)[:500]
        await telegram_client.send_message(
            chat_id,
            "El SAT rechazó el complemento de pago. Tu despacho te contactará."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"❌ Error REP FacturAPI [error_timbre_sat]\nCliente: {client_profile.nombre_comercial}\n{error_msg}"
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
            monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
            tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        )
        return
    except httpx.RequestError as exc:
        # Timeout o error de conexión: el REP pudo haberse timbrado. No reintentar a ciegas.
        error_msg = f"{type(exc).__name__}: {exc}"[:500]
        await telegram_client.send_message(
            chat_id,
            "Hubo un problema de conexión al generar el complemento. El despacho ha sido notificado."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⏱️ Error de red en REP\nCliente: {client_profile.nombre_comercial}\nID: {invoice_id}\n{error_msg}\n\n"
            f"⚠️ Verifica en FacturAPI si el REP se timbró ANTES de reintentar."
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
            monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
            requirio_revision=False, estado="error", error_detalle=error_msg,
            tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        )
        return

    # REP ya timbrado: registrar SIEMPRE en bitácora aunque falle la entrega,
    # porque la siguiente parcialidad depende de este imp_saldo_insoluto.
    try:
        pdf_bytes = await download_pdf(folio, facturapi_key)
        xml_bytes = await download_xml(folio, facturapi_key)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("REP %s timbrado (folio %s) pero falló la descarga: %s", invoice_id, folio, exc)
        await telegram_client.send_message(
            chat_id,
            f"✅ Tu complemento de pago fue timbrado (folio: {folio}), pero hubo un problema "
            "al enviarte los archivos. El despacho te los hará llegar."
        )
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ REP {invoice_id} timbrado (folio {folio}) pero falló la descarga del PDF/XML. "
            f"Descárgalos de FacturAPI y envíalos al cliente.\n{type(exc).__name__}: {exc}"
        )
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=chat_id,
            rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
            monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
            requirio_revision=False, estado="timbrado", folio_fiscal=folio,
            error_detalle="timbrado OK; falló descarga/entrega de PDF y XML",
            tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
            imp_saldo_insoluto=rep_data.imp_saldo_insoluto,
        )
        return

    await telegram_client.send_document(
        chat_id, pdf_bytes, f"rep_{invoice_id[:8]}.pdf",
        caption=f"✅ Complemento de pago timbrado. Folio: {folio}"
    )
    await telegram_client.send_document(chat_id, xml_bytes, f"rep_{invoice_id[:8]}.xml")
    await asyncio.to_thread(
        sheets_client.log_to_bitacora,
        invoice_id=invoice_id, canal_id=chat_id,
        rfc_emisor=client_profile.rfc, rfc_receptor=rep_data.receptor.rfc,
        monto=rep_data.monto_pagado, total=rep_data.monto_pagado,
        requirio_revision=False, estado="timbrado", folio_fiscal=folio,
        tipo="rep", uuid_factura_origen=rep_data.uuid_factura_origen,
        imp_saldo_insoluto=rep_data.imp_saldo_insoluto,
    )
    logger.info("REP timbrado: %s → folio %s", invoice_id, folio)


# ---------------------------------------------------------------------------
# Aprobación de ANB (/aprobar, /rechazar) — solo para escalamientos
# ---------------------------------------------------------------------------

async def _execute_approval(command: str, invoice_id: str) -> str:
    """
    Ejecuta 'aprobar' o 'rechazar' de ANB sobre un escalamiento
    (tipo_aprobacion='anb_revision'). 'aprobar' NUNCA timbra directo: para
    facturas, vuelve a calcular y manda al CLIENTE la confirmación final
    (la aprobación de ANB resuelve la inconsistencia, no reemplaza la
    confirmación del cliente). Para REP, sí puede timbrar directo si el
    recálculo ya no escala (no hay confirmación de cliente para REP).
    """
    pending = await asyncio.to_thread(sheets_client.get_pending, invoice_id)
    if not pending:
        msg = f"No encontré la solicitud con ID: {invoice_id}"
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    if str(pending.get("tipo_aprobacion", "anb_revision")) != "anb_revision":
        msg = f"{invoice_id} no es una revisión de ANB (es confirmación de cliente) — usa los botones que le llegaron al cliente."
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    estado_actual = str(pending.get("estado", ""))
    if estado_actual != "pendiente":
        msg = f"La solicitud {invoice_id} ya fue procesada (estado: {estado_actual}). No se timbró de nuevo."
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    client_canal_id = str(pending["canal_id"])

    try:
        payload = json.loads(pending["invoice_json"])
        envelope = PendingPayload(**payload)
    except Exception as exc:
        logger.exception("Error leyendo invoice_json para %s", invoice_id)
        msg = f"Error al leer datos de la solicitud: {exc}"
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    es_rep = envelope.tipo == "rep"

    if command == "rechazar":
        await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "rechazado")
        await telegram_client.send_message(
            client_canal_id,
            "Tu solicitud no pudo procesarse. El despacho te contactará para más información."
        )
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"✅ {'REP' if es_rep else 'Factura'} {invoice_id} rechazado.")
        await asyncio.to_thread(
            sheets_client.log_to_bitacora,
            invoice_id=invoice_id, canal_id=client_canal_id,
            rfc_emisor="", rfc_receptor="",
            monto=0, total=0,
            requirio_revision=True, estado="rechazado",
            tipo="rep" if es_rep else "ingreso",
            uuid_factura_origen=envelope.rep_draft.uuid_factura_origen if es_rep else "",
        )
        return f"{'REP' if es_rep else 'Factura'} rechazado."

    # aprobar
    canal = str(pending.get("canal", "telegram"))
    client_profile = await asyncio.to_thread(sheets_client.get_client_by_canal_id, canal, client_canal_id)
    if not client_profile:
        msg = (
            f"⚠️ No encontré el perfil del cliente (canal_id: {client_canal_id}). "
            "No se puede timbrar sin la API key de FacturAPI."
        )
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "aprobado")
    nuevo_invoice_id = str(uuid.uuid4())

    if es_rep:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Reintentando REP {invoice_id}...")
        await _calcular_y_timbrar_rep(nuevo_invoice_id, envelope.rep_draft, client_profile, client_canal_id, 0)
        return f"Reintentando {invoice_id}..."

    draft = envelope.invoice_draft
    omitir_validacion = True
    if envelope.escalation_reason == EscalationReason.CLAVE_PROD_SERV_NUEVA.value:
        draft = draft.model_copy(deep=True)
        for c in draft.factura.conceptos:
            if c.clave_prod_serv == tools.CLAVE_NUEVA and c.clave_prod_serv_propuesta:
                await asyncio.to_thread(
                    sheets_client.save_clave_aprobada,
                    despacho_id=client_profile.despacho_id,
                    id_cliente=client_profile.id_cliente,
                    clave_prod_serv=c.clave_prod_serv_propuesta,
                    descripcion_clave=c.descripcion,
                    aplica_ieps=False,  # ANB puede corregir esto en Supabase si el concepto sí lleva IEPS
                    aprobada_por="ALEJANDRO",
                )
                c.clave_prod_serv = c.clave_prod_serv_propuesta
        omitir_validacion = False  # ya no hay "NUEVA" en el draft, no hace falta omitir nada

    await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Recalculando {invoice_id}...")
    await _calcular_y_procesar_factura(
        nuevo_invoice_id, draft, client_profile, client_canal_id, 0,
        omitir_validacion_cruzada=omitir_validacion,
    )
    return f"Recalculando {invoice_id}..."


async def handle_approval_command(chat_id: str, message_id: int, text: str) -> None:
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

    command = parts[0].lower().lstrip("/")
    invoice_id = parts[1]
    await _execute_approval(command, invoice_id)


# ---------------------------------------------------------------------------
# Confirmación del cliente (botones Sí/No sobre su propia factura)
# ---------------------------------------------------------------------------

async def _execute_client_confirmation(command: str, invoice_id: str, pending: dict) -> None:
    estado_actual = str(pending.get("estado", ""))
    if estado_actual != "pendiente":
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID, f"La solicitud {invoice_id} ya fue procesada (estado: {estado_actual})."
        )
        return

    chat_id = str(pending["canal_id"])

    if command == "cliente_no":
        await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "rechazado_cliente")
        await telegram_client.send_message(chat_id, "Entendido, mándame los datos corregidos cuando quieras.")
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"ℹ️ El cliente no confirmó su vista previa: {invoice_id}")
        return

    try:
        payload = json.loads(pending["invoice_json"])
        es_rep = "uuid_factura_origen" in payload
        invoice_data = None if es_rep else InvoiceData(**payload)
        rep_data = RepData(**payload) if es_rep else None
    except Exception as exc:
        logger.exception("Error reconstruyendo datos confirmados por cliente %s", invoice_id)
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⚠️ Error al leer datos de {invoice_id}: {exc}")
        return

    canal = str(pending.get("canal", "telegram"))
    client_profile = await asyncio.to_thread(sheets_client.get_client_by_canal_id, canal, chat_id)
    if not client_profile:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⚠️ No encontré el perfil del cliente para {invoice_id}.")
        return

    await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "aprobado")
    message_id = int(pending.get("telegram_message_id") or 0)

    if es_rep:
        facturapi_key = client_profile.facturapi_key if client_profile else ""
        # Recalcular num_parcialidad y buscar la factura original FRESCOS,
        # justo antes de timbrar — el cliente pudo tardar en confirmar y
        # otro pago pudo haberse timbrado mientras tanto.
        try:
            original_invoice = await search_invoice_by_uuid(rep_data.uuid_factura_origen, facturapi_key)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            await telegram_client.send_message(
                chat_id,
                "Hubo un problema de conexión al confirmar tu pago. El despacho ha sido notificado."
            )
            await telegram_client.send_message(
                ALEJANDRO_CHAT_ID,
                f"⏱️ Error de red confirmando REP {invoice_id}\n{type(exc).__name__}: {exc}"
            )
            return
        if not original_invoice:
            await telegram_client.send_message(
                ALEJANDRO_CHAT_ID, f"⚠️ No encontré la factura original al confirmar REP {invoice_id}."
            )
            return
        previous_reps = await asyncio.to_thread(sheets_client.get_rep_history, rep_data.uuid_factura_origen)
        num_parcialidad = len(previous_reps) + 1
        await _timbre_and_deliver_rep(
            invoice_id, rep_data, client_profile, chat_id, facturapi_key, num_parcialidad, original_invoice
        )
    else:
        await _timbre_and_deliver(invoice_id, invoice_data, client_profile, chat_id, message_id)


async def handle_callback_query(callback_query: dict) -> None:
    callback_id = callback_query["id"]
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    data = callback_query.get("data", "")
    command, _, invoice_id = data.partition(":")

    if command in ("aprobar", "rechazar"):
        if not chat_id or int(chat_id) != ALEJANDRO_CHAT_ID:
            logger.warning("Callback de aprobación de ANB de chat no autorizado: %s", chat_id)
            await telegram_client.answer_callback_query(callback_id)
            return
        await telegram_client.answer_callback_query(callback_id, "Procesando...")
        await _execute_approval(command, invoice_id)
        return

    if command in ("cliente_si", "cliente_no"):
        if not invoice_id:
            await telegram_client.answer_callback_query(callback_id, "Acción inválida")
            return
        pending = await asyncio.to_thread(sheets_client.get_pending, invoice_id)
        if not pending or str(pending.get("tipo_aprobacion", "")) != "cliente_confirmacion":
            await telegram_client.answer_callback_query(callback_id, "Solicitud no encontrada")
            return
        # CRÍTICO: un cliente no puede aprobar la factura de otro — se
        # verifica contra el canal_id que guardamos al crear el pendiente,
        # no basta con conocer el invoice_id.
        if chat_id != str(pending.get("canal_id_aprobador", "")):
            logger.warning(
                "chat_id %s intentó aprobar una confirmación que no le pertenece (invoice_id=%s)",
                chat_id, invoice_id,
            )
            await telegram_client.answer_callback_query(callback_id, "No autorizado")
            return
        await telegram_client.answer_callback_query(callback_id, "Procesando...")
        await _execute_client_confirmation(command, invoice_id, pending)
        return

    await telegram_client.answer_callback_query(callback_id, "Acción inválida")


# ---------------------------------------------------------------------------
# Cron: check overdue pending invoices
# ---------------------------------------------------------------------------

@app.post("/check-pending")
async def check_pending(x_cron_secret: Optional[str] = Header(None)):
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    overdue = await asyncio.to_thread(sheets_client.get_overdue_pending, 24)
    if not overdue:
        return {"checked": 0}

    for row in overdue:
        invoice_id = row.get("id", "?")
        tipo_aprobacion = str(row.get("tipo_aprobacion", "anb_revision"))
        canal_id = row.get("canal_id", "?")
        motivo = row.get("motivo_revision", "")

        if tipo_aprobacion == "cliente_confirmacion":
            # No son botones de ANB -- solo un aviso informativo, sin
            # acción: forzar el timbrado aquí saltaría la confirmación
            # del cliente.
            await telegram_client.send_message(
                ALEJANDRO_CHAT_ID,
                f"ℹ️ El cliente (canal_id: {canal_id}) no ha confirmado su factura "
                f"{invoice_id} en más de 24h. Puedes darle seguimiento manual."
            )
            continue

        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID,
            f"⚠️ Solicitud sin revisión hace más de 24h\n"
            f"ID: {invoice_id}\nCliente canal_id: {canal_id}\nMotivo: {motivo}\n\n"
            f"/aprobar {invoice_id}\n/rechazar {invoice_id}",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Aprobar", "callback_data": f"aprobar:{invoice_id}"},
                {"text": "❌ Rechazar", "callback_data": f"rechazar:{invoice_id}"},
            ]]},
        )

    return {"checked": len(overdue)}
