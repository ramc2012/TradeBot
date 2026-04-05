from __future__ import annotations

import asyncio
import gzip
from datetime import date
from pathlib import Path

from data.index_analytics_collector import (
    IndexAnalyticsCollector,
    IndexAnalyticsProgress,
    _sort_expiry_keys,
    load_index_analytics_summary,
)


def test_sort_expiry_keys_marks_last_expiry_of_month_as_monthly() -> None:
    expiries = [
        date(2026, 1, 1),
        date(2026, 1, 8),
        date(2026, 1, 29),
        date(2026, 2, 5),
        date(2026, 2, 26),
    ]
    kinds = _sort_expiry_keys(expiries)

    assert kinds[date(2026, 1, 1)] == "weekly"
    assert kinds[date(2026, 1, 29)] == "monthly"
    assert kinds[date(2026, 2, 5)] == "weekly"
    assert kinds[date(2026, 2, 26)] == "monthly"


def test_index_analytics_collector_writes_partitioned_dataset(tmp_path: Path) -> None:
    collector = IndexAnalyticsCollector("token", data_root=tmp_path)

    async def fake_fetch_expiry_dates(underlying: str) -> list[date]:
        if underlying == "NIFTY":
            return [date(2026, 1, 1), date(2026, 1, 29)]
        return [date(2026, 1, 2)]

    async def fake_fetch_contracts_for_expiry(underlying: str, expiry: date):
        if underlying == "NIFTY" and expiry == date(2026, 1, 1):
            return [
                {
                    "instrument_key": "expired|nifty|ce|100",
                    "trading_symbol": "NIFTY01JAN25100CE",
                    "instrument_type": "CE",
                    "strike_price": 25100,
                },
                {
                    "instrument_key": "expired|nifty|pe|100",
                    "trading_symbol": "NIFTY01JAN25100PE",
                    "instrument_type": "PE",
                    "strike_price": 25100,
                },
            ]
        return [
            {
                "instrument_key": f"{underlying.lower()}|{expiry.isoformat()}|ce",
                "trading_symbol": f"{underlying}{expiry.strftime('%d%b').upper()}25200CE",
                "instrument_type": "CE",
                "strike_price": 25200,
            }
        ]

    async def fake_fetch_chunked_candles(*args, **kwargs):
        rows = [
            {
                "time": "2026-01-01T09:15:00+05:30",
                "open": 100.0,
                "high": 110.0,
                "low": 95.0,
                "close": 108.0,
                "volume": 1000,
                "oi": 2000,
            },
            {
                "time": "2026-01-01T09:16:00+05:30",
                "open": 108.0,
                "high": 112.0,
                "low": 107.0,
                "close": 111.0,
                "volume": 1200,
                "oi": 2050,
            },
        ]
        persist_chunk = kwargs.get("persist_chunk")
        if persist_chunk:
            persist_chunk(rows, ["2025-12-18::2025-12-31"])
        return {
            "candles": rows,
            "completed_windows": ["2025-12-18::2025-12-31"],
            "complete": True,
            "blocked_window": None,
            "error": "",
        }

    collector.client._fetch_expiry_dates = fake_fetch_expiry_dates  # type: ignore[method-assign]
    collector._fetch_contracts_for_expiry = fake_fetch_contracts_for_expiry  # type: ignore[method-assign]
    collector._fetch_chunked_candles = fake_fetch_chunked_candles  # type: ignore[method-assign]

    progress = asyncio.run(
        collector.run(
            underlyings=["NIFTY", "SENSEX"],
            from_date=date(2025, 12, 1),
            to_date=date(2026, 1, 31),
            progress=IndexAnalyticsProgress(task_id="task-1", underlyings=["NIFTY", "SENSEX"]),
        )
    )

    assert progress.status == "done"
    assert progress.total_spot_series == 2
    assert progress.processed_spot_series == 2
    assert progress.total_expiries == 3
    assert progress.total_contracts == 4
    assert progress.stored_files == 6
    assert progress.stored_spot_files == 2
    assert progress.stored_candles == 12
    assert progress.stored_spot_candles == 4

    output_files = sorted(tmp_path.glob("**/*.csv.gz"))
    assert len(output_files) == 6
    with gzip.open(output_files[0], "rt") as handle:
        content = handle.read()
    assert "time,open,high,low,close,volume,oi" in content
    assert "2026-01-01T09:15:00+05:30" in content

    summary = load_index_analytics_summary(tmp_path)
    assert summary["summary"]["contracts"] == 6
    assert summary["summary"]["files"] == 6
    assert summary["summary"]["candles"] == 12
    assert {row["expiry_kind"] for row in summary["rows"] if row["underlying"] == "NIFTY"} == {"weekly", "monthly", "spot"}
    assert any(
        row["underlying"] == "NIFTY" and row["expiry_kind"] == "spot" and row["dataset_type"] == "spot"
        for row in summary["rows"]
    )


def test_chunked_candles_resume_skips_completed_windows(tmp_path: Path) -> None:
    collector = IndexAnalyticsCollector("token", data_root=tmp_path)
    output_path = tmp_path / "contract.csv.gz"
    existing_rows = [
        {
            "time": "2026-01-01T09:15:00+05:30",
            "open": 100.0,
            "high": 110.0,
            "low": 95.0,
            "close": 108.0,
            "volume": 1000,
            "oi": 2000,
        }
    ]
    collector._write_contract_file(output_path, existing_rows)

    seen_windows: list[str] = []

    async def fake_fetch_candles(instrument_key: str, interval: str, from_date: date, to_date: date, retry_count: int = 0):
        seen_windows.append(f"{from_date.isoformat()}::{to_date.isoformat()}")
        return [
            {
                "time": "2026-01-02T09:15:00+05:30",
                "open": 109.0,
                "high": 111.0,
                "low": 108.0,
                "close": 110.0,
                "volume": 900,
                "oi": 2100,
            }
        ]

    collector._fetch_candles = fake_fetch_candles  # type: ignore[method-assign]

    persisted_lengths: list[int] = []

    result = asyncio.run(
        collector._fetch_chunked_candles(
            "NSE_FO|demo",
            "1minute",
            date(2026, 1, 1),
            date(2026, 1, 20),
            existing_candles=collector._load_existing_candles(output_path),
            completed_windows=["2026-01-01::2026-01-14"],
            persist_chunk=lambda candles, completed: persisted_lengths.append(len(candles)),
        )
    )

    assert seen_windows == ["2026-01-15::2026-01-20"]
    assert result["complete"] is True
    assert persisted_lengths == [2]
    assert len(result["candles"]) == 2
