"""
Auditoria interna: timbra en sandbox varias combinaciones de reglas
fiscales (retenciones + IEPS + multi-concepto + REP con proporcion +
segunda parcialidad) para confirmar que las interacciones entre reglas
funcionan, no solo cada regla por separado. Usa Sin Culpa (tiene IEPS Y
retenciones configuradas simultaneamente) con receptor PM para activar
todas las reglas a la vez.
"""
import asyncio
import re
from decimal import Decimal

import sheets_client
import fiscal_engine
from facturapi_client import create_invoice, create_rep, download_xml, search_invoice_by_uuid
from models import ConceptoItem, EmisorData, FacturaData, InvoiceData, ReceptorData, RepData

CANAL_ID = "8208800448"  # Sin Culpa: ieps_tasa=0.08, retencion_iva=0.1067, retencion_isr=0.0125

RECEPTOR_PM = ReceptorData(
    razon_social="Auditoria Comercial SA de CV", rfc="AUC010101AA1",
    regimen_fiscal="601", cp_fiscal="06600", uso_cfdi="G03",
)
RECEPTOR_PF = ReceptorData(
    razon_social="Juan Perez Auditoria", rfc="PEAJ800101AB1",
    regimen_fiscal="612", cp_fiscal="44100", uso_cfdi="G03",
)


async def timbrar_factura(profile, reglas, conceptos, receptor, metodo_pago, forma_pago, etiqueta):
    resultado = fiscal_engine.calcular_factura(conceptos, receptor, reglas, metodo_pago, forma_pago)
    if resultado.escalation:
        print(f"[{etiqueta}] ESCALAMIENTO INESPERADO: {resultado.escalation}")
        return None
    calculada = resultado.factura
    print(f"[{etiqueta}] Local: subtotal={calculada.subtotal} ieps={calculada.ieps} "
          f"iva={calculada.iva} ret_iva={calculada.retencion_iva} ret_isr={calculada.retencion_isr} "
          f"total={calculada.total}")

    emisor = EmisorData(nombre_comercial=profile.nombre_comercial, razon_social=profile.razon_social,
                         rfc=profile.rfc, regimen_fiscal=profile.regimen_fiscal, cp_fiscal=profile.cp_fiscal)
    conceptos_item = [ConceptoItem(descripcion=cc.descripcion, clave_prod_serv=cc.clave_prod_serv,
                                    cantidad=cc.cantidad, clave_unidad=cc.clave_unidad,
                                    precio_unitario=cc.precio_unitario, ieps=cc.ieps, ieps_tasa=cc.ieps_tasa)
                      for cc in calculada.conceptos]
    factura = FacturaData(conceptos=conceptos_item, monto_antes_impuestos=calculada.subtotal,
                           ieps=calculada.ieps, iva=calculada.iva, tasa_iva=calculada.tasa_iva,
                           retencion_iva=calculada.retencion_iva, retencion_iva_tasa=calculada.retencion_iva_tasa,
                           retencion_isr=calculada.retencion_isr, retencion_isr_tasa=calculada.retencion_isr_tasa,
                           total_estimado=calculada.total, metodo_pago=calculada.metodo_pago,
                           forma_pago=calculada.forma_pago)
    invoice_data = InvoiceData(estatus="confirmado_por_cliente", emisor=emisor, receptor=receptor, factura=factura)

    result = await create_invoice(invoice_data, profile.facturapi_key)
    total_facturapi = Decimal(str(result.get("total")))
    ok = total_facturapi == calculada.total
    print(f"[{etiqueta}] FacturAPI total={total_facturapi} -- {'OK EXACTO' if ok else 'X DIFERENTE!!'}")
    return result.get("uuid"), calculada


async def main():
    profile = sheets_client.get_client_by_canal_id("telegram", CANAL_ID)
    reglas = sheets_client.get_fiscal_rules(profile.despacho_id, profile.id_cliente)
    print(f"Reglas Sin Culpa: ieps_tasa={reglas.ieps_tasa} ret_iva={reglas.retencion_iva_tasa} "
          f"ret_isr={reglas.retencion_isr_tasa} claves_ieps={reglas.claves_con_ieps}\n")

    # CASO 1: PM + IEPS + retenciones + multi-concepto mixto (gravado + no gravado), PPD
    conceptos_1 = [
        fiscal_engine.ConceptoExtraido(descripcion="Botana gravada A", cantidad=Decimal("15"),
            precio_unitario=Decimal("37.25"), clave_unidad="H87", clave_prod_serv="50192100"),
        fiscal_engine.ConceptoExtraido(descripcion="Botana gravada B", cantidad=Decimal("8"),
            precio_unitario=Decimal("112.90"), clave_unidad="H87", clave_prod_serv="50192100"),
        fiscal_engine.ConceptoExtraido(descripcion="Flete (no gravado)", cantidad=Decimal("1"),
            precio_unitario=Decimal("350.00"), clave_unidad="E48", clave_prod_serv="78101800"),
    ]
    r1 = await timbrar_factura(profile, reglas, conceptos_1, RECEPTOR_PM, "PPD", "99",
                                "CASO 1: PM+IEPS+retenciones+multiconcepto+PPD")
    print()

    # CASO 2: PF (sin retenciones aunque el cliente las tenga configuradas) + IEPS, PUE
    conceptos_2 = [
        fiscal_engine.ConceptoExtraido(descripcion="Botana gravada", cantidad=Decimal("3"),
            precio_unitario=Decimal("89.99"), clave_unidad="H87", clave_prod_serv="50192100"),
    ]
    r2 = await timbrar_factura(profile, reglas, conceptos_2, RECEPTOR_PF, "PUE", "01",
                                "CASO 2: PF+IEPS (retenciones deben ser 0)")
    print()

    if r1:
        uuid1, calc1 = r1
        # CASO 3: REP pago parcial CON retenciones + IEPS de por medio (combinacion nunca probada)
        original = await search_invoice_by_uuid(uuid1, profile.facturapi_key)
        invoice_total = Decimal(str(original.get("total")))
        pago1 = Decimal("2000.00")
        resultado_rep1 = fiscal_engine.calcular_rep(monto_pagado=pago1, imp_saldo_ant=invoice_total)
        print(f"[CASO 3] REP parcial 1: saldo_ant={invoice_total} pago={pago1} "
              f"saldo_insoluto={resultado_rep1.rep.imp_saldo_insoluto}")
        rep_data1 = RepData(estatus="confirmado_por_cliente", uuid_factura_origen=uuid1,
            receptor=RECEPTOR_PM, fecha_pago="2026-09-29T15:00:00", forma_pago="03",
            monto_pagado=resultado_rep1.rep.monto_pagado, imp_saldo_ant=resultado_rep1.rep.imp_saldo_ant,
            imp_saldo_insoluto=resultado_rep1.rep.imp_saldo_insoluto)
        rep_result1 = await create_rep(rep_data1, profile.facturapi_key, 1, original)
        folio1 = rep_result1.get("id")
        print(f"[CASO 3] REP timbrado folio={folio1}")
        xml1 = await download_xml(folio1, profile.facturapi_key)
        text1 = xml1.decode("utf-8", errors="replace")
        for m in re.finditer(r'<(?:pago20:)?(DoctoRelacionado|TrasladoDR|RetencionDR)[^>]*/?>', text1):
            print("  ", m.group(0))
        print()

        # CASO 4: segunda parcialidad sobre la MISMA factura (encadenar saldo insoluto)
        saldo_tras_pago1 = resultado_rep1.rep.imp_saldo_insoluto
        pago2 = saldo_tras_pago1  # liquidar el saldo restante en la segunda parcialidad
        resultado_rep2 = fiscal_engine.calcular_rep(monto_pagado=pago2, imp_saldo_ant=saldo_tras_pago1)
        print(f"[CASO 4] REP parcial 2 (encadenado): saldo_ant={saldo_tras_pago1} pago={pago2} "
              f"saldo_insoluto={resultado_rep2.rep.imp_saldo_insoluto}")
        rep_data2 = RepData(estatus="confirmado_por_cliente", uuid_factura_origen=uuid1,
            receptor=RECEPTOR_PM, fecha_pago="2026-09-30T10:00:00", forma_pago="04",
            monto_pagado=resultado_rep2.rep.monto_pagado, imp_saldo_ant=resultado_rep2.rep.imp_saldo_ant,
            imp_saldo_insoluto=resultado_rep2.rep.imp_saldo_insoluto)
        rep_result2 = await create_rep(rep_data2, profile.facturapi_key, 2, original)
        folio2 = rep_result2.get("id")
        print(f"[CASO 4] REP #2 timbrado folio={folio2}")
        xml2 = await download_xml(folio2, profile.facturapi_key)
        text2 = xml2.decode("utf-8", errors="replace")
        for m in re.finditer(r'<(?:pago20:)?(DoctoRelacionado|TrasladoDR|RetencionDR)[^>]*/?>', text2):
            print("  ", m.group(0))


if __name__ == "__main__":
    asyncio.run(main())
