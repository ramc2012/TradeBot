"""Post-close F&O stock spot sweep (2026-07-19).

Stock 30-minute spot had no durable live writer: the only live producer is
``upstox_research_sync`` with ``spot_limit=25`` (25 of ~211 names per pass —
123 names on 07-16, 19 on 07-17), so every full-coverage day came from a MANUAL
backfill and the hole re-opened each evening. These tests pin the properties
that make the replacement runner safe to leave scheduled.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from market_data import stock_spot_sweeper as sweeper


def test_parse_intervals_accepts_configured_list():
    assert sweeper._parse_intervals("30minute,3minute") == ["30minute", "3minute"]


def test_parse_intervals_ignores_unknown_and_dedupes():
    assert sweeper._parse_intervals("30minute, bogus ,30minute,3minute") == ["30minute", "3minute"]


def test_parse_intervals_defaults_when_empty():
    """A blank/garbage setting must degrade to the decision grid, never to nothing."""
    assert sweeper._parse_intervals("") == ["30minute"]
    assert sweeper._parse_intervals(None) == ["30minute"]
    assert sweeper._parse_intervals("nonsense") == ["30minute"]


def test_every_swept_interval_has_a_broker_resolution():
    for interval in sweeper._parse_intervals("30minute,3minute"):
        assert interval in sweeper._RESOLUTION


def test_normalize_skips_malformed_rows_without_raising():
    rows = sweeper._normalize([
        {"time": "2026-07-17T03:45:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"time": "not-a-time", "open": 1, "high": 2, "low": 1, "close": 1, "volume": 1},
        {"open": 1, "high": 2, "low": 1, "close": 1},          # no time
        {"time": "2026-07-17T03:46:00Z", "open": "x", "high": 2, "low": 1, "close": 1},
    ])
    assert len(rows) == 1
    assert rows[0]["close"] == 1.5


@pytest.mark.asyncio
async def test_sweep_disabled_is_a_provable_noop(monkeypatch):
    """Flag off ⇒ no broker session is even resolved."""
    monkeypatch.setattr(sweeper.settings, "STOCK_SPOT_SWEEP_ENABLED", False, raising=False)
    assert await sweeper.sweep_stock_spot() == {"status": "disabled"}


@pytest.mark.asyncio
async def test_sweep_without_broker_session_skips_cleanly(monkeypatch):
    """No Fyers session must degrade to a reported skip, never an exception —
    a data-maintenance job may not take the supervisor down."""
    monkeypatch.setattr(sweeper.settings, "STOCK_SPOT_SWEEP_ENABLED", True, raising=False)

    import api.routers.auth as auth

    async def _no_session(*_a, **_kw):
        return False

    monkeypatch.setattr(auth, "ensure_fyers_session", _no_session, raising=False)
    monkeypatch.setattr(auth, "get_active_adapter", lambda *_a, **_kw: None, raising=False)

    result = await sweeper.sweep_stock_spot()
    assert result["status"] in {"skipped_no_broker", "error"}


@pytest.mark.asyncio
async def test_sweep_survives_internal_failure(monkeypatch):
    """Any unexpected failure is reported, not raised."""
    monkeypatch.setattr(sweeper.settings, "STOCK_SPOT_SWEEP_ENABLED", True, raising=False)

    import api.routers.auth as auth

    # Pin the broker-session step so the failure under test is the one we inject
    # (otherwise suite ordering decides whether a real adapter is resolved first).
    async def _session_ok(*_a, **_kw):
        return True

    monkeypatch.setattr(auth, "ensure_fyers_session", _session_ok, raising=False)
    monkeypatch.setattr(auth, "get_active_adapter", lambda *_a, **_kw: object(), raising=False)

    async def _boom(*_a, **_kw):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(sweeper, "_stock_universe", _boom)
    result = await sweeper.sweep_stock_spot()
    assert result["status"] == "error"
    assert "broker exploded" in result["error"]


def test_runner_is_registered_post_close_and_not_in_session():
    """The runner must be post-close-forced, so it never competes with live
    decision traffic during the session."""
    from core.market_hours_paper_supervisor import MarketHoursPaperSupervisor

    configs = MarketHoursPaperSupervisor()._default_runners()
    by_key = {c.key: c for c in configs}
    assert "stock_spot_sweep" in by_key, "the durable stock-spot writer must be scheduled"

    config = by_key["stock_spot_sweep"]
    assert config.post_close_force_daily is True
    assert config.post_close_catchup is True
    assert config.market_hours_fn is not None

    # The real guarantee, not just the flags: the supervisor dispatches on
    # ``_runtime_market_open(...) and is_due(...)`` and only falls through to the
    # post-close branch when the market reads CLOSED. So market_hours_fn must be
    # False at EVERY moment of a live NSE session — otherwise this 211-symbol
    # sweep fires hourly in-session (and instantly at 09:15, last_started_at
    # being None), competing with exactly the decision traffic it must not touch.
    ist = ZoneInfo("Asia/Kolkata")
    session_day = date(2026, 7, 17)  # a Friday NSE session
    for hh, mm in ((9, 15), (9, 16), (11, 0), (13, 30), (15, 29), (15, 30)):
        moment = datetime(session_day.year, session_day.month, session_day.day, hh, mm, tzinfo=ist)
        assert config.market_hours_fn(moment) is False, (
            f"stock_spot_sweep must never read market-open in-session ({hh:02d}:{mm:02d} IST)"
        )
