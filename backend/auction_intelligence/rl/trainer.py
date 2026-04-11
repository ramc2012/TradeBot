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

from auction_intelligence.rl.policy import rl_policy, decode_action, DEFAULT_ACTION_IDX, N_ACTIONS
from auction_intelligence.rl.reward import compute_proxy_reward, compute_reward
from auction_intelligence.rl.state import MPState, _day_type_idx, _tail_bin, _ib_bin

logger = logging.getLogger(__name__)

# Regime label → integer for day_type lookup
_REGIME_LABELS = [
    "trend_day", "trend_continuation", "breakout_acceptance",
    "balance", "developing_balance",
    "rotational_day", "neutral_extreme",
    "failed_auction", "breakout_rejection", "reversal",
    "no_trade",
]


def _state_from_record(record: dict[str, Any]) -> MPState | None:
    """Reconstruct MPState from a shadow_observations record.

    We use stored metadata fields where available.
    Falls back to zero-bins for missing data (produces a valid but approximate state).
    """
    action_str = str(record.get("action") or "FLAT")
    if action_str == "FLAT":
        return None  # No state for FLAT decisions

    direction = 1 if action_str == "SHORT" else 0
    regime_label = str(record.get("regime_label") or "no_trade")

    # Extract tail / IB info from metadata if available
    meta = record.get("metadata") or {}
    decision_meta = meta.get("decision_metadata") or {}
    diagnostics = decision_meta.get("diagnostics") or {}

    # Infer buyer/seller fail from diagnostics if stored
    buyer_fail_bin = int(meta.get("buyer_fail_bin", 0))
    seller_fail_bin = int(meta.get("seller_fail_bin", 0))
    ib_size_bin = int(meta.get("ib_size_bin", 1))

    # Use close_price and computed tolerance as ib proxy
    close_price = float(diagnostics.get("close_price") or 0.0)
    vah = float(diagnostics.get("vah") or 0.0)
    val = float(diagnostics.get("val") or 0.0)
    ib_range = vah - val  # rough IB proxy from value area
    if ib_range > 0 and close_price > 0:
        ib_size_bin = _ib_bin(ib_range, close_price)

    return MPState(
        day_type_idx=_day_type_idx(regime_label, direction),
        buyer_fail_bin=buyer_fail_bin,
        seller_fail_bin=seller_fail_bin,
        ib_size_bin=ib_size_bin,
        direction=direction,
    )


def _action_from_record(record: dict[str, Any]) -> int:
    """Recover the action index that was effectively used for this trade.

    If RL metadata is stored, use it. Otherwise, match the closest pre-set params
    to the recorded min_confidence / sleeve_fraction in the agent config.
    Defaults to DEFAULT_ACTION_IDX if nothing can be inferred.
    """
    meta = record.get("metadata") or {}
    rl_action_idx = meta.get("rl_action_idx")
    if rl_action_idx is not None:
        idx = int(rl_action_idx)
        if 0 <= idx < N_ACTIONS:
            return idx

    # Infer from confidence — pick action tier
    confidence = float(record.get("confidence") or 0.62)
    if confidence >= 0.70:
        conf_idx = 2
    elif confidence >= 0.62:
        conf_idx = 1
    else:
        conf_idx = 0

    # Default risk and size
    return conf_idx * 9 + 1 * 3 + 1  # risk_idx=1 (2.0×), size_idx=1 (0.35)


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
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    where_clauses = ["action != 'FLAT'", "entry_price IS NOT NULL", "stop_price IS NOT NULL", "target_price IS NOT NULL"]
    params: dict[str, Any] = {"limit": max_trades}

    if symbol:
        where_clauses.append("symbol = :symbol")
        params["symbol"] = symbol

    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT signal_id, symbol, action, regime_label, confidence,
               entry_price, stop_price, target_price, metadata
        FROM shadow_observations
        WHERE {where_sql}
        ORDER BY recorded_at DESC
        LIMIT :limit
    """

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params)
        records = [
            {
                "signal_id": row.signal_id,
                "symbol": row.symbol,
                "action": row.action,
                "regime_label": row.regime_label,
                "confidence": row.confidence,
                "entry_price": row.entry_price,
                "stop_price": row.stop_price,
                "target_price": row.target_price,
                "metadata": row.metadata or {},
            }
            for row in result.fetchall()
        ]

    if not records:
        return {
            "trained_on": 0,
            "skipped": 0,
            "message": "No eligible shadow observations found for training.",
        }

    trained = 0
    skipped = 0

    for record in records:
        state = _state_from_record(record)
        if state is None:
            skipped += 1
            continue

        action_idx = _action_from_record(record)

        if use_proxy_reward:
            reward = compute_proxy_reward(
                action=record["action"],
                entry_price=record["entry_price"],
                stop_price=record["stop_price"],
                target_price=record["target_price"],
                confidence=float(record.get("confidence") or 0.62),
            )
        else:
            # Look for real outcome in metadata
            outcome = (record["metadata"] or {}).get("outcome")
            exit_price = (record["metadata"] or {}).get("exit_price")
            if not outcome:
                skipped += 1
                continue
            reward = compute_reward(
                action=record["action"],
                entry_price=record["entry_price"],
                stop_price=record["stop_price"],
                target_price=record["target_price"],
                outcome=outcome,
                exit_price=exit_price,
                confidence=float(record.get("confidence") or 0.62),
            )

        await rl_policy.update(state, action_idx, reward)
        trained += 1

    logger.info("[RL] Training complete: %d trained, %d skipped", trained, skipped)
    return {
        "trained_on": trained,
        "skipped": skipped,
        "states_in_cache": len(rl_policy._q_cache),
        "total_episodes": rl_policy._total_episodes,
        "message": f"Q-table updated from {trained} shadow observations.",
    }
