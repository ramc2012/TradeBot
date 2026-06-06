"""S1 winner-filter synthesis + overfitting check.

Reads the per-trade feature dataset (analysis.s1_feature_research → s1_trades.jsonl)
and distils a small, interpretable filter that captures as much of the +4819%
theoretical max (perfect winner selection) as possible, then validates it honestly:
  • direction-aware univariate quantile→P/L per feature,
  • greedy distillation of a 3–5 rule filter (objective = total captured return %),
  • TIME-SPLIT out-of-sample: fit on the earliest 60% of trading days, test on the
    latest 40% (so the "overfitting" the user warned about is measured, not hidden),
  • a gradient-boosted winner-classifier (if sklearn present) as an independent
    cross-check of feature importance + a model-filtered OOS P/L.

  docker run --rm --memory=900m -e PYTHONPATH=/app -w /app -v /opt/TradeBot/backend:/app \
    tradebot-backend:latest python -m analysis.s1_filter_synth
"""
from __future__ import annotations

import json
import os
import statistics

DATA = os.environ.get("FEAT_OUT", "/app/runtime/s1_trades.jsonl")
# STRUCTURAL, premium-scale-invariant features only. dte_days is EXCLUDED: over a
# single June expiry cycle it monotonically decreases, so it is a calendar proxy for
# "early in the window" (the 06-01 windfall) and does not generalise — the greedy
# search latched onto dte>=29 and the OOS test had 0 trades. Raw MACD values scale
# with the premium level, so we normalise slope/macd/hist by entry_premium.
NUMERIC = ["macd30_slope_n", "macd30_n", "macd30_hist_n", "tod_min",
           "ss_macd_dir", "ss_ret6_dir", "rsi14", "macd15_hist_n"]


def _load():
    rows = []
    with open(DATA) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            side = 1.0 if t.get("ot") == "CE" else -1.0
            for raw, dst in (("ss_macd", "ss_macd_dir"), ("ss_ret6", "ss_ret6_dir")):
                v = t.get(raw)
                t[dst] = (side * v) if isinstance(v, (int, float)) else None
            prem = t.get("entry_premium")
            for raw, dst in (("macd30_slope", "macd30_slope_n"), ("macd30", "macd30_n"),
                             ("macd30_hist", "macd30_hist_n"), ("macd15_hist", "macd15_hist_n")):
                v = t.get(raw)
                t[dst] = (v / prem) if (isinstance(v, (int, float)) and isinstance(prem, (int, float)) and prem) else None
            rows.append(t)
    return rows


def _per_day(rows, rules):
    from collections import defaultdict
    bd = defaultdict(list)
    for r in _apply(rows, rules):
        bd[r["day"]].append(r["ret"])
    return {d: _stat(v) for d, v in sorted(bd.items())}


def _stat(rets):
    if not rets:
        return {"n": 0, "tot": 0.0, "avg": 0.0, "win": 0.0}
    return {"n": len(rets), "tot": round(sum(rets), 1), "avg": round(sum(rets) / len(rets), 3),
            "win": round(100 * sum(1 for r in rets if r > 0) / len(rets), 1)}


def _feat_quantiles(rows, feat):
    vals = sorted(r[feat] for r in rows if isinstance(r.get(feat), (int, float)))
    if len(vals) < 20:
        return None
    qs = [vals[int(len(vals) * q)] for q in (0.2, 0.4, 0.6, 0.8)]
    buckets = [[] for _ in range(5)]
    for r in rows:
        v = r.get(feat)
        if not isinstance(v, (int, float)):
            continue
        b = sum(1 for q in qs if v >= q)
        buckets[b].append(r["ret"])
    return qs, [_stat(b) for b in buckets]


def _apply(rows, rules):
    """rules: list of (feat, op, thr). op in {'<=','>='}."""
    out = []
    for r in rows:
        ok = True
        for feat, op, thr in rules:
            v = r.get(feat)
            if not isinstance(v, (int, float)):
                ok = False; break
            if op == "<=" and not (v <= thr):
                ok = False; break
            if op == ">=" and not (v >= thr):
                ok = False; break
        if ok:
            out.append(r)
    return out


def _greedy(rows, min_keep=60):
    """Greedily add (feat, op, thr) rules that maximise total captured return while
    keeping >= min_keep trades. Candidate thresholds = feature quintile edges."""
    rules = []
    kept = rows
    base = sum(r["ret"] for r in kept)
    improved = True
    while improved and len(rules) < 5:
        improved = False
        best = None
        for feat in NUMERIC:
            vals = sorted(r[feat] for r in kept if isinstance(r.get(feat), (int, float)))
            if len(vals) < min_keep:
                continue
            cands = {vals[int(len(vals) * q)] for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)}
            for thr in cands:
                for op in ("<=", ">="):
                    sub = _apply(kept, [(feat, op, thr)])
                    if len(sub) < min_keep:
                        continue
                    tot = sum(r["ret"] for r in sub)
                    if best is None or tot > best[0]:
                        best = (tot, feat, op, thr, sub)
        if best and best[0] > base + 5:  # require a real improvement
            base = best[0]; rules.append((best[1], best[2], round(best[3], 4))); kept = best[4]; improved = True
    return rules, kept


def main():
    rows = _load()
    rets_all = [r["ret"] for r in rows]
    winners = [r for r in rets_all if r > 0]
    print(f"loaded {len(rows)} trades | ACTUAL {_stat(rets_all)} | THEO-MAX winners tot={round(sum(winners),1)}%", flush=True)

    print("\n=== direction-aware univariate quintile P/L ===", flush=True)
    for feat in NUMERIC:
        q = _feat_quantiles(rows, feat)
        if not q:
            continue
        edges, st = q
        print(f"  {feat:<14} edges={[round(e,3) for e in edges]}", flush=True)
        print(f"     Q1..Q5 avg%: {[s['avg'] for s in st]}  n: {[s['n'] for s in st]}", flush=True)

    # time split: earliest 60% of days = train, latest 40% = test
    days = sorted({r["day"] for r in rows})
    cut = days[int(len(days) * 0.6)] if len(days) > 3 else days[-1]
    train = [r for r in rows if r["day"] < cut]
    test = [r for r in rows if r["day"] >= cut]
    print(f"\n=== time-split: train days < {cut} (n={len(train)}), test >= (n={len(test)}) ===", flush=True)

    rules, kept_tr = _greedy(train)
    print(f"DISTILLED FILTER (fit on train): {rules}", flush=True)
    print(f"  TRAIN filtered: {_stat([r['ret'] for r in kept_tr])}  (all-train {_stat([r['ret'] for r in train])})", flush=True)
    kept_te = _apply(test, rules)
    print(f"  TEST  filtered: {_stat([r['ret'] for r in kept_te])}  (all-test  {_stat([r['ret'] for r in test])})", flush=True)
    kept_all = _apply(rows, rules)
    print(f"  FULL  filtered: {_stat([r['ret'] for r in kept_all])}  capture={round(sum(r['ret'] for r in kept_all),1)}% of theo {round(sum(winners),1)}%", flush=True)

    # ── per-day robustness of the dominant STRUCTURAL filters (the real OOS test
    #    given only ~5 days: a robust filter should be positive on MOST days) ──
    print("\n=== per-day robustness of structural filters ===", flush=True)
    slope_edges = _feat_quantiles(rows, "macd30_slope_n")
    thr = slope_edges[0][2] if slope_edges else 0.0  # ~60th pct of normalized slope
    candidates = {
        f"slope_n<={round(thr,4)} (avoid steep/chasing crosses)": [("macd30_slope_n", "<=", thr)],
        f"slope_n<={round(thr,4)} & tod_min>=105 (also: afternoon)": [("macd30_slope_n", "<=", thr), ("tod_min", ">=", 105)],
        f"slope_n<={round(thr,4)} & ss_ret6_dir>=1.0 (also: spot momentum)": [("macd30_slope_n", "<=", thr), ("ss_ret6_dir", ">=", 1.0)],
    }
    for label, rules in candidates.items():
        kept = _apply(rows, rules)
        pd = _per_day(rows, rules)
        pos_days = sum(1 for d, s in pd.items() if s["avg"] > 0 and s["n"] >= 3)
        tot_days = sum(1 for d, s in pd.items() if s["n"] >= 3)
        print(f"  [{label}] FULL {_stat([r['ret'] for r in kept])} | +days {pos_days}/{tot_days}", flush=True)
        print(f"     per-day avg%: { {d: s['avg'] for d, s in pd.items() if s['n'] >= 3} }", flush=True)

    # GBM cross-check
    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier
        feats = [f for f in NUMERIC]
        def X(rs):
            return np.array([[float(r[f]) if isinstance(r.get(f), (int, float)) else 0.0 for f in feats] for r in rs])
        ytr = np.array([1 if r["ret"] > 0 else 0 for r in train])
        m = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05, random_state=0)
        m.fit(X(train), ytr)
        imp = sorted(zip(feats, m.feature_importances_), key=lambda x: -x[1])[:8]
        print("\n=== GBM winner-classifier feature importance (train) ===", flush=True)
        print("  " + ", ".join(f"{f}:{round(w,3)}" for f, w in imp), flush=True)
        prob = m.predict_proba(X(test))[:, 1]
        for thr in (0.5, 0.55, 0.6):
            sel = [test[i]["ret"] for i in range(len(test)) if prob[i] >= thr]
            print(f"  TEST trade only if P(win)>={thr}: {_stat(sel)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"\n(GBM skipped: {exc})", flush=True)


if __name__ == "__main__":
    main()
