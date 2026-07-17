from __future__ import annotations

import importlib
import json
from datetime import datetime

import pytest

from brokers.base import OptionChain, OptionChainEntry
from market_data.market_intelligence_runtime import APP_SYMBOLS, IST, MarketIntelligenceRuntime


market_intelligence_module = importlib.import_module("market_data.market_intelligence_runtime")
atm_watchlist_module = importlib.import_module("market_data.atm_watchlist")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value


class _FakeUpstoxAdapter:
    broker_name = "upstox"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        self.calls.append((symbol, expiry))
        if symbol == "BSE_INDEX|SENSEX":
            raise RuntimeError("upstox missing sensex chain")
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=22500.0,
            entries=[
                OptionChainEntry(
                    strike=22500.0,
                    option_type="CE",
                    ltp=150.0,
                    oi=1000,
                    volume=500,
                    bid=149.5,
                    ask=150.5,
                    prev_oi=900,
                    prev_close=140.0,
                    instrument_key="UPSTOX-CE",
                ),
                OptionChainEntry(
                    strike=22500.0,
                    option_type="PE",
                    ltp=130.0,
                    oi=1100,
                    volume=450,
                    bid=129.5,
                    ask=130.5,
                    prev_oi=1000,
                    prev_close=135.0,
                    instrument_key="UPSTOX-PE",
                ),
            ],
        )


class _FakeFyersAdapter:
    broker_name = "fyers"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        self.calls.append((symbol, expiry))
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=78500.0,
            entries=[
                OptionChainEntry(
                    strike=78500.0,
                    option_type="CE",
                    ltp=220.0,
                    oi=800,
                    volume=300,
                    bid=219.5,
                    ask=220.5,
                    prev_oi=750,
                    prev_close=210.0,
                    instrument_key="FYERS-CE",
                ),
                OptionChainEntry(
                    strike=78500.0,
                    option_type="PE",
                    ltp=205.0,
                    oi=780,
                    volume=290,
                    bid=204.5,
                    ask=205.5,
                    prev_oi=760,
                    prev_close=215.0,
                    instrument_key="FYERS-PE",
                ),
            ],
        )


async def _fake_get_redis(redis: _FakeRedis) -> _FakeRedis:
    return redis


@pytest.mark.asyncio
async def test_refresh_index_option_chains_prefers_upstox_and_falls_back_to_fyers(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MarketIntelligenceRuntime()
    redis = _FakeRedis()
    upstox = _FakeUpstoxAdapter()
    fyers = _FakeFyersAdapter()

    async def fake_candidates() -> list[tuple[str, str]]:
        return [
            ("NIFTY", "2026-04-28"),
            ("SENSEX", "2026-04-30"),
        ]

    async def fake_ensure_session(*, force_validate: bool = False) -> bool:
        return False

    monkeypatch.setattr(runtime, "_load_chain_refresh_candidates", fake_candidates)
    monkeypatch.setattr(market_intelligence_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(
        market_intelligence_module,
        "get_active_adapter",
        lambda broker: {"upstox": upstox, "fyers": fyers}.get(broker),
    )
    monkeypatch.setattr(market_intelligence_module, "ensure_upstox_session", fake_ensure_session)
    monkeypatch.setattr(market_intelligence_module, "ensure_fyers_session", fake_ensure_session)

    payload = await runtime.refresh_index_option_chains()

    assert payload["status"] == "ok"
    assert payload["requests"] == [
        {"symbol_code": "NIFTY", "expiry": "2026-04-28", "status": "refreshed", "source": "upstox"},
        {"symbol_code": "SENSEX", "expiry": "2026-04-30", "status": "refreshed", "source": "fyers"},
    ]
    assert upstox.calls == [
        ("NSE_INDEX|Nifty 50", "2026-04-28"),
        ("BSE_INDEX|SENSEX", "2026-04-30"),
    ]
    assert fyers.calls == [("BSE:SENSEX-INDEX", "2026-04-30")]

    cached_nifty = json.loads(redis.values[f"oc:{APP_SYMBOLS['NIFTY']}:2026-04-28"])
    cached_sensex = json.loads(redis.values[f"oc:{APP_SYMBOLS['SENSEX']}:2026-04-30"])
    assert cached_nifty["source"] == "upstox"
    assert cached_sensex["source"] == "fyers"
    assert cached_nifty["entries"][0]["instrument_key"] == "UPSTOX-CE"
    assert cached_sensex["entries"][0]["instrument_key"] == "FYERS-CE"


@pytest.mark.asyncio
async def test_refresh_index_option_chains_respects_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MarketIntelligenceRuntime()
    runtime._last_chain_refresh_at = datetime.now(IST)
    observed: list[str] = []

    def fake_get_active_adapter(broker: str):
        observed.append(broker)
        return None

    monkeypatch.setattr(market_intelligence_module, "get_active_adapter", fake_get_active_adapter)

    payload = await runtime.refresh_index_option_chains()

    assert payload["status"] == "cooldown"
    assert payload["source"] == "cached"
    assert payload["requests"] == []
    assert observed == []


@pytest.mark.asyncio
async def test_refresh_nse_watchlists_limits_full_universe_refresh_to_stock_monthly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MarketIntelligenceRuntime()
    observed_requests: list[tuple[str | None, tuple[str, ...]]] = []

    async def fake_get_expiries(selected_expiry=None, *, live_refresh=False):
        assert selected_expiry is None
        assert live_refresh is True
        return {
            "monthly_expiry": "2026-04-28",
            "stock_monthly_expiry": "2026-04-30",
            "index_monthlies": {
                "NIFTY": "2026-04-28",
                "BANKNIFTY": "2026-04-28",
                "FINNIFTY": "2026-04-28",
                "MIDCPNIFTY": "2026-04-28",
                "SENSEX": "2026-04-24",
            },
        }

    async def fake_get_watchlist(expiry=None, symbols=None, *, live_refresh=False, force_rebuild=False):
        assert live_refresh is True
        assert force_rebuild is (symbols is None)
        observed_requests.append((expiry, tuple(symbols or ())))
        return {
            "build_status": "ready",
            "summary": {"total_rows": 218 if not symbols else len(symbols)},
            "detail": None,
        }

    fake_service = type(
        "_FakeATMWatchlistService",
        (),
        {
            "get_expiries": staticmethod(fake_get_expiries),
            "get_watchlist": staticmethod(fake_get_watchlist),
        },
    )()

    monkeypatch.setattr(atm_watchlist_module, "atm_watchlist_service", fake_service)

    payload = await runtime.refresh_nse_watchlists()

    assert payload["stock_monthly_expiry"] == "2026-04-30"
    assert payload["monthly_expiries"] == ["2026-04-24", "2026-04-28"]
    assert observed_requests == [
        ("2026-04-30", ()),
        ("2026-04-24", ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")),
        ("2026-04-28", ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")),
    ]


def test_strategy_readiness_blocks_stale_latest_session_for_execution() -> None:
    stale = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=0,
        watchlist_rows_latest=171,
        watchlist_age_seconds=14 * 24 * 60 * 60,
        market_open=False,
    )
    assert stale["ready"] is True
    assert stale["execution_ready"] is False
    assert stale["readiness_mode"] == "latest_session"
    assert stale["execution_mode"] == "stale_latest_session"

    fresh = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=0,
        watchlist_rows_latest=171,
        watchlist_age_seconds=60 * 60,
        market_open=False,
    )
    assert fresh["ready"] is True
    assert fresh["execution_ready"] is True
    assert fresh["execution_mode"] == "latest_session"

    live = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=171,
        watchlist_rows_latest=171,
        watchlist_age_seconds=120,
        market_open=True,
    )
    assert live["execution_ready"] is True
    assert live["execution_mode"] == "live"


def test_strategy_readiness_requires_live_watchlist_during_market_hours() -> None:
    partial_session = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=36,
        watchlist_rows_latest=171,
        watchlist_age_seconds=90,
        market_open=True,
    )
    assert partial_session["ready"] is True
    assert partial_session["today_session_ready"] is False
    assert partial_session["execution_ready"] is False
    assert partial_session["execution_mode"] == "partial_live_session"

    previous_session = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=0,
        watchlist_rows_latest=171,
        watchlist_age_seconds=60 * 60,
        market_open=True,
    )
    assert previous_session["ready"] is True
    assert previous_session["execution_ready"] is False
    assert previous_session["readiness_mode"] == "latest_session"
    assert previous_session["execution_mode"] == "missing_live_session"

    stale_today = market_intelligence_module._strategy_readiness_fields(
        watchlist_rows_today=171,
        watchlist_rows_latest=171,
        watchlist_age_seconds=30 * 60,
        market_open=True,
    )
    assert stale_today["ready"] is True
    assert stale_today["execution_ready"] is False
    assert stale_today["execution_mode"] == "stale_live_session"


def test_index_spot_readiness_requires_fresh_index_rows_during_market_hours() -> None:
    now = datetime.fromisoformat("2026-05-27T04:00:00+00:00")
    fresh = {
        symbol: "2026-05-27T03:58:00+00:00"
        for symbol in market_intelligence_module.NSE_INDEX_SCOPE
    }
    ready = market_intelligence_module._index_spot_readiness_fields(
        fresh,
        market_open=True,
        now_utc=now,
    )
    assert ready["index_spot_ready"] is True
    assert ready["index_spot_missing"] == []
    assert ready["index_spot_stale"] == {}

    stale = dict(fresh)
    stale["NIFTY"] = "2026-05-22T09:59:00+00:00"
    stale.pop("SENSEX")
    blocked = market_intelligence_module._index_spot_readiness_fields(
        stale,
        market_open=True,
        now_utc=now,
    )
    assert blocked["index_spot_ready"] is False
    assert blocked["index_spot_missing"] == ["SENSEX"]
    assert "NIFTY" in blocked["index_spot_stale"]


def test_drop_contaminated_spot_rows_drops_out_of_band_backfill_rows() -> None:
    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    drop = market_intelligence_module._drop_contaminated_spot_rows
    rows = [
        {"time": "t1", "open": 24050.0, "high": 24090.0, "low": 24010.0, "close": 24075.0},
        # whole-frame BANKNIFTY contamination misrouted under NIFTY
        {"time": "t2", "open": 57800.0, "high": 57840.0, "low": 57770.0, "close": 57831.0},
        {"time": "t3", "open": 24080.0, "high": 24120.0, "low": 24060.0, "close": 24100.0},
        # clean close but a contaminated intra-minute HIGH
        {"time": "t4", "open": 24070.0, "high": 57826.0, "low": 24050.0, "close": 24090.0},
        {"time": "t5", "open": 24065.0, "high": 24110.0, "low": 24040.0, "close": 24088.0},
    ]
    cleaned = drop(rows, symbol_code="NIFTY")
    closes = {r["close"] for r in cleaned}
    assert 57831.0 not in closes           # gross cross-symbol frame dropped
    assert 24090.0 not in closes           # contaminated-high bar dropped
    assert {24075.0, 24100.0, 24088.0} <= closes


def test_drop_contaminated_spot_rows_survives_majority_contamination() -> None:
    # A payload that is MOSTLY BANKNIFTY contamination: the poisonable median
    # would move onto 57.8k and start dropping the real NIFTY rows. The absolute
    # band anchors on the true level regardless of the contamination fraction.
    from market_data import index_band_guard

    index_band_guard.clear_reference_closes()
    drop = market_intelligence_module._drop_contaminated_spot_rows
    rows = [{"time": f"c{i}", "open": 57800.0, "high": 57840.0, "low": 57770.0, "close": 57831.0} for i in range(8)]
    rows += [{"time": f"n{i}", "open": 24050.0, "high": 24090.0, "low": 24010.0, "close": 24075.0} for i in range(3)]
    cleaned = drop(rows, symbol_code="NIFTY")
    assert cleaned, "real NIFTY rows must survive a contaminated-majority payload"
    assert all(r["close"] == 24075.0 for r in cleaned)
