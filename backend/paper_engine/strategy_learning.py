"""Durable learning scores for NSE paper strategies.

This module intentionally learns slowly: it ranks and sizes candidates from
paper-trade outcomes, but it does not bypass strategy/risk gates.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy import text

from core.config import settings
from db.database import AsyncSessionLocal


IST = timezone(timedelta(hours=5, minutes=30))
ALL_REASONS = "all"


@dataclass(frozen=True)
class LearningKey:
    strategy_key: str
    underlying: str
    option_type: str
    signal_reason: str = ALL_REASONS

    @classmethod
    def from_parts(
        cls,
        strategy_key: str,
        underlying: str,
        option_type: str | None,
        signal_reason: str | None = None,
    ) -> "LearningKey":
        return cls(
            strategy_key=str(strategy_key or "").strip(),
            underlying=str(underlying or "").upper().strip(),
            option_type=str(option_type or "NA").upper().strip(),
            signal_reason=str(signal_reason or ALL_REASONS).lower().strip() or ALL_REASONS,
        )


@dataclass
class LearningScore:
    strategy_key: str
    underlying: str
    option_type: str
    signal_reason: str
    observations: int = 0
    candidates: int = 0
    entries: int = 0
    open_count: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    avg_realized_pnl: Optional[float] = None
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    score: float = 0.0
    confidence: float = 0.5
    risk_multiplier: float = 1.0
    size_multiplier: float = 1.0
    block_new_entries: bool = False
    last_signal_at: Optional[str] = None
    last_trade_at: Optional[str] = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = self.metadata or {}
        return payload


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_dt_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.astimezone(IST).isoformat()
    return str(value) if value else None


class StrategyLearningService:
    def __init__(self) -> None:
        self._cache: dict[LearningKey, LearningScore] = {}
        self._cache_loaded_at = 0.0
        self._cache_ttl_seconds = 30.0

    def _compute_score(self, payload: dict[str, Any]) -> LearningScore:
        entries = int(payload.get("entries") or 0)
        open_count = int(payload.get("open_count") or 0)
        wins = int(payload.get("wins") or 0)
        losses = int(payload.get("losses") or 0)
        closed = wins + losses
        realized_pnl = float(payload.get("realized_pnl") or 0.0)
        unrealized_pnl = float(payload.get("unrealized_pnl") or 0.0)
        win_rate = (wins / closed) if closed else None
        avg_realized = (realized_pnl / closed) if closed else None
        expectancy = avg_realized

        win_component = ((win_rate - 0.5) * 80.0) if win_rate is not None else 0.0
        expectancy_component = math.tanh((expectancy or 0.0) / 2500.0) * 35.0
        pnl_component = math.tanh(realized_pnl / 10000.0) * 20.0
        experience_component = min(entries, 20) * 0.8
        drawdown_penalty = max(losses - wins, 0) * 4.0
        score = _clamp(
            win_component + expectancy_component + pnl_component + experience_component - drawdown_penalty,
            -100.0,
            100.0,
        )

        min_trades = max(int(settings.STRATEGY_LEARNING_MIN_TRADES or 3), 1)
        if closed < min_trades:
            confidence = _clamp(0.50 + score / 500.0, 0.42, 0.58)
            risk_multiplier = _clamp(1.0 + score / 500.0, 0.90, 1.08)
            size_multiplier = _clamp(1.0 + score / 600.0, 0.90, 1.06)
            block_new_entries = False
        else:
            confidence = _clamp(0.52 + score / 220.0, 0.35, 0.82)
            risk_multiplier = _clamp(1.0 + score / 220.0, 0.65, 1.25)
            size_multiplier = _clamp(1.0 + score / 260.0, 0.55, 1.20)
            block_new_entries = bool(
                settings.STRATEGY_LEARNING_BLOCK_ENTRIES_ENABLED
                and win_rate is not None
                and win_rate < 0.25
                and (expectancy or 0.0) < 0.0
            )

        return LearningScore(
            strategy_key=str(payload.get("strategy_key") or ""),
            underlying=str(payload.get("underlying") or "").upper(),
            option_type=str(payload.get("option_type") or "NA").upper(),
            signal_reason=str(payload.get("signal_reason") or ALL_REASONS).lower(),
            observations=int(payload.get("observations") or 0),
            candidates=int(payload.get("candidates") or 0),
            entries=entries,
            open_count=open_count,
            wins=wins,
            losses=losses,
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            avg_realized_pnl=round(avg_realized, 2) if avg_realized is not None else None,
            win_rate=round(win_rate, 4) if win_rate is not None else None,
            expectancy=round(expectancy, 2) if expectancy is not None else None,
            score=round(score, 4),
            confidence=round(confidence, 4),
            risk_multiplier=round(risk_multiplier, 4),
            size_multiplier=round(size_multiplier, 4),
            block_new_entries=block_new_entries,
            last_signal_at=_as_dt_text(payload.get("last_signal_at")),
            last_trade_at=_as_dt_text(payload.get("last_trade_at")),
            metadata={
                "closed_trades": closed,
                "min_trades": min_trades,
                "learning_mode": "ranking_and_sizing",
            },
        )

    async def refresh_scores(
        self,
        *,
        lookback_days: Optional[int] = None,
        strategy_keys: Iterable[str] = ("macd_strategy", "index_mp_strategy"),
        persist: bool = True,
    ) -> dict[str, Any]:
        days = int(lookback_days or settings.STRATEGY_LEARNING_LOOKBACK_DAYS or 120)
        keys = [str(item) for item in strategy_keys if str(item or "").strip()]
        if not keys:
            keys = ["macd_strategy", "index_mp_strategy"]

        async with AsyncSessionLocal() as session:
            signal_result = await session.execute(
                text(
                    """
                    SELECT strategy_key,
                           upper(underlying) AS underlying,
                           upper(COALESCE(option_type, 'NA')) AS option_type,
                           lower(COALESCE(signal_reason, :all_reason)) AS signal_reason,
                           COUNT(*)::int AS observations,
                           COUNT(*) FILTER (WHERE status IN ('candidate', 'open', 'closed'))::int AS candidates,
                           MAX(signal_bar_time) AS last_signal_at
                    FROM agent_signals
                    WHERE market = 'NSE'
                      AND strategy_key = ANY(:strategy_keys)
                      AND updated_at >= NOW() - (CAST(:lookback_days AS INTEGER) * INTERVAL '1 day')
                    GROUP BY strategy_key, upper(underlying), upper(COALESCE(option_type, 'NA')), lower(COALESCE(signal_reason, :all_reason))
                    """
                ),
                {"strategy_keys": keys, "lookback_days": days, "all_reason": ALL_REASONS},
            )
            position_result = await session.execute(
                text(
                    """
                    SELECT strategy_key,
                           upper(underlying) AS underlying,
                           upper(COALESCE(option_type, 'NA')) AS option_type,
                           lower(COALESCE(signal_reason, :all_reason)) AS signal_reason,
                           COUNT(*)::int AS entries,
                           COUNT(*) FILTER (WHERE status = 'open')::int AS open_count,
                           COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl > 0)::int AS wins,
                           COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl < 0)::int AS losses,
                           COALESCE(SUM(realized_pnl), 0.0) AS realized_pnl,
                           COALESCE(SUM(unrealized_pnl), 0.0) AS unrealized_pnl,
                           MAX(COALESCE(closed_at, entered_at, updated_at)) AS last_trade_at
                    FROM agent_positions
                    WHERE market = 'NSE'
                      AND strategy_key = ANY(:strategy_keys)
                      AND updated_at >= NOW() - (CAST(:lookback_days AS INTEGER) * INTERVAL '1 day')
                    GROUP BY strategy_key, upper(underlying), upper(COALESCE(option_type, 'NA')), lower(COALESCE(signal_reason, :all_reason))
                    """
                ),
                {"strategy_keys": keys, "lookback_days": days, "all_reason": ALL_REASONS},
            )

            merged: dict[LearningKey, dict[str, Any]] = {}

            def _merge(row: dict[str, Any], *, aggregate_reason: bool = False) -> None:
                reason = ALL_REASONS if aggregate_reason else str(row.get("signal_reason") or ALL_REASONS)
                key = LearningKey.from_parts(
                    str(row.get("strategy_key") or ""),
                    str(row.get("underlying") or ""),
                    str(row.get("option_type") or "NA"),
                    reason,
                )
                payload = merged.setdefault(
                    key,
                    {
                        "strategy_key": key.strategy_key,
                        "underlying": key.underlying,
                        "option_type": key.option_type,
                        "signal_reason": key.signal_reason,
                    },
                )
                for field in (
                    "observations",
                    "candidates",
                    "entries",
                    "open_count",
                    "wins",
                    "losses",
                    "realized_pnl",
                    "unrealized_pnl",
                ):
                    if field in row:
                        payload[field] = payload.get(field, 0) + (row.get(field) or 0)
                for field in ("last_signal_at", "last_trade_at"):
                    if row.get(field) and (
                        not payload.get(field) or row.get(field) > payload.get(field)
                    ):
                        payload[field] = row.get(field)

            for mapping in signal_result.mappings():
                row = dict(mapping)
                _merge(row)
                if str(row.get("signal_reason") or ALL_REASONS).lower() != ALL_REASONS:
                    _merge(row, aggregate_reason=True)
            for mapping in position_result.mappings():
                row = dict(mapping)
                _merge(row)
                if str(row.get("signal_reason") or ALL_REASONS).lower() != ALL_REASONS:
                    _merge(row, aggregate_reason=True)

            scores = [self._compute_score(payload) for payload in merged.values()]

            if persist and scores:
                await session.execute(
                    text(
                        """
                        INSERT INTO strategy_learning_scores (
                            market, strategy_key, underlying, option_type, signal_reason,
                            observations, candidates, entries, open_count, wins, losses,
                            realized_pnl, unrealized_pnl, avg_realized_pnl, win_rate, expectancy,
                            score, confidence, risk_multiplier, size_multiplier, block_new_entries,
                            last_signal_at, last_trade_at, metadata, updated_at
                        ) VALUES (
                            'NSE', :strategy_key, :underlying, :option_type, :signal_reason,
                            :observations, :candidates, :entries, :open_count, :wins, :losses,
                            :realized_pnl, :unrealized_pnl, :avg_realized_pnl, :win_rate, :expectancy,
                            :score, :confidence, :risk_multiplier, :size_multiplier, :block_new_entries,
                            :last_signal_at, :last_trade_at, CAST(:metadata AS JSONB), NOW()
                        )
                        ON CONFLICT (market, strategy_key, underlying, option_type, signal_reason)
                        DO UPDATE SET
                            observations = EXCLUDED.observations,
                            candidates = EXCLUDED.candidates,
                            entries = EXCLUDED.entries,
                            open_count = EXCLUDED.open_count,
                            wins = EXCLUDED.wins,
                            losses = EXCLUDED.losses,
                            realized_pnl = EXCLUDED.realized_pnl,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            avg_realized_pnl = EXCLUDED.avg_realized_pnl,
                            win_rate = EXCLUDED.win_rate,
                            expectancy = EXCLUDED.expectancy,
                            score = EXCLUDED.score,
                            confidence = EXCLUDED.confidence,
                            risk_multiplier = EXCLUDED.risk_multiplier,
                            size_multiplier = EXCLUDED.size_multiplier,
                            block_new_entries = EXCLUDED.block_new_entries,
                            last_signal_at = EXCLUDED.last_signal_at,
                            last_trade_at = EXCLUDED.last_trade_at,
                            metadata = EXCLUDED.metadata,
                            updated_at = NOW()
                        """
                    ),
                    [
                        {
                            **score.to_dict(),
                            "metadata": json.dumps(score.metadata or {}),
                        }
                        for score in scores
                    ],
                )
                await session.commit()

        self._cache = {
            LearningKey.from_parts(
                score.strategy_key,
                score.underlying,
                score.option_type,
                score.signal_reason,
            ): score
            for score in scores
        }
        self._cache_loaded_at = monotonic()
        return {
            "refreshed": True,
            "lookback_days": days,
            "scores": len(scores),
            "strategies": keys,
            "top": [score.to_dict() for score in sorted(scores, key=lambda item: item.score, reverse=True)[:10]],
            "bottom": [score.to_dict() for score in sorted(scores, key=lambda item: item.score)[:10]],
        }

    async def load_scores(self, strategy_key: Optional[str] = None) -> dict[LearningKey, LearningScore]:
        if not settings.STRATEGY_LEARNING_ENABLED:
            return {}
        if self._cache and monotonic() - self._cache_loaded_at <= self._cache_ttl_seconds:
            if strategy_key:
                return {key: value for key, value in self._cache.items() if key.strategy_key == strategy_key}
            return dict(self._cache)

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT strategy_key, underlying, option_type, signal_reason,
                               observations, candidates, entries, open_count, wins, losses,
                               realized_pnl, unrealized_pnl, avg_realized_pnl, win_rate, expectancy,
                               score, confidence, risk_multiplier, size_multiplier, block_new_entries,
                               last_signal_at, last_trade_at, metadata
                        FROM strategy_learning_scores
                        WHERE market = 'NSE'
                          AND (:strategy_key IS NULL OR strategy_key = :strategy_key)
                        ORDER BY score DESC, updated_at DESC
                        """
                    ),
                    {"strategy_key": strategy_key},
                )
                scores: dict[LearningKey, LearningScore] = {}
                for row in result.mappings():
                    payload = dict(row)
                    metadata = payload.get("metadata") or {}
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except json.JSONDecodeError:
                            metadata = {}
                    score = LearningScore(
                        **{
                            **payload,
                            "last_signal_at": _as_dt_text(payload.get("last_signal_at")),
                            "last_trade_at": _as_dt_text(payload.get("last_trade_at")),
                            "metadata": metadata,
                        }
                    )
                    scores[LearningKey.from_parts(score.strategy_key, score.underlying, score.option_type, score.signal_reason)] = score
                if strategy_key is None:
                    self._cache = scores
                    self._cache_loaded_at = monotonic()
                return scores
        except Exception as exc:
            logger.warning(f"[StrategyLearning] score load failed: {exc}")
            return {}

    def pick_score(
        self,
        scores: dict[LearningKey, LearningScore],
        *,
        strategy_key: str,
        underlying: str,
        option_type: str | None,
        signal_reason: str | None = None,
    ) -> Optional[LearningScore]:
        exact = LearningKey.from_parts(strategy_key, underlying, option_type, signal_reason)
        if exact in scores:
            return scores[exact]
        aggregate = LearningKey.from_parts(strategy_key, underlying, option_type, ALL_REASONS)
        return scores.get(aggregate)

    def annotate_payload(
        self,
        payload: dict[str, Any],
        score: Optional[LearningScore],
    ) -> dict[str, Any]:
        if not score:
            payload.setdefault("learning_score", 0.0)
            payload.setdefault("learning_confidence", 0.5)
            payload.setdefault("learning_size_multiplier", 1.0)
            payload.setdefault("learning_risk_multiplier", 1.0)
            return payload
        payload["learning_score"] = score.score
        payload["learning_confidence"] = score.confidence
        payload["learning_size_multiplier"] = score.size_multiplier
        payload["learning_risk_multiplier"] = score.risk_multiplier
        payload["learning_entries"] = score.entries
        payload["learning_win_rate"] = score.win_rate
        payload["learning_expectancy"] = score.expectancy
        payload["learning_blocked"] = score.block_new_entries
        return payload

    async def summary(self, *, refresh: bool = False, limit: int = 20) -> dict[str, Any]:
        if refresh:
            await self.refresh_scores()
        scores = await self.load_scores()
        ordered = sorted(scores.values(), key=lambda item: item.score, reverse=True)
        return {
            "enabled": bool(settings.STRATEGY_LEARNING_ENABLED),
            "mode": "ranking_and_sizing",
            "score_count": len(ordered),
            "top": [item.to_dict() for item in ordered[:limit]],
            "bottom": [item.to_dict() for item in sorted(scores.values(), key=lambda item: item.score)[:limit]],
        }


strategy_learning_service = StrategyLearningService()
