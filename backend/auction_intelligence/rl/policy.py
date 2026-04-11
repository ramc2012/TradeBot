"""Q-learning policy for Market Profile trade parameter selection.

Action space (27 total = 3 × 3 × 3):
  - confidence tier:  0=0.55, 1=0.62, 2=0.70
  - risk_multiple:    0=1.5×, 1=2.0×, 2=3.0×
  - sleeve_fraction:  0=0.20, 1=0.35, 2=0.50

Q-values are persisted in PostgreSQL (rl_agent_qtable) and cached in-memory.
The cache is refreshed on first use and after every training update.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Optional

from auction_intelligence.rl.state import MPState

logger = logging.getLogger(__name__)


# ── Action space ─────────────────────────────────────────────────────────────

N_ACTIONS = 27

_CONFIDENCE_LEVELS = [0.55, 0.62, 0.70]
_RISK_MULTIPLES = [1.5, 2.0, 3.0]
_SLEEVE_FRACTIONS = [0.20, 0.35, 0.50]


@dataclass(frozen=True)
class TradeParams:
    """RL-selected trade parameters to override SwingAgent defaults."""

    min_confidence: float   # entry confidence threshold
    risk_multiple: float    # target = entry ± risk_multiple × per_unit_risk
    sleeve_fraction: float  # fraction of portfolio to risk per trade
    action_idx: int         # encoded action (0-26) for Q-table update


def decode_action(idx: int) -> TradeParams:
    """Decode action index 0-26 into TradeParams."""
    confidence_idx = (idx // 9) % 3
    risk_idx = (idx // 3) % 3
    size_idx = idx % 3
    return TradeParams(
        min_confidence=_CONFIDENCE_LEVELS[confidence_idx],
        risk_multiple=_RISK_MULTIPLES[risk_idx],
        sleeve_fraction=_SLEEVE_FRACTIONS[size_idx],
        action_idx=idx,
    )


# Precompute all 27 params for fast lookup
_ALL_PARAMS: list[TradeParams] = [decode_action(i) for i in range(N_ACTIONS)]


# Default action: normal confidence (0.62), 2× R:R, 35% sleeve
DEFAULT_ACTION_IDX = 13  # (1*9 + 1*3 + 1) = 13


# ── Q-table cache ─────────────────────────────────────────────────────────────

class QLearningPolicy:
    """Tabular Q-learning policy backed by PostgreSQL.

    Uses ε-greedy exploration. ε decays from 0.30 → 0.05 over episodes.
    Q-values are stored in rl_agent_qtable and cached in-memory.
    """

    LEARNING_RATE = 0.10
    EPSILON_START = 0.30
    EPSILON_MIN = 0.05
    EPSILON_DECAY = 0.98

    def __init__(self) -> None:
        # state_key → list of 27 q-values
        self._q_cache: dict[str, list[float]] = {}
        self._cache_loaded = False
        self._total_episodes = 0

    # ── Cache management ──────────────────────────────────────────────────────

    async def load_cache(self) -> None:
        """Load full Q-table from PostgreSQL into memory."""
        try:
            from db.database import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT state_hash, action_idx, q_value FROM rl_agent_qtable")
                )
                rows = result.fetchall()

            # Reset and rebuild cache
            self._q_cache = {}
            for row in rows:
                state_key = row.state_hash
                if state_key not in self._q_cache:
                    self._q_cache[state_key] = [0.0] * N_ACTIONS
                self._q_cache[state_key][row.action_idx] = float(row.q_value)

            # Also load total episodes
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT COALESCE(SUM(visit_count), 0) FROM rl_agent_qtable")
                )
                row = result.first()
                self._total_episodes = int(row[0]) if row else 0

            self._cache_loaded = True
            logger.info(
                "[RL] Q-table cache loaded: %d states, %d total visits",
                len(self._q_cache),
                self._total_episodes,
            )
        except Exception as exc:
            logger.warning("[RL] Could not load Q-table cache: %s", exc)
            self._cache_loaded = True  # Don't retry on every call

    def _ensure_state(self, state_key: str) -> list[float]:
        """Return Q-values for state, initializing to zeros if unknown."""
        if state_key not in self._q_cache:
            self._q_cache[state_key] = [0.0] * N_ACTIONS
        return self._q_cache[state_key]

    # ── Action selection ──────────────────────────────────────────────────────

    def _epsilon(self) -> float:
        return max(
            self.EPSILON_MIN,
            self.EPSILON_START * (self.EPSILON_DECAY ** self._total_episodes),
        )

    def select_action_sync(
        self,
        state: MPState,
        *,
        force_exploit: bool = False,
    ) -> TradeParams:
        """Select an action synchronously using cached Q-values (ε-greedy).

        This is safe to call from synchronous SwingAgent.evaluate().
        Ensure load_cache() has been awaited at startup.
        """
        if not self._cache_loaded:
            # Return default params if cache not yet loaded (cold start)
            return _ALL_PARAMS[DEFAULT_ACTION_IDX]

        eps = self._epsilon()
        if not force_exploit and random.random() < eps:
            idx = random.randint(0, N_ACTIONS - 1)
        else:
            q_values = self._ensure_state(state.to_key())
            # Argmax with random tie-breaking
            max_q = max(q_values)
            best = [i for i, v in enumerate(q_values) if v == max_q]
            idx = random.choice(best)

        return _ALL_PARAMS[idx]

    # ── Q-table update ────────────────────────────────────────────────────────

    async def update(
        self,
        state: MPState,
        action_idx: int,
        reward: float,
    ) -> None:
        """Update Q-value for (state, action) with reward signal.

        Uses one-step Q-learning (no next-state bootstrapping — terminal reward).
        Q(s,a) ← Q(s,a) + α * (r − Q(s,a))
        """
        state_key = state.to_key()

        # Update in-memory cache
        q_values = self._ensure_state(state_key)
        q_old = q_values[action_idx]
        q_new = q_old + self.LEARNING_RATE * (reward - q_old)
        self._q_cache[state_key][action_idx] = q_new
        self._total_episodes += 1

        # Persist to DB
        try:
            from db.database import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO rl_agent_qtable (state_hash, action_idx, q_value, visit_count, last_updated)
                        VALUES (:state_hash, :action_idx, :q_value, 1, now())
                        ON CONFLICT (state_hash, action_idx) DO UPDATE
                          SET q_value = :q_value,
                              visit_count = rl_agent_qtable.visit_count + 1,
                              last_updated = now()
                        """
                    ),
                    {
                        "state_hash": state_key,
                        "action_idx": action_idx,
                        "q_value": q_new,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning("[RL] Q-table update failed for state=%s: %s", state_key, exc)

    # ── Introspection ─────────────────────────────────────────────────────────

    def get_policy_summary(self) -> dict:
        """Return a human-readable summary of the learned policy."""
        summary: list[dict] = []
        for state_key, q_values in sorted(self._q_cache.items()):
            max_q = max(q_values)
            best_idx = q_values.index(max_q)
            best = _ALL_PARAMS[best_idx]
            summary.append(
                {
                    "state": state_key,
                    "best_action": best_idx,
                    "min_confidence": best.min_confidence,
                    "risk_multiple": best.risk_multiple,
                    "sleeve_fraction": best.sleeve_fraction,
                    "q_value": round(max_q, 4),
                    "all_q": [round(v, 4) for v in q_values],
                }
            )
        return {
            "states_learned": len(self._q_cache),
            "total_episodes": self._total_episodes,
            "epsilon": round(self._epsilon(), 4),
            "learning_rate": self.LEARNING_RATE,
            "cache_loaded": self._cache_loaded,
            "policy": summary,
        }

    async def reset(self) -> None:
        """Wipe Q-table from DB and in-memory cache."""
        try:
            from db.database import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                await session.execute(text("DELETE FROM rl_agent_qtable"))
                await session.commit()
        except Exception as exc:
            logger.warning("[RL] Q-table reset failed: %s", exc)
        self._q_cache = {}
        self._total_episodes = 0
        logger.info("[RL] Q-table reset complete.")


# Module-level singleton shared across service and API
rl_policy = QLearningPolicy()
