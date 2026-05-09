"""RL trainer: learns from shadow observations stored in PostgreSQL.

Each shadow_observations row represents one paper trade signal. We reconstruct
the MP state from stored metadata, decode the action that was effectively used,
compute a proxy reward (or real reward if outcome is recorded), then update the
Q-table via the policy singleton.

Usage:
    from auction_intelligence.rl.trainer import train_from_journal
    result = await train_from_journal(max_trades=500)
"""
from __future__ import annotations

import logging
from typing import Any

from auction_intelligence.rl.policy import QLearningPolicy, rl_policy, encode_action, N_ACTIONS
from auction_intelligence.rl.reward import compute_proxy_reward, compute_reward
from auction_intelligence.rl.state import MPState, extract_state_from_bins, _ib_bin

logger = logging.getLogger(__name__)

# Regime label → integer for day_type lookup
def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decision_metadata(record: dict[str, Any]) -> dict[str, Any]:
    meta = record.get("metadata") or {}
    return meta.get("decision_metadata") or {}


def _diagnostics(record: dict[str, Any]) -> dict[str, Any]:
    return _decision_metadata(record).get("diagnostics") or {}


def _state_from_record(record: dict[str, Any]) -> MPState | None:
    """Reconstruct MPState from a shadow_observations record.

    We use stored metadata fields where available.
    Falls back to zero-bins for missing data (produces a valid but approximate state).
    """
    action_str = str(record.get("action") or "FLAT")
    if action_str == "FLAT":
        return None  # No state for FLAT decisions

    regime_label = str(record.get("regime_label") or "no_trade")
    decision_meta = _decision_metadata(record)
    meta = record.get("metadata") or {}

    state_key = str(decision_meta.get("rl_state_key") or meta.get("rl_state_key") or "")
    parsed_state = MPState.from_key(state_key)
    if parsed_state is not None:
        return parsed_state

    diagnostics = _diagnostics(record)

    buyer_fail_bin = _safe_int(
        decision_meta.get("buyer_fail_bin", meta.get("buyer_fail_bin")),
        0,
    )
    seller_fail_bin = _safe_int(
        decision_meta.get("seller_fail_bin", meta.get("seller_fail_bin")),
        0,
    )
    ib_size_bin = _safe_int(
        decision_meta.get("ib_size_bin", meta.get("ib_size_bin")),
        1,
    )

    close_price = _safe_float(diagnostics.get("close_price"), 0.0)
    vah = _safe_float(diagnostics.get("vah"), 0.0)
    val = _safe_float(diagnostics.get("val"), 0.0)
    ib_range = vah - val  # rough IB proxy from value area
    if ib_range > 0 and close_price > 0:
        ib_size_bin = _ib_bin(ib_range, close_price)

    return extract_state_from_bins(
        regime_label=regime_label,
        direction=action_str,
        buyer_fail_bin=buyer_fail_bin,
        seller_fail_bin=seller_fail_bin,
        ib_size_bin=ib_size_bin,
        trade_imbalance=_safe_float(diagnostics.get("trade_imbalance"), 0.0),
        book_pressure=_safe_float(diagnostics.get("book_pressure"), 0.0),
        toxicity_score=_safe_float(diagnostics.get("toxicity_score"), 0.5),
        timing_confidence=_safe_float(diagnostics.get("timing_confidence"), 0.5),
    )


def _action_from_record(record: dict[str, Any]) -> int:
    """Recover the action index that was effectively used for this trade.

    If RL metadata is stored, use it. Otherwise, match the closest pre-set params
    to the recorded min_confidence / sleeve_fraction in the agent config.
    Defaults to DEFAULT_ACTION_IDX if nothing can be inferred.
    """
    meta = record.get("metadata") or {}
    decision_meta = _decision_metadata(record)
    rl_action_idx = decision_meta.get("rl_action_idx", meta.get("rl_action_idx"))
    if rl_action_idx is not None:
        try:
            idx = int(rl_action_idx)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < N_ACTIONS:
            return idx

    return encode_action(
        min_confidence=_safe_float(
            decision_meta.get("min_confidence"),
            _safe_float(record.get("confidence"), 0.62),
        ),
        risk_multiple=_safe_float(decision_meta.get("risk_multiple"), 2.0),
        sleeve_fraction=_safe_float(decision_meta.get("sleeve_fraction"), 0.35),
    )


def _reward_kwargs(record: dict[str, Any]) -> dict[str, Any]:
    diagnostics = _diagnostics(record)
    return {
        "fill_drift_ticks": record.get("fill_drift_ticks"),
        "stale_signal": bool(record.get("stale_signal", False)),
        "reconciliation_status": record.get("reconciliation_status"),
        "toxicity_score": diagnostics.get("toxicity_score"),
        "adverse_selection_risk": diagnostics.get("adverse_selection_risk"),
        "timing_confidence": diagnostics.get("timing_confidence"),
    }


def _reward_from_record(record: dict[str, Any], *, use_proxy_reward: bool) -> float | None:
    if use_proxy_reward:
        return compute_proxy_reward(
            action=record["action"],
            entry_price=record["entry_price"],
            stop_price=record["stop_price"],
            target_price=record["target_price"],
            confidence=float(record.get("confidence") or 0.62),
            **_reward_kwargs(record),
        )

    metadata = record.get("metadata") or {}
    decision_meta = _decision_metadata(record)
    outcome = metadata.get("outcome") or decision_meta.get("outcome")
    exit_price = (
        metadata.get("exit_price")
        or decision_meta.get("exit_price")
        or record.get("observed_fill_price")
    )
    if not outcome:
        return None
    return compute_reward(
        action=record["action"],
        entry_price=record["entry_price"],
        stop_price=record["stop_price"],
        target_price=record["target_price"],
        outcome=outcome,
        exit_price=exit_price,
        confidence=float(record.get("confidence") or 0.62),
        **_reward_kwargs(record),
    )


async def fetch_training_records(
    *,
    max_trades: int = 500,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    where_clauses = [
        "action != 'FLAT'",
        "entry_price IS NOT NULL",
        "stop_price IS NOT NULL",
        "target_price IS NOT NULL",
    ]
    params: dict[str, Any] = {"limit": max_trades}

    if symbol:
        where_clauses.append("symbol = :symbol")
        params["symbol"] = symbol

    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT signal_id, symbol, action, regime_label, confidence,
               entry_price, stop_price, target_price,
               observed_fill_price, fill_drift_ticks, stale_signal,
               reconciliation_status, metadata
        FROM shadow_observations
        WHERE {where_sql}
        ORDER BY recorded_at DESC
        LIMIT :limit
    """

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params)
        return [
            {
                "signal_id": row.signal_id,
                "symbol": row.symbol,
                "action": row.action,
                "regime_label": row.regime_label,
                "confidence": row.confidence,
                "entry_price": row.entry_price,
                "stop_price": row.stop_price,
                "target_price": row.target_price,
                "observed_fill_price": row.observed_fill_price,
                "fill_drift_ticks": row.fill_drift_ticks,
                "stale_signal": row.stale_signal,
                "reconciliation_status": row.reconciliation_status,
                "metadata": row.metadata or {},
            }
            for row in result.fetchall()
        ]


async def train_policy_from_records(
    policy: QLearningPolicy,
    records: list[dict[str, Any]],
    *,
    use_proxy_reward: bool = True,
    persist: bool = False,
) -> dict[str, Any]:
    trained = 0
    skipped = 0
    reward_total = 0.0

    for record in records:
        state = _state_from_record(record)
        if state is None:
            skipped += 1
            continue

        reward = _reward_from_record(record, use_proxy_reward=use_proxy_reward)
        if reward is None:
            skipped += 1
            continue

        action_idx = _action_from_record(record)
        await policy.update(state, action_idx, reward, persist=persist)
        trained += 1
        reward_total += float(reward)

    return {
        "trained_on": trained,
        "skipped": skipped,
        "average_reward": round(reward_total / trained, 4) if trained else 0.0,
        "states_in_cache": len(policy._q_cache),
        "total_episodes": policy._total_episodes,
    }


async def train_from_journal(
    *,
    max_trades: int = 500,
    use_proxy_reward: bool = True,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Train Q-table from shadow_observations records.

    For each non-FLAT decision with valid entry/stop/target prices:
    1. Reconstruct MP state from stored metadata
    2. Determine action index
    3. Compute reward (proxy or real)
    4. Update Q-table via rl_policy.update()

    Args:
        max_trades:       Max number of records to process (newest first)
        use_proxy_reward: If True, use expected-value proxy reward (no outcome needed).
                          If False, only use records with an actual outcome field.
        symbol:           Filter by symbol (e.g. "BANKNIFTY")

    Returns:
        Summary dict with stats.
    """
    records = await fetch_training_records(max_trades=max_trades, symbol=symbol)

    if not records:
        return {
            "trained_on": 0,
            "skipped": 0,
            "message": "No eligible shadow observations found for training.",
        }

    summary = await train_policy_from_records(
        rl_policy,
        records,
        use_proxy_reward=use_proxy_reward,
        persist=True,
    )
    logger.info(
        "[RL] Training complete: %d trained, %d skipped",
        summary["trained_on"],
        summary["skipped"],
    )
    summary["message"] = f"Q-table updated from {summary['trained_on']} shadow observations."
    return summary
