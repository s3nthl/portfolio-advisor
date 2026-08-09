"""Phase-4 API tests. Uses the offline fixture source + a temp DB.

Regression guard: the SECOND /api/refresh exercises the delta path (two
snapshots -> Delta dataclasses), which must serialize to JSON.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import api.app as apimod
from api.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHAI_SOURCE", "fixture")
    monkeypatch.setattr(config, "CHAI_DB_PATH", tmp_path / "api_test.db")
    # tests need each /api/refresh to actually pull+write (delta needs 2 snapshots);
    # disable the production TTL cache and reset any warm payload for isolation.
    monkeypatch.setattr(apimod, "_REFRESH_TTL", 0)
    apimod._REFRESH_CACHE.update(t=0.0, payload=None)
    # Keep tests offline: no Finnhub network calls (falls back to static reference).
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(config, "FINNHUB_CACHE_PATH", tmp_path / "finnhub_cache.json")
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_refresh_returns_full_payload(client):
    d = client.get("/api/refresh").json()
    assert d["snapshot_id"] == 1
    s = d["summary"]
    assert round(s["net_liq"]) == 1_099_690
    assert s["buckets"]["csp"]["count"] == 14
    assert s["posture"]["kind"] == "VXN" and s["posture"]["band"] == "fear"
    assert s["posture_vix"]["kind"] == "VIX"
    assert round(d["waterfall"]["peak_loan"]) == 256_902
    assert d["breakdown"]["groups"]["stock"]["rows"][0]["pct"] > 0


def test_second_refresh_serializes_delta(client):
    assert client.get("/api/refresh").status_code == 200
    r2 = client.get("/api/refresh")
    assert r2.status_code == 200  # was 500 before delta serialization fix
    d = r2.json()
    assert "deltas" in d["delta"]
    assert d["delta"]["deltas"]["net_liq"]["change"] == 0.0  # same fixture -> no change


def test_history_and_delta_endpoints(client):
    client.get("/api/refresh")
    client.get("/api/refresh")
    h = client.get("/api/history").json()
    assert h["count"] == 2
    dl = client.get("/api/delta").json()
    assert "deltas" in dl


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "s3nthl portfolio dashboard" in r.text
