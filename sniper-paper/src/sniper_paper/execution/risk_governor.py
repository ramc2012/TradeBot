"""Risk governor — the kill-switch layer between signal engine and paper executor.

A signal that passes the EV gate STILL has to pass these checks before a
paper order is opened. Failure modes are open-fail: if any state lookup
errors, the order is REJECTED.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import asyncpg

from sniper_paper.common.logging import get_logger
from sniper_paper.common.settings import Instrument, Settings
from sniper_paper.common.time import is_in_trading_hours
from sniper_paper.persistence import repository as repo

log = get_logger(__name__)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str | None = None


class RiskGovernor:
    def __init__(self, pool: asyncpg.Pool, settings: Settings):
        self.pool = pool
        self.settings = settings

    async def check_signal(
        self, instrument: Instrument, decision_ts, p_win: float, expected_R: float
    ) -> RiskDecision:
        # 1. Trading hours
        if self.settings.risk.reject_signals_outside_trading_hours and not is_in_trading_hours(decision_ts, instrument):
            return RiskDecision(False, "outside_trading_hours")

        # 2. OOD policy
        if not instrument.model_in_distribution and not self.settings.risk.allow_ood_paper_trades:
            return RiskDecision(False, "ood_disabled")

        # 3. Daily signal budget
        try:
            today = decision_ts.date() if hasattr(decision_ts, "date") else date.today()
            pnl = await repo.get_daily_pnl(self.pool, today)
        except Exception as e:
            log.error("Risk check: failed to load daily P&L: %s", e)
            return RiskDecision(False, "daily_pnl_lookup_failed")

        if pnl:
            if pnl.get("kill_switch_tripped"):
                return RiskDecision(False, "kill_switch_tripped")
            if pnl["n_taken"] >= self.settings.risk.max_signals_per_day:
                return RiskDecision(False, "max_signals_per_day_reached")
            if pnl["net_pnl"] <= -self.settings.risk.daily_loss_cap_inr:
                return RiskDecision(False, "daily_loss_cap_reached")
            if pnl["consec_losses"] >= self.settings.risk.consecutive_loss_kill_switch:
                return RiskDecision(False, "consecutive_loss_kill_switch")

        # 4. Position concurrency
        try:
            open_pos = await repo.open_positions(self.pool)
        except Exception as e:
            log.error("Risk check: failed to load open positions: %s", e)
            return RiskDecision(False, "open_positions_lookup_failed")

        if len(open_pos) >= self.settings.risk.max_open_positions_total:
            return RiskDecision(False, "max_total_positions_reached")
        same_inst = [p for p in open_pos if p["instrument"] == instrument.name]
        if len(same_inst) >= self.settings.risk.max_open_positions_per_instrument:
            return RiskDecision(False, f"max_positions_{instrument.name}_reached")

        return RiskDecision(True, None)
