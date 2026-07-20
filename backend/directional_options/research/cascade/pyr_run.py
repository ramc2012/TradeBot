"""(C-cascade, study 4) PYRAMID ECONOMICS — the owner's structure simulated end
to end on OPTIONS, net of costs, against fixed size, stage-2-only, and matched
controls.

The owner's structure, verbatim: "When one timeframe lower to that large move
confirm we enter the position with small qty and adding to that when large time
frame confirms. We exit similarly as trade matures."

ARMS (identical universe, identical exit machinery, identical costs; only the
sizing/entry schedule differs)
  pyramid    : 1 unit at the stage-1 bar, +2 units at the stage-2 bar
  fixed_t1   : 3 units at the stage-1 bar, SAME abandonment rule (pure sizing
               comparison — only the schedule differs)
  fixed_hold : 3 units at the stage-1 bar, NO abandonment (held to maturity
               exit regardless of whether the higher timeframe confirms)
  s2_only    : 3 units at the stage-2 bar only (skip the early tranche)
  ...and every arm is also run on ctrl_long / ctrl_short / ctrl_random, whose
  bars carry no signal, through byte-identical machinery.

CAPITAL NORMALISATION. Every arm allocates the same maximum, UNITS_MAX * UNIT
rupees of premium. Returns are quoted on that allocation, so an arm that
deploys less capital is not flattered.

EXIT (position level, applied to whatever is open)
  * hard protective stop: spot touches -1.0 x entry-session daily ATR from the
    stage-1 entry spot -> close everything at that 30m bar's option CLOSE;
  * maturity scale-out: first firing of the maturity rule closes half the open
    position at the OPEN of the next session, second firing closes the rest;
  * hard caps: HOLD_CAP sessions, and always out at expiry - 2 calendar days
    (the vehicle is a DTE 8-22 monthly, so it cannot be held to a 20-session
    maturity exit — this is a property of the vehicle, reported not hidden);
  * abandonment: if the higher timeframe never confirms inside the 3-session
    window, the first tranche is closed at the open of session s0+4.

COSTS. Round-trip cost in % of premium is charged PER UNIT (COST_RT from
../setups_2d3d/harness.py: optimistic 0.6%, base 1.6%, pessimistic 4.0%).
Scaling out in two clips does not double the % cost, but it does double the
number of fills; the pessimistic column is the honest read for that.

No PG queries: spot comes from the cascade parquet cache, options from the
../setups_2d3d/data/optintra_*.csv extracts.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import harness  # noqa: E402
import mat_run  # noqa: E402
import run_cascade as rc  # noqa: E402
from mat_defs import RULES, STOP_ATR, maturity_fire_session  # noqa: E402

DATA = os.path.join(HERE, "data")
UNIT = 25_000.0             # rupees of premium per unit
UNITS_MAX = 3.0
HOLD_CAP = 10               # sessions (vehicle-constrained, see module docstring)
EXPIRY_BUFFER_DAYS = 2
ABANDON_SESSIONS = 3        # = stages.S2_WINDOW_SESSIONS
PRIMARY_RULE = "atr_contract"   # the owner's own first-named maturity tool
COST = harness.COST_RT
ARMS = ("pyramid", "fixed_t1", "fixed_hold", "s2_only")
FAMS = ("s1_primary", "ctrl_long", "ctrl_short", "ctrl_random")


# =========================================================================
# option tape
# =========================================================================

def load_opt_cached() -> pd.DataFrame:
    p = os.path.join(DATA, "pyr_opt.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    o = harness.load_options()
    o = o[["time", "underlying", "expiry", "strike", "option_type", "open", "close",
           "iv", "delta", "oi", "underlying_price", "instrument_key", "session", "mins",
           "is_monthly"]]
    o.to_parquet(p)
    return o


def contract_map(sel: pd.DataFrame, sess_map: dict) -> dict:
    """(underlying, side, ENTRY session index) -> contract row.

    `sel` is keyed by the SELECTION session (the 15:15 snapshot of the prior
    session), so the entry session index is sel_sidx + 1. Nothing here can see
    the entry session's own tape.
    """
    out = {}
    for r in sel.itertuples():
        s = sess_map.get((r.underlying, r.sel_session))
        if s is None:
            continue
        out[(r.underlying, int(r.side), int(s) + 1)] = (
            r.instrument_key, r.expiry, float(r.sel_mny))
    return out


PREM_FLOOR = True     # see module docstring: no-arbitrage floor on stale prints
FLOOR_HITS = {"entry_below_intrinsic": 0, "entry_total": 0}


def opt_series(opt: pd.DataFrame, keys: set) -> dict:
    """Per-contract 30m premium arrays.

    NO-ARBITRAGE FLOOR. The tape contains stale/illiquid prints that quote an
    ITM option BELOW its intrinsic value (observed: CDSL 1340CE quoted 12.00
    with spot at 1358.4, i.e. 6.4 points under intrinsic). Left alone, such a
    print becomes a fake cheap entry and manufactures a 20x "winner" that then
    dominates the whole concentration profile. Every premium is therefore
    floored at max(quote, intrinsic) using the SAME bar's underlying price —
    a same-bar, causal correction. The count of floored entries is reported.
    """
    ser = {}
    op = opt[opt["instrument_key"].isin(keys)]
    for k, g in op.groupby("instrument_key", sort=False):
        g = g.sort_values("time")
        strike = float(g["strike"].iloc[0])
        up = g["underlying_price"].to_numpy(float)
        intr = np.maximum(0.0, (up - strike) if g["option_type"].iloc[0] == "CE"
                          else (strike - up))
        o = g["open"].to_numpy(float)
        c = g["close"].to_numpy(float)
        if PREM_FLOOR:
            o = np.where(np.isfinite(intr), np.maximum(o, intr), o)
            c = np.where(np.isfinite(intr), np.maximum(c, intr), c)
        ser[k] = {"time": g["time"].to_numpy(), "open": o, "close": c,
                  "raw_open": g["open"].to_numpy(float),
                  "expiry": g["expiry"].iloc[0]}
    return ser


def prem(S: dict, t, field: str) -> float:
    i = int(np.searchsorted(S["time"], t))
    if i < len(S["time"]) and S["time"][i] == t:
        v = S[field][i]
        if np.isfinite(v) and v > 0:
            return float(v)
    i = int(np.searchsorted(S["time"], t, side="right")) - 1
    if i >= 0:
        v = S["close"][i]
        if np.isfinite(v) and v > 0:
            return float(v)
    return np.nan


# =========================================================================
# one episode, one arm
# =========================================================================

def simulate(B: dict, d: dict, ep, cmap: dict, ser: dict, arm: str, rule: str,
             lag_bars: int = 0) -> dict | None:
    """Return the episode's rupee P&L schedule for `arm`, or None if unfillable."""
    u, side, s0, atr = ep.underlying, int(ep.side), int(ep.s0), float(ep.atr_abs)
    e1 = int(ep.bar)
    sid = B["sidx"]

    has_s2 = bool(ep.s2)
    e2 = rc_first.get((u, int(ep.s2_sidx) + 1)) if has_s2 else None

    # the protective stop and the maturity clock are anchored at the arm's OWN
    # first entry (stage-2-only has no stage-1 tranche to anchor to)
    if arm == "s2_only":
        if e2 is None:
            return None
        anchor, s_anchor = e2, int(sid[e2])
        atr = float(B["atr"][e2])
    else:
        anchor, s_anchor = e1, s0
    entry_spot = B["open"][anchor]
    if not np.isfinite(entry_spot) or entry_spot <= 0 or not np.isfinite(atr) or atr <= 0:
        return None

    # --- position-level event calendar (all causal) ----------------------
    end_all = int(np.searchsorted(sid, s_anchor + HOLD_CAP, side="right"))
    if end_all <= anchor:
        return None
    if sid[end_all - 1] < s_anchor + HOLD_CAP:
        return None                      # truncated tape
    hi, lo = B["high"][anchor:end_all], B["low"][anchor:end_all]
    s_hit = (lo <= entry_spot - STOP_ATR * atr) if side > 0 else \
            (hi >= entry_spot + STOP_ATR * atr)
    stop_pos = anchor + int(np.argmax(s_hit)) if s_hit.any() else 10 ** 9

    f1 = maturity_fire_session(d, s_anchor, side, rule, HOLD_CAP)
    f2 = (maturity_fire_session(d, f1, side, rule, HOLD_CAP - (f1 - s_anchor))
          if f1 >= 0 else -1)

    def bar_of(sess):
        return rc_first.get((u, sess), None)

    sc1 = bar_of(f1 + 1) if f1 >= 0 else None
    sc2 = bar_of(f2 + 1) if f2 >= 0 else None
    cap_pos = end_all - 1                            # hard time cap (close of last bar)
    aband_pos = bar_of(s0 + ABANDON_SESSIONS + 1)

    # --- entries ----------------------------------------------------------
    lots = []                                        # (bar_pos, contract, units)
    if arm == "pyramid":
        if e2 is None and has_s2:
            return None
        lots.append((e1, cmap.get((u, side, s0)), 1.0))
        if has_s2:
            lots.append((e2, cmap.get((u, side, int(sid[e2]))), 2.0))
    elif arm in ("fixed_t1", "fixed_hold"):
        lots.append((e1, cmap.get((u, side, s0)), 3.0))
    elif arm == "s2_only":
        if not has_s2 or e2 is None:
            return None
        lots.append((e2, cmap.get((u, side, int(sid[e2]))), 3.0))
    else:
        raise ValueError(arm)
    if any(c is None for _, c, _ in lots):
        return None

    # --- open lots --------------------------------------------------------
    open_lots = []
    for pos, con, units in lots:
        key, expiry, mny = con
        S = ser.get(key)
        if S is None:
            return None
        t0 = B["time"][min(pos + lag_bars, len(B["time"]) - 1)]
        p0 = prem(S, t0, "open")
        if not np.isfinite(p0) or p0 < 1.0:
            return None
        ti = int(np.searchsorted(S["time"], t0))
        if ti < len(S["time"]) and S["time"][ti] == t0:
            FLOOR_HITS["entry_total"] += 1
            if S["open"][ti] > S["raw_open"][ti] + 1e-9:
                FLOOR_HITS["entry_below_intrinsic"] += 1
        # expiry guard: last bar strictly before expiry - buffer
        exp_ts = pd.Timestamp(expiry)
        open_lots.append({"pos": pos, "key": key, "S": S, "units": units,
                          "p0": p0, "expiry": exp_ts, "mny": mny})

    # --- exit calendar ----------------------------------------------------
    events = []          # (bar_pos, fraction_of_open, tag, use_close)
    if arm in ("pyramid", "fixed_t1") and not has_s2:
        if aband_pos is None:
            return None
        events.append((aband_pos, 1.0, "abandon", False))
    else:
        if sc1 is not None and sc1 <= cap_pos:
            events.append((sc1, 0.5, "mat1", False))
        if sc2 is not None and sc2 <= cap_pos:
            events.append((sc2, 1.0, "mat2", False))
        events.append((cap_pos, 1.0, "timecap", True))
    events.sort(key=lambda z: (z[0], z[1]))

    # --- run --------------------------------------------------------------
    pnl = {k: 0.0 for k in COST}
    fills = 0
    exited = 0.0
    for pos, frac, tag, use_close in events:
        live = [L for L in open_lots if L["pos"] <= pos and L["units"] > 1e-9]
        if not live:
            continue
        # stop takes precedence
        xpos, xtag, xclose = pos, tag, use_close
        if stop_pos <= pos:
            xpos, xtag, xclose = stop_pos, "stop", True
            frac = 1.0
        xpos = min(xpos + lag_bars, len(B["time"]) - 1)
        for L in live:
            # expiry cap
            xp = xpos
            while xp > L["pos"] and (pd.Timestamp(B["time"][xp]).tz_localize(None)
                                     >= L["expiry"] - pd.Timedelta(days=EXPIRY_BUFFER_DAYS)):
                xp -= 1
            px = prem(L["S"], B["time"][xp], "close" if xclose or xp != xpos else "open")
            if not np.isfinite(px):
                continue
            n = L["units"] * frac
            g = px / L["p0"] - 1.0
            for cname, c in COST.items():
                pnl[cname] += n * UNIT * (g - c)
            L["units"] -= n
            exited += n
            fills += 1
        if stop_pos <= pos:
            break
    # anything still open (shouldn't happen) marked at the cap
    for L in open_lots:
        if L["units"] > 1e-9:
            px = prem(L["S"], B["time"][cap_pos], "close")
            if np.isfinite(px):
                g = px / L["p0"] - 1.0
                for cname, c in COST.items():
                    pnl[cname] += L["units"] * UNIT * (g - c)
            L["units"] = 0.0

    units_used = sum(un for _, _, un in lots)
    return {"family": ep.family, "arm": arm, "rule": rule, "underlying": u,
            "mkt": ep.mkt, "side": side, "quarter": ep.quarter,
            "entry_time": ep.entry_time, "s2": int(has_s2),
            "units": units_used, "fills": fills,
            "mny": float(np.mean([L["mny"] for L in open_lots])),
            **{"pnl_" + k: v for k, v in pnl.items()},
            **{"roc_" + k: v / (UNITS_MAX * UNIT) for k, v in pnl.items()}}


rc_first: dict = {}


# =========================================================================

def main() -> None:
    global rc_first
    print("loading spot ...", flush=True)
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    rc_first = bars.first_bar
    D = mat_run.daily_arrays(daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    epi = epi[epi["family"].isin(FAMS)].copy()
    print("episodes", epi.groupby("family").size().to_dict(), flush=True)

    print("loading options ...", flush=True)
    opt = load_opt_cached()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}

    out = []
    for band in harness.MNY_BANDS:
        sel = harness.build_selection(opt, harness.MNY_BANDS[band])
        cmap = contract_map(sel, sess_map)
        keys = {v[0] for v in cmap.values()}
        ser = opt_series(opt, keys)
        print(f"band {band}: contracts {len(keys)} series {len(ser)}", flush=True)
        for rule in (PRIMARY_RULE,) + tuple(r for r in RULES if r != PRIMARY_RULE):
            for arm in ARMS:
                for ep in epi.itertuples():
                    B = bars.u.get(ep.underlying)
                    d = D.get(ep.underlying)
                    if B is None or d is None:
                        continue
                    r = simulate(B, d, ep, cmap, ser, arm, rule)
                    if r is None:
                        continue
                    r["band"] = band
                    # +1 entry/exit bar lag variant, primary rule only
                    if rule == PRIMARY_RULE:
                        rl = simulate(B, d, ep, cmap, ser, arm, rule, lag_bars=1)
                        r["roc_base_lag1"] = rl["roc_base"] if rl else np.nan
                    out.append(r)
            print(f"  rule {rule} done, rows {len(out)}", flush=True)
    tr = pd.DataFrame(out)
    print("entry quotes floored to intrinsic: "
          f"{FLOOR_HITS['entry_below_intrinsic']} / {FLOOR_HITS['entry_total']}")
    tr.to_parquet(os.path.join(DATA, "pyr_trades.parquet"))
    print("wrote pyr_trades.parquet", len(tr))


if __name__ == "__main__":
    main()
