"""
Timbra en el SANDBOX de FacturAPI (no toca SAT real) el resultado del test
de probar_conversacion.py, para validar que el payload calculado por
fiscal_engine es aceptado y que el redondeo coincide con lo que FacturAPI
calcula internamente. Ver TODOS.md "Verificar redondeo contra el Anexo 20".

Uso: python scripts/probar_timbrado_sandbox.py
"""
import asyncio
from decimal import Decimal

import sheets_client
import fiscal_engine
from facturapi_client import create_invoice, download_xml
from models import ConceptoItem, EmisorData, FacturaData, InvoiceData, ReceptorData

CANAL_ID_PRUEBA = "8208800448"  # Sin Culpa


async def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID_PRUEBA)
    reglas = sheets_client.get_fiscal_rules(profile.despacho_id, profile.id_cliente)

    receptor = ReceptorData(
        razon_social="Distribuidora del Valle SA de CV",
        rfc="DVA010101AA1", regimen_fiscal="601", cp_fiscal="06600", uso_cfdi="G03",
    )
    conceptos_extraidos = [
        fiscal_engine.ConceptoExtraido(
            descripcion="Bolsas de botana", cantidad=Decimal("20"),
            precio_unitario=Decimal("45"), clave_unidad="H87", clave_prod_serv="50192100",
        ),
        fiscal_engine.ConceptoExtraido(
            descripcion="Flete", cantidad=Decimal("1"),
            precio_unitario=Decimal("200"), clave_unidad="E48", clave_prod_serv="78101800",
        ),
    ]
    resultado = fiscal_engine.calcular_factura(
        conceptos_extraidos, receptor, reglas, metodo_pago="PUE", forma_pago="03",
    )
    if resultado.escalation:
        print(f"ESCALAMIENTO: {resultado.escalation}")
        return

    calculada = resultado.factura
    print(f"Calculado localmente: subtotal={calculada.subtotal} ieps={calculada.ieps} "
          f"iva={calculada.iva} ret_iva={calculada.retencion_iva} ret_isr={calculada.retencion_isr} "
          f"total={calculada.total}")

    emisor = EmisorData(
        nombre_comercial=profile.nombre_comercial, razon_social=profile.razon_social,
        rfc=profile.rfc, regimen_fiscal=profile.regimen_fiscal, cp_fiscal=profile.cp_fiscal,
    )
    conceptos = [
        ConceptoItem(
            descripcion=cc.descripcion, clave_prod_serv=cc.clave_prod_serv,
            cantidad=cc.cantidad, clave_unidad=cc.clave_unidad,
            precio_unitario=cc.precio_unitario, ieps=cc.ieps, ieps_tasa=cc.ieps_tasa,
        )
        for cc in calculada.conceptos
    ]
    factura = FacturaData(
        conceptos=conceptos, monto_antes_impuestos=calculada.subtotal, ieps=calculada.ieps,
        iva=calculada.iva, tasa_iva=calculada.tasa_iva,
        retencion_iva=calculada.retencion_iva, retencion_iva_tasa=calculada.retencion_iva_tasa,
        retencion_isr=calculada.retencion_isr, retencion_isr_tasa=calculada.retencion_isr_tasa,
        total_estimado=calculada.total, metodo_pago=calculada.metodo_pago,
        forma_pago=calculada.forma_pago,
    )
    invoice_data = InvoiceData(estatus="confirmado_por_cliente", emisor=emisor, receptor=receptor, factura=factura)

    print("\nTimbrando en sandbox de FacturAPI...")
    result = await create_invoice(invoice_data, profile.facturapi_key)
    folio = result.get("id", "")
    print(f"Timbrado OK. folio={folio}")
    print(f"total (FacturAPI): {result.get('total')}")
    print(f"UUID: {result.get('uuid')}")

    xml = await download_xml(folio, profile.facturapi_key)
    print(f"\nXML descargado ({len(xml)} bytes) — buscando nodos de impuestos:")
    xml_text = xml.decode("utf-8", errors="replace")
    import re
    for m in re.finditer(r"<(?:cfdi:)?Impuestos[^>]*/?>", xml_text):
        print(" ", m.group(0))
    for m in re.finditer(r"<(?:cfdi:)?Traslado[^>]*/?>", xml_text):
        print(" ", m.group(0))
    for m in re.finditer(r"<(?:cfdi:)?Retencion[^>]*/?>", xml_text):
        print(" ", m.group(0))


if __name__ == "__main__":
    asyncio.run(main())
