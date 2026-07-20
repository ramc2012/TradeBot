"""(C-cascade) THE CASCADE TEST — does sequential two-timeframe confirmation
carry information the single-shot study never measured?

What is measured (all on SPOT, which is where the directional information
either exists or does not; option economics is a separate pass):

  1. P(stage-2 confirm | stage-1 confirm) within the a-priori 3-session window,
     and the LAG distribution, versus the same probability for matched
     control bars drawn from the identical universe.
  2. P(sustained large move | stage-1 THEN stage-2) versus
     P(... | stage-1, unconditional on stage-2)  ["stage-1 alone"]
     versus the unconditional base rate at matched control bars.
  3. Outcomes of the FAILED FIRST TRANCHE: stage-1 episodes whose higher
     timeframe never confirms, exited at the end of the window.
  4. Lift over matched controls, episode-clustered, cluster-bootstrapped,
     with Bonferroni/BH applied over every comparison made.

Honesty machinery
  * ONE observation per (underlying, episode). Consecutive stage-1 fires of
    the same side within EPISODE_GAP_SESSIONS collapse to their FIRST bar.
  * Controls run through the IDENTICAL machinery (same universe, same
    episode clustering, same barriers, same window).
  * Cluster bootstrap by underlying (instruments are the independent unit).
  * Every comparison is counted; Bonferroni and Benjamini-Hochberg reported
    next to the raw p.
  * Definitions fixed a priori in stages.py; the 3x3 alternate grid is
    reported in full, not mined.
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
import stages  # noqa: E402
from stages import (  # noqa: E402
    EPISODE_GAP_SESSIONS, LARGE_HORIZON_SESSIONS, LARGE_STP_ATR, LARGE_TGT_ATR,
    S1_VARIANTS, S2_VARIANTS, S2_WINDOW_SESSIONS,
    add_daily_stage_features, daily_state, stage1_mask, stage2_events,
)

DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
RNG = np.random.default_rng(20260721)
NBOOT = 2000
DECISION_HI = harness.DECISION_HI          # last 30m decision bar = 14:45 IST
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
               "SENSEX", "BANKEX"}


# =========================================================================
# load
# =========================================================================

def load():
    ip = os.path.join(DATA, "intra.parquet")
    dp = os.path.join(DATA, "daily.parquet")
    if os.path.exists(ip) and os.path.exists(dp):
        return pd.read_parquet(ip), pd.read_parquet(dp)
    intra, daily = harness.load_spot()
    keep = ["time", "underlying", "session", "mins", "bidx", "open", "high",
            "low", "close", "pd_atr14", "pd_atr_pct"]
    keep += [c for c in intra.columns if c.startswith("m_")]
    intra = intra[keep].copy()
    daily = pd.concat(
        [add_daily_stage_features(g.reset_index(drop=True))
         for _, g in daily.sort_values(["underlying", "sidx"]).groupby("underlying", sort=False)],
        ignore_index=True)
    intra.to_parquet(ip)
    daily.to_parquet(dp)
    return intra, daily


class Bars:
    """Per-underlying 30m arrays + session index, for barrier scanning."""

    def __init__(self, intra: pd.DataFrame, daily: pd.DataFrame):
        sidx_map = {(r.underlying, r.session): int(r.sidx)
                    for r in daily[["underlying", "session", "sidx"]].itertuples()}
        self.u: dict[str, dict] = {}
        for u, g in intra.groupby("underlying", sort=False):
            g = g.sort_values("time")
            sid = np.array([sidx_map.get((u, s), -1) for s in g["session"]], dtype=np.int64)
            ok = sid >= 0
            g = g[ok]
            sid = sid[ok]
            self.u[u] = {
                "atr": g["pd_atr14"].to_numpy(float),
                "high": g["high"].to_numpy(float),
                "low": g["low"].to_numpy(float),
                "close": g["close"].to_numpy(float),
                "open": g["open"].to_numpy(float),
                "sidx": sid,
                "mins": g["mins"].to_numpy(int),
                "time": g["time"].to_numpy(),
                "row": g.index.to_numpy(),
            }
            # row-label -> positional index
            self.u[u]["pos"] = {int(r): i for i, r in enumerate(g.index.to_numpy())}
        # first bar position of each session index (for stage-2 tranche entry)
        self.first_bar: dict[tuple[str, int], int] = {}
        for u, d in self.u.items():
            sid = d["sidx"]
            chg = np.concatenate([[True], sid[1:] != sid[:-1]])
            for p in np.where(chg)[0]:
                self.first_bar[(u, int(sid[p]))] = int(p)


def path_stats(B: dict, e: int, side: int, atr_abs: float, limit_sidx: int) -> dict:
    """Triple barrier + path shape from entry bar position e (inclusive).

    Returns first-touch outcome, MFE/MAE in ATR units and terminal return in
    ATR units, all measured over bars e .. last bar with sidx <= limit_sidx.
    """
    sid = B["sidx"]
    end = int(np.searchsorted(sid, limit_sidx, side="right"))
    if end <= e:
        return {}
    entry = B["open"][e]
    if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr_abs) or atr_abs <= 0:
        return {}
    hi = B["high"][e:end]
    lo = B["low"][e:end]
    cl = B["close"][e:end]
    if side > 0:
        fav, adv = hi - entry, entry - lo
        t_hit = hi >= entry + LARGE_TGT_ATR * atr_abs
        s_hit = lo <= entry - LARGE_STP_ATR * atr_abs
    else:
        fav, adv = entry - lo, hi - entry
        t_hit = lo <= entry - LARGE_TGT_ATR * atr_abs
        s_hit = hi >= entry + LARGE_STP_ATR * atr_abs
    ti = int(np.argmax(t_hit)) if t_hit.any() else 10**9
    si = int(np.argmax(s_hit)) if s_hit.any() else 10**9
    if ti == si == 10**9:
        hit, k = "time", len(cl) - 1
    elif si <= ti:                     # conservative tie-break -> stop
        hit, k = "stop", si
    else:
        hit, k = "target", ti
    return {
        "hit": hit,
        "large": int(hit == "target"),
        "bars": k,
        "mfe_atr": float(fav.max() / atr_abs),
        "mae_atr": float(adv.max() / atr_abs),
        "term_atr": float(side * (cl[-1] - entry) / atr_abs),
        "term_ret": float(side * (cl[-1] / entry - 1.0)),
        "entry_spot": float(entry),
        "truncated": int(sid[end - 1] < limit_sidx),
    }


def cutoff_stats(B: dict, e: int, side: int, atr_abs: float, limit_sidx: int) -> dict:
    """Managed outcome of a tranche abandoned at the end of `limit_sidx`:
    stop out at -1 ATR if touched first, otherwise mark out at that close."""
    sid = B["sidx"]
    end = int(np.searchsorted(sid, limit_sidx, side="right"))
    if end <= e:
        return {}
    entry = B["open"][e]
    hi, lo, cl = B["high"][e:end], B["low"][e:end], B["close"][e:end]
    if side > 0:
        s_hit = lo <= entry - LARGE_STP_ATR * atr_abs
        t_hit = hi >= entry + LARGE_TGT_ATR * atr_abs
    else:
        s_hit = hi >= entry + LARGE_STP_ATR * atr_abs
        t_hit = lo <= entry - LARGE_TGT_ATR * atr_abs
    si = int(np.argmax(s_hit)) if s_hit.any() else 10**9
    ti = int(np.argmax(t_hit)) if t_hit.any() else 10**9
    if si <= ti and si < 10**9:
        return {"exit_atr": -LARGE_STP_ATR, "exit_ret": float(-LARGE_STP_ATR * atr_abs / entry),
                "how": "stop"}
    if ti < si:
        return {"exit_atr": LARGE_TGT_ATR, "exit_ret": float(LARGE_TGT_ATR * atr_abs / entry),
                "how": "target"}
    return {"exit_atr": float(side * (cl[-1] - entry) / atr_abs),
            "exit_ret": float(side * (cl[-1] / entry - 1.0)), "how": "cutoff"}


# =========================================================================
# episodes
# =========================================================================

def episodes_from_mask(x: pd.DataFrame, mask: pd.Series, sess_idx: pd.Series,
                       side: int) -> pd.DataFrame:
    """Collapse consecutive same-side triggers into episodes (first bar wins)."""
    sel = x.loc[mask.fillna(False).to_numpy()]
    if sel.empty:
        return pd.DataFrame()
    sel = sel.assign(sidx=sess_idx.loc[sel.index].to_numpy())
    sel = sel.sort_values(["underlying", "bidx"])
    g = sel.groupby("underlying")["sidx"]
    newep = (sel["sidx"] - g.shift(1)) > EPISODE_GAP_SESSIONS
    newep = newep.fillna(True)
    first = sel[newep.to_numpy()].copy()
    first["side"] = side
    return first


def control_mask(x: pd.DataFrame, kind: str) -> pd.Series:
    h = pd.util.hash_pandas_object(
        x["underlying"].astype(str) + "|" + x["time"].astype(str), index=False).to_numpy()
    if kind == "long":
        return pd.Series(h % 40 == 7, index=x.index)
    if kind == "short":
        return pd.Series(h % 40 == 11, index=x.index)
    if kind == "rand_long":
        return pd.Series((h % 200 == 0) & (h % 400 == 0), index=x.index)
    if kind == "rand_short":
        return pd.Series((h % 200 == 0) & (h % 400 != 0), index=x.index)
    raise ValueError(kind)


# =========================================================================
# statistics
# =========================================================================

def cluster_boot_diff(df: pd.DataFrame, col: str, grp_a: np.ndarray, grp_b: np.ndarray,
                      nboot: int = NBOOT) -> dict:
    """Cluster (by underlying) bootstrap of mean(col|A) - mean(col|B)."""
    a = df[grp_a]
    b = df[grp_b]
    if len(a) < 10 or len(b) < 10:
        return {"n_a": len(a), "n_b": len(b), "mean_a": np.nan, "mean_b": np.nan,
                "diff": np.nan, "lo": np.nan, "hi": np.nan, "p": np.nan}
    obs = a[col].mean() - b[col].mean()
    unds = df["underlying"].unique()
    ai = defaultdict(list)
    bi = defaultdict(list)
    for u, v in zip(a["underlying"].to_numpy(), a[col].to_numpy()):
        ai[u].append(v)
    for u, v in zip(b["underlying"].to_numpy(), b[col].to_numpy()):
        bi[u].append(v)
    ai = {u: np.asarray(v, float) for u, v in ai.items()}
    bi = {u: np.asarray(v, float) for u, v in bi.items()}
    n = len(unds)
    out = np.empty(nboot)
    for r in range(nboot):
        pick = unds[RNG.integers(0, n, n)]
        sa = [ai[u] for u in pick if u in ai]
        sb = [bi[u] for u in pick if u in bi]
        if not sa or not sb:
            out[r] = np.nan
            continue
        out[r] = np.concatenate(sa).mean() - np.concatenate(sb).mean()
    out = out[np.isfinite(out)]
    lo, hi = np.percentile(out, [2.5, 97.5])
    # two-sided bootstrap p: how often the recentred distribution is as extreme
    cen = out - out.mean()
    p = float((np.abs(cen) >= abs(obs)).mean())
    p = max(p, 1.0 / max(len(out), 1))
    return {"n_a": len(a), "n_b": len(b), "mean_a": float(a[col].mean()),
            "mean_b": float(b[col].mean()), "diff": float(obs),
            "lo": float(lo), "hi": float(hi), "p": p}


def bh(pvals: list[float]) -> list[float]:
    p = np.asarray(pvals, float)
    ok = np.isfinite(p)
    q = np.full_like(p, np.nan)
    idx = np.where(ok)[0]
    m = len(idx)
    if m == 0:
        return list(q)
    order = idx[np.argsort(p[idx])]
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = p[i] * m / (rank + 1)
        prev = min(prev, val)
        q[i] = prev
    return list(q)


# =========================================================================
# the cascade
# =========================================================================

def build(intra: pd.DataFrame, daily: pd.DataFrame, bars: Bars,
          s2_variant: str) -> pd.DataFrame:
    """One row per stage-1 episode (real families and controls alike)."""
    sess_idx_map = {(r.underlying, r.session): int(r.sidx)
                    for r in daily[["underlying", "session", "sidx"]].itertuples()}
    sess_idx = pd.Series(
        [sess_idx_map.get((u, s), -1) for u, s in zip(intra["underlying"], intra["session"])],
        index=intra.index)

    # daily stage-2 state as of the LAST CLOSED daily bar (session s-1),
    # mapped onto every 30m bar of session s
    already = {}
    ev = {}
    for side in (1, -1):
        st = []
        for u, g in daily.groupby("underlying", sort=False):
            g = g.sort_values("sidx")
            v = daily_state(g, s2_variant, side).fillna(False)
            st.append(pd.DataFrame({"underlying": u, "sidx": g["sidx"].to_numpy(),
                                    "state_prev": np.concatenate([[False], v.to_numpy()[:-1]])}))
        st = pd.concat(st, ignore_index=True)
        already[side] = {(r.underlying, r.sidx): bool(r.state_prev) for r in st.itertuples()}
        e = stage2_events(daily, s2_variant, side)
        d = defaultdict(list)
        for r in e.itertuples():
            d[r.underlying].append(int(r.sidx))
        ev[side] = {u: np.sort(np.asarray(v)) for u, v in d.items()}

    x = intra.assign(_sidx=sess_idx)
    x = x[(x["_sidx"] >= 0) & (x["mins"] <= DECISION_HI) & x["pd_atr14"].notna()
          & (x["pd_atr14"] > 0)]

    fams = []
    for side in (1, -1):
        for v in S1_VARIANTS:
            fams.append((f"s1_{v}", side, stage1_mask(x, v, side)))
        fams.append(("ctrl_long", 1, control_mask(x, "long")) if side > 0
                    else ("ctrl_short", -1, control_mask(x, "short")))
        fams.append(("ctrl_random", side,
                     control_mask(x, "rand_long" if side > 0 else "rand_short")))

    rows = []
    memo: dict[tuple, dict] = {}
    for fam, side, mask in fams:
        ep = episodes_from_mask(x, mask, x["_sidx"], side)
        if ep.empty:
            continue
        # NOT-YET-CONFIRMED gate: the owner's sequence requires the higher
        # timeframe to be unconfirmed when stage-1 fires. Applied to controls
        # too, so the comparison universe is identical.
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
            if B["sidx"][e] != B["sidx"][p]:          # entry must stay in-session
                continue
            atr_abs = float(r.pd_atr14)
            s0 = int(B["sidx"][e])
            key = (u, e, side, round(atr_abs, 6), s0 + LARGE_HORIZON_SESSIONS)
            ps = memo.get(key)
            if ps is None:
                ps = path_stats(B, e, side, atr_abs, s0 + LARGE_HORIZON_SESSIONS)
                memo[key] = ps
            if not ps or ps["truncated"]:
                continue
            cs = cutoff_stats(B, e, side, atr_abs, s0 + S2_WINDOW_SESSIONS)
            # stage-2 search: first fresh daily confirm in [s0, s0+W]
            arr = ev[side].get(u)
            s2_sidx = -1
            if arr is not None and len(arr):
                j = int(np.searchsorted(arr, s0))
                if j < len(arr) and arr[j] <= s0 + S2_WINDOW_SESSIONS:
                    s2_sidx = int(arr[j])
            row = {
                "family": fam, "side": side, "underlying": u,
                "mkt": "index" if u in INDEX_NAMES else "stock",
                "s1_sidx": s0, "entry_time": B["time"][e],
                "quarter": str(pd.Period(pd.Timestamp(B["time"][e]).tz_localize(None), freq="Q")),
                "atr_pct": atr_abs / ps["entry_spot"],
                "large": ps["large"], "hit": ps["hit"], "mfe_atr": ps["mfe_atr"],
                "mae_atr": ps["mae_atr"], "term_atr": ps["term_atr"],
                "t1_exit_atr": cs.get("exit_atr", np.nan),
                "t1_exit_how": cs.get("how", ""),
                "s2": int(s2_sidx >= 0), "s2_lag": (s2_sidx - s0) if s2_sidx >= 0 else -1,
            }
            # tranche-2 anchor: first bar of the session AFTER the daily confirm
            if s2_sidx >= 0:
                fb = bars.first_bar.get((u, s2_sidx + 1))
                if fb is not None:
                    a2 = float(B["atr"][fb])
                    if np.isfinite(a2) and a2 > 0:
                        p2 = path_stats(B, fb, side, a2,
                                        int(B["sidx"][fb]) + LARGE_HORIZON_SESSIONS)
                        if p2 and not p2["truncated"]:
                            row["t2_large"] = p2["large"]
                            row["t2_mfe_atr"] = p2["mfe_atr"]
                            row["t2_term_atr"] = p2["term_atr"]
                            row["t2_bar"] = fb
            rows.append(row)
    df = pd.DataFrame(rows)
    df["s2_variant"] = s2_variant
    df["s1_variant"] = df["family"].str.replace("^s1_", "", regex=True)
    return df


def s2_only_arm(intra, daily, bars, s1_variant, s2_variant, epi: pd.DataFrame) -> pd.DataFrame:
    """Stage-2 confirmations with NO stage-1 of the same side in the window
    before them -- the 'skip the early tranche' comparison."""
    out = []
    s1fam = f"s1_{s1_variant}"
    s1_by_u = defaultdict(list)
    for r in epi[epi["family"] == s1fam].itertuples():
        s1_by_u[(r.underlying, r.side)].append(r.s1_sidx)
    s1_by_u = {k: np.sort(np.asarray(v)) for k, v in s1_by_u.items()}
    for side in (1, -1):
        ev = stage2_events(daily, s2_variant, side)
        for r in ev.itertuples():
            u, s = r.underlying, int(r.sidx)
            arr = s1_by_u.get((u, side))
            if arr is not None and len(arr):
                j = int(np.searchsorted(arr, s, side="right")) - 1
                if j >= 0 and s - arr[j] <= S2_WINDOW_SESSIONS:
                    continue        # preceded by stage-1 -> that's the cascade arm
            B = bars.u.get(u)
            if B is None:
                continue
            fb = bars.first_bar.get((u, s + 1))
            if fb is None:
                continue
            a2 = float(B["atr"][fb])
            if not np.isfinite(a2) or a2 <= 0:
                continue
            ps = path_stats(B, fb, side, a2, int(B["sidx"][fb]) + LARGE_HORIZON_SESSIONS)
            if not ps or ps["truncated"]:
                continue
            out.append({"family": "s2_only", "side": side, "underlying": u,
                        "mkt": "index" if u in INDEX_NAMES else "stock",
                        "entry_time": B["time"][fb], "large": ps["large"],
                        "mfe_atr": ps["mfe_atr"], "term_atr": ps["term_atr"]})
    return pd.DataFrame(out)


def main() -> None:
    print("loading ...", flush=True)
    intra, daily = load()
    print("  30m bars", len(intra), "underlyings", intra["underlying"].nunique(),
          "sessions", daily["session"].nunique(), flush=True)
    bars = Bars(intra, daily)
    frames = []
    for s2v in S2_VARIANTS:
        print(f"building s2={s2v} ...", flush=True)
        frames.append(build(intra, daily, bars, s2v))
    epi = pd.concat(frames, ignore_index=True)
    epi.to_parquet(os.path.join(DATA, "episodes.parquet"))
    print("episodes", len(epi), flush=True)

    s2o = s2_only_arm(intra, daily, bars, "primary", "primary",
                      epi[(epi["s1_variant"] == "primary") & (epi["s2_variant"] == "primary")])
    s2o.to_parquet(os.path.join(DATA, "s2_only.parquet"))
    print("s2-only episodes", len(s2o), flush=True)


if __name__ == "__main__":
    main()
