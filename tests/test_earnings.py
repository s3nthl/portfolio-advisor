"""Earnings pipeline regression tests.

These lock in the two accuracy/reliability fixes:
  1. `_pick_next` always returns the soonest report dated today-or-later.
  2. A failed fetch NEVER poisons the daily cache (so blanks self-heal on the
     next refresh instead of sticking for the whole day).

All network calls are stubbed — these run offline and deterministically.
"""
from datetime import date, timedelta

import ingest.fmp as fmp


def _d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def test_pick_next_soonest_future_ignoring_past():
    rows = [
        {"symbol": "NVDA", "date": _d(-80), "epsActual": 0.9},   # already reported
        {"symbol": "NVDA", "date": _d(21), "epsActual": None},   # the next one
        {"symbol": "NVDA", "date": _d(140), "epsActual": None},  # further out
    ]
    nxt = fmp._pick_next(rows, date.today().isoformat())
    assert nxt["date"] == _d(21)
    assert nxt["confirmed"] is True


def test_pick_next_none_when_no_future():
    rows = [{"symbol": "X", "date": _d(-3), "epsActual": 1.0}]
    assert fmp._pick_next(rows, date.today().isoformat()) is None


def test_bulk_hits_and_stragglers(monkeypatch, tmp_path):
    monkeypatch.setattr(fmp.config, "FMP_API_KEY", "TESTKEY")
    monkeypatch.setattr(fmp.config, "FMP_CACHE_PATH", tmp_path / "e.json")
    monkeypatch.setattr(fmp, "_bulk_earnings_calendar", lambda *a, **k: [
        {"symbol": "AAPL", "date": _d(30), "epsActual": None},
        {"symbol": "CDE", "date": _d(6), "epsActual": None},
        {"symbol": "AMZN", "date": _d(-5), "epsActual": 1.9},   # only a past row -> straggler
    ])
    seen = {}

    def fake_fill(syms, key, today_iso, cache):
        seen["syms"] = list(syms)
        cache["AMZN"] = {"as_of": today_iso, "next": {"date": _d(88), "eps_est": 2.0}}

    monkeypatch.setattr(fmp, "_fill_per_symbol", fake_fill)

    res = fmp.next_earnings(["AAPL", "CDE", "AMZN"], force=True)
    assert res["AAPL"]["date"] == _d(30)
    assert res["CDE"]["date"] == _d(6)
    assert seen["syms"] == ["AMZN"]          # no future row in bulk -> verified per-symbol
    assert res["AMZN"]["date"] == _d(88)


def test_earnings_moves_respects_report_time():
    from api.app import _earnings_moves
    daily = [
        {"date": "2026-01-05", "close": 100.0},
        {"date": "2026-01-06", "close": 102.0},
        {"date": "2026-01-07", "close": 110.0},
    ]
    # before-open on the 6th -> the 6th's session reacts: 102/100 = +2%
    assert _earnings_moves([{"date": "2026-01-06", "time": "bmo"}], daily) == [
        {"date": "2026-01-06", "move": 2.0}]
    # after-close on the 6th -> the NEXT session reacts: 110/102 = +7.84%
    assert _earnings_moves([{"date": "2026-01-06", "time": "amc"}], daily) == [
        {"date": "2026-01-06", "move": 7.84}]
    # unknown time -> bracket the report: 110/100 = +10%
    assert _earnings_moves([{"date": "2026-01-06", "time": None}], daily) == [
        {"date": "2026-01-06", "move": 10.0}]


def test_earnings_history_failure_returns_stale_not_poison(monkeypatch, tmp_path):
    monkeypatch.setattr(fmp.config, "FMP_API_KEY", "TESTKEY")
    monkeypatch.setattr(fmp.config, "FMP_CACHE_PATH", tmp_path / "e.json")

    class _Resp:
        status_code = 429
        def json(self):  # pragma: no cover - not reached on 429
            return []

    monkeypatch.setattr(fmp, "httpx", type("H", (), {"get": staticmethod(lambda *a, **k: _Resp())}), raising=False)
    # pre-seed a good history from earlier today
    fmp._save_cache({"AAPL::HIST": {"as_of": date.today().isoformat(),
                                    "rows": [{"date": _d(-90), "eps_actual": 1.0}]}})
    # a *different* symbol whose fetch 429s must not get a poisoned entry
    rows = fmp.earnings_history("NVDA", force=True)
    assert rows is None                      # unreachable -> None (distinct from empty=[]), and...
    assert "NVDA::HIST" not in fmp._load_cache()   # ...nothing cached -> retries next time


def test_bulk_failure_does_not_poison_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(fmp.config, "FMP_API_KEY", "TESTKEY")
    monkeypatch.setattr(fmp.config, "FMP_CACHE_PATH", tmp_path / "e.json")
    monkeypatch.setattr(fmp, "_bulk_earnings_calendar", lambda *a, **k: None)  # 429/error

    fmp._save_cache({"XYZ": {"as_of": date.today().isoformat(),
                             "next": {"date": _d(9), "eps_est": 1.0}}})
    res = fmp.next_earnings(["XYZ", "NEWSYM"], force=True)

    cache = fmp._load_cache()
    assert res["XYZ"]["date"] == _d(9)       # existing good value preserved
    assert "NEWSYM" not in cache             # failure NOT cached -> retries next refresh
    assert res["NEWSYM"] is None
