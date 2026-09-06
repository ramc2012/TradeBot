"""The full auction: POC, value area, tails, day type, migration — not just IB.

THE CRITICISM (owner), and it is correct on both counts:
  1. IB WIDTH MAY EQUAL ATR IN MAGNITUDE BUT IT CAN SHOW DIRECTION -- through
     DAY TYPE. The MP teaching is that a SMALL initial balance tends to produce
     a TREND day (the opening hour failed to find both sides, so the auction has
     to travel to find them) while a LARGE one tends to BALANCE (both sides were
     found early, and the day rotates). Testing "does the IB break" ignores this
     entirely, which is why IB looked like nothing but restated volatility.
  2. ONE METRIC IS NOT AN AUCTION. A real MP read uses POC and prior POC, the
     value area and its extremes, developing value, poor highs and lows, failed
     auctions, initiative vs responsive activity, where value sits against prior
     value, the day type, and how value MIGRATES session to session.

So this builds the whole metric set and lets the tests choose.

WHAT THIS DATABASE CANNOT SUPPORT, stated up front rather than faked:
  VWAP and ABSORPTION both need volume, and BANKNIFTY SPOT CARRIES NONE -- every
  30m bar has volume 0 (2021-2026). The volume-less analogue of VWAP is the TPO
  PROFILE MEAN, which is computed here and named honestly; it is not VWAP and is
  not called VWAP. Absorption (size absorbed with no price progress) has no
  volume-free analogue and is simply absent. Both are computable on the OPTION
  series, which does carry volume, but only from 2026-03 when strike coverage
  becomes wide enough to track a consistent ATM contract.

TPO CONSTRUCTION
  A 30m bar is one TPO period; 13 periods per session. Each period marks every
  price bin its high-low range touches. The bin is PROPORTIONAL (TICK_BPS of
  price) rather than absolute, so a profile from 2021 at 35,000 and one from
  2026 at 57,000 have comparable resolution instead of the older one being
  coarser by a third.

DEVELOPING vs FINAL. Every metric that a trader would act on intraday is also
computed AS OF THE IB CLOSE (dev_*), because a day type known only at 15:15 is a
description, not a signal. The final versions are kept for classification and
for next-session context, where they are legitimately known in advance.

    python vanguard/research/mp_auction.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

IB_BARS = 2                  # 09:15 + 09:45 = the first hour
MIN_BARS = 12
TICK_BPS = 0.0004            # profile bin = 4bp of price (~23pts on BANKNIFTY)
VA_SHARE = 0.70
TAIL_MIN_BINS = 2            # single prints needed at an extreme to be a tail
TREND_CLOSE_PCT = 0.15       # trend day closes within this of its extreme

BAR_SQL = """
SELECT underlying,
       (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, high, low, close, volume
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND open IS NOT NULL AND high IS NOT NULL
  AND low IS NOT NULL AND close IS NOT NULL
ORDER BY underlying, ts
"""


def dsn() -> str:
    return os.environ.get("VANGUARD_DATABASE_URL",
                          "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie")


class Profile:
    """A TPO profile over a fixed price grid, built from 30m bars."""

    def __init__(self, low: np.ndarray, high: np.ndarray, tick: float):
        self.tick = tick
        self.lo = float(np.min(low))
        self.hi = float(np.max(high))
        n = max(int(round((self.hi - self.lo) / tick)) + 1, 1)
        self.n = n
        self.counts = np.zeros(n)
        # every period marks each bin its range touches: that is a TPO
        for l, h in zip(low, high):
            a = int(round((l - self.lo) / tick))
            b = int(round((h - self.lo) / tick))
            a, b = max(min(a, n - 1), 0), max(min(b, n - 1), 0)
            if b < a:
                a, b = b, a
            self.counts[a:b + 1] += 1.0

    def price(self, i: int) -> float:
        return self.lo + i * self.tick

    @property
    def poc(self) -> float:
        return self.price(int(self.counts.argmax()))

    def value_area(self) -> tuple[float, float]:
        """VAL, VAH by MP's rule: expand from the POC toward the busier side."""
        total = self.counts.sum()
        if total <= 0:
            return np.nan, np.nan
        p = int(self.counts.argmax())
        lo_i = hi_i = p
        held = self.counts[p]
        while held < VA_SHARE * total and (lo_i > 0 or hi_i < self.n - 1):
            below = self.counts[lo_i - 1] if lo_i > 0 else -1.0
            above = self.counts[hi_i + 1] if hi_i < self.n - 1 else -1.0
            if above >= below:
                hi_i += 1
                held += above
            else:
                lo_i -= 1
                held += below
        return self.price(lo_i), self.price(hi_i)

    @property
    def tpo_mean(self) -> float:
        """Profile mean price. The volume-free analogue of VWAP -- NOT VWAP."""
        total = self.counts.sum()
        if total <= 0:
            return np.nan
        return float((self.counts * (self.lo + np.arange(self.n) * self.tick)).sum() / total)

    def tail(self, end: str) -> int:
        """Single-print bins running in from an extreme: excess/tail length."""
        seq = self.counts[::-1] if end == "high" else self.counts
        k = 0
        for c in seq:
            if c == 1.0:
                k += 1
            else:
                break
        return k

    def poor(self, end: str) -> bool:
        """Poor high/low: the extreme bin was revisited, so the auction is
        UNFINISHED -- no single-print excess pushed it away."""
        c = self.counts[-1] if end == "high" else self.counts[0]
        return bool(c >= 2)

    def double_distribution(self) -> bool:
        """Two separated TPO modes with a genuine valley between them."""
        c = self.counts
        if self.n < 7 or c.max() < 3:
            return False
        peak = c.max()
        strong = c >= 0.7 * peak
        # indices of strong bins, split into runs
        idx = np.flatnonzero(strong)
        if len(idx) == 0:
            return False
        runs, start = [], idx[0]
        for a, b in zip(idx, idx[1:]):
            if b - a > 1:
                runs.append((start, a))
                start = b
        runs.append((start, idx[-1]))
        if len(runs) < 2:
            return False
        # the valley between the two busiest runs must be genuinely thin
        (a0, a1), (b0, b1) = runs[0], runs[-1]
        if b0 <= a1 + 1:
            return False
        valley = c[a1 + 1:b0]
        return bool(len(valley) and valley.min() <= 0.4 * min(c[a0:a1 + 1].max(),
                                                             c[b0:b1 + 1].max()))


def _classify(rng: float, ib: float, ext_up: float, ext_dn: float,
              close_pos: float, dd: bool) -> str:
    """Dalton's day types, from range against IB and which sides extended."""
    if ib <= 0:
        return "unknown"
    r = rng / ib
    two_sided = ext_up > 0 and ext_dn > 0
    if dd and r >= 2.0:
        return "double_distribution"
    if two_sided:
        # both sides of the IB were extended: the auction found both references
        return "neutral_extreme" if (close_pos >= 0.85 or close_pos <= 0.15) else "neutral"
    if r >= 2.0 and (close_pos >= 1 - TREND_CLOSE_PCT or close_pos <= TREND_CLOSE_PCT):
        return "trend"
    if r < 1.15:
        return "normal"
    if r < 2.0:
        return "normal_variation"
    return "normal_variation"        # extended but closed mid-range


def sessions(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")

    rows = []
    for (name, dt), g in bars.groupby(["underlying", "dt"], sort=False):
        g = g.sort_values("ts").reset_index(drop=True)
        if len(g) < MIN_BARS or g["ts"].iloc[0].time() != pd.Timestamp("09:15").time():
            continue
        hi, lo, cl = g["high"].values, g["low"].values, g["close"].values
        op = g["open"].values
        ref = cl[IB_BARS - 1]
        if not np.isfinite(ref) or ref <= 0:
            continue
        tick = max(ref * TICK_BPS, 1e-6)

        full = Profile(lo, hi, tick)
        dev = Profile(lo[:IB_BARS], hi[:IB_BARS], tick)      # the IB itself
        val, vah = full.value_area()
        d_val, d_vah = dev.value_area()

        ib_hi, ib_lo = hi[:IB_BARS].max(), lo[:IB_BARS].min()
        ib_range = ib_hi - ib_lo
        day_hi, day_lo = hi.max(), lo.min()
        rng = day_hi - day_lo
        ext_up, ext_dn = max(day_hi - ib_hi, 0.0), max(ib_lo - day_lo, 0.0)
        close_pos = (cl[-1] - day_lo) / rng if rng > 0 else 0.5

        rows.append({
            "underlying": name, "dt": dt,
            "open": op[0], "close": cl[-1], "high": day_hi, "low": day_lo,
            "range_pct": rng / ref, "volume": g["volume"].sum(),
            "ib_hi": ib_hi, "ib_lo": ib_lo, "ib_ref": ref,
            "ib_width": ib_range / ref,
            "ib_close_pos": (ref - ib_lo) / ib_range if ib_range > 0 else 0.5,
            "range_over_ib": rng / ib_range if ib_range > 0 else np.nan,
            "ext_up": ext_up / ref, "ext_dn": ext_dn / ref,
            "close_pos": close_pos,
            # final profile
            "poc": full.poc, "vah": vah, "val": val,
            "va_width": (vah - val) / ref if np.isfinite(vah) else np.nan,
            "tpo_mean": full.tpo_mean,
            "poor_high": full.poor("high"), "poor_low": full.poor("low"),
            "tail_high": full.tail("high") * tick / ref,
            "tail_low": full.tail("low") * tick / ref,
            "single_prints": int((full.counts == 1).sum()),
            "double_dist": full.double_distribution(),
            # developing, as of the IB close -- the only ones a signal may use
            "dev_poc": dev.poc, "dev_vah": d_vah, "dev_val": d_val,
            "dev_tpo_mean": dev.tpo_mean,
            "day_type": _classify(rng, ib_range, ext_up, ext_dn, close_pos,
                                  full.double_distribution()),
            # outcomes, all from the IB close so they are actually tradeable
            "rest_ret": cl[-1] / ref - 1.0,
            "rest_mfe": (day_hi - ref) / ref,
            "rest_mae": (ref - day_lo) / ref,
        })
    return pd.DataFrame(rows)


def add_context(frame: pd.DataFrame) -> pd.DataFrame:
    """Prior-session value, migration, initiative/responsive, forward returns."""
    out = []
    for _, g in frame.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        for c in ("poc", "vah", "val", "high", "low", "close", "tpo_mean"):
            g[f"prev_{c}"] = g[c].shift(1)
        ref = g["ib_ref"]
        va_w = (g["prev_vah"] - g["prev_val"]).replace(0, np.nan)

        # WHERE THE DAY OPENED against prior value -- MP's first read
        g["open_vs_prev_vah"] = (g["open"] - g["prev_vah"]) / va_w
        g["open_vs_prev_val"] = (g["open"] - g["prev_val"]) / va_w
        g["open_vs_prev_poc"] = (g["open"] - g["prev_poc"]) / va_w
        g["open_location"] = np.select(
            [g["open"] > g["prev_vah"], g["open"] < g["prev_val"]],
            ["above_value", "below_value"], default="inside_value")

        # INITIATIVE vs RESPONSIVE, judged at the IB close
        g["ib_above_prev_vah"] = (g["ib_hi"] - g["prev_vah"]) / va_w
        g["ib_below_prev_val"] = (g["prev_val"] - g["ib_lo"]) / va_w
        g["initiative"] = np.select(
            [g["ib_ref"] > g["prev_vah"], g["ib_ref"] < g["prev_val"]],
            ["initiative_buy", "initiative_sell"], default="responsive")

        # VALUE MIGRATION, session to session
        g["poc_migration"] = (g["poc"] - g["prev_poc"]) / ref
        g["va_overlap"] = ((np.minimum(g["vah"], g["prev_vah"])
                            - np.maximum(g["val"], g["prev_val"]))
                           / (g["vah"] - g["val"]).replace(0, np.nan))
        g["value_shift"] = np.select(
            [(g["val"] > g["prev_vah"]), (g["vah"] < g["prev_val"]),
             (g["val"] >= g["prev_val"]) & (g["vah"] <= g["prev_vah"])],
            ["higher_outside", "lower_outside", "inside"], default="overlapping")

        # FAILED AUCTION: probed beyond the prior extreme and closed back inside
        g["failed_high"] = (g["high"] > g["prev_high"]) & (g["close"] < g["prev_high"])
        g["failed_low"] = (g["low"] < g["prev_low"]) & (g["close"] > g["prev_low"])
        g["gap"] = g["open"] / g["prev_close"] - 1.0

        # IB width against the NAME'S OWN history, which is the form that
        # predicted break probability -- a raw percent just restates volatility
        g["ib_pct_rank"] = (g["ib_width"].rolling(120, min_periods=40)
                            .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))

        # trailing volatility, strictly lagged (a rolling mean includes its own
        # row -- that bug cost a corrected IC once already)
        prev_close = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"], (g["high"] - prev_close).abs(),
                        (g["low"] - prev_close).abs()], axis=1).max(axis=1)
        g["atr20"] = tr.rolling(20, min_periods=10).mean().shift(1) / prev_close

        for h in (1, 3):
            g[f"fwd{h}"] = g["close"].shift(-h) / g["close"] - 1.0
        g["next_open_ret"] = g["open"].shift(-1) / g["close"] - 1.0
        out.append(g)
    return pd.concat(out, ignore_index=True)


def load(connection, names: list[str], start) -> pd.DataFrame:
    bars = pd.read_sql(BAR_SQL, connection, params={"start": start, "names": names})
    return add_context(sessions(bars))
