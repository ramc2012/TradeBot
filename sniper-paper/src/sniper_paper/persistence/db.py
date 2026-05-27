from __future__ import annotations

import asyncpg

from sniper_paper.common.settings import Settings


_pool: asyncpg.Pool | None = None


async def init_pool(settings: Settings) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.db_dsn(),
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    return _pool


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised; call init_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
