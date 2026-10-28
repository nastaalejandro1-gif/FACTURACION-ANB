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

TOOL_BUSCAR_RECEPTOR = "buscar_receptor_guardado"


def build_invoice_tools(claves_catalogo: list[str], folios_cancelables: list[str] = ()) -> list[dict]:
    """
    claves_catalogo: códigos clave_prod_serv aprobados para este cliente
    (propios + globales del despacho, ver sheets_client.get_catalogo_claves).
    Se arma en cada turno porque el catálogo puede crecer cuando ANB aprueba
    una clave nueva.

    folios_cancelables: ids de FacturAPI de las facturas/REPs timbrados
    recientes de ESTE cliente. Son el enum de la herramienta de cancelación
    (Claude no puede inventar un folio ni cancelar uno de otro cliente); sin
    folios, la herramienta no se ofrece.
    """
    claves_enum = list(dict.fromkeys([*claves_catalogo, CLAVE_NUEVA]))
    tools = _invoice_tools(claves_enum)
    if folios_cancelables:
        tools.append({
            "name": "request_invoice_cancellation",
            "description": (
                "Solicita cancelar ante el SAT una factura o REP ya timbrado de este cliente. "
                "Llamar SOLO cuando el cliente pidió cancelar, ya identificaste CUÁL (de la lista "
                "de facturas recientes) y el motivo. El sistema le pide confirmación final con un "
                "botón antes de cancelar."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "folio": {
                        "type": "string",
                        "enum": list(dict.fromkeys(folios_cancelables)),
                        "description": "folio/UUID de la factura a cancelar, tal como aparece en la lista de facturas recientes.",
                    },
                    "motivo": {
                        "type": "string",
                        "enum": ["02", "03"],
                        "description": (
                            "02 = la factura tiene errores (datos, montos, conceptos) y NO se va a "
                            "sustituir por otra ligada; 03 = la operación no se llevó a cabo."
                        ),
                    },
                },
                "required": ["folio", "motivo"],
            },
        })
    return tools


def _invoice_tools(claves_enum: list[str]) -> list[dict]:
    return [
        {
            "name": TOOL_BUSCAR_RECEPTOR,
            "description": (
                "Busca en el directorio del cliente un receptor al que ya se le facturó antes "
                "(se guarda solo tras timbrar con su CSF). Devuelve razón social, RFC, régimen, "
                "CP y el último uso CFDI. Llamar cuando el cliente nombra al receptor (nombre o "
                "RFC) y NO mandó su CSF."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "texto": {
                        "type": "string",
                        "description": (
                            "Nombre o RFC del receptor tal como lo dio el cliente. Para nombres, usa "
                            "la palabra más distintiva (ej. 'Envoy', no 'Grupo Envoy SA de CV')."
                        ),
                    },
                },
                "required": ["texto"],
            },
        },
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
                                    "explícitamente, tal como aparece (con o sin IVA). Omite este "
                                    "campo si no hay documento o no muestra un total."
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
                        "description": "Monto pagado en esta transacción. Omítelo si liquidar_saldo=true.",
                    },
                    "liquidar_saldo": {
                        "type": "boolean",
                        "description": (
                            "true si el cliente dice que este pago LIQUIDA lo que resta de la "
                            "factura sin dar el monto: el sistema calcula el saldo pendiente y lo "
                            "usa como monto pagado. Si el cliente sí dio un monto, usa monto_pagado."
                        ),
                    },
                },
                "required": [
                    "estatus", "uuid_factura_origen", "receptor",
                    "fecha_pago", "forma_pago",
                ],
            },
        },
    ]
