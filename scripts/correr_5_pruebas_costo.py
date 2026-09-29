"""
Corre 5 conversaciones de factura de prueba (mezcla de texto y PDF) y
captura el logging real de claude_client._log_uso_claude para armar una
tabla de costo por paso. Ver TODOS.md / pedido del usuario:
"dame una tabla del costo por paso para ver donde se van los 30 centavos".
"""
import logging
import re

import sheets_client
from claude_client import run_conversation_turn

CANAL_ID = "8208800448"  # Sin Culpa

registros = []  # (prueba, paso, modelo, input, output, cache_read, cache_write, costo)

PATRON = re.compile(
    r"claude_usage paso=(?P<paso>\S+) modelo=(?P<modelo>\S+) "
    r"input_tokens=(?P<input>\d+) output_tokens=(?P<output>\d+) "
    r"cache_read_tokens=(?P<cache_read>\d+) cache_write_tokens=(?P<cache_write>\d+) "
    r"costo_usd=(?P<costo>[\d.]+)"
)


class CapturaHandler(logging.Handler):
    def __init__(self, prueba_actual_ref):
        super().__init__()
        self.prueba_actual_ref = prueba_actual_ref

    def emit(self, record):
        msg = record.getMessage()
        m = PATRON.search(msg)
        if m:
            registros.append((
                self.prueba_actual_ref[0], m["paso"], m["modelo"],
                int(m["input"]), int(m["output"]), int(m["cache_read"]),
                int(m["cache_write"]), float(m["costo"]),
            ))


prueba_actual = [""]
handler = CapturaHandler(prueba_actual)
logging.getLogger("claude_client").addHandler(handler)
logging.getLogger("claude_client").setLevel(logging.INFO)


def correr_prueba(nombre, pasos, chat_id_falso):
    prueba_actual[0] = nombre
    for kwargs in pasos:
        history = sheets_client.load_history(chat_id_falso)
        profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID)
        catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)
        run_conversation_turn(profile=profile, catalogo=catalogo, history=history, **kwargs)
        sheets_client.save_history("telegram", chat_id_falso, history)
    sb = sheets_client._get_supabase()
    sb.table("conversaciones").delete().eq("canal_id", chat_id_falso).execute()
    print(f"'{nombre}' completa.")


def main():
    with open("scripts/_csf_prueba.pdf", "rb") as f:
        csf_pdf = f.read()
    with open("scripts/_cotizacion_prueba.pdf", "rb") as f:
        cot_pdf = f.read()

    # Prueba 1: todo texto, factura simple (2 conceptos)
    correr_prueba("1. Texto puro (2 conceptos)", [
        dict(user_text="Mi cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, "
                        "regimen 601, CP 06600."),
        dict(user_text="Si, correcto."),
        dict(user_text="20 bolsas de botana a $45, flete $200. Uso CFDI G03, PUE, forma 03."),
        dict(user_text="Si, confirmo."),
    ], "900000001")

    # Prueba 2: PDF CSF + texto cotizacion
    correr_prueba("2. PDF CSF + texto cotizacion", [
        dict(file_bytes=csf_pdf, media_type="application/pdf"),
        dict(user_text="Si, correcto."),
        dict(user_text="20 bolsas de botana a $45, flete $200. Uso CFDI G03, PUE, forma 03."),
        dict(user_text="Si, confirmo."),
    ], "900000002")

    # Prueba 3: texto CSF + PDF cotizacion
    correr_prueba("3. Texto CSF + PDF cotizacion", [
        dict(user_text="Mi cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, "
                        "regimen 601, CP 06600."),
        dict(user_text="Si, correcto."),
        dict(file_bytes=cot_pdf, media_type="application/pdf",
             user_text="Aqui esta la cotizacion. PPD, uso CFDI G03, forma de pago 03."),
        dict(user_text="Si, confirmo."),
    ], "900000003")

    # Prueba 4: PDF CSF + PDF cotizacion (flujo completo, como el caso real)
    correr_prueba("4. PDF CSF + PDF cotizacion (flujo real)", [
        dict(file_bytes=csf_pdf, media_type="application/pdf"),
        dict(user_text="Si, correcto."),
        dict(file_bytes=cot_pdf, media_type="application/pdf",
             user_text="Aqui esta la cotizacion. PPD, uso CFDI G03, forma de pago 03."),
        dict(user_text="Si, confirmo."),
    ], "900000004")

    # Prueba 5: texto puro con MAS conceptos (7, como Loft Fitness) para ver
    # si el volumen de la respuesta pesa
    correr_prueba("5. Texto puro (7 conceptos)", [
        dict(user_text="Mi cliente es Loft Fitness, RFC LFI2406114F6, regimen 601, CP 98619."),
        dict(user_text="Si, correcto."),
        dict(user_text=(
            "Los conceptos son: 40 bolsas Jooga a $255.50, 10 maletas Ibawi a $259.00, "
            "10 maletas Parma a $321.00, 10 maletas Sicilia a $234.50, 10 maletas Smatch a "
            "$248.00, 100 cilindros shaker a $63.50, y envio de $1200.00. "
            "Uso CFDI G03, PPD."
        )),
        dict(user_text="Si, confirmo."),
    ], "900000005")

    print("\n=== Tabla de costo por llamada ===")
    print(f"{'Prueba':<38} {'Paso':<45} {'In':>6} {'Out':>6} {'CRead':>7} {'CWrite':>7} {'Costo USD':>10}")
    totales_por_prueba = {}
    for prueba, paso, modelo, inp, out, cread, cwrite, costo in registros:
        print(f"{prueba:<38} {paso:<45} {inp:>6} {out:>6} {cread:>7} {cwrite:>7} {costo:>10.5f}")
        totales_por_prueba[prueba] = totales_por_prueba.get(prueba, 0.0) + costo

    print("\n=== Total por prueba ===")
    for prueba, total in totales_por_prueba.items():
        print(f"{prueba:<45} ${total:.4f}")
    print(f"\nPromedio: ${sum(totales_por_prueba.values())/len(totales_por_prueba):.4f}")


if __name__ == "__main__":
    main()
