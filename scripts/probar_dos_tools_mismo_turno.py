"""
Prueba: REP de una factura YA EXISTENTE + factura nueva, en el mismo
mensaje. A diferencia del caso anterior, aca AMBAS acciones son
legitimas/posibles en el mismo turno -- si Claude intenta llamar
generate_invoice_draft Y generate_rep_draft a la vez, corre el bug
conocido: run_conversation_turn solo procesa el primer tool_use
(TODOS.md: "Claude tool use paralelo").
"""
import sheets_client
from claude_client import run_conversation_turn

CANAL_ID_PRUEBA = "8208800448"


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    history = []
    texto = (
        "Hola, dos cosas: primero, te aviso que ya pagaron la factura con folio fiscal "
        "c8171e7e-283a-4ac7-bf7f-5584051e5a9d, pago de $5652.14 hoy a la 1:45pm por transferencia (03), "
        "cliente Distribuidora del Valle SA de CV RFC DVA010101AA1 regimen 601 CP 06600. "
        "Y ademas necesito una factura NUEVA para otro cliente: Comercial Rio SA de CV, "
        "RFC CRI010101AA1, regimen 601, CP 06600, concepto: 1 servicio de flete a $500, "
        "uso CFDI G03, PUE, forma de pago 03."
    )
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

    # Confirmar y ver si el turno SIGUIENTE crashea (por un tool_use sin
    # tool_result colgado en el historial si Claude llamo 2 tools)
    print("\n--- Turno 2: confirmar ---")
    texto2 = "Si, confirmo los datos que ya tengas."
    print(f"Cliente: {texto2}")
    try:
        client_message, invoice_draft, rep_draft = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, user_text=texto2,
        )
    except Exception as exc:
        print(f"!!! EXCEPCION EN TURNO 2 (bug de tool_use huerfano): {type(exc).__name__}: {exc}")
        return
    print(f"Bot: {client_message}")
    print(f"invoice_draft={'SI' if invoice_draft else None} rep_draft={'SI' if rep_draft else None}")


if __name__ == "__main__":
    main()
