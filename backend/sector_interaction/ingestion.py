"""Runtime ingestion store for sector interaction source observations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import json
import uuid

from core.config import settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = BACKEND_ROOT / "runtime" / "sector_interaction"
_DURABLE_STATE_KEY = "sector_interaction_ingestion_v1"


@dataclass(frozen=True)
class SectorObservation:
    date: str
    country: str
    indicator_code: str
    sector: str
    value: float
    quality_score: float
    source: str
    source_status: str
    collector_version: str
    run_id: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectorRun:
    run_id: str
    country: str
    mode: str
    started_at: str
    finished_at: str
    status: str
    attempted_connectors: int
    stored_observations: int
    blocked_connectors: list[dict[str, Any]]
    errors: list[str]
    collector_version: str = "sector-ingestion-v1"


class SectorIngestionStore:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root

    def append_observations(self, observations: list[SectorObservation]) -> int:
        if not observations:
            return 0
        path = self._observations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for observation in observations:
                handle.write(json.dumps(asdict(observation), sort_keys=True) + "\n")
        self._save_durable_snapshot()
        return len(observations)

    def append_run(self, run: CollectorRun) -> None:
        path = self._runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(run), sort_keys=True) + "\n")
        self._save_durable_snapshot()

    def load_observations(self, country: str | None = None, limit: int = 5_000) -> list[dict[str, Any]]:
        rows = self._combined_observation_rows()
        if country:
            country_code = country.upper()
            rows = [row for row in rows if str(row.get("country", "")).upper() == country_code]
        rows.sort(key=lambda row: (str(row.get("date", "")), str(row.get("created_at", ""))), reverse=True)
        return rows[: max(1, min(int(limit), 20_000))]

    def load_runs(self, country: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._combined_run_rows()
        if country:
            country_code = country.upper()
            rows = [row for row in rows if str(row.get("country", "")).upper() == country_code]
        rows.sort(key=lambda row: str(row.get("started_at", "")), reverse=True)
        return rows[: max(1, min(int(limit), 500))]

    def summary(self, country: str) -> dict[str, Any]:
        observations = self.load_observations(country=country, limit=20_000)
        runs = self.load_runs(country=country, limit=20)
        indicators = {str(row.get("indicator_code")) for row in observations}
        sectors = {str(row.get("sector")) for row in observations}
        latest_date = max((str(row.get("date")) for row in observations), default=None)
        latest_created = max((str(row.get("created_at")) for row in observations), default=None)
        return {
            "observation_count": len(observations),
            "indicator_count": len(indicators),
            "sector_count": len(sectors),
            "latest_observation_date": latest_date,
            "latest_created_at": latest_created,
            "run_count": len(runs),
            "last_run": runs[0] if runs else None,
        }

    def storage_status(self) -> dict[str, Any]:
        durable_payload, durable_updated_at = self._load_durable_payload()
        local_observations = self._read_jsonl(self._observations_path())
        local_runs = self._read_jsonl(self._runs_path())
        durable_observations = list(durable_payload.get("observations") or []) if durable_payload else []
        durable_runs = list(durable_payload.get("runs") or []) if durable_payload else []
        return {
            "local_root": str(self.root),
            "local_observation_count": len(local_observations),
            "local_run_count": len(local_runs),
            "durable_enabled": self._durable_enabled(),
            "durable_state_key": _DURABLE_STATE_KEY,
            "durable_updated_at": durable_updated_at.isoformat() if durable_updated_at else None,
            "durable_observation_count": len(durable_observations),
            "durable_run_count": len(durable_runs),
            "effective_observation_count": len(self._combined_observation_rows()),
            "effective_run_count": len(self._combined_run_rows()),
            "backend": "postgres_runtime_state+jsonl" if self._durable_enabled() else "jsonl_local",
        }

    def build_run_id(self) -> str:
        return str(uuid.uuid4())

    def now_iso(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    def _observations_path(self) -> Path:
        return self.root / "observations.jsonl"

    def _runs_path(self) -> Path:
        return self.root / "collector_runs.jsonl"

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    def _durable_enabled(self) -> bool:
        return bool(settings.SECTOR_INTERACTION_DURABLE_STATE_ENABLED)

    def _load_durable_payload(self) -> tuple[dict[str, Any] | None, datetime | None]:
        if not self._durable_enabled():
            return None, None
        try:
            from core.runtime_state import load_runtime_state
        except ModuleNotFoundError:
            return None, None
        payload, updated_at = load_runtime_state(_DURABLE_STATE_KEY)
        return payload if isinstance(payload, dict) else None, updated_at

    def _save_durable_snapshot(self) -> datetime | None:
        if not self._durable_enabled():
            return None
        try:
            from core.runtime_state import save_runtime_state
        except ModuleNotFoundError:
            return None
        payload = {
            "version": 1,
            "observations": self._combined_observation_rows(include_durable=True)[-20_000:],
            "runs": self._combined_run_rows(include_durable=True)[-500:],
            "mirrored_at": self.now_iso(),
        }
        return save_runtime_state(_DURABLE_STATE_KEY, payload)

    def _combined_observation_rows(self, *, include_durable: bool = True) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self._observations_path())
        if include_durable:
            durable_payload, _ = self._load_durable_payload()
            rows.extend(list(durable_payload.get("observations") or []) if durable_payload else [])
        return self._dedupe_rows(
            rows,
            keys=("run_id", "date", "country", "indicator_code", "sector"),
        )

    def _combined_run_rows(self, *, include_durable: bool = True) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self._runs_path())
        if include_durable:
            durable_payload, _ = self._load_durable_payload()
            rows.extend(list(durable_payload.get("runs") or []) if durable_payload else [])
        return self._dedupe_rows(rows, keys=("run_id",))

    def _dedupe_rows(self, rows: list[dict[str, Any]], *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        deduped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(str(row.get(item, "")) for item in keys)
            deduped[key] = row
        return list(deduped.values())


sector_ingestion_store = SectorIngestionStore()
