"""MACD diffusion — NSE-session gating + current-ATM-watchlist breadth scope.

2026-07-16 fixes under test:
  • the daemon's live snapshot is gated to the NSE session (it used to upsert a
    frozen 'live' row every hour all night/weekend — observed 21:30 → 05:30 IST
    buckets on 2026-07-15/16 with identical breadth);
  • breadth counts ONLY the current ATM watchlist legs (latest CE + PE per
    underlying, unexpired, fresh) instead of every (underlying, expiry, strike,
    option_type) ever snapshotted (~4.5k legs across weeks of retention).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import market_data.macd_diffusion as macd_diffusion_module
from market_data.macd_diffusion import (
    WATCHLIST_FRESHNESS_DAYS,
    _in_snapshot_window,
    backfill_from_candles,
    compute_and_store,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_snapshot_window_is_nse_session_only() -> None:
    # Tue 2026-07-14 is an NSE session day.
    assert _in_snapshot_window(_ist(2026, 7, 14, 10, 0)) is True
    assert _in_snapshot_window(_ist(2026, 7, 14, 9, 15)) is True
    # ≤16:00 grace lets the final in-session tick land in the 15:00 bucket.
    assert _in_snapshot_window(_ist(2026, 7, 14, 15, 45)) is True
    assert _in_snapshot_window(_ist(2026, 7, 14, 16, 0)) is True
    # After the grace: idle (this was the all-night 'live' row writer).
    assert _in_snapshot_window(_ist(2026, 7, 14, 16, 1)) is False
    assert _in_snapshot_window(_ist(2026, 7, 14, 22, 30)) is False
    assert _in_snapshot_window(_ist(2026, 7, 14, 2, 0)) is False
    # Pre-open: idle.
    assert _in_snapshot_window(_ist(2026, 7, 14, 9, 0)) is False
    # Weekend: idle.
    assert _in_snapshot_window(_ist(2026, 7, 18, 11, 0)) is False


class _CaptureSession:
    """Fake AsyncSession capturing executed SQL + params."""

    def __init__(self, results: list[object]) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self._results = list(results)

    async def execute(self, statement, params=None):  # noqa: ANN001
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else SimpleNamespace()

    async def commit(self) -> None:
        return None


def _session_factory(session: _CaptureSession):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    return _Ctx


def test_compute_and_store_counts_only_current_atm_watchlist_legs(monkeypatch) -> None:
    class _CountRow:
        ce_total, ce_above, pe_total, pe_above = 200, 120, 200, 60

    class _CountResult:
        def first(self):
            return _CountRow()

    session = _CaptureSession(results=[_CountResult(), SimpleNamespace()])
    monkeypatch.setattr(macd_diffusion_module, "AsyncSessionLocal", _session_factory(session))

    result = asyncio.run(compute_and_store(market="NSE"))

    breadth_sql, breadth_params = session.calls[0]
    # Leg selection = latest CE + PE per underlying (the tracked ATM legs) …
    assert "DISTINCT ON (underlying, option_type)" in breadth_sql
    # … restricted to live contracts and a fresh watchlist horizon — NOT the
    # whole snapshot table (the old query keyed on strike+expiry with no
    # freshness/expiry bound and counted ~4.5k legs).
    assert "expiry >= CURRENT_DATE" in breadth_sql
    assert ":freshness_days" in breadth_sql
    assert breadth_params == {"freshness_days": WATCHLIST_FRESHNESS_DAYS}

    assert result is not None
    assert result["ce_total"] == 200
    assert result["pe_above_zero"] == 60
    # net = 120/200 - 60/200
    assert abs(result["net_diffusion"] - 0.30) < 1e-9

    upsert_sql, upsert_params = session.calls[1]
    assert "INSERT INTO macd_diffusion_snapshots" in upsert_sql
    assert upsert_params["market"] == "NSE"


def test_backfill_legs_restricted_to_current_atm_watchlist(monkeypatch) -> None:
    class _LegsResult:
        def all(self):
            return []

    session = _CaptureSession(results=[_LegsResult()])
    monkeypatch.setattr(macd_diffusion_module, "AsyncSessionLocal", _session_factory(session))

    filled = asyncio.run(backfill_from_candles(days=21))

    assert filled == 0
    legs_sql, legs_params = session.calls[0]
    assert "DISTINCT ON (underlying, option_type)" in legs_sql
    assert "expiry >= CURRENT_DATE" in legs_sql
    assert legs_params == {"freshness_days": WATCHLIST_FRESHNESS_DAYS}
