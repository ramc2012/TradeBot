"""OI–price interpretation matrix and option-positioning signals.

Stage 4/7 of the F&O analytics design (section 10, "Signal library"). Given
a futures/option contract's price change and OI change between two snapshots
(intraday delta or D–1 EOD delta), this module returns the standard four-way
participant interpretation:

    price ↑ + OI ↑   →  long buildup       (fresh longs entering)
    price ↑ + OI ↓   →  short covering     (shorts exiting)
    price ↓ + OI ↑   →  short buildup      (fresh shorts entering)
    price ↓ + OI ↓   →  long unwinding     (longs exiting)

For options it also surfaces participant intent at a strike level — heavy
call writing above spot is a bearish bias; heavy put writing below spot is
bullish. These are widely-used heuristics from NSE/MCX trading desks and
fold directly into the dashboard cards described in the design.

This module is purely analytical. It does not call brokers, does not place
trades. Inputs are dicts (or any mapping) so the caller decides where the
numbers come from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


# A small relative-change threshold so noise on illiquid contracts does not
# trigger spurious "buildup" labels. Tunable per-caller via min_pct_move.
_DEFAULT_MIN_PCT_MOVE = 0.05  # 0.05% — looser than the 0.10% used for paper
_DEFAULT_MIN_OI_PCT = 0.50    # 0.50% OI change to call it a buildup
_HIGH_CONVICTION_OI_PCT = 5.0
_HIGH_CONVICTION_PRICE_PCT = 1.5


@dataclass(frozen=True)
class OiPriceSignal:
    """Result of one contract's OI/price interpretation."""

    contract_id: str
    label: str  # "long_buildup" | "short_covering" | "short_buildup" | "long_unwinding" | "neutral"
    direction: str  # "bullish" | "bearish" | "neutral"
    conviction: str  # "high" | "medium" | "low"
    price_change_pct: Optional[float]
    oi_change_pct: Optional[float]
    notes: list[str]


def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    try:
        c = float(current)
        p = float(previous)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    return (c - p) / abs(p) * 100.0


def classify_oi_price(
    *,
    contract_id: str,
    price_change_pct: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
    current_price: Optional[float] = None,
    previous_price: Optional[float] = None,
    current_oi: Optional[float] = None,
    previous_oi: Optional[float] = None,
    min_pct_move: float = _DEFAULT_MIN_PCT_MOVE,
    min_oi_pct: float = _DEFAULT_MIN_OI_PCT,
) -> OiPriceSignal:
    """Classify one contract by OI–price participant signal.

    Either provide the pre-computed *_pct values or the current/previous
    raw values and the function will compute the deltas itself.
    """
    if price_change_pct is None:
        price_change_pct = _pct_change(current_price, previous_price)
    if oi_change_pct is None:
        oi_change_pct = _pct_change(current_oi, previous_oi)

    notes: list[str] = []

    if price_change_pct is None or oi_change_pct is None:
        return OiPriceSignal(
            contract_id=contract_id,
            label="neutral",
            direction="neutral",
            conviction="low",
            price_change_pct=price_change_pct,
            oi_change_pct=oi_change_pct,
            notes=["Insufficient price/OI history for OI–price classification."],
        )

    price_up = price_change_pct > min_pct_move
    price_dn = price_change_pct < -min_pct_move
    oi_up = oi_change_pct > min_oi_pct
    oi_dn = oi_change_pct < -min_oi_pct

    label = "neutral"
    direction = "neutral"
    if price_up and oi_up:
        label, direction = "long_buildup", "bullish"
    elif price_up and oi_dn:
        label, direction = "short_covering", "bullish"
    elif price_dn and oi_up:
        label, direction = "short_buildup", "bearish"
    elif price_dn and oi_dn:
        label, direction = "long_unwinding", "bearish"

    # Conviction tier — both legs meaningful + same-sign confirmation
    if (
        abs(oi_change_pct) >= _HIGH_CONVICTION_OI_PCT
        and abs(price_change_pct) >= _HIGH_CONVICTION_PRICE_PCT
        and label != "neutral"
    ):
        conviction = "high"
    elif label == "neutral":
        conviction = "low"
    else:
        conviction = "medium"

    if label == "long_buildup":
        notes.append("Fresh longs entering — bullish momentum if volume confirms.")
    elif label == "short_covering":
        notes.append("Shorts exiting — bullish but typically shorter-lived than buildup.")
    elif label == "short_buildup":
        notes.append("Fresh shorts entering — bearish; watch for resistance hold.")
    elif label == "long_unwinding":
        notes.append("Longs booking — bearish but often a pullback, not reversal.")

    return OiPriceSignal(
        contract_id=contract_id,
        label=label,
        direction=direction,
        conviction=conviction,
        price_change_pct=round(price_change_pct, 3) if price_change_pct is not None else None,
        oi_change_pct=round(oi_change_pct, 3) if oi_change_pct is not None else None,
        notes=notes,
    )


def classify_many(rows: Iterable[dict]) -> list[OiPriceSignal]:
    """Classify a stream of contract dicts.

    Each row should have ``contract_id`` and either pre-computed pct deltas
    or raw current/previous price + OI.
    """
    signals: list[OiPriceSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("contract_id") or row.get("symbol") or row.get("trading_symbol") or "")
        if not cid:
            continue
        signals.append(
            classify_oi_price(
                contract_id=cid,
                price_change_pct=row.get("price_change_pct"),
                oi_change_pct=row.get("oi_change_pct"),
                current_price=row.get("price") or row.get("ltp") or row.get("close"),
                previous_price=row.get("previous_price") or row.get("prev_close"),
                current_oi=row.get("open_interest") or row.get("oi"),
                previous_oi=row.get("previous_oi") or row.get("prev_oi"),
            )
        )
    return signals


@dataclass(frozen=True)
class StrikePositioning:
    """Aggregate writer/buyer pressure at one option strike."""

    underlying: str
    expiry: str
    strike: float
    call_oi: Optional[float]
    put_oi: Optional[float]
    call_oi_change: Optional[float]
    put_oi_change: Optional[float]
    bias: str  # "call_writing" | "put_writing" | "call_buying" | "put_buying" | "neutral"
    note: str


def classify_strike_positioning(
    *,
    underlying: str,
    expiry: str,
    strike: float,
    spot: Optional[float],
    call_oi: Optional[float],
    put_oi: Optional[float],
    call_oi_change: Optional[float],
    put_oi_change: Optional[float],
    call_price_change_pct: Optional[float] = None,
    put_price_change_pct: Optional[float] = None,
) -> StrikePositioning:
    """Decide whether a strike is being written (resistance/support build)
    or bought (directional speculation).

    Heuristic — applied per-strike, not per-chain:

    * Call OI ↑ + Call price ↓  →  call writing  (sellers see the strike as
      a ceiling). Bearish for spot.
    * Call OI ↑ + Call price ↑  →  call buying   (speculation; spot
      pressure higher).
    * Put OI ↑  + Put price  ↓  →  put writing   (sellers see the strike as
      a floor). Bullish for spot.
    * Put OI ↑  + Put price  ↑  →  put buying    (hedging or speculation
      lower).
    """
    bias = "neutral"
    note = ""

    call_writing = (
        call_oi_change is not None
        and call_oi_change > 0
        and (call_price_change_pct is None or call_price_change_pct <= 0)
    )
    call_buying = (
        call_oi_change is not None
        and call_oi_change > 0
        and call_price_change_pct is not None
        and call_price_change_pct > 0
    )
    put_writing = (
        put_oi_change is not None
        and put_oi_change > 0
        and (put_price_change_pct is None or put_price_change_pct <= 0)
    )
    put_buying = (
        put_oi_change is not None
        and put_oi_change > 0
        and put_price_change_pct is not None
        and put_price_change_pct > 0
    )

    # Resolve to the dominant move on the strike. Writing dominates the
    # narrative when its magnitude exceeds buying on the same side.
    if call_writing and (call_oi_change or 0) >= abs(put_oi_change or 0):
        bias = "call_writing"
        note = (
            f"Call writers built {int(call_oi_change or 0):,} OI at {strike:g}; "
            "treats strike as resistance."
        )
    elif put_writing and (put_oi_change or 0) >= abs(call_oi_change or 0):
        bias = "put_writing"
        note = (
            f"Put writers built {int(put_oi_change or 0):,} OI at {strike:g}; "
            "treats strike as support."
        )
    elif call_buying:
        bias = "call_buying"
        note = f"Call buying conviction at {strike:g} — upside speculation."
    elif put_buying:
        bias = "put_buying"
        note = f"Put buying conviction at {strike:g} — hedging / downside speculation."

    if spot is not None and bias != "neutral":
        if bias == "call_writing" and spot < strike:
            note += " Spot below strike — resistance overhead."
        elif bias == "put_writing" and spot > strike:
            note += " Spot above strike — support beneath."

    return StrikePositioning(
        underlying=underlying,
        expiry=expiry,
        strike=float(strike),
        call_oi=call_oi,
        put_oi=put_oi,
        call_oi_change=call_oi_change,
        put_oi_change=put_oi_change,
        bias=bias,
        note=note,
    )


def max_pain(option_chain: Iterable[dict]) -> Optional[float]:
    """Compute max-pain strike from an option chain.

    Each item must have ``strike``, ``call_oi`` and ``put_oi``. Returns the
    strike at which total option-writer payout is minimized at expiry —
    the classic "max pain" level that the design doc lists alongside PCR
    and expected-move in the option-chain card.
    """
    rows = [
        (
            float(r.get("strike")),
            float(r.get("call_oi") or 0.0),
            float(r.get("put_oi") or 0.0),
        )
        for r in option_chain
        if isinstance(r, dict) and r.get("strike") is not None
    ]
    if not rows:
        return None
    strikes = sorted({s for s, _, _ in rows})
    best_strike: Optional[float] = None
    best_pain: Optional[float] = None
    for k in strikes:
        pain = 0.0
        for s, c_oi, p_oi in rows:
            # Call writers lose max(0, k-s) per share for every OI lot
            pain += max(0.0, k - s) * c_oi
            # Put writers lose max(0, s-k) per share for every OI lot
            pain += max(0.0, s - k) * p_oi
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = k
    return best_strike


__all__ = [
    "OiPriceSignal",
    "StrikePositioning",
    "classify_oi_price",
    "classify_many",
    "classify_strike_positioning",
    "max_pain",
]
