from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import desc, func, select

from auction_intelligence.validation.schemas import ValidationReport
from db.database import AsyncSessionLocal
from db.models import ValidationArtifact, ValidationMetric, ValidationRun


class ValidationPersistenceService:
    async def record_report(
        self,
        report: ValidationReport,
        *,
        gate: str,
        symbol: str | None = None,
        mode: str | None = None,
        scenario: str | None = None,
        source: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = jsonable_encoder(asdict(report))
        try:
            async with AsyncSessionLocal() as session:
                run = ValidationRun(
                    gate=gate,
                    label=report.label,
                    passed=report.passed,
                    score=report.score,
                    symbol=symbol,
                    mode=mode,
                    scenario=scenario,
                    source=source,
                    context=context or {},
                    report=payload,
                )
                session.add(run)
                await session.flush()
                for metric_name, metric_value in report.metrics.items():
                    session.add(
                        ValidationMetric(
                            run_id=run.id,
                            metric_name=metric_name,
                            metric_value=jsonable_encoder(metric_value),
                        )
                    )
                for artifact in report.artifacts:
                    session.add(
                        ValidationArtifact(
                            run_id=run.id,
                            artifact_type=artifact.artifact_type,
                            artifact_key=artifact.artifact_key,
                            payload=jsonable_encoder(artifact.payload),
                        )
                    )
                await session.commit()
                return {
                    "persisted": True,
                    "run_id": str(run.id),
                    "artifact_count": len(report.artifacts),
                }
        except Exception as exc:
            return {"persisted": False, "error": str(exc)}

    async def latest_report(
        self,
        *,
        gate: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(ValidationRun).order_by(desc(ValidationRun.created_at))
                if gate:
                    stmt = stmt.where(ValidationRun.gate == gate)
                if symbol:
                    stmt = stmt.where(ValidationRun.symbol == symbol)
                result = await session.execute(stmt.limit(1))
                run = result.scalar_one_or_none()
                if run is None:
                    return None
                counts_result = await session.execute(
                    select(
                        ValidationArtifact.artifact_type,
                        func.count(ValidationArtifact.id),
                    )
                    .where(ValidationArtifact.run_id == run.id)
                    .group_by(ValidationArtifact.artifact_type)
                )
                artifact_counts = {
                    artifact_type: count
                    for artifact_type, count in counts_result.all()
                }
                return {
                    "run_id": str(run.id),
                    "gate": run.gate,
                    "label": run.label,
                    "passed": run.passed,
                    "score": run.score,
                    "symbol": run.symbol,
                    "mode": run.mode,
                    "scenario": run.scenario,
                    "source": run.source,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "context": run.context or {},
                    "artifact_counts": artifact_counts,
                    "report": run.report or {},
                }
        except Exception:
            return None

    async def list_artifacts(
        self,
        run_id: str,
        *,
        artifact_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(ValidationArtifact)
                    .where(ValidationArtifact.run_id == run_id)
                    .order_by(ValidationArtifact.created_at.asc(), ValidationArtifact.artifact_key.asc())
                    .limit(limit)
                )
                if artifact_type:
                    stmt = stmt.where(ValidationArtifact.artifact_type == artifact_type)
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "artifact_id": str(row.id),
                        "run_id": str(row.run_id),
                        "artifact_type": row.artifact_type,
                        "artifact_key": row.artifact_key,
                        "payload": row.payload or {},
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ]
        except Exception:
            return []
