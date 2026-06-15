"""
EXPRESSION experiments on the index-option substrate — does changing the
*structure of the trade* (not the signal) clear the ~3% cost wall?

Established ground truth (see analysis/nse_option_walkforward.py): the long-ATM
index-option MACD-zero-cross lane is KILLED net-of-cost (NIFTY held-out
-17.7%/trade, 32/32 OOS-negative history). The single structural finding is that
a ~3% round-trip on long ATM premium is a friction WALL no coin-flip-grade gross
signal clears. This module asks the only remaining honest question: can a change
of EXPRESSION (longer holds / spreads / premium-selling), on the SAME best gross
signal (MACD zero-cross), make a real net-of-cost OOS edge appear?

It reuses, unchanged, the gate machinery from nse_option_walkforward:
  * the SAME data loader, no-arb guards (analysis.safe_candles),
  * the SAME shared cost model (analysis.cost_model.NSE_OPTION_COST, ~3% RT),
  * the SAME signal (MACD zero-cross on the closed 30m spot bar, exec bar i+1),
  * the SAME sacred held-out split (analysis.walk_forward.HELD_OUT_START).
The ONLY thing that changes is how the trade is expressed.

VARIANTS
--------
  A) LONGER HOLDS   : sweep time_stop_bars {13 baseline, 26, 52, EOD-of-entry-day,
                      hold-to-expiry}. Amortize the fixed 3% over a bigger move?
  B) DEBIT VERTICAL : buy ATM, sell N-strikes-OTM (same expiry/type). Net debit is
                      small → the ~3% applies to a SMALLER premium; cost charged on
                      BOTH legs; payoff capped at the strike width. Lower friction?
  C) PREMIUM SELL   : short the ATM (collect premium, theta+ / spread+), defined
                      stop at premium x (1+stop). Right side of theta+spread?

Every variant is scored cost-ON on (1) the DEV set, (2) the rolling walk-forward
DEV OOS, and (3) the sacred held-out block EXACTLY ONCE, ALWAYS side-by-side with
the long-ATM baseline. A bootstrap CI on the held-out mean is printed so a small-n
"positive" is not mistaken for an edge. NEVER mutates the original harness.

Run (local stack, backend container):
  docker exec -e NSE_WF_DSN=postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie \
      -w /app -e PYTHONPATH=/app nomadcurie_backend \
      python -m analysis.expression_experiments --underlying NIFTY

RESULT (2026-06, NIFTY + BANKNIFTY) — the wall STANDS net-of-cost.
  * (A) Longer holds: the cost-amortization mechanism IS real on DEV (NIFTY DEV
    mean -4.7% @ts13 -> -0.3% @ts52) but never crosses positive, and every
    held-out cell stays negative (longer holds make held-out WORSE, not better).
  * (B) Debit vertical: catastrophic (-30% to -107% net). The short OTM leg is too
    thin/noisy; the ~3% on BOTH legs as a fraction of the tiny net debit explodes.
    Lower-friction-in-theory is unworkable on this option DB in practice.
  * (C) Premium sell: best DEV (NIFTY breakeven, PF~1.0) and ONE held-out cell came
    back positive — BANKNIFTY short-ATM stop100/tp50/ts13: held-out n=11, +6.2%/tr,
    PF 5.8, CI95 [+0.014,+0.111]. BUT it is NEGATIVE on the 144-trade DEV set
    (-1.7%/tr, PF 0.85) and the proper rolling DEV walk-forward FAILs (OOS -1.6%/tr,
    MC-sharpe-p05 -0.35). A positive living ONLY in the 11-trade frozen block while
    the governing development sample is negative is the small-n held-out artifact
    the sacred-block discipline exists to catch — NOT a validated edge.
  VERDICT: no EXPRESSION change produces a real net-of-cost OOS edge. The 33rd
  honest OOS test, still negative. The cost wall is not an artifact of expression.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from analysis.cost_model import NSE_OPTION_COST
from analysis.macd_engine import compute_macd
from analysis import walk_forward as wf
from analysis import validation_metrics as vm
# Reuse, unchanged, the baseline harness's loader / option book / fill helper so
# the data, guards, ATM selection and execution timing are IDENTICAL.
from analysis.nse_option_walkforward import (
    LOT,
    DEFAULT_DSN,
    _load,
    OptionBook,
    _premium_at,
    run_trades as run_long_atm_baseline,
    _summ,
)

IST = pd.Timestamp  # alias only for readability


# ── shared signal (identical to the baseline) ───────────────────────────────
def _macd_signals(closes: list[float], fast=12, slow=26, signal=9):
    macd, sig, _ = compute_macd(closes, fast, slow, signal)

    def crossed(i: int) -> int:
        if i < 1 or macd[i] is None or sig[i] is None or macd[i - 1] is None or sig[i - 1] is None:
            return 0
        prev, cur = macd[i - 1] - sig[i - 1], macd[i] - sig[i]
        if prev <= 0 < cur:
            return +1
        if prev >= 0 > cur:
            return -1
        return 0

    return crossed


def _eod_bar_limit(times: list[pd.Timestamp], i_entry: int, n: int) -> int:
    """Index (exclusive) of the first bar on a LATER calendar day than entry.
    'hold-to-EOD' exits at the last bar of the entry trading day."""
    entry_day = times[i_entry].tz_convert("Asia/Kolkata").date()
    j = i_entry + 1
    while j < n and times[j].tz_convert("Asia/Kolkata").date() == entry_day:
        j += 1
    return j  # exclusive


# ════════════════════════════════════════════════════════════════════════════
# VARIANT A — longer holds (long ATM, sweep the time stop incl. EOD / expiry)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class _CfgA:
    time_stop_bars: int = 13      # baseline; "EOD" and "EXPIRY" handled specially
    hard_stop_pct: float = 0.25
    hold_mode: str = "bars"       # "bars" | "eod" | "expiry"
    fast: int = 12
    slow: int = 26
    signal: int = 9


def run_longhold(spot: pd.DataFrame, params: dict, *, book: OptionBook, apply_cost: bool = True) -> list:
    """Long ATM, identical to the baseline EXCEPT the exit horizon (the expression
    lever being tested). hold_mode 'bars' = N bars; 'eod' = to entry-day close;
    'expiry' = to the contract's last available bar before expiry (or hard stop)."""
    cfg = _CfgA(**{k: params[k] for k in params if k in _CfgA().__dict__})
    s = spot.sort_values("time").reset_index(drop=True)
    if len(s) < cfg.slow + 5:
        return []
    times = pd.to_datetime(s["time"], utc=True).tolist()
    closes = s["close"].astype(float).tolist()
    crossed = _macd_signals(closes, cfg.fast, cfg.slow, cfg.signal)
    lot = LOT.get(str(s.attrs.get("underlying", "NIFTY")).upper(), 50)

    trades: list = []
    i, n = 1, len(s)
    while i < n - 1:
        direction = crossed(i)
        if direction == 0:
            i += 1
            continue
        otype = "CE" if direction > 0 else "PE"
        entry_ts = times[i + 1]
        leg = book.atm_leg(entry_ts, closes[i], otype)
        if leg is None:
            i += 1
            continue
        _strike, leg_df = leg
        entry_px = _premium_at(leg_df, entry_ts, "open")
        if entry_px is None:
            i += 1
            continue

        if cfg.hold_mode == "eod":
            j_limit = _eod_bar_limit(times, i + 1, n)
        elif cfg.hold_mode == "expiry":
            j_limit = n  # ride until the contract bars run out / hard stop
        else:
            j_limit = min(i + 1 + cfg.time_stop_bars, n)

        exit_px, exit_ts = None, entry_ts
        for j in range(i + 1, j_limit):
            px = _premium_at(leg_df, times[j], "close")
            if px is None:
                continue
            exit_px, exit_ts = px, times[j]
            if px <= entry_px * (1 - cfg.hard_stop_pct):
                break
            if cfg.hold_mode == "bars" and crossed(j) == -direction:
                break
            # In eod/expiry modes we DELIBERATELY ignore the opposite cross so the
            # "longer hold" lever is what's actually tested, not a re-cross exit.
        if exit_px is None:
            i += 1
            continue
        gross = exit_px / entry_px - 1.0
        cost = NSE_OPTION_COST.round_trip_pct(entry_px, lot_qty=lot) if apply_cost else 0.0
        trades.append({"time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost,
                       "otype": otype, "entry": entry_px, "exit": exit_px})
        while i < n - 1 and times[i] <= exit_ts:
            i += 1
    return trades


# ════════════════════════════════════════════════════════════════════════════
# VARIANT B — debit vertical spread (buy ATM, sell N-strikes-OTM, same expiry)
# ════════════════════════════════════════════════════════════════════════════
def _otm_leg(book: OptionBook, entry_ts: pd.Timestamp, atm_strike: float,
             option_type: str, n_strikes_otm: int):
    """The strike `n_strikes_otm` steps OUT-of-the-money from `atm_strike` in the
    SAME active expiry (CE → higher strike, PE → lower), with its premium series.
    Returns (strike, df) or None if that strike isn't quoted for this expiry."""
    exp = book.active_expiry(entry_ts)
    if exp is None:
        return None
    strikes = book.by_expiry_type.get((exp, option_type))
    if not strikes:
        return None
    avail = sorted(strikes.keys())
    if atm_strike not in avail:
        # atm came from a possibly different expiry's ladder; map to nearest here
        atm_strike = min(avail, key=lambda k: abs(k - atm_strike))
    idx = avail.index(atm_strike)
    tgt = idx + n_strikes_otm if option_type == "CE" else idx - n_strikes_otm
    if tgt < 0 or tgt >= len(avail):
        return None
    k = avail[tgt]
    return k, strikes[k]


@dataclass
class _CfgB:
    time_stop_bars: int = 13
    n_strikes_otm: int = 2        # width of the vertical, in strike steps
    fast: int = 12
    slow: int = 26
    signal: int = 9


def run_debit_vertical(spot: pd.DataFrame, params: dict, *, book: OptionBook, apply_cost: bool = True) -> list:
    """Debit vertical: long ATM + short N-strikes-OTM (same expiry/type). Net debit
    = long_prem - short_prem (much smaller than the ATM premium). The ~3% RT cost
    is charged on BOTH legs (each leg's own premium). Payoff is the change in the
    spread value: (exit_long - exit_short) vs (entry_long - entry_short), returned
    as a fraction of the net entry debit. No hard-stop on premium% (the structure
    is already loss-capped at the debit); exit on time-stop or opposite cross."""
    cfg = _CfgB(**{k: params[k] for k in params if k in _CfgB().__dict__})
    s = spot.sort_values("time").reset_index(drop=True)
    if len(s) < cfg.slow + 5:
        return []
    times = pd.to_datetime(s["time"], utc=True).tolist()
    closes = s["close"].astype(float).tolist()
    crossed = _macd_signals(closes, cfg.fast, cfg.slow, cfg.signal)
    lot = LOT.get(str(s.attrs.get("underlying", "NIFTY")).upper(), 50)

    trades: list = []
    i, n = 1, len(s)
    while i < n - 1:
        direction = crossed(i)
        if direction == 0:
            i += 1
            continue
        otype = "CE" if direction > 0 else "PE"
        entry_ts = times[i + 1]
        long_leg = book.atm_leg(entry_ts, closes[i], otype)
        if long_leg is None:
            i += 1
            continue
        atm_k, long_df = long_leg
        short_leg = _otm_leg(book, entry_ts, atm_k, otype, cfg.n_strikes_otm)
        if short_leg is None:
            i += 1
            continue
        _short_k, short_df = short_leg

        el = _premium_at(long_df, entry_ts, "open")
        es = _premium_at(short_df, entry_ts, "open")
        if el is None or es is None:
            i += 1
            continue
        net_debit = el - es
        if net_debit <= 0:   # degenerate (short richer than long) — skip
            i += 1
            continue

        exit_val, exit_ts = None, entry_ts
        for j in range(i + 1, min(i + 1 + cfg.time_stop_bars, n)):
            xl = _premium_at(long_df, times[j], "close")
            xs = _premium_at(short_df, times[j], "close")
            if xl is None or xs is None:
                continue
            exit_val, exit_ts = (xl - xs), times[j]
            if crossed(j) == -direction:
                break
        if exit_val is None:
            i += 1
            continue
        # spread value is bounded to [0, width] economically; clamp the short-leg
        # mark so a stale OTM print can't push the spread value negative/explosive.
        exit_val = max(exit_val, 0.0)
        gross = exit_val / net_debit - 1.0
        # cost: round-trip on EACH leg, expressed as fraction of NET debit.
        if apply_cost:
            cost_long = NSE_OPTION_COST.round_trip_pct(el, lot_qty=lot) * el
            cost_short = NSE_OPTION_COST.round_trip_pct(es, lot_qty=lot) * es
            cost = (cost_long + cost_short) / net_debit
        else:
            cost = 0.0
        trades.append({"time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost,
                       "otype": otype, "entry": net_debit, "exit": exit_val})
        while i < n - 1 and times[i] <= exit_ts:
            i += 1
    return trades


# ════════════════════════════════════════════════════════════════════════════
# VARIANT C — premium sell (short the ATM, collect premium, defined stop)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class _CfgC:
    time_stop_bars: int = 13
    stop_pct: float = 1.0         # stop when premium rises by this fraction (x2 => -100%)
    take_profit_pct: float = 0.5  # cover when premium decays this fraction (theta capture)
    fast: int = 12
    slow: int = 26
    signal: int = 9


def run_premium_sell(spot: pd.DataFrame, params: dict, *, book: OptionBook, apply_cost: bool = True) -> list:
    """Short the ATM on the SAME signal: a +1 (bullish) cross SELLS the ATM PUT
    (theta+, benefits if spot holds/rises); a -1 SELLS the ATM CALL. Return is the
    SHORT P&L as a fraction of premium collected: (entry - exit)/entry. Defined
    stop at entry x (1+stop_pct); take-profit at entry x (1-take_profit_pct). Cost
    is the same ~3% RT on the premium (a seller crosses the spread too)."""
    cfg = _CfgC(**{k: params[k] for k in params if k in _CfgC().__dict__})
    s = spot.sort_values("time").reset_index(drop=True)
    if len(s) < cfg.slow + 5:
        return []
    times = pd.to_datetime(s["time"], utc=True).tolist()
    closes = s["close"].astype(float).tolist()
    crossed = _macd_signals(closes, cfg.fast, cfg.slow, cfg.signal)
    lot = LOT.get(str(s.attrs.get("underlying", "NIFTY")).upper(), 50)

    trades: list = []
    i, n = 1, len(s)
    while i < n - 1:
        direction = crossed(i)
        if direction == 0:
            i += 1
            continue
        # INVERT: bullish cross sells the PUT, bearish cross sells the CALL.
        otype = "PE" if direction > 0 else "CE"
        entry_ts = times[i + 1]
        leg = book.atm_leg(entry_ts, closes[i], otype)
        if leg is None:
            i += 1
            continue
        _strike, leg_df = leg
        entry_px = _premium_at(leg_df, entry_ts, "open")
        if entry_px is None:
            i += 1
            continue

        exit_px, exit_ts = None, entry_ts
        for j in range(i + 1, min(i + 1 + cfg.time_stop_bars, n)):
            px = _premium_at(leg_df, times[j], "close")
            if px is None:
                continue
            exit_px, exit_ts = px, times[j]
            if px >= entry_px * (1 + cfg.stop_pct):           # short stop (loss)
                break
            if px <= entry_px * (1 - cfg.take_profit_pct):    # theta take-profit
                break
            if crossed(j) == -direction:                       # signal flip
                break
        if exit_px is None:
            i += 1
            continue
        gross = (entry_px - exit_px) / entry_px    # SHORT P&L as fraction of premium
        cost = NSE_OPTION_COST.round_trip_pct(entry_px, lot_qty=lot) if apply_cost else 0.0
        trades.append({"time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost,
                       "otype": otype, "entry": entry_px, "exit": exit_px})
        while i < n - 1 and times[i] <= exit_ts:
            i += 1
    return trades


# ── reporting helpers ────────────────────────────────────────────────────────
def _returns(trades) -> list:
    return [t["net_ret"] for t in trades]


def _heldout_ci(trades) -> dict:
    """Bootstrap 95% CI on the held-out mean per-trade net return — so a tiny-n
    'positive' isn't read as an edge. Positive lower bound = mean clears zero."""
    r = np.asarray(_returns(trades), dtype=float)
    if r.size < 3:
        return {"ci": "n<3 — no CI"}
    ci = vm.bootstrap_ci(r, stat=np.mean, n=2000)
    return {"mean": round(float(r.mean()), 4),
            "ci95": [round(ci["lo"], 4), round(ci["hi"], 4)],
            "positive_lower_bound": bool(ci["lo"] > 0)}


def _score_block(label: str, run_fn, frame: pd.DataFrame, params: dict, book: OptionBook):
    tr = run_fn(frame, params, book=book, apply_cost=True)
    summ = _summ(tr)
    print(f"  [{label}] {params}  ->  {summ}", flush=True)
    return tr, summ


# ── driver ────────────────────────────────────────────────────────────────--
async def main_async(underlying: str, dsn: str):
    print(f"\n{'='*78}\n EXPRESSION EXPERIMENTS — {underlying}  (same signal/data/cost/held-out)\n{'='*78}", flush=True)
    spot, opt = await _load(dsn, underlying)
    spot.attrs["underlying"] = underlying
    book = OptionBook.build(opt)
    dev, held = wf.split_development_heldout(spot, time_col="time")
    wf.assert_tuning_window_safe(pd.to_datetime(dev["time"]).max(), label="dev frame")
    print(f"dev bars: {len(dev)} (<{wf.HELD_OUT_START.date()})  held-out bars: {len(held)}  "
          f"option rows: {len(opt)}", flush=True)

    # ── BASELINE (long ATM, ts=13) — the wall everything is measured against ──
    base_p = {"time_stop_bars": 13, "hard_stop_pct": 0.25}
    print("\n--- BASELINE: long ATM ts=13 (cost ON) ---", flush=True)
    base_dev, _ = _score_block("DEV", run_long_atm_baseline, dev, base_p, book)
    base_held = run_long_atm_baseline(held, base_p, book=book, apply_cost=True)
    print(f"  [HELD-OUT] {base_p}  ->  {_summ(base_held)}", flush=True)
    print(f"  [HELD-OUT CI] {_heldout_ci(base_held)}", flush=True)

    results = {"baseline_longatm": {"dev": _summ(base_dev), "held": _summ(base_held),
                                     "held_ci": _heldout_ci(base_held)}}

    # ── VARIANT A: longer holds ──────────────────────────────────────────────
    print("\n--- (A) LONGER HOLDS: long ATM, vary the exit horizon (cost ON) ---", flush=True)
    a_modes = [
        ("ts=26", {"time_stop_bars": 26, "hard_stop_pct": 0.25, "hold_mode": "bars"}),
        ("ts=52", {"time_stop_bars": 52, "hard_stop_pct": 0.25, "hold_mode": "bars"}),
        ("hold_EOD", {"hard_stop_pct": 0.25, "hold_mode": "eod"}),
        ("hold_to_expiry", {"hard_stop_pct": 0.25, "hold_mode": "expiry"}),
    ]
    for label, p in a_modes:
        dev_tr, dev_s = _score_block(f"DEV {label}", run_longhold, dev, p, book)
        held_tr = run_longhold(held, p, book=book, apply_cost=True)
        print(f"  [HELD-OUT {label}] -> {_summ(held_tr)}  CI {_heldout_ci(held_tr)}", flush=True)
        results[f"A_{label}"] = {"dev": dev_s, "held": _summ(held_tr), "held_ci": _heldout_ci(held_tr)}

    # ── VARIANT B: debit vertical spread ─────────────────────────────────────
    print("\n--- (B) DEBIT VERTICAL: long ATM + short N-OTM, cost on BOTH legs ---", flush=True)
    for w in (1, 2):
        p = {"time_stop_bars": 13, "n_strikes_otm": w}
        dev_tr, dev_s = _score_block(f"DEV width={w}", run_debit_vertical, dev, p, book)
        held_tr = run_debit_vertical(held, p, book=book, apply_cost=True)
        print(f"  [HELD-OUT width={w}] -> {_summ(held_tr)}  CI {_heldout_ci(held_tr)}", flush=True)
        results[f"B_width{w}"] = {"dev": dev_s, "held": _summ(held_tr), "held_ci": _heldout_ci(held_tr)}

    # ── VARIANT C: premium sell ──────────────────────────────────────────────
    print("\n--- (C) PREMIUM SELL: short ATM, theta+ with defined stop (cost ON) ---", flush=True)
    for label, p in [
        ("stop100_tp50_ts13", {"time_stop_bars": 13, "stop_pct": 1.0, "take_profit_pct": 0.5}),
        ("stop50_tp30_ts26", {"time_stop_bars": 26, "stop_pct": 0.5, "take_profit_pct": 0.3}),
    ]:
        dev_tr, dev_s = _score_block(f"DEV {label}", run_premium_sell, dev, p, book)
        held_tr = run_premium_sell(held, p, book=book, apply_cost=True)
        print(f"  [HELD-OUT {label}] -> {_summ(held_tr)}  CI {_heldout_ci(held_tr)}", flush=True)
        results[f"C_{label}"] = {"dev": dev_s, "held": _summ(held_tr), "held_ci": _heldout_ci(held_tr)}

    # ── verdict table ────────────────────────────────────────────────────────
    print(f"\n{'='*78}\n SUMMARY — {underlying} (held-out, cost-ON, net per-trade %)\n{'='*78}", flush=True)
    print(f"{'variant':<22}{'dev_n':>6}{'dev_mean%':>10}{'held_n':>7}{'held_mean%':>11}{'held_PF':>8}  CI95_positive", flush=True)
    for name, r in results.items():
        d, h, ci = r["dev"], r["held"], r["held_ci"]
        pos = ci.get("positive_lower_bound", False)
        print(f"{name:<22}{d.get('n',0):>6}{d.get('mean_ret_pct',float('nan')):>10}"
              f"{h.get('n',0):>7}{h.get('mean_ret_pct',float('nan')):>11}{h.get('pf',float('nan')):>8}  {pos}", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    a = ap.parse_args()
    asyncio.run(main_async(a.underlying.upper(), a.dsn))


if __name__ == "__main__":
    main()
