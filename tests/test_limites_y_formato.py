"""
Límites por chat, tamaño de archivo y formato de los mensajes de Telegram.
"""
import pytest

import main
import telegram_client


# ---------------------------------------------------------------------------
# Markdown de Claude -> HTML de Telegram
# ---------------------------------------------------------------------------

def test_markdown_a_html_convierte_negritas_y_vinetas():
    texto = "**Receptor:** Zepeda\n* Concepto 1\n- Concepto 2\n`F-123`"
    assert telegram_client.markdown_a_html(texto) == (
        "<b>Receptor:</b> Zepeda\n• Concepto 1\n• Concepto 2\n<code>F-123</code>"
    )


def test_markdown_a_html_escapa_lo_que_parece_html():
    assert telegram_client.markdown_a_html("Total < $5,000 & IVA") == "Total &lt; $5,000 &amp; IVA"


def test_monto_con_asteriscos_sueltos_no_se_rompe():
    assert telegram_client.markdown_a_html("2 * 3 = 6") == "2 * 3 = 6"


# ---------------------------------------------------------------------------
# Mensajes de más de 4096 caracteres
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mensaje_largo_se_parte_y_los_botones_van_al_final(monkeypatch):
    enviados = []

    async def fake_send_one(chat_id, text, reply_markup=None):
        enviados.append((text, reply_markup))
    monkeypatch.setattr(telegram_client, "_send_one", fake_send_one)

    parrafo = "x" * 3000
    await telegram_client.send_message(1, f"{parrafo}\n\n{parrafo}\n\n{parrafo}", reply_markup={"k": 1})

    assert len(enviados) == 3
    assert all(len(t) <= telegram_client.MAX_CARACTERES for t, _ in enviados)
    assert [m for _, m in enviados] == [None, None, {"k": 1}]


def test_texto_sin_saltos_se_corta_en_el_limite():
    partes = telegram_client._partir("y" * 9000)
    assert [len(p) for p in partes] == [4096, 4096, 808]


# ---------------------------------------------------------------------------
# Límite de mensajes por chat
# ---------------------------------------------------------------------------

def test_limite_por_chat_avisa_una_sola_vez(monkeypatch):
    monkeypatch.setattr(main, "_mensajes_recientes", main.defaultdict(main.deque))
    monkeypatch.setattr(main, "_ultimo_aviso_limite", {})
    resultados = [main._excede_limite("555") for _ in range(main.LIMITE_MENSAJES + 3)]
    assert resultados[: main.LIMITE_MENSAJES] == [(False, False)] * main.LIMITE_MENSAJES
    assert resultados[main.LIMITE_MENSAJES:] == [(True, True), (True, False), (True, False)]


def test_alejandro_no_tiene_limite(monkeypatch):
    monkeypatch.setattr(main, "_mensajes_recientes", main.defaultdict(main.deque))
    alejandro = str(main.ALEJANDRO_CHAT_ID)
    assert all(main._excede_limite(alejandro) == (False, False) for _ in range(main.LIMITE_MENSAJES * 2))


def test_otro_chat_no_se_ve_afectado(monkeypatch):
    monkeypatch.setattr(main, "_mensajes_recientes", main.defaultdict(main.deque))
    monkeypatch.setattr(main, "_ultimo_aviso_limite", {})
    for _ in range(main.LIMITE_MENSAJES + 1):
        main._excede_limite("555")
    assert main._excede_limite("777") == (False, False)
