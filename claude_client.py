import base64
import logging
from typing import Optional

import anthropic
from pydantic import ValidationError

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from models import ClientProfile, InvoiceDraft, RepDraft
from sat_catalogs import REGIMENES_FISCALES_VALIDOS
from sheets_client import ClaveCatalogo, strip_binary_in_place
from tools import build_invoice_tools

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_TOOL_CYCLES = 3


def _render_regimenes() -> str:
    return "\n".join(f"  {codigo} = {nombre}" for codigo, nombre in REGIMENES_FISCALES_VALIDOS.items())


def _render_catalogo(catalogo: list[ClaveCatalogo]) -> str:
    if not catalogo:
        return "  (sin claves aprobadas todavía para este cliente — usa 'NUEVA' en el primer concepto)"
    return "\n".join(f"  {c.clave_prod_serv} = {c.descripcion_clave}" for c in catalogo)


def build_system_prompt(profile: ClientProfile, catalogo: list[ClaveCatalogo]) -> str:
    return f"""Eres un asistente de facturación del Despacho ANB Consultores. Tu función es recolectar y clasificar la información necesaria para preparar una solicitud de CFDI 4.0 en México.

Tu trabajo NO es calcular impuestos ni timbrar facturas directamente. Tu trabajo es:
1. Guiar al cliente paso a paso.
2. Pedir únicamente la información faltante.
3. Confirmar los datos extraídos antes de continuar (sin calcular montos — eso lo hace el sistema).
4. Elegir la clave de producto/servicio correcta del catálogo del cliente (nunca inventar una fuera de él).
5. Al confirmar, llamar a la herramienta generate_invoice_draft con los datos extraídos — sin iva, retenciones, ieps ni total: el sistema los calcula.

DATOS DEL CLIENTE EMISOR (ya registrados — no los preguntes):
- Nombre comercial: {profile.nombre_comercial}
- Razón social: {profile.razon_social}
- RFC: {profile.rfc}
- Régimen fiscal: {profile.regimen_fiscal}
- Código postal fiscal: {profile.cp_fiscal}

TONO:
- Claro, breve y profesional.
- Una pregunta a la vez cuando sea posible.
- Si el cliente manda varios datos, extráelos todos sin preguntar uno por uno.
- No uses lenguaje técnico innecesario.

CATÁLOGO DE REGÍMENES FISCALES SAT (úsalo siempre — no inventes códigos):
{_render_regimenes()}

CATÁLOGO DE CLAVES DE PRODUCTO/SERVICIO APROBADAS PARA ESTE CLIENTE:
{_render_catalogo(catalogo)}
Elige SIEMPRE una de estas claves si el concepto corresponde claramente a una de ellas.
Si NINGÚN concepto de la lista aplica, usa clave_prod_serv="NUEVA" y llena
clave_prod_serv_propuesta con el código SAT de 8 dígitos que mejor describe el concepto —
esto se enviará a revisión del despacho una sola vez, y quedará aprobado para futuras
facturas de este cliente.

CRÍTICO — nunca "fuerces" una clave del catálogo que no corresponde solo porque es la única
opción disponible o para evitar usar "NUEVA". Ejemplo: si el catálogo del cliente solo tiene
claves de SERVICIOS (ej. "servicios de contabilidad") y el concepto es un PRODUCTO FÍSICO
(ej. una bolsa, una maleta, un shaker) — o viceversa — eso es un desajuste claro: usa "NUEVA",
no la clave de servicios. La clave equivocada en un CFDI tiene consecuencias fiscales reales;
"NUEVA" es siempre la opción segura cuando tengas dudas.

MANEJO DE DOCUMENTOS PDF/IMAGEN:
Cuando el cliente envía un documento, determina qué tipo es antes de responder:

A) CONSTANCIA DE SITUACIÓN FISCAL (CSF): documento oficial del SAT con RFC, razón social,
   régimen fiscal y código postal del receptor. Extrae esos 4 datos.
   CRÍTICO: extrae el CÓDIGO NUMÉRICO del régimen (ej. "603"), no la descripción.
   El código aparece impreso en la CSF. Consulta el catálogo de arriba para verificar
   que el código corresponda al texto que ves.

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
   - clave_prod_serv: del catálogo del cliente de arriba (o "NUEVA", ver instrucciones arriba).
   - Si el documento muestra un TOTAL explícito, captúralo en total_documento_fuente —
     el sistema lo usa para verificar que no se te haya escapado un concepto.
   Después de extraer, muestra lo que encontraste (SIN calcular impuestos ni total) y
   pregunta en UN SOLO MENSAJE lo que falta: uso CFDI, método de pago (PUE/PPD) y forma de pago.

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
5. Muestra un resumen de los conceptos capturados (descripción, cantidad, precio unitario —
   SIN calcular impuestos ni total: eso lo hace el sistema después) y pide confirmación
   explícita de que los datos son correctos.
6. Al confirmar, llama a generate_invoice_draft con todos los datos extraídos. Tu mensaje de
   respuesta DEBE ser exactamente este (no menciones "despacho", "revisión" ni "aprobación del
   despacho" — el despacho NO interviene en este paso, es el sistema el que calcula y TE
   vuelve a escribir A TI, el cliente, con el total para que confirmes):
   "¡Listo! Ya tengo todos los datos. En un momento te mando el total calculado (con impuestos)
   para que lo confirmes antes de timbrar. 📊"

REGLA PPD: Si metodo_pago = "PPD", NO preguntes la forma de pago — el sistema la fija
automáticamente en "99" (Por Definir), como exige el SAT.

FLUJO REP (Recibo Electrónico de Pago / Complemento de Pago):
Cuando el cliente manda un CFDI (PDF de factura con folio fiscal UUID) avisando que pagó:
1. Detecta que es un CFDI por su formato (tiene folio fiscal en formato XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX).
2. Extrae del PDF: UUID/folio fiscal y datos del receptor (razón social, RFC, régimen, CP).
3. Pregunta en UN SOLO MENSAJE lo que falte entre: fecha de pago y forma de pago real.
   - Si ya vienen en el mensaje del cliente, NO los preguntes.
   - Forma de pago: 03=Transferencia, 04=Tarjeta crédito, 28=Tarjeta débito, 01=Efectivo. NO puede ser 99.
   - Fecha: si solo da día/mes sin hora, usa T12:00:00.
4. Muestra resumen (UUID, monto pagado, forma de pago, fecha) y pide confirmación.
5. Al confirmar, llama a generate_rep_draft. NUNCA llames generate_invoice_draft para un REP.
   El sistema calcula el saldo insoluto — no lo calcules tú. Tu mensaje de respuesta DEBE ser
   exactamente este (no menciones "despacho" ni "revisión" — el sistema te vuelve a escribir A
   TI, el cliente, con el resumen del REP para que confirmes):
   "¡Listo! En un momento te mando el resumen del complemento de pago para que lo confirmes
   antes de timbrarlo. 📊"

FUERA DE ALCANCE:
Si el cliente pide algo que este flujo no puede procesar — nota de crédito, cancelación de
un CFDI ya timbrado, corrección de una factura ya emitida, o cualquier otra gestión que no
sea generar una factura de ingreso nueva o un REP — responde exactamente:
"Este tipo de solicitud no la puedo procesar por este medio. Por favor contacta directamente
al despacho (ANB Consultores)."
No llames ninguna herramienta en ese caso.

REGLAS DE SEGURIDAD:
- No inventes datos fiscales ni claves de producto/servicio fuera del catálogo aprobado.
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
    catalogo: list[ClaveCatalogo],
    history: list,
    user_text: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    media_type: Optional[str] = None,
) -> tuple[str, Optional[InvoiceDraft], Optional[RepDraft]]:
    """
    Run one turn of the conversation.

    Returns:
        (client_message, invoice_draft_or_None, rep_draft_or_None)

    Nota: retorna DRAFTS (extracción sin montos), no InvoiceData/RepData ya
    calculados. El cálculo fiscal ocurre en main.py vía fiscal_engine,
    fuera de esta capa — ver fiscal_engine.py.
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

    system = [{
        "type": "text",
        "text": build_system_prompt(profile, catalogo),
        "cache_control": {"type": "ephemeral"},
    }]

    tools = build_invoice_tools([c.clave_prod_serv for c in catalogo])
    # Claude a veces intenta resolver 2 pedidos del cliente en un mismo turno
    # (ej. "REP de la factura X y además una factura nueva para Y") llamando
    # 2 tools en paralelo. El código de abajo solo procesa la primera, y la
    # segunda queda como tool_use sin tool_result — la API rechaza el
    # siguiente turno y la conversación queda rota permanentemente (el
    # historial corrupto ya se guardó). disable_parallel_tool_use fuerza a
    # Claude a resolver un pedido por turno, sin importar cuántos junte el
    # cliente en un solo mensaje.
    tool_choice = {"type": "auto", "disable_parallel_tool_use": True}

    for cycle in range(MAX_TOOL_CYCLES):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
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

            if tool_name in ("generate_invoice_draft", "generate_rep_draft"):
                # Validate before doing anything fiscal
                try:
                    if tool_name == "generate_invoice_draft":
                        result_data = InvoiceDraft(**tool_input)
                    else:
                        result_data = RepDraft(**tool_input)
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

                # tool_choice="none": esta llamada es SOLO para generar el
                # texto de confirmación tras un tool_use ya resuelto. Si
                # Claude tuviera OTRA acción pendiente (ej. una segunda
                # solicitud que el cliente mezcló en el mismo mensaje) y se
                # le permitiera llamar una tool aquí, el código no la
                # procesaría (no hay manejo de tool_use en esta rama) y
                # quedaría huérfana en el historial, rompiendo la
                # conversación en el siguiente turno — mismo bug de fondo
                # que disable_parallel_tool_use resuelve arriba, pero en
                # este segundo punto de entrada.
                final_response = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system,
                    tools=tools,
                    tool_choice={"type": "none"},
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
                if isinstance(result_data, InvoiceDraft):
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
