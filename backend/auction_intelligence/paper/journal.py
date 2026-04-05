from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JournalWriter:
    def __init__(self, root: Path):
        self.root = root

    def append(self, stream: str, payload: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{stream}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
        return path
