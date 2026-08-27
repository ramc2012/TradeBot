"""Outcome labelling — what actually happened to every logged candidate.

This is the step that converts one market decision into many supervised
examples: an outcome is computed for EVERY candidate that was evaluated, not
just the one some lane traded. Without it `candidate_snapshots` is a pile of
features with nothing to predict.

THE HONESTY RULES THIS MODULE IS BUILT AROUND
─────────────────────────────────────────────
1. NEVER SILENTLY SUBSTITUTE A PRICE. Every mark carries the source it came
   from, the REALIZED lag between the horizon we asked for and the sample we
   got, and the number of samples the path statistic was built from. A row that
   cannot be honestly marked is written with an `unlabellable_*` status, not
   dropped — a dropped row makes coverage look complete when it was selective.

2. LTP IS A PRINT, NOT A MARK. A zero forward return usually means no trade
   arrived, not that the price held: 49.3% of ~6-minute option LTP intervals in
   this data show exactly zero change. Every option label therefore carries
   trade-arrival evidence (volume delta), so "flat" and "untraded" stay
   distinguishable.

3. SAME CONTRACT, ALWAYS. The 2026-08-21 incident where held legs were marked
   at ANOTHER contract's premium fabricated 54% of a lane's lifetime P&L in both
   directions. Every forward lookup here matches on the full logical contract
   key — (underlying, expiry, strike, option_type) — and the option-candle path
   additionally de-duplicates on that logical key, because the physical unique
   constraint there is (instrument_key, interval, time), which is BROKER
   specific and lets the same bar exist twice at two different prices.

4. TIME IS BOUND DIRECTLY. Every query bounds `time` with literal UTC instants
   and never wraps the partitioning column in a function or cast. Doing so once
   SIGKILLed the live Postgres mid-session; the 60s statement timeout does not
   protect against it.

5. COSTS GO IN THE LABEL. Net-of-cost is computed here, once, and must not be
   subtracted again at evaluation time.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from candidate_capture.costs import breakeven_move_pct, round_trip_cost
from db.database import AsyncSessionLocal

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

TABLE = "candidate_outcomes"
LABEL_VERSION = "candidate_label_v1"

# ── label statuses ────────────────────────────────────────────────────────
OK = "ok"
NO_TRADE_ROW = "no_trade"
UNLABELLABLE_SOURCE_DARK = "unlabellable_source_dark"
UNLABELLABLE_NO_FORWARD = "unlabellable_no_forward"
UNLABELLABLE_OUT_OF_TOLERANCE = "unlabellable_out_of_tolerance"
UNLABELLABLE_NO_SPOT = "unlabellable_no_spot"

# ── horizons ──────────────────────────────────────────────────────────────
# The plan asks for 5/15/30/60 minutes. They are all computed, but 300s is
# retained with open eyes: measured on this data the first option mark at or
# after t+300s arrives at a MEDIAN of ~407s, so a "5m" option label is really a
# ~6.8-minute one with a 14-minute tail. That is exactly why `forward_lag_
# seconds` is stored per row and why the tolerance band below is asymmetric —
# the label is only kept when the realized lag is close enough to mean it.
DEFAULT_HORIZONS_SECONDS = (300, 900, 1800, 3600)

# A forward mark is accepted inside [H - before, H + after]. Asymmetric because
# the sampling cadence means marks essentially always arrive LATE, never early,
# and the tolerance scales with the horizon: 90s of slop is a third of a
# 5-minute label but only 2.5% of an hour one.
TOLERANCE_BEFORE_FRACTION = 0.20
TOLERANCE_AFTER_FRACTION = 0.50
TOLERANCE_MIN_SECONDS = 60.0

# The spot barrier width, in units of the underlying's own realized volatility
# over the horizon. Volatility-scaled rather than a fixed percentage so one
# definition works across NIFTY and BANKNIFTY and across calm and violent
# sessions — the standing rule that risk levels come from measured volatility,
# never a hardcoded fraction.
BARRIER_SIGMA_MULTIPLE = 1.0
# Realized vol is estimated from the session's own ticks BEFORE the anchor, so
# the barrier width can never see the future it is used to label.
VOL_LOOKBACK_SECONDS = 1800
VOL_MIN_TICKS = 60
# A COUNT guard alone is not a sample guard: at ~4 ticks/second, 60 ticks is
# 15 seconds of tape. The estimate must also span real wall-clock time, or a
# feed stall produces a confident, far-too-narrow barrier.
VOL_MIN_SPAN_SECONDS = 300.0

# Index underlying → the symbol its ticks and chain snapshots are stored under.
# Copied in spirit from market_data.greeks_enrichment.INDEX_SYMBOL_MAP, which is
# the only proven-correct mapping to option_chain_snapshots.
INDEX_TICK_SYMBOL: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}

SOURCE_CANDIDATE_SNAPSHOTS = "candidate_snapshots"
SOURCE_OPTION_CHAIN_SNAPSHOTS = "option_chain_snapshots"


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def tolerance_window(horizon_seconds: int) -> tuple[float, float]:
    """(earliest, latest) acceptable realized lag for a horizon, in seconds."""
    before = max(horizon_seconds * TOLERANCE_BEFORE_FRACTION, TOLERANCE_MIN_SECONDS)
    after = max(horizon_seconds * TOLERANCE_AFTER_FRACTION, TOLERANCE_MIN_SECONDS)
    return (horizon_seconds - before, horizon_seconds + after)


# ══════════════════════════════════════════════════════════════════════════
# (1) Pure computation
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SpotPath:
    """Stage A: the underlying's realized path over one horizon."""

    entry: Optional[float]
    forward: Optional[float]
    return_pct: Optional[float]
    mfe_pct: Optional[float]
    mae_pct: Optional[float]
    barrier_hit: Optional[str]
    time_to_barrier_seconds: Optional[float]
    barrier_width_pct: Optional[float]
    tick_count: int
    # REALIZED lag of the last tick used, and whether the window actually
    # reached its horizon. The option leg has carried its realized lag from the
    # start; the spot leg needs the same honesty, because a tick feed that dies
    # mid-window otherwise stores a 10-minute move under a 60-minute label.
    forward_lag_seconds: Optional[float]
    window_complete: Optional[bool]


def realized_vol_per_sqrt_second(
    ticks: Sequence[tuple[datetime, float]],
) -> Optional[float]:
    """Volatility of the underlying in fraction per sqrt(second).

    TIME-NORMALISED ON PURPOSE. An earlier version returned a per-TICK sigma
    scaled by sqrt(tick count), which silently made the answer depend on how
    many ticks happened to arrive rather than on how long the window was. Two
    consequences, both measured on real data: the barrier could not vary with
    the horizon at all, and an anchor early in the session (whose lookback is
    truncated by the open) got a materially narrower barrier than an identical
    anchor at midday. Volatility is a per-unit-TIME quantity; anything else
    makes the label a function of feed cadence.

    Returns None unless the sample spans a real stretch of wall-clock AND
    carries enough ticks. A count guard alone is not enough: at ~4 ticks/second
    sixty ticks is fifteen seconds of data, and during the documented
    mid-session tick blackout that guard passes while the estimate is nonsense.
    """
    usable = [(t, p) for t, p in ticks if _finite(p) and (_finite(p) or 0) > 0]
    if len(usable) < VOL_MIN_TICKS:
        return None
    span = (usable[-1][0] - usable[0][0]).total_seconds()
    if span < VOL_MIN_SPAN_SECONDS:
        return None

    rets: list[float] = []
    for (_, prev), (_, cur) in zip(usable, usable[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    # Total variance over the window, re-expressed per second.
    total_var = var * len(rets)
    return math.sqrt(total_var / span)


def barrier_width_for_horizon(
    sigma_per_sqrt_second: Optional[float], horizon_seconds: int
) -> Optional[float]:
    """Scale volatility to the horizon actually being labelled.

    sigma * sqrt(H) is the standard diffusive scaling. Computing this per
    horizon — rather than once per anchor — is what makes `spot_barrier_hit`
    mean the same thing across the 5-minute and 60-minute rows. With a single
    shared width, measured on NIFTY 2026-08-25, the barrier was touched in 1.6%
    of 5-minute windows and 47.6% of 60-minute ones: the same column carried a
    near-constant "none" at one horizon and a coin flip at another.
    """
    if not sigma_per_sqrt_second or sigma_per_sqrt_second <= 0:
        return None
    return round(
        sigma_per_sqrt_second * math.sqrt(float(horizon_seconds)) * BARRIER_SIGMA_MULTIPLE,
        8,
    )


def build_spot_path(
    *,
    anchor_price: Optional[float],
    forward_ticks: Sequence[tuple[datetime, float]],
    anchor_time: datetime,
    horizon_seconds: int,
    barrier_width_pct: Optional[float],
) -> SpotPath:
    """Stage A label from an exact tick path. No interpolation, no substitution.

    `forward_ticks` must be ascending and already bounded to
    (anchor_time, anchor_time + horizon]. The final tick inside the window IS
    the forward price — with ~0.25s spacing there is no meaningful gap to bridge.
    """
    entry = _finite(anchor_price)
    usable = [(t, p) for t, p in forward_ticks if _finite(p) and (_finite(p) or 0) > 0]
    if entry is None or entry <= 0 or not usable:
        return SpotPath(
            entry, None, None, None, None, None, None, barrier_width_pct,
            len(usable), None, None,
        )

    prices = [p for _, p in usable]
    forward = prices[-1]
    lag = round((usable[-1][0] - anchor_time).total_seconds(), 3)
    # "Complete" means the tape actually carried the window to (near) its end.
    # The tolerance band is the same one the option leg uses, so a truncated
    # spot window is flagged by the same rule rather than a second convention.
    complete = lag >= tolerance_window(horizon_seconds)[0]
    ret = (forward - entry) / entry

    high = max(prices)
    low = min(prices)
    mfe = (high - entry) / entry
    mae = (low - entry) / entry

    barrier_hit: Optional[str] = None
    time_to_barrier: Optional[float] = None
    if barrier_width_pct and barrier_width_pct > 0:
        up = entry * (1.0 + barrier_width_pct)
        down = entry * (1.0 - barrier_width_pct)
        for stamp, price in usable:
            if price >= up or price <= down:
                # First touch. When a single tick clears both (impossible with a
                # symmetric barrier, but guarded anyway) the adverse side wins —
                # the conservative reading, matching the house convention that a
                # stop-before-target tie resolves against the position.
                barrier_hit = "down" if price <= down else "up"
                time_to_barrier = round((stamp - anchor_time).total_seconds(), 3)
                break
        if barrier_hit is None:
            barrier_hit = "none"

    return SpotPath(
        entry=entry,
        forward=forward,
        return_pct=round(ret, 8),
        mfe_pct=round(mfe, 8),
        mae_pct=round(mae, 8),
        barrier_hit=barrier_hit,
        time_to_barrier_seconds=time_to_barrier,
        barrier_width_pct=barrier_width_pct,
        tick_count=len(usable),
        forward_lag_seconds=lag,
        window_complete=complete,
    )


@dataclass(frozen=True)
class ForwardMark:
    """Stage B: one forward observation of the SAME option contract."""

    price: Optional[float]
    lag_seconds: Optional[float]
    source: Optional[str]
    sample_count: int
    volume: Optional[int]
    oi: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    mfe_pct: Optional[float]
    mae_pct: Optional[float]
    status: str
    reason: Optional[str]


def select_forward_mark(
    *,
    samples: Sequence[Mapping[str, Any]],
    anchor_time: datetime,
    anchor_price: Optional[float],
    horizon_seconds: int,
) -> ForwardMark:
    """Pick the forward mark closest to the horizon, inside the tolerance band.

    `samples` are forward observations of ONE contract, ascending, already
    restricted to the same logical contract by the caller's query.

    The mark is the sample whose lag is nearest to the requested horizon rather
    than simply the first past it: with a p95 gap of ~8 minutes, "first sample
    after t+H" can land far later than a sample slightly before H.
    """
    ordered = [
        s for s in samples if _finite(s.get("price")) and (_finite(s.get("price")) or 0) > 0
    ]
    if not ordered:
        return ForwardMark(
            None, None, None, 0, None, None, None, None, None, None,
            UNLABELLABLE_NO_FORWARD,
            "no forward sample of this contract in the horizon window",
        )

    lo, hi = tolerance_window(horizon_seconds)
    scored: list[tuple[float, Mapping[str, Any], float]] = []
    for sample in ordered:
        lag = (sample["time"] - anchor_time).total_seconds()
        if lag <= 0:
            continue
        scored.append((abs(lag - horizon_seconds), sample, lag))
    if not scored:
        return ForwardMark(
            None, None, None, len(ordered), None, None, None, None, None, None,
            UNLABELLABLE_NO_FORWARD,
            "every candidate sample is at or before the anchor instant",
        )

    # Prefer a mark that is actually INSIDE the tolerance band. Sorting purely
    # by distance-to-horizon and only then testing the band meant a slightly
    # closer out-of-band sample could shadow a perfectly usable in-band one and
    # send the whole row to unlabellable_out_of_tolerance — discarding a real
    # observation because a worse one sorted first.
    in_band = [row for row in scored if lo <= row[2] <= hi]
    (in_band or scored).sort(key=lambda row: row[0])
    _, chosen, lag = (in_band or scored)[0]

    entry = _finite(anchor_price)
    # Path statistics use only samples up to the chosen mark.
    path = [s for s in ordered if 0 < (s["time"] - anchor_time).total_seconds() <= lag]
    mfe = mae = None
    if entry and entry > 0 and path:
        prices = [float(s["price"]) for s in path]
        mfe = round((max(prices) - entry) / entry, 8)
        mae = round((min(prices) - entry) / entry, 8)

    if not (lo <= lag <= hi):
        return ForwardMark(
            price=_finite(chosen.get("price")),
            lag_seconds=round(lag, 3),
            source=chosen.get("source"),
            sample_count=len(path),
            volume=chosen.get("volume"),
            oi=_finite(chosen.get("oi")),
            bid=_finite(chosen.get("bid")),
            ask=_finite(chosen.get("ask")),
            mfe_pct=mfe,
            mae_pct=mae,
            status=UNLABELLABLE_OUT_OF_TOLERANCE,
            reason=(
                f"nearest forward mark lagged {lag:.1f}s, outside the "
                f"[{lo:.0f}s, {hi:.0f}s] band for a {horizon_seconds}s horizon"
            ),
        )

    return ForwardMark(
        price=_finite(chosen.get("price")),
        lag_seconds=round(lag, 3),
        source=chosen.get("source"),
        sample_count=len(path),
        volume=chosen.get("volume"),
        oi=_finite(chosen.get("oi")),
        bid=_finite(chosen.get("bid")),
        ask=_finite(chosen.get("ask")),
        mfe_pct=mfe,
        mae_pct=mae,
        status=OK,
        reason=None,
    )


def build_outcome_row(
    *,
    anchor: Mapping[str, Any],
    horizon_seconds: int,
    spot: SpotPath,
    mark: ForwardMark,
    lot_size: Optional[int],
    source_dark: bool = False,
) -> dict[str, Any]:
    """Assemble one fully-provenanced outcome row from its measured parts."""
    option_type = str(anchor.get("option_type") or "")
    session_date = anchor["time"].astimezone(IST).date()

    base: dict[str, Any] = {
        "time": anchor["time"],
        "decision_id": str(anchor["decision_id"]),
        "session_date": session_date,
        "underlying": anchor["underlying"],
        "expiry": anchor.get("expiry"),
        "strike": _finite(anchor.get("strike")),
        "option_type": option_type,
        "horizon_seconds": int(horizon_seconds),
        "label_status": OK,
        "label_reason": None,
        "spot_entry": spot.entry,
        "spot_forward": spot.forward,
        "spot_return_pct": spot.return_pct,
        "spot_mfe_pct": spot.mfe_pct,
        "spot_mae_pct": spot.mae_pct,
        "spot_barrier_hit": spot.barrier_hit,
        "spot_time_to_barrier_seconds": spot.time_to_barrier_seconds,
        "spot_barrier_width_pct": spot.barrier_width_pct,
        "spot_tick_count": spot.tick_count,
        "spot_forward_lag_seconds": spot.forward_lag_seconds,
        "spot_window_complete": spot.window_complete,
        "option_entry_mid": None,
        "option_forward_price": mark.price,
        "forward_lag_seconds": mark.lag_seconds,
        "forward_sample_count": mark.sample_count,
        "forward_source": mark.source,
        "option_gross_return_pct": None,
        "option_net_return_pct": None,
        "option_mfe_pct": mark.mfe_pct,
        "option_mae_pct": mark.mae_pct,
        "trade_arrived": None,
        "volume_delta": None,
        "oi_delta": None,
        "entry_half_spread_pct": None,
        "entry_half_spread_measured": None,
        "exit_half_spread_pct": None,
        "exit_half_spread_measured": None,
        "cost_spread_rupees": None,
        "cost_statutory_rupees": None,
        "cost_total_rupees": None,
        "cost_pct_of_notional": None,
        "breakeven_move_pct": None,
        "economically_decidable": None,
        "quantity": lot_size,
        "lot_size": lot_size,
        "label_version": LABEL_VERSION,
    }

    # The NO_TRADE candidate has no contract, so it carries the spot label only.
    # That is the whole point of storing it: abstention's outcome is exactly the
    # move you declined to take, which Stage A measures precisely.
    if option_type == "NO_TRADE":
        base["label_status"] = NO_TRADE_ROW
        base["label_reason"] = "abstain candidate — spot outcome only, no contract to mark"
        if spot.entry is None:
            base["label_status"] = UNLABELLABLE_NO_SPOT
            base["label_reason"] = "no spot ticks for this underlying in the horizon window"
        return base

    if source_dark:
        base["label_status"] = UNLABELLABLE_SOURCE_DARK
        base["label_reason"] = (
            "the forward option source has ZERO rows for this session — a known "
            "silent outage mode that a stack health check does not detect"
        )
        return base

    if mark.status != OK:
        base["label_status"] = mark.status
        base["label_reason"] = mark.reason
        return base

    entry_bid = _finite(anchor.get("bid"))
    entry_ask = _finite(anchor.get("ask"))
    entry_mid = (
        (entry_bid + entry_ask) / 2.0
        if entry_bid and entry_ask and entry_ask >= entry_bid
        else _finite(anchor.get("ltp"))
    )
    base["option_entry_mid"] = entry_mid

    if entry_mid is None or entry_mid <= 0 or mark.price is None:
        base["label_status"] = UNLABELLABLE_NO_FORWARD
        base["label_reason"] = "no usable entry mid or forward price"
        return base

    gross = (mark.price - entry_mid) / entry_mid
    base["option_gross_return_pct"] = round(gross, 8)

    quantity = int(lot_size or 0)
    cost = round_trip_cost(
        entry_mid=entry_mid,
        exit_mid=mark.price,
        quantity=quantity,
        lot_size=lot_size,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        # A forward mark taken from a later candidate snapshot DOES carry a real
        # quote, which is the only way the exit half-spread is ever measured.
        exit_bid=mark.bid,
        exit_ask=mark.ask,
    )
    base.update(
        {
            "entry_half_spread_pct": cost.entry_half_spread_pct,
            "entry_half_spread_measured": cost.entry_half_spread_measured,
            "exit_half_spread_pct": cost.exit_half_spread_pct,
            "exit_half_spread_measured": cost.exit_half_spread_measured,
            "cost_spread_rupees": cost.spread_cost_rupees,
            "cost_statutory_rupees": cost.statutory_rupees,
            "cost_total_rupees": cost.total_rupees,
            "cost_pct_of_notional": cost.total_pct_of_entry_notional,
            "quantity": quantity,
        }
    )

    if quantity > 0 and entry_mid > 0:
        gross_rupees = (mark.price - entry_mid) * quantity
        notional = entry_mid * quantity
        base["option_net_return_pct"] = round(
            (gross_rupees - cost.total_rupees) / notional, 8
        )

    breakeven = breakeven_move_pct(
        entry_mid=entry_mid,
        quantity=quantity,
        lot_size=lot_size,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
    )
    base["breakeven_move_pct"] = breakeven
    if breakeven is not None and mark.mfe_pct is not None:
        # max(mfe, 0), not abs(mfe). These are LONG option positions, so only a
        # favourable excursion can pay for the round trip. abs() made a contract
        # that never once printed above entry — whose "best" excursion is a
        # loss — come out as decidable, which is the opposite of the truth.
        # Decidable when the contract's own best excursion over the horizon
        # could actually have cleared its own cost. This is a statement about
        # the HORIZON, not about the trade: a horizon over which nothing can
        # clear cost produces labels that are all losses by construction, and a
        # ranker trained on them learns to abstain rather than to rank.
        base["economically_decidable"] = bool(max(mark.mfe_pct, 0.0) >= breakeven)

    anchor_volume = anchor.get("volume")
    if anchor_volume is not None and mark.volume is not None:
        delta = int(mark.volume) - int(anchor_volume)
        base["volume_delta"] = delta
        # Cumulative session volume, so a positive delta means prints arrived
        # between the anchor and the mark. Zero means the forward "price" is a
        # stale print being read as a live mark.
        base["trade_arrived"] = bool(delta > 0)
    anchor_oi = _finite(anchor.get("oi"))
    if anchor_oi is not None and mark.oi is not None:
        base["oi_delta"] = round(mark.oi - anchor_oi, 4)

    return base
