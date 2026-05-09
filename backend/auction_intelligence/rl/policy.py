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
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from auction_intelligence.rl.state import MPState, describe_state_key

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


def encode_action(
    *,
    min_confidence: float,
    risk_multiple: float,
    sleeve_fraction: float,
) -> int:
    """Map parameter values to the nearest discrete action index."""

    def _nearest(value: float, choices: list[float]) -> int:
        return min(range(len(choices)), key=lambda idx: abs(choices[idx] - value))

    confidence_idx = _nearest(min_confidence, _CONFIDENCE_LEVELS)
    risk_idx = _nearest(risk_multiple, _RISK_MULTIPLES)
    size_idx = _nearest(sleeve_fraction, _SLEEVE_FRACTIONS)
    return (confidence_idx * 9) + (risk_idx * 3) + size_idx


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
        self._visit_cache: dict[str, list[int]] = {}
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
                    text("SELECT state_hash, action_idx, q_value, visit_count FROM rl_agent_qtable")
                )
                rows = result.fetchall()

            # Reset and rebuild cache
            self._q_cache = {}
            self._visit_cache = {}
            for row in rows:
                state_key = row.state_hash
                if state_key not in self._q_cache:
                    self._q_cache[state_key] = [0.0] * N_ACTIONS
                    self._visit_cache[state_key] = [0] * N_ACTIONS
                self._q_cache[state_key][row.action_idx] = float(row.q_value)
                self._visit_cache[state_key][row.action_idx] = int(row.visit_count or 0)

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
        if state_key not in self._visit_cache:
            self._visit_cache[state_key] = [0] * N_ACTIONS
        return self._q_cache[state_key]

    def _ensure_visits(self, state_key: str) -> list[int]:
        """Return visit counts for state, initializing to zeros if unknown."""
        self._ensure_state(state_key)
        return self._visit_cache[state_key]

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
        *,
        persist: bool = True,
    ) -> None:
        """Update Q-value for (state, action) with reward signal.

        Uses one-step Q-learning (no next-state bootstrapping — terminal reward).
        Q(s,a) ← Q(s,a) + α * (r − Q(s,a))
        """
        state_key = state.to_key()

        # Update in-memory cache
        q_values = self._ensure_state(state_key)
        visits = self._ensure_visits(state_key)
        q_old = q_values[action_idx]
        q_new = q_old + self.LEARNING_RATE * (reward - q_old)
        self._q_cache[state_key][action_idx] = q_new
        self._visit_cache[state_key][action_idx] += 1
        self._total_episodes += 1

        # Persist to DB
        if not persist:
            return
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
                    "state_label": describe_state_key(state_key),
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

    def snapshot(self) -> dict:
        """Return a serializable in-memory snapshot of the Q-table."""
        return {
            "q_values": deepcopy(self._q_cache),
            "visit_counts": deepcopy(self._visit_cache),
            "total_episodes": int(self._total_episodes),
        }

    def load_snapshot(self, snapshot: dict | None) -> None:
        """Load a serializable Q-table snapshot into memory."""
        snapshot = snapshot or {}
        raw_q_values = snapshot.get("q_values") or {}
        raw_visits = snapshot.get("visit_counts") or {}

        self._q_cache = {
            str(state_key): [float(value) for value in values]
            for state_key, values in raw_q_values.items()
        }
        self._visit_cache = {
            str(state_key): [int(value) for value in values]
            for state_key, values in raw_visits.items()
        }
        for state_key in list(self._q_cache.keys()):
            if state_key not in self._visit_cache:
                self._visit_cache[state_key] = [0] * N_ACTIONS
        self._total_episodes = int(snapshot.get("total_episodes") or 0)
        self._cache_loaded = True

    def clone(self) -> "QLearningPolicy":
        """Clone the current in-memory policy for offline candidate training."""
        clone = QLearningPolicy()
        clone.load_snapshot(self.snapshot())
        clone._cache_loaded = self._cache_loaded
        return clone

    async def activate_snapshot(self, snapshot: dict) -> None:
        """Persist a candidate snapshot as the active live Q-table."""
        from db.database import AsyncSessionLocal
        from sqlalchemy import text

        q_values = snapshot.get("q_values") or {}
        visit_counts = snapshot.get("visit_counts") or {}

        async with AsyncSessionLocal() as session:
            await session.execute(text("DELETE FROM rl_agent_qtable"))

            rows: list[dict[str, object]] = []
            for state_key, values in q_values.items():
                visits = visit_counts.get(state_key) or [0] * len(values)
                for action_idx, q_value in enumerate(values):
                    visit_count = int(visits[action_idx]) if action_idx < len(visits) else 0
                    if float(q_value) == 0.0 and visit_count == 0:
                        continue
                    rows.append(
                        {
                            "state_hash": str(state_key),
                            "action_idx": int(action_idx),
                            "q_value": float(q_value),
                            "visit_count": visit_count,
                        }
                    )

            if rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO rl_agent_qtable (state_hash, action_idx, q_value, visit_count, last_updated)
                        VALUES (:state_hash, :action_idx, :q_value, :visit_count, now())
                        """
                    ),
                    rows,
                )
            await session.commit()

        self.load_snapshot(snapshot)

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
        self._visit_cache = {}
        self._total_episodes = 0
        logger.info("[RL] Q-table reset complete.")


# Module-level singleton shared across service and API
rl_policy = QLearningPolicy()
