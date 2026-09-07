"""Index-only directional long-options paper trading, with real journals.

Scope: NIFTY, BANKNIFTY, SENSEX.  Long premium only — the lane buys calls or
puts, it does not write them.

The module is built around three commitments that the existing directional
paper store does not make:

  1. ONE AUTHORITATIVE BOOK.  `index_paper_positions` is the book.  The
     journals are append-only records of what happened; nothing reconstructs
     the book by replaying them, and nothing clears the book to rebuild it.
  2. EVERY DECISION IS JOURNALLED, including the skips.  A lane that only
     records its fills cannot tell you why it did nothing for a week, and
     "97.4% of candidate deaths were one gate" is the kind of finding that is
     invisible without a rejection journal.
  3. COSTS ARE PART OF THE FILL, not a post-hoc deduction.  A long-premium
     lane that books the mid is not a paper trade, it is a fantasy.
"""
from __future__ import annotations

from directional_options.index_paper.costs import (
    CostModel,
    FeeSchedule,
    FillEstimate,
    estimate_fill,
)
from directional_options.index_paper.sizing import SizingDecision, size_position
from directional_options.index_paper.schemas import (
    Contract,
    DirectionalView,
    GateResult,
    PaperPosition,
)

__all__ = [
    "CostModel",
    "FeeSchedule",
    "FillEstimate",
    "estimate_fill",
    "SizingDecision",
    "size_position",
    "Contract",
    "DirectionalView",
    "GateResult",
    "PaperPosition",
]
