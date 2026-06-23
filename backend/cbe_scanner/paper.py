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
    quantity sized so that notional fits the hedge mandate: cash-only gross
    exposure, max net exposure, max single-name weight, max sector weight, and
    available capital. The unit count rounds down.
  - For each currently-open position:
      * If still on the watchlist with the same bias: refresh latest_close +
        unrealized_pnl.
      * If on the watchlist with a flipped bias: close (close_reason="bias_flip")
        ONLY after MIN_HOLD_TRADING_DAYS — weekly-rebalance rule.
      * If absent from the watchlist: close (close_reason="dropped_from_watchlist")
        ONLY after MIN_HOLD_TRADING_DAYS.
  - Mark-to-market uses the `latest_close` field injected by
    `cbe_scanner.features.scan_universe`.

Capital accounting mirrors AI/FMP/S1/S2 — `_summary()` returns initial_capital
/ available_capital / reserved_margin / total_equity / sharpe / max_drawdown /
win_rate plus hedge-book exposure diagnostics, so the frontend portfolio panel
renders uniformly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CBE_INITIAL_CAPITAL: float = 1_000_000.0

# Notional cap per individual cash-equity position. With ₹1L per stock, a
# balanced long/short book can use the full cash gross budget while one-sided
# scans are constrained by the hedge mandate. Sizing rounds DOWN to integer
# shares so reserved margin <= the cap.
DEFAULT_POSITION_NOTIONAL_CAP: float = 100_000.0

# Minimum hold period in TRADING DAYS. Spec calls for weekly rebalance;
# this enforces that positions opened today cannot exit before 5 trading
# days have elapsed unless a hard stop fires. Replaces the old "drop after
# 3 missed scans" rule which conflicted with weekly cadence.
MIN_HOLD_TRADING_DAYS: int = 5

# Hedge-fund style portfolio construction guardrails. These are deliberately
# simple cash-book limits: no leverage, constrained net beta, capped single-name
# and sector concentration. The UI reads the same values from each summary.
HEDGE_MAX_GROSS_EXPOSURE_RATIO: float = 1.0
HEDGE_MAX_NET_EXPOSURE_RATIO: float = 0.40
HEDGE_MAX_SINGLE_NAME_RATIO: float = 0.10
HEDGE_MAX_SECTOR_EXPOSURE_RATIO: float = 0.30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_symbol(value: str | None) -> str:
    return str(value or "").strip().upper()


def _min_hold_satisfied(position: dict[str, Any]) -> bool:
    """True if MIN_HOLD_TRADING_DAYS have elapsed since opened_at.

    Approximation: 5 trading days ≈ 7 calendar days. Good enough for the
    weekly-rebalance rule; refinement via core/trading_calendar can come
    later if precision matters.
    """
    opened_at = position.get("opened_at")
    if not opened_at:
        return True
    try:
        opened_dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    except Exception:
        return True
    elapsed_days = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 86400.0
    # 5 trading days ≈ 7 calendar days (covers a weekend).
    return elapsed_days >= (MIN_HOLD_TRADING_DAYS * 7.0 / 5.0)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def _position_notional(position: dict[str, Any]) -> float:
    notional = _coerce_float(position.get("notional"))
    if notional > 0:
        return abs(notional)
    entry = _coerce_float(position.get("entry_price"))
    qty = _coerce_float(position.get("quantity"))
    return abs(entry * qty)


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
        # Apply L1 equity exposure budget — scales notional cap. With 100% it
        # behaves unchanged; with 40% the per-position cap halves+halves
        # so the book sizes down when equities are not the asset-class leader.
        exposure_pct = float(scan_payload.get("equity_exposure_pct") or 100.0)
        sized_cap = self.position_notional_cap * (exposure_pct / 100.0)
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

            # Pass 1 — refresh / flip / drop existing positions, with the
            # weekly-rebalance min-hold rule enforced. A position cannot
            # close on its first signal flip until MIN_HOLD_TRADING_DAYS
            # have elapsed since open. Mark-to-market still updates every
            # scan; only the close decision is gated.
            surviving: list[dict[str, Any]] = []
            for pos in open_positions:
                sym = _norm_symbol(pos.get("instrument"))
                row = watchlist_by_symbol.get(sym)
                hold_ok = _min_hold_satisfied(pos)

                # Case A: symbol absent from watchlist.
                if row is None:
                    if hold_ok:
                        self._close_position(
                            pos,
                            mark_price=float(pos.get("latest_close") or pos.get("entry_price") or 0.0),
                            close_time=recorded_at,
                            close_reason="dropped_from_watchlist",
                        )
                        closed_positions.append(pos)
                        continue
                    # Min-hold not satisfied — keep the position; just bump
                    # last_seen so we can audit how long it's been silent.
                    pos["last_silent_scan_at"] = recorded_at
                    surviving.append(pos)
                    continue

                bias = str(row.get("directional_bias") or "neutral").lower()
                expected_direction = "long" if bias == "bullish" else "short" if bias == "bearish" else None
                pos_direction = str(pos.get("direction") or "").lower()
                latest_close = self._coerce_price(row.get("latest_close"))

                # Case B: bias flipped or went neutral. Only close if min-hold met.
                if expected_direction is None or expected_direction != pos_direction:
                    if hold_ok:
                        self._close_position(
                            pos,
                            mark_price=latest_close if latest_close is not None else float(pos.get("latest_close") or 0.0),
                            close_time=recorded_at,
                            close_reason="bias_flip" if expected_direction else "neutral_bias",
                        )
                        closed_positions.append(pos)
                        continue
                    # Still in min-hold window — refresh mark and hold.
                    pos["pending_close_reason"] = "bias_flip" if expected_direction else "neutral_bias"
                    if latest_close is not None:
                        pos["latest_close"] = latest_close
                        entry = float(pos.get("entry_price") or 0.0)
                        qty = int(pos.get("quantity") or 0)
                        sign = 1 if pos_direction == "long" else -1
                        pos["unrealized_pnl"] = round((latest_close - entry) * qty * sign, 2)
                    surviving.append(pos)
                    continue

                # Case C: same-direction reaffirmation — mark to market.
                pos.pop("pending_close_reason", None)
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
                pos["latest_alpha_score"] = _coerce_float(
                    row.get("composite_alpha_score"),
                    _coerce_float(row.get("composite_score")) * 10.0,
                )
                pos["sector_code"] = row.get("sector_code") or pos.get("sector_code")
                pos["sector_quadrant"] = row.get("sector_quadrant") or pos.get("sector_quadrant")
                pos["stock_quadrant"] = row.get("stock_quadrant") or pos.get("stock_quadrant")
                pos["stock_rs_pct"] = row.get("stock_rs_pct") if row.get("stock_rs_pct") is not None else pos.get("stock_rs_pct")
                pos["rank_overall"] = row.get("rank_overall") or pos.get("rank_overall")
                pos["rank_actionable"] = row.get("rank_actionable") or pos.get("rank_actionable")
                surviving.append(pos)

            open_positions = surviving

            # Recompute available_capital after closes freed margin.
            tmp_state = {"open_positions": open_positions, "closed_positions": closed_positions}
            available_capital = self._available_capital(tmp_state)

            # Pass 2 — open new positions for watchlist rows not yet held.
            # Hedge-fund mandate: stay cash-only, but run the CBE book as a
            # constrained long/short sleeve. That means each new name has to
            # fit the gross, net, single-name, and sector budgets before it is
            # admitted. When a one-sided scan floods the book, only the first
            # tranche gets capital until offsetting signals arrive.
            open_symbols = {_norm_symbol(pos.get("instrument")) for pos in open_positions}
            sorted_watchlist = sorted(
                watchlist_by_symbol.items(),
                key=lambda item: _coerce_float(
                    item[1].get("composite_alpha_score"),
                    _coerce_float(item[1].get("composite_score")) * 10.0,
                ),
                reverse=True,
            )
            equity_budget = self.initial_capital * max(0.0, min(exposure_pct, 100.0)) / 100.0
            max_gross = equity_budget * HEDGE_MAX_GROSS_EXPOSURE_RATIO
            max_net = equity_budget * HEDGE_MAX_NET_EXPOSURE_RATIO
            max_single_name = min(sized_cap, self.initial_capital * HEDGE_MAX_SINGLE_NAME_RATIO)
            max_sector = equity_budget * HEDGE_MAX_SECTOR_EXPOSURE_RATIO
            for symbol, row in sorted_watchlist:
                if symbol in open_symbols:
                    continue
                bias = str(row.get("directional_bias") or "neutral").lower()
                if bias not in ("bullish", "bearish"):
                    continue
                latest_close = self._coerce_price(row.get("latest_close"))
                if latest_close is None or latest_close <= 0.0:
                    continue

                direction = "long" if bias == "bullish" else "short"
                sector_code = str(row.get("sector_code") or "unclassified").strip() or "unclassified"
                exposure = self._portfolio_exposure(open_positions)
                gross_room = max(0.0, max_gross - exposure["gross"])
                if direction == "long":
                    net_room = max(0.0, max_net - exposure["net"])
                else:
                    net_room = max(0.0, max_net + exposure["net"])
                sector_room = max(0.0, max_sector - exposure["sectors"].get(sector_code, {}).get("gross", 0.0))

                # Size: integer quantity such that notional fits every budget.
                notional_budget = min(
                    max_single_name,
                    max(0.0, available_capital),
                    gross_room,
                    net_room,
                    sector_room,
                )
                if notional_budget < latest_close:
                    # Can't afford even one share at current price.
                    continue
                quantity = int(notional_budget // latest_close)
                if quantity <= 0:
                    continue
                notional = round(latest_close * quantity, 2)
                available_capital = round(available_capital - notional, 2)

                alpha_score = _coerce_float(
                    row.get("composite_alpha_score"),
                    _coerce_float(row.get("composite_score")) * 10.0,
                )
                journal_row = {
                    "recorded_at": recorded_at,
                    "scan_date": scan_date,
                    "instrument": symbol,
                    "event": "open",
                    "direction": direction,
                    "bias": bias,
                    "composite_score": float(row.get("composite_score") or 0.0),
                    "alpha_score": alpha_score,
                    "bias_conviction": float(row.get("bias_conviction") or 0.0),
                    "sector_code": sector_code,
                    "risk_budget": {
                        "max_gross": round(max_gross, 2),
                        "max_net": round(max_net, 2),
                        "max_single_name": round(max_single_name, 2),
                        "max_sector": round(max_sector, 2),
                    },
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
                        "pending_close_reason": None,
                        "instrument": symbol,
                        "direction": direction,
                        "bias": bias,
                        "composite_score": float(row.get("composite_score") or 0.0),
                        "alpha_score": alpha_score,
                        "bias_conviction": float(row.get("bias_conviction") or 0.0),
                        "latest_composite_score": float(row.get("composite_score") or 0.0),
                        "latest_alpha_score": alpha_score,
                        "latest_bias_conviction": float(row.get("bias_conviction") or 0.0),
                        "sector_code": sector_code,
                        "sector_quadrant": row.get("sector_quadrant"),
                        "stock_quadrant": row.get("stock_quadrant"),
                        "stock_rs_pct": row.get("stock_rs_pct"),
                        "rank_overall": row.get("rank_overall"),
                        "rank_actionable": row.get("rank_actionable"),
                        "risk_budget_gross": round(max_gross, 2),
                        "risk_budget_net": round(max_net, 2),
                        "risk_budget_single_name": round(max_single_name, 2),
                        "risk_budget_sector": round(max_sector, 2),
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

    async def refresh_open_marks(self, prices: dict[str, Any]) -> dict[str, Any]:
        """Lightweight mark-to-market for OPEN positions only.

        Updates ``latest_close`` + ``unrealized_pnl`` for each held position
        whose symbol has a fresh price in ``prices``, WITHOUT re-running the
        alpha scan or touching any open/close/rebalance logic. Intended for a
        fast (5-minute) cadence so the UI shows a live LTP between the heavier
        end-of-day-design scans, at a fraction of the CPU cost.

        ``prices`` maps symbol -> latest price. Symbols absent from the map (or
        with a non-positive price) keep their existing mark.
        """
        norm_prices = {
            _norm_symbol(sym): price
            for sym, price in (prices or {}).items()
            if _norm_symbol(sym)
        }
        async with self._lock:
            state = self._load_positions()
            open_positions: list[dict[str, Any]] = list(state.get("open_positions") or [])
            closed_positions: list[dict[str, Any]] = list(state.get("closed_positions") or [])
            refreshed = 0
            stamp = _utc_now()
            for pos in open_positions:
                sym = _norm_symbol(pos.get("instrument"))
                price = self._coerce_price(norm_prices.get(sym))
                if price is None:
                    continue
                entry = float(pos.get("entry_price") or 0.0)
                qty = int(pos.get("quantity") or 0)
                sign = 1 if str(pos.get("direction") or "").lower() == "long" else -1
                pos["latest_close"] = price
                pos["unrealized_pnl"] = round((price - entry) * qty * sign, 2)
                pos["mark_refreshed_at"] = stamp
                refreshed += 1
            if refreshed:
                self._save_positions(
                    {
                        "open_positions": open_positions,
                        "closed_positions": closed_positions,
                        "last_synced_at": state.get("last_synced_at"),
                        "last_mark_refresh_at": stamp,
                    }
                )
            summary = self._summary(open_positions, closed_positions)
            summary["marks_refreshed"] = refreshed
            return summary

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

    def _portfolio_exposure(self, open_positions: list[dict[str, Any]]) -> dict[str, Any]:
        long_exposure = 0.0
        short_exposure = 0.0
        sectors: dict[str, dict[str, Any]] = {}
        names: list[float] = []

        for pos in open_positions:
            notional = _position_notional(pos)
            if notional <= 0:
                continue
            direction = str(pos.get("direction") or "").lower()
            sector = str(pos.get("sector_code") or "unclassified").strip() or "unclassified"
            sector_row = sectors.setdefault(
                sector,
                {"sector": sector, "long": 0.0, "short": 0.0, "gross": 0.0, "names": 0},
            )
            if direction == "short":
                short_exposure += notional
                sector_row["short"] += notional
            else:
                long_exposure += notional
                sector_row["long"] += notional
            sector_row["gross"] += notional
            sector_row["names"] += 1
            names.append(notional)

        gross = long_exposure + short_exposure
        net = long_exposure - short_exposure
        names.sort(reverse=True)
        return {
            "long": round(long_exposure, 2),
            "short": round(short_exposure, 2),
            "gross": round(gross, 2),
            "net": round(net, 2),
            "largest": round(names[0], 2) if names else 0.0,
            "top3": round(sum(names[:3]), 2),
            "sectors": sectors,
        }

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
        exposure = self._portfolio_exposure(open_positions)
        equity_base = total_equity if total_equity > 0 else initial_capital
        gross_ratio = (exposure["gross"] / equity_base) if equity_base else 0.0
        net_ratio = (exposure["net"] / equity_base) if equity_base else 0.0
        largest_ratio = (exposure["largest"] / equity_base) if equity_base else 0.0
        concentration_top3_ratio = (exposure["top3"] / equity_base) if equity_base else 0.0
        long_count = sum(1 for p in open_positions if str(p.get("direction") or "").lower() == "long")
        short_count = sum(1 for p in open_positions if str(p.get("direction") or "").lower() == "short")

        sector_exposures: list[dict[str, Any]] = []
        for sector, row in exposure["sectors"].items():
            gross = float(row.get("gross") or 0.0)
            long_val = float(row.get("long") or 0.0)
            short_val = float(row.get("short") or 0.0)
            sector_exposures.append(
                {
                    "sector": sector,
                    "long_exposure": round(long_val, 2),
                    "short_exposure": round(short_val, 2),
                    "gross_exposure": round(gross, 2),
                    "net_exposure": round(long_val - short_val, 2),
                    "gross_exposure_ratio": round((gross / equity_base) if equity_base else 0.0, 4),
                    "names": int(row.get("names") or 0),
                }
            )
        sector_exposures.sort(key=lambda r: float(r.get("gross_exposure") or 0.0), reverse=True)

        top_sector_ratio = float(sector_exposures[0]["gross_exposure_ratio"]) if sector_exposures else 0.0
        risk_flags: list[str] = []
        if gross_ratio > HEDGE_MAX_GROSS_EXPOSURE_RATIO:
            risk_flags.append("gross_exposure_over_mandate")
        if abs(net_ratio) > HEDGE_MAX_NET_EXPOSURE_RATIO:
            risk_flags.append("net_exposure_over_mandate")
        if largest_ratio > HEDGE_MAX_SINGLE_NAME_RATIO:
            risk_flags.append("single_name_over_mandate")
        if top_sector_ratio > HEDGE_MAX_SECTOR_EXPOSURE_RATIO:
            risk_flags.append("sector_concentration_over_mandate")
        if not risk_flags and open_positions:
            risk_flags.append("inside_hedge_mandate")

        if abs(net_ratio) < 0.05:
            book_bias = "balanced"
        elif net_ratio > 0:
            book_bias = "net_long"
        else:
            book_bias = "net_short"

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
            "long_positions": long_count,
            "short_positions": short_count,
            "long_exposure": exposure["long"],
            "short_exposure": exposure["short"],
            "gross_exposure": exposure["gross"],
            "net_exposure": exposure["net"],
            "gross_exposure_ratio": round(gross_ratio, 4),
            "net_exposure_ratio": round(net_ratio, 4),
            "largest_position_exposure": exposure["largest"],
            "largest_position_ratio": round(largest_ratio, 4),
            "concentration_top3_ratio": round(concentration_top3_ratio, 4),
            "risk_budget_usage_ratio": round(
                min(1.0, gross_ratio / HEDGE_MAX_GROSS_EXPOSURE_RATIO),
                4,
            ),
            "book_bias": book_bias,
            "sector_exposures": sector_exposures,
            "risk_flags": risk_flags,
            "mandate": {
                "strategy": "cbe_long_short_hedge_fund",
                "max_gross_exposure_ratio": HEDGE_MAX_GROSS_EXPOSURE_RATIO,
                "max_net_exposure_ratio": HEDGE_MAX_NET_EXPOSURE_RATIO,
                "max_single_name_ratio": HEDGE_MAX_SINGLE_NAME_RATIO,
                "max_sector_exposure_ratio": HEDGE_MAX_SECTOR_EXPOSURE_RATIO,
                "rebalance": "weekly",
                "min_hold_trading_days": MIN_HOLD_TRADING_DAYS,
            },
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
