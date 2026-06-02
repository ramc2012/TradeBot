from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from nomad_sniper.labels.directional import build_directional_labels_for_grid, label_directional_point
from nomad_sniper.labels.profitability_gate import ATRProxyGate
from nomad_sniper.utils.timeutil import IST


def _trend_bars(direction: str) -> pd.DataFrame:
    rows = []
    price = 1000.0
    start = IST.localize(datetime.combine(date(2025, 1, 1), time(9, 15)))
    for i in range(140):
        ts = start + timedelta(minutes=i)
        step = 3.0 if direction == "up" else -3.0 if direction == "down" else 0.2
        o = price
        c = price + step
        h = max(o, c) + 1
        lo = min(o, c) - 1
        rows.append({"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": 1000, "oi": 1})
        price = c
    df = pd.DataFrame(rows).set_index("ts")
    df.index = df.index.tz_convert(IST)
    return df


def test_directional_label_up():
    bars = _trend_bars("up")
    dt = IST.localize(datetime(2025, 1, 1, 9, 30))
    row = label_directional_point(
        bars,
        dt,
        atr_ref=10,
        horizon_minutes=60,
        barrier_m=1.0,
        gate=ATRProxyGate(m_breakeven=0.5),
    )
    assert row is not None
    assert row["direction"] == "up"
    assert row["is_move"] == 1
    assert row["magnitude_atr"] >= 1


def test_gate_relabels_small_move_to_none():
    bars = _trend_bars("up")
    dt = IST.localize(datetime(2025, 1, 1, 9, 30))
    row = label_directional_point(
        bars,
        dt,
        atr_ref=10,
        horizon_minutes=60,
        barrier_m=1.0,
        gate=ATRProxyGate(m_breakeven=2.0),
    )
    assert row is not None
    assert row["candidate_direction"] == "up"
    assert row["direction"] == "none"
    assert row["is_move"] == 0


def test_grid_labeler_keys_rows_by_underlying_and_time():
    prior = _trend_bars("none")
    prior2 = _trend_bars("none")
    prior2.index = prior2.index + timedelta(days=1)
    current = _trend_bars("down")
    current.index = current.index + timedelta(days=2)
    bars = pd.concat([prior, prior2, current])
    dt = IST.localize(datetime(2025, 1, 3, 9, 30))
    labels = build_directional_labels_for_grid(
        [("nifty", dt)],
        {"nifty": bars},
        horizon_minutes=60,
        barrier_m=1.0,
        m_breakeven=0.5,
    )
    assert len(labels) == 1
    assert labels.iloc[0]["direction"] == "down"
    assert labels.index[0].startswith("nifty|")
