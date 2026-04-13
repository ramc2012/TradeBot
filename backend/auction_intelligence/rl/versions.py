from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import AsyncSessionLocal


class RLPolicyVersionStore:
    async def create_version(
        self,
        *,
        version_name: str,
        status: str,
        source: str,
        symbol: str | None,
        trained_on: int,
        skipped: int,
        average_reward: float,
        metrics: dict[str, Any],
        qtable_snapshot: dict[str, Any],
        promotion_reason: str | None = None,
    ) -> dict[str, Any]:
        version_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO rl_policy_versions (
                        id, version_name, status, source, symbol,
                        trained_on, skipped, average_reward,
                        metrics, qtable_snapshot, promotion_reason
                    )
                    VALUES (
                        :id, :version_name, :status, :source, :symbol,
                        :trained_on, :skipped, :average_reward,
                        :metrics, :qtable_snapshot, :promotion_reason
                    )
                    """
                ),
                {
                    "id": version_id,
                    "version_name": version_name,
                    "status": status,
                    "source": source,
                    "symbol": symbol,
                    "trained_on": trained_on,
                    "skipped": skipped,
                    "average_reward": average_reward,
                    "metrics": metrics,
                    "qtable_snapshot": qtable_snapshot,
                    "promotion_reason": promotion_reason,
                },
            )
            await session.commit()
        return {
            "id": version_id,
            "version_name": version_name,
            "status": status,
            "source": source,
            "symbol": symbol,
            "trained_on": trained_on,
            "skipped": skipped,
            "average_reward": average_reward,
            "metrics": metrics,
            "promotion_reason": promotion_reason,
        }

    async def promote_version(self, version_id: str, *, promotion_reason: str | None = None) -> dict[str, Any] | None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE rl_policy_versions SET status = 'retired' WHERE status = 'active'")
            )
            result = await session.execute(
                text(
                    """
                    UPDATE rl_policy_versions
                    SET status = 'active',
                        promoted_at = now(),
                        promotion_reason = COALESCE(:promotion_reason, promotion_reason)
                    WHERE id = :id
                    RETURNING id::text AS id, version_name, status, source, symbol,
                              trained_on, skipped, average_reward, metrics,
                              promotion_reason, created_at, promoted_at
                    """
                ),
                {"id": version_id, "promotion_reason": promotion_reason},
            )
            await session.commit()
            row = result.mappings().first()
        return dict(row) if row else None

    async def list_versions(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        where_sql = "WHERE status = :status" if status else ""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT id::text AS id, version_name, status, source, symbol,
                           trained_on, skipped, average_reward, metrics,
                           promotion_reason, created_at, promoted_at
                    FROM rl_policy_versions
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return [dict(row) for row in result.mappings().all()]

    async def latest_version(self, *, status: str | None = None) -> dict[str, Any] | None:
        versions = await self.list_versions(limit=1, status=status)
        return versions[0] if versions else None

    async def has_run_for_session(self, *, session_date: date, sources: tuple[str, ...]) -> bool:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT source, created_at
                    FROM rl_policy_versions
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
            )
            rows = result.mappings().all()

        tz = ZoneInfo("Asia/Kolkata")
        allowed_sources = set(sources)
        for row in rows:
            created_at = row.get("created_at")
            if row.get("source") in allowed_sources and created_at is not None:
                created_local = created_at.astimezone(tz)
                if created_local.date() == session_date:
                    return True
        return False
