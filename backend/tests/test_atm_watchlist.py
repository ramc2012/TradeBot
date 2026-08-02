from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import market_data.atm_watchlist as atm_watchlist_module
from brokers.base import OptionChain, OptionChainEntry
from freezegun import freeze_time
from market_data.atm_watchlist import ATMWatchlistService, UnderlyingMeta
from sqlalchemy.exc import ProgrammingError

# The get_expiries tests below hardcode a 2026-04/05/06 expiry ladder and assert that
# the nearest-monthly default resolves to 2026-05-26. That only holds when "today" is
# on/before that ladder; with the real clock those dates are in the past and the
# default rolls to the live monthly. Freeze "now" to a date inside the fixture window
# (before 2026-05-12) so the assertions are stable regardless of when the suite runs.
_EXPIRY_LADDER_TODAY = "2026-04-24"


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str):
        return self._values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


def test_premium_candle_fallback_selects_time_once(monkeypatch) -> None:
    statements: list[str] = []

    class Result:
        def mappings(self):
            return self

        def all(self):
            return []

        def fetchall(self):
            return []

    class Session:
        async def execute(self, statement, params=None):  # noqa: ANN001
            statements.append(str(statement))
            return Result()

    class Context:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    monkeypatch.setattr(atm_watchlist_module, "AsyncSessionLocal", Context)
    service = ATMWatchlistService()
    meta = UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|NIFTY", "NSE:NIFTY50-INDEX")

    asyncio.run(service._load_premium_candle_watchlist_rows("2026-07-28", [meta]))

    latest_contracts = statements[0].split("latest_spot AS", 1)[0]
    assert latest_contracts.count("time,") == 1

class _FakeFyersAdapter:
    async def get_option_contracts(self, symbol: str) -> list[dict]:
        return [
            {"expiry": "2026-04-23"},
            {"expiry": "2026-05-28"},
        ]

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=55912.75,
            entries=[
                OptionChainEntry(
                    strike=55900.0,
                    option_type="CE",
                    ltp=212.0,
                    oi=100,
                    volume=120,
                    bid=211.5,
                    ask=212.5,
                    prev_close=198.0,
                    prev_oi=90,
                    instrument_key="FYERS-CE",
                ),
                OptionChainEntry(
                    strike=55900.0,
                    option_type="PE",
                    ltp=188.0,
                    oi=110,
                    volume=100,
                    bid=187.5,
                    ask=188.5,
                    prev_close=176.0,
                    prev_oi=95,
                    instrument_key="FYERS-PE",
                ),
            ],
        )


async def _fake_get_redis(redis: _FakeRedis) -> _FakeRedis:
    return redis


def test_get_broker_expiries_reuses_persisted_ladder_when_brokers_are_unavailable(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    meta = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )

    async def fake_load_persisted(symbol: str) -> list[str]:
        assert symbol == "BANKNIFTY"
        return ["2026-04-28", "2026-05-26"]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)

    expiries = asyncio.run(service._get_broker_expiries_for_symbol(meta, None, None))

    assert expiries == ["2026-04-28", "2026-05-26"]
    assert redis._values["atm_watchlist:sym_expiries:v2:BANKNIFTY"]


def test_get_broker_expiries_prefers_live_fyers_ladder_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    meta = UnderlyingMeta(
        symbol="NIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        underlying_key="NSE_INDEX|Nifty 50",
    )

    async def fake_load_persisted(symbol: str) -> list[str]:
        assert symbol == "NIFTY"
        return ["2026-04-28", "2026-05-26"]

    # NSE indices expire Tuesday; a Thursday NIFTY series is a phantom that the ladder
    # filter now rejects. Use a live fyers ladder of VALID Tuesday expiries that differs
    # from the persisted ladder (the 06-30 is the tell that live-fyers was preferred).
    class _ValidNseFyersAdapter(_FakeFyersAdapter):
        async def get_option_contracts(self, symbol: str) -> list[dict]:
            return [{"expiry": "2026-04-28"}, {"expiry": "2026-06-30"}]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)

    expiries = asyncio.run(service._get_broker_expiries_for_symbol(meta, None, _ValidNseFyersAdapter()))

    assert expiries == ["2026-04-28", "2026-06-30"]
    assert redis._values["atm_watchlist:sym_expiries:v2:NIFTY"] == json.dumps(
        {"expiries": expiries, "source": "fyers"}
    )


def test_build_row_uses_saved_contract_metadata_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    meta = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )
    selected_expiry = "2026-04-28"
    resolved_expiry = atm_watchlist_module._index_monthly_for_selected_expiry(
        "BANKNIFTY",
        date.fromisoformat(selected_expiry),
    ).isoformat()

    async def fake_get_expiries(meta_arg: UnderlyingMeta, upstox_adapter, fyers_adapter) -> list[str]:
        assert meta_arg.symbol == "BANKNIFTY"
        return [selected_expiry, "2026-05-26"]

    async def fake_get_contracts(meta_arg: UnderlyingMeta, expiry_arg: str, upstox_adapter) -> list[dict]:
        assert meta_arg.symbol == "BANKNIFTY"
        assert expiry_arg == resolved_expiry
        assert upstox_adapter is None
        return [
            {
                "instrument_key": "UPSTOX-CE",
                "trading_symbol": "BANKNIFTY26APR55900CE",
                "strike_price": 55900.0,
                "instrument_type": "CE",
                "expiry": resolved_expiry,
                "lot_size": 30,
            },
            {
                "instrument_key": "UPSTOX-PE",
                "trading_symbol": "BANKNIFTY26APR55900PE",
                "strike_price": 55900.0,
                "instrument_type": "PE",
                "expiry": resolved_expiry,
                "lot_size": 30,
            },
        ]

    async def fake_load_technicals(**kwargs) -> dict:
        return {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}

    async def fake_persist_snapshot(**kwargs) -> None:
        return None

    persisted_lot_sizes: list[tuple[str, int]] = []

    async def fake_persist_lot_size(symbol: str, lot_size: int) -> None:
        persisted_lot_sizes.append((symbol, lot_size))

    monkeypatch.setattr(service, "_get_broker_expiries_for_symbol", fake_get_expiries)
    monkeypatch.setattr(service, "_get_contracts_for_expiry", fake_get_contracts)
    monkeypatch.setattr(service, "_load_technicals", fake_load_technicals)
    monkeypatch.setattr(service, "_persist_snapshot", fake_persist_snapshot)
    monkeypatch.setattr(service, "_persist_lot_size", fake_persist_lot_size)

    row = asyncio.run(
            service._build_row(
                meta,
                selected_expiry,
                date.fromisoformat(selected_expiry),
                None,
                _FakeFyersAdapter(),
            )
        )

    assert row is not None
    assert row["expiry"] == resolved_expiry
    assert row["live_source"] == "fyers"
    assert row["lot_size"] == 30
    assert row["ce"]["instrument_key"] == "UPSTOX-CE"
    assert row["ce"]["trading_symbol"] == "BANKNIFTY26APR55900CE"
    assert row["pe"]["instrument_key"] == "UPSTOX-PE"
    assert persisted_lot_sizes == [("BANKNIFTY", 30)]


def test_build_row_keeps_selected_weekly_expiry_for_nse_indices(monkeypatch) -> None:
    service = ATMWatchlistService()
    meta = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )
    selected_expiry = "2026-04-21"

    async def fake_get_contracts(meta_arg: UnderlyingMeta, expiry_arg: str, upstox_adapter) -> list[dict]:
        assert meta_arg.symbol == "BANKNIFTY"
        assert expiry_arg == selected_expiry
        return [
            {
                "instrument_key": "UPSTOX-CE",
                "trading_symbol": "BANKNIFTY26APR55900CE",
                "strike_price": 55900.0,
                "instrument_type": "CE",
                "expiry": selected_expiry,
                "lot_size": 30,
            },
            {
                "instrument_key": "UPSTOX-PE",
                "trading_symbol": "BANKNIFTY26APR55900PE",
                "strike_price": 55900.0,
                "instrument_type": "PE",
                "expiry": selected_expiry,
                "lot_size": 30,
            },
        ]

    monkeypatch.setattr(service, "_get_contracts_for_expiry", fake_get_contracts)
    monkeypatch.setattr(service, "_load_technicals", lambda **kwargs: _async_payload({"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}))
    monkeypatch.setattr(service, "_persist_snapshot", lambda **kwargs: _async_payload(None))
    monkeypatch.setattr(service, "_persist_lot_size", lambda symbol, lot_size: _async_payload(None))

    row = asyncio.run(
        service._build_row(
            meta,
            selected_expiry,
            date.fromisoformat(selected_expiry),
            None,
            _FakeFyersAdapter(),
        )
    )

    assert row is not None
    assert row["expiry"] == selected_expiry


@freeze_time(_EXPIRY_LADDER_TODAY)
def test_get_expiries_reports_catalog_fallback_when_saved_ladder_is_used(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("FINNIFTY", "INDEX", "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Fin Service"),
            UnderlyingMeta("MIDCPNIFTY", "INDEX", "NSE_INDEX|Nifty Midcap Select", "NSE_INDEX|Nifty Midcap Select"),
            UnderlyingMeta("SENSEX", "INDEX", "BSE_INDEX|SENSEX", "BSE_INDEX|SENSEX"),
        ]

    async def fake_get_upstox_adapter():
        return None

    async def fake_ensure_fyers_session(*args, **kwargs) -> bool:
        return False

    async def fake_broker_expiries(meta: UnderlyingMeta, *args, **kwargs) -> tuple[list[str], str]:
        ladders = {
            "NIFTY": ["2026-05-26", "2026-06-30"],
            "BANKNIFTY": ["2026-05-26", "2026-06-30"],
            "FINNIFTY": ["2026-05-26", "2026-06-30"],
            "MIDCPNIFTY": ["2026-05-26", "2026-06-30"],
            "SENSEX": ["2026-05-29", "2026-06-26"],
        }
        return ladders[meta.symbol], "catalog"

    async def fake_load_persisted(symbol: str) -> list[str]:
        ladders = {
            "NIFTY": ["2026-05-26", "2026-06-30"],
            "BANKNIFTY": ["2026-05-26", "2026-06-30"],
            "FINNIFTY": ["2026-05-26", "2026-06-30"],
            "MIDCPNIFTY": ["2026-05-26", "2026-06-30"],
            "SENSEX": ["2026-05-29", "2026-06-26"],
        }
        return ladders[symbol]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", fake_ensure_fyers_session)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(service, "_get_broker_expiry_snapshot_for_symbol", fake_broker_expiries)

    payload = asyncio.run(service.get_expiries(live_refresh=False))

    assert payload["source"] == "catalog"
    assert payload["default_expiry"] == "2026-05-26"
    assert "saved expiry catalog" in str(payload["detail"])
    assert "2026-05-26" in payload["expiries"]
    assert "BNKN 2026-05-26" in payload["expiry_scope_note"]


@freeze_time(_EXPIRY_LADDER_TODAY)
def test_get_expiries_prefers_live_fyers_ladders_over_saved_catalog_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    class _LiveFyersAdapter:
        async def get_option_contracts(self, symbol: str) -> list[dict]:
            ladders = {
                "NSE:NIFTY50-INDEX": ["2026-05-12", "2026-05-26", "2026-06-30"],
                "NSE:NIFTYBANK-INDEX": ["2026-05-12", "2026-05-26", "2026-06-30"],
                "NSE:FINNIFTY-INDEX": ["2026-05-12", "2026-05-26", "2026-06-30"],
                "NSE:MIDCPNIFTY-INDEX": ["2026-05-12", "2026-05-26", "2026-06-30"],
                "BSE:SENSEX-INDEX": ["2026-05-29", "2026-06-26"],
            }
            return [{"expiry": expiry} for expiry in ladders[symbol]]

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("FINNIFTY", "INDEX", "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Fin Service"),
            UnderlyingMeta("MIDCPNIFTY", "INDEX", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|NIFTY MID SELECT"),
            UnderlyingMeta("SENSEX", "INDEX", "BSE_INDEX|SENSEX", "BSE_INDEX|SENSEX"),
        ]

    async def fake_get_upstox_adapter():
        return None

    async def fake_load_persisted(symbol: str) -> list[str]:
        stale = {
            "NIFTY": ["2026-05-26", "2026-06-30"],
            "BANKNIFTY": ["2026-05-26", "2026-06-30"],
            "FINNIFTY": ["2026-05-26", "2026-06-30"],
            "MIDCPNIFTY": ["2026-05-26", "2026-06-30"],
            "SENSEX": ["2026-05-29", "2026-06-26"],
        }
        return stale[symbol]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: _LiveFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda *args, **kwargs: _async_payload(False))
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)

    payload = asyncio.run(service.get_expiries(live_refresh=True))

    assert payload["source"] == "fyers"
    assert payload["default_expiry"] == "2026-05-26"
    assert payload["index_monthlies"]["NIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["BANKNIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["FINNIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["MIDCPNIFTY"] == "2026-05-26"
    # SENSEX monthly expiry moved Friday -> Thursday (commit 92743411). The last
    # Thursday of May 2026 (05-28) is Bakri Id on both exchanges, so the
    # calendar-driven policy (core.expiry_policy) walks back one session to
    # Wednesday 2026-05-27 — this was one of the dates the old hand-maintained
    # holiday set got wrong (see the expiry_policy module docstring).
    assert payload["index_monthlies"]["SENSEX"] == "2026-05-27"
    assert payload["detail"] is None


@freeze_time(_EXPIRY_LADDER_TODAY)
def test_get_expiries_uses_common_nse_monthlies_even_when_live_ladders_are_stale(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("FINNIFTY", "INDEX", "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Fin Service"),
            UnderlyingMeta("MIDCPNIFTY", "INDEX", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|NIFTY MID SELECT"),
            UnderlyingMeta("SENSEX", "INDEX", "BSE_INDEX|SENSEX", "BSE_INDEX|SENSEX"),
        ]

    class _UniformUpstoxAdapter:
        async def get_option_contracts(self, _symbol: str) -> list[dict]:
            return [{"expiry": expiry} for expiry in ["2026-05-12", "2026-05-26", "2026-06-30"]]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda *args, **kwargs: _async_payload(False))
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", lambda symbol: _async_payload([]))
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(_UniformUpstoxAdapter()))

    payload = asyncio.run(service.get_expiries(live_refresh=True))

    assert payload["source"] == "upstox"
    assert payload["expiries"] == ["2026-05-12", "2026-05-26", "2026-06-30"]
    assert payload["default_expiry"] == "2026-05-26"
    assert payload["index_monthlies"]["NIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["BANKNIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["FINNIFTY"] == "2026-05-26"
    assert payload["index_monthlies"]["MIDCPNIFTY"] == "2026-05-26"
    # SENSEX monthly expiry moved Friday -> Thursday (commit 92743411). The last
    # Thursday of May 2026 (05-28) is Bakri Id on both exchanges, so the
    # calendar-driven policy (core.expiry_policy) walks back one session to
    # Wednesday 2026-05-27 — this was one of the dates the old hand-maintained
    # holiday set got wrong (see the expiry_policy module docstring).
    assert payload["index_monthlies"]["SENSEX"] == "2026-05-27"


def test_get_watchlist_returns_building_payload_on_first_load(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    scheduled = []

    async def fake_get_expiries(_expiry: str | None = None, *, live_refresh: bool = False) -> dict[str, str]:
        return {"default_expiry": "2026-04-28"}

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("RELIANCE", "STOCK", "NSE_EQ|RELIANCE", "NSE_EQ|RELIANCE"),
        ]

    async def fake_get_upstox_adapter():
        return None

    async def fake_build_row(meta: UnderlyingMeta, expiry: str, expiry_date: date, upstox_adapter, fyers_adapter):
        return {
            "underlying": meta.symbol,
            "kind": meta.kind,
            "spot_price": 100.0,
            "expiry": expiry,
            "atm_strike": 100.0,
            "live_source": "fyers",
            "ce": {"instrument_key": f"{meta.symbol}-CE", "option_type": "CE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
            "pe": {"instrument_key": f"{meta.symbol}-PE", "option_type": "PE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
        }

    def fake_ensure_future(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: _FakeFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(atm_watchlist_module.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(service, "get_expiries", fake_get_expiries)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(service, "_load_persisted_watchlist_rows", lambda expiry, underlyings: _async_payload([]))
    monkeypatch.setattr(service, "_build_row", fake_build_row)

    payload = asyncio.run(service.get_watchlist("2026-04-28", live_refresh=True))

    assert payload["build_status"] == "building"
    assert payload["summary"]["total_rows"] == 2
    assert payload["rows"][0]["underlying"] in {"BANKNIFTY", "NIFTY"}
    assert "Building 1 remaining symbols in background." in str(payload["detail"])
    assert scheduled


def test_get_watchlist_returns_cached_building_payload_without_rebuild(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    cache_key = f"atm_watchlist:{atm_watchlist_module.WATCHLIST_CACHE_VERSION}:local:2026-04-28:all"
    cached_payload = {
        "expiry": "2026-04-28",
        "rows": [{"underlying": "NIFTY", "kind": "INDEX"}],
        "summary": {"total_rows": 1, "ce_ready": 0, "pe_ready": 0},
        "source": "snapshot",
        "detail": "Building remaining symbols in background.",
        "build_status": "building",
        "timestamp": "2026-04-11T10:00:00+00:00",
    }
    redis._values[cache_key] = json.dumps(cached_payload)

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "get_expiries", lambda _expiry=None, live_refresh=False: _async_payload({"default_expiry": "2026-04-28"}))

    payload = asyncio.run(service.get_watchlist("2026-04-28"))

    assert payload == cached_payload
    assert redis._values[cache_key] == json.dumps(cached_payload)


def test_get_watchlist_filters_scoped_rows_from_full_cache(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    full_cache_key = f"atm_watchlist:{atm_watchlist_module.WATCHLIST_CACHE_VERSION}:live:2026-04-28:all"
    cached_payload = {
        "expiry": "2026-04-28",
        "rows": [
            {
                "underlying": "NIFTY",
                "kind": "INDEX",
                "live_source": "fyers",
                "ce": {"option_type": "CE"},
                "pe": {"option_type": "PE"},
            },
            {
                "underlying": "RELIANCE",
                "kind": "STOCK",
                "live_source": "upstox",
                "ce": {"option_type": "CE"},
                "pe": None,
            },
        ],
        "summary": {"total_rows": 2, "ce_ready": 2, "pe_ready": 1},
        "source": "snapshot",
        "detail": None,
        "build_status": "ready",
        "timestamp": "2026-04-11T10:00:00+00:00",
    }
    redis._values[full_cache_key] = json.dumps(cached_payload)

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "get_expiries", lambda _expiry=None, live_refresh=False: _async_payload({"default_expiry": "2026-04-28"}))

    payload = asyncio.run(service.get_watchlist("2026-04-28", ["NIFTY"]))

    assert [row["underlying"] for row in payload["rows"]] == ["NIFTY"]
    assert payload["summary"]["total_rows"] == 1
    assert payload["summary"]["fyers_rows"] == 1


def test_get_watchlist_uses_persisted_snapshot_board_when_brokers_are_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    async def fake_get_expiries(_expiry: str | None = None, *, live_refresh: bool = False) -> dict:
        return {"default_expiry": "2026-04-28"}

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
        ]

    async def fake_load_persisted_rows(expiry: str, underlyings: list[UnderlyingMeta]) -> list[dict]:
        assert expiry == "2026-04-28"
        assert len(underlyings) == 2
        return [
            {
                "underlying": "BANKNIFTY",
                "kind": "INDEX",
                "spot_price": 55912.75,
                "expiry": "2026-04-28",
                "atm_strike": 55900.0,
                "live_source": "fyers",
                "fyers_symbol": "NSE:NIFTYBANK-INDEX",
                "lot_size": 30,
                "ce": {"ltp": 212.0, "option_type": "CE", "strike": 55900.0},
                "pe": {"ltp": 188.0, "option_type": "PE", "strike": 55900.0},
            }
        ]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "get_expiries", fake_get_expiries)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_watchlist_rows", fake_load_persisted_rows)
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(None))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda **kwargs: _async_payload(False))

    payload = asyncio.run(service.get_watchlist("2026-04-28"))

    assert payload["source"] == "snapshot"
    assert payload["build_status"] == "ready"
    assert payload["summary"]["total_rows"] == 1
    assert payload["rows"][0]["underlying"] == "BANKNIFTY"
    assert "last saved ATM watchlist" in payload["detail"]


def test_get_watchlist_refreshes_stale_full_persisted_board(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    scheduled = []

    async def fake_get_expiries(_expiry: str | None = None, *, live_refresh: bool = False) -> dict:
        return {"default_expiry": "2026-05-26"}

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("RELIANCE", "STOCK", "NSE_EQ|RELIANCE", "NSE_EQ|RELIANCE"),
            UnderlyingMeta("VEDL", "STOCK", "NSE_EQ|VEDL", "NSE_EQ|VEDL"),
        ]

    async def fake_load_persisted_rows(expiry: str, underlyings: list[UnderlyingMeta]) -> list[dict]:
        stale_as_of = datetime(2026, 4, 24, 6, 26, tzinfo=timezone.utc).isoformat()
        return [
            {
                "underlying": meta.symbol,
                "kind": meta.kind,
                "spot_price": 100.0,
                "as_of": stale_as_of,
                "expiry": expiry,
                "atm_strike": 100.0,
                "live_source": "snapshot",
                "fyers_symbol": f"NSE:{meta.symbol}-EQ",
                "lot_size": 1,
                "ce": {"ltp": 10.0, "option_type": "CE", "strike": 100.0, "as_of": stale_as_of},
                "pe": {"ltp": 11.0, "option_type": "PE", "strike": 100.0, "as_of": stale_as_of},
            }
            for meta in underlyings
        ]

    def fake_ensure_future(coro):
        scheduled.append(coro)
        coro.close()
        return None

    async def fake_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        # Keep the seed build deterministic and DB-free: the stale stock rows have
        # been force-dropped and are being rebuilt, so nothing is ready yet.
        return None

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: _FakeFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(atm_watchlist_module.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(service, "get_expiries", fake_get_expiries)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(None))
    monkeypatch.setattr(service, "_load_persisted_watchlist_rows", fake_load_persisted_rows)
    monkeypatch.setattr(service, "_build_row", fake_build_row)

    payload = asyncio.run(service.get_watchlist("2026-05-26", live_refresh=True))

    # The saved board is stale (as_of 2026-04-24, older than today's 09:15 session
    # open), so the force-refresh path (commit 3b04d0f7, 2026-05-18: "force live
    # refresh past stale prior_rows") drops the stale rows into the pending set and
    # rebuilds them in the background instead of surfacing them. The immediate payload
    # is therefore "building" with the stale rows removed and a rebuild scheduled.
    assert payload["build_status"] == "building"
    assert payload["summary"]["total_rows"] == 0
    assert "Building 2 remaining symbols in background" in str(payload["detail"])
    assert scheduled


def test_live_refresh_ignores_stale_cached_watchlist_payload(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    stale_as_of = datetime(2026, 4, 24, 6, 26, tzinfo=timezone.utc).isoformat()
    cache_key = f"atm_watchlist:{atm_watchlist_module.WATCHLIST_CACHE_VERSION}:live:2026-05-26:scope:VEDL"
    stale_row = {
        "underlying": "VEDL",
        "kind": "STOCK",
        "spot_price": 711.0,
        "as_of": stale_as_of,
        "expiry": "2026-05-26",
        "atm_strike": 710.0,
        "live_source": "snapshot",
        "ce": {"ltp": 17.0, "option_type": "CE", "strike": 710.0, "as_of": stale_as_of},
        "pe": {"ltp": 14.6, "option_type": "PE", "strike": 710.0, "as_of": stale_as_of},
    }
    redis._values[cache_key] = json.dumps({
        "expiry": "2026-05-26",
        "rows": [stale_row],
        "summary": {"total_rows": 1, "ce_ready": 1, "pe_ready": 1},
        "source": "snapshot",
        "detail": "cached stale payload",
        "build_status": "ready",
        "timestamp": stale_as_of,
    })

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda **kwargs: _async_payload(False))
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(None))
    monkeypatch.setattr(service, "get_expiries", lambda _expiry=None, live_refresh=False: _async_payload({"default_expiry": "2026-05-26"}))
    monkeypatch.setattr(service, "_load_underlyings", lambda: _async_payload([
        UnderlyingMeta("VEDL", "STOCK", "NSE_EQ|VEDL", "NSE_EQ|VEDL"),
    ]))
    monkeypatch.setattr(service, "_load_persisted_watchlist_rows", lambda expiry, underlyings: _async_payload([stale_row]))

    payload = asyncio.run(service.get_watchlist("2026-05-26", ["VEDL"], live_refresh=True))

    assert payload["build_status"] == "stale"
    assert payload["detail"] != "cached stale payload"
    assert "saved ATM watchlist board is stale" in payload["detail"]


def test_get_upstox_adapter_uses_analytics_token_when_session_is_expired(monkeypatch) -> None:
    service = ATMWatchlistService()
    authenticated: list[str] = []

    class _FakeUpstoxAdapter:
        async def authenticate(self, credentials: dict):
            authenticated.append(credentials["access_token"])
            return SimpleNamespace(access_token=credentials["access_token"])

    monkeypatch.setattr(atm_watchlist_module, "ensure_upstox_session", lambda **kwargs: _async_payload(False))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "get_broker_token", lambda broker: "analytics-token" if broker == "upstox" else "")
    monkeypatch.setattr(atm_watchlist_module, "UpstoxAdapter", _FakeUpstoxAdapter)

    adapter = asyncio.run(service._get_upstox_adapter())

    assert isinstance(adapter, _FakeUpstoxAdapter)
    assert authenticated == ["analytics-token"]


def test_load_persisted_watchlist_rows_falls_back_when_underlying_lot_size_column_is_missing(monkeypatch) -> None:
    service = ATMWatchlistService()
    underlyings = [
        UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
    ]

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeSession:
        def __init__(self):
            self.calls = 0
            self.rolled_back = False

        async def execute(self, statement, params):
            self.calls += 1
            sql = str(statement)
            assert params["expiry"] == date(2026, 4, 30)
            if self.calls == 1:
                assert "catalog.lot_size" in sql
                raise ProgrammingError(sql, params, Exception("column catalog.lot_size does not exist"))
            assert "NULL::INTEGER AS lot_size" in sql
            return _FakeResult(
                [
                    SimpleNamespace(
                        underlying="NIFTY",
                        kind="INDEX",
                        expiry=date(2026, 4, 30),
                        strike=23850.0,
                        source_broker="fyers",
                        underlying_price=23842.65,
                        lot_size=None,
                        ce_instrument_key="NIFTY-CE",
                        ce_trading_symbol="NIFTY26APR23850CE",
                        ce_ltp=240.5,
                        ce_prev_close=220.0,
                        ce_change=20.5,
                        ce_change_pct=9.32,
                        ce_oi=100,
                        ce_prev_oi=90,
                        ce_oi_change=10,
                        ce_oi_change_pct=11.11,
                        ce_volume=1000,
                        ce_iv=0.2247,
                        ce_macd=1.2,
                        ce_macd_signal=0.8,
                        ce_macd_histogram=0.4,
                        ce_rsi=61.0,
                        pe_instrument_key="NIFTY-PE",
                        pe_trading_symbol="NIFTY26APR23850PE",
                        pe_ltp=180.25,
                        pe_prev_close=170.0,
                        pe_change=10.25,
                        pe_change_pct=6.03,
                        pe_oi=120,
                        pe_prev_oi=110,
                        pe_oi_change=10,
                        pe_oi_change_pct=9.09,
                        pe_volume=900,
                        pe_iv=0.2311,
                        pe_macd=-0.7,
                        pe_macd_signal=-0.9,
                        pe_macd_histogram=0.2,
                        pe_rsi=48.0,
                    )
                ]
            )

        async def rollback(self):
            self.rolled_back = True

    fake_context = None

    class _FakeSessionContext:
        def __init__(self):
            self.session = _FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _factory():
        nonlocal fake_context
        fake_context = _FakeSessionContext()
        return fake_context

    monkeypatch.setattr(atm_watchlist_module, "AsyncSessionLocal", _factory)

    rows = asyncio.run(service._load_persisted_watchlist_rows("2026-04-30", underlyings))

    assert len(rows) == 1
    assert rows[0]["underlying"] == "NIFTY"
    assert rows[0]["lot_size"] is None
    assert rows[0]["ce"]["instrument_key"] == "NIFTY-CE"
    assert rows[0]["pe"]["instrument_key"] == "NIFTY-PE"
    assert fake_context is not None and fake_context.session.rolled_back is True


def test_load_persisted_watchlist_rows_filters_to_requested_underlyings(monkeypatch) -> None:
    service = ATMWatchlistService()
    underlyings = [
        UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
    ]

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeSession:
        async def execute(self, _statement, params):
            assert params["expiry"] == date(2026, 4, 30)
            assert params["underlyings"] == ["NIFTY"]
            return _FakeResult(
                [
                    SimpleNamespace(
                        underlying="NIFTY",
                        kind="INDEX",
                        expiry=date(2026, 4, 30),
                        strike=23850.0,
                        source_broker="fyers",
                        underlying_price=23842.65,
                        lot_size=75,
                        ce_instrument_key="NIFTY-CE",
                        ce_trading_symbol="NIFTY26APR23850CE",
                        ce_ltp=240.5,
                        ce_prev_close=220.0,
                        ce_change=20.5,
                        ce_change_pct=9.32,
                        ce_oi=100,
                        ce_prev_oi=90,
                        ce_oi_change=10,
                        ce_oi_change_pct=11.11,
                        ce_volume=1000,
                        ce_iv=0.2247,
                        ce_macd=1.2,
                        ce_macd_signal=0.8,
                        ce_macd_histogram=0.4,
                        ce_rsi=61.0,
                        pe_instrument_key="NIFTY-PE",
                        pe_trading_symbol="NIFTY26APR23850PE",
                        pe_ltp=180.25,
                        pe_prev_close=170.0,
                        pe_change=10.25,
                        pe_change_pct=6.03,
                        pe_oi=120,
                        pe_prev_oi=110,
                        pe_oi_change=10,
                        pe_oi_change_pct=9.09,
                        pe_volume=900,
                        pe_iv=0.2311,
                        pe_macd=-0.7,
                        pe_macd_signal=-0.9,
                        pe_macd_histogram=0.2,
                        pe_rsi=48.0,
                    ),
                    SimpleNamespace(
                        underlying="BANKNIFTY",
                        kind="INDEX",
                        expiry=date(2026, 4, 30),
                        strike=55900.0,
                        source_broker="fyers",
                        underlying_price=55912.75,
                        lot_size=30,
                        ce_instrument_key="BANKNIFTY-CE",
                        ce_trading_symbol="BANKNIFTY26APR55900CE",
                        ce_ltp=212.0,
                        ce_prev_close=198.0,
                        ce_change=14.0,
                        ce_change_pct=7.07,
                        ce_oi=100,
                        ce_prev_oi=90,
                        ce_oi_change=10,
                        ce_oi_change_pct=11.11,
                        ce_volume=120,
                        ce_iv=0.201,
                        ce_macd=1.0,
                        ce_macd_signal=0.7,
                        ce_macd_histogram=0.3,
                        ce_rsi=58.0,
                        pe_instrument_key="BANKNIFTY-PE",
                        pe_trading_symbol="BANKNIFTY26APR55900PE",
                        pe_ltp=188.0,
                        pe_prev_close=176.0,
                        pe_change=12.0,
                        pe_change_pct=6.82,
                        pe_oi=110,
                        pe_prev_oi=95,
                        pe_oi_change=15,
                        pe_oi_change_pct=15.79,
                        pe_volume=100,
                        pe_iv=0.215,
                        pe_macd=-0.4,
                        pe_macd_signal=-0.6,
                        pe_macd_histogram=0.2,
                        pe_rsi=46.0,
                    ),
                ]
            )

    class _FakeSessionContext:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(atm_watchlist_module, "AsyncSessionLocal", lambda: _FakeSessionContext())

    rows = asyncio.run(service._load_persisted_watchlist_rows("2026-04-30", underlyings))

    assert len(rows) == 1
    assert rows[0]["underlying"] == "NIFTY"
    assert rows[0]["lot_size"] == 75


def test_build_row_keeps_selected_weekly_for_nse_indices_and_monthly_for_stocks(monkeypatch) -> None:
    service = ATMWatchlistService()
    fyers = _FakeFyersAdapter()
    selected_expiry = date(2026, 5, 5)

    banknifty = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )
    reliance = UnderlyingMeta(
        symbol="RELIANCE",
        kind="STOCK",
        spot_instrument_key="NSE_EQ|RELIANCE",
        underlying_key="NSE_EQ|RELIANCE",
    )

    async def fake_get_contracts(meta_arg: UnderlyingMeta, expiry_arg: str, _upstox_adapter) -> list[dict]:
        expected = (
            "2026-05-05"
            if meta_arg.symbol == "BANKNIFTY"
            else atm_watchlist_module._stock_monthly_for_selected_expiry(selected_expiry).isoformat()
        )
        assert expiry_arg == expected
        return [
            {
                "instrument_key": f"{meta_arg.symbol}-CE",
                "trading_symbol": f"{meta_arg.symbol}-{expiry_arg}-CE",
                "strike_price": 55900.0,
                "instrument_type": "CE",
                "expiry": expiry_arg,
                "lot_size": 30 if meta_arg.symbol == "BANKNIFTY" else 250,
            },
            {
                "instrument_key": f"{meta_arg.symbol}-PE",
                "trading_symbol": f"{meta_arg.symbol}-{expiry_arg}-PE",
                "strike_price": 55900.0,
                "instrument_type": "PE",
                "expiry": expiry_arg,
                "lot_size": 30 if meta_arg.symbol == "BANKNIFTY" else 250,
            },
        ]

    monkeypatch.setattr(service, "_get_contracts_for_expiry", fake_get_contracts)
    monkeypatch.setattr(service, "_load_technicals", lambda **kwargs: _async_payload({"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}))
    monkeypatch.setattr(service, "_persist_snapshot", lambda **kwargs: _async_payload(None))
    monkeypatch.setattr(service, "_persist_lot_size", lambda symbol, lot_size: _async_payload(None))

    bank_row = asyncio.run(service._build_row(banknifty, "2026-05-05", selected_expiry, None, fyers))
    stock_row = asyncio.run(service._build_row(reliance, "2026-05-05", selected_expiry, None, fyers))

    assert bank_row is not None
    assert bank_row["expiry"] == "2026-05-05"
    assert stock_row is not None
    assert stock_row["expiry"] == atm_watchlist_module._stock_monthly_for_selected_expiry(selected_expiry).isoformat()


def test_load_underlyings_falls_back_to_default_index_universe_when_catalog_is_empty(monkeypatch) -> None:
    service = ATMWatchlistService()

    class _FakeResult:
        def fetchall(self):
            return []

    class _FakeSession:
        async def execute(self, *_args, **_kwargs):
            return _FakeResult()

    class _FakeSessionContext:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(atm_watchlist_module, "AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(
        atm_watchlist_module,
        "ensure_fo_underlying_catalog",
        lambda **_kwargs: _async_payload({"status": "skipped_no_upstox"}),
    )

    rows = asyncio.run(service._load_underlyings())

    assert [row.symbol for row in rows] == ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    assert rows[0].spot_instrument_key == "NSE_INDEX|Nifty 50"
    assert rows[3].underlying_key == "NSE_INDEX|NIFTY MID SELECT"
    assert rows[4].underlying_key == "BSE_INDEX|SENSEX"


def test_load_underlyings_bootstraps_when_catalog_has_only_indices(monkeypatch) -> None:
    service = ATMWatchlistService()
    state = {"bootstrapped": False}

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Row:
        def __init__(self, symbol, kind, spot_instrument_key, underlying_key):
            self.symbol = symbol
            self.kind = kind
            self.spot_instrument_key = spot_instrument_key
            self.underlying_key = underlying_key

    class _FakeSession:
        async def execute(self, *_args, **_kwargs):
            if not state["bootstrapped"]:
                rows = [
                    _Row("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
                    _Row("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
                    _Row("FINNIFTY", "INDEX", "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Fin Service"),
                    _Row("MIDCPNIFTY", "INDEX", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|NIFTY MID SELECT"),
                    _Row("SENSEX", "INDEX", "BSE_INDEX|SENSEX", "BSE_INDEX|SENSEX"),
                ]
            else:
                rows = [
                    _Row("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
                    _Row("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
                    _Row("RELIANCE", "STOCK", "NSE_EQ|INE002A01018", "NSE_EQ|INE002A01018"),
                ]
            return _FakeResult(rows)

    class _FakeSessionContext:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_bootstrap(**_kwargs):
        state["bootstrapped"] = True
        return {"status": "ready"}

    monkeypatch.setattr(atm_watchlist_module, "AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(atm_watchlist_module, "ensure_fo_underlying_catalog", fake_bootstrap)

    rows = asyncio.run(service._load_underlyings())

    assert [row.symbol for row in rows] == [
        "NIFTY",
        "BANKNIFTY",
        "FINNIFTY",
        "MIDCPNIFTY",
        "SENSEX",
        "RELIANCE",
    ]
    assert rows[4].underlying_key == "BSE_INDEX|SENSEX"


async def _async_payload(payload):
    return payload


# ── _extended_strike_window unit tests ──────────────────────────────────


def _make_chain_entry(strike: float, option_type: str, *, ltp: float = 0.0,
                      volume: int = 0, oi: int = 0) -> OptionChainEntry:
    return OptionChainEntry(
        strike=strike,
        option_type=option_type,
        ltp=ltp,
        oi=oi,
        volume=volume,
        bid=0.0,
        ask=0.0,
        instrument_key=f"NSE_FO|{int(strike)}{option_type}",
    )


def test_extended_strike_window_ce_3itm_1atm_6otm():
    """CE window. The band was widened 2026-06-29 to 8 ITM / 1 ATM / 8 OTM
    (was 3/1/6). This chain only supplies 3 ITM below and 7 OTM above the ATM,
    so the whole 11-strike chain is now returned (24700 is no longer trimmed)."""
    strikes = [23700.0, 23800.0, 23900.0, 24000.0, 24100.0, 24200.0,
               24300.0, 24400.0, 24500.0, 24600.0, 24700.0]
    entries = [_make_chain_entry(s, ot) for s in strikes for ot in ("CE", "PE")]
    window = atm_watchlist_module._extended_strike_window(
        sorted_strikes=strikes,
        atm_strike=24000.0,
        option_type="CE",
        chain_entries=entries,
    )
    assert len(window) == 11
    assert [w["strike"] for w in window] == [
        23700, 23800, 23900, 24000, 24100, 24200, 24300, 24400, 24500, 24600, 24700,
    ]
    atm_rows = [w for w in window if w["is_atm"]]
    assert len(atm_rows) == 1 and atm_rows[0]["strike"] == 24000.0


def test_extended_strike_window_pe_3itm_1atm_6otm():
    """PE side inverts the ITM/OTM relationship — higher strikes are ITM. With the
    widened 8/1/8 band this chain supplies only 4 ITM (above) and 6 OTM (below), so
    the whole 11-strike chain is returned (24400 is no longer trimmed)."""
    strikes = [23400.0, 23500.0, 23600.0, 23700.0, 23800.0, 23900.0,
               24000.0, 24100.0, 24200.0, 24300.0, 24400.0]
    entries = [_make_chain_entry(s, ot) for s in strikes for ot in ("CE", "PE")]
    window = atm_watchlist_module._extended_strike_window(
        sorted_strikes=strikes,
        atm_strike=24000.0,
        option_type="PE",
        chain_entries=entries,
    )
    assert len(window) == 11
    returned = [w["strike"] for w in window]
    assert 24400.0 in returned  # 4 ITM (above) now fit within the widened band
    assert 24100.0 in returned and 24200.0 in returned and 24300.0 in returned
    assert 24000.0 in returned
    for s in (23400.0, 23500.0, 23600.0, 23700.0, 23800.0, 23900.0):
        assert s in returned


def test_extended_strike_window_drops_strikes_without_chain_entry():
    """Strikes with no chain entry on the requested side are skipped."""
    strikes = [100.0, 110.0, 120.0]
    entries = [_make_chain_entry(s, "CE") for s in strikes]
    window = atm_watchlist_module._extended_strike_window(
        sorted_strikes=strikes,
        atm_strike=110.0,
        option_type="PE",
        chain_entries=entries,
    )
    assert window == []


def test_extended_strike_window_atm_at_low_edge_returns_partial():
    """ATM at the lowest strike means no ITM possible on CE side. Still returns
    ATM + however many OTM strikes are available (up to 8 with the widened band —
    here all 8 higher strikes fit)."""
    strikes = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0, 140.0]
    entries = [_make_chain_entry(s, "CE") for s in strikes]
    window = atm_watchlist_module._extended_strike_window(
        sorted_strikes=strikes,
        atm_strike=100.0,
        option_type="CE",
        chain_entries=entries,
    )
    assert [w["strike"] for w in window] == [
        100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0, 140.0,
    ]


def test_extended_strike_window_empty_inputs_safe():
    """Empty / invalid inputs return []; never raises."""
    assert atm_watchlist_module._extended_strike_window(
        sorted_strikes=[], atm_strike=100, option_type="CE", chain_entries=[]
    ) == []
    assert atm_watchlist_module._extended_strike_window(
        sorted_strikes=[100, 110], atm_strike=0, option_type="CE", chain_entries=[]
    ) == []
    assert atm_watchlist_module._extended_strike_window(
        sorted_strikes=[100, 110], atm_strike=100, option_type="XX", chain_entries=[]
    ) == []


def test_extended_strike_window_picks_atm_when_not_in_list():
    """If the ATM isn't exactly a listed strike, snap to nearest."""
    strikes = [23500.0, 23600.0, 23700.0, 23800.0, 23900.0, 24000.0,
               24100.0, 24200.0, 24300.0]
    entries = [_make_chain_entry(s, "CE") for s in strikes]
    # 23970 is closer to 24000 than to 23900 (30 vs 70)
    window = atm_watchlist_module._extended_strike_window(
        sorted_strikes=strikes,
        atm_strike=23970.0,
        option_type="CE",
        chain_entries=entries,
    )
    assert len(window) > 0
    atm_rows = [w for w in window if w["is_atm"]]
    assert len(atm_rows) == 1
    assert atm_rows[0]["strike"] == 24000.0


# ── Row-build admission fix (2026-07-15) ─────────────────────────────────────
# Tuesday 2026-07-14 stall: the per-row wait_for(75s) wrapped build() itself,
# so the deadline burned on OUR OWN chain-semaphore + rate-limiter queue waits
# and cancelled healthy-but-queued rows after their broker quota was already
# spent. The fix starts the deadline only AFTER semaphore admission and runs
# the row's broker calls at PRIORITY_HIGH. These tests pin that behaviour.


def _row_fix_scaffold(monkeypatch, service, redis, build_row):
    async def fake_get_expiries(_expiry=None, *, live_refresh=False):
        return {"default_expiry": "2026-04-28"}

    async def fake_load_underlyings():
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("RELIANCE", "STOCK", "NSE_EQ|RELIANCE", "NSE_EQ|RELIANCE"),
        ]

    async def fake_get_upstox_adapter():
        return None

    def fake_ensure_future(coro):
        coro.close()
        return None

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(
        atm_watchlist_module,
        "get_active_adapter",
        lambda broker: _FakeFyersAdapter() if broker == "fyers" else None,
    )
    monkeypatch.setattr(atm_watchlist_module.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(service, "get_expiries", fake_get_expiries)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(
        service, "_load_persisted_watchlist_rows", lambda expiry, underlyings: _async_payload([])
    )
    monkeypatch.setattr(service, "_build_row", build_row)


def _fake_row(meta: UnderlyingMeta, expiry: str) -> dict:
    return {
        "underlying": meta.symbol,
        "kind": meta.kind,
        "spot_price": 100.0,
        "expiry": expiry,
        "atm_strike": 100.0,
        "live_source": "fyers",
        "ce": {"instrument_key": f"{meta.symbol}-CE", "option_type": "CE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
        "pe": {"instrument_key": f"{meta.symbol}-PE", "option_type": "PE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
    }


def test_row_build_deadline_excludes_semaphore_wait(monkeypatch) -> None:
    """A row that waits LONGER than the row deadline for semaphore admission
    but builds quickly once admitted must SUCCEED (the old shape cancelled it
    while it was still queued behind our own semaphore)."""
    service = ATMWatchlistService()
    redis = _FakeRedis()
    monkeypatch.setattr(atm_watchlist_module, "ROW_BUILD_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(atm_watchlist_module, "SEED_PHASE_TIMEOUT_SECONDS", 10.0)

    async def fast_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        await asyncio.sleep(0.05)
        return _fake_row(meta, expiry)

    _row_fix_scaffold(monkeypatch, service, redis, fast_build_row)

    async def scenario():
        # Fresh semaphore bound to THIS loop; hold both permits well past the
        # 0.4s row deadline so every seed has to queue for admission first.
        semaphore = asyncio.Semaphore(2)
        monkeypatch.setattr(ATMWatchlistService, "_chain_semaphore", semaphore)
        await semaphore.acquire()
        await semaphore.acquire()

        async def release_later():
            await asyncio.sleep(0.9)
            semaphore.release()
            semaphore.release()

        # NB: the scaffold monkeypatches asyncio.ensure_future globally (to
        # swallow the BG build), so the holder task must use create_task.
        release_task = asyncio.get_running_loop().create_task(release_later())
        try:
            return await service.get_watchlist("2026-04-28", live_refresh=True)
        finally:
            await release_task

    payload = asyncio.run(scenario())
    built = {row["underlying"] for row in payload["rows"]}
    assert built == {"NIFTY", "BANKNIFTY"}  # both index seeds survived the queue wait


def test_row_build_timeout_applies_post_admission_and_skips_row(monkeypatch) -> None:
    """Once admitted, a row that overruns ROW_BUILD_TIMEOUT_SECONDS is skipped
    (returns None) without failing the build or poisoning other rows."""
    service = ATMWatchlistService()
    redis = _FakeRedis()
    monkeypatch.setattr(atm_watchlist_module, "ROW_BUILD_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(atm_watchlist_module, "SEED_PHASE_TIMEOUT_SECONDS", 10.0)

    async def mixed_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        if meta.symbol == "NIFTY":
            await asyncio.sleep(1.0)  # hung row — must be bounded post-admission
        return _fake_row(meta, expiry)

    _row_fix_scaffold(monkeypatch, service, redis, mixed_build_row)

    async def scenario():
        monkeypatch.setattr(ATMWatchlistService, "_chain_semaphore", asyncio.Semaphore(2))
        return await service.get_watchlist("2026-04-28", live_refresh=True)

    payload = asyncio.run(scenario())
    built = {row["underlying"] for row in payload["rows"]}
    assert built == {"BANKNIFTY"}  # NIFTY skipped, build carried on


def test_row_build_runs_at_high_broker_priority(monkeypatch) -> None:
    """Broker calls under _build_row must inherit PRIORITY_HIGH so the serial
    universe build out-ranks bulk premium-top-up/chain-sweep tickets."""
    from brokers.rate_limiter import PRIORITY_HIGH, _request_priority

    service = ATMWatchlistService()
    redis = _FakeRedis()
    seen: list[float] = []

    async def recording_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        seen.append(_request_priority.get())
        return _fake_row(meta, expiry)

    _row_fix_scaffold(monkeypatch, service, redis, recording_build_row)

    async def scenario():
        monkeypatch.setattr(ATMWatchlistService, "_chain_semaphore", asyncio.Semaphore(2))
        return await service.get_watchlist("2026-04-28", live_refresh=True)

    asyncio.run(scenario())
    assert seen and all(prio == PRIORITY_HIGH for prio in seen)


def test_seed_phase_deadline_keeps_completed_rows(monkeypatch) -> None:
    """The seed phase is bounded as a WHOLE; rows that completed before the
    phase deadline are kept, unfinished seeds are cancelled (and left for the
    background build), and the call returns promptly for the MI runner."""
    import time as _time

    service = ATMWatchlistService()
    redis = _FakeRedis()
    monkeypatch.setattr(atm_watchlist_module, "ROW_BUILD_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(atm_watchlist_module, "SEED_PHASE_TIMEOUT_SECONDS", 0.3)

    async def mixed_build_row(meta, expiry, expiry_date, upstox_adapter, fyers_adapter):
        if meta.symbol == "NIFTY":
            await asyncio.sleep(5.0)  # would blow the MI runner budget
        return _fake_row(meta, expiry)

    _row_fix_scaffold(monkeypatch, service, redis, mixed_build_row)

    async def scenario():
        monkeypatch.setattr(ATMWatchlistService, "_chain_semaphore", asyncio.Semaphore(2))
        return await service.get_watchlist("2026-04-28", live_refresh=True)

    started = _time.monotonic()
    payload = asyncio.run(scenario())
    elapsed = _time.monotonic() - started

    built = {row["underlying"] for row in payload["rows"]}
    assert built == {"BANKNIFTY"}  # completed seed preserved, hung seed dropped
    assert elapsed < 2.0  # phase bound respected (nowhere near the 5s hang)


# ── Live-refresh window (2026-07-16) ─────────────────────────────────────────
# Outside 07:00–16:35 IST on NSE session days every live_refresh degrades to
# cached/DB reads, so no caller (S1 closed-market prep, UI polls, …) can kick
# overnight full-universe broker rebuilds. NOTE: conftest disables the window
# suite-wide (`_LIVE_REFRESH_WINDOW_ENFORCED = False`) so the other
# live_refresh tests stay wall-clock independent — these tests re-enable it.


def test_live_refresh_allowed_window(monkeypatch) -> None:
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    monkeypatch.setattr(atm_watchlist_module, "_LIVE_REFRESH_WINDOW_ENFORCED", True)
    ist = _tz(_td(hours=5, minutes=30))

    def _at(y, m, d, hh, mm):
        return atm_watchlist_module._live_refresh_allowed(datetime(y, m, d, hh, mm, tzinfo=ist))

    # Tue 2026-07-14 is an NSE session day.
    assert _at(2026, 7, 14, 11, 0) is True     # in session
    assert _at(2026, 7, 14, 7, 30) is True     # pre-open prep band
    assert _at(2026, 7, 14, 16, 20) is True    # post-close catch-up grace
    assert _at(2026, 7, 14, 6, 59) is False    # before the prep band
    assert _at(2026, 7, 14, 16, 40) is False   # evening
    assert _at(2026, 7, 14, 2, 0) is False     # overnight (the 00:00–05:30 storm)
    # Sat 2026-07-18: never.
    assert _at(2026, 7, 18, 11, 0) is False


def test_live_refresh_window_not_enforced_flag(monkeypatch) -> None:
    monkeypatch.setattr(atm_watchlist_module, "_LIVE_REFRESH_WINDOW_ENFORCED", False)
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    ist = _tz(_td(hours=5, minutes=30))
    assert atm_watchlist_module._live_refresh_allowed(datetime(2026, 7, 18, 2, 0, tzinfo=ist)) is True


@freeze_time(_EXPIRY_LADDER_TODAY)
def test_get_expiries_demotes_live_refresh_outside_window(monkeypatch) -> None:
    """With the window closed, live_refresh=True must never touch a broker
    adapter — the build serves persisted/cached data instead."""
    service = ATMWatchlistService()
    redis = _FakeRedis()
    adapter_requests: list[str] = []

    def spy_get_active_adapter(broker: str):
        adapter_requests.append(broker)
        return None

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
        ]

    async def fake_load_persisted(symbol: str) -> list[str]:
        return ["2026-05-26", "2026-06-30"]

    monkeypatch.setattr(atm_watchlist_module, "_LIVE_REFRESH_WINDOW_ENFORCED", True)
    monkeypatch.setattr(atm_watchlist_module, "_live_refresh_allowed", lambda now=None: False)
    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", spy_get_active_adapter)
    monkeypatch.setattr(
        atm_watchlist_module,
        "ensure_fyers_session",
        lambda *args, **kwargs: _async_payload(False),
    )
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(None))

    payload = asyncio.run(service.get_expiries(live_refresh=True))

    assert adapter_requests == []  # no broker adapter acquisition attempted
    assert payload["default_expiry"] == "2026-05-26"
