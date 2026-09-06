"""The index paper lane: manage what is held, then decide what to add.

Order of operations per pass is deliberate — marks and exits run BEFORE any
entry is considered, so a stop is never skipped because the entry logic threw,
and the exposure gate always sees the true book.

Every path through this module ends in a journal write.  There is no branch
that returns quietly, because a lane that trades nothing for a week and cannot
say why is indistinguishable from a broken one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper import book, journal
from directional_options.index_paper.adapters import flat_view, latest_view
from directional_options.index_paper.attribution import attribute_pathwise, load_marks
from directional_options.index_paper.costs import (
    CostModel,
    estimate_fill,
    round_trip_cost_premium_fraction,
)
from directional_options.index_paper.schemas import (
    Contract,
    DirectionalView,
    GateResult,
    PaperPosition,
)
from directional_options.index_paper.sizing import size_position, vrp_risk_multiplier
from directional_options.index_paper.store import ensure_tables
from directional_options.vol.blackscholes import black76_greeks, black76_price
from directional_options.vol.series import front_coordinates, minimum_variance_delta
from directional_options.vol.surface import (
    INDEX_UNIVERSE,
    expiry_datetime_utc,
    SOURCE_INTERVAL,
    SurfaceSlice,
    SurfaceSnapshot,
    build_snapshot_from_rows,
    latest_bar_ts,
    load_chain_bar,
)

IST = timezone(timedelta(hours=5, minutes=30))

ViewProvider = Callable[[str, datetime], Awaitable[DirectionalView]]


@dataclass
class IndexPaperConfig:
    """Every threshold the lane uses, in one place and all named.

    Nothing here is a risk floor stated in rupees or basis points — the stop
    and the size come out of `sizing`, which derives them from the option's own
    greeks and the index's implied vol.  What lives here are structural limits
    (how many positions, which maturities, how much cost is tolerable), which
    genuinely are constants.
    """

    initial_capital: float = 3_000_000.0
    risk_fraction: float = 0.004
    max_concurrent_positions: int = 3
    max_positions_per_underlying: int = 1

    min_confidence: float = 0.35
    target_abs_delta: float = 0.40
    min_abs_delta: float = 0.25
    max_abs_delta: float = 0.60

    min_dte: float = 1.0
    max_dte: float = 45.0
    force_close_dte: float = 0.6

    # NOT a volume floor.  Volume is absent from this chain feed (97.5% of
    # index option rows carry volume = 0 while OI is populated), so a volume
    # gate is an unpassable veto rather than a filter.  Liquidity is gated on
    # open interest alone.
    min_contract_oi: float = 5_000.0

    # A round trip that eats more than this fraction of the premium needs a
    # move larger than the cost before it earns anything.
    max_round_trip_cost_fraction: float = 0.06
    # The expected move must clear the round-trip cost by this multiple.
    min_edge_over_cost: float = 1.5

    max_bar_age_minutes: float = 240.0
    max_hold_bars: int = 13
    bar_minutes: float = 30.0

    stop_sigmas: float = 1.0
    reward_multiple: float = 2.0
    max_premium_fraction: float = 0.02
    max_lots: int = 20

    vrp_rich_threshold: float = 0.02
    vrp_risk_floor: float = 0.35

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

    The four-way key match is not defensive over-engineering.  A sibling lane
    in this stack resolved a held leg's mark by nearest-match and booked a
    different contract's premium on 51% of its closes, fabricating more than
    half its lifetime P&L in both directions.  There is no fallback to a
    neighbouring strike here — if this exact contract did not print, the caller
    marks it off the surface and says so.
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


def _slice_for_expiry(snapshot: SurfaceSnapshot, expiry: date) -> SurfaceSlice | None:
    for sl in snapshot.slices:
        if sl.expiry == expiry and sl.ok:
            return sl
    return None


def _theoretical_mark(sl: SurfaceSlice, strike: float, option_type: str) -> tuple[float, float] | None:
    """(premium, iv) from the fitted slice when the contract did not print."""
    if not sl.ok or not sl.forward or sl.forward <= 0 or strike <= 0:
        return None
    k = math.log(strike / sl.forward)
    if sl.fit.k_min is not None and (k < sl.fit.k_min - 1e-9 or k > sl.fit.k_max + 1e-9):
        return None
    iv = float(sl.fit.params.implied_vol(k, sl.tenor_years))
    premium = black76_price(sl.forward, strike, sl.tenor_years, iv, option_type)
    return premium, iv


def _greeks_for(sl: SurfaceSlice, strike: float, option_type: str, iv: float):
    return black76_greeks(sl.forward, strike, sl.tenor_years, iv, option_type, spot=sl.spot)


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
    """Front-tenor coordinates plus the stored variance risk premium."""
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
    }
    metrics = await latest_tenor_metrics(
        snapshot.underlying,
        lower=snapshot.ts - timedelta(hours=6),
        upper=snapshot.ts + timedelta(minutes=1),
        tenor_kind="front",
    )
    if metrics:
        ctx["vrp_spread"] = metrics.get("vrp_spread")
        ctx["vrp_ratio"] = metrics.get("vrp_ratio")
        ctx["realized_vol"] = metrics.get("realized_vol")
        ctx["rv_percentile"] = metrics.get("rv_percentile")
    return ctx


_SPOT_AT_SQL = text(
    """
    WITH dominant AS (
        SELECT instrument_key
        FROM underlying_spot_candles
        WHERE underlying = :underlying
          AND interval = :interval
          AND time >= :lower
          AND time <= :upper
        GROUP BY instrument_key
        ORDER BY count(*) DESC
        LIMIT 1
    )
    SELECT c.close
    FROM underlying_spot_candles c
    JOIN dominant d ON d.instrument_key = c.instrument_key
    WHERE c.underlying = :underlying
      AND c.interval = :interval
      AND c.time >= :lower
      AND c.time <= :upper
      AND (c.time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:15' AND TIME '15:30'
    ORDER BY c.time DESC
    LIMIT 1
    """
)


async def index_spot_at(
    underlying: str, ts: datetime, *, interval: str = "30minute", window_hours: int = 30
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


async def settle_if_expired(
    position: PaperPosition,
    now_ts: datetime,
    config: IndexPaperConfig,
    *,
    session_date: date,
    view: DirectionalView,
    vol_ctx: dict[str, Any],
    run_id: str | None,
) -> bool:
    """Cash-settle a position whose contract has expired.

    Without this a held leg that stops printing — which happens routinely,
    because the chain sweep tracks strikes near the money and abandons one that
    has drifted — stays open forever: no quote means no mark, no mark means the
    exit rules never run.  An expired option does not need a quote.  It settles
    at intrinsic against the closing index level, and that is knowable.
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

    closed = await book.close_position(
        position.position_id,
        exit_ts=expiry_ts,
        exit_premium=intrinsic,
        exit_iv=None,
        exit_cost=charges["total"],
        exit_reason="expiry_settlement",
        realized_pnl=realized,
    )
    if not closed:
        return False

    await journal.record_decision(
        run_id=run_id,
        decided_at=expiry_ts,
        session_date=session_date,
        bar_ts=now_ts,
        underlying=position.underlying,
        action="exit",
        reason_code="expiry_settlement",
        reason=(
            f"settled at intrinsic {intrinsic:.2f} against a {spot:.2f} close; "
            "the contract had stopped printing"
        ),
        position_id=position.position_id,
        view=view.as_dict(),
        vol_context=vol_ctx,
    )
    closed_position = await book.get_position(position.position_id)
    if closed_position:
        marks = await load_marks(position.position_id)
        await journal.record_attribution(
            attribute_pathwise(closed_position, marks, exit_spot=spot, exit_iv=None).as_dict()
        )
    return True


async def manage_open_positions(
    underlying: str,
    snapshot: SurfaceSnapshot,
    view: DirectionalView,
    config: IndexPaperConfig,
    vol_ctx: dict[str, Any],
    result: PassResult,
    run_id: str | None = None,
) -> None:
    positions = await book.list_open(underlying)
    session_date = snapshot.ts.astimezone(IST).date()

    for position in positions:
        sl = _slice_for_expiry(snapshot, position.expiry)
        quote = await contract_quote(
            underlying, snapshot.ts, position.expiry, position.strike, position.option_type
        )

        mark_source = "chain_bar"
        premium: float | None = None
        iv: float | None = None

        if quote and quote.get("close") is not None:
            premium = float(quote["close"])
            iv = float(quote["iv"]) if quote.get("iv") is not None else None
        elif sl is not None:
            theoretical = _theoretical_mark(sl, position.strike, position.option_type)
            if theoretical:
                premium, iv = theoretical
                mark_source = "surface_theoretical"

        if premium is None:
            if await settle_if_expired(
                position, snapshot.ts, config,
                session_date=session_date, view=view, vol_ctx=vol_ctx, run_id=run_id,
            ):
                result.exits += 1
                continue
            # Not expired, no print, and outside the fitted surface.  Refuse to
            # invent a mark; the position keeps its previous one and the gap is
            # journalled so the coverage hole is visible.
            await journal.record_decision(
                run_id=run_id,
                decided_at=snapshot.ts,
                session_date=session_date,
                bar_ts=snapshot.ts,
                underlying=underlying,
                action="hold",
                reason_code="mark_unavailable",
                reason=(
                    f"{position.strike:g}{position.option_type} exp {position.expiry} did not print "
                    "and is outside the fitted surface; carrying the stale mark rather than inventing one"
                ),
                position_id=position.position_id,
                view=view.as_dict(),
                vol_context=vol_ctx,
            )
            continue

        greeks = _greeks_for(sl, position.strike, position.option_type, iv) if (sl and iv) else None
        mv_delta = _mv_delta(sl, position.strike, greeks) if (sl and greeks) else None

        unrealized = (premium - position.entry_premium) * position.quantity - position.entry_cost
        await book.mark_position(
            position.position_id,
            latest_ts=snapshot.ts,
            latest_premium=premium,
            latest_iv=iv,
            unrealized_pnl=unrealized,
        )
        await journal.record_mark(
            ts=snapshot.ts,
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

        exit_reason = _exit_decision(position, premium, snapshot, view, config, sl)
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
            log_moneyness=(
                math.log(position.strike / sl.forward) if sl and sl.forward else None
            ),
            adv_contracts=None,
            sigma=iv,
            vega_per_vol_point=(greeks.vega * 0.01) if greeks else None,
        )
        realized = (fill.fill_price - position.entry_premium) * position.quantity \
            - position.entry_cost - fill.charges["total"]

        closed = await book.close_position(
            position.position_id,
            exit_ts=snapshot.ts,
            exit_premium=fill.fill_price,
            exit_iv=iv,
            exit_cost=fill.charges["total"] + fill.slippage_per_unit * position.quantity,
            exit_reason=exit_reason,
            realized_pnl=realized,
        )
        if not closed:
            continue

        await journal.record_fill(
            position_id=position.position_id,
            filled_at=snapshot.ts,
            estimate=fill,
            detail={"mark_source": mark_source, "exit_reason": exit_reason},
        )
        await journal.record_decision(
            run_id=run_id,
            decided_at=snapshot.ts,
            session_date=session_date,
            bar_ts=snapshot.ts,
            underlying=underlying,
            action="exit",
            reason_code=exit_reason,
            reason=f"exited at {fill.fill_price:.2f} (reference {premium:.2f})",
            position_id=position.position_id,
            view=view.as_dict(),
            vol_context=vol_ctx,
            candidate={"mark_source": mark_source},
        )

        closed_position = await book.get_position(position.position_id)
        if closed_position:
            marks = await load_marks(position.position_id)
            attribution = attribute_pathwise(
                closed_position,
                marks,
                exit_spot=sl.spot if sl else None,
                exit_iv=iv,
            )
            await journal.record_attribution(attribution.as_dict())
        result.exits += 1


def _exit_decision(
    position: PaperPosition,
    premium: float,
    snapshot: SurfaceSnapshot,
    view: DirectionalView,
    config: IndexPaperConfig,
    sl: SurfaceSlice | None,
) -> str | None:
    dte = (sl.days_to_expiry if sl else None)
    if dte is not None and dte <= config.force_close_dte:
        return "expiry_force_close"
    if position.stop_premium is not None and premium <= position.stop_premium:
        return "stop"
    if position.target_premium is not None and premium >= position.target_premium:
        return "target"
    if position.max_hold_bars:
        held = (snapshot.ts - position.entry_ts).total_seconds() / (config.bar_minutes * 60.0)
        if held >= position.max_hold_bars:
            return "max_hold"
    if view.side and view.side != position.option_type and view.confidence >= config.min_confidence:
        return "view_flip"
    return None


def _select_candidate(
    sl: SurfaceSlice,
    side: str,
    config: IndexPaperConfig,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Pick the strike by DELTA, not by strike offset.

    Choosing "two strikes out" means a different exposure every day, because a
    fixed strike distance is a different delta at every vol level.  Selecting
    on delta keeps the position's character constant across regimes, which is
    the only way a series of these trades is comparable to each other.
    """
    notes: list[str] = []
    best: dict[str, Any] | None = None
    best_gap = math.inf

    # Per-condition survivor counts, so the rejection journal can name the gate
    # that actually binds instead of reporting a conjunction.  A conjunction
    # hides the pincer case where two individually reasonable limits leave an
    # empty feasible set — the exact shape of a sibling lane firing 34 setups
    # and executing zero of them.
    sided = [q for q in sl.quotes if q.option_type == side]
    survivors = {"total": len(sided), "delta_band": 0, "oi_floor": 0, "both": 0}
    max_abs_delta_seen = 0.0

    for quote in sided:
        greeks = black76_greeks(
            sl.forward, quote.strike, sl.tenor_years, quote.implied_vol, side, spot=sl.spot
        )
        abs_delta = abs(greeks.delta)
        max_abs_delta_seen = max(max_abs_delta_seen, abs_delta)

        delta_ok = config.min_abs_delta <= abs_delta <= config.max_abs_delta
        oi_ok = (quote.oi or 0) >= config.min_contract_oi
        survivors["delta_band"] += int(delta_ok)
        survivors["oi_floor"] += int(oi_ok)
        survivors["both"] += int(delta_ok and oi_ok)
        if not (delta_ok and oi_ok):
            continue

        gap = abs(abs_delta - config.target_abs_delta)
        if gap < best_gap:
            best_gap = gap
            best = {
                "strike": quote.strike,
                "option_type": side,
                "premium": quote.price,
                "implied_vol": quote.implied_vol,
                "log_moneyness": quote.log_moneyness,
                "volume": quote.volume,
                "oi": quote.oi,
                "delta": greeks.delta,
                "gamma": greeks.gamma,
                "vega": greeks.vega,
                "theta": greeks.theta,
                "abs_delta": abs_delta,
            }

    if best is None:
        binding = "delta_band" if survivors["delta_band"] == 0 else (
            "oi_floor" if survivors["oi_floor"] == 0 else "delta_band_and_oi_floor"
        )
        notes.append(
            f"{survivors['total']} {side} quotes; {survivors['delta_band']} inside delta "
            f"[{config.min_abs_delta:.2f}, {config.max_abs_delta:.2f}], "
            f"{survivors['oi_floor']} above OI {config.min_contract_oi:,.0f}, "
            f"{survivors['both']} clear both. Binding constraint: {binding}. "
            f"Deepest delta observed on this slice: {max_abs_delta_seen:.2f} "
            "(only OTM quotes are inverted, so delta cannot exceed ~0.5)."
        )
    return best, notes


async def evaluate_entry(
    underlying: str,
    snapshot: SurfaceSnapshot,
    view: DirectionalView,
    config: IndexPaperConfig,
    vol_ctx: dict[str, Any],
    result: PassResult,
    run_id: str | None = None,
) -> None:
    session_date = snapshot.ts.astimezone(IST).date()
    gates: list[GateResult] = []

    async def refuse(reason_code: str, reason: str, **extra: Any) -> None:
        await journal.record_decision(
            run_id=run_id,
            decided_at=snapshot.ts,
            session_date=session_date,
            bar_ts=snapshot.ts,
            underlying=underlying,
            action="skip",
            reason_code=reason_code,
            reason=reason,
            view=view.as_dict(),
            gates=gates,
            vol_context=vol_ctx,
            **extra,
        )
        result.decisions.append({"action": "skip", "reason_code": reason_code, "reason": reason})

    side = view.side
    gates.append(
        GateResult("view_direction", side is not None, detail=view.thesis or view.direction)
    )
    gates.append(
        GateResult("view_confidence", view.confidence >= config.min_confidence,
                   observed=view.confidence, threshold=config.min_confidence)
    )
    if side is None:
        await refuse("no_direction", view.thesis or "view is flat")
        return
    if view.confidence < config.min_confidence:
        await refuse(
            "low_confidence",
            f"confidence {view.confidence:.2f} below {config.min_confidence:.2f}",
        )
        return

    open_positions = await book.list_open()
    same_underlying = [p for p in open_positions if p.underlying == underlying]
    gates.append(
        GateResult("exposure_total", len(open_positions) < config.max_concurrent_positions,
                   observed=len(open_positions), threshold=config.max_concurrent_positions)
    )
    gates.append(
        GateResult("exposure_underlying", len(same_underlying) < config.max_positions_per_underlying,
                   observed=len(same_underlying), threshold=config.max_positions_per_underlying)
    )
    if len(open_positions) >= config.max_concurrent_positions:
        await refuse("max_exposure", f"{len(open_positions)} positions already open")
        return
    if len(same_underlying) >= config.max_positions_per_underlying:
        await refuse("already_positioned", f"already holding {underlying}")
        return

    usable = [s for s in snapshot.usable_slices() if config.min_dte <= s.days_to_expiry <= config.max_dte]
    gates.append(
        GateResult("expiry_window", bool(usable), observed=len(usable),
                   detail=f"DTE window [{config.min_dte}, {config.max_dte}]")
    )
    if not usable:
        observed = ", ".join(f"{s.days_to_expiry:.1f}d" for s in snapshot.usable_slices()) or "none"
        await refuse(
            "no_expiry_in_window",
            f"fitted maturities ({observed}) all outside [{config.min_dte}, {config.max_dte}] DTE",
        )
        return

    sl = min(usable, key=lambda s: s.days_to_expiry)
    candidate, notes = _select_candidate(sl, side, config)
    gates.append(
        GateResult("candidate_found", candidate is not None, detail="; ".join(notes))
    )
    if candidate is None:
        await refuse("no_candidate", "; ".join(notes) or "no contract cleared the delta/liquidity band")
        return

    lot_size = await resolve_lot_size(underlying, sl.expiry)
    gates.append(GateResult("lot_size", lot_size is not None, observed=lot_size))
    if lot_size is None:
        await refuse("no_lot_size", f"no lot size in fo_contract_catalog for {underlying} {sl.expiry}")
        return

    round_trip = round_trip_cost_premium_fraction(
        candidate["premium"], lot_size,
        model=config.cost_model,
        volume=candidate["volume"],
        oi=candidate["oi"],
        log_moneyness=candidate["log_moneyness"],
        adv_contracts=None,
        sigma=candidate["implied_vol"],
    )
    gates.append(
        GateResult("cost_viability", (round_trip or 1.0) <= config.max_round_trip_cost_fraction,
                   observed=round_trip, threshold=config.max_round_trip_cost_fraction)
    )
    if round_trip is None or round_trip > config.max_round_trip_cost_fraction:
        await refuse(
            "cost_too_high",
            f"a round trip costs {(round_trip or 0) * 100:.1f}% of premium, above "
            f"{config.max_round_trip_cost_fraction * 100:.1f}%",
            candidate=candidate,
        )
        return

    # The move the view expects, translated into premium via delta, must clear
    # the round-trip cost by a margin.  This is the test that a directional
    # options trade is actually worth doing rather than merely correct.
    expected_premium_move = abs(candidate["delta"]) * (sl.spot or sl.forward or 0.0) * view.expected_move_pct
    cost_per_unit = (round_trip or 0.0) * candidate["premium"]
    edge_ratio = (expected_premium_move / cost_per_unit) if cost_per_unit > 0 else 0.0
    gates.append(
        GateResult("edge_over_cost", edge_ratio >= config.min_edge_over_cost,
                   observed=edge_ratio, threshold=config.min_edge_over_cost)
    )
    if edge_ratio < config.min_edge_over_cost:
        await refuse(
            "edge_below_cost",
            f"expected premium move {expected_premium_move:.2f} is {edge_ratio:.2f}x the "
            f"{cost_per_unit:.2f} round-trip cost, below {config.min_edge_over_cost:.1f}x",
            candidate=candidate,
        )
        return

    risk_multiplier, vrp_note = vrp_risk_multiplier(
        vol_ctx.get("vrp_spread"),
        rich_threshold=config.vrp_rich_threshold,
        floor=config.vrp_risk_floor,
    )
    sizing = size_position(
        capital=config.initial_capital,
        premium=candidate["premium"],
        lot_size=lot_size,
        spot=sl.spot or sl.forward or 0.0,
        atm_iv=vol_ctx.get("atm_iv") or candidate["implied_vol"],
        delta=candidate["delta"],
        vega=candidate["vega"],
        theta_per_day=candidate["theta"],
        horizon_bars=max(view.horizon_bars, 1),
        bar_minutes=config.bar_minutes,
        risk_fraction=config.risk_fraction,
        risk_multiplier=risk_multiplier,
        stop_sigmas=config.stop_sigmas,
        reward_multiple=config.reward_multiple,
        max_premium_fraction=config.max_premium_fraction,
        max_lots=config.max_lots,
    )
    sizing_payload = {**sizing.as_dict(), "vrp_note": vrp_note, "risk_multiplier": risk_multiplier}
    gates.append(
        GateResult("sizing", sizing.approved, observed=sizing.lots, detail=sizing.reason)
    )
    if not sizing.approved:
        await refuse(sizing.reason_code, sizing.reason, candidate=candidate, sizing=sizing_payload)
        return

    fill = estimate_fill(
        candidate["premium"],
        sizing.quantity,
        "buy",
        "entry",
        model=config.cost_model,
        volume=candidate["volume"],
        oi=candidate["oi"],
        log_moneyness=candidate["log_moneyness"],
        adv_contracts=None,
        sigma=candidate["implied_vol"],
        vega_per_vol_point=candidate["vega"] * 0.01,
    )

    greeks = black76_greeks(
        sl.forward, candidate["strike"], sl.tenor_years, candidate["implied_vol"], side, spot=sl.spot
    )
    position = PaperPosition(
        position_id=book.new_position_id(underlying),
        status="open",
        session_date=session_date,
        underlying=underlying,
        expiry=sl.expiry,
        strike=candidate["strike"],
        option_type=side,
        lots=sizing.lots,
        lot_size=lot_size,
        quantity=sizing.quantity,
        entry_ts=snapshot.ts,
        entry_premium=fill.fill_price,
        entry_cost=fill.total_cost,
        entry_iv=candidate["implied_vol"],
        entry_forward=sl.forward,
        entry_spot=sl.spot,
        entry_delta=greeks.delta,
        entry_gamma=greeks.gamma,
        entry_vega=greeks.vega,
        entry_theta=greeks.theta,
        entry_mv_delta=_mv_delta(sl, candidate["strike"], greeks),
        # Stop and target are anchored on the FILL, not on the reference price,
        # so the cost of getting in is inside the risk that was sized for.
        stop_premium=max(fill.fill_price - (sizing.adverse_move_per_unit or 0.0), 0.0),
        target_premium=fill.fill_price + (sizing.adverse_move_per_unit or 0.0) * config.reward_multiple,
        max_hold_bars=min(config.max_hold_bars, max(view.horizon_bars * 2, 2)),
        latest_ts=snapshot.ts,
        latest_premium=fill.fill_price,
        latest_iv=candidate["implied_vol"],
        unrealized_pnl=-fill.total_cost,
        payload={
            "view": view.as_dict(),
            "vol_context": vol_ctx,
            "sizing": sizing_payload,
            "fill": fill.as_dict(),
            "candidate": candidate,
            "round_trip_cost_fraction": round_trip,
            "edge_ratio": edge_ratio,
        },
    )

    await book.open_position(position)
    await journal.record_fill(
        position_id=position.position_id,
        filled_at=snapshot.ts,
        estimate=fill,
        detail={"candidate": candidate, "edge_ratio": edge_ratio},
    )
    await journal.record_decision(
        run_id=run_id,
        decided_at=snapshot.ts,
        session_date=session_date,
        bar_ts=snapshot.ts,
        underlying=underlying,
        action="enter",
        reason_code="entered",
        reason=(
            f"long {candidate['strike']:g}{side} exp {sl.expiry} "
            f"x{sizing.lots} lots at {fill.fill_price:.2f}"
        ),
        position_id=position.position_id,
        view=view.as_dict(),
        gates=gates,
        candidate=candidate,
        sizing=sizing_payload,
        vol_context=vol_ctx,
    )
    result.entries += 1
    result.decisions.append(
        {"action": "enter", "reason_code": "entered", "position_id": position.position_id}
    )


_LOT_SIZE_SQL = text(
    """
    SELECT lot_size FROM fo_contract_catalog
    WHERE underlying = :underlying AND expiry = :expiry AND lot_size IS NOT NULL
    ORDER BY lot_size DESC
    LIMIT 1
    """
)

_LOT_SIZE_FALLBACK = text(
    """
    SELECT lot_size FROM fo_contract_catalog
    WHERE underlying = :underlying AND lot_size IS NOT NULL AND expiry <= :expiry
    ORDER BY expiry DESC
    LIMIT 1
    """
)


async def resolve_lot_size(underlying: str, expiry: date) -> int | None:
    """Effective-dated lot size, never a constant.

    Index lot sizes have been re-based twice inside this database's history
    (BANKNIFTY 30 -> 35 -> 30, NIFTY to 65 from 31-Dec-2025) after SEBI moved
    the target contract value to Rs 15-20 lakh.  Hard-coding one breaks every
    notional the lane computes, including the size of a single indivisible lot,
    which is now large enough to be the binding constraint on entry.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(_LOT_SIZE_SQL, {"underlying": underlying, "expiry": expiry})
        ).first()
        if row and row.lot_size:
            return int(row.lot_size)
        row = (
            await session.execute(_LOT_SIZE_FALLBACK, {"underlying": underlying, "expiry": expiry})
        ).first()
    return int(row.lot_size) if row and row.lot_size else None


async def run_underlying(
    underlying: str,
    *,
    config: IndexPaperConfig | None = None,
    as_of: datetime | None = None,
    bar_ts: datetime | None = None,
    view_provider: ViewProvider | None = None,
    run_id: str | None = None,
) -> PassResult:
    cfg = config or IndexPaperConfig()
    symbol = underlying.strip().upper()
    if symbol not in INDEX_UNIVERSE:
        return PassResult(symbol, None, "out_of_scope", f"{symbol} is not in {INDEX_UNIVERSE}")

    ts = bar_ts or await latest_bar_ts(symbol, as_of=as_of)
    if ts is None:
        return PassResult(symbol, None, "no_data", "no chain bar with enough strikes in the window")

    rows = await load_chain_bar(symbol, ts)
    snapshot = build_snapshot_from_rows(symbol, ts, rows, max_age_minutes=cfg.max_bar_age_minutes)
    result = PassResult(symbol, ts, snapshot.status, snapshot.reason)

    provider = view_provider or (lambda u, t: latest_view(u, as_of=t))
    try:
        view = await provider(symbol, ts)
    except Exception as exc:  # pragma: no cover - a broken view must not stop marks
        logger.warning(f"[index_paper] view provider failed for {symbol}: {exc}")
        view = flat_view(symbol, f"view provider raised: {exc}")

    vol_ctx = await _vol_context(snapshot) if snapshot.ok else {"bar_ts": ts.isoformat()}
    session_date = ts.astimezone(IST).date()

    if not snapshot.ok:
        await journal.record_decision(
            run_id=run_id,
            decided_at=ts,
            session_date=session_date,
            bar_ts=ts,
            underlying=symbol,
            action="skip",
            reason_code="surface_unusable",
            reason=snapshot.reason or snapshot.status,
            view=view.as_dict(),
            vol_context=vol_ctx,
        )
        return result

    await manage_open_positions(symbol, snapshot, view, cfg, vol_ctx, result, run_id)
    await evaluate_entry(symbol, snapshot, view, cfg, vol_ctx, result, run_id)
    result.status = "ok"
    return result


async def run(
    *,
    underlyings: Sequence[str] = INDEX_UNIVERSE,
    config: IndexPaperConfig | None = None,
    as_of: datetime | None = None,
    view_provider: ViewProvider | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    cfg = config or IndexPaperConfig()
    await ensure_tables()
    run_id = run_id or f"live-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"

    results: list[PassResult] = []
    for symbol in underlyings:
        try:
            results.append(
                await run_underlying(
                    symbol, config=cfg, as_of=as_of, view_provider=view_provider,
                    run_id=run_id,
                )
            )
        except Exception as exc:
            logger.exception(f"[index_paper] {symbol} pass failed: {exc}")
            results.append(PassResult(symbol, None, "error", str(exc)))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "results": [r.as_dict() for r in results],
        "book": await book.summary(cfg.initial_capital),
    }
