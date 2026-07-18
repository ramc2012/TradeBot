"""Option-flow freshness watchdog (detection / telemetry only — default OFF).

Owner directive 2026-07-17 routes the FAST lanes (directional, auction,
convergence, MP+OF) onto the Fyers tick plane. But Fyers-format WS *option*
ticks have NEVER persisted (the dormant defect in
[[option-candles-rest-only-defect]]): ``option_premium_candles`` is 100 %
REST-fed, so whenever the shared broker budget starves, that table simply stops
receiving fresh rows and every "lane not updating" freeze traces back here.

This watchdog makes that defect VISIBLE the moment it becomes load-bearing: on a
fixed interval during market hours, if options are subscribed on the WS but the
newest ``option_premium_candles`` row is older than the stale threshold, it emits
an alert. It NEVER trades, restarts, or mutates state — it only observes and
reports. Gated by ``OPTION_FLOW_WATCHDOG_ENABLED`` (default False), so it is a
provable no-op until an operator opts in.

The decision logic is factored into the pure ``evaluate_option_flow`` so it is
unit-testable without a database, a clock, or a live socket; the async
``run_option_flow_watchdog`` is the thin, defensive I/O wrapper the supervisor
schedules.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

# Watchdog verdicts (also the ``status`` field of the returned dict).
STATUS_DISABLED = "disabled"   # flag off — no-op
STATUS_IDLE = "idle"           # no options subscribed → nothing to watch (not an alert)
STATUS_UNKNOWN = "unknown"     # could not determine freshness (DB down etc.) → no alert
STATUS_OK = "ok"               # fresh rows within the window
STATUS_STALE = "stale"         # ALERT: subscribed but no fresh persist within the window


def evaluate_option_flow(
    *,
    options_subscribed: bool,
    newest_persist_age_s: Optional[float],
    stale_seconds: float,
) -> dict[str, Any]:
    """Pure watchdog decision (no I/O). Returns a verdict dict with ``status`` and
    an ``alert`` bool.

    - ``options_subscribed`` False → IDLE (nothing subscribed, so silence is
      expected — never alert).
    - ``newest_persist_age_s`` None → UNKNOWN (freshness could not be read — a DB
      error must not masquerade as a stall; fail-safe to no-alert).
    - age > ``stale_seconds`` → STALE (ALERT — the premium feed has frozen while
      options are live).
    - otherwise → OK.
    """
    if not options_subscribed:
        return {"status": STATUS_IDLE, "alert": False, "age_s": newest_persist_age_s}
    if newest_persist_age_s is None:
        return {"status": STATUS_UNKNOWN, "alert": False, "age_s": None}
    stale = float(newest_persist_age_s) > float(stale_seconds)
    return {
        "status": STATUS_STALE if stale else STATUS_OK,
        "alert": stale,
        "age_s": float(newest_persist_age_s),
        "stale_seconds": float(stale_seconds),
    }


def _options_subscribed() -> bool:
    """Best-effort: are any symbols subscribed on the live WS right now? Defensive
    — any failure (socket not attached, attribute missing) returns False so the
    watchdog degrades to IDLE rather than crashing."""
    try:
        from market_data.data_router import data_router

        subs = getattr(data_router, "_subscribed_symbols", None)
        return bool(subs)
    except Exception:  # noqa: BLE001
        return False


async def _newest_premium_persist_age_seconds() -> Optional[float]:
    """Seconds since the newest ``option_premium_candles.synced_at``. None on any
    error (DB unreachable, empty table) so UNKNOWN → no false alert."""
    try:
        from sqlalchemy import text

        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            row = await session.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(synced_at))) "
                    "FROM option_premium_candles"
                )
            )
            value = row.scalar()
            return float(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


async def run_option_flow_watchdog() -> dict[str, Any]:
    """Supervisor runner entrypoint. Flag-gated; detection/telemetry only.

    Returns a verdict dict (also surfaced to the scan-audit trail by the
    supervisor). Emits a single WARNING log line on a STALE verdict — never
    raises, never mutates trading state."""
    from core.config import settings

    if not bool(getattr(settings, "OPTION_FLOW_WATCHDOG_ENABLED", False)):
        return {"status": STATUS_DISABLED, "alert": False}

    stale_seconds = float(getattr(settings, "OPTION_FLOW_WATCHDOG_STALE_SECONDS", 300))
    subscribed = _options_subscribed()
    age = await _newest_premium_persist_age_seconds() if subscribed else None
    verdict = evaluate_option_flow(
        options_subscribed=subscribed,
        newest_persist_age_s=age,
        stale_seconds=stale_seconds,
    )
    if verdict.get("alert"):
        logger.warning(
            "[option-flow-watchdog] option_premium_candles STALE — newest persist "
            f"{verdict.get('age_s'):.0f}s old (> {stale_seconds:.0f}s) while options "
            "are subscribed; the REST premium feed has frozen (see "
            "option-candles-rest-only-defect)."
        )
    return verdict
