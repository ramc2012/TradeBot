"""Paper journal and position book for directional live snapshots."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_symbol(value: str | None) -> str:
    return str(value or "").upper().strip()


def _same_contract(position: dict[str, Any], contract: dict[str, Any]) -> bool:
    position_key = str(position.get("instrument_key") or "").strip()
    contract_key = str(contract.get("instrument_key") or "").strip()
    if position_key and contract_key:
        return position_key == contract_key
    position_symbol = str(position.get("trading_symbol") or "").strip()
    contract_symbol = str(contract.get("trading_symbol") or "").strip()
    if position_symbol and contract_symbol:
        return position_symbol == contract_symbol
    return (
        str(position.get("option_type") or "") == str(contract.get("option_type") or "")
        and str(position.get("expiry") or "") == str(contract.get("expiry") or "")
        and float(position.get("strike") or 0.0) == float(contract.get("strike") or 0.0)
    )


class DirectionalOptionsPaperStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()

    async def list_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        records = self._load_journal()
        normalized = _normalize_symbol(symbol)
        if normalized:
            records = [row for row in records if _normalize_symbol(row.get("underlying")) == normalized]
        records.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
        return {
            "symbol_filter": normalized or None,
            "count": len(records),
            "records": records[:limit],
        }

    async def list_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        state = self._load_positions()
        normalized = _normalize_symbol(symbol)
        open_positions = list(state.get("open_positions", []))
        closed_positions = list(state.get("closed_positions", []))
        if normalized:
            open_positions = [row for row in open_positions if _normalize_symbol(row.get("underlying")) == normalized]
            closed_positions = [row for row in closed_positions if _normalize_symbol(row.get("underlying")) == normalized]
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

    async def sync_snapshot(
        self,
        snapshot_payload: dict[str, Any],
        *,
        position_marks: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(snapshot_payload.get("snapshot") or {})
        selection = dict(snapshot_payload.get("selection") or {})
        underlying = _normalize_symbol(selection.get("underlying") or snapshot.get("underlying"))
        signal = dict(snapshot.get("signal") or {})
        contract = dict(snapshot.get("selected_contract") or {})
        risk = dict(snapshot.get("risk") or {})
        data_status = dict(snapshot.get("data_status") or {})
        rag_context = dict(snapshot.get("rag_context") or {})
        recorded_at = str(snapshot.get("as_of") or _utc_now())
        execution_ready = bool(data_status.get("execution_ready"))
        actionable = bool(signal and contract and risk.get("approved") and execution_ready)
        latest_spot = float(snapshot.get("spot_price") or 0.0)
        latest_mark = float(contract.get("option_price") or 0.0) if contract else 0.0

        journal_entry = {
            "recorded_at": recorded_at,
            "underlying": underlying,
            "timeframe": selection.get("timeframe"),
            "regime": (snapshot.get("regime") or {}).get("label"),
            "direction": signal.get("direction"),
            "confidence": signal.get("confidence"),
            "expected_move": signal.get("expected_move"),
            "expected_horizon_bars": signal.get("expected_horizon_bars"),
            "selection_reason": snapshot.get("selection_reason"),
            "approved": bool(risk.get("approved")),
            "execution_ready": execution_ready,
            "trading_symbol": contract.get("trading_symbol"),
            "instrument_key": contract.get("instrument_key"),
            "option_type": contract.get("option_type"),
            "expiry": contract.get("expiry"),
            "strike": contract.get("strike"),
            "latest_premium": latest_mark or None,
            "latest_spot": latest_spot,
            "expected_pnl": contract.get("expected_pnl"),
            "rag_context": rag_context,
            "data_status": data_status,
        }
        self._append_journal(journal_entry)

        async with self._lock:
            state = self._load_positions()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))
            matching = [row for row in open_positions if _normalize_symbol(row.get("underlying")) == underlying]
            marks = position_marks or {}

            for row in matching:
                mark = marks.get(str(row.get("position_id") or "")) or {}
                if mark:
                    latest_value = float(mark.get("premium") or row.get("latest_premium") or row.get("entry_premium") or 0.0)
                    latest_spot_value = float(mark.get("spot") or row.get("latest_spot") or row.get("entry_spot") or 0.0)
                    row["updated_at"] = recorded_at
                    row["latest_premium"] = latest_value
                    row["latest_spot"] = latest_spot_value
                    row["price_source"] = mark.get("price_source") or row.get("price_source")
                    row["mark_time"] = mark.get("mark_time") or row.get("mark_time")
                    entry_premium = float(row.get("entry_premium") or latest_value or 0.0)
                    quantity = int(row.get("quantity_units") or 0)
                    row["unrealized_pnl"] = round((latest_value - entry_premium) * quantity, 2)

            if not execution_ready:
                self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return self._summary(open_positions, closed_positions)

            if not actionable:
                for row in list(matching):
                    self._close_position(
                        row,
                        mark=marks.get(str(row.get("position_id") or "")) or {},
                        close_time=recorded_at,
                        close_reason="flat_signal",
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

            refreshed = False
            for row in list(matching):
                if _same_contract(row, contract) and str(row.get("direction") or "") == str(signal.get("direction") or ""):
                    row["updated_at"] = recorded_at
                    row["latest_premium"] = latest_mark
                    row["latest_spot"] = latest_spot
                    row["confidence"] = float(signal.get("confidence") or row.get("confidence") or 0.0)
                    row["expected_move"] = float(signal.get("expected_move") or row.get("expected_move") or 0.0)
                    row["regime"] = (snapshot.get("regime") or {}).get("label") or row.get("regime")
                    row["selection_reason"] = snapshot.get("selection_reason") or row.get("selection_reason")
                    entry_premium = float(row.get("entry_premium") or latest_mark or 0.0)
                    quantity = int(row.get("quantity_units") or 0)
                    row["unrealized_pnl"] = round((latest_mark - entry_premium) * quantity, 2)
                    refreshed = True
                    continue
                self._close_position(
                    row,
                    mark=marks.get(str(row.get("position_id") or "")) or {},
                    close_time=recorded_at,
                    close_reason="signal_flip",
                )
                open_positions.remove(row)
                closed_positions.append(row)

            if not refreshed:
                open_positions.append(
                    {
                        "position_id": uuid4().hex,
                        "status": "open",
                        "opened_at": recorded_at,
                        "updated_at": recorded_at,
                        "closed_at": None,
                        "underlying": underlying,
                        "timeframe": selection.get("timeframe"),
                        "direction": signal.get("direction"),
                        "regime": (snapshot.get("regime") or {}).get("label"),
                        "confidence": float(signal.get("confidence") or 0.0),
                        "expected_move": float(signal.get("expected_move") or 0.0),
                        "trading_symbol": contract.get("trading_symbol"),
                        "instrument_key": contract.get("instrument_key"),
                        "option_type": contract.get("option_type"),
                        "expiry": contract.get("expiry"),
                        "expiry_kind": contract.get("expiry_kind"),
                        "strike": float(contract.get("strike") or 0.0),
                        "quantity_lots": int(risk.get("quantity_lots") or 0),
                        "quantity_units": int(risk.get("quantity_units") or 0),
                        "entry_premium": latest_mark,
                        "latest_premium": latest_mark,
                        "exit_premium": None,
                        "entry_spot": latest_spot,
                        "latest_spot": latest_spot,
                        "exit_spot": None,
                        "unrealized_pnl": 0.0,
                        "realized_pnl": 0.0,
                        "expected_pnl": float(contract.get("expected_pnl") or 0.0),
                        "selection_reason": snapshot.get("selection_reason"),
                        "rag_context": rag_context,
                        "price_source": contract.get("price_source") or "local_watchlist",
                        "mark_time": recorded_at,
                    }
                )

            self._save_positions(
                {
                    "last_synced_at": recorded_at,
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                }
            )
            return self._summary(open_positions, closed_positions)

    def _close_position(
        self,
        position: dict[str, Any],
        *,
        mark: dict[str, Any],
        close_time: str,
        close_reason: str,
    ) -> None:
        latest_premium = float(
            mark.get("premium")
            or position.get("latest_premium")
            or position.get("entry_premium")
            or 0.0
        )
        latest_spot = float(
            mark.get("spot")
            or position.get("latest_spot")
            or position.get("entry_spot")
            or 0.0
        )
        quantity = int(position.get("quantity_units") or 0)
        entry_premium = float(position.get("entry_premium") or latest_premium or 0.0)
        position["status"] = "closed"
        position["updated_at"] = close_time
        position["closed_at"] = close_time
        position["close_reason"] = close_reason
        position["exit_premium"] = latest_premium
        position["latest_premium"] = latest_premium
        position["exit_spot"] = latest_spot
        position["latest_spot"] = latest_spot
        position["price_source"] = mark.get("price_source") or position.get("price_source")
        position["mark_time"] = mark.get("mark_time") or position.get("mark_time")
        position["unrealized_pnl"] = 0.0
        position["realized_pnl"] = round((latest_premium - entry_premium) * quantity, 2)

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
