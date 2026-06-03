"""Add total order-book buy/sell quantity to market_ticks.

Real `depth_imbalance` source (order-flow P1d, 2026-06-03). The brokers
deliver the aggregate order-book depth on every tick for tradable
contracts (Fyers SymbolUpdate: tot_buy_qty / tot_sell_qty; Upstox full
feed: tbq / tsq). Persisting it lets the auction-intelligence order-flow
path compute a REAL depth_imbalance instead of the synthetic 3-level
geometric-decay ladder it fabricates today. Two nullable columns, fully
additive — old rows and existing INSERTs are unaffected (they just leave
these NULL). Index spot symbols carry no book so they stay NULL there;
the futures/option book contracts wired via AUCTION_OF_BOOK_SYMBOLS
populate them.

Revision ID: 024_market_depth_totals
Revises: 023_alpha_engine_columns
Create Date: 2026-06-03
"""
from typing import Sequence, Union

from alembic import op


revision: str = "024_market_depth_totals"
down_revision: Union[str, None] = "023_alpha_engine_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE market_ticks
            ADD COLUMN IF NOT EXISTS total_buy_qty  BIGINT,
            ADD COLUMN IF NOT EXISTS total_sell_qty BIGINT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE market_ticks
            DROP COLUMN IF EXISTS total_buy_qty,
            DROP COLUMN IF EXISTS total_sell_qty
        """
    )
