from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


def _database_pool_config() -> dict[str, int | bool]:
    is_development = settings.APP_ENV == "development"
    # Production was sized at pool_size=3 / max_overflow=2 (5 connections max
    # per Cloud Run instance). That was exhausted by the new agents (audit
    # writes, paper bootstrap, data-quality, AI/FMP/DO paper cycles, NSE +
    # commodity scans) running together — observed
    # "QueuePool limit of size 6 overflow 4 reached, connection timed out"
    # on /api/commodity/overview and /api/system/health under load.
    # Cloud SQL standard tiers tolerate well above 25 connections; budget
    # 16 per Cloud Run instance and rely on Cloud Run scaling to bound the
    # global footprint. DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW env vars
    # still override, so the cap can be tightened without a code change.
    pool_size = settings.DATABASE_POOL_SIZE or (8 if is_development else 8)
    max_overflow = settings.DATABASE_MAX_OVERFLOW or (8 if is_development else 8)
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
