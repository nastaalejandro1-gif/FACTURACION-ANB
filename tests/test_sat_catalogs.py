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


def test_regimen_resico_pf_no_coincide_con_pm():
    assert regimen_coincide_con_tipo_persona("621", "PM") is False


def test_regimen_resico_pm_no_coincide_con_pf():
    assert regimen_coincide_con_tipo_persona("626", "PF") is False


def test_regimen_ambiguo_no_restringe_ningun_tipo_persona():
    # 610, 616, 622, 628, 629: el despacho nunca los etiquetó PF/PM exclusivo,
    # así que no deben rechazar ni PF ni PM.
    for regimen in ("610", "616", "622", "628", "629"):
        assert regimen_coincide_con_tipo_persona(regimen, "PF") is True
        assert regimen_coincide_con_tipo_persona(regimen, "PM") is True


def test_regimen_desconocido_no_restringe():
    assert regimen_coincide_con_tipo_persona("999", "PF") is True


def test_catalogo_regimenes_tiene_los_17_codigos_originales():
    esperados = {
        "601", "603", "605", "606", "607", "608", "610", "611", "612",
        "614", "616", "621", "622", "625", "626", "628", "629",
    }
    assert set(REGIMENES_FISCALES_VALIDOS.keys()) == esperados
