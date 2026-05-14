FORMAS_PAGO_ENUM = [
    "01", "02", "03", "04", "05", "06", "08",
    "12", "13", "17", "23", "24", "25", "26",
    "27", "28", "29", "30", "31", "99",
]

CLAUDE_TOOLS = [
    {
        "name": "generate_invoice_data",
        "description": (
            "Genera los datos estructurados de la factura ÚNICAMENTE cuando el cliente "
            "ha confirmado explícitamente todos los datos. No llamar antes de la confirmación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "estatus": {
                    "type": "string",
                    "enum": ["confirmado_por_cliente"],
                },
                "requiere_revision": {"type": "boolean"},
                "motivo_revision": {"type": "string", "default": ""},
                "emisor": {
                    "type": "object",
                    "properties": {
                        "nombre_comercial": {"type": "string"},
                        "razon_social": {"type": "string"},
                        "rfc": {"type": "string"},
                        "regimen_fiscal": {"type": "string"},
                        "cp_fiscal": {"type": "string"},
                    },
                    "required": ["nombre_comercial", "razon_social", "rfc", "regimen_fiscal", "cp_fiscal"],
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
                                    "clave_prod_serv": {"type": "string"},
                                    "cantidad": {"type": "number"},
                                    "clave_unidad": {
                                        "type": "string",
                                        "description": "Clave SAT: E48=Servicio, H87=Pieza, KGM=Kilogramo, LTR=Litro, MTR=Metro",
                                    },
                                    "precio_unitario": {"type": "number"},
                                },
                                "required": ["descripcion", "clave_prod_serv", "cantidad", "clave_unidad", "precio_unitario"],
                            },
                        },
                        "monto_antes_impuestos": {
                            "type": "number",
                            "description": "Suma de cantidad * precio_unitario de todos los conceptos",
                        },
                        "iva": {"type": "number"},
                        "retencion_iva": {"type": "number"},
                        "retencion_isr": {"type": "number"},
                        "total_estimado": {"type": "number"},
                        "metodo_pago": {"type": "string", "enum": ["PUE", "PPD"]},
                        "forma_pago": {"type": "string", "enum": FORMAS_PAGO_ENUM},
                        "observaciones": {"type": "string", "default": ""},
                    },
                    "required": [
                        "conceptos", "monto_antes_impuestos",
                        "iva", "retencion_iva", "retencion_isr", "total_estimado",
                        "metodo_pago", "forma_pago",
                    ],
                },
            },
            "required": ["estatus", "requiere_revision", "emisor", "receptor", "factura"],
        },
    }
]
