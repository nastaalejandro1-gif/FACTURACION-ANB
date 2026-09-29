"""Prueba manual: concepto fuera del catálogo del cliente -> debe usar 'NUEVA'."""
import sheets_client
from claude_client import run_conversation_turn

CANAL_ID_PRUEBA = "8208800448"  # Sin Culpa


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    history = []
    mensajes = [
        "Mi cliente es Comercializadora Norte SA de CV, RFC CNO010101AA1, régimen 601, CP 06600.",
        "Sí, correcto.",
        "El concepto es: servicio de instalación de anaqueles industriales, $3500. "
        "Uso CFDI G03, PUE, forma de pago 03.",
        "Sí, confirmo.",
    ]

    invoice_draft = None
    for texto in mensajes:
        print(f"Cliente: {texto}")
        client_message, invoice_draft, _ = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, user_text=texto,
        )
        print(f"Bot: {client_message}\n")
        if invoice_draft:
            break

    if invoice_draft:
        print("=== Conceptos ===")
        for c in invoice_draft.factura.conceptos:
            print(f"  clave_prod_serv={c.clave_prod_serv!r} propuesta={c.clave_prod_serv_propuesta!r} desc={c.descripcion!r}")


if __name__ == "__main__":
    main()
