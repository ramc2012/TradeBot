"""Catalog-truth strike ladders: snap a requested strike onto a real rung,
and refuse contracts the exchange does not list.

Why this exists (2026-08-04)
----------------------------
The S1 paper book held ``OPT:ITC:2026-08-25:288:PE`` — qty 8625, ~Rs 71.6k of
notional — on a strike that DOES NOT EXIST. ITC's ladder for that expiry is
2.5-wide (280, 282.50, 285, 287.50, 290, ...). The leg had a NULL
``instrument_key``, a ``current_price`` frozen at entry, and exactly 0.0
unrealized P&L from 2026-07-28 onward: it could never resolve to a tradeable
symbol, so neither the WS subscription nor the held-position candle
maintenance could price it, and no price-based exit could ever fire.

The mechanism was NOT the ATM pick. ``resolve_row_strikes`` selects from the
live chain's own strike list and never rounds — the pre-incident book
(``app_runtime_state_bkp_0803``) proves it, holding ``strike: 287.5`` with the
correct ``NSE_FO|117951`` / ``ITC 287.5 PE 25 AUG 26``. What broke was the
*symbol*: ``_contract_symbol`` rendered the strike with ``int(round(strike))``,
so 287.5 became the token ``288``. Python's round-half-to-even makes that
lossy in both directions (287.5 -> 288, 282.5 -> 282). The symbol became a
lie the position still survived — until the 2026-08-03 historical recovery
re-parsed the strike back OUT of that symbol string and wrote 288.0 over the
real 287.5, dropping instrument_key and lot_size with it.

19 NSE underlyings carry half-rung (x.50) strikes — ITC, JIOFIN, POWERGRID,
ONGC, UNIONBANK, WIPRO, TATASTEEL, GAIL, SAIL, IOC, CANBK and friends — so
this was never ITC-specific, just ITC-first.

What this module guarantees
---------------------------
1. ``format_strike`` renders a strike EXACTLY, with no rounding: 287.5 stays
   "287.5", 288.0 renders "288". This is the token both the internal
   ``OPT:...`` key and the Fyers WS symbol are built from. (Fyers itself uses
   decimal strikes — ``NSE:ONGC26JUL247.5CE`` and ``NSE:CANBK26JUL125.8PE``
   are real broker-fed symbols in this repo's own captures.)
2. ``snap_to_ladder`` moves a requested strike onto the nearest REAL rung when
   it is within half a ladder step — repairing exactly the representation
   artifacts above — and refuses to move it further than that.
3. ``validate_contract`` is fail-closed: a (underlying, expiry, strike,
   option_type) absent from ``fo_contract_catalog`` is rejected, loudly, with
   the ladder printed so the exclusion is never silent.

Strike increments are derived per (underlying, expiry, option_type) from the
catalog, never assumed — they vary from 2.5 (ITC) through 5/10/50 to 100
(SENSEX). The catalog is the only source of truth here; nothing in this module
infers a ladder arithmetically.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

# The catalog only changes on a contract sync (daily at most), so a short TTL
# keeps the entry path off the DB without ever serving a stale expiry.
_LADDER_TTL_SECONDS = 900.0

# (underlying, expiry_iso, option_type) -> (expires_at_monotonic, rows)
_ladder_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


def format_strike(strike: float | int | str) -> str:
    """Render a strike as a lossless token for symbol construction.

    ``287.5 -> "287.5"``, ``288.0 -> "288"``, ``76000 -> "76000"``,
    ``125.8 -> "125.8"``.

    NEVER rounds. The old ``int(round(strike))`` silently mapped every x.50
    strike onto a neighbouring integer that the exchange does not list, which
    is what stranded the ITC 287.5 PE leg as an unpriceable "288".
    """
    value = float(strike)
    if value.is_integer():
        return str(int(value))
    # 4dp covers every listed increment (finest live rung is 0.10) without
    # carrying float noise like 287.49999999999994 into a symbol.
    return f"{value:.4f}".rstrip("0").rstrip(".")


def clear_ladder_cache() -> None:
    """Drop the memoised ladders (contract sync / tests)."""
    _ladder_cache.clear()


async def load_strike_ladder(
    *,
    underlying: str,
    expiry: date | str,
    option_type: str,
) -> list[dict[str, Any]]:
    """Every listed rung for this contract series, ascending.

    Returns catalog rows ``{strike, instrument_key, trading_symbol, lot_size}``.
    An empty list means the catalog has NO coverage for the series — a sync
    failure, not a thin ladder — and callers must treat it as such.

    Deliberately not filtered by ``market``: (underlying, expiry, option_type)
    is already unambiguous (SENSEX is BSE-only, the rest NSE-only), and a
    market filter would silently empty the ladder for any row whose market
    column disagreed with the caller's assumption.
    """
    expiry_iso = expiry.isoformat() if isinstance(expiry, date) else str(expiry)[:10]
    key = (str(underlying or "").upper(), expiry_iso, str(option_type or "").upper())
    cached = _ladder_cache.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]

    # Bind a real ``date``, never the ISO string. ``expiry`` is a DATE column
    # and asyncpg infers the parameter type from it, so a ``str`` is rejected
    # outright ("'str' object has no attribute 'toordinal'") — which raised on
    # EVERY series and, through the fail-closed guard in resolve_contract(),
    # refused every Strategy-1 entry for two sessions (08-04, 08-05).
    expiry_param = expiry if isinstance(expiry, date) else date.fromisoformat(expiry_iso)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT strike::float8 AS strike,
                           instrument_key,
                           trading_symbol,
                           lot_size
                    FROM fo_contract_catalog
                    WHERE underlying = :underlying
                      AND expiry = :expiry
                      AND option_type = :option_type
                    ORDER BY strike ASC, last_synced_at DESC NULLS LAST
                    """
                ),
                {"underlying": key[0], "expiry": expiry_param, "option_type": key[2]},
            )
        ).mappings().all()

    ladder: list[dict[str, Any]] = []
    seen: set[float] = set()
    for row in rows:
        strike = float(row["strike"])
        if strike in seen:
            continue  # keep the freshest row per rung (ORDER BY above)
        seen.add(strike)
        ladder.append(
            {
                "strike": strike,
                "instrument_key": row["instrument_key"],
                "trading_symbol": row["trading_symbol"],
                "lot_size": int(row["lot_size"]) if row["lot_size"] else None,
            }
        )

    _ladder_cache[key] = (now + _LADDER_TTL_SECONDS, ladder)
    return ladder


def ladder_step(strikes: list[float]) -> Optional[float]:
    """The MODAL gap between adjacent rungs — the ladder's increment.

    Modal rather than minimum because real ladders widen in the wings (ITC is
    2.5-wide near spot and 5-wide far out); the minimum would understate the
    step and the mean would be dragged by the wings. Returns None for a ladder
    too short to have a gap.
    """
    ordered = sorted({float(s) for s in strikes})
    if len(ordered) < 2:
        return None
    gaps: dict[float, int] = {}
    for lo, hi in zip(ordered, ordered[1:]):
        gap = round(hi - lo, 4)
        if gap > 0:
            gaps[gap] = gaps.get(gap, 0) + 1
    if not gaps:
        return None
    # Most frequent gap; ties break to the SMALLER gap (the dense near-spot
    # region, which is where entries actually land).
    return min(gaps, key=lambda g: (-gaps[g], g))


def snap_strike(requested: float, ladder: list[float]) -> tuple[Optional[float], str]:
    """Snap ``requested`` onto the nearest real rung within half a step.

    Returns ``(snapped_strike, outcome)`` where outcome is one of
    ``exact`` / ``snapped`` / ``off_ladder`` / ``no_ladder``.

    Half a step is the textbook "which rung does this value belong to" bound
    and is exactly wide enough to repair an ``int(round())`` artifact (<= 0.5
    off) on every listed increment. Anything further away is not a
    representation error — it is a different contract, and the caller must
    fail rather than be quietly re-pointed at a strike nobody chose.
    """
    rungs = sorted({float(s) for s in ladder})
    if not rungs:
        return None, "no_ladder"
    target = float(requested)
    if target in set(rungs):
        return target, "exact"
    step = ladder_step(rungs)
    if step is None:
        return None, "off_ladder"
    nearest = min(rungs, key=lambda s: (abs(s - target), s))
    if abs(nearest - target) <= step / 2.0:
        return nearest, "snapped"
    return None, "off_ladder"


async def resolve_contract(
    *,
    underlying: str,
    expiry: date | str,
    strike: float,
    option_type: str,
    snap: bool = True,
) -> dict[str, Any]:
    """Snap a requested strike to the catalog ladder and resolve its contract.

    Always returns a verdict dict; never raises on a bad contract. Keys:

    ``ok``            — the contract is listed and tradeable
    ``strike``        — the resolved (possibly snapped) strike, else None
    ``requested``     — what the caller asked for
    ``outcome``       — exact | snapped | off_ladder | no_ladder
    ``reason``        — set when ``ok`` is False
    ``instrument_key`` / ``trading_symbol`` / ``lot_size`` — catalog values
    ``ladder``        — the rungs considered, for logging
    """
    expiry_iso = expiry.isoformat() if isinstance(expiry, date) else str(expiry)[:10]
    verdict: dict[str, Any] = {
        "ok": False,
        "strike": None,
        "requested": None,
        "outcome": "no_ladder",
        "reason": None,
        "instrument_key": None,
        "trading_symbol": None,
        "lot_size": None,
        "ladder": [],
    }
    try:
        requested = float(strike)
    except (TypeError, ValueError):
        verdict["reason"] = "unparseable_strike"
        return verdict
    verdict["requested"] = requested

    try:
        rows = await load_strike_ladder(
            underlying=underlying, expiry=expiry_iso, option_type=option_type
        )
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED but never raise into the scan loop: an entry path that
        # cannot reach the catalog cannot verify the contract, so it must not
        # open the leg — but one flaky DB read must not abort the whole scan.
        verdict["reason"] = "catalog_unavailable"
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        return verdict

    ladder = [row["strike"] for row in rows]
    verdict["ladder"] = ladder
    if not rows:
        # Catalog coverage gap, NOT a thin ladder. Distinct reason so a
        # contract-sync outage is diagnosable from the log alone rather than
        # looking like every underlying suddenly went off-ladder.
        verdict["reason"] = "catalog_empty"
        return verdict

    if snap:
        resolved, outcome = snap_strike(requested, ladder)
    else:
        resolved, outcome = (
            (requested, "exact") if requested in set(ladder) else (None, "off_ladder")
        )
    verdict["outcome"] = outcome
    if resolved is None:
        verdict["reason"] = "strike_not_in_catalog"
        return verdict

    row = next((r for r in rows if r["strike"] == resolved), None)
    if row is None:  # unreachable — snap only returns rungs drawn from `rows`
        verdict["reason"] = "strike_not_in_catalog"
        return verdict

    verdict.update(
        {
            "ok": True,
            "strike": resolved,
            "instrument_key": row["instrument_key"],
            "trading_symbol": row["trading_symbol"],
            "lot_size": row["lot_size"],
        }
    )
    return verdict


def log_verdict(verdict: dict[str, Any], *, underlying: str, expiry: str, option_type: str, context: str) -> None:
    """Emit the loud, ladder-printing log line for a snap or a refusal."""
    requested = verdict.get("requested")
    outcome = verdict.get("outcome")
    if verdict.get("ok"):
        if outcome == "snapped":
            logger.warning(
                "[StrikeLadder] {} SNAPPED {} {} {} {} -> {} (catalog rung; requested strike is "
                "not listed). instrument_key={}",
                context,
                underlying,
                expiry,
                option_type,
                format_strike(requested) if requested is not None else "?",
                format_strike(verdict["strike"]),
                verdict.get("instrument_key"),
            )
        return

    ladder = verdict.get("ladder") or []
    near = sorted(ladder, key=lambda s: abs(s - (requested or 0.0)))[:8]
    error = verdict.get("error")
    if error:
        # COULD NOT READ the catalog is not the same claim as the catalog being
        # empty, and printing the latter for the former cost two sessions of
        # misdiagnosis: the ladder read was raising for every series while the
        # log insisted the exchange had no such contract. Lead with the error.
        rungs = f"UNKNOWN — the catalog read FAILED, so no ladder was loaded: {error}"
    elif near:
        rungs = ", ".join(format_strike(s) for s in sorted(near))
    else:
        rungs = "NONE — catalog has no rows for this series"
    logger.error(
        "[StrikeLadder] {} REFUSED {} {} {} {} — {} ({}). Nearest listed rungs: [{}]. "
        "fo_contract_catalog is the exchange truth; a leg that is not in it can never "
        "resolve to a tradeable symbol, so it would price at 0 P&L forever and never "
        "trigger a price-based exit.",
        context,
        underlying,
        expiry,
        option_type,
        format_strike(requested) if requested is not None else "?",
        verdict.get("reason"),
        outcome,
        rungs,
    )
