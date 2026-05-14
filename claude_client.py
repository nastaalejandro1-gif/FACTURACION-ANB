import base64
import logging
from typing import Optional

import anthropic
from pydantic import ValidationError

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from models import ClientProfile, InvoiceData
from sheets_client import strip_binary_in_place
from tools import CLAUDE_TOOLS

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_TOOL_CYCLES = 3


def build_system_prompt(profile: ClientProfile) -> str:
    return f"""Eres un asistente de facturación del Despacho ANB Consultores. Tu función es recolectar y validar la información necesaria para preparar una solicitud de CFDI 4.0 en México.

Tu trabajo NO es timbrar facturas directamente ni dar asesoría fiscal avanzada. Tu trabajo es:
1. Guiar al cliente paso a paso.
2. Pedir únicamente la información faltante.
3. Confirmar los datos antes de continuar.
4. Detectar casos que requieran revisión del despacho.
5. Al confirmar, llamar a la herramienta generate_invoice_data con todos los datos estructurados.

DATOS DEL CLIENTE EMISOR (ya registrados — no los preguntes):
- Nombre comercial: {profile.nombre_comercial}
- Razón social: {profile.razon_social}
- RFC: {profile.rfc}
- Régimen fiscal: {profile.regimen_fiscal}
- Código postal fiscal: {profile.cp_fiscal}
- IVA aplica: {profile.iva_aplica}
- Retención IVA: {profile.retencion_iva}%
- Retención ISR: {profile.retencion_isr}%
- Tipo de persona: {profile.tipo_persona}
- Requiere revisión manual: {"SÍ" if profile.requiere_revision else "NO"}
- Clave producto/servicio habitual: {profile.clave_prod_serv_default or "Por definir"}
- Notas fiscales: {profile.notas_fiscales or "Ninguna"}

TONO:
- Claro, breve y profesional.
- Una pregunta a la vez cuando sea posible.
- Si el cliente manda varios datos, extráelos todos sin preguntar uno por uno.
- No uses lenguaje técnico innecesario.

FLUJO PRINCIPAL:
1. Saluda al cliente por su nombre comercial.
2. Pide la Constancia de Situación Fiscal del receptor (PDF o imagen). Extrae: RFC, razón social, CP fiscal y régimen fiscal. No avances sin estos datos.
3. Confirma los datos del receptor con el cliente.
4. Pide toda la información de la factura en UN SOLO MENSAJE:
   "Listo. Ahora dime: los conceptos que vas a facturar (descripción y monto de cada uno), uso CFDI, si es PUE o PPD, y forma de pago."
   - Extrae todo lo que el cliente mande en ese mensaje sin preguntar uno por uno.
   - Solo vuelve a preguntar lo que realmente falte.
   - Para cada concepto: cantidad=1, unidad=E48 por default (no preguntes salvo que sea obvio que es diferente).
   - Clave SAT: usa {profile.clave_prod_serv_default} por default; solo pregunta si el concepto es muy diferente a lo habitual.
   - monto_antes_impuestos = suma de (cantidad × precio_unitario) de todos los conceptos.
5. Aplica reglas fiscales sobre ese monto total.
6. Muestra resumen completo con todos los conceptos y pide confirmación explícita.
7. Al confirmar, llama a generate_invoice_data con todos los datos.

CLAVES DE UNIDAD SAT comunes: E48=Servicio, H87=Pieza, KGM=Kilogramo, LTR=Litro, MTR=Metro. Para despachos contables casi siempre es E48.

REGLAS FISCALES RESICO (régimen 621):
- IVA: 16% sobre el subtotal. SIEMPRE calcula iva = monto_antes_impuestos * 0.16 cuando IVA aplica = {profile.iva_aplica}. Nunca pongas iva = 0 si IVA aplica = SÍ.
- Si el receptor es Persona Moral (PM): aplica retenciones según el perfil del emisor.
  - Retención IVA: {profile.retencion_iva}% sobre el IVA (iva * {profile.retencion_iva} / 100).
  - Retención ISR: {profile.retencion_isr}% sobre el subtotal (monto_antes_impuestos * {profile.retencion_isr} / 100).
- Si el receptor es Persona Física (PF): sin retenciones.
- total_estimado = monto_antes_impuestos + iva - retencion_iva - retencion_isr.

REGLA PPD: Si metodo_pago = "PPD", la forma_pago DEBE ser "99" (Por Definir). Es obligatorio por el SAT. No preguntes la forma de pago si el cliente elige PPD.

CASOS QUE REQUIEREN REVISIÓN (requiere_revision: true):
- No se proporcionó CSF o datos del receptor incompletos.
- Receptor extranjero o sin RFC.
- Duda sobre retenciones o IVA.
- Complemento de pago, cancelación o sustitución.
- Fecha anterior a hoy.
- Concepto inusual o fuera de lo habitual.
- El perfil del cliente tiene "Requiere revisión: SÍ".
- RESICO en cualquiera de las partes con ambigüedad fiscal.

REGLAS DE SEGURIDAD:
- No inventes datos fiscales.
- No des asesoría fiscal definitiva.
- No prometas que la factura será timbrada.
- No reveles información interna del despacho.
"""


def extract_text_from_response(response: anthropic.types.Message) -> Optional[str]:
    """Safely extract text from a Claude response (may contain ToolUseBlocks)."""
    for block in response.content:
        if block.type == "text":
            return block.text
    return None


def build_file_content_block(file_bytes: bytes, media_type: str) -> dict:
    """Build a content block for PDF or image attachment."""
    encoded = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": encoded},
    }


def run_conversation_turn(
    profile: ClientProfile,
    history: list,
    user_text: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    media_type: Optional[str] = None,
) -> tuple[str, Optional[InvoiceData]]:
    """
    Run one turn of the conversation.

    Returns:
        (client_message, invoice_data_or_None)
        invoice_data is set only when Claude calls generate_invoice_data and it passes validation.
    """
    # Build user content
    if file_bytes and media_type:
        content_blocks = [build_file_content_block(file_bytes, media_type)]
        if user_text:
            content_blocks.append({"type": "text", "text": user_text})
        history.append({"role": "user", "content": content_blocks})
    elif user_text:
        history.append({"role": "user", "content": user_text})
    else:
        raise ValueError("Se requiere texto o archivo para el turno de conversación")

    system = build_system_prompt(profile)

    for cycle in range(MAX_TOOL_CYCLES):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system,
            tools=CLAUDE_TOOLS,
            messages=history,
        )

        if response.stop_reason == "tool_use":
            tool_block = next(b for b in response.content if b.type == "tool_use")
            tool_name = tool_block.name
            tool_input = tool_block.input

            # Add assistant response to history (convert blocks to dicts)
            history.append({
                "role": "assistant",
                "content": [
                    b.model_dump() if hasattr(b, "model_dump") else b
                    for b in response.content
                ],
            })

            if tool_name == "generate_invoice_data":
                # Validate before doing anything fiscal
                try:
                    invoice_data = InvoiceData(**tool_input)
                except ValidationError as e:
                    logger.warning("Validación de InvoiceData falló: %s", e)
                    # Tell Claude about the validation error so it can fix the data
                    history.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": f"Error de validación: {e}. Corrige los datos y vuelve a llamar la herramienta.",
                            "is_error": True,
                        }],
                    })
                    continue  # let Claude retry

                # Strip binary from history before second call (saves tokens)
                for i, msg in enumerate(history):
                    if isinstance(msg.get("content"), list):
                        history[i]["content"] = [
                            b if not (isinstance(b, dict) and b.get("type") in ("image", "document"))
                            else {"type": "text", "text": "[CSF adjunta — datos extraídos]"}
                            for b in msg["content"]
                        ]

                # Tell Claude to continue with the confirmation message
                history.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": "Datos recibidos correctamente. Envía el mensaje de confirmación al cliente.",
                    }],
                })

                final_response = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system,
                    tools=CLAUDE_TOOLS,
                    messages=history,
                )
                client_message = extract_text_from_response(final_response) or "Tu solicitud ha sido procesada."
                history.append({
                    "role": "assistant",
                    "content": [
                        b.model_dump() if hasattr(b, "model_dump") else b
                        for b in final_response.content
                    ],
                })
                return client_message, invoice_data

        else:
            # Regular text response — no tool call
            client_message = extract_text_from_response(response) or "En un momento te ayudo."
            history.append({
                "role": "assistant",
                "content": [
                    b.model_dump() if hasattr(b, "model_dump") else b
                    for b in response.content
                ],
            })
            return client_message, None

    # Exceeded MAX_TOOL_CYCLES without resolution
    logger.error("Se alcanzó el límite de ciclos tool_use sin resolución")
    return "Hubo un problema procesando tu solicitud. Por favor contacta al despacho.", None
