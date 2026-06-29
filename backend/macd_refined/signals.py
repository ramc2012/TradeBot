"""Signal generation for MACD Refined (spec §4–§5).

Methodology (matches the research that produced `data/signals/macd_signals.parquet`):

  For each (underlying, expiry, option_type) the strategy watches ONE contract —
  the ATM strike selected at contract start (≈ the prior monthly expiry) — and
  takes the FIRST premium-MACD(12,26,9) zero-cross-up as the entry trigger. It is
  one early ATM long per cycle per leg, gated by the low-IV / liquidity / entry-
  window filters and annotated with the volume-led directional context.

This is deliberately NOT "every intraday MACD cross on every strike" — that
over-trades late, noisy, theta-bleeding crosses and inverts the edge.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from macd_refined.indicators import (
    compute_macd,
    daily_turnover_series,
    iv_rank,
    realized_vol_annualized,
    trailing_baseline_turnover,
    zero_cross_up,
)
from macd_refined.schemas import MacdSignal

# Realized-vol comparison uses daily spot; 20 sessions, 252-day annualisation.
_RV_SPOT_WINDOW = 20
_BARS_PER_YEAR_DAILY = 252.0


def infer_strike_step(strikes: pd.Series | np.ndarray) -> float:
    vals = np.unique(np.asarray(strikes, dtype=float))
    vals = vals[np.isfinite(vals)]
    if vals.size < 2:
        return max(float(vals[0]) * 0.01, 1.0) if vals.size else 1.0
    diffs = np.diff(np.sort(vals))
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return 1.0
    return float(np.median(diffs))


def _daily_spot_ma(spot_frame: pd.DataFrame, sessions: int) -> pd.Series:
    if spot_frame is None or spot_frame.empty:
        return pd.Series(dtype="float64")
    work = spot_frame.copy()
    daily_close = work.assign(_d=work["time"].dt.date).groupby("_d")["close"].last()
    return daily_close.rolling(int(max(sessions, 1)), min_periods=1).mean()


def _asof(series: pd.Series, d) -> float:
    """Last value of a date-indexed series on or before date `d` (no lookahead)."""
    if series is None or series.empty:
        return float("nan")
    prior = series[[idx <= d for idx in series.index]]
    if prior.empty:
        return float("nan")
    return float(prior.iloc[-1])


def _daily_close(spot_frame: pd.DataFrame) -> pd.Series:
    if spot_frame is None or spot_frame.empty:
        return pd.Series(dtype="float64")
    work = spot_frame.copy()
    return work.assign(_d=work["time"].dt.date).groupby("_d")["close"].last()


def _daily_directional_turnover(option_frame: pd.DataFrame) -> pd.DataFrame:
    """Per session: total CE vs PE turnover for the underlying (spec §4.5)."""
    work = option_frame.loc[:, ["time", "option_type", "close", "volume"]].copy()
    work["turnover"] = (
        pd.to_numeric(work["close"], errors="coerce").fillna(0.0)
        * pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
    )
    work["_d"] = work["time"].dt.date
    pivot = work.groupby(["_d", "option_type"])["turnover"].sum().unstack(fill_value=0.0)
    for col in ("CE", "PE"):
        if col not in pivot.columns:
            pivot[col] = 0.0
    return pivot


def _direction_bias(
    ce_turn: float, pe_turn: float, *, pe_ratio: float, ce_ratio: float
) -> tuple[str, float]:
    """Map the CE/PE turnover imbalance to a directional read (spec §4.5)."""
    if pe_turn <= 0 and ce_turn <= 0:
        return "neutral", 0.0
    if pe_turn >= pe_ratio * max(ce_turn, 1e-9):
        return "down", 0.89   # PE-dominant → high-confidence down (study)
    if ce_turn >= ce_ratio * max(pe_turn, 1e-9):
        return "up", 0.68     # CE-dominant → softer up (study)
    return "neutral", 0.43    # two-sided


def _select_atm_strike(otype_frame: pd.DataFrame, start_spot: float, step: float) -> float | None:
    """Nearest available strike to the contract-start spot."""
    strikes = np.unique(otype_frame["strike"].to_numpy(dtype=float))
    strikes = strikes[np.isfinite(strikes)]
    if strikes.size == 0 or not np.isfinite(start_spot) or start_spot <= 0:
        return None
    return float(strikes[int(np.argmin(np.abs(strikes - start_spot)))])


def generate_signals(
    *,
    underlying: str,
    expiry: date,
    option_frame: pd.DataFrame,
    spot_frame: pd.DataFrame,
    atm_iv_history: pd.Series,
    config: dict[str, Any],
    contract_lot_size: int = 1,
) -> list[MacdSignal]:
    """Emit at most one ATM premium-MACD zero-cross signal per leg (CE/PE)."""
    sig_cfg = config["signal"]
    flt_cfg = config["filters"]
    if option_frame is None or option_frame.empty:
        return []

    frame = option_frame[option_frame["underlying"] == underlying].copy()
    if frame.empty:
        return []

    step = infer_strike_step(frame["strike"])
    if not np.isfinite(step) or step <= 0:
        return []

    # Contract-start spot = the earliest underlying_price in this cycle's data
    # (≈ the prior monthly expiry / selection time). The ATM strike is fixed
    # for the whole cycle off this spot, mirroring the research.
    finite_spot = frame.loc[np.isfinite(frame["underlying_price"]) & (frame["underlying_price"] > 0)]
    if finite_spot.empty:
        return []
    start_spot = float(finite_spot.sort_values("time").iloc[0]["underlying_price"])

    daily_dir = _daily_directional_turnover(frame)
    spot_ma = _daily_spot_ma(spot_frame, int(flt_cfg.get("trend_ma_sessions", 20)))
    # Daily spot closes (date-indexed) — sliced strictly before each signal
    # date so the realised-vol fallback never sees future bars (causality).
    daily_close = _daily_close(spot_frame)

    min_bars = int(sig_cfg["macd_slow"]) + int(sig_cfg["macd_signal"])
    window_days = int(flt_cfg["entry_window_days_before_expiry"])
    iv_rank_max = float(flt_cfg["iv_rank_max"])
    iv_below_median_ratio = float(flt_cfg["iv_below_median_ratio"])
    use_rv_fallback = bool(flt_cfg.get("iv_below_realized_vol", True))
    min_turnover = float(flt_cfg["min_daily_turnover_rupees"])
    baseline_sessions = int(sig_cfg["volume_baseline_sessions"])
    iv_rank_window = int(flt_cfg["iv_rank_window_sessions"])

    # ── Leg selection (spec §4.3 / §5.4) ─────────────────────────────────
    # Both ATM legs' premiums tend to cross MACD up on cycle-start vol
    # expansion, but only the trend-aligned leg pays (a CE bleeds while spot
    # falls). Decide ONE direction per cycle, causally, from the spot trend at
    # contract start: spot > 20d MA → buy CE (up), spot < 20d MA → buy PE
    # (down). Fall back to the CE/PE turnover imbalance when spot/MA is
    # unavailable (e.g. an index with no spot file); only if BOTH are
    # unavailable do we consider both legs.
    legs_to_consider: list[str] = ["CE", "PE"]
    cycle_direction = "neutral"
    if flt_cfg.get("trend_alignment_enabled", True):
        start_date = pd.Timestamp(finite_spot["time"].min()).date()
        ma_at_start = _asof(spot_ma, start_date)
        if np.isfinite(ma_at_start) and ma_at_start > 0:
            cycle_direction = "up" if start_spot >= ma_at_start else "down"
        else:
            # Volume-bias fallback over the first sessions of the cycle.
            early = daily_dir.iloc[: int(sig_cfg["volume_baseline_sessions"])] if not daily_dir.empty else daily_dir
            ce_sum = float(early["CE"].sum()) if "CE" in early else 0.0
            pe_sum = float(early["PE"].sum()) if "PE" in early else 0.0
            if pe_sum >= float(sig_cfg["pe_dominant_ratio"]) * max(ce_sum, 1e-9):
                cycle_direction = "down"
            elif ce_sum >= float(sig_cfg["ce_dominant_ratio"]) * max(pe_sum, 1e-9):
                cycle_direction = "up"
        if cycle_direction == "up":
            legs_to_consider = ["CE"]
        elif cycle_direction == "down":
            legs_to_consider = ["PE"]

    signals: list[MacdSignal] = []

    for option_type in legs_to_consider:
        otype_frame = frame[frame["option_type"] == option_type]
        if otype_frame.empty:
            continue
        atm_strike = _select_atm_strike(otype_frame, start_spot, step)
        if atm_strike is None:
            continue
        contract = (
            otype_frame[otype_frame["strike"].astype(float) == atm_strike]
            .sort_values("time")
            .reset_index(drop=True)
        )
        # Keep only bars with a real traded premium so flat zero-volume padding
        # at the start of the contract's life doesn't manufacture noise crosses.
        contract = contract[contract["close"].astype(float) > 0].reset_index(drop=True)
        if len(contract) < min_bars + 2:
            continue

        macd, macd_sig, hist = compute_macd(
            contract["close"],
            int(sig_cfg["macd_fast"]),
            int(sig_cfg["macd_slow"]),
            int(sig_cfg["macd_signal"]),
        )
        crosses = zero_cross_up(macd)
        cross_positions = [int(x) for x in np.where(crosses.to_numpy())[0] if x >= min_bars]
        if not cross_positions:
            continue
        i = cross_positions[0]  # FIRST zero-cross-up of the cycle

        row = contract.iloc[i]
        sig_time = pd.Timestamp(row["time"])
        sig_date = sig_time.date()
        spot = float(row.get("underlying_price") or 0.0)
        if not np.isfinite(spot) or spot <= 0:
            spot = start_spot
        premium = float(row.get("close") or 0.0)
        if premium <= 0:
            continue
        dte = (expiry - sig_date).days
        if dte < 0:
            continue
        iv_val = float(row.get("iv") or 0.0)

        # ── Gates ──
        passed_window = dte >= window_days

        # All IV / realised-vol inputs are sliced to data strictly BEFORE the
        # signal date — the gate decision uses only information available then.
        prior_iv = (
            atm_iv_history[[d < sig_date for d in atm_iv_history.index]]
            if atm_iv_history is not None and not atm_iv_history.empty
            else pd.Series(dtype="float64")
        )
        ivr = iv_rank(iv_val, prior_iv.iloc[-iv_rank_window:]) if not prior_iv.empty else None
        prior_close = daily_close[[d < sig_date for d in daily_close.index]] if not daily_close.empty else daily_close
        realized_vol = realized_vol_annualized(prior_close, _RV_SPOT_WINDOW, _BARS_PER_YEAR_DAILY)
        passed_iv = False
        if ivr is not None:
            passed_iv = ivr < iv_rank_max
        if not passed_iv and not prior_iv.empty:
            iv_median_prior = float(prior_iv.median())
            if iv_median_prior > 0:
                passed_iv = iv_val < iv_below_median_ratio * iv_median_prior
        if not passed_iv and use_rv_fallback and realized_vol > 0:
            passed_iv = iv_val < realized_vol

        contract_daily_turnover = daily_turnover_series(contract)
        baseline_turn = trailing_baseline_turnover(
            contract_daily_turnover, as_of_date=sig_date, sessions=baseline_sessions
        )
        signal_day_turn = float(contract_daily_turnover.get(sig_date, 0.0))
        effective_turn = baseline_turn if baseline_turn > 0 else signal_day_turn
        passed_liquidity = effective_turn >= min_turnover

        # Trend already encoded by the cycle-direction leg selection above.
        passed_trend = True

        surge_ratio = (signal_day_turn / baseline_turn) if baseline_turn > 0 else 0.0
        ce_turn = float(daily_dir.loc[sig_date, "CE"]) if sig_date in daily_dir.index else 0.0
        pe_turn = float(daily_dir.loc[sig_date, "PE"]) if sig_date in daily_dir.index else 0.0
        bias, bias_conf = _direction_bias(
            ce_turn, pe_turn,
            pe_ratio=float(sig_cfg["pe_dominant_ratio"]),
            ce_ratio=float(sig_cfg["ce_dominant_ratio"]),
        )

        # IV is mapping-only unless an explicit iv_gate is enabled (it isn't by
        # default) — the strategy is pure premium-MACD + liquidity + window.
        iv_gate_enabled = bool(flt_cfg.get("iv_gate_enabled", False))
        reasons: list[str] = []
        if not passed_window:
            reasons.append(f"inside last {window_days}d to expiry (dte={dte})")
        if iv_gate_enabled and not passed_iv:
            reasons.append(f"IV not cheap (iv={iv_val:.3f} rank={ivr})")
        if not passed_liquidity:
            reasons.append(f"turnover ₹{effective_turn:,.0f} < floor ₹{min_turnover:,.0f}")
        if not passed_trend:
            reasons.append("trend misaligned")

        accepted = passed_window and passed_liquidity and passed_trend and (passed_iv or not iv_gate_enabled)
        signals.append(
            MacdSignal(
                underlying=underlying,
                expiry=expiry.isoformat(),
                option_type=option_type,
                strike=float(atm_strike),
                signal_time=sig_time.isoformat(),
                premium_at_signal=premium,
                macd=float(macd.iloc[i]),
                macd_signal=float(macd_sig.iloc[i]),
                histogram=float(hist.iloc[i]),
                spot_at_signal=spot,
                days_to_expiry=float(dte),
                iv=iv_val,
                iv_rank=ivr,
                realized_vol=realized_vol or None,
                daily_turnover_rupees=effective_turn,
                lot_size=int(contract_lot_size or 1),
                tick_size=0.05,
                signal_kind="macd_confirmation",
                volume_surge_ratio=float(surge_ratio),
                direction_bias=bias,
                direction_confidence=float(bias_conf),
                passed_iv_gate=passed_iv,
                passed_liquidity_gate=passed_liquidity,
                passed_window_gate=passed_window,
                passed_trend_gate=passed_trend,
                accepted=accepted,
                reasons=reasons,
            )
        )

    signals.sort(key=lambda s: s.signal_time)
    return signals
