from datetime import datetime, timedelta, timezone

from market_data.validated_snapshots import (
    ValidatedSnapshotStore,
    validate_candle_rows,
    validate_option_chain_rows,
    validated_snapshot_store,
)


UTC = timezone.utc


def test_candle_validation_rejects_bad_ohlc_and_deduplicates() -> None:
    validated_snapshot_store.clear()
    now = datetime(2026, 7, 5, 4, 30, tzinfo=UTC)
    rows = []
    for offset in range(10):
        ts = now - timedelta(seconds=60 * (9 - offset))
        rows.append(
            {"time": ts.isoformat(), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 20}
        )
    rows.append({**rows[-1], "close": 100.5})
    rows.append(
        {"time": now.isoformat(), "open": 100, "high": 98, "low": 99, "close": 101, "volume": 20}
    )

    result = validate_candle_rows(rows, symbol="NIFTY", source="test", now=now, min_rows=2)

    assert len(result.rows) == 10
    assert result.rows[-1]["close"] == 100.5
    assert result.quality["rejection_counts"] == {
        "duplicate_bar": 1,
        "inconsistent_ohlc": 1,
    }
    assert result.quality["execution_ready"] is False  # >10% of the input was rejected


def test_option_chain_validation_exposes_freshness_and_provenance() -> None:
    validated_snapshot_store.clear()
    now = datetime(2026, 7, 5, 4, 30, tzinfo=UTC)
    rows = [
        {"strike": 24000, "option_type": "CE", "ltp": 120, "oi": 1000, "volume": 500, "bid": 119, "ask": 121},
        {"strike": 24000, "option_type": "PE", "ltp": 100, "oi": 1200, "volume": 600, "bid": 99, "ask": 101},
    ]

    result = validate_option_chain_rows(
        rows,
        symbol="NIFTY",
        expiry="2026-07-09",
        spot_price=24020,
        source="fyers",
        observed_at=now,
        now=now,
    )

    assert result.quality["execution_ready"] is True
    status = validated_snapshot_store.status(now=now + timedelta(seconds=61))
    assert status["feeds"][0]["source"] == "fyers"
    assert status["feeds"][0]["fresh"] is False
    assert status["feeds"][0]["execution_ready"] is False


def test_snapshot_registry_is_bounded() -> None:
    store = ValidatedSnapshotStore(max_entries=2)
    for index in range(3):
        store.publish(
            {
                "key": f"candles:S{index}:1minute",
                "received_at": datetime(2026, 7, 5, 4, index, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 7, 5, 4, index, tzinfo=UTC).isoformat(),
                "freshness_budget_seconds": 60,
                "execution_ready": True,
            }
        )
    assert store.status(now=datetime(2026, 7, 5, 4, 3, tzinfo=UTC))["feed_count"] == 2
