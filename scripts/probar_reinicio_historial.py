"""
Verifica que despues de completar una factura, el historial se reinicia y
la SIGUIENTE factura del mismo cliente en el mismo chat arranca limpia
(no acumula la anterior) -- confirma el fix de history.clear().
"""
import logging
import re

import sheets_client
from claude_client import run_conversation_turn

CANAL_ID = "8208800448"
CHAT_ID_FALSO = "900000099"

registros = []


class CapturaHandler(logging.Handler):
    def emit(self, record):
        m = re.search(
            r"claude_usage paso=(?P<paso>\S+) modelo=\S+ "
            r"input_tokens=(?P<input>\d+) output_tokens=(?P<output>\d+) "
            r"cache_read_tokens=(?P<cache_read>\d+) cache_write_tokens=(?P<cache_write>\d+) "
            r"costo_usd=(?P<costo>[\d.]+)",
            record.getMessage(),
        )
        if m:
            registros.append((m["paso"], int(m["input"]), float(m["costo"])))


logging.getLogger("claude_client").addHandler(CapturaHandler())
logging.getLogger("claude_client").setLevel(logging.INFO)


def correr_factura(etiqueta, cliente_nombre):
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)
    turnos = [
        dict(user_text=f"Mi cliente es {cliente_nombre} SA de CV, RFC ABC010101AA1, regimen 601, CP 06600."),
        dict(user_text="Si, correcto."),
        dict(user_text="20 bolsas de botana a $45, flete $200. Uso CFDI G03, PUE, forma 03."),
        dict(user_text="Si, confirmo."),
    ]
    print(f"--- {etiqueta} ---")
    for kwargs in turnos:
        history = sheets_client.load_history(CHAT_ID_FALSO)
        print(f"  (historial cargado: {len(history)} mensajes)")
        run_conversation_turn(profile=profile, catalogo=catalogo, history=history, **kwargs)
        sheets_client.save_history("telegram", CHAT_ID_FALSO, history)
    print()


def main():
    correr_factura("Factura #1 (cliente A)", "Cliente Uno")
    correr_factura("Factura #2 (cliente B, MISMO chat)", "Cliente Dos")

    print("=== Costo por llamada ===")
    for paso, inp, costo in registros:
        print(f"  paso={paso:<40} input={inp:>6} costo=${costo:.5f}")

    sb = sheets_client._get_supabase()
    sb.table("conversaciones").delete().eq("canal_id", CHAT_ID_FALSO).execute()
    print("\n(fila de prueba borrada)")


if __name__ == "__main__":
    main()
