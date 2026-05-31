"""Cash-equity paper-trading book for CBE scanner watchlist signals.

The CBE scanner produces a daily ranked watchlist of NSE F&O stocks each with
a `directional_bias` (bullish/bearish/neutral) and a `composite_score`. This
module turns those signals into a long/short cash-equity paper book.

Why cash-equity instead of options:
  Pulling ATM option contracts for ~200 F&O stocks each day would require a
  major broker-plumbing expansion that conflicts with the explicit
  "TICK_CAPTURE_APP_SYMBOLS only" constraint. Long/short stock requires zero
  new contracts — it uses the same spot OHLC the scanner already loads.

Mechanics:
  - On each scan, iterate watchlist rows (composite_score >= watchlist_min_score
    AND directional_bias != neutral).
  - For each such symbol, OPEN a position if not already held:
        LONG  for bullish bias
        SHORT for bearish bias
    quantity sized so that notional = position_notional_cap (default ₹1L,
    or whatever fits in available_capital). The unit count rounds down.
  - For each currently-open position:
      * If still on the watchlist with the same bias: refresh latest_close +
        unrealized_pnl.
      * If on the watchlist with a flipped bias: close (close_reason="bias_flip")
        and re-open in the new direction.
      * If absent from the latest scan's watchlist for more than
        FLAT_CONFIRMATION_SCANS scans: close (close_reason="dropped_from_watchlist").
  - Mark-to-market uses the `latest_close` field injected by
    `cbe_scanner.features.scan_universe`.

Capital accounting mirrors AI/FMP/S1/S2 — `_summary()` returns initial_capital
/ available_capital / reserved_margin / total_equity / sharpe / max_drawdown /
win_rate, so the frontend portfolio panel renders uniformly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CBE_INITIAL_CAPITAL: float = 1_000_000.0

# Notional cap per individual cash-equity position. With ₹1L per stock the
# book can carry ~10 simultaneous concurrent bets at 100% capital deployed,
# matching the watchlist_max_size default. Sizing rounds DOWN to integer
# shares so the actual reserved margin is <= the cap.
DEFAULT_POSITION_NOTIONAL_CAP: float = 100_000.0

# A position must miss this many consecutive scans before being closed. One
# missed scan is plausible noise (transient feature unavailability); three in
# a row is genuine signal decay.
FLAT_CONFIRMATION_SCANS: int = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_symbol(value: str | None) -> str:
    return str(value or "").strip().upper()


class CBEPaperBook:
    """File-backed cash-equity paper-trading book driven by CBE scan output."""

    def __init__(
        self,
        root: Path | str,
        *,
        initial_capital: float = CBE_INITIAL_CAPITAL,
        position_notional_cap: float = DEFAULT_POSITION_NOTIONAL_CAP,
    ):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()
        self.initial_capital = float(initial_capital)
        self.position_notional_cap = float(position_notional_cap)

    async def sync_from_scan(self, scan_payload: dict[str, Any]) -> dict[str, Any]:
        """Drive the book from a single scan payload (output of run_scan).

        Returns the updated capital summary.
        """
        scan_date = str(scan_payload.get("scan_date") or "")
        recorded_at = _utc_now()
        watchlist = list(scan_payload.get("watchlist") or [])
        watchlist_by_symbol = {
            _norm_symbol(row.get("instrument")): row
            for row in watchlist
            if _norm_symbol(row.get("instrument"))
        }

        async with self._lock:
            state = self._load_positions()
            open_positions: list[dict[str, Any]] = list(state.get("open_positions") or [])
            closed_positions: list[dict[str, Any]] = list(state.get("closed_positions") or [])

            available_capital = self._available_capital(state)

            # Pass 1 — refresh / flip / drop existing positions.
            surviving: list[dict[str, Any]] = []
            for pos in open_positions:
                sym = _norm_symbol(pos.get("instrument"))
                row = watchlist_by_symbol.get(sym)
                if row is None:
                    miss = int(pos.get("consecutive_watchlist_misses") or 0) + 1
                    pos["consecutive_watchlist_misses"] = miss
                    if miss >= FLAT_CONFIRMATION_SCANS:
                        self._close_position(
                            pos,
                            mark_price=float(pos.get("latest_close") or pos.get("entry_price") or 0.0),
                            close_time=recorded_at,
                            close_reason="dropped_from_watchlist",
                        )
                        closed_positions.append(pos)
                        # Free margin freed by closing this row.
                        continue
                    surviving.append(pos)
                    continue

                bias = str(row.get("directional_bias") or "neutral").lower()
                expected_direction = "long" if bias == "bullish" else "short" if bias == "bearish" else None
                pos_direction = str(pos.get("direction") or "").lower()
                latest_close = self._coerce_price(row.get("latest_close"))

                if expected_direction is None or expected_direction != pos_direction:
                    self._close_position(
                        pos,
                        mark_price=latest_close if latest_close is not None else float(pos.get("latest_close") or 0.0),
                        close_time=recorded_at,
                        close_reason="bias_flip" if expected_direction else "neutral_bias",
                    )
                    closed_positions.append(pos)
                    continue

                # Same-direction reaffirmation — mark to market.
                pos["consecutive_watchlist_misses"] = 0
                pos["last_seen_at"] = recorded_at
                pos["last_scan_date"] = scan_date
                if latest_close is not None:
                    pos["latest_close"] = latest_close
                    entry = float(pos.get("entry_price") or 0.0)
                    qty = int(pos.get("quantity") or 0)
                    sign = 1 if pos_direction == "long" else -1
                    pos["unrealized_pnl"] = round((latest_close - entry) * qty * sign, 2)
                pos["latest_composite_score"] = float(row.get("composite_score") or 0.0)
                pos["latest_bias_conviction"] = float(row.get("bias_conviction") or 0.0)
                surviving.append(pos)

            open_positions = surviving

            # Recompute available_capital after closes freed margin.
            tmp_state = {"open_positions": open_positions, "closed_positions": closed_positions}
            available_capital = self._available_capital(tmp_state)

            # Pass 2 — open new positions for watchlist rows not yet held.
            open_symbols = {_norm_symbol(pos.get("instrument")) for pos in open_positions}
            for symbol, row in watchlist_by_symbol.items():
                if symbol in open_symbols:
                    continue
                bias = str(row.get("directional_bias") or "neutral").lower()
                if bias not in ("bullish", "bearish"):
                    continue
                latest_close = self._coerce_price(row.get("latest_close"))
                if latest_close is None or latest_close <= 0.0:
                    continue

                # Size: integer quantity such that notional <= min(cap, available).
                notional_budget = min(self.position_notional_cap, max(0.0, available_capital))
                if notional_budget < latest_close:
                    # Can't afford even one share at current price.
                    continue
                quantity = int(notional_budget // latest_close)
                if quantity <= 0:
                    continue
                notional = round(latest_close * quantity, 2)
                available_capital = round(available_capital - notional, 2)

                direction = "long" if bias == "bullish" else "short"
                journal_row = {
                    "recorded_at": recorded_at,
                    "scan_date": scan_date,
                    "instrument": symbol,
                    "event": "open",
                    "direction": direction,
                    "bias": bias,
                    "composite_score": float(row.get("composite_score") or 0.0),
                    "bias_conviction": float(row.get("bias_conviction") or 0.0),
                    "entry_price": latest_close,
                    "quantity": quantity,
                    "notional": notional,
                }
                self._append_journal(journal_row)

                open_positions.append(
                    {
                        "position_id": uuid4().hex,
                        "status": "open",
                        "opened_at": recorded_at,
                        "updated_at": recorded_at,
                        "last_seen_at": recorded_at,
                        "last_scan_date": scan_date,
                        "consecutive_watchlist_misses": 0,
                        "instrument": symbol,
                        "direction": direction,
                        "bias": bias,
                        "composite_score": float(row.get("composite_score") or 0.0),
                        "bias_conviction": float(row.get("bias_conviction") or 0.0),
                        "latest_composite_score": float(row.get("composite_score") or 0.0),
                        "latest_bias_conviction": float(row.get("bias_conviction") or 0.0),
                        "entry_price": latest_close,
                        "latest_close": latest_close,
                        "exit_price": None,
                        "quantity": quantity,
                        "notional": notional,
                        "unrealized_pnl": 0.0,
                        "realized_pnl": 0.0,
                        "close_reason": None,
                        "closed_at": None,
                    }
                )

            self._save_positions(
                {
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                    "last_synced_at": recorded_at,
                }
            )
            return self._summary(open_positions, closed_positions)

    async def list_positions(self, *, status: str = "all", limit: int = 100) -> dict[str, Any]:
        state = self._load_positions()
        open_positions = list(state.get("open_positions") or [])
        closed_positions = list(state.get("closed_positions") or [])
        open_positions.sort(key=lambda r: str(r.get("opened_at") or ""), reverse=True)
        closed_positions.sort(key=lambda r: str(r.get("closed_at") or r.get("updated_at") or ""), reverse=True)
        if status == "open":
            closed_positions = []
        elif status == "closed":
            open_positions = []
        return {
            "status": status,
            "summary": self._summary(
                list(state.get("open_positions") or []),
                list(state.get("closed_positions") or []),
            ),
            "open_positions": open_positions[:limit],
            "closed_positions": closed_positions[:limit],
            "last_synced_at": state.get("last_synced_at"),
        }

    async def list_journal(self, *, instrument: str | None = None, limit: int = 100) -> dict[str, Any]:
        records = self._load_journal()
        if instrument:
            norm = _norm_symbol(instrument)
            records = [row for row in records if _norm_symbol(row.get("instrument")) == norm]
        records.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        return {
            "instrument_filter": instrument or None,
            "count": len(records),
            "records": records[:limit],
        }

    async def capital_status(self) -> dict[str, Any]:
        state = self._load_positions()
        return self._summary(
            list(state.get("open_positions") or []),
            list(state.get("closed_positions") or []),
        )

    async def reset_account(self, *, actor: str | None = None) -> dict[str, Any]:
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
            "initial_capital": self.initial_capital,
        }

    def _close_position(
        self,
        position: dict[str, Any],
        *,
        mark_price: float,
        close_time: str,
        close_reason: str,
    ) -> None:
        entry = float(position.get("entry_price") or 0.0)
        qty = int(position.get("quantity") or 0)
        direction = str(position.get("direction") or "").lower()
        sign = 1 if direction == "long" else -1
        realized = round((mark_price - entry) * qty * sign, 2)
        position["status"] = "closed"
        position["closed_at"] = close_time
        position["updated_at"] = close_time
        position["exit_price"] = mark_price
        position["latest_close"] = mark_price
        position["realized_pnl"] = realized
        position["unrealized_pnl"] = 0.0
        position["close_reason"] = close_reason
        self._append_journal(
            {
                "recorded_at": close_time,
                "instrument": position.get("instrument"),
                "event": "close",
                "direction": direction,
                "entry_price": entry,
                "exit_price": mark_price,
                "quantity": qty,
                "realized_pnl": realized,
                "close_reason": close_reason,
            }
        )

    def _available_capital(self, state: dict[str, Any]) -> float:
        realized = sum(float(r.get("realized_pnl") or 0.0) for r in state.get("closed_positions") or [])
        reserved = sum(
            float(p.get("entry_price") or 0.0) * float(p.get("quantity") or 0)
            for p in state.get("open_positions") or []
        )
        return round(self.initial_capital + realized - reserved, 2)

    def _summary(
        self,
        open_positions: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        realized = round(sum(float(r.get("realized_pnl") or 0.0) for r in closed_positions), 2)
        unrealized = round(sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions), 2)
        reserved_margin = round(
            sum(
                float(p.get("entry_price") or 0.0) * float(p.get("quantity") or 0)
                for p in open_positions
            ),
            2,
        )
        initial_capital = self.initial_capital
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

    @staticmethod
    def _coerce_price(value: Any) -> float | None:
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f or f <= 0.0:  # NaN or non-positive
            return None
        return f

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


# Module-level singleton, mirrors directional_options_service / fmp_service pattern.
CBE_PAPER_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "cbe_scanner" / "paper"
cbe_paper_book = CBEPaperBook(CBE_PAPER_ROOT)
