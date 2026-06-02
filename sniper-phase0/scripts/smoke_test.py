"""Grid-directional smoke test on synthetic data.

This verifies the blueprint wiring without requiring broker files:
feature grid -> directional labels -> uniqueness weights -> directional verdict. If LightGBM is
installed, it also runs a tiny train/predict pass; otherwise it uses labels as plumbing
predictions and prints that model training was skipped.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    from nomad_sniper.evaluation.phase0 import run_directional_phase0_verdict
    from nomad_sniper.evaluation.splits import sample_uniqueness_weights
    from nomad_sniper.features.pipeline import build_features_for_grid
    from nomad_sniper.labels.directional import build_directional_labels_for_grid
    from nomad_sniper.utils.timeutil import IST, decision_grid

    print("[1/5] Synthesizing bars...")
    bars = _synthetic_bars()
    sessions = sorted(set(bars.index.date))
    grid = [("nifty", ts) for d in sessions[15:22] for ts in decision_grid(d, grid_minutes=15)]
    bars_by_underlying = {"nifty": bars}
    print(f"      {len(bars):,} bars, {len(grid)} grid points")

    print("[2/5] Building grid features...")
    features = build_features_for_grid(grid, bars_by_underlying, include_underlying=False)
    print(f"      features: {features.shape}")

    print("[3/5] Building directional labels...")
    labels = build_directional_labels_for_grid(
        grid,
        bars_by_underlying,
        horizon_minutes=60,
        barrier_m=0.2,
        m_breakeven=0.1,
    )
    labels["sample_weight"] = sample_uniqueness_weights(labels)
    print(f"      labels: {labels.shape}, classes={labels['direction'].value_counts().to_dict()}")

    common = features.index.intersection(labels.index)
    features = features.loc[common]
    labels = labels.loc[common]

    print("[4/5] Producing predictions...")
    predictions = _predict_or_echo(features, labels)
    print(f"      predictions: {predictions.shape}")

    print("[5/5] Computing directional verdict...")
    verdict = run_directional_phase0_verdict(
        labels,
        predictions,
        leakage_tests_passed=True,
        instrument_independence_tests_passed=True,
        artifact_path=Path("artifacts/smoke_directional_verdict.json"),
    )
    print()
    print("=" * 64)
    print(f"SMOKE TEST COMPLETE - verdict: {verdict.verdict.upper()}")
    print(f"  none recall:       {verdict.none_recall:.1%}")
    print(f"  up precision:      {verdict.up_precision:.1%}")
    print(f"  down precision:    {verdict.down_precision:.1%}")
    print(f"  acted EV @ 2x:     {verdict.acted_ev_atr_2x_slippage:.3f} ATR")
    print("  artifact:          artifacts/smoke_directional_verdict.json")
    print("=" * 64)
    return 0


def _synthetic_bars() -> pd.DataFrame:
    from nomad_sniper.utils.timeutil import IST

    rng = np.random.default_rng(7)
    rows = []
    price = 22000.0
    start = date(2025, 1, 1)
    for d_offset in range(35):
        d = start + timedelta(days=d_offset)
        if d.weekday() >= 5:
            continue
        session_start = IST.localize(datetime.combine(d, time(9, 15)))
        day_drift = 2.5 if d_offset % 5 in (1, 2) else -2.0 if d_offset % 5 == 3 else 0.0
        for m in range(375):
            ts = session_start + timedelta(minutes=m)
            o = price
            c = o + day_drift + rng.normal(0, 3)
            h = max(o, c) + abs(rng.normal(0, 1.5))
            lo = min(o, c) - abs(rng.normal(0, 1.5))
            rows.append({
                "ts": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": int(abs(rng.normal(10000, 2500))),
                "oi": 1_000_000 + d_offset * 1000 + m,
            })
            price = c
    df = pd.DataFrame(rows).set_index("ts")
    df.index = df.index.tz_convert(IST)
    return df


def _predict_or_echo(features: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    try:
        from nomad_sniper.models.directional import train_directional_model

        cats = [c for c in ("u_location_vs_prev_value", "u_open_location", "c_time_of_day_bucket") if c in features]
        model = train_directional_model(
            features,
            labels,
            categorical_features=cats,
            sample_weight=labels["sample_weight"],
            num_boost_round=20,
        )
        return model.predict_frame(features)
    except ModuleNotFoundError as exc:
        print(f"      LightGBM unavailable ({exc}); using label echo for plumbing verdict.")
        return pd.DataFrame({"pred_direction": labels["direction"]}, index=labels.index)


if __name__ == "__main__":
    raise SystemExit(main())
