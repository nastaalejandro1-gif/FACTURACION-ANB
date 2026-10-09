"""
Lectura determinista del XML de un CFDI 4.0 emitido FUERA de FacturAPI.

Caso que lo motiva: el cliente pide el REP de una factura PPD que hizo en
otro programa. search_invoice_by_uuid no la encuentra (solo ve lo timbrado
en FacturAPI) y sin ella no hay total, saldo ni impuestos para el documento
relacionado. El XML timbrado trae todo eso con precisión de centavo, así que
se lee con código (no con Claude): sin tokens y sin riesgo de un monto mal
extraído.

El resultado tiene la misma forma que el dict de FacturAPI que ya consume
create_rep ("total") más "bases_impuestos": las bases reales por (impuesto,
tasa, retención) tal como vienen en los conceptos del XML — ver
facturapi_client._build_related_document_taxes.
"""
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

MAX_XML_BYTES = 2 * 1024 * 1024  # un CFDI con cientos de conceptos pesa < 1 MB

NS_CFDI_40 = "http://www.sat.gob.mx/cfd/4"
NS_CFDI_33 = "http://www.sat.gob.mx/cfd/3"
NS_TFD = "http://www.sat.gob.mx/TimbreFiscalDigital"

# c_Impuesto del SAT -> nombre que usa FacturAPI
IMPUESTOS_SAT = {"001": "ISR", "002": "IVA", "003": "IEPS"}

TOLERANCIA_TOTAL = Decimal("0.02")


class CfdiXmlError(ValueError):
    """El archivo no es un CFDI 4.0 timbrado legible. El mensaje es apto para el cliente."""


def _dec(value: str | None, campo: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise CfdiXmlError(f"El XML trae un valor inválido en {campo}: {value!r}")


def _tasa_normalizada(tasa: Decimal) -> str:
    # "0.160000" y "0.16" deben agruparse igual
    return format(tasa.normalize(), "f")


def parse_cfdi_xml(xml_bytes: bytes) -> dict:
    if len(xml_bytes) > MAX_XML_BYTES:
        raise CfdiXmlError("El archivo es demasiado grande para ser el XML de una factura.")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        raise CfdiXmlError("El archivo no es un XML válido.")

    if root.tag == f"{{{NS_CFDI_33}}}Comprobante":
        raise CfdiXmlError(
            "Ese XML es un CFDI 3.3 (anterior a 2022); este flujo solo maneja CFDI 4.0."
        )
    if root.tag != f"{{{NS_CFDI_40}}}Comprobante":
        raise CfdiXmlError("El XML no es un CFDI (no encontré el nodo Comprobante 4.0).")

    ns = {"cfdi": NS_CFDI_40, "tfd": NS_TFD}
    timbre = root.find(".//tfd:TimbreFiscalDigital", ns)
    if timbre is None or not timbre.get("UUID"):
        raise CfdiXmlError("El XML no está timbrado (no trae el Timbre Fiscal Digital con UUID).")
    emisor = root.find("cfdi:Emisor", ns)
    receptor = root.find("cfdi:Receptor", ns)
    if emisor is None or receptor is None:
        raise CfdiXmlError("El XML no trae Emisor o Receptor.")

    advertencias: list[str] = []
    agregados: dict[tuple, Decimal] = {}  # (type, tasa_str, withholding) -> base acumulada
    for concepto in root.findall("cfdi:Conceptos/cfdi:Concepto", ns):
        for nodo, withholding in (
            ("cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado", False),
            ("cfdi:Impuestos/cfdi:Retenciones/cfdi:Retencion", True),
        ):
            for imp in concepto.findall(nodo, ns):
                tipo = IMPUESTOS_SAT.get(imp.get("Impuesto", ""))
                factor = imp.get("TipoFactor", "")
                if tipo is None:
                    advertencias.append(f"impuesto desconocido {imp.get('Impuesto')!r}")
                    continue
                if factor != "Tasa":
                    # Exento / Cuota: FacturAPI los maneja distinto en el
                    # documento relacionado y no lo hemos probado en sandbox.
                    advertencias.append(f"{tipo} con TipoFactor {factor} (no soportado aún)")
                    continue
                key = (
                    tipo,
                    _tasa_normalizada(_dec(imp.get("TasaOCuota"), "TasaOCuota")),
                    withholding,
                )
                agregados[key] = agregados.get(key, Decimal("0")) + _dec(imp.get("Base"), "Base")

    total = _dec(root.get("Total"), "Total")
    subtotal = _dec(root.get("SubTotal"), "SubTotal")
    descuento = _dec(root.get("Descuento", "0"), "Descuento")
    impuestos = root.find("cfdi:Impuestos", ns)
    trasladados = retenidos = Decimal("0")
    if impuestos is not None:
        trasladados = _dec(impuestos.get("TotalImpuestosTrasladados", "0"), "TotalImpuestosTrasladados")
        retenidos = _dec(impuestos.get("TotalImpuestosRetenidos", "0"), "TotalImpuestosRetenidos")
    esperado = subtotal - descuento + trasladados - retenidos
    if abs(esperado - total) > TOLERANCIA_TOTAL:
        # Típicamente impuestos locales (complemento implocal): el REP no
        # los reconstruye, mejor que lo revise ANB.
        advertencias.append(
            f"el total {total} no cuadra con subtotal-descuento+traslados-retenciones ({esperado}); "
            "¿impuestos locales?"
        )

    serie_folio = f"{root.get('Serie', '')}{root.get('Folio', '')}"
    return {
        "fuente": "xml",
        "uuid": timbre.get("UUID").upper(),
        "fecha": root.get("Fecha", ""),
        "serie_folio": serie_folio,
        "tipo_comprobante": root.get("TipoDeComprobante", ""),
        "metodo_pago": root.get("MetodoPago", ""),
        "moneda": root.get("Moneda", ""),
        "total": str(total),
        "rfc_emisor": emisor.get("Rfc", "").upper(),
        "nombre_emisor": emisor.get("Nombre", ""),
        "rfc_receptor": receptor.get("Rfc", "").upper(),
        "nombre_receptor": receptor.get("Nombre", ""),
        "regimen_receptor": receptor.get("RegimenFiscalReceptor", ""),
        "cp_receptor": receptor.get("DomicilioFiscalReceptor", ""),
        "bases_impuestos": [
            {"type": tipo, "rate": tasa, "withholding": withholding, "base": str(base)}
            for (tipo, tasa, withholding), base in agregados.items()
        ],
        "advertencias": advertencias,
    }


def problemas_para_rep(origen: dict, rfc_emisor_cliente: str) -> list[str]:
    """
    Motivos por los que el REP de una factura externa NO se puede timbrar en
    automático. Lista vacía = sigue el flujo normal (confirmación del
    cliente). Todos son bloqueantes: o el REP no procede (PUE, factura
    ajena) o saldría con impuestos mal declarados (Exento/Cuota, impuestos
    locales, otra moneda) — esos los timbra ANB a mano.

    El receptor NO se valida aquí: para facturas externas el REP siempre se
    timbra con el receptor del propio CFDI (ver main._receptor_de_origen).
    """
    problemas = list(origen.get("advertencias", []))
    if origen.get("rfc_emisor", "").upper() != rfc_emisor_cliente.upper():
        problemas.append(
            f"la factura la emitió {origen.get('rfc_emisor')}, no el cliente ({rfc_emisor_cliente})"
        )
    if origen.get("tipo_comprobante") not in ("I", ""):
        problemas.append(f"el comprobante es tipo {origen.get('tipo_comprobante')}, no de ingreso")
    if origen.get("metodo_pago") != "PPD":
        problemas.append(f"la factura es {origen.get('metodo_pago') or 'sin método de pago'}, no PPD")
    if origen.get("moneda") != "MXN":
        problemas.append(f"la factura está en {origen.get('moneda')}; el REP en otra moneda no está soportado")
    return problemas
