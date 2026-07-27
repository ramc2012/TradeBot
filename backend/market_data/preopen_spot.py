"""Pre-open spot snapshot + activeness flag.

Owner spec (2026-07-27), verbatim:

    "For spot add pre-open historical values also. add activeness flag to mark
     interesting activeness in pre-open trades."

WHAT THE NSE PRE-OPEN ACTUALLY IS
─────────────────────────────────
09:00-09:08 IST  order collection (no trades; indicative price only)
09:08-09:12 IST  order matching + confirmation  → EQUILIBRIUM PRICE + MATCHED QTY
09:12-09:15 IST  buffer
09:15 IST        continuous session (and only then does NSE F&O open)

Two consequences that shape every rule below:

  1. Only `NSE:<SYM>-EQ` frames inside 09:00-09:15 IST are genuine cash auction
     prints. `...FUT` / `...CE` / `...PE` frames present in that window are
     carried-over/stale last-traded frames, NOT auction prints — F&O has no
     call auction. They are excluded.
  2. MCX has NO call auction at all (MCX simply opens at 09:00 IST and trades
     continuously). An MCX "pre-open snapshot" would be a fabricated concept,
     so MCX is excluded ENTIRELY rather than written with zeros. This is
     reported, not hidden — see `MCX_EXCLUSION_REASON`.

WHERE THE FIELDS COME FROM (verified on the 2026-07-24 tape)
────────────────────────────────────────────────────────────
For `NSE:BAJFINANCE-EQ` at 03:37:20Z the WS frame carries:
    ltp/open/high/low = 1027.3   → the equilibrium print (all four equal)
    volume            = 80110    → the MATCHED pre-open quantity (cumulative)
    close             = 1039.8   → the PRIOR-SESSION close (constant all window)
    bid/ask, total_buy_qty/total_sell_qty → the auction order book
So every field this module stores is read from real data. Nothing is defaulted.

NOTHING HERE TOUCHES STRATEGY. No entry rule, exit rule, gate, threshold or
sizing formula is read or written. This module only observes and records.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

TABLE = "preopen_spot_snapshots"
SOURCE_WS_TAPE = "market_ticks_ws"

# The activeness definition is versioned so a later redefinition can never be
# silently mixed with rows produced by this one.
DEFINITION_VERSION = "preopen_activeness_v1"

MCX_EXCLUSION_REASON = (
    "MCX has no call auction — the exchange opens at 09:00 IST into continuous "
    "trading. Ticks present in the 09:00-09:15 IST band for MCX roots are "
    "ordinary continuous-session trades, not auction prints, so no pre-open "
    "snapshot concept exists for them. Excluded rather than zero-filled."
)

# ── window ────────────────────────────────────────────────────────────────
# Read the whole 09:00-09:15 band: order collection starts at 09:00 and the
# equilibrium is published during 09:08-09:12; measured prints on 07-17..07-24
# cluster at 09:07:20-09:08:20 IST.
PREOPEN_START_IST = dt_time(9, 0)
PREOPEN_END_IST = dt_time(9, 15)

# ── data statuses ─────────────────────────────────────────────────────────
STATUS_OK = "ok"
STATUS_NO_MATCH = "no_match"
STATUS_NO_TICKS = "no_preopen_ticks"
STATUS_STALE_CARRY = "stale_carry"
STATUS_BAND_REJECT = "price_band_reject"
STATUS_SESSION_DARK = "session_dark"

# ── activeness states ─────────────────────────────────────────────────────
STATE_ACTIVE = "active"
STATE_QUIET = "quiet"
STATE_UNKNOWN = "unknown"

# ══════════════════════════════════════════════════════════════════════════
# ACTIVENESS DEFINITION — three components, each SELF-RELATIVE by construction
#
# The point of the flag is "interesting participation", which must not collapse
# into "is a large cap". So every component is normalised against the name's
# OWN history or its OWN order book — never an absolute rupee/quantity level.
#
# Thresholds below are a-priori round structural points. NONE was swept: no
# forward return, no P&L, no outcome variable of any kind was consulted while
# choosing them, and there is no tuning loop anywhere in this module.
# ══════════════════════════════════════════════════════════════════════════

# C1 — relative matched pre-open volume.
#      preopen_volume / median(this name's own prior pre-open volumes)
#      Threshold 2.0 = a DOUBLING of the name's own typical matched quantity.
REL_VOLUME_THRESHOLD = 2.0
# A median over fewer than 3 observations is not a distribution. Below this the
# component is `unknown` — never "not active".
REL_VOLUME_MIN_BASELINE = 3

# C2 — gap magnitude measured in the name's OWN daily range.
#      |gap_pct| / atr_pct_14
#      Threshold 1.0 = the overnight gap alone is as large as one full average
#      true daily range. That is the structural point where the gap is no
#      longer inside normal daily noise; it is not a fitted number.
GAP_ATR_THRESHOLD = 1.0
# ATR needs a real sample. 10 sessions is the floor; the target is 14.
ATR_TARGET_SESSIONS = 14
ATR_MIN_SESSIONS = 10

# C3 — auction order-book imbalance, measured against the SAME session's
#      cross-section rather than in absolute terms.
#
#      raw = (total_buy_qty - total_sell_qty) / (total_buy_qty + total_sell_qty)
#
#      MEASURED, and the reason this component is a z-score and not a level:
#      across the 288 captured auction prints in 2026-07-13..07-24 the raw
#      imbalance is negative for 269 of them (93%), the median |raw| is 0.47,
#      and the session mean is -0.37..-0.55 on EVERY single session. The
#      pre-open `total_sell_qty` field carries the whole unmatched sell-side
#      depth, so a large negative reading is a market-wide property of the
#      feed, not a fact about the name. Thresholding the level would have
#      flagged 208/288 = 72% of names "active", which is not a signal.
#
#      De-meaning against the session's own cross-section removes that
#      market-wide component by construction and leaves the name-specific part.
#      Robust statistics (median / MAD) because a handful of extreme books
#      must not move the reference.
BOOK_IMBALANCE_Z_THRESHOLD = 2.0
# Consistency scale factor turning MAD into a robust sigma for a normal.
MAD_TO_SIGMA = 1.4826
# A cross-sectional statistic needs a cross-section. Below this the z-score is
# `unknown`, never 0.
BOOK_IMBALANCE_MIN_PEERS = 8

# Sub-scores are capped at 3x the threshold so one extreme component cannot
# saturate the mean; the cap is a display/robustness choice, not a tuned knob.
SUBSCORE_CAP_MULTIPLE = 3.0

COMPONENT_REL_VOLUME = "rel_volume"
COMPONENT_GAP_ATR = "gap_vs_atr"
COMPONENT_BOOK_IMBALANCE = "book_imbalance"

# Which components an instrument CAN have at all. An index has no traded
# volume and no auction order book, so those two are not "missing data" for an
# index — they are inapplicable, and the verdict must not be penalised for it.
APPLICABLE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "STOCK": (COMPONENT_REL_VOLUME, COMPONENT_GAP_ATR, COMPONENT_BOOK_IMBALANCE),
    "INDEX": (COMPONENT_GAP_ATR,),
}

# `underlying_spot_candles` carries the SAME bar under several sources. The
# tick-derived ones (`live_tick`, `timescaledb_spot_1minute`, the *_cont
# futures fallbacks) inherit the Fyers WS cross-symbol contamination: measured
# 2026-07-17, JIOFIN's `live_tick` 30-minute bars report a session high of
# 57,582 against a true 249.95, which alone produced an "ATR" of 1,759%.
# The ATR denominator therefore reads ONLY broker-history sources.
#
# 2026-07-27 VERIFICATION FIX. The first version of this list held only the
# three STOCK-side labels, which silently killed C2 for every index: the index
# broker-history rows are written by auction_intelligence/live.py under
# `upstox_spot_index` (Upstox /v2/historical-candle, line 778) and
# `fyers_spot_index` (Fyers get_historical_candles, line 741) — the same broker
# history, just a different label. With them excluded every index resolved to
# atr_sessions_n=4 and therefore `unknown` on EVERY session from 2026-07-15
# onward. Measured: adding them takes NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY from
# 4 to 13-14 sessions.
#
# `fyers_continuous_futures` is deliberately NOT here even though it is broker
# history: it is a FUTURES series stored under the index underlying, so its
# range is not the spot range. Nor are `live_tick`, `timescaledb_spot_1minute`,
# `source_1minute_aggregate`, `readiness_backfill_aggregate` or
# `strategy_agent` — all derived from the tick tape or from another derived
# series.
ATR_TRUSTED_SOURCES: tuple[str, ...] = (
    # stock broker history
    "fyers",
    "upstox_spot",
    "upstox",
    # index broker history (same brokers, index-specific label)
    "upstox_spot_index",
    "fyers_spot_index",
    "upstox_spot_index_manual_gapfill",
)
# A real but ANCIENT sample is still a lie if it is presented as current. The
# index broker-history series stops writing for stretches (last write 07-08 as
# of 07-24), so beyond this many calendar days the ATR is refused and the
# reason is recorded — the value is never silently used. 30 days ~= 21 trading
# sessions, i.e. the sample may not predate the ATR's own 14-session window by
# more than about a week.
ATR_MAX_SAMPLE_STALENESS_DAYS = 30
# Structural backstop for any residual bad bar in a trusted source: the widest
# NSE circuit band is 20%, so a single session whose range exceeds half its
# close is not a legal session. Dropped from the ATR sample and counted out.
ATR_MAX_SESSION_RANGE_FRACTION = 0.5

# NSE pre-open operating range. A print outside +/-20% of the prior close is
# not a legal auction equilibrium — it is a corrupt frame (the Fyers WS
# cross-symbol contamination class, see the 2026-07-20 incident). Rejected,
# not stored as a signal.
PRICE_BAND_PCT = 20.0
# Prior-close cross-check tolerance against the external 30m spot anchor.
PREV_CLOSE_ANCHOR_TOLERANCE_PCT = 5.0

# Index roots → the app symbol they are captured under in `market_ticks`.
# NIFTYNXT50 is deliberately absent: it is in the F&O catalog but is NOT in the
# tick-capture subscription set, so it will always resolve to `no_preopen_ticks`
# — a recorded fact, not a silent omission.
INDEX_TICK_SYMBOLS: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def _now_ist() -> datetime:
    return datetime.now(IST)


def preopen_window_utc(session_date: date) -> tuple[datetime, datetime]:
    """[start, end) of the NSE pre-open band for `session_date`, in UTC."""
    start = datetime.combine(session_date, PREOPEN_START_IST, tzinfo=IST).astimezone(UTC)
    end = datetime.combine(session_date, PREOPEN_END_IST, tzinfo=IST).astimezone(UTC)
    return start, end


def tick_symbol_for(underlying: str, kind: str) -> Optional[str]:
    """The `market_ticks` symbol an underlying's pre-open prints arrive under."""
    sym = str(underlying or "").strip().upper()
    if not sym:
        return None
    if str(kind).upper() == "INDEX":
        return INDEX_TICK_SYMBOLS.get(sym)
    return f"NSE:{sym}-EQ"


# ══════════════════════════════════════════════════════════════════════════
# (1) Pure computation — no I/O, fully unit-testable
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PreopenTick:
    time: datetime
    ltp: Optional[float]
    volume: Optional[int]
    close: Optional[float]
    bid: Optional[float] = None
    ask: Optional[float] = None
    total_buy_qty: Optional[int] = None
    total_sell_qty: Optional[int] = None


@dataclass
class ActivenessVerdict:
    state: str
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    unknown: dict[str, str] = field(default_factory=dict)


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def compute_activeness(
    *,
    kind: str,
    data_status: str,
    rel_volume: Optional[float],
    rel_volume_baseline_n: Optional[int],
    gap_atr_ratio: Optional[float],
    atr_sessions_n: Optional[int],
    book_imbalance_z: Optional[float],
    peer_n: Optional[int] = None,
    atr_unavailable_reason: Optional[str] = None,
) -> ActivenessVerdict:
    """Turn the measured components into a state + score + queryable reasons.

    Rules, in order:
      * anything other than a good auction print  → `unknown` (never `quiet`)
      * >=1 component TRIGGERED                   → `active`  (positive evidence
        is sufficient on its own; a triggered component is a fact regardless of
        how many other components happen to be unavailable)
      * otherwise `quiet` ONLY if at least half of the components the
        instrument CAN have were actually computable; else `unknown`.
    """
    kind_u = str(kind or "").upper()
    applicable = APPLICABLE_COMPONENTS.get(kind_u, APPLICABLE_COMPONENTS["STOCK"])

    if data_status != STATUS_OK:
        return ActivenessVerdict(
            state=STATE_UNKNOWN,
            score=None,
            unknown={c: f"data_status={data_status}" for c in applicable},
        )

    available: list[str] = []
    reasons: list[str] = []
    unknown: dict[str, str] = {}
    subscores: list[float] = []

    def _consider(name: str, raw: Optional[float], threshold: float, missing: Optional[str]) -> None:
        if name not in applicable:
            unknown[name] = "not_applicable_for_kind_" + kind_u.lower()
            return
        if missing is not None:
            unknown[name] = missing
            return
        value = _finite(raw)
        if value is None:
            unknown[name] = "value_not_finite"
            return
        available.append(name)
        subscores.append(min(value / threshold, SUBSCORE_CAP_MULTIPLE) / SUBSCORE_CAP_MULTIPLE)
        if value >= threshold:
            reasons.append(name)

    # C1 — relative matched volume
    baseline_n = int(rel_volume_baseline_n or 0)
    if COMPONENT_REL_VOLUME in applicable:
        if rel_volume is None:
            missing_c1 = (
                f"baseline_n={baseline_n}<{REL_VOLUME_MIN_BASELINE}"
                if baseline_n < REL_VOLUME_MIN_BASELINE
                else "no_matched_preopen_volume"
            )
        elif baseline_n < REL_VOLUME_MIN_BASELINE:
            missing_c1 = f"baseline_n={baseline_n}<{REL_VOLUME_MIN_BASELINE}"
        else:
            missing_c1 = None
        _consider(COMPONENT_REL_VOLUME, rel_volume, REL_VOLUME_THRESHOLD, missing_c1)
    else:
        unknown[COMPONENT_REL_VOLUME] = "not_applicable_for_kind_" + kind_u.lower()

    # C2 — gap measured in the name's own daily range
    atr_n = int(atr_sessions_n or 0)
    if gap_atr_ratio is None:
        if atr_unavailable_reason:
            # e.g. the ATR sample exists but its newest bar is too old to stand
            # in for today's daily range. Surfaced verbatim so the row says WHY
            # rather than implying the sample was simply too small.
            missing_c2 = atr_unavailable_reason
        elif atr_n < ATR_MIN_SESSIONS:
            missing_c2 = f"atr_sessions_n={atr_n}<{ATR_MIN_SESSIONS}"
        else:
            missing_c2 = "no_gap_pct"
    else:
        missing_c2 = None
    _consider(COMPONENT_GAP_ATR, abs(gap_atr_ratio) if gap_atr_ratio is not None else None,
              GAP_ATR_THRESHOLD, missing_c2)

    # C3 — auction order-book imbalance vs the session's own cross-section
    if COMPONENT_BOOK_IMBALANCE in applicable:
        peers = int(peer_n or 0)
        if book_imbalance_z is None:
            missing_c3 = (
                f"peer_n={peers}<{BOOK_IMBALANCE_MIN_PEERS}"
                if peers < BOOK_IMBALANCE_MIN_PEERS
                else "no_auction_book_quantities_or_zero_peer_dispersion"
            )
        else:
            missing_c3 = None
        _consider(
            COMPONENT_BOOK_IMBALANCE,
            abs(book_imbalance_z) if book_imbalance_z is not None else None,
            BOOK_IMBALANCE_Z_THRESHOLD,
            missing_c3,
        )
    else:
        unknown[COMPONENT_BOOK_IMBALANCE] = "not_applicable_for_kind_" + kind_u.lower()

    score = round(sum(subscores) / len(subscores), 6) if subscores else None

    if reasons:
        return ActivenessVerdict(STATE_ACTIVE, score, reasons, available, unknown)

    # No trigger. "Quiet" is a claim about the name, so it needs enough of the
    # applicable evidence to actually be a claim.
    required = max(1, (len(applicable) + 1) // 2)
    if len(available) >= required:
        return ActivenessVerdict(STATE_QUIET, score, [], available, unknown)
    return ActivenessVerdict(STATE_UNKNOWN, score, [], available, unknown)


def summarise_ticks(
    ticks: Sequence[PreopenTick],
    *,
    kind: str,
) -> dict[str, Any]:
    """Reduce one instrument's pre-open frames to the auction print + provenance.

    STOCK: the print is the LAST frame carrying volume > 0 — that is the
    matched auction quantity. Frames at 09:00 with volume 0 are order-collection
    indicative frames, not prints, and are never used as a price.

    INDEX: there is no volume. The print is the LAST frame in the window (the
    published pre-open index). If every frame in the window equals the prior
    close exactly and never moves, the stream was a stale carry, not a pre-open
    index — recorded as such rather than reported as a 0.00% gap.
    """
    kind_u = str(kind or "").upper()
    usable = [t for t in ticks if _finite(t.ltp) and (_finite(t.ltp) or 0) > 0]
    out: dict[str, Any] = {
        "tick_count": len(ticks),
        "distinct_price_count": len({_finite(t.ltp) for t in usable}),
        "first_tick_at": min((t.time for t in ticks), default=None),
        "last_tick_at": max((t.time for t in ticks), default=None),
        "preopen_price": None,
        "preopen_price_at": None,
        "preopen_volume": None,
        "preopen_bid": None,
        "preopen_ask": None,
        "total_buy_qty": None,
        "total_sell_qty": None,
        "tick_prev_close": None,
        "data_status": STATUS_NO_TICKS,
        "data_status_reason": None,
    }
    if not ticks:
        out["data_status_reason"] = "no frames captured in the 09:00-09:15 IST window"
        return out

    ordered = sorted(ticks, key=lambda t: t.time)
    # `close` is constant across the window and is the prior-session close.
    closes = [_finite(t.close) for t in ordered if _finite(t.close)]
    out["tick_prev_close"] = closes[-1] if closes else None

    if not usable:
        out["data_status"] = STATUS_NO_MATCH
        out["data_status_reason"] = "frames captured but none carried a usable ltp"
        return out

    if kind_u == "INDEX":
        print_tick = sorted(usable, key=lambda t: t.time)[-1]
        prev_close = out["tick_prev_close"]
        ltp = _finite(print_tick.ltp)
        if out["distinct_price_count"] <= 1 and prev_close is not None and ltp == prev_close:
            out["data_status"] = STATUS_STALE_CARRY
            out["data_status_reason"] = (
                "every pre-open frame equalled the prior close and the value never "
                "moved — a carried-over stale frame, not a published pre-open index"
            )
            return out
        out["preopen_price"] = ltp
        out["preopen_price_at"] = print_tick.time
        out["data_status"] = STATUS_OK
        out["data_status_reason"] = "index_no_traded_volume"  # why preopen_volume is NULL
        return out

    matched = [t for t in usable if (t.volume or 0) > 0]
    if not matched:
        out["data_status"] = STATUS_NO_MATCH
        out["data_status_reason"] = (
            "order-collection frames present but no frame carried a matched "
            "quantity — no auction print was captured"
        )
        return out

    print_tick = sorted(matched, key=lambda t: t.time)[-1]
    out["preopen_price"] = _finite(print_tick.ltp)
    out["preopen_price_at"] = print_tick.time
    out["preopen_volume"] = int(print_tick.volume or 0)
    out["preopen_bid"] = _finite(print_tick.bid)
    out["preopen_ask"] = _finite(print_tick.ask)
    out["total_buy_qty"] = int(print_tick.total_buy_qty) if print_tick.total_buy_qty is not None else None
    out["total_sell_qty"] = int(print_tick.total_sell_qty) if print_tick.total_sell_qty is not None else None
    out["data_status"] = STATUS_OK
    return out


def resolve_prev_close(
    *,
    tick_prev_close: Optional[float],
    spot_prev_close: Optional[float],
) -> tuple[Optional[float], str]:
    """Prior close + its LABELLED source, with an external-anchor cross-check.

    The tick `close` field is the broker's own prior close and is the primary.
    The prior session's last 30-minute spot bar is the external anchor. When
    they disagree by more than the tolerance the tick field is the suspect one
    (that is the documented Fyers cross-symbol contamination failure mode), so
    the anchor wins and the disagreement is recorded in the source label.
    """
    tick_c = _finite(tick_prev_close)
    spot_c = _finite(spot_prev_close)
    if tick_c and tick_c > 0 and spot_c and spot_c > 0:
        drift = abs(tick_c - spot_c) / spot_c * 100.0
        if drift > PREV_CLOSE_ANCHOR_TOLERANCE_PCT:
            return spot_c, "spot_30m_prior_session_anchor_mismatch"
        return tick_c, "tick_close_field"
    if tick_c and tick_c > 0:
        return tick_c, "tick_close_field"
    if spot_c and spot_c > 0:
        return spot_c, "spot_30m_prior_session"
    return None, "unavailable"


def compute_gap_pct(preopen_price: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    price = _finite(preopen_price)
    close = _finite(prev_close)
    if price is None or close is None or close <= 0:
        return None
    return round((price - close) / close * 100.0, 6)


def compute_atr_pct(daily_ohlc: Sequence[Mapping[str, Any]]) -> tuple[Optional[float], int]:
    """ATR% over the trailing sessions, from session-aggregated 30m spot bars.

    `daily_ohlc` is ascending by session with keys high/low/close. True range
    uses the prior close, so the overnight gap is inside the denominator too —
    that is what makes "gap vs ATR" a fair comparison rather than a tautology.
    Returns (atr_pct, sessions_used). atr_pct is None when the sample is too
    small; the count is always returned so the reason is recordable.
    """
    rows = [r for r in daily_ohlc if _finite(r.get("high")) and _finite(r.get("low")) and _finite(r.get("close"))]
    # Structural backstop — see ATR_MAX_SESSION_RANGE_FRACTION.
    rows = [
        r for r in rows
        if float(r["close"]) > 0
        and (float(r["high"]) - float(r["low"])) / float(r["close"]) <= ATR_MAX_SESSION_RANGE_FRACTION
    ]
    if len(rows) < 2:
        return None, len(rows)
    trs: list[float] = []
    for prev, cur in zip(rows, rows[1:]):
        pc = float(prev["close"])
        hi = max(float(cur["high"]), pc)
        lo = min(float(cur["low"]), pc)
        trs.append(hi - lo)
    used = trs[-ATR_TARGET_SESSIONS:]
    if len(used) < ATR_MIN_SESSIONS:
        return None, len(used)
    last_close = float(rows[-1]["close"])
    if last_close <= 0:
        return None, len(used)
    atr = statistics.fmean(used)
    return round(atr / last_close * 100.0, 6), len(used)


def atr_sample_last_session(daily_ohlc: Sequence[Mapping[str, Any]]) -> Optional[date]:
    """Session date of the newest bar that could enter the ATR sample.

    Kept separate from `compute_atr_pct` so the staleness fact is recorded on
    the row even when the ATR itself is perfectly usable.
    """
    sessions = [r.get("session") for r in daily_ohlc if isinstance(r.get("session"), date)]
    return max(sessions) if sessions else None


def compute_rel_volume(
    preopen_volume: Optional[int],
    baseline_volumes: Sequence[float],
) -> tuple[Optional[float], Optional[float], int]:
    """(ratio, baseline_median, baseline_n) against the name's OWN history."""
    usable = [float(v) for v in baseline_volumes if _finite(v) and float(v) > 0]
    n = len(usable)
    if preopen_volume is None or preopen_volume <= 0:
        return None, (round(statistics.median(usable), 4) if usable else None), n
    if n < REL_VOLUME_MIN_BASELINE:
        return None, (round(statistics.median(usable), 4) if usable else None), n
    median = statistics.median(usable)
    if median <= 0:
        return None, round(median, 4), n
    return round(float(preopen_volume) / median, 6), round(median, 4), n


def compute_book_imbalance(buy_qty: Optional[int], sell_qty: Optional[int]) -> Optional[float]:
    b = _finite(buy_qty)
    s = _finite(sell_qty)
    if b is None or s is None:
        return None
    total = b + s
    if total <= 0:
        return None
    return round((b - s) / total, 6)


def peer_imbalance_stats(values: Sequence[float]) -> tuple[Optional[float], Optional[float], int]:
    """(median, MAD-derived sigma, n) of one session's cross-section.

    Robust statistics on purpose: the reference must not be dragged by a few
    extreme books.
    """
    usable = [float(v) for v in values if _finite(v) is not None]
    n = len(usable)
    if n < BOOK_IMBALANCE_MIN_PEERS:
        return (round(statistics.median(usable), 6) if usable else None), None, n
    median = statistics.median(usable)
    mad = statistics.median([abs(v - median) for v in usable])
    sigma = mad * MAD_TO_SIGMA
    return round(median, 6), (round(sigma, 6) if sigma > 0 else None), n


def compute_book_imbalance_z(
    raw: Optional[float],
    peer_median: Optional[float],
    peer_sigma: Optional[float],
) -> Optional[float]:
    if raw is None or peer_median is None or not peer_sigma or peer_sigma <= 0:
        return None
    return round((raw - peer_median) / peer_sigma, 6)


# ══════════════════════════════════════════════════════════════════════════
# (2) I/O — bounded reads, one row per (session_date, underlying)
# ══════════════════════════════════════════════════════════════════════════
async def load_universe(kinds: Sequence[str] = ("INDEX", "STOCK")) -> list[tuple[str, str]]:
    """(underlying, kind) for the F&O universe. MCX is not in this catalog."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT symbol, kind
                      FROM fo_underlying_catalog
                     WHERE kind = ANY(:kinds)
                     ORDER BY kind, symbol
                    """
                ),
                {"kinds": list(kinds)},
            )
        ).fetchall()
    return [(str(r[0]), str(r[1]).upper()) for r in rows]


async def load_preopen_ticks(session_date: date) -> dict[str, list[PreopenTick]]:
    """Every frame in the pre-open band, keyed by tick symbol.

    ONE bounded query. `time` is bound directly with literal UTC instants (the
    15-minute band), so chunk exclusion applies — never a function over `time`.
    """
    start_utc, end_utc = preopen_window_utc(session_date)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT symbol, time, ltp, volume, close, bid, ask,
                           total_buy_qty, total_sell_qty
                      FROM market_ticks
                     WHERE time >= :start_utc
                       AND time <  :end_utc
                     ORDER BY symbol, time
                    """
                ),
                {"start_utc": start_utc, "end_utc": end_utc},
            )
        ).fetchall()

    out: dict[str, list[PreopenTick]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append(
            PreopenTick(
                time=r[1],
                ltp=r[2],
                volume=r[3],
                close=r[4],
                bid=r[5],
                ask=r[6],
                total_buy_qty=r[7],
                total_sell_qty=r[8],
            )
        )
    return out


async def load_prior_daily_ohlc(
    session_date: date,
    *,
    lookback_days: int = 40,
) -> dict[str, list[dict[str, Any]]]:
    """Session-aggregated OHLC per underlying from 30-minute spot bars.

    There is no daily bar store in this schema, so the daily series is derived
    from the 30-minute grid — real bars, aggregated, nothing synthesised.
    `time` is bound directly with literal UTC instants; the IST session bucket
    appears only in SELECT/GROUP BY, never in WHERE.
    """
    end_utc = datetime.combine(session_date, dt_time(0, 0), tzinfo=IST).astimezone(UTC)
    start_utc = end_utc - timedelta(days=lookback_days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT underlying,
                           (time AT TIME ZONE 'Asia/Kolkata')::date AS sess,
                           MAX(high) AS hi,
                           MIN(low)  AS lo,
                           (ARRAY_AGG(close ORDER BY time DESC))[1] AS cl
                      FROM underlying_spot_candles
                     WHERE time >= :start_utc
                       AND time <  :end_utc
                       AND interval IN ('30minute', '1minute')
                       AND source = ANY(:sources)
                       AND high IS NOT NULL
                       AND low IS NOT NULL
                       AND close IS NOT NULL
                     GROUP BY 1, 2
                     ORDER BY 1, 2
                    """
                ),
                {
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "sources": list(ATR_TRUSTED_SOURCES),
                },
            )
        ).fetchall()

    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append(
            {"session": r[1], "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
        )
    return out


async def load_volume_baselines(
    session_date: date,
    *,
    lookback_sessions: int = 20,
) -> dict[str, list[float]]:
    """Each name's own prior matched pre-open volumes, from this very table.

    Reading the baseline from `preopen_spot_snapshots` (not from raw ticks) is
    deliberate: it means the baseline only ever contains verified auction
    prints, and it makes the backfill order-dependent in the honest direction —
    sessions must be processed oldest→newest, so an early session genuinely has
    a thinner baseline than a later one instead of borrowing the future.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT underlying, preopen_volume
                      FROM (
                        SELECT underlying,
                               preopen_volume,
                               ROW_NUMBER() OVER (
                                 PARTITION BY underlying ORDER BY session_date DESC
                               ) AS rn
                          FROM {TABLE}
                         WHERE session_date < :session_date
                           AND data_status = '{STATUS_OK}'
                           AND preopen_volume IS NOT NULL
                           AND preopen_volume > 0
                      ) x
                     WHERE rn <= :lookback
                    """
                ),
                {"session_date": session_date, "lookback": int(lookback_sessions)},
            )
        ).fetchall()
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append(float(r[1]))
    return out


_UPSERT_SQL = text(
    f"""
    INSERT INTO {TABLE} (
        session_date, underlying, kind, tick_symbol,
        data_status, data_status_reason, source, universe_source,
        window_start, window_end, tick_count, distinct_price_count,
        first_tick_at, last_tick_at,
        preopen_price, preopen_price_at, preopen_volume,
        preopen_bid, preopen_ask, total_buy_qty, total_sell_qty,
        prev_close, prev_close_source, gap_pct,
        activeness_state, activeness_score,
        activeness_reasons, components_available, components_unknown,
        rel_volume, rel_volume_baseline, rel_volume_baseline_n,
        gap_atr_ratio, atr_pct_14, atr_sessions_n, atr_last_session, book_imbalance,
        book_imbalance_z, peer_median_book_imbalance, peer_sigma_book_imbalance, peer_n,
        definition_version, computed_at
    ) VALUES (
        :session_date, :underlying, :kind, :tick_symbol,
        :data_status, :data_status_reason, :source, :universe_source,
        :window_start, :window_end, :tick_count, :distinct_price_count,
        :first_tick_at, :last_tick_at,
        :preopen_price, :preopen_price_at, :preopen_volume,
        :preopen_bid, :preopen_ask, :total_buy_qty, :total_sell_qty,
        :prev_close, :prev_close_source, :gap_pct,
        :activeness_state, :activeness_score,
        CAST(:activeness_reasons AS jsonb),
        CAST(:components_available AS jsonb),
        CAST(:components_unknown AS jsonb),
        :rel_volume, :rel_volume_baseline, :rel_volume_baseline_n,
        :gap_atr_ratio, :atr_pct_14, :atr_sessions_n, :atr_last_session, :book_imbalance,
        :book_imbalance_z, :peer_median_book_imbalance, :peer_sigma_book_imbalance, :peer_n,
        :definition_version, now()
    )
    ON CONFLICT (session_date, underlying) DO UPDATE SET
        kind = EXCLUDED.kind,
        tick_symbol = EXCLUDED.tick_symbol,
        data_status = EXCLUDED.data_status,
        data_status_reason = EXCLUDED.data_status_reason,
        source = EXCLUDED.source,
        universe_source = EXCLUDED.universe_source,
        window_start = EXCLUDED.window_start,
        window_end = EXCLUDED.window_end,
        tick_count = EXCLUDED.tick_count,
        distinct_price_count = EXCLUDED.distinct_price_count,
        first_tick_at = EXCLUDED.first_tick_at,
        last_tick_at = EXCLUDED.last_tick_at,
        preopen_price = EXCLUDED.preopen_price,
        preopen_price_at = EXCLUDED.preopen_price_at,
        preopen_volume = EXCLUDED.preopen_volume,
        preopen_bid = EXCLUDED.preopen_bid,
        preopen_ask = EXCLUDED.preopen_ask,
        total_buy_qty = EXCLUDED.total_buy_qty,
        total_sell_qty = EXCLUDED.total_sell_qty,
        prev_close = EXCLUDED.prev_close,
        prev_close_source = EXCLUDED.prev_close_source,
        gap_pct = EXCLUDED.gap_pct,
        activeness_state = EXCLUDED.activeness_state,
        activeness_score = EXCLUDED.activeness_score,
        activeness_reasons = EXCLUDED.activeness_reasons,
        components_available = EXCLUDED.components_available,
        components_unknown = EXCLUDED.components_unknown,
        rel_volume = EXCLUDED.rel_volume,
        rel_volume_baseline = EXCLUDED.rel_volume_baseline,
        rel_volume_baseline_n = EXCLUDED.rel_volume_baseline_n,
        gap_atr_ratio = EXCLUDED.gap_atr_ratio,
        atr_pct_14 = EXCLUDED.atr_pct_14,
        atr_sessions_n = EXCLUDED.atr_sessions_n,
        atr_last_session = EXCLUDED.atr_last_session,
        book_imbalance = EXCLUDED.book_imbalance,
        book_imbalance_z = EXCLUDED.book_imbalance_z,
        peer_median_book_imbalance = EXCLUDED.peer_median_book_imbalance,
        peer_sigma_book_imbalance = EXCLUDED.peer_sigma_book_imbalance,
        peer_n = EXCLUDED.peer_n,
        definition_version = EXCLUDED.definition_version,
        computed_at = now()
    """
)


async def persist_rows(rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    # Drop private carriers (leading underscore) — they are computation
    # plumbing, never columns, and a stray key would break the bind set.
    payload = [{k: v for k, v in dict(r).items() if not k.startswith("_")} for r in rows]
    for r in payload:
        r["activeness_reasons"] = json.dumps(r.get("activeness_reasons") or [])
        r["components_available"] = json.dumps(r.get("components_available") or [])
        r["components_unknown"] = json.dumps(r.get("components_unknown") or {})
    async with AsyncSessionLocal() as session:
        await session.execute(_UPSERT_SQL, payload)
        await session.commit()
    return len(payload)


def build_row(
    *,
    session_date: date,
    underlying: str,
    kind: str,
    tick_symbol: Optional[str],
    ticks: Sequence[PreopenTick],
    spot_prev_close: Optional[float],
    daily_ohlc: Sequence[Mapping[str, Any]],
    baseline_volumes: Sequence[float],
    universe_source: str,
    session_dark: bool = False,
) -> dict[str, Any]:
    """One instrument → one fully-derived row. No value is ever invented."""
    start_utc, end_utc = preopen_window_utc(session_date)
    summary = summarise_ticks(ticks, kind=kind)

    data_status = summary["data_status"]
    data_status_reason = summary["data_status_reason"]
    if not ticks and tick_symbol is None:
        data_status_reason = (
            "instrument is not in the pre-open tick-capture subscription set"
        )
    # Session-dark is the DOMINANT fact and must win the reason: on a dark
    # session nothing can be concluded about any instrument, including the ones
    # that are also outside the subscription set. (Checked last on purpose —
    # the first version let the subscription-set message overwrite it, which
    # made a dark session read like a per-instrument omission.)
    if session_dark and not ticks:
        data_status = STATUS_SESSION_DARK
        data_status_reason = (
            "the WS tape captured ZERO pre-open frames for ANY instrument in this "
            "session — the pre-open is genuinely absent, not quiet"
        )

    preopen_price = summary["preopen_price"]
    prev_close, prev_close_source = resolve_prev_close(
        tick_prev_close=summary["tick_prev_close"],
        spot_prev_close=spot_prev_close,
    )
    gap_pct = compute_gap_pct(preopen_price, prev_close)

    # Price-band sanity: a legal auction equilibrium cannot sit outside the
    # exchange operating range. Outside it the whole FRAME is corrupt — this is
    # the documented Fyers WS cross-symbol contamination (2026-07-20 incident):
    # a coherent frame belonging to another instrument arrives under this
    # symbol, carrying ITS price AND ITS volume AND ITS book. Measured on
    # 2026-07-17 this hit 17 of 59 captured names.
    #
    # So the rejection must void EVERY field the frame supplied, not just the
    # price. Keeping the volume would poison this name's own relative-volume
    # baseline with another name's quantity — the exact silent corruption this
    # table exists to avoid. Only the capture provenance (counts, timestamps)
    # and the externally-anchored prior close survive.
    if data_status == STATUS_OK and gap_pct is not None and abs(gap_pct) > PRICE_BAND_PCT:
        data_status = STATUS_BAND_REJECT
        data_status_reason = (
            f"pre-open print {preopen_price} is {gap_pct:.2f}% from the prior close "
            f"{prev_close} — outside the +/-{PRICE_BAND_PCT:.0f}% NSE pre-open "
            f"operating range, so the whole frame is corrupt (cross-symbol "
            f"contamination), not a signal. Price, volume and book all voided."
        )
        preopen_price = None
        gap_pct = None
        summary["preopen_volume"] = None
        summary["preopen_bid"] = None
        summary["preopen_ask"] = None
        summary["total_buy_qty"] = None
        summary["total_sell_qty"] = None

    atr_pct, atr_n = compute_atr_pct(daily_ohlc)
    atr_last = atr_sample_last_session(daily_ohlc)
    atr_unavailable_reason: Optional[str] = None
    # A real ATR built from bars that stopped weeks ago is not today's daily
    # range. Refuse it and say so, rather than quietly normalising a live gap
    # against a stale denominator.
    if atr_pct is not None and atr_last is not None:
        staleness_days = (session_date - atr_last).days
        if staleness_days > ATR_MAX_SAMPLE_STALENESS_DAYS:
            atr_unavailable_reason = (
                f"atr_sample_stale_last_session={atr_last.isoformat()}"
                f"_{staleness_days}d_gt_{ATR_MAX_SAMPLE_STALENESS_DAYS}d"
            )
            atr_pct = None
    gap_atr_ratio = None
    if gap_pct is not None and atr_pct is not None and atr_pct > 0:
        gap_atr_ratio = round(abs(gap_pct) / atr_pct, 6)

    rel_volume, rel_baseline, rel_n = compute_rel_volume(
        summary["preopen_volume"], baseline_volumes
    )
    book_imbalance = compute_book_imbalance(
        summary["total_buy_qty"], summary["total_sell_qty"]
    )

    return {
        "session_date": session_date,
        "underlying": underlying,
        "kind": str(kind).upper(),
        "tick_symbol": tick_symbol,
        "data_status": data_status,
        "data_status_reason": data_status_reason,
        "source": SOURCE_WS_TAPE,
        "universe_source": universe_source,
        "window_start": start_utc,
        "window_end": end_utc,
        "tick_count": summary["tick_count"],
        "distinct_price_count": summary["distinct_price_count"],
        "first_tick_at": summary["first_tick_at"],
        "last_tick_at": summary["last_tick_at"],
        "preopen_price": preopen_price,
        "preopen_price_at": summary["preopen_price_at"] if preopen_price is not None else None,
        "preopen_volume": summary["preopen_volume"],
        "preopen_bid": summary["preopen_bid"],
        "preopen_ask": summary["preopen_ask"],
        "total_buy_qty": summary["total_buy_qty"],
        "total_sell_qty": summary["total_sell_qty"],
        "prev_close": prev_close,
        "prev_close_source": prev_close_source,
        "gap_pct": gap_pct,
        # Activeness is filled by apply_session_activeness(): component C3 is a
        # CROSS-SECTIONAL statistic, so no single instrument can be judged
        # before the whole session's auction books are on the table.
        "activeness_state": STATE_UNKNOWN,
        "activeness_score": None,
        "activeness_reasons": [],
        "components_available": [],
        "components_unknown": {},
        "rel_volume": rel_volume,
        "rel_volume_baseline": rel_baseline,
        "rel_volume_baseline_n": rel_n,
        "gap_atr_ratio": gap_atr_ratio,
        "atr_pct_14": atr_pct,
        "atr_sessions_n": atr_n,
        "atr_last_session": atr_last,
        # Private carrier: consumed by apply_session_activeness() so the C2
        # `components_unknown` entry says "stale" rather than implying the
        # sample was too small. Popped before the row is persisted.
        "_atr_unavailable_reason": atr_unavailable_reason,
        "book_imbalance": book_imbalance,
        "book_imbalance_z": None,
        "peer_median_book_imbalance": None,
        "peer_sigma_book_imbalance": None,
        "peer_n": 0,
        "definition_version": DEFINITION_VERSION,
    }


def apply_session_activeness(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Second phase: de-mean C3 against the session cross-section, then judge.

    The peer set is the session's OK stock auction prints only — indices have
    no auction book, and rejected/absent rows contribute nothing.
    """
    out = [dict(r) for r in rows]
    peer_values = [
        float(r["book_imbalance"])
        for r in out
        if r.get("kind") == "STOCK"
        and r.get("data_status") == STATUS_OK
        and r.get("book_imbalance") is not None
    ]
    median, sigma, peer_n = peer_imbalance_stats(peer_values)

    for r in out:
        z = compute_book_imbalance_z(r.get("book_imbalance"), median, sigma)
        r["book_imbalance_z"] = z
        r["peer_median_book_imbalance"] = median
        r["peer_sigma_book_imbalance"] = sigma
        r["peer_n"] = peer_n
        verdict = compute_activeness(
            kind=r.get("kind", "STOCK"),
            data_status=r.get("data_status", STATUS_NO_TICKS),
            rel_volume=r.get("rel_volume"),
            rel_volume_baseline_n=r.get("rel_volume_baseline_n"),
            gap_atr_ratio=r.get("gap_atr_ratio"),
            atr_sessions_n=r.get("atr_sessions_n"),
            book_imbalance_z=z,
            peer_n=peer_n,
            atr_unavailable_reason=r.get("_atr_unavailable_reason"),
        )
        r["activeness_state"] = verdict.state
        r["activeness_score"] = verdict.score
        r["activeness_reasons"] = verdict.reasons
        r["components_available"] = verdict.available
        r["components_unknown"] = verdict.unknown
        # Private carrier — must never reach the upsert parameter set.
        r.pop("_atr_unavailable_reason", None)
    return out


async def build_session_snapshot(
    session_date: date,
    *,
    universe_source: str = "session_catalog",
    persist: bool = True,
) -> dict[str, Any]:
    """Compute (and by default persist) the whole session's pre-open snapshot.

    `universe_source`:
      session_catalog — walk the full F&O catalog and write a row for every
                        name, so a name that did NOT tick is a RECORDED
                        `no_preopen_ticks`, not a silent omission.
      ticks_only      — only names that actually ticked. Used when backfilling
                        past sessions, where today's catalog is not that
                        session's universe and asserting it would be an
                        anachronism.
    """
    started = datetime.now(UTC)
    ticks_by_symbol = await load_preopen_ticks(session_date)
    session_dark = not ticks_by_symbol

    universe = await load_universe()
    kind_by_symbol: dict[str, str] = {u: k for u, k in universe}

    if universe_source == "ticks_only":
        selected: list[tuple[str, str]] = []
        for underlying, kind in universe:
            sym = tick_symbol_for(underlying, kind)
            if sym and ticks_by_symbol.get(sym):
                selected.append((underlying, kind))
    else:
        selected = list(universe)

    daily = await load_prior_daily_ohlc(session_date)
    baselines = await load_volume_baselines(session_date)

    rows: list[dict[str, Any]] = []
    for underlying, kind in selected:
        sym = tick_symbol_for(underlying, kind)
        instrument_ticks = ticks_by_symbol.get(sym, []) if sym else []
        series = daily.get(underlying, [])
        spot_prev_close = float(series[-1]["close"]) if series else None
        rows.append(
            build_row(
                session_date=session_date,
                underlying=underlying,
                kind=kind,
                tick_symbol=sym,
                ticks=instrument_ticks,
                spot_prev_close=spot_prev_close,
                daily_ohlc=series,
                baseline_volumes=baselines.get(underlying, []),
                universe_source=universe_source,
                session_dark=session_dark,
            )
        )

    # Second phase — C3 is cross-sectional, so the verdict can only be reached
    # once every instrument in the session has been measured.
    rows = apply_session_activeness(rows)

    written = await persist_rows(rows) if persist else 0

    by_status: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for r in rows:
        by_status[r["data_status"]] = by_status.get(r["data_status"], 0) + 1
        by_state[r["activeness_state"]] = by_state.get(r["activeness_state"], 0) + 1

    active = sorted(
        (r for r in rows if r["activeness_state"] == STATE_ACTIVE),
        key=lambda r: (r["activeness_score"] or 0.0),
        reverse=True,
    )
    summary = {
        "session_date": session_date.isoformat(),
        "universe_source": universe_source,
        "definition_version": DEFINITION_VERSION,
        "session_dark": session_dark,
        "rows": len(rows),
        "written": written,
        "by_data_status": by_status,
        "by_activeness_state": by_state,
        "active_top": [
            {
                "underlying": r["underlying"],
                "score": r["activeness_score"],
                "reasons": r["activeness_reasons"],
                "gap_pct": r["gap_pct"],
            }
            for r in active[:15]
        ],
        "mcx_excluded_reason": MCX_EXCLUSION_REASON,
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
    }
    logger.info(
        "[preopen-spot] {} rows={} written={} statuses={} states={} dark={}",
        session_date.isoformat(), len(rows), written, by_status, by_state, session_dark,
    )
    return summary


async def load_session_rows(
    session_date: date,
    *,
    state: Optional[str] = None,
    underlying: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read back one session's snapshot (read-only; used by the API).

    Every filter is pushed into SQL. Filtering a single name in Python AFTER
    the LIMIT would silently miss it whenever the session has more rows than
    the page size — and this session has 217.
    """
    clauses = ["session_date = :session_date"]
    params: dict[str, Any] = {"session_date": session_date, "limit": int(limit)}
    if state:
        clauses.append("activeness_state = :state")
        params["state"] = state
    if underlying:
        clauses.append("upper(underlying) = :underlying")
        params["underlying"] = str(underlying).strip().upper()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT session_date, underlying, kind, tick_symbol,
                           data_status, data_status_reason, universe_source,
                           tick_count, first_tick_at, last_tick_at,
                           preopen_price, preopen_price_at, preopen_volume,
                           preopen_bid, preopen_ask, total_buy_qty, total_sell_qty,
                           prev_close, prev_close_source, gap_pct,
                           activeness_state, activeness_score, activeness_reasons,
                           components_available, components_unknown,
                           rel_volume, rel_volume_baseline, rel_volume_baseline_n,
                           gap_atr_ratio, atr_pct_14, atr_sessions_n, atr_last_session,
                           book_imbalance, book_imbalance_z,
                           peer_median_book_imbalance, peer_sigma_book_imbalance, peer_n,
                           definition_version, computed_at
                      FROM {TABLE}
                     WHERE {' AND '.join(clauses)}
                     ORDER BY (activeness_score IS NULL), activeness_score DESC, underlying
                     LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().fetchall()
    return [dict(r) for r in rows]


async def load_session_coverage(limit: int = 60) -> list[dict[str, Any]]:
    """Per-session coverage: how many rows, how many real prints, how many dark."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT session_date,
                           COUNT(*) AS rows,
                           COUNT(*) FILTER (WHERE data_status = 'ok') AS ok_rows,
                           COUNT(*) FILTER (WHERE preopen_volume IS NOT NULL) AS with_volume,
                           COUNT(*) FILTER (WHERE activeness_state = 'active') AS active_rows,
                           COUNT(*) FILTER (WHERE activeness_state = 'quiet') AS quiet_rows,
                           COUNT(*) FILTER (WHERE activeness_state = 'unknown') AS unknown_rows
                      FROM {TABLE}
                     GROUP BY session_date
                     ORDER BY session_date DESC
                     LIMIT :limit
                    """
                ),
                {"limit": int(limit)},
            )
        ).mappings().fetchall()
    return [dict(r) for r in rows]


async def run_preopen_spot_snapshot(now: Optional[datetime] = None) -> dict[str, Any]:
    """Runner entry point. Flag-gated OFF by default.

    Fires AFTER the pre-open window closes (09:12-09:30 IST) and only reads
    ticks that are already in Postgres — no broker call, no competition with
    the 09:04-09:14 MACD ladder build.
    """
    from core.config import settings

    if not bool(getattr(settings, "PREOPEN_SPOT_SNAPSHOT_ENABLED", False)):
        return {"status": "disabled", "flag": "PREOPEN_SPOT_SNAPSHOT_ENABLED"}
    now = now or _now_ist()
    return await build_session_snapshot(now.astimezone(IST).date())
