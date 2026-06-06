"""S1 profit-maximisation research: theoretical max + per-trade feature dataset.

Runs the refined S1 backtest (analysis.s1_walkforward semantics: ATM CE/PE, intra-bar
30m premium-MACD zero-cross, −25%→trail-after-+50%, flip, 15m re-entry, intraday) but
at every entry captures a no-lookahead FEATURE VECTOR and, on close, the realised
return. Emits:
  • the THEORETICAL MAX (sum of all winning trades = perfect winner selection),
  • a JSONL of {features…, ret} per trade for downstream filter research.

Features (all computable at entry, no lookahead):
  meta      : underlying, day, dow, ot(CE/PE), kind, tod_min (since 09:15), dte_days
  option    : entry_premium, macd30, macd30_slope, macd30_hist, rsi14, vol_z,
              macd15, macd15_hist
  synth-spot: ss = strike + (CE−CE_close? no) = strike + (CE_close − PE_close)   [put/call parity]
              ss_macd30, ss_macd30_slope, ss_ret6 (last-6×3m return), ss_ema_spread,
              trend_agree (+1 if synth-spot trend matches the option side)
  iv-proxy  : atm_straddle = CE+PE (rich/cheap premium proxy), straddle_z

  docker run --rm --network tradebot_default --memory=1500m -e PYTHONPATH=/app -w /app \
    -v /opt/TradeBot/backend:/app tradebot-backend:latest python -m analysis.s1_feature_research
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta, timezone

import asyncpg

from analysis.macd_engine import compute_ema, compute_macd

DSN = os.environ.get("AUDIT_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
OUT = os.environ.get("FEAT_OUT", "/sniper/s1_trades.jsonl")  # /sniper is a persistent host mount
IST = timezone(timedelta(hours=5, minutes=30))
JUNE_START, JUNE_END = date(2026, 6, 1), date(2026, 7, 1)
FAST, SLOW, SIG = 12, 26, 9
HARD_STOP, TRAIL_ACT, TRAIL_GB = 0.25, 0.50, 0.25
RTH_OPEN, RTH_CLOSE = 9 * 60 + 15, 15 * 60 + 30
A_F, A_S = 2 / (FAST + 1), 2 / (SLOW + 1)


def _b(ts, m):
    l = ts.astimezone(IST)
    return l.replace(minute=(l.minute // m) * m, second=0, microsecond=0)


def _rth(ts):
    l = ts.astimezone(IST); mm = l.hour * 60 + l.minute
    return RTH_OPEN <= mm <= RTH_CLOSE


def _rsi(closes, n=14):
    if len(closes) <= n:
        return [None] * len(closes)
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0); losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    out[n] = 100 - 100 / (1 + (ag / al if al else 999))
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


def _macd_full(closes):
    macd, sig, hist = compute_macd(closes, FAST, SLOW, SIG)
    return macd, sig, hist


async def _c3m(conn, u, e, k, ot):
    rows = await conn.fetch(
        """SELECT time, open, high, low, close, volume FROM option_premium_candles
           WHERE interval='3minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
           ORDER BY time ASC""", u, e, k, ot)
    return [(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def _prep_side(bars):
    """Per-3m features for one option side: forming 30m macd + closed-bar context.
    Returns dict ts -> {low, high, close, vol, fm, pfm, pcm, macd30_closed, sig30_closed,
    rsi30_closed, macd15, macd15_hist}."""
    seq = sorted([(t, o, h, l, c, v) for (t, o, h, l, c, v) in bars if c and _rth(t)], key=lambda x: x[0])
    # closed 30m closes + their index per ts
    closed, last_in_b, cur_b = [], None, None
    ema_f_closed, ema_s_closed = [], []
    # precompute 30m closed macd/sig/rsi progressively
    out = {}
    pfm = None
    # build closed 30m close list aligned: we need, at each 3m bar, the closed-bar macd/sig/rsi
    # (computed over closed bars so far) + forming
    for (t, o, h, l, c, v) in seq:
        b = _b(t, 30)
        if cur_b is None:
            cur_b = b
        elif b != cur_b:
            if last_in_b is not None:
                closed.append(last_in_b)
            cur_b = b
        last_in_b = float(c)
        fm = pcm = m30 = s30 = r30 = None
        if len(closed) >= SLOW:
            ef = compute_ema(closed, FAST)[-1]; es = compute_ema(closed, SLOW)[-1]
            if ef is not None and es is not None:
                pcm = ef - es
                fm = (A_F * float(c) + (1 - A_F) * ef) - (A_S * float(c) + (1 - A_S) * es)
            mm, ss_, hh = _macd_full(closed)
            m30, s30 = mm[-1], ss_[-1]
            rr = _rsi(closed)
            r30 = rr[-1] if rr else None
        out[t] = {"low": float(l) if l else float(c), "high": float(h) if h else float(c),
                  "close": float(c), "vol": float(v or 0), "fm": fm, "pfm": pfm,
                  "pcm": pcm, "m30": m30, "s30": s30, "r30": r30}
        pfm = fm
    return out


def _synth_spot_15m(ce_bars, pe_bars, strike):
    """Synthetic spot path (strike + CE−PE) resampled to 30m; returns per-30m-bucket dict
    of {macd, macd_slope, ret6, ema_spread} keyed by 30m bucket ts."""
    ce30, pe30 = {}, {}
    for (t, o, h, l, c, v) in ce_bars:
        if c and _rth(t): ce30[_b(t, 30)] = float(c)
    for (t, o, h, l, c, v) in pe_bars:
        if c and _rth(t): pe30[_b(t, 30)] = float(c)
    buckets = sorted(set(ce30) & set(pe30))
    ss = [strike + (ce30[b] - pe30[b]) for b in buckets]
    if len(ss) < SLOW + 2:
        return {}
    macd, sig, hist = compute_macd(ss, FAST, SLOW, SIG)
    ef = compute_ema(ss, FAST); es = compute_ema(ss, SLOW)
    out = {}
    for i, b in enumerate(buckets):
        ret6 = ((ss[i] - ss[i - 6]) / ss[i - 6] * 100.0) if i >= 6 and ss[i - 6] else None
        spread = (ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
        out[b] = {"macd": macd[i], "slope": (macd[i] - macd[i - 1]) if (i and macd[i] is not None and macd[i - 1] is not None) else None,
                  "ret6": ret6, "spread": spread}
    return out


def _stats(rets):
    if not rets:
        return {"n": 0, "tot": 0.0, "avg": 0.0, "win": 0.0}
    return {"n": len(rets), "tot": round(sum(rets), 1), "avg": round(sum(rets) / len(rets), 3),
            "win": round(100 * sum(1 for r in rets if r > 0) / len(rets), 1)}


async def main():
    conn = await asyncpg.connect(DSN)
    trades = []
    try:
        unders = [r["underlying"] for r in await conn.fetch(
            "SELECT DISTINCT underlying FROM option_premium_candles WHERE interval='3minute' AND expiry>=$1 AND expiry<$2",
            JUNE_START, JUNE_END)]
        for u in unders:
            exps = [r["expiry"] for r in await conn.fetch(
                "SELECT DISTINCT expiry FROM option_premium_candles WHERE interval='3minute' AND underlying=$1 AND expiry>=$2 AND expiry<$3 ORDER BY expiry",
                u, JUNE_START, JUNE_END)]
            for e in exps:
                strikes = [float(r["strike"]) for r in await conn.fetch(
                    "SELECT DISTINCT strike FROM option_premium_candles WHERE interval='3minute' AND underlying=$1 AND expiry=$2", u, e)]
                ce_s, pe_s = {}, {}
                for k in strikes:
                    ce_s[k] = await _c3m(conn, u, e, k, "CE")
                    pe_s[k] = await _c3m(conn, u, e, k, "PE")
                days = sorted({_b(t, 30).date() for k in strikes for (t, *_x) in ce_s.get(k, [])})
                for d in days:
                    atm, best = None, None
                    for k in strikes:
                        cb = [c for (t, o, h, l, c, v) in ce_s.get(k, []) if _b(t, 30).date() == d and c]
                        pb = [c for (t, o, h, l, c, v) in pe_s.get(k, []) if _b(t, 30).date() == d and c]
                        if cb and pb and (best is None or abs(cb[0] - pb[0]) < best):
                            best, atm = abs(cb[0] - pb[0]), k
                    if atm is None:
                        continue
                    _simulate_capture(u, d, e, atm, ce_s[atm], pe_s[atm], trades)

        # ── results ──────────────────────────────────────────────────────
        rets = [t["ret"] for t in trades]
        winners = [r for r in rets if r > 0]
        losers = [r for r in rets if r <= 0]
        print(f"trades: {len(trades)}", flush=True)
        print("ACTUAL (all trades):", _stats(rets), flush=True)
        print(f"THEORETICAL MAX (only winners): n={len(winners)} tot={round(sum(winners),1)}% "
              f"avg={round(sum(winners)/len(winners),2) if winners else 0}% "
              f"(losers avoided: n={len(losers)} tot={round(sum(losers),1)}%)", flush=True)
        print(f"  capture ratio if perfect = {round(sum(winners),1)}% vs actual {round(sum(rets),1)}%", flush=True)
        # quick univariate hints
        import os as _os
        _os.makedirs(_os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as f:
            for t in trades:
                f.write(json.dumps(t, default=str) + "\n")
        print(f"wrote {len(trades)} trade-feature rows -> {OUT}", flush=True)
        # a couple of headline univariate splits to confirm signal exists
        for feat in ("trend_agree", "ss_macd", "tod_min", "macd30_slope", "dte_days"):
            vals = [(t.get(feat), t["ret"]) for t in trades if t.get(feat) is not None]
            if not vals:
                continue
            med = statistics.median(v for v, _ in vals)
            hi = [r for v, r in vals if v >= med]; lo = [r for v, r in vals if v < med]
            print(f"  split {feat:<14} >=median: {_stats(hi)}  <median: {_stats(lo)}", flush=True)
    finally:
        await conn.close()


def _simulate_capture(u, d, e, strike, ce_bars, pe_bars, trades):
    ce = _prep_side(ce_bars)
    pe = _prep_side(pe_bars)
    ss = _synth_spot_15m(ce_bars, pe_bars, strike)
    dte = (e - d).days if hasattr(e, "year") else None
    # 15m macd per side
    def macd15(bars):
        b = {}
        for (t, o, h, l, c, v) in bars:
            if c and _rth(t): b[_b(t, 15)] = float(c)
        ks = sorted(b); cl = [b[x] for x in ks]
        if len(cl) < SLOW + SIG + 2: return {}, {}
        m, s, h = compute_macd(cl, FAST, SLOW, SIG)
        return ({ks[i]: m[i] for i in range(len(ks))}, {ks[i]: (m[i] - s[i]) if (m[i] is not None and s[i] is not None) else None for i in range(len(ks))})
    ce_m15, ce_h15 = macd15(ce_bars)
    pe_m15, pe_h15 = macd15(pe_bars)

    ts_all = sorted(t for t in set(ce) | set(pe) if t.astimezone(IST).date() == d)
    pos = None; fired = set(); reused = set()

    def cu(m, t):
        v = m.get(t)
        return v and v["fm"] is not None and v["pcm"] is not None and v["pfm"] is not None and v["pcm"] < 0 and v["pfm"] <= 0 < v["fm"]

    def cd(m, t):
        v = m.get(t)
        return v and v["fm"] is not None and v["pcm"] is not None and v["pfm"] is not None and v["pcm"] > 0 and v["pfm"] >= 0 > v["fm"]

    def feat(ot, t, kind):
        m = ce if ot == "CE" else pe
        v = m[t]
        ssb = ss.get(_b(t, 30), {})
        m15 = (ce_m15 if ot == "CE" else pe_m15).get(_b(t, 15))
        h15 = (ce_h15 if ot == "CE" else pe_h15).get(_b(t, 15))
        l = t.astimezone(IST)
        ce_c = ce[t]["close"] if t in ce else None
        pe_c = pe[t]["close"] if t in pe else None
        straddle = (ce_c + pe_c) if (ce_c and pe_c) else None
        ss_macd = ssb.get("macd")
        # trend agreement: CE wants synth-spot rising (ss_macd>0), PE wants falling
        agree = None
        if ss_macd is not None:
            agree = 1 if ((ot == "CE" and ss_macd > 0) or (ot == "PE" and ss_macd < 0)) else 0
        return {
            "underlying": u, "day": str(d), "dow": l.weekday(), "ot": ot, "kind": kind,
            "tod_min": l.hour * 60 + l.minute - RTH_OPEN, "dte_days": dte,
            "entry_premium": v["close"], "macd30": v["fm"],
            "macd30_slope": (v["fm"] - v["pfm"]) if (v["fm"] is not None and v["pfm"] is not None) else None,
            "macd30_hist": (v["m30"] - v["s30"]) if (v["m30"] is not None and v["s30"] is not None) else None,
            "rsi14": v["r30"], "macd15": m15, "macd15_hist": h15,
            "ss_macd": ss_macd, "ss_slope": ssb.get("slope"), "ss_ret6": ssb.get("ret6"),
            "ss_spread": ssb.get("spread"), "trend_agree": agree, "straddle": straddle,
        }

    def stop_level(p):
        return p["peak"] * (1 - TRAIL_GB) if p["peak"] >= p["entry"] * (1 + TRAIL_ACT) else p["entry"] * (1 - HARD_STOP)

    def close(p, exit_px):
        r = (exit_px - p["entry"]) / p["entry"] * 100.0
        rec = dict(p["feat"]); rec["ret"] = round(r, 3); trades.append(rec)

    for t in ts_all:
        bkt = _b(t, 30)
        if pos is not None:
            m = ce if pos["ot"] == "CE" else pe
            opp = pe if pos["ot"] == "CE" else ce
            oot = "PE" if pos["ot"] == "CE" else "CE"
            v = m.get(t)
            if v is not None:
                pos["peak"] = max(pos["peak"], v["high"])
                st = stop_level(pos)
                if v["low"] <= st:
                    close(pos, st); pos = None
                elif cu(opp, t) and (oot, bkt) not in fired:
                    fired.add((oot, bkt)); close(pos, v["close"])
                    pos = {"ot": oot, "entry": opp[t]["close"], "peak": opp[t]["close"], "feat": feat(oot, t, "flip")}
                    continue
                elif cd(m, t):
                    close(pos, v["close"]); pos = None
        if pos is None:
            if cu(ce, t) and ("CE", bkt) not in fired:
                fired.add(("CE", bkt)); pos = {"ot": "CE", "entry": ce[t]["close"], "peak": ce[t]["close"], "feat": feat("CE", t, "primary")}
            elif cu(pe, t) and ("PE", bkt) not in fired:
                fired.add(("PE", bkt)); pos = {"ot": "PE", "entry": pe[t]["close"], "peak": pe[t]["close"], "feat": feat("PE", t, "primary")}
            else:
                for ot, m, m15map, h15map in (("CE", ce, ce_m15, ce_h15), ("PE", pe, pe_m15, pe_h15)):
                    v = m.get(t)
                    if v is None or v["fm"] is None or v["fm"] <= 0:
                        continue
                    bts = _b(t, 15)
                    cur = m15map.get(bts); prv = None
                    ks = sorted(x for x in m15map if x <= bts)
                    if len(ks) >= 2:
                        prv = m15map.get(ks[-2])
                    # 15m macd/signal cross-up within trend
                    crossed = (cur is not None and prv is not None and h15map.get(bts) is not None
                               and h15map.get(bts) > 0 and (h15map.get(ks[-2]) if len(ks) >= 2 else 1) <= 0)
                    if crossed and (ot, bts) not in reused:
                        reused.add((ot, bts))
                        pos = {"ot": ot, "entry": v["close"], "peak": v["close"], "feat": feat(ot, t, "reentry")}
                        break
    if pos is not None:
        m = ce if pos["ot"] == "CE" else pe
        lt = max((t for t in m if t.astimezone(IST).date() == d), default=None)
        if lt is not None:
            close(pos, m[lt]["close"])


if __name__ == "__main__":
    asyncio.run(main())
