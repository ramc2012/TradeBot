"""Async orchestration for the Black-76 dealer-positioning panel.

Pulls cached option chains for the nearest N expiries of an underlying, runs the
pure `gex_engine` per expiry, and assembles the per-expiry + term-structure
payload the rebuilt OptionAnalyticsPanel consumes. (Progression / heatmaps live
in `gex_progression.py`.)

This is additive: it does NOT touch `chain_analytics.fetch_chain_analytics`, the
legacy payload the RL policy still consumes.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from directional_options.chain_analytics import ensure_chain_tracked
from directional_options.gex_engine import (
    build_term_structure,
    chain_entries_by_strike,
    compute_expiry_gex,
    time_to_expiry_years,
)
from market_data.option_chain import option_chain_service
from market_data.symbols import to_app_symbol

IST = ZoneInfo("Asia/Kolkata")


def _parse_expiry(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    s = str(value or "").strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


async def _expiry_chain(
    underlying: str,
    app_symbol: str,
    expiry: str,
    *,
    now: datetime,
    timeout: float,
    warm: bool,
) -> Optional[dict[str, Any]]:
    """Fetch one cached chain and compute its GEX analytics. Returns None on miss."""
    if warm:
        try:
            await ensure_chain_tracked(underlying, expiry)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[gex] ensure_chain_tracked {app_symbol} {expiry}: {exc}")
    try:
        cached = await asyncio.wait_for(
            option_chain_service.get_cached(app_symbol, expiry), timeout=timeout
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        cached = None
    if not cached or not cached.get("entries"):
        return None

    spot = cached.get("spot_price")
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        return None
    if spot <= 0:
        return None

    exp_date = _parse_expiry(cached.get("expiry") or expiry)
    if exp_date is None:
        return None
    T = time_to_expiry_years(exp_date, now)

    tot_ce = cached.get("total_ce_oi")
    tot_pe = cached.get("total_pe_oi")
    totals = None
    try:
        if tot_ce is not None and tot_pe is not None and float(tot_ce) > 0:
            totals = (float(tot_ce), float(tot_pe))
    except (TypeError, ValueError):
        totals = None

    by_strike = chain_entries_by_strike(list(cached.get("entries") or []))
    out = compute_expiry_gex(
        by_strike, spot, T, totals=totals, expiry_label=str(cached.get("expiry") or expiry)
    )
    out["meta"]["expiry"] = str(cached.get("expiry") or expiry)
    # Cache provenance: the chain poll keeps re-stamping the Redis cache every
    # ~30s after market close, so without an as_of in the payload a client
    # cannot tell live data from an EOD-frozen chain.
    out["meta"]["as_of"] = cached.get("timestamp") or cached.get("ts")
    out["meta"]["chain_source"] = cached.get("source")
    return out


async def fetch_gex_analytics(
    underlying: str,
    expiries: Optional[list[str]] = None,
    *,
    max_expiries: int = 3,
    timeout: float = 2.0,
    warm: bool = True,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Per-expiry Black-76 GEX profiles + term structure for an underlying.

    `expiries`: explicit ordered list; if None, falls back to the expiries already
    tracked by the option-chain service for this symbol. Returns a payload with
    `available=False` when nothing is cached yet (cold start / pre-market).
    """
    now = now or datetime.now(IST)
    try:
        app_symbol = to_app_symbol(underlying) or underlying
    except Exception:  # noqa: BLE001
        app_symbol = underlying

    if not expiries:
        seen: list[str] = []
        for sym, exp in list(getattr(option_chain_service, "_tracked", [])):
            if sym == app_symbol and exp not in seen:
                seen.append(exp)
        expiries = sorted(seen, key=lambda e: (_parse_expiry(e) or date.max))

    expiries = [e for e in (expiries or []) if e][:max_expiries]
    if not expiries:
        return {"available": False, "underlying": underlying, "expiries": [], "per_expiry": [], "term": None}

    results = await asyncio.gather(
        *(
            _expiry_chain(underlying, app_symbol, exp, now=now, timeout=timeout, warm=warm)
            for exp in expiries
        ),
        return_exceptions=False,
    )
    per_expiry = [r for r in results if r]
    if not per_expiry:
        return {"available": False, "underlying": underlying, "expiries": expiries, "per_expiry": [], "term": None}

    per_expiry.sort(key=lambda o: _parse_expiry(o["meta"].get("expiry")) or date.max)
    term = build_term_structure([o["meta"] for o in per_expiry])
    as_of_values = [o["meta"].get("as_of") for o in per_expiry if o["meta"].get("as_of")]
    return {
        "available": True,
        "underlying": underlying,
        "spot": per_expiry[0]["meta"].get("spot"),
        "as_of": max(as_of_values) if as_of_values else None,
        "expiries": [o["meta"].get("expiry") for o in per_expiry],
        "per_expiry": per_expiry,
        "term": term,
    }
