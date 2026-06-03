import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, DESPACHO_ID
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
) -> None:
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("pendientes").insert(
        {
            "id": invoice_id,
            "despacho_id": DESPACHO_ID,
            "canal": canal,
            "canal_id": canal_id,
            "telegram_message_id": telegram_message_id,
            "invoice_json": invoice_json,
            "motivo_revision": motivo_revision,
            "timestamp": now,
            "estado": "pendiente",
        }
    ).execute()


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


def is_message_already_processed(telegram_message_id: int) -> bool:
    sb = _get_supabase()
    result = (
        sb.table("pendientes")
        .select("id")
        .eq("telegram_message_id", telegram_message_id)
        .execute()
    )
    return len(result.data) > 0


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------

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
) -> None:
    sb = _get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("bitacora").upsert(
        {
            "id": invoice_id,
            "despacho_id": DESPACHO_ID,
            "canal_id": str(int(float(canal_id))),
            "rfc_emisor": rfc_emisor,
            "rfc_receptor": rfc_receptor,
            "monto": monto,
            "total": total,
            "requirio_revision": requirio_revision,
            "estado": estado,
            "folio_fiscal": folio_fiscal,
            "timestamp": now,
            "error_detalle": error_detalle,
        },
        on_conflict="id",
    ).execute()
