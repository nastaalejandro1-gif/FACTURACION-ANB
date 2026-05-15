import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, GOOGLE_SHEETS_ID, DESPACHO_ID
from models import ClientProfile

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Per-channel locks — prevents concurrent history writes for the same user
_channel_locks: dict[str, asyncio.Lock] = {}


def get_channel_lock(canal_id: str) -> asyncio.Lock:
    if canal_id not in _channel_locks:
        _channel_locks[canal_id] = asyncio.Lock()
    return _channel_locks[canal_id]


def _build_client() -> gspread.Client:
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    # Refresh to get a valid access token
    creds.refresh(Request())
    return gspread.authorize(creds)


def _get_sheet(name: str) -> gspread.Worksheet:
    gc = _build_client()
    spreadsheet = gc.open_by_key(GOOGLE_SHEETS_ID)
    return spreadsheet.worksheet(name)


# ---------------------------------------------------------------------------
# Retry decorator for Sheets API quota errors
# ---------------------------------------------------------------------------

_sheets_retry = retry(
    retry=retry_if_exception_type(gspread.exceptions.APIError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(value) -> float:
    """Convierte un valor de Sheets a float, tolerando % y celdas vacías."""
    try:
        return float(str(value).replace("%", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _get(row: dict, key: str, default="") -> str:
    """Lookup case-insensitive en el dict de gspread."""
    if key in row:
        return str(row[key])
    key_lower = key.lower()
    for k, v in row.items():
        if k.lower() == key_lower:
            return str(v)
    return str(default)


# ---------------------------------------------------------------------------
# Client profile
# ---------------------------------------------------------------------------

@_sheets_retry
def get_client_by_canal_id(canal: str, canal_id: str) -> Optional[ClientProfile]:
    ws = _get_sheet("Clientes")
    records = ws.get_all_records()

    canal_id_str = str(int(float(canal_id)))  # normalize: "7963818260.0" → "7963818260"

    for row in records:
        if (
            _get(row, "canal").lower() == canal.lower()
            and str(int(float(_get(row, "canal_id", "0") or "0"))) == canal_id_str
            and _get(row, "activo", "NO").upper() == "SI"
        ):
            return ClientProfile(
                despacho_id=_get(row, "despacho_id"),
                id_cliente=_get(row, "ID_cliente"),
                nombre_comercial=_get(row, "Nombre_comercial"),
                razon_social=_get(row, "Razon_social"),
                rfc=_get(row, "RFC"),
                canal=_get(row, "canal"),
                canal_id=canal_id_str,
                email_factura=_get(row, "Email_factura"),
                tipo_persona=_get(row, "Tipo_persona"),
                regimen_fiscal=_get(row, "Regimen_fiscal"),
                cp_fiscal=_get(row, "CP_fiscal"),
                iva_aplica=_get(row, "IVA_aplica"),
                retencion_iva=_to_float(_get(row, "Retencion_IVA", "0")),
                retencion_isr=_to_float(_get(row, "Retencion_ISR", "0")),
                ieps_rate=_to_float(_get(row, "IEPS_rate", "0")),
                clave_prod_serv_default=_get(row, "Clave_prod_serv_default"),
                requiere_revision=_get(row, "Requiere_revision", "NO").upper() == "SI",
                notas_fiscales=_get(row, "Notas_fiscales"),
                activo=True,
                facturapi_key=_get(row, "Facturapi_key"),
            )
    return None


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def _content_to_serializable(content) -> str:
    """Convert message content (str or list of SDK blocks) to a JSON string."""
    if isinstance(content, str):
        return content
    # List of content blocks (may include TextBlock, ToolUseBlock, ToolResultBlock)
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
    """Reconstruct content from what was stored in Sheets."""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return raw  # plain string message


def strip_binary_from_messages(messages: list) -> list:
    """Replace image/document content blocks with a text marker before saving to Sheets."""
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
    """Strip binary from in-memory history after extraction (before 2nd Claude call)."""
    return strip_binary_from_messages(messages)


@_sheets_retry
def load_history(canal_id: str) -> list:
    ws = _get_sheet("Conversaciones")
    canal_id_str = str(int(float(canal_id)))
    records = ws.get_all_records()
    for row in records:
        if str(int(float(str(row.get("canal_id", "0"))))) == canal_id_str:
            raw = row.get("historial", "[]")
            try:
                msgs = json.loads(raw)
                return [
                    {"role": m["role"], "content": _content_from_serializable(m["content"])}
                    for m in msgs
                ]
            except Exception:
                return []
    return []


@_sheets_retry
def save_history(canal: str, canal_id: str, messages: list) -> None:
    ws = _get_sheet("Conversaciones")
    canal_id_str = str(int(float(canal_id)))

    # Serialize — strip binary first, then convert blocks to dicts
    cleaned = strip_binary_from_messages(messages)
    serialized = [
        {"role": m["role"], "content": _content_to_serializable(m["content"])}
        for m in cleaned
    ]
    historial_json = json.dumps(serialized, ensure_ascii=False)

    # Enforce cell size limit: keep last N messages if over 45K chars
    while len(historial_json) > 45_000 and len(serialized) > 4:
        serialized = serialized[2:]  # drop oldest pair
        # Drop any leading tool_result messages orphaned after the trim
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
    records = ws.get_all_records()

    for i, row in enumerate(records, start=2):  # row 1 = header
        if str(int(float(str(row.get("canal_id", "0"))))) == canal_id_str:
            ws.update(f"D{i}", [[historial_json]])
            ws.update(f"E{i}", [[now]])
            return

    # New client — append row
    ws.append_row([DESPACHO_ID, canal, canal_id_str, historial_json, now])


# ---------------------------------------------------------------------------
# Pendientes (approval queue)
# ---------------------------------------------------------------------------

@_sheets_retry
def save_pending(
    invoice_id: str,
    canal: str,
    canal_id: str,
    telegram_message_id: int,
    invoice_json: str,
    motivo_revision: str,
) -> None:
    ws = _get_sheet("Pendientes")
    now = datetime.now(timezone.utc).isoformat()
    ws.append_row([
        invoice_id,
        DESPACHO_ID,
        canal,
        canal_id,
        telegram_message_id,
        invoice_json,
        motivo_revision,
        now,
        "pendiente",
        "",  # timestamp_respuesta
    ])


@_sheets_retry
def get_pending(invoice_id: str) -> Optional[dict]:
    ws = _get_sheet("Pendientes")
    records = ws.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row.get("id", "")) == invoice_id:
            return {"row_index": i, **row}
    return None


@_sheets_retry
def update_pending_status(row_index: int, estado: str) -> None:
    ws = _get_sheet("Pendientes")
    now = datetime.now(timezone.utc).isoformat()
    ws.update(f"I{row_index}", [[estado]])
    ws.update(f"J{row_index}", [[now]])


@_sheets_retry
def get_overdue_pending(hours: int = 24) -> list[dict]:
    """Return pending invoices older than `hours` hours."""
    from datetime import timedelta
    ws = _get_sheet("Pendientes")
    records = ws.get_all_records()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    overdue = []
    for row in records:
        if str(row.get("estado", "")) == "pendiente":
            ts_str = str(row.get("timestamp", ""))
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts < cutoff:
                    overdue.append(row)
            except ValueError:
                pass
    return overdue


@_sheets_retry
def is_message_already_processed(telegram_message_id: int) -> bool:
    """Check Pendientes and Bitacora for an existing entry with this message_id."""
    ws = _get_sheet("Pendientes")
    records = ws.get_all_records()
    msg_id_str = str(telegram_message_id)
    return any(
        str(row.get("telegram_message_id", "")) == msg_id_str
        for row in records
    )


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------

@_sheets_retry
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
    ws = _get_sheet("Bitacora")
    now = datetime.now(timezone.utc).isoformat()
    ws.append_row([
        invoice_id,
        DESPACHO_ID,
        str(int(float(canal_id))),
        rfc_emisor,
        rfc_receptor,
        monto,
        total,
        "SI" if requirio_revision else "NO",
        estado,
        folio_fiscal,
        now,
        error_detalle,
    ])
