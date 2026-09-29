"""
Prueba manual contra Claude real (sin tocar Telegram ni FacturAPI):
simula una conversación de facturación con un cliente real de Supabase y
corre fiscal_engine sobre el resultado. Ver TODOS.md "Probar contra
Claude real".

Uso: python scripts/probar_conversacion.py
"""
from decimal import Decimal

import sheets_client
from claude_client import run_conversation_turn
import fiscal_engine

CANAL_ID_PRUEBA = "8208800448"  # Sin Culpa — tiene IEPS, buen caso de prueba


def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    if not profile:
        print("No encontré al cliente de prueba.")
        return
    print(f"Cliente: {profile.nombre_comercial} (id_cliente={profile.id_cliente})\n")

    catalogo = sheets_client.get_catalogo_claves(profile.despacho_id, profile.id_cliente)
    print("Catálogo de claves:")
    for c in catalogo:
        print(f"  {c.clave_prod_serv} = {c.descripcion_clave} (IEPS: {c.aplica_ieps}, cliente: {c.id_cliente})")
    print()

    history = []

    mensajes_cliente = [
        "Hola, necesito facturar. Mi cliente es Distribuidora del Valle SA de CV, "
        "RFC DVA010101AA1, régimen fiscal 601, código postal 06600.",
        "Sí, esos datos son correctos.",
        "Los conceptos son: 20 bolsas de botana a $45 c/u, y el flete que cuesta $200. "
        "Uso CFDI G03, método de pago PUE, forma de pago transferencia (03).",
        "Sí, confirmo, está todo correcto.",
    ]

    invoice_draft = None
    for i, texto in enumerate(mensajes_cliente, 1):
        print(f"--- Turno {i} ---")
        print(f"Cliente: {texto}")
        client_message, invoice_draft, rep_draft = run_conversation_turn(
            profile=profile, catalogo=catalogo, history=history, user_text=texto,
        )
        print(f"Bot: {client_message}\n")
        if invoice_draft:
            print(">>> invoice_draft generado, deteniendo conversación simulada.\n")
            break

    if not invoice_draft:
        print("No se generó ningún invoice_draft en los turnos simulados.")
        return

    print("=== InvoiceDraft crudo ===")
    print(invoice_draft.model_dump_json(indent=2))
    print()

    reglas = sheets_client.get_fiscal_rules(profile.despacho_id, profile.id_cliente)
    print(f"=== FiscalRules ===\n{reglas}\n")

    conceptos_extraidos = [
        fiscal_engine.ConceptoExtraido(
            descripcion=c.descripcion,
            cantidad=Decimal(str(c.cantidad)),
            precio_unitario=Decimal(str(c.precio_unitario)),
            clave_unidad=c.clave_unidad,
            clave_prod_serv=c.clave_prod_serv,
        )
        for c in invoice_draft.factura.conceptos
    ]
    total_fuente = (
        Decimal(str(invoice_draft.factura.total_documento_fuente))
        if invoice_draft.factura.total_documento_fuente is not None else None
    )

    resultado = fiscal_engine.calcular_factura(
        conceptos_extraidos, invoice_draft.receptor, reglas,
        invoice_draft.factura.metodo_pago, invoice_draft.factura.forma_pago,
        total_documento_fuente=total_fuente,
    )

    if resultado.escalation:
        print(f"=== ESCALAMIENTO ===\n{resultado.escalation.reason.value}: {resultado.escalation.detail}")
        return

    f = resultado.factura
    print("=== Factura calculada ===")
    for c in f.conceptos:
        print(f"  {c.descripcion}: {c.cantidad} x ${c.precio_unitario} = ${c.importe} (IEPS: ${c.ieps} @ {c.ieps_tasa})")
    print(f"Subtotal: ${f.subtotal}")
    print(f"IEPS: ${f.ieps}")
    print(f"IVA ({f.tasa_iva}): ${f.iva}")
    print(f"Retención IVA ({f.retencion_iva_tasa}): ${f.retencion_iva}")
    print(f"Retención ISR ({f.retencion_isr_tasa}): ${f.retencion_isr}")
    print(f"TOTAL: ${f.total}")
    print(f"Forma de pago final: {f.forma_pago}")


if __name__ == "__main__":
    main()
