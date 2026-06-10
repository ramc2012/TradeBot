"""Multi-horizon expected-move labels (spec v2 §4–6).

Tests the central thesis: the same MP+OF condition may be weak at 90 min but strong at
EOD / multi-day / weekly. For each intraday grid decision point we compute, per horizon,
the endpoint forward return in ATR units, the path MFE/MAE in ATR, and a 3-class direction
with an ATR deadband. Labels join to the existing grid features on (underlying_key, decision_time).

Horizons are expressed in TRADING days (EOD = same session close). ATR_ref is the prior-close
14-session daily ATR (leak-free, instrument-independent) — used for every horizon so moves are
comparable across NIFTY/BANKNIFTY.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from tqdm import tqdm

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import decision_grid, ensure_ist

log = get_logger()

# Horizon name → number of trading days forward (EOD = 0 → same-day close).
DEFAULT_HORIZONS: dict[str, int] = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "1w": 5}


@dataclass
class _SessionIndex:
    """Maps a date to its position in the sorted session list, for trading-day arithmetic."""

    dates: list
    pos: dict

    @classmethod
    def build(cls, bars: pd.DataFrame) -> "_SessionIndex":
        ds = sorted({d for d in bars.index.date})
        return cls(ds, {d: i for i, d in enumerate(ds)})

    def target_date(self, d, n_days: int):
        i = self.pos.get(d)
        if i is None:
            return None
        j = i + n_days
        return self.dates[j] if 0 <= j < len(self.dates) else None


def _entry_price(bars: pd.DataFrame, t):
    fwd = bars[bars.index > t]
    return None if fwd.empty else float(fwd.iloc[0]["open"])


def build_multihorizon_labels(
    session_dates: list,
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    horizons: dict[str, int] | None = None,
    deadband_atr: float = 0.25,
    grid_minutes: int = 30,
    grid_start: str = "09:45",
    grid_end: str = "14:30",
) -> pd.DataFrame:
    """One row per (underlying, grid decision_time) with per-horizon labels.

    Columns per horizon h: ret_atr_{h}, dir_{h} (up/down/none), is_move_{h},
    mfe_atr_{h}, mae_atr_{h}.
    """
    horizons = horizons or DEFAULT_HORIZONS
    rows: list[dict] = []

    for underlying, bars in bars_by_underlying.items():
        sidx = _SessionIndex.build(bars)
        for sdate in tqdm(session_dates, desc=f"mh-labels:{underlying}", leave=False):
            atr = atr_reference(bars.rename(columns=str.lower), sdate)
            if atr is None or atr <= 0:
                continue
            day_bars = bars[bars.index.date == sdate]
            if day_bars.empty:
                continue
            for t in decision_grid(sdate, grid_minutes=grid_minutes, start=grid_start, end=grid_end):
                t = ensure_ist(t)
                entry = _entry_price(bars, t)
                if entry is None:
                    continue
                row = {"underlying_key": underlying, "decision_time": t}
                ok = False
                for hname, ndays in horizons.items():
                    tgt_date = sdate if ndays == 0 else sidx.target_date(sdate, ndays)
                    if tgt_date is None:
                        continue
                    # Forward path: from just after t through the close of the target date.
                    tgt_bars = bars[bars.index.date == tgt_date]
                    if tgt_bars.empty:
                        continue
                    target_close_time = tgt_bars.index[-1]
                    path = bars[(bars.index > t) & (bars.index <= target_close_time)]
                    if path.empty:
                        continue
                    target_close = float(tgt_bars.iloc[-1]["close"])
                    ret_atr = (target_close - entry) / atr
                    up_exc = float((path["high"] - entry).max()) / atr
                    dn_exc = float((entry - path["low"]).max()) / atr
                    if ret_atr > deadband_atr:
                        d, mfe, mae = "up", up_exc, dn_exc
                    elif ret_atr < -deadband_atr:
                        d, mfe, mae = "down", dn_exc, up_exc
                    else:
                        d, mfe, mae = "none", max(up_exc, dn_exc), min(up_exc, dn_exc)
                    row[f"ret_atr_{hname}"] = ret_atr
                    row[f"dir_{hname}"] = d
                    row[f"is_move_{hname}"] = int(d != "none")
                    row[f"mfe_atr_{hname}"] = mfe
                    row[f"mae_atr_{hname}"] = mae
                    ok = True
                if ok:
                    rows.append(row)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for hname in horizons:
        col = f"dir_{hname}"
        if col in df.columns:
            counts = df[col].value_counts().to_dict()
            log.info(f"horizon {hname}: {counts}")
    return df
