"""(C-cascade, study 3) SATURATION / MATURITY — build episodes and measure how
much of a move each causal maturity signal captures, versus fixed-time exits,
versus holding to consolidation, versus the ex-post oracle.

Reuses, unchanged:
  ../setups_2d3d/harness.py   (spot loading, session grid, contamination guard)
  ../setups_2d3d/features.py  (causal indicator filters)
  ./stages.py                 (a-priori stage-1 / stage-2 definitions)
  ./run_cascade.py            (episode clustering, control masks, Bars)

Writes  data/mat_episodes.parquet  (one row per (family, episode) with an exit
column per maturity rule and per baseline) and mat_results.txt.

No PG queries are issued: everything comes from the cached parquet the cascade
pass already built.
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

import mat_defs  # noqa: E402
import run_cascade as rc  # noqa: E402
from mat_defs import (  # noqa: E402
    BASELINES, HORIZON_SESSIONS, RULES, STOP_ATR, maturity_fire_session,
)
from stages import EPISODE_GAP_SESSIONS, daily_state, stage1_mask, stage2_events  # noqa: E402

DATA = os.path.join(HERE, "data")
S1_VARIANT = "primary"
S2_VARIANT = "primary"
S2_WINDOW = 3
INDEX_NAMES = rc.INDEX_NAMES


# =========================================================================
# per-underlying daily arrays (session index == array position)
# =========================================================================

def daily_arrays(daily: pd.DataFrame) -> dict[str, dict]:
    out = {}
    for u, g in daily.groupby("underlying", sort=False):
        g = g.sort_values("sidx")
        assert (g["sidx"].to_numpy() == np.arange(len(g))).all(), u
        out[u] = {
            "n": len(g),
            "adx": g["D_adx14"].to_numpy(float),
            "atr": g["D_atr14"].to_numpy(float),
            "hist": g["D_macd_hist"].to_numpy(float),
            "close": g["s_close"].to_numpy(float),
            "sma20": g["D_sma20"].to_numpy(float),
            "high": g["s_high"].to_numpy(float),
            "low": g["s_low"].to_numpy(float),
            "state_long": daily_state(g, S2_VARIANT, 1).fillna(False).to_numpy(),
            "state_short": daily_state(g, S2_VARIANT, -1).fillna(False).to_numpy(),
        }
    return out


# =========================================================================
# spot path with a hard stop, exit at an arbitrary later bar's OPEN
# =========================================================================

def spot_path(B: dict, e: int, side: int, atr_abs: float, horizon: int):
    """Return (end_pos, stop_pos, mfe_atr, mfe_pos) for the window
    [e, last bar with sidx <= sidx[e]+horizon]. stop_pos = -1 if never hit."""
    sid = B["sidx"]
    s0 = int(sid[e])
    end = int(np.searchsorted(sid, s0 + horizon, side="right"))
    if end <= e:
        return None
    entry = B["open"][e]
    if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr_abs) or atr_abs <= 0:
        return None
    hi, lo = B["high"][e:end], B["low"][e:end]
    if side > 0:
        fav = hi - entry
        s_hit = lo <= entry - STOP_ATR * atr_abs
    else:
        fav = entry - lo
        s_hit = hi >= entry + STOP_ATR * atr_abs
    sp = e + int(np.argmax(s_hit)) if s_hit.any() else -1
    # MFE measured only up to the stop (a stopped trade cannot enjoy later travel)
    lim = (sp - e + 1) if sp >= 0 else len(fav)
    mfe = float(np.nanmax(fav[:lim])) / atr_abs if lim > 0 else np.nan
    mpos = e + int(np.nanargmax(fav[:lim])) if lim > 0 else e
    return {"end": end, "stop_pos": sp, "mfe_atr": mfe, "mfe_pos": mpos,
            "entry": float(entry), "truncated": int(sid[end - 1] < s0 + horizon)}


def exit_at_session_open(bars: rc.Bars, u: str, sidx_target: int):
    """Position of the first 30m bar of session `sidx_target` (execution point
    for a daily signal fired at the close of session sidx_target-1)."""
    return bars.first_bar.get((u, sidx_target))


def realise(B: dict, e: int, side: int, atr_abs: float, exit_pos: int,
            stop_pos: int, at_open: bool = True) -> float:
    """Signed return in ATR units, with the hard stop taking precedence."""
    entry = B["open"][e]
    if stop_pos >= 0 and (exit_pos < 0 or stop_pos <= exit_pos):
        return -STOP_ATR
    if exit_pos < 0:
        return np.nan
    px = B["open"][exit_pos] if at_open else B["close"][exit_pos]
    return float(side * (px - entry) / atr_abs)


# =========================================================================
# episode construction (families + controls, identical machinery)
# =========================================================================

def build_episodes(intra: pd.DataFrame, daily: pd.DataFrame, bars: rc.Bars) -> pd.DataFrame:
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}
    sess_idx = pd.Series([sess_map.get((u, s), -1) for u, s in
                          zip(intra["underlying"], intra["session"])], index=intra.index)
    x = intra.assign(_sidx=sess_idx)
    x = x[(x["_sidx"] >= 0) & (x["mins"] <= rc.DECISION_HI) & x["pd_atr14"].notna()
          & (x["pd_atr14"] > 0)]

    already, ev = {}, {}
    for side in (1, -1):
        st = []
        for u, g in daily.groupby("underlying", sort=False):
            g = g.sort_values("sidx")
            v = daily_state(g, S2_VARIANT, side).fillna(False).to_numpy()
            st.append(pd.DataFrame({"underlying": u, "sidx": g["sidx"].to_numpy(),
                                    "prev": np.concatenate([[False], v[:-1]])}))
        st = pd.concat(st, ignore_index=True)
        already[side] = {(r.underlying, r.sidx): bool(r.prev) for r in st.itertuples()}
        e2 = stage2_events(daily, S2_VARIANT, side)
        d = defaultdict(list)
        for r in e2.itertuples():
            d[r.underlying].append(int(r.sidx))
        ev[side] = {u: np.sort(np.asarray(v)) for u, v in d.items()}

    fams = []
    for side in (1, -1):
        fams.append((f"s1_{S1_VARIANT}", side, stage1_mask(x, S1_VARIANT, side)))
        fams.append(("ctrl_random", side,
                     rc.control_mask(x, "rand_long" if side > 0 else "rand_short")))
    fams.append(("ctrl_long", 1, rc.control_mask(x, "long")))
    fams.append(("ctrl_short", -1, rc.control_mask(x, "short")))

    rows = []
    for fam, side, mask in fams:
        ep = rc.episodes_from_mask(x, mask, x["_sidx"], side)
        if ep.empty:
            continue
        keep = np.array([not already[side].get((u, s), False)
                         for u, s in zip(ep["underlying"], ep["sidx"])])
        ep = ep[keep]
        for r in ep.itertuples():
            u = r.underlying
            B = bars.u.get(u)
            if B is None:
                continue
            p = B["pos"].get(int(r.Index))
            if p is None or p + 1 >= len(B["sidx"]):
                continue
            e = p + 1
            if B["sidx"][e] != B["sidx"][p]:
                continue
            s0 = int(B["sidx"][e])
            arr = ev[side].get(u)
            s2_sidx = -1
            if arr is not None and len(arr):
                j = int(np.searchsorted(arr, s0))
                if j < len(arr) and arr[j] <= s0 + S2_WINDOW:
                    s2_sidx = int(arr[j])
            rows.append({"family": fam, "side": side, "underlying": u,
                         "mkt": "index" if u in INDEX_NAMES else "stock",
                         "bar": e, "s0": s0, "atr_abs": float(r.pd_atr14),
                         "entry_time": B["time"][e],
                         "quarter": str(pd.Period(pd.Timestamp(B["time"][e]).tz_localize(None),
                                                  freq="Q")),
                         "s2": int(s2_sidx >= 0), "s2_sidx": s2_sidx})
    return pd.DataFrame(rows)


# =========================================================================
# maturity measurement
# =========================================================================

def measure(epi: pd.DataFrame, bars: rc.Bars, D: dict[str, dict]) -> pd.DataFrame:
    out = []
    for r in epi.itertuples():
        B = bars.u[r.underlying]
        sp = spot_path(B, r.bar, r.side, r.atr_abs, HORIZON_SESSIONS)
        if sp is None or sp["truncated"]:
            continue
        d = D.get(r.underlying)
        if d is None:
            continue
        rec = {"family": r.family, "side": r.side, "underlying": r.underlying,
               "mkt": r.mkt, "quarter": r.quarter, "entry_time": r.entry_time,
               "s2": r.s2, "mfe_atr": sp["mfe_atr"],
               "stopped": int(sp["stop_pos"] >= 0),
               "mfe_sess": int(B["sidx"][sp["mfe_pos"]]) - r.s0}
        # baselines
        for k, h in (("fix_3", 3), ("fix_5", 5), ("fix_10", 10)):
            pos = exit_at_session_open(bars, r.underlying, r.s0 + h)
            rec["x_" + k] = realise(B, r.bar, r.side, r.atr_abs, pos if pos else -1,
                                    sp["stop_pos"])
        rec["x_oracle_mfe"] = sp["mfe_atr"]
        rec["x_hold_full"] = realise(B, r.bar, r.side, r.atr_abs,
                                     sp["end"] - 1, sp["stop_pos"], at_open=False)
        # maturity rules
        for rule in RULES:
            fs = maturity_fire_session(d, r.s0, r.side, rule, HORIZON_SESSIONS)
            if fs < 0:
                pos, lag = sp["end"] - 1, HORIZON_SESSIONS
                rec["x_" + rule] = realise(B, r.bar, r.side, r.atr_abs, pos,
                                           sp["stop_pos"], at_open=False)
            else:
                pos = exit_at_session_open(bars, r.underlying, fs + 1)
                lag = fs + 1 - r.s0
                rec["x_" + rule] = realise(B, r.bar, r.side, r.atr_abs,
                                           pos if pos else -1, sp["stop_pos"])
            rec["lag_" + rule] = lag
            rec["fired_" + rule] = int(fs >= 0)
        out.append(rec)
    return pd.DataFrame(out)


# =========================================================================

def cap(df: pd.DataFrame, col: str) -> tuple[float, float, float]:
    """Aggregate capture = sum(exit)/sum(MFE); plus mean and median exit."""
    m = df["mfe_atr"]
    ok = m.notna() & df[col].notna()
    agg = df.loc[ok, col].sum() / m[ok].sum() if m[ok].sum() > 0 else np.nan
    return float(agg), float(df.loc[ok, col].mean()), float(df.loc[ok, col].median())


def main() -> None:
    print("loading ...", flush=True)
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    D = daily_arrays(daily)
    epi = build_episodes(intra, daily, bars)
    print("episodes", len(epi), epi.groupby("family").size().to_dict(), flush=True)
    res = measure(epi, bars, D)
    res.to_parquet(os.path.join(DATA, "mat_episodes.parquet"))
    print("measured", len(res), flush=True)

    L = []
    P = L.append
    P("=" * 78)
    P("(3) SATURATION / MATURITY — how much of a move is captured, causally")
    P("=" * 78)
    P(f"horizon {HORIZON_SESSIONS} sessions, hard stop -{STOP_ATR} ATR, "
      f"daily signal fired at close of s -> executed at OPEN of s+1")
    P(f"episodes measured: {len(res)}  families: {sorted(res['family'].unique())}")
    P("")

    cols = ["x_" + r for r in RULES] + ["x_" + b for b in BASELINES] + ["x_hold_full"]
    for scope, sub in (("ALL stage-1 episodes", res[res["family"] == f"s1_{S1_VARIANT}"]),
                       ("stage-1 that reached +2 ATR MFE (EX-POST subset)",
                        res[(res["family"] == f"s1_{S1_VARIANT}") & (res["mfe_atr"] >= 2.0)]),
                       ("matched control_long", res[res["family"] == "ctrl_long"]),
                       ("matched control_random", res[res["family"] == "ctrl_random"])):
        P("-" * 78)
        P(f"{scope}   n={len(sub)}   mean MFE {sub['mfe_atr'].mean():.3f} ATR  "
          f"median {sub['mfe_atr'].median():.3f}  stopped {sub['stopped'].mean():.1%}")
        P(f"{'exit rule':<18}{'capture':>9}{'mean ATR':>10}{'med ATR':>9}"
          f"{'fired%':>8}{'med lag':>9}")
        for c in cols:
            a, mu, md = cap(sub, c)
            rule = c[2:]
            fired = f"{sub['fired_' + rule].mean():.0%}" if ("fired_" + rule) in sub else "-"
            lag = f"{sub['lag_' + rule].median():.0f}" if ("lag_" + rule) in sub else "-"
            P(f"{rule:<18}{a:>9.3f}{mu:>10.3f}{md:>9.3f}{fired:>8}{lag:>9}")
        P("")

    P("-" * 78)
    P("BY MARKET (stage-1 episodes)")
    s1 = res[res["family"] == f"s1_{S1_VARIANT}"]
    for mk, sub in s1.groupby("mkt"):
        P(f"  {mk}  n={len(sub)}  meanMFE {sub['mfe_atr'].mean():.3f}")
        for c in cols:
            a, mu, md = cap(sub, c)
            P(f"    {c[2:]:<18}capture {a:>7.3f}  mean {mu:>7.3f}")
    P("")

    P("-" * 78)
    P("IS MATURITY DETECTABLE IN TIME TO ACT?  (stage-1 episodes that reached")
    P("+2 ATR MFE — the ex-post 'a move happened' population)")
    mv = res[(res["family"] == f"s1_{S1_VARIANT}") & (res["mfe_atr"] >= 2.0)]
    P(f"  n={len(mv)}   median session of the MFE peak = {mv['mfe_sess'].median():.0f}"
      f"  (mean {mv['mfe_sess'].mean():.1f})")
    P(f"  {'rule':<16}{'fired%':>8}{'medFireSess':>13}{'late vs peak':>14}"
      f"{'%late':>8}")
    for rule in RULES:
        f = mv["fired_" + rule] == 1
        if f.sum() < 10:
            continue
        late = mv.loc[f, "lag_" + rule] - mv.loc[f, "mfe_sess"]
        P(f"  {rule:<16}{f.mean():>8.0%}{mv.loc[f, 'lag_' + rule].median():>13.0f}"
          f"{late.median():>14.1f}{(late > 0).mean():>8.0%}")
    P("")
    P("PAIRED comparison against the two benchmarks that need beating, "
      "episode-clustered")
    P("(cluster bootstrap by underlying, 2000 draws; BH across the whole block)")
    pv, lab = [], []
    for scope_name, sub in (("all_s1", res[res["family"] == f"s1_{S1_VARIANT}"]),
                            ("movesubset", mv)):
        for base in ("x_hold_full", "x_fix_10", "x_fix_3"):
            for rule in RULES:
                col = "x_" + rule
                d = sub[[col, base, "underlying"]].dropna()
                if len(d) < 30:
                    continue
                d = d.assign(_d=d[col] - d[base])
                st = rc.cluster_boot_diff(
                    pd.concat([d.assign(_v=d["_d"]), d.assign(_v=0.0)], ignore_index=True),
                    "_v",
                    np.r_[np.ones(len(d), bool), np.zeros(len(d), bool)],
                    np.r_[np.zeros(len(d), bool), np.ones(len(d), bool)])
                lab.append((scope_name, rule, base, st))
                pv.append(st["p"])
    qs = rc.bh(pv)
    P(f"  {'scope':<12}{'rule':<16}{'vs':<14}{'diffATR':>9}{'95% CI':>20}"
      f"{'p':>8}{'q':>8}")
    for (sc, rule, base, st), q in zip(lab, qs):
        P(f"  {sc:<12}{rule:<16}{base:<14}{st['diff']:>9.3f}"
          f"  [{st['lo']:>7.3f},{st['hi']:>7.3f}]{st['p']:>8.4f}{q:>8.4f}")
    P(f"  K = {len(pv)} paired comparisons; Bonferroni alpha = {0.05/max(len(pv),1):.5f}")
    P("")

    P("-" * 78)
    P("SENSITIVITY GRID (reported in full, NOT selected) — stage-1 episodes")
    P(f"  {'rule':<16}{'param':<16}{'value':>7}{'capture':>9}{'meanATR':>9}{'fired%':>8}")
    ep1 = epi[epi["family"] == f"s1_{S1_VARIANT}"]
    for rule, (pname, vals) in mat_defs.GRID.items():
        for v in vals:
            vals_out, mfes, fired = [], [], []
            for r in ep1.itertuples():
                B = bars.u[r.underlying]
                sp = spot_path(B, r.bar, r.side, r.atr_abs, HORIZON_SESSIONS)
                if sp is None or sp["truncated"]:
                    continue
                d = D.get(r.underlying)
                if d is None:
                    continue
                fs = maturity_fire_session(d, r.s0, r.side, rule, HORIZON_SESSIONS,
                                           **{pname: v})
                if fs < 0:
                    x = realise(B, r.bar, r.side, r.atr_abs, sp["end"] - 1,
                                sp["stop_pos"], at_open=False)
                else:
                    pos = exit_at_session_open(bars, r.underlying, fs + 1)
                    x = realise(B, r.bar, r.side, r.atr_abs, pos if pos else -1,
                                sp["stop_pos"])
                if np.isfinite(x):
                    vals_out.append(x)
                    mfes.append(sp["mfe_atr"])
                    fired.append(int(fs >= 0))
            if not vals_out:
                continue
            a = float(np.sum(vals_out) / np.sum(mfes)) if np.sum(mfes) > 0 else np.nan
            P(f"  {rule:<16}{pname:<16}{v:>7}{a:>9.3f}{np.mean(vals_out):>9.3f}"
              f"{np.mean(fired):>8.0%}")
    P("")
    P("27 grid cells + 10 headline exit rules reported; none used for selection.")
    P("")
    txt = "\n".join(L)
    with open(os.path.join(HERE, "mat_results.txt"), "w") as fh:
        fh.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
