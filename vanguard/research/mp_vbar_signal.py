"""Test the owner's four-stage volume-bar directional framework, as specified.

THE RULE SET IS FIXED BEFORE LOOKING AT RESULTS -- this is the point of the
design and it is preserved deliberately. Every threshold below (0.20 imbalance,
0.60 close location, 80th percentile efficiency, the 1.5R/-1R/10-bar triple
barrier) was specified by the owner in the brief, not fitted here. That makes
this closer to a genuine confirmatory test than anything else in this project:
if it fails, the right move is to report the failure, not retune the
thresholds until something passes.

SIGNAL SOURCE: NIFTY and BANKNIFTY FUTURES only (front-contract stitched via
research.mp_futures), never option volume -- fragmented across strikes/expiries,
mixes hedging and vol trades with directional flow (the owner's own point).

STAGES, exactly as specified:
    1. LOCATION      close above the prior session's value-area high, OR above
                     today's own opening-range (IB) high.
    2. INITIATION    a bar completes fast (below its own bucket's median
                     duration), has efficiency at/above its bucket's 80th
                     percentile, imbalance_proxy >= +0.20, close_location >= +0.60.
                     Two entry variants are tested separately:
                       RAW       enter at the initiation bar's own close
                       CONFIRMED enter at the NEXT bar's close, and only if that
                                 bar held above the broken level (stage 3's
                                 first condition) -- a strictly later, costlier,
                                 but non-lookahead entry
    3. ACCEPTANCE    tracked as a bar-by-bar outcome after entry, not as a
                     pre-entry filter -- "the next bars stay above the level" is
                     only knowable in hindsight relative to the initiation bar,
                     so it cannot gate an entry made AT that bar without leaking
                     the future. CONFIRMED entries use it as the entry trigger
                     instead of a subsequent filter, which is the honest way to
                     use it.
    4. DEFENDED PULLBACK  measured as a DESCRIPTIVE outcome on winning trades
                     (does the first adverse excursion after entry show falling
                     efficiency, consistent with absorption of selling) rather
                     than as a second gate -- gating on it would need the
                     pullback to have already happened, which is after entry.

BENCHMARKS, so the marginal value of each stage is visible rather than assumed:
    UNCONDITIONAL   the triple-barrier test applied to every bar
    LOCATION-ONLY   stage 1 alone, no efficiency/imbalance/close-location filter
    FULL GATE       all of stage 2's four conditions
    4-OF-5          stage 1 + at least 4 of the 5 conditions (location counted
                    as one), the owner's own suggested relaxation

    python vanguard/research/mp_vbar_signal.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_futures import load_futures_bars, load_futures_sessions  # noqa: E402
from research.mp_vbar_flow import (  # noqa: E402
    add_session_phase, add_vol_regime, normalize_causal, volume_bars_with_flow,
)

HORIZON_BARS = 10
TP_R, SL_R = 1.5, 1.0
EFF_PCT_MIN = 0.80
IMBAL_MIN = 0.20
CLOSE_LOC_MIN = 0.60
BUCKET_WINDOW = 60


def build_bars(connection, symbol: str, start, threshold_hint_days: int = 95) -> pd.DataFrame:
    """Volume bars on a FIXED threshold sized from a recent, data-dense window,
    applied across the whole requested history -- so the threshold is not
    re-picked per period (which would be a second free parameter)."""
    recent = load_futures_bars(connection, [symbol],
                               date.today() - timedelta(days=threshold_hint_days))
    hint_days = recent["ts"].dt.date.nunique()
    threshold = recent["volume"].sum() / max(hint_days * 13, 1)   # ~one 30m-equivalent bar

    all_bars = load_futures_bars(connection, [symbol], start)
    vb = volume_bars_with_flow(all_bars, target_bars=int(all_bars["volume"].sum() / threshold))
    vb["symbol"] = symbol
    return vb


def add_location(vb: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    vb = vb.copy()
    vb["dt"] = vb["t_start"].dt.date
    s = sessions.copy()
    s["dt"] = pd.to_datetime(s["dt"]).dt.date
    ref = s[["dt", "prev_vah", "prev_val", "ib_hi", "ib_lo"]].rename(
        columns={"prev_vah": "prior_vah", "prev_val": "prior_val",
                "ib_hi": "today_ib_hi", "ib_lo": "today_ib_lo"})
    vb = vb.merge(ref, on="dt", how="left")
    vb["above_location"] = (vb["close"] > vb["prior_vah"]) | (vb["close"] > vb["today_ib_hi"])
    vb["below_location"] = (vb["close"] < vb["prior_val"]) | (vb["close"] < vb["today_ib_lo"])
    return vb


def gate_features(vb: pd.DataFrame) -> pd.DataFrame:
    vb = add_session_phase(vb)
    vb = add_vol_regime(vb)
    vb["speed"] = -vb["duration_s"]   # so "high percentile" = fast, consistent direction
    vb["eff_pct"] = normalize_causal(vb, "efficiency", BUCKET_WINDOW)
    vb["speed_pct"] = normalize_causal(vb, "speed", BUCKET_WINDOW)
    return vb


def triple_barrier(vb: pd.DataFrame, entry_idx: np.ndarray, direction: int,
                   atr_col: str = "atr14") -> pd.DataFrame:
    """Y=1 if +1.5R hits before -1R within HORIZON_BARS volume bars, direction
    signed (+1 long, -1 short). R = the entry bar's own trailing ATR14."""
    hi, lo, cl = vb["high"].to_numpy(), vb["low"].to_numpy(), vb["close"].to_numpy()
    atr = vb[atr_col].to_numpy()
    rows = []
    for i in entry_idx:
        if i + 1 >= len(vb) or not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        entry = cl[i]
        tp = entry + direction * TP_R * atr[i]
        sl = entry - direction * SL_R * atr[i]
        y, bars_to, mfe, mae = 0, np.nan, 0.0, 0.0
        end = min(i + 1 + HORIZON_BARS, len(vb))
        for k, j in enumerate(range(i + 1, end), start=1):
            fav = direction * (hi[j] - entry) if direction > 0 else direction * (lo[j] - entry)
            adv = direction * (entry - lo[j]) if direction > 0 else direction * (entry - hi[j])
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            hit_tp = (hi[j] >= tp) if direction > 0 else (lo[j] <= tp)
            hit_sl = (lo[j] <= sl) if direction > 0 else (hi[j] >= sl)
            if hit_tp and hit_sl:
                y, bars_to = 0, k          # ambiguous same-bar: conservative, count as loss
                break
            if hit_sl:
                y, bars_to = 0, k
                break
            if hit_tp:
                y, bars_to = 1, k
                break
        r_at_timeout = direction * (cl[end - 1] - entry) / atr[i] if end > i + 1 else 0.0
        rows.append({
            "i": i, "t": vb["t_start"].iloc[i], "entry": entry, "atr": atr[i],
            "y": y, "bars_to_resolve": bars_to,
            "mfe_R": mfe / atr[i], "mae_R": mae / atr[i],
            "r_at_timeout": r_at_timeout,
            "session_phase": vb["session_phase"].iloc[i], "vol_regime": vb["vol_regime"].iloc[i],
            "above_location": vb["above_location"].iloc[i] if "above_location" in vb else None,
        })
    return pd.DataFrame(rows)


def summarize(label: str, tb: pd.DataFrame) -> dict:
    if len(tb) < 20:
        return {"label": label, "n": len(tb)}
    win = tb["y"].mean()
    exp_R = np.where(tb["y"] == 1, TP_R, tb["r_at_timeout"].where(tb["bars_to_resolve"].isna(), -SL_R))
    exp_R = np.where(tb["y"] == 1, TP_R,
                     np.where(tb["bars_to_resolve"].notna(), -SL_R, tb["r_at_timeout"]))
    mean_R = np.mean(exp_R)
    se = np.std(exp_R, ddof=1) / np.sqrt(len(exp_R))
    t = mean_R / se if se > 0 else np.nan
    return {"label": label, "n": len(tb), "win_rate": win, "mean_R": mean_R, "t": t,
            "mean_mfe_R": tb["mfe_R"].mean(), "mean_mae_R": tb["mae_R"].mean(),
            "mean_bars": tb["bars_to_resolve"].dropna().mean()}


def report_row(s: dict) -> str:
    if s.get("n", 0) < 20:
        return f"   {s['label']:<32}{s.get('n',0):>6}   (too few)"
    return (f"   {s['label']:<32}{s['n']:>6}{s['win_rate']*100:>8.1f}%"
            f"{s['mean_R']:>+9.3f}{s['t']:>+8.2f}{s['mean_mfe_R']:>+9.2f}"
            f"{s['mean_mae_R']:>+9.2f}{s['mean_bars']:>8.1f}")


def run_symbol(connection, symbol: str, start) -> None:
    print(f"\n{'='*100}\n{symbol} FUTURES  (front-contract stitched)\n{'='*100}")
    vb = build_bars(connection, symbol, start)
    sessions = load_futures_sessions(connection, [symbol], start)
    vb = add_location(vb, sessions)
    vb = gate_features(vb)
    vb = vb.dropna(subset=["atr14"]).reset_index(drop=True)
    print(f"{len(vb):,} volume bars, {vb['t_start'].min()} .. {vb['t_start'].max()}")
    print(f"of_available=True, of_measured=False -- imbalance_proxy is a tick-rule "
          f"inference from 1-minute prints, NOT measured bid/ask depth")

    long_gate5 = (vb["eff_pct"] >= EFF_PCT_MIN) & (vb["imbalance_proxy"] >= IMBAL_MIN) \
        & (vb["close_location"] >= CLOSE_LOC_MIN) & (vb["speed_pct"] >= 0.5)
    long_full = vb["above_location"].fillna(False) & long_gate5
    cond_count = (vb["above_location"].fillna(False).astype(int)
                 + (vb["eff_pct"] >= EFF_PCT_MIN).astype(int)
                 + (vb["imbalance_proxy"] >= IMBAL_MIN).astype(int)
                 + (vb["close_location"] >= CLOSE_LOC_MIN).astype(int)
                 + (vb["speed_pct"] >= 0.5).astype(int))
    long_4of5 = cond_count >= 4
    # core-4: drop the speed/duration condition specifically -- it is the
    # noisiest of the five (duration is mechanically distorted by the sparse-
    # coverage 2024 period even after the rolling-window fix) and dropping it
    # tests whether the other four alone, without the least reliable one, carry
    # the signal.
    long_core4 = vb["above_location"].fillna(False) & (vb["eff_pct"] >= EFF_PCT_MIN) \
        & (vb["imbalance_proxy"] >= IMBAL_MIN) & (vb["close_location"] >= CLOSE_LOC_MIN)

    # bearish mirror: location and the three signed conditions flip; the speed
    # condition does not (a fast bar is urgent regardless of direction)
    short_gate5 = vb["below_location"].fillna(False) & (vb["eff_pct"] <= 1 - EFF_PCT_MIN) \
        & (vb["imbalance_proxy"] <= -IMBAL_MIN) & (vb["close_location"] <= -CLOSE_LOC_MIN) \
        & (vb["speed_pct"] >= 0.5)

    all_idx = np.arange(len(vb) - 1)
    loc_idx = np.flatnonzero(vb["above_location"].fillna(False).to_numpy())[:-1] \
        if vb["above_location"].fillna(False).any() else np.array([], dtype=int)
    full_idx = np.flatnonzero(long_full.to_numpy())
    full_idx = full_idx[full_idx < len(vb) - 1]
    g4_idx = np.flatnonzero(long_4of5.to_numpy())
    g4_idx = g4_idx[g4_idx < len(vb) - 1]
    core4_idx = np.flatnonzero(long_core4.to_numpy())
    core4_idx = core4_idx[core4_idx < len(vb) - 1]
    short_idx = np.flatnonzero(short_gate5.to_numpy())
    short_idx = short_idx[short_idx < len(vb) - 1]
    # CONFIRMED variant: entry at bar i+1's close, only if i+1 held above the
    # level broken at bar i (its low stayed above the initiation bar's own close)
    confirmed = []
    for i in full_idx:
        if i + 1 < len(vb) and vb["low"].iloc[i + 1] >= vb["close"].iloc[i]:
            confirmed.append(i + 1)
    confirmed_idx = np.array(confirmed, dtype=int)
    confirmed_idx = confirmed_idx[confirmed_idx < len(vb) - 1]

    print(f"\ncounts: all={len(all_idx):,}  location-only={len(loc_idx):,}  "
          f"full-gate(raw)={len(full_idx):,}  full-gate(confirmed)={len(confirmed_idx):,}  "
          f"4-of-5={len(g4_idx):,}")

    print(f"\nLONG, triple barrier +{TP_R}R / -{SL_R}R within {HORIZON_BARS} bars, "
          f"R = bar's own ATR14")
    print(f"   {'cohort':<32}{'n':>6}{'win':>9}{'mean R':>9}{'t':>8}{'MFE(R)':>9}"
          f"{'MAE(R)':>9}{'bars':>8}")
    for label, idx in (("unconditional (all bars)", all_idx),
                       ("location only", loc_idx),
                       ("full gate (5-of-5), RAW entry", full_idx),
                       ("full gate, CONFIRMED entry", confirmed_idx),
                       ("core-4 (no speed condition)", core4_idx),
                       ("4-of-5 conditions", g4_idx)):
        if len(idx) < 5:
            print(f"   {label:<32}{len(idx):>6}   (too few)")
            continue
        tb = triple_barrier(vb, idx, direction=1)
        print(report_row(summarize(label, tb)))

    print(f"\nSHORT mirror (below-location, bottom-20th eff, imbalance<=-0.20, "
          f"close_loc<=-0.60, fast)")
    if len(short_idx) >= 5:
        tbs = triple_barrier(vb, short_idx, direction=-1)
        print(report_row(summarize("full gate (5-of-5), RAW entry", tbs)))
    else:
        print(f"   {'full gate (5-of-5), RAW entry':<32}{len(short_idx):>6}   (too few)")

    # split-half on whichever cohort has enough observations to split at all
    for label, idx in (("core-4", core4_idx), ("4-of-5", g4_idx)):
        if len(idx) >= 40:
            h = idx[len(idx)//2]
            a_idx, b_idx = idx[idx < h], idx[idx >= h]
            ta, tb_ = triple_barrier(vb, a_idx, 1), triple_barrier(vb, b_idx, 1)
            sa, sb = summarize(f"{label} 1st half", ta), summarize(f"{label} 2nd half", tb_)
            print(f"\n   split-half ({label}):")
            print(report_row(sa)); print(report_row(sb))
        else:
            print(f"\n   split-half ({label}): n={len(idx)}, too few to split meaningfully")

    print(f"\nDESCRIPTIVE: defended-pullback check on RAW full-gate WINNERS "
          f"(does the first adverse bar after entry show falling efficiency?)")
    if len(full_idx):
        tb = triple_barrier(vb, full_idx, 1)
        winners = tb[tb["y"] == 1]["i"].to_numpy()
        if len(winners):
            first_pullback_eff = []
            for i in winners:
                for j in range(i+1, min(i+4, len(vb))):
                    if vb["close"].iloc[j] < vb["close"].iloc[j-1]:
                        first_pullback_eff.append(vb["eff_pct"].iloc[j])
                        break
            if first_pullback_eff:
                arr = np.array(first_pullback_eff)
                print(f"   winners with an identifiable pullback: {len(arr)}   "
                      f"median pullback efficiency percentile: {np.nanmedian(arr):.2f}   "
                      f"(low = consistent with absorption, as the framework predicts)")

    print(f"\nEFFICIENCY / IMBALANCE / CLOSE-LOCATION marginal value "
          f"(rank IC of raw features vs {HORIZON_BARS}-bar forward R, unconditional)")
    fwd = vb["close"].shift(-HORIZON_BARS)
    fwd_R = (fwd - vb["close"]) / vb["atr14"]
    for col in ("efficiency", "imbalance_proxy", "close_location", "speed_pct"):
        d = pd.DataFrame({"x": vb[col], "y": fwd_R}).dropna()
        if len(d) < 60:
            continue
        ic = d["x"].corr(d["y"], method="spearman")
        print(f"   {col:<20} IC={ic:+.3f}   n={len(d)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.1)
    parser.add_argument("--dsn", default=os.environ.get(
        "VANGUARD_DATABASE_URL", "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"))
    args = parser.parse_args()
    start = date.today() - timedelta(days=int(args.years * 365.25))
    connection = psycopg2.connect(args.dsn)
    try:
        for symbol in ("NIFTY", "BANKNIFTY"):
            run_symbol(connection, symbol, start)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
