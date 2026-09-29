from tools import CLAVE_NUEVA, build_invoice_tools


def test_build_invoice_tools_incluye_clave_nueva_en_enum():
    tools = build_invoice_tools(["78101803", "78101800"])
    invoice_tool = next(t for t in tools if t["name"] == "generate_invoice_draft")
    enum = invoice_tool["input_schema"]["properties"]["factura"]["properties"]["conceptos"]["items"]["properties"]["clave_prod_serv"]["enum"]
    assert "78101803" in enum
    assert "78101800" in enum
    assert CLAVE_NUEVA in enum


def test_build_invoice_tools_no_duplica_claves():
    tools = build_invoice_tools(["78101800", "78101800"])
    invoice_tool = next(t for t in tools if t["name"] == "generate_invoice_draft")
    enum = invoice_tool["input_schema"]["properties"]["factura"]["properties"]["conceptos"]["items"]["properties"]["clave_prod_serv"]["enum"]
    assert enum.count("78101800") == 1


def test_build_invoice_tools_catalogo_vacio_solo_tiene_nueva():
    tools = build_invoice_tools([])
    invoice_tool = next(t for t in tools if t["name"] == "generate_invoice_draft")
    enum = invoice_tool["input_schema"]["properties"]["factura"]["properties"]["conceptos"]["items"]["properties"]["clave_prod_serv"]["enum"]
    assert enum == [CLAVE_NUEVA]


def test_build_invoice_tools_no_pide_montos_calculados():
    tools = build_invoice_tools(["78101803"])
    invoice_tool = next(t for t in tools if t["name"] == "generate_invoice_draft")
    factura_props = invoice_tool["input_schema"]["properties"]["factura"]["properties"]
    for campo_prohibido in ("iva", "retencion_iva", "retencion_isr", "total_estimado", "ieps"):
        assert campo_prohibido not in factura_props

    concepto_props = factura_props["conceptos"]["items"]["properties"]
    assert "ieps" not in concepto_props


def test_build_invoice_tools_no_pide_emisor():
    tools = build_invoice_tools(["78101803"])
    invoice_tool = next(t for t in tools if t["name"] == "generate_invoice_draft")
    assert "emisor" not in invoice_tool["input_schema"]["properties"]


def test_generate_rep_draft_forma_pago_excluye_99():
    tools = build_invoice_tools([])
    rep_tool = next(t for t in tools if t["name"] == "generate_rep_draft")
    enum = rep_tool["input_schema"]["properties"]["forma_pago"]["enum"]
    assert "99" not in enum
