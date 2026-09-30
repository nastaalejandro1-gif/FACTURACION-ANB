import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, DESPACHO_ID
from fiscal_engine import FiscalRules
from models import ClientProfile

logger = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None
_channel_locks: dict[str, asyncio.Lock] = {}


def _get_supabase() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


def get_channel_lock(canal_id: str) -> asyncio.Lock:
    if canal_id not in _channel_locks:
        _channel_locks[canal_id] = asyncio.Lock()
    return _channel_locks[canal_id]


def _to_float(value) -> float:
    try:
        return float(str(value).replace("%", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# History serialization helpers (unchanged)
# ---------------------------------------------------------------------------

def _content_to_serializable(content) -> str:
    if isinstance(content, str):
        return content
    blocks = []
    for block in content:
        if hasattr(block, "model_dump"):
            blocks.append(block.model_dump())
        elif isinstance(block, dict):
            blocks.append(block)
        else:
            blocks.append({"type": "text", "text": str(block)})
    return json.dumps(blocks)


def _content_from_serializable(raw: str):
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def strip_binary_from_messages(messages: list) -> list:
    cleaned = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                block_dict = block.model_dump() if hasattr(block, "model_dump") else block
                if isinstance(block_dict, dict) and block_dict.get("type") in ("image", "document"):
                    new_blocks.append({"type": "text", "text": "[CSF adjunta — datos extraídos]"})
                else:
                    new_blocks.append(block_dict)
            cleaned.append({"role": msg["role"], "content": new_blocks})
        else:
            cleaned.append(msg)
    return cleaned


def strip_binary_in_place(messages: list) -> list:
    return strip_binary_from_messages(messages)


# ---------------------------------------------------------------------------
# Client profile
# ---------------------------------------------------------------------------

def get_client_by_canal_id(canal: str, canal_id: str) -> Optional[ClientProfile]:
    sb = _get_supabase()
    canal_id_str = str(int(float(canal_id)))

    result = (
        sb.table("clientes")
        .select("*")
        .eq("canal", canal.lower())
        .eq("canal_id", canal_id_str)
        .eq("activo", True)
        .execute()
    )

    if not result.data:
        return None

    row = result.data[0]
    return ClientProfile(
        despacho_id=row.get("despacho_id", ""),
        id_cliente=str(row.get("id_cliente", "")),
        nombre_comercial=row.get("nombre_comercial", ""),
        razon_social=row.get("razon_social", ""),
        rfc=row.get("rfc", ""),
        canal=row.get("canal", ""),
        canal_id=canal_id_str,
        email_factura=row.get("email_factura", ""),
        tipo_persona=row.get("tipo_persona", "PF"),
        regimen_fiscal=str(row.get("regimen_fiscal", "")),
        cp_fiscal=str(row.get("cp_fiscal", "")),
        iva_aplica=row.get("iva_aplica", "SI"),
        retencion_iva=_to_float(row.get("retencion_iva", 0)),
        retencion_isr=_to_float(row.get("retencion_isr", 0)),
        ieps_rate=_to_float(row.get("ieps_rate", 0)),
        clave_prod_serv_default=str(row.get("clave_prod_serv_default", "")),
        requiere_revision=bool(row.get("requiere_revision", False)),
        notas_fiscales=row.get("notas_fiscales", ""),
        activo=True,
        facturapi_key=row.get("facturapi_key", ""),
    )


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def load_history(canal_id: str) -> list:
    sb = _get_supabase()
    canal_id_str = str(int(float(canal_id)))

    result = (
        sb.table("conversaciones")
        .select("historial")
        .eq("canal_id", canal_id_str)
        .execute()
    )

    if not result.data:
        return []

    raw = result.data[0].get("historial", "[]")
    try:
        msgs = json.loads(raw)
        return [
            {"role": m["role"], "content": _content_from_serializable(m["content"])}
            for m in msgs
        ]
    except Exception:
        return []


def save_history(canal: str, canal_id: str, messages: list) -> None:
    sb = _get_supabase()
    canal_id_str = str(int(float(canal_id)))

    cleaned = strip_binary_from_messages(messages)
    serialized = [
        {"role": m["role"], "content": _content_to_serializable(m["content"])}
        for m in cleaned
    ]
    historial_json = json.dumps(serialized, ensure_ascii=False)

    # Keep last N messages if approaching Supabase text column limits
    while len(historial_json) > 45_000 and len(serialized) > 4:
        serialized = serialized[2:]
        while serialized:
            content = serialized[0].get("content", [])
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                serialized = serialized[1:]
            else:
                break
        historial_json = json.dumps(serialized, ensure_ascii=False)

    now = datetime.now(timezone.utc).isoformat()
    sb.table("conversaciones").upsert(
        {
            "despacho_id": DESPACHO_ID,
            "canal": canal,
            "canal_id": canal_id_str,
            "historial": historial_json,
            "ultima_actualizacion": now,
        },
        on_conflict="canal,canal_id",
    ).execute()


# ---------------------------------------------------------------------------
# Pendientes (approval queue)
# ---------------------------------------------------------------------------

def save_pending(
    invoice_id: str,
    canal: str,
    canal_id: str,
    telegram_message_id: int,
    invoice_json: str,
    motivo_revision: str,
    tipo_aprobacion: str = "anb_revision",
    canal_id_aprobador: Optional[str] = None,
) -> None:
    """
    tipo_aprobacion: 'anb_revision' (default, escalamiento a ANB) o
    'cliente_confirmacion' (vista previa con botones Sí/No para el cliente).
    canal_id_aprobador: quién está autorizado a pulsar el botón — obligatorio
    para 'cliente_confirmacion' (ver main.py::handle_callback_query, que
    verifica esto antes de timbrar para que un cliente no apruebe la
    factura de otro).
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": invoice_id,
        "despacho_id": DESPACHO_ID,
        "canal": canal,
        "canal_id": canal_id,
        "telegram_message_id": telegram_message_id,
        "invoice_json": invoice_json,
        "motivo_revision": motivo_revision,
        "timestamp": now,
        "estado": "pendiente",
        "tipo_aprobacion": tipo_aprobacion,
    }
    if canal_id_aprobador is not None:
        row["canal_id_aprobador"] = canal_id_aprobador
    sb.table("pendientes").insert(row).execute()


def get_pending(invoice_id: str) -> Optional[dict]:
    sb = _get_supabase()
    result = sb.table("pendientes").select("*").eq("id", invoice_id).execute()
    if not result.data:
        return None
    return result.data[0]


def update_pending_status(invoice_id: str, estado: str) -> None:
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("pendientes").update(
        {"estado": estado, "timestamp_respuesta": now}
    ).eq("id", invoice_id).execute()


def get_overdue_pending(hours: int = 24) -> list[dict]:
    sb = _get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = (
        sb.table("pendientes")
        .select("*")
        .eq("estado", "pendiente")
        .lt("timestamp", cutoff)
        .execute()
    )
    return result.data or []


def is_message_already_processed(canal_id: str, telegram_message_id: int) -> bool:
    # message_id en Telegram es un contador POR CHAT, no global: dos clientes
    # distintos comparten los mismos números. Filtrar siempre junto con canal_id.
    sb = _get_supabase()
    result = (
        sb.table("pendientes")
        .select("id")
        .eq("canal_id", str(canal_id))
        .eq("telegram_message_id", telegram_message_id)
        .execute()
    )
    return len(result.data) > 0


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FacturaReciente:
    tipo: str  # 'ingreso' | 'rep'
    rfc_receptor: str
    total: float
    timestamp: str
    estado: str
    folio_fiscal: str
    uuid_factura_origen: str


def get_facturas_recientes(despacho_id: str, canal_id: str, limite: int = 3) -> list[FacturaReciente]:
    """
    Últimas facturas/REPs de bitácora para este canal_id (fuente de verdad
    de qué se timbró, no el historial de chat con Claude). Se usa para que
    el prompt pueda responder "cancela la que acabas de hacer" o "otra
    igual" sin necesitar guardar la conversación completa — ver
    claude_client.build_system_prompt.
    """
    sb = _get_supabase()
    canal_id_str = str(int(float(canal_id)))
    result = (
        sb.table("bitacora")
        .select("*")
        .eq("despacho_id", despacho_id)
        .eq("canal_id", canal_id_str)
        .order("timestamp", desc=True)
        .limit(limite)
        .execute()
    )
    return [
        FacturaReciente(
            tipo=row.get("tipo", "ingreso"),
            rfc_receptor=row.get("rfc_receptor", ""),
            total=float(row.get("total") or 0),
            timestamp=row.get("timestamp", ""),
            estado=row.get("estado", ""),
            folio_fiscal=row.get("folio_fiscal", ""),
            uuid_factura_origen=row.get("uuid_factura_origen", ""),
        )
        for row in (result.data or [])
    ]


def get_rep_history(uuid_factura_origen: str) -> list[dict]:
    """Retorna los REPs timbrados para un UUID de factura origen, ordenados por timestamp.

    Solo cuenta estado='timbrado': los intentos fallidos no consumen parcialidad
    ni alteran el saldo insoluto."""
    sb = _get_supabase()
    result = (
        sb.table("bitacora")
        .select("*")
        .eq("uuid_factura_origen", uuid_factura_origen.upper())
        .eq("tipo", "rep")
        .eq("estado", "timbrado")
        .order("timestamp")
        .execute()
    )
    return result.data or []


def log_to_bitacora(
    invoice_id: str,
    canal_id: str,
    rfc_emisor: str,
    rfc_receptor: str,
    monto: float,
    total: float,
    requirio_revision: bool,
    estado: str,
    folio_fiscal: str = "",
    error_detalle: str = "",
    tipo: str = "ingreso",
    uuid_factura_origen: str = "",
    imp_saldo_insoluto: Optional[float] = None,
) -> None:
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": invoice_id,
        "despacho_id": DESPACHO_ID,
        "canal_id": str(int(float(canal_id))),
        "rfc_emisor": rfc_emisor,
        "rfc_receptor": rfc_receptor,
        # float() explícito: puede llegar Decimal (fiscal_engine/models.py) o
        # float (paths de error, que aún pasan montos crudos) — Supabase/JSON
        # no serializa Decimal directamente.
        "monto": float(monto),
        "total": float(total),
        "requirio_revision": requirio_revision,
        "estado": estado,
        "folio_fiscal": folio_fiscal,
        "timestamp": now,
        "error_detalle": error_detalle,
        "tipo": tipo,
    }
    if uuid_factura_origen:
        row["uuid_factura_origen"] = uuid_factura_origen.upper()
    if imp_saldo_insoluto is not None:
        row["imp_saldo_insoluto"] = float(imp_saldo_insoluto)
    sb.table("bitacora").upsert(row, on_conflict="id").execute()


# ---------------------------------------------------------------------------
# Catálogo de clave_prod_serv y reglas fiscales (motor de cálculo)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaveCatalogo:
    clave_prod_serv: str
    descripcion_clave: str
    aplica_ieps: bool
    id_cliente: Optional[str]  # None = clave global del despacho


def get_catalogo_claves(despacho_id: str, id_cliente: str) -> list[ClaveCatalogo]:
    """Claves aprobadas para este cliente + las globales del despacho (ej. flete)."""
    sb = _get_supabase()
    propias = (
        sb.table("catalogo_clave_prod_serv")
        .select("*")
        .eq("despacho_id", despacho_id)
        .eq("id_cliente", id_cliente)
        .eq("activa", True)
        .execute()
    ).data or []
    globales = (
        sb.table("catalogo_clave_prod_serv")
        .select("*")
        .eq("despacho_id", despacho_id)
        .is_("id_cliente", "null")
        .eq("activa", True)
        .execute()
    ).data or []
    return [
        ClaveCatalogo(
            clave_prod_serv=str(f["clave_prod_serv"]),
            descripcion_clave=str(f["descripcion_clave"]),
            aplica_ieps=bool(f["aplica_ieps"]),
            id_cliente=f.get("id_cliente"),
        )
        for f in (propias + globales)
    ]


def save_clave_aprobada(
    despacho_id: str,
    id_cliente: str,
    clave_prod_serv: str,
    descripcion_clave: str,
    aplica_ieps: bool,
    aprobada_por: str,
) -> None:
    """Agrega (o reactiva) una clave al catálogo del cliente — se llama cuando
    ANB aprueba una clave escalada por EscalationReason.CLAVE_PROD_SERV_NUEVA.
    Idempotente vía upsert sobre el UNIQUE (despacho_id, id_cliente, clave_prod_serv)."""
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("catalogo_clave_prod_serv").upsert(
        {
            "despacho_id": despacho_id,
            "id_cliente": id_cliente,
            "clave_prod_serv": clave_prod_serv,
            "descripcion_clave": descripcion_clave,
            "aplica_ieps": aplica_ieps,
            "aprobada_por": aprobada_por,
            "fecha_aprobacion": now,
            "activa": True,
        },
        on_conflict="despacho_id,id_cliente,clave_prod_serv",
    ).execute()


def get_fiscal_rules(despacho_id: str, id_cliente: str) -> FiscalRules:
    """Trae la regla fiscal vigente (vigente_hasta IS NULL) y el catálogo de
    claves del cliente, y arma el FiscalRules que espera fiscal_engine."""
    sb = _get_supabase()
    result = (
        sb.table("reglas_fiscales_cliente")
        .select("*")
        .eq("despacho_id", despacho_id)
        .eq("id_cliente", id_cliente)
        .is_("vigente_hasta", "null")
        .order("vigente_desde", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise ValueError(f"No hay reglas fiscales vigentes para el cliente {id_cliente}")
    row = result.data[0]

    claves = get_catalogo_claves(despacho_id, id_cliente)
    claves_con_ieps = frozenset(c.clave_prod_serv for c in claves if c.aplica_ieps)

    return FiscalRules(
        iva_aplica=bool(row["iva_aplica"]),
        tasa_iva=Decimal(str(row["tasa_iva"])),
        retencion_iva_tasa=Decimal(str(row["retencion_iva_tasa"])),
        retencion_isr_tasa=Decimal(str(row["retencion_isr_tasa"])),
        ieps_tasa=Decimal(str(row["ieps_tasa"])),
        claves_con_ieps=claves_con_ieps,
        monto_maximo_sin_autorizacion=Decimal(str(row.get("monto_maximo_sin_autorizacion") or "100000")),
    )
