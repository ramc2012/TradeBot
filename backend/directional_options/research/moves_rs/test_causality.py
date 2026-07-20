"""(C) Prefix-invariance proof of causality for the move segmentation.

We do not ASSERT that ATR / zigzag are causal -- we prove it: recompute the
whole pipeline on the truncated prefix rows 0..k and require that every leg
CONFIRMED at or before bar k is bit-for-bit identical (rtol 1e-12) to the leg
the full-history run produced. Any lookahead -- a centred window, a global
normaliser, a peak identified before its retracement -- breaks this.

Run: ./.venv/bin/python backend/directional_options/research/moves_rs/test_causality.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from moves import segment, wilder_atr

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(HERE, "data", "daily.parquet")
FIELDS = ("direction", "start_i", "end_i", "confirm_i", "start_price",
          "end_price", "atr0", "atr_mult", "ret", "duration", "lag")


def main() -> int:
    daily = pd.read_parquet(DAILY)
    names = sorted(daily["underlying"].unique())
    rng = np.random.default_rng(7)
    sample = list(rng.choice(names, size=min(25, len(names)), replace=False))

    checked = 0
    for name in sample:
        g = daily[daily["underlying"] == name].sort_values("session").reset_index(drop=True)
        if len(g) < 80:
            continue
        full = segment(g, name)
        if not full:
            continue
        for k in [int(len(g) * f) for f in (0.35, 0.55, 0.75, 0.9)]:
            pre = segment(g.iloc[: k + 1].reset_index(drop=True), name)
            exp = [l for l in full if l.confirm_i <= k]
            assert len(pre) == len(exp), f"{name} k={k}: {len(pre)} vs {len(exp)} legs"
            for a, b in zip(pre, exp):
                for f in FIELDS:
                    va, vb = getattr(a, f), getattr(b, f)
                    if isinstance(va, float):
                        assert np.isclose(va, vb, rtol=1e-12, atol=0.0), \
                            f"{name} k={k} {f}: {va} != {vb}"
                    else:
                        assert va == vb, f"{name} k={k} {f}: {va} != {vb}"
                checked += 1

        # ATR itself
        a_full = wilder_atr(g["high"].to_numpy(float), g["low"].to_numpy(float),
                            g["close"].to_numpy(float))
        for k in (60, 120, 200):
            if k >= len(g):
                continue
            a_pre = wilder_atr(g["high"].to_numpy(float)[: k + 1],
                               g["low"].to_numpy(float)[: k + 1],
                               g["close"].to_numpy(float)[: k + 1])
            assert np.allclose(a_pre, a_full[: k + 1], rtol=1e-12, atol=0.0,
                               equal_nan=True), f"{name} ATR prefix k={k}"

    print(f"PREFIX-INVARIANCE PASS: {len(sample)} names, {checked} leg-field comparisons, "
          "ATR prefixes identical at rtol=1e-12")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
