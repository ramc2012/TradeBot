from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.routers import analysis


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        return self

    def all(self) -> list[dict]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _query):
        return _FakeResult(self._rows)


def test_stage_for_symbol() -> None:
    assert analysis._stage_for_symbol(
        {
            "total_expiries": 0,
            "discovered_expiries": 0,
            "spot_candles": 0,
            "total_contracts": 0,
            "complete_contracts": 0,
            "pending_contracts": 0,
            "option_candles": 0,
        }
    ) == "queued"

    assert analysis._stage_for_symbol(
        {
            "total_expiries": 11,
            "discovered_expiries": 0,
            "spot_candles": 0,
            "total_contracts": 0,
            "complete_contracts": 0,
            "pending_contracts": 0,
            "option_candles": 0,
        }
    ) == "metadata"

    assert analysis._stage_for_symbol(
        {
            "total_expiries": 11,
            "discovered_expiries": 11,
            "spot_candles": 3200,
            "total_contracts": 357,
            "complete_contracts": 0,
            "pending_contracts": 357,
            "option_candles": 0,
        }
    ) == "contracts"

    assert analysis._stage_for_symbol(
        {
            "total_expiries": 12,
            "discovered_expiries": 12,
            "spot_candles": 3205,
            "total_contracts": 2716,
            "complete_contracts": 262,
            "pending_contracts": 2446,
            "option_candles": 43250,
        }
    ) == "populating"


def test_symbol_progress_pct() -> None:
    pct = analysis._symbol_progress_pct(
        {
            "total_expiries": 12,
            "discovered_expiries": 12,
            "selection_spots_ready": 11,
            "spot_candles": 3205,
            "total_contracts": 2716,
            "complete_contracts": 262,
            "empty_contracts": 8,
        }
    )
    assert pct == pytest.approx(62.7, abs=0.1)

    queued_pct = analysis._symbol_progress_pct(
        {
            "total_expiries": 0,
            "discovered_expiries": 0,
            "selection_spots_ready": 0,
            "spot_candles": 0,
            "total_contracts": 0,
            "complete_contracts": 0,
            "empty_contracts": 0,
        }
    )
    assert queued_pct == 0.0


@pytest.mark.asyncio
async def test_get_research_cache_status_summarises_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "symbol": "NIFTY",
            "kind": "INDEX",
            "total_expiries": 12,
            "discovered_expiries": 12,
            "selection_spots_ready": 11,
            "spot_candles": 3205,
            "total_contracts": 2716,
            "complete_contracts": 262,
            "pending_contracts": 2446,
            "empty_contracts": 8,
            "option_candles": 43250,
            "option_contracts": 262,
            "expiries_synced_at": now - timedelta(hours=2),
            "spot_synced_at": now - timedelta(hours=2),
            "last_activity_at": now - timedelta(minutes=5),
        },
        {
            "symbol": "ABB",
            "kind": "STOCK",
            "total_expiries": 11,
            "discovered_expiries": 11,
            "selection_spots_ready": 10,
            "spot_candles": 3200,
            "total_contracts": 357,
            "complete_contracts": 0,
            "pending_contracts": 357,
            "empty_contracts": 0,
            "option_candles": 0,
            "option_contracts": 0,
            "expiries_synced_at": now - timedelta(hours=1),
            "spot_synced_at": now - timedelta(hours=1),
            "last_activity_at": now - timedelta(minutes=10),
        },
        {
            "symbol": "ZYDUSLIFE",
            "kind": "STOCK",
            "total_expiries": 0,
            "discovered_expiries": 0,
            "selection_spots_ready": 0,
            "spot_candles": 0,
            "total_contracts": 0,
            "complete_contracts": 0,
            "pending_contracts": 0,
            "empty_contracts": 0,
            "option_candles": 0,
            "option_contracts": 0,
            "expiries_synced_at": None,
            "spot_synced_at": None,
            "last_activity_at": None,
        },
    ]

    monkeypatch.setattr(analysis, "AsyncSessionLocal", lambda: _FakeSession(rows))

    payload = await analysis.get_research_cache_status()

    assert payload["summary"]["universe_total"] == 3
    assert payload["summary"]["underlyings_with_expiries"] == 2
    assert payload["summary"]["underlyings_with_spot"] == 2
    assert payload["summary"]["option_candles"] == 43250
    assert payload["summary"]["contracts_complete"] == 262
    assert payload["summary"]["contracts_pending"] == 2803
    assert payload["summary"]["active_symbols"] == 2
    assert payload["summary"]["populated_symbols"] == 1
    assert payload["summary"]["symbols_in_progress"] == 2
    assert payload["summary"]["stage_counts"] == {
        "queued": 1,
        "metadata": 0,
        "spot": 0,
        "contracts": 1,
        "populating": 1,
        "populated": 0,
    }

    assert [row["symbol"] for row in payload["symbols"]] == ["NIFTY", "ABB", "ZYDUSLIFE"]
    assert payload["symbols"][0]["stage"] == "populating"
    assert payload["symbols"][0]["active_now"] is True
    assert payload["symbols"][1]["stage"] == "contracts"
    assert payload["symbols"][2]["stage"] == "queued"
    assert payload["symbols"][2]["active_now"] is False
