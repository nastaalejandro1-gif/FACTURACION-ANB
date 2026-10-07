import pytest

from sat_catalogs import (
    REGIMENES_FISCALES_VALIDOS,
    regimen_coincide_con_tipo_persona,
    tipo_persona_from_rfc,
)


def test_tipo_persona_pf_13_chars():
    assert tipo_persona_from_rfc("PERJ800101AB1") == "PF"


def test_tipo_persona_pm_12_chars():
    assert tipo_persona_from_rfc("EMP010101AA1") == "PM"


def test_tipo_persona_lowercase_normalizado():
    assert tipo_persona_from_rfc("perj800101ab1") == "PF"


def test_tipo_persona_longitud_invalida_lanza_error():
    with pytest.raises(ValueError):
        tipo_persona_from_rfc("CORTO123")


def test_regimen_pm_coincide_con_tipo_persona_pm():
    assert regimen_coincide_con_tipo_persona("601", "PM") is True


def test_regimen_pm_no_coincide_con_tipo_persona_pf():
    assert regimen_coincide_con_tipo_persona("601", "PF") is False


def test_regimen_pf_coincide_con_tipo_persona_pf():
    assert regimen_coincide_con_tipo_persona("612", "PF") is True


def test_regimen_rif_no_coincide_con_pm():
    assert regimen_coincide_con_tipo_persona("621", "PM") is False


def test_regimen_resico_626_aplica_a_pf_y_pm():
    # Regresión: una PF en RESICO se mapeaba a 621 (RIF) y 626 se rechazaba para PF.
    assert regimen_coincide_con_tipo_persona("626", "PF") is True
    assert regimen_coincide_con_tipo_persona("626", "PM") is True


def test_catalogo_no_confunde_resico_con_rif():
    assert "Incorporación Fiscal" in REGIMENES_FISCALES_VALIDOS["621"]
    assert "RESICO" not in REGIMENES_FISCALES_VALIDOS["621"]
    assert "Simplificado de Confianza" in REGIMENES_FISCALES_VALIDOS["626"]


def test_regimen_sin_restriccion_acepta_ambos_tipos():
    # 610 aplica a PF y PM oficialmente; 628/629 no se pudieron confirmar
    # contra la columna Física/Moral del SAT, así que no se restringen.
    for regimen in ("610", "628", "629"):
        assert regimen_coincide_con_tipo_persona(regimen, "PF") is True
        assert regimen_coincide_con_tipo_persona(regimen, "PM") is True


def test_regimenes_exclusivos_segun_columnas_sat():
    for regimen in ("615", "616", "621"):
        assert regimen_coincide_con_tipo_persona(regimen, "PM") is False
    for regimen in ("620", "622", "623", "624"):
        assert regimen_coincide_con_tipo_persona(regimen, "PF") is False


def test_regimen_desconocido_no_restringe():
    assert regimen_coincide_con_tipo_persona("999", "PF") is True


def test_catalogo_regimenes_completo():
    esperados = {
        "601", "603", "605", "606", "607", "608", "610", "611", "612",
        "614", "615", "616", "620", "621", "622", "623", "624", "625",
        "626", "628", "629",
    }
    assert set(REGIMENES_FISCALES_VALIDOS.keys()) == esperados
