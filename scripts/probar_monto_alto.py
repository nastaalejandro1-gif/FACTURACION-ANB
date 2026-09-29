"""Prueba en vivo: factura de mas de $100,000 debe escalar a ANB en vez de
ir directo a confirmacion del cliente."""
import asyncio
import sheets_client
import main
from claude_client import run_conversation_turn


async def probar():
    canal_id_falso = "900000077"
    profile = sheets_client.get_client_by_canal_id("telegram", "8208800448")
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    history = []
    mensajes = [
        "Mi cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, regimen 601, CP 06600.",
        "Si, correcto.",
        "1000 bolsas de botana a $150 cada una. Uso CFDI G03, PUE, forma de pago 03.",
        "Si, confirmo.",
    ]
    invoice_draft = None
    for texto in mensajes:
        msg, invoice_draft, _ = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, user_text=texto,
        )
        print(f"Bot: {msg[:150]}")
        if invoice_draft:
            break

    if not invoice_draft:
        print("No se genero draft.")
        return

    # Mockear envios de Telegram para ver que mensaje le llegaria a ANB
    enviados = []
    async def fake_send(chat_id, text, reply_markup=None):
        enviados.append((chat_id, text))
    main.telegram_client.send_message = fake_send

    saved_pending = []
    def fake_save_pending(**kw):
        saved_pending.append(kw)
    main.sheets_client.save_pending = fake_save_pending
    main.sheets_client.log_to_bitacora = lambda **kw: None

    invoice_id = "test-monto-alto-1"
    await main._calcular_y_procesar_factura(invoice_id, invoice_draft, profile, canal_id_falso, 1)

    print(f"\nsaved_pending: {len(saved_pending)}")
    if saved_pending:
        print("tipo_aprobacion:", saved_pending[0]["tipo_aprobacion"])
        print("motivo_revision:", saved_pending[0]["motivo_revision"])

    print("\nMensajes que se hubieran mandado:")
    for chat_id, text in enviados:
        print(f"  -> chat_id={chat_id}: {text[:200]}")


if __name__ == "__main__":
    asyncio.run(probar())
