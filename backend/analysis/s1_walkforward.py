"""S1 walk-forward backtest — refined spec (option-premium 30m MACD, ATM CE/PE).

Rules (exactly as specified):
  • ATM CE & PE per underlying per day (ATM = strike where |CE−PE| is smallest at the
    day's first bar — put/call parity ⇒ CE≈PE at the money).
  • Entry: the contract's own 30-minute premium MACD crosses UP through zero
    (prev_closed_macd < 0 < forming_macd). Detected INTRA-BAR — no wait for the 30m
    candle to close: each 3-minute sub-bar extends the forming 30m bar and the MACD is
    recomputed incrementally (mirrors the live synthetic-forming-bar).
  • Buy whichever of ATM CE / PE gives the cross. ONE position per underlying (CE or PE).
  • Hard stop: −25% on premium (intrabar 3m low ≤ 0.75×entry ⇒ exit at the stop).
  • Flip: if the OPPOSITE side gives a fresh zero-cross-up while in a position, close the
    current and open the new one.
  • Same-side reversal: if the held side's 30m MACD crosses DOWN through zero, exit
    (the signal is over) — the natural MACD exit, kept alongside the stop/flip.
  • Re-entry: after a flat, if the 30m MACD is still > 0 AND a 15m MACD/SIGNAL line
    cross-up fires, re-enter that side (ride the higher-TF trend).
  • Intraday: any open position is squared off at 15:30 IST; ATM re-picked each day.

Walk-forward: the strategy has no free parameters (MACD 12/26/9, 25% stop are fixed),
so "walk-forward" here is a forward, per-session out-of-sample run — we report P/L
per trading day and per underlying so consistency over time is visible.

Self-contained sidecar (off-prod):
  docker run --rm --network tradebot_default --memory=1200m -e PYTHONPATH=/app -w /app \
    -v /opt/TradeBot/backend:/app tradebot-backend:latest python -m analysis.s1_walkforward
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import date, timedelta, timezone

import asyncpg

from analysis.macd_engine import compute_ema, compute_macd

DSN = os.environ.get("AUDIT_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
IST = timezone(timedelta(hours=5, minutes=30))
JUNE_START = date(2026, 6, 1)
JUNE_END = date(2026, 7, 1)
FAST, SLOW, SIGNAL = 12, 26, 9
HARD_STOP = 0.25          # 25% premium hard stop
RTH_OPEN, RTH_CLOSE = 9 * 60 + 15, 15 * 60 + 30
A_FAST, A_SLOW = 2 / (FAST + 1), 2 / (SLOW + 1)


def _bucket(ts, minutes):
    local = ts.astimezone(IST)
    return local.replace(minute=(local.minute // minutes) * minutes, second=0, microsecond=0)


def _in_rth(ts):
    local = ts.astimezone(IST)
    m = local.hour * 60 + local.minute
    return RTH_OPEN <= m <= RTH_CLOSE


def _resample(rows, minutes):
    """rows: [(ts, close)] ascending → ([bucket_ts...], [close...]) last-close per bucket, RTH."""
    b = {}
    for ts, c in rows:
        if c is None or not _in_rth(ts):
            continue
        b[_bucket(ts, minutes)] = float(c)
    o = sorted(b.items())
    return [k for k, _ in o], [v for _, v in o]


def _forming_macd_series(closes_30):
    """Return (macd_closed list, ema_fast list, ema_slow list) over closed 30m closes."""
    ef = compute_ema(closes_30, FAST)
    es = compute_ema(closes_30, SLOW)
    macd = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None for i in range(len(closes_30))]
    return macd, ef, es



async def _contract_3m(conn, underlying, expiry, strike, ot):
    rows = await conn.fetch(
        """SELECT time, open, high, low, close FROM option_premium_candles
           WHERE interval='3minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
           ORDER BY time ASC""",
        underlying, expiry, strike, ot,
    )
    return [(r["time"], r["open"], r["high"], r["low"], r["close"]) for r in rows]



def _stats(rets):
    n = len(rets)
    if not n:
        return {"trades": 0, "total_%": 0.0, "avg_%": 0.0, "win_%": 0.0, "best_%": 0.0, "worst_%": 0.0}
    w = sum(1 for r in rets if r > 0)
    return {"trades": n, "total_%": round(sum(rets), 1), "avg_%": round(sum(rets) / n, 2),
            "win_%": round(100 * w / n, 1), "best_%": round(max(rets), 1), "worst_%": round(min(rets), 1)}


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        unders = [r["underlying"] for r in await conn.fetch(
            """SELECT DISTINCT underlying FROM option_premium_candles
               WHERE interval='3minute' AND expiry >= $1 AND expiry < $2""", JUNE_START, JUNE_END)]
        print(f"underlyings with June 3m data: {len(unders)} -> {unders[:12]}", flush=True)

        all_rets = []
        by_day = defaultdict(list)
        by_under = defaultdict(list)
        by_kind = defaultdict(list)
        contracts_traded = 0

        for underlying in unders:
            expiries = [r["expiry"] for r in await conn.fetch(
                """SELECT DISTINCT expiry FROM option_premium_candles
                   WHERE interval='3minute' AND underlying=$1 AND expiry >= $2 AND expiry < $3
                   ORDER BY expiry""", underlying, JUNE_START, JUNE_END)]
            for expiry in expiries:
                strikes = [float(r["strike"]) for r in await conn.fetch(
                    """SELECT DISTINCT strike FROM option_premium_candles
                       WHERE interval='3minute' AND underlying=$1 AND expiry=$2""", underlying, expiry)]
                if not strikes:
                    continue
                # load all CE/PE 3m for these strikes once (per expiry) into memory
                ce_by_strike, pe_by_strike = {}, {}
                for k in strikes:
                    ce_by_strike[k] = await _contract_3m(conn, underlying, expiry, k, "CE")
                    pe_by_strike[k] = await _contract_3m(conn, underlying, expiry, k, "PE")

                # group 3m bars by trading day
                days = sorted({_bucket(t, 30).date() for k in strikes for (t, *_r) in ce_by_strike.get(k, [])})
                for d in days:
                    # ATM = strike minimizing |CE-PE| at the day's first common bar
                    atm, best = None, None
                    for k in strikes:
                        ceb = [(t, c) for (t, o, h, l, c) in ce_by_strike.get(k, []) if _bucket(t, 30).date() == d and c]
                        peb = [(t, c) for (t, o, h, l, c) in pe_by_strike.get(k, []) if _bucket(t, 30).date() == d and c]
                        if not ceb or not peb:
                            continue
                        diff = abs(ceb[0][1] - peb[0][1])
                        if best is None or diff < best:
                            best, atm = diff, k
                    if atm is None:
                        continue
                    contracts_traded += 1
                    rets = _simulate_day(underlying, d, ce_by_strike[atm], pe_by_strike[atm], by_kind)
                    all_rets.extend(rets)
                    by_day[str(d)].extend(rets)
                    by_under[underlying].extend(rets)

        print(f"\nATM contract-days simulated: {contracts_traded}", flush=True)
        print("\n===== WALK-FORWARD P/L (per-trade premium return %, −25% hard stop) =====", flush=True)
        print("OVERALL:", _stats(all_rets), flush=True)
        print("by entry kind:", flush=True)
        for kind in ("primary_ce", "primary_pe", "reentry"):
            print(f"  {kind:<12}", _stats(by_kind.get(kind, [])), flush=True)
        print("by trading day:", flush=True)
        for d in sorted(by_day):
            print(f"  {d}", _stats(by_day[d]), flush=True)
        print("by underlying (top 12 by trades):", flush=True)
        for u in sorted(by_under, key=lambda x: -len(by_under[x]))[:12]:
            print(f"  {u:<12}", _stats(by_under[u]), flush=True)
    finally:
        await conn.close()


def _simulate_day(underlying, d, ce_bars, pe_bars, by_kind):
    """Simulate one ATM contract-day. Returns list of trade return %."""
    # day 3m bars (RTH), continuous prior history for warmup is included in the full
    # series; we compute forming MACD using ALL bars up to each point but only TRADE
    # on day d.
    def prep(bars):
        # ascending; compute per-3m forming 30m macd using closed 30m closes up to each bar
        seq = [(t, o, h, l, c) for (t, o, h, l, c) in bars if c and _in_rth(t)]
        seq.sort(key=lambda x: x[0])
        # closed 30m closes (last 3m close of each completed bucket)
        closed, last_close_in_bucket, cur_b = [], None, None
        prev_forming = None  # forming MACD of the PREVIOUS 3m sub-bar (for edge detection)
        per_bar = []  # (t, low, close, forming_macd, prev_closed_macd, prev_forming_macd)
        for (t, o, h, l, c) in seq:
            b = _bucket(t, 30)
            if cur_b is None:
                cur_b = b
            elif b != cur_b:
                if last_close_in_bucket is not None:
                    closed.append(last_close_in_bucket)
                cur_b = b
            last_close_in_bucket = float(c)
            prev_macd = forming = None
            if len(closed) >= SLOW:
                ef = compute_ema(closed, FAST)[-1]
                es = compute_ema(closed, SLOW)[-1]
                if ef is not None and es is not None:
                    prev_macd = ef - es
                    forming = (A_FAST * float(c) + (1 - A_FAST) * ef) - (A_SLOW * float(c) + (1 - A_SLOW) * es)
            per_bar.append((t, float(l) if l is not None else float(c), float(c), forming, prev_macd, prev_forming))
            prev_forming = forming
        return per_bar

    ce = {t: (lo, cl, fm, pm, pf) for (t, lo, cl, fm, pm, pf) in prep(ce_bars)}
    pe = {t: (lo, cl, fm, pm, pf) for (t, lo, cl, fm, pm, pf) in prep(pe_bars)}
    # 15m re-entry context per side (signal-cross set on 15m + 30m macd>0 gate)
    ce15_cross, ce15_macd = _macd15_for(ce_bars)
    pe15_cross, pe15_macd = _macd15_for(pe_bars)

    # timeline = union of CE/PE 3m timestamps on day d, ascending
    ts_all = sorted(t for t in set(ce) | set(pe) if t.astimezone(IST).date() == d)
    rets = []
    pos = None  # dict(side, entry, ot)
    reentry_used = set()  # (side, 15m-bucket) already used for a re-entry → no churn

    # A zero-cross fires ONCE per 30m bucket per side: the forming 30m MACD transitions
    # up through zero (prev_forming <= 0 < curr_forming) WHILE the last CLOSED 30m MACD
    # is genuinely negative (prev_closed < 0). The per-bucket 'fired' guard stops the
    # noisy forming line re-triggering after an exit within the same window — that churn
    # produced unrealistic >100-trade days. (v = (low, close, forming, prev_closed, prev_forming))
    def cross_up(side_map, t):
        v = side_map.get(t)
        return (v is not None and v[2] is not None and v[3] is not None and v[4] is not None
                and v[3] < 0 and v[4] <= 0 < v[2])

    def cross_down(side_map, t):
        v = side_map.get(t)
        return (v is not None and v[2] is not None and v[3] is not None and v[4] is not None
                and v[3] > 0 and v[4] >= 0 > v[2])

    fired = set()  # (side, 30m-bucket) already used for a primary/flip entry

    for t in ts_all:
        bkt = _bucket(t, 30)
        # manage open position first
        if pos is not None:
            sm = ce if pos["ot"] == "CE" else pe
            opp = pe if pos["ot"] == "CE" else ce
            opp_ot = "PE" if pos["ot"] == "CE" else "CE"
            v = sm.get(t)
            if v is not None:
                lo, cl = v[0], v[1]
                # 1) hard stop −25% (intrabar low)
                if lo <= pos["entry"] * (1 - HARD_STOP):
                    rets.append(-HARD_STOP * 100.0); by_kind[pos["kind"]].append(-HARD_STOP * 100.0)
                    pos = None
                # 2) flip on opposite fresh cross-up (once per bucket)
                elif cross_up(opp, t) and (opp_ot, bkt) not in fired:
                    fired.add((opp_ot, bkt))
                    r = (cl - pos["entry"]) / pos["entry"] * 100.0
                    rets.append(r); by_kind[pos["kind"]].append(r)
                    ov = opp.get(t)
                    pos = {"ot": opp_ot, "entry": ov[1], "kind": f"primary_{opp_ot.lower()}"}
                    continue
                # 3) same-side reversal (macd down through zero)
                elif cross_down(sm, t):
                    r = (cl - pos["entry"]) / pos["entry"] * 100.0
                    rets.append(r); by_kind[pos["kind"]].append(r)
                    pos = None
        # entries when flat
        if pos is None:
            if cross_up(ce, t) and ("CE", bkt) not in fired:
                fired.add(("CE", bkt)); pos = {"ot": "CE", "entry": ce[t][1], "kind": "primary_ce"}
            elif cross_up(pe, t) and ("PE", bkt) not in fired:
                fired.add(("PE", bkt)); pos = {"ot": "PE", "entry": pe[t][1], "kind": "primary_pe"}
            else:
                # 15m re-entry: 30m macd>0 AND 15m macd/signal cross-up at this 15m bucket
                for ot, sm, cross15, m15 in (("CE", ce, ce15_cross, ce15_macd), ("PE", pe, pe15_cross, pe15_macd)):
                    v = sm.get(t)
                    if v is None or v[2] is None or v[2] <= 0:
                        continue
                    bts = _bucket(t, 15)
                    if cross15.get(bts) and (ot, bts) not in reentry_used:
                        reentry_used.add((ot, bts))
                        pos = {"ot": ot, "entry": v[1], "kind": "reentry"}
                        break
    # square off at day end
    if pos is not None:
        sm = ce if pos["ot"] == "CE" else pe
        last_t = max((t for t in sm if t.astimezone(IST).date() == d), default=None)
        if last_t is not None:
            r = (sm[last_t][1] - pos["entry"]) / pos["entry"] * 100.0
            rets.append(r); by_kind[pos["kind"]].append(r)
    return rets


def _macd15_for(bars):
    """Return (dict bucket_ts->True for signal-cross-up, dict bucket_ts->macd15)."""
    t15, c15 = _resample([(t, c) for (t, o, h, l, c) in bars], 15)
    cross = {}
    macd_by_ts = {}
    if len(c15) >= SLOW + SIGNAL + 2:
        macd, sig, _ = compute_macd(c15, FAST, SLOW, SIGNAL)
        for i in range(len(c15)):
            macd_by_ts[t15[i]] = macd[i]
            if i >= 1 and None not in (macd[i - 1], macd[i], sig[i - 1], sig[i]) and macd[i - 1] <= sig[i - 1] and macd[i] > sig[i]:
                cross[t15[i]] = True
    return cross, macd_by_ts


if __name__ == "__main__":
    asyncio.run(main())
