"""Paper journal and position book for directional live snapshots."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from analysis.instruments import normalize_index_contract_expiry
from core.paper_trade_recorder import paper_trade_recorder
from directional_options.config import DIRECTIONAL_INITIAL_CAPITAL


_TIMEFRAME_MINUTES = {
    "1minute": 1, "1m": 1,
    "3minute": 3, "3m": 3,
    "5minute": 5, "5m": 5,
    "15minute": 15, "15m": 15,
    "30minute": 30, "30m": 30,
    "60minute": 60, "1hour": 60, "1h": 60,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _has_satisfied_min_hold(
    position: dict[str, Any],
    *,
    min_hold_bars: int,
    timeframe: str | None,
) -> bool:
    if min_hold_bars <= 0:
        return True
    opened = _parse_iso(position.get("opened_at"))
    if opened is None:
        return True
    minutes_per_bar = _TIMEFRAME_MINUTES.get(str(timeframe or "").lower(), 5)
    elapsed = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
    return elapsed >= float(min_hold_bars * minutes_per_bar)


def _normalize_symbol(value: str | None) -> str:
    return str(value or "").upper().strip()


def _with_current_expiry_calendar(position: dict[str, Any]) -> dict[str, Any]:
    row = dict(position)
    raw_expiry = row.get("expiry")
    normalized_expiry = normalize_index_contract_expiry(row.get("underlying"), raw_expiry)
    if normalized_expiry is None or normalized_expiry.isoformat() == str(raw_expiry or "")[:10]:
        return row

    row["raw_expiry"] = raw_expiry
    row["expiry"] = normalized_expiry.isoformat()
    row["expiry_kind"] = "monthly"
    row["expiry_correction_reason"] = "current_index_monthly_expiry_calendar"
    return row


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
    # How long the "actionable=False" signal must persist before a flat_signal
    # close fires. The directional strategy re-evaluates every ~60 s; without
    # this, a single noisy cycle (brief spread widening / data gap / regime
    # uncertainty) churns the position. Five minutes of confirmation prevents
    # ~90 % churn observed on 2026-05-20 (71 opens / 64 closes in a session).
    FLAT_CONFIRMATION_SECONDS: float = 300.0

    def __init__(self, root: Path | str, *, min_hold_bars: int = 3):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()
        self.min_hold_bars = int(min_hold_bars)

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
        open_positions = [_with_current_expiry_calendar(row) for row in state.get("open_positions", [])]
        closed_positions = [_with_current_expiry_calendar(row) for row in state.get("closed_positions", [])]
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
                # Two-stage close: require persistent flat signal before
                # exiting. The strategy re-evaluates every ~60 s and any
                # one of (signal/contract/risk/exec_ready) flipping to
                # False sets actionable=False — but a single noisy cycle
                # shouldn't churn a held position. Track when each row
                # last saw an actionable=True cycle; only fire flat_signal
                # close after FLAT_CONFIRMATION_SECONDS of sustained flat.
                now_dt = datetime.now(timezone.utc)
                position_timeframe = str(selection.get("timeframe") or "")
                for row in list(matching):
                    if not _has_satisfied_min_hold(
                        row,
                        min_hold_bars=self.min_hold_bars,
                        timeframe=row.get("timeframe") or position_timeframe,
                    ):
                        # Held too briefly — refuse to flatten on a single
                        # noisy bar. Keep the position open; it will close
                        # naturally on stop / target / a later flat-signal.
                        continue
                    last_actionable_iso = row.get("last_actionable_at") or row.get("opened_at")
                    last_actionable_dt = _parse_iso(last_actionable_iso)
                    if last_actionable_dt is not None:
                        flat_for = (now_dt - last_actionable_dt).total_seconds()
                        if flat_for < self.FLAT_CONFIRMATION_SECONDS:
                            # Position recently green-lit; brief flat is
                            # noise. Don't churn the position.
                            continue
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
            position_timeframe = str(selection.get("timeframe") or "")
            for row in list(matching):
                if _same_contract(row, contract) and str(row.get("direction") or "") == str(signal.get("direction") or ""):
                    row["updated_at"] = recorded_at
                    # Mark this cycle as green-lit so the flat-signal
                    # confirmation timer above resets every time we get a
                    # fresh actionable=True for the same contract.
                    row["last_actionable_at"] = recorded_at
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
                if not _has_satisfied_min_hold(
                    row,
                    min_hold_bars=self.min_hold_bars,
                    timeframe=row.get("timeframe") or position_timeframe,
                ):
                    # Held too briefly — keep position open through a single
                    # noisy regime flip. Will reassess on the next bar.
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
                new_position_id = uuid4().hex
                try:
                    await paper_trade_recorder.record_event(
                        strategy="directional_options",
                        event="open",
                        underlying=underlying,
                        instrument_key=contract.get("instrument_key"),
                        option_type=contract.get("option_type"),
                        strike=float(contract.get("strike") or 0.0),
                        expiry=str(contract.get("expiry") or ""),
                        quantity=int(risk.get("quantity_units") or 0),
                        entry_premium=latest_mark,
                        latest_premium=latest_mark,
                        position_id=new_position_id,
                        reason=str(snapshot.get("selection_reason") or ""),
                        extra={"direction": signal.get("direction"), "timeframe": selection.get("timeframe")},
                    )
                except Exception:
                    pass
                open_positions.append(
                    {
                        "position_id": new_position_id,
                        "status": "open",
                        "opened_at": recorded_at,
                        "updated_at": recorded_at,
                        # Anchor for the flat-signal confirmation timer
                        # (FLAT_CONFIRMATION_SECONDS). Initialized to open
                        # time so the position has a full grace window
                        # before any single noisy cycle can close it.
                        "last_actionable_at": recorded_at,
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
        realized = round((latest_premium - entry_premium) * quantity, 2)
        position["realized_pnl"] = realized
        try:
            asyncio.create_task(
                paper_trade_recorder.record_event(
                    strategy="directional_options",
                    event="close",
                    underlying=position.get("underlying"),
                    instrument_key=position.get("instrument_key"),
                    option_type=position.get("option_type"),
                    strike=position.get("strike"),
                    expiry=position.get("expiry"),
                    quantity=quantity,
                    entry_premium=entry_premium,
                    exit_premium=latest_premium,
                    realized=realized,
                    position_id=position.get("position_id"),
                    reason=close_reason,
                )
            )
        except RuntimeError:
            # No running event loop (e.g. unit tests) — skip event logging.
            pass

    def _summary(self, open_positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
        realized = round(sum(float(row.get("realized_pnl") or 0.0) for row in closed_positions), 2)
        unrealized = round(sum(float(row.get("unrealized_pnl") or 0.0) for row in open_positions), 2)

        # Capital accounting matches the AI/FMP/S1/S2 canonical shape so the
        # frontend portfolio panel can render uniformly across all lanes.
        # Premium × quantity is the cash locked against each open long-option.
        initial_capital = DIRECTIONAL_INITIAL_CAPITAL
        reserved_margin = round(
            sum(
                float(p.get("entry_premium") or 0.0) * float(p.get("quantity_units") or 0)
                for p in open_positions
            ),
            2,
        )
        total_equity = round(initial_capital + realized + unrealized, 2)
        available_capital = round(initial_capital + realized - reserved_margin, 2)
        total_return_pct = round(
            ((total_equity - initial_capital) / initial_capital) * 100.0, 4
        ) if initial_capital else 0.0

        closed_sorted = sorted(
            closed_positions,
            key=lambda r: str(r.get("closed_at") or r.get("updated_at") or ""),
        )
        running_equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        trade_returns_pct: list[float] = []
        wins = 0
        losses = 0
        for row in closed_sorted:
            pnl = float(row.get("realized_pnl") or 0.0)
            pre_equity = running_equity if running_equity > 0 else initial_capital
            running_equity = max(0.0, running_equity + pnl)
            if running_equity > peak:
                peak = running_equity
            if peak > 0:
                dd = (peak - running_equity) / peak
                max_dd = max(max_dd, dd)
            if pre_equity > 0:
                trade_returns_pct.append((pnl / pre_equity) * 100.0)
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        sharpe = 0.0
        if len(trade_returns_pct) >= 2:
            mean = sum(trade_returns_pct) / len(trade_returns_pct)
            var = sum((r - mean) ** 2 for r in trade_returns_pct) / max(len(trade_returns_pct) - 1, 1)
            stdev = var ** 0.5
            if stdev > 0:
                sharpe = round(mean / stdev, 4)

        win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0

        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 2),
            "initial_capital": initial_capital,
            "available_capital": available_capital,
            "reserved_margin": reserved_margin,
            "total_equity": total_equity,
            "total_return_pct": total_return_pct,
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": sharpe,
            "total_trades": wins + losses,
            "win_rate": round(win_rate, 4),
        }

    async def reset_account(self, *, actor: str | None = None) -> dict[str, Any]:
        """Archive current state and wipe positions+journal back to the
        funded baseline. Mirrors `archive_and_reset_paper_account` on S1/S2.
        Idempotent — a second call on an already-empty book is a no-op."""
        async with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_dir = self.root / "archive" / stamp
            archive_dir.mkdir(parents=True, exist_ok=True)
            if self.positions_path.exists():
                self.positions_path.replace(archive_dir / "paper_positions.json")
            if self.journal_path.exists():
                self.journal_path.replace(archive_dir / "paper_journal.jsonl")
            self._save_positions(
                {
                    "open_positions": [],
                    "closed_positions": [],
                    "last_synced_at": _utc_now(),
                }
            )
        return {
            "reset": True,
            "actor": actor,
            "archived_to": str(archive_dir.relative_to(self.root.parent)) if archive_dir.exists() else None,
            "initial_capital": DIRECTIONAL_INITIAL_CAPITAL,
        }

    def capital_status(self) -> dict[str, Any]:
        state = self._load_positions()
        return self._summary(
            list(state.get("open_positions", [])),
            list(state.get("closed_positions", [])),
        )

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
