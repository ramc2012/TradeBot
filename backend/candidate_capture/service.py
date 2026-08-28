"""Candidate capture runner — records the whole decision set, every cycle.

READ-ONLY BY CONSTRUCTION
─────────────────────────
This module places no orders and makes no broker call. It reads the option
chain that `market_data.option_chain.OptionChainService` has ALREADY cached in
Redis, and it deliberately does NOT call `track()` / `ensure_running()`: doing
so would enlist new chains into the broker poll loop and turn an observer into
a source of REST load on the same budget the trading lanes share. An expiry
nobody else is polling is therefore recorded as an uncovered gap rather than
quietly fetched — see `coverage_gaps` in the cycle summary.

There is no central paper/live kill switch in this codebase; routing is decided
by which code path a caller invokes. So the safety guarantee here is the import
list, and `tests/test_candidate_capture_safety.py` enforces it.

WHAT ONE CYCLE PRODUCES
───────────────────────
For each configured underlying, one DECISION SET sharing a `decision_id`:
  * one row per contract inside the capture envelope, eligible or not, and
  * exactly one NO_TRADE row.

Rejected-but-envelope-resident contracts are stored WITH their rejection reason
rather than dropped. "This contract was unquotable at 14:05" is a fact a model
should be able to condition on, and silently omitting it would make the capture
look complete when it was selective. The envelope itself (DTE + moneyness
bounds) is what limits volume; quality is recorded, not filtered.
"""
from __future__ import annotations

import json
import time as _time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from analysis.instruments import ALL_FO_INDICES, get_fo_market
from candidate_capture.taxonomy import (
    DEFINITION_VERSION,
    chain_liquidity_percentiles,
    classify_contract,
)
from db.database import AsyncSessionLocal
from market_data.strike_ladder import ladder_step

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

TABLE = "candidate_snapshots"
CAPTURE_VERSION = "candidate_capture_v1"
SOURCE_CHAIN_CACHE = "option_chain_redis_cache"

# The abstain candidate. Stored in `option_type` so a decision set can be read
# back with one query and the abstain option is never a special case a consumer
# has to remember to add.
NO_TRADE = "NO_TRADE"

# ── eligibility statuses ──────────────────────────────────────────────────
ELIGIBLE = "eligible"
REJECT_NO_QUOTE = "no_quote"
REJECT_CROSSED = "crossed_market"
REJECT_WIDE_SPREAD = "spread_over_budget"
REJECT_STALE = "stale_quote"

# ── capture envelope ──────────────────────────────────────────────────────
# Bounds what is LOOKED AT, purely for volume control. Everything inside the
# envelope is recorded whatever its quality.
#
# Moneyness is bounded in ladder STEPS rather than a rupee or percentage width
# so one number covers every underlying's own increment.
DEFAULT_MAX_MONEYNESS_STEPS = 8.0
# A quote older than this is recorded with is_stale=True. It is NOT dropped:
# staleness is itself a measurement, and dropping it would make the capture
# look continuous across a feed outage. Sized off the chain cache's own 60s
# Redis TTL — beyond two TTLs the payload cannot be a live quote.
STALE_QUOTE_SECONDS = 120.0
# Structural, not tuned: a spread wider than this fraction of the mid means the
# quoted price carries no usable information about where a fill would land.
MAX_SPREAD_FRACTION = 0.35

# `fo_contract_catalog` changes on a contract sync (daily at most).
_LISTED_TTL_SECONDS = 900.0
_listed_cache: dict[str, tuple[float, list[date]]] = {}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _int_or_none(value: Any) -> Optional[int]:
    numeric = _finite(value)
    return int(numeric) if numeric is not None else None


def clear_listed_cache() -> None:
    """Drop the memoised expiry listings (contract sync / tests)."""
    _listed_cache.clear()


# ══════════════════════════════════════════════════════════════════════════
# (1) Pure computation — no I/O, unit-testable
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class QuoteVerdict:
    status: str
    reason: Optional[str]
    spread: Optional[float]
    spread_pct: Optional[float]
    is_stale: bool


def assess_quote(
    *,
    ltp: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
    quote_age_seconds: Optional[float],
    max_spread_fraction: float = MAX_SPREAD_FRACTION,
    stale_after_seconds: float = STALE_QUOTE_SECONDS,
) -> QuoteVerdict:
    """Grade one quote. Every outcome is RECORDED; none is a silent drop.

    Order matters: a crossed book is reported as crossed even when it is also
    stale, because a crossed market is a corruption signal about the feed while
    staleness is only an age fact.
    """
    is_stale = quote_age_seconds is not None and quote_age_seconds > stale_after_seconds

    bid_v = _finite(bid)
    ask_v = _finite(ask)
    ltp_v = _finite(ltp)

    if bid_v is None or ask_v is None or bid_v <= 0 or ask_v <= 0:
        # No two-sided quote. An LTP alone cannot say what a fill would cost,
        # so the contract is not eligible — but it is still stored, because
        # "never quotable" is a real property of a strike.
        return QuoteVerdict(REJECT_NO_QUOTE, "no_two_sided_quote", None, None, is_stale)

    if ask_v < bid_v:
        return QuoteVerdict(
            REJECT_CROSSED, f"ask {ask_v} < bid {bid_v}", None, None, is_stale
        )

    spread = round(ask_v - bid_v, 6)
    mid = (ask_v + bid_v) / 2.0
    spread_pct = round(spread / mid, 6) if mid > 0 else None

    if spread_pct is not None and spread_pct > max_spread_fraction:
        return QuoteVerdict(
            REJECT_WIDE_SPREAD,
            f"spread {spread_pct:.4f} of mid > {max_spread_fraction}",
            spread,
            spread_pct,
            is_stale,
        )

    if is_stale:
        return QuoteVerdict(
            REJECT_STALE,
            f"quote_age={quote_age_seconds:.1f}s > {stale_after_seconds}s",
            spread,
            spread_pct,
            True,
        )

    if ltp_v is None:
        return QuoteVerdict(ELIGIBLE, None, spread, spread_pct, is_stale)
    return QuoteVerdict(ELIGIBLE, None, spread, spread_pct, is_stale)


def within_envelope(
    steps: Optional[float], *, max_steps: float = DEFAULT_MAX_MONEYNESS_STEPS
) -> bool:
    """Is this contract close enough to the money to be worth recording?

    A contract whose moneyness cannot be computed (no spot, no ladder step) is
    KEPT: excluding it would silently make missing-spot cycles look like thin
    chains rather than degraded captures.
    """
    if steps is None:
        return True
    return abs(steps) <= max_steps


def chain_features(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Chain-level shape/skew already computed by OptionChainService.

    Reused rather than recomputed — `_calculate_analytics` is the one place
    these are defined, and a second implementation would drift from it.
    """
    keys = (
        "pcr_oi", "pcr_volume", "pcr_prev_oi", "pcr_oi_change",
        "max_pain", "atm_strike", "atm_iv",
        "total_ce_oi", "total_pe_oi", "total_ce_volume", "total_pe_volume",
        "total_ce_oi_change", "total_pe_oi_change",
    )
    return {key: payload.get(key) for key in keys if payload.get(key) is not None}


def build_rows(
    *,
    payload: Mapping[str, Any],
    underlying: str,
    expiry: date,
    listed_expiries: Sequence[date],
    decision_id: str,
    captured_at: datetime,
    vix: Optional[float],
    vix_missing_reason: Optional[str],
    max_moneyness_steps: float = DEFAULT_MAX_MONEYNESS_STEPS,
    lot_sizes: Optional[Mapping[tuple[Any, float, str], int]] = None,
) -> list[dict[str, Any]]:
    """One cached chain payload → the rows for its part of a decision set.

    Pure: the caller supplies the payload and the clock, so a whole cycle can
    be exercised in tests without Redis or Postgres.
    """
    entries = [e for e in (payload.get("entries") or []) if isinstance(e, Mapping)]
    spot = _finite(payload.get("spot_price"))

    # The ladder step comes from the chain's OWN listed strikes rather than the
    # STRIKE_STEPS table: the table covers 42 of ~220 F&O names and would force
    # a fallback guess for the rest, while the chain always carries the truth.
    step = ladder_step([s for s in (_finite(e.get("strike")) for e in entries) if s])

    quote_age = _quote_age_seconds(payload, captured_at)
    percentiles = chain_liquidity_percentiles(
        [{"oi": e.get("oi"), "volume": e.get("volume")} for e in entries]
    )

    shared_features = chain_features(payload)
    shared_features["india_vix"] = vix
    if vix is None and vix_missing_reason:
        shared_features["india_vix_unavailable"] = vix_missing_reason
    data_quality = payload.get("data_quality")
    if isinstance(data_quality, Mapping):
        shared_features["chain_data_quality"] = dict(data_quality)

    exchange = get_fo_market(underlying)
    session_date = captured_at.astimezone(IST).date()

    rows: list[dict[str, Any]] = []
    for entry, percentile in zip(entries, percentiles):
        taxonomy = classify_contract(
            exchange=exchange,
            underlying=underlying,
            index_symbols=ALL_FO_INDICES,
            expiry=expiry,
            listed_expiries=listed_expiries,
            option_type=str(entry.get("option_type") or ""),
            strike=entry.get("strike"),
            spot=spot,
            ladder_step=step,
            liquidity_percentile=percentile,
            now=captured_at,
        )
        if not within_envelope(taxonomy.moneyness_steps, max_steps=max_moneyness_steps):
            continue

        verdict = assess_quote(
            ltp=entry.get("ltp"),
            bid=entry.get("bid"),
            ask=entry.get("ask"),
            quote_age_seconds=quote_age,
        )
        missing = _missing_fields(entry, spot=spot, step=step, vix=vix)

        rows.append(
            {
                "time": captured_at,
                "decision_id": decision_id,
                "session_date": session_date,
                **_taxonomy_columns(taxonomy),
                "spot": spot,
                "ltp": _finite(entry.get("ltp")),
                "bid": _finite(entry.get("bid")),
                "ask": _finite(entry.get("ask")),
                "spread": verdict.spread,
                "spread_pct": verdict.spread_pct,
                # Measured from a real two-sided quote, never estimated — the
                # backfill path is the only writer that sets this True.
                "spread_pct_estimated": False,
                "volume": _int_or_none(entry.get("volume")),
                "oi": _int_or_none(entry.get("oi")),
                "oi_change": _finite(entry.get("oi_change")),
                "iv": _finite(entry.get("iv")),
                "delta": _finite(entry.get("delta")),
                "gamma": _finite(entry.get("gamma")),
                "theta": _finite(entry.get("theta")),
                "vega": _finite(entry.get("vega")),
                # Captured, not joined at read time: cost as a fraction of
                # premium is lot-size dependent (the flat per-order brokerage
                # divides by lot x premium), and the catalog's lot size changes
                # between expiries, so a later join would apply today's lot to
                # an old row.
                "lot_size": (lot_sizes or {}).get(
                    (expiry, taxonomy.strike, taxonomy.option_type)
                ),
                "features": dict(shared_features),
                "missing_fields": missing,
                "chain_is_stale": verdict.is_stale,
                "chain_quote_age_seconds": quote_age,
                "eligibility_status": verdict.status,
                "eligibility_reason": verdict.reason,
                "is_selected": False,
                "source": SOURCE_CHAIN_CACHE,
                "capture_version": CAPTURE_VERSION,
                "definition_version": DEFINITION_VERSION,
            }
        )
    return rows


def build_no_trade_row(
    *,
    underlying: str,
    decision_id: str,
    captured_at: datetime,
    expiry: Optional[date] = None,
    features: Optional[Mapping[str, Any]] = None,
    missing_fields: Optional[Sequence[str]] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """The abstain candidate that closes every decision set.

    Written even when the chain was unavailable — in that case it is the ONLY
    row for the underlying, and its `missing_fields` is the durable record that
    the cycle happened and found nothing to look at. Without it a feed outage
    would be indistinguishable from a session where no cycle ever ran.
    """
    return {
        "time": captured_at,
        "decision_id": decision_id,
        "session_date": captured_at.astimezone(IST).date(),
        "exchange": get_fo_market(underlying),
        "underlying": str(underlying).strip().upper(),
        "underlying_class": (
            "INDEX" if str(underlying).strip().upper() in set(ALL_FO_INDICES) else "STOCK"
        ),
        "expiry": expiry,
        "expiry_class": "UNKNOWN",
        "expiry_class_reason": "no_trade_candidate_has_no_contract",
        "days_to_expiry": None,
        "hours_to_expiry": None,
        "expiry_day_flag": False,
        "monthly_expiry_week_flag": False,
        "option_type": NO_TRADE,
        "strike": None,
        "moneyness": "UNKNOWN",
        "moneyness_steps": None,
        "liquidity_bucket": "UNKNOWN",
        "liquidity_percentile": None,
        "spot": None,
        "ltp": None,
        "bid": None,
        "ask": None,
        "spread": None,
        "spread_pct": None,
        "spread_pct_estimated": False,
        "volume": None,
        "oi": None,
        "oi_change": None,
        "iv": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "lot_size": None,
        "features": dict(features or {}),
        "missing_fields": list(missing_fields or []),
        "chain_is_stale": False,
        "chain_quote_age_seconds": None,
        "eligibility_status": ELIGIBLE,
        "eligibility_reason": reason,
        "is_selected": False,
        "source": SOURCE_CHAIN_CACHE,
        "capture_version": CAPTURE_VERSION,
        "definition_version": DEFINITION_VERSION,
    }


def _taxonomy_columns(taxonomy: Any) -> dict[str, Any]:
    return {
        "exchange": taxonomy.exchange,
        "underlying": taxonomy.underlying,
        "underlying_class": taxonomy.underlying_class,
        "expiry": taxonomy.expiry,
        "expiry_class": taxonomy.expiry_class,
        "expiry_class_reason": taxonomy.expiry_class_reason,
        "days_to_expiry": taxonomy.days_to_expiry,
        "hours_to_expiry": taxonomy.hours_to_expiry,
        "expiry_day_flag": taxonomy.expiry_day_flag,
        "monthly_expiry_week_flag": taxonomy.monthly_expiry_week_flag,
        "option_type": taxonomy.option_type,
        "strike": taxonomy.strike,
        "moneyness": taxonomy.moneyness,
        "moneyness_steps": taxonomy.moneyness_steps,
        "liquidity_bucket": taxonomy.liquidity_bucket,
        "liquidity_percentile": taxonomy.liquidity_percentile,
    }


def _missing_fields(
    entry: Mapping[str, Any],
    *,
    spot: Optional[float],
    step: Optional[float],
    vix: Optional[float],
) -> list[str]:
    """Name every field this row wanted and could not get.

    Recorded rather than zero-filled. A NULL that says why it is NULL is a
    usable observation; a fabricated 0 is a silent lie that survives into
    training data.
    """
    missing: list[str] = []
    for field in ("ltp", "bid", "ask", "iv", "delta", "gamma", "theta", "vega"):
        if _finite(entry.get(field)) is None:
            missing.append(field)
    if _int_or_none(entry.get("oi")) is None:
        missing.append("oi")
    if _int_or_none(entry.get("volume")) is None:
        missing.append("volume")
    if spot is None:
        missing.append("spot")
    if step is None:
        missing.append("ladder_step")
    if vix is None:
        missing.append("india_vix")
    # Underlying momentum / order-flow features are absent BY DECISION in v1,
    # not by failure: the spot-candle sources split into tick-derived series
    # carrying known cross-symbol contamination and broker-history series that
    # go stale for weeks, and shipping a momentum feature before that is
    # settled would bake the ambiguity into every row. Named here so its
    # absence is explicit in the data rather than inferred from a missing key.
    missing.append("underlying_momentum_not_captured_in_v1")
    return missing


def _quote_age_seconds(
    payload: Mapping[str, Any], captured_at: datetime
) -> Optional[float]:
    stamp = payload.get("timestamp")
    if not stamp:
        return None
    try:
        observed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return round((captured_at - observed).total_seconds(), 3)


# ══════════════════════════════════════════════════════════════════════════
# (2) I/O — bounded reads, one bulk insert
# ══════════════════════════════════════════════════════════════════════════
async def load_listed_expiries(underlying: str) -> list[date]:
    """Every expiry the exchange lists for this underlying, ascending.

    This is what makes `expiry_class` data-driven: the WEEKLY/MONTHLY split is
    read off the listed set, never off an expiry-weekday assumption.
    """
    key = str(underlying or "").strip().upper()
    cached = _listed_cache.get(key)
    now = _time.monotonic()
    if cached and cached[0] > now:
        return cached[1]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT expiry
                      FROM fo_contract_catalog
                     WHERE underlying = :underlying
                     ORDER BY expiry ASC
                    """
                ),
                {"underlying": key},
            )
        ).fetchall()

    expiries = [r[0] for r in rows if isinstance(r[0], date)]
    _listed_cache[key] = (now + _LISTED_TTL_SECONDS, expiries)
    return expiries


async def read_cached_chain(
    underlying: str, expiry: date
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The cached chain for a contract series, plus the key form that hit.

    THE KEY FORM MATTERS AND IS NOT THE UNDERLYING. Chains are cached under the
    APP symbol — `market_data.option_subscription_manager` tracks them as
    `to_app_symbol(underlying)`, so the live key is `oc:NSE:NIFTY50-INDEX:<expiry>`,
    NOT `oc:NIFTY:<expiry>`. The first version of this module read the raw
    underlying and therefore missed EVERY chain, recording "chain_not_in_cache"
    on every cycle while looking like it was running fine — the silent-no-op
    failure shape this repo has shipped before.

    The raw symbol is tried as a fallback because `macd_refined.live` caches
    under it, and the key that actually hit is returned so the row's provenance
    records which namespace served it rather than leaving it ambiguous.
    """
    from market_data.option_chain import option_chain_service
    from market_data.symbols import to_app_symbol

    expiry_iso = expiry.isoformat()
    candidates: list[str] = []
    try:
        app_symbol = to_app_symbol(underlying)
    except Exception:  # noqa: BLE001 — resolution is a convenience, not a dependency
        app_symbol = None
    if app_symbol:
        candidates.append(str(app_symbol))
    if underlying not in candidates:
        candidates.append(underlying)

    for key in candidates:
        payload = await option_chain_service.get_cached(key, expiry_iso)
        if payload:
            return payload, f"oc:{key}:{expiry_iso}"
    return None, None


_INSERT_SQL = text(
    f"""
    INSERT INTO {TABLE} (
        time, decision_id, session_date,
        exchange, underlying, underlying_class,
        expiry, expiry_class, expiry_class_reason,
        days_to_expiry, hours_to_expiry, expiry_day_flag, monthly_expiry_week_flag,
        option_type, strike, moneyness, moneyness_steps,
        liquidity_bucket, liquidity_percentile, lot_size,
        spot, ltp, bid, ask, spread, spread_pct, spread_pct_estimated,
        volume, oi, oi_change, iv, delta, gamma, theta, vega,
        features, missing_fields, chain_is_stale, chain_quote_age_seconds,
        eligibility_status, eligibility_reason, is_selected,
        source, capture_version, definition_version
    ) VALUES (
        :time, CAST(:decision_id AS uuid), :session_date,
        :exchange, :underlying, :underlying_class,
        :expiry, :expiry_class, :expiry_class_reason,
        :days_to_expiry, :hours_to_expiry, :expiry_day_flag, :monthly_expiry_week_flag,
        :option_type, :strike, :moneyness, :moneyness_steps,
        :liquidity_bucket, :liquidity_percentile, :lot_size,
        :spot, :ltp, :bid, :ask, :spread, :spread_pct, :spread_pct_estimated,
        :volume, :oi, :oi_change, :iv, :delta, :gamma, :theta, :vega,
        CAST(:features AS jsonb), CAST(:missing_fields AS jsonb),
        :chain_is_stale, :chain_quote_age_seconds,
        :eligibility_status, :eligibility_reason, :is_selected,
        :source, :capture_version, :definition_version
    )
    """
)


async def persist_rows(rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    payload = []
    for row in rows:
        record = dict(row)
        record["features"] = json.dumps(record.get("features") or {})
        record["missing_fields"] = json.dumps(record.get("missing_fields") or [])
        payload.append(record)
    async with AsyncSessionLocal() as session:
        await session.execute(_INSERT_SQL, payload)
        await session.commit()
    return len(payload)


async def _safe_vix() -> tuple[Optional[float], Optional[str]]:
    """India VIX from the shared 5-minute cache, or the reason it is absent.

    Reuses `institutional_convergence.service._load_india_vix` — a cached read
    of NSE's public all-indices endpoint, not a broker call. Failure is a
    recorded None, never a substituted value.
    """
    try:
        from institutional_convergence.service import _load_india_vix

        value = await _load_india_vix()
        if value is None:
            return None, "vix_source_returned_none"
        return float(value), None
    except Exception as exc:  # noqa: BLE001 — VIX is a feature, not a dependency
        return None, f"vix_unavailable: {type(exc).__name__}"


async def capture_underlying(
    underlying: str,
    *,
    captured_at: datetime,
    max_dte: int,
    max_moneyness_steps: float,
    vix: Optional[float],
    vix_missing_reason: Optional[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One underlying's decision set: its contract rows plus its NO_TRADE row."""
    decision_id = str(uuid.uuid4())
    today = captured_at.astimezone(IST).date()
    symbol = str(underlying).strip().upper()

    listed = await load_listed_expiries(symbol)
    in_window = [e for e in listed if 0 <= (e - today).days <= max_dte]

    meta: dict[str, Any] = {
        "underlying": symbol,
        "decision_id": decision_id,
        "listed_expiries": len(listed),
        "expiries_in_window": [e.isoformat() for e in in_window],
        "coverage_gaps": [],
    }

    if not listed:
        meta["status"] = "no_listed_expiries"
        return [
            build_no_trade_row(
                underlying=symbol,
                decision_id=decision_id,
                captured_at=captured_at,
                missing_fields=["listed_expiries", "option_chain"],
                reason="fo_contract_catalog has no rows for this underlying",
            )
        ], meta

    rows: list[dict[str, Any]] = []
    for expiry in in_window:
        try:
            payload, cache_key = await read_cached_chain(symbol, expiry)
        except Exception as exc:  # noqa: BLE001 — one bad read must not end the cycle
            meta["coverage_gaps"].append(
                {"expiry": expiry.isoformat(), "reason": f"cache_read_failed: {exc}"}
            )
            continue
        if payload:
            meta.setdefault("cache_keys", []).append(cache_key)
        if not payload:
            # Nobody is polling this chain. Recorded, never fetched: enlisting it
            # would put this observer on the shared broker budget.
            meta["coverage_gaps"].append(
                {"expiry": expiry.isoformat(), "reason": "chain_not_in_cache"}
            )
            continue
        rows.extend(
            build_rows(
                payload=payload,
                underlying=symbol,
                expiry=expiry,
                listed_expiries=listed,
                decision_id=decision_id,
                captured_at=captured_at,
                vix=vix,
                vix_missing_reason=vix_missing_reason,
                max_moneyness_steps=max_moneyness_steps,
            )
        )

    no_trade_missing = ["underlying_momentum_not_captured_in_v1"]
    if vix is None:
        no_trade_missing.append("india_vix")
    if not rows:
        no_trade_missing.append("option_chain")

    rows.append(
        build_no_trade_row(
            underlying=symbol,
            decision_id=decision_id,
            captured_at=captured_at,
            features={"india_vix": vix} if vix is not None else {},
            missing_fields=no_trade_missing,
            reason=(
                "no chain payload was cached for any in-window expiry"
                if not rows
                else None
            ),
        )
    )
    meta["status"] = "ok" if len(rows) > 1 else "chain_unavailable"
    meta["rows"] = len(rows)
    return rows, meta


async def run_candidate_capture(now: Optional[datetime] = None) -> dict[str, Any]:
    """Runner entry point. Flag-gated OFF by default.

    Returns the supervisor's cycle summary shape. Never raises into the
    scheduler: a capture failure must not be able to disturb a trading lane.
    """
    from core.config import settings

    if not bool(getattr(settings, "CANDIDATE_CAPTURE_ENABLED", False)):
        return {"status": "disabled", "flag": "CANDIDATE_CAPTURE_ENABLED"}

    started = datetime.now(UTC)
    captured_at = (now or _now_ist()).astimezone(UTC)
    underlyings = [
        s.strip().upper()
        for s in str(getattr(settings, "CANDIDATE_CAPTURE_UNDERLYINGS", "")).split(",")
        if s.strip()
    ]
    if not underlyings:
        return {
            "status": "no_universe",
            "reason": "CANDIDATE_CAPTURE_UNDERLYINGS is empty",
            "result_count": 0,
        }

    max_dte = int(getattr(settings, "CANDIDATE_CAPTURE_MAX_DTE", 45))
    max_steps = float(
        getattr(settings, "CANDIDATE_CAPTURE_MAX_MONEYNESS_STEPS", DEFAULT_MAX_MONEYNESS_STEPS)
    )
    vix, vix_reason = await _safe_vix()

    all_rows: list[dict[str, Any]] = []
    per_underlying: list[dict[str, Any]] = []
    failures: dict[str, str] = {}

    for symbol in underlyings:
        try:
            rows, meta = await capture_underlying(
                symbol,
                captured_at=captured_at,
                max_dte=max_dte,
                max_moneyness_steps=max_steps,
                vix=vix,
                vix_missing_reason=vix_reason,
            )
            all_rows.extend(rows)
            per_underlying.append(meta)
        except Exception as exc:  # noqa: BLE001
            failures[symbol] = f"{type(exc).__name__}: {exc}"
            logger.warning("[candidate-capture] {} failed: {!r}", symbol, exc)

    written = 0
    if all_rows:
        try:
            written = await persist_rows(all_rows)
        except Exception as exc:  # noqa: BLE001
            failures["persist"] = f"{type(exc).__name__}: {exc}"
            logger.error("[candidate-capture] persist failed: {!r}", exc)

    eligible = sum(1 for r in all_rows if r["eligibility_status"] == ELIGIBLE)
    stale = sum(1 for r in all_rows if r["chain_is_stale"])
    gaps = sum(len(m.get("coverage_gaps") or []) for m in per_underlying)

    summary = {
        "status": "error" if failures and not written else ("partial" if failures else "ok"),
        "result_count": written,
        "failure_count": len(failures),
        "failures": failures,
        "captured_at": captured_at.isoformat(),
        "rows_built": len(all_rows),
        "eligible_rows": eligible,
        "stale_rows": stale,
        "coverage_gaps": gaps,
        "india_vix": vix,
        "capture_version": CAPTURE_VERSION,
        "underlyings": per_underlying,
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
    }
    logger.info(
        "[candidate-capture] rows={} written={} eligible={} stale={} gaps={} failures={}",
        len(all_rows), written, eligible, stale, gaps, len(failures),
    )
    return summary
