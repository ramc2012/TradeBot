"""Risk management — applied to both paper and live trading.

Enhanced with circuit breakers per STRATEGY_DOCUMENT.md §9:
- Consecutive stop counter (3 stops → pause 5 trading days)
- Portfolio drawdown tracking (>15% → reduce to cautious sizing)
- Sector concentration limits (max 3 per sector)
- Dead zone universe monitoring
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Optional

from loguru import logger

from agent.strategy_config import CIRCUIT, MAX_SIMULTANEOUS_POSITIONS


@dataclass
class RiskConfig:
    max_loss_per_trade: float = 5_000.0
    max_daily_loss: float = 15_000.0
    max_open_positions: int = MAX_SIMULTANEOUS_POSITIONS
    concentration_limit: float = 0.40   # 40% of deployed capital
    max_delta: float = 500.0
    max_vega: float = 10_000.0
    max_sector_positions: int = 3       # max positions in same sector


@dataclass
class RiskState:
    daily_loss: float = 0.0
    trading_disabled: bool = False
    open_position_count: int = 0
    last_reset: date = field(default_factory=date.today)
    # Circuit breaker state
    consecutive_stops: int = 0
    paused_until: Optional[date] = None
    peak_equity: float = 0.0
    current_equity: float = 0.0
    drawdown_pct: float = 0.0
    sizing_mode: str = "normal"  # "normal", "cautious", "cash"
    # Sector tracking
    sector_positions: Dict[str, int] = field(default_factory=lambda: defaultdict(int))


# Basic NSE sector mapping for F&O underlyings
SECTOR_MAP: Dict[str, str] = {
    "RELIANCE": "energy", "ONGC": "energy", "BPCL": "energy", "IOC": "energy",
    "GAIL": "energy", "HINDPETRO": "energy",
    "HDFCBANK": "banks", "ICICIBANK": "banks", "SBIN": "banks", "KOTAKBANK": "banks",
    "AXISBANK": "banks", "BANKBARODA": "banks", "INDUSINDBK": "banks", "PNB": "banks",
    "IDFCFIRSTB": "banks", "BANDHANBNK": "banks", "FEDERALBNK": "banks",
    "INFY": "it", "TCS": "it", "WIPRO": "it", "HCLTECH": "it", "TECHM": "it",
    "LTIM": "it", "MPHASIS": "it", "COFORGE": "it", "PERSISTENT": "it",
    "TATAMOTORS": "auto", "MARUTI": "auto", "M&M": "auto", "BAJAJ-AUTO": "auto",
    "HEROMOTOCO": "auto", "EICHERMOT": "auto", "ASHOKLEY": "auto", "TVSMOTOR": "auto",
    "BHARTIARTL": "telecom", "IDEA": "telecom",
    "SUNPHARMA": "pharma", "DRREDDY": "pharma", "CIPLA": "pharma",
    "DIVISLAB": "pharma", "APOLLOHOSP": "pharma", "BIOCON": "pharma",
    "TATASTEEL": "metals", "JSWSTEEL": "metals", "HINDALCO": "metals",
    "VEDL": "metals", "COALINDIA": "metals", "NMDC": "metals",
}


def _get_sector(underlying: str) -> str:
    return SECTOR_MAP.get(underlying, "other")


class RiskManager:
    """Enforces pre-trade and post-trade risk checks with circuit breakers."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self._state = RiskState()
        self._deployed_capital: Dict[str, float] = {}  # symbol → deployed

    def reset_daily(self):
        """Call at market open to reset daily counters."""
        self._state.daily_loss = 0.0
        self._state.trading_disabled = False
        self._state.last_reset = date.today()
        # Check if pause period has expired
        if self._state.paused_until and date.today() >= self._state.paused_until:
            self._state.paused_until = None
            self._state.consecutive_stops = 0
            logger.info("[Risk] Consecutive-stop pause expired, trading resumed")
        logger.info("[Risk] Daily counters reset")

    def update_config(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)

    def update_equity(self, current_equity: float):
        """Update portfolio equity for drawdown tracking."""
        if current_equity > self._state.peak_equity:
            self._state.peak_equity = current_equity
        self._state.current_equity = current_equity

        if self._state.peak_equity > 0:
            self._state.drawdown_pct = (
                (self._state.peak_equity - current_equity) / self._state.peak_equity * 100.0
            )
        else:
            self._state.drawdown_pct = 0.0

        # Update sizing mode based on drawdown
        if self._state.drawdown_pct >= CIRCUIT.max_portfolio_drawdown_pct:
            if self._state.sizing_mode != "cautious":
                self._state.sizing_mode = "cautious"
                logger.warning(
                    f"[Risk] Drawdown {self._state.drawdown_pct:.1f}% ≥ "
                    f"{CIRCUIT.max_portfolio_drawdown_pct}%. Switching to CAUTIOUS sizing."
                )
        elif self._state.sizing_mode == "cautious" and self._state.drawdown_pct < 10.0:
            self._state.sizing_mode = "normal"
            logger.info("[Risk] Drawdown recovered below 10%. Back to normal sizing.")

    def update_dead_zone_pct(self, dead_zone_pct: float):
        """Update the percentage of F&O universe in dead zone.

        If ≥70% of underlyings are in dead zone → cash mode.
        """
        if dead_zone_pct >= CIRCUIT.dead_zone_universe_pct:
            if self._state.sizing_mode != "cash":
                self._state.sizing_mode = "cash"
                logger.warning(
                    f"[Risk] {dead_zone_pct:.0f}% of universe in dead zone. CASH MODE."
                )
        elif self._state.sizing_mode == "cash" and dead_zone_pct < 50.0:
            self._state.sizing_mode = "normal"
            logger.info("[Risk] Dead zone universe dropped below 50%. Exiting cash mode.")

    # ── Pre-trade Checks ────────────────────────────────────────────────────

    def check_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        price: float,
        sl: Optional[float] = None,
        total_capital: float = 1_000_000.0,
        underlying: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Returns (allowed: bool, reason: str)."""
        # Self-reset if new day
        if self._state.last_reset != date.today():
            self.reset_daily()

        if self._state.trading_disabled:
            return False, "Trading disabled: daily loss limit breached"

        # Circuit breaker: consecutive stops pause
        if self._state.paused_until and date.today() < self._state.paused_until:
            return False, (
                f"Trading paused until {self._state.paused_until} "
                f"({self._state.consecutive_stops} consecutive stops)"
            )

        # Circuit breaker: cash mode
        if self._state.sizing_mode == "cash" and action == "BUY":
            return False, "Cash mode active — no new entries (dead zone ≥70%)"

        if self._state.open_position_count >= self.config.max_open_positions:
            return False, f"Max open positions ({self.config.max_open_positions}) reached"

        # Sector concentration check
        if underlying:
            sector = _get_sector(underlying)
            if self._state.sector_positions.get(sector, 0) >= self.config.max_sector_positions:
                return False, (
                    f"Sector limit: already {self._state.sector_positions[sector]} "
                    f"positions in {sector} (max {self.config.max_sector_positions})"
                )

        # Max loss per trade check
        if sl is not None and price > 0:
            potential_loss = abs(price - sl) * qty
            if potential_loss > self.config.max_loss_per_trade:
                return False, (
                    f"Trade risk ₹{potential_loss:.0f} exceeds max ₹{self.config.max_loss_per_trade:.0f}"
                )

        # Concentration check
        order_value = price * qty
        symbol_deployed = self._deployed_capital.get(symbol, 0.0) + order_value
        if symbol_deployed > total_capital * self.config.concentration_limit:
            return False, (
                f"Concentration limit: {symbol} would be "
                f"{symbol_deployed / total_capital:.1%} of capital"
            )

        return True, "OK"

    # ── Post-trade Updates ───────────────────────────────────────────────────

    def on_trade_close(
        self,
        symbol: str,
        pnl: float,
        capital_released: float,
        exit_reason: str = "",
        underlying: Optional[str] = None,
    ):
        if pnl < 0:
            self._state.daily_loss += abs(pnl)
            if self._state.daily_loss >= self.config.max_daily_loss:
                self._state.trading_disabled = True
                logger.warning(
                    f"[Risk] Daily loss limit breached: ₹{self._state.daily_loss:.0f} "
                    f"≥ ₹{self.config.max_daily_loss:.0f}. Trading DISABLED."
                )

        # Consecutive stops tracking
        if exit_reason in ("hard_stop", "trailing_stoploss"):
            self._state.consecutive_stops += 1
            if self._state.consecutive_stops >= CIRCUIT.max_consecutive_stops:
                self._state.paused_until = date.today() + timedelta(
                    days=CIRCUIT.pause_days_after_stops
                )
                logger.warning(
                    f"[Risk] {self._state.consecutive_stops} consecutive stops! "
                    f"Pausing new entries until {self._state.paused_until}."
                )
        else:
            # Reset consecutive counter on a non-stop exit
            self._state.consecutive_stops = 0

        self._deployed_capital[symbol] = max(
            0.0, self._deployed_capital.get(symbol, 0.0) - capital_released
        )
        self._state.open_position_count = max(0, self._state.open_position_count - 1)

        # Update sector tracking
        if underlying:
            sector = _get_sector(underlying)
            self._state.sector_positions[sector] = max(
                0, self._state.sector_positions.get(sector, 0) - 1
            )

    def on_position_opened(
        self,
        symbol: str,
        capital_deployed: float,
        underlying: Optional[str] = None,
    ):
        self._deployed_capital[symbol] = (
            self._deployed_capital.get(symbol, 0.0) + capital_deployed
        )
        self._state.open_position_count += 1

        if underlying:
            sector = _get_sector(underlying)
            self._state.sector_positions[sector] = (
                self._state.sector_positions.get(sector, 0) + 1
            )

    @property
    def is_trading_allowed(self) -> bool:
        if self._state.trading_disabled:
            return False
        if self._state.paused_until and date.today() < self._state.paused_until:
            return False
        if self._state.sizing_mode == "cash":
            return False
        return True

    @property
    def sizing_mode(self) -> str:
        return self._state.sizing_mode

    def get_status(self) -> dict:
        return {
            "trading_allowed": self.is_trading_allowed,
            "daily_loss": round(self._state.daily_loss, 2),
            "max_daily_loss": self.config.max_daily_loss,
            "open_positions": self._state.open_position_count,
            "max_positions": self.config.max_open_positions,
            "sizing_mode": self._state.sizing_mode,
            "circuit_breakers": {
                "consecutive_stops": self._state.consecutive_stops,
                "paused_until": str(self._state.paused_until) if self._state.paused_until else None,
                "drawdown_pct": round(self._state.drawdown_pct, 2),
                "peak_equity": round(self._state.peak_equity, 2),
            },
            "sector_positions": dict(self._state.sector_positions),
            "config": {
                "max_loss_per_trade": self.config.max_loss_per_trade,
                "max_daily_loss": self.config.max_daily_loss,
                "max_open_positions": self.config.max_open_positions,
                "concentration_limit": self.config.concentration_limit,
                "max_sector_positions": self.config.max_sector_positions,
            },
        }
