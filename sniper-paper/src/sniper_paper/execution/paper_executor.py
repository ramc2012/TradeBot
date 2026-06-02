"""Paper executor — simulates fills, tracks open positions, exits at stop/target/timeout.

Hard rule: This module must NEVER import the Fyers order module. The lint test
in tests/test_no_live_orders.py enforces this.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg
import pandas as pd

from sniper_paper.common.logging import get_logger
from sniper_paper.common.settings import Instrument, Settings
from sniper_paper.execution.cost_model import round_trip_costs, slippage_inr_one_side
from sniper_paper.persistence import repository as repo
from sniper_paper.signals.engine import ScoredSignal

log = get_logger(__name__)


MAX_HOLD_MINUTES = 90   # mirrors sniper-phase0 labeling.max_hold_minutes


@dataclass
class OpenPosition:
    position_id: int
    instrument: str
    symbol: str
    side: str
    qty: int
    entry_ts: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float
    signal_id: int
    mae: float = 0.0
    mfe: float = 0.0


class PaperExecutor:
    def __init__(self, pool: asyncpg.Pool, settings: Settings):
        self.pool = pool
        self.settings = settings
        self.open_positions: dict[int, OpenPosition] = {}

    # ─── Entry ────────────────────────────────────────────────────
    async def open_paper_trade(
        self, signal_id: int, scored: ScoredSignal, fill_price: float, fill_ts: pd.Timestamp,
        instrument: Instrument,
    ) -> int | None:
        # Compute and apply entry slippage to the recorded fill price.
        slip = slippage_inr_one_side(self.settings.costs, fill_price, instrument.lot_size)
        adjusted_fill = (
            fill_price + slip / instrument.lot_size
            if scored.candidate.side == "long"
            else fill_price - slip / instrument.lot_size
        )

        order_id = await repo.insert_order(
            self.pool,
            {
                "signal_id": signal_id,
                "placed_ts": fill_ts,
                "instrument": instrument.name,
                "symbol": instrument.near_month_symbol,
                "side": scored.candidate.side,
                "qty": instrument.lot_size,
                "intended_price": scored.candidate.entry_price,
                "fill_ts": fill_ts,
                "fill_price": adjusted_fill,
                "slippage_inr": slip,
                "status": "filled",
            },
        )

        position_id = await repo.insert_position(
            self.pool,
            {
                "signal_id": signal_id,
                "open_order_id": order_id,
                "instrument": instrument.name,
                "symbol": instrument.near_month_symbol,
                "side": scored.candidate.side,
                "qty": instrument.lot_size,
                "entry_ts": fill_ts,
                "entry_price": adjusted_fill,
                "stop_price": scored.candidate.stop_price,
                "target_price": scored.candidate.target_price,
            },
        )

        self.open_positions[position_id] = OpenPosition(
            position_id=position_id,
            instrument=instrument.name,
            symbol=instrument.near_month_symbol,
            side=scored.candidate.side,
            qty=instrument.lot_size,
            entry_ts=fill_ts,
            entry_price=adjusted_fill,
            stop_price=scored.candidate.stop_price,
            target_price=scored.candidate.target_price,
            signal_id=signal_id,
        )
        log.info(
            "Opened paper %s %s @ %.2f (stop %.2f, target %.2f) pos=%d",
            scored.candidate.side, instrument.name,
            adjusted_fill, scored.candidate.stop_price, scored.candidate.target_price,
            position_id,
        )
        return position_id

    # ─── Update + exit ────────────────────────────────────────────
    def update_mae_mfe(self, position: OpenPosition, ltp: float) -> None:
        if position.side == "long":
            position.mae = min(position.mae, ltp - position.entry_price)
            position.mfe = max(position.mfe, ltp - position.entry_price)
        else:
            position.mae = min(position.mae, position.entry_price - ltp)
            position.mfe = max(position.mfe, position.entry_price - ltp)

    def check_exit(self, position: OpenPosition, ltp: float, ts: pd.Timestamp) -> str | None:
        if position.side == "long":
            if ltp <= position.stop_price:
                return "stop"
            if ltp >= position.target_price:
                return "target"
        else:
            if ltp >= position.stop_price:
                return "stop"
            if ltp <= position.target_price:
                return "target"
        if (ts - position.entry_ts) >= pd.Timedelta(minutes=MAX_HOLD_MINUTES):
            return "timeout"
        return None

    async def close_paper_trade(
        self, position: OpenPosition, exit_price: float, exit_ts: pd.Timestamp,
        outcome: str, instrument: Instrument,
    ) -> None:
        slip = slippage_inr_one_side(self.settings.costs, exit_price, position.qty)
        adjusted_exit = (
            exit_price - slip / position.qty
            if position.side == "long"
            else exit_price + slip / position.qty
        )

        close_order = await repo.insert_order(
            self.pool,
            {
                "signal_id": position.signal_id,
                "placed_ts": exit_ts,
                "instrument": instrument.name,
                "symbol": position.symbol,
                "side": "short" if position.side == "long" else "long",
                "qty": position.qty,
                "intended_price": exit_price,
                "fill_ts": exit_ts,
                "fill_price": adjusted_exit,
                "slippage_inr": slip,
                "status": "filled",
            },
        )

        if position.side == "long":
            gross = (adjusted_exit - position.entry_price) * position.qty
        else:
            gross = (position.entry_price - adjusted_exit) * position.qty

        costs = round_trip_costs(
            self.settings.costs, instrument.exchange, position.qty,
            position.entry_price, adjusted_exit,
        )
        net = gross - costs["total"]
        risk_inr = abs(position.entry_price - position.stop_price) * position.qty
        net_R = net / risk_inr if risk_inr > 0 else 0.0

        await repo.close_position(
            self.pool, position.position_id,
            {
                "close_order_id": close_order,
                "exit_ts": exit_ts,
                "exit_price": adjusted_exit,
                "outcome": outcome,
                "gross_pnl": gross,
                "costs_inr": costs["total"],
                "net_pnl": net,
                "net_R": net_R,
                "mae": position.mae * position.qty,
                "mfe": position.mfe * position.qty,
            },
        )
        self.open_positions.pop(position.position_id, None)
        log.info(
            "Closed paper %s %s outcome=%s gross=%.2f net=%.2f netR=%.3f",
            position.side, instrument.name, outcome, gross, net, net_R,
        )

    async def on_tick(
        self, instrument: Instrument, ltp: float, ts: pd.Timestamp,
    ) -> None:
        """Called on every tick — checks open positions for stop/target hits."""
        for pos in list(self.open_positions.values()):
            if pos.symbol != instrument.near_month_symbol:
                continue
            self.update_mae_mfe(pos, ltp)
            outcome = self.check_exit(pos, ltp, ts)
            if outcome is not None:
                await self.close_paper_trade(pos, ltp, ts, outcome, instrument)
