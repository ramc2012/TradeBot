from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


def _database_pool_config() -> dict[str, int | bool]:
    is_development = settings.APP_ENV == "development"
    # Production defaults bumped from (3, 2) to (10, 20). The previous values
    # left a single Cloud Run instance with at most 5 concurrent connections,
    # which the multi-agent runtime (Strategy 1+2 scanners, FMP, directional,
    # auction-IQ, MP, commodity, plus API traffic) routinely exhausted —
    # surfacing as `QueuePool limit ... reached, connection timed out`
    # cascades that hung whole runners. 10+20 ≈ 30 conns/instance keeps a
    # comfortable safety margin while staying well under typical Postgres
    # max_connections.
    pool_size = settings.DATABASE_POOL_SIZE or (10 if is_development else 10)
    max_overflow = settings.DATABASE_MAX_OVERFLOW or (20 if is_development else 20)
    return {
        "pool_pre_ping": True,
        "pool_size": max(pool_size, 1),
        "max_overflow": max(max_overflow, 0),
        "pool_timeout": max(int(settings.DATABASE_POOL_TIMEOUT_SECONDS), 1),
        "pool_recycle": max(int(settings.DATABASE_POOL_RECYCLE_SECONDS), 30),
        "pool_use_lifo": not is_development,
    }


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_ENV == "development",
    **_database_pool_config(),
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
