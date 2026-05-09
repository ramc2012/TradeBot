from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from auction_intelligence.config import clone_default_config
from auction_intelligence.rl.policy import QLearningPolicy, rl_policy
from auction_intelligence.rl.trainer import (
    _action_from_record,
    _reward_from_record,
    _state_from_record,
    fetch_training_records,
    train_policy_from_records,
)
from auction_intelligence.rl.versions import RLPolicyVersionStore

logger = logging.getLogger(__name__)


def _parse_clock(raw: str, fallback: str) -> time:
    try:
        return time.fromisoformat(raw)
    except (TypeError, ValueError):
        return time.fromisoformat(fallback)


def split_records_for_cycle(
    records: list[dict[str, Any]],
    *,
    holdout_fraction: float,
    min_holdout_records: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not records:
        return [], []

    ordered = list(records)
    holdout_size = max(min_holdout_records, ceil(len(ordered) * holdout_fraction))
    holdout_size = min(max(holdout_size, 1), len(ordered))
    train_size = max(len(ordered) - holdout_size, 0)
    return ordered[:train_size], ordered[train_size:]


def evaluate_policy_on_records(
    policy: QLearningPolicy,
    records: list[dict[str, Any]],
    *,
    use_proxy_reward: bool,
) -> dict[str, Any]:
    evaluable = 0
    matched = 0
    reward_total = 0.0
    negative = 0
    fill_drift_total = 0.0

    for record in records:
        state = _state_from_record(record)
        if state is None:
            continue
        reward = _reward_from_record(record, use_proxy_reward=use_proxy_reward)
        if reward is None:
            continue

        evaluable += 1
        actual_action_idx = _action_from_record(record)
        selected_action_idx = policy.select_action_sync(state, force_exploit=True).action_idx
        if selected_action_idx != actual_action_idx:
            continue

        matched += 1
        reward_total += float(reward)
        if float(reward) < 0:
            negative += 1
        fill_drift_total += float(record.get("fill_drift_ticks") or 0.0)

    return {
        "evaluable_records": evaluable,
        "matched_actions": matched,
        "match_rate": round((matched / evaluable), 4) if evaluable else 0.0,
        "average_reward": round((reward_total / matched), 4) if matched else 0.0,
        "negative_reward_ratio": round((negative / matched), 4) if matched else 0.0,
        "average_fill_drift_ticks": round((fill_drift_total / matched), 4) if matched else 0.0,
    }


def build_promotion_decision(
    *,
    training_summary: dict[str, Any],
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []

    min_train_records = int(config.get("min_train_records", 80))
    min_holdout_records = int(config.get("min_holdout_records", 20))
    min_candidate_matches = int(config.get("min_candidate_matches", 8))
    min_avg_reward_edge = float(config.get("min_avg_reward_edge", 0.03))
    max_negative_ratio_worsening = float(config.get("max_negative_ratio_worsening", 0.05))
    max_fill_drift_worsening = float(config.get("max_fill_drift_worsening_ticks", 0.5))

    trained_on = int(training_summary.get("trained_on") or 0)
    holdout_count = int(candidate_metrics.get("evaluable_records") or 0)
    candidate_matches = int(candidate_metrics.get("matched_actions") or 0)

    reward_edge = float(candidate_metrics.get("average_reward") or 0.0) - float(
        baseline_metrics.get("average_reward") or 0.0
    )
    negative_ratio_edge = float(candidate_metrics.get("negative_reward_ratio") or 0.0) - float(
        baseline_metrics.get("negative_reward_ratio") or 0.0
    )
    fill_drift_edge = float(candidate_metrics.get("average_fill_drift_ticks") or 0.0) - float(
        baseline_metrics.get("average_fill_drift_ticks") or 0.0
    )

    if trained_on < min_train_records:
        blockers.append(f"trained_on_below_min:{trained_on}<{min_train_records}")
    if holdout_count < min_holdout_records:
        blockers.append(f"holdout_below_min:{holdout_count}<{min_holdout_records}")
    if candidate_matches < min_candidate_matches:
        blockers.append(f"candidate_matches_below_min:{candidate_matches}<{min_candidate_matches}")
    if reward_edge < min_avg_reward_edge:
        blockers.append(f"reward_edge_below_min:{reward_edge:.4f}<{min_avg_reward_edge:.4f}")
    if negative_ratio_edge > max_negative_ratio_worsening:
        blockers.append(
            f"negative_ratio_worsened:{negative_ratio_edge:.4f}>{max_negative_ratio_worsening:.4f}"
        )
    if fill_drift_edge > max_fill_drift_worsening:
        blockers.append(f"fill_drift_worsened:{fill_drift_edge:.4f}>{max_fill_drift_worsening:.4f}")

    return {
        "should_promote": not blockers,
        "reward_edge": round(reward_edge, 4),
        "negative_ratio_edge": round(negative_ratio_edge, 4),
        "fill_drift_edge": round(fill_drift_edge, 4),
        "blockers": blockers,
    }


class RLAutoTrainer:
    def __init__(
        self,
        config: dict | None = None,
        *,
        policy: QLearningPolicy | None = None,
        version_store: RLPolicyVersionStore | None = None,
    ) -> None:
        self.config = config or clone_default_config()
        self.policy = policy or rl_policy
        self.version_store = version_store or RLPolicyVersionStore()
        self._scheduler: AsyncIOScheduler | None = None
        self._lock = asyncio.Lock()

    @property
    def rl_config(self) -> dict[str, Any]:
        return self.config.get("rl", {})

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(str(self.rl_config.get("timezone", "Asia/Kolkata")))

    def schedule_time(self) -> time:
        close_clock = _parse_clock(
            str(self.config.get("mvp_scope", {}).get("session", {}).get("close", "15:30")),
            "15:30",
        )
        after_close_minutes = int(self.rl_config.get("run_after_close_minutes", 45))
        scheduled_dt = datetime.combine(date.today(), close_clock) + timedelta(minutes=after_close_minutes)
        return scheduled_dt.time().replace(second=0, microsecond=0)

    async def start(self) -> None:
        if not bool(self.rl_config.get("auto_train_enabled", True)):
            return
        if self._scheduler is not None:
            return

        run_clock = self.schedule_time()
        scheduler = AsyncIOScheduler(timezone=self.timezone)
        scheduler.add_job(
            self._run_scheduled_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=run_clock.hour,
                minute=run_clock.minute,
                timezone=self.timezone,
            ),
            id="auction-intelligence-rl-cycle",
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        await self._catch_up_if_needed()

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _run_scheduled_job(self) -> None:
        result = await self.run_cycle(source="scheduled")
        logger.info("[RL] scheduled cycle result=%s", result.get("status"))

    def _market_session_date(self, now_local: datetime) -> date:
        open_clock = _parse_clock(
            str(self.config.get("mvp_scope", {}).get("session", {}).get("open", "09:15")),
            "09:15",
        )
        candidate = now_local.date() if now_local.timetz().replace(tzinfo=None) >= open_clock else now_local.date() - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate

    async def _catch_up_if_needed(self) -> None:
        session_date = self._market_session_date(datetime.now(self.timezone))
        if await self.version_store.has_run_for_session(
            session_date=session_date,
            sources=("scheduled", "startup_catchup"),
        ):
            return

        run_dt = datetime.combine(session_date, self.schedule_time(), tzinfo=self.timezone)
        if datetime.now(self.timezone) >= run_dt:
            await self.run_cycle(source="startup_catchup")

    async def run_cycle(
        self,
        *,
        source: str = "manual",
        symbol: str | None = None,
        max_trades: int | None = None,
        use_proxy_reward: bool | None = None,
        promote_if_eligible: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            if not self.policy._cache_loaded:
                await self.policy.load_cache()

            max_trades = int(max_trades or self.rl_config.get("max_trades", 600))
            use_proxy_reward = bool(
                self.rl_config.get("use_proxy_reward", True)
                if use_proxy_reward is None
                else use_proxy_reward
            )

            records = await fetch_training_records(max_trades=max_trades, symbol=symbol)
            if not records:
                return {
                    "status": "skipped",
                    "reason": "no_records",
                    "max_trades": max_trades,
                    "symbol": symbol,
                }

            records = list(reversed(records))
            train_records, holdout_records = split_records_for_cycle(
                records,
                holdout_fraction=float(self.rl_config.get("holdout_fraction", 0.2)),
                min_holdout_records=int(self.rl_config.get("min_holdout_records", 20)),
            )

            baseline = self.policy.clone()
            candidate = baseline.clone()
            training_summary = await train_policy_from_records(
                candidate,
                train_records,
                use_proxy_reward=use_proxy_reward,
                persist=False,
            )
            baseline_metrics = evaluate_policy_on_records(
                baseline,
                holdout_records,
                use_proxy_reward=use_proxy_reward,
            )
            candidate_metrics = evaluate_policy_on_records(
                candidate,
                holdout_records,
                use_proxy_reward=use_proxy_reward,
            )
            decision = build_promotion_decision(
                training_summary=training_summary,
                baseline_metrics=baseline_metrics,
                candidate_metrics=candidate_metrics,
                config=self.rl_config,
            )

            version_name = f"rl-{datetime.now(self.timezone):%Y%m%dT%H%M%S}-{source}"
            snapshot = candidate.snapshot()
            metrics = {
                "training": training_summary,
                "holdout": {
                    "records": len(holdout_records),
                    "baseline": baseline_metrics,
                    "candidate": candidate_metrics,
                },
                "decision": decision,
                "use_proxy_reward": use_proxy_reward,
            }

            status = "candidate" if decision["should_promote"] and not promote_if_eligible else (
                "rejected" if not decision["should_promote"] else "candidate"
            )
            version = await self.version_store.create_version(
                version_name=version_name,
                status=status,
                source=source,
                symbol=symbol,
                trained_on=int(training_summary.get("trained_on") or 0),
                skipped=int(training_summary.get("skipped") or 0),
                average_reward=float(training_summary.get("average_reward") or 0.0),
                metrics=metrics,
                qtable_snapshot=snapshot,
                promotion_reason=None if decision["should_promote"] else "; ".join(decision["blockers"]),
            )

            promotion = None
            if decision["should_promote"] and promote_if_eligible:
                await self.policy.activate_snapshot(snapshot)
                promotion = await self.version_store.promote_version(
                    version["id"],
                    promotion_reason=(
                        f"Reward edge {decision['reward_edge']:.4f}; "
                        f"negative ratio edge {decision['negative_ratio_edge']:.4f}; "
                        f"fill drift edge {decision['fill_drift_edge']:.4f}"
                    ),
                )
                version = promotion or version

            return {
                "status": "promoted" if promotion else status,
                "source": source,
                "symbol": symbol,
                "version": version,
                "training": training_summary,
                "holdout": {
                    "records": len(holdout_records),
                    "baseline": baseline_metrics,
                    "candidate": candidate_metrics,
                },
                "decision": decision,
                "use_proxy_reward": use_proxy_reward,
            }


rl_auto_trainer = RLAutoTrainer()
