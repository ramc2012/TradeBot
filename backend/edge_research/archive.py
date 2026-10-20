"""Append-only inputs and atomic shadow state in a separate SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Archive:
    def __init__(self, path: Path, config: dict, started_at: str):
        if path.name != "edge.sqlite3":
            raise ValueError(
                "Shadow archive must be a separate file named edge.sqlite3"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if tables - {"manifest", "events", "decisions", "states", "trades"}:
            self.db.close()
            raise ValueError(
                "Refusing to modify a database belonging to another application"
            )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
          CREATE TABLE IF NOT EXISTS manifest(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, available_at TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS states(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """
        )
        source = Path(__file__).parent
        paths = sorted(source.glob("*.py")) + [source.parent / "paper_engine/costs.py"]
        paths.append(source.parent / "core/trading_calendar.py")
        digest = hashlib.sha256()
        for module in paths:
            digest.update(module.name.encode())
            digest.update(module.read_bytes())
        from paper_engine import costs
        from core.trading_calendar import trading_calendar

        if not costs.PAPER_APPLY_COSTS:
            self.db.close()
            raise ValueError("Shadow evidence requires PAPER_APPLY_COSTS=true")
        rates = {
            k: v
            for k, v in vars(costs).items()
            if k.isupper() and isinstance(v, (bool, int, float))
        }
        identity = {
            "config": config,
            "source_sha256": digest.hexdigest(),
            "cost_model": rates,
            "schema_version": 1,
            "calendar": trading_calendar.serialize(),
        }
        row = self.db.execute("SELECT payload FROM manifest WHERE id=1").fetchone()
        if row:
            self.manifest = json.loads(row[0])
            if {k: self.manifest[k] for k in identity} != identity:
                self.db.close()
                raise ValueError(
                    "Frozen run differs from source/config; use a NEW archive, never overwrite this run"
                )
        else:
            self.manifest = {
                **identity,
                "started_at": started_at,
                "mode": "shadow_only",
                "promotion": "unproven",
            }
            self.db.execute(
                "INSERT INTO manifest VALUES(1,?)", (encode(self.manifest),)
            )
            self.db.commit()

    def event(self, kind: str, available_at: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO events(kind,available_at,payload) VALUES(?,?,?)",
            (kind, available_at, encode(payload)),
        )

    def state(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload FROM states WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key: str, state: dict | None) -> None:
        if state is None:
            self.db.execute("DELETE FROM states WHERE key=?", (key,))
        else:
            self.db.execute(
                "INSERT INTO states VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, encode(state)),
            )

    def close(self) -> None:
        self.db.close()
