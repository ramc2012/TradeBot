from __future__ import annotations

import asyncio
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

    def one(self) -> dict:
        return self._rows[0]


class _FakeSession:
    def __init__(self, responses: list[list[dict]]):
        self._responses = responses
        self._idx = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _query):
        response = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return _FakeResult(response)


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
    ) == "populated"


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
    assert pct == pytest.approx(98.8, abs=0.1)

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


def test_research_scheduler_marks_overdue_waiting_runtime_as_stalled() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    scheduler = analysis._build_research_scheduler_summary(
        now_utc=now,
        recent_activity_at=now - timedelta(hours=2),
        contracts_pending=42,
        active_recent_symbols=0,
        runtime_state={
            "state": "waiting",
            "run_started_at": (now - timedelta(hours=2, minutes=5)).isoformat(),
            "run_completed_at": (now - timedelta(hours=2)).isoformat(),
            "next_run_at": (now - timedelta(minutes=30)).isoformat(),
        },
    )

    assert scheduler["state"] == "stalled"
    assert scheduler["label"] == "Aggregation overdue"
    assert scheduler["seconds_until_next_batch"] == 0
    assert scheduler["next_batch_at"] == (now - timedelta(minutes=30)).isoformat()


def test_research_scheduler_keeps_future_waiting_runtime_as_waiting() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    scheduler = analysis._build_research_scheduler_summary(
        now_utc=now,
        recent_activity_at=now - timedelta(minutes=5),
        contracts_pending=42,
        active_recent_symbols=0,
        runtime_state={
            "state": "waiting",
            "run_started_at": (now - timedelta(minutes=10)).isoformat(),
            "run_completed_at": (now - timedelta(minutes=5)).isoformat(),
            "next_run_at": (now + timedelta(minutes=25)).isoformat(),
        },
    )

    assert scheduler["state"] == "waiting"
    assert scheduler["label"] == "Waiting for next aggregation pass"
    assert scheduler["seconds_until_next_batch"] == 1500


def test_research_scheduler_accepts_naive_recent_activity() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    naive_recent_activity = (now - timedelta(minutes=5)).replace(tzinfo=None)

    scheduler = analysis._build_research_scheduler_summary(
        now_utc=now,
        recent_activity_at=naive_recent_activity,
        contracts_pending=42,
        active_recent_symbols=0,
        runtime_state={
            "state": "waiting",
            "run_started_at": (now - timedelta(minutes=10)).isoformat(),
            "run_completed_at": (now - timedelta(minutes=5)).isoformat(),
            "next_run_at": (now + timedelta(minutes=25)).isoformat(),
        },
    )

    assert scheduler["state"] == "waiting"
    assert scheduler["seconds_until_next_batch"] == 1500
    assert scheduler["last_batch_activity_at"] == (now - timedelta(minutes=5)).isoformat()


def test_api_budget_summary_uses_runtime_history_and_research_target() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    budget = analysis._build_api_budget_summary(
        now_utc=now,
        summary={
            "universe_total": 3,
            "underlyings_with_expiries": 2,
            "underlyings_with_spot": 1,
            "expiry_total": 10,
            "expiries_discovered": 4,
            "research_contract_target": 20,
            "research_contracts_processed": 8,
        },
        runtime_state={
            "elapsed_seconds": 80,
            "last_result": {
                "api_calls": {
                    "total": 120,
                    "by_endpoint": {"historical_candle": 115, "expired_contracts": 5},
                },
                "rate_limit": {"inter_call_delay_seconds": 1.0},
            },
            "history": [
                {
                    "completed_at": (now - timedelta(minutes=5)).isoformat(),
                    "elapsed_seconds": 80,
                    "api_calls": {
                        "total": 120,
                        "by_endpoint": {"historical_candle": 115, "expired_contracts": 5},
                    },
                },
                {
                    "completed_at": (now - timedelta(minutes=20)).isoformat(),
                    "elapsed_seconds": 90,
                    "api_calls": {
                        "total": 100,
                        "by_endpoint": {"historical_candle": 92, "historical_day": 8},
                    },
                },
            ],
        },
    )

    assert budget["limits"]["per_30_minutes"] == 2000
    assert budget["configured"]["calls_per_30_minutes"] == pytest.approx(1800.0)
    assert budget["rolling_30m"]["calls"] == 220
    assert budget["rolling_30m"]["by_endpoint"]["historical_candle"] == 207
    assert budget["last_run"]["calls"] == 120
    assert budget["theoretical"]["total_calls"] == 36
    assert budget["theoretical"]["remaining_calls"] == 21


def test_get_research_cache_status_summarises_rows(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(
        analysis,
        "AsyncSessionLocal",
        lambda: _FakeSession(
            [
                rows,
                [
                    {
                        "last_successful_option_sync_at": now - timedelta(minutes=2),
                        "option_candles_added_last_30m": 1200,
                    }
                ],
                [
                    {
                        "last_complete_contract_sync_at": now - timedelta(minutes=2),
                        "last_empty_contract_touch_at": now - timedelta(minutes=3),
                        "complete_contracts_touched_last_30m": 12,
                        "empty_contracts_touched_last_30m": 1,
                    }
                ],
            ]
        ),
    )

    payload = asyncio.run(analysis.get_research_cache_status())

    assert payload["summary"]["universe_total"] == 3
    assert payload["summary"]["underlyings_with_expiries"] == 2
    assert payload["summary"]["underlyings_with_spot"] == 2
    assert payload["summary"]["option_candles"] == 43250
    assert payload["summary"]["contracts_complete"] == 262
    assert payload["summary"]["contracts_pending"] == 2803
    assert payload["api_budget"]["limits"]["per_30_minutes"] == 2000
    assert payload["summary"]["active_symbols"] == 2
    assert payload["summary"]["populated_symbols"] == 1
    assert payload["summary"]["symbols_in_progress"] == 1
    assert payload["summary"]["stage_counts"] == {
        "queued": 1,
        "metadata": 0,
        "spot": 0,
        "contracts": 1,
        "populating": 0,
        "populated": 1,
    }

    assert [row["symbol"] for row in payload["symbols"]] == ["ABB", "NIFTY", "ZYDUSLIFE"]
    assert payload["symbols"][0]["stage"] == "contracts"
    assert payload["symbols"][0]["active_now"] is True
    assert payload["symbols"][1]["stage"] == "populated"
    assert payload["symbols"][2]["stage"] == "queued"
    assert payload["symbols"][2]["active_now"] is False
