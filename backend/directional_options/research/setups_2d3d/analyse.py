"""(C) Analysis of the labelled setup dataset.

Headline metrics are deliberately the ones that killed the previous two
candidates, plus one more this pass added:

  * net PF / net mean AFTER round-trip cost (three cost scenarios),
  * total net PnL EXCLUDING THE TOP-3 WINNERS  (winner concentration),
  * per-quarter net PnL                        (one-period dependence),
  * a +1-bar execution-lag variant             (execution fragility),
  * EPISODE CLUSTERING to one observation per (underlying, session)
    -- a 30m setup rule fires on many consecutive bars of the same move, so
    raw trade counts massively overstate the independent sample size and the
    t-stat. This is the correction that decides the whole study.
  * comparison against a MATCHED CONTROL (unconditional long / unconditional
    short, same instrument selection, same barriers, same costs). In a sample
    with drift, "always long" looks like edge; a family must beat its control,
    not zero.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
NOTIONAL = 25_000.0
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 300)
pd.set_option("display.float_format", lambda v: f"{v:,.2f}")


def pf(x: np.ndarray) -> float:
    g = x[x > 0].sum()
    b = -x[x < 0].sum()
    return float(g / b) if b > 0 else np.inf


def tstat(r: np.ndarray) -> float:
    if len(r) < 2:
        return np.nan
    sd = r.std(ddof=1)
    return float(r.mean() / (sd / np.sqrt(len(r)))) if sd > 0 else np.nan


def episodes(g: pd.DataFrame, col: str) -> np.ndarray:
    """One observation per (underlying, entry session): overlapping intra-session
    firings of the same rule are NOT independent trades."""
    d = pd.to_datetime(g["entry_time"]).dt.date
    return g.groupby([g["underlying"], d])[col].mean().to_numpy(float)


def block(g: pd.DataFrame, col: str = "net_base") -> pd.Series:
    r = g[col].to_numpy(float)
    if len(r) == 0:
        return pd.Series(dtype=float)
    p = r * NOTIONAL
    ep = episodes(g, col)
    return pd.Series({
        "n": len(r),
        "n_ep": len(ep),
        "hit%": 100 * (r > 0).mean(),
        "mean%": 100 * r.mean(),
        "med%": 100 * np.median(r),
        "t_raw": tstat(r),
        "t_ep": tstat(ep),
        "mean_ep%": 100 * ep.mean(),
        "PF": pf(r),
        "pnl": p.sum(),
        "ex_top3": p.sum() - np.sort(p)[-3:].sum() if len(p) > 3 else np.nan,
        "pess%": 100 * g["net_pessimistic"].mean(),
        "lag1%": 100 * g["net_base_lag1"].mean(),
    })


def grouped(t: pd.DataFrame, by, col="net_base") -> pd.DataFrame:
    return t.groupby(by, sort=True).apply(block, col, include_groups=False).round(2)


def main() -> None:
    t = pd.read_parquet(os.path.join(DATA, "trades.parquet"))
    t["is_index"] = t["underlying"].isin(INDEXES)
    t["mkt"] = np.where(t["is_index"], "index", "stock")
    t["is_control"] = t["family"].str.startswith("control")

    print("=" * 118)
    print("1. DATASET")
    print("=" * 118)
    print("trades", len(t), "| underlyings", t["underlying"].nunique(),
          "| contracts", t["contract"].nunique())
    print("period", t["entry_time"].min(), "->", t["entry_time"].max())
    print("hold: <=", t["bars_held"].max(), "30m bars | dte at entry:",
          f'{t["dte_entry"].min()}-{t["dte_entry"].max()}',
          f'(median {t["dte_entry"].median():.0f})')
    print("stale exit quote rate:", round((t["stale_exit_quote"] > 0).mean(), 4))
    print("\ntrades by family x band:")
    print(pd.crosstab(t["family"], [t["band"], t["mkt"]]))
    print("\nbarrier outcome mix (%):")
    print(pd.crosstab(t["family"], t["outcome"], normalize="index").mul(100).round(1))

    print("\n" + "=" * 118)
    print("2. DOES THE SPOT VIEW EVEN WORK?  (directional accuracy at SPOT level)")
    print("=" * 118)
    print(t.groupby("family").apply(lambda g: pd.Series({
        "n": len(g),
        "spot_win%": 100 * (g["spot_ret"] > 0).mean(),
        "spot_mean%": 100 * g["spot_ret"].mean(),
        "target%": 100 * (g["outcome"] == "target").mean(),
        "stop%": 100 * (g["outcome"] == "stop").mean(),
    }), include_groups=False).round(2))

    print("\n" + "=" * 118)
    print("3. NET OF COST, BY FAMILY x BAND  (base cost 1.6% round trip)")
    print("=" * 118)
    for band in sorted(t["band"].unique()):
        print(f"\n--- band={band} ---")
        print(grouped(t[t["band"] == band], "family"))

    print("\n" + "=" * 118)
    print("4. THE SPLIT THAT MATTERS: market x side  (drift makes 'always long' look good)")
    print("=" * 118)
    for mkt in ("index", "stock"):
        for side in (1, -1):
            sub = t[(t["mkt"] == mkt) & (t["side"] == side)]
            if sub.empty:
                continue
            print(f"\n--- {mkt}  side={side:+d} ---")
            print(grouped(sub, ["band", "family"]))

    print("\n" + "=" * 118)
    print("5. EXCESS OVER MATCHED CONTROL  (family mean - unconditional-same-side mean)")
    print("   episode-clustered; a family with excess <= 0 is adding nothing")
    print("=" * 118)
    rows = []
    for (mkt, band, side), sub in t.groupby(["mkt", "band", "side"]):
        ctl = "control_long" if side == 1 else "control_short"
        c = sub[sub["family"] == ctl]
        if len(c) < 5:
            continue
        cep = episodes(c, "net_base")
        for fam, g in sub.groupby("family"):
            if fam.startswith("control"):
                continue
            fep = episodes(g, "net_base")
            rows.append({
                "mkt": mkt, "band": band, "side": side, "family": fam,
                "n": len(g), "n_ep": len(fep),
                "fam_mean%": 100 * g["net_base"].mean(),
                "fam_ep_mean%": 100 * fep.mean(),
                "ctl_ep_mean%": 100 * cep.mean(),
                "excess_ep%": 100 * (fep.mean() - cep.mean()),
                "t_ep": tstat(fep),
            })
    ex = pd.DataFrame(rows)
    print(ex.sort_values("excess_ep%", ascending=False).round(2).to_string(index=False))
    print("\ncells with positive ABSOLUTE episode mean :",
          int((ex["fam_ep_mean%"] > 0).sum()), "/", len(ex))
    print("cells with positive EXCESS over control    :",
          int((ex["excess_ep%"] > 0).sum()), "/", len(ex))
    print("cells with excess > 0 AND t_ep > 2         :",
          int(((ex["excess_ep%"] > 0) & (ex["t_ep"] > 2)).sum()), "/", len(ex))

    print("\n" + "=" * 118)
    print("6. PER-QUARTER NET PnL (base cost, Rs on 25k premium/leg)")
    print("=" * 118)
    for band in sorted(t["band"].unique()):
        piv = t[t["band"] == band].pivot_table(
            index="family", columns="quarter", values="net_base",
            aggfunc=lambda x: (x * NOTIONAL).sum())
        print(f"\n[{band}]")
        print(piv.round(0))
        print("quarters positive / total:")
        print((piv > 0).sum(axis=1).astype(str) + " / " + piv.notna().sum(axis=1).astype(str))

    print("\n" + "=" * 118)
    print("7. ROBUSTNESS: PnL after removing the best underlying / best quarter / top-3 trades")
    print("=" * 118)
    rows = []
    for (band, fam), g in t.groupby(["band", "family"]):
        p = (g["net_base"] * NOTIONAL)
        rows.append({
            "band": band, "family": fam, "n": len(g), "pnl": p.sum(),
            "ex_best_underlying": p.sum() - p.groupby(g["underlying"]).sum().max(),
            "ex_best_quarter": p.sum() - p.groupby(g["quarter"]).sum().max(),
            "ex_top3": p.sum() - np.sort(p.to_numpy())[-3:].sum(),
        })
    print(pd.DataFrame(rows).set_index(["band", "family"]).round(0))

    print("\n" + "=" * 118)
    print("8. PER-UNDERLYING, index names, base cost, all non-control families")
    print("=" * 118)
    print(grouped(t[t["is_index"] & ~t["is_control"]], ["band", "underlying"]))


if __name__ == "__main__":
    out = os.path.join(HERE, "results.txt")
    with open(out, "w") as fh:
        class Tee:
            def write(self, s):
                sys.__stdout__.write(s)
                fh.write(s)

            def flush(self):
                sys.__stdout__.flush()
                fh.flush()

        sys.stdout = Tee()
        main()
        sys.stdout = sys.__stdout__
    print("\nwrote", out)
