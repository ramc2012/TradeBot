"""Proper Market Profile from 30-minute bars, and the TWO-SIDED break move.

WHY THIS REPLACES THE MONTHLY WORK. mp_initial_balance.py and ib_picker.py used
a "monthly IB" of the first 3 daily bars -- an analogy, not a profile. The 30m
table supports the real thing: 13 bars per session at 09:15..15:15, so the
INITIAL BALANCE IS THE FIRST HOUR, which is what MP actually means by it, and a
TPO profile can be built bar by bar instead of smeared from daily ranges.

THE REFRAMING THAT MATTERS (owner): we are interested in MOVES, not in signed
returns. A break BELOW the IB is just as tradeable as one above -- it is simply
a PE instead of a CE. Every earlier study scored signed return, which treats a
-8% break as a failure when it is a doubled put. So the target here is the
CORRECT-SIDE move: favourable excursion measured in the direction the auction
actually broke.

This also explains why IB width was inert before. IB width is a VOLATILITY
measure, and volatility does not rank signed returns -- but it should rank
MAGNITUDE. The two-sided target is the one it has a right to work on.

THE CONTROL THAT DECIDES IT. "Volatile names move more" is true, useless, and
already priced: an option on a volatile name costs more in exactly that
proportion. So IB width is tested against TRAILING ATR -- if it only reproduces
what ATR already knew, there is no information in the profile. The feature that
matters is IB width RELATIVE to the name's own recent range (ib_vs_atr): an
unusually wide or narrow opening hour, which is a statement about today rather
than about the name.

PROFILE CONSTRUCTION
    TPO         time-price opportunity: each 30m bar marks every price bin its
                range touches. Volume-weighting is NOT used as the primary
                measure because the indices carry zero volume in this table
                (BANKNIFTY: 2,497 of 2,540 bars) -- TPO is the classic MP
                construction anyway and is the only one that is uniform across
                indices and stocks. Volume profile is computed alongside for
                stocks as a cross-check.
    POC / VA    busiest bin, and the 70% band expanded from it, MP's rule of
                taking whichever adjacent bin holds more.
    IB          first two bars, 09:15-10:15.
    BREAK       a 30m bar CLOSING beyond the IB extreme -- acceptance, not the
                wick that MP reads as rejection. Entry is that bar's close, so
                nothing in the outcome is knowable before the entry exists.

    python vanguard/research/mp_profile.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

IB_BARS = 2                 # 09:15 and 09:45 -> the first hour
MIN_BARS = 12
PROFILE_BINS = 40
VA_SHARE = 0.70
FWD_SESSIONS = 3            # the owner's "1 hour to 2-3 days" horizon

BAR_SQL = """
SELECT underlying,
       (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, high, low, close, volume
FROM underlying_spot_candles
WHERE interval = '30minute'
  AND time >= %(start)s
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND open IS NOT NULL AND high IS NOT NULL
  AND low IS NOT NULL AND close IS NOT NULL
ORDER BY underlying, ts
"""


def dsn() -> str:
    return os.environ.get("VANGUARD_DATABASE_URL",
                          "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie")


def _profile(low: np.ndarray, high: np.ndarray, weight: np.ndarray
             ) -> tuple[float, float, float]:
    """POC, VAL, VAH over PROFILE_BINS bins of the session range."""
    lo, hi = float(low.min()), float(high.max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.nan, np.nan, np.nan
    edges = np.linspace(lo, hi, PROFILE_BINS + 1)
    centres = (edges[:-1] + edges[1:]) / 2
    counts = np.zeros(PROFILE_BINS)
    for l, h, w in zip(low, high, weight):
        if not (np.isfinite(l) and np.isfinite(h)) or h < l:
            continue
        a = min(max(int(np.searchsorted(edges, l) - 1), 0), PROFILE_BINS - 1)
        b = min(max(int(np.searchsorted(edges, h) - 1), 0), PROFILE_BINS - 1)
        if b < a:
            a, b = b, a
        counts[a:b + 1] += w / (b - a + 1)      # one TPO shared across its bins
    total = counts.sum()
    if total <= 0:
        return np.nan, np.nan, np.nan
    poc = int(counts.argmax())
    lo_i = hi_i = poc
    held = counts[poc]
    while held < VA_SHARE * total and (lo_i > 0 or hi_i < PROFILE_BINS - 1):
        below = counts[lo_i - 1] if lo_i > 0 else -1.0
        above = counts[hi_i + 1] if hi_i < PROFILE_BINS - 1 else -1.0
        if above >= below:
            hi_i += 1
            held += above
        else:
            lo_i -= 1
            held += below
    return float(centres[poc]), float(centres[lo_i]), float(centres[hi_i])


def sessions(bars: pd.DataFrame) -> pd.DataFrame:
    """One row per (name, session): profile, IB, and the first IB break."""
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars["volume"] = bars["volume"].fillna(0.0)

    rows = []
    for (name, dt), g in bars.groupby(["underlying", "dt"], sort=False):
        g = g.sort_values("ts").reset_index(drop=True)
        if len(g) < MIN_BARS or g["ts"].iloc[0].time() != pd.Timestamp("09:15").time():
            continue                              # stub session or a late feed
        hi, lo, cl = g["high"].values, g["low"].values, g["close"].values
        ib_hi, ib_lo = hi[:IB_BARS].max(), lo[:IB_BARS].min()
        ib_ref = cl[IB_BARS - 1]
        if not np.isfinite(ib_ref) or ib_ref <= 0 or ib_hi <= ib_lo:
            continue
        poc, val, vah = _profile(lo, hi, np.ones(len(g)))
        vpoc, vval, vvah = _profile(lo, hi, g["volume"].values)

        # First ACCEPTANCE beyond either IB extreme, after the IB itself.
        post = np.arange(IB_BARS, len(g))
        up = post[cl[IB_BARS:] > ib_hi]
        dn = post[cl[IB_BARS:] < ib_lo]
        first_up = int(up[0]) if len(up) else None
        first_dn = int(dn[0]) if len(dn) else None
        if first_up is not None and (first_dn is None or first_up < first_dn):
            side, k = 1, first_up
        elif first_dn is not None:
            side, k = -1, first_dn
        else:
            side, k = 0, None

        rec = {
            "underlying": name, "dt": dt,
            "open": g["open"].iloc[0], "close": cl[-1],
            "high": hi.max(), "low": lo.min(), "volume": g["volume"].sum(),
            "ib_hi": ib_hi, "ib_lo": ib_lo, "ib_ref": ib_ref,
            "ib_width": (ib_hi - ib_lo) / ib_ref,
            "poc": poc, "val": val, "vah": vah,
            "vpoc": vpoc, "vval": vval, "vvah": vvah,
            "side": side, "break_bar": k, "bars": len(g),
        }
        if side != 0:
            entry = cl[k]
            # the exact bar the break was accepted on -- the option leg has to
            # buy at this timestamp or it is not the same trade
            rec["break_ts"] = g["ts"].iloc[k]
            after_hi = hi[k + 1:]
            after_lo = lo[k + 1:]
            rec["entry"] = entry
            rec["bars_after"] = len(after_hi)
            if len(after_hi):
                # favourable and adverse excursion IN THE BREAK DIRECTION
                fav = (after_hi.max() - entry) if side > 0 else (entry - after_lo.min())
                adv = (entry - after_lo.min()) if side > 0 else (after_hi.max() - entry)
                rec["mfe_intraday"] = fav / entry
                rec["mae_intraday"] = adv / entry
            rec["eod"] = side * (cl[-1] / entry - 1.0)
            rec["break_frac"] = k / (len(g) - 1)      # how early the break came
        rows.append(rec)
    return pd.DataFrame(rows)


def add_context(frame: pd.DataFrame, fwd: int = FWD_SESSIONS) -> pd.DataFrame:
    """Prior-session profile context, trailing volatility, forward excursions."""
    out = []
    for _, g in frame.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        prev_close = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"],
                        (g["high"] - prev_close).abs(),
                        (g["low"] - prev_close).abs()], axis=1).max(axis=1)
        # LOOK-AHEAD, FIXED: tr at row t is built from session t's OWN high and
        # low, rolling(20) at row t includes row t, and dividing by g["close"]
        # uses that session's 15:15 close. All three are unknown at the 10:15
        # break. Both the window and the divisor are lagged one session so this
        # is strictly trailing and usable live.
        g["atr20"] = (tr.rolling(20, min_periods=10).mean().shift(1)
                      / g["close"].shift(1))
        # the feature that is about TODAY rather than about the name
        g["ib_vs_atr"] = g["ib_width"] / g["atr20"].replace(0, np.nan)
        g["gap"] = g["open"] / prev_close - 1.0

        for c in ("vah", "val", "poc"):
            g[f"prev_{c}"] = g[c].shift(1)
        va_w = (g["prev_vah"] - g["prev_val"]).replace(0, np.nan)
        g["open_vs_prev_vah"] = (g["open"] - g["prev_vah"]) / va_w
        g["open_vs_prev_poc"] = (g["open"] - g["prev_poc"]) / va_w
        g["ib_vs_prev_poc"] = (g["ib_ref"] - g["prev_poc"]) / va_w

        # Forward excursion over the next `fwd` SESSIONS, from the break close.
        # Uses only sessions strictly after the break session, so the intraday
        # part and this part never overlap and cannot double-count.
        fhi = g["high"].shift(-1).rolling(fwd, min_periods=1).max().shift(-fwd + 1)
        flo = g["low"].shift(-1).rolling(fwd, min_periods=1).min().shift(-fwd + 1)
        fcl = g["close"].shift(-fwd)
        side, entry = g["side"], g["entry"] if "entry" in g else np.nan
        best = np.where(side > 0, fhi, flo)
        worst = np.where(side > 0, flo, fhi)
        combined_fav = np.where(side > 0, np.maximum(fhi, entry), np.minimum(flo, entry))
        g[f"mfe_{fwd}d"] = side * (pd.Series(best, index=g.index) / entry - 1.0)
        g[f"mae_{fwd}d"] = -side * (pd.Series(worst, index=g.index) / entry - 1.0)
        g[f"ret_{fwd}d"] = side * (fcl / entry - 1.0)
        # peak reached anywhere from the break to the end of the horizon:
        # what a trade with a good exit could actually have captured
        g["mfe_total"] = np.maximum(
            g["mfe_intraday"].fillna(0.0),
            side * (pd.Series(combined_fav, index=g.index) / entry - 1.0))
        out.append(g)
    return pd.concat(out, ignore_index=True)


def load(connection, names: list[str], start) -> pd.DataFrame:
    bars = pd.read_sql(BAR_SQL, connection, params={"start": start, "names": names})
    return add_context(sessions(bars))
