"""NIFTY-50 stock expansion of the directional universe (2026-07-17).

Covers the design constraints of the expansion:
  * universe resolution — static dated NIFTY-50 list ∩ live F&O catalog,
    one-flag revertible (DIRECTIONAL_INCLUDE_STOCK_UNIVERSE);
  * gate split — STOCKS bypass the index-only positioning fail-closed gate
    and run the standard signal engine, INDICES keep the positional path;
  * per-symbol data readiness — unready stocks are skipped-and-reported,
    a readiness DB failure fails closed for stocks only;
  * rotating batch math for the bounded-load runner;
  * a synthetic stock signal flows end-to-end into a paper proposal.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from directional_options.config import (
    DIRECTIONAL_STOCK_UNIVERSE,
    INDEX_UNIVERSE,
    clone_default_config,
)
from directional_options.schemas import RegimeSnapshot
from directional_options.service import DirectionalOptionsService, rotate_batch
from directional_options.signals import DirectionalSignalEngine


# ── shared fixtures/helpers ─────────────────────────────────────────────────

TREND_ROW = {
    "ema_spread_pct": 0.0032,
    "breakout_up": 0.4,
    "breakout_down": -0.2,
    "plus_di": 31.0,
    "minus_di": 16.0,
    "momentum_3": 0.004,
    "momentum_8": 0.009,
    "atr": 22.0,
    "close": 1502.0,
    "range_expansion": 1.3,
    "rv_percentile": 0.42,
}

TREND_REGIME = RegimeSnapshot(
    label="trend",
    trade_allowed=True,
    confidence=0.72,
    reasons=["trend confirmed"],
    preferred_expiry_kind="weekly",
    delta_target_min=0.35,
    delta_target_max=0.55,
    exit_profile="balanced",
)


def _isolate_paper_store(store, monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    state: dict[str, list] = {"open_positions": [], "closed_positions": []}

    async def _load_positions() -> dict[str, list]:
        return {
            "open_positions": [dict(row) for row in state["open_positions"]],
            "closed_positions": [dict(row) for row in state["closed_positions"]],
        }

    async def _save_positions(payload: dict) -> None:
        state["open_positions"] = [dict(row) for row in payload.get("open_positions", [])]
        state["closed_positions"] = [dict(row) for row in payload.get("closed_positions", [])]

    async def _load_journal() -> list[dict]:
        return []

    async def _append_journal(_payload: dict) -> None:
        return None

    async def _summary(open_positions: list[dict], closed_positions: list[dict]) -> dict:
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
        }

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(store, "_load_positions", _load_positions)
    monkeypatch.setattr(store, "_save_positions", _save_positions)
    monkeypatch.setattr(store, "_load_journal", _load_journal)
    monkeypatch.setattr(store, "_append_journal", _append_journal)
    monkeypatch.setattr(store, "_summary", _summary)
    monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", _noop)
    monkeypatch.setattr("directional_options.chain_analytics.ensure_chain_tracked", _noop)
    monkeypatch.setattr("directional_options.chain_analytics.chain_strike_mark", _noop)
    return state


# ── static universe sanity ──────────────────────────────────────────────────


def test_static_nifty50_universe_is_50_unique_stock_names() -> None:
    assert len(DIRECTIONAL_STOCK_UNIVERSE) == 50
    assert len(set(DIRECTIONAL_STOCK_UNIVERSE)) == 50
    assert not set(DIRECTIONAL_STOCK_UNIVERSE) & set(INDEX_UNIVERSE)
    config = clone_default_config()
    assert list(config["stock_universe"]) == list(DIRECTIONAL_STOCK_UNIVERSE)
    # Index universe unchanged by the expansion.
    assert list(config["universe"]) == ["NIFTY", "BANKNIFTY", "SENSEX"]


# ── universe resolution / intersection ──────────────────────────────────────


@pytest.mark.asyncio
async def test_universe_resolution_intersects_fo_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_INCLUDE_STOCK_UNIVERSE", True, raising=False)
    service = DirectionalOptionsService(clone_default_config())

    async def fake_catalog() -> set[str]:
        return {"RELIANCE", "TCS", "INFY", "NOT_IN_NIFTY50"}

    monkeypatch.setattr(service.store, "list_fo_stock_symbols", fake_catalog)

    universe = await service.resolve_runner_universe()
    assert universe["indices"] == ["NIFTY", "BANKNIFTY", "SENSEX"]
    # Intersection keeps static-list order and drops non-catalog names.
    assert universe["stocks"] == ["INFY", "RELIANCE", "TCS"]
    assert "fo_underlying_catalog" in universe["stock_universe_source"]


@pytest.mark.asyncio
async def test_universe_resolution_flag_off_restores_index_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_INCLUDE_STOCK_UNIVERSE", False, raising=False)
    service = DirectionalOptionsService(clone_default_config())

    async def fail_catalog() -> set[str]:  # must not even be consulted
        raise AssertionError("catalog should not be queried when the flag is off")

    monkeypatch.setattr(service.store, "list_fo_stock_symbols", fail_catalog)

    universe = await service.resolve_runner_universe()
    assert universe["indices"] == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert universe["stocks"] == []
    assert universe["stock_universe_source"] == "disabled"


@pytest.mark.asyncio
async def test_universe_resolution_falls_back_to_static_on_catalog_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_INCLUDE_STOCK_UNIVERSE", True, raising=False)
    service = DirectionalOptionsService(clone_default_config())

    async def broken_catalog() -> set[str]:
        raise RuntimeError("db down")

    monkeypatch.setattr(service.store, "list_fo_stock_symbols", broken_catalog)

    universe = await service.resolve_runner_universe()
    # Fallback: full static list (downstream readiness guard still protects).
    assert universe["stocks"] == list(DIRECTIONAL_STOCK_UNIVERSE)
    assert "catalog_unavailable" in universe["stock_universe_source"]


# ── positioning-gate split (signals.py) ─────────────────────────────────────


def test_stock_bypasses_positioning_gate_while_index_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", True, raising=False)
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])

    # INDEX + missing positioning row → fail closed (unchanged behaviour).
    assert engine.predict(TREND_ROW, TREND_REGIME, "3minute", positioning=None, underlying="NIFTY") is None
    # Unknown caller (no underlying) → conservative index-scope fail-closed.
    assert engine.predict(TREND_ROW, TREND_REGIME, "3minute", positioning=None, underlying=None) is None

    # STOCK + missing positioning row → standard engine still fires.
    signal = engine.predict(TREND_ROW, TREND_REGIME, "3minute", positioning=None, underlying="RELIANCE")
    assert signal is not None
    assert signal.positional is False
    assert signal.direction == "CE"


def test_index_positional_confirmation_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", True, raising=False)
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])

    positioning = {
        "is_stale": False,
        "htf_up": True,
        "oi_build_bias": 0.5,
        "pcr_oi": 0.8,
        "d_atm_iv": 0.2,
    }
    signal = engine.predict(TREND_ROW, TREND_REGIME, "3minute", positioning=positioning, underlying="BANKNIFTY")
    assert signal is not None
    assert signal.positional is True
    assert signal.direction == "CE"

    # Stale index feed still fails closed.
    stale = dict(positioning, is_stale=True)
    assert engine.predict(TREND_ROW, TREND_REGIME, "3minute", positioning=stale, underlying="BANKNIFTY") is None


# ── rotating batch math ─────────────────────────────────────────────────────


def test_rotate_batch_wraps_and_visits_every_symbol() -> None:
    symbols = [f"S{i}" for i in range(7)]

    batch1, cursor = rotate_batch(symbols, 0, 3)
    assert batch1 == ["S0", "S1", "S2"] and cursor == 3
    batch2, cursor = rotate_batch(symbols, cursor, 3)
    assert batch2 == ["S3", "S4", "S5"] and cursor == 6
    batch3, cursor = rotate_batch(symbols, cursor, 3)
    # Wraparound: tail + head.
    assert batch3 == ["S6", "S0", "S1"] and cursor == 2
    assert set(batch1) | set(batch2) | set(batch3) == set(symbols)


def test_rotate_batch_degenerate_inputs() -> None:
    assert rotate_batch([], 5, 3) == ([], 0)
    assert rotate_batch(["A", "B"], 1, 0) == ([], 0)
    # batch >= universe → whole list, cursor pinned at 0 (no rotation needed).
    assert rotate_batch(["A", "B"], 1, 5) == (["A", "B"], 0)
    # Out-of-range cursor (universe shrank between cycles) wraps safely.
    batch, cursor = rotate_batch(["A", "B", "C"], 9, 2)
    assert batch == ["A", "B"] and cursor == 2


# ── per-symbol readiness guard ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_ready_stock_symbols_skips_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DirectionalOptionsService(clone_default_config())

    async def fake_readiness(symbols, *, spot_max_age_seconds, watchlist_max_age_seconds):
        return {
            "RELIANCE": {
                "latest_spot_time": "2026-07-17T04:00:00+00:00",
                "spot_age_seconds": 30.0,
                "spot_fresh": True,
                "latest_watchlist_time": "2026-07-17T04:00:00+00:00",
                "watchlist_age_seconds": 45.0,
                "watchlist_rows": 4,
                "watchlist_fresh": True,
            },
            "TCS": {
                "latest_spot_time": None,
                "spot_age_seconds": None,
                "spot_fresh": False,
                "latest_watchlist_time": None,
                "watchlist_age_seconds": None,
                "watchlist_rows": 0,
                "watchlist_fresh": False,
            },
            "INFY": {
                "latest_spot_time": "2026-07-17T04:00:00+00:00",
                "spot_age_seconds": 40.0,
                "spot_fresh": True,
                "latest_watchlist_time": "2026-07-17T02:00:00+00:00",
                "watchlist_age_seconds": 7200.0,
                "watchlist_rows": 4,
                "watchlist_fresh": False,
            },
        }

    monkeypatch.setattr(service.store, "stock_readiness", fake_readiness)

    ready, skipped = await service.filter_ready_stock_symbols(["RELIANCE", "TCS", "INFY"])
    assert ready == ["RELIANCE"]
    assert skipped["TCS"] == "no_recent_spot_bars"
    assert skipped["INFY"].startswith("option_quotes_stale_")


@pytest.mark.asyncio
async def test_filter_ready_fails_closed_on_readiness_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DirectionalOptionsService(clone_default_config())

    async def broken_readiness(symbols, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(service.store, "stock_readiness", broken_readiness)

    ready, skipped = await service.filter_ready_stock_symbols(["RELIANCE", "TCS"])
    assert ready == []
    assert set(skipped) == {"RELIANCE", "TCS"}
    assert all(reason.startswith("readiness_check_failed") for reason in skipped.values())


# ── end-to-end: synthetic stock signal → paper proposal ─────────────────────


def _stock_live_fixture(service: DirectionalOptionsService, monkeypatch: pytest.MonkeyPatch, *, quote_age_seconds: float):
    as_of = pd.Timestamp.now(tz="UTC").floor("3min")
    quote_time = as_of - pd.Timedelta(seconds=quote_age_seconds)
    live_spot = pd.DataFrame(
        {
            "time": pd.to_datetime([as_of - pd.Timedelta(minutes=6), as_of - pd.Timedelta(minutes=3), as_of], utc=True),
            "open": [1488.0, 1493.0, 1498.0],
            "high": [1494.0, 1499.0, 1504.0],
            "low": [1486.0, 1491.0, 1496.0],
            "close": [1493.0, 1498.0, 1502.0],
            "volume": [90000.0, 110000.0, 120000.0],
            "oi": [0.0, 0.0, 0.0],
        }
    )
    feature_frame = pd.DataFrame(
        [
            {
                "time": as_of,
                "open": 1498.0,
                "high": 1504.0,
                "low": 1496.0,
                "close": 1502.0,
                "volume": 120000.0,
                "oi": 0.0,
                "adx": 28.0,
                "plus_di": 31.0,
                "minus_di": 12.0,
                "atr": 9.0,
                "ema_spread_pct": 0.0032,
                "breakout_up": 0.42,
                "breakout_down": -0.12,
                "rv_annualized": 0.24,
                "rv_percentile": 0.41,
                "range_expansion": 1.2,
                "momentum_3": 0.004,
                "momentum_8": 0.008,
                "range_pct": 0.005,
                "session_progress": 0.2,
                "ema_fast": 1500.0,
                "ema_slow": 1495.0,
            }
        ]
    )
    monthly_expiry = (as_of + pd.Timedelta(days=20)).date().isoformat()

    async def fake_load_live_spot_frame(underlying: str, lookback_days: int = 10):
        return live_spot, "timescaledb_spot_1minute", underlying

    async def fake_list_live_contract_snapshots(**kwargs):
        return [
            {
                "time": quote_time.isoformat(),
                "underlying": "RELIANCE",
                "expiry": monthly_expiry,
                "expiry_kind": "monthly",
                "strike": 1500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|RELIANCE1500CE",
                "trading_symbol": "RELIANCE 1500 CE",
                "underlying_price": 1502.0,
                "ltp": 32.0,
                "volume": 5200.0,
                "oi": 148000.0,
                "iv": 0.26,
                # NSE stock option lot (RELIANCE ≈ 500) — differs from index lots.
                "lot_size": 500,
                "tick_size": 0.05,
            }
        ]

    async def fake_strategy_health():
        return {
            "ready": True,
            "watchlist_rows_today": 40,
            "latest_watchlist_time": as_of.isoformat(),
            "watchlist_age_seconds": 30.0,
            "latest_spot_rows": {"RELIANCE": as_of.isoformat()},
        }

    async def fake_loss_windows():
        return (0.0, 0.0)

    async def fake_latest_local_option_mark(**_kwargs):
        return 32.0, quote_time.isoformat(), "local_watchlist"

    monkeypatch.setattr(service.store, "load_live_spot_frame", fake_load_live_spot_frame)
    monkeypatch.setattr(service.feature_engine, "build_frame", lambda *_a, **_k: feature_frame)
    monkeypatch.setattr(service.store, "list_live_contract_snapshots", fake_list_live_contract_snapshots)
    monkeypatch.setattr(service.store, "latest_local_option_mark", fake_latest_local_option_mark)
    monkeypatch.setattr(service, "_loss_cap_realized", fake_loss_windows)
    monkeypatch.setattr(
        "directional_options.service.market_intelligence_runtime.get_strategy_health",
        fake_strategy_health,
    )

    async def fake_rag(**_kwargs):
        return {"decision": "warn", "reason_codes": [], "summary": "test stub"}

    monkeypatch.setattr(service, "_build_rag_context_async", fake_rag)


@pytest.mark.asyncio
async def test_stock_signal_flows_end_to_end_to_paper_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RELIANCE (stock) with the positional flag ON must still produce a
    standard-engine signal, select the monthly stock contract off the
    watchlist row (stock lot size), pass risk, and open a paper position."""
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings, "SIGNAL_VALIDATION_UNCAPPED", True, raising=False)

    config = clone_default_config()
    config["paper_trading"]["journal_root"] = tmp_path / "directional-paper"
    # Deterministic flow: policy off → permissive always-act path.
    config["rl_policy"]["enabled"] = False
    service = DirectionalOptionsService(config)

    _stock_live_fixture(service, monkeypatch, quote_age_seconds=30.0)
    state = _isolate_paper_store(service.paper, monkeypatch)
    monkeypatch.setattr(
        "core.trading_calendar.trading_calendar.is_exchange_open",
        lambda *_args, **_kwargs: True,
    )

    payload = await service.record_paper_snapshot("RELIANCE", "3minute", 4)

    snapshot = payload["snapshot"]
    assert snapshot["signal"] is not None
    assert snapshot["signal"]["positional"] is False  # standard engine, not positional
    assert snapshot["data_status"]["execution_ready"] is True
    contract = snapshot["selected_contract"]
    assert contract["trading_symbol"] == "RELIANCE 1500 CE"
    assert contract["expiry_kind"] == "monthly"
    assert contract["lot_size"] == 500
    assert snapshot["risk"]["approved"] is True
    # Quantity respects the stock lot ladder.
    assert snapshot["risk"]["quantity_units"] % 500 == 0
    # Chain analytics stays index-only.
    assert snapshot["chain_analytics"] is None

    open_positions = state["open_positions"]
    assert len(open_positions) == 1
    assert open_positions[0]["underlying"] == "RELIANCE"
    assert open_positions[0]["option_type"] == "CE"


@pytest.mark.asyncio
async def test_stale_stock_option_quotes_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Global MI health can be fresh (index-driven) while THIS stock's option
    quotes are old — the stock guard must force execution_ready False so no
    entry fills against a stale premium."""
    from core.config import settings as _settings

    monkeypatch.setattr(_settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", True, raising=False)

    config = clone_default_config()
    config["paper_trading"]["journal_root"] = tmp_path / "directional-paper"
    config["rl_policy"]["enabled"] = False
    service = DirectionalOptionsService(config)

    stale_age = float(_settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS) + 600.0
    _stock_live_fixture(service, monkeypatch, quote_age_seconds=stale_age)

    async def fake_positions(**_kwargs):
        return []

    monkeypatch.setattr(service.paper, "list_positions", fake_positions)
    monkeypatch.setattr(service.paper, "list_journal", fake_positions)

    payload = await service.live_snapshot("RELIANCE", "3minute", 4)
    data_status = payload["snapshot"]["data_status"]
    assert data_status["execution_ready"] is False
    assert data_status["degraded_reason"] == "stock_option_quotes_stale"
