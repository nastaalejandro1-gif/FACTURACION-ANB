import main


def test_nombre_con_serie_y_folio():
    assert main._nombre_archivo_cfdi({"series": "A", "folio_number": 123}, "factura", "abcdef123456") == "A-123"


def test_nombre_sin_serie():
    assert main._nombre_archivo_cfdi({"series": None, "folio_number": 45}, "factura", "abcdef123456") == "45"


def test_nombre_sin_folio_usa_id_interno():
    assert main._nombre_archivo_cfdi({}, "rep", "abcdef123456") == "rep_abcdef12"


def test_serie_con_caracteres_raros_se_limpia():
    assert main._nombre_archivo_cfdi({"series": "F/2 ", "folio_number": 7}, "factura", "x") == "F2-7"
