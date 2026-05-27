"""Market Profile extensions usable across S2, Commodity, Auction
Intelligence, and FMP.

These pure functions take primitive profile snapshots (dicts with
`poc`, `vah`, `val`, `ibh`, `ibl`, etc.) and derive higher-order
context useful for entry/exit decisions.

Conventions
-----------
* A "profile dict" must have at least `poc` (point of control). Most
  helpers also want `vah` (value-area high), `val` (value-area low),
  `ibh` (initial-balance high), `ibl` (initial-balance low),
  `session_high`, `session_low`. Missing keys → the function returns
  None or a conservative default.
* Pure functions, no I/O, no broker calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _get(profile: dict, key: str) -> Optional[float]:
    return _opt_float(profile.get(key))


# ─── 1. POC migration ──────────────────────────────────────────────────────

@dataclass
class POCMigration:
    direction: str  # 'up' | 'down' | 'flat'
    delta: float  # signed price move
    pct: float  # relative to prior POC
    aligned_with_close: bool  # POC moved in same direction as close


def poc_migration(
    today: dict[str, Any],
    prior: Optional[dict[str, Any]],
) -> Optional[POCMigration]:
    """How did POC shift vs the prior session?

    A consistent up-migration with prior-day close also up is the
    classic "value-up" continuation context.
    """
    if not prior:
        return None
    today_poc = _get(today, "poc")
    prior_poc = _get(prior, "poc")
    if today_poc is None or prior_poc is None or prior_poc == 0:
        return None
    delta = today_poc - prior_poc
    pct = delta / prior_poc
    if abs(pct) < 0.0005:  # <0.05% considered flat
        direction = "flat"
    elif delta > 0:
        direction = "up"
    else:
        direction = "down"
    today_close = _get(today, "close")
    prior_close = _get(prior, "close")
    aligned = False
    if today_close is not None and prior_close is not None:
        aligned = (delta > 0 and today_close > prior_close) or (
            delta < 0 and today_close < prior_close
        )
    return POCMigration(
        direction=direction,
        delta=round(delta, 4),
        pct=round(pct, 6),
        aligned_with_close=aligned,
    )


# ─── 2. Single prints ──────────────────────────────────────────────────────

def single_prints(tpo_counts: dict[float, int]) -> list[float]:
    """Identify single-TPO prices in a TPO-letter profile.

    `tpo_counts` is `{price_level: tpo_count}`. Single prints are levels
    where only one TPO letter printed — common at fast-move zones and
    often act as magnets if the day later revisits.
    """
    return sorted(price for price, count in tpo_counts.items() if count == 1)


def naked_poc(
    prior_pocs: Iterable[float],
    session_high: float,
    session_low: float,
    today_high: float,
    today_low: float,
) -> list[float]:
    """Find prior-session POCs that haven't been touched yet today.

    A "naked POC" is a magnet — price often returns to test it.
    Returns POCs within the [today_low, today_high+10%] reachable range
    that haven't been printed today.
    """
    today_lo = min(today_low, today_high)
    today_hi = max(today_low, today_high)
    out: list[float] = []
    for p in prior_pocs:
        if p is None:
            continue
        if today_lo <= p <= today_hi:
            continue  # already touched
        # Keep only POCs within a reasonable distance (5% of today's mid)
        mid = (today_hi + today_lo) / 2 if (today_hi + today_lo) else p
        if mid > 0 and abs(p - mid) / mid > 0.05:
            continue
        out.append(p)
    return sorted(out)


# ─── 3. Initial Balance extension ──────────────────────────────────────────

@dataclass
class IBExtension:
    extended_above: bool
    extended_below: bool
    extension_above_pct: float  # of IB range
    extension_below_pct: float
    ib_range: float


def ib_extension(profile: dict[str, Any], current_price: float) -> Optional[IBExtension]:
    """How much has price extended outside the Initial Balance?

    IB = first 1-hour range. Strong directional days extend the IB by
    >50%. Failures often extend <25% and revert.
    """
    ibh = _get(profile, "ibh")
    ibl = _get(profile, "ibl")
    if ibh is None or ibl is None:
        return None
    ib_range = ibh - ibl
    if ib_range <= 0:
        return None
    above = max(current_price - ibh, 0.0)
    below = max(ibl - current_price, 0.0)
    return IBExtension(
        extended_above=above > 0,
        extended_below=below > 0,
        extension_above_pct=round(above / ib_range, 4),
        extension_below_pct=round(below / ib_range, 4),
        ib_range=round(ib_range, 4),
    )


# ─── 4. Value-area overlap ─────────────────────────────────────────────────

def value_area_overlap(
    today: dict[str, Any],
    prior: dict[str, Any],
) -> Optional[float]:
    """Fraction of today's value area that overlaps prior session's.

    Useful for trend continuation logic:
      * Overlap > 0.7 → balance day inside prior range
      * Overlap < 0.3 → value migration (often trend day)
      * Zero overlap with today above prior → strong gap-up acceptance
    """
    tva_h = _get(today, "vah")
    tva_l = _get(today, "val")
    pva_h = _get(prior, "vah")
    pva_l = _get(prior, "val")
    if None in (tva_h, tva_l, pva_h, pva_l):
        return None
    today_range = tva_h - tva_l
    if today_range <= 0:
        return 0.0
    overlap_lo = max(tva_l, pva_l)
    overlap_hi = min(tva_h, pva_h)
    overlap = max(overlap_hi - overlap_lo, 0.0)
    return round(overlap / today_range, 4)


# ─── 5. Day-type confidence (multi-signal voting) ─────────────────────────

@dataclass
class DayTypeAssessment:
    classification: str  # 'trend_up' | 'trend_down' | 'balance' | 'rotational' | 'failed_up' | 'failed_down'
    confidence: float  # 0..1
    reasons: list[str]


def assess_day_type(
    profile: dict[str, Any],
    current_price: float,
    *,
    ib_extension_info: Optional[IBExtension] = None,
    poc_migration_info: Optional[POCMigration] = None,
    cvd_session: Optional[float] = None,
) -> DayTypeAssessment:
    """Synthesize an end-of-day or intraday assessment from MP + OF inputs.

    Inputs are loosely coupled: any can be None. The more present, the
    higher the confidence ceiling.
    """
    reasons: list[str] = []
    score_up = 0.0
    score_down = 0.0
    score_balance = 0.0
    inputs_used = 0

    poc = _get(profile, "poc")
    vah = _get(profile, "vah")
    val = _get(profile, "val")
    ibh = _get(profile, "ibh")
    ibl = _get(profile, "ibl")

    # Signal 1: price vs value area
    if vah is not None and val is not None and current_price > 0:
        inputs_used += 1
        if current_price > vah:
            score_up += 1
            reasons.append(f"price {current_price:.2f} > VAH {vah:.2f}")
        elif current_price < val:
            score_down += 1
            reasons.append(f"price {current_price:.2f} < VAL {val:.2f}")
        else:
            score_balance += 1
            reasons.append("price inside value area")

    # Signal 2: IB extension
    if ib_extension_info is not None:
        inputs_used += 1
        ext = ib_extension_info
        if ext.extended_above and ext.extension_above_pct > 0.5:
            score_up += 1.5
            reasons.append(f"IB extension up {ext.extension_above_pct:.0%}")
        elif ext.extended_below and ext.extension_below_pct > 0.5:
            score_down += 1.5
            reasons.append(f"IB extension down {ext.extension_below_pct:.0%}")
        elif ext.extended_above and ext.extension_above_pct < 0.25:
            score_balance += 0.5
            reasons.append("weak upside IB break — possible failure")
        elif ext.extended_below and ext.extension_below_pct < 0.25:
            score_balance += 0.5
            reasons.append("weak downside IB break — possible failure")

    # Signal 3: POC migration vs prior session
    if poc_migration_info is not None:
        inputs_used += 1
        m = poc_migration_info
        if m.direction == "up" and m.aligned_with_close:
            score_up += 1
            reasons.append(f"POC migrated up {m.pct:.2%}")
        elif m.direction == "down" and m.aligned_with_close:
            score_down += 1
            reasons.append(f"POC migrated down {m.pct:.2%}")
        elif m.direction == "flat":
            score_balance += 0.5
            reasons.append("POC flat vs prior")

    # Signal 4: CVD
    if cvd_session is not None:
        inputs_used += 1
        if cvd_session > 0:
            score_up += 0.75
            reasons.append("session CVD positive")
        elif cvd_session < 0:
            score_down += 0.75
            reasons.append("session CVD negative")

    if inputs_used == 0:
        return DayTypeAssessment(classification="unknown", confidence=0.0, reasons=["no inputs"])

    # Decide
    total = score_up + score_down + score_balance
    if total == 0:
        return DayTypeAssessment(classification="balance", confidence=0.2, reasons=reasons)

    winner = max(("up", score_up), ("down", score_down), ("balance", score_balance), key=lambda t: t[1])
    label_map = {"up": "trend_up", "down": "trend_down", "balance": "balance"}
    confidence = round(winner[1] / total, 3)
    return DayTypeAssessment(
        classification=label_map[winner[0]],
        confidence=confidence,
        reasons=reasons,
    )


# ─── 6. Convenience snapshot ───────────────────────────────────────────────

def market_profile_ext_snapshot(
    today: dict[str, Any],
    prior: Optional[dict[str, Any]] = None,
    *,
    current_price: Optional[float] = None,
    cvd_session: Optional[float] = None,
    prior_session_pocs: Optional[Iterable[float]] = None,
) -> dict[str, Any]:
    """Pack everything useful into one dict for inclusion in status payloads."""
    if current_price is None:
        current_price = _get(today, "close") or _get(today, "poc") or 0.0
    ib_info = ib_extension(today, current_price) if current_price else None
    poc_info = poc_migration(today, prior) if prior else None
    va_overlap = value_area_overlap(today, prior) if prior else None
    naked = (
        naked_poc(
            list(prior_session_pocs),
            session_high=_get(today, "session_high") or 0.0,
            session_low=_get(today, "session_low") or 0.0,
            today_high=_get(today, "session_high") or current_price,
            today_low=_get(today, "session_low") or current_price,
        )
        if prior_session_pocs
        else []
    )
    assessment = assess_day_type(
        today,
        current_price,
        ib_extension_info=ib_info,
        poc_migration_info=poc_info,
        cvd_session=cvd_session,
    )
    return {
        "ib_extension": (
            {
                "extended_above": ib_info.extended_above,
                "extended_below": ib_info.extended_below,
                "extension_above_pct": ib_info.extension_above_pct,
                "extension_below_pct": ib_info.extension_below_pct,
                "ib_range": ib_info.ib_range,
            }
            if ib_info
            else None
        ),
        "poc_migration": (
            {
                "direction": poc_info.direction,
                "delta": poc_info.delta,
                "pct": poc_info.pct,
                "aligned_with_close": poc_info.aligned_with_close,
            }
            if poc_info
            else None
        ),
        "value_area_overlap": va_overlap,
        "naked_pocs": naked,
        "day_type_assessment": {
            "classification": assessment.classification,
            "confidence": assessment.confidence,
            "reasons": assessment.reasons,
        },
    }
