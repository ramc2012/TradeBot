"""Sniper RETRAIN harness — close the learning loop, safely.

Assembles a training set from the shadow log (X = the feature vector logged at prediction time,
INCLUDING the live u_of_* order-flow and o_* option families) joined to the scorer's realized
outcomes (y). When enough order-flow-bearing labeled samples have accumulated, it trains a candidate
ExcursionEstimator (whose feature set now spans u_of_*, which the incumbent lacks), evaluates
candidate vs incumbent on a time holdout, and PROMOTES the candidate only if it is strictly better —
otherwise it holds the incumbent. Every run writes sniper_retrain_report.json for the dashboard.

Why a gate: the live set is small and order flow exists only going forward, so a data-starved
candidate must never silently replace the battle-tested incumbent. Promotion requires a real margin
on a minimum-size holdout across a majority of horizons, and the incumbent is always backed up first.

Paper/shadow only — this swaps the model the SHADOW lane uses; it places no orders.

Run:  python sniper_retrain.py
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from nomad_sniper.models.excursion import ExcursionEstimator, train_excursion_estimator

SNIPER_DIR = os.environ.get("SNIPER_DIR", "/sniper")
LOG = os.path.join(SNIPER_DIR, "sniper_shadow.jsonl")
SCORED = os.path.join(SNIPER_DIR, "sniper_scored.jsonl")
MODEL = os.environ.get("SNIPER_MODEL", os.path.join(SNIPER_DIR, "sniper_artifacts/excursion_estimator_sensex.joblib"))
REPORT = os.path.join(SNIPER_DIR, "sniper_retrain_report.json")

# Promotion gate — deliberately conservative (small, forward-only live data).
TARGET_OF_SAMPLES = int(os.environ.get("SNIPER_RETRAIN_TARGET", "600"))  # OF-bearing labeled rows to attempt
MIN_TEST = int(os.environ.get("SNIPER_RETRAIN_MIN_TEST", "120"))         # min holdout rows to trust a verdict
IC_MARGIN = float(os.environ.get("SNIPER_RETRAIN_IC_MARGIN", "0.03"))    # candidate must beat incumbent mag-IC by this
PROMOTE = os.environ.get("SNIPER_RETRAIN_PROMOTE", "1") == "1"

INTRADAY = {"30m": 30, "60m": 60, "90m": 90, "120m": 120}
SWING = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "1w": 5, "1M": 21}
TFS = list(INTRADAY) + list(SWING)
TTP_COLS = {tf: f"time_to_peak_frac_{tf}" for tf in INTRADAY}
TTP_COLS.update({tf: f"time_to_peak_days_{tf}" for tf in SWING})
CATS = ["u_location_vs_prev_value", "u_open_location", "u_htf_week_location", "u_htf_month_location",
        "u_htf_quarter_location", "u_htf_year_location", "c_time_of_day_bucket"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(report: dict) -> None:
    report["generated_at"] = _now()
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps({k: report[k] for k in ("status", "n_labeled", "n_of_bearing", "promoted")
                      if k in report}))


def _rank_ic(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    m = np.isfinite(p) & np.isfinite(y)
    if m.sum() < 20 or np.std(p[m]) == 0 or np.std(y[m]) == 0:
        return np.nan
    return float(pd.Series(p[m]).rank().corr(pd.Series(y[m]).rank()))


def _dir_acc(p_up, dom_dir):
    p_up, dom_dir = np.asarray(p_up, float), np.asarray(dom_dir, float)
    m = np.isfinite(p_up) & np.isfinite(dom_dir)
    if m.sum() < 20:
        return np.nan
    return float(((p_up[m] >= 0.5).astype(int) == (dom_dir[m] == 1).astype(int)).mean())


def _build_table() -> pd.DataFrame:
    if not (os.path.exists(LOG) and os.path.exists(SCORED)):
        return pd.DataFrame()
    feats = {}
    for line in open(LOG):
        if not line.strip():
            continue
        r = json.loads(line)
        f = r.get("features")
        if not f:
            continue  # only records logged after feature-logging shipped
        key = (r["symbol"], r["decision_time"])
        feats[key] = {**f, "underlying_key": r["symbol"], "decision_time": r["decision_time"],
                      "has_live_of": bool(r.get("has_live_of"))}
    if not feats:
        return pd.DataFrame()
    # attach realized labels per horizon from the scorer
    labels: dict = {}
    for line in open(SCORED):
        if not line.strip():
            continue
        s = json.loads(line)
        key = (s["symbol"], s["decision_time"])
        if key not in feats:
            continue
        tf = s["horizon"]
        d = labels.setdefault(key, {})
        d[f"magnitude_atr_{tf}"] = s.get("real_mag")
        d[f"dom_dir_{tf}"] = s.get("real_dom_dir")
        ttp = s.get("real_ttp_frac")
        d[TTP_COLS[tf]] = (ttp * SWING[tf] if tf in SWING and ttp is not None else ttp)
    rows = [{**feats[k], **labels.get(k, {})} for k in feats]
    df = pd.DataFrame(rows)
    df["decision_time"] = pd.to_datetime(df["decision_time"])
    return df.sort_values("decision_time").reset_index(drop=True)


def main() -> int:
    df = _build_table()
    n_labeled = int(df.shape[0]) if not df.empty else 0
    n_of = int(df["has_live_of"].sum()) if n_labeled else 0
    base = {"model": os.path.basename(MODEL), "n_labeled": n_labeled, "n_of_bearing": n_of,
            "target_of_samples": TARGET_OF_SAMPLES, "promoted": False}

    if n_of < TARGET_OF_SAMPLES:
        _write({**base, "status": "accumulating",
                "note": f"{n_of}/{TARGET_OF_SAMPLES} order-flow-bearing labeled samples — holding incumbent. "
                        f"Order flow exists only forward, so this grows ~{3*12} rows per market day."})
        return 0

    # feature columns: contract families u/o/c, exclude bookkeeping + label-like; INCLUDE u_of_*
    EX = {"underlying_key", "decision_time", "has_live_of"}
    LBL = ("magnitude_atr_", "dom_dir_", "time_to_peak_")
    fcols = [c for c in df.columns if c and c[0] in "uoc" and c not in EX and not c.startswith(LBL)]
    fcols = [c for c in fcols if c in CATS or pd.api.types.is_numeric_dtype(df[c])]
    cats = [c for c in CATS if c in fcols]

    cut = df["decision_time"].quantile(0.8)
    tr, te = df[df["decision_time"] <= cut], df[df["decision_time"] > cut]
    if len(te) < MIN_TEST:
        _write({**base, "status": "ready_but_thin_holdout",
                "note": f"holdout {len(te)} < {MIN_TEST}; need more recent samples before a trustworthy verdict."})
        return 0

    incumbent = ExcursionEstimator.load(MODEL)
    candidate = train_excursion_estimator(tr, TFS, feature_columns=fcols, categorical_features=cats,
                                          ttp_cols=TTP_COLS, num_boost_round=300)
    inc_p, cand_p = incumbent.predict(te), candidate.predict(te)

    per_tf, inc_ics, cand_ics, wins = {}, [], [], 0
    for tf in TFS:
        if f"magnitude_atr_{tf}" not in te or te[f"magnitude_atr_{tf}"].notna().sum() < 20:
            continue
        ymag, ydir = te[f"magnitude_atr_{tf}"].values, te[f"dom_dir_{tf}"].values
        ii = _rank_ic(inc_p.get(tf, {}).get("magnitude"), ymag) if tf in inc_p else np.nan
        ci = _rank_ic(cand_p[tf]["magnitude"], ymag) if tf in cand_p else np.nan
        ida = _dir_acc(inc_p.get(tf, {}).get("p_up"), ydir) if tf in inc_p else np.nan
        cda = _dir_acc(cand_p[tf]["p_up"], ydir) if tf in cand_p else np.nan
        per_tf[tf] = {"n": int(np.isfinite(ymag).sum()), "inc_mag_ic": ii, "cand_mag_ic": ci,
                      "inc_dir_acc": ida, "cand_dir_acc": cda}
        if np.isfinite(ii) and np.isfinite(ci):
            inc_ics.append(ii); cand_ics.append(ci); wins += int(ci > ii)
    inc_mean, cand_mean = float(np.nanmean(inc_ics or [np.nan])), float(np.nanmean(cand_ics or [np.nan]))
    majority = wins >= max(1, len([1 for v in per_tf.values() if np.isfinite(v["cand_mag_ic"])]) // 2 + 1)
    better = np.isfinite(cand_mean) and np.isfinite(inc_mean) and (cand_mean >= inc_mean + IC_MARGIN) and majority

    promoted = False
    if better and PROMOTE:
        bak = MODEL + ".bak." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(MODEL, bak)
        tmp = MODEL + ".tmp"
        candidate.save(tmp); os.replace(tmp, MODEL)  # atomic swap
        promoted = True

    _write({**base, "status": "promoted" if promoted else "held_incumbent",
            "incumbent_mag_ic": inc_mean, "candidate_mag_ic": cand_mean, "ic_margin": IC_MARGIN,
            "candidate_horizon_wins": wins, "n_features_candidate": len(fcols),
            "n_uof_features": len([c for c in fcols if c.startswith("u_of_")]),
            "train_rows": int(len(tr)), "test_rows": int(len(te)),
            "last_retrain": _now() if promoted else None, "per_horizon": per_tf,
            "note": ("candidate beat incumbent — promoted (incumbent backed up)" if promoted else
                     "candidate did not clear the margin — incumbent kept")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
