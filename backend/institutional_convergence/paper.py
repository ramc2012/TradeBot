from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

from .engine import lots_for_risk


INITIAL_CAPITAL = 1_000_000.0


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
                    self._close(position, last_price, "intraday_squareoff_stale_mark", now, open_positions, closed_positions)
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
            cvd_reversal = len(cvd) >= 2 and ((cvd[-1]["cvd"] < cvd[-2]["cvd"] if direction == "LONG" else cvd[-1]["cvd"] > cvd[-2]["cvd"]))
            if stop_hit:
                self._close(position, price, "hard_stop", now, open_positions, closed_positions)
            elif not position.get("target1_done") and target1_hit:
                exit_lots = max(1, int(position["lots"]) // 2)
                position["realized_pnl"] = round((price - position["entry_price"]) * position["lot_size"] * exit_lots * (1 if direction == "LONG" else -1), 2)
                position["lots"] -= exit_lots
                position["target1_done"] = True
                position["stop"] = position["entry_price"]
                position["break_even_at"] = now.isoformat()
                if position["lots"] <= 0:
                    self._close(position, price, "target1", now, open_positions, closed_positions)
            elif position.get("target1_done") and (wall_hit or cvd_reversal):
                self._close(position, price, "oi_wall" if wall_hit else "cvd_reversal", now, open_positions, closed_positions)
            elif squareoff_due:
                self._close(position, price, "intraday_squareoff", now, open_positions, closed_positions)

        circuit = self._circuit_state(closed_positions, today, state)
        quarantined = self.entry_quarantine(now)
        open_symbols = {row["symbol"] for row in open_positions}
        if not circuit["locked"] and not quarantined and not squareoff_due:
            capital = self._equity(state, open_positions, closed_positions)
            for row in results:
                if row.get("status") != "actionable_paper" or row.get("action") not in {"LONG", "SHORT"} or row.get("symbol") in open_symbols:
                    continue
                risk = row.get("risk") or {}
                entry, stop = float(risk.get("entry") or 0), float(risk.get("stop") or 0)
                lot_size = int(risk.get("lot_size") or 0)
                lots = lots_for_risk(capital, float(risk.get("risk_fraction") or 0.01), abs(entry - stop), lot_size)
                if lots <= 0:
                    continue
                position = {
                    "position_id": f"IC-{row['symbol']}-{int(now.timestamp())}",
                    "symbol": row["symbol"], "direction": row["action"], "entry_price": entry,
                    "futures_contract": row.get("futures_contract"),
                    "current_price": entry, "stop": stop, "target1": risk.get("target1"),
                    "target2": risk.get("target2_long") if row["action"] == "LONG" else risk.get("target2_short"),
                    "lot_size": lot_size, "lots": lots, "initial_lots": lots, "target1_done": False,
                    "opened_at": now.isoformat(), "session_date": today, "realized_pnl": 0.0,
                    "risk_fraction": risk.get("risk_fraction"), "status": "open",
                }
                open_positions.append(position)
                open_symbols.add(row["symbol"])

        state.update({"open_positions": open_positions, "closed_positions": closed_positions[-500:], "updated_at": now.isoformat(), "circuit_breaker": circuit})
        self._save(state)
        return self.summary(state)

    def _close(self, position, price, reason, now, open_positions, closed_positions):
        if position not in open_positions:
            return
        direction = 1 if position["direction"] == "LONG" else -1
        pnl = (price - position["entry_price"]) * position["lot_size"] * position["lots"] * direction
        position.update({"status": "closed", "exit_price": price, "closed_at": now.isoformat(), "exit_reason": reason, "realized_pnl": round(float(position.get("realized_pnl") or 0) + pnl, 2)})
        open_positions.remove(position)
        closed_positions.append(position)

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
        return {"initial_capital": float(state.get("initial_capital") or INITIAL_CAPITAL), "equity": round(equity, 2), "realized_pnl": round(realized, 2), "open_count": len(open_positions), "closed_count": len(closed_positions), "open_positions": open_positions, "closed_positions": closed_positions[-100:], "circuit_breaker": state.get("circuit_breaker") or {}}


PAPER_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "paper.json"
convergence_paper_book = ConvergencePaperBook(PAPER_FILE)
