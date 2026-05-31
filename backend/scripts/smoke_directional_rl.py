"""Plumbing smoke test for the directional-options RL engine.

Goal: prove the data pipeline works end-to-end with REAL persisted data,
without running a full backtest. We walk a handful of bars through every
layer and assert the shape / presence of each field. If this passes, the
module is correctly plumbed and will fire signals as soon as live data
arrives.

Pipeline checked:

    spot_frame → feature_engine → regime → signal
                  ↓
                selector → top-K candidates
                  ↓
                policy.rank_candidates → chosen idx
                  ↓
                policy.decide → {act, size_multiplier, sampled_value}
                  ↓
                risk.approve(size_multiplier) → {approved, qty, risk_budget}
                  ↓
                policy.register_open → posterior pending entry
                  ↓
                policy.record_close → posterior update + persist

Side-effects validated:
  * /tmp/policy_state_smoke.json materializes with the expected schema
  * value_model.n_seen increments by exactly the trades we closed
  * size_buckets reflect our chosen multipliers

Run inside the backend container:

    docker exec nomadcurie_backend bash -lc \
      "cd /app && PYTHONPATH=/app python /app/scripts/smoke_directional_rl.py"
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine
from directional_options.policy import DirectionalPolicy, reset_policy_for_tests, EXPECTED_FEATURE_DIM
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine


CHECKS = 0
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    status = "✓" if cond else "✗"
    msg = f"  [{status}] {label}"
    if detail:
        msg += f"   ({detail})"
    print(msg)
    if not cond:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    config = clone_default_config()
    store = DirectionalOptionsDataStore(config["data_root"])
    feature_engine = FeatureEngine(config["feature_engine"])
    regime_engine = RegimeClassifier()
    signal_engine = DirectionalSignalEngine(config["signal_engine"])
    selector = OptionSelectionEngine(store, config["selector"])
    risk_engine = DirectionalOptionsRiskEngine(config["risk"])

    section("CONFIG")
    check("universe is indices-only", config["universe"] == ["NIFTY", "BANKNIFTY", "SENSEX"],
          detail=str(config["universe"]))
    check("premium_cap_pct is unbounded (None)", config["risk"]["premium_cap_pct"] is None)
    check("rl_policy.enabled defaults True", config["rl_policy"]["enabled"] is True)
    check("one_position_per_symbol defaults True",
          config["paper_trading"]["one_position_per_symbol"] is True)
    check("no min_confidence in signal_engine",
          "min_confidence" not in config["signal_engine"],
          detail=f"keys={sorted(config['signal_engine'].keys())}")

    section("POLICY INITIALIZATION")
    state_path = Path("/tmp/policy_state_smoke.json")
    if state_path.exists():
        state_path.unlink()
    reset_policy_for_tests()
    policy = DirectionalPolicy(state_path)
    check("policy initialised with EXPECTED_FEATURE_DIM",
          policy._value_model.dim == EXPECTED_FEATURE_DIM,
          detail=f"dim={EXPECTED_FEATURE_DIM}")
    check("policy.n_seen starts at 0", policy._value_model.n_seen == 0)
    check("policy has 4 size buckets", len(policy._size_buckets) == 4)

    section("DATA STORE — NIFTY spot frame")
    spot = store.load_spot_frame("NIFTY")
    check("NIFTY spot frame loaded", spot is not None and not spot.empty,
          detail=f"rows={len(spot) if spot is not None else 0}")
    if spot is None or spot.empty:
        print("  SKIPPING further checks — no NIFTY data available")
        return 1
    check("spot frame has time + close columns",
          {"time", "close"}.issubset(set(spot.columns)),
          detail=f"cols={list(spot.columns)[:8]}")

    # Tail slice — ~200 bars is plenty for plumbing
    latest_tradeable = store.latest_tradeable_timestamp("NIFTY")
    if latest_tradeable is not None:
        spot = spot.loc[spot["time"] <= latest_tradeable].reset_index(drop=True)
    check("latest_tradeable_timestamp resolves",
          latest_tradeable is not None, detail=str(latest_tradeable))

    section("FEATURE ENGINE")
    frame = feature_engine.build_frame(spot, "5minute", lookback_sessions=8)
    check("feature_frame non-empty", not frame.empty, detail=f"rows={len(frame)}")
    expected_cols = {"time", "close", "ema_spread_pct", "atr", "adx", "plus_di", "minus_di",
                     "breakout_up", "breakout_down", "rv_annualized", "rv_percentile",
                     "momentum_3", "momentum_8", "range_expansion"}
    missing = expected_cols - set(frame.columns)
    check("feature frame has all expected columns", not missing,
          detail=f"missing={sorted(missing)}" if missing else f"cols={len(frame.columns)}")

    # Walk through the last N bars; first signal-producing bar gets the
    # full pipeline pass with assertions, then we close it to feed reward.
    sample_n = min(50, len(frame))
    frame_tail = frame.tail(sample_n).reset_index(drop=True)
    print(f"  walking last {sample_n} bars (asof {frame_tail['time'].iloc[0]} → {frame_tail['time'].iloc[-1]})")

    section("PIPELINE WALK — find first bar with a tradeable signal")
    selected = None
    for idx, row in frame_tail.iterrows():
        ts = pd.Timestamp(row["time"])
        regime = regime_engine.classify(row, timeframe="5minute")
        signal = signal_engine.predict(row, regime, "5minute")
        if signal is None:
            continue
        selection = selector.select(
            underlying="NIFTY",
            timestamp=ts,
            spot_price=float(row["close"]),
            row=row,
            signal=signal,
            regime=regime,
            timeframe="5minute",
        )
        if not selection.get("candidates"):
            continue
        selected = (ts, row, regime, signal, selection)
        break

    check("found at least one bar producing a tradeable signal", selected is not None)
    if selected is None:
        print("  SKIPPING — no signal in tail slice; tail probably falls in low-momentum tape")
        return _finish()

    ts, row, regime, signal, selection = selected
    candidates = selection["candidates"]
    print(f"  bar={ts}  regime={regime.label}  direction={signal.direction}  "
          f"conf={signal.confidence:.3f}  candidates={len(candidates)}")

    section("REGIME + SIGNAL shapes")
    check("regime.label is a known label",
          regime.label in {"trend", "breakout", "micro_trend", "exploration", "chop", "risk_off"},
          detail=regime.label)
    check("signal.direction ∈ {CE, PE}", signal.direction in ("CE", "PE"))
    check("0 < signal.confidence ≤ 0.85",
          0.0 < signal.confidence <= 0.85,
          detail=f"{signal.confidence:.4f}")
    check("signal.expected_horizon_bars > 0",
          signal.expected_horizon_bars > 0,
          detail=str(signal.expected_horizon_bars))
    check("signal has policy-feature fields",
          all(hasattr(signal, k) for k in
              ("jump_score", "timing_precision", "tail_probability", "model_uncertainty", "p_up")))

    section("SELECTOR — top-K candidates")
    check("selector returned ≥1 candidate", len(candidates) >= 1, detail=f"K={len(candidates)}")
    best = selection["best"]
    check("selection.best matches first candidate (sorted by score)",
          best is candidates[0])
    for i, c in enumerate(candidates[:3]):
        check(f"candidate[{i}] has delta_bucket and contract_score",
              hasattr(c, "delta_bucket") and hasattr(c, "contract_score"),
              detail=f"{c.delta_bucket} score={c.contract_score:.2f} strike={c.strike}")

    section("POLICY — rank + decide")
    signal_dict = asdict(signal)
    regime_dict = asdict(regime)
    candidate_dicts = [asdict(c) for c in candidates]
    best_idx, samples = policy.rank_candidates(
        signal=signal_dict, candidates=candidate_dicts, regime=regime_dict
    )
    check("policy.rank_candidates returned an index", best_idx is not None,
          detail=f"idx={best_idx}")
    check("policy returned a sample per candidate",
          len(samples) == len(candidates),
          detail=f"samples={len(samples)}")
    decision = policy.decide(
        signal=signal_dict, candidate=candidate_dicts[best_idx], regime=regime_dict
    )
    check("decision.act is bool", isinstance(decision.act, bool))
    check("decision.size_multiplier ∈ {0.5, 1.0, 1.5, 2.0}",
          decision.size_multiplier in (0.5, 1.0, 1.5, 2.0),
          detail=str(decision.size_multiplier))
    check("decision has sampled_value + posterior_mean + posterior_var",
          all(isinstance(getattr(decision, k), float)
              for k in ("sampled_value", "posterior_mean", "posterior_var")))
    print(f"  policy decision: act={decision.act}  size={decision.size_multiplier:.1f}×  "
          f"sampled={decision.sampled_value:+.3f}  mean={decision.posterior_mean:+.3f}")

    section("RISK ENGINE — sizing path")
    chosen = candidates[best_idx]
    risk_decision = risk_engine.approve(
        candidate=chosen,
        signal=signal,
        equity=float(config["risk"]["starting_equity"]),
        size_multiplier=decision.size_multiplier,
    )
    check("RiskDecision.approved is bool", isinstance(risk_decision.approved, bool))
    check("RiskDecision.risk_budget > 0", risk_decision.risk_budget > 0,
          detail=f"₹{risk_decision.risk_budget:.0f}")
    check("RiskDecision.premium_cap is None (no cap)", risk_decision.premium_cap is None)
    if risk_decision.approved:
        check("quantity_lots ≥ 1 when approved", risk_decision.quantity_lots >= 1,
              detail=f"lots={risk_decision.quantity_lots}")
    else:
        check("rejection has a reason", len(risk_decision.reasons) > 0,
              detail=f"reasons={risk_decision.reasons[:1]}")
    print(f"  risk: approved={risk_decision.approved}  lots={risk_decision.quantity_lots}  "
          f"premium_at_risk=₹{risk_decision.premium_at_risk:.0f}  "
          f"max_loss=₹{risk_decision.max_loss:.0f}")

    section("POLICY register_open → record_close round-trip")
    position_id = "smoke-001"
    n_seen_before = policy._value_model.n_seen
    policy.register_open(
        position_id=position_id,
        signal=signal_dict,
        candidate=candidate_dicts[best_idx],
        regime=regime_dict,
        size_multiplier=decision.size_multiplier,
        risk_budget=risk_decision.risk_budget,
    )
    check("pending entry registered",
          position_id in policy._pending,
          detail=f"pending={list(policy._pending.keys())}")
    # Simulate a winning trade: realized PnL of +₹2000
    r_multiple = policy.record_close(position_id=position_id, realized_pnl=2000.0)
    check("record_close returned an R-multiple", r_multiple is not None,
          detail=f"R={r_multiple:.4f}" if r_multiple is not None else "None")
    check("pending entry cleared after close",
          position_id not in policy._pending)
    check("value_model.n_seen incremented by 1",
          policy._value_model.n_seen == n_seen_before + 1,
          detail=f"{n_seen_before} → {policy._value_model.n_seen}")
    chosen_bucket = policy._size_buckets[decision.size_multiplier]
    check(f"size bucket {decision.size_multiplier}× recorded the trade",
          chosen_bucket.n == 1, detail=f"n={chosen_bucket.n} mean_R={chosen_bucket.sum_r:.4f}")

    section("PERSISTENCE — policy_state.json on disk")
    check("policy_state.json materialised", state_path.exists(),
          detail=str(state_path))
    if state_path.exists():
        on_disk = json.loads(state_path.read_text())
        check("on-disk schema has value_model", "value_model" in on_disk)
        check("on-disk value_model.n_seen matches in-memory",
              on_disk["value_model"]["n_seen"] == policy._value_model.n_seen,
              detail=f"{on_disk['value_model']['n_seen']}")
        check("on-disk has size_buckets dict",
              isinstance(on_disk.get("size_buckets"), dict))
        check("size_buckets persisted for the chosen multiplier",
              any(
                  abs(float(k) - decision.size_multiplier) < 1e-6
                  and v.get("n") == 1
                  for k, v in on_disk["size_buckets"].items()
              ))

    section("RESTART SIMULATION — load policy from disk into fresh instance")
    reloaded = DirectionalPolicy(state_path)
    check("reloaded.n_seen matches", reloaded._value_model.n_seen == policy._value_model.n_seen)
    check("reloaded size bucket matches",
          reloaded._size_buckets[decision.size_multiplier].n == 1,
          detail=f"n={reloaded._size_buckets[decision.size_multiplier].n}")
    # Ranking should be deterministic up to RNG; just check it produces an idx
    new_idx, _ = reloaded.rank_candidates(
        signal=signal_dict, candidates=candidate_dicts, regime=regime_dict
    )
    check("reloaded policy can rank candidates", new_idx is not None)

    return _finish()


def _finish() -> int:
    print()
    print("=" * 64)
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} of {CHECKS} checks failed:")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    print(f"PASS — all {CHECKS} checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
