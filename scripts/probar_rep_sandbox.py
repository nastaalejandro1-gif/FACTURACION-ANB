"""
Prueba manual de REP: conversacion real con Claude reportando el pago de
una factura PPD ya timbrada en sandbox, y timbrado real del complemento
de pago. Ver TODOS.md "Probar REP contra el sandbox real".

Uso: python scripts/probar_rep_sandbox.py <uuid_factura_origen>
"""
import asyncio
import sys
from decimal import Decimal

import sheets_client
import fiscal_engine
from claude_client import run_conversation_turn
from facturapi_client import create_rep, download_xml, search_invoice_by_uuid
from models import RepData

CANAL_ID_PRUEBA = "8208800448"  # Sin Culpa


async def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/probar_rep_sandbox.py <uuid_factura_origen>")
        return
    uuid_origen = sys.argv[1]

    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)

    history = []
    mensajes = [
        f"Te aviso que ya pagaron la factura con folio fiscal {uuid_origen}. "
        f"El cliente es Distribuidora del Valle SA de CV, RFC DVA010101AA1, régimen 601, CP 06600. "
        f"Pagó $3391.28 el 2026-09-29 por transferencia (03).",
        "Sí, confirmo.",
    ]

    rep_draft = None
    for texto in mensajes:
        print(f"Cliente: {texto}")
        client_message, invoice_draft, rep_draft = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, user_text=texto,
        )
        print(f"Bot: {client_message}\n")
        if rep_draft:
            break

    if not rep_draft:
        print("No se generó rep_draft.")
        return

    print("=== RepDraft ===")
    print(rep_draft.model_dump_json(indent=2))

    original_invoice = await search_invoice_by_uuid(rep_draft.uuid_factura_origen, profile.facturapi_key)
    if not original_invoice:
        print(f"No se encontró la factura original {rep_draft.uuid_factura_origen} en FacturAPI.")
        return

    invoice_total = Decimal(str(original_invoice.get("total", 0)))
    previous_reps = sheets_client.get_rep_history(rep_draft.uuid_factura_origen)
    if previous_reps:
        imp_saldo_ant = Decimal(str(previous_reps[-1].get("imp_saldo_insoluto") or 0))
        num_parcialidad = len(previous_reps) + 1
    else:
        imp_saldo_ant = invoice_total
        num_parcialidad = 1

    print(f"\ninvoice_total={invoice_total} imp_saldo_ant={imp_saldo_ant} num_parcialidad={num_parcialidad}")

    resultado = fiscal_engine.calcular_rep(
        monto_pagado=Decimal(str(rep_draft.monto_pagado)), imp_saldo_ant=imp_saldo_ant,
    )
    if resultado.escalation:
        print(f"ESCALAMIENTO: {resultado.escalation}")
        return

    rep_data = RepData(
        estatus="confirmado_por_cliente", uuid_factura_origen=rep_draft.uuid_factura_origen,
        receptor=rep_draft.receptor, fecha_pago=rep_draft.fecha_pago, forma_pago=rep_draft.forma_pago,
        monto_pagado=resultado.rep.monto_pagado, imp_saldo_ant=resultado.rep.imp_saldo_ant,
        imp_saldo_insoluto=resultado.rep.imp_saldo_insoluto,
    )
    print(f"\nCalculado: monto_pagado={rep_data.monto_pagado} imp_saldo_ant={rep_data.imp_saldo_ant} "
          f"imp_saldo_insoluto={rep_data.imp_saldo_insoluto}")

    print("\nTimbrando REP en sandbox...")
    result = await create_rep(rep_data, profile.facturapi_key, num_parcialidad, original_invoice)
    folio = result.get("id", "")
    print(f"Timbrado OK. folio={folio}")

    xml = await download_xml(folio, profile.facturapi_key)
    xml_text = xml.decode("utf-8", errors="replace")
    import re
    for m in re.finditer(r"<(?:pago20:)?DoctoRelacionado[^>]*/?>", xml_text):
        print(" ", m.group(0))


if __name__ == "__main__":
    asyncio.run(main())
