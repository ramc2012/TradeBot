from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fractal_market_profile.config import PAPER_ROOT
from fractal_market_profile.schemas import FMPPaperPositionRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FMPPaperStore:
    def __init__(self, root: Path | str = PAPER_ROOT):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()

    async def list_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        records = self._load_journal()
        normalized = str(symbol or "").upper().strip()
        if normalized:
            records = [row for row in records if str(row.get("underlying") or "").upper() == normalized]
        records.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
        return {
            "symbol_filter": normalized or None,
            "count": len(records),
            "records": records[:limit],
        }

    async def list_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        state = self._load_positions()
        normalized = str(symbol or "").upper().strip()
        open_positions = list(state.get("open_positions", []))
        closed_positions = list(state.get("closed_positions", []))
        if normalized:
            open_positions = [row for row in open_positions if str(row.get("underlying") or "").upper() == normalized]
            closed_positions = [row for row in closed_positions if str(row.get("underlying") or "").upper() == normalized]
        open_positions.sort(key=lambda row: str(row.get("opened_at") or ""), reverse=True)
        closed_positions.sort(key=lambda row: str(row.get("closed_at") or row.get("updated_at") or ""), reverse=True)
        if status == "open":
            closed_positions = []
        elif status == "closed":
            open_positions = []
        return {
            "symbol_filter": normalized or None,
            "status": status,
            "summary": self._summary(open_positions, closed_positions),
            "open_positions": open_positions[:limit],
            "closed_positions": closed_positions[:limit],
        }

    async def record_signal(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        signal = snapshot.get("current_signal") or {}
        recorded_at = _utc_now()
        entry = {
            "recorded_at": recorded_at,
            "underlying": snapshot.get("symbol_code"),
            "session_date": snapshot.get("session", {}).get("session_date"),
            "hourly_number": signal.get("hourly_number"),
            "setup_name": signal.get("setup_name"),
            "action": signal.get("action"),
            "confidence": signal.get("confidence"),
            "horizon": signal.get("horizon"),
            "daily_shape": signal.get("daily_shape"),
            "hourly_shape": signal.get("hourly_shape"),
            "entry_trigger": signal.get("entry_trigger"),
            "stop_level": signal.get("stop_level"),
            "target_level": signal.get("target_level"),
            "filters": signal.get("filters") or [],
            "rationale": signal.get("rationale") or [],
            "options": signal.get("options"),
            "order_flow_bias": signal.get("order_flow_bias"),
            "actionable": bool(signal.get("actionable")),
        }
        self._append_journal(entry)

        async with self._lock:
            state = self._load_positions()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))
            underlying = str(snapshot.get("symbol_code") or "").upper().strip()
            matching = [row for row in open_positions if str(row.get("underlying") or "").upper() == underlying]

            if not signal.get("actionable") or not signal.get("options"):
                for row in matching:
                    row["status"] = "closed"
                    row["updated_at"] = recorded_at
                    row["closed_at"] = recorded_at
                    row["close_reason"] = "flat_snapshot"
                    row["exit_premium"] = row.get("latest_premium")
                    row["realized_pnl"] = round(
                        (float(row.get("exit_premium") or 0.0) - float(row.get("entry_premium") or 0.0))
                        * int(row.get("quantity") or 0),
                        2,
                    )
                    open_positions.remove(row)
                    closed_positions.append(row)
                self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return self._summary(open_positions, closed_positions)

            options = signal["options"]
            lot_size = int(options.get("lot_size") or 1)
            quantity = lot_size
            latest_premium = float(options.get("premium") or 0.0)
            new_position = asdict(
                FMPPaperPositionRecord(
                    position_id=uuid4().hex,
                    status="open",
                    opened_at=recorded_at,
                    updated_at=recorded_at,
                    closed_at=None,
                    underlying=underlying,
                    setup_name=str(signal.get("setup_name") or ""),
                    action=str(signal.get("action") or ""),
                    horizon=str(signal.get("horizon") or "swing"),
                    trading_symbol=options.get("trading_symbol"),
                    instrument_key=options.get("instrument_key"),
                    option_type=options.get("option_type"),
                    strike=float(options.get("strike") or 0.0),
                    expiry=str(options.get("expiry") or ""),
                    quantity=quantity,
                    lot_size=lot_size,
                    entry_premium=latest_premium,
                    latest_premium=latest_premium,
                    exit_premium=None,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    stop_level=float(signal.get("stop_level") or 0.0),
                    target_level=float(signal.get("target_level") or 0.0),
                    confidence=float(signal.get("confidence") or 0.0),
                    daily_shape=str(signal.get("daily_shape") or ""),
                    hourly_shape=str(signal.get("hourly_shape") or ""),
                )
            )

            refreshed = False
            for row in list(matching):
                same_contract = (
                    str(row.get("instrument_key") or "") == str(options.get("instrument_key") or "")
                    or (
                        str(row.get("option_type") or "") == str(options.get("option_type") or "")
                        and float(row.get("strike") or 0.0) == float(options.get("strike") or 0.0)
                        and str(row.get("expiry") or "") == str(options.get("expiry") or "")
                    )
                )
                same_action = str(row.get("action") or "") == str(signal.get("action") or "")
                if same_contract and same_action:
                    row["updated_at"] = recorded_at
                    row["latest_premium"] = latest_premium
                    row["unrealized_pnl"] = round(
                        (latest_premium - float(row.get("entry_premium") or 0.0)) * int(row.get("quantity") or 0),
                        2,
                    )
                    row["confidence"] = float(signal.get("confidence") or row.get("confidence") or 0.0)
                    row["stop_level"] = float(signal.get("stop_level") or row.get("stop_level") or 0.0)
                    row["target_level"] = float(signal.get("target_level") or row.get("target_level") or 0.0)
                    row["daily_shape"] = str(signal.get("daily_shape") or row.get("daily_shape") or "")
                    row["hourly_shape"] = str(signal.get("hourly_shape") or row.get("hourly_shape") or "")
                    refreshed = True
                    continue

                row["status"] = "closed"
                row["updated_at"] = recorded_at
                row["closed_at"] = recorded_at
                row["close_reason"] = "signal_flip"
                row["exit_premium"] = latest_premium
                row["realized_pnl"] = round(
                    (latest_premium - float(row.get("entry_premium") or 0.0)) * int(row.get("quantity") or 0),
                    2,
                )
                open_positions.remove(row)
                closed_positions.append(row)

            if not refreshed:
                open_positions.append(new_position)

            self._save_positions(
                {
                    "last_synced_at": recorded_at,
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                }
            )
            return self._summary(open_positions, closed_positions)

    def _summary(self, open_positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
        realized = round(sum(float(row.get("realized_pnl") or 0.0) for row in closed_positions), 2)
        unrealized = round(sum(float(row.get("unrealized_pnl") or 0.0) for row in open_positions), 2)
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 2),
        }

    def _append_journal(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    def _load_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _load_positions(self) -> dict[str, Any]:
        if not self.positions_path.exists():
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}
        try:
            return json.loads(self.positions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}

    def _save_positions(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.positions_path.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
