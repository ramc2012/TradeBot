"""Fixed-volume bars as an order-flow read: effort vs result, not "volume=bullish".

THE FRAME (owner brief, 2026-08-29). A fixed-volume bar holds ~constant Q, so:
    duration          activity intensity (fast bar = eager participants)
    signed efficiency (C-O)/Q            impact per unit volume -- ALREADY a
                                          price-per-volume measure because Q is
                                          constant by construction
    imbalance         which side was aggressive
    close location    (2C-H-L)/(H-L)     did the bar hold its extreme or reject it
    follow-through    did the NEXT bars accept the level, or give it back

Effort without result (huge volume, no displacement) is absorption, not a
breakout -- the opposite of "high volume = bullish".

WHAT THIS DATASET CANNOT MEASURE, stated up front. There is no tick-level trade
print and no bid/ask depth anywhere in this store (confirmed repeatedly this
project: market_ticks carries quotes only, underlying/futures candles carry
OHLCV only). True aggressor-side imbalance -- (V_ask-V_bid)/(V_ask+V_bid) from
actual trade prints -- DOES NOT EXIST HERE. `imbalance_proxy` below is an
INFERRED substitute: each bar is built from many 1-minute prints, and each of
those is tick-rule classified (uptick = buyer-initiated, downtick =
seller-initiated, a flat print inherits the prior tick's side -- the same
zero-tick convention as MACD mini's of_normalise.py). Imbalance is then the
volume-weighted share of up-classified vs down-classified minutes inside the
bar. This is a proxy with real misclassification error (Lee & Ready-style rules
are known to mislabel a material share of prints -- see Chakrabarty et al., cited
by the owner) and is reported as `of_available=True, of_measured=False`
throughout so it is never confused with real depth data.

SIGNAL SOURCE DISCIPLINE. Built on NIFTY/BANKNIFTY FUTURES only, front-contract
stitched via mp_futures.load_futures_bars -- never on option volume, which is
fragmented across strikes/expiries and mixes hedging and vol trades with
directional flow (the owner's point, and it directly indicts the option-volume-
bar tabs in the earlier chart artifact).

    from research.mp_vbar_flow import build_vbars, add_location
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TICK_MIN = 1   # the 30m-bar-equivalent; here we use the constituent 1-minute
               # prints directly, so no separate tick-bar aggregation is needed


def volume_bars_with_flow(bars: pd.DataFrame, target_bars: int) -> pd.DataFrame:
    """Fixed-volume OHLCV bars from 1-minute prints, carrying the tick-rule
    imbalance proxy and every raw ingredient the framework asks for."""
    bars = bars.sort_values("ts").reset_index(drop=True)
    threshold = max(bars["volume"].sum() / target_bars, 1.0)

    # Lee-Ready-style tick classification of each 1-minute print, causal
    # (uses only that print's close vs the PRIOR print's close; a flat tick
    # carries forward the last classified side rather than guessing).
    c = bars["close"].to_numpy()
    side = np.zeros(len(c), dtype=int)
    last = 0
    for i in range(len(c)):
        if i == 0:
            side[i] = 0
        elif c[i] > c[i - 1]:
            side[i] = 1
        elif c[i] < c[i - 1]:
            side[i] = -1
        else:
            side[i] = last
        last = side[i] if side[i] != 0 else last
    bars = bars.copy()
    bars["_side"] = side

    rows = []
    acc_v = 0.0
    up_v = down_v = 0.0
    cur_o = cur_h = cur_l = None
    cur_start = None
    n_minutes = 0
    for i in range(len(bars)):
        r = bars.iloc[i]
        if cur_o is None:
            cur_o, cur_h, cur_l = r.open, r.high, r.low
            cur_start = r.ts
        else:
            cur_h = max(cur_h, r.high)
            cur_l = min(cur_l, r.low)
        acc_v += r.volume
        n_minutes += 1
        if r["_side"] > 0:
            up_v += r.volume
        elif r["_side"] < 0:
            down_v += r.volume
        else:
            # unclassifiable at the very first print of the whole series only
            up_v += r.volume / 2
            down_v += r.volume / 2
        if acc_v >= threshold or i == len(bars) - 1:
            denom = up_v + down_v
            rows.append({
                "t_start": cur_start, "t_end": r.ts,
                "open": cur_o, "high": cur_h, "low": cur_l, "close": r.close,
                "volume": acc_v, "n_minutes": n_minutes,
                "duration_s": (r.ts - cur_start).total_seconds(),
                "up_volume": up_v, "down_volume": down_v,
                "imbalance_proxy": (up_v - down_v) / denom if denom > 0 else np.nan,
            })
            cur_o = None
            acc_v = 0.0
            up_v = down_v = 0.0
            n_minutes = 0
    out = pd.DataFrame(rows)
    out["net_move"] = out["close"] - out["open"]
    out["efficiency"] = out["net_move"] / out["volume"].replace(0, np.nan)
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    out["close_location"] = (2 * out["close"] - out["high"] - out["low"]) / rng
    out["range_pct"] = rng / out["close"].shift(1).fillna(out["close"]) * 100
    return out


def add_session_phase(df: pd.DataFrame) -> pd.DataFrame:
    """Opening / midday / closing tercile of the NSE session, from t_start."""
    df = df.copy()
    t = df["t_start"].dt.time
    df["session_phase"] = pd.cut(
        pd.to_datetime(df["t_start"].dt.strftime("%H:%M:%S")),
        bins=[pd.Timestamp("09:14:59"), pd.Timestamp("11:05:00"),
              pd.Timestamp("13:25:00"), pd.Timestamp("15:30:00")],
        labels=["opening", "midday", "closing"])
    return df


def add_vol_regime(df: pd.DataFrame, atr_col: str = "atr14") -> pd.DataFrame:
    """Low/mid/high tercile of trailing volatility, computed causally on the
    bar's OWN lagged rolling ATR so a bar is never bucketed using itself."""
    df = df.copy()
    if atr_col not in df.columns:
        c = df["close"]
        h, l = df["high"], df["low"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        df[atr_col] = tr.rolling(14, min_periods=8).mean()
    lagged = df[atr_col].shift(1)
    df["vol_regime"] = pd.qcut(lagged.rank(method="first"), 3,
                               labels=["low", "mid", "high"])
    return df


def normalize_causal(df: pd.DataFrame, col: str, window: int = 60) -> pd.Series:
    """Percentile rank of `col` within its trailing `window` bars of the SAME
    (session_phase, vol_regime) bucket -- causal, excludes the current bar.

    MUST be a bounded ROLLING window, not expanding: an early bug used
    .expanding() here, so a handful of degenerate zero-duration bars from the
    sparse-coverage 2024 period (one lumpy 1-minute print alone crossing the
    volume threshold -- see mp_vbar_signal's front-matter on sparse coverage)
    never aged out of the trailing set and permanently pinned every later
    bar's speed percentile near zero. A bounded window lets stale extremes
    fall out after `window` bars, as a causal percentile requires."""
    out = pd.Series(np.nan, index=df.index)
    for (phase, regime), g in df.groupby(["session_phase", "vol_regime"], observed=True):
        s = g[col]
        pct = s.rolling(window, min_periods=10).apply(
            lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else np.nan,
            raw=True)
        out.loc[g.index] = pct
    return out
