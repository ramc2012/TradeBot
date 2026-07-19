"""S2 MP+OF — port of the commodity Market-Profile + Order-Flow engine to
the NSE index-options lane.

The evaluator itself (:func:`commodity_mp_signal.evaluate_commodity_mp_signal`)
is symbol-agnostic — it consumes 1-minute OHLCV bars plus a today/prior
profile and emits a BUY/SELL signal with `entry_style`, `confidence`,
`stop_hint`, and the standard `mp_*` fields. This module is a thin
adapter that adds the bits S2 needs:

* **Universe + expiry routing** — NIFTY and SENSEX trade BOTH weekly and
  monthly ATM options on a signal; BANKNIFTY / FINNIFTY / MIDCPNIFTY
  trade monthly only (NSE discontinued their weeklies in late 2024).
* **Direction → option side** — a BUY signal opens long ATM CE, a SELL
  opens long ATM PE (long-premium only; no shorting).
* **Index spot loader** — reads from ``underlying_spot_candles``, the
  same table the commodity desk uses for futures bars, so the data path
  is identical to the validated commodity setup.
* **Prior-session profile loader + persistence** — mirrors the commodity
  persistence under ``runtime/commodity_profiles/<ROOT>/<DATE>.json``,
  so the historical timeline (Yesterday / Week / Month) reuses the same
  store. The commodity desk and S2 are intentionally writing to the
  same store: an index profile and a futures profile coexist by root.

When the feature flag :data:`settings.NSE_S2_USE_MP_OF_ENGINE` is on,
:meth:`PaperStrategyAgent._build_strategy2_signal_context` calls
:func:`evaluate_strategy2_mp_of` before the legacy MACD path, and falls
through to MACD when the MP+OF result has no signal (e.g. when there
isn't enough 1-minute history yet or the prior session is missing).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal


# ─── Universe / routing ───────────────────────────────────────────────────

# Each underlying maps to the set of expiry tracks we trade when a signal
# fires. NIFTY and SENSEX have active weekly contracts; the others do not
# (BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies were discontinued by NSE in late
# 2024, leaving monthly as the only listed expiry).
S2_EXPIRY_ROUTING: dict[str, tuple[str, ...]] = {
    "NIFTY":      ("weekly", "monthly"),
    "SENSEX":     ("weekly", "monthly"),
    "BANKNIFTY":  ("monthly",),
    "FINNIFTY":   ("monthly",),
    "MIDCPNIFTY": ("monthly",),
}

# Tick-size hint per index. Used by MarketProfileEngine when binning 1-min
# bars into TPO rows. Values match what NSE / BSE publish for the spot index
# tick. The MP engine clamps to its own minimum so a wrong value here just
# coarsens the profile slightly, never breaks it.
# Index-scale TPO ticks. The old exchange-tick values (0.05 on a ~25k index)
# made every ladder build ~4,000+ levels per period — and with one
# contaminated bar it exploded to 100k+ levels and seized the event loop
# (2026-07-13). The MP engine now also hard-caps the ladder; these tune
# granularity only.
S2_TICK_SIZE: dict[str, float] = {
    "NIFTY":      1.0,
    "BANKNIFTY":  2.0,
    "FINNIFTY":   1.0,
    "MIDCPNIFTY": 0.5,
    "SENSEX":     2.0,
}

# Minimum periods (30-min auction windows) before the MP engine is allowed
# to emit a signal. Matches the commodity threshold so behaviour is the
# same across desks — wait for IB to print, then evaluate.
S2_MP_MIN_PERIODS = 2

# Look-back when loading 1-min spot. Two days covers today's developing
# session plus the prior one (for the prior_profile / open_drive trigger).
# Kept tight because the load + per-row NSE-hours parse runs every eval and
# is part of the per-scan CPU cost.
S2_SPOT_LOOKBACK_DAYS = 2

# Per-underlying MP+OF throttle. A full evaluation (DB load → NSE-hours filter
# → profile build → CVD/ATR → 4-trigger check) is CPU-heavy; running it for
# every underlying on every 60s scan saturated the box's single core. We
# therefore recompute each underlying at most once per this interval and
# return the cached result in between. With the small universe the evals
# stagger naturally, so at most one or two run per scan.
S2_MPOF_THROTTLE_SECONDS = 90.0

# Roll-to-next-expiry threshold. When the nearest expiry for a track is within
# this many CALENDAR days of expiring, we skip it and use the next one — the
# "decide to trade the next expiry based on its logic" rule. Avoids opening a
# position on a contract about to expire (decay/assignment risk, thin gamma).
S2_EXPIRY_ROLL_CALENDAR_DAYS = 1


# ─── Spot loader ──────────────────────────────────────────────────────────


def _filter_nse_session_hours(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only rows inside the NSE regular session (09:15–15:30 IST).

    Index 1-minute spot carries pre-open and post-close minutes (the feed
    keeps emitting flat candles outside trading hours). Building a Market
    Profile / CVD over those minutes corrupts the auction read, so we scope
    to regular hours — the index analogue of the commodity desk's MCX-hours
    scoping.
    """
    from paper_engine.commodity_strategy_agent import _parse_iso_timestamp, IST

    open_min = 9 * 60 + 15
    close_min = 15 * 60 + 30
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_iso_timestamp(row.get("time"))
        if ts is None:
            continue
        ist = ts.astimezone(IST)
        minute_of_day = ist.hour * 60 + ist.minute
        if open_min <= minute_of_day <= close_min:
            out.append(row)
    return out


async def load_index_1m_spot(
    underlying: str,
    *,
    lookback_days: int = S2_SPOT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Pull 1-minute spot candles for an NSE index.

    Reads from ``underlying_spot_candles`` — the same hypertable the
    commodity desk uses for its 1-min futures bars. Returns a list of
    plain dicts shaped like ``{"time": datetime, "open": float, "high":
    float, "low": float, "close": float, "volume": int}``, sorted
    ascending by time so downstream MP/CVD code can scan once.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, COALESCE(volume, 0) AS volume
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '1minute'
                  AND time >= NOW() - (:lookback_days * INTERVAL '1 day')
                ORDER BY time
                """
            ),
            {"underlying": underlying, "lookback_days": int(lookback_days)},
        )
        rows = result.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(
                {
                    "time": row.time,
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": int(row.volume or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


# ─── Evaluator (BUY/SELL → CE/PE + expiry tracks) ─────────────────────────


def map_signal_to_option_side(signal: Optional[str]) -> Optional[str]:
    """BUY → CE (long calls), SELL → PE (long puts); everything else → None.

    S2 is long-premium only; there's no short-CE / short-PE leg.
    """
    if signal == "BUY":
        return "CE"
    if signal == "SELL":
        return "PE"
    return None


def s2_symbol_supported(underlying: str) -> bool:
    """Capability check: is this underlying an explicitly-routed S2 symbol?

    ``expiry_tracks_for`` returns a ``("monthly",)`` default for anything not
    in :data:`S2_EXPIRY_ROUTING`, which silently trades a *defaulted* monthly
    contract for a mis-configured symbol. Callers building the S2 request
    matrix should gate on this predicate and skip-and-report unsupported
    symbols instead, so a future universe mis-config fails closed rather than
    trading an assumed expiry. The current universe (NIFTY/SENSEX) is fully
    routed, so this is a no-op for present behaviour.
    """
    return str(underlying or "").upper() in S2_EXPIRY_ROUTING


def expiry_tracks_for(underlying: str) -> tuple[str, ...]:
    """Return the expiry types to trade for an underlying.

    Default fallback is ``("monthly",)`` so an unknown symbol still trades
    the safer (deeper) contract instead of silently dropping the signal.
    Kept for callers that have already passed :func:`s2_symbol_supported`;
    the capability check is the fail-closed gate, not this resolver.
    """
    return S2_EXPIRY_ROUTING.get(str(underlying).upper(), ("monthly",))


def select_s2_expiry_targets(
    underlying: str,
    *,
    monthlies: list[str],
    listed_expiries: list[str],
    today_iso: str,
) -> list[tuple[str, str]]:
    """Pure expiry-policy selector, fed from the expiry calendar catalog.

    Inputs (all ISO date strings):
      * ``monthlies`` — this underlying's MONTHLY expiries from
        ``fo_expiry_catalog`` (current + next month), sorted ascending.
      * ``listed_expiries`` — every expiry the underlying actually lists in
        the option chain / ingested option candles (weeklies + monthlies),
        sorted ascending. Used to source weeklies.

    Policy:
      * NIFTY and SENSEX trade WEEKLY + MONTHLY; the other indices trade
        MONTHLY only (S2_EXPIRY_ROUTING).
      * **Roll to next expiry** when the nearest candidate for a track is
        within ``S2_EXPIRY_ROLL_CALENDAR_DAYS`` of expiring — the "decide to
        trade the next expiry based on its logic" rule.

    Returns ``[(track, expiry_iso), …]``, monthly-first.
    """
    underlying = str(underlying or "").upper()
    tracks = expiry_tracks_for(underlying)
    out: list[tuple[str, str]] = []

    def _roll(candidates: list[str]) -> Optional[str]:
        """Pick the nearest future expiry, skipping one that's about to expire."""
        from datetime import date as _date

        today = _date.fromisoformat(today_iso)
        future = sorted({c for c in candidates if c and c >= today_iso})
        for iso in future:
            try:
                days = (_date.fromisoformat(iso) - today).days
            except ValueError:
                continue
            if days >= S2_EXPIRY_ROLL_CALENDAR_DAYS:
                return iso
        # Everything left is within the roll window AND there's nothing
        # further out — take the furthest available rather than nothing.
        return future[-1] if future else None

    monthly_iso = _roll(monthlies)
    monthly_set = set(monthlies)

    if "monthly" in tracks and monthly_iso:
        out.append(("monthly", monthly_iso))

    if "weekly" in tracks and monthly_iso:
        # A weekly is any listed expiry that is NOT a monthly and lands on or
        # before the chosen monthly. Pick the nearest (roll-adjusted) one.
        weekly_candidates = [
            iso for iso in listed_expiries
            if iso and iso not in monthly_set and iso <= monthly_iso
        ]
        weekly_iso = _roll(weekly_candidates)
        # Don't duplicate the monthly as a "weekly".
        if weekly_iso and weekly_iso != monthly_iso:
            out.append(("weekly", weekly_iso))

    return out


async def load_s2_expiry_inputs(underlying: str) -> dict[str, list[str]]:
    """Load per-underlying expiry inputs from the expiry calendar catalog.

    * ``monthlies`` — ``fo_expiry_catalog`` rows for this underlying (the
      monthly master: current + next month).
    * ``listed_expiries`` — every expiry the underlying has actually listed in
      ``option_premium_candles`` recently (weeklies + monthlies), so SENSEX
      gets its own BSE weeklies rather than NIFTY's NSE ladder.
    """
    underlying = str(underlying or "").upper()
    monthlies: list[str] = []
    listed: list[str] = []
    try:
        async with AsyncSessionLocal() as session:
            cat = await session.execute(
                text(
                    """
                    SELECT expiry FROM fo_expiry_catalog
                    WHERE underlying = :u AND expiry >= CURRENT_DATE
                    ORDER BY expiry
                    """
                ),
                {"u": underlying},
            )
            monthlies = [r[0].isoformat() for r in cat.fetchall() if r[0]]
            chain = await session.execute(
                text(
                    """
                    SELECT DISTINCT expiry FROM option_premium_candles
                    WHERE underlying = :u
                      AND expiry >= CURRENT_DATE
                      AND time > NOW() - INTERVAL '10 days'
                    ORDER BY expiry
                    """
                ),
                {"u": underlying},
            )
            listed = [r[0].isoformat() for r in chain.fetchall() if r[0]]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[s2_mp_of] expiry-catalog load failed for {underlying}: {exc}")
    return {"monthlies": monthlies, "listed_expiries": listed}


def resolve_s2_expiry_targets(
    underlying: str,
    expiry_scope: dict[str, Any],
) -> list[tuple[str, str]]:
    """Legacy scope-based resolver (fallback when the catalog is empty).

    Pick the expiry contracts S2 should trade for an underlying.

    Returns a list of ``(track, expiry_iso)`` tuples. ``track`` is
    ``"weekly"`` or ``"monthly"``. The order is monthly-first so the
    deeper contract takes precedence when capacity is tight (the lane's
    position cap counts all open positions equally).

    Resolution rules — keyed off the live ``expiry_scope`` payload that
    :func:`atm_watchlist_service.get_expiries` returns:

    * ``monthly`` — read from ``expiry_scope["index_monthlies"][underlying]``.
      This is the authoritative NSE/BSE monthly anchor (already
      day-of-week normalised by the watchlist service).
    * ``weekly`` — pick the **earliest** broker-listed expiry that is
      both later than today and earlier than the monthly anchor. We
      intentionally use the *nearest* weekly rather than the LAST one
      before the monthly because S2's MP+OF triggers want high gamma /
      short TTE; the nearest weekly is therefore preferred. When no such
      contract exists (e.g. SENSEX often has only one listed expiry per
      month, or weeklies were discontinued), the weekly slot is skipped.

    Only the underlyings in :data:`S2_EXPIRY_ROUTING` with ``"weekly"``
    in their track tuple are eligible for a weekly contract. The
    others get a single-element ``[("monthly", iso)]`` result even when
    multiple expiries exist in the broker chain — that's the policy
    layer the user requested.
    """
    underlying = str(underlying or "").upper()
    tracks = expiry_tracks_for(underlying)
    out: list[tuple[str, str]] = []

    index_monthlies = dict(expiry_scope.get("index_monthlies") or {})
    monthly_iso = str(index_monthlies.get(underlying) or "").strip()
    if "monthly" in tracks and monthly_iso:
        out.append(("monthly", monthly_iso))

    # The global ``expiries`` ladder is the NIFTY NSE board — it does NOT
    # describe SENSEX (BSE) or any other underlying's weeklies. Resolving a
    # weekly off it for SENSEX yields a NIFTY date with no SENSEX contract,
    # which blocks the underlying entirely (preparation-failure context).
    # So the weekly track is only resolvable for the underlying the ladder
    # actually represents (NIFTY). Everyone else degrades to monthly-only —
    # the safe, tradeable contract — until a per-underlying expiry source is
    # wired (a BSE ladder for SENSEX, etc.).
    if "weekly" in tracks and monthly_iso and underlying == "NIFTY":
        # Walk the broker's expiry ladder for anything earlier than monthly.
        # ``expiries`` is a sorted-ascending list of ISO date strings; we
        # pick the first one strictly between today and monthly_iso.
        from datetime import date as _date

        today_iso = _date.today().isoformat()
        all_expiries = list(expiry_scope.get("expiries") or [])
        weekly_candidate: Optional[str] = None
        for entry in all_expiries:
            iso = str(entry or "").strip()
            if not iso or iso == monthly_iso:
                continue
            if iso <= today_iso:
                continue
            if iso < monthly_iso:
                weekly_candidate = iso
                break
        if weekly_candidate:
            out.append(("weekly", weekly_candidate))

    return out


def shape_result_for_s2(
    result: dict[str, Any],
    *,
    underlying: str,
) -> dict[str, Any]:
    """Tag a commodity-MP result with S2-specific routing fields.

    Adds:
      * ``side``: "CE" / "PE" / None
      * ``expiry_tracks``: tuple of "weekly" / "monthly"
      * ``underlying``: copied through so the agent doesn't have to re-thread it
    """
    enriched = dict(result or {})
    enriched["side"] = map_signal_to_option_side(enriched.get("signal"))
    enriched["expiry_tracks"] = expiry_tracks_for(underlying)
    enriched["underlying"] = underlying
    return enriched


# ─── MP engine + prior profile (self-contained) ───────────────────────────


def build_index_market_profile(underlying: str, rows: list[dict[str, Any]]):
    """Build a MarketProfileSnapshot for an NSE index from 1-min bars.

    Mirrors :py:meth:`CommodityStrategyAgent._build_market_profile` but
    sized for an index instead of a futures contract — tick size comes
    from :data:`S2_TICK_SIZE`, IB length is 4 periods (= 1 hour at 15m).
    """
    if not rows or len(rows) < 2:
        return None
    # Late imports — these pull in heavyweight engines we don't want to
    # load until the flag is on. Same modules the commodity desk uses;
    # the previous `analytics.market_profile` path does not exist, which
    # silently broke MP+OF for S2 (it fell back to the legacy MACD path).
    from auction_intelligence.market_profile.engine import MarketProfileEngine
    from auction_intelligence.schemas import MarketBar
    from paper_engine.commodity_strategy_agent import _parse_iso_timestamp

    bars: list = []
    for row in rows:
        parsed = _parse_iso_timestamp(row.get("time"))
        close = row.get("close")
        if parsed is None or close is None:
            continue
        bars.append(
            MarketBar(
                timestamp=parsed,
                open=float(row.get("open", close) or close),
                high=float(row.get("high", close) or close),
                low=float(row.get("low", close) or close),
                close=float(close),
                volume=float(row.get("volume") or 0.0),
            )
        )
    if len(bars) < 2:
        return None
    engine = MarketProfileEngine(
        {
            "period_minutes": 15,
            "tick_size": S2_TICK_SIZE.get(underlying.upper(), 0.05),
            "initial_balance_periods": 4,
            "value_area_pct": 0.70,
            "min_tail_tpos": 2,
        }
    )
    try:
        return engine.build_profile(symbol=underlying, bars=bars)
    except Exception as exc:
        logger.debug(f"[s2_mp_of] MP build failed for {underlying}: {exc}")
        return None


# In-process cache for prior-session profiles: (underlying, today) -> snapshot.
# Cleared automatically when the calendar date rolls.
_S2_PRIOR_PROFILE_CACHE: dict[tuple[str, date], Any] = {}

# MP period length (minutes) used to key today's-profile cache: the build is
# reused until a new period closes. Matches build_index_market_profile's
# period_minutes=15.
S2_MP_PERIOD_MINUTES = 15

# In-process cache for today's developing profile, keyed by
# (underlying, session_date) -> (period_count, snapshot). Rebuilt only when
# the period count advances, so the heavy MarketProfileEngine build runs ~once
# per 15 min per underlying instead of every 60s scan.
_S2_TODAY_PROFILE_CACHE: dict[tuple[str, str], tuple[int, Any]] = {}

# Throttle cache: underlying -> (monotonic_ts, result). Returns the last
# result until S2_MPOF_THROTTLE_SECONDS elapse, capping per-scan CPU.
_S2_MPOF_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def load_strategy2_prior_session_profile(
    underlying: str,
    *,
    today: Optional[date] = None,
):
    """Build a snapshot of the prior trading session's MP for an index.

    Cached in-process for the rest of the calendar day. Used by the
    ``open_drive`` and ``va_migration`` triggers in the MP+OF evaluator;
    returns None when only one session of 1-min history is present.
    """
    from paper_engine.commodity_strategy_agent import (
        _filter_closed_interval_rows,
        _latest_session_rows,
        _parse_iso_timestamp,
        IST,
    )

    if today is None:
        from paper_engine.commodity_strategy_agent import _now_ist
        today = _now_ist().date()
    key = (underlying.upper(), today)
    if key in _S2_PRIOR_PROFILE_CACHE:
        return _S2_PRIOR_PROFILE_CACHE[key]

    prior_profile = None
    try:
        candles = await load_index_1m_spot(underlying, lookback_days=5)
        if candles:
            closed = _filter_closed_interval_rows(candles, interval="1minute")
            closed = _filter_nse_session_hours(closed)
            latest_session, latest_date = _latest_session_rows(closed)
            if latest_date is not None:
                prior_rows = [
                    c for c in closed
                    if (
                        (parsed := _parse_iso_timestamp(c.get("time"))) is not None
                        and parsed.astimezone(IST).date() < latest_date
                    )
                ]
                if prior_rows:
                    prior_session, _prior_date = _latest_session_rows(prior_rows)
                    if prior_session:
                        prior_profile = build_index_market_profile(underlying, prior_session)
    except Exception as exc:
        logger.debug(f"[s2_mp_of] prior MP load failed for {underlying}: {exc}")
        prior_profile = None

    _S2_PRIOR_PROFILE_CACHE[key] = prior_profile
    return prior_profile


# ─── Top-level evaluator ──────────────────────────────────────────────────


async def evaluate_strategy2_mp_of(
    *,
    underlying: str,
    started_at: Optional[datetime] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the MP+OF evaluator on 1-min index spot and return an S2 signal.

    Loads everything internally — caller just provides the underlying.
    Returns the standard commodity-MP result dict with three S2-specific
    routing fields appended (``side``, ``expiry_tracks``, ``underlying``).

    When there isn't enough 1-minute history yet, returns ``{"signal":
    None, "reason": "insufficient_1m_spot", ...}`` so the caller can
    fall through to its legacy path.

    With ``persist=True`` (default) today's profile is saved to disk
    once IB has printed, so the historical timeline (Yesterday / Week /
    Month) populates automatically without a separate batch job.
    """
    from time import monotonic

    # Throttle: a full evaluation is CPU-heavy, so recompute each underlying
    # at most once per S2_MPOF_THROTTLE_SECONDS and return the cached result
    # in between. Caps per-scan CPU on the single-core box.
    u_key = str(underlying or "").upper()
    now_mono = monotonic()
    cached_result = _S2_MPOF_RESULT_CACHE.get(u_key)
    if cached_result is not None and (now_mono - cached_result[0]) < S2_MPOF_THROTTLE_SECONDS:
        return cached_result[1]

    # Late imports keep this module cheap when the flag is off.
    from paper_engine.commodity_mp_signal import evaluate_commodity_mp_signal
    from paper_engine.commodity_strategy_agent import (
        _infer_09ist_anchor,
        _compute_atr_series,
        _latest_session_rows,
        _filter_closed_interval_rows,
    )
    from paper_engine.commodity_profile_store import (
        build_daily_profile_from_snapshot,
        save_profile,
    )

    closed_1m_raw = await load_index_1m_spot(underlying)
    closed_1m = _filter_closed_interval_rows(closed_1m_raw, interval="1minute")
    # Scope to the NSE regular session (09:15–15:30 IST). The index 1-min
    # spot feed keeps writing flat candles before the open and after the
    # close; including those minutes inflates the session (~3x) and corrupts
    # the TPO profile, value area and CVD. Mirrors how the commodity desk
    # scopes to MCX hours.
    closed_1m = _filter_nse_session_hours(closed_1m)
    if not closed_1m or len(closed_1m) < 30:
        shaped = shape_result_for_s2(
            {
                "signal": None,
                "reason": "insufficient_1m_spot",
                "rows_seen": len(closed_1m or []),
            },
            underlying=underlying,
        )
        _S2_MPOF_RESULT_CACHE[u_key] = (now_mono, shaped)
        return shaped

    session_rows, session_date = _latest_session_rows(closed_1m)
    if not session_rows:
        shaped = shape_result_for_s2(
            {"signal": None, "reason": "no_session_rows"},
            underlying=underlying,
        )
        _S2_MPOF_RESULT_CACHE[u_key] = (now_mono, shaped)
        return shaped

    # Cache the (CPU-heavy) MarketProfileEngine build. Rebuilding it on every
    # 60s scan for all five indices saturates the box's single core and
    # blocks the event loop (health checks time out). The TPO profile only
    # changes materially when a new 15-min period closes, so we rebuild only
    # when the period count advances; the cheap trigger evaluation below still
    # runs fresh against the latest bars every scan.
    period_count = max(1, len(session_rows) // S2_MP_PERIOD_MINUTES)
    profile_key = (underlying.upper(), str(session_date))
    cached_profile = _S2_TODAY_PROFILE_CACHE.get(profile_key)
    if cached_profile is not None and cached_profile[0] == period_count:
        today_profile = cached_profile[1]
    else:
        today_profile = build_index_market_profile(underlying, session_rows)
        _S2_TODAY_PROFILE_CACHE[profile_key] = (period_count, today_profile)
        # Keep the cache to one entry per underlying (drop stale sessions).
        for k in [k for k in _S2_TODAY_PROFILE_CACHE if k[0] == underlying.upper() and k != profile_key]:
            _S2_TODAY_PROFILE_CACHE.pop(k, None)
    prior_profile = await load_strategy2_prior_session_profile(
        underlying, today=session_date,
    )
    cvd_anchor = _infer_09ist_anchor(closed_1m)
    # NB: despite the alias name, `_compute_atr_series` IS
    # `commodity_mp_signal._compute_atr`, which returns a single scalar ATR
    # (Optional[float]) — not a list/series. Subscripting it with `[-1]`
    # raised "'float' object is not subscriptable" on every NIFTY/SENSEX scan
    # (ATR is non-zero during market hours, so the `if atr_series` guard never
    # short-circuited), silently forcing the lane back onto the MACD fallback
    # and never running MP+OF. Use the scalar directly, exactly as the
    # commodity desk does in `_analyze_futures_symbol`.
    atr_1m = _compute_atr_series(closed_1m, period=14)

    result = evaluate_commodity_mp_signal(
        closed_1m,
        symbol=underlying,
        today_profile=today_profile,
        prior_profile=prior_profile,
        cvd_anchor_index=cvd_anchor,
        atr_1m=atr_1m,
    )

    if (
        persist
        and today_profile is not None
        and int(getattr(today_profile, "period_count", 0) or 0) >= S2_MP_MIN_PERIODS
    ):
        try:
            snapshot = build_daily_profile_from_snapshot(underlying, today_profile)
            if snapshot is not None:
                save_profile(snapshot)
        except Exception as exc:
            logger.debug(f"[s2_mp_of] persist failed for {underlying}: {exc}")

    shaped = shape_result_for_s2(result, underlying=underlying)
    _S2_MPOF_RESULT_CACHE[u_key] = (now_mono, shaped)
    return shaped
