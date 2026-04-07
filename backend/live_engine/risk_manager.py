"""Risk management — applied to both paper and live trading."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional
from loguru import logger


@dataclass
class RiskConfig:
    max_loss_per_trade: float = 5_000.0
    max_daily_loss: float = 15_000.0
    max_open_positions: int = 5
    concentration_limit: float = 0.40   # 40% of deployed capital
    max_delta: float = 500.0
    max_vega: float = 10_000.0


@dataclass
class RiskState:
    daily_loss: float = 0.0
    trading_disabled: bool = False
    open_position_count: int = 0
    last_reset: date = field(default_factory=date.today)


class RiskManager:
    """Enforces pre-trade and post-trade risk checks."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self._state = RiskState()
        self._deployed_capital: Dict[str, float] = {}  # symbol → deployed

    def reset_daily(self):
        """Call at market open to reset daily counters."""
        self._state.daily_loss = 0.0
        self._state.trading_disabled = False
        self._state.last_reset = date.today()
        logger.info("[Risk] Daily counters reset")

    def update_config(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)

    # ── Pre-trade Checks ────────────────────────────────────────────────────

    def check_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        price: float,
        sl: Optional[float] = None,
        total_capital: float = 1_000_000.0,
    ) -> tuple[bool, str]:
        """Returns (allowed: bool, reason: str)."""
        # Self-reset if new day
        if self._state.last_reset != date.today():
            self.reset_daily()

        if self._state.trading_disabled:
            return False, "Trading disabled: daily loss limit breached"

        if self._state.open_position_count >= self.config.max_open_positions:
            return False, f"Max open positions ({self.config.max_open_positions}) reached"

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

    def on_trade_close(self, symbol: str, pnl: float, capital_released: float):
        if pnl < 0:
            self._state.daily_loss += abs(pnl)
            if self._state.daily_loss >= self.config.max_daily_loss:
                self._state.trading_disabled = True
                logger.warning(
                    f"[Risk] Daily loss limit breached: ₹{self._state.daily_loss:.0f} "
                    f"≥ ₹{self.config.max_daily_loss:.0f}. Trading DISABLED."
                )

        self._deployed_capital[symbol] = max(
            0.0, self._deployed_capital.get(symbol, 0.0) - capital_released
        )
        self._state.open_position_count = max(0, self._state.open_position_count - 1)

    def on_position_opened(self, symbol: str, capital_deployed: float):
        self._deployed_capital[symbol] = (
            self._deployed_capital.get(symbol, 0.0) + capital_deployed
        )
        self._state.open_position_count += 1

    @property
    def is_trading_allowed(self) -> bool:
        return not self._state.trading_disabled

    def get_status(self) -> dict:
        return {
            "trading_allowed": self.is_trading_allowed,
            "daily_loss": round(self._state.daily_loss, 2),
            "max_daily_loss": self.config.max_daily_loss,
            "open_positions": self._state.open_position_count,
            "max_positions": self.config.max_open_positions,
            "config": {
                "max_loss_per_trade": self.config.max_loss_per_trade,
                "max_daily_loss": self.config.max_daily_loss,
                "max_open_positions": self.config.max_open_positions,
                "concentration_limit": self.config.concentration_limit,
            },
        }
