"""
Tests del manejo de historial en claude_client.py: el tope de seguridad
(_recortar_historial_si_excede) y la detección de pedido pendiente en el
mismo lote (_PATRON_PEDIDO_PENDIENTE) que evita reiniciar el historial a
mitad de un lote de varios pedidos.
"""
from claude_client import (
    HISTORY_TRIM_KEEP,
    MAX_HISTORY_MESSAGES,
    _PATRON_PEDIDO_PENDIENTE,
    _recortar_historial_si_excede,
)


def _mensaje_texto(role, texto):
    return {"role": role, "content": texto}


def test_no_recorta_si_no_excede_el_tope():
    history = [_mensaje_texto("user", f"msg {i}") for i in range(MAX_HISTORY_MESSAGES)]
    original = list(history)
    _recortar_historial_si_excede(history)
    assert history == original


def test_recorta_al_exceder_el_tope():
    history = [_mensaje_texto("user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(60)]
    _recortar_historial_si_excede(history)
    assert len(history) <= HISTORY_TRIM_KEEP + 1  # +1 por si retrocede para no huerfanar tool_result
    # Se conservan los mensajes MAS RECIENTES
    assert history[-1]["content"] == "msg 59"


def test_recorte_no_deja_tool_result_huerfano():
    """El punto de corte cae justo en un tool_result -- debe retroceder
    para incluir tambien el tool_use del turno anterior."""
    history = [_mensaje_texto("user", f"msg {i}") for i in range(30)]
    # Insertar un tool_use/tool_result exactamente donde caeria el corte
    corte_esperado = len(history) + 30 - HISTORY_TRIM_KEEP  # len tras agregar 30 mas abajo
    history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]})
    history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})
    history += [_mensaje_texto("user", f"post {i}") for i in range(30)]

    _recortar_historial_si_excede(history)

    # Ningun mensaje inicial debe ser un tool_result sin su tool_use precedente
    primero = history[0]
    contenido = primero.get("content")
    if isinstance(contenido, list):
        for b in contenido:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                raise AssertionError("El recorte dejo un tool_result huerfano al inicio del historial")


def test_patron_pedido_pendiente_detecta_continuacion():
    assert _PATRON_PEDIDO_PENDIENTE.search("Ahora paso a la SOLICITUD 2 — Factura nueva")
    assert _PATRON_PEDIDO_PENDIENTE.search("Sigo con la otra factura")


def test_patron_pedido_pendiente_no_dispara_en_confirmacion_normal():
    texto = "¡Listo! Ya tengo todos los datos. En un momento te mando el total calculado (con impuestos) para que lo confirmes antes de timbrar. 📊"
    assert not _PATRON_PEDIDO_PENDIENTE.search(texto)
