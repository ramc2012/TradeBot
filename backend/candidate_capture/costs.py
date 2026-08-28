"""Round-trip cost for one NSE option position — the ONE model labels use.

WHY ANOTHER COST MODULE
───────────────────────
There are already four cost implementations in this repo carrying three
different rate sets:

  backend/paper_engine/costs.py          statutory only, CORRECT post-Oct-2024
                                         option STT (0.10% sell side). No spread
                                         term at all.
  backend/analysis/cost_model.py         has a spread term (1.4%/side) but its
                                         option STT is STALE at 0.0625%.
  backend/auction_intelligence/
      validation/gate_b.py               2 bps of notional — effectively free.
  backend/cbe_scanner/paper.py           15 bps, equity-delivery shaped.

They disagree by more than 4x on the same trade, so "net of costs" currently
means four different things depending on which lane said it. A label is a
permanent artifact that models will be trained and promoted on, so it gets one
explicit model rather than inheriting whichever one was nearest.

WHAT THIS MODEL DOES DIFFERENTLY
────────────────────────────────
1. Statutory rates are taken from `paper_engine.costs` — the only implementation
   whose option STT is current — by importing it, not by copying the constants.
   A rate correction there flows here.

2. The spread term is driven by THE ROW'S OWN MEASURED SPREAD, not a flat
   percentage. Every other model charges a constant per side, which manufactures
   edge on exactly the illiquid contracts where the real spread is widest. A
   captured candidate row carries a real quoted bid/ask, so the entry half-spread
   is measured, not assumed.

3. The measured half and the assumed half are NEVER fused into one number.
   No table in this schema holds a forward bid/ask for an option, so the EXIT
   half-spread cannot be measured — only assumed. Fusing them would produce an
   unfalsifiable "net" figure that hides which half is evidence. They are
   returned as separate fields and stored as separate columns.

4. Costs go into LABELS, not into evaluation — the design rule already written
   down in sniper-phase0/CLAUDE.md. Do not subtract these again downstream.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Optional

# NSE options quote in 0.05 rupee ticks. Sourced as a constant because
# fo_contract_catalog.tick_size is NULL for ~96% of index-option rows, so a
# join would silently yield nothing for the universe this pipeline captures.
# Declared here so the assumption is visible rather than implied.
NSE_OPTION_TICK = 0.05

# When a row carries no usable quoted spread, fall back to this half-spread
# fraction of premium. Chosen to match backend/analysis/cost_model.py's 1.4% per
# side so a fallback row is costed no more optimistically than the repo's
# existing research model. It is a FLOOR for missing data, never a substitute
# for a measured spread.
FALLBACK_HALF_SPREAD_PCT = 0.014

# A quoted spread wider than this fraction of mid is treated as a broken quote
# rather than a real cost: charging it literally would produce a label asserting
# a loss the trade could not actually have taken, because nobody crosses a
# 50%-wide book. Rows hitting this are flagged, not silently clipped.
MAX_CREDIBLE_HALF_SPREAD_PCT = 0.25


def _rate_source() -> Any:
    """The statutory rate constants, from `paper_engine/costs.py`.

    Loaded as a standalone leaf rather than imported, because
    `paper_engine/__init__` pulls in `PaperOrderBook` — an order path this
    package must never be able to reach. See candidate_capture/_leaf_import.py.
    """
    from candidate_capture._leaf_import import statutory_rates

    return statutory_rates()


def round_to_tick(price: float, tick: float = NSE_OPTION_TICK) -> float:
    """Snap a modelled fill to a real exchange tick.

    Nothing else in this repo does this — `tick_size` is read only as a
    market-profile bucket width — which is why the S1 book's 5 bps of slippage
    is fictional: on a 20 rupee option it moves the price by 0.01 against a 0.05
    tick, an adjustment no exchange could represent.
    """
    if tick <= 0:
        return float(price)
    return round(round(float(price) / tick) * tick, 2)


@dataclass(frozen=True)
class CostBreakdown:
    """Every component kept separate so any one of them can be audited."""

    # Spread — the dominant term, and the only one split by evidence status.
    entry_half_spread_pct: Optional[float]
    entry_half_spread_measured: bool
    exit_half_spread_pct: Optional[float]
    # Always False today: no forward option bid/ask exists anywhere in this
    # schema. Kept as a field so that if one ever lands, the label records
    # which rows were measured and which were assumed.
    exit_half_spread_measured: bool
    spread_cost_rupees: float

    # Statutory, from paper_engine.costs
    statutory_rupees: float

    total_rupees: float
    total_pct_of_entry_notional: Optional[float]

    # The premium a buyer would actually pay / receive after crossing and
    # tick-rounding. These are what a realistic fill looks like.
    entry_fill_price: Optional[float]
    exit_fill_price: Optional[float]

    quantity: int
    lot_size: Optional[int]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["notes"] = list(self.notes)
        return out


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def measured_half_spread_pct(
    *, bid: Optional[float], ask: Optional[float]
) -> tuple[Optional[float], list[str]]:
    """Half the quoted spread as a fraction of mid, from a real two-sided quote.

    Half, not full: crossing the book once costs you half the spread relative to
    mid. A round trip pays it twice, which is why entry and exit are charged
    separately rather than one "round-trip spread" constant.
    """
    notes: list[str] = []
    bid_v = _finite(bid)
    ask_v = _finite(ask)
    if bid_v is None or ask_v is None or bid_v <= 0 or ask_v <= 0:
        return None, ["no_two_sided_quote"]
    if ask_v < bid_v:
        return None, ["crossed_quote"]
    mid = (bid_v + ask_v) / 2.0
    if mid <= 0:
        return None, ["non_positive_mid"]
    half = (ask_v - bid_v) / 2.0 / mid
    if half > MAX_CREDIBLE_HALF_SPREAD_PCT:
        notes.append(
            f"half_spread_{half:.4f}_exceeds_credible_{MAX_CREDIBLE_HALF_SPREAD_PCT}"
        )
    return round(half, 6), notes


def statutory_round_trip(
    *,
    entry_price: float,
    exit_price: float,
    quantity: int,
) -> float:
    """Brokerage + STT + exchange + SEBI + stamp + GST for a BUY-then-SELL option.

    Takes the RATE CONSTANTS from `paper_engine.costs` — the repo's only
    implementation carrying the current post-Oct-2024 0.10% option STT — so a
    statutory rate correction there flows here with no second table of rates.

    It deliberately does NOT call `round_trip_charges()`, even though that
    function computes exactly this. That function is gated by the
    `PAPER_APPLY_COSTS` environment variable and returns 0.0 when it is off. A
    LABEL is a permanent training artifact, and letting a paper-book display
    toggle silently zero the cost term would produce a stored dataset asserting
    that trading is free — with nothing on the row to show why. Labels are
    always costed; only paper books get a toggle.
    """
    pec = _rate_source()

    qty = int(quantity)
    if qty <= 0:
        return 0.0
    entry_turnover = abs(float(entry_price)) * qty
    exit_turnover = abs(float(exit_price)) * qty
    total_turnover = entry_turnover + exit_turnover
    if total_turnover <= 0:
        return 0.0

    # Long option: we BUY on entry, SELL on exit. STT is sell-side only,
    # stamp duty buy-side only.
    brokerage = 2.0 * pec.BROKERAGE_PER_ORDER
    stt = pec.STT_OPTION_SELL * exit_turnover
    exch = pec.EXCH_OPTION * total_turnover
    stamp = pec.STAMP_OPTION * entry_turnover
    sebi = pec.SEBI_FEE * total_turnover
    gst = pec.GST * (brokerage + exch + sebi)
    return round(brokerage + stt + exch + sebi + stamp + gst, 4)


def round_trip_cost(
    *,
    entry_mid: Optional[float],
    exit_mid: Optional[float],
    quantity: int,
    lot_size: Optional[int] = None,
    entry_bid: Optional[float] = None,
    entry_ask: Optional[float] = None,
    exit_bid: Optional[float] = None,
    exit_ask: Optional[float] = None,
    estimated_half_spread_pct: Optional[float] = None,
    fallback_half_spread_pct: float = FALLBACK_HALF_SPREAD_PCT,
) -> CostBreakdown:
    """Full cost of buying at `entry_mid` and selling at `exit_mid`.

    `entry_bid`/`entry_ask` come from the candidate snapshot and give a MEASURED
    entry half-spread. `exit_bid`/`exit_ask` are accepted for symmetry but are
    None in practice — nothing persists a forward option quote — so the exit
    half-spread falls back and is flagged as assumed.
    """
    notes: list[str] = []
    entry_v = _finite(entry_mid)
    exit_v = _finite(exit_mid)
    qty = max(int(quantity or 0), 0)

    entry_half, entry_notes = measured_half_spread_pct(bid=entry_bid, ask=entry_ask)
    notes.extend(f"entry:{n}" for n in entry_notes)
    entry_measured = entry_half is not None
    if entry_half is None:
        # PREFER A CALLER'S ESTIMATE over the flat fallback. A reconstructed row
        # has no quote but may carry a band-calibrated spread, and that estimate
        # is the whole reason the row was restricted to a liquid band. Falling
        # through to the generic constant would silently discard it — measured
        # here as roughly doubling the charged spread and pushing every
        # backfilled contract below breakeven.
        estimated = _finite(estimated_half_spread_pct)
        if estimated is not None and estimated > 0:
            entry_half = estimated
            notes.append("entry_half_spread_estimated_by_caller")
        else:
            entry_half = fallback_half_spread_pct
            notes.append("entry_half_spread_assumed")

    exit_half, exit_notes = measured_half_spread_pct(bid=exit_bid, ask=exit_ask)
    notes.extend(f"exit:{n}" for n in exit_notes)
    exit_measured = exit_half is not None
    if exit_half is None:
        # The normal path. No forward option quote exists in this schema, so the
        # exit spread is the entry spread carried forward — which assumes
        # liquidity does not deteriorate over the hold. That assumption fails
        # precisely on the illiquid contracts where it matters most, hence the
        # separate `exit_half_spread_measured=False` flag on every row.
        exit_half = entry_half
        notes.append(
            "exit_half_spread_assumed_from_entry"
            if entry_measured
            else "exit_half_spread_assumed_from_entry_estimate"
        )

    if entry_v is None or exit_v is None or qty <= 0:
        return CostBreakdown(
            entry_half_spread_pct=entry_half,
            entry_half_spread_measured=entry_measured,
            exit_half_spread_pct=exit_half,
            exit_half_spread_measured=exit_measured,
            spread_cost_rupees=0.0,
            statutory_rupees=0.0,
            total_rupees=0.0,
            total_pct_of_entry_notional=None,
            entry_fill_price=None,
            exit_fill_price=None,
            quantity=qty,
            lot_size=lot_size,
            notes=tuple(notes + ["uncostable_missing_price_or_quantity"]),
        )

    # A buyer crosses UP on the way in and DOWN on the way out — both adverse.
    entry_fill = round_to_tick(entry_v * (1.0 + entry_half))
    exit_fill = round_to_tick(exit_v * (1.0 - exit_half))

    spread_cost = round(
        (entry_fill - entry_v) * qty + (exit_v - exit_fill) * qty, 4
    )

    try:
        statutory = statutory_round_trip(
            entry_price=entry_fill, exit_price=exit_fill, quantity=qty
        )
    except Exception as exc:  # noqa: BLE001
        statutory = 0.0
        notes.append(f"statutory_unavailable:{type(exc).__name__}")

    total = round(spread_cost + statutory, 4)
    notional = entry_v * qty
    pct = round(total / notional, 6) if notional > 0 else None

    return CostBreakdown(
        entry_half_spread_pct=entry_half,
        entry_half_spread_measured=entry_measured,
        exit_half_spread_pct=exit_half,
        exit_half_spread_measured=exit_measured,
        spread_cost_rupees=spread_cost,
        statutory_rupees=round(statutory, 4),
        total_rupees=total,
        total_pct_of_entry_notional=pct,
        entry_fill_price=entry_fill,
        exit_fill_price=exit_fill,
        quantity=qty,
        lot_size=lot_size,
        notes=tuple(notes),
    )


def breakeven_move_pct(
    *,
    entry_mid: Optional[float],
    quantity: int,
    lot_size: Optional[int] = None,
    entry_bid: Optional[float] = None,
    entry_ask: Optional[float] = None,
    estimated_half_spread_pct: Optional[float] = None,
) -> Optional[float]:
    """The premium move, as a fraction, needed just to break even on a round trip.

    This is sniper-phase0's `m_breakeven` — "this single number gates every
    label". It is what makes a label economically decidable: a horizon over
    which the typical contract cannot move this far produces labels that are
    almost all losses by construction, and a ranker trained on them learns to
    abstain rather than to rank.

    Computed by solving for the exit mid at which total cost equals gross P&L.
    Done numerically rather than algebraically because the statutory layer is
    piecewise (a flat per-order brokerage plus several turnover rates).
    """
    entry_v = _finite(entry_mid)
    qty = max(int(quantity or 0), 0)
    if entry_v is None or entry_v <= 0 or qty <= 0:
        return None

    # UPPER BOUND IS CHECKED FIRST. Without this the loop can only ever move
    # `low`, so an unreachable breakeven silently returned the bracket itself
    # (5.0 = "a 500% move") as though it were a converged answer. Measured: a
    # 0.30 premium at qty 30 is still 3.78 rupees underwater at 5x, yet reported
    # 5.0. Deep-OTM contracts where the flat brokerage dwarfs the premium
    # genuinely have no breakeven inside any sane range, and NULL says that;
    # a fabricated 5.0 would be read as a real (if extreme) number.
    low, high = 0.0, 5.0
    ceiling_cost = round_trip_cost(
        entry_mid=entry_v,
        exit_mid=entry_v * (1.0 + high),
        quantity=qty,
        lot_size=lot_size,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        estimated_half_spread_pct=estimated_half_spread_pct,
    )
    if (entry_v * high * qty) - ceiling_cost.total_rupees < 0:
        return None

    for _ in range(60):
        mid_move = (low + high) / 2.0
        exit_mid = entry_v * (1.0 + mid_move)
        cost = round_trip_cost(
            entry_mid=entry_v,
            exit_mid=exit_mid,
            quantity=qty,
            lot_size=lot_size,
            entry_bid=entry_bid,
            entry_ask=entry_ask,
            estimated_half_spread_pct=estimated_half_spread_pct,
        )
        gross = (exit_mid - entry_v) * qty
        if gross - cost.total_rupees >= 0:
            high = mid_move
        else:
            low = mid_move
    return round(high, 6)
