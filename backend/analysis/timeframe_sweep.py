"""
MACD Zero-Cross Timeframe Sweep — 1m / 3m / 5m / 15m

Runs MACD(12,26,9) zero-line bullish cross on resampled option premium candles
(ATM CE+PE) across 1, 3, 5, 15-minute timeframes.

Groups:
  - weekly_series      : full expiry window, NIFTY + SENSEX weekly contracts
  - monthly_series     : full expiry window, NIFTY + SENSEX monthly contracts
  - weekly_expiry_day  : expiry-day only slice
  - monthly_expiry_day : expiry-day only slice

Exit strategies tested per timeframe:
  Series:    target_20pct, target_30pct, trail_after_20pct_dd10pct, trail_after_30pct_dd15pct
  Expiry day: target_10pct, target_20pct, trail_after_10pct_dd5pct, trail_after_20pct_dd10pct

Output: runtime/index_analytics_data/timeframe_sweep/
  summary.json, variant_results.csv, trade_results.csv, report.md
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "timeframe_sweep"

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_TIMEFRAMES = ["1m", "3m", "5m", "15m"]

TIMEFRAME_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}
TIMEFRAME_FREQ = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min"}

FULL_SERIES_EXITS: dict[str, dict[str, Any]] = {
    "target_20pct":              {"kind": "target",   "target_pct": 20.0},
    "target_30pct":              {"kind": "target",   "target_pct": 30.0},
    "trail_after_20pct_dd10pct": {"kind": "trailing", "activation_pct": 20.0, "trail_drawdown_pct": 10.0},
    "trail_after_30pct_dd15pct": {"kind": "trailing", "activation_pct": 30.0, "trail_drawdown_pct": 15.0},
}

EXPIRY_DAY_EXITS: dict[str, dict[str, Any]] = {
    "target_10pct":              {"kind": "target",   "target_pct": 10.0},
    "target_20pct":              {"kind": "target",   "target_pct": 20.0},
    "trail_after_10pct_dd5pct":  {"kind": "trailing", "activation_pct": 10.0, "trail_drawdown_pct": 5.0},
    "trail_after_20pct_dd10pct": {"kind": "trailing", "activation_pct": 20.0, "trail_drawdown_pct": 10.0},
}

MIN_CANDLES = 40

# ── MACD engine (inline — avoids broken analytics/__init__ import) ─────────────

def _compute_ema(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    result: list[Optional[float]] = [None] * n
    if n < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    k = 2.0 / (period + 1)
    prev = sma
    for i in range(period, n):
        v = values[i] * k + prev * (1.0 - k)
        result[i] = v
        prev = v
    return result


def _compute_macd(closes: list[float]) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    n = len(closes)
    ema_fast = _compute_ema(closes, 12)
    ema_slow = _compute_ema(closes, 26)
    macd_line: list[Optional[float]] = [
        (ema_fast[i] - ema_slow[i]) if ema_fast[i] is not None and ema_slow[i] is not None else None
        for i in range(n)
    ]
    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), -1)
    signal_line: list[Optional[float]] = [None] * n
    histogram: list[Optional[float]] = [None] * n
    if first_valid == -1:
        return macd_line, signal_line, histogram
    valid_macd = [macd_line[i] for i in range(first_valid, n)]  # type: ignore[misc]
    ema_sig = _compute_ema(valid_macd, 9)  # type: ignore[arg-type]
    for j, v in enumerate(ema_sig):
        idx = first_valid + j
        signal_line[idx] = v
        if macd_line[idx] is not None and v is not None:
            histogram[idx] = macd_line[idx] - v  # type: ignore[operator]
    return macd_line, signal_line, histogram

# ── Data loading / resampling ─────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _load_csv(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    if not path.exists():
        raise FileNotFoundError(path)
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "oi"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@lru_cache(maxsize=1024)
def _resample(path_str: str, timeframe: str) -> pd.DataFrame:
    df = _load_csv(path_str)
    if timeframe == "1m":
        return df.copy()
    freq = TIMEFRAME_FREQ[timeframe]
    indexed = df.set_index("time").sort_index()
    agg = {c: ("first" if c == "open" else "max" if c == "high" else "min" if c == "low"
                else "last" if c == "close" else "sum" if c == "volume" else "last")
           for c in ("open", "high", "low", "close", "volume", "oi") if c in indexed.columns}
    resampled = (
        indexed.resample(freq, label="right", closed="right")
        .agg(agg)
        .dropna(subset=["open", "close"])
        .reset_index()
    )
    return resampled


@lru_cache(maxsize=8)
def _spot_frame(underlying: str) -> pd.DataFrame:
    return _load_csv(f"spot/underlying={underlying}/1minute.csv.gz")


def _spot_price_at(underlying: str, ts: pd.Timestamp) -> Optional[float]:
    spot = _spot_frame(underlying).set_index("time").sort_index()
    before = spot.loc[:ts]
    if not before.empty:
        return float(before.iloc[-1]["close"])
    after = spot.loc[ts:]
    if not after.empty:
        return float(after.iloc[0]["close"])
    return None

# ── Series descriptors ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SeriesDesc:
    series_id: str
    underlying: str
    expiry_kind: str
    expiry: str
    strike: float
    spot_at_start: float
    pair_start_time: str
    ce_path: str
    pe_path: str
    ce_symbol: str
    pe_symbol: str


def _build_descriptors() -> list[SeriesDesc]:
    raw = json.loads((DATA_ROOT / "contract_index.json").read_text())
    metas = []
    for item in raw.values():
        if not all([item.get("file_path"), item.get("candle_count"),
                    item.get("earliest_candle"), item.get("latest_candle"),
                    item.get("strike") is not None, item.get("option_type")]):
            continue
        metas.append(item)

    by_group: dict[tuple[str, str, str], list[dict]] = {}
    for m in metas:
        by_group.setdefault((m["underlying"], m["expiry_kind"], m["expiry"]), []).append(m)

    descs: list[SeriesDesc] = []
    for (underlying, expiry_kind, expiry), group in sorted(by_group.items()):
        ce_map = {float(m["strike"]): m for m in group if m["option_type"] == "CE"}
        pe_map = {float(m["strike"]): m for m in group if m["option_type"] == "PE"}
        common = sorted(set(ce_map) & set(pe_map))
        if not common:
            continue

        candidates = []
        for strike in common:
            ce = ce_map[strike]; pe = pe_map[strike]
            pair_start = max(pd.Timestamp(ce["earliest_candle"]), pd.Timestamp(pe["earliest_candle"]))
            pair_end   = min(pd.Timestamp(ce["latest_candle"]),   pd.Timestamp(pe["latest_candle"]))
            if pair_end <= pair_start:
                continue
            candidates.append((strike, pair_start, ce, pe))
        if not candidates:
            continue

        group_start_day = min(p for _, p, _, _ in candidates).date()
        spot = _spot_price_at(underlying, min(p for _, p, _, _ in candidates))
        if spot is None:
            continue

        eligible = [c for c in candidates if c[1].date() == group_start_day] or candidates
        strike, pair_start, ce, pe = min(eligible, key=lambda c: (abs(c[0] - spot), c[1], c[0]))

        descs.append(SeriesDesc(
            series_id=f"{underlying}|{expiry_kind}|{expiry}",
            underlying=underlying,
            expiry_kind=expiry_kind,
            expiry=expiry,
            strike=float(strike),
            spot_at_start=float(round(spot, 4)),
            pair_start_time=pair_start.isoformat(),
            ce_path=ce["file_path"],
            pe_path=pe["file_path"],
            ce_symbol=ce["trading_symbol"],
            pe_symbol=pe["trading_symbol"],
        ))
    return descs

# ── Exit simulation ───────────────────────────────────────────────────────────

def _simulate_exit(candles: list[dict], entry_idx: int, exit_spec: dict) -> dict:
    kind = exit_spec["kind"]
    entry_price = float(candles[entry_idx]["close"])

    if kind == "target":
        target = entry_price * (1.0 + exit_spec["target_pct"] / 100.0)
        for i in range(entry_idx + 1, len(candles)):
            if float(candles[i]["high"]) >= target:
                return {"exit_idx": i, "exit_price": target,
                        "exit_time": str(candles[i]["time"]),
                        "exit_reason": f"target_{int(exit_spec['target_pct'])}pct_hit"}
        i = len(candles) - 1
        return {"exit_idx": i, "exit_price": float(candles[i]["close"]),
                "exit_time": str(candles[i]["time"]), "exit_reason": "hold_to_end"}

    if kind == "trailing":
        act_price = entry_price * (1.0 + exit_spec["activation_pct"] / 100.0)
        dd = exit_spec["trail_drawdown_pct"]
        peak = entry_price
        activated = False
        for i in range(entry_idx + 1, len(candles)):
            c = candles[i]
            hi = float(c["high"]); cl = float(c["close"])
            if hi > peak:
                peak = hi
            if not activated and hi >= act_price:
                activated = True
            if activated and cl <= peak * (1.0 - dd / 100.0):
                return {"exit_idx": i, "exit_price": cl,
                        "exit_time": str(c["time"]),
                        "exit_reason": f"trail_hit_act{int(exit_spec['activation_pct'])}dd{int(dd)}"}
        i = len(candles) - 1
        return {"exit_idx": i, "exit_price": float(candles[i]["close"]),
                "exit_time": str(candles[i]["time"]), "exit_reason": "hold_to_end"}

    raise ValueError(f"Unknown exit kind: {kind}")


def _max_possible(candles: list[dict], entry_idx: int) -> tuple[float, float, int]:
    entry_price = float(candles[entry_idx]["close"])
    max_price = entry_price
    max_idx = entry_idx
    for idx in range(entry_idx, len(candles)):
        hi = float(candles[idx]["high"])
        if hi > max_price:
            max_price = hi
            max_idx = idx
    max_ret = (max_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
    return round(max_ret, 4), round(max_price, 4), max_idx - entry_idx

# ── Trade simulation ──────────────────────────────────────────────────────────

def _simulate_series(desc: SeriesDesc, timeframe: str, exit_name: str,
                     exit_spec: dict, expiry_day_only: bool) -> list[dict]:
    trades = []
    bar_min = TIMEFRAME_MINUTES[timeframe]

    for opt_type, path, symbol in (
        ("CE", desc.ce_path, desc.ce_symbol),
        ("PE", desc.pe_path, desc.pe_symbol),
    ):
        frame = _resample(path, timeframe)
        frame = frame[frame["time"] >= pd.Timestamp(desc.pair_start_time)].copy()
        if expiry_day_only:
            expiry_date = pd.Timestamp(desc.expiry).date()
            frame = frame[frame["time"].dt.date == expiry_date].copy()
        frame = frame.reset_index(drop=True)
        if len(frame) < MIN_CANDLES:
            continue

        candles = frame.to_dict("records")
        closes = [float(c["close"]) for c in candles]
        macd_line, _, _ = _compute_macd(closes)

        idx = 1
        while idx < len(candles):
            prev = macd_line[idx - 1]
            curr = macd_line[idx]
            # Zero-line bullish cross: prev <= 0, curr > 0
            if prev is None or curr is None or not (prev <= 0.0 and curr > 0.0):
                idx += 1
                continue

            entry_price = float(candles[idx]["close"])
            if entry_price <= 0.0:
                idx += 1
                continue

            exit_result = _simulate_exit(candles, idx, exit_spec)
            exit_idx = int(exit_result["exit_idx"])
            exit_price = float(exit_result["exit_price"])
            ret_pct = (exit_price - entry_price) / entry_price * 100.0
            max_ret, max_price, bars_to_max = _max_possible(candles, idx)
            holding_bars = max(exit_idx - idx, 0)

            trades.append({
                "series_id":       desc.series_id,
                "underlying":      desc.underlying,
                "expiry_kind":     desc.expiry_kind,
                "expiry":          desc.expiry,
                "option_type":     opt_type,
                "symbol":          symbol,
                "strike":          desc.strike,
                "timeframe":       timeframe,
                "exit_strategy":   exit_name,
                "expiry_day_only": expiry_day_only,
                "entry_time":      str(candles[idx]["time"]),
                "entry_price":     round(entry_price, 4),
                "entry_macd":      round(float(curr), 6),
                "exit_time":       exit_result["exit_time"],
                "exit_price":      round(exit_price, 4),
                "exit_reason":     exit_result["exit_reason"],
                "return_pct":      round(ret_pct, 4),
                "max_possible_return_pct": max_ret,
                "max_possible_price": max_price,
                "holding_bars":    holding_bars,
                "holding_minutes": holding_bars * bar_min,
                "bars_to_max":     bars_to_max,
            })
            idx = exit_idx + 1

    return trades

# ── Statistics ────────────────────────────────────────────────────────────────

def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {
            "opportunities": 0, "win_rate": 0.0, "avg_return_pct": 0.0,
            "median_return_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "max_realized_pct": 0.0, "avg_max_possible_pct": 0.0,
            "avg_holding_minutes": 0.0, "capture_ratio": 0.0,
            "profit_factor": 0.0, "score": 0.0,
        }
    returns = [float(t["return_pct"]) for t in trades]
    possible = [float(t["max_possible_return_pct"]) for t in trades]
    holds = [float(t["holding_minutes"]) for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    capture = [r / p for r, p in zip(returns, possible) if p > 0]
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    n = len(trades)
    wr = len(wins) / n
    avg = statistics.fmean(returns)
    med = statistics.median(returns)
    score = round(((avg * 0.4 + med * 0.6) * max(wr, 0.01) * math.log1p(n)), 4)
    return {
        "opportunities":      n,
        "win_rate":           round(wr, 4),
        "avg_return_pct":     round(avg, 4),
        "median_return_pct":  round(med, 4),
        "avg_win_pct":        round(statistics.fmean(wins), 4) if wins else 0.0,
        "avg_loss_pct":       round(statistics.fmean(losses), 4) if losses else 0.0,
        "max_realized_pct":   round(max(returns), 4),
        "avg_max_possible_pct": round(statistics.fmean(possible), 4),
        "avg_holding_minutes": round(statistics.fmean(holds), 2),
        "capture_ratio":      round(statistics.fmean(capture), 4) if capture else 0.0,
        "profit_factor":      round(pf, 4) if not math.isinf(pf) else 999.0,
        "score":              score,
    }

# ── Main runner ───────────────────────────────────────────────────────────────

def run() -> dict[str, Any]:
    print("Loading series descriptors...", flush=True)
    all_descs = _build_descriptors()
    weekly  = [d for d in all_descs if d.expiry_kind == "weekly"]
    monthly = [d for d in all_descs if d.expiry_kind == "monthly"]
    print(f"  weekly={len(weekly)}  monthly={len(monthly)}", flush=True)

    groups = [
        ("weekly_series",       weekly,  False),
        ("monthly_series",      monthly, False),
        ("weekly_expiry_day",   weekly,  True),
        ("monthly_expiry_day",  monthly, True),
    ]

    all_trades: list[dict] = []
    variant_rows: list[dict] = []
    group_summaries: dict[str, Any] = {}

    for group_name, descs, expiry_day_only in groups:
        print(f"\n{'='*60}\nGroup: {group_name}  ({len(descs)} series)", flush=True)
        exits = EXPIRY_DAY_EXITS if expiry_day_only else FULL_SERIES_EXITS
        group_trades: list[dict] = []
        by_variant: dict[str, list[dict]] = defaultdict(list)

        for tf in TARGET_TIMEFRAMES:
            for exit_name, exit_spec in exits.items():
                variant_key = f"{group_name}|{tf}|{exit_name}"
                print(f"  {tf:4s} × {exit_name}", end="", flush=True)
                trades: list[dict] = []
                for desc in descs:
                    trades.extend(_simulate_series(desc, tf, exit_name, exit_spec, expiry_day_only))
                print(f"  → {len(trades)} trades", flush=True)
                group_trades.extend(trades)
                by_variant[variant_key] = trades

                s = _stats(trades)
                variant_rows.append({
                    "variant_key":       variant_key,
                    "group":             group_name,
                    "timeframe":         tf,
                    "exit_strategy":     exit_name,
                    "expiry_day_only":   expiry_day_only,
                    **s,
                })

        all_trades.extend(group_trades)

        # Best variant per timeframe
        best_per_tf: dict[str, dict] = {}
        for tf in TARGET_TIMEFRAMES:
            tf_rows = [r for r in variant_rows if r["group"] == group_name and r["timeframe"] == tf]
            if tf_rows:
                best = max(tf_rows, key=lambda r: r["score"])
                best_per_tf[tf] = best

        # Per-underlying breakdown
        per_ul: dict[str, dict] = {}
        for ul in ("NIFTY", "SENSEX"):
            ul_trades = [t for t in group_trades if t["underlying"] == ul]
            per_ul[ul] = _stats(ul_trades)

        group_summaries[group_name] = {
            "series_count":  len(descs),
            "total_trades":  len(group_trades),
            "overall":       _stats(group_trades),
            "per_underlying": per_ul,
            "best_per_tf":   best_per_tf,
        }

    results = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "data_root":    str(DATA_ROOT),
        "timeframes":   TARGET_TIMEFRAMES,
        "groups":       group_summaries,
        "total_trades": len(all_trades),
    }

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _write_outputs(results, variant_rows, all_trades)
    return results


def _write_outputs(results: dict, variant_rows: list[dict], trades: list[dict]) -> None:
    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(results, indent=2))

    if variant_rows:
        fields = sorted({k for r in variant_rows for k in r})
        with (OUTPUT_ROOT / "variant_results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(variant_rows)

    if trades:
        fields = sorted({k for t in trades for k in t})
        with (OUTPUT_ROOT / "trade_results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(trades)

    _write_report(results)
    print(f"\nOutputs written to: {OUTPUT_ROOT}", flush=True)


def _write_report(results: dict) -> None:
    lines = [
        "# MACD Zero-Cross Timeframe Sweep  (1m / 3m / 5m / 15m)",
        "",
        f"Generated : {results['generated_at']}",
        f"Total trades simulated : {results['total_trades']:,}",
        "",
        "---",
        "",
    ]

    for group_name, g in results["groups"].items():
        o = g["overall"]
        lines += [
            f"## {group_name}",
            f"Series: {g['series_count']}  |  Trades: {g['total_trades']:,}",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Opportunities | {o['opportunities']:,} |",
            f"| Win Rate | {o['win_rate']*100:.1f}% |",
            f"| Avg Return | {o['avg_return_pct']:+.2f}% |",
            f"| Median Return | {o['median_return_pct']:+.2f}% |",
            f"| Avg Win | {o['avg_win_pct']:+.2f}% |",
            f"| Avg Loss | {o['avg_loss_pct']:+.2f}% |",
            f"| Profit Factor | {o['profit_factor']:.2f} |",
            f"| Capture Ratio | {o['capture_ratio']*100:.1f}% |",
            f"| Avg Holding | {o['avg_holding_minutes']:.0f} min |",
            "",
            "### Best variant per timeframe",
            "",
            "| TF | Exit Strategy | Trades | Win% | Avg Ret% | Median Ret% | Score |",
            "|----|--------------|--------|------|----------|-------------|-------|",
        ]
        for tf in ["1m", "3m", "5m", "15m"]:
            best = g["best_per_tf"].get(tf)
            if best:
                lines.append(
                    f"| {tf} | {best['exit_strategy']} "
                    f"| {best['opportunities']} "
                    f"| {best['win_rate']*100:.1f}% "
                    f"| {best['avg_return_pct']:+.2f}% "
                    f"| {best['median_return_pct']:+.2f}% "
                    f"| {best['score']:.2f} |"
                )
            else:
                lines.append(f"| {tf} | — | — | — | — | — | — |")

        lines += ["", "### By underlying", ""]
        lines += ["| Underlying | Trades | Win% | Avg Ret% | Median Ret% |",
                  "|------------|--------|------|----------|-------------|"]
        for ul, s in g["per_underlying"].items():
            lines.append(
                f"| {ul} | {s['opportunities']} "
                f"| {s['win_rate']*100:.1f}% "
                f"| {s['avg_return_pct']:+.2f}% "
                f"| {s['median_return_pct']:+.2f}% |"
            )
        lines += ["", "---", ""]

    (OUTPUT_ROOT / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    results = run()
    # Print compact summary
    print("\n" + "="*70)
    print("TIMEFRAME SWEEP — SUMMARY")
    print("="*70)
    for group_name, g in results["groups"].items():
        print(f"\n{group_name.upper()}")
        print(f"  {'TF':<5} {'Exit':<30} {'N':>5} {'Win%':>6} {'Avg%':>7} {'Med%':>7} {'Score':>8}")
        print(f"  {'-'*4} {'-'*30} {'-'*5} {'-'*6} {'-'*7} {'-'*7} {'-'*8}")
        tf_rows = sorted(
            [r for r in [g["best_per_tf"].get(tf) for tf in TARGET_TIMEFRAMES] if r],
            key=lambda r: r["score"], reverse=True,
        )
        for r in tf_rows:
            print(f"  {r['timeframe']:<5} {r['exit_strategy']:<30} "
                  f"{r['opportunities']:>5} {r['win_rate']*100:>5.1f}% "
                  f"{r['avg_return_pct']:>+7.2f}% {r['median_return_pct']:>+7.2f}% "
                  f"{r['score']:>8.2f}")
