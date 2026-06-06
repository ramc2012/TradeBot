"""S1 (NSE-S1 option-premium MACD) June signal audit.

Re-simulates the OLD (buggy) vs the CORRECTED MACD logic over June-expiry option
contracts to quantify: how many signals each would generate and the P/L implication
of the fix. Also cross-references the count of signals the live system ACTUALLY
recorded in agent_signals for June.

Corrected rules (this audit mirrors the fixed strategy_agent_entries/exits):
  • 30m PRIMARY  — buy when the contract's own premium MACD crosses UP through zero
                   (prev < 0 < curr), for BOTH CE and PE. Exit on the premium MACD
                   crossing DOWN through zero.
  • 15m RE-ENTRY — when the 30m premium MACD is already > 0, buy on a 15m MACD/SIGNAL
                   line crossover (macd crosses up over signal). Exit on 30m down-cross.

Old (buggy) rules being compared against:
  • 30m PRIMARY  — CE on up-cross (ok); PE on DOWN-cross (inverted). Exit mirrored.
  • 15m RE-ENTRY — none (was logically impossible: 15m == re-bucketed 30m).

Self-contained: direct asyncpg, no app/broker bootstrap. Run in the approved sidecar:
  docker run --rm --network tradebot_default -e PYTHONPATH=/app -w /app \
    -v /opt/TradeBot/backend:/app tradebot-backend:latest python -m analysis.s1_june_signal_audit
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import date, timedelta, timezone

import asyncpg

from analysis.macd_engine import compute_macd

DSN = os.environ.get("AUDIT_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
IST = timezone(timedelta(hours=5, minutes=30))
JUNE_START = date(2026, 6, 1)
JUNE_END = date(2026, 7, 1)
FAST, SLOW, SIGNAL = 12, 26, 9


def _zero_cross_up(macd):
    return [i for i in range(1, len(macd))
            if macd[i - 1] is not None and macd[i] is not None and macd[i - 1] < 0 < macd[i]]


def _zero_cross_down(macd):
    return [i for i in range(1, len(macd))
            if macd[i - 1] is not None and macd[i] is not None and macd[i - 1] > 0 > macd[i]]


def _sig_cross_up(macd, sig):
    out = []
    for i in range(1, len(macd)):
        if None in (macd[i - 1], macd[i], sig[i - 1], sig[i]):
            continue
        if macd[i - 1] <= sig[i - 1] and macd[i] > sig[i]:
            out.append(i)
    return out


def _simulate(entries, exits, closes, times=None):
    """Non-overlapping: each entry exits at the first exit bar after it (else last
    bar). Returns list of (return_pct, entry_time). entries/exits are bar indices."""
    rets = []
    exits_sorted = sorted(exits)
    used = -1
    for e in sorted(entries):
        if e <= used:
            continue
        entry_px = closes[e]
        if not entry_px or entry_px <= 0:
            continue
        ex = next((x for x in exits_sorted if x > e), None)
        exit_idx = ex if ex is not None else len(closes) - 1
        exit_px = closes[exit_idx]
        if not exit_px or exit_px <= 0:
            continue
        rets.append(((exit_px - entry_px) / entry_px) * 100.0)
        used = exit_idx
    return rets


def _resample_15m(rows):
    """rows: list of (time, close) ascending at 3m → dict of 15m bucket -> last close,
    returned as (times, closes) ascending."""
    buckets = {}
    for t, c in rows:
        if c is None:
            continue
        local = t.astimezone(IST)
        mins = local.hour * 60 + local.minute
        if mins < 9 * 60 + 15 or mins > 15 * 60 + 30:
            continue
        b = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0)
        buckets[b] = float(c)
    ordered = sorted(buckets.items())
    return [b for b, _ in ordered], [c for _, c in ordered]


def _stats(rets):
    n = len(rets)
    if not n:
        return {"trades": 0, "total_ret_pct": 0.0, "avg_ret_pct": 0.0, "win_rate_pct": 0.0}
    wins = sum(1 for r in rets if r > 0)
    return {
        "trades": n,
        "total_ret_pct": round(sum(rets), 1),
        "avg_ret_pct": round(sum(rets) / n, 2),
        "win_rate_pct": round(100 * wins / n, 1),
    }


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        contracts = await conn.fetch(
            """
            SELECT DISTINCT underlying, expiry, strike, option_type
            FROM option_premium_candles
            WHERE interval = '30minute' AND expiry >= $1 AND expiry < $2
            """,
            JUNE_START, JUNE_END,
        )
        print(f"June-expiry contracts with 30m candles: {len(contracts)}", flush=True)

        old_cnt = defaultdict(int); new_cnt = defaultdict(int)
        old_rets = defaultdict(list); new_rets = defaultdict(list)

        for c in contracts:
            ot = c["option_type"]
            rows30 = await conn.fetch(
                """SELECT time, close FROM option_premium_candles
                   WHERE interval='30minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
                   ORDER BY time ASC""",
                c["underlying"], c["expiry"], c["strike"], ot,
            )
            t30 = [r["time"] for r in rows30]
            closes30 = [float(r["close"]) if r["close"] is not None else None for r in rows30]
            if len([x for x in closes30 if x]) < SLOW + SIGNAL + 2:
                continue
            macd30, sig30, _ = compute_macd([x or 0.0 for x in closes30], FAST, SLOW, SIGNAL)
            up = _zero_cross_up(macd30)
            down = _zero_cross_down(macd30)

            # NEW 30m primary: both sides buy on up-cross, exit on down-cross.
            new_cnt[f"30m_{ot}"] += len(up)
            up_rets = _simulate(up, down, closes30)
            new_rets["30m"].extend(up_rets)
            new_rets[f"30m_{ot}"].extend(up_rets)  # CE and corrected-PE both up-cross
            # OLD 30m primary: CE up-cross (ok); PE down-cross (inverted), exit on up.
            if ot == "CE":
                old_cnt["30m_CE"] += len(up)
                old_rets["30m"].extend(up_rets)
                old_rets["30m_CE"].extend(up_rets)
            else:
                old_cnt["30m_PE"] += len(down)
                pe_down_rets = _simulate(down, up, closes30)
                old_rets["30m"].extend(pe_down_rets)
                old_rets["30m_PE_downcross"].extend(pe_down_rets)

            # NEW 15m re-entry: 3m→15m signal-cross-up gated by 30m macd>0 at that time.
            rows3 = await conn.fetch(
                """SELECT time, close FROM option_premium_candles
                   WHERE interval='3minute' AND underlying=$1 AND expiry=$2 AND strike=$3 AND option_type=$4
                   ORDER BY time ASC""",
                c["underlying"], c["expiry"], c["strike"], ot,
            )
            t15, closes15 = _resample_15m([(r["time"], r["close"]) for r in rows3])
            if len(closes15) >= SLOW + SIGNAL + 2:
                macd15, sig15, _ = compute_macd(closes15, FAST, SLOW, SIGNAL)
                sc = _sig_cross_up(macd15, sig15)
                # 30m macd>0 lookup by time (step function from 30m bars)
                def macd30_at(ts):
                    v = None
                    for k in range(len(t30)):
                        if t30[k] <= ts and macd30[k] is not None:
                            v = macd30[k]
                        elif t30[k] > ts:
                            break
                    return v
                reentries = [i for i in sc if (macd30_at(t15[i]) or -1) > 0]
                new_cnt["15m_reentry"] += len(reentries)
                # exit on next 15m signal-cross-down
                sig_down = [i for i in range(1, len(macd15))
                            if None not in (macd15[i - 1], macd15[i], sig15[i - 1], sig15[i])
                            and macd15[i - 1] >= sig15[i - 1] and macd15[i] < sig15[i]]
                new_rets["15m_reentry"].extend(_simulate(reentries, sig_down, closes15))
            # OLD 15m re-entry: none (impossible by construction).

        # Actual recorded signals (live system) in June.
        actual_total = await conn.fetchval(
            """SELECT count(*) FROM agent_signals
               WHERE strategy_key='macd_strategy' AND signal_bar_time >= $1 AND signal_bar_time < $2""",
            JUNE_START, JUNE_END,
        )
        actual_by = await conn.fetch(
            """SELECT option_type, count(*) AS n FROM agent_signals
               WHERE strategy_key='macd_strategy' AND signal_bar_time >= $1 AND signal_bar_time < $2
               GROUP BY option_type ORDER BY n DESC""",
            JUNE_START, JUNE_END,
        )

        print("\n===== SIGNAL COUNTS (June-expiry contracts) =====", flush=True)
        print("CORRECTED (would-have):", dict(new_cnt),
              "| total =", sum(new_cnt.values()), flush=True)
        print("OLD (as-was):          ", dict(old_cnt),
              "| total =", sum(old_cnt.values()), flush=True)
        print(f"ACTUAL recorded in agent_signals (macd_strategy, June): {actual_total}", flush=True)
        print("  actual by option_type:", {r["option_type"]: r["n"] for r in actual_by}, flush=True)

        print("\n===== P/L IMPLICATION (premium return %, entry→opposite-cross exit) =====", flush=True)
        for label, rets in (("CORRECTED 30m", new_rets["30m"]), ("OLD 30m", old_rets["30m"]),
                            ("CORRECTED 15m re-entry", new_rets["15m_reentry"])):
            print(f"  {label:<24}", _stats(rets), flush=True)
        print("  --- per-side 30m breakdown ---", flush=True)
        for label, rets in (("CE up-cross (both)", new_rets["30m_CE"]),
                            ("PE up-cross (CORRECTED)", new_rets["30m_PE"]),
                            ("PE down-cross (OLD/buggy)", old_rets["30m_PE_downcross"])):
            print(f"    {label:<28}", _stats(rets), flush=True)
        all_new = new_rets["30m"] + new_rets["15m_reentry"]
        print(f"  {'CORRECTED total':<24}", _stats(all_new), flush=True)
        print(f"  {'OLD total':<24}", _stats(old_rets['30m']), flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
