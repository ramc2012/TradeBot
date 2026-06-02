"""Main orchestrator. Long-running async process.

Wires:  Fyers WS → tick buffer + tick writer → decision loop (every 30s)
        → signal engine → risk governor → paper executor.
Also runs the tick-driven exit checker (every tick) for open positions.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from pathlib import Path

import asyncpg
import pandas as pd

from sniper_paper.common.logging import get_logger, setup_logging
from sniper_paper.common.settings import Instrument, Settings
from sniper_paper.common.time import IST, is_in_trading_hours, now_ist
from sniper_paper.execution.paper_executor import PaperExecutor
from sniper_paper.execution.risk_governor import RiskGovernor
from sniper_paper.ingest.broker_creds import BrokerCredsStore
from sniper_paper.ingest.fyers_ws import FyersIngest
from sniper_paper.ingest.tick_buffer import TickBuffer
from sniper_paper.ingest.tick_writer import TickWriter
from sniper_paper.model.loader import load_active
from sniper_paper.persistence import repository as repo
from sniper_paper.persistence.db import close_pool, init_pool
from sniper_paper.signals.engine import evaluate

log = get_logger(__name__)


class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.buffer = TickBuffer()
        self.model = load_active(settings.model.active_model_pointer)
        self.run_id = uuid.uuid4()
        self._stop = asyncio.Event()
        self.pool: asyncpg.Pool | None = None
        self.executor: PaperExecutor | None = None
        self.governor: RiskGovernor | None = None
        self.ingest: FyersIngest | None = None
        self.writer: TickWriter | None = None
        self.creds_store: BrokerCredsStore | None = None

    async def start(self) -> None:
        self.pool = await init_pool(self.settings)
        self.creds_store = BrokerCredsStore(self.pool)
        self.executor = PaperExecutor(self.pool, self.settings)
        self.governor = RiskGovernor(self.pool, self.settings)
        self.ingest = FyersIngest(self.settings, self.creds_store)
        self.writer = TickWriter(self.pool)

        from sniper_paper.training.train import _git_sha, _hash_settings
        await repo.insert_run(self.pool, {
            "run_id": self.run_id,
            "started_ts": now_ist(),
            "model_artifact": self.model.artifact_id,
            "config_hash": _hash_settings(self.settings),
            "git_sha": _git_sha(),
            "notes": "paper-trader run",
        })

        await self.ingest.start()
        log.info("Runner started, run_id=%s, model=%s", self.run_id, self.model.artifact_id)

        await asyncio.gather(
            self._ingest_loop(),
            self.writer.run(),
            self._decision_loop(),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self.ingest:
            await self.ingest.stop()
        if self.writer:
            await self.writer.stop()
        await close_pool()

    # ─── Ingest path ──────────────────────────────────────────────
    async def _ingest_loop(self) -> None:
        assert self.ingest is not None and self.writer is not None
        async for tick in self.ingest.stream():
            if self._stop.is_set():
                break
            self.buffer.add(tick)
            self.writer.enqueue(tick)
            try:
                instrument = self.settings.instrument_by_symbol(tick["symbol"])
            except KeyError:
                continue
            if self.executor is not None:
                await self.executor.on_tick(instrument, tick["ltp"], pd.Timestamp(tick["ts"]))
            await self.ingest.publish(tick)

    # ─── Decision loop ────────────────────────────────────────────
    async def _decision_loop(self) -> None:
        cadence = self.settings.signal.decision_cadence_seconds
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("Decision tick failed: %s", e)
            await asyncio.sleep(cadence)

    async def _tick(self) -> None:
        ts = now_ist()
        for instrument in self.settings.instruments:
            if not is_in_trading_hours(ts, instrument):
                continue
            await self._evaluate_instrument(instrument, ts)

    async def _evaluate_instrument(self, instrument: Instrument, decision_ts: pd.Timestamp) -> None:
        assert self.pool is not None and self.executor is not None and self.governor is not None

        spot = self.buffer.last_price(instrument.near_month_symbol)
        if spot is None:
            return

        # Pull session ticks from the buffer (faster than DB).
        from sniper_paper.common.time import parse_hm
        session_open_ist = pd.Timestamp(datetime.combine(
            decision_ts.date(), parse_hm(instrument.trading_hours_ist.open), tzinfo=IST,
        ))
        session_ticks = self.buffer.session_df(instrument.near_month_symbol, session_open_ist, decision_ts)
        if len(session_ticks) < 30:
            return  # too early

        # Prior-session ticks come from the DB.
        prev_ticks_rows = await repo.session_ticks(
            self.pool, instrument.near_month_symbol,
            session_open_ist - pd.Timedelta(days=1),
            session_open_ist - pd.Timedelta(minutes=1),
        )
        prev_session_ticks = pd.DataFrame(prev_ticks_rows) if prev_ticks_rows else pd.DataFrame(columns=["ts", "ltp"])

        scored_signals = evaluate(
            instrument=instrument,
            decision_ts=decision_ts,
            session_ticks=session_ticks,
            prev_session_ticks=prev_session_ticks,
            spot=spot,
            model=self.model,
            settings=self.settings,
        )

        for scored in scored_signals:
            signal_id = await repo.insert_signal(self.pool, {
                "decision_ts": decision_ts,
                "instrument": instrument.name,
                "symbol": instrument.near_month_symbol,
                "setup_name": scored.candidate.setup_name,
                "side": scored.candidate.side,
                "entry_price": scored.candidate.entry_price,
                "stop_price": scored.candidate.stop_price,
                "target_price": scored.candidate.target_price,
                "p_win": scored.p_win,
                "expected_net_R": scored.expected_net_R,
                "in_distribution": scored.in_distribution,
                "gate_decision": scored.gate_decision,
                "gate_reason": scored.gate_reason,
                "features": scored.feature_values,
                "model_artifact": self.model.artifact_id,
                "run_id": self.run_id,
            })

            if scored.gate_decision != "take":
                await self._tally(signal_id, taken=False)
                continue

            risk = await self.governor.check_signal(
                instrument, decision_ts, scored.p_win, scored.expected_net_R
            )
            if not risk.allowed:
                log.info("Risk rejected signal %d: %s", signal_id, risk.reason)
                await self._tally(signal_id, taken=False)
                continue

            await self.executor.open_paper_trade(
                signal_id=signal_id,
                scored=scored,
                fill_price=spot,
                fill_ts=decision_ts,
                instrument=instrument,
            )
            await self._tally(signal_id, taken=True)

    async def _tally(self, signal_id: int, taken: bool) -> None:
        assert self.pool is not None
        today = date.today()
        existing = await repo.get_daily_pnl(self.pool, today)
        row = existing or {
            "date": today, "n_signals": 0, "n_taken": 0, "n_skipped": 0,
            "gross_pnl": 0.0, "costs_inr": 0.0, "net_pnl": 0.0,
            "consec_losses": 0, "kill_switch_tripped": False,
        }
        row["n_signals"] += 1
        if taken:
            row["n_taken"] += 1
        else:
            row["n_skipped"] += 1
        await repo.upsert_daily_pnl(self.pool, row)


async def main_async(config: str = "configs/paper.yaml") -> None:
    setup_logging()
    settings = Settings.load(config)
    runner = Runner(settings)
    try:
        await runner.start()
    finally:
        await runner.stop()


def main() -> None:
    asyncio.run(main_async())
