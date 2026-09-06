"""ATM/OTM CE premium ratio across the full available expiry series.

WHY THE DAY-BY-DAY GUARD RAILS. Monthly aggregate strike counts look fine
(NIFTY 20-46, BANKNIFTY 4-48), but that hid two landmines found by checking
per-day: BANKNIFTY's 2026-03 expiry was tracked at only TWO strikes (61200 /
61300) for its entire life while spot sat near 57,000-58,000 -- "nearest
available strike" there is ~4,000 points from spot, nowhere near ATM. And the
last 2-4 trading days before every expiry, on both instruments, thin to 1-7
strikes as the pipeline's attention shifts to the next front month. A naive
"closest strike we have" ATM picker would silently mislabel far-OTM strikes as
ATM on exactly these days -- the same failure mode as the BANKNIFTY 60000 CE
mislabelled "near-ATM" earlier in this project, and the sparse-bar chart
artifact before that.

THE GUARD: a day only contributes if
    (a) at least MIN_STRIKES strikes are tracked for that front expiry, AND
    (b) the nearest available strike sits within MAX_ATM_DIST_PCT of spot
        (spot = that day's front-contract futures close, mp_futures-stitched).
A day failing either check is marked unavailable and gapped in the chart --
never interpolated, never silently swapped for a nearby session.

OTM DEFINITION. A single "one strike out" step is not comparable across days
with different strike spacing (50-point NIFTY days vs thinly-tracked days with
gaps of 500+). OTM is instead defined as the nearest available strike at or
beyond OTM_DIST_PCT above spot (default 1.0%) -- a distance-based definition
that means the same thing on every day regardless of what strikes happen to be
tracked.

FULL EXPIRY SERIES = every front-month CE roll in the reliably-covered window,
stitched with roll boundaries marked -- not one single contract's life, and not
silently bridged across the sparse months (2026-01/02, most of 2025) that
would corrupt the ratio the same way the earlier volume-bar chart was corrupted
by sparse early data.

    from research.mp_atm_otm import build_atm_otm_series
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_STRIKES = 8
MAX_ATM_DIST_PCT = 3.0      # nearest strike must be within this % of spot to count as ATM
OTM_DIST_PCT = 1.0          # OTM strike = nearest strike at/beyond this % above spot

FRONT_SQL = """
WITH front AS (
    SELECT date(time AT TIME ZONE 'Asia/Kolkata') AS dt, MIN(expiry) AS fexp
    FROM option_premium_candles
    WHERE underlying = %(sym)s AND interval = '30minute' AND option_type = 'CE'
      AND expiry >= date(time AT TIME ZONE 'Asia/Kolkata')
      AND time >= %(start)s AND time < %(end)s
    GROUP BY 1
)
SELECT f.dt, f.fexp AS expiry, o.strike,
       (array_agg(o.close ORDER BY o.time DESC))[1] AS close
FROM front f
JOIN option_premium_candles o
  ON o.underlying = %(sym)s AND o.interval = '30minute' AND o.option_type = 'CE'
 AND o.expiry = f.fexp AND date(o.time AT TIME ZONE 'Asia/Kolkata') = f.dt
 AND o.close IS NOT NULL AND o.close > 0
GROUP BY 1, 2, 3
"""

SPOT_SQL = """
SELECT date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       (array_agg(close ORDER BY time DESC))[1] AS spot
FROM index_futures_candles
WHERE underlying = %(sym)s AND interval = '30minute'
  AND time >= %(start)s AND time < %(end)s
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:29'
GROUP BY 1
"""


def build_atm_otm_series(connection, symbol: str, start, end) -> pd.DataFrame:
    opt = pd.read_sql(FRONT_SQL, connection,
                      params={"sym": symbol, "start": start, "end": end})
    spot = pd.read_sql(SPOT_SQL, connection,
                       params={"sym": symbol, "start": start, "end": end})
    if opt.empty or spot.empty:
        return pd.DataFrame()
    opt["strike"] = pd.to_numeric(opt["strike"])
    opt["close"] = pd.to_numeric(opt["close"])
    spot["spot"] = pd.to_numeric(spot["spot"])
    opt["dt"] = pd.to_datetime(opt["dt"])
    spot["dt"] = pd.to_datetime(spot["dt"])

    rows = []
    for dt, g in opt.groupby("dt"):
        srow = spot[spot["dt"] == dt]
        if srow.empty:
            continue
        s = float(srow["spot"].iloc[0])
        n_strikes = g["strike"].nunique()
        if n_strikes < MIN_STRIKES:
            rows.append({"dt": dt, "expiry": g["expiry"].iloc[0], "spot": s,
                         "available": False, "reason": f"only {n_strikes} strikes tracked"})
            continue
        g = g.sort_values("strike")
        atm_i = (g["strike"] - s).abs().idxmin()
        atm_strike = g.loc[atm_i, "strike"]
        atm_dist_pct = abs(atm_strike - s) / s * 100
        base = {"dt": dt, "expiry": g["expiry"].iloc[0], "spot": s, "n_strikes": n_strikes}
        if atm_dist_pct > MAX_ATM_DIST_PCT:
            rows.append({**base, "available": False, "atm_available": False,
                         "reason": f"nearest strike {atm_dist_pct:.1f}% from spot"})
            continue
        # ATM leg clears its own guard independent of the OTM leg below, so the
        # CE-premium series can stay denser than the ratio series -- never
        # silently dropped just because the ratio's second leg is missing.
        atm_px = float(g.loc[atm_i, "close"])
        base.update({"atm_strike": atm_strike, "atm_dist_pct": atm_dist_pct,
                     "atm_px": atm_px, "atm_available": atm_px > 0})
        otm_target = s * (1 + OTM_DIST_PCT / 100)
        above = g[g["strike"] >= otm_target]
        if above.empty:
            rows.append({**base, "available": False,
                         "reason": "no strike far enough OTM"})
            continue
        otm_i = above.index[0]     # sorted ascending -> first at/beyond target
        otm_strike = g.loc[otm_i, "strike"]
        otm_px = float(g.loc[otm_i, "close"])
        if otm_px <= 0:
            rows.append({**base, "available": False,
                         "reason": "OTM premium non-positive"})
            continue
        rows.append({
            **base, "available": True,
            "otm_strike": otm_strike,
            "otm_dist_pct": (otm_strike - s) / s * 100, "otm_px": otm_px,
            "ratio": atm_px / otm_px,
        })
    out = pd.DataFrame(rows).sort_values("dt").reset_index(drop=True)
    out["roll"] = out["expiry"] != out["expiry"].shift(1)
    out.loc[out.index[:1], "roll"] = False
    return out
