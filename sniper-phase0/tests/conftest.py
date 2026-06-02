"""Shared pytest fixtures. Builds a small synthetic bar set for fast unit tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from nomad_sniper.utils.timeutil import IST


@pytest.fixture
def synthetic_bars():
    """5 days of 1-minute bars for a fake underlying. Deterministic random walk."""
    rng = np.random.default_rng(42)
    rows = []
    price = 22000.0
    for d_offset in range(5):
        d = date(2025, 1, 6) + timedelta(days=d_offset)
        if d.weekday() >= 5:
            continue
        start = IST.localize(datetime.combine(d, time(9, 15)))
        for m in range(375):  # 09:15 - 15:30
            ts = start + timedelta(minutes=m)
            o = price
            move = rng.normal(0, 5)
            c = o + move
            h = max(o, c) + abs(rng.normal(0, 2))
            lo = min(o, c) - abs(rng.normal(0, 2))
            v = int(abs(rng.normal(10000, 3000)))
            oi = 1_000_000 + d_offset * 50_000 + m * 10
            rows.append({"ts": ts, "open": o, "high": h, "low": lo, "close": c,
                         "volume": v, "oi": oi})
            price = c

    df = pd.DataFrame(rows).set_index("ts")
    df.index = df.index.tz_convert(IST)
    df["contract_expiry"] = date(2025, 1, 30)
    return df


@pytest.fixture
def synthetic_decision_time():
    """A timestamp on day 3 at 11:30 IST, well inside the synthetic_bars range."""
    return IST.localize(datetime(2025, 1, 8, 11, 30))
