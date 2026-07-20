"""(E) OPTION-LEVEL TRANSLATION of Study A (move richness) and Study B (RS).

Question the lane needs answered:
  1. Does RS-based instrument selection improve the OPTION-level outcome vs
     trading the same setups on an unselected universe, and vs a matched
     RANDOM-selection control?
  2. Does selecting for MOVE-RICHNESS (Study A) beat selecting for RS
     (Study B)? Are they the same names?

Data: reuses local extracts ONLY. No PG queries are issued by this file.
  - panel_2d3d/data/panel_opt.parquet  (option EOD snapshots, read-only)
  - moves_rs/data/daily.parquet        (Study A daily spot)
  - moves_rs/data/panel_K3.parquet     (Study A monthly move counts)
  - moves_rs/data_rs/rs_panel.parquet  (Study B RS features + XS ranks)

Causality: every selector is built from information available strictly BEFORE
the entry session (prior-month move counts; RS rank as of the entry session's
own close, which is the 15:15 decision bar the panel snapshots at; rv_21
through the entry close). Forward option returns are joined by the
underlying's session index, never by row position.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # backend/
PANEL = os.path.join(ROOT, "directional_options", "research", "panel_2d3d", "data")
DATA = os.path.join(HERE, "data")
DATA_RS = os.path.join(HERE, "data_rs")
OUT = os.path.join(HERE, "data_opt")
os.makedirs(OUT, exist_ok=True)

INDEX_NAMES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYNXT50",
    "BANKEX", "SENSEX50",
}
NON_EQUITY = INDEX_NAMES | {
    "CRUDEOIL", "NATURALGAS", "GOLD", "GOLDM", "SILVER", "SILVERM", "COPPER",
    "NICKEL", "ZINC", "ALUMINIUM", "LEAD",
}

HORIZONS = (3, 5, 10)
# round-trip cost on premium, as a fraction. index vs stock differ enormously.
COSTS = {"index": 0.016, "stock": 0.080}
COST_GRID = (0.0, 0.02, 0.05, 0.10)


# --------------------------------------------------------------------- panel
def load_opt() -> pd.DataFrame:
    o = pd.read_parquet(os.path.join(PANEL, "panel_opt.parquet"))
    o["session"] = pd.to_datetime(o["session"])
    o = o[o["is_monthly"] == True]  # noqa: E712
    o = o[(o["dte"] >= 8) & (o["dte"] <= 22)]
    o = o[o["close"] >= 1.0]
    o = o[o["mny"].notna() & o["atr_pct"].notna()]
    # forward premium returns at 5 and 10 sessions, by session index
    key = o[["contract", "sidx", "close"]].copy()
    for h in (5, 10):
        nxt = key.rename(columns={"close": f"p_f{h}"}).copy()
        nxt["sidx"] = nxt["sidx"] - h
        o = o.merge(nxt, on=["contract", "sidx"], how="left")
        o[f"ret{h}"] = o[f"p_f{h}"] / o["close"] - 1.0
    return o


def pick_band(o: pd.DataFrame) -> pd.DataFrame:
    """One contract per (underlying, session, side, band): the one whose signed
    moneyness is closest to the band's centre. Deterministic, no lookahead."""
    bands = {
        "deep_ITM": (-0.10, -0.03, -0.045),
        "slight_ITM": (-0.03, -0.0075, -0.018),
    }
    out = []
    for name, (lo, hi, mid) in bands.items():
        b = o[(o["mny"] >= lo) & (o["mny"] < hi)].copy()
        b["band"] = name
        b["dist"] = (b["mny"] - mid).abs()
        b = b.sort_values("dist").drop_duplicates(
            ["underlying", "session", "option_type", "band"], keep="first"
        )
        out.append(b)
    r = pd.concat(out, ignore_index=True)
    r["market"] = np.where(r["underlying"].isin(NON_EQUITY), "index", "stock")
    return r


# ----------------------------------------------------------------- selectors
def build_selectors() -> pd.DataFrame:
    """Per (underlying, session): RS rank, prior-month move count rank, rv_21 rank.
    All strictly causal at the entry session's 15:15 decision bar."""
    rs = pd.read_parquet(os.path.join(DATA_RS, "rs_panel.parquet"))
    rs["session"] = pd.to_datetime(rs["session"])
    rs = rs[["underlying", "session", "rs_ret_21", "rs_ret_21_rank",
             "alpha_21", "alpha_21_rank", "beta_120"]]

    d = pd.read_parquet(os.path.join(DATA, "daily.parquet"))
    d["session"] = pd.to_datetime(d["session"])
    d = d.sort_values(["underlying", "session"])
    d["lr"] = np.log(d.groupby("underlying")["close"].transform(
        lambda s: s / s.shift(1)))
    # trailing realised vol through the entry close (causal)
    d["rv_21"] = d.groupby("underlying")["lr"].transform(
        lambda s: s.rolling(21, min_periods=15).std())
    d["month"] = d["session"].dt.to_period("M").astype(str)
    sel = d[["underlying", "session", "month", "rv_21"]].copy()

    # prior-month move count (Study A, K=3). month m selector = month m-1 count.
    mk = pd.read_parquet(os.path.join(DATA, "panel_K3.parquet"))
    mk = mk[["underlying", "month", "n_moves", "mean_absret", "n_sessions"]].copy()
    mk = mk[mk["n_sessions"] >= 15]
    mk["mp"] = pd.PeriodIndex(mk["month"], freq="M")
    mk["apply_month"] = (mk["mp"] + 1).astype(str)
    mk = mk.rename(columns={"n_moves": "prev_n_moves",
                            "mean_absret": "prev_mean_absret"})
    sel = sel.merge(mk[["underlying", "apply_month", "prev_n_moves",
                        "prev_mean_absret"]],
                    left_on=["underlying", "month"],
                    right_on=["underlying", "apply_month"], how="left")
    sel = sel.merge(rs, on=["underlying", "session"], how="left")

    # cross-sectional percentile ranks WITHIN each session (date-t rows only)
    for col in ("rv_21", "prev_n_moves"):
        sel[col + "_rank"] = sel.groupby("session")[col].rank(pct=True)
    return sel


# ----------------------------------------------------------------- inference
def cluster_stats(df: pd.DataFrame, col: str) -> dict:
    """Date-clustered mean + t. Every trade opened on the same session is one
    cluster (they share the market move); this is the episode-cluster
    correction. Overlapping holds across dates are further handled by the
    de-overlapped variant (dates spaced h apart) reported alongside."""
    x = df[[col, "session"]].dropna()
    if len(x) < 30:
        return dict(n=len(x), mean=np.nan, t=np.nan, nclust=0)
    g = x.groupby("session")[col].mean()
    n_c = len(g)
    m = g.mean()
    se = g.std(ddof=1) / np.sqrt(n_c)
    return dict(n=len(x), mean=m, t=m / se if se > 0 else np.nan, nclust=n_c,
                median=x[col].median(), win=(x[col] > 0).mean())


def deoverlap_stats(df: pd.DataFrame, col: str, h: int) -> dict:
    """Average the phase-offset sub-samples where entry dates are spaced h
    sessions apart, so no two trades in a sub-sample overlap."""
    x = df[[col, "session"]].dropna()
    if x.empty:
        return dict(t=np.nan)
    dates = np.sort(x["session"].unique())
    ts = []
    for off in range(h):
        keep = set(dates[off::h])
        s = x[x["session"].isin(keep)]
        if s["session"].nunique() < 12:
            continue
        g = s.groupby("session")[col].mean()
        se = g.std(ddof=1) / np.sqrt(len(g))
        if se > 0:
            ts.append(g.mean() / se)
    return dict(t=float(np.mean(ts)) if ts else np.nan, n_phase=len(ts))


if __name__ == "__main__":
    o = load_opt()
    print("opt rows dte8-22 monthly", len(o), o["underlying"].nunique())
    b = pick_band(o)
    print("banded rows", len(b))
    sel = build_selectors()
    b = b.merge(sel, on=["underlying", "session"], how="left")
    b.to_parquet(os.path.join(OUT, "opt_sel.parquet"))
    print("joined", len(b), "with rs rank",
          b["rs_ret_21_rank"].notna().mean().round(3),
          "with prev_n_moves", b["prev_n_moves_rank"].notna().mean().round(3))
