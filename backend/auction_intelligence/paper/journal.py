from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def resolve_journal_root(root: Path | str) -> Path:
    path = Path(root)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


class JournalWriter:
    def __init__(self, root: Path | str):
        self.root = resolve_journal_root(root)

    def append(self, stream: str, payload: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{stream}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
        return path


class JournalReader:
    def __init__(self, root: Path | str):
        self.root = resolve_journal_root(root)

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self.root.exists():
            return []

        paths = sorted(self.root.glob("*.jsonl"))
        records: list[dict[str, Any]] = []
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
        return records
