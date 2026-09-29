"""
Prueba: ¿qué pasa si el cliente pide "factura nueva Y genera un REP" en el
mismo mensaje? Simula el patrón real que describió el usuario -- cotización
+ pedido de REP juntos en un solo texto. Ver si Claude intenta llamar 2
tools en el mismo turno (bug conocido: run_conversation_turn solo procesa
el primer tool_use que encuentra).
"""
import sheets_client
from claude_client import run_conversation_turn

CANAL_ID_PRUEBA = "8208800448"  # Sin Culpa


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    history = []
    mensajes = [
        "Mi cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, régimen 601, CP 06600.",
        "Sí, correcto.",
        "Los conceptos son: 1 servicio de consultoría a $5000. Uso CFDI G03, método de pago PPD. "
        "Y de una vez genera un REP de esta factura por un pago de $10,302.55 con fecha de hoy "
        "01:45 PM, ya me pagaron por transferencia.",
    ]

    for i, texto in enumerate(mensajes, 1):
        print(f"--- Turno {i} ---")
        print(f"Cliente: {texto}")
        try:
            client_message, invoice_draft, rep_draft = run_conversation_turn(
                profile=profile, catalogo=catalogo, history=history, user_text=texto,
            )
        except Exception as exc:
            print(f"!!! EXCEPCION: {type(exc).__name__}: {exc}")
            return
        print(f"Bot: {client_message}\n")
        print(f"invoice_draft={'SI' if invoice_draft else None} rep_draft={'SI' if rep_draft else None}")
        if invoice_draft or rep_draft:
            break

    # Turno extra: si Claude no generó nada todavía, confirmar
    if not invoice_draft and not rep_draft:
        print("\n--- Turno extra: confirmar ---")
        texto = "Sí, confirmo todo."
        print(f"Cliente: {texto}")
        try:
            client_message, invoice_draft, rep_draft = run_conversation_turn(
                profile=profile, catalogo=catalogo, history=history, user_text=texto,
            )
        except Exception as exc:
            print(f"!!! EXCEPCION EN SIGUIENTE TURNO (esto es el bug de tool_use sin tool_result): {type(exc).__name__}: {exc}")
            return
        print(f"Bot: {client_message}")
        print(f"invoice_draft={'SI' if invoice_draft else None} rep_draft={'SI' if rep_draft else None}")


if __name__ == "__main__":
    main()
