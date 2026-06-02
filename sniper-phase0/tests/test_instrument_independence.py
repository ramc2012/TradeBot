"""The instrument-independence guard (spec section 3, contract section 2).

Builds the full feature row for two synthetic instruments at very different price levels
(22,000 vs 48,000) and asserts the normalized features are comparable — i.e. the model row
does not encode price scale.
"""
from __future__ import annotations
from datetime import date, datetime, time, timedelta
import numpy as np, pandas as pd
import pytest
from nomad_sniper.utils.timeutil import IST
from nomad_sniper.features.pipeline import build_all_features


def _make_bars(base_price, vol_scale, seed):
    rng = np.random.default_rng(seed)
    rows = []
    price = float(base_price)
    step = base_price * 0.0002
    for d in range(25):
        dd = date(2025, 1, 6) + timedelta(days=d)
        if dd.weekday() >= 5:
            continue
        start = IST.localize(datetime.combine(dd, time(9, 15)))
        for m in range(375):
            ts = start + timedelta(minutes=m)
            o = price; c = o + rng.normal(0, step)
            h = max(o, c) + abs(rng.normal(0, step / 3))
            lo = min(o, c) - abs(rng.normal(0, step / 3))
            rows.append({"ts": ts, "open": o, "high": h, "low": lo, "close": c,
                         "volume": int(abs(rng.normal(vol_scale, vol_scale * 0.3))), "oi": 1_000_000 + m})
            price = c
    b = pd.DataFrame(rows).set_index("ts"); b.index = b.index.tz_convert(IST)
    return b


def test_no_price_scale_in_features():
    dt = IST.localize(datetime(2025, 1, 24, 11, 30))
    for base, vscale, seed in [(22000, 10000, 1), (48000, 250000, 2)]:
        bars = _make_bars(base, vscale, seed)
        row = build_all_features(dt, bars).to_row(strict=True)
        for k, v in row.items():
            if isinstance(v, (int, float)) and v is not None and k != "decision_time":
                assert abs(v) < 1000, f"{k}={v} for base={base} is price/volume-scale"


def test_feature_rows_comparable_across_instruments():
    """Same normalized features for two instruments at 22k vs 48k should occupy the same
    numeric range (median absolute value within a small band), proving scale independence."""
    dt = IST.localize(datetime(2025, 1, 24, 11, 30))
    r1 = build_all_features(dt, _make_bars(22000, 10000, 1)).to_row(strict=True)
    r2 = build_all_features(dt, _make_bars(48000, 250000, 2)).to_row(strict=True)
    common = [k for k in r1 if k in r2 and isinstance(r1[k], (int, float))
              and isinstance(r2[k], (int, float)) and r1[k] is not None and r2[k] is not None
              and k != "decision_time"]
    assert len(common) > 15
    # normalized features cluster at small magnitude regardless of instrument price.
    frac1 = np.mean([abs(r1[k]) < 10 for k in common])
    frac2 = np.mean([abs(r2[k]) < 10 for k in common])
    assert frac1 > 0.7 and frac2 > 0.7, f"too many large features: {frac1:.2f} vs {frac2:.2f}"
    assert abs(frac1 - frac2) < 0.2, f"normalization differs across instruments: {frac1} vs {frac2}"
