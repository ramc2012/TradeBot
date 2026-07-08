"""File-backed paper book for MACD Refined.

Strategy execution model: enter on a premium-MACD zero-cross (one leg per stock,
separate CE/PE books, slot limits, daily-entry cap), then manage each position
with a HARD stop-loss + PARTIAL profit booking ladder + a TRAILING stop on the
runner, with a final time-based window_end exit. The primary paper P&L follows
displayed entry/latest/exit premiums so the table math is transparent. Fills
still carry round-trip slippage and statutory charges, exposed separately as
net execution P&L. The hard stop is gap-safe: evaluated every cycle on the
freshest available mark.

Persisted as JSON under ``runtime/macd_refined/paper``. Summary matches the
canonical shape the frontend portfolio panel renders.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from macd_refined.config import MACD_REFINED_INITIAL_CAPITAL
from macd_refined.risk import kill_switch_state


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(sym: Any) -> str:
    return str(sym or "").upper().strip()


def _round_trip_charges(symbol: str, book: str, entry: float, exit_: float, qty: int) -> float:
    """Statutory + brokerage round-trip charges for a long option (BUY→SELL).
    Uses the shared paper_engine cost model so every lane charges identically.
    Returns 0.0 if the model is unavailable (degraded)."""
    try:
        from paper_engine.costs import round_trip_charges
        return float(round_trip_charges(
            symbol=symbol, instrument_type=book or "CE",
            entry_price=entry, exit_price=exit_, qty=qty, entry_action="BUY",
        ))
    except Exception:
        return 0.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _price_pnl(entry: float, mark: float, qty: int) -> float:
    """Screen P&L from the displayed premiums and unit quantity."""
    return round((float(mark) - float(entry)) * int(qty), 2)


def _remaining_qty(position: dict[str, Any]) -> int:
    return max(0, _as_int(position.get("quantity_units") or position.get("qty") or 0))


def _initial_qty(position: dict[str, Any]) -> int:
    return max(
        0,
        _as_int(
            position.get("initial_qty")
            or position.get("quantity_units")
            or position.get("qty")
            or 0
        ),
    )


def _unrealized_pnl_gross(position: dict[str, Any]) -> float:
    entry = _as_float(position.get("entry_premium"))
    latest = _as_float(
        position.get("latest_premium")
        if position.get("latest_premium") is not None
        else position.get("entry_premium")
    )
    return _price_pnl(entry, latest, _remaining_qty(position))


def _unrealized_pnl_net(position: dict[str, Any], slip_half: float | None = None) -> float:
    if slip_half is not None:
        entry = _as_float(position.get("entry_premium"))
        latest = _as_float(
            position.get("latest_premium")
            if position.get("latest_premium") is not None
            else position.get("entry_premium")
        )
        qty = _remaining_qty(position)
        entry_fill = entry * (1.0 + slip_half)
        mark_fill = latest * (1.0 - slip_half)
        return round((mark_fill - entry_fill) * qty, 2)
    if position.get("unrealized_pnl_net") is not None:
        return round(_as_float(position.get("unrealized_pnl_net")), 2)
    return round(_as_float(position.get("unrealized_pnl")), 2)


def _realized_pnl_gross(position: dict[str, Any]) -> float:
    for key in ("realized_pnl_gross", "gross_pnl"):
        if position.get(key) is not None:
            return round(_as_float(position.get(key)), 2)
    if _remaining_qty(position) <= 0 and not position.get("targets_booked"):
        qty = _initial_qty(position)
        entry = _as_float(position.get("entry_premium"))
        exit_ = _as_float(
            position.get("exit_premium")
            if position.get("exit_premium") is not None
            else position.get("latest_premium")
        )
        if qty > 0 and entry > 0 and exit_ > 0:
            return _price_pnl(entry, exit_, qty)
    return round(_as_float(position.get("realized_pnl")), 2)


def _realized_pnl_net(position: dict[str, Any]) -> float:
    for key in ("realized_pnl_net", "net_realized_pnl"):
        if position.get(key) is not None:
            return round(_as_float(position.get(key)), 2)
    return round(_as_float(position.get("realized_pnl")), 2)


def _decorate_position_for_read(position: dict[str, Any], slip_half: float | None = None) -> dict[str, Any]:
    row = dict(position)
    if row.get("status") == "open" or _remaining_qty(row) > 0:
        gross = _unrealized_pnl_gross(row)
        row["unrealized_pnl_gross"] = gross
        row["unrealized_pnl_net"] = _unrealized_pnl_net(row, slip_half)
        row["unrealized_pnl"] = gross
    if row.get("status") == "closed" or row.get("closed_at") or _remaining_qty(row) <= 0:
        gross = _realized_pnl_gross(row)
        row["realized_pnl_gross"] = gross
        row["realized_pnl_net"] = _realized_pnl_net(row)
        row["realized_pnl"] = gross
    return row


class MacdRefinedPaperStore:
    _EMPTY_LIFETIME = {
        "realized_pnl": 0.0,
        "realized_pnl_net": 0.0,
        "wins": 0,
        "losses": 0,
        "closed_count": 0,
    }

    def __init__(self, root: Path | str, *, config: dict[str, Any]):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.positions_path = self.root / "paper_positions.json"
        self.journal_path = self.root / "paper_journal.jsonl"
        self._lock = threading.Lock()
        self.config = config
        self.initial_capital = float(config.get("risk", {}).get("starting_equity", MACD_REFINED_INITIAL_CAPITAL))
        self._slip_half = float(config.get("execution", {}).get("round_trip_slippage_pct", 0.05)) / 2.0

    # ── State IO ──────────────────────────────────────────────────────────
    def _load(self) -> dict[str, Any]:
        if not self.positions_path.exists():
            return {"open_positions": [], "closed_positions": [], "last_synced_at": _utc_now(), "lifetime": dict(self._EMPTY_LIFETIME)}
        try:
            state = json.loads(self.positions_path.read_text())
        except Exception:
            state = {}
        return {
            "open_positions": list(state.get("open_positions") or []),
            "closed_positions": list(state.get("closed_positions") or []),
            "last_synced_at": state.get("last_synced_at") or _utc_now(),
            "lifetime": {**self._EMPTY_LIFETIME, **(state.get("lifetime") or {})},
        }

    def _save(self, state: dict[str, Any]) -> None:
        state["closed_positions"] = list(state.get("closed_positions") or [])[-5000:]
        tmp = self.positions_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        tmp.replace(self.positions_path)

    def _append_journal(self, row: dict[str, Any]) -> None:
        with self.journal_path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    # ── Read surfaces ─────────────────────────────────────────────────────
    def list_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        state = self._load()
        norm = _norm(symbol)
        opens = state["open_positions"]
        closed = state["closed_positions"]
        if norm:
            opens = [p for p in opens if _norm(p.get("underlying")) == norm]
            closed = [p for p in closed if _norm(p.get("underlying")) == norm]
        opens.sort(key=lambda p: str(p.get("opened_at") or ""), reverse=True)
        closed.sort(key=lambda p: str(p.get("closed_at") or ""), reverse=True)
        if status == "open":
            closed = []
        elif status == "closed":
            opens = []
        return {
            "symbol_filter": norm or None,
            "status": status,
            "summary": self._summary(state["open_positions"], state["closed_positions"], state.get("lifetime")),
            "open_positions": [_decorate_position_for_read(p, self._slip_half) for p in opens[:limit]],
            "closed_positions": [_decorate_position_for_read(p, self._slip_half) for p in closed[:limit]],
        }

    def list_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        if self.journal_path.exists():
            for line in self.journal_path.read_text().splitlines()[-4000:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        norm = _norm(symbol)
        if norm:
            rows = [r for r in rows if _norm(r.get("underlying")) == norm]
        rows.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        return {"symbol_filter": norm or None, "count": len(rows), "records": rows[:limit]}

    def capital_status(self) -> dict[str, Any]:
        state = self._load()
        return self._summary(state["open_positions"], state["closed_positions"], state.get("lifetime"))

    # ── Mutation: sync one cycle ──────────────────────────────────────────
    def sync_cycle(
        self,
        *,
        proposals: list[dict[str, Any]],
        marks: dict[str, dict[str, Any]] | None = None,
        now: str | None = None,
        allow_entries: bool = True,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        marks = marks or {}
        port = self.config["portfolio"]
        ce_slots = int(port["ce_slots"])
        pe_slots = int(port["pe_slots"])
        one_leg = bool(port.get("one_leg_per_stock", True))
        daily_cap = int(port.get("daily_new_entry_cap", 8))

        with self._lock:
            state = self._load()
            opens = list(state["open_positions"])
            closed = list(state["closed_positions"])
            lifetime = {**self._EMPTY_LIFETIME, **(state.get("lifetime") or {})}

            # 1) Manage open positions: mark, hard-stop, partial book, trail, window.
            survivors: list[dict[str, Any]] = []
            for p in opens:
                pid = str(p.get("position_id") or "")
                mark = marks.get(pid) or {}
                fully_closed, realized_delta, realized_net_delta = self._manage(p, mark, now)
                if realized_delta:
                    lifetime["realized_pnl"] = round(_as_float(lifetime.get("realized_pnl")) + realized_delta, 2)
                if realized_net_delta:
                    lifetime["realized_pnl_net"] = round(_as_float(lifetime.get("realized_pnl_net")) + realized_net_delta, 2)
                if fully_closed:
                    closed.append(p)
                    lifetime["closed_count"] += 1
                    total_rp = float(p.get("realized_pnl") or 0.0)
                    if total_rp > 0:
                        lifetime["wins"] += 1
                    elif total_rp < 0:
                        lifetime["losses"] += 1
                else:
                    survivors.append(p)
            opens = survivors

            # 2) Kill switch — drawdown INCLUDES open MTM (not realized-only).
            closed_returns = [float(c.get("return_pct") or 0.0) for c in closed]
            unreal = sum(float(p.get("unrealized_pnl") or 0.0) for p in opens)
            dd = self._drawdown_incl_open(closed, unreal, lifetime)
            paused, pause_reason = kill_switch_state(closed_returns, risk_cfg=self.config["risk"], current_drawdown_pct=dd)
            today_prefix = str(now)[:10]
            daily_count = sum(1 for p in (opens + closed) if str(p.get("opened_at") or "")[:10] == today_prefix)

            # 3) Admit new proposals.
            admitted = 0
            if allow_entries and not paused:
                for prop in proposals:
                    underlying = _norm(prop.get("underlying"))
                    book = str(prop.get("option_type") or "").upper()
                    if one_leg and any(_norm(p.get("underlying")) == underlying for p in opens):
                        continue
                    book_open = sum(1 for p in opens if str(p.get("option_type")) == book)
                    if (book == "CE" and book_open >= ce_slots) or (book == "PE" and book_open >= pe_slots):
                        continue
                    if daily_count >= daily_cap:
                        break
                    # qty key tolerant: accept quantity_units|qty_units.
                    qty = int(prop.get("quantity_units") or prop.get("qty_units") or 0)
                    lots = int(prop.get("quantity_lots") or prop.get("qty_lots") or 0)
                    if qty <= 0:
                        continue
                    pid = uuid4().hex
                    entry_gross = float(prop.get("entry_premium") or 0.0)
                    entry_fill = round(entry_gross * (1.0 + self._slip_half), 4)  # pay up on entry
                    opens.append({
                        "position_id": pid, "status": "open", "opened_at": now, "updated_at": now, "closed_at": None,
                        "underlying": underlying, "book": book, "option_type": book, "direction": book,
                        "trading_symbol": prop.get("trading_symbol") or f"{underlying} {prop.get('strike')} {book}",
                        "instrument_key": prop.get("instrument_key") or "",
                        "expiry": prop.get("expiry"), "expiry_window_end": prop.get("expiry_window_end"),
                        "strike": float(prop.get("strike") or 0.0), "lot_size": int(prop.get("lot_size") or 1),
                        "quantity_lots": lots, "quantity_units": qty, "initial_qty": qty,
                        "entry_premium": entry_gross, "entry_fill_premium": entry_fill,
                        "latest_premium": entry_gross, "exit_premium": None,
                        "entry_spot": float(prop.get("spot") or 0.0), "latest_spot": float(prop.get("spot") or 0.0),
                        "peak_premium": entry_gross, "phase": "open", "targets_booked": [],
                        "unrealized_pnl": 0.0, "realized_pnl": 0.0,
                        "entry_iv_pct": round(float(prop.get("iv") or 0.0) * 100.0, 2), "iv_rank": prop.get("iv_rank"),
                        "direction_bias": prop.get("direction_bias"), "signal_kind": prop.get("signal_kind"),
                        "selection_reason": prop.get("selection_reason"), "daily_turnover_rupees": prop.get("daily_turnover_rupees"),
                    })
                    self._append_journal({
                        "recorded_at": now, "event": "open", "underlying": underlying, "book": book,
                        "strike": prop.get("strike"), "expiry": prop.get("expiry"), "entry_premium": entry_gross,
                        "entry_fill": entry_fill, "qty": qty, "selection_reason": prop.get("selection_reason"),
                        "iv_rank": prop.get("iv_rank"), "direction_bias": prop.get("direction_bias"),
                    })
                    admitted += 1
                    daily_count += 1

            self._refresh_lifetime(lifetime, opens, closed)
            state = {"open_positions": opens, "closed_positions": closed, "last_synced_at": now, "lifetime": lifetime}
            self._save(state)
            summary = self._summary(opens, closed, lifetime)
            summary["kill_switch_paused"] = paused
            summary["kill_switch_reason"] = pause_reason
            summary["admitted_this_cycle"] = admitted
            return summary

    # ── Position management: stop / partial / trail / window ──────────────
    def _manage(self, p: dict[str, Any], mark: dict[str, Any], now: str) -> tuple[bool, float, float]:
        """Mark a position and apply hard-SL / partial-book / trailing / window.
        Returns (fully_closed, gross_realized_delta, net_realized_delta)."""
        ex = self.config["exits"]
        stop_pct = float(ex.get("stop_loss_pct", 0.30))
        targets = list(ex.get("targets") or [])
        trail_on = bool(ex.get("trail_after_first_target", True))
        trail_give = float(ex.get("trail_giveback_pct", 0.25))

        entry_gross = float(p.get("entry_premium") or 0.0)
        entry_fill = round(entry_gross * (1.0 + self._slip_half), 4)
        p["entry_fill_premium"] = entry_fill
        latest = float(mark.get("premium") if mark.get("premium") is not None else p.get("latest_premium") or entry_gross)
        spot = float(mark.get("spot") or p.get("latest_spot") or p.get("entry_spot") or 0.0)
        qty = int(p.get("quantity_units") or 0)
        p["latest_premium"] = latest
        p["latest_spot"] = spot
        p["peak_premium"] = max(float(p.get("peak_premium") or entry_gross), latest)
        p["updated_at"] = now
        # Primary P&L follows the displayed entry/latest premiums. Execution-net
        # P&L is retained separately so UI and reports do not mix bases.
        mark_fill = latest * (1.0 - self._slip_half)
        gross_unrealized = _price_pnl(entry_gross, latest, qty)
        net_unrealized = round((mark_fill - entry_fill) * qty, 2)
        p["unrealized_pnl"] = gross_unrealized
        p["unrealized_pnl_gross"] = gross_unrealized
        p["unrealized_pnl_net"] = net_unrealized
        p["slippage_unrealized_pnl"] = round(net_unrealized - gross_unrealized, 2)

        if entry_gross <= 0 or qty <= 0:
            return True, 0.0, 0.0  # malformed → drop

        realized_delta = 0.0
        realized_net_delta = 0.0

        # (a) HARD STOP — gap-safe: always evaluated on the freshest mark.
        if latest <= entry_gross * (1.0 - stop_pct):
            gross_delta, net_delta = self._book(p, latest, spot, now, qty, "stop_loss")
            realized_delta += gross_delta
            realized_net_delta += net_delta
            return True, realized_delta, realized_net_delta

        # (b) PARTIAL BOOKING ladder — book once per target, in order.
        booked = set(int(i) for i in (p.get("targets_booked") or []))
        initial_qty = int(p.get("initial_qty") or qty)
        for idx, tgt in enumerate(targets):
            if idx in booked:
                continue
            if latest >= entry_gross * (1.0 + float(tgt.get("gain_pct", 0.0))):
                book_qty = min(int(round(initial_qty * float(tgt.get("book_fraction", 0.0)))), int(p.get("quantity_units") or 0))
                if book_qty > 0:
                    gross_delta, net_delta = self._book(p, latest, spot, now, book_qty, f"target_{idx+1}", partial=True)
                    realized_delta += gross_delta
                    realized_net_delta += net_delta
                booked.add(idx)
                p["targets_booked"] = sorted(booked)
                if trail_on:
                    p["phase"] = "trailing"
                # re-read remaining qty
                qty = int(p.get("quantity_units") or 0)
                if qty <= 0:
                    return True, realized_delta, realized_net_delta

        # (c) TRAILING STOP on the runner once trailing.
        if p.get("phase") == "trailing":
            trail_level = float(p.get("peak_premium") or entry_gross) * (1.0 - trail_give)
            if latest <= trail_level:
                gross_delta, net_delta = self._book(p, latest, spot, now, int(p.get("quantity_units") or 0), "trailing_stop")
                realized_delta += gross_delta
                realized_net_delta += net_delta
                return True, realized_delta, realized_net_delta

        # (d) WINDOW END — final time-based exit on the remainder.
        if bool(mark.get("window_end_passed")):
            gross_delta, net_delta = self._book(p, latest, spot, now, int(p.get("quantity_units") or 0), "window_end")
            realized_delta += gross_delta
            realized_net_delta += net_delta
            return True, realized_delta, realized_net_delta

        qty = int(p.get("quantity_units") or 0)
        if qty > 0:
            gross_unrealized = _price_pnl(entry_gross, latest, qty)
            net_unrealized = round((mark_fill - entry_fill) * qty, 2)
            p["unrealized_pnl"] = gross_unrealized
            p["unrealized_pnl_gross"] = gross_unrealized
            p["unrealized_pnl_net"] = net_unrealized
            p["slippage_unrealized_pnl"] = round(net_unrealized - gross_unrealized, 2)
        return False, realized_delta, realized_net_delta

    def _book(self, p: dict[str, Any], exit_gross: float, spot: float, now: str, qty_close: int, reason: str, *, partial: bool = False) -> tuple[float, float]:
        """Realize qty_close units at exit_gross.

        ``realized_pnl`` is the screen/gross P&L from displayed premiums.
        ``realized_pnl_net`` keeps the conservative slippage+charges result.
        """
        qty_close = max(0, min(int(qty_close), int(p.get("quantity_units") or 0)))
        if qty_close <= 0:
            return 0.0, 0.0
        entry_gross = float(p.get("entry_premium") or 0.0)
        entry_fill = float(p.get("entry_fill_premium") or entry_gross)
        exit_fill = exit_gross * (1.0 - self._slip_half)
        gross = _price_pnl(entry_gross, exit_gross, qty_close)
        fill_pnl = round((exit_fill - entry_fill) * qty_close, 2)
        charges = _round_trip_charges(
            str(p.get("trading_symbol") or p.get("underlying") or ""),
            str(p.get("book") or "CE"), entry_gross, exit_gross, qty_close,
        )
        net = round(fill_pnl - charges, 2)
        p["quantity_units"] = int(p.get("quantity_units") or 0) - qty_close
        prior_gross = _as_float(p.get("realized_pnl_gross"), _as_float(p.get("gross_pnl"), _as_float(p.get("realized_pnl"))))
        prior_net = _as_float(p.get("realized_pnl_net"), _as_float(p.get("net_realized_pnl"), _as_float(p.get("realized_pnl"))))
        p["realized_pnl"] = round(prior_gross + gross, 2)
        p["realized_pnl_gross"] = p["realized_pnl"]
        p["gross_pnl"] = p["realized_pnl"]
        p["realized_pnl_net"] = round(prior_net + net, 2)
        p["net_realized_pnl"] = p["realized_pnl_net"]
        p["transaction_costs"] = round(_as_float(p.get("transaction_costs")) + charges, 2)
        p["slippage_pnl"] = round(_as_float(p.get("slippage_pnl")) + (fill_pnl - gross), 2)
        remaining = int(p.get("quantity_units") or 0)
        self._append_journal({
            "recorded_at": now, "event": ("partial_book" if partial else "close"),
            "underlying": p.get("underlying"), "book": p.get("book"), "strike": p.get("strike"),
            "expiry": p.get("expiry"), "qty_closed": qty_close, "remaining_qty": remaining,
            "entry_premium": entry_gross, "exit_premium": exit_gross, "exit_fill": round(exit_fill, 4),
            "gross_pnl": gross, "fill_pnl": fill_pnl, "charges": round(charges, 2),
            "realized_pnl": gross, "realized_pnl_net": net, "reason": reason,
        })
        if remaining <= 0:
            # Position fully closed.
            p["status"] = "closed"
            p["closed_at"] = now
            p["updated_at"] = now
            p["close_reason"] = reason
            p["exit_premium"] = exit_gross
            p["latest_premium"] = exit_gross
            p["exit_spot"] = spot
            p["unrealized_pnl"] = 0.0
            p["unrealized_pnl_gross"] = 0.0
            p["unrealized_pnl_net"] = 0.0
            total_invested = entry_gross * int(p.get("initial_qty") or 1)
            p["return_pct"] = round(float(p.get("realized_pnl") or 0.0) / total_invested * 100.0, 4) if total_invested else 0.0
        return gross, net

    # ── Account / drawdown ────────────────────────────────────────────────
    def _drawdown_incl_open(self, closed: list[dict[str, Any]], unrealized: float, lifetime: dict[str, Any] | None) -> float:
        """Drawdown of total equity (initial + realized curve + open MTM) from
        its running peak — so big open losses pause new entries (not just realized)."""
        eq = self.initial_capital
        peak = eq
        for c in sorted(closed, key=lambda r: str(r.get("closed_at") or "")):
            eq += _realized_pnl_gross(c)
            peak = max(peak, eq)
        # Account for realized history beyond the capped closed list only once it
        # has been written on the current gross-premium basis.
        if lifetime and lifetime.get("pnl_basis") == "gross_premium":
            life_real = float(lifetime.get("realized_pnl") or 0.0)
            eq = self.initial_capital + life_real
            peak = max(peak, eq)
        current = eq + float(unrealized or 0.0)
        peak = max(peak, current)
        return (peak - current) / peak if peak > 0 else 0.0

    def _refresh_lifetime(self, lifetime: dict[str, Any], opens: list[dict[str, Any]], closed: list[dict[str, Any]]) -> None:
        positions = list(opens) + list(closed)
        lifetime["pnl_basis"] = "gross_premium"
        lifetime["realized_pnl"] = round(sum(_realized_pnl_gross(p) for p in positions), 2)
        lifetime["realized_pnl_net"] = round(sum(_realized_pnl_net(p) for p in positions), 2)
        lifetime["closed_count"] = len(closed)
        lifetime["wins"] = sum(1 for p in closed if _realized_pnl_gross(p) > 0)
        lifetime["losses"] = sum(1 for p in closed if _realized_pnl_gross(p) < 0)

    def reset_account(self, *, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = self.root / "archive" / stamp
            archive.mkdir(parents=True, exist_ok=True)
            if self.positions_path.exists():
                self.positions_path.replace(archive / "paper_positions.json")
            if self.journal_path.exists():
                self.journal_path.replace(archive / "paper_journal.jsonl")
        return {"reset": True, "actor": actor, "initial_capital": self.initial_capital}

    def _summary(self, opens, closed, lifetime=None) -> dict[str, Any]:
        positions = list(opens) + list(closed)
        if lifetime and lifetime.get("pnl_basis") == "gross_premium":
            realized = round(float(lifetime.get("realized_pnl") or 0.0), 2)
            realized_net = round(float(lifetime.get("realized_pnl_net") or realized), 2)
            wins = int(lifetime.get("wins") or 0)
            losses = int(lifetime.get("losses") or 0)
            closed_count = int(lifetime.get("closed_count") or len(closed))
        else:
            realized = round(sum(_realized_pnl_gross(p) for p in positions), 2)
            realized_net = round(sum(_realized_pnl_net(p) for p in positions), 2)
            wins = sum(1 for c in closed if _realized_pnl_gross(c) > 0)
            losses = sum(1 for c in closed if _realized_pnl_gross(c) < 0)
            closed_count = len(closed)
        unreal = round(sum(_unrealized_pnl_gross(p) for p in opens), 2)
        unreal_net = round(sum(_unrealized_pnl_net(p, self._slip_half) for p in opens), 2)
        reserved = round(sum(float(p.get("entry_fill_premium") or p.get("entry_premium") or 0.0) * float(p.get("quantity_units") or 0) for p in opens), 2)
        total_equity = round(self.initial_capital + realized + unreal, 2)
        total_equity_net = round(self.initial_capital + realized_net + unreal_net, 2)
        available = round(self.initial_capital + realized - reserved, 2)
        available_net = round(self.initial_capital + realized_net - reserved, 2)
        win_rate = round(wins / (wins + losses), 4) if (wins + losses) else 0.0
        return {
            "open_positions": len(opens),
            "closed_positions": closed_count,
            "realized_pnl": realized,
            "realized_pnl_net": realized_net,
            "unrealized_pnl": unreal,
            "unrealized_pnl_net": unreal_net,
            "total_pnl": round(realized + unreal, 2),
            "total_pnl_net": round(realized_net + unreal_net, 2),
            "initial_capital": self.initial_capital,
            "available_capital": available,
            "available_capital_net": available_net,
            "reserved_margin": reserved,
            "total_equity": total_equity,
            "total_equity_net": total_equity_net,
            "total_return_pct": round((total_equity - self.initial_capital) / self.initial_capital * 100.0, 4) if self.initial_capital else 0.0,
            "max_drawdown": round(self._drawdown_incl_open(closed, unreal, lifetime), 4),
            "total_trades": wins + losses,
            "win_rate": win_rate,
            "ce_open": sum(1 for p in opens if str(p.get("book")) == "CE"),
            "pe_open": sum(1 for p in opens if str(p.get("book")) == "PE"),
        }
