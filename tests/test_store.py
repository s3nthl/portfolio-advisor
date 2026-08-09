"""Phase-3 store tests: two refreshes -> queryable history + day-over-day deltas."""
from __future__ import annotations

import copy

import pytest

from ingest.fixture import load_fixture_book
from store import (
    count_snapshots,
    latest_delta,
    latest_snapshots,
    write_snapshot,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_chai.db"


def test_single_refresh_is_queryable(db_path):
    book = load_fixture_book()
    rid = write_snapshot(book, source="fixture", refreshed_at="2026-07-31T16:00:00",
                         db_path=db_path)
    assert rid == 1
    assert count_snapshots(db_path=db_path) == 1

    snaps = latest_snapshots(db_path=db_path)
    assert len(snaps) == 1
    s = snaps[0]
    assert s.source == "fixture"
    assert round(s.metrics["net_liq"]) == 1_099_690
    assert round(s.metrics["stock_mv"]) == 863_056
    # full payload round-trips
    assert s.payload["posture"]["band"] == "fear"


def test_two_refreshes_history_and_delta(db_path):
    # Day 1: the frozen fixture.
    day1 = load_fixture_book()
    write_snapshot(day1, source="fixture", refreshed_at="2026-07-31T16:00:00",
                   db_path=db_path)

    # Day 2: same book but cash drawn down and a stock mark up — simulate a move.
    day2 = copy.deepcopy(day1)
    day2.balances.cash_and_sweep -= 50_000          # cash 239,898 -> 189,898
    day2.balances.net_liq -= 20_000                 # net liq moves too
    day2.stocks[0].mark += 10.0                      # NVDA 118 -> 128 (+10,000 MV)
    write_snapshot(day2, source="fixture", refreshed_at="2026-08-01T16:00:00",
                   db_path=db_path)

    assert count_snapshots(db_path=db_path) == 2

    # History is queryable, newest first.
    snaps = latest_snapshots(db_path=db_path)
    assert [s.refreshed_at for s in snaps] == [
        "2026-08-01T16:00:00", "2026-07-31T16:00:00"
    ]

    # Day-over-day delta.
    d = latest_delta(db_path=db_path)
    assert "error" not in d
    assert d["current"]["refreshed_at"] == "2026-08-01T16:00:00"
    assert d["previous"]["refreshed_at"] == "2026-07-31T16:00:00"

    cash = d["deltas"]["real_cash"]
    assert round(cash.previous) == 239_898
    assert round(cash.current) == 189_898
    assert round(cash.change) == -50_000

    stock = d["deltas"]["stock_mv"]
    assert round(stock.change) == 10_000  # NVDA +$10 * 1000 sh

    netliq = d["deltas"]["net_liq"]
    assert round(netliq.change) == -20_000


def test_delta_needs_two_snapshots(db_path):
    write_snapshot(load_fixture_book(), source="fixture", db_path=db_path)
    d = latest_delta(db_path=db_path)
    assert "error" in d
    assert d["count"] == 1
