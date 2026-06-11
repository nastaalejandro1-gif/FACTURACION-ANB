import logging

import httpx

from config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TIMEOUT = 20.0


async def send_message(chat_id: int | str, text: str) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(f"{BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })
        if response.status_code == 200:
            return
        # Causa más común de rechazo: el texto contiene < o > que Telegram
        # interpreta como HTML inválido. Reintentar como texto plano.
        logger.warning(
            "Telegram sendMessage falló (%s): %s — reintentando sin HTML",
            response.status_code, response.text[:200],
        )
        retry = await client.post(f"{BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
        })
        if retry.status_code != 200:
            logger.error(
                "Telegram sendMessage falló definitivamente para %s (%s): %s",
                chat_id, retry.status_code, retry.text[:200],
            )


async def send_document(chat_id: int | str, file_bytes: bytes, filename: str, caption: str = "") -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{BASE}/sendDocument",
            data={"chat_id": str(chat_id), "caption": caption},
            files={"document": (filename, file_bytes)},
        )
        if response.status_code != 200:
            logger.error(
                "Telegram sendDocument falló para %s (%s): %s",
                chat_id, response.status_code, response.text[:200],
            )


async def get_file(file_id: str) -> bytes:
    """Download a file from Telegram servers."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get file path
        r = await client.get(f"{BASE}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        # Download
        r2 = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        r2.raise_for_status()
        return r2.content
