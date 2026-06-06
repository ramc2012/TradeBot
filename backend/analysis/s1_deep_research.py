"""S1 DEEP research — 6 weeks / 13 expiries / 221 underlyings (30-minute option data).

Goes beyond the June-only 3m study: uses the full 30-minute option_premium_candles
history (2026-04-23 → 06-06, 13 expiries) so filters can be validated with a real
EXPIRY-BASED walk-forward (train on earlier expiries, test on later) — the genuine
OOS the single-cycle June run could not give.

Richer features per entry (no lookahead):
  premium MACD : macd_n, slope_n, hist_n, roc9_n (ROC(9)/premium), rsi14
  cross shape  : cross_depth_n (how far below zero the macd was, last 6 bars),
                 bars_below (how long macd stayed < 0 before the cross — "cross location"),
                 macd60_sign (higher-TF 60m MACD agreement — cross-timeframe)
  synth-spot   : adx, di_diff(+DI−−DI), ema_spread_pct, mom3, mom8, rv_pct, roc9,
                 trend_agree   (directional-options-style features on spot≈strike+CE−PE)
  context      : tod_min, dte_days, dow, ot, kind

Entry: 30m premium-MACD zero-cross-up (closed bars). Exits: −25% hard → trail 25%-of-peak
after +50%, flip on opposite cross, same-side reversal, EOD square-off. ATM per day.

Outputs theoretical max, the labeled dataset, direction-aware univariate, and a greedy
filter distilled on TRAIN expiries + measured on TEST expiries (+ per-expiry robustness).

  docker run --rm --network tradebot_default --memory=1800m -e PYTHONPATH=/app -w /app \
    -v /opt/TradeBot/backend:/app tradebot-backend:latest python -m analysis.s1_deep_research
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import timedelta, timezone

import asyncpg

from analysis.macd_engine import compute_ema, compute_macd

try:
    from analytics.technicals import compute_adx
except Exception:  # noqa: BLE001
    compute_adx = None

DSN = os.environ.get("AUDIT_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
OUT = os.environ.get("DEEP_OUT", "/app/runtime/s1_deep_trades.jsonl")
IST = timezone(timedelta(hours=5, minutes=30))
FAST, SLOW, SIG = 12, 26, 9
HARD_STOP, TRAIL_ACT, TRAIL_GB = 0.25, 0.50, 0.25
RTH_OPEN, RTH_CLOSE = 9 * 60 + 15, 15 * 60 + 30


def _ist(ts):
    return ts.astimezone(IST)


def _rth(ts):
    m = _ist(ts).hour * 60 + _ist(ts).minute
    return RTH_OPEN <= m <= RTH_CLOSE


def _roc(closes, n):
    return [None if i < n or not closes[i - n] else (closes[i] - closes[i - n]) / closes[i - n] * 100.0 for i in range(len(closes))]


def _rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 - 100 / (1 + (g / l if l else 999))
    for i in range(n + 1, len(c)):
        ch = c[i] - c[i - 1]
        g = (g * (n - 1) + max(ch, 0)) / n
        l = (l * (n - 1) + max(-ch, 0)) / n
        out[i] = 100 - 100 / (1 + (g / l if l else 999))
    return out


def _stat(r):
    if not r:
        return {"n": 0, "tot": 0.0, "avg": 0.0, "win": 0.0}
    return {"n": len(r), "tot": round(sum(r), 1), "avg": round(sum(r) / len(r), 3),
            "win": round(100 * sum(1 for x in r if x > 0) / len(r), 1)}


async def _c30(conn, u, e, k, ot):
    rows = await conn.fetch(
        """SELECT time, open, high, low, close, volume FROM option_premium_candles
           WHERE interval='30minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
           ORDER BY time ASC""", u, e, k, ot)
    return [(r["time"], float(r["open"] or 0), float(r["high"] or 0), float(r["low"] or 0),
             float(r["close"]), float(r["volume"] or 0)) for r in rows if r["close"] and _rth(r["time"])]


def _macd_arrays(closes):
    m, s, h = compute_macd(closes, FAST, SLOW, SIG)
    return m, s, h


def _cross_shape(macd, i):
    """At a zero-cross-up at bar i: depth (most-negative macd in last 6), bars_below."""
    lookback = macd[max(0, i - 6):i]
    depth = min((x for x in lookback if x is not None), default=None)
    bb = 0
    j = i - 1
    while j >= 0 and macd[j] is not None and macd[j] < 0:
        bb += 1; j -= 1
    return depth, bb


def _macd60_sign(closes30, idx_time, times30):
    """60m MACD sign at the 30m bar's time (resample 30m→60m by pairing)."""
    # build 60m closes = every 2nd 30m close (coarse); macd sign at/just before idx_time
    pairs = []
    last60 = None
    for j, t in enumerate(times30):
        b = _ist(t).replace(minute=0, second=0, microsecond=0)
        if last60 != b:
            pairs.append(closes30[j]); last60 = b
        else:
            pairs[-1] = closes30[j]
    if len(pairs) < SLOW + 2:
        return None
    m, _s, _h = compute_macd(pairs, FAST, SLOW, SIG)
    return (1 if (m[-1] or 0) > 0 else -1) if m[-1] is not None else None


def _synth_feats(ce_day, pe_day, strike):
    """Directional-style features on synth-spot (strike + CE−PE) at 30m, per bar time."""
    # align CE/PE by time
    pe_map = {t: (o, h, l, c) for (t, o, h, l, c, v) in pe_day}
    ts, sc, sh, sl = [], [], [], []
    for (t, o, h, l, c, v) in ce_day:
        if t not in pe_map:
            continue
        po, ph, pl, pc = pe_map[t]
        ts.append(t)
        sc.append(strike + (c - pc))
        sh.append(strike + (h - pl))
        sl.append(strike + (l - ph))
    if len(sc) < SLOW + 2:
        return {}
    ef= compute_ema(sc, FAST); es = compute_ema(sc, SLOW)
    m, _s, _h = compute_macd(sc, FAST, SLOW, SIG)
    roc9 = _roc(sc, 9)
    mom3 = [None if i < 3 or not sc[i-3] else (sc[i]-sc[i-3])/sc[i-3]*100 for i in range(len(sc))]
    mom8 = [None if i < 8 or not sc[i-8] else (sc[i]-sc[i-8])/sc[i-8]*100 for i in range(len(sc))]
    rets = [0.0] + [(sc[i]-sc[i-1])/sc[i-1] if sc[i-1] else 0.0 for i in range(1, len(sc))]
    rv = [None]*len(sc)
    for i in range(20, len(sc)):
        w = rets[i-19:i+1]; mu = sum(w)/len(w); rv[i] = (sum((x-mu)**2 for x in w)/len(w))**0.5
    rvv = [x for x in rv if x is not None]
    rvmin, rvmax = (min(rvv), max(rvv)) if rvv else (0, 1)
    rvpct = [None if rv[i] is None else (rv[i]-rvmin)/max(rvmax-rvmin, 1e-9) for i in range(len(sc))]
    adx = pdi = mdi = [None]*len(sc)
    if compute_adx is not None:
        try:
            adx, pdi, mdi = compute_adx(sh, sl, sc, 14)
        except Exception:  # noqa: BLE001
            pass
    out = {}
    for i, t in enumerate(ts):
        out[t] = {
            "ss_macd": m[i], "ss_roc9": roc9[i], "mom3": mom3[i], "mom8": mom8[i],
            "ema_spread_pct": ((ef[i]-es[i])/sc[i]) if (ef[i] is not None and es[i] is not None and sc[i]) else None,
            "rv_pct": rvpct[i],
            "adx": adx[i] if i < len(adx) else None,
            "di_diff": (pdi[i]-mdi[i]) if (i < len(pdi) and pdi[i] is not None and mdi[i] is not None) else None,
        }
    return out


def _simulate(u, e, d, strike, ce_full, pe_full, trades):
    """ce_full/pe_full = full-contract 30m bars (for warmup); trade only on day d."""
    ce_c = [c for (t, o, h, l, c, v) in ce_full]
    pe_c = [c for (t, o, h, l, c, v) in pe_full]
    ce_t = [t for (t, *_x) in ce_full]
    pe_t = [t for (t, *_x) in pe_full]
    if len(ce_c) < SLOW + SIG + 2 or len(pe_c) < SLOW + SIG + 2:
        return
    ce_m, ce_s, ce_h = _macd_arrays(ce_c)
    pe_m, pe_s, pe_h = _macd_arrays(pe_c)
    ce_roc = _roc(ce_c, 9); pe_roc = _roc(pe_c, 9)
    ce_rsi = _rsi(ce_c); pe_rsi = _rsi(pe_c)
    ce_day = [(t, o, h, l, c, v) for (t, o, h, l, c, v) in ce_full if t.astimezone(IST).date() == d]
    pe_day = [(t, o, h, l, c, v) for (t, o, h, l, c, v) in pe_full if t.astimezone(IST).date() == d]
    synth = _synth_feats(ce_full, pe_full, strike)
    dte = (e - d).days

    def idx_of(side_t, t):
        return side_t.index(t) if t in side_t else None

    # iterate day bars in time order, union of CE/PE day timestamps
    day_ts = sorted({t for (t, *_x) in ce_day} | {t for (t, *_x) in pe_day})
    pos = None; fired = set()

    def side_arrays(ot):
        return (ce_full, ce_m, ce_s, ce_h, ce_roc, ce_rsi, ce_t, ce_c) if ot == "CE" else (pe_full, pe_m, pe_s, pe_h, pe_roc, pe_rsi, pe_t, pe_c)

    def bar(ot, t):
        full, *_ = side_arrays(ot)
        for (tt, o, h, l, c, v) in (ce_day if ot == "CE" else pe_day):
            if tt == t:
                return o, h, l, c, v
        return None

    def cross_up(ot, t):
        _f, m, *_ = side_arrays(ot); _t = side_arrays(ot)[6]
        i = idx_of(_t, t)
        return i is not None and i >= 1 and m[i] is not None and m[i - 1] is not None and m[i - 1] < 0 < m[i]

    def cross_dn(ot, t):
        _f, m, *_ = side_arrays(ot); _t = side_arrays(ot)[6]
        i = idx_of(_t, t)
        return i is not None and i >= 1 and m[i] is not None and m[i - 1] is not None and m[i - 1] > 0 > m[i]

    def feat(ot, t, kind, entry):
        full, m, s, h, roc, rsi, tt, cc = side_arrays(ot)
        i = idx_of(tt, t)
        depth, bb = _cross_shape(m, i) if i is not None else (None, 0)
        m60 = _macd60_sign(cc, t, tt)
        sf = synth.get(t, {})
        ssm = sf.get("ss_macd")
        side = 1.0 if ot == "CE" else -1.0
        agree = None if ssm is None else (1 if side * ssm > 0 else 0)
        l = _ist(t)
        return {
            "underlying": u, "expiry": str(e), "day": str(d), "dow": l.weekday(), "ot": ot, "kind": kind,
            "tod_min": l.hour * 60 + l.minute - RTH_OPEN, "dte_days": dte,
            "macd_n": (m[i] / entry) if (i is not None and m[i] is not None and entry) else None,
            "slope_n": ((m[i] - m[i - 1]) / entry) if (i is not None and i >= 1 and m[i] is not None and m[i - 1] is not None and entry) else None,
            "hist_n": (h[i] / entry) if (i is not None and h[i] is not None and entry) else None,
            "roc9_n": (roc[i] / 100.0) if (i is not None and roc[i] is not None) else None,
            "rsi14": rsi[i] if i is not None else None,
            "cross_depth_n": (depth / entry) if (depth is not None and entry) else None,
            "bars_below": bb, "macd60_sign": m60,
            "ss_roc9": sf.get("ss_roc9"), "ss_mom3": sf.get("mom3"), "ss_mom8": sf.get("mom8"),
            "ema_spread_pct": sf.get("ema_spread_pct"), "rv_pct": sf.get("rv_pct"),
            "adx": sf.get("adx"), "di_diff_dir": (side * sf["di_diff"]) if sf.get("di_diff") is not None else None,
            "trend_agree": agree, "ss_macd_dir": (side * ssm) if ssm is not None else None,
        }

    def stop(p):
        return p["peak"] * (1 - TRAIL_GB) if p["peak"] >= p["entry"] * (1 + TRAIL_ACT) else p["entry"] * (1 - HARD_STOP)

    def cl(p, px):
        rec = dict(p["feat"]); rec["ret"] = round((px - p["entry"]) / p["entry"] * 100.0, 3); trades.append(rec)

    for t in day_ts:
        bkt = _ist(t).replace(minute=0 if _ist(t).minute < 30 else 30, second=0, microsecond=0)
        if pos is not None:
            b = bar(pos["ot"], t)
            if b is not None:
                o, h, l, c, v = b
                pos["peak"] = max(pos["peak"], h)
                st = stop(pos)
                if l <= st:
                    cl(pos, st); pos = None
                elif cross_up(("PE" if pos["ot"] == "CE" else "CE"), t) and ((("PE" if pos["ot"] == "CE" else "CE"), bkt) not in fired):
                    oot = "PE" if pos["ot"] == "CE" else "CE"
                    fired.add((oot, bkt)); cl(pos, c)
                    ob = bar(oot, t)
                    if ob:
                        pos = {"ot": oot, "entry": ob[3], "peak": ob[3], "feat": feat(oot, t, "flip", ob[3])}
                    else:
                        pos = None
                    continue
                elif cross_dn(pos["ot"], t):
                    cl(pos, c); pos = None
        if pos is None:
            for ot in ("CE", "PE"):
                if cross_up(ot, t) and (ot, bkt) not in fired:
                    b = bar(ot, t)
                    if b:
                        fired.add((ot, bkt))
                        pos = {"ot": ot, "entry": b[3], "peak": b[3], "feat": feat(ot, t, "primary", b[3])}
                    break
    if pos is not None:
        last = max((t for (t, *_x) in (ce_day if pos["ot"] == "CE" else pe_day)), default=None)
        if last is not None:
            b = bar(pos["ot"], last)
            if b:
                cl(pos, b[3])


async def main():
    conn = await asyncpg.connect(DSN)
    trades = []
    try:
        unders = [r["underlying"] for r in await conn.fetch(
            "SELECT DISTINCT underlying FROM option_premium_candles WHERE interval='30minute'")]
        print(f"underlyings (30m): {len(unders)}", flush=True)
        for ui, u in enumerate(unders):
            exps = [r["expiry"] for r in await conn.fetch(
                "SELECT DISTINCT expiry FROM option_premium_candles WHERE interval='30minute' AND underlying=$1 ORDER BY expiry", u)]
            for e in exps:
                strikes = [float(r["strike"]) for r in await conn.fetch(
                    "SELECT DISTINCT strike FROM option_premium_candles WHERE interval='30minute' AND underlying=$1 AND expiry=$2", u, e)]
                ce_s = {k: await _c30(conn, u, e, k, "CE") for k in strikes}
                pe_s = {k: await _c30(conn, u, e, k, "PE") for k in strikes}
                days = sorted({t.astimezone(IST).date() for k in strikes for (t, *_x) in ce_s.get(k, [])})
                for d in days:
                    atm, best = None, None
                    for k in strikes:
                        cb = [c for (t, o, h, l, c, v) in ce_s.get(k, []) if t.astimezone(IST).date() == d]
                        pb = [c for (t, o, h, l, c, v) in pe_s.get(k, []) if t.astimezone(IST).date() == d]
                        if cb and pb and (best is None or abs(cb[0] - pb[0]) < best):
                            best, atm = abs(cb[0] - pb[0]), k
                    if atm is None:
                        continue
                    _simulate(u, e, d, atm, ce_s[atm], pe_s[atm], trades)
            if (ui + 1) % 50 == 0:
                print(f"  ...{ui+1}/{len(unders)} underlyings, {len(trades)} trades", flush=True)

        rets = [t["ret"] for t in trades]
        wins = [r for r in rets if r > 0]
        print(f"\nTRADES: {len(trades)} | ACTUAL {_stat(rets)} | THEO-MAX winners tot={round(sum(wins),1)}% n={len(wins)}", flush=True)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as f:
            for t in trades:
                f.write(json.dumps(t, default=str) + "\n")
        print(f"wrote {len(trades)} -> {OUT}", flush=True)

        # expiry-based walk-forward: train = earliest 8 expiries, test = rest
        exps_sorted = sorted({t["expiry"] for t in trades})
        k = max(1, int(len(exps_sorted) * 0.6))
        train_exp = set(exps_sorted[:k]); test_exp = set(exps_sorted[k:])
        train = [t for t in trades if t["expiry"] in train_exp]
        test = [t for t in trades if t["expiry"] in test_exp]
        print(f"\nexpiries={len(exps_sorted)} train={len(train_exp)}({len(train)}) test={len(test_exp)}({len(test)})", flush=True)

        feats = ["slope_n", "ss_roc9", "roc9_n", "ss_macd_dir", "di_diff_dir", "adx", "ema_spread_pct",
                 "ss_mom3", "ss_mom8", "rv_pct", "tod_min", "cross_depth_n", "bars_below", "macd60_sign",
                 "macd_n", "hist_n", "rsi14", "trend_agree"]
        print("\n=== univariate (train) median split avg% ===", flush=True)
        import statistics as st
        for fz in feats:
            vals = [(t[fz], t["ret"]) for t in train if isinstance(t.get(fz), (int, float))]
            if len(vals) < 50:
                continue
            med = st.median(v for v, _ in vals)
            hi = [r for v, r in vals if v >= med]; lo = [r for v, r in vals if v < med]
            print(f"  {fz:<16} >=med {_stat(hi)['avg']:>7}  <med {_stat(lo)['avg']:>7}  (n {len(vals)})", flush=True)

        def apply(rows, rules):
            return [r for r in rows if all(isinstance(r.get(f), (int, float)) and (r[f] <= th if op == "<=" else r[f] >= th) for f, op, th in rules)]

        # greedy on TRAIN, objective = total return, min 80 trades
        rules, kept = [], train
        base = sum(r["ret"] for r in kept)
        for _ in range(5):
            bestc = None
            for fz in feats:
                vals = sorted(r[fz] for r in kept if isinstance(r.get(fz), (int, float)))
                if len(vals) < 80:
                    continue
                for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
                    th = vals[int(len(vals) * q)]
                    for op in ("<=", ">="):
                        sub = apply(kept, [(fz, op, th)])
                        if len(sub) >= 80 and (bestc is None or sum(r["ret"] for r in sub) > bestc[0]):
                            bestc = (sum(r["ret"] for r in sub), fz, op, round(th, 5), sub)
            if bestc and bestc[0] > base + 10:
                base = bestc[0]; rules.append((bestc[1], bestc[2], bestc[3])); kept = bestc[4]
            else:
                break
        print(f"\nDISTILLED (train): {rules}", flush=True)
        print(f"  TRAIN {_stat([r['ret'] for r in apply(train, rules)])}  (all-train {_stat([r['ret'] for r in train])})", flush=True)
        print(f"  TEST  {_stat([r['ret'] for r in apply(test, rules)])}  (all-test  {_stat([r['ret'] for r in test])})", flush=True)
        full = apply(trades, rules)
        print(f"  FULL  {_stat([r['ret'] for r in full])}  capture={round(sum(r['ret'] for r in full),1)}% of theo {round(sum(wins),1)}%", flush=True)
        per_exp = defaultdict(list)
        for r in full:
            per_exp[r["expiry"]].append(r["ret"])
        pos_e = sum(1 for e_, v in per_exp.items() if sum(v) > 0 and len(v) >= 5)
        tot_e = sum(1 for e_, v in per_exp.items() if len(v) >= 5)
        print(f"  per-expiry: +{pos_e}/{tot_e}  " + str({e_: _stat(v)['avg'] for e_, v in sorted(per_exp.items()) if len(v) >= 5}), flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
