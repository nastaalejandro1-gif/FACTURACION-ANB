"""Verifica que un turno NUEVO despues de un tool_use sin 'final_response'
(dos mensajes user seguidos: tool_result + mensaje real del cliente) no
rompa la conversacion -- validacion critica del cambio que elimina la
segunda llamada a la API."""
import sheets_client
from claude_client import run_conversation_turn

CANAL_ID_PRUEBA = "8208800448"
CHAT_ID_FALSO = "999999998"


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    pasos = [
        "Mi cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, regimen 601, CP 06600.",
        "Si, correcto.",
        "Los conceptos son: 20 bolsas de botana a $45 c/u, y el flete que cuesta $200. Uso CFDI G03, PUE, forma de pago 03.",
        "Si, confirmo.",  # <- aqui se genera el tool_use, sin final_response ahora
        "Oye, tambien necesito facturar a otro cliente despues, te aviso en un rato.",  # <- turno EXTRA post-tool
    ]

    for i, texto in enumerate(pasos, 1):
        history = sheets_client.load_history(CHAT_ID_FALSO)
        try:
            msg, inv, rep = run_conversation_turn(
                profile=profile, catalogo=catalogo, history=history, user_text=texto,
            )
        except Exception as exc:
            print(f"!!! EXCEPCION en turno {i}: {type(exc).__name__}: {exc}")
            return
        sheets_client.save_history("telegram", CHAT_ID_FALSO, history)
        print(f"Turno {i} OK. draft={'SI' if inv else None}")
        print(f"  Bot: {msg[:150]}")

    sb = sheets_client._get_supabase()
    sb.table("conversaciones").delete().eq("canal_id", CHAT_ID_FALSO).execute()
    print("\n(fila de prueba borrada)")


if __name__ == "__main__":
    main()
