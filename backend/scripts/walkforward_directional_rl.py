"""Walk-forward test of the directional-options RL policy.

Runs two arms over the same historical bars from the persisted runtime
data store:

  * "rl"      — policy decides act/skip, size multiplier, and strike
                from the selector's top-K.
  * "baseline" — selector.best taken at size_multiplier=1.0 every time,
                no skip ever. Mirrors the pre-RL behavior with all
                hard gates removed.

For each window (default 5-trading-day buckets) we report trade count,
win rate, sum of R-multiples, mean R, hit-rate, max drawdown, and the
policy's posterior `n_seen`. Online-learning convention: the policy is
NEVER reset between windows — it learns as the test progresses, so the
later windows reflect a more informed posterior. That matches how it
will behave in live paper trading.

USAGE (inside the backend container):

    docker exec -it nomadcurie_backend \
        python /app/scripts/walkforward_directional_rl.py \
        --underlying NIFTY --timeframe 5minute --max-bars 6000 \
        --window-bars 375  # ~one trading day per window on 5m
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine, timeframe_minutes
from directional_options.policy import DirectionalPolicy, get_policy, reset_policy_for_tests
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.schemas import ContractCandidate, DirectionalSignal, RegimeSnapshot
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine


def _buy_fill(option_price: float, contract: ContractCandidate, entry_slip: float) -> float:
    spread_half = (option_price * contract.spread_pct) / 2.0
    return option_price + spread_half + (option_price * entry_slip)


def _sell_fill(option_price: float, current_mark: float, contract: ContractCandidate, exit_slip: float) -> float:
    spread_half = (current_mark * contract.spread_pct) / 2.0
    return max(0.0, current_mark - spread_half - (option_price * exit_slip))


def _mark_price(store: DirectionalOptionsDataStore, file_path: str, timestamp: pd.Timestamp, fallback: float) -> float:
    frame = store.load_option_frame(file_path)
    rows = frame.loc[frame["time"] <= timestamp]
    if rows.empty:
        return fallback
    return float(rows.iloc[-1]["close"])


def _exit_decision(
    *,
    position: dict[str, Any],
    spot_price: float,
    current_mark: float,
    timestamp: pd.Timestamp,
    expiry_guard_days: float,
    trail_giveback_pct: float,
) -> Optional[str]:
    if current_mark <= position["stop_price"]:
        return "stop"
    if current_mark >= position["target_price"]:
        return "target"
    expiry_date = pd.Timestamp(position["expiry"]).date()
    if (expiry_date - timestamp.date()).days <= expiry_guard_days:
        return "expiry_guard"
    if position["held_bars"] >= position["max_horizon_bars"]:
        return "horizon"
    if position["peak_mark"] > position["entry_mark"]:
        giveback = (position["peak_mark"] - current_mark) / position["peak_mark"]
        if giveback >= trail_giveback_pct:
            return "trail"
    direction = position["direction"]
    stop_underlying = position["stop_underlying"]
    if direction == "CE" and spot_price <= stop_underlying:
        return "underlying_stop"
    if direction == "PE" and spot_price >= stop_underlying:
        return "underlying_stop"
    return None


def _featurize_top_k(
    *,
    selector_payload: dict[str, Any],
    signal: DirectionalSignal,
    regime: RegimeSnapshot,
    policy: DirectionalPolicy,
) -> tuple[Optional[ContractCandidate], dict[str, Any]]:
    candidates = list(selector_payload.get("candidates") or [])
    if not candidates:
        best = selector_payload.get("best")
        return best, {"act": False, "reason": "no_candidates"}
    signal_dict = asdict(signal)
    regime_dict = asdict(regime)
    candidate_dicts = [asdict(c) for c in candidates]
    best_idx, samples = policy.rank_candidates(
        signal=signal_dict, candidates=candidate_dicts, regime=regime_dict
    )
    if best_idx is None:
        return candidates[0] if candidates else None, {"act": False, "reason": "policy_no_rank"}
    chosen = candidates[best_idx]
    decision = policy.decide(signal=signal_dict, candidate=candidate_dicts[best_idx], regime=regime_dict)
    return chosen, {
        "act": decision.act,
        "size_multiplier": decision.size_multiplier,
        "sampled_value": decision.sampled_value,
        "posterior_mean": decision.posterior_mean,
        "posterior_var": decision.posterior_var,
        "candidate_index": best_idx,
        "n_seen": decision.n_seen,
        "samples": samples,
    }


def _open_position(
    *,
    candidate: ContractCandidate,
    signal: DirectionalSignal,
    regime: RegimeSnapshot,
    timestamp: pd.Timestamp,
    spot_price: float,
    size_multiplier: float,
    config: dict[str, Any],
    cash: float,
    risk_engine: DirectionalOptionsRiskEngine,
) -> Optional[dict[str, Any]]:
    risk_decision = risk_engine.approve(
        candidate=candidate,
        signal=signal,
        equity=cash,
        size_multiplier=size_multiplier,
    )
    if not risk_decision.approved or risk_decision.quantity_units <= 0:
        return None
    risk_cfg = config["risk"]
    execution_cfg = config["execution"]
    entry_slip = float(execution_cfg["entry_slippage_pct"])
    fee_per_unit = float(execution_cfg["fee_per_unit"])
    entry_fill = _buy_fill(candidate.option_price, candidate, entry_slip)
    entry_total = (entry_fill * risk_decision.quantity_units) + (fee_per_unit * risk_decision.quantity_units)
    if entry_total > cash:
        return None
    stop_price = candidate.option_price * (1.0 - float(risk_cfg["planned_stop_pct"]))
    target_price = candidate.option_price * (1.0 + float(risk_cfg["profit_target_pct"]))
    stop_under = (
        spot_price - signal.expected_move * 0.55
        if signal.direction == "CE"
        else spot_price + signal.expected_move * 0.55
    )
    return {
        "entry_time": timestamp.isoformat(),
        "entry_spot": spot_price,
        "entry_mark": candidate.option_price,
        "entry_fill": entry_fill,
        "entry_total": entry_total,
        "stop_price": stop_price,
        "target_price": target_price,
        "stop_underlying": stop_under,
        "quantity_units": risk_decision.quantity_units,
        "quantity_lots": risk_decision.quantity_lots,
        "max_horizon_bars": signal.expected_horizon_bars,
        "held_bars": 0,
        "peak_mark": candidate.option_price,
        "expiry": candidate.expiry,
        "expiry_kind": candidate.expiry_kind,
        "direction": signal.direction,
        "regime": regime.label,
        "confidence": signal.confidence,
        "delta_bucket": candidate.delta_bucket,
        "trading_symbol": candidate.trading_symbol,
        "file_path": candidate.file_path,
        "contract_spread_pct": candidate.spread_pct,
        "risk_budget": risk_decision.risk_budget,
        "size_multiplier": size_multiplier,
        "feature_candidate": candidate,
    }


def run_arm(
    *,
    arm: str,
    underlying: str,
    timeframe: str,
    frame: pd.DataFrame,
    config: dict[str, Any],
    store: DirectionalOptionsDataStore,
    feature_engine: FeatureEngine,
    regime_engine: RegimeClassifier,
    signal_engine: DirectionalSignalEngine,
    selector: OptionSelectionEngine,
    risk_engine: DirectionalOptionsRiskEngine,
    policy: Optional[DirectionalPolicy],
    window_bars: int,
) -> dict[str, Any]:
    risk_cfg = config["risk"]
    execution_cfg = config["execution"]
    starting_equity = float(risk_cfg["starting_equity"])
    fee_per_unit = float(execution_cfg["fee_per_unit"])
    exit_slip = float(execution_cfg["exit_slippage_pct"])
    expiry_guard_days = float(risk_cfg["expiry_guard_days"])
    trail_giveback_pct = float(risk_cfg["trail_giveback_pct"])

    cash = starting_equity
    open_position: Optional[dict[str, Any]] = None
    trades: list[dict[str, Any]] = []
    window_results: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "sum_r": 0.0, "sum_pnl": 0.0,
        "starting_n_seen": None, "ending_n_seen": None,
    })

    next_position_id = 0

    for idx, row in frame.reset_index(drop=True).iterrows():
        timestamp = pd.Timestamp(row["time"])
        window_idx = int(idx) // window_bars
        spot_price = float(row["close"])
        regime = regime_engine.classify(row, timeframe=timeframe)

        if window_results[window_idx]["starting_n_seen"] is None:
            window_results[window_idx]["starting_n_seen"] = policy._value_model.n_seen if policy else 0

        if open_position is not None:
            current_mark = _mark_price(store, open_position["file_path"], timestamp, open_position["entry_mark"])
            open_position["held_bars"] += 1
            open_position["peak_mark"] = max(open_position["peak_mark"], current_mark)
            exit_reason = _exit_decision(
                position=open_position,
                spot_price=spot_price,
                current_mark=current_mark,
                timestamp=timestamp,
                expiry_guard_days=expiry_guard_days,
                trail_giveback_pct=trail_giveback_pct,
            )
            if exit_reason:
                exit_fill = _sell_fill(open_position["entry_mark"], current_mark, open_position["feature_candidate"], exit_slip)
                qty = open_position["quantity_units"]
                pnl = ((exit_fill - open_position["entry_fill"]) * qty) - (2.0 * fee_per_unit * qty)
                cash += (exit_fill * qty) - (fee_per_unit * qty)
                risk_budget = open_position["risk_budget"]
                size_mult = open_position["size_multiplier"]
                denom = max(risk_budget * size_mult, 1.0)
                r_multiple = float(max(min(pnl / denom, 5.0), -3.0))
                trade = {
                    "underlying": underlying,
                    "direction": open_position["direction"],
                    "regime": open_position["regime"],
                    "delta_bucket": open_position["delta_bucket"],
                    "confidence": open_position["confidence"],
                    "entry_time": open_position["entry_time"],
                    "exit_time": timestamp.isoformat(),
                    "held_bars": open_position["held_bars"],
                    "entry_fill": round(open_position["entry_fill"], 4),
                    "exit_fill": round(exit_fill, 4),
                    "qty": qty,
                    "pnl": round(pnl, 2),
                    "r_multiple": round(r_multiple, 4),
                    "exit_reason": exit_reason,
                    "size_multiplier": size_mult,
                }
                trades.append(trade)
                position_id = open_position.get("position_id")
                if policy is not None and position_id:
                    policy.record_close(position_id=position_id, realized_pnl=pnl)
                bucket = window_results[open_position["entry_window"]]
                bucket["trades"] += 1
                bucket["sum_r"] += r_multiple
                bucket["sum_pnl"] += pnl
                if pnl > 0:
                    bucket["wins"] += 1
                elif pnl < 0:
                    bucket["losses"] += 1
                open_position = None
            window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0
            continue

        # No open position — look for entry
        signal = signal_engine.predict(row, regime, timeframe)
        if signal is None:
            window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0
            continue

        selection = selector.select(
            underlying=underlying,
            timestamp=timestamp,
            spot_price=spot_price,
            row=row,
            signal=signal,
            regime=regime,
            timeframe=timeframe,
        )
        if not selection.get("candidates"):
            window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0
            continue

        if arm == "rl" and policy is not None:
            chosen, decision = _featurize_top_k(
                selector_payload=selection,
                signal=signal,
                regime=regime,
                policy=policy,
            )
            if chosen is None or not decision.get("act"):
                window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0
                continue
            size_mult = float(decision.get("size_multiplier") or 1.0)
        else:
            chosen = selection["best"]
            decision = {}
            size_mult = 1.0

        if chosen is None:
            continue

        pos = _open_position(
            candidate=chosen,
            signal=signal,
            regime=regime,
            timestamp=timestamp,
            spot_price=spot_price,
            size_multiplier=size_mult,
            config=config,
            cash=cash,
            risk_engine=risk_engine,
        )
        if pos is None:
            window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0
            continue
        pos["entry_window"] = window_idx
        next_position_id += 1
        pos["position_id"] = f"{arm}-{next_position_id}"
        cash -= pos["entry_total"]
        if policy is not None and arm == "rl":
            policy.register_open(
                position_id=pos["position_id"],
                signal=asdict(signal),
                candidate=asdict(chosen),
                regime=asdict(regime),
                size_multiplier=size_mult,
                risk_budget=pos["risk_budget"],
            )
        open_position = pos
        window_results[window_idx]["ending_n_seen"] = policy._value_model.n_seen if policy else 0

    # Force-close any dangling position at the last bar so PnL is final.
    if open_position is not None:
        last_row = frame.iloc[-1]
        last_time = pd.Timestamp(last_row["time"])
        last_spot = float(last_row["close"])
        current_mark = _mark_price(store, open_position["file_path"], last_time, open_position["entry_mark"])
        exit_fill = _sell_fill(open_position["entry_mark"], current_mark, open_position["feature_candidate"], exit_slip)
        qty = open_position["quantity_units"]
        pnl = ((exit_fill - open_position["entry_fill"]) * qty) - (2.0 * fee_per_unit * qty)
        cash += (exit_fill * qty) - (fee_per_unit * qty)
        denom = max(open_position["risk_budget"] * open_position["size_multiplier"], 1.0)
        r_multiple = float(max(min(pnl / denom, 5.0), -3.0))
        trades.append({
            "underlying": underlying,
            "direction": open_position["direction"],
            "regime": open_position["regime"],
            "delta_bucket": open_position["delta_bucket"],
            "confidence": open_position["confidence"],
            "entry_time": open_position["entry_time"],
            "exit_time": last_time.isoformat(),
            "held_bars": open_position["held_bars"],
            "entry_fill": round(open_position["entry_fill"], 4),
            "exit_fill": round(exit_fill, 4),
            "qty": qty,
            "pnl": round(pnl, 2),
            "r_multiple": round(r_multiple, 4),
            "exit_reason": "session_end",
            "size_multiplier": open_position["size_multiplier"],
        })
        if policy is not None and open_position.get("position_id"):
            policy.record_close(position_id=open_position["position_id"], realized_pnl=pnl)
        bucket = window_results[open_position["entry_window"]]
        bucket["trades"] += 1
        bucket["sum_r"] += r_multiple
        bucket["sum_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

    total_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    sum_r = sum(t["r_multiple"] for t in trades)
    sum_pnl = sum(t["pnl"] for t in trades)
    win_rate = wins / max(total_trades, 1)
    mean_r = sum_r / max(total_trades, 1)
    sharpe = 0.0
    if total_trades >= 2:
        rs = [t["r_multiple"] for t in trades]
        mean = statistics.mean(rs)
        stdev = statistics.pstdev(rs) or 1e-6
        sharpe = mean / stdev * math.sqrt(252.0)

    # Equity curve max drawdown
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    return {
        "arm": arm,
        "trade_count": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "sum_pnl": round(sum_pnl, 2),
        "sum_r": round(sum_r, 4),
        "mean_r": round(mean_r, 4),
        "sharpe_annualized": round(sharpe, 4),
        "ending_equity": round(starting_equity + sum_pnl, 2),
        "max_drawdown": round(max_dd, 4),
        "windows": [
            {"window": int(w), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in bucket.items()}}
            for w, bucket in sorted(window_results.items())
        ],
        "exit_reason_breakdown": dict(
            sorted(
                {er: sum(1 for t in trades if t["exit_reason"] == er) for er in {t["exit_reason"] for t in trades}}.items()
            )
        ),
        "regime_breakdown": dict(
            sorted(
                {rg: sum(1 for t in trades if t["regime"] == rg) for rg in {t["regime"] for t in trades}}.items()
            )
        ),
        "delta_bucket_breakdown": dict(
            sorted(
                {db: sum(1 for t in trades if t["delta_bucket"] == db) for db in {t["delta_bucket"] for t in trades}}.items()
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--timeframe", default="5minute")
    parser.add_argument("--lookback-sessions", type=int, default=60)
    parser.add_argument("--max-bars", type=int, default=6000)
    parser.add_argument("--window-bars", type=int, default=375)
    parser.add_argument("--report-path", default="/tmp/walkforward_directional_rl.json")
    parser.add_argument("--policy-state-path", default="/tmp/policy_state_walkforward.json")
    args = parser.parse_args()

    config = clone_default_config()
    store = DirectionalOptionsDataStore(config["data_root"])
    feature_engine = FeatureEngine(config["feature_engine"])
    regime_engine = RegimeClassifier()
    signal_engine = DirectionalSignalEngine(config["signal_engine"])
    selector = OptionSelectionEngine(store, config["selector"])
    risk_engine = DirectionalOptionsRiskEngine(config["risk"])

    spot = store.load_spot_frame(args.underlying)
    latest = store.latest_tradeable_timestamp(args.underlying)
    if latest is not None:
        spot = spot.loc[spot["time"] <= latest].reset_index(drop=True)
    frame = feature_engine.build_frame(
        spot, args.timeframe, lookback_sessions=args.lookback_sessions
    )
    if args.max_bars and len(frame) > args.max_bars:
        frame = frame.tail(args.max_bars).reset_index(drop=True)

    print(f"Walk-forward harness: {args.underlying} {args.timeframe} | bars={len(frame)} window={args.window_bars}")
    print(f"  first_bar={frame['time'].iloc[0]} last_bar={frame['time'].iloc[-1]}")

    # Fresh policy for the RL arm
    reset_policy_for_tests()
    rl_policy = get_policy(Path(args.policy_state_path))
    # Wipe any persisted state so this run is clean
    if Path(args.policy_state_path).exists():
        Path(args.policy_state_path).unlink()
    reset_policy_for_tests()
    rl_policy = get_policy(Path(args.policy_state_path))

    print("Running RL arm ...")
    rl_report = run_arm(
        arm="rl",
        underlying=args.underlying,
        timeframe=args.timeframe,
        frame=frame,
        config=config,
        store=store,
        feature_engine=feature_engine,
        regime_engine=regime_engine,
        signal_engine=signal_engine,
        selector=selector,
        risk_engine=risk_engine,
        policy=rl_policy,
        window_bars=args.window_bars,
    )

    print("Running baseline arm ...")
    baseline_report = run_arm(
        arm="baseline",
        underlying=args.underlying,
        timeframe=args.timeframe,
        frame=frame,
        config=config,
        store=store,
        feature_engine=feature_engine,
        regime_engine=regime_engine,
        signal_engine=signal_engine,
        selector=selector,
        risk_engine=risk_engine,
        policy=None,
        window_bars=args.window_bars,
    )

    out = {
        "underlying": args.underlying,
        "timeframe": args.timeframe,
        "bar_count": int(len(frame)),
        "first_bar": str(frame["time"].iloc[0]),
        "last_bar": str(frame["time"].iloc[-1]),
        "window_bars": args.window_bars,
        "rl": rl_report,
        "baseline": baseline_report,
        "policy_snapshot": rl_policy.snapshot(),
    }
    Path(args.report_path).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nReport written to {args.report_path}")

    def _summarize(report: dict[str, Any]) -> str:
        return (
            f"  trades={report['trade_count']:>4} | wr={report['win_rate']*100:5.1f}% | "
            f"sum_R={report['sum_r']:>7.2f} | mean_R={report['mean_r']:>5.2f} | "
            f"sum_PnL=₹{report['sum_pnl']:>10.0f} | maxDD={report['max_drawdown']*100:4.1f}%"
        )

    print("\n=== ARM SUMMARY ===")
    print(f"rl       :{_summarize(rl_report)}")
    print(f"baseline :{_summarize(baseline_report)}")
    print(f"\nPolicy posterior: n_seen={rl_policy.snapshot()['n_seen']}")


if __name__ == "__main__":
    main()
