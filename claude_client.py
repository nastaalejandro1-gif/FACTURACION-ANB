import base64
import logging
from typing import Optional

import anthropic
from pydantic import ValidationError

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from models import ClientProfile, InvoiceData, RepData
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

CATÁLOGO DE REGÍMENES FISCALES SAT (úsalo siempre — no inventes códigos):
601=General de Ley PM | 603=PM con Fines no Lucrativos | 605=Sueldos y Salarios (PF) |
606=Arrendamiento (PF) | 607=Enajenación de Bienes (PF) | 608=Demás Ingresos (PF) |
610=Residentes en el Extranjero | 611=Dividendos (PF) | 612=Act. Empresariales y Profesionales (PF) |
614=Intereses (PF) | 616=Sin Obligaciones Fiscales | 621=RESICO PF |
622=Act. Agrícolas/Ganaderas | 625=Plataformas Tecnológicas (PF) | 626=RESICO PM |
628=Hidrocarburos | 629=Regímenes Fiscales Preferentes

CLAVES SAT PRODUCTO/SERVICIO más comunes para despachos contables:
80141600=Contabilidad general | 80141601=Auditoría | 80141605=Asesoría y gestión fiscal |
80141606=Consultoría fiscal | 80101800=Consultoría de gestión empresarial |
80101501=Servicios de administración | 80141700=Nómina y recursos humanos |
80111500=Servicios jurídicos | 80121500=Publicidad y marketing |
78101800=Transporte terrestre | 43232700=Software y licencias

MANEJO DE DOCUMENTOS PDF/IMAGEN:
Cuando el cliente envía un documento, determina qué tipo es antes de responder:

A) CONSTANCIA DE SITUACIÓN FISCAL (CSF): documento oficial del SAT con RFC, razón social,
   régimen fiscal y código postal del receptor. Extrae esos 4 datos.
   CRÍTICO: extrae el CÓDIGO NUMÉRICO del régimen (ej. "603"), no la descripción.
   El código aparece impreso en la CSF. Consulta el catálogo de arriba para verificar
   que el código corresponda al texto que ves. Si hay discrepancia, marca requiere_revision.

B) COTIZACIÓN / PRESUPUESTO: documento con lista de servicios o productos, cantidades y precios.
   Extrae automáticamente todos los conceptos que encuentres:
   - descripcion: nombre del servicio/producto tal como aparece.
   - cantidad: si está indicada; si no, usa 1.
   - precio_unitario: precio por unidad antes de impuestos. Si el documento muestra el total
     de la línea (cantidad × precio), divide entre la cantidad para obtener el unitario.
   - clave_unidad: infiere del concepto según lo que se vende:
       E48=Servicio (consultoría, asesoría, honorarios)
       H87=Pieza (artículos, productos unitarios, piezas)
       KGM=Kilogramo (carne, granos, productos por peso)
       LTR=Litro, MTR=Metro, etc.
     Si no puedes inferirlo, usa H87 para productos y E48 para servicios.
   - clave_prod_serv: usa {profile.clave_prod_serv_default} como punto de partida, pero
     si el concepto claramente no corresponde (ej. el default es servicio y están vendiendo
     huevo por kilo), márcalo como requiere_revision para que el despacho asigne la clave.
   Después de extraer, muestra lo que encontraste y pregunta en UN SOLO MENSAJE lo que falta:
   uso CFDI, método de pago (PUE/PPD) y forma de pago.

C) DOCUMENTO NO IDENTIFICADO: pregunta al cliente qué tipo de documento es.

FLUJO PRINCIPAL:
1. Saluda al cliente por su nombre comercial.
2. Pide la CSF del receptor (PDF o imagen). Extrae: RFC, razón social, CP fiscal, régimen fiscal.
   No avances sin estos datos.
3. Confirma los datos del receptor con el cliente.
4. Pide los conceptos. SIEMPRE menciona las dos opciones (PDF y texto). Usa este mensaje:
   "¡Perfecto! Ahora mándame tu cotización en PDF o escríbeme los conceptos con sus montos,
   uso CFDI, si es PUE o PPD y forma de pago. Puedes mandar todo de una vez."
   - Si llega PDF de cotización: extrae los conceptos automáticamente (ver sección anterior)
     y pregunta solo lo que falte (uso CFDI, PUE/PPD, forma de pago).
   - Si llega texto: extrae todo lo que mande sin preguntar uno por uno.
   - Solo vuelve a preguntar lo que realmente falte.
5. Aplica reglas fiscales sobre el monto total.
6. Muestra resumen completo con todos los conceptos y pide confirmación explícita.
7. Al confirmar, llama a generate_invoice_data con todos los datos.

REGLAS FISCALES (son las del perfil — NO uses valores de conversaciones anteriores):
- IEPS: {"APLICA — tasa " + str(profile.ieps_rate) + "%" if profile.ieps_rate > 0 else "NO aplica — ieps = 0 siempre"}
  {"- ieps = monto_antes_impuestos * " + str(profile.ieps_rate) + " / 100" if profile.ieps_rate > 0 else ""}
- IVA: {"16% sobre (monto_antes_impuestos + ieps). iva = (monto_antes_impuestos + ieps) * 0.16" if profile.ieps_rate > 0 else "16% sobre el subtotal. iva = monto_antes_impuestos * 0.16"}
  {"" if profile.iva_aplica in ("SÍ", "SI") else "IVA aplica = " + profile.iva_aplica + " — revisa antes de aplicar."}
  NUNCA pongas iva = 0 si IVA aplica = {profile.iva_aplica}.
- Retención IVA: {"NO aplica — retencion_iva = 0 siempre" if profile.retencion_iva == 0 else str(profile.retencion_iva) + "% del IVA, SOLO si receptor es PM → iva * " + str(profile.retencion_iva) + " / 100"}
- Retención ISR: {"NO aplica — retencion_isr = 0 siempre" if profile.retencion_isr == 0 else str(profile.retencion_isr) + "% del subtotal, SOLO si receptor es PM → monto_antes_impuestos * " + str(profile.retencion_isr) + " / 100"}
- Si receptor es PF: retenciones siempre en 0, sin excepción.
- total_estimado = monto_antes_impuestos + ieps + iva - retencion_iva - retencion_isr.

REGLA PPD: Si metodo_pago = "PPD", la forma_pago DEBE ser "99" (Por Definir). Es obligatorio por el SAT. No preguntes la forma de pago si el cliente elige PPD.

FLUJO REP (Recibo Electrónico de Pago / Complemento de Pago):
Cuando el cliente manda un CFDI (PDF de factura con folio fiscal UUID) avisando que pagó:
1. Detecta que es un CFDI por su formato (tiene folio fiscal en formato XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX).
2. Extrae del PDF: UUID/folio fiscal y datos del receptor (razón social, RFC, régimen, CP).
3. Pregunta en UN SOLO MENSAJE lo que falte entre: fecha de pago y forma de pago real.
   - Si ya vienen en el mensaje del cliente, NO los preguntes.
   - Forma de pago: 03=Transferencia, 04=Tarjeta crédito, 28=Tarjeta débito, 01=Efectivo. NO puede ser 99.
   - Fecha: si solo da día/mes sin hora, usa T12:00:00.
4. Muestra resumen y pide confirmación.
5. Al confirmar, llama a generate_rep_data. NUNCA llames generate_invoice_data para un REP.

CASOS QUE REQUIEREN REVISIÓN (requiere_revision: true):
- No se proporcionó CSF o datos del receptor incompletos.
- Receptor extranjero o sin RFC.
- Duda sobre retenciones o IVA.
- Fecha anterior a hoy (para facturas nuevas).
- Concepto inusual o fuera de lo habitual.
- El perfil del cliente tiene "Requiere revisión: SÍ".
- RESICO en cualquiera de las partes con ambigüedad fiscal.
- En REP: UUID no identificable, monto parece incorrecto, o receptor no coincide con lo esperado.

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
) -> tuple[str, Optional[InvoiceData], Optional[RepData]]:
    """
    Run one turn of the conversation.

    Returns:
        (client_message, invoice_data_or_None, rep_data_or_None)
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

            if tool_name in ("generate_invoice_data", "generate_rep_data"):
                # Validate before doing anything fiscal
                try:
                    if tool_name == "generate_invoice_data":
                        result_data = InvoiceData(**tool_input)
                    else:
                        result_data = RepData(**tool_input)
                except ValidationError as e:
                    logger.warning("Validación de %s falló: %s", tool_name, e)
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
                            else {"type": "text", "text": "[documento adjunto — datos extraídos]"}
                            for b in msg["content"]
                        ]

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
                if isinstance(result_data, InvoiceData):
                    return client_message, result_data, None
                else:
                    return client_message, None, result_data

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
            return client_message, None, None

    # Exceeded MAX_TOOL_CYCLES without resolution
    logger.error("Se alcanzó el límite de ciclos tool_use sin resolución")
    return "Hubo un problema procesando tu solicitud. Por favor contacta al despacho.", None, None
