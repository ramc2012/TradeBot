"""Batched writer from in-memory tick queue → TimescaleDB."""
from __future__ import annotations

import asyncio
from collections import deque

import asyncpg

from sniper_paper.common.logging import get_logger
from sniper_paper.persistence import repository as repo

log = get_logger(__name__)

BATCH_SIZE = 200
FLUSH_INTERVAL_SEC = 2.0


class TickWriter:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.buffer: deque[dict] = deque()
        self._stop = asyncio.Event()

    def enqueue(self, tick: dict) -> None:
        self.buffer.append(tick)

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(FLUSH_INTERVAL_SEC)
            await self._flush()

    async def stop(self) -> None:
        self._stop.set()
        await self._flush()

    async def _flush(self) -> None:
        if not self.buffer:
            return
        batch = []
        while self.buffer and len(batch) < BATCH_SIZE:
            batch.append(self.buffer.popleft())
        try:
            await repo.insert_ticks_batch(self.pool, batch)
        except Exception as e:
            log.error("Tick write failed (%d rows lost): %s", len(batch), e)
