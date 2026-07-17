from __future__ import annotations

from datetime import datetime, timedelta, timezone

from paper_engine.commodity_strategy_agent import _sanitize_commodity_history


def _row(index: int, price: float) -> dict:
    return {
        "time": datetime(2026, 7, 17, 3, 30, tzinfo=timezone.utc) + timedelta(minutes=index),
        "open": price,
        "high": price + 10,
        "low": price - 10,
        "close": price + 2,
        "volume": 10,
    }


def test_sanitize_commodity_history_drops_cross_wired_price_scales() -> None:
    rows = [_row(index, 140_000 + index) for index in range(40)]
    rows.insert(8, _row(8, 1_615))
    rows.insert(12, _row(12, 219_900))
    rows.insert(
        16,
        {
            **_row(16, 140_016),
            "low": 276.5,
        },
    )

    clean = _sanitize_commodity_history(rows)

    assert len(clean) == 40
    assert min(float(row["low"]) for row in clean) > 100_000
    assert max(float(row["high"]) for row in clean) < 180_000
