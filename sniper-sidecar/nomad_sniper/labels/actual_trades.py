"""Label the user's actual historical round trips with net P&L and quality buckets.

> **DEMOTED — validation overlay only (contract §7).** Realized trades are a *censored*
> sample and are NO LONGER a training target. The training target is directional-move
> detection on a time grid (`labels/directional.py`). This module is retained solely to
> compute the realized-trade *agreement overlay*: for each actual trade, did the model (at
> that timestamp) call `up`/`down` in the trade's direction on winners and `none`/opposite
> on losers? Reported as agreement rates, never mixed into training.
>
> Do not import this from any training/feature-grid path. See `cli.py validate-overlay`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from nomad_sniper.data.round_trips import RoundTrip
from nomad_sniper.labels.cost_model import CostModel, ZerodhaFnoCostModel
from nomad_sniper.utils.logging import get_logger

log = get_logger()


@dataclass
class TradeLabel:
    trade_id: str  # entry trade id, used as the join key with features
    symbol: str
    direction: Literal["long", "short"]
    entry_at: pd.Timestamp
    exit_at: pd.Timestamp
    gross_pnl: float
    total_cost: float
    net_pnl: float
    quantity: int
    holding_seconds: int
    is_winner: int          # 1 if net_pnl > 0 else 0
    is_loser: int           # 1 if net_pnl < 0 else 0
    pnl_quartile: int       # 1 (worst) .. 4 (best), computed across the full set
    pnl_decile: int         # 1 (worst) .. 10 (best)


def label_actual_trades(
    round_trips: list[RoundTrip],
    cost_model: CostModel | None = None,
    *,
    infer_instrument_type=None,
) -> pd.DataFrame:
    """Compute net P&L and quality buckets for each round trip.

    Args:
        round_trips: List of RoundTrips from `pair_round_trips()`.
        cost_model:  Cost model. Defaults to ZerodhaFnoCostModel().
        infer_instrument_type: Callable(symbol)->'future'|'option'. Defaults to a simple
                               substring check on the symbol.
    """
    cost_model = cost_model or ZerodhaFnoCostModel()
    infer_instrument_type = infer_instrument_type or _default_infer_instrument_type

    rows = []
    for rt in round_trips:
        inst = infer_instrument_type(rt.symbol)
        costs = cost_model.compute(
            instrument_type=inst,
            direction=rt.direction,
            entry_price=rt.entry_price,
            exit_price=rt.exit_price,
            quantity=rt.quantity,
        )
        net = rt.gross_pnl - costs.total
        rows.append({
            "trade_id": rt.entry_trade_id,
            "symbol": rt.symbol,
            "direction": rt.direction,
            "entry_at": rt.entry_at,
            "exit_at": rt.exit_at,
            "gross_pnl": rt.gross_pnl,
            "total_cost": costs.total,
            "net_pnl": net,
            "quantity": rt.quantity,
            "holding_seconds": rt.holding_seconds,
            "is_winner": int(net > 0),
            "is_loser": int(net < 0),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        log.warning("No labeled trades produced.")
        return df

    df["pnl_quartile"] = _safe_qcut(df["net_pnl"], 4)
    df["pnl_decile"] = _safe_qcut(df["net_pnl"], 10)

    log.info(
        f"Labeled {len(df)} round trips. Net P&L: ₹{df['net_pnl'].sum():,.0f} "
        f"(gross ₹{df['gross_pnl'].sum():,.0f}, costs ₹{df['total_cost'].sum():,.0f}). "
        f"Win rate: {df['is_winner'].mean():.1%}"
    )
    return df.set_index("trade_id")


def _default_infer_instrument_type(symbol: str) -> str:
    """Crude inference: anything ending in CE/PE is an option, else a future.

    NSE F&O symbol conventions:
        NIFTY25FEBFUT  → future
        NIFTY25FEB22000CE → call option
        BANKNIFTY25JAN48000PE → put option
    """
    s = symbol.upper()
    if s.endswith("CE") or s.endswith("PE"):
        return "option"
    return "future"


def _safe_qcut(s: pd.Series, q: int) -> pd.Series:
    """qcut with deduplicated edges; falls back to rank-based binning on collisions."""
    try:
        return pd.qcut(s.rank(method="first"), q=q, labels=range(1, q + 1)).astype(int)
    except ValueError:
        # Last-resort: equal-frequency by rank
        ranks = s.rank(method="first", pct=True)
        return ((ranks * q).clip(upper=q - 1e-9).astype(int) + 1).astype(int)
