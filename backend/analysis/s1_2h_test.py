"""S1 hypothesis test on the 2-HOUR option-premium MACD (+ CE/PE trade list).

Thesis (user): MACD on the option PREMIUM intentionally accounts for theta — when the
premium itself shows MACD strength, a directional move has overcome decay. Test that on
a slower, 2-hour timeframe.

Rules: resample 30m option candles → 2h (session-anchored 120-min OHLC). Enter when the
contract's 2h premium MACD crosses UP through zero (prev<0<curr) — CE or PE, one position
per underlying. Exit: −25% hard stop → trail 25%-of-peak after +50% profit; flip on the
opposite side's 2h cross; same-side 2h MACD cross down. Held across sessions (2h = swing);
squared off at the contract's last bar. ATM = strike with min |CE−PE| at the contract's
first 2h bar.

Prints: aggregate P/L (all + per index), and the FULL CE/PE trade list for the index
underlyings (entry/exit time, premium, return %, exit reason).

  docker run --rm --network tradebot_default --memory=1500m -e PYTHONPATH=/app -w /app \
    -v /opt/TradeBot/backend:/app tradebot-backend:latest python -m analysis.s1_2h_test
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import timedelta, timezone

import asyncpg

from analysis.macd_engine import compute_macd

DSN = os.environ.get("AUDIT_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
IST = timezone(timedelta(hours=5, minutes=30))
FAST, SLOW, SIG = 12, 26, 9
HARD_STOP, TRAIL_ACT, TRAIL_GB = 0.25, 0.50, 0.25
RTH_OPEN, RTH_CLOSE = 9 * 60 + 15, 15 * 60 + 30
TF_MIN = 120  # 2 hours
INDICES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "BANKEX"}
# DATA HYGIENE: option_premium_candles carries phantom Thursday-expiry (06-25) contracts
# for NSE names — NSE actually expires 06-30 (Tuesday); 06-25 is the BSE/SENSEX expiry.
# Restrict each underlying to its real monthly expiry to avoid the contaminated series.
BSE_UNDERS = {"SENSEX", "BANKEX", "SENSEX50"}
NSE_EXPIRY = os.environ.get("NSE_EXPIRY", "2026-06-30")
BSE_EXPIRY = os.environ.get("BSE_EXPIRY", "2026-06-25")


def _valid_expiry(u):
    return BSE_EXPIRY if u in BSE_UNDERS else NSE_EXPIRY


def _ist(ts):
    return ts.astimezone(IST)


def _rth(ts):
    m = _ist(ts).hour * 60 + _ist(ts).minute
    return RTH_OPEN <= m <= RTH_CLOSE


def _tf_bucket(ts):
    """Session-anchored 2h bucket key: 09:15 + floor(min_since_open/120)*120 (per day)."""
    l = _ist(ts)
    mins = l.hour * 60 + l.minute - RTH_OPEN
    if mins < 0:
        mins = 0
    start = RTH_OPEN + (mins // TF_MIN) * TF_MIN
    return l.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def _resample_2h(bars):
    """30m bars [(t,o,h,l,c,v)] → 2h OHLC list [(bucket_ts,o,h,l,c)] ascending."""
    agg = {}
    for (t, o, h, l, c, v) in bars:
        if not c or not _rth(t):
            continue
        b = _tf_bucket(t)
        if b not in agg:
            agg[b] = [o, h, l, c]
        else:
            a = agg[b]; a[1] = max(a[1], h); a[2] = min(a[2], l); a[3] = c
    return [(b, *agg[b]) for b in sorted(agg)]


async def _c30(conn, u, e, k, ot):
    rows = await conn.fetch(
        """SELECT time, open, high, low, close, volume FROM option_premium_candles
           WHERE interval='30minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
           ORDER BY time ASC""", u, e, k, ot)
    return [(r["time"], float(r["open"] or 0), float(r["high"] or 0), float(r["low"] or 0),
             float(r["close"]), float(r["volume"] or 0)) for r in rows if r["close"] and _rth(r["time"])]


def _stat(r):
    if not r:
        return {"n": 0, "tot": 0.0, "avg": 0.0, "win": 0.0}
    return {"n": len(r), "tot": round(sum(r), 1), "avg": round(sum(r) / len(r), 2),
            "win": round(100 * sum(1 for x in r if x > 0) / len(r), 1)}


def _simulate(u, e, atm, ce2, pe2, trades):
    """ce2/pe2 = 2h OHLC lists for the ATM CE/PE. Records trades into `trades`."""
    ce_t = [b[0] for b in ce2]; ce_c = [b[4] for b in ce2]
    pe_t = [b[0] for b in pe2]; pe_c = [b[4] for b in pe2]
    if len(ce_c) < SLOW + SIG + 2 or len(pe_c) < SLOW + SIG + 2:
        return
    ce_m, _cs, _ch = compute_macd(ce_c, FAST, SLOW, SIG)
    pe_m, _ps, _ph = compute_macd(pe_c, FAST, SLOW, SIG)
    ce_i = {t: i for i, t in enumerate(ce_t)}
    pe_i = {t: i for i, t in enumerate(pe_t)}
    ce_bar = {b[0]: b for b in ce2}; pe_bar = {b[0]: b for b in pe2}

    def macd(ot):
        return ce_m if ot == "CE" else pe_m

    def idx(ot, t):
        return (ce_i if ot == "CE" else pe_i).get(t)

    def cu(ot, t):
        i = idx(ot, t); m = macd(ot)
        return i is not None and i >= 1 and m[i] is not None and m[i - 1] is not None and m[i - 1] < 0 < m[i]

    def cd(ot, t):
        i = idx(ot, t); m = macd(ot)
        return i is not None and i >= 1 and m[i] is not None and m[i - 1] is not None and m[i - 1] > 0 > m[i]

    all_ts = sorted(set(ce_t) | set(pe_t))
    pos = None

    def stop(p):
        return p["peak"] * (1 - TRAIL_GB) if p["peak"] >= p["entry"] * (1 + TRAIL_ACT) else p["entry"] * (1 - HARD_STOP)

    def rec(p, t, px, reason):
        r = (px - p["entry"]) / p["entry"] * 100.0
        trades.append({"underlying": u, "expiry": str(e), "strike": atm, "ot": p["ot"],
                       "entry_time": _ist(p["t0"]).strftime("%m-%d %H:%M"),
                       "exit_time": _ist(t).strftime("%m-%d %H:%M"),
                       "entry": round(p["entry"], 2), "exit": round(px, 2),
                       "ret": round(r, 1), "reason": reason, "bars": p["bars"]})

    for t in all_ts:
        if pos is not None:
            ot = pos["ot"]; bar = (ce_bar if ot == "CE" else pe_bar).get(t)
            oot = "PE" if ot == "CE" else "CE"
            if bar is not None:
                pos["bars"] += 1
                pos["peak"] = max(pos["peak"], bar[2])  # high
                st = stop(pos)
                if bar[3] <= st:  # low <= stop
                    rec(pos, t, st, "trail" if pos["peak"] >= pos["entry"] * (1 + TRAIL_ACT) else "stop25"); pos = None
                elif cu(oot, t):
                    rec(pos, t, bar[4], "flip")
                    ob = (ce_bar if oot == "CE" else pe_bar).get(t)
                    pos = {"ot": oot, "entry": ob[4], "peak": ob[4], "t0": t, "bars": 0} if ob else None
                    continue
                elif cd(ot, t):
                    rec(pos, t, bar[4], "macd_reversal"); pos = None
        if pos is None:
            for ot in ("CE", "PE"):
                if cu(ot, t):
                    b = (ce_bar if ot == "CE" else pe_bar).get(t)
                    if b:
                        pos = {"ot": ot, "entry": b[4], "peak": b[4], "t0": t, "bars": 0}
                    break
    if pos is not None:
        ot = pos["ot"]; last = max((b[0] for b in (ce2 if ot == "CE" else pe2)), default=None)
        if last is not None:
            rec(pos, last, (ce_bar if ot == "CE" else pe_bar)[last][4], "expiry_end")


async def main():
    conn = await asyncpg.connect(DSN)
    trades = []
    try:
        unders = [r["underlying"] for r in await conn.fetch(
            "SELECT DISTINCT underlying FROM option_premium_candles WHERE interval='30minute'")]
        for u in unders:
            ve = _valid_expiry(u)
            exps = [r["expiry"] for r in await conn.fetch(
                "SELECT DISTINCT expiry FROM option_premium_candles WHERE interval='30minute' AND underlying=$1 AND expiry::text=$2 ORDER BY expiry", u, ve)]
            for e in exps:
                strikes = [float(r["strike"]) for r in await conn.fetch(
                    "SELECT DISTINCT strike FROM option_premium_candles WHERE interval='30minute' AND underlying=$1 AND expiry=$2", u, e)]
                ce2s, pe2s = {}, {}
                for k in strikes:
                    ce2s[k] = _resample_2h(await _c30(conn, u, e, k, "CE"))
                    pe2s[k] = _resample_2h(await _c30(conn, u, e, k, "PE"))
                # ATM = min |CE-PE| at the first common 2h bar
                atm, best = None, None
                for k in strikes:
                    if ce2s[k] and pe2s[k]:
                        diff = abs(ce2s[k][0][4] - pe2s[k][0][4])
                        if best is None or diff < best:
                            best, atm = diff, k
                if atm is not None:
                    _simulate(u, e, atm, ce2s[atm], pe2s[atm], trades)

        rets = [t["ret"] for t in trades]
        wins = [r for r in rets if r > 0]
        print(f"=== 2h premium-MACD — ALL underlyings === {_stat(rets)}", flush=True)
        print(f"  theoretical-max (winners only): tot={round(sum(wins),1)}% n={len(wins)}", flush=True)
        ce = [t["ret"] for t in trades if t["ot"] == "CE"]; pe = [t["ret"] for t in trades if t["ot"] == "PE"]
        print(f"  CE {_stat(ce)} | PE {_stat(pe)}", flush=True)
        per_exp = defaultdict(list)
        for t in trades:
            per_exp[t["expiry"]].append(t["ret"])
        print("  per-expiry:", {e: _stat(v)["avg"] for e, v in sorted(per_exp.items()) if len(v) >= 5}, flush=True)

        idx_tr = [t for t in trades if t["underlying"] in INDICES]
        print(f"\n=== INDICES ({sorted(set(t['underlying'] for t in idx_tr))}) === {_stat([t['ret'] for t in idx_tr])}", flush=True)
        for u in sorted(set(t["underlying"] for t in idx_tr)):
            ut = [t["ret"] for t in idx_tr if t["underlying"] == u]
            print(f"  {u:<11} {_stat(ut)}", flush=True)

        print("\n=== INDEX CE/PE TRADE LIST (2h premium MACD) ===", flush=True)
        hdr = f"{'underlying':<11}{'exp':<7}{'strike':>9} {'ot':<3}{'entry_time':<13}{'exit_time':<13}{'entry':>8}{'exit':>8}{'ret%':>7} {'reason':<13}{'bars':>4}"
        print(hdr, flush=True)
        for t in sorted(idx_tr, key=lambda x: (x["underlying"], x["expiry"], x["entry_time"])):
            print(f"{t['underlying']:<11}{str(t['expiry'])[5:]:<7}{t['strike']:>9.0f} {t['ot']:<3}{t['entry_time']:<13}{t['exit_time']:<13}{t['entry']:>8.2f}{t['exit']:>8.2f}{t['ret']:>7.1f} {t['reason']:<13}{t['bars']:>4}", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
