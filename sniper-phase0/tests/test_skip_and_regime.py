from __future__ import annotations

import pandas as pd

from sniper_phase0.evaluation.regime import skip_accuracy_by_regime, tag_regime
from sniper_phase0.evaluation.skip_accuracy import (
    skip_accuracy_by_ev,
    skip_accuracy_by_pwin,
)


def test_skip_by_ev_uses_expected_R_not_pwin() -> None:
    # Construct a case where p_win and expected_net_R disagree.
    # Trade 0: low p_win, but high E[net_R] (rare-but-large payoff). Actual: WIN.
    # Trade 1: high p_win, low E[net_R] (small expected payoff). Actual: LOSS.
    # If we rank by p_win, we'd skip trade 0 (wrong — it was a winner).
    # If we rank by expected_net_R, we'd skip trade 1 (correct — it was a loser).
    preds = pd.DataFrame(
        {
            "trade_id": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            "p_win":     [0.10, 0.95, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
            "expected_net_R": [2.0, -0.5, 0, 0, 0, 0, 0, 0, 0, 0],
            "net_R": [1.5, -1.0, 0.1, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1, -0.1],
        }
    )
    # Bottom-decile (1 row out of 10).
    assert skip_accuracy_by_ev(preds) == 1.0   # skips trade 1 → loser → correct
    assert skip_accuracy_by_pwin(preds) == 0.0  # skips trade 0 → winner → wrong


def test_tag_regime_priority_expiry_over_gap() -> None:
    feats = pd.DataFrame(
        {
            "trade_id": [0, 1, 2, 3],
            "ctx_is_expiry_day": [1, 0, 0, 0],
            "ctx_is_expiry_week": [1, 1, 0, 0],
            "ctx_overnight_gap_pct": [1.0, 0.0, 1.0, 0.0],
            "ctx_minutes_into_session": [30, 30, 30, 200],
        }
    )
    regime = tag_regime(feats).tolist()
    assert regime[0] == "expiry_day"     # expiry beats gap & opening
    assert regime[1] == "expiry_week"    # week, not day
    assert regime[2] == "gap_day"        # gap beats opening
    assert regime[3] == "normal"


def test_skip_accuracy_by_regime_smoke() -> None:
    preds = pd.DataFrame(
        {
            "trade_id": list(range(20)),
            "p_win": [0.5] * 20,
            "expected_net_R": [(-1.0 if i < 5 else 0.5) for i in range(20)],
            "net_R": [(-1.0 if i < 5 else 0.5) for i in range(20)],
        }
    )
    feats = pd.DataFrame(
        {
            "trade_id": list(range(20)),
            "ctx_is_expiry_day": [1] * 10 + [0] * 10,
            "ctx_is_expiry_week": [1] * 10 + [0] * 10,
            "ctx_overnight_gap_pct": [0.0] * 20,
            "ctx_minutes_into_session": [120] * 20,
        }
    )
    out = skip_accuracy_by_regime(preds, feats)
    assert {"regime", "n", "skip_accuracy_by_ev"} <= set(out.columns)
    assert set(out["regime"]) == {"expiry_day", "normal"}
