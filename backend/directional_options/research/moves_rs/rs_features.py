"""(B) Relative-strength features vs NIFTY — causal by construction.

Every feature at row t is a function of rows <= t ONLY. No centred windows, no
negative shifts, no full-sample normalisation. `rs_test_causality.py` proves
this empirically by prefix-invariance rather than by assertion.

Formulations (all computed; the primary is stated in the report):

  rs_ret_L      log(P_t/P_{t-L}) - log(N_t/N_{t-L})
                Raw relative return over L sessions. The plain "price ratio and
                its trend" reading of RS: it is exactly log((P/N)_t/(P/N)_{t-L}).
  rs_slope_L    OLS slope of log(P/N) on time over the trailing L sessions,
                divided by the residual scale. A trend-quality form: rewards a
                smooth ratio uptrend over one jumpy day.
  alpha_L       r_stock(L) - beta_120 * r_nifty(L), with beta_120 the OLS beta
                of daily stock returns on daily NIFTY returns over the trailing
                120 sessions. This is RS with the beta component removed.
  beta_120      the control. If raw RS is "just beta", beta should carry the
                same information raw RS does.

Cross-sectional percentile ranks (`*_rank`) are also produced. NOTE, and this
is stated plainly in the report: a per-date Spearman IC is rank-invariant, so
rs_ret_L and its cross-sectional rank have IDENTICAL IC. The rank form is a
different object only for pooled/decile work, not for IC.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

INDEX_NAMES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX",
    "BANKEX", "SENSEX50",
}
BENCH = "NIFTY"
LOOKBACKS = (21, 63)
BETA_WIN = 120
HORIZONS = (3, 5, 10)


def _slope_norm(y: pd.Series, win: int) -> pd.Series:
    """Trailing OLS slope of y on 0..win-1, scaled by the residual std.

    Closed form, causal: uses only the trailing window ending at t.
    """
    x = np.arange(win, dtype=float)
    x = x - x.mean()
    sxx = (x * x).sum()

    def f(w: np.ndarray) -> float:
        b = float((x * (w - w.mean())).sum() / sxx)
        resid = w - w.mean() - b * x
        s = float(resid.std(ddof=1))
        return b / s if s > 1e-12 else np.nan

    return y.rolling(win).apply(f, raw=True)


def _roll_beta(rs: pd.Series, rb: pd.Series, win: int) -> pd.Series:
    cov = rs.rolling(win).cov(rb)
    var = rb.rolling(win).var()
    return cov / var.replace(0.0, np.nan)


def per_name_features(df: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    """df: one underlying's daily bars, sorted by session, with column c/h/l.

    bench: benchmark daily bars indexed identically (same session index).
    """
    out = df.copy()
    lp = np.log(out["c"].to_numpy(dtype=float))
    lb = np.log(bench["c"].to_numpy(dtype=float))
    out["lp"] = lp
    out["lb"] = lb
    out["lratio"] = lp - lb

    r_s = pd.Series(lp, index=out.index).diff()
    r_b = pd.Series(lb, index=out.index).diff()
    out["r_s"] = r_s
    out["r_b"] = r_b
    out["beta_120"] = _roll_beta(r_s, r_b, BETA_WIN)

    lrat = pd.Series(out["lratio"].to_numpy(), index=out.index)
    for L in LOOKBACKS:
        out[f"rs_ret_{L}"] = lrat - lrat.shift(L)
        out[f"rs_slope_{L}"] = _slope_norm(lrat, L)
        rl_s = pd.Series(lp, index=out.index) - pd.Series(lp, index=out.index).shift(L)
        rl_b = pd.Series(lb, index=out.index) - pd.Series(lb, index=out.index).shift(L)
        out[f"alpha_{L}"] = rl_s - out["beta_120"] * rl_b
        out[f"mom_{L}"] = rl_s  # absolute momentum, for the beta diagnostics
    return out


def add_forwards(out: pd.DataFrame) -> pd.DataFrame:
    """Forward outcomes. These are OUTCOMES, not features: they look ahead by
    construction and are never fed back into any feature."""
    c = out["c"]
    hi_rev = out["h"][::-1]
    lo_rev = out["l"][::-1]
    for h in HORIZONS:
        out[f"fwd_{h}"] = c.shift(-h) / c - 1.0
        # rolling extreme over the forward window t+1 .. t+h
        hi = hi_rev.rolling(h).max()[::-1].shift(-1)
        lo = lo_rev.rolling(h).min()[::-1].shift(-1)
        out[f"fwd_hi_{h}"] = hi / c - 1.0
        out[f"fwd_lo_{h}"] = lo / c - 1.0
        out[f"fwd_exc_{h}"] = np.maximum(out[f"fwd_hi_{h}"].abs(), out[f"fwd_lo_{h}"].abs())
        out[f"fwd_absret_{h}"] = out[f"fwd_{h}"].abs()
    return out


def build_panel(daily: pd.DataFrame, min_sessions: int = 300) -> pd.DataFrame:
    """Assemble the cross-sectional panel: stocks only, aligned to NIFTY."""
    bench = daily[daily.underlying == BENCH][["session", "c"]] \
        .rename(columns={"c": "bc"}).sort_values("session")
    cov = daily.groupby("underlying").size()
    keep = [u for u in cov[cov >= min_sessions].index if u not in INDEX_NAMES]

    frames = []
    for u in sorted(keep):
        d = daily[daily.underlying == u][["session", "o", "h", "l", "c", "v"]] \
            .sort_values("session")
        m = d.merge(bench, on="session", how="inner").reset_index(drop=True)
        if len(m) < min_sessions:
            continue
        b = m[["bc"]].rename(columns={"bc": "c"})
        f = per_name_features(m, b)
        f = add_forwards(f)
        f["underlying"] = u
        frames.append(f)
    panel = pd.concat(frames, ignore_index=True)
    return panel


def add_xs_ranks(panel: pd.DataFrame, cols) -> pd.DataFrame:
    """Cross-sectional percentile rank per session (causal: same-date only)."""
    for c in cols:
        panel[f"{c}_rank"] = panel.groupby("session")[c].rank(pct=True)
    return panel
