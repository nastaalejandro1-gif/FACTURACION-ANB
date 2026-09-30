"""
Tool schema de Claude — SOLO extracción y clasificación.

Ningún campo aquí es un monto calculado (iva, retenciones, total): esos los
produce fiscal_engine a partir de lo que Claude extrae. Ver models.py
(InvoiceDraft/RepDraft) y fiscal_engine.py.

clave_prod_serv está restringida a un enum dinámico armado con el catálogo
aprobado del cliente (build_invoice_tools) — Claude no puede inventar una
clave SAT fuera de esa lista; solo puede elegir "NUEVA" + proponer un
código, que dispara escalamiento (EscalationReason.CLAVE_PROD_SERV_NUEVA).
"""

FORMAS_PAGO_ENUM = [
    "01", "02", "03", "04", "05", "06", "08",
    "12", "13", "17", "23", "24", "25", "26",
    "27", "28", "29", "30", "31", "99",
]

FORMAS_PAGO_ENUM_REP = [f for f in FORMAS_PAGO_ENUM if f != "99"]

CLAVE_NUEVA = "NUEVA"


def build_invoice_tools(claves_catalogo: list[str]) -> list[dict]:
    """
    claves_catalogo: códigos clave_prod_serv aprobados para este cliente
    (propios + globales del despacho, ver sheets_client.get_catalogo_claves).
    Se arma en cada turno porque el catálogo puede crecer cuando ANB aprueba
    una clave nueva.
    """
    claves_enum = list(dict.fromkeys([*claves_catalogo, CLAVE_NUEVA]))

    return [
        {
            "name": "generate_invoice_draft",
            "description": (
                "Genera los datos EXTRAÍDOS de la factura ÚNICAMENTE cuando el cliente "
                "ha confirmado explícitamente todos los datos. No calcules impuestos ni "
                "totales — eso lo hace el sistema. No llamar antes de la confirmación."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "estatus": {
                        "type": "string",
                        "enum": ["confirmado_por_cliente"],
                    },
                    "mas_pedidos_en_este_lote": {
                        "type": "boolean",
                        "description": (
                            "true SOLO si el cliente pidió más de una factura o REP en el "
                            "mismo mensaje y todavía falta procesar al menos uno después de "
                            "este. false (o simplemente omite el campo) si este es el único "
                            "pedido, o el último del lote."
                        ),
                    },
                    "receptor": {
                        "type": "object",
                        "properties": {
                            "razon_social": {"type": "string"},
                            "rfc": {"type": "string"},
                            "regimen_fiscal": {"type": "string"},
                            "cp_fiscal": {"type": "string"},
                            "uso_cfdi": {"type": "string"},
                        },
                        "required": ["razon_social", "rfc", "regimen_fiscal", "cp_fiscal", "uso_cfdi"],
                    },
                    "factura": {
                        "type": "object",
                        "properties": {
                            "conceptos": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "descripcion": {"type": "string"},
                                        "cantidad": {"type": "number"},
                                        "clave_unidad": {
                                            "type": "string",
                                            "description": "Clave SAT: E48=Servicio, H87=Pieza, KGM=Kilogramo, LTR=Litro, MTR=Metro",
                                        },
                                        "precio_unitario": {
                                            "type": "number",
                                            "description": "Precio por unidad antes de impuestos.",
                                        },
                                        "clave_prod_serv": {
                                            "type": "string",
                                            "enum": claves_enum,
                                            "description": (
                                                "Elige la clave del catálogo aprobado que mejor describe "
                                                "el concepto. Si ninguna aplica, usa 'NUEVA' y llena "
                                                "clave_prod_serv_propuesta."
                                            ),
                                        },
                                        "clave_prod_serv_propuesta": {
                                            "type": "string",
                                            "description": (
                                                "Código SAT de 8 dígitos que propones — SOLO si "
                                                "clave_prod_serv='NUEVA'. Se enviará a revisión del "
                                                "despacho una sola vez."
                                            ),
                                        },
                                    },
                                    "required": ["descripcion", "cantidad", "clave_unidad", "precio_unitario", "clave_prod_serv"],
                                },
                            },
                            "metodo_pago": {"type": "string", "enum": ["PUE", "PPD"]},
                            "forma_pago": {"type": "string", "enum": FORMAS_PAGO_ENUM},
                            "observaciones": {"type": "string", "default": ""},
                            "total_documento_fuente": {
                                "type": "number",
                                "description": (
                                    "Total impreso en la cotización/documento fuente, si lo viste "
                                    "explícitamente. Omite este campo si no hay documento o no "
                                    "muestra un total."
                                ),
                            },
                        },
                        "required": ["conceptos", "metodo_pago", "forma_pago"],
                    },
                },
                "required": ["estatus", "receptor", "factura"],
            },
        },
        {
            "name": "generate_rep_draft",
            "description": (
                "Genera los datos EXTRAÍDOS del Recibo Electrónico de Pago (complemento de "
                "pago / REP) ÚNICAMENTE cuando el cliente ha confirmado explícitamente todos "
                "los datos del pago. No calcules el saldo insoluto — eso lo hace el sistema. "
                "Usar solo cuando el cliente reporta el pago de una factura PPD existente, "
                "NO para facturas nuevas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "estatus": {"type": "string", "enum": ["confirmado_por_cliente"]},
                    "mas_pedidos_en_este_lote": {
                        "type": "boolean",
                        "description": (
                            "true SOLO si el cliente pidió más de una factura o REP en el "
                            "mismo mensaje y todavía falta procesar al menos uno después de "
                            "este. false (o simplemente omite el campo) si este es el único "
                            "pedido, o el último del lote."
                        ),
                    },
                    "uuid_factura_origen": {
                        "type": "string",
                        "description": "UUID / Folio Fiscal del CFDI PPD original. Formato: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
                    },
                    "receptor": {
                        "type": "object",
                        "properties": {
                            "razon_social": {"type": "string"},
                            "rfc": {"type": "string"},
                            "regimen_fiscal": {"type": "string"},
                            "cp_fiscal": {"type": "string"},
                            "uso_cfdi": {"type": "string"},
                        },
                        "required": ["razon_social", "rfc", "regimen_fiscal", "cp_fiscal", "uso_cfdi"],
                    },
                    "fecha_pago": {
                        "type": "string",
                        "description": "Fecha y hora del pago ISO 8601: YYYY-MM-DDTHH:MM:SS. Sin hora: usa T12:00:00.",
                    },
                    "forma_pago": {
                        "type": "string",
                        "enum": FORMAS_PAGO_ENUM_REP,
                        "description": "Forma de pago real (no puede ser 99). 03=Transferencia, 04=Tarjeta crédito, 28=Tarjeta débito, 01=Efectivo.",
                    },
                    "monto_pagado": {
                        "type": "number",
                        "description": "Monto pagado en esta transacción.",
                    },
                },
                "required": [
                    "estatus", "uuid_factura_origen", "receptor",
                    "fecha_pago", "forma_pago", "monto_pagado",
                ],
            },
        },
    ]
