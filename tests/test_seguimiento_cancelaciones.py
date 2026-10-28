"""
Seguimiento de cancelaciones que esperaban la aceptación del receptor en el
SAT (bitácora 'cancelacion_pendiente'): el cron consulta FacturAPI y cierra
el ciclo. Todo mockeado.
"""
from datetime import datetime, timedelta, timezone

import pytest

import main
from tests.test_main_orchestration import CLIENT_PROFILE, _patch_common

FILA = {
    "id": "bit-1", "rfc_emisor": CLIENT_PROFILE.rfc, "canal_id": "555",
    "folio_fiscal": "fapi-1", "tipo": "ingreso", "timestamp": "2026-10-01T00:00:00+00:00",
}


def _hace(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _preparar(monkeypatch, factura: dict, solicitud: dict | None):
    sent, _, _ = _patch_common(monkeypatch)
    monkeypatch.setattr(main.sheets_client, "get_cancelaciones_en_espera", lambda: [FILA])
    monkeypatch.setattr(main.sheets_client, "get_clientes_activos", lambda: [(CLIENT_PROFILE, [])])
    monkeypatch.setattr(main.sheets_client, "get_solicitud_cancelacion", lambda bid: solicitud)

    async def fake_get_invoice(folio, key):
        assert (folio, key) == ("fapi-1", CLIENT_PROFILE.facturapi_key)
        return {"series": "F", "folio_number": 20, **factura}
    monkeypatch.setattr(main, "get_invoice", fake_get_invoice)

    estados, notificados = [], []
    monkeypatch.setattr(
        main.sheets_client, "update_bitacora_estado",
        lambda bid, estado, detalle="": estados.append((bid, estado)) or True,
    )
    monkeypatch.setattr(main.sheets_client, "marcar_notificados", lambda ids: notificados.extend(ids))
    return sent, estados, notificados


@pytest.mark.asyncio
async def test_cancelada_en_el_sat_actualiza_bitacora_y_avisa_a_quien_la_pidio(monkeypatch):
    solicitud = {"id": "pend-1", "canal_id": "777", "timestamp_respuesta": _hace(days=2)}
    sent, estados, _ = _preparar(monkeypatch, {"status": "canceled", "cancellation_status": "none"}, solicitud)

    resueltas, atoradas = await main._seguir_cancelaciones()

    assert estados == [("bit-1", "cancelado")]
    assert [(c, t) for c, t, _ in sent] == [("777", "✅ La factura F-20 ya quedó cancelada ante el SAT.")]
    assert len(resueltas) == 1 and "F-20" in resueltas[0] and atoradas == []


@pytest.mark.asyncio
async def test_rechazada_por_el_receptor_vuelve_a_vigente_y_avisa(monkeypatch):
    sent, estados, _ = _preparar(monkeypatch, {"status": "valid", "cancellation_status": "rejected"}, None)

    resueltas, _ = await main._seguir_cancelaciones()

    assert estados == [("bit-1", "timbrado")]
    assert sent[0][0] == "555"  # sin solicitud, avisa al chat de la bitácora
    assert "rechazó" in sent[0][1] and "sigue vigente" in sent[0][1]
    assert "RECHAZÓ" in resueltas[0]


@pytest.mark.asyncio
async def test_en_espera_dentro_del_plazo_no_hace_nada(monkeypatch):
    solicitud = {"id": "pend-1", "canal_id": "777", "timestamp_respuesta": _hace(days=2)}
    sent, estados, notificados = _preparar(
        monkeypatch, {"status": "valid", "cancellation_status": "pending"}, solicitud
    )

    assert await main._seguir_cancelaciones() == ([], [])
    assert sent == [] and estados == [] and notificados == []


@pytest.mark.asyncio
async def test_en_espera_pasado_el_plazo_avisa_a_alejandro_una_vez_al_dia(monkeypatch):
    solicitud = {"id": "pend-1", "canal_id": "777", "timestamp_respuesta": _hace(days=6)}
    sent, estados, notificados = _preparar(
        monkeypatch, {"status": "valid", "cancellation_status": "pending"}, solicitud
    )

    _, atoradas = await main._seguir_cancelaciones()
    assert len(atoradas) == 1 and "hace 6 días" in atoradas[0]
    assert notificados == ["pend-1"] and sent == [] and estados == []

    # Ya se avisó hace unas horas: no se repite.
    solicitud["notificado_at"] = _hace(hours=3)
    assert await main._seguir_cancelaciones() == ([], [])


@pytest.mark.asyncio
async def test_error_de_facturapi_no_rompe_el_resto(monkeypatch):
    sent, estados, _ = _preparar(monkeypatch, {}, None)

    async def falla(folio, key):
        raise main.httpx.RequestError("sin red")
    monkeypatch.setattr(main, "get_invoice", falla)

    assert await main._seguir_cancelaciones() == ([], [])
    assert sent == [] and estados == []
