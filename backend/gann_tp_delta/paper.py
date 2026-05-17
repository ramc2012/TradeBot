"""Paper-only journal for Gann TP Delta proposals."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class GannTPDeltaPaperStore:
    def __init__(self, journal_root: Path):
        self.journal_root = Path(journal_root)
        self.journal_path = self.journal_root / "paper_journal.jsonl"

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.journal_root.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return record

    def list(self, *, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        if self.journal_path.exists():
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if symbol and str(row.get("underlying") or "").upper() != symbol.upper():
                    continue
                records.append(row)
        records = records[-int(limit) :]
        return {
            "records": list(reversed(records)),
            "summary": {
                "count": len(records),
                "latest": records[-1].get("recorded_at") if records else None,
            },
        }
