from __future__ import annotations

import asyncio
from datetime import date

from data.run_upstox_research_sync import _planned_sleep_seconds
from analysis.backtest import UpstoxAuthError
import data.upstox_research_sync as research_sync_module
from data.upstox_research_sync import UpstoxResearchSync


def test_select_contract_sync_batch_prefers_finishing_smaller_underlyings() -> None:
    rows = [
        {
            "instrument_key": "A1",
            "underlying": "ALPHA",
            "kind": "STOCK",
            "underlying_pending_contracts": 2,
            "underlying_complete_contracts": 6,
        },
        {
            "instrument_key": "A2",
            "underlying": "ALPHA",
            "kind": "STOCK",
            "underlying_pending_contracts": 2,
            "underlying_complete_contracts": 6,
        },
        {
            "instrument_key": "B1",
            "underlying": "BETA",
            "kind": "INDEX",
            "underlying_pending_contracts": 3,
            "underlying_complete_contracts": 8,
        },
        {
            "instrument_key": "B2",
            "underlying": "BETA",
            "kind": "INDEX",
            "underlying_pending_contracts": 3,
            "underlying_complete_contracts": 8,
        },
        {
            "instrument_key": "B3",
            "underlying": "BETA",
            "kind": "INDEX",
            "underlying_pending_contracts": 3,
            "underlying_complete_contracts": 8,
        },
        {
            "instrument_key": "C1",
            "underlying": "CHARLIE",
            "kind": "STOCK",
            "underlying_pending_contracts": 5,
            "underlying_complete_contracts": 0,
        },
    ]

    selected = UpstoxResearchSync._select_contract_sync_batch(rows, limit=5)

    assert [row["instrument_key"] for row in selected] == ["A1", "A2", "B1", "B2", "B3"]


def test_should_pause_discovery_when_pending_backlog_is_large() -> None:
    assert UpstoxResearchSync._should_pause_discovery(pending_contracts=500, contract_limit=120) is True
    assert UpstoxResearchSync._should_pause_discovery(pending_contracts=100, contract_limit=120) is False


def test_planned_sleep_seconds_prefers_fast_loop_while_backlog_remains() -> None:
    last_result = {
        "db_summary": {"contract_status": {"pending": 320}},
        "rate_limit": {"hits": 0},
        "focus_mode": "backlog_drain",
    }

    sleep_seconds = _planned_sleep_seconds(
        poll_minutes=30,
        backlog_poll_seconds=60,
        last_result=last_result,
        errored=False,
    )

    assert sleep_seconds == 60


def test_fetch_chunked_candles_uses_single_call_for_one_year_30minute_window() -> None:
    sync = UpstoxResearchSync(
        access_token="token",
        from_date=date(2025, 3, 28),
        to_date=date(2026, 3, 29),
        interval="30minute",
    )
    calls: list[tuple[str, date, date]] = []

    async def fake_fetch(instrument_key: str, from_date: date, to_date: date) -> list[dict]:
        calls.append((instrument_key, from_date, to_date))
        return []

    sync.client._fetch_candles_from_upstox = fake_fetch  # type: ignore[method-assign]

    asyncio.run(
        sync._fetch_chunked_candles(
            "NSE_EQ|TEST",
            date(2025, 3, 28),
            date(2026, 3, 29),
        )
    )

    assert calls == [("NSE_EQ|TEST", date(2025, 3, 28), date(2026, 3, 29))]


def test_fetch_chunked_candles_splits_longer_windows() -> None:
    sync = UpstoxResearchSync(
        access_token="token",
        from_date=date(2025, 1, 1),
        to_date=date(2026, 3, 29),
        interval="30minute",
    )
    calls: list[tuple[str, date, date]] = []

    async def fake_fetch(instrument_key: str, from_date: date, to_date: date) -> list[dict]:
        calls.append((instrument_key, from_date, to_date))
        return []

    sync.client._fetch_candles_from_upstox = fake_fetch  # type: ignore[method-assign]

    asyncio.run(
        sync._fetch_chunked_candles(
            "NSE_EQ|TEST",
            date(2025, 1, 1),
            date(2026, 3, 29),
        )
    )

    assert len(calls) > 1


def test_expiry_metadata_to_date_keeps_future_monthly_windows_available() -> None:
    sync = UpstoxResearchSync(
        access_token="token",
        from_date=date(2025, 3, 28),
        to_date=date(2026, 4, 1),
        interval="30minute",
    )

    assert sync._expiry_metadata_to_date(today=date(2026, 4, 1)) == date(2026, 5, 31)


def test_expired_contract_discovery_to_date_does_not_scan_future_expiries() -> None:
    sync = UpstoxResearchSync(
        access_token="token",
        from_date=date(2025, 3, 28),
        to_date=date(2026, 5, 31),
        interval="30minute",
    )

    assert sync._expired_contract_discovery_to_date(today=date(2026, 4, 1)) == date(2026, 4, 1)


def test_expired_contract_auth_errors_are_soft_for_discovery_only() -> None:
    soft_error = UpstoxAuthError(
        "Expired contracts API rejected the Upstox token for HDFCBANK 2026-05-26 (HTTP 401)."
    )
    hard_error = UpstoxAuthError(
        "Historical candle API rejected the Upstox token for NSE_EQ|TEST (HTTP 401)."
    )

    assert UpstoxResearchSync._is_expired_contract_discovery_auth_error(soft_error) is True
    assert UpstoxResearchSync._is_expired_contract_discovery_auth_error(hard_error) is False


def test_rebuild_chain_metrics_uses_upsert_without_delete(monkeypatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.committed = False

        async def execute(self, statement, params=None):  # noqa: ANN001
            self.statements.append(str(statement))
            return None

        async def commit(self) -> None:
            self.committed = True

    class FakeSessionContext:
        def __init__(self, session: FakeSession) -> None:
            self._session = session

        async def __aenter__(self) -> FakeSession:
            return self._session

        async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

    fake_session = FakeSession()
    monkeypatch.setattr(
        research_sync_module,
        "AsyncSessionLocal",
        lambda: FakeSessionContext(fake_session),
    )

    sync = UpstoxResearchSync(
        access_token="token",
        from_date=date(2025, 3, 28),
        to_date=date(2026, 3, 29),
        interval="30minute",
    )

    asyncio.run(sync._rebuild_chain_metrics({("INDUSTOWER", date(2026, 6, 30))}))

    assert fake_session.committed is True
    assert len(fake_session.statements) == 1
    sql = fake_session.statements[0]
    assert "INSERT INTO fo_option_chain_metrics" in sql
    assert "ON CONFLICT (underlying, expiry, interval, time) DO UPDATE" in sql
    assert "DELETE FROM fo_option_chain_metrics" not in sql


def test_db_summary_uses_catalog_and_estimates_instead_of_hypertable_counts(monkeypatch) -> None:
    class Result:
        def __init__(self, *, row=None, scalar_value=None, rows=None) -> None:
            self._row = row
            self._scalar = scalar_value
            self._rows = rows or []

        def fetchone(self):
            return self._row

        def scalar(self):
            return self._scalar

        def fetchall(self):
            return self._rows

    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, statement, params=None):  # noqa: ANN001
            sql = str(statement)
            self.statements.append(sql)
            if "SUM(candle_count)" in sql:
                return Result(row=type("Row", (), {"option_candles": 10, "option_contracts": 2, "option_underlyings": 1})())
            if "underlying_spot_candles" in sql:
                return Result(scalar_value=20)
            if "fo_option_chain_metrics" in sql:
                return Result(scalar_value=30)
            return Result(rows=[type("Row", (), {"sync_status": "complete", "contracts": 2})()])

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    session = FakeSession()
    monkeypatch.setattr(research_sync_module, "AsyncSessionLocal", Context)
    sync = UpstoxResearchSync("token", date(2025, 1, 1), date(2026, 1, 1))

    summary = asyncio.run(sync.get_db_summary())

    assert summary["count_mode"] == "catalog_and_postgres_estimates"
    assert not any("FROM option_premium_candles" in sql for sql in session.statements)
    assert not any("COUNT(*) AS spot_candles" in sql for sql in session.statements)


def test_deadlock_is_classified_as_retryable() -> None:
    assert UpstoxResearchSync._is_retryable_db_conflict(RuntimeError("deadlock detected")) is True
    assert UpstoxResearchSync._is_retryable_db_conflict(RuntimeError("connection refused")) is False
