import asyncio
import html
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import cfdi_xml
import fiscal_engine
import sheets_client
import telegram_client
import tools
from claude_client import extraer_factura_origen_de_pdf, run_conversation_turn
from config import ALEJANDRO_CHAT_ID, CRON_SECRET, TELEGRAM_WEBHOOK_SECRET
from escalation import EscalationReason
from facturapi_client import (
    create_invoice, create_rep, download_pdf, download_xml, get_invoice, search_invoice_by_uuid,
)
from models import (
    ConceptoItem,
    EmisorData,
    FacturaData,
    InvoiceData,
    InvoiceDraft,
    PendingPayload,
    ReceptorData,
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
    facturas_recientes = await asyncio.to_thread(
        sheets_client.get_facturas_recientes, client_profile.despacho_id, chat_id
    )

    # Extract text and/or file
    user_text: Optional[str] = message.get("text") or message.get("caption")
    file_bytes: Optional[bytes] = None
    media_type: Optional[str] = None

    try:
        if message.get("document"):
            file_id = message["document"]["file_id"]
            mime = message["document"].get("mime_type", "application/octet-stream")
            nombre_archivo = (message["document"].get("file_name") or "").lower()
            if mime in ("application/xml", "text/xml") or nombre_archivo.endswith(".xml"):
                xml_bytes = await telegram_client.get_file(file_id)
                nota = await _procesar_xml_factura_origen(client_profile, chat_id, message_id, xml_bytes)
                if nota is None:
                    return
                # XML registrado sin un REP esperándolo: Claude sigue la
                # conversación sabiendo que esa factura ya está disponible.
                user_text = f"{user_text}\n\n{nota}" if user_text else nota
            elif mime == "application/pdf":
                file_bytes = await telegram_client.get_file(file_id)
                media_type = "application/pdf"
                esperando = await asyncio.to_thread(sheets_client.get_pending_esperando_xml, chat_id)
                if esperando and await _procesar_pdf_factura_origen(
                    client_profile, chat_id, message_id, file_bytes, esperando
                ):
                    return
            else:
                await telegram_client.send_message(
                    chat_id, "Por favor envía el archivo como PDF, XML o imagen (JPG/PNG)."
                )
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
            facturas_recientes=facturas_recientes,
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
) -> Optional[InvoiceData]:
    """
    Calcula la factura con fiscal_engine y decide la ruta:
    - clave_prod_serv nueva sin aprobar -> escalar a ANB.
    - el motor detecta inconsistencia (VALIDACION_ARITMETICA) -> escalar a ANB.
    - todo cuadra -> vista previa con montos exactos al CLIENTE (botones Sí/No).

    omitir_validacion_cruzada=True SOLO cuando ANB ya aprobó una escalación
    por VALIDACION_ARITMETICA y se está recalculando tras esa aprobación
    (ver _execute_approval). Nunca se activa desde el flujo normal.

    Devuelve el InvoiceData si se envió al cliente para confirmación, o None
    si se escaló a ANB o hubo un error (ambos ya avisan por su cuenta).
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
            return None

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
        return None

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
        # Montos tal como quedarían si ANB aprueba, para que la revisión
        # traiga el detalle completo (mismo cálculo que hace la aprobación,
        # sin los chequeos de consistencia).
        vista_previa = fiscal_engine.calcular_factura(
            conceptos_extraidos, draft.receptor, reglas,
            draft.factura.metodo_pago, draft.factura.forma_pago,
            omitir_validacion_cruzada=True,
        ).factura
        await _escalar_a_anb(invoice_id, chat_id, message_id, client_profile, envelope, vista_previa)
        return None

    invoice_data = _build_invoice_data(draft, client_profile, resultado.factura)
    await _solicitar_confirmacion_cliente(invoice_id, invoice_data, client_profile, chat_id, message_id)
    return invoice_data


MAX_CONCEPTOS_EN_REVISION = 25  # Telegram corta mensajes de más de 4096 caracteres


def _detalle_factura_origen(origen: Optional[dict]) -> list[str]:
    if not origen:
        return []
    e = html.escape
    lineas = [
        "",
        f"<b>Factura origen (leída de {e(origen.get('fuente', ''))}):</b>",
        f"Emisor {e(origen.get('rfc_emisor', ''))} · Método {e(origen.get('metodo_pago', ''))} · "
        f"Moneda {e(origen.get('moneda', ''))}",
        f"Total: ${Decimal(str(origen.get('total', 0))):,.2f}",
    ]
    for b in origen.get("bases_impuestos", []):
        tipo = f"Ret. {b['type']}" if b["withholding"] else b["type"]
        lineas.append(f"• {e(tipo)} {e(str(b['rate']))} sobre base ${Decimal(str(b['base'])):,.2f}")
    return lineas


def _detalle_para_revision(
    envelope: PendingPayload, vista_previa: Optional[fiscal_engine.FacturaCalculada]
) -> str:
    """Detalle completo de la solicitud para el mensaje de revisión de ANB (HTML)."""
    e = html.escape
    if envelope.tipo == "rep":
        rep = envelope.rep_draft
        r = rep.receptor
        return "\n".join([
            f"<b>Receptor:</b> {e(r.razon_social)}",
            f"RFC {e(r.rfc)} · Régimen {e(r.regimen_fiscal)} · CP {e(r.cp_fiscal)}",
            f"<b>Factura origen:</b> {e(rep.uuid_factura_origen)}",
            f"<b>Fecha de pago:</b> {e(rep.fecha_pago)} · Forma de pago {e(rep.forma_pago)}",
            f"<b>Monto pagado:</b> ${rep.monto_pagado:,.2f}",
            *_detalle_factura_origen(envelope.factura_origen),
        ])

    draft = envelope.invoice_draft
    r = draft.receptor
    f = draft.factura
    lineas = [
        f"<b>Receptor:</b> {e(r.razon_social)}",
        f"RFC {e(r.rfc)} · Régimen {e(r.regimen_fiscal)} · CP {e(r.cp_fiscal)} · Uso {e(r.uso_cfdi)}",
        f"<b>Método/forma de pago:</b> {e(f.metodo_pago)} / {e(f.forma_pago)}",
        "",
        f"<b>Conceptos ({len(f.conceptos)}):</b>",
    ]
    for c in f.conceptos[:MAX_CONCEPTOS_EN_REVISION]:
        clave = c.clave_prod_serv
        if clave == tools.CLAVE_NUEVA:
            clave = f"NUEVA → {c.clave_prod_serv_propuesta}"
        importe = Decimal(str(c.cantidad)) * Decimal(str(c.precio_unitario))
        lineas.append(
            f"• {e(c.descripcion)}\n"
            f"   {c.cantidad:g} {e(c.clave_unidad)} × ${c.precio_unitario:,.2f} = ${importe:,.2f} · Clave {e(clave)}"
        )
    if len(f.conceptos) > MAX_CONCEPTOS_EN_REVISION:
        lineas.append(f"… y {len(f.conceptos) - MAX_CONCEPTOS_EN_REVISION} conceptos más")
    lineas.append("")

    if vista_previa is not None:
        v = vista_previa
        lineas.append(f"Subtotal: ${v.subtotal:,.2f}")
        if v.ieps > 0:
            lineas.append(f"IEPS: ${v.ieps:,.2f}")
        lineas.append(f"IVA: ${v.iva:,.2f}")
        if v.retencion_iva > 0:
            lineas.append(f"Retención IVA: -${v.retencion_iva:,.2f}")
        if v.retencion_isr > 0:
            lineas.append(f"Retención ISR: -${v.retencion_isr:,.2f}")
        lineas.append(f"<b>Total: ${v.total:,.2f}</b>")
    else:
        subtotal = sum(
            (Decimal(str(c.cantidad)) * Decimal(str(c.precio_unitario)) for c in f.conceptos),
            Decimal("0"),
        )
        lineas.append(f"Subtotal: ${subtotal:,.2f} (los impuestos se calculan al aprobar)")
    if f.total_documento_fuente is not None:
        lineas.append(f"Total en el documento fuente: ${f.total_documento_fuente:,.2f}")
    if f.observaciones:
        lineas.append(f"Observaciones: {e(f.observaciones)}")
    return "\n".join(lineas)


async def _escalar_a_anb(
    invoice_id: str, chat_id: str, message_id: int, client_profile, envelope: PendingPayload,
    vista_previa: Optional[fiscal_engine.FacturaCalculada] = None,
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
        f"Cliente: {html.escape(client_profile.nombre_comercial)}\n"
        f"ID: {invoice_id}\n"
        f"Motivo: [{envelope.escalation_reason}] {html.escape(envelope.escalation_detail)}\n\n"
        f"{_detalle_para_revision(envelope, vista_previa)}\n\n"
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

# XML recibidos sin un REP esperándolos (el cliente mandó el XML antes de
# pedir el REP). Solo en memoria: si el proceso se reinicia, el bot
# simplemente vuelve a pedir el XML — no se guarda en Supabase.
XML_RECIENTE_TTL_SEG = 24 * 3600
_xml_recientes: dict[tuple[str, str], tuple[float, dict]] = {}


def _guardar_xml_reciente(chat_id: str, origen: dict) -> None:
    _xml_recientes[(chat_id, origen["uuid"])] = (time.monotonic(), origen)


def _xml_reciente(chat_id: str, uuid_origen: str) -> Optional[dict]:
    guardado = _xml_recientes.get((chat_id, uuid_origen.upper()))
    if not guardado or time.monotonic() - guardado[0] > XML_RECIENTE_TTL_SEG:
        return None
    return guardado[1]


def _es_externa(origen: dict) -> bool:
    return origen.get("fuente") in ("xml", "pdf")


def _saldo_anterior(origen: dict, previous_reps: list[dict]) -> Decimal:
    """Último saldo insoluto timbrado por el bot; si no hay, el saldo que fijó
    ANB por pagos hechos en otro programa; si tampoco, el total de la factura."""
    if previous_reps:
        return Decimal(str(previous_reps[-1].get("imp_saldo_insoluto") or 0))
    return Decimal(str(origen.get("saldo_inicial") or origen.get("total", 0)))


async def _num_parcialidad(origen: dict, previous_reps: list[dict], facturapi_key: str) -> int:
    """
    Número de esta parcialidad. Para facturas externas pueden existir pagos
    timbrados en el otro programa, así que no basta contar los nuestros: se
    toma la parcialidad del último REP del bot tal como quedó en FacturAPI
    (fuente de verdad, sin guardar nada extra) o, si es el primero del bot,
    las previas que fijó ANB. Los errores de red de FacturAPI se propagan.
    """
    if not _es_externa(origen):
        return len(previous_reps) + 1
    if not previous_reps:
        return int(origen.get("parcialidades_previas", 0)) + 1
    ultimo = await get_invoice(previous_reps[-1]["folio_fiscal"], facturapi_key)
    docto = ultimo["complements"][0]["data"][0]["related_documents"][0]
    return int(docto["installment"]) + 1


def _receptor_de_origen(origen: dict, receptor_draft: ReceptorData) -> ReceptorData:
    """El receptor de un REP debe ser el de la factura que liquida: para
    facturas externas manda lo que dice el CFDI, no lo que juntó Claude."""
    return ReceptorData(
        razon_social=origen.get("nombre_receptor") or receptor_draft.razon_social,
        rfc=origen.get("rfc_receptor") or receptor_draft.rfc,
        regimen_fiscal=origen.get("regimen_receptor") or receptor_draft.regimen_fiscal,
        cp_fiscal=origen.get("cp_receptor") or receptor_draft.cp_fiscal,
        uso_cfdi="CP01",
    )


async def _pedir_xml_factura_origen(
    invoice_id: str, draft: RepDraft, client_profile, chat_id: str, message_id: int
) -> None:
    """La factura no está en FacturAPI: se hizo en otro programa. Se deja el
    REP esperando su XML (ver _procesar_xml_factura_origen)."""
    await asyncio.to_thread(
        sheets_client.save_pending,
        invoice_id=invoice_id,
        canal="telegram",
        canal_id=chat_id,
        telegram_message_id=message_id,
        invoice_json=draft.model_dump_json(),
        motivo_revision="esperando XML de factura origen externa",
        tipo_aprobacion="esperando_xml_origen",
        canal_id_aprobador=chat_id,
    )
    await telegram_client.send_message(
        chat_id,
        f"La factura {html.escape(draft.uuid_factura_origen)} no se hizo en este sistema, así que "
        "para hacer su complemento de pago necesito el <b>XML</b> de esa factura. "
        "Mándamelo por aquí como archivo. 📎\n\n"
        "Si solo tienes el PDF, mándalo y el despacho revisará los datos antes de timbrar."
    )
    await telegram_client.send_message(
        ALEJANDRO_CHAT_ID,
        f"ℹ️ REP de factura externa: pedí el XML a {html.escape(client_profile.nombre_comercial)}\n"
        f"UUID: {html.escape(draft.uuid_factura_origen)}\nMonto: ${draft.monto_pagado:,.2f}"
    )


async def _rechazar_factura_origen(
    client_profile, chat_id: str, origen: dict, problemas: list[str], monto_pagado
) -> None:
    detalle = "\n".join(f"• {html.escape(p)}" for p in problemas)
    await telegram_client.send_message(
        chat_id,
        "No puedo hacer este complemento de pago automáticamente:\n"
        f"{detalle}\n\nEl despacho ya fue notificado y te contactará."
    )
    await telegram_client.send_message(
        ALEJANDRO_CHAT_ID,
        f"⚠️ REP de factura externa no procesable — {html.escape(client_profile.nombre_comercial)}\n"
        f"UUID: {html.escape(origen.get('uuid', ''))} (fuente: {origen.get('fuente')})\n"
        f"Monto pagado: ${Decimal(str(monto_pagado)):,.2f}\n{detalle}\n\n"
        "Si procede, timbra el REP a mano en FacturAPI."
    )


async def _procesar_xml_factura_origen(
    client_profile, chat_id: str, message_id: int, xml_bytes: bytes
) -> Optional[str]:
    """
    XML de una factura emitida en otro programa. Si había un REP esperándolo
    (ver _pedir_xml_factura_origen), lo retoma y devuelve None. Si no,
    devuelve una nota para que Claude continúe la conversación (el cliente
    pudo mandar el XML antes de pedir el REP). None también cuando el
    archivo no sirve — ya se le avisó al cliente.
    """
    try:
        origen = cfdi_xml.parse_cfdi_xml(xml_bytes)
    except cfdi_xml.CfdiXmlError as exc:
        await telegram_client.send_message(
            chat_id,
            f"No pude leer ese archivo como factura: {html.escape(str(exc))}\n"
            "¿Me mandas el XML timbrado de la factura?"
        )
        return None
    if origen["rfc_emisor"] != client_profile.rfc.upper():
        await telegram_client.send_message(
            chat_id,
            f"Ese XML es de una factura emitida por {html.escape(origen['rfc_emisor'])}, no por "
            f"{html.escape(client_profile.rfc)}. Solo puedo hacer complementos de pago de tus propias facturas."
        )
        return None

    esperando = await asyncio.to_thread(sheets_client.get_pending_esperando_xml, chat_id)
    if esperando:
        draft = RepDraft.model_validate_json(esperando["invoice_json"])
        if draft.uuid_factura_origen.upper() == origen["uuid"]:
            await asyncio.to_thread(sheets_client.update_pending_status, esperando["id"], "xml_recibido")
            await telegram_client.send_message(chat_id, "Recibí el XML de tu factura. ✅ Preparo el complemento de pago...")
            await _calcular_y_timbrar_rep(
                str(uuid.uuid4()), draft, client_profile, chat_id, message_id, origen_externo=origen
            )
            return None

    _guardar_xml_reciente(chat_id, origen)
    nota = (
        f"[Sistema: el cliente envió el XML de su factura {origen['uuid']}"
        f"{' (folio ' + origen['serie_folio'] + ')' if origen['serie_folio'] else ''}, emitida a "
        f"{origen['nombre_receptor']} (RFC {origen['rfc_receptor']}), total ${Decimal(origen['total']):,.2f}, "
        f"método {origen['metodo_pago']}. El sistema ya tiene sus datos: se le puede hacer complemento "
        "de pago con generate_rep_draft usando ese UUID."
    )
    if esperando:
        nota += (
            " Ojo: hay un complemento de pago esperando el XML de OTRA factura ("
            f"{RepDraft.model_validate_json(esperando['invoice_json']).uuid_factura_origen})."
        )
    return nota + "]"


async def _procesar_pdf_factura_origen(
    client_profile, chat_id: str, message_id: int, pdf_bytes: bytes, esperando: dict
) -> bool:
    """
    Hay un REP esperando el XML y el cliente mandó un PDF. Si es esa factura,
    Claude lee sus datos y el REP va SIEMPRE a revisión de ANB (un PDF no
    da la certeza del XML). Devuelve False si el PDF no es esa factura (ej.
    una cotización) para que siga la conversación normal.
    """
    draft = RepDraft.model_validate_json(esperando["invoice_json"])
    try:
        origen = await asyncio.to_thread(extraer_factura_origen_de_pdf, pdf_bytes)
    except Exception:
        logger.exception("No se pudo leer el PDF de factura origen para %s", chat_id)
        return False
    if origen["uuid"] != draft.uuid_factura_origen.upper():
        return False

    await asyncio.to_thread(sheets_client.update_pending_status, esperando["id"], "pdf_recibido")
    problemas = cfdi_xml.problemas_para_rep(origen, client_profile.rfc)
    if problemas:
        await _rechazar_factura_origen(client_profile, chat_id, origen, problemas, draft.monto_pagado)
        return True

    draft = draft.model_copy(update={"receptor": _receptor_de_origen(origen, draft.receptor)})
    envelope = PendingPayload(
        tipo="rep",
        escalation_reason=EscalationReason.FACTURA_ORIGEN_PDF.value,
        escalation_detail=(
            "factura hecha en otro programa; el cliente solo mandó el PDF y Claude leyó los datos. "
            "Verifica total e impuestos contra el PDF (va abajo) antes de aprobar."
        ),
        rep_draft=draft,
        factura_origen=origen,
    )
    await _escalar_a_anb(str(uuid.uuid4()), chat_id, message_id, client_profile, envelope)
    await telegram_client.send_document(ALEJANDRO_CHAT_ID, pdf_bytes, "factura_origen.pdf")
    return True


async def _calcular_y_timbrar_rep(
    invoice_id: str, draft: RepDraft, client_profile, chat_id: str, message_id: int,
    origen_externo: Optional[dict] = None,
) -> None:
    """
    Busca la factura original + REPs previos, calcula el saldo insoluto con
    fiscal_engine (fresco, justo antes de timbrar — evita depender de un
    cálculo viejo si llegaron más pagos mientras tanto), y timbra o escala.

    origen_externo: factura hecha en otro programa, leída de su XML/PDF
    (cfdi_xml.py). Sin ella, si FacturAPI no conoce el UUID se pide el XML
    en vez de rendirse (ver _pedir_xml_factura_origen).
    """
    original_invoice = origen_externo or _xml_reciente(chat_id, draft.uuid_factura_origen)
    if original_invoice is None:
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
        await _pedir_xml_factura_origen(invoice_id, draft, client_profile, chat_id, message_id)
        return

    externa = _es_externa(original_invoice)
    if externa:
        problemas = cfdi_xml.problemas_para_rep(original_invoice, client_profile.rfc)
        if problemas:
            await _rechazar_factura_origen(client_profile, chat_id, original_invoice, problemas, draft.monto_pagado)
            return
        draft = draft.model_copy(update={"receptor": _receptor_de_origen(original_invoice, draft.receptor)})

    previous_reps = await asyncio.to_thread(sheets_client.get_rep_history, draft.uuid_factura_origen)
    imp_saldo_ant = _saldo_anterior(original_invoice, previous_reps)

    resultado = fiscal_engine.calcular_rep(
        monto_pagado=Decimal(str(draft.monto_pagado)), imp_saldo_ant=imp_saldo_ant
    )

    if resultado.escalation:
        envelope = PendingPayload(
            tipo="rep",
            escalation_reason=resultado.escalation.reason.value,
            escalation_detail=resultado.escalation.detail,
            rep_draft=draft,
            # sin esto, al aprobar ANB se volvería a pedir el XML
            factura_origen=original_invoice if externa else None,
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
        factura_origen_externa=original_invoice if externa else None,
    )
    # Factura de otro programa sin pagos nuestros ni saldo fijado por ANB:
    # se asumió primer pago (saldo = total). El cliente puede corregirlo.
    supuso_primer_pago = (
        externa and not previous_reps and not original_invoice.get("saldo_confirmado", False)
    )
    await _solicitar_confirmacion_cliente_rep(
        invoice_id, rep_data, client_profile, chat_id, message_id,
        ofrecer_pagos_previos=supuso_primer_pago,
    )


async def _solicitar_confirmacion_cliente_rep(
    invoice_id: str, rep_data: RepData, client_profile, chat_id: str, message_id: int,
    ofrecer_pagos_previos: bool = False,
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
        f"Cliente: {html.escape(rep_data.receptor.razon_social)} ({rep_data.receptor.rfc})",
        f"Monto pagado: ${rep_data.monto_pagado:,.2f}",
        f"Fecha de pago: {rep_data.fecha_pago}",
        f"Saldo anterior: ${rep_data.imp_saldo_ant:,.2f}",
        f"Saldo insoluto después de este pago: ${rep_data.imp_saldo_insoluto:,.2f}",
    ]
    teclado = [[
        {"text": "✅ Sí, confirmar", "callback_data": f"cliente_si:{invoice_id}"},
        {"text": "❌ No, corregir", "callback_data": f"cliente_no:{invoice_id}"},
    ]]
    if ofrecer_pagos_previos:
        lineas += [
            "",
            "ℹ️ Tomé que este es el <b>primer pago</b> de esa factura. Si ya habías hecho "
            "complementos de pago de ella en otro sistema, avísame con el botón de abajo.",
        ]
        teclado.append([{"text": "🔁 Ya hubo pagos antes", "callback_data": f"cliente_previos:{invoice_id}"}])
    lineas += ["", "¿Confirmas estos datos para timbrar el complemento de pago?"]
    await telegram_client.send_message(
        chat_id, "\n".join(lineas), reply_markup={"inline_keyboard": teclado},
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

async def _execute_approval(
    command: str, invoice_id: str, saldo: Optional[Decimal] = None, parcialidad: Optional[int] = None,
) -> str:
    """
    Ejecuta 'aprobar' o 'rechazar' de ANB sobre un escalamiento
    (tipo_aprobacion='anb_revision'). 'aprobar' NUNCA timbra directo: para
    facturas, vuelve a calcular y manda al CLIENTE la confirmación final
    (la aprobación de ANB resuelve la inconsistencia, no reemplaza la
    confirmación del cliente). Para REP, sí puede timbrar directo si el
    recálculo ya no escala (no hay confirmación de cliente para REP).

    saldo/parcialidad: solo para REP de factura externa con pagos previos en
    otro programa (PAGOS_PREVIOS_EXTERNOS) — /aprobar {id} saldo=... parcialidad=...
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

    origen_externo = dict(envelope.factura_origen) if es_rep and envelope.factura_origen else None
    if es_rep and envelope.escalation_reason == EscalationReason.PAGOS_PREVIOS_EXTERNOS.value:
        if saldo is None or parcialidad is None or saldo <= 0 or parcialidad < 1 or not origen_externo:
            msg = (
                "Para este REP necesito el saldo antes de este pago y el número de parcialidad:\n"
                f"/aprobar {invoice_id} saldo=12345.67 parcialidad=2"
            )
            await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
            return msg
        origen_externo.update(
            saldo_inicial=str(saldo), parcialidades_previas=parcialidad - 1, saldo_confirmado=True
        )

    await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "aprobado")
    nuevo_invoice_id = str(uuid.uuid4())

    if es_rep:
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, f"⏳ Reintentando REP {invoice_id}...")
        await _calcular_y_timbrar_rep(
            nuevo_invoice_id, envelope.rep_draft, client_profile, client_canal_id, 0,
            origen_externo=origen_externo,
        )
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
    try:
        invoice_data = await _calcular_y_procesar_factura(
            nuevo_invoice_id, draft, client_profile, client_canal_id, 0,
            omitir_validacion_cruzada=omitir_validacion,
        )
    except Exception as exc:
        logger.exception("Error recalculando %s tras aprobación", invoice_id)
        msg = f"🔴 No se envió a {client_profile.nombre_comercial}: error al recalcular {invoice_id}: {exc}"
        await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
        return msg

    if invoice_data is None:
        # Se volvió a escalar o faltaron reglas fiscales: el aviso con el
        # motivo ya se mandó desde _calcular_y_procesar_factura.
        msg = f"⚠️ No se envió a {client_profile.nombre_comercial} (ver mensaje anterior)."
    else:
        msg = (
            f"✅ Enviada a {client_profile.nombre_comercial} para confirmación.\n"
            f"Total: ${invoice_data.factura.total_estimado:,.2f}"
        )
    await telegram_client.send_message(ALEJANDRO_CHAT_ID, msg)
    return msg


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
    opciones = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
    try:
        saldo = Decimal(opciones["saldo"].replace(",", "").lstrip("$")) if "saldo" in opciones else None
        parcialidad = int(opciones["parcialidad"]) if "parcialidad" in opciones else None
    except (ArithmeticError, ValueError):
        await telegram_client.send_message(
            ALEJANDRO_CHAT_ID, "Formato: /aprobar {id} saldo=12345.67 parcialidad=2"
        )
        return
    await _execute_approval(command, invoice_id, saldo, parcialidad)


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

    message_id = int(pending.get("telegram_message_id") or 0)

    if command == "cliente_previos":
        # Factura de otro programa con pagos ya timbrados allá: el bot no
        # puede saber el saldo real, lo fija ANB después de hablar con el cliente.
        if not es_rep:
            return
        await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "pagos_previos")
        nuevo_id = str(uuid.uuid4())
        draft = RepDraft(
            estatus="confirmado_por_cliente",
            uuid_factura_origen=rep_data.uuid_factura_origen,
            receptor=rep_data.receptor,
            fecha_pago=rep_data.fecha_pago,
            forma_pago=rep_data.forma_pago,
            monto_pagado=float(rep_data.monto_pagado),
        )
        envelope = PendingPayload(
            tipo="rep",
            escalation_reason=EscalationReason.PAGOS_PREVIOS_EXTERNOS.value,
            escalation_detail=(
                "el cliente dice que ya hubo complementos de pago de esta factura en otro sistema. "
                "Confirma con él el saldo antes de este pago y qué número de parcialidad es, y aprueba con "
                f"/aprobar {nuevo_id} saldo=SALDO_ANTERIOR parcialidad=N"
            ),
            rep_draft=draft,
            factura_origen=rep_data.factura_origen_externa,
        )
        await _escalar_a_anb(nuevo_id, chat_id, message_id, client_profile, envelope)
        return

    await asyncio.to_thread(sheets_client.update_pending_status, pending["id"], "aprobado")

    if es_rep:
        facturapi_key = client_profile.facturapi_key if client_profile else ""
        # Recalcular num_parcialidad y buscar la factura original FRESCOS,
        # justo antes de timbrar — el cliente pudo tardar en confirmar y
        # otro pago pudo haberse timbrado mientras tanto.
        try:
            original_invoice = rep_data.factura_origen_externa or await search_invoice_by_uuid(
                rep_data.uuid_factura_origen, facturapi_key
            )
            previous_reps = await asyncio.to_thread(sheets_client.get_rep_history, rep_data.uuid_factura_origen)
            num_parcialidad = (
                await _num_parcialidad(original_invoice, previous_reps, facturapi_key)
                if original_invoice else 0
            )
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

    if command in ("cliente_si", "cliente_no", "cliente_previos"):
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

        if tipo_aprobacion == "esperando_xml_origen":
            await telegram_client.send_message(
                ALEJANDRO_CHAT_ID,
                f"ℹ️ El cliente (canal_id: {canal_id}) no ha mandado en más de 24h el XML de la "
                f"factura externa para su REP ({invoice_id}). Puedes darle seguimiento manual."
            )
            continue

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
