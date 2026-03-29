from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from analytics.greeks_sync import GreeksSyncConfig, compute_greeks_sync_frame


def _frame(
    *,
    option_type: str,
    underlying_prices: list[float],
    deltas: list[float],
    ivs: list[float],
    gamma: float,
    theta: float,
    vega: float,
) -> pd.DataFrame:
    start = datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=30 * idx) for idx in range(len(underlying_prices))]
    return pd.DataFrame(
        {
            "time": times,
            "close": [100.0 + idx for idx in range(len(times))],
            "iv": ivs,
            "delta": deltas,
            "gamma": [gamma] * len(times),
            "theta": [theta] * len(times),
            "vega": [vega] * len(times),
            "underlying_price": underlying_prices,
            "option_type": [option_type] * len(times),
        }
    )


def test_bullish_call_confluence_reaches_strong_signal() -> None:
    df = _frame(
        option_type="CE",
        underlying_prices=[22000, 22008, 22018, 22031, 22047, 22066],
        deltas=[0.45, 0.49, 0.54, 0.62, 0.71, 0.79],
        ivs=[0.18, 0.181, 0.184, 0.188, 0.193, 0.199],
        gamma=0.0065,
        theta=-0.65,
        vega=8.5,
    )

    result = compute_greeks_sync_frame(df, "CE", config=GreeksSyncConfig())

    last_row = result.iloc[-1]
    assert float(last_row["greeks_sync_score"]) >= 85.0
    assert bool(last_row["greeks_sync_ready"]) is True
    assert bool(last_row["greeks_sync_strong"]) is True
    assert bool(result["greeks_sync_signal"].any()) is True
    assert last_row["greeks_sync_score_bucket"] == "score_85_plus"
    assert float(last_row["theta_overwhelm_ratio"]) > 3.0


def test_flat_greeks_environment_does_not_emit_signal() -> None:
    df = _frame(
        option_type="CE",
        underlying_prices=[22000, 22000, 22001, 22001, 22002, 22002],
        deltas=[0.5, 0.5, 0.51, 0.5, 0.5, 0.5],
        ivs=[0.2, 0.2, 0.2, 0.2, 0.2, 0.2],
        gamma=0.0065,
        theta=-1.2,
        vega=3.0,
    )

    result = compute_greeks_sync_frame(df, "CE", config=GreeksSyncConfig())

    assert float(result.iloc[-1]["greeks_sync_score"]) < 70.0
    assert bool(result["greeks_sync_signal"].any()) is False


def test_bearish_put_uses_aligned_delta_and_price_move() -> None:
    df = _frame(
        option_type="PE",
        underlying_prices=[22000, 21992, 21980, 21965, 21947, 21925],
        deltas=[-0.44, -0.48, -0.53, -0.61, -0.7, -0.79],
        ivs=[0.19, 0.191, 0.193, 0.197, 0.202, 0.208],
        gamma=0.006,
        theta=-0.7,
        vega=8.0,
    )

    result = compute_greeks_sync_frame(df, "PE", config=GreeksSyncConfig())

    last_row = result.iloc[-1]
    assert float(last_row["aligned_delta"]) > 0
    assert float(last_row["aligned_underlying_move"]) > 0
    assert float(last_row["greeks_sync_score"]) >= 70.0
    assert bool(result["greeks_sync_signal"].any()) is True
