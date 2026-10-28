"""
Modo despacho: Alejandro y los socios que él autorice facturan desde su
propio chat A NOMBRE de los clientes del despacho.

- Alejandro (ALEJANDRO_CHAT_ID) puede usar a todos los clientes activos.
- Un socio puede usar los clientes cuya columna `clientes.operadores`
  incluye su chat_id (Alejandro la administra con /permiso y /quitar).
- Un cliente puede tener varias filas con el mismo RFC (un chat por persona
  de su equipo); como emisor cuenta una sola: la del propio chat si la hay,
  si no la de id_cliente más bajo ("canónica", donde se guardan los permisos).

El emisor elegido se guarda por chat (sheets_client.set_emisor_activo) y se
cambia con /emisor o nombrándolo en el mensaje (tool seleccionar_emisor).
"""
import html
import logging
import unicodedata
from typing import Optional

from config import ALEJANDRO_CHAT_ID
from models import ClientProfile
import sheets_client

logger = logging.getLogger(__name__)


def normalizar(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_acentos.lower().replace(",", " ").replace(".", " ").split())


def _orden_id(perfil: ClientProfile) -> tuple:
    return (0, int(perfil.id_cliente)) if perfil.id_cliente.isdigit() else (1, perfil.id_cliente)


def canonicos(clientes: list[ClientProfile]) -> list[ClientProfile]:
    """Una fila por RFC: la de id_cliente más bajo."""
    por_rfc: dict[str, ClientProfile] = {}
    for c in sorted(clientes, key=_orden_id):
        por_rfc.setdefault(c.rfc.upper(), c)
    return sorted(por_rfc.values(), key=lambda c: normalizar(c.nombre_comercial))


def calcular_emisores(
    chat_id: str, es_admin: bool, clientes: list[tuple[ClientProfile, list[str]]],
) -> Optional[list[ClientProfile]]:
    """
    Emisores que puede usar ese chat, o None si no está en modo despacho
    (cliente normal). Lógica pura — ver emisores_permitidos para la consulta.
    """
    if es_admin:
        visibles = [c for c, _ in clientes]
    else:
        con_permiso = {c.rfc.upper() for c, operadores in clientes if chat_id in operadores}
        if not con_permiso:
            return None
        propios_rfc = {c.rfc.upper() for c, _ in clientes if c.canal_id == chat_id}
        visibles = [c for c, _ in clientes if c.rfc.upper() in con_permiso | propios_rfc]
    propias = {c.rfc.upper(): c for c, _ in clientes if c.canal_id == chat_id}
    return [propias.get(c.rfc.upper(), c) for c in canonicos(visibles)]


def emisores_permitidos(chat_id: str) -> Optional[list[ClientProfile]]:
    es_admin = int(chat_id) == ALEJANDRO_CHAT_ID
    try:
        clientes = sheets_client.get_clientes_activos()
    except Exception:
        logger.exception("No se pudo leer clientes para el modo despacho de %s", chat_id)
        if not es_admin:
            return None
        raise
    return calcular_emisores(chat_id, es_admin, clientes)


def como_operador(perfil: ClientProfile, chat_id: str) -> ClientProfile:
    return perfil.model_copy(update={"operador_canal_id": chat_id})


def perfil_operador(chat_id: str, id_cliente: str) -> Optional[ClientProfile]:
    """Perfil del emisor para ese socio, SOLO si todavía tiene permiso (se
    vuelve a verificar al confirmar: el permiso pudo quitarse mientras tanto)."""
    emisores = emisores_permitidos(chat_id) or []
    perfil = next((e for e in emisores if e.id_cliente == id_cliente), None)
    return como_operador(perfil, chat_id) if perfil else None


def perfil_activo(chat_id: str, emisores: list[ClientProfile]) -> Optional[ClientProfile]:
    """El emisor elegido por ese chat; si no ha elegido, su propia fila (si tiene)."""
    elegido = sheets_client.get_emisor_activo(chat_id)
    perfil = next((e for e in emisores if e.id_cliente == elegido), None)
    if perfil is None:
        perfil = next((e for e in emisores if e.canal_id == chat_id), None)
    return como_operador(perfil, chat_id) if perfil else None


def buscar_emisor(texto: str, emisores: list[ClientProfile]) -> list[ClientProfile]:
    """Coincidencias de lo que escribió el socio (nombre parcial o RFC)."""
    buscado = normalizar(texto)
    if not buscado:
        return []
    exactos = [e for e in emisores if buscado in (normalizar(e.nombre_comercial), e.rfc.lower())]
    if exactos:
        return exactos
    return [
        e for e in emisores
        if buscado in normalizar(e.nombre_comercial) or buscado in normalizar(e.razon_social)
        or buscado == e.rfc.lower()
    ]


def mencionados(mensaje: str, emisores: list[ClientProfile]) -> list[ClientProfile]:
    """Emisores cuyo nombre comercial o RFC aparece completo en el mensaje
    (para un socio que aún no elige emisor y escribe "a nombre de Envoy...")."""
    texto = f" {normalizar(mensaje)} "
    return [
        e for e in emisores
        if f" {normalizar(e.nombre_comercial)} " in texto or f" {e.rfc.lower()} " in texto
    ]


def lista_emisores(emisores: list[ClientProfile], activo: Optional[ClientProfile] = None) -> str:
    lineas = []
    for e in emisores:
        marca = "👉 " if activo and e.id_cliente == activo.id_cliente else "• "
        lineas.append(f"{marca}{html.escape(e.nombre_comercial)} ({html.escape(e.rfc)})")
    return "\n".join(lineas)
