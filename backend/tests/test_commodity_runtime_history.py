import asyncio
from datetime import datetime, timezone


def test_load_commodity_history_rows_prefers_active_rollover_contract(monkeypatch) -> None:
    from market_data import commodity_runtime_history as history
    import paper_engine.commodity_strategy_agent as commodity_agent_module

    calls: list[str] = []

    class FakeAgent:
        def get_symbols(self) -> list[str]:
            return ["MCX:NICKEL26JULFUT"]

        async def _active_futures_symbols(self) -> dict[str, str]:
            return {"MCX:NICKEL26JULFUT": "MCX:NICKEL26AUGFUT"}

        def get_selected_option_lookup_symbols(self) -> dict[str, str]:
            return {}

        async def _load_history(self, symbol: str, *, interval: str, lookback_days: int):
            calls.append(symbol)
            assert interval == "1minute"
            assert lookback_days == 2
            return [{"time": "2026-07-16T15:24:00+00:00", "close": 1643.4}]

    monkeypatch.setattr(commodity_agent_module, "CommodityStrategyAgent", FakeAgent)

    rows, selected_symbol = asyncio.run(
        history.load_commodity_history_rows(
            "NICKEL",
            interval="1minute",
            lookback_days=2,
            persist=False,
        )
    )

    assert rows
    assert selected_symbol == "MCX:NICKEL26AUGFUT"
    assert calls == ["MCX:NICKEL26AUGFUT"]


def test_persist_commodity_spot_rows_repairs_recent_gap_window(monkeypatch) -> None:
    from market_data import commodity_runtime_history as history

    cache_key = ("MCX_FO|568837", "3minute")
    history._LATEST_PERSISTED.clear()
    history._PERSIST_LOCKS.clear()
    history._LATEST_PERSISTED[cache_key] = datetime(2026, 7, 17, 15, 18, tzinfo=timezone.utc)

    class FakeSession:
        def __init__(self) -> None:
            self.payload = None
            self.committed = False

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def execute(self, _statement, payload):
            self.payload = payload

        async def commit(self) -> None:
            self.committed = True

    fake_session = FakeSession()

    class FakeContext:
        async def __aenter__(self) -> FakeSession:
            return fake_session

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr("db.database.AsyncSessionLocal", lambda: FakeContext())

    rows = [
        {"time": "2026-07-17T11:00:00+00:00", "close": 1600.0},
        {"time": "2026-07-17T15:12:00+00:00", "close": 1625.1},
        {"time": "2026-07-17T15:15:00+00:00", "close": 1625.4},
        {"time": "2026-07-17T15:18:00+00:00", "close": 1625.6},
        {"time": "2026-07-17T15:21:00+00:00", "close": 1626.1},
    ]

    try:
        inserted = asyncio.run(
            history._persist_commodity_spot_rows(
                underlying="NICKEL",
                instrument_key="MCX_FO|568837",
                rows=rows,
                interval="3minute",
            )
        )
    finally:
        history._LATEST_PERSISTED.clear()
        history._PERSIST_LOCKS.clear()

    assert inserted == 4
    assert fake_session.committed is True
    assert fake_session.payload is not None
    persisted_times = {row["time"].isoformat() for row in fake_session.payload}
    assert persisted_times == {
        "2026-07-17T15:12:00+00:00",
        "2026-07-17T15:15:00+00:00",
        "2026-07-17T15:18:00+00:00",
        "2026-07-17T15:21:00+00:00",
    }


def test_persist_commodity_spot_rows_accepts_first_rows_without_watermark(monkeypatch) -> None:
    from market_data import commodity_runtime_history as history

    cache_key = ("MCX:GOLD26AUGFUT", "3minute")
    history._LATEST_PERSISTED.clear()
    history._PERSIST_LOCKS.clear()

    class FakeSession:
        def __init__(self) -> None:
            self.payload = None
            self.committed = False

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def scalar(self, _statement, _params):
            return None

        async def execute(self, _statement, payload):
            self.payload = payload

        async def commit(self) -> None:
            self.committed = True

    fake_session = FakeSession()
    monkeypatch.setattr("db.database.AsyncSessionLocal", lambda: fake_session)

    try:
        inserted = asyncio.run(
            history._persist_commodity_spot_rows(
                underlying="GOLD",
                instrument_key=cache_key[0],
                rows=[
                    {
                        "time": "2026-07-20T14:45:00+00:00",
                        "open": 141500,
                        "high": 141600,
                        "low": 141450,
                        "close": 141550,
                    }
                ],
                interval=cache_key[1],
            )
        )
    finally:
        history._LATEST_PERSISTED.clear()
        history._PERSIST_LOCKS.clear()

    assert inserted == 1
    assert fake_session.committed is True
    assert fake_session.payload[0]["time"] == datetime(
        2026, 7, 20, 14, 45, tzinfo=timezone.utc
    )
