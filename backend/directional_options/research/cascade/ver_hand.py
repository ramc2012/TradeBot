"""(VERIFY 1d) Hand-derivation of ONE full pyramid episode.

Prints every decision input and every fill of a single (underlying, side,
episode) so each number can be checked against raw PG by eye:
  * the stage-1 30m decision bar and the values that made it fire
  * the daily state on the last CLOSED daily bar (proving it was NOT already on)
  * the stage-2 confirming daily bar and the tranche-2 entry bar
  * the contract chosen at the prior session's 15:15 snapshot
  * every option fill with the exact bar timestamp, and whether that bar exists
    in the truncated tape, the repaired tape, or neither.

Usage:  python ver_hand.py [UNDERLYING] [YYYY-MM-DD]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import harness  # noqa: E402
import mat_run  # noqa: E402
import pyr_run as P  # noqa: E402
import run_cascade as rc  # noqa: E402
import ver_repair as VR  # noqa: E402
from mat_defs import STOP_ATR, maturity_fire_session  # noqa: E402
from stages import daily_state  # noqa: E402

U = sys.argv[1] if len(sys.argv) > 1 else "ICICIGI"
DAY = sys.argv[2] if len(sys.argv) > 2 else "2026-03-13"
BAND = "deep_itm"
IST = harness.IST


def show(title):
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)


def main() -> None:
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    P.rc_first = bars.first_bar
    D = mat_run.daily_arrays(daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    e = epi[(epi.underlying == U) & (epi.family == "s1_primary")
            & (epi.entry_time.astype(str).str.startswith(DAY))]
    if e.empty:
        print("no episode", U, DAY)
        return
    ep = list(e.itertuples())[0]
    B = bars.u[U]
    d = D[U]
    side, s0 = int(ep.side), int(ep.s0)
    show(f"EPISODE  {U}  side={side:+d}  entry_time={ep.entry_time}  s2={ep.s2}")
    print(f"stage-1 decision bar row -> entry bar pos {ep.bar}, session idx {s0}")
    print(f"entry spot (open of entry bar) = {B['open'][ep.bar]:.2f}   "
          f"prior-session daily ATR14 = {ep.atr_abs:.3f}")
    print(f"hard stop level = {B['open'][ep.bar] - side * STOP_ATR * ep.atr_abs:.2f}")

    dg = daily[daily.underlying == U].sort_values("sidx")
    st = daily_state(dg, "primary", side).fillna(False).to_numpy()
    cols = ["session", "sidx", "s_close", "D_sma20", "D_macd_hist", "D_adx14"]
    win = dg[(dg.sidx >= s0 - 3) & (dg.sidx <= s0 + 8)][cols].copy()
    win["state_on"] = st[(dg.sidx >= s0 - 3).to_numpy() & (dg.sidx <= s0 + 8).to_numpy()]
    show("DAILY (higher timeframe) around the episode")
    print(win.round(3).to_string(index=False))
    print(f"\nstage-1 requires the daily state OFF as of session {s0-1}: "
          f"state[{s0-1}] = {bool(st[s0-1])}")
    if ep.s2:
        print(f"stage-2 confirming daily bar = session idx {int(ep.s2_sidx)} "
              f"({dg[dg.sidx == int(ep.s2_sidx)].session.iloc[0]}), "
              f"tranche-2 entry = first 30m bar of session {int(ep.s2_sidx)+1}")

    old = P.load_opt_cached()
    new = VR.full_tape()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}
    sel = harness.build_selection(old, harness.MNY_BANDS[BAND])
    cmap = P.contract_map(sel, sess_map)
    ser_o = P.opt_series(old, {v[0] for v in cmap.values()})
    ser_n = P.opt_series(new, {v[0] for v in cmap.values()})

    show("CONTRACT SELECTION (15:15 snapshot of the PRIOR session)")
    for r in sel[(sel.underlying == U) & (sel.side == side)].itertuples():
        s = sess_map.get((U, r.sel_session))
        if s is not None and s0 - 1 <= s <= s0 + 5:
            print(f"  sel_session={r.sel_session} (sidx {s}) -> entry session {s+1}: "
                  f"{r.instrument_key}  K={r.strike}  exp={r.expiry}  "
                  f"mny={r.sel_mny:+.4f}  dte_sel={r.dte_sel}")

    for tag, ser in (("TRUNCATED tape (shipped)", ser_o), ("REPAIRED tape", ser_n)):
        show(f"SIMULATION on {tag}")
        r = P.simulate(B, d, ep, cmap, ser, "pyramid", P.PRIMARY_RULE)
        print("  result:", {k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in r.items()
                            if k in ("units", "fills", "mny", "pnl_base", "roc_base")})

    show("OPTION TAPE for the traded contracts: truncated vs repaired")
    cons = [cmap.get((U, side, s0))]
    e2 = bars.first_bar.get((U, int(ep.s2_sidx) + 1)) if ep.s2 else None
    if e2 is not None:
        cons.append(cmap.get((U, side, int(B["sidx"][e2]))))
    f1 = maturity_fire_session(d, s0, side, P.PRIMARY_RULE, P.HOLD_CAP)
    print(f"  maturity rule '{P.PRIMARY_RULE}' first fires at session {f1} "
          f"(entry session {s0}); scale-out executes at the open of session {f1+1}")
    for c in cons:
        if not c:
            continue
        k = c[0]
        so, sn = ser_o.get(k), ser_n.get(k)
        print(f"\n  {k}")
        print(f"    truncated: {len(so['time'])} bars, "
              f"{pd.Timestamp(so['time'][0])} .. {pd.Timestamp(so['time'][-1])}")
        print(f"    repaired : {len(sn['time'])} bars, "
              f"{pd.Timestamp(sn['time'][0])} .. {pd.Timestamp(sn['time'][-1])}")
        extra = sn["time"][~np.isin(sn["time"], so["time"])]
        print(f"    bars MISSING from the shipped tape: {len(extra)}")
        if len(extra):
            g = new[(new.instrument_key == k) & (new.time.isin(pd.to_datetime(extra, utc=True)))]
            g = g.sort_values("time")
            g["absmny"] = (g.strike - g.underlying_price).abs() / g.underlying_price
            print(g[["time", "strike", "underlying_price", "absmny", "open",
                     "close", "volume", "oi"]].head(12).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
