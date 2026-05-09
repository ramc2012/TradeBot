from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from auction_intelligence.rl.automation import (
    RLAutoTrainer,
    build_promotion_decision,
    evaluate_policy_on_records,
    split_records_for_cycle,
)
from auction_intelligence.rl.policy import QLearningPolicy, encode_action
from auction_intelligence.rl.state import extract_state_from_bins


def _record(*, suffix: str, confidence: float = 0.7) -> dict:
    return {
        "signal_id": f"signal-{suffix}",
        "symbol": "NIFTY FUT",
        "action": "LONG",
        "regime_label": "trend_continuation",
        "confidence": confidence,
        "entry_price": 23120.0,
        "stop_price": 23070.0,
        "target_price": 23270.0,
        "observed_fill_price": 23121.0,
        "fill_drift_ticks": 1.0,
        "stale_signal": False,
        "reconciliation_status": "matched",
        "metadata": {
            "decision_metadata": {
                "min_confidence": 0.70,
                "risk_multiple": 3.0,
                "sleeve_fraction": 0.50,
                "buyer_fail_bin": 2,
                "seller_fail_bin": 1,
                "ib_size_bin": 0,
                "diagnostics": {
                    "close_price": 23120.0,
                    "vah": 23140.0,
                    "val": 23060.0,
                    "trade_imbalance": 0.62,
                    "book_pressure": 0.55,
                    "toxicity_score": 0.35,
                    "timing_confidence": 0.76,
                },
            }
        },
    }


class _FakeVersionStore:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.promoted: list[tuple[str, str | None]] = []

    async def create_version(self, **kwargs):
        payload = {"id": f"version-{len(self.created) + 1}", **kwargs}
        self.created.append(payload)
        return payload

    async def promote_version(self, version_id: str, *, promotion_reason: str | None = None):
        self.promoted.append((version_id, promotion_reason))
        for row in self.created:
            if row["id"] == version_id:
                return {**row, "status": "active", "promotion_reason": promotion_reason}
        return None

    async def has_run_for_session(self, *, session_date, sources):
        return False

    async def list_versions(self, *, limit=20, status=None):
        return self.created[:limit]

    async def latest_version(self, *, status=None):
        return self.created[-1] if self.created else None


def test_split_records_for_cycle_keeps_recent_holdout() -> None:
    records = [{"signal_id": f"s-{idx}"} for idx in range(10)]
    train, holdout = split_records_for_cycle(records, holdout_fraction=0.2, min_holdout_records=3)

    assert len(train) == 7
    assert len(holdout) == 3
    assert holdout[0]["signal_id"] == "s-7"


def test_evaluate_policy_on_records_counts_only_matched_actions() -> None:
    state = extract_state_from_bins(
        regime_label="trend_continuation",
        direction="LONG",
        buyer_fail_bin=2,
        seller_fail_bin=1,
        ib_size_bin=0,
        trade_imbalance=0.62,
        book_pressure=0.55,
        toxicity_score=0.35,
        timing_confidence=0.76,
    )
    policy = QLearningPolicy()
    policy._cache_loaded = True
    policy._q_cache[state.to_key()] = [0.0] * 27
    policy._visit_cache[state.to_key()] = [0] * 27
    action_idx = encode_action(min_confidence=0.70, risk_multiple=3.0, sleeve_fraction=0.50)
    policy._q_cache[state.to_key()][action_idx] = 1.2

    metrics = evaluate_policy_on_records(policy, [_record(suffix="one")], use_proxy_reward=True)

    assert metrics["evaluable_records"] == 1
    assert metrics["matched_actions"] == 1
    assert metrics["average_reward"] > 0


def test_build_promotion_decision_blocks_when_candidate_is_worse() -> None:
    decision = build_promotion_decision(
        training_summary={"trained_on": 100},
        baseline_metrics={
            "evaluable_records": 20,
            "matched_actions": 12,
            "average_reward": 0.45,
            "negative_reward_ratio": 0.25,
            "average_fill_drift_ticks": 1.0,
        },
        candidate_metrics={
            "evaluable_records": 20,
            "matched_actions": 9,
            "average_reward": 0.40,
            "negative_reward_ratio": 0.40,
            "average_fill_drift_ticks": 1.7,
        },
        config={
            "min_train_records": 80,
            "min_holdout_records": 20,
            "min_candidate_matches": 8,
            "min_avg_reward_edge": 0.03,
            "max_negative_ratio_worsening": 0.05,
            "max_fill_drift_worsening_ticks": 0.5,
        },
    )

    assert decision["should_promote"] is False
    assert any("reward_edge_below_min" in item for item in decision["blockers"])


@pytest.mark.asyncio
async def test_rl_auto_trainer_promotes_candidate_when_holdout_improves(monkeypatch) -> None:
    records = [_record(suffix=str(idx)) for idx in range(12)]
    store = _FakeVersionStore()
    policy = QLearningPolicy()
    policy._cache_loaded = True

    trainer = RLAutoTrainer(
        config={
            "mvp_scope": {"session": {"open": "09:15", "close": "15:30"}},
            "rl": {
                "auto_train_enabled": True,
                "timezone": "Asia/Kolkata",
                "run_after_close_minutes": 45,
                "max_trades": 50,
                "use_proxy_reward": True,
                "holdout_fraction": 0.25,
                "min_train_records": 4,
                "min_holdout_records": 2,
                "min_candidate_matches": 1,
                "min_avg_reward_edge": 0.01,
                "max_negative_ratio_worsening": 0.2,
                "max_fill_drift_worsening_ticks": 1.0,
            },
        },
        policy=policy,
        version_store=store,
    )

    async def _fake_fetch_training_records(*, max_trades: int, symbol: str | None = None):
        return list(reversed(records))

    monkeypatch.setattr(
        "auction_intelligence.rl.automation.fetch_training_records",
        _fake_fetch_training_records,
    )

    result = await trainer.run_cycle(source="manual", promote_if_eligible=True)

    assert result["status"] == "promoted"
    assert store.created
    assert store.promoted
    assert policy._q_cache
