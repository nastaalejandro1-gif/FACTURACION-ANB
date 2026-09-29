"""
Simula el flujo de produccion REAL: cada turno pasa por save_history/
load_history entre medio (como pasa entre mensajes de Telegram separados),
en vez de reusar el mismo objeto history en memoria. El intento anterior
(medir_tokens_completo.py) no pasaba por ese ciclo y sobreestimaba el
costo porque no se despojaba el PDF entre turnos.
"""
import sheets_client
from claude_client import run_conversation_turn, client

CANAL_ID_PRUEBA = "8208800448"
CHAT_ID_FALSO = "999999999"  # no es un cliente real, solo para medir

_original_create = client.messages.create
llamadas = []

def _create_con_medicion(*args, **kwargs):
    response = _original_create(*args, **kwargs)
    llamadas.append(response.usage)
    return response

client.messages.create = _create_con_medicion


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    with open("scripts/_csf_prueba.pdf", "rb") as f:
        csf_bytes = f.read()
    with open("scripts/_cotizacion_prueba.pdf", "rb") as f:
        cotizacion_bytes = f.read()

    pasos = [
        dict(file_bytes=csf_bytes, media_type="application/pdf"),
        dict(user_text="Sí, correcto."),
        dict(file_bytes=cotizacion_bytes, media_type="application/pdf",
             user_text="Aquí está la cotización. PPD, uso CFDI G03, forma de pago 03."),
        dict(user_text="Sí, confirmo todo."),
    ]

    for i, kwargs in enumerate(pasos, 1):
        history = sheets_client.load_history(CHAT_ID_FALSO)  # <- fresco, como en produccion
        client_message, invoice_draft, rep_draft = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, **kwargs,
        )
        sheets_client.save_history("telegram", CHAT_ID_FALSO, history)  # <- despoja binarios
        print(f"Turno {i} listo. draft={'SI' if invoice_draft else None}")

    print("\n=== Uso de tokens por llamada ===")
    total_input = total_output = total_cache_read = total_cache_write = 0
    for i, u in enumerate(llamadas, 1):
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        print(f"Llamada {i}: input={u.input_tokens} output={u.output_tokens} "
              f"cache_read={cache_read} cache_write={cache_write}")
        total_input += u.input_tokens; total_output += u.output_tokens
        total_cache_read += cache_read; total_cache_write += cache_write

    precio_input, precio_output, precio_cache_write, precio_cache_read = 3.0, 15.0, 3.75, 0.30
    costo = (total_input*precio_input + total_output*precio_output
             + total_cache_write*precio_cache_write + total_cache_read*precio_cache_read) / 1_000_000
    print(f"\nTotales: input={total_input} output={total_output} "
          f"cache_read={total_cache_read} cache_write={total_cache_write}")
    print(f"Costo estimado: ${costo:.4f} USD")

    # limpieza
    sb = sheets_client._get_supabase()
    sb.table("conversaciones").delete().eq("canal_id", CHAT_ID_FALSO).execute()
    print("(fila de prueba borrada de conversaciones)")


if __name__ == "__main__":
    main()
