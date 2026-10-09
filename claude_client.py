import base64
import logging
import re
from typing import Optional

import anthropic
from pydantic import ValidationError

from config import ANTHROPIC_API_KEY, ANTHROPIC_EFFORT, ANTHROPIC_MODEL
from models import ClientProfile, InvoiceDraft, RepDraft
from sat_catalogs import REGIMENES_FISCALES_VALIDOS
from sheets_client import ClaveCatalogo, FacturaReciente, strip_binary_in_place
from tools import build_invoice_tools

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_TOOL_CYCLES = 3

# Precios por millón de tokens (USD): input, output, cache write (5 min),
# cache read. Solo para el log de costo; agregar aquí si se usa otro modelo.
PRECIOS_POR_MILLON = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
}
(
    PRECIO_INPUT_POR_MILLON,
    PRECIO_OUTPUT_POR_MILLON,
    PRECIO_CACHE_WRITE_POR_MILLON,
    PRECIO_CACHE_READ_POR_MILLON,
) = PRECIOS_POR_MILLON.get(ANTHROPIC_MODEL, PRECIOS_POR_MILLON["claude-sonnet-4-6"])

# Tope de seguridad: si una conversación se alarga sin completar ninguna
# factura/REP (cliente indeciso, "hola"/"gracias" sueltos, intentos
# abandonados) el historial podría crecer indefinidamente igual que antes
# de que se agregara el reinicio condicional de abajo. Por encima de este
# tamaño se recorta a los últimos HISTORY_TRIM_KEEP mensajes.
MAX_HISTORY_MESSAGES = 40
HISTORY_TRIM_KEEP = 16

# Si el mensaje de confirmación de Claude tras un tool_use exitoso menciona
# alguna de estas frases, hay MÁS pedidos del mismo lote todavía pendientes
# (ej. cliente pidió "factura A y REP B" junto, Claude resuelve uno por
# turno) — no hay que reiniciar el historial todavía o se pierde el
# contexto del/los pedido(s) que faltan. Heurística de texto, no perfecta,
# pero el respaldo es el tope de arriba: aunque falle, el historial no
# crece sin límite.
_PATRON_PEDIDO_PENDIENTE = re.compile(
    r"(otra factura|otra solicitud|segunda solicitud|tercera solicitud|"
    r"la siguiente|sigo con|ahora paso a|solicitud 2|solicitud 3|"
    r"la otra factura|el otro pedido|el otro rep)",
    re.IGNORECASE,
)


def _recortar_historial_si_excede(history: list) -> None:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return
    corte = len(history) - HISTORY_TRIM_KEEP
    # Nunca empezar el recorte justo en un tool_result huérfano (dejaría
    # su tool_use del turno anterior fuera, y la API rechazaría el
    # siguiente turno) — retroceder al tool_use correspondiente si hace falta.
    while corte > 0:
        content = history[corte].get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            corte -= 1
        else:
            break
    if corte > 0:
        logger.info("Historial recortado de %d a %d mensajes (tope de seguridad)", len(history), len(history) - corte)
        del history[:corte]


def _log_uso_claude(paso: str, response: anthropic.types.Message) -> None:
    """
    Logging permanente de costo por llamada a la API — visible en los logs
    de Railway. `paso` identifica en qué parte del flujo ocurrió la
    llamada (extracción de documento, turno de texto, qué tool se llamó o
    si fue un reintento por validación fallida), para poder ver en qué
    parte de una conversación se concentra el costo.
    """
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    costo = (
        usage.input_tokens * PRECIO_INPUT_POR_MILLON
        + usage.output_tokens * PRECIO_OUTPUT_POR_MILLON
        + cache_write * PRECIO_CACHE_WRITE_POR_MILLON
        + cache_read * PRECIO_CACHE_READ_POR_MILLON
    ) / 1_000_000
    logger.info(
        "claude_usage paso=%s modelo=%s input_tokens=%d output_tokens=%d "
        "cache_read_tokens=%d cache_write_tokens=%d costo_usd=%.6f",
        paso, ANTHROPIC_MODEL, usage.input_tokens, usage.output_tokens,
        cache_read, cache_write, costo,
    )


def _render_regimenes() -> str:
    return "\n".join(f"  {codigo} = {nombre}" for codigo, nombre in REGIMENES_FISCALES_VALIDOS.items())


def _render_catalogo(catalogo: list[ClaveCatalogo]) -> str:
    if not catalogo:
        return "  (sin claves aprobadas todavía para este cliente — usa 'NUEVA' en el primer concepto)"
    return "\n".join(f"  {c.clave_prod_serv} = {c.descripcion_clave}" for c in catalogo)


def _render_facturas_recientes(facturas: list[FacturaReciente]) -> str:
    if not facturas:
        return "  (sin facturas previas registradas para este cliente)"
    lineas = []
    for f in facturas:
        tipo_texto = "REP" if f.tipo == "rep" else "Factura"
        referencia = f.folio_fiscal or f.uuid_factura_origen or "sin folio"
        lineas.append(
            f"  {tipo_texto} | receptor RFC {f.rfc_receptor} | total ${f.total:,.2f} | "
            f"{f.timestamp[:16]} | estado: {f.estado} | folio/UUID: {referencia}"
        )
    return "\n".join(lineas)


def build_system_prompt(
    profile: ClientProfile, catalogo: list[ClaveCatalogo], facturas_recientes: list[FacturaReciente],
) -> str:
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

ÚLTIMAS FACTURAS/REPS DE ESTE CLIENTE (para referencia si menciona "la anterior",
"la que acabas de hacer", o pide "otra igual" — el historial de esta conversación se
reinicia después de cada factura completada, así que ESTA es tu única memoria de lo
que ya se procesó; NO tienes los conceptos línea por línea de facturas pasadas, solo
este resumen):
{_render_facturas_recientes(facturas_recientes)}
Si el cliente pide cancelar o corregir una factura ya timbrada, es FUERA DE ALCANCE
(ver abajo) — puedes usar este resumen para identificar CUÁL menciona y dar una
respuesta más útil, pero igual debes remitirlo al despacho, nunca intentar cancelarla
tú. Si pide "otra igual" pero a otro RFC, no tienes los conceptos exactos de la
anterior — pide que te los reenvíe o comparta la cotización de nuevo.

CATÁLOGO DE CLAVES DE PRODUCTO/SERVICIO APROBADAS PARA ESTE CLIENTE:
{_render_catalogo(catalogo)}
Elige SIEMPRE una de estas claves si el concepto corresponde claramente a una de ellas.
Si NINGÚN concepto de la lista aplica, usa clave_prod_serv="NUEVA" y llena
clave_prod_serv_propuesta con el código SAT de 8 dígitos que mejor describe el concepto —
esto se enviará a revisión del despacho una sola vez, y quedará aprobado para futuras
facturas de este cliente.

CRÍTICO — nunca "fuerces" una clave del catálogo que no corresponde solo porque es la única
opción disponible o para evitar usar "NUEVA". Juzga por la DESCRIPCIÓN de cada clave del
catálogo (lo que dice arriba, no el nombre del segmento SAT — el catálogo SAT a veces mete
claves de mercancía dentro de segmentos que se llaman "Servicios de Gestión" o similar, así
que el nombre del segmento NO es un indicador confiable de si es producto o servicio). Si la
DESCRIPCIÓN de una clave del catálogo describe razonablemente el concepto, úsala — aunque el
concepto sea un objeto físico y la clave "suene" a servicio. Usa "NUEVA" únicamente cuando
NINGUNA descripción del catálogo corresponde al concepto. La clave equivocada en un CFDI
tiene consecuencias fiscales reales; "NUEVA" es la opción segura ante una duda genuina, pero
no la uses solo porque el nombre del segmento SAT te generó dudas.

MANEJO DE DOCUMENTOS PDF/IMAGEN:
Cuando el cliente envía un documento, determina qué tipo es antes de responder:

A) CONSTANCIA DE SITUACIÓN FISCAL (CSF): documento oficial del SAT con RFC, razón social,
   régimen fiscal y código postal del receptor. Extrae esos 4 datos.
   CRÍTICO: el régimen debe ir como CÓDIGO NUMÉRICO (ej. "603"), no la descripción.
   La CSF normalmente imprime SOLO el nombre del régimen, sin código — tradúcelo con el
   catálogo de arriba. "Régimen Simplificado de Confianza" (RESICO) es SIEMPRE 626, sea
   persona física o moral; NO lo confundas con 621 (Incorporación Fiscal / RIF).
   Si la CSF lista varios regímenes, pregunta al cliente cuál aplica a esta factura.
   OBLIGATORIO: en tu respuesta a la CSF escribe SIEMPRE el RFC, la razón social, el CP
   y el/los régimen(es) que leíste — también cuando además hagas una pregunta. El archivo
   se borra del historial después de este turno y lo que no escribas se pierde.

NUNCA inventes ni uses marcadores como "[RFC de la CSF]" o "[Razón social]". Si un dato que
necesitas ya no aparece en la conversación (el archivo se borró y no lo anotaste), pídeselo
al cliente: que reenvíe la CSF o lo escriba.

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
6. Facturas hechas en otro programa SÍ admiten REP. Si la factura no está en el sistema, el
   sistema le pide el XML al cliente por su cuenta — tú no lo pidas ni digas que no se puede.
   Cuando el cliente manda un XML, te llega una nota "[Sistema: ...]" con sus datos: úsalos.

VARIOS PEDIDOS EN EL MISMO MENSAJE:
Si el cliente junta más de un pedido (ej. "te aviso que pagaron la factura X, y además
necesito una factura nueva para Y") solo puedes llamar UNA herramienta por turno — resuelve
primero uno (el que tenga todos los datos completos, o el que el cliente mencionó primero)
y avísale que sigues con el/los otro(s) después. Es CRÍTICO que al llamar generate_invoice_draft
o generate_rep_draft en este caso pongas mas_pedidos_en_este_lote=true — el sistema usa ese
campo (no tu mensaje de texto) para saber si debe conservar el contexto de los pedidos que
faltan. Si te falta algún dato del segundo pedido, pídelo en el mismo mensaje de confirmación
del primero. Cuando proceses el ÚLTIMO pedido del lote, omite el campo o pon
mas_pedidos_en_este_lote=false.

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
    facturas_recientes: list[FacturaReciente],
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

    _recortar_historial_si_excede(history)

    paso_base = "extraccion_documento" if (file_bytes and media_type) else "turno_texto"

    system = [{
        "type": "text",
        "text": build_system_prompt(profile, catalogo, facturas_recientes),
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
            # Cachea también el historial (no solo system+tools): cada turno
            # reenvía la conversación completa, y sin esto se pagaba entera.
            cache_control={"type": "ephemeral"},
            output_config={"effort": ANTHROPIC_EFFORT},
        )

        if response.stop_reason == "tool_use":
            tool_block = next(b for b in response.content if b.type == "tool_use")
            tool_name = tool_block.name
            tool_input = tool_block.input
            # Señal ESTRUCTURADA (no adivinada por texto libre) de que el
            # cliente juntó varios pedidos en un mismo mensaje y todavía
            # queda al menos uno por procesar — ver tools.py. InvoiceDraft/
            # RepDraft no declaran este campo, así que Pydantic lo ignora
            # sin problema al construir el draft más abajo.
            mas_pedidos_pendientes = bool(tool_input.get("mas_pedidos_en_este_lote", False))

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
                    _log_uso_claude(f"{paso_base}→retry_validacion_{tool_name}", response)
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

                # Ahorro de costo: NO se hace una segunda llamada a la API solo
                # para generar el texto de confirmación. Claude ya incluye ese
                # texto junto con el tool_use en ESTA MISMA respuesta (se lo
                # pedimos explícitamente en el prompt — "Tu mensaje de
                # respuesta DEBE ser exactamente..."), confirmado empíricamente:
                # el modelo devuelve un bloque de texto + el tool_use en la
                # misma respuesta. Esto elimina una llamada completa a la API
                # (con su round-trip de latencia) por cada factura/REP generado.
                client_message = extract_text_from_response(response) or "Tu solicitud ha sido procesada."

                # Aun así hay que registrar el tool_result en el historial
                # (aunque no se vuelva a llamar a la API en este turno): la
                # API exige un tool_result por cada tool_use antes de la
                # PRÓXIMA llamada — si no se guarda, el siguiente turno del
                # cliente rompe la conversación (mismo bug que
                # disable_parallel_tool_use/tool_choice="none" evitan arriba).
                history.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": "Datos recibidos correctamente.",
                    }],
                })

                # Reiniciar el historial: la extracción para ESTE pedido ya
                # terminó (el draft se le entrega a fiscal_engine fuera de
                # esta capa; la confirmación final es por botones, no pasa
                # por Claude). Medido contra una conversación real de
                # producción: sin este reinicio, cada factura nueva del
                # mismo cliente paga por reenviar TODAS las facturas/REPs
                # previos ya completados en el mismo hilo de Telegram como
                # contexto (~$0.22 extra por factura en un caso real de 80
                # mensajes acumulados) — y peor, Claude puede confundirse
                # con contexto de pedidos viejos ya resueltos y no disparar
                # la herramienta cuando debería.
                #
                # EXCEPCIÓN: si el cliente juntó varios pedidos en el mismo
                # lote (ej. "factura A y REP B" en un solo mensaje), Claude
                # resuelve uno por turno (disable_parallel_tool_use). Reiniciar
                # aquí perdería el contexto de los pedidos que faltan del
                # MISMO lote. Se detecta por la señal ESTRUCTURADA del tool
                # (mas_pedidos_pendientes, arriba) — un patrón de texto libre
                # ("sigo con la otra factura") resultó no confiable: Claude
                # a veces avisa con frases que no coinciden con ningún patrón
                # razonable ("proceso los dos al mismo tiempo 🚀", etc.).
                # El patrón de texto queda como red de respaldo (por si
                # Claude no marca el campo estructurado pero su frase de
                # todas formas delata que hay más pendiente) — no como
                # mecanismo principal.
                if mas_pedidos_pendientes or _PATRON_PEDIDO_PENDIENTE.search(client_message):
                    logger.info("No se reinicia el historial: el cliente aún tiene otro pedido pendiente en este lote.")
                else:
                    history.clear()

                _log_uso_claude(f"{paso_base}→{tool_name}", response)
                if isinstance(result_data, InvoiceDraft):
                    return client_message, result_data, None
                else:
                    return client_message, None, result_data

        else:
            # Regular text response — no tool call
            _log_uso_claude(f"{paso_base}→respuesta_texto", response)
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


# ---------------------------------------------------------------------------
# Factura origen externa en PDF (el cliente no tiene el XML)
# ---------------------------------------------------------------------------

_TOOL_FACTURA_ORIGEN = {
    "name": "registrar_factura_origen",
    "description": "Registra los datos fiscales leídos del PDF de un CFDI 4.0.",
    "input_schema": {
        "type": "object",
        "properties": {
            "uuid": {"type": "string", "description": "Folio fiscal (UUID) del CFDI."},
            "rfc_emisor": {"type": "string"},
            "rfc_receptor": {"type": "string"},
            "nombre_receptor": {"type": "string"},
            "regimen_receptor": {"type": "string", "description": "Clave de 3 dígitos del régimen fiscal del receptor."},
            "cp_receptor": {"type": "string", "description": "Código postal del domicilio fiscal del receptor."},
            "metodo_pago": {"type": "string", "description": "PUE o PPD."},
            "moneda": {"type": "string", "description": "Clave de moneda, ej. MXN."},
            "total": {"type": "number"},
            "impuestos": {
                "type": "array",
                "description": "Una línea por impuesto y tasa, con la base sobre la que se calculó.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tipo": {"type": "string", "enum": ["IVA", "ISR", "IEPS"]},
                        "tasa": {"type": "number", "description": "Tasa decimal, ej. 0.16 o 0.0125."},
                        "retencion": {"type": "boolean"},
                        "base": {"type": "number"},
                    },
                    "required": ["tipo", "tasa", "retencion", "base"],
                },
            },
        },
        "required": [
            "uuid", "rfc_emisor", "rfc_receptor", "nombre_receptor", "regimen_receptor",
            "cp_receptor", "metodo_pago", "moneda", "total", "impuestos",
        ],
    },
}


def extraer_factura_origen_de_pdf(pdf_bytes: bytes) -> dict:
    """
    Lee el PDF de un CFDI con Claude y lo devuelve con la misma forma que
    cfdi_xml.parse_cfdi_xml (fuente='pdf'). Lo extraído de un PDF NUNCA
    se timbra sin que ANB lo revise — ver main._procesar_pdf_factura_origen.
    """
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        tools=[_TOOL_FACTURA_ORIGEN],
        tool_choice={"type": "tool", "name": "registrar_factura_origen"},
        messages=[{"role": "user", "content": [
            build_file_content_block(pdf_bytes, "application/pdf"),
            {"type": "text", "text": (
                "Extrae los datos de este CFDI tal como aparecen impresos. No inventes ni "
                "calcules nada que no esté en el documento; si un dato no aparece, déjalo vacío."
            )},
        ]}],
    )
    _log_uso_claude("extraccion_factura_origen_pdf", response)
    datos = next(b for b in response.content if b.type == "tool_use").input
    return {
        "fuente": "pdf",
        "uuid": str(datos.get("uuid", "")).strip().upper(),
        "fecha": "",
        "serie_folio": "",
        "tipo_comprobante": "",
        "metodo_pago": str(datos.get("metodo_pago", "")).upper(),
        "moneda": str(datos.get("moneda", "")).upper(),
        "total": str(datos.get("total", 0)),
        "rfc_emisor": str(datos.get("rfc_emisor", "")).upper(),
        "nombre_emisor": "",
        "rfc_receptor": str(datos.get("rfc_receptor", "")).upper(),
        "nombre_receptor": datos.get("nombre_receptor", ""),
        "regimen_receptor": str(datos.get("regimen_receptor", "")),
        "cp_receptor": str(datos.get("cp_receptor", "")),
        "bases_impuestos": [
            {"type": i["tipo"], "rate": str(i["tasa"]), "withholding": bool(i["retencion"]), "base": str(i["base"])}
            for i in datos.get("impuestos", [])
        ],
        "advertencias": [],
    }
