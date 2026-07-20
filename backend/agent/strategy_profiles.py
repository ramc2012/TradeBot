"""Strategy contract profiles — declarative specs that tell Market
Intelligence how to resolve contracts and strikes for a given strategy.

Each strategy has different contract-selection needs:

  * S1 (30-min MACD on ATM options): monthly expiry, ≥1 trading day
    left for indices (T-1 entry OK, T-0 skipped), monthly rollover
    when stocks have ≤3 trading days left on the active expiry. Wider
    strike-neighbour search (±2) and looser lift threshold (1.5×) so
    less-liquid stocks don't get pinned to thin literal-ATM strikes.

  * S2 (5-min MACD + MP on indices): weekly expiry, expiry-day trading
    allowed, narrower strike search (±1) and stricter lift threshold
    (2.0×) because intraday convexity decisions need tight liquidity.

Profiles let the Market Intelligence layer serve each strategy the
contract it actually needs, instead of forcing every strategy to share
a single watchlist build keyed to monthly-expiry / S1 conventions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StrategyContractProfile:
    """Per-strategy contract-selection rules for Market Intelligence."""

    name: str
    # Which expiry the strategy wants by default.
    index_expiry: Literal["weekly", "monthly"]
    stock_expiry: Literal["weekly", "monthly"]
    # Index T-0 (expiry day) behaviour.
    index_allow_t0: bool
    # Stocks: roll active monthly forward when trading-days-remaining
    # is at or below this number. Set to 0 to disable rollover.
    stock_rollover_td: int
    # Strike-picker tuning. neighbours = how far around the side-anchor
    # to search. lift_threshold = required volume lift before we switch
    # from the side-anchor strike to a more-liquid neighbour.
    strike_neighbours: int = 2
    strike_lift_threshold: float = 1.5
    # When True, CE biases to at-or-above spot and PE biases to
    # at-or-below spot. When False, both sides target the same
    # literal-ATM strike (rare; useful for straddle strategies if we
    # ever add one).
    strike_asymmetric: bool = True


# ── S1 — 30-minute MACD zero-cross on ATM option premiums ───────────────────
S1_CONTRACT_PROFILE = StrategyContractProfile(
    name="s1_monthly_macd",
    index_expiry="monthly",
    stock_expiry="monthly",
    index_allow_t0=False,        # T-1 OK, T-0 skipped (theta annihilation)
    # OWNER-DIRECTED CHANGE 2026-07-20: 3 → 5 trading days. Indian single-stock
    # options are PHYSICALLY SETTLED; rolling the WATCHLIST five trading days
    # out means no NEW position is ever opened inside the compulsory-delivery
    # window. This is instrument SELECTION, not a strategy gate — the entry
    # gate MIN_TTE_DAYS_STOCK in agent/strategy_config.py is untouched.
    #
    # The literal stays at the LEGACY 3 on purpose. The effective horizon is
    # read at use-time from settings.EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS
    # whenever EXPIRY_POLICY_ENABLED (see atm_watchlist._stock_roll_horizon),
    # so the owner's 5-day roll lands WITH the rest of this pass under one
    # flag instead of firing silently on the next restart. That matters: on
    # 2026-07-21 the July monthly is exactly 5 trading days out, so an
    # unconditional 5 would have rolled the stock watchlist to August on the
    # very first restart while 75 open July stock positions were still live.
    stock_rollover_td=3,         # legacy default; 5 arrives via EXPIRY_POLICY_ENABLED
    strike_neighbours=2,
    strike_lift_threshold=1.5,
    strike_asymmetric=True,
)

# ── S2 — 5-minute MACD + Market Profile on indices ─────────────────────────
S2_CONTRACT_PROFILE = StrategyContractProfile(
    name="s2_weekly_macd_mp",
    index_expiry="weekly",       # weekly contracts (NIFTY/SENSEX Tuesday)
    stock_expiry="monthly",      # unused — S2 is indices-only
    index_allow_t0=True,         # S2 explicitly trades on expiry day
    stock_rollover_td=0,         # n/a for S2
    strike_neighbours=1,         # tighter intraday band
    strike_lift_threshold=2.0,   # stricter quality bar for fast trades
    strike_asymmetric=True,
)


PROFILES_BY_NAME: dict[str, StrategyContractProfile] = {
    S1_CONTRACT_PROFILE.name: S1_CONTRACT_PROFILE,
    S2_CONTRACT_PROFILE.name: S2_CONTRACT_PROFILE,
}


__all__ = [
    "StrategyContractProfile",
    "S1_CONTRACT_PROFILE",
    "S2_CONTRACT_PROFILE",
    "PROFILES_BY_NAME",
]
