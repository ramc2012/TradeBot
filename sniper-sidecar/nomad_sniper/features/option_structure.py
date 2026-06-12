"""Families C & D — ATM option price-structure and flow features (contract §5.C/D).

This is the family that encodes the core insight: *a balancing underlying with
disproportionate CE/PE premium behaviour tells you whether a move is coming.* Direction is
read elsewhere (on the underlying); here we read move/no-move, IV regime, and lean.

**Null-safe by contract.** When ATM option data is unavailable (`AtmSeries.available` is
False) every feature is emitted as null so the row schema is stable. No raw premium / volume
/ OI ever leaves this module — only ratios, %, z-scores, Δ, and bounded scores (prefix ``o_``).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nomad_sniper.data.option_bars import AtmSeries
from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.features.market_profile import build_volume_profile
from nomad_sniper.utils.normalize import pct_change, safe_ratio, zscore
from nomad_sniper.utils.timeutil import ensure_ist

# Underlying "balancing" threshold for o_balanced_divergence: |underlying %Δ| < ε.
BALANCED_EPS_PCT = 0.10

_C_NAMES = (
    "o_implied_move_atr",
    "o_iv_vs_realized",
    "o_straddle_decay_vs_theta",
    "o_iv_level",
    "o_iv_change",
    "o_ce_pe_premium_ratio",
    "o_ce_pe_premium_ratio_drift",
    "o_ce_ret_minus_pe_ret",
    "o_balanced_divergence",
    "o_ce_value_break_vs_u_hold",
    "o_pe_value_break_vs_u_hold",
    "o_straddle_value_width_ratio",
)
_D_NAMES = (
    "o_ce_oi_change_pct",
    "o_pe_oi_change_pct",
    "o_ce_volume_z",
    "o_pe_volume_z",
    "o_pcr_volume",
    "o_pcr_oi",
    "o_ce_pe_aggressor_imbalance",
)
OPTION_FEATURE_NAMES = _C_NAMES + _D_NAMES


def build_option_features(
    decision_time: datetime,
    atm: AtmSeries | None,
    bars_underlying: pd.DataFrame,
    *,
    snapshot: FeatureSnapshot | None = None,
    lookback_minutes: int = 30,
    spot_bars: pd.DataFrame | None = None,
    atr_ref: float | None = None,
) -> FeatureSnapshot:
    """Families C+D from the ATM CE/PE/straddle series. Null-safe when option data absent.

    `spot_bars` (index spot) is used as the underlying reference for the option-vs-underlying
    features (balanced-divergence gate, value-break-vs-underlying-hold) — options are priced
    off spot, so the futures basis would make these comparisons inconsistent. Falls back to
    `bars_underlying` when spot is unavailable.
    """
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)
    u_ref = spot_bars if spot_bars is not None else bars_underlying

    if atm is None or not atm.available:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    # Option bars are close-stamped at load (data/bars.close_stamp), so `index <= decision_time`
    # = bars that have CLOSED by t — no forward leak from the in-progress 30-min bar.
    ce = atm.ce[atm.ce.index <= decision_time]
    pe = atm.pe[atm.pe.index <= decision_time]
    if ce.empty or pe.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    avail = ensure_ist(min(ce.index[-1], pe.index[-1]).to_pydatetime())
    ce_w = ce.tail(lookback_minutes)
    pe_w = pe.tail(lookback_minutes)

    ce_last = float(ce_w["close"].iloc[-1])
    pe_last = float(pe_w["close"].iloc[-1])
    ce_first = float(ce_w["close"].iloc[0])
    pe_first = float(pe_w["close"].iloc[0])

    # ---- Family C ----
    # Straddle realized decay vs theoretical theta proxy.
    snapshot.add(Feature(
        "o_straddle_decay_vs_theta",
        _straddle_decay_vs_theta(atm, decision_time, lookback_minutes),
        avail, "option",
    ))

    # IV level + change (mean of CE/PE iv where present).
    iv_now = _mean_iv(ce_w, pe_w, -1)
    iv_then = _mean_iv(ce_w, pe_w, 0)
    snapshot.add(Feature("o_iv_level", iv_now, avail, "option"))
    snapshot.add(Feature(
        "o_iv_change", (iv_now - iv_then) if (iv_now is not None and iv_then is not None) else None,
        avail, "option",
    ))

    # ── Premium-LEVEL normalization (instrument-independence): the raw ATM straddle premium is
    # the market's price of movement on THIS instrument; on its own it is instrument-specific
    # (strike/IV/DTE scale). Normalize it by the underlying's OWN scale to make it comparable
    # across instruments. Both are in the underlying's price-points, so the ratio is dimensionless.
    spot_now = _last_close(u_ref, decision_time)
    straddle_now = ce_last + pe_last
    # (a) Implied move in ATR units = ATM straddle ÷ ATR. "How many ATRs of movement is priced
    #     in over the option's remaining life." DTE is carried separately (c_days_to_weekly_expiry).
    impl_atr = None
    if atr_ref and atr_ref > 0:
        impl_atr = float(min(20.0, straddle_now / atr_ref))
    snapshot.add(Feature("o_implied_move_atr", impl_atr, avail, "option"))
    # (b) Variance-risk premium = IV ÷ realized vol (rich/cheap vol). Realized vol proxied from
    #     ATR%: realized_ann ≈ (ATR/spot)·√252. Ratio is instrument-independent (the ATR→vol
    #     constant cancels across instruments). The key directional-vs-mean-revert signal.
    iv_vs_real = None
    if iv_now is not None and atr_ref and atr_ref > 0 and spot_now and spot_now > 0:
        realized_ann = (atr_ref / spot_now) * np.sqrt(252.0)
        if realized_ann > 0:
            iv_vs_real = float(min(10.0, iv_now / realized_ann))
    snapshot.add(Feature("o_iv_vs_realized", iv_vs_real, avail, "option"))

    # CE/PE premium *share* = CE/(CE+PE) ∈ [0,1] — bounded form of the premium ratio (a raw
    # ratio explodes when one leg → 0, violating §2). Drift = change in share over the window.
    share_now = _share(ce_last, pe_last)
    share_then = _share(ce_first, pe_first)
    snapshot.add(Feature("o_ce_pe_premium_ratio", share_now, avail, "option"))
    snapshot.add(Feature(
        "o_ce_pe_premium_ratio_drift",
        (share_now - share_then) if (share_now is not None and share_then is not None) else None,
        avail, "option",
    ))

    # CE return minus PE return over window, as a CLIPPED fraction (±1 = ±100%).
    ce_ret = _clip_frac((ce_last - ce_first) / ce_first if ce_first else None)
    pe_ret = _clip_frac((pe_last - pe_first) / pe_first if pe_first else None)
    ce_minus_pe = (ce_ret - pe_ret) if (ce_ret is not None and pe_ret is not None) else None
    snapshot.add(Feature("o_ce_ret_minus_pe_ret", ce_minus_pe, avail, "option"))

    # Balanced divergence: |CE%Δ − PE%Δ| gated on underlying balancing (spot-referenced). Bounded.
    u_ret = _underlying_window_return_pct(u_ref, decision_time, lookback_minutes)
    balanced = None
    if ce_minus_pe is not None and u_ret is not None:
        balanced = abs(ce_minus_pe) if abs(u_ret) < BALANCED_EPS_PCT else 0.0
    snapshot.add(Feature("o_balanced_divergence", balanced, avail, "option"))

    # Value-break vs underlying-holds: CE/PE break own developing VA while underlying in value.
    u_holds = _underlying_holds_value(u_ref, decision_time)
    snapshot.add(Feature(
        "o_ce_value_break_vs_u_hold", _value_break(ce_w, u_holds), avail, "option",
    ))
    snapshot.add(Feature(
        "o_pe_value_break_vs_u_hold", _value_break(pe_w, u_holds), avail, "option",
    ))

    # Straddle developing VA width ÷ trailing median width.
    snapshot.add(Feature(
        "o_straddle_value_width_ratio", _straddle_width_ratio(atm, decision_time, lookback_minutes),
        avail, "option",
    ))

    # ---- Family D ---- (all bounded: %-clipped, shares ∈ [0,1], or within-series z)
    snapshot.add(Feature("o_ce_oi_change_pct", _oi_change_pct(ce_w), avail, "option"))
    snapshot.add(Feature("o_pe_oi_change_pct", _oi_change_pct(pe_w), avail, "option"))
    # Option volume z vs the option's OWN trailing window (ATM strike rolls daily, so a
    # cross-session same-TOD baseline does not exist → was all-null).
    snapshot.add(Feature("o_ce_volume_z", _series_volume_z(ce_w), avail, "option"))
    snapshot.add(Feature("o_pe_volume_z", _series_volume_z(pe_w), avail, "option"))
    # PCR as put *share* ∈ [0,1] (bounded form; raw P/C ratio explodes when one leg → 0).
    snapshot.add(Feature(
        "o_pcr_volume", _share(float(pe_w["volume"].sum()), float(ce_w["volume"].sum())),
        avail, "option"))
    pcr_oi = None
    if ce_w["oi"].notna().any() and pe_w["oi"].notna().any():
        pcr_oi = _share(float(pe_w["oi"].dropna().iloc[-1]), float(ce_w["oi"].dropna().iloc[-1]))
    snapshot.add(Feature("o_pcr_oi", pcr_oi, avail, "option"))
    snapshot.add(Feature(
        "o_ce_pe_aggressor_imbalance", _aggressor_imbalance(ce_w, pe_w), avail, "option"))

    return snapshot


# ─── helpers ────────────────────────────────────────────────────────
def _last_close(bars: pd.DataFrame | None, decision_time: datetime) -> float | None:
    """Underlying close at/just before decision_time (the spot scale for premium normalization)."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return None
    w = bars[bars.index <= decision_time]
    if w.empty:
        return None
    v = float(w["close"].iloc[-1])
    return v if np.isfinite(v) and v > 0 else None


def _mean_iv(ce_w: pd.DataFrame, pe_w: pd.DataFrame, pos: int) -> float | None:
    vals = []
    for w in (ce_w, pe_w):
        if "iv" in w.columns and w["iv"].notna().any():
            try:
                v = float(w["iv"].iloc[pos])
                if np.isfinite(v):
                    vals.append(v)
            except (TypeError, ValueError):
                pass
    return float(np.mean(vals)) if vals else None


def _straddle_decay_vs_theta(atm: AtmSeries, decision_time: datetime, lookback: int) -> float | None:
    """Realized straddle decay rate ÷ theoretical theta rate (ratio).

    Realized decay rate = −(straddle_now − straddle_start)/minutes (positive = decaying).
    Theta proxy: when IV unavailable we cannot price theta; fall back to a time-decay proxy
    using fraction of session elapsed (crude, unitless). Returns None when straddle missing.
    """
    if atm.straddle is None or atm.straddle.empty:
        return None
    s = atm.straddle[atm.straddle.index <= decision_time].tail(lookback)
    if len(s) < 2:
        return None
    minutes = max(1.0, (s.index[-1] - s.index[0]).total_seconds() / 60.0)
    realized_decay_rate = -(float(s.iloc[-1]) - float(s.iloc[0])) / minutes
    # Theoretical theta proxy: straddle value × (1 / minutes_remaining_in_session).
    # This gives a unitless decay-vs-expected ratio without needing greeks.
    end_of_session = decision_time.replace(hour=15, minute=30, second=0, microsecond=0)
    minutes_remaining = max(1.0, (end_of_session - decision_time).total_seconds() / 60.0)
    theoretical_rate = float(s.iloc[-1]) / minutes_remaining
    return safe_ratio(realized_decay_rate, theoretical_rate)


def _underlying_window_return_pct(bars_u: pd.DataFrame, decision_time: datetime, lookback: int) -> float | None:
    w = bars_u[bars_u.index <= decision_time].tail(lookback)
    if len(w) < 2:
        return None
    return pct_change(float(w["close"].iloc[-1]) - float(w["close"].iloc[0]), float(w["close"].iloc[0]))


def _underlying_holds_value(bars_u: pd.DataFrame, decision_time: datetime) -> bool | None:
    """True if current underlying price is within its developing value area."""
    today = decision_time.date()
    cur = bars_u[(bars_u.index.date == today) & (bars_u.index <= decision_time)]
    if cur.empty:
        return None
    prof = build_volume_profile(cur)
    price = float(cur["close"].iloc[-1])
    return bool(prof.val <= price <= prof.vah)


def _value_break(opt_w: pd.DataFrame, u_holds: bool | None) -> int | None:
    """Binary: option breaks its own developing VA while the underlying holds value."""
    if u_holds is None or opt_w.empty:
        return None
    prof = build_volume_profile(opt_w)
    last = float(opt_w["close"].iloc[-1])
    opt_breaks = last > prof.vah or last < prof.val
    return int(opt_breaks and u_holds)


def _straddle_width_ratio(atm: AtmSeries, decision_time: datetime, lookback: int,
                          win: int = 3) -> float | None:
    """Recent straddle range ÷ trailing-median range, bounded. Uses a small BAR-count window
    (the straddle series is a single session ~13 bars at 30-min, so a 30-bar window was always
    insufficient → all-null). Result clipped to [0, 10]."""
    if atm.straddle is None or atm.straddle.empty:
        return None
    s = atm.straddle[atm.straddle.index <= decision_time]
    if len(s) < 2 * win:
        return None
    recent_width = float(s.tail(win).max() - s.tail(win).min())
    widths = s.rolling(win).apply(lambda x: x.max() - x.min(), raw=True).dropna()
    if widths.empty:
        return None
    r = safe_ratio(recent_width, float(widths.median()))
    return None if r is None else float(min(10.0, r))


def _share(a: float | None, b: float | None) -> float | None:
    """Bounded share a/(a+b) ∈ [0,1] — the §2-safe form of a ratio (a raw ratio explodes when
    b → 0). None-safe; None when both are 0/missing."""
    if a is None or b is None:
        return None
    tot = a + b
    return float(a / tot) if tot != 0 else None


def _clip_frac(x: float | None, lo: float = -1.0, hi: float = 1.0) -> float | None:
    """Clip a fractional change to [lo, hi] (option returns beyond ±100% are illiquid prints)."""
    if x is None or not np.isfinite(x):
        return None
    return float(min(hi, max(lo, x)))


def _oi_change_pct(opt_w: pd.DataFrame) -> float | None:
    """OI change as % of prior OI, CLIPPED to [-100, 300] (raw % explodes when prior OI tiny)."""
    if "oi" not in opt_w.columns or not opt_w["oi"].notna().any():
        return None
    oi = opt_w["oi"].dropna().astype(float)
    if len(oi) < 2:
        return None
    p = pct_change(float(oi.iloc[-1]) - float(oi.iloc[0]), float(oi.iloc[0]))
    return None if p is None else float(min(300.0, max(-100.0, p)))


def _series_volume_z(opt_w: pd.DataFrame, recent_bars: int = 1, baseline_bars: int = 12,
                     min_base: int = 3) -> float | None:
    """Z-score of the option's recent volume vs its OWN trailing window in the same SESSION.

    The ATM series is a single session (rolling strike), so there is no cross-session same-TOD
    baseline (that was all-null). Uses whatever trailing bars have accumulated so far — needs
    only `min_base` prior bars, so it populates from mid-session onward."""
    if "volume" not in opt_w.columns or len(opt_w) < recent_bars + min_base:
        return None
    v = opt_w["volume"].astype(float)
    recent = float(v.iloc[-recent_bars:].mean())
    base = v.iloc[-(baseline_bars + recent_bars):-recent_bars]
    if len(base) < min_base:
        return None
    return zscore(recent, float(base.mean()), float(base.std(ddof=1)))


def _aggressor_imbalance(ce_w: pd.DataFrame, pe_w: pd.DataFrame) -> float | None:
    """Inferred (CE aggressive − PE aggressive) flow, bounded to [-1, 1].

    Aggressive proxy per side = signed-volume (candle direction × volume) summed over window.
    Imbalance = (ce_signed − pe_signed) / (|ce_signed| + |pe_signed|).
    """
    def signed(w: pd.DataFrame) -> float:
        body = w["close"] - w["open"]
        rng = (w["high"] - w["low"]).replace(0, np.nan)
        return float(((body / rng).fillna(0.0) * w["volume"]).sum())

    ce_s = signed(ce_w)
    pe_s = signed(pe_w)
    denom = abs(ce_s) + abs(pe_s)
    if denom == 0:
        return 0.0
    return (ce_s - pe_s) / denom


def _emit_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    avail = ensure_ist(decision_time)
    for name in OPTION_FEATURE_NAMES:
        snapshot.add(Feature(name, None, avail, "option"))
