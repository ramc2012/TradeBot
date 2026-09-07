"""The index directional long-options swing lane.

Holding horizon is 1 to 5 TRADING SESSIONS. That is stated in sessions, counted
in sessions, and charged for in calendar days, because those are three
different things and conflating them is what broke the first version of this
lane: it measured the hold in wall-clock 30-minute intervals, so an overnight
gap counted as 36 bars and every position that survived one night tripped its
max-hold on the next morning's first evaluation. The book showed it — 34 trades,
average hold 0.262 days, 27 of them closed in the session they opened.

Order of operations per pass:

  1. MARK AND MANAGE what is held. This runs even when the volatility surface
     fails to fit. The previous version returned early on `surface_unusable`
     (264 occurrences in the journal) before marks were taken, so stops, targets
     and expiry settlement were all skipped on exactly the bars where the data
     was worst — recreating the stranded-open-row failure the package exists to
     avoid.
  2. DECIDE, from the whole factor panel rather than one signal, at most once
     per session per underlying.

Every path ends in a journal write, including the ones that do nothing.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper import book, journal
from directional_options.index_paper.attribution import attribute_pathwise, load_marks
from directional_options.index_paper.costs import (
    CostModel,
    estimate_fill,
    round_trip_cost_premium_fraction,
)
from directional_options.index_paper.factors import FactorPanel, build_panel
from directional_options.index_paper.horizon import (
    CALENDAR_DAYS_PER_YEAR,
    ExpiryGeometry,
    HoldPlan,
    calendar_days_for_sessions,
    evaluate_geometry,
    ist_session_date,
    plan_hold,
    sessions_between,
    sessions_to_expiry,
)
from directional_options.index_paper.schemas import (
    DirectionalView,
    GateResult,
    PaperPosition,
)
from directional_options.index_paper.sizing import size_position, vrp_risk_multiplier
from directional_options.index_paper.store import ensure_tables
from directional_options.vol.blackscholes import black76_greeks, black76_price
from directional_options.vol.realized import DailySeries, load_daily_series
from directional_options.vol.series import front_coordinates, minimum_variance_delta
from directional_options.vol.surface import (
    DEFAULT_RISK_FREE,
    INDEX_UNIVERSE,
    SOURCE_INTERVAL,
    SurfaceSlice,
    SurfaceSnapshot,
    build_snapshot_from_rows,
    expiry_datetime_utc,
    latest_bar_ts,
    load_chain_bar,
)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class IndexPaperConfig:
    """Every threshold in one place.

    Nothing here is a risk floor in rupees or basis points — the stop and the
    size come out of `sizing`, which derives them from the option's own greeks
    and the index's implied vol. What lives here are structural limits, which
    genuinely are constants.
    """

    initial_capital: float = 3_000_000.0

    # 1.2% of capital per trade, and the reason is GRANULARITY, not appetite.
    # One BANKNIFTY lot at 25 DTE risks roughly Rs 12,000 of premium against a
    # modelled adverse move. At the original 0.4% the budget was Rs 12,000 — so
    # full conviction bought exactly one lot and ANY downscaling bought zero.
    # The journal showed it plainly: budgets of Rs 10,406-10,876 against a
    # risk-per-lot of Rs 11,017-12,676, refused as `below_one_lot` while the
    # panel multiplier was a perfectly healthy 0.87-0.91. A budget that cannot
    # express conviction as anything but one-or-nothing is not sizing.
    #
    # At 1.2% full conviction is about three lots, and the position still
    # clears one lot down to a 0.34x multiplier. Three concurrent positions put
    # ~3.6% of capital at modelled risk.
    risk_fraction: float = 0.012

    # ── horizon ─────────────────────────────────────────────────────────────
    horizon_sessions: int = 3          # target hold, inside the 1-5 session band
    min_horizon_sessions: int = 1
    max_horizon_sessions: int = 5
    expiry_buffer_sessions: int = 1    # never plan to hold into the last session

    # ── exposure ────────────────────────────────────────────────────────────
    max_concurrent_positions: int = 3
    max_positions_per_underlying: int = 1
    # A swing lane has no business re-deciding thirteen times a session. Capping
    # it at one keeps the rejection journal readable: the first version produced
    # 535 of ~1100 decisions dying on a capacity limit, which drowned every
    # other reason code.
    one_entry_decision_per_session: bool = True
    entry_decision_after_ist: time = time(13, 0)

    # ── contract selection ──────────────────────────────────────────────────
    target_abs_delta: float = 0.40
    min_abs_delta: float = 0.25
    max_abs_delta: float = 0.60
    min_contract_oi: float = 5_000.0   # NOT volume: it is 0 on 97.5% of rows

    # A delta-0.40 call at 25 DTE needs 0.15 of a 1-sigma move to break even and
    # clears it 49.0% of the time; at 4 DTE it needs 0.43 sigma and clears it
    # 40.7%. Ranking candidates by this ratio, rather than taking the nearest
    # expiry, is the single largest structural edge available to the lane.
    max_breakeven_ratio: float = 0.45
    max_round_trip_cost_fraction: float = 0.06

    # ── decision ────────────────────────────────────────────────────────────
    min_direction_score: float = 0.15
    min_direction_agreement: int = 2   # factors that must point the same way
    min_size_score: float = -0.60      # below this the panel says "not now"

    # ── risk ────────────────────────────────────────────────────────────────
    stop_sigmas: float = 1.0
    reward_multiple: float = 2.0
    max_premium_fraction: float = 0.02
    max_lots: int = 20
    vrp_rich_threshold: float = 0.02
    vrp_risk_floor: float = 0.35

    max_bar_age_minutes: float = 240.0
    # Fill at the NEXT bar, not the one the decision was made on. The chain
    # sweep lands 45-60 minutes behind its bar, so a decision stamped at bar T
    # uses information a live pass at T would not yet have had; filling at T+1
    # is the minimum honest correction. This lane's own directional research
    # found a measured entry edge flipped negative under exactly one bar of
    # fill lag, so it is not a rounding detail.
    entry_lag_bars: int = 1
    risk_free: float = DEFAULT_RISK_FREE
    cost_model: CostModel = field(default_factory=CostModel)


@dataclass
class PassResult:
    underlying: str
    bar_ts: datetime | None
    status: str
    reason: str = ""
    marks: int = 0
    exits: int = 0
    entries: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "bar_ts": self.bar_ts.isoformat() if self.bar_ts else None,
            "status": self.status,
            "reason": self.reason,
            "marks": self.marks,
            "exits": self.exits,
            "entries": self.entries,
            "decisions": self.decisions,
        }


# ── data helpers ────────────────────────────────────────────────────────────

_CONTRACT_QUOTE = text(
    """
    SELECT DISTINCT ON (expiry, strike, option_type)
           close, volume, oi, underlying_price, iv, synced_at
    FROM option_premium_candles
    WHERE underlying = :underlying
      AND interval = :interval
      AND time = :bar_ts
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
    ORDER BY expiry, strike, option_type, synced_at DESC
    """
)


async def contract_quote(
    underlying: str,
    bar_ts: datetime,
    expiry: date,
    strike: float,
    option_type: str,
    *,
    interval: str = SOURCE_INTERVAL,
) -> dict[str, Any] | None:
    """Fetch the quote for EXACTLY this contract, or nothing.

    The four-way key match is not over-engineering. A sibling lane resolved a
    held leg's mark by nearest-match and booked a different contract's premium
    on 51% of its closes, fabricating more than half its lifetime P&L in both
    directions. There is no fallback to a neighbouring strike here.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _CONTRACT_QUOTE,
                {
                    "underlying": underlying,
                    "interval": interval,
                    "bar_ts": bar_ts,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                },
            )
        ).mappings().first()
    return dict(row) if row else None


_SPOT_AT_SQL = text(
    """
    WITH dominant AS (
        SELECT instrument_key
        FROM underlying_spot_candles
        WHERE underlying = :underlying AND interval = :interval
          AND time >= :lower AND time <= :upper
        GROUP BY instrument_key ORDER BY count(*) DESC LIMIT 1
    )
    SELECT c.close
    FROM underlying_spot_candles c
    JOIN dominant d ON d.instrument_key = c.instrument_key
    WHERE c.underlying = :underlying AND c.interval = :interval
      AND c.time >= :lower AND c.time <= :upper
      AND (c.time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:15' AND TIME '15:30'
    ORDER BY c.time DESC
    LIMIT 1
    """
)


async def index_spot_at(
    underlying: str, ts: datetime, *, interval: str = "30minute", window_hours: int = 72
) -> float | None:
    """Last session close at or before `ts`, off the dominant instrument key."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _SPOT_AT_SQL,
                {
                    "underlying": underlying,
                    "interval": interval,
                    "lower": ts - timedelta(hours=window_hours),
                    "upper": ts,
                },
            )
        ).first()
    return float(row.close) if row and row.close is not None else None


_LOT_SIZES_SQL = text(
    """
    SELECT lot_size, count(*) AS n
    FROM fo_contract_catalog
    WHERE underlying = :underlying AND expiry = :expiry AND lot_size IS NOT NULL
    GROUP BY lot_size
    """
)

_LOT_SIZE_FALLBACK = text(
    """
    SELECT lot_size, count(*) AS n
    FROM fo_contract_catalog
    WHERE underlying = :underlying AND lot_size IS NOT NULL AND expiry <= :expiry
      AND expiry >= :lower
    GROUP BY lot_size, expiry
    ORDER BY expiry DESC
    LIMIT 5
    """
)


async def resolve_lot_size(underlying: str, expiry: date) -> int | None:
    """Effective-dated lot size, taken as the MODE for that expiry.

    Index lot sizes have been re-based repeatedly in this database's history
    (BANKNIFTY 30 -> 35 -> 30; NIFTY to 65 from 31-Dec-2025) after SEBI moved
    the target contract value to Rs 15-20 lakh. The first version ordered by
    `lot_size DESC`, which deterministically picks the LARGER of any two values
    present and silently over-sizes every notional derived from it. The modal
    value is right whichever way a stray row is wrong.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(_LOT_SIZES_SQL, {"underlying": underlying, "expiry": expiry})
        ).all()
        if rows:
            return int(max(rows, key=lambda r: r.n).lot_size)
        rows = (
            await session.execute(
                _LOT_SIZE_FALLBACK,
                {
                    "underlying": underlying,
                    "expiry": expiry,
                    "lower": expiry - timedelta(days=400),
                },
            )
        ).all()
    return int(rows[0].lot_size) if rows else None


_DECIDED_SQL = text(
    """
    SELECT count(*) AS n
    FROM index_paper_decisions
    WHERE underlying = :underlying
      AND session_date = :session_date
      AND action IN ('enter', 'skip')
      AND (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
    """
)


async def already_decided_this_session(
    underlying: str, session_date: date, run_id: str | None
) -> bool:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _DECIDED_SQL,
                {"underlying": underlying, "session_date": session_date, "run_id": run_id},
            )
        ).first()
    return bool(row and row.n)


_NEXT_BAR_SQL = text(
    """
    SELECT DISTINCT time AS ts
    FROM option_premium_candles
    WHERE underlying = :underlying
      AND interval = :interval
      AND time > :after
      AND time <= :upper
    ORDER BY time
    LIMIT :lag
    """
)


async def next_bars(
    underlying: str, after: datetime, lag: int, *, interval: str = SOURCE_INTERVAL
) -> list[datetime]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _NEXT_BAR_SQL,
                {
                    "underlying": underlying,
                    "interval": interval,
                    "after": after,
                    "upper": after + timedelta(days=4),
                    "lag": max(int(lag), 1),
                },
            )
        ).all()
    return [r.ts for r in rows]


# ── surface helpers ─────────────────────────────────────────────────────────


def _slice_for_expiry(snapshot: SurfaceSnapshot | None, expiry: date) -> SurfaceSlice | None:
    if snapshot is None:
        return None
    for sl in snapshot.slices:
        if sl.expiry == expiry and sl.ok:
            return sl
    return None


def _theoretical_mark(
    sl: SurfaceSlice, strike: float, option_type: str, r: float
) -> tuple[float, float] | None:
    """(premium, iv) from the fitted slice when the contract did not print."""
    if not sl.ok or not sl.forward or sl.forward <= 0 or strike <= 0:
        return None
    k = math.log(strike / sl.forward)
    if sl.fit.k_min is not None and (k < sl.fit.k_min - 1e-9 or k > sl.fit.k_max + 1e-9):
        return None
    iv = float(sl.fit.params.implied_vol(k, sl.tenor_years))
    # Priced at the SAME rate the quote was inverted at. The first version
    # solved implied vol at r=0.065 and then priced the theoretical mark at
    # r=0.0, so every surface-derived mark was undiscounted while the vol that
    # produced it was discounted.
    premium = black76_price(sl.forward, strike, sl.tenor_years, iv, option_type, r)
    return premium, iv


def _greeks_for(sl: SurfaceSlice, strike: float, option_type: str, iv: float, r: float):
    return black76_greeks(sl.forward, strike, sl.tenor_years, iv, option_type, r, spot=sl.spot)


def _mv_delta(sl: SurfaceSlice, strike: float, greeks) -> float | None:
    if not sl.ok or not sl.forward or not sl.spot:
        return None
    k = math.log(strike / sl.forward)
    w = float(sl.fit.params.total_variance(k))
    dw = float(sl.fit.params.d_total_variance(k))
    if w <= 0 or sl.tenor_years <= 0:
        return None
    dsigma_dk = dw / (2.0 * math.sqrt(w * sl.tenor_years))
    return minimum_variance_delta(greeks.delta, greeks.vega, dsigma_dk, sl.spot)


async def _vol_context(snapshot: SurfaceSnapshot) -> dict[str, Any]:
    from directional_options.vol.store import latest_tenor_metrics

    front = front_coordinates(snapshot)
    ctx: dict[str, Any] = {
        "bar_ts": snapshot.ts.isoformat(),
        "front_tenor_days": front.tenor_days if front else None,
        "atm_iv": front.atm_iv() if front else None,
        "rr_25": front.rr_25 if front else None,
        "bf_25": front.bf_25 if front else None,
        "skew_slope": front.skew_slope if front else None,
        "vrp_spread": None,
        "vrp_ratio": None,
        "realized_vol": None,
        "rv_percentile": None,
    }
    metrics = await latest_tenor_metrics(
        snapshot.underlying,
        lower=snapshot.ts - timedelta(hours=6),
        upper=snapshot.ts + timedelta(minutes=1),
        tenor_kind="front",
    )
    if metrics:
        for key in ("vrp_spread", "vrp_ratio", "realized_vol", "rv_percentile"):
            ctx[key] = metrics.get(key)
    return ctx


# ── holding: mark, exit, settle ─────────────────────────────────────────────


async def settle_if_expired(
    position: PaperPosition,
    now_ts: datetime,
    config: IndexPaperConfig,
    *,
    view: dict[str, Any],
    vol_ctx: dict[str, Any],
    run_id: str | None,
) -> bool:
    """Cash-settle a position whose contract has expired.

    Without this a held leg that stops printing — which happens routinely,
    because the chain sweep tracks strikes near the money and abandons one that
    has drifted — stays open forever: no quote means no mark, and no mark means
    the exit rules never run. An expired option does not need a quote. It
    settles at intrinsic against the closing index level, and that is knowable.
    """
    expiry_ts = expiry_datetime_utc(position.expiry)
    if now_ts < expiry_ts:
        return False

    spot = await index_spot_at(position.underlying, expiry_ts)
    if spot is None:
        return False

    intrinsic = (
        max(spot - position.strike, 0.0)
        if position.option_type == "CE"
        else max(position.strike - spot, 0.0)
    )
    charges = (
        config.cost_model.fees.charges(intrinsic, position.quantity, "sell")
        if intrinsic > 0
        else {"total": 0.0}
    )
    realized = (
        (intrinsic - position.entry_premium) * position.quantity
        - position.entry_cost
        - charges["total"]
    )

    if not await book.close_position(
        position.position_id,
        exit_ts=expiry_ts,
        exit_premium=intrinsic,
        exit_iv=None,
        exit_cost=charges["total"],
        exit_reason="expiry_settlement",
        realized_pnl=realized,
    ):
        return False

    await journal.record_decision(
        run_id=run_id,
        decided_at=expiry_ts,
        session_date=ist_session_date(expiry_ts),
        bar_ts=now_ts,
        underlying=position.underlying,
        action="exit",
        reason_code="expiry_settlement",
        reason=(
            f"settled at intrinsic {intrinsic:.2f} against a {spot:.2f} close; "
            "the contract had stopped printing"
        ),
        position_id=position.position_id,
        view=view,
        vol_context=vol_ctx,
    )
    closed = await book.get_position(position.position_id)
    if closed:
        marks = await load_marks(position.position_id)
        await journal.record_attribution(
            attribute_pathwise(closed, marks, exit_spot=spot, exit_iv=None).as_dict()
        )
    return True


def _exit_decision(
    position: PaperPosition,
    premium: float,
    session_date: date,
    config: IndexPaperConfig,
    sl: SurfaceSlice | None,
    panel_direction: float | None,
) -> tuple[str | None, dict[str, Any]]:
    """The exit ladder, rung by rung, in sessions rather than bars.

    Returns (reason, diagnostics). The diagnostics go to the journal whether or
    not a rung fires, because "how close was it" is the only way to learn that a
    rung is unreachable — the first version's target rung fired 0 times out of
    179 exits and nothing in the record said why.
    """
    entry_day = ist_session_date(position.entry_ts)
    held = sessions_between(entry_day, session_date)
    to_expiry = sessions_to_expiry(session_date, position.expiry)
    max_hold = int(position.max_hold_bars or config.horizon_sessions)

    diag = {
        "sessions_held": held,
        "max_hold_sessions": max_hold,
        "sessions_to_expiry": to_expiry,
        "premium": premium,
        "stop": position.stop_premium,
        "target": position.target_premium,
        "distance_to_stop": None if position.stop_premium is None else premium - position.stop_premium,
        "distance_to_target": None if position.target_premium is None else position.target_premium - premium,
        "panel_direction": panel_direction,
    }

    if to_expiry <= config.expiry_buffer_sessions:
        return "expiry_force_close", diag
    if position.stop_premium is not None and premium <= position.stop_premium:
        return "stop", diag
    if position.target_premium is not None and premium >= position.target_premium:
        return "target", diag
    if held >= max_hold:
        return "max_hold", diag
    if panel_direction is not None:
        wanted = "CE" if panel_direction > 0 else "PE"
        if wanted != position.option_type and abs(panel_direction) >= config.min_direction_score:
            return "view_flip", diag
    return None, diag


async def manage_open_positions(
    underlying: str,
    bar_ts: datetime,
    snapshot: SurfaceSnapshot | None,
    config: IndexPaperConfig,
    vol_ctx: dict[str, Any],
    result: PassResult,
    *,
    panel_direction: float | None = None,
    view: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    """Mark and manage the book. Runs with or without a usable surface.

    A missing surface degrades the MARK (a printed quote is still a quote, and
    an expired contract still settles); it must never suspend risk management.
    """
    positions = await book.list_open(underlying)
    if not positions:
        return
    session_date = ist_session_date(bar_ts)
    view = view or {}

    for position in positions:
        sl = _slice_for_expiry(snapshot, position.expiry)
        quote = await contract_quote(
            underlying, bar_ts, position.expiry, position.strike, position.option_type
        )

        mark_source, premium, iv = "chain_bar", None, None
        if quote and quote.get("close") is not None:
            premium = float(quote["close"])
            iv = float(quote["iv"]) if quote.get("iv") is not None else None
        elif sl is not None:
            theoretical = _theoretical_mark(sl, position.strike, position.option_type, config.risk_free)
            if theoretical:
                premium, iv = theoretical
                mark_source = "surface_theoretical"

        if premium is None:
            if await settle_if_expired(
                position, bar_ts, config, view=view, vol_ctx=vol_ctx, run_id=run_id
            ):
                result.exits += 1
                continue
            await journal.record_decision(
                run_id=run_id,
                decided_at=bar_ts,
                session_date=session_date,
                bar_ts=bar_ts,
                underlying=underlying,
                action="hold",
                reason_code="mark_unavailable",
                reason=(
                    f"{position.strike:g}{position.option_type} exp {position.expiry} did not print "
                    "and is outside any fitted surface; carrying the stale mark rather than inventing one"
                ),
                position_id=position.position_id,
                view=view,
                vol_context=vol_ctx,
            )
            continue

        greeks = _greeks_for(sl, position.strike, position.option_type, iv, config.risk_free) if (sl and iv) else None
        mv_delta = _mv_delta(sl, position.strike, greeks) if (sl and greeks) else None

        # entry_premium is already the adverse fill price and entry_cost is
        # CHARGES ONLY, so slippage is counted exactly once.
        unrealized = (premium - position.entry_premium) * position.quantity - position.entry_cost
        await book.mark_position(
            position.position_id,
            latest_ts=bar_ts,
            latest_premium=premium,
            latest_iv=iv,
            unrealized_pnl=unrealized,
        )
        await journal.record_mark(
            ts=bar_ts,
            position_id=position.position_id,
            premium=premium,
            unrealized_pnl=unrealized,
            iv=iv,
            forward=sl.forward if sl else None,
            spot=sl.spot if sl else None,
            delta=greeks.delta if greeks else None,
            gamma=greeks.gamma if greeks else None,
            vega=greeks.vega if greeks else None,
            theta=greeks.theta if greeks else None,
            mv_delta=mv_delta,
            source=mark_source,
        )
        result.marks += 1

        exit_reason, diag = _exit_decision(
            position, premium, session_date, config, sl, panel_direction
        )
        if exit_reason is None:
            continue

        fill = estimate_fill(
            premium,
            position.quantity,
            "sell",
            "exit",
            model=config.cost_model,
            volume=(quote or {}).get("volume"),
            oi=(quote or {}).get("oi"),
            log_moneyness=(math.log(position.strike / sl.forward) if sl and sl.forward else None),
            sigma=iv,
            vega_per_vol_point=(greeks.vega * 0.01) if greeks else None,
        )
        exit_charges = fill.charges["total"]
        realized = (
            (fill.fill_price - position.entry_premium) * position.quantity
            - position.entry_cost
            - exit_charges
        )

        if not await book.close_position(
            position.position_id,
            exit_ts=bar_ts,
            exit_premium=fill.fill_price,
            exit_iv=iv,
            exit_cost=exit_charges,
            exit_reason=exit_reason,
            realized_pnl=realized,
        ):
            continue

        await journal.record_fill(
            position_id=position.position_id,
            filled_at=bar_ts,
            estimate=fill,
            detail={"mark_source": mark_source, "exit_reason": exit_reason, "ladder": diag},
        )
        await journal.record_decision(
            run_id=run_id,
            decided_at=bar_ts,
            session_date=session_date,
            bar_ts=bar_ts,
            underlying=underlying,
            action="exit",
            reason_code=exit_reason,
            reason=f"exited at {fill.fill_price:.2f} (reference {premium:.2f}) after {diag['sessions_held']} session(s)",
            position_id=position.position_id,
            view=view,
            vol_context=vol_ctx,
            candidate={"mark_source": mark_source, "ladder": diag},
        )

        closed_position = await book.get_position(position.position_id)
        if closed_position:
            marks = await load_marks(position.position_id)
            await journal.record_attribution(
                attribute_pathwise(
                    closed_position, marks, exit_spot=sl.spot if sl else None, exit_iv=iv
                ).as_dict()
            )
        result.exits += 1


# ── entry ───────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    slice_: SurfaceSlice
    strike: float
    option_type: str
    premium: float
    implied_vol: float
    log_moneyness: float
    oi: int | None
    volume: int | None
    delta: float
    gamma: float
    vega: float
    theta: float
    lot_size: int
    hold: HoldPlan
    geometry: ExpiryGeometry

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiry": self.slice_.expiry.isoformat(),
            "days_to_expiry": self.slice_.days_to_expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "premium": self.premium,
            "implied_vol": self.implied_vol,
            "log_moneyness": self.log_moneyness,
            "oi": self.oi,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "lot_size": self.lot_size,
            "hold": self.hold.as_dict(),
            "geometry": self.geometry.as_dict(),
        }


async def select_candidate(
    underlying: str,
    snapshot: SurfaceSnapshot,
    side: str,
    session_date: date,
    config: IndexPaperConfig,
) -> tuple[Candidate | None, dict[str, Any]]:
    """Pick the contract with the best BREAKEVEN GEOMETRY, across all expiries.

    Two departures from the obvious approach, both measured:

      * Expiry is chosen by geometry, not proximity. Taking the nearest expiry
        systematically selects the worst contract available for a multi-session
        hold — at a 3-session horizon BANKNIFTY's 25-DTE monthly needs 0.15 of a
        sigma to break even where NIFTY's 4-DTE weekly needs 0.43.
      * Strike is chosen by DELTA, not by strike offset. A fixed distance from
        the money is a different exposure at every vol level, which makes a
        series of such trades non-comparable.

    The diagnostics returned name the binding constraint, so a conjunction can
    never hide which of the filters actually emptied the set.
    """
    survivors = Counter()
    detail: list[str] = []
    best: Candidate | None = None
    best_ratio = math.inf

    for sl in snapshot.usable_slices():
        hold = plan_hold(
            session_date, sl.expiry, config.horizon_sessions,
            expiry_buffer_sessions=config.expiry_buffer_sessions,
        )
        survivors["expiries_seen"] += 1
        if not hold.feasible:
            detail.append(f"{sl.expiry} ({sl.days_to_expiry:.0f}d): {hold.reason}")
            continue
        survivors["expiries_holdable"] += 1

        lot_size = await resolve_lot_size(underlying, sl.expiry)
        if not lot_size:
            detail.append(f"{sl.expiry}: no lot size in fo_contract_catalog")
            continue

        for quote in sl.quotes:
            if quote.option_type != side:
                continue
            survivors["quotes_on_side"] += 1
            greeks = _greeks_for(sl, quote.strike, side, quote.implied_vol, config.risk_free)
            abs_delta = abs(greeks.delta)
            if not (config.min_abs_delta <= abs_delta <= config.max_abs_delta):
                continue
            survivors["in_delta_band"] += 1
            if (quote.oi or 0) < config.min_contract_oi:
                continue
            survivors["above_oi_floor"] += 1

            geo = evaluate_geometry(
                sl,
                strike=quote.strike, option_type=side, premium=quote.price,
                implied_vol=quote.implied_vol, delta=greeks.delta, oi=quote.oi,
                log_moneyness=quote.log_moneyness, lot_size=lot_size,
                entry_day=session_date, hold_sessions=hold.planned_sessions,
                cost_model=config.cost_model, r=config.risk_free,
            )
            if geo.breakeven_ratio is None:
                continue
            survivors["geometry_computable"] += 1
            if geo.breakeven_ratio > config.max_breakeven_ratio:
                continue
            survivors["within_breakeven_ceiling"] += 1

            # Among contracts that clear every filter, prefer the one that has
            # to travel least — then, as a tiebreak, the one closest to target
            # delta so the exposure stays comparable across days.
            rank = geo.breakeven_ratio + 0.05 * abs(abs_delta - config.target_abs_delta)
            if rank < best_ratio:
                best_ratio = rank
                best = Candidate(
                    slice_=sl, strike=quote.strike, option_type=side, premium=quote.price,
                    implied_vol=quote.implied_vol, log_moneyness=quote.log_moneyness,
                    oi=quote.oi, volume=quote.volume, delta=greeks.delta, gamma=greeks.gamma,
                    vega=greeks.vega, theta=greeks.theta, lot_size=lot_size,
                    hold=hold, geometry=geo,
                )

    binding = "none"
    for stage in (
        "expiries_holdable", "quotes_on_side", "in_delta_band",
        "above_oi_floor", "geometry_computable", "within_breakeven_ceiling",
    ):
        if survivors[stage] == 0:
            binding = stage
            break

    return best, {
        "survivors": dict(survivors),
        "binding_constraint": binding,
        "detail": detail[:6],
        "breakeven_ceiling": config.max_breakeven_ratio,
        "delta_band": [config.min_abs_delta, config.max_abs_delta],
    }


async def evaluate_entry(
    underlying: str,
    bar_ts: datetime,
    snapshot: SurfaceSnapshot,
    panel: FactorPanel,
    config: IndexPaperConfig,
    vol_ctx: dict[str, Any],
    daily_series: DailySeries,
    result: PassResult,
    *,
    run_id: str | None = None,
) -> None:
    session_date = ist_session_date(bar_ts)
    gates: list[GateResult] = []
    panel_payload = panel.as_dict()

    async def refuse(reason_code: str, reason: str, **extra: Any) -> None:
        await journal.record_decision(
            run_id=run_id, decided_at=bar_ts, session_date=session_date, bar_ts=bar_ts,
            underlying=underlying, action="skip", reason_code=reason_code, reason=reason,
            gates=gates, vol_context=vol_ctx, view=panel_payload, **extra,
        )
        result.decisions.append({"action": "skip", "reason_code": reason_code, "reason": reason})

    # ── exposure ────────────────────────────────────────────────────────────
    open_positions = await book.list_open()
    same = [p for p in open_positions if p.underlying == underlying]
    gates.append(GateResult("exposure_total", len(open_positions) < config.max_concurrent_positions,
                            observed=len(open_positions), threshold=config.max_concurrent_positions))
    gates.append(GateResult("exposure_underlying", len(same) < config.max_positions_per_underlying,
                            observed=len(same), threshold=config.max_positions_per_underlying))
    if same:
        await refuse("already_positioned", f"already holding {underlying}")
        return
    if len(open_positions) >= config.max_concurrent_positions:
        await refuse("max_exposure", f"{len(open_positions)} positions already open")
        return

    # ── direction, from the whole panel ─────────────────────────────────────
    direction_score, terms = panel.direction_score()
    agree, acting = panel.direction_agreement()
    gates.append(GateResult("direction_available", direction_score is not None,
                            detail="; ".join(terms)[:220]))
    if direction_score is None:
        await refuse("no_direction_factor", "no direction factor is acting: " + "; ".join(terms)[:200])
        return

    gates.append(GateResult("direction_conviction", abs(direction_score) >= config.min_direction_score,
                            observed=abs(direction_score), threshold=config.min_direction_score))
    gates.append(GateResult("direction_agreement", agree >= config.min_direction_agreement,
                            observed=agree, threshold=config.min_direction_agreement,
                            detail=f"{agree} of {acting} acting factors point the same way"))
    if abs(direction_score) < config.min_direction_score:
        await refuse(
            "direction_inconclusive",
            f"composite direction {direction_score:+.3f} below {config.min_direction_score:.2f} ({'; '.join(terms)[:150]})",
        )
        return
    if agree < config.min_direction_agreement:
        await refuse(
            "direction_disagreement",
            f"only {agree} of {acting} acting direction factors agree; need {config.min_direction_agreement}",
        )
        return

    side = "CE" if direction_score > 0 else "PE"

    # ── the panel's own view on whether to be long premium at all ───────────
    size_score, size_terms = panel.size_score()
    gates.append(GateResult("size_regime", size_score >= config.min_size_score,
                            observed=size_score, threshold=config.min_size_score,
                            detail="; ".join(size_terms)[:200]))
    if size_score < config.min_size_score:
        await refuse(
            "size_regime_hostile",
            f"composite size score {size_score:+.2f} below {config.min_size_score:.2f} — "
            f"the panel says this is not a regime to buy premium in ({'; '.join(size_terms)[:140]})",
        )
        return

    # ── contract ────────────────────────────────────────────────────────────
    candidate, diag = await select_candidate(underlying, snapshot, side, session_date, config)
    gates.append(GateResult("candidate_found", candidate is not None,
                            detail=f"binding={diag['binding_constraint']} survivors={diag['survivors']}"))
    if candidate is None:
        await refuse(
            f"no_candidate_{diag['binding_constraint']}",
            f"no {side} contract survived; binding constraint is {diag['binding_constraint']}. "
            + "; ".join(diag["detail"])[:220],
            candidate=diag,
        )
        return

    rt = round_trip_cost_premium_fraction(
        candidate.premium, candidate.lot_size, model=config.cost_model,
        volume=candidate.volume, oi=candidate.oi,
        log_moneyness=candidate.log_moneyness, sigma=candidate.implied_vol,
    )
    gates.append(GateResult("cost_viability", (rt or 1.0) <= config.max_round_trip_cost_fraction,
                            observed=rt, threshold=config.max_round_trip_cost_fraction))
    if rt is None or rt > config.max_round_trip_cost_fraction:
        await refuse(
            "cost_too_high",
            f"a round trip costs {(rt or 0) * 100:.1f}% of premium, above "
            f"{config.max_round_trip_cost_fraction * 100:.1f}%",
            candidate=candidate.as_dict(),
        )
        return

    # ── size ────────────────────────────────────────────────────────────────
    # The variance risk premium enters the budget ONCE, through the panel.
    # Applying `vrp_risk_multiplier` on top of a size score that already
    # contains `vrp_spread` compounded the same penalty twice: with premium
    # three vol points rich, the standalone multiplier floored at 0.35 and the
    # panel multiplier fell to ~0.5, leaving a budget of about Rs 2,100 against
    # a BANKNIFTY risk-per-lot near Rs 9,000. The lane refused every trade with
    # `below_one_lot` — economically defensible and analytically useless, since
    # a lane that never trades never measures anything.
    #
    # `vrp_risk_multiplier` is retained for callers that size without a panel;
    # it is reported here for comparison but not applied.
    vrp_mult, vrp_note = vrp_risk_multiplier(
        vol_ctx.get("vrp_spread"), rich_threshold=config.vrp_rich_threshold, floor=config.vrp_risk_floor
    )
    panel_mult = max(0.4, min(1.6, 1.0 + 0.6 * size_score))
    sizing = size_position(
        capital=config.initial_capital,
        premium=candidate.premium,
        lot_size=candidate.lot_size,
        spot=candidate.slice_.spot or candidate.slice_.forward or 0.0,
        atm_iv=vol_ctx.get("atm_iv") or candidate.implied_vol,
        delta=candidate.delta,
        vega=candidate.vega,
        theta_per_day=candidate.theta,
        horizon_sessions=candidate.hold.planned_sessions,
        horizon_calendar_days=candidate.hold.calendar_days,
        risk_fraction=config.risk_fraction,
        risk_multiplier=panel_mult,
        stop_sigmas=config.stop_sigmas,
        reward_multiple=config.reward_multiple,
        max_premium_fraction=config.max_premium_fraction,
        max_lots=config.max_lots,
    )
    sizing_payload = {
        **sizing.as_dict(),
        "vrp_note": vrp_note,
        "vrp_multiplier_not_applied": vrp_mult,
        "panel_multiplier": panel_mult,
        "size_score": size_score,
        "size_terms": size_terms,
    }
    gates.append(GateResult("sizing", sizing.approved, observed=sizing.lots, detail=sizing.reason))
    if not sizing.approved:
        await refuse(sizing.reason_code, sizing.reason,
                     candidate=candidate.as_dict(), sizing=sizing_payload)
        return

    # ── fill, at the LAGGED bar ─────────────────────────────────────────────
    fill_ts, fill_reference = bar_ts, candidate.premium
    if config.entry_lag_bars > 0:
        later = await next_bars(underlying, bar_ts, config.entry_lag_bars)
        lagged = later[config.entry_lag_bars - 1] if len(later) >= config.entry_lag_bars else None
        quote = (
            await contract_quote(
                underlying, lagged, candidate.slice_.expiry, candidate.strike, side
            )
            if lagged
            else None
        )
        gates.append(
            GateResult("lagged_fill_available", quote is not None and quote.get("close") is not None,
                       detail=f"fill bar {lagged.isoformat() if lagged else 'none'}")
        )
        if not quote or quote.get("close") is None:
            await refuse(
                "no_lagged_fill",
                f"the chosen contract did not print at the +{config.entry_lag_bars} bar; "
                "refusing to fill at a price that was not observable when the order would have gone in",
                candidate=candidate.as_dict(), sizing=sizing_payload,
            )
            return
        fill_ts, fill_reference = lagged, float(quote["close"])

    fill = estimate_fill(
        fill_reference, sizing.quantity, "buy", "entry",
        model=config.cost_model, volume=candidate.volume, oi=candidate.oi,
        log_moneyness=candidate.log_moneyness, sigma=candidate.implied_vol,
        vega_per_vol_point=candidate.vega * 0.01,
    )
    greeks = _greeks_for(
        candidate.slice_, candidate.strike, side, candidate.implied_vol, config.risk_free
    )
    adverse = sizing.adverse_move_per_unit or 0.0

    position = PaperPosition(
        position_id=book.new_position_id(underlying),
        status="open",
        session_date=session_date,
        underlying=underlying,
        expiry=candidate.slice_.expiry,
        strike=candidate.strike,
        option_type=side,
        lots=sizing.lots,
        lot_size=candidate.lot_size,
        quantity=sizing.quantity,
        entry_ts=fill_ts,
        entry_premium=fill.fill_price,
        # CHARGES ONLY. Slippage is already inside fill_price; the first version
        # stored total_cost here and every P&L expression then subtracted the
        # slippage a second time, inflating the published cost floor.
        entry_cost=fill.charges["total"],
        entry_iv=candidate.implied_vol,
        entry_forward=candidate.slice_.forward,
        entry_spot=candidate.slice_.spot,
        entry_delta=greeks.delta,
        entry_gamma=greeks.gamma,
        entry_vega=greeks.vega,
        entry_theta=greeks.theta,
        entry_mv_delta=_mv_delta(candidate.slice_, candidate.strike, greeks),
        stop_premium=max(fill.fill_price - adverse, 0.0),
        target_premium=fill.fill_price + adverse * config.reward_multiple,
        max_hold_bars=candidate.hold.planned_sessions,   # SESSIONS, not bars
        latest_ts=fill_ts,
        latest_premium=fill.fill_price,
        latest_iv=candidate.implied_vol,
        unrealized_pnl=-fill.charges["total"],
        payload={
            "panel": panel_payload,
            "vol_context": vol_ctx,
            "sizing": sizing_payload,
            "fill": fill.as_dict(),
            "candidate": candidate.as_dict(),
            "selection": diag,
            "round_trip_cost_fraction": rt,
            "entry_slippage_total": fill.slippage_per_unit * sizing.quantity,
            "decision_bar": bar_ts.isoformat(),
            "fill_bar": fill_ts.isoformat(),
            "signal_price": candidate.premium,
            "fill_reference_price": fill_reference,
        },
    )

    await book.open_position(position)
    await journal.record_fill(
        position_id=position.position_id, filled_at=fill_ts, estimate=fill,
        detail={
            "candidate": candidate.as_dict(),
            "direction_score": direction_score,
            "decision_bar": bar_ts.isoformat(),
            "signal_price": candidate.premium,
        },
    )
    await journal.record_decision(
        run_id=run_id, decided_at=bar_ts, session_date=session_date, bar_ts=bar_ts,
        underlying=underlying, action="enter", reason_code="entered",
        reason=(
            f"long {candidate.strike:g}{side} exp {candidate.slice_.expiry} "
            f"x{sizing.lots} lots at {fill.fill_price:.2f}; hold {candidate.hold.planned_sessions} "
            f"session(s), breakeven {candidate.geometry.breakeven_ratio:.2f} sigma"
        ),
        position_id=position.position_id, view=panel_payload, gates=gates,
        candidate=candidate.as_dict(), sizing=sizing_payload, vol_context=vol_ctx,
    )
    result.entries += 1
    result.decisions.append(
        {"action": "enter", "reason_code": "entered", "position_id": position.position_id}
    )


# ── pass orchestration ──────────────────────────────────────────────────────


async def probe_breakeven_ratio(
    underlying: str,
    snapshot: SurfaceSnapshot,
    session_date: date,
    config: IndexPaperConfig,
) -> float | None:
    """Best achievable breakeven ratio in the current opportunity set.

    Side-agnostic on purpose — it describes what the CHAIN is offering, not what
    the lane intends to do, so it can enter the factor panel before a direction
    has been chosen. A call and a put at the same |delta| have near-identical
    geometry, so the call side is a fair probe for both.
    """
    best = None
    for sl in snapshot.usable_slices():
        hold = plan_hold(
            session_date, sl.expiry, config.horizon_sessions,
            expiry_buffer_sessions=config.expiry_buffer_sessions,
        )
        if not hold.feasible:
            continue
        lot_size = await resolve_lot_size(underlying, sl.expiry)
        if not lot_size:
            continue
        target = None
        gap = math.inf
        for quote in sl.quotes:
            if quote.option_type != "CE":
                continue
            greeks = _greeks_for(sl, quote.strike, "CE", quote.implied_vol, config.risk_free)
            d = abs(abs(greeks.delta) - config.target_abs_delta)
            if d < gap and (quote.oi or 0) >= config.min_contract_oi:
                gap, target = d, (quote, greeks)
        if target is None:
            continue
        quote, greeks = target
        geo = evaluate_geometry(
            sl, strike=quote.strike, option_type="CE", premium=quote.price,
            implied_vol=quote.implied_vol, delta=greeks.delta, oi=quote.oi,
            log_moneyness=quote.log_moneyness, lot_size=lot_size,
            entry_day=session_date, hold_sessions=hold.planned_sessions,
            cost_model=config.cost_model, r=config.risk_free,
        )
        if geo.breakeven_ratio is not None and (best is None or geo.breakeven_ratio < best):
            best = geo.breakeven_ratio
    return best


async def run_underlying(
    underlying: str,
    *,
    config: IndexPaperConfig | None = None,
    as_of: datetime | None = None,
    bar_ts: datetime | None = None,
    run_id: str | None = None,
    daily_series: DailySeries | None = None,
) -> PassResult:
    cfg = config or IndexPaperConfig()
    symbol = underlying.strip().upper()
    if symbol not in INDEX_UNIVERSE:
        return PassResult(symbol, None, "out_of_scope", f"{symbol} is not in {INDEX_UNIVERSE}")

    ts = bar_ts or await latest_bar_ts(symbol, as_of=as_of)
    if ts is None:
        return PassResult(symbol, None, "no_data", "no chain bar with enough strikes in the window")

    session_date = ist_session_date(ts)
    result = PassResult(symbol, ts, "ok")

    rows = await load_chain_bar(symbol, ts)
    snapshot = build_snapshot_from_rows(
        symbol, ts, rows, r=cfg.risk_free, max_age_minutes=cfg.max_bar_age_minutes
    )
    vol_ctx = await _vol_context(snapshot) if snapshot.ok else {"bar_ts": ts.isoformat()}

    # Daily bars sliced to sessions that CLOSED BEFORE this one — a factor may
    # not see the session it is deciding in.
    if daily_series is None:
        daily_series = await load_daily_series(symbol, as_of=ts)
    as_of_bars = [b for b in daily_series.bars if b.day < session_date]

    breakeven_probe = (
        await probe_breakeven_ratio(symbol, snapshot, session_date, cfg) if snapshot.ok else None
    )
    panel = await build_panel(
        symbol, session_date, ts,
        daily_bars=as_of_bars, vol_ctx=vol_ctx, breakeven_ratio=breakeven_probe,
    )
    direction_score, _ = panel.direction_score()

    # 1. Risk management ALWAYS runs, surface or no surface.
    await manage_open_positions(
        symbol, ts, snapshot if snapshot.ok else None, cfg, vol_ctx, result,
        panel_direction=direction_score, view=panel.as_dict(), run_id=run_id,
    )

    # 2. Then, at most once a session, consider adding.
    if not snapshot.ok:
        result.status = snapshot.status
        result.reason = snapshot.reason
        await journal.record_decision(
            run_id=run_id, decided_at=ts, session_date=session_date, bar_ts=ts,
            underlying=symbol, action="skip", reason_code="surface_unusable",
            reason=snapshot.reason or snapshot.status,
            view=panel.as_dict(), vol_context=vol_ctx,
        )
        return result

    if ts.astimezone(IST).time() < cfg.entry_decision_after_ist:
        return result
    if cfg.one_entry_decision_per_session and await already_decided_this_session(
        symbol, session_date, run_id
    ):
        return result

    await journal.record_factor_panel(panel, run_id=run_id, bar_ts=ts)
    await evaluate_entry(
        symbol, ts, snapshot, panel, cfg, vol_ctx, daily_series, result, run_id=run_id
    )
    return result


async def run(
    *,
    underlyings: Sequence[str] = INDEX_UNIVERSE,
    config: IndexPaperConfig | None = None,
    as_of: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    cfg = config or IndexPaperConfig()
    await ensure_tables()
    run_id = run_id or f"live-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"

    results: list[PassResult] = []
    for symbol in underlyings:
        try:
            results.append(await run_underlying(symbol, config=cfg, as_of=as_of, run_id=run_id))
        except Exception as exc:
            logger.exception(f"[index_paper] {symbol} pass failed: {exc}")
            results.append(PassResult(symbol, None, "error", str(exc)))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "horizon_sessions": cfg.horizon_sessions,
        "results": [r.as_dict() for r in results],
        "book": await book.summary(cfg.initial_capital),
    }
