"""
Mide el impacto real de que el historial de un cliente NUNCA se reinicie
entre facturas/REPs completados. Toma el historial REAL de Envoy (102
mensajes, guardado en Supabase) hasta justo antes de su última factura+REP
("nueva factura" en adelante), y corre esa MISMA secuencia de turnos dos
veces: (A) con los 80 mensajes previos reales como contexto -- lo que
pasó de verdad en producción -- y (B) desde cero (history=[]) -- una
conversación limpia. La diferencia de costo es el impacto puro de la
acumulación del hilo.

Los documentos ya vienen despojados en el historial real guardado (solo
queda el placeholder de texto) -- para los 2 turnos que en la vida real
llevaban un PDF (CSF y cotización), se usa un PDF sintético de prueba en
ambas corridas, así que esa parte del costo es idéntica en A y B; la
diferencia medida es puramente el efecto del contexto acumulado.
"""
import copy
import json
import logging
import re

import sheets_client
from claude_client import run_conversation_turn

CANAL_ID_ENVOY_REAL = "7963818260"

registros = []


class CapturaHandler(logging.Handler):
    def __init__(self, etiqueta_ref):
        super().__init__()
        self.etiqueta_ref = etiqueta_ref

    def emit(self, record):
        msg = record.getMessage()
        m = re.search(
            r"claude_usage paso=(?P<paso>\S+) modelo=\S+ "
            r"input_tokens=(?P<input>\d+) output_tokens=(?P<output>\d+) "
            r"cache_read_tokens=(?P<cache_read>\d+) cache_write_tokens=(?P<cache_write>\d+) "
            r"costo_usd=(?P<costo>[\d.]+)",
            msg,
        )
        if m:
            registros.append((
                self.etiqueta_ref[0], m["paso"], int(m["input"]), int(m["output"]),
                int(m["cache_read"]), int(m["cache_write"]), float(m["costo"]),
            ))


etiqueta_actual = [""]
logging.getLogger("claude_client").addHandler(CapturaHandler(etiqueta_actual))
logging.getLogger("claude_client").setLevel(logging.INFO)


def correr_secuencia(etiqueta, history_inicial):
    etiqueta_actual[0] = etiqueta
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_ENVOY_REAL)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    with open("scripts/_csf_prueba.pdf", "rb") as f:
        csf_pdf = f.read()
    with open("scripts/_cotizacion_prueba.pdf", "rb") as f:
        cot_pdf = f.read()

    history = copy.deepcopy(history_inicial)
    turnos = [
        dict(user_text="nueva factura"),
        dict(file_bytes=csf_pdf, media_type="application/pdf"),
        dict(user_text="si"),
        dict(file_bytes=cot_pdf, media_type="application/pdf", user_text="ppd g03"),
        dict(user_text="si"),
    ]
    for kwargs in turnos:
        run_conversation_turn(profile=profile, catalogo=catalogo, history=history, **kwargs)
    print(f"'{etiqueta}' completa. Mensajes finales en el historial: {len(history)}")


def main():
    row = sheets_client._get_supabase().table("conversaciones").select("historial").eq(
        "canal_id", CANAL_ID_ENVOY_REAL
    ).execute().data[0]
    historia_completa = json.loads(row["historial"])
    print(f"Historial real total: {len(historia_completa)} mensajes")

    # Punto de corte: justo antes de "nueva factura" (indice 82 en la
    # inspeccion manual -- buscar por seguridad en vez de hardcodear).
    corte = None
    for i, msg in enumerate(historia_completa):
        content = msg.get("content")
        if isinstance(content, str) and content.strip().lower() == "nueva factura":
            corte = i
    if corte is None:
        print("No se encontro el punto de corte 'nueva factura' -- abortando.")
        return
    historia_previa_real = historia_completa[:corte]
    print(f"Contexto previo real: {len(historia_previa_real)} mensajes (hasta justo antes de 'nueva factura')\n")

    correr_secuencia("A. CON historial real acumulado (80 msgs previos)", historia_previa_real)
    print()
    correr_secuencia("B. Desde cero (conversacion limpia)", [])

    print("\n=== Costo por turno ===")
    print(f"{'Etiqueta':<48} {'Paso':<38} {'In':>6} {'Out':>6} {'CRead':>7} {'CWrite':>7} {'Costo':>9}")
    totales = {}
    for etq, paso, inp, out, cread, cwrite, costo in registros:
        print(f"{etq:<48} {paso:<38} {inp:>6} {out:>6} {cread:>7} {cwrite:>7} {costo:>9.5f}")
        totales[etq] = totales.get(etq, 0.0) + costo

    print("\n=== Totales ===")
    for etq, total in totales.items():
        print(f"{etq:<50} ${total:.4f}")

    valores = list(totales.values())
    if len(valores) == 2:
        print(f"\nDiferencia (impacto del historial acumulado): ${valores[0]-valores[1]:.4f}")


if __name__ == "__main__":
    main()
