"""Barrier-free move labels — the magnitude/direction/character of the ACTUAL forward move.

The fixed-barrier labeler asks a yes/no question against a level *we* picked (±m·ATR). That
imposes the magnitude as an input. Here we do the opposite: we record what price actually did,
and let the model LEARN the magnitude from the features. Per decision point per horizon:

  up_exc_atr   : max favourable excursion upward, ATR units (≥0)
  dn_exc_atr   : max favourable excursion downward, ATR units (≥0)
  net_atr      : signed endpoint displacement, ATR units
  magnitude_atr: max(up_exc, dn_exc) — "how big a move became available" (the regression target
                 for the magnitude head; far less noisy than endpoint return)
  dom_dir      : +1 if the up excursion dominated, else -1 (the direction the move 'went')
  reversed     : 1 if the endpoint closed against the dominant excursion (a reversal: price ran
                 one way then came back) — the mean-reversion signature
  time_to_peak_frac : minutes-to-dominant-extreme ÷ horizon (the "expected period" of the move)

Implementation note: paths are sliced with np.searchsorted over precomputed OHLC arrays and ATR
is precomputed once per session (vectorized), so this scales to multi-year minute data (the
earlier pandas-boolean-mask version was O(n) per grid point × horizon → unusable on 5yr × 1-min).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from tqdm import tqdm

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.timeutil import decision_grid, ensure_ist

log = get_logger()

DEFAULT_MOVE_HORIZONS: dict[str, int] = {"30m": 30, "60m": 60, "90m": 90, "120m": 120}
SWING_HORIZONS: dict[str, int] = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "1w": 5, "1M": 21}


class _Bars:
    """Precomputed array view of one underlying's minute bars for O(log n) path slicing."""

    def __init__(self, bars: pd.DataFrame, *, atr_window: int = 14):
        bars = bars.sort_index()
        # force nanoseconds (source index may be µs) so it matches pd.Timestamp(...).value
        self.ts = bars.index.values.astype("datetime64[ns]").view("int64")
        self.o = bars["open"].to_numpy(float)
        self.h = bars["high"].to_numpy(float)
        self.l = bars["low"].to_numpy(float)
        self.c = bars["close"].to_numpy(float)
        dates = bars.index.date
        # per-date first/last bar index + ordered session dates
        pos = pd.Series(np.arange(len(bars)), index=dates).groupby(level=0)
        last = pos.max(); first = pos.min()
        self.last_idx_by_date = last.to_dict()
        self.first_idx_by_date = first.to_dict()
        self.sorted_dates = list(last.index)
        self.pos = {d: i for i, d in enumerate(self.sorted_dates)}
        # ATR(as_of) = mean daily TR over the `window` sessions STRICTLY BEFORE the date (leak-free)
        daily = bars.groupby(dates).agg(high=("high", "max"), low=("low", "min"),
                                        close=("close", "last"))
        pc = daily["close"].shift(1)
        tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(),
                        (daily["low"] - pc).abs()], axis=1).max(axis=1)
        atr_asof = tr.rolling(atr_window, min_periods=2).mean().shift(1)
        self.atr_by_date = {d: (float(v) if pd.notna(v) and v > 0 else None)
                            for d, v in atr_asof.items()}

    def entry_open(self, t_ns: int) -> tuple[int, float] | None:
        i = int(np.searchsorted(self.ts, t_ns, "right"))
        return (i, float(self.o[i])) if i < len(self.ts) else None

    def excursions(self, i0: int, i1: int, entry: float, atr: float):
        """(up_exc, dn_exc, net, dom, mag, peak_ns) over bar slice [i0:i1] in ATR units."""
        hi = self.h[i0:i1]; lo = self.l[i0:i1]
        up = max(0.0, (float(hi.max()) - entry) / atr)
        dn = max(0.0, (entry - float(lo.min())) / atr)
        net = (float(self.c[i1 - 1]) - entry) / atr
        if up >= dn:
            dom, mag, peak = 1, up, i0 + int(hi.argmax())
        else:
            dom, mag, peak = -1, dn, i0 + int(lo.argmin())
        return up, dn, net, dom, mag, int(self.ts[peak])


def _build(session_dates, bars_by_underlying, horizons, grid_minutes, grid_start, grid_end,
           *, swing: bool):
    rows: list[dict] = []
    for underlying, raw in bars_by_underlying.items():
        b = _Bars(raw)
        desc = f"{'swing' if swing else 'move'}-labels:{underlying}"
        for sdate in tqdm(session_dates, desc=desc, leave=False):
            atr = b.atr_by_date.get(sdate)
            if not atr:
                continue
            for t in decision_grid(sdate, grid_minutes=grid_minutes, start=grid_start, end=grid_end):
                t_ist = ensure_ist(t)
                t_ns = pd.Timestamp(t_ist).value
                ent = b.entry_open(t_ns)
                if ent is None:
                    continue
                i0, entry = ent
                # §18 continuation/reversion anchor: the session's prevailing direction at t
                fi = b.first_idx_by_date.get(sdate)
                sess_open = float(b.o[fi]) if fi is not None else entry
                disp_sign = float(np.sign(entry - sess_open))
                row = {"underlying_key": underlying, "decision_time": t_ist,
                       "disp_sign": disp_sign}
                ok = False
                for hname, hv in horizons.items():
                    if swing:
                        j = b.pos.get(sdate)
                        if j is None:
                            continue
                        tgt = sdate if hv == 0 else (b.sorted_dates[j + hv]
                                                     if j + hv < len(b.sorted_dates) else None)
                        if tgt is None:
                            continue
                        i1 = b.last_idx_by_date.get(tgt)
                        i1 = None if i1 is None else i1 + 1
                    else:
                        end_ns = t_ns + hv * 60_000_000_000
                        i1 = int(np.searchsorted(b.ts, end_ns, "right"))
                    if i1 is None or i1 - i0 < 2:
                        continue
                    up, dn, net, dom, mag, peak_ns = b.excursions(i0, i1, entry, atr)
                    rev_min = 0.25 if swing else 0.1
                    reversed_ = int(np.sign(net) != dom and abs(net) > 1e-9 and mag > rev_min)
                    row[f"up_exc_atr_{hname}"] = up
                    row[f"dn_exc_atr_{hname}"] = dn
                    row[f"net_atr_{hname}"] = net
                    row[f"magnitude_atr_{hname}"] = mag
                    row[f"dom_dir_{hname}"] = dom
                    row[f"reversed_{hname}"] = reversed_
                    # §18 judgement labels: did the forward move CONTINUE the day's direction
                    # (trend) or REVERT against it (mean-reversion)? Signed continuation factors
                    # out the symmetric sign → the regime-predictable part. Direction is then
                    # reconstructed as disp_sign × sign(continuation).
                    cont = net * disp_sign                       # +continue, −revert (ATR units)
                    row[f"continuation_atr_{hname}"] = float(cont)
                    row[f"trend_continuation_score_{hname}"] = float(max(0.0, cont))
                    row[f"mean_reversion_score_{hname}"] = float(max(0.0, -cont))
                    if swing:
                        peak_date = pd.Timestamp(peak_ns, tz="UTC").tz_convert("Asia/Kolkata").date()
                        ttp_days = (peak_date - sdate).days
                        row[f"time_to_peak_days_{hname}"] = float(max(0, ttp_days))
                    else:
                        ttp_min = (peak_ns - t_ns) / 60_000_000_000
                        row[f"time_to_peak_frac_{hname}"] = min(1.0, max(0.0, ttp_min / hv))
                    ok = True
                if ok:
                    rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for hname in horizons:
        c = f"magnitude_atr_{hname}"
        if c in df.columns:
            log.info(f"{hname}: median mag={df[c].median():.2f} ATR, "
                     f"p90={df[c].quantile(0.9):.2f}, reversal={df[f'reversed_{hname}'].mean():.2f}")
    return df


def build_move_labels(session_dates, bars_by_underlying, *, horizons=None,
                      grid_minutes=30, grid_start="09:45", grid_end="14:30") -> pd.DataFrame:
    """One row per (underlying, decision_time) with per-horizon intraday barrier-free move labels."""
    return _build(session_dates, bars_by_underlying, horizons or DEFAULT_MOVE_HORIZONS,
                  grid_minutes, grid_start, grid_end, swing=False)


def build_swing_move_labels(session_dates, bars_by_underlying, *, horizons_days=None,
                            grid_minutes=30, grid_start="09:45", grid_end="14:30") -> pd.DataFrame:
    """Barrier-free move labels over multi-day (swing) horizons measured in TRADING DAYS."""
    return _build(session_dates, bars_by_underlying, horizons_days or SWING_HORIZONS,
                  grid_minutes, grid_start, grid_end, swing=True)
