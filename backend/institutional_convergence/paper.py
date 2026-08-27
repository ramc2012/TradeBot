from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

from core.config import settings

from .engine import lots_for_risk
from .stats import compute_statistics, open_position_detail, trade_records


INITIAL_CAPITAL = 1_000_000.0
# Order-log retention: every open / partial_close / close action is one row,
# so a full day of a 12-name universe is well under 100 rows.
ORDER_LOG_LIMIT = 2000


def _nse_noon_quarantine(now: datetime) -> bool:
    return now.hour == 12 or (now.hour == 11 and now.minute >= 45) or (now.hour == 13 and now.minute <= 15)


class ConvergencePaperBook:
    def __init__(
        self,
        path: Path,
        *,
        squareoff: time = time(15, 25),
        entry_quarantine: Callable[[datetime], bool] | None = _nse_noon_quarantine,
    ):
        self.path = path
        # Intraday square-off boundary (exchange-local wall clock) and the
        # no-new-entries window. NSE defaults preserved; the MCX book passes
        # its evening-session equivalents.
        self.squareoff = squareoff
        self.entry_quarantine = entry_quarantine or (lambda _now: False)

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"initial_capital": INITIAL_CAPITAL, "open_positions": [], "closed_positions": []}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True))
        temp.replace(self.path)

    def sync(self, results: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        state = self._load()
        open_positions = list(state.get("open_positions") or [])
        closed_positions = list(state.get("closed_positions") or [])
        # Backward compatible: state files written before the order log simply
        # start with an empty one — no migration needed.
        order_log = list(state.get("order_log") or [])
        # Insertion-ordered so the retention trim evicts the OLDEST ids —
        # a sorted() trim would evict alphabetically-first symbols' ids
        # (including same-day ones) while retaining stale late-alphabet ids.
        consumed_setup_list = [str(item) for item in (state.get("consumed_setups") or [])]
        consumed_setups = set(consumed_setup_list)
        closed_this_cycle: set[str] = set()
        marks = {str(row.get("symbol")): row for row in results if row.get("spot")}
        today = now.date().isoformat()

        squareoff_due = now.time() >= self.squareoff
        for position in list(open_positions):
            row = marks.get(str(position.get("symbol")))
            if not row:
                # A symbol rotated out of the universe (daily CBE picks change)
                # stops producing marks — without this branch its position was
                # immortal: never marked, never squared off. Close it at the
                # last known mark at the intraday boundary.
                if squareoff_due:
                    last_price = float(position.get("current_price") or position.get("entry_price") or 0.0)
                    self._close(position, last_price, "intraday_squareoff_stale_mark", now, open_positions, closed_positions, order_log)
                    closed_this_cycle.add(str(position.get("symbol")))
                continue
            price = float(row["spot"])
            position["current_price"] = price
            position["updated_at"] = now.isoformat()
            direction = position["direction"]
            stop_hit = price <= position["stop"] if direction == "LONG" else price >= position["stop"]
            target1_hit = price >= position["target1"] if direction == "LONG" else price <= position["target1"]
            wall = position.get("target2")
            wall_hit = bool(wall) and (price >= wall if direction == "LONG" else price <= wall)
            cvd = ((row.get("cvd") or {}).get("series") or [])
            # One adverse CVD print is normal noise. Exit the runner only after
            # two consecutive adverse completed observations.
            cvd_reversal = len(cvd) >= 3 and (
                cvd[-1]["cvd"] < cvd[-2]["cvd"] < cvd[-3]["cvd"]
                if direction == "LONG"
                else cvd[-1]["cvd"] > cvd[-2]["cvd"] > cvd[-3]["cvd"]
            )
            if stop_hit:
                self._close(position, price, "hard_stop", now, open_positions, closed_positions, order_log)
                closed_this_cycle.add(str(position.get("symbol")))
            elif not position.get("target1_done") and target1_hit:
                exit_lots = max(1, int(position["lots"]) // 2)
                partial_pnl = round((price - position["entry_price"]) * position["lot_size"] * exit_lots * (1 if direction == "LONG" else -1), 2)
                position["realized_pnl"] = partial_pnl
                position["lots"] -= exit_lots
                position["target1_done"] = True
                position["stop"] = position["entry_price"]
                position["break_even_at"] = now.isoformat()
                order_log.append(self._order_record(
                    "partial_close", position, price, "target1_partial", now,
                    lots=exit_lots, pnl=partial_pnl, lots_remaining=int(position["lots"]),
                ))
                if position["lots"] <= 0:
                    self._close(position, price, "target1", now, open_positions, closed_positions, order_log)
                    closed_this_cycle.add(str(position.get("symbol")))
            elif position.get("target1_done") and (wall_hit or cvd_reversal):
                self._close(position, price, "oi_wall" if wall_hit else "cvd_reversal", now, open_positions, closed_positions, order_log)
                closed_this_cycle.add(str(position.get("symbol")))
            elif squareoff_due:
                self._close(position, price, "intraday_squareoff", now, open_positions, closed_positions, order_log)
                closed_this_cycle.add(str(position.get("symbol")))

        circuit = self._circuit_state(closed_positions, today, state)
        quarantined = self.entry_quarantine(now)
        open_symbols = {row["symbol"] for row in open_positions}
        # OWNER DIRECTIVE 2026-07-17 (signal validation, paper-only): the
        # 2-consecutive-loss / −3%-day circuit breaker still REPORTS its state
        # (persisted below as `circuit_breaker`) but does not LOCK entries
        # while validating signals — both the NSE and MCX book instances of
        # this class. Quarantine window, squareoff and all protective exits
        # (stop / target / wall / CVD-reversal) stay fully active.
        circuit_locks_entries = circuit["locked"] and not settings.SIGNAL_VALIDATION_UNCAPPED
        if not circuit_locks_entries and not quarantined and not squareoff_due:
            capital = self._equity(state, open_positions, closed_positions)
            for row in results:
                if row.get("status") != "actionable_paper" or row.get("action") not in {"LONG", "SHORT"} or row.get("symbol") in open_symbols or str(row.get("symbol")) in closed_this_cycle:
                    continue
                setup = row.get("long_setup") if row.get("action") == "LONG" else row.get("short_setup")
                setup_time = (setup or {}).get("bar_time")
                setup_id = f"{row.get('symbol')}:{row.get('action')}:{setup_time}" if setup_time else None
                if setup_id and setup_id in consumed_setups:
                    continue
                risk = row.get("risk") or {}
                entry, stop = float(risk.get("entry") or 0), float(risk.get("stop") or 0)
                lot_size = int(risk.get("lot_size") or 0)
                lots = lots_for_risk(
                    capital,
                    float(risk.get("risk_fraction") or 0.01),
                    abs(entry - stop),
                    lot_size,
                    # Size against VOLATILITY, not a raw structural stop. Both
                    # `atr_3m` and the VIX `size_multiplier` were already computed
                    # per instrument and thrown away here — sizing was purely
                    # 1/stop, which is how a noise-width stop bought 2308x
                    # leverage on DIVISLAB. Pass the SIGNED stop too: the
                    # `abs(entry - stop)` above hides a wrong-side stop.
                    entry_price=entry,
                    atr=float(risk.get("atr_3m") or 0.0),
                    size_multiplier=float((row.get("vix") or {}).get("size_multiplier") or 1.0),
                    stop_price=stop,
                    direction=row["action"],
                )
                if lots <= 0:
                    continue
                position = {
                    "position_id": f"IC-{row['symbol']}-{int(now.timestamp())}",
                    "symbol": row["symbol"], "direction": row["action"], "entry_price": entry,
                    "futures_contract": row.get("futures_contract"),
                    "current_price": entry, "stop": stop, "initial_stop": stop, "target1": risk.get("target1"),
                    "target2": risk.get("target2_long") if row["action"] == "LONG" else risk.get("target2_short"),
                    "lot_size": lot_size, "lots": lots, "initial_lots": lots, "target1_done": False,
                    "opened_at": now.isoformat(), "session_date": today, "realized_pnl": 0.0,
                    "risk_fraction": risk.get("risk_fraction"), "status": "open",
                    "setup_id": setup_id,
                }
                open_positions.append(position)
                order_log.append(self._order_record("open", position, entry, "signal_entry", now, lots=lots))
                open_symbols.add(row["symbol"])
                if setup_id:
                    consumed_setups.add(setup_id)
                    consumed_setup_list.append(setup_id)

        state.update({"open_positions": open_positions, "closed_positions": closed_positions[-500:], "order_log": order_log[-ORDER_LOG_LIMIT:], "consumed_setups": consumed_setup_list[-500:], "updated_at": now.isoformat(), "circuit_breaker": circuit})
        self._save(state)
        return self.summary(state)

    def _close(self, position, price, reason, now, open_positions, closed_positions, order_log):
        if position not in open_positions:
            return
        direction = 1 if position["direction"] == "LONG" else -1
        pnl = (price - position["entry_price"]) * position["lot_size"] * position["lots"] * direction
        order_log.append(self._order_record("close", position, price, reason, now, lots=int(position.get("lots") or 0), pnl=round(pnl, 2)))
        position.update({"status": "closed", "exit_price": price, "closed_at": now.isoformat(), "exit_reason": reason, "realized_pnl": round(float(position.get("realized_pnl") or 0) + pnl, 2)})
        open_positions.remove(position)
        closed_positions.append(position)

    @staticmethod
    def _order_record(action, position, price, reason, now, *, lots, pnl=None, lots_remaining=None):
        """One append-only row per paper transition (instant-fill order log)."""
        record = {
            "action": action,
            "time": now.isoformat(),
            "position_id": position.get("position_id"),
            "symbol": position.get("symbol"),
            "direction": position.get("direction"),
            "price": float(price),
            "lots": int(lots),
            "lot_size": int(position.get("lot_size") or 0),
            "reason": reason,
        }
        if pnl is not None:
            record["pnl"] = float(pnl)
        if lots_remaining is not None:
            record["lots_remaining"] = int(lots_remaining)
        return record

    @staticmethod
    def _circuit_state(closed, today, state):
        rows = [row for row in closed if str(row.get("session_date")) == today]
        pnl = sum(float(row.get("realized_pnl") or 0) for row in rows)
        consecutive = 0
        for row in reversed(rows):
            if float(row.get("realized_pnl") or 0) < 0:
                consecutive += 1
            else:
                break
        capital = float(state.get("initial_capital") or INITIAL_CAPITAL)
        return {"locked": consecutive >= 2 or pnl <= -(capital * 0.03), "consecutive_losses": consecutive, "day_pnl": round(pnl, 2), "loss_limit": -capital * 0.03}

    @staticmethod
    def _equity(state, open_positions, closed_positions):
        realized = sum(float(row.get("realized_pnl") or 0) for row in closed_positions)
        unrealized = sum((float(row.get("current_price") or 0) - float(row.get("entry_price") or 0)) * int(row.get("lot_size") or 0) * int(row.get("lots") or 0) * (1 if row.get("direction") == "LONG" else -1) for row in open_positions)
        return float(state.get("initial_capital") or INITIAL_CAPITAL) + realized + unrealized

    def summary(self, state=None):
        state = state or self._load()
        open_positions, closed_positions = list(state.get("open_positions") or []), list(state.get("closed_positions") or [])
        realized = sum(float(row.get("realized_pnl") or 0) for row in closed_positions)
        equity = self._equity(state, open_positions, closed_positions)
        # Derived read-time detail (stop/target distances, unrealized pnl,
        # R-so-far, age). Copies only — never persisted back into the state file.
        read_now = datetime.now(timezone.utc)
        detailed_open = [{**row, **open_position_detail(row, read_now)} for row in open_positions]
        return {"initial_capital": float(state.get("initial_capital") or INITIAL_CAPITAL), "equity": round(equity, 2), "realized_pnl": round(realized, 2), "open_count": len(open_positions), "closed_count": len(closed_positions), "open_positions": detailed_open, "closed_positions": closed_positions[-100:], "circuit_breaker": state.get("circuit_breaker") or {}}

    # ── Trade surfaces (read-only, computed from the persisted state) ──────

    def orders(self, limit: int = 500) -> dict[str, Any]:
        """The order LOG: paper fills are instant, so every open / partial /
        close action is one append-only record."""
        state = self._load()
        log = list(state.get("order_log") or [])
        limit = max(1, int(limit))
        return {"orders": log[-limit:], "count": len(log), "updated_at": state.get("updated_at")}

    def trades(self) -> dict[str, Any]:
        """Full closed-trade book as flat CSV-able rows."""
        state = self._load()
        records = trade_records(list(state.get("closed_positions") or []))
        return {"trades": records, "count": len(records), "updated_at": state.get("updated_at")}

    def statistics(self) -> dict[str, Any]:
        """Performance statistics computed from the closed-trade book."""
        state = self._load()
        stats = compute_statistics(
            list(state.get("closed_positions") or []),
            float(state.get("initial_capital") or INITIAL_CAPITAL),
        )
        stats["updated_at"] = state.get("updated_at")
        return stats


PAPER_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "paper.json"
convergence_paper_book = ConvergencePaperBook(PAPER_FILE)
