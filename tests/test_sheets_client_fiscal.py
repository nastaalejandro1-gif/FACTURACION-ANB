"""
Tests de las funciones nuevas de sheets_client (catálogo de claves + reglas
fiscales) contra un fake Supabase en memoria — NUNCA contra el .env real.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import pytest

import sheets_client


@dataclass
class FakeTableQuery:
    db: "FakeSupabase"
    name: str
    filters: list = field(default_factory=list)
    _order: tuple = None
    _limit: int = None
    _write_row: dict = None
    _is_upsert: bool = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, col, val):
        self.filters.append((col, "eq", val))
        return self

    def is_(self, col, val):
        self.filters.append((col, "is", val))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, row):
        self._write_row = row
        return self

    def upsert(self, row, on_conflict=None):
        self._write_row = row
        self._is_upsert = True
        return self

    def execute(self):
        if self._write_row is not None:
            rows = self.db.tables.setdefault(self.name, [])
            rows.append(self._write_row)
            self.db.writes.append((self.name, self._write_row, self._is_upsert))
            return SimpleNamespace(data=[self._write_row])

        rows = list(self.db.tables.get(self.name, []))
        for col, op, val in self.filters:
            if op == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif op == "is" and val == "null":
                rows = [r for r in rows if r.get(col) is None]
        if self._order:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(col), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows)


class FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.writes: list = []

    def table(self, name):
        return FakeTableQuery(self, name)


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeSupabase({
        "catalogo_clave_prod_serv": [
            {"despacho_id": "ANB-001", "id_cliente": "2", "clave_prod_serv": "50192100",
             "descripcion_clave": "Botanas", "aplica_ieps": True, "activa": True},
            {"despacho_id": "ANB-001", "id_cliente": "2", "clave_prod_serv": "78101800",
             "descripcion_clave": "Transporte de carga (propia)", "aplica_ieps": False, "activa": True},
            {"despacho_id": "ANB-001", "id_cliente": None, "clave_prod_serv": "78101800",
             "descripcion_clave": "Transporte de carga (global)", "aplica_ieps": False, "activa": True},
            {"despacho_id": "ANB-001", "id_cliente": "1", "clave_prod_serv": "80141605",
             "descripcion_clave": "Servicios de contabilidad", "aplica_ieps": False, "activa": True},
            {"despacho_id": "ANB-001", "id_cliente": "1", "clave_prod_serv": "OLD00000",
             "descripcion_clave": "Clave desactivada", "aplica_ieps": False, "activa": False},
        ],
        "reglas_fiscales_cliente": [
            {"despacho_id": "ANB-001", "id_cliente": "2", "vigente_desde": "2026-09-29",
             "vigente_hasta": None, "iva_aplica": True, "tasa_iva": 0.16,
             "retencion_iva_tasa": 0.1067, "retencion_isr_tasa": 0.0125, "ieps_tasa": 0.08},
        ],
    })
    monkeypatch.setattr(sheets_client, "_get_supabase", lambda: db)
    return db


def test_get_catalogo_claves_incluye_propias_y_globales(fake_db):
    claves = sheets_client.get_catalogo_claves("ANB-001", "2")
    codigos = {c.clave_prod_serv for c in claves}
    assert "50192100" in codigos
    assert "78101800" in codigos  # aparece (propia + global, ambas activas)
    assert "OLD00000" not in codigos  # inactiva, no es de este cliente de todas formas


def test_get_catalogo_claves_excluye_inactivas(fake_db):
    claves = sheets_client.get_catalogo_claves("ANB-001", "1")
    codigos = {c.clave_prod_serv for c in claves}
    assert "80141605" in codigos
    assert "OLD00000" not in codigos


def test_get_fiscal_rules_arma_fiscal_rules_con_claves_ieps(fake_db):
    reglas = sheets_client.get_fiscal_rules("ANB-001", "2")
    assert reglas.iva_aplica is True
    assert reglas.tasa_iva == Decimal("0.16")
    assert reglas.retencion_iva_tasa == Decimal("0.1067")
    assert reglas.ieps_tasa == Decimal("0.08")
    assert "50192100" in reglas.claves_con_ieps
    assert "78101800" not in reglas.claves_con_ieps


def test_get_fiscal_rules_sin_regla_vigente_lanza_error(fake_db):
    with pytest.raises(ValueError):
        sheets_client.get_fiscal_rules("ANB-001", "999")


def test_save_clave_aprobada_hace_upsert(fake_db):
    sheets_client.save_clave_aprobada(
        despacho_id="ANB-001", id_cliente="1", clave_prod_serv="90111500",
        descripcion_clave="Servicios de consultoría", aplica_ieps=False,
        aprobada_por="ALEJANDRO",
    )
    table_name, row, is_upsert = fake_db.writes[-1]
    assert table_name == "catalogo_clave_prod_serv"
    assert is_upsert is True
    assert row["clave_prod_serv"] == "90111500"
    assert row["aprobada_por"] == "ALEJANDRO"

    claves = sheets_client.get_catalogo_claves("ANB-001", "1")
    assert any(c.clave_prod_serv == "90111500" for c in claves)


def test_save_pending_con_tipo_cliente_confirmacion(fake_db):
    sheets_client.save_pending(
        invoice_id="abc-123", canal="telegram", canal_id="555",
        telegram_message_id=1, invoice_json="{}", motivo_revision="",
        tipo_aprobacion="cliente_confirmacion", canal_id_aprobador="555",
    )
    table_name, row, _ = fake_db.writes[-1]
    assert table_name == "pendientes"
    assert row["tipo_aprobacion"] == "cliente_confirmacion"
    assert row["canal_id_aprobador"] == "555"


def test_save_pending_default_sigue_siendo_anb_revision(fake_db):
    sheets_client.save_pending(
        invoice_id="abc-456", canal="telegram", canal_id="555",
        telegram_message_id=2, invoice_json="{}", motivo_revision="dato roto",
    )
    table_name, row, _ = fake_db.writes[-1]
    assert row["tipo_aprobacion"] == "anb_revision"
    assert "canal_id_aprobador" not in row
