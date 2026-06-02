"""Paper trading portfolio — tracks virtual positions and P&L."""
from __future__ import annotations
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import numpy as np
from loguru import logger

from paper_engine.order_book import PaperOrder


@dataclass
class VirtualPosition:
    symbol: str
    action: str          # BUY (long) or SELL (short)
    qty: int
    avg_price: float
    current_price: float = 0.0
    instrument_type: str = "CE"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    signal_id: Optional[str] = None
    setup_type: Optional[str] = None
    entry_iv_pct: Optional[float] = None
    regime: Optional[str] = None
    opened_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def unrealized_pnl(self) -> float:
        multiplier = 1 if self.action == "BUY" else -1
        return multiplier * (self.current_price - self.avg_price) * self.qty

    @property
    def value(self) -> float:
        return self.current_price * self.qty


@dataclass
class TradeRecord:
    symbol: str
    action: str
    qty: int
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: datetime
    exit_time: datetime
    instrument_type: str = "CE"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    signal_id: Optional[str] = None
    setup_type: Optional[str] = None
    entry_iv_pct: Optional[float] = None
    regime: Optional[str] = None


class PaperPortfolio:
    """Virtual portfolio that tracks positions, P&L, and performance metrics."""

    LOT_SIZES = {
        "NIFTY":       65,   # NSE-mandated (verified Apr 2026)
        "BANKNIFTY":   30,
        "FINNIFTY":    60,
        "MIDCPNIFTY":  75,
        "NIFTYNXT50":  25,
        "SENSEX":      10,
        "BANKEX":      15,
    }
    DEFAULT_LOT_SIZE = 1   # Emergency fallback only — all underlyings resolved from DB

    def __init__(self, initial_capital: float = 1_000_000.0, session_id: Optional[str] = None):
        self.initial_capital = initial_capital
        self.available_capital = initial_capital
        self.session_id = session_id
        self._positions: Dict[str, VirtualPosition] = {}  # key = order_id
        self._trade_history: List[TradeRecord] = []
        self._daily_pnl: Dict[date, float] = defaultdict(float)
        self._equity_curve: List[tuple[datetime, float]] = []
        self._peak_equity = initial_capital

    # ── Position Management ──────────────────────────────────────────────────

    def on_fill(self, order: PaperOrder):
        """Called by PaperOrderBook when an order fills."""
        fill_price = order.fill_price or 0
        margin_used = self._estimate_margin(
            order.symbol,
            order.qty,
            fill_price,
            instrument_type=order.instrument_type,
            option_type=order.option_type,
        )

        # Check if we have an existing position for this symbol in same direction
        existing_key = self._find_position_key(order.symbol, order.action)

        if existing_key:
            pos = self._positions[existing_key]
            # Average in
            total_qty = pos.qty + order.qty
            pos.avg_price = (pos.avg_price * pos.qty + fill_price * order.qty) / total_qty
            pos.qty = total_qty
        else:
            # Check if closing an opposing position
            opp_action = "SELL" if order.action == "BUY" else "BUY"
            opp_key = self._find_position_key(order.symbol, opp_action)
            if opp_key:
                self._close_position(opp_key, order)
                return

            # Open new position
            self._positions[order.order_id] = VirtualPosition(
                symbol=order.symbol,
                action=order.action,
                qty=order.qty,
                avg_price=fill_price,
                current_price=fill_price,
                instrument_type=order.instrument_type,
                expiry=order.expiry,
                strike=order.strike,
                option_type=order.option_type,
                signal_id=order.signal_id,
                setup_type=order.setup_type,
                entry_iv_pct=order.entry_iv_pct,
                regime=order.regime,
                opened_at=order.fill_time or datetime.now(timezone.utc),
            )
            self.available_capital -= margin_used
            logger.info(f"[Portfolio] Opened position: {order.symbol} {order.action} {order.qty} @ {fill_price}")

    def _close_position(self, pos_key: str, close_order: PaperOrder):
        pos = self._positions[pos_key]
        fill_price = close_order.fill_price or 0
        multiplier = 1 if pos.action == "BUY" else -1
        pnl = multiplier * (fill_price - pos.avg_price) * close_order.qty

        trade = TradeRecord(
            symbol=pos.symbol,
            action=pos.action,
            qty=close_order.qty,
            entry_price=pos.avg_price,
            exit_price=fill_price,
            pnl=pnl,
            entry_time=pos.opened_at,
            exit_time=close_order.fill_time or datetime.now(timezone.utc),
            instrument_type=pos.instrument_type,
            expiry=pos.expiry,
            strike=pos.strike,
            option_type=pos.option_type,
            signal_id=pos.signal_id,
            setup_type=pos.setup_type,
            entry_iv_pct=pos.entry_iv_pct,
            regime=pos.regime,
        )
        self._trade_history.append(trade)

        trade_day = trade.exit_time.date()
        self._daily_pnl[trade_day] = self._daily_pnl[trade_day] + pnl

        margin_released = self._estimate_margin(
            pos.symbol,
            close_order.qty,
            pos.avg_price,
            instrument_type=pos.instrument_type,
            option_type=pos.option_type,
        )
        self.available_capital += margin_released + pnl

        if close_order.qty >= pos.qty:
            del self._positions[pos_key]
        else:
            pos.qty -= close_order.qty

        # Record equity
        equity = self.total_equity
        self._equity_curve.append((datetime.now(timezone.utc), equity))
        if equity > self._peak_equity:
            self._peak_equity = equity

        logger.info(f"[Portfolio] Closed position: {pos.symbol} PnL={pnl:.2f}")

    def update_prices(self, price_map: dict[str, float]):
        """Update mark-to-market prices for all open positions."""
        for pos in self._positions.values():
            if pos.symbol in price_map:
                pos.current_price = price_map[pos.symbol]

    def reserved_margin(self) -> float:
        """Capital reserved against currently open paper positions."""
        return sum(
            self._estimate_margin(
                pos.symbol,
                pos.qty,
                pos.avg_price,
                instrument_type=pos.instrument_type,
                option_type=pos.option_type,
            )
            for pos in self._positions.values()
        )

    def reconcile_available_capital(self) -> None:
        """Rebuild cash from realized P&L and currently reserved entry margin."""
        self.available_capital = self.initial_capital + self.realized_pnl - self.reserved_margin()

    def _find_position_key(self, symbol: str, action: str) -> Optional[str]:
        for key, pos in self._positions.items():
            if pos.symbol == symbol and pos.action == action:
                return key
        return None

    def _estimate_margin(
        self,
        symbol: str,
        qty: int,
        price: float,
        *,
        instrument_type: Optional[str] = None,
        option_type: Optional[str] = None,
    ) -> float:
        """Approximate SPAN margin = lot_size × price × margin_pct."""
        token = str(option_type or instrument_type or "").upper()
        if token in {"CE", "PE", "OPT"}:
            return max(price, 0.0) * max(qty, 0)
        base = symbol.split(":")[1] if ":" in symbol else symbol[:10].rstrip("0123456789CEPEF")
        lot_size = self.LOT_SIZES.get(base, self.DEFAULT_LOT_SIZE)
        margin_pct = 0.15  # 15% approximate
        return price * qty * margin_pct

    # ── P&L Metrics ──────────────────────────────────────────────────────────

    @property
    def total_equity(self) -> float:
        return self.available_capital + self.reserved_margin() + self.unrealized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self._trade_history)

    @property
    def day_realized_pnl(self) -> float:
        """P&L from trades CLOSED today. Distinct from `day_pnl`, which
        also includes today's mark-to-market on still-open positions."""
        return self._daily_pnl.get(date.today(), 0.0)

    @property
    def day_pnl(self) -> float:
        """Today's TOTAL P&L change = realized-today + current unrealized MTM.

        This is what a trader means by "Day P&L" — what the account moved
        today, whether the move came from closed trades or from open
        positions riding intra-day. Previously this returned realized-today
        only, which left the UI showing open P&L under "Day P&L" as a
        workaround. Lifetime realized P&L remains exposed on `realized_pnl`."""
        return self.day_realized_pnl + self.unrealized_pnl

    @property
    def max_drawdown(self) -> float:
        if not self._equity_curve:
            return 0.0
        values = [v for _, v in self._equity_curve]
        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def win_rate(self) -> float:
        wins = [t for t in self._trade_history if t.pnl > 0]
        return len(wins) / len(self._trade_history) if self._trade_history else 0.0

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self._trade_history if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self._trade_history if t.pnl < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self._trade_history if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self._trade_history if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    def sharpe_ratio(self, rolling_days: int = 30) -> float:
        """Rolling Sharpe ratio using daily P&L."""
        today = date.today()
        daily = []
        for i in range(rolling_days):
            from datetime import timedelta
            d = today - timedelta(days=i)
            daily.append(self._daily_pnl.get(d, 0.0))
        if not daily or np.std(daily) == 0:
            return 0.0
        return (np.mean(daily) / np.std(daily)) * math.sqrt(252)

    def get_positions_list(self) -> List[dict]:
        return [
            {
                "symbol": p.symbol,
                "action": p.action,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "ltp": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "instrument_type": p.instrument_type,
                "expiry": p.expiry,
                "strike": p.strike,
                "option_type": p.option_type,
            }
            for p in self._positions.values()
        ]

    def get_summary(self) -> dict:
        def safe_round(value: float, digits: int = 2) -> float | None:
            if not math.isfinite(value):
                return None
            return round(value, digits)

        # Self-correct any drift between `available_capital` (mutated
        # incrementally by on_fill / close_position) and the canonical
        # derivation `initial + realized − reserved`. Drift can accumulate
        # when partial closes round differently, when reset wipes some but
        # not all accounting state, or when `_estimate_margin` evaluates
        # at a different `avg_price` than the original fill basis. Reading
        # is the right place: zero side effects on the trade path, and the
        # dashboard always sees coherent numbers.
        self.reconcile_available_capital()

        return {
            "session_id": str(self.session_id),
            "initial_capital": safe_round(self.initial_capital, 2),
            "available_capital": safe_round(self.available_capital, 2),
            "total_equity": safe_round(self.total_equity, 2),
            "unrealized_pnl": safe_round(self.unrealized_pnl, 2),
            "realized_pnl": safe_round(self.realized_pnl, 2),
            # P&L split (2026-06-02): UI now shows Day P&L (realized-today +
            # open MTM) AND lifetime realized P&L as separate cards.
            "day_pnl": safe_round(self.day_pnl, 2),
            "day_realized_pnl": safe_round(self.day_realized_pnl, 2),
            "realized_pnl_lifetime": safe_round(self.realized_pnl, 2),
            "total_trades": len(self._trade_history),
            "win_rate": safe_round(self.win_rate, 4),
            "avg_win": safe_round(self.avg_win, 2),
            "avg_loss": safe_round(self.avg_loss, 2),
            "profit_factor": safe_round(self.profit_factor, 2),
            "max_drawdown": safe_round(self.max_drawdown, 4),
            "sharpe_ratio": safe_round(self.sharpe_ratio(), 4),
        }

    def snapshot_equity(self) -> None:
        """Capture a timestamped equity snapshot (call after each strategy scan)."""
        equity = self.total_equity
        self._equity_curve.append((datetime.now(timezone.utc), equity))
        if equity > self._peak_equity:
            self._peak_equity = equity

    async def persist_equity_to_redis(self) -> None:
        """Persist the equity curve + key portfolio state to Redis for restart survival."""
        try:
            from db.redis_client import get_redis
            import json
            redis = await get_redis()
            key = f"paper_portfolio:equity:{self.session_id}"
            # Store last 2000 snapshots (covers ~33 hours at 60s interval)
            data = {
                "initial_capital": self.initial_capital,
                "available_capital": self.available_capital,
                "peak_equity": self._peak_equity,
                "daily_pnl": {str(k): v for k, v in self._daily_pnl.items()},
                "trade_count": len(self._trade_history),
                "equity_curve": [
                    {"time": t.isoformat(), "equity": round(v, 2)}
                    for t, v in self._equity_curve[-2000:]
                ],
            }
            await redis.set(key, json.dumps(data), ex=86400 * 7)  # 7 day TTL
        except Exception as exc:
            logger.debug(f"[Portfolio] Redis equity persist failed: {exc}")

    async def restore_from_redis(self) -> bool:
        """Restore equity curve from Redis after a restart. Returns True if restored."""
        try:
            from db.redis_client import get_redis
            import json
            redis = await get_redis()
            key = f"paper_portfolio:equity:{self.session_id}"
            cached = await redis.get(key)
            if not cached:
                return False
            data = json.loads(cached)
            self._peak_equity = float(data.get("peak_equity", self.initial_capital))
            for item in data.get("equity_curve", []):
                t = datetime.fromisoformat(item["time"])
                v = float(item["equity"])
                self._equity_curve.append((t, v))
            # Restore daily P&L
            for d_str, pnl in data.get("daily_pnl", {}).items():
                try:
                    d = date.fromisoformat(d_str)
                    self._daily_pnl[d] = float(pnl)
                except ValueError:
                    pass
            logger.info(
                f"[Portfolio] Restored {len(self._equity_curve)} equity points from Redis"
            )
            return True
        except Exception as exc:
            logger.debug(f"[Portfolio] Redis equity restore failed: {exc}")
            return False

    def get_equity_curve(self) -> list[dict]:
        """Return equity curve as list of {time, equity} dicts for charting."""
        return [
            {"time": t.isoformat(), "equity": round(v, 2)}
            for t, v in self._equity_curve
        ]
