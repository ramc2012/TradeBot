from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import market_data.atm_watchlist as atm_watchlist_module
from brokers.base import OptionChain, OptionChainEntry
from market_data.atm_watchlist import ATMWatchlistService, UnderlyingMeta
from sqlalchemy.exc import ProgrammingError


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str):
        return self._values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


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

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)

    expiries = asyncio.run(service._get_broker_expiries_for_symbol(meta, None, _FakeFyersAdapter()))

    assert expiries == ["2026-04-23", "2026-05-26"]
    assert redis._values["atm_watchlist:sym_expiries:v2:NIFTY"] == json.dumps(expiries)


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

    async def fake_broker_expiries(meta: UnderlyingMeta, *args, **kwargs) -> list[str]:
        ladders = {
            "NIFTY": ["2026-04-30", "2026-05-28"],
            "BANKNIFTY": ["2026-04-29", "2026-05-27"],
            "FINNIFTY": ["2026-04-28", "2026-05-26"],
            "MIDCPNIFTY": ["2026-04-27", "2026-05-25"],
            "SENSEX": ["2026-04-24", "2026-05-29"],
        }
        return ladders[meta.symbol]

    async def fake_load_persisted(symbol: str) -> list[str]:
        ladders = {
            "NIFTY": ["2026-04-30", "2026-05-28"],
            "BANKNIFTY": ["2026-04-29", "2026-05-27"],
            "FINNIFTY": ["2026-04-28", "2026-05-26"],
            "MIDCPNIFTY": ["2026-04-27", "2026-05-25"],
            "SENSEX": ["2026-04-24", "2026-05-29"],
        }
        return ladders[symbol]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", fake_ensure_fyers_session)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(service, "_get_broker_expiries_for_symbol", fake_broker_expiries)

    payload = asyncio.run(service.get_expiries())

    assert payload["source"] == "catalog"
    assert payload["default_expiry"] == "2026-04-28"
    assert "saved expiry catalog" in str(payload["detail"])
    assert "2026-04-28" in payload["expiries"]
    assert "BNKN 2026-04-28" in payload["expiry_scope_note"]


def test_get_expiries_prefers_live_fyers_ladders_over_saved_catalog_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    class _LiveFyersAdapter:
        async def get_option_contracts(self, symbol: str) -> list[dict]:
            ladders = {
                "NSE:NIFTY50-INDEX": ["2026-04-21", "2026-04-28", "2026-05-28"],
                "NSE:NIFTYBANK-INDEX": ["2026-04-23", "2026-04-29", "2026-05-27"],
                "NSE:FINNIFTY-INDEX": ["2026-04-22", "2026-04-28", "2026-05-26"],
                "NSE:MIDCPNIFTY-INDEX": ["2026-04-20", "2026-04-27", "2026-05-25"],
                "BSE:SENSEX-INDEX": ["2026-04-24", "2026-05-29"],
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
            "NIFTY": ["2026-04-28", "2026-05-26"],
            "BANKNIFTY": ["2026-04-28", "2026-05-26"],
            "FINNIFTY": ["2026-04-28", "2026-05-26"],
            "MIDCPNIFTY": ["2026-04-28", "2026-05-26"],
            "SENSEX": ["2026-04-24", "2026-05-29"],
        }
        return stale[symbol]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: _LiveFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda *args, **kwargs: _async_payload(False))
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)

    payload = asyncio.run(service.get_expiries())

    assert payload["source"] == "fyers"
    assert payload["default_expiry"] == "2026-04-28"
    assert payload["index_monthlies"]["NIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["BANKNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["FINNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["MIDCPNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["SENSEX"] == "2026-04-24"
    assert payload["detail"] is None


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
            return [{"expiry": expiry} for expiry in ["2026-04-21", "2026-04-28", "2026-05-26"]]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", lambda *args, **kwargs: _async_payload(False))
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", lambda symbol: _async_payload([]))
    monkeypatch.setattr(service, "_get_upstox_adapter", lambda: _async_payload(_UniformUpstoxAdapter()))

    payload = asyncio.run(service.get_expiries())

    assert payload["source"] == "upstox"
    assert payload["expiries"] == ["2026-04-21", "2026-04-28", "2026-05-26"]
    assert payload["default_expiry"] == "2026-04-28"
    assert payload["index_monthlies"]["NIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["BANKNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["FINNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["MIDCPNIFTY"] == "2026-04-28"
    assert payload["index_monthlies"]["SENSEX"] == "2026-04-24"


def test_get_watchlist_returns_building_payload_on_first_load(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    scheduled = []

    async def fake_get_expiries(_expiry: str | None = None) -> dict[str, str]:
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

    payload = asyncio.run(service.get_watchlist("2026-04-28"))

    assert payload["build_status"] == "building"
    assert payload["summary"]["total_rows"] == 2
    assert payload["rows"][0]["underlying"] in {"BANKNIFTY", "NIFTY"}
    assert "Building 1 remaining symbols in background." in str(payload["detail"])
    assert scheduled


def test_get_watchlist_returns_cached_building_payload_without_rebuild(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    cache_key = f"atm_watchlist:{atm_watchlist_module.WATCHLIST_CACHE_VERSION}:2026-04-28"
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
    monkeypatch.setattr(service, "get_expiries", lambda _expiry=None: _async_payload({"default_expiry": "2026-04-28"}))

    payload = asyncio.run(service.get_watchlist("2026-04-28"))

    assert payload == cached_payload
    assert redis._values[cache_key] == json.dumps(cached_payload)


def test_get_watchlist_uses_persisted_snapshot_board_when_brokers_are_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    async def fake_get_expiries(_expiry: str | None = None) -> dict:
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

    assert [row.symbol for row in rows] == ["NIFTY", "BANKNIFTY", "RELIANCE"]


async def _async_payload(payload):
    return payload
