"""
build_system_prompt es pura (sin red) — se puede probar sin mockear Anthropic.
"""
from claude_client import build_system_prompt
from models import ClientProfile
from sheets_client import ClaveCatalogo

PROFILE = ClientProfile(
    despacho_id="ANB-001",
    id_cliente="2",
    nombre_comercial="Sin Culpa",
    razon_social="Sin Culpa SA de CV",
    rfc="SIN010101AA1",
    canal="telegram",
    canal_id="555",
    email_factura="facturas@sinculpa.mx",
    tipo_persona="PM",
    regimen_fiscal="601",
    cp_fiscal="06600",
    iva_aplica="SI",
    retencion_iva=0.0,
    retencion_isr=0.0,
    ieps_rate=8.0,
    clave_prod_serv_default="50192100",
    requiere_revision=False,
    notas_fiscales="",
    activo=True,
    facturapi_key="fake-key",
)

CATALOGO = [
    ClaveCatalogo(clave_prod_serv="50192100", descripcion_clave="Botanas", aplica_ieps=True, id_cliente="2"),
    ClaveCatalogo(clave_prod_serv="78101800", descripcion_clave="Transporte de carga", aplica_ieps=False, id_cliente=None),
]


def test_prompt_no_contiene_formulas_fiscales():
    prompt = build_system_prompt(PROFILE, CATALOGO, [])
    for texto_prohibido in ("ieps_rate", "retencion_iva * ", "monto_antes_impuestos * 0.16", "REGLAS FISCALES"):
        assert texto_prohibido not in prompt


def test_prompt_incluye_catalogo_de_claves_del_cliente():
    prompt = build_system_prompt(PROFILE, CATALOGO, [])
    assert "50192100" in prompt
    assert "Botanas" in prompt
    assert "78101800" in prompt


def test_prompt_catalogo_vacio_instruye_usar_nueva():
    prompt = build_system_prompt(PROFILE, [], [])
    assert "NUEVA" in prompt


def test_prompt_incluye_bloque_fuera_de_alcance():
    prompt = build_system_prompt(PROFILE, CATALOGO, [])
    assert "FUERA DE ALCANCE" in prompt
    assert "contacta directamente" in prompt.lower()


def test_prompt_no_pide_a_claude_calcular_impuestos():
    prompt = build_system_prompt(PROFILE, CATALOGO, [])
    assert "no calcules impuestos" in prompt.lower() or "sin calcular impuestos" in prompt.lower()


def test_prompt_incluye_facturas_recientes():
    from sheets_client import FacturaReciente
    facturas = [
        FacturaReciente(
            tipo="ingreso", rfc_receptor="DVA010101AA1", total=1325.76,
            timestamp="2026-09-29T21:00:00+00:00", estado="timbrado",
            folio_fiscal="abc123", uuid_factura_origen="",
        ),
    ]
    prompt = build_system_prompt(PROFILE, CATALOGO, facturas)
    assert "DVA010101AA1" in prompt
    assert "1,325.76" in prompt
    assert "timbrado" in prompt


def test_prompt_sin_facturas_recientes_no_falla():
    prompt = build_system_prompt(PROFILE, CATALOGO, [])
    assert "sin facturas previas" in prompt.lower()
