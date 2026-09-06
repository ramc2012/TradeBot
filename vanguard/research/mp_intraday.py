"""Bar-by-bar Market Profile state, for intraday (scalping) strategies.

Everything until now collapsed a session to one row. A scalp needs the profile AS
IT DEVELOPS: what the value area looked like at 11:15, whether the IB had broken
by then, how far price sat from the developing POC. This builds that state for
each of the 13 thirty-minute bars of a session, using ONLY bars up to and
including the one in question, so a rule reading row k could have been acted on
at the close of bar k.

STATE PER BAR (all strictly causal):
    dev_poc/dev_vah/dev_val   TPO profile of bars 0..k
    dist_poc                  (close - developing POC) / price
    in_value                  close inside the developing value area
    ib_broken / ib_side       has a bar CLOSED beyond the IB extreme yet
    ext_ib                    how far beyond the IB extreme, in IB ranges
    vs_py_*                   position against the PRIOR SESSION's value area,
                              high and low -- the reference an intraday trader
                              actually uses
    back_in_value             re-entered the prior session's value area after
                              opening outside it -- the setup behind MP's "80%
                              rule", which is one of the few MP claims specific
                              enough to be falsified
    tpo_above / tpo_below     share of the session's TPOs so far above/below the
                              developing POC -- the shape of the distribution,
                              not just its location

FORWARD OUTCOMES per bar, for supervising a scalp: the return to the next bar,
to the session close, and the best/worst excursion between.

    from research.mp_intraday import load_intraday
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.mp_auction import BAR_SQL, IB_BARS, MIN_BARS, Profile, TICK_BPS


def _dev_profiles(low: np.ndarray, high: np.ndarray, tick: float) -> list[tuple]:
    """Developing POC/VAL/VAH after each bar -- rebuilt cumulatively."""
    out = []
    for k in range(len(low)):
        p = Profile(low[:k + 1], high[:k + 1], tick)
        val, vah = p.value_area()
        total = p.counts.sum()
        poc_i = int(p.counts.argmax())
        above = p.counts[poc_i + 1:].sum() / total if total > 0 else np.nan
        below = p.counts[:poc_i].sum() / total if total > 0 else np.nan
        out.append((p.poc, val, vah, above, below))
    return out


def build(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")

    rows = []
    prev = {}
    for (name, dt), g in bars.groupby(["underlying", "dt"], sort=True):
        g = g.sort_values("ts").reset_index(drop=True)
        if len(g) < MIN_BARS or g["ts"].iloc[0].time() != pd.Timestamp("09:15").time():
            continue
        hi, lo, cl, op = (g["high"].values, g["low"].values,
                          g["close"].values, g["open"].values)
        ref = cl[IB_BARS - 1]
        if not np.isfinite(ref) or ref <= 0:
            continue
        tick = max(ref * TICK_BPS, 1e-6)
        dev = _dev_profiles(lo, hi, tick)
        ib_hi, ib_lo = hi[:IB_BARS].max(), lo[:IB_BARS].min()
        ib_rng = max(ib_hi - ib_lo, 1e-9)
        p = prev.get(name, {})

        broken, side = False, 0
        was_out = None
        for k in range(len(g)):
            poc, val, vah, tabove, tbelow = dev[k]
            if k >= IB_BARS and not broken:
                if cl[k] > ib_hi:
                    broken, side = True, 1
                elif cl[k] < ib_lo:
                    broken, side = True, -1
            ext = 0.0
            if broken:
                ext = ((cl[k] - ib_hi) if side > 0 else (ib_lo - cl[k])) / ib_rng

            rec = {
                "underlying": name, "dt": dt, "ts": g["ts"].iloc[k], "bar": k,
                "open": op[k], "high": hi[k], "low": lo[k], "close": cl[k],
                "sess_open": op[0], "ib_hi": ib_hi, "ib_lo": ib_lo,
                "ib_width": ib_rng / ref,
                "dev_poc": poc, "dev_val": val, "dev_vah": vah,
                "dist_poc": (cl[k] - poc) / cl[k] * 100,
                "in_value": bool(val <= cl[k] <= vah),
                "tpo_above": tabove, "tpo_below": tbelow,
                "ib_broken": broken, "ib_side": side, "ext_ib": ext,
                "vs_ib_hi": (cl[k] - ib_hi) / ref * 100,
                "vs_ib_lo": (cl[k] - ib_lo) / ref * 100,
            }
            # position against the PRIOR session -- the intraday reference
            for key in ("poc", "val", "vah", "high", "low", "close"):
                rec[f"py_{key}"] = p.get(key, np.nan)
            pw = p.get("vah", np.nan) - p.get("val", np.nan)
            rec["vs_py_vah"] = (cl[k] - p.get("vah", np.nan)) / pw if pw else np.nan
            rec["vs_py_val"] = (cl[k] - p.get("val", np.nan)) / pw if pw else np.nan
            rec["in_py_value"] = bool(p.get("val", np.nan) <= cl[k] <= p.get("vah", np.nan)) \
                if np.isfinite(p.get("val", np.nan)) else False
            # the 80%-rule setup: opened outside prior value, then came back in
            if k == 0:
                was_out = (not rec["in_py_value"]) and np.isfinite(p.get("val", np.nan))
            rec["opened_outside_py"] = bool(was_out)
            rec["back_in_value"] = bool(was_out and rec["in_py_value"])
            rows.append(rec)

        full = Profile(lo, hi, tick)
        fval, fvah = full.value_area()
        prev[name] = {"poc": full.poc, "val": fval, "vah": fvah,
                      "high": hi.max(), "low": lo.min(), "close": cl[-1]}
    return pd.DataFrame(rows)


def add_outcomes(f: pd.DataFrame) -> pd.DataFrame:
    """Forward returns for a scalp: next bar, session close, and excursions."""
    out = []
    for (_, _), g in f.groupby(["underlying", "dt"], sort=False):
        g = g.sort_values("bar").reset_index(drop=True)
        c = g["close"]
        g["r_next"] = (c.shift(-1) / c - 1) * 100
        g["r_eod"] = (c.iloc[-1] / c - 1) * 100
        n = len(g)
        fmax, fmin = [], []
        hi, lo = g["high"].values, g["low"].values
        for k in range(n):
            if k + 1 >= n:
                fmax.append(np.nan)
                fmin.append(np.nan)
            else:
                fmax.append(hi[k + 1:].max())
                fmin.append(lo[k + 1:].min())
        g["mfe_eod"] = (np.array(fmax) / c - 1) * 100
        g["mae_eod"] = (np.array(fmin) / c - 1) * 100
        g["bars_left"] = n - 1 - g["bar"]
        out.append(g)
    return pd.concat(out, ignore_index=True)


def load_intraday(connection, names: list[str], start) -> pd.DataFrame:
    bars = pd.read_sql(BAR_SQL, connection, params={"start": start, "names": names})
    return add_outcomes(build(bars))
