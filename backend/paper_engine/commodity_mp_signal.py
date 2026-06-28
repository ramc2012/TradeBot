"""Market-Profile + Order-Flow signal engine for the MCX futures lane.

Replaces the 15-minute MACD trigger that previously drove
`commodity_strategy_agent._analyze_futures_symbol`. All four canonical
auction entries are evaluated on 1-minute closed bars; the highest-
priority non-`None` trigger wins. The output shape mirrors what the
old MACD evaluator emitted so the surrounding harness
(`_open_new_futures_positions`, decorators, audit, UI table) keeps
working unchanged.

Reuses without modification:

* `MarketProfileEngine` snapshots for `poc/vah/val/ibh/ibl/poor_high/
  poor_low/single_prints/tpo_counts/period_count`.
* `analytics.orderflow` for `anchored_cvd`, `cvd_agrees_with`,
  `cvd_divergence`, `vwap_bands`, `volume_node_density`, `hvn_lvn`.
* `analytics.market_profile_ext` for `ib_extension`, `poc_migration`,
  `value_area_overlap`.

The four triggers, in priority order:

1. **open_drive** — IB prints entirely above prior pVAH (BUY) or below
   prior pVAL (SELL); confirmed by anchored CVD agreement on the first
   bar after IB completion. Highest conviction.
2. **ib_break** — two consecutive 1-min closes outside IBH/IBL with
   bar-CVD agreement and price on the right side of VWAP; skipped if
   IB extension is already > 50%.
3. **failed_auction** — poor-high / poor-low reversal back through
   the value-area edge, confirmed by CVD divergence.
4. **va_migration** — today's value area barely overlaps prior
   (overlap < 30%) and POC shifted > 0.5%; trade in the migration
   direction.
5. **lvn_fade** — fallback: price tests a single-print / LVN level
   from the wrong side with CVD absorption; fade back to POC.

Confidence ∈ [0,1] is observable but doesn't size positions in v1.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

from analytics.market_profile_ext import (
    assess_day_type,
    ib_extension as compute_ib_extension,
    poc_migration as compute_poc_migration,
    value_area_overlap as compute_value_area_overlap,
)
from analytics.orderflow import (
    anchored_cvd,
    bar_cvd,
    cvd_agrees_with,
    cvd_divergence,
    hvn_lvn,
    volume_node_density,
    vwap_bands,
)
from core.config import settings
from market_data.commodity_contract_specs import extract_commodity_root
from paper_engine.commodity_volume_baseline import (
    is_large_volume as _vb_is_large_volume,
    load_baseline as _vb_load_baseline,
    pressure_ratio as _vb_pressure_ratio,
)

# 1-min bars per MP period (15-min TPO) — the granularity at which per-instrument
# volume baselines and the OF quality reads are computed (1-min is too sparse).
_MP_PERIOD_BARS = 15


# Corrected `lvn_fade` absorption thresholds — used ONLY when
# settings.COMMODITY_LVN_ABSORPTION_FIX_ENABLED is set. Textbook absorption is
# LARGE aggressive per-bar flow that FAILS to move price (orders absorbed at the
# level), the inverse of the legacy (buggy) "small flow + moving price" check.
LVN_ABSORB_FLOW_MULT = 1.0    # |3-bar CVD delta| >= this * stdev(per-bar deltas)
LVN_ABSORB_STALL_MULT = 0.15  # |3-bar price move| <= this * ATR (price stalled)


def _of_volume_coverage(
    closed_1m: list[dict[str, Any]],
    *,
    period_bars: int = 15,
    lookback_periods: int = 20,
) -> float:
    """Fraction of recent MP-period buckets carrying nonzero volume (R0 gate).

    Bar-OHLCV volume on MCX is sparse on illiquid names (NICKEL ~5% of 1-min bars
    nonzero, GOLD ~25%) but far denser at the 15-min MP period, so coverage is
    measured at the MP-period bucket, not the 1-min bar. Returns 1.0 (=full, do
    not degrade) when there is too little history to judge.
    """
    if not closed_1m:
        return 1.0
    bars = closed_1m[-(period_bars * lookback_periods):]
    if len(bars) < period_bars:
        return 1.0
    groups = 0
    nonzero = 0
    for i in range(0, len(bars) - period_bars + 1, period_bars):
        chunk = bars[i:i + period_bars]
        groups += 1
        if sum(float(b.get("volume") or 0.0) for b in chunk) > 0.0:
            nonzero += 1
    return (nonzero / groups) if groups else 1.0


def _symbol_excluded_from_daytype(symbol: str) -> bool:
    """True if the symbol's root is on the day-type exclude list (R1) — names
    where the IB/excess reads are noisy across the MCX evening-open."""
    raw = str(getattr(settings, "COMMODITY_DAYTYPE_EXCLUDE_SYMBOLS", "") or "")
    excl = [s.strip().upper() for s in raw.split(",") if s.strip()]
    su = str(symbol or "").upper()
    return any(su.startswith(e) for e in excl)


# ─── Public output shape ───────────────────────────────────────────────────


@dataclass
class TriggerResult:
    """Internal result returned by each `_trigger_*` helper."""

    signal: str  # "BUY" | "SELL"
    entry_style: str  # "open_drive" | "ib_break" | "failed_auction" | "va_migration" | "lvn_fade"
    reason: str  # short audit-friendly tag
    validation_detail: str  # human-readable evidence with numbers
    confidence: float  # 0..1
    stop_hint: Optional[float] = None
    target_hint: Optional[float] = None
    evidence: dict[str, Any] = field(default_factory=dict)


# Priority order — index 0 wins ties.
_TRIGGER_PRIORITY: tuple[str, ...] = (
    "open_drive",
    "ib_break",
    "failed_auction",
    "va_migration",
    "lvn_fade",
)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _candle_close(candle: dict[str, Any]) -> float:
    try:
        return float(candle.get("close") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _candle_high(candle: dict[str, Any]) -> float:
    try:
        return float(candle.get("high") or candle.get("close") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _candle_low(candle: dict[str, Any]) -> float:
    try:
        return float(candle.get("low") or candle.get("close") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # filter NaN


def _attr(profile: Any, name: str) -> Optional[float]:
    """Read a snapshot field that may be present as attr or dict key."""
    if profile is None:
        return None
    if hasattr(profile, name):
        return _safe_float(getattr(profile, name))
    if isinstance(profile, dict):
        return _safe_float(profile.get(name))
    return None


def _profile_attr(profile: Any, name: str, default: Any = None) -> Any:
    if profile is None:
        return default
    if hasattr(profile, name):
        return getattr(profile, name)
    if isinstance(profile, dict):
        return profile.get(name, default)
    return default


def _make_ext_dict(profile: Any) -> dict[str, Optional[float]]:
    """Adapt a MarketProfileSnapshot to the dict shape expected by
    `analytics.market_profile_ext` helpers."""
    return {
        "poc": _attr(profile, "poc"),
        "vah": _attr(profile, "vah"),
        "val": _attr(profile, "val"),
        "ibh": _attr(profile, "initial_balance_high"),
        "ibl": _attr(profile, "initial_balance_low"),
        "session_high": _attr(profile, "high_price"),
        "session_low": _attr(profile, "low_price"),
        "close": _attr(profile, "close_price"),
    }


def _round(value: Optional[float], decimals: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return None


# ─── ATR (1-min, 14 by default) ────────────────────────────────────────────


def _compute_atr(candles: list[dict[str, Any]], period: int = 14) -> Optional[float]:
    if not candles or period <= 0:
        return None
    trs: list[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        h, l, cl = _candle_high(c), _candle_low(c), _candle_close(c)
        if prev_close is None:
            tr = max(h - l, 0.0)
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = cl
    if len(trs) < period:
        return None
    # Wilder smoothing approximated by simple rolling mean — good enough at 14.
    return sum(trs[-period:]) / period


# ─── Triggers ─────────────────────────────────────────────────────────────


def _trigger_open_drive(
    *,
    today_profile: Any,
    prior_profile: Any,
    closed_1m: list[dict[str, Any]],
    cvd_anchored_last: Optional[float],
    of_degraded: bool = False,
) -> Optional[TriggerResult]:
    if prior_profile is None:
        return None
    period_count = int(_profile_attr(today_profile, "period_count", 0) or 0)
    # Only consider during/just after IB (4 × 15-min periods = 60 min).
    if period_count < 4 or period_count > 5:
        return None
    pvah = _attr(prior_profile, "vah")
    pval = _attr(prior_profile, "val")
    ib_high = _attr(today_profile, "initial_balance_high")
    ib_low = _attr(today_profile, "initial_balance_low")
    if None in (pvah, pval, ib_high, ib_low):
        return None
    if not closed_1m or cvd_anchored_last is None:
        return None

    last_close = _candle_close(closed_1m[-1])
    if last_close <= 0:
        return None

    # All IB action must sit entirely on one side of the prior value area.
    drive_up = ib_low > pvah
    drive_dn = ib_high < pval
    if not drive_up and not drive_dn:
        return None

    if drive_up and last_close > ib_high and (of_degraded or cvd_anchored_last > 0):
        return TriggerResult(
            signal="BUY",
            entry_style="open_drive",
            reason="open_drive_up_above_pvah",
            validation_detail=(
                f"Open-drive BUY: IB [{ib_low:.2f}–{ib_high:.2f}] entirely "
                f"above prior pVAH {pvah:.2f}; close {last_close:.2f}, "
                f"anchored CVD {cvd_anchored_last:+.0f}."
            ),
            confidence=0.85,
            stop_hint=float(pvah),
            evidence={
                "prior_pvah": pvah,
                "prior_pval": pval,
                "ib_high": ib_high,
                "ib_low": ib_low,
                "cvd_anchored": cvd_anchored_last,
            },
        )
    if drive_dn and last_close < ib_low and (of_degraded or cvd_anchored_last < 0):
        return TriggerResult(
            signal="SELL",
            entry_style="open_drive",
            reason="open_drive_down_below_pval",
            validation_detail=(
                f"Open-drive SELL: IB [{ib_low:.2f}–{ib_high:.2f}] entirely "
                f"below prior pVAL {pval:.2f}; close {last_close:.2f}, "
                f"anchored CVD {cvd_anchored_last:+.0f}."
            ),
            confidence=0.85,
            stop_hint=float(pval),
            evidence={
                "prior_pvah": pvah,
                "prior_pval": pval,
                "ib_high": ib_high,
                "ib_low": ib_low,
                "cvd_anchored": cvd_anchored_last,
            },
        )
    return None


def _trigger_ib_break(
    *,
    today_profile: Any,
    closed_1m: list[dict[str, Any]],
    cvd_anchored: list[float],
    vwap_last: Optional[float],
    ib_ext: Optional[Any],
    of_degraded: bool = False,
) -> Optional[TriggerResult]:
    period_count = int(_profile_attr(today_profile, "period_count", 0) or 0)
    if period_count < 4 or len(closed_1m) < 2:
        return None
    ib_high = _attr(today_profile, "initial_balance_high")
    ib_low = _attr(today_profile, "initial_balance_low")
    if None in (ib_high, ib_low):
        return None

    last = _candle_close(closed_1m[-1])
    prev = _candle_close(closed_1m[-2])
    if last <= 0 or prev <= 0:
        return None

    # Late-entry guard: if price is already well outside IB, the move is
    # mature and the R:R is bad.
    if ib_ext is not None:
        if ib_ext.extended_above and ib_ext.extension_above_pct > 0.5:
            return None
        if ib_ext.extended_below and ib_ext.extension_below_pct > 0.5:
            return None

    cvd_window = cvd_anchored[-3:] if len(cvd_anchored) >= 3 else cvd_anchored[-2:]

    if last > ib_high and prev > ib_high:
        if not of_degraded and not cvd_agrees_with("BUY", cvd_window):
            return None
        if vwap_last is not None and last < vwap_last:
            return None
        ib_range = ib_high - ib_low
        stop = ib_high - 0.3 * ib_range
        return TriggerResult(
            signal="BUY",
            entry_style="ib_break",
            reason="ib_break_up",
            validation_detail=(
                f"IB break BUY: closes {prev:.2f}/{last:.2f} > IBH {ib_high:.2f}; "
                f"cvdΔ {(cvd_window[-1] - cvd_window[0]):+.0f}; "
                f"vwap {vwap_last:.2f}." if vwap_last is not None
                else f"IB break BUY: closes {prev:.2f}/{last:.2f} > IBH {ib_high:.2f}; "
                     f"cvdΔ {(cvd_window[-1] - cvd_window[0]):+.0f}."
            ),
            confidence=0.75,
            stop_hint=float(stop),
            evidence={
                "ib_high": ib_high,
                "ib_low": ib_low,
                "ib_range": ib_range,
                "cvd_window": list(cvd_window),
                "vwap": vwap_last,
            },
        )
    if last < ib_low and prev < ib_low:
        if not of_degraded and not cvd_agrees_with("SELL", cvd_window):
            return None
        if vwap_last is not None and last > vwap_last:
            return None
        ib_range = ib_high - ib_low
        stop = ib_low + 0.3 * ib_range
        return TriggerResult(
            signal="SELL",
            entry_style="ib_break",
            reason="ib_break_down",
            validation_detail=(
                f"IB break SELL: closes {prev:.2f}/{last:.2f} < IBL {ib_low:.2f}; "
                f"cvdΔ {(cvd_window[-1] - cvd_window[0]):+.0f}; "
                f"vwap {vwap_last:.2f}." if vwap_last is not None
                else f"IB break SELL: closes {prev:.2f}/{last:.2f} < IBL {ib_low:.2f}; "
                     f"cvdΔ {(cvd_window[-1] - cvd_window[0]):+.0f}."
            ),
            confidence=0.75,
            stop_hint=float(stop),
            evidence={
                "ib_high": ib_high,
                "ib_low": ib_low,
                "ib_range": ib_range,
                "cvd_window": list(cvd_window),
                "vwap": vwap_last,
            },
        )
    return None


def _trigger_failed_auction(
    *,
    today_profile: Any,
    closed_1m: list[dict[str, Any]],
    cvd_total: list[float],
    atr_1m: Optional[float],
    of_degraded: bool = False,
) -> Optional[TriggerResult]:
    period_count = int(_profile_attr(today_profile, "period_count", 0) or 0)
    if period_count < 5 or not closed_1m:
        return None
    vah = _attr(today_profile, "vah")
    val = _attr(today_profile, "val")
    poc = _attr(today_profile, "poc")
    poor_high = bool(_profile_attr(today_profile, "poor_high", False))
    poor_low = bool(_profile_attr(today_profile, "poor_low", False))
    high_price = _attr(today_profile, "high_price")
    low_price = _attr(today_profile, "low_price")
    if None in (vah, val, poc):
        return None

    last_close = _candle_close(closed_1m[-1])
    if last_close <= 0:
        return None

    div = cvd_divergence(closed_1m, cvd_total, lookback=20)
    if not of_degraded and (div is None or div.strength < 0.4):
        return None
    # OF-degraded: fall back to structure-only (poor extreme + VA position); the
    # CVD-divergence confirmation is noise on thin volume so it is not required.
    div_strength = div.strength if div is not None else 0.5
    bearish_ok = of_degraded or (div is not None and div.kind == "bearish")
    bullish_ok = of_degraded or (div is not None and div.kind == "bullish")

    atr_pad = max(atr_1m or 0.0, 0.0)

    if poor_high and last_close < vah and bearish_ok and high_price is not None:
        stop = float(high_price) + (atr_pad if atr_pad > 0 else 0.001 * last_close)
        conf = 0.55 + 0.4 * min(div_strength, 1.0) / 2.0
        return TriggerResult(
            signal="SELL",
            entry_style="failed_auction",
            reason="failed_auction_high",
            validation_detail=(
                f"Failed-auction SELL: poor_high at {high_price:.2f} rejected; "
                f"close {last_close:.2f} < VAH {vah:.2f}; bearish CVD divergence "
                f"strength {div_strength:.2f}."
            ),
            confidence=round(conf, 3),
            stop_hint=float(stop),
            target_hint=float(poc),
            evidence={
                "vah": vah,
                "val": val,
                "poc": poc,
                "poor_high_extreme": high_price,
                "divergence_strength": div_strength,
                "atr_1m": atr_1m,
            },
        )
    if poor_low and last_close > val and bullish_ok and low_price is not None:
        stop = float(low_price) - (atr_pad if atr_pad > 0 else 0.001 * last_close)
        conf = 0.55 + 0.4 * min(div_strength, 1.0) / 2.0
        return TriggerResult(
            signal="BUY",
            entry_style="failed_auction",
            reason="failed_auction_low",
            validation_detail=(
                f"Failed-auction BUY: poor_low at {low_price:.2f} rejected; "
                f"close {last_close:.2f} > VAL {val:.2f}; bullish CVD divergence "
                f"strength {div_strength:.2f}."
            ),
            confidence=round(conf, 3),
            stop_hint=float(stop),
            target_hint=float(poc),
            evidence={
                "vah": vah,
                "val": val,
                "poc": poc,
                "poor_low_extreme": low_price,
                "divergence_strength": div_strength,
                "atr_1m": atr_1m,
            },
        )
    return None


def _trigger_va_migration(
    *,
    today_profile: Any,
    prior_profile: Any,
    closed_1m: list[dict[str, Any]],
    cvd_anchored_last: Optional[float],
    of_degraded: bool = False,
) -> Optional[TriggerResult]:
    if prior_profile is None:
        return None
    period_count = int(_profile_attr(today_profile, "period_count", 0) or 0)
    if period_count < 6 or not closed_1m or cvd_anchored_last is None:
        return None

    today_ext = _make_ext_dict(today_profile)
    prior_ext = _make_ext_dict(prior_profile)
    overlap = compute_value_area_overlap(today_ext, prior_ext)
    if overlap is None or overlap >= 0.3:
        return None
    poc_info = compute_poc_migration(today_ext, prior_ext)
    if poc_info is None or abs(poc_info.pct) < 0.005:
        return None

    today_poc = _attr(today_profile, "poc")
    pvah = _attr(prior_profile, "vah")
    pval = _attr(prior_profile, "val")
    if today_poc is None or pvah is None or pval is None:
        return None

    last_close = _candle_close(closed_1m[-1])
    if last_close <= 0:
        return None

    if poc_info.direction == "up" and last_close > today_poc and (of_degraded or cvd_anchored_last > 0):
        return TriggerResult(
            signal="BUY",
            entry_style="va_migration",
            reason="va_migration_up",
            validation_detail=(
                f"VA migration BUY: overlap {overlap:.2f} < 0.30; POC shifted "
                f"+{poc_info.pct * 100:.2f}% to {today_poc:.2f}; close "
                f"{last_close:.2f} > POC; anchored CVD {cvd_anchored_last:+.0f}."
            ),
            confidence=0.65,
            stop_hint=float(pval),
            evidence={
                "overlap": overlap,
                "poc_shift_pct": poc_info.pct,
                "today_poc": today_poc,
                "prior_pval": pval,
            },
        )
    if poc_info.direction == "down" and last_close < today_poc and (of_degraded or cvd_anchored_last < 0):
        return TriggerResult(
            signal="SELL",
            entry_style="va_migration",
            reason="va_migration_down",
            validation_detail=(
                f"VA migration SELL: overlap {overlap:.2f} < 0.30; POC shifted "
                f"{poc_info.pct * 100:.2f}% to {today_poc:.2f}; close "
                f"{last_close:.2f} < POC; anchored CVD {cvd_anchored_last:+.0f}."
            ),
            confidence=0.65,
            stop_hint=float(pvah),
            evidence={
                "overlap": overlap,
                "poc_shift_pct": poc_info.pct,
                "today_poc": today_poc,
                "prior_pvah": pvah,
            },
        )
    return None


def _trigger_lvn_fade(
    *,
    today_profile: Any,
    closed_1m: list[dict[str, Any]],
    cvd_total: list[float],
    atr_1m: Optional[float],
    of_degraded: bool = False,
    baseline: Any = None,
    current_mp_volume: float = 0.0,
) -> Optional[TriggerResult]:
    if not closed_1m:
        return None
    # lvn_fade is a pure order-flow (absorption) trigger; on a thin-volume symbol
    # its CVD inputs are noise, so the R0 quality gate suppresses it entirely.
    if of_degraded:
        return None
    poc = _attr(today_profile, "poc")
    single_prints = list(_profile_attr(today_profile, "single_prints", []) or [])
    if poc is None or not single_prints:
        return None

    last_close = _candle_close(closed_1m[-1])
    if last_close <= 0:
        return None

    histogram = volume_node_density(closed_1m, bins=24)
    nodes = hvn_lvn(histogram)
    in_lvn = any(
        bin_.get("price_low", 0) <= last_close <= bin_.get("price_high", 0)
        for bin_ in nodes.get("lvn", [])
    )
    if not in_lvn:
        return None

    # Find a single-print level between price and POC (price testing the
    # wrong side of a fast-move zone).
    candidates_up = [sp for sp in single_prints if last_close < sp <= poc]
    candidates_dn = [sp for sp in single_prints if poc <= sp < last_close]
    if not (candidates_up or candidates_dn):
        return None

    # Absorption confirmation. The LEGACY definition (kept as default until
    # COMMODITY_LVN_ABSORPTION_FIX_ENABLED is paper-validated) is mis-defined: it
    # fires on SMALL flow + a MOVING price, with sigma taken on the CUMULATIVE CVD
    # series. Textbook absorption is the inverse — LARGE aggressive per-bar flow
    # that FAILS to move price (absorbed at the level) -> fade back toward POC.
    if len(cvd_total) < 6 or atr_1m is None or atr_1m <= 0:
        return None
    cvd_delta_3 = cvd_total[-1] - cvd_total[-4]
    last_n_closes = [_candle_close(c) for c in closed_1m[-4:]]
    price_move_3 = last_n_closes[-1] - last_n_closes[0] if len(last_n_closes) >= 2 else 0.0

    if settings.COMMODITY_LVN_ABSORPTION_FIX_ENABLED:
        # sigma on the per-bar DELTA series (flow intensity), not the level.
        deltas = [cvd_total[i] - cvd_total[i - 1] for i in range(1, len(cvd_total))]
        recent = deltas[-10:]
        try:
            sigma = statistics.pstdev(recent) if len(recent) > 1 else 0.0
        except statistics.StatisticsError:
            sigma = 0.0
        if sigma <= 0:
            return None
        # Per-instrument "large flow": prefer this instrument's learned baseline
        # (its own p90 MP-bar volume) when available — the adaptive-CVD path that
        # replaces R0's demote; else fall back to the rolling CVD-delta sigma.
        if baseline is not None and getattr(baseline, "ready", False):
            flow_strong = _vb_is_large_volume(current_mp_volume, baseline)
        else:
            flow_strong = abs(cvd_delta_3) >= LVN_ABSORB_FLOW_MULT * sigma
        stalled = abs(price_move_3) <= LVN_ABSORB_STALL_MULT * atr_1m
        if not (flow_strong and stalled):
            return None
        # Heavy SELL flow absorbed below POC -> fade up (BUY); heavy BUY flow
        # absorbed above POC -> fade down (SELL).
        if candidates_up and cvd_delta_3 < 0:
            direction = "BUY"
        elif candidates_dn and cvd_delta_3 > 0:
            direction = "SELL"
        else:
            return None
    else:
        window = cvd_total[-10:]
        try:
            sigma = statistics.pstdev(window) if len(window) > 1 else 0.0
        except statistics.StatisticsError:
            sigma = 0.0
        if sigma <= 0:
            return None
        absorbed = abs(cvd_delta_3) < 0.2 * sigma
        moved_enough = abs(price_move_3) >= 0.3 * atr_1m
        if not (absorbed and moved_enough):
            return None
        if candidates_up and price_move_3 < 0:
            direction = "BUY"
        elif candidates_dn and price_move_3 > 0:
            direction = "SELL"
        else:
            return None

    # Fade back toward POC: BUY when price probed below into LVN with a
    # single print above, SELL mirror.
    if direction == "BUY":
        sp_low = min(candidates_up)
        stop = float(sp_low) - 0.5 * atr_1m
        return TriggerResult(
            signal="BUY",
            entry_style="lvn_fade",
            reason="lvn_fade_buy_toward_poc",
            validation_detail=(
                f"LVN fade BUY: close {last_close:.2f} in LVN with single-print "
                f"{sp_low:.2f}<sp≤POC {poc:.2f}; cvd absorbed (Δ {cvd_delta_3:+.0f} "
                f"vs σ {sigma:.0f}); price Δ {price_move_3:+.2f} (absorption confirmed)."
            ),
            confidence=0.45,
            stop_hint=float(stop),
            target_hint=float(poc),
            evidence={
                "single_print": sp_low,
                "poc": poc,
                "cvd_delta_3": cvd_delta_3,
                "sigma": sigma,
                "atr_1m": atr_1m,
            },
        )
    if direction == "SELL":
        sp_high = max(candidates_dn)
        stop = float(sp_high) + 0.5 * atr_1m
        return TriggerResult(
            signal="SELL",
            entry_style="lvn_fade",
            reason="lvn_fade_sell_toward_poc",
            validation_detail=(
                f"LVN fade SELL: close {last_close:.2f} in LVN with single-print "
                f"POC {poc:.2f}≤sp<{sp_high:.2f}; cvd absorbed (Δ {cvd_delta_3:+.0f} "
                f"vs σ {sigma:.0f}); price Δ {price_move_3:+.2f} (absorption confirmed)."
            ),
            confidence=0.45,
            stop_hint=float(stop),
            target_hint=float(poc),
            evidence={
                "single_print": sp_high,
                "poc": poc,
                "cvd_delta_3": cvd_delta_3,
                "sigma": sigma,
                "atr_1m": atr_1m,
            },
        )
    return None


# ─── Public evaluator ─────────────────────────────────────────────────────


def evaluate_commodity_mp_signal(
    closed_1m: list[dict[str, Any]],
    *,
    symbol: str,
    today_profile: Any,
    prior_profile: Optional[Any] = None,
    cvd_anchor_index: Optional[int] = None,
    atr_1m: Optional[float] = None,
) -> dict[str, Any]:
    """Evaluate all four MP+OF triggers + LVN fallback against the latest
    closed 1-min bar and return a row matching the shape today's MACD
    evaluator emits.

    The caller (`_analyze_futures_symbol`) decorates this with symbol-
    specific fields (price, lot_size, change_pct, etc.); we only own the
    signal-decision portion.
    """
    last_bar_time = str(closed_1m[-1].get("time") or "") if closed_1m else ""
    base: dict[str, Any] = {
        "signal": None,
        "candidate_signal": None,
        "candidate_reason": "insufficient_data",
        "raw_signal": None,
        "reason": "insufficient_data",
        "entry_style": None,
        "signal_validation_detail": "Insufficient 1-min history for MP+OF evaluation.",
        "regime": "unknown",
        "macd": None,  # legacy keys preserved as null for any downstream consumer
        "macd_signal": None,
        "macd_histogram": None,
        "prev_macd": None,
        "prev_macd_histogram": None,
        "atr": _round(atr_1m, 4),
        "bar_time": last_bar_time,
        "indicator_timeframe": "1minute",
        "stop_hint": None,
        "target_hint": None,
        "confidence": 0.0,
        "mp_status": "warming_up",
        "mp_day_type": "unknown",
        "mp_reason": "mp_pending",
        "mp_direction": None,
        "mp_poc": _round(_attr(today_profile, "poc"), 2),
        "mp_vah": _round(_attr(today_profile, "vah"), 2),
        "mp_val": _round(_attr(today_profile, "val"), 2),
        "mp_ib_high": _round(_attr(today_profile, "initial_balance_high"), 2),
        "mp_ib_low": _round(_attr(today_profile, "initial_balance_low"), 2),
        "mp_periods": int(_profile_attr(today_profile, "period_count", 0) or 0),
        "mp_session_date": str(_profile_attr(today_profile, "session_date", "") or ""),
        # Full TPO map for the day so the UI can render the classic letter
        # histogram instead of the abbreviated POC/VAH/VAL summary. The map
        # is small (typically ~30-60 price levels × short letter strings) and
        # is already computed by MarketProfileEngine; we just surface it.
        "mp_tpo_letters": _profile_attr(today_profile, "tpo_letters", None) or {},
        "mp_tpo_counts": _profile_attr(today_profile, "tpo_counts", None) or {},
        "mp_tick_size": _safe_float(_profile_attr(today_profile, "tick_size", None)),
        "mp_high": _round(_attr(today_profile, "high_price"), 2),
        "mp_low": _round(_attr(today_profile, "low_price"), 2),
        "mp_single_prints": list(_profile_attr(today_profile, "single_prints", []) or []),
        "mp_poor_high": bool(_profile_attr(today_profile, "poor_high", False)),
        "mp_poor_low": bool(_profile_attr(today_profile, "poor_low", False)),
        "mp_buying_tail": list(_profile_attr(today_profile, "buying_tail", []) or []),
        "mp_selling_tail": list(_profile_attr(today_profile, "selling_tail", []) or []),
        "cvd_latest": None,
        "cvd_session": None,
        "cvd_block_active": False,
        "cvd_window_delta": None,
        "cvd_agrees": False,
        "vwap": None,
        "vwap_upper": None,
        "vwap_lower": None,
        "cvd_divergence": None,
        "hvn_count": 0,
        "lvn_count": 0,
        "ib_extended_above": False,
        "ib_extended_below": False,
        "ib_extension_pct": None,
        "prior_session_date": None,
        "trigger_evidence": {},
    }

    if not closed_1m or today_profile is None:
        return base

    # ── Order flow series ─────────────────────────────────────────────
    anchor = cvd_anchor_index if cvd_anchor_index is not None else 0
    anchor = max(0, min(anchor, len(closed_1m) - 1))
    cvd_total = bar_cvd(closed_1m)
    cvd_anc = anchored_cvd(closed_1m, anchor_index=anchor)
    bands = vwap_bands(closed_1m, anchor_index=anchor)
    vwap_last = bands["vwap"][-1] if bands["vwap"] else None
    vwap_upper_last = bands["upper"][-1] if bands["upper"] else None
    vwap_lower_last = bands["lower"][-1] if bands["lower"] else None
    cvd_anchored_last = cvd_anc[-1] if cvd_anc else None
    cvd_latest = cvd_total[-1] if cvd_total else None

    # IB extension snapshot.
    today_ext = _make_ext_dict(today_profile)
    last_close = _candle_close(closed_1m[-1])
    ib_ext = compute_ib_extension(today_ext, last_close) if last_close > 0 else None

    # Divergence — also surfaced even if no failed_auction fires.
    div = cvd_divergence(closed_1m, cvd_total, lookback=20)

    # Volume node density snapshot (used by lvn_fade and for the UI).
    histogram = volume_node_density(closed_1m, bins=24)
    nodes = hvn_lvn(histogram)
    hvn_count = len(nodes.get("hvn", []))
    lvn_count = len(nodes.get("lvn", []))

    # Regime tag — descriptive, not a signal source.
    poc = _attr(today_profile, "poc") or last_close
    regime = "balance"
    if last_close > 0 and poc is not None:
        if last_close > poc * 1.001:
            regime = "balance_above_poc"
        elif last_close < poc * 0.999:
            regime = "balance_below_poc"
    vah = _attr(today_profile, "vah")
    val = _attr(today_profile, "val")
    if vah is not None and val is not None and last_close > 0:
        if last_close > vah:
            regime = "trend_up"
        elif last_close < val:
            regime = "trend_down"

    base["regime"] = regime
    base["mp_day_type"] = regime

    # R1: day-type classification (assess_day_type) for trigger suppression. Off
    # by default → mp_day_type keeps the crude regime above and no suppression.
    day_type: Optional[str] = None
    if settings.COMMODITY_DAYTYPE_ENABLED and not _symbol_excluded_from_daytype(symbol):
        prior_ext = _make_ext_dict(prior_profile) if prior_profile is not None else None
        assessment = assess_day_type(
            {
                "poc": poc,
                "vah": vah,
                "val": val,
                "ibh": _attr(today_profile, "initial_balance_high"),
                "ibl": _attr(today_profile, "initial_balance_low"),
            },
            last_close,
            ib_extension_info=ib_ext,
            poc_migration_info=(
                compute_poc_migration(today_ext, prior_ext) if prior_ext is not None else None
            ),
            cvd_session=cvd_anchored_last,
        )
        day_type = assessment.classification
        base["mp_day_type"] = day_type
        base["mp_day_type_confidence"] = round(float(assessment.confidence), 3)
    base["cvd_latest"] = _round(cvd_latest, 0)
    base["cvd_session"] = _round(cvd_anchored_last, 0)
    base["vwap"] = _round(vwap_last, 2)
    base["vwap_upper"] = _round(vwap_upper_last, 2)
    base["vwap_lower"] = _round(vwap_lower_last, 2)
    base["cvd_divergence"] = (
        {"kind": div.kind, "strength": round(div.strength, 3)} if div is not None else None
    )
    base["hvn_count"] = hvn_count
    base["lvn_count"] = lvn_count
    base["ib_extended_above"] = bool(ib_ext and ib_ext.extended_above)
    base["ib_extended_below"] = bool(ib_ext and ib_ext.extended_below)
    base["ib_extension_pct"] = (
        ib_ext.extension_above_pct
        if ib_ext and ib_ext.extended_above
        else (ib_ext.extension_below_pct if ib_ext and ib_ext.extended_below else None)
    )
    base["prior_session_date"] = (
        str(_profile_attr(prior_profile, "session_date", "") or "") or None
    )

    period_count = int(_profile_attr(today_profile, "period_count", 0) or 0)
    if period_count >= 4:
        base["mp_status"] = "ready"

    # ── Evaluate triggers in priority order ─────────────────────────
    candidates: dict[str, TriggerResult] = {}

    # ── Per-instrument ADAPTIVE order-flow context (supersedes R0 demote) ────
    # Judge pressure / large-volume RELATIVE to THIS instrument's learned volume
    # distribution (median/p90/p95 of its MP-period bars). CVD is normalized to the
    # instrument's own scale, never turned off. Surfaced for the UI + fed to
    # lvn_fade's absorption. Off by default.
    vol_baseline = None
    current_mp_volume = 0.0
    current_mp_signed = 0.0
    if settings.COMMODITY_VOL_BASELINE_ENABLED:
        try:
            root = extract_commodity_root(symbol) if ":" in str(symbol) else str(symbol or "").strip().upper()
            vol_baseline = _vb_load_baseline(root)
            mp_bucket = closed_1m[-_MP_PERIOD_BARS:]
            current_mp_volume = sum(max(float(b.get("volume") or 0.0), 0.0) for b in mp_bucket)
            if len(cvd_total) >= 2:
                k = min(_MP_PERIOD_BARS, len(cvd_total) - 1)
                current_mp_signed = cvd_total[-1] - cvd_total[-1 - k]
            base["vol_baseline_ready"] = bool(vol_baseline and vol_baseline.ready)
            base["vol_baseline_median"] = _round(getattr(vol_baseline, "median", None), 0) if vol_baseline else None
            base["cvd_pressure_ratio"] = _round(_vb_pressure_ratio(current_mp_signed, vol_baseline), 3)
            base["large_volume"] = bool(_vb_is_large_volume(current_mp_volume, vol_baseline))
        except Exception:
            vol_baseline = None

    # R0 order-flow quality gate — SUPERSEDED by the per-instrument baseline above
    # and FORCED OFF when that path is on (CVD must never be demoted). Retained
    # only as a legacy fallback; default-off either way.
    of_degraded = (
        not settings.COMMODITY_VOL_BASELINE_ENABLED
        and settings.COMMODITY_OF_QUALITY_GATE_ENABLED
        and _of_volume_coverage(closed_1m) < float(settings.COMMODITY_OF_MIN_VOL_COVERAGE)
    )
    base["of_degraded"] = bool(of_degraded)

    open_drive = _trigger_open_drive(
        today_profile=today_profile,
        prior_profile=prior_profile,
        closed_1m=closed_1m,
        cvd_anchored_last=cvd_anchored_last,
        of_degraded=of_degraded,
    )
    if open_drive is not None:
        candidates["open_drive"] = open_drive

    ib_break = _trigger_ib_break(
        today_profile=today_profile,
        closed_1m=closed_1m,
        cvd_anchored=cvd_anc,
        vwap_last=vwap_last,
        ib_ext=ib_ext,
        of_degraded=of_degraded,
    )
    if ib_break is not None:
        candidates["ib_break"] = ib_break

    failed_auction = _trigger_failed_auction(
        today_profile=today_profile,
        closed_1m=closed_1m,
        cvd_total=cvd_total,
        atr_1m=atr_1m,
        of_degraded=of_degraded,
    )
    if failed_auction is not None:
        candidates["failed_auction"] = failed_auction

    va_migration = _trigger_va_migration(
        today_profile=today_profile,
        prior_profile=prior_profile,
        closed_1m=closed_1m,
        cvd_anchored_last=cvd_anchored_last,
        of_degraded=of_degraded,
    )
    if va_migration is not None:
        candidates["va_migration"] = va_migration

    lvn_fade = _trigger_lvn_fade(
        today_profile=today_profile,
        closed_1m=closed_1m,
        cvd_total=cvd_total,
        atr_1m=atr_1m,
        of_degraded=of_degraded,
        baseline=vol_baseline,
        current_mp_volume=current_mp_volume,
    )
    if lvn_fade is not None:
        candidates["lvn_fade"] = lvn_fade

    # R1 day-type suppression: on a trend day drop counter-trend FADES; in
    # balance drop the breakout triggers (go-with-breakout is for imbalance).
    if day_type in ("trend_up", "trend_down"):
        counter = "SELL" if day_type == "trend_up" else "BUY"
        for name in ("failed_auction", "lvn_fade"):
            cand = candidates.get(name)
            if cand is not None and cand.signal == counter:
                candidates.pop(name, None)
                base["signal_validation_detail"] = (
                    f"{name} {counter} suppressed: counter-trend on {day_type} day."
                )
    elif day_type == "balance":
        for name in ("open_drive", "ib_break"):
            if name in candidates:
                candidates.pop(name, None)
                base["signal_validation_detail"] = (
                    f"{name} breakout suppressed: balance day (no established imbalance)."
                )

    chosen: Optional[TriggerResult] = None
    for name in _TRIGGER_PRIORITY:
        if name in candidates:
            chosen = candidates[name]
            break

    if chosen is None:
        base["reason"] = "no_trigger"
        base["candidate_reason"] = "no_trigger"
        base["signal_validation_detail"] = (
            f"MP context ready (period {period_count}); no MP+OF trigger fired this bar."
            if period_count >= 4
            else f"Warming up — only {period_count} MP periods printed (need ≥ 4 for IB triggers)."
        )
        base["mp_reason"] = "mp_no_trigger" if period_count >= 4 else "mp_warming_up"
        # CVD-agreement flag still useful for the dashboard
        if cvd_anc and last_close > 0:
            cvd_recent = cvd_anc[-6:] if len(cvd_anc) >= 6 else cvd_anc
            base["cvd_window_delta"] = _round(
                cvd_recent[-1] - cvd_recent[0] if len(cvd_recent) >= 2 else 0, 0
            )
        return base

    # Trigger fired
    base["signal"] = chosen.signal
    base["candidate_signal"] = chosen.signal
    base["candidate_reason"] = chosen.reason
    base["raw_signal"] = chosen.signal
    base["reason"] = chosen.reason
    base["entry_style"] = chosen.entry_style
    base["signal_validation_detail"] = chosen.validation_detail
    base["stop_hint"] = chosen.stop_hint
    base["target_hint"] = chosen.target_hint
    base["confidence"] = round(float(chosen.confidence), 3)
    base["mp_direction"] = chosen.signal
    base["mp_reason"] = chosen.reason
    base["trigger_evidence"] = dict(chosen.evidence or {})

    cvd_window = cvd_anc[-6:] if len(cvd_anc) >= 6 else cvd_anc
    if len(cvd_window) >= 2:
        base["cvd_window_delta"] = _round(cvd_window[-1] - cvd_window[0], 0)
        base["cvd_agrees"] = cvd_agrees_with(chosen.signal, cvd_window)

    return base


__all__ = [
    "TriggerResult",
    "evaluate_commodity_mp_signal",
    "_compute_atr",
]
