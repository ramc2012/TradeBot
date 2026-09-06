"""Is tail_low_long a SHAPE or a SPIKE? Threshold and horizon sensitivity.

A real effect is smooth. If the excess return exists only at the exact tail
percentile I happened to write down (0.80) and the exact horizon I happened to
declare (4 sessions), it is a coincidence dressed as a rule. Two gradients:

  1. THRESHOLD. Trailing percentile of the buying tail from 0.50 to 0.95, plus
     "any non-zero tail" and "no tail at all" as the two ends of the ladder. The
     excess should rise with tail size, monotonically-ish.
  2. HORIZON. H = 1..10 sessions. The excess should accumulate and then flatten,
     not appear at 4 and vanish either side.

Everything is measured as EXCESS over the same-window no-signal mean, with HAC
t-statistics, on the OOS window only -- the in-sample stretch is excluded so
these gradients cannot be read as an invitation to re-tune.

Also reported: the de-overlapped excess (trades spaced >= H sessions apart), the
one number that owes nothing to the overlap in H-session windows.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load                      # noqa: E402
from research.mp_swing_failure import t_stat, TAIL_WINDOW      # noqa: E402
from research.mp_swing_refute import hac_dummy, OOS_START      # noqa: E402


def main() -> int:
    connection = psycopg2.connect(dsn())
    try:
        raw = load(connection, ["BANKNIFTY"], date(2021, 1, 1))
    finally:
        connection.close()
    d = raw.sort_values("dt").reset_index(drop=True).copy()
    for h in range(1, 11):
        d[f"long{h}"] = (d["close"].shift(-h) / d["close"] - 1.0) * 100.0
    d["rank_low"] = (d["tail_low"].rolling(TAIL_WINDOW, min_periods=60)
                     .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
    o = d[d["dt"] >= OOS_START].reset_index(drop=True)

    print(f"\nBANKNIFTY OOS window only  {o['dt'].min().date()} .. "
          f"{o['dt'].max().date()}  ({len(o)} sessions)")

    print("\n" + "=" * 92)
    print("1. THRESHOLD GRADIENT at H=4 -- excess over the same-window no-signal mean")
    print("=" * 92)
    print(f"   {'buying-tail filter':<26}{'n':>6}{'fires%':>8}{'mean %':>10}"
          f"{'excess':>10}{'HAC t':>8}{'win':>7}")
    y = o["long4"].values
    ladder = [("no tail at all (tail==0)", (o["tail_low"] == 0)),
              ("any non-zero tail", (o["tail_low"] > 0))]
    for q in (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        ladder.append((f"tail rank >= {q:.2f}",
                       (o["tail_low"] > 0) & (o["rank_low"] >= q)))
    for label, mask in ladder:
        m = mask.fillna(False).astype(bool).values
        ok = np.isfinite(y)
        if m[ok].sum() < 12:
            continue
        beta, t = hac_dummy(y[ok], m[ok].astype(float), 3)
        r = y[ok & m]
        star = "   <-- the rule as declared" if label == "tail rank >= 0.80" else ""
        print(f"   {label:<26}{len(r):>6}{m[ok].mean() * 100:>7.1f}%"
              f"{r.mean():>+10.3f}{beta:>+10.3f}{t:>+8.2f}"
              f"{(r > 0).mean() * 100:>6.0f}%{star}")

    print("\n" + "=" * 92)
    print("2. HORIZON GRADIENT for the declared rule (tail rank >= 0.80)")
    print("=" * 92)
    print(f"   {'H':<5}{'n':>6}{'mean %':>10}{'excess':>10}{'HAC t':>8}{'win':>7}"
          f"{'excess/session':>16}")
    sig = ((o["tail_low"] > 0) & (o["rank_low"] >= 0.80)).fillna(False).values
    for h in range(1, 11):
        yy = o[f"long{h}"].values
        ok = np.isfinite(yy)
        beta, t = hac_dummy(yy[ok], sig[ok].astype(float), max(h - 1, 1))
        r = yy[ok & sig]
        mark = "   <-- declared" if h == 4 else ""
        print(f"   {h:<5}{len(r):>6}{r.mean():>+10.3f}{beta:>+10.3f}{t:>+8.2f}"
              f"{(r > 0).mean() * 100:>6.0f}%{beta / h:>+16.4f}{mark}")

    print("\n" + "=" * 92)
    print("3. DE-OVERLAPPED: signal days kept only if >= 4 sessions after the last kept one")
    print("=" * 92)
    idx = np.flatnonzero(sig & np.isfinite(o["long4"].values))
    kept, last = [], -99
    for i in idx:
        if i - last >= 4:
            kept.append(i)
            last = i
    kr = o.loc[kept, "long4"].values
    nonsig = o.loc[~sig & np.isfinite(o["long4"].values), "long4"].values
    print(f"   independent signal trades      {len(kr):>5}   mean {kr.mean():>+7.3f}%"
          f"   t {t_stat(kr):>+5.2f}   win {(kr > 0).mean() * 100:.0f}%")
    print(f"   excess over no-signal mean            {kr.mean() - nonsig.mean():>+7.3f}%"
          f"   (no-signal mean {nonsig.mean():+.3f}%)")
    print(f"   net of 4bp                            {kr.mean() - 0.04:>+7.3f}% per trade")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
