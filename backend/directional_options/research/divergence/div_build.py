"""(D) Build the divergence-setup episode table.

Inputs are ALREADY ON DISK -- nothing is pulled from PG here:
  ../cascade/data/daily.parquet   daily bars + D_* daily features
  ../cascade/data/intra.parquet   30m tape (barrier scanning + hourly element)
  ../panel_2d3d/data/spot_*.csv   30m bars WITH volume (daily volume, element b)

Output:
  ./data/episodes.parquet   one row per (arm, underlying, episode) with the
                            triple-barrier outcome, the strength measures, the
                            hourly-lead measure and the matched-control twin.

Machinery reused verbatim from ../cascade/run_cascade.py: Bars, path_stats,
control_mask, cluster_boot_diff, bh.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "cascade"))
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import div_defs as D  # noqa: E402
import run_cascade as rc  # noqa: E402

CASCADE_DATA = os.path.join(HERE, "..", "cascade", "data")
PANEL = os.path.join(HERE, "..", "panel_2d3d", "data")
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYNXT50", "BANKEX"}
MCX = {"COPPER", "NATURALGAS", "SILVERM", "ZINCMINI", "ALUMINI", "GOLD", "NICKEL", "CRUDEOIL"}
RNG = np.random.default_rng(20260721)


def mkt(u: str) -> str:
    return "index" if u in INDEX else ("cmdty" if u in MCX else "stock")


# ==========================================================================
# load
# ==========================================================================

def daily_volume() -> pd.DataFrame:
    p = os.path.join(DATA, "dvol.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    frames = []
    for f in sorted(glob.glob(os.path.join(PANEL, "spot_*.csv"))):
        frames.append(pd.read_csv(f, usecols=["time", "underlying", "volume"]))
    s = pd.concat(frames, ignore_index=True)
    s["time"] = pd.to_datetime(s["time"], utc=True)
    s["session"] = (s["time"] + pd.Timedelta(hours=5, minutes=30)).dt.date
    v = s.groupby(["underlying", "session"], observed=True)["volume"].sum().reset_index()
    v = v.rename(columns={"volume": "s_vol"})
    v.to_parquet(p, index=False)
    return v


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.read_parquet(os.path.join(CASCADE_DATA, "daily.parquet"))
    intra = pd.read_parquet(os.path.join(CASCADE_DATA, "intra.parquet"))
    v = daily_volume()
    daily = daily.merge(v, on=["underlying", "session"], how="left")
    daily["s_vol"] = daily["s_vol"].fillna(0.0)
    daily["mkt"] = daily["underlying"].map(mkt)
    daily = daily[daily["mkt"] != "cmdty"].copy()      # options universe only
    intra = intra[intra["underlying"].isin(set(daily["underlying"]))].copy()
    return daily, intra


def build_daily_elements(daily: pd.DataFrame) -> pd.DataFrame:
    p = os.path.join(DATA, "elem.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    out = []
    for u, g in daily.sort_values(["underlying", "sidx"]).groupby("underlying", sort=False):
        out.append(D.build_elements(g))
    e = pd.concat(out, ignore_index=True)
    e.to_parquet(p, index=False)
    return e


def build_hourly(intra: pd.DataFrame) -> pd.DataFrame:
    p = os.path.join(DATA, "hourly.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    out = []
    for u, g in intra.groupby("underlying", sort=False):
        h = D.hourly_from_30m(g[["time", "session", "bidx", "open", "high", "low", "close"]])
        h["underlying"] = u
        out.append(h)
    h = pd.concat(out, ignore_index=True)
    h.to_parquet(p, index=False)
    return h


# ==========================================================================
# arms -> trigger/entry session indices
# ==========================================================================

def arm_events(e: pd.DataFrame, arm: str) -> pd.DataFrame:
    """(underlying, sidx_trigger, sidx_entry) for one arm, causally.

    sidx_entry is always the session whose OPEN is taken; every predicate used
    is knowable at the close of sidx_entry - 1.
    """
    els = D.ARMS[arm]
    rows = []
    for u, g in e.groupby("underlying", sort=False):
        g = g.sort_values("sidx").reset_index(drop=True)
        n = len(g)
        base = np.ones(n, bool)
        if "cross" in els:
            base &= g["cross"].to_numpy(bool)
        else:
            # no crossover element: use the RISING EDGE of the divergence state
            dv = g["div"].to_numpy(bool)
            base &= dv & ~np.concatenate([[False], dv[:-1]])
        if "div" in els:
            base &= g["div"].to_numpy(bool)
        if "div_any" in els:
            base &= g["div_any"].to_numpy(bool)
        if "tl_recent" in els:
            base &= g["tl_recent"].to_numpy(bool)
        sidx = g["sidx"].to_numpy(int)
        if "HL" not in els:
            for i in np.where(base)[0]:
                if i + 1 < n:
                    rows.append((u, sidx[i], sidx[i + 1], sidx[i]))
            continue
        hl_conf = g["hl_conf"].to_numpy(bool)
        hl_piv = g["hl_pivot"].to_numpy(int)
        for i in np.where(base)[0]:
            hi_lim = min(n - 1, i + D.HL_WINDOW)
            j = None
            for k in range(i + 1, hi_lim + 1):
                if hl_conf[k] and hl_piv[k] > i:
                    j = k
                    break
            if j is None or j + 1 >= n:
                continue
            rows.append((u, sidx[j], sidx[j + 1], sidx[i]))
    return pd.DataFrame(rows, columns=["underlying", "sidx_trig", "sidx_entry", "sidx_cross"])


def episodes(ev: pd.DataFrame) -> pd.DataFrame:
    """One observation per (underlying, episode): entries <= EPISODE_GAP apart merge."""
    if ev.empty:
        return ev
    ev = ev.sort_values(["underlying", "sidx_entry"]).reset_index(drop=True)
    prev = ev.groupby("underlying")["sidx_entry"].shift(1)
    new = ((ev["sidx_entry"] - prev) > D.EPISODE_GAP_SESSIONS) | prev.isna()
    return ev[new.to_numpy()].copy()


# ==========================================================================
# outcomes
# ==========================================================================

def attach_outcomes(ev: pd.DataFrame, bars: rc.Bars, e: pd.DataFrame) -> pd.DataFrame:
    """Triple barrier from the OPEN of sidx_entry, plus fixed-horizon returns."""
    key = {(r.underlying, int(r.sidx)): r for r in e.itertuples()}
    atr_map = {(r.underlying, int(r.sidx)): float(r.D_atr14) for r in e.itertuples()}
    sess_map = {(r.underlying, int(r.sidx)): r.session for r in e.itertuples()}
    out = []
    for r in ev.itertuples():
        B = bars.u.get(r.underlying)
        if B is None:
            continue
        pos = bars.first_bar.get((r.underlying, int(r.sidx_entry)))
        if pos is None:
            continue
        # ATR known at the close of the session BEFORE entry
        a = atr_map.get((r.underlying, int(r.sidx_entry) - 1), np.nan)
        if not np.isfinite(a) or a <= 0:
            continue
        st = rc.path_stats(B, pos, D.SIDE, a, int(r.sidx_entry) + D.HORIZON_SESSIONS)
        if not st:
            continue
        rec = {"underlying": r.underlying, "mkt": mkt(r.underlying),
               "sidx_trig": int(r.sidx_trig), "sidx_entry": int(r.sidx_entry),
               "sidx_cross": int(r.sidx_cross),
               "session_entry": sess_map.get((r.underlying, int(r.sidx_entry))),
               "atr_abs": a, **st}
        # fixed-horizon spot returns off the same entry open
        for H in (5, 10, 15):
            p2 = bars.first_bar.get((r.underlying, int(r.sidx_entry) + H))
            rec[f"ret{H}"] = (float(B["open"][p2] / st["entry_spot"] - 1.0)
                              if p2 is not None else np.nan)
        # strength measures, read at the CROSS session (knowable, pre-entry)
        cr = key.get((r.underlying, int(r.sidx_cross)))
        for c in ("str_hist", "str_slope", "str_below0", "str_thrust", "str_volz",
                  "str_div_macd", "div_price", "div_macd", "hl_lift", "tl_slope"):
            rec[c] = float(getattr(cr, c)) if cr is not None else np.nan
        out.append(rec)
    df = pd.DataFrame(out)
    if not df.empty:
        df["quarter"] = pd.PeriodIndex(pd.to_datetime(df["session_entry"]), freq="Q").astype(str)
    return df


def control_events(e: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Matched controls run through byte-identical machinery.

    unconditional : EVERY session of the same universe is a candidate entry
                    (episode-clustered exactly like the real arms).
    random        : a hash-selected ~1-in-40 subset of the same sessions,
                    carrying no signal by construction.
    matched       : for each real entry, one session drawn uniformly from the
                    SAME underlying and the SAME calendar quarter.
    """
    g = e[["underlying", "sidx"]].copy()
    if kind == "unconditional":
        m = np.ones(len(g), bool)
    elif kind == "random":
        h = pd.util.hash_pandas_object(
            g["underlying"].astype(str) + "|" + g["sidx"].astype(str), index=False).to_numpy()
        m = (h % 40) == 7
    else:
        raise ValueError(kind)
    g = g[m].copy()
    g["sidx_trig"] = g["sidx"]
    g["sidx_entry"] = g["sidx"] + 1
    g["sidx_cross"] = g["sidx"]
    return g[["underlying", "sidx_trig", "sidx_entry", "sidx_cross"]]


def matched_control(real: pd.DataFrame, e: pd.DataFrame, reps: int = 20) -> pd.DataFrame:
    """`reps` control entries per real entry: same underlying, same quarter,
    random session. Replicated so the control mean is not itself a small-sample
    lottery; every replicate runs through byte-identical machinery."""
    q = e[["underlying", "sidx", "session"]].copy()
    q["quarter"] = pd.PeriodIndex(pd.to_datetime(q["session"]), freq="Q").astype(str)
    pool = {(u, qq): grp["sidx"].to_numpy(int)
            for (u, qq), grp in q.groupby(["underlying", "quarter"], observed=True)}
    rows = []
    for _ in range(reps):
        for r in real.itertuples():
            cand = pool.get((r.underlying, r.quarter))
            if cand is None or len(cand) == 0:
                continue
            s = int(RNG.choice(cand))
            rows.append((r.underlying, s, s + 1, s))
    return pd.DataFrame(rows, columns=["underlying", "sidx_trig", "sidx_entry", "sidx_cross"])


# ==========================================================================
def main() -> None:
    daily, intra = load_panel()
    e = build_daily_elements(daily)
    print("daily element panel", e.shape, flush=True)
    bars = rc.Bars(intra, daily)
    print("bars built", len(bars.u), flush=True)

    frames = []
    for arm in D.ARMS:
        ev = episodes(arm_events(e, arm))
        o = attach_outcomes(ev, bars, e)
        if o.empty:
            print(f"{arm:14s} n=0", flush=True)
            continue
        o["arm"] = arm
        frames.append(o)
        print(f"{arm:14s} events={len(ev):6d} outcomes={len(o):6d} "
              f"P(large)={o['large'].mean():.3f}", flush=True)

    for kind in ("unconditional", "random"):
        raw = control_events(e, kind)
        # the unconditional arm is a BASE RATE over every session, so episode
        # clustering (which exists to stop one signal being counted twice) is
        # not applied to it; the random arm is a trading arm and is clustered.
        ev = raw if kind == "unconditional" else episodes(raw)
        o = attach_outcomes(ev, bars, e)
        o["arm"] = "ctrl_" + kind
        frames.append(o)
        print(f"ctrl_{kind:9s} outcomes={len(o):6d} P(large)={o['large'].mean():.3f}", flush=True)

    ep = pd.concat(frames, ignore_index=True)

    # matched controls for the two headline arms
    for arm in (D.PRIMARY_ARM, D.FULL_ARM):
        real = ep[ep["arm"] == arm]
        if real.empty:
            continue
        o = attach_outcomes(matched_control(real, e), bars, e)
        o["arm"] = "ctrl_matched_" + arm
        ep = pd.concat([ep, o], ignore_index=True)
        print(f"ctrl_matched_{arm:12s} outcomes={len(o):6d} P(large)={o['large'].mean():.3f}",
              flush=True)

    ep.to_parquet(os.path.join(DATA, "episodes.parquet"), index=False)
    print("wrote episodes.parquet", len(ep))


if __name__ == "__main__":
    main()
