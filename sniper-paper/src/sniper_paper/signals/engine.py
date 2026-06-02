"""Signal engine: at every decision tick, runs all detectors, scores candidates,
applies the EV gate, and emits a (taken, skipped) decision.

A signal is always WRITTEN regardless of gate decision — for audit + future
retraining. Only `gate_decision == 'take'` flows to the executor.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from sniper_paper.common.settings import Instrument, Settings
from sniper_paper.common.time import minutes_into_session
from sniper_paper.features.live import build_feature_vector, feature_vector_to_array
from sniper_paper.features.mp_state import MPState, compute_mp_state, compute_session_mp
from sniper_paper.model.loader import ActiveModel, predict_one
from sniper_paper.signals.candidate import Candidate
from sniper_paper.signals.detectors import (
    detect_failed_auction,
    detect_ib_breakout,
    detect_lvn_rejection,
    detect_poc_magnet,
    detect_va_acceptance,
    detect_va_rejection,
)


@dataclass
class ScoredSignal:
    candidate: Candidate
    p_win: float
    expected_net_R: float
    in_distribution: bool
    gate_decision: str           # 'take' | 'skip'
    gate_reason: str | None
    feature_values: dict


def _gate(
    p_win: float, expected_net_R: float, in_distribution: bool,
    cand: Candidate, settings: Settings,
) -> tuple[str, str | None]:
    sig = settings.signal
    if not in_distribution and not settings.risk.allow_ood_paper_trades:
        return "skip", "ood_blocked"
    if expected_net_R < sig.ev_threshold_R:
        return "skip", f"ev_below_threshold ({expected_net_R:.3f} < {sig.ev_threshold_R})"
    if p_win < sig.min_p_win:
        return "skip", f"p_win_below_threshold ({p_win:.3f} < {sig.min_p_win})"
    if cand.risk_per_unit() <= 0:
        return "skip", "invalid_barriers"
    return "take", None


def evaluate(
    instrument: Instrument,
    decision_ts: pd.Timestamp,
    session_ticks: pd.DataFrame,
    prev_session_ticks: pd.DataFrame,
    spot: float,
    model: ActiveModel,
    settings: Settings,
) -> list[ScoredSignal]:
    """Run all enabled detectors. Score and gate each candidate."""
    from datetime import datetime
    from sniper_paper.common.time import IST, parse_hm
    session_open = pd.Timestamp(
        datetime.combine(
            decision_ts.date(),
            parse_hm(instrument.trading_hours_ist.open),
            tzinfo=IST,
        )
    )
    mp = compute_mp_state(session_ticks, decision_ts, session_open, instrument.tick_size)
    if mp is None:
        return []
    prev_mp = (
        compute_session_mp(
            prev_session_ticks,
            session_open - pd.Timedelta(days=1),
            session_open - pd.Timedelta(minutes=1),
            instrument.tick_size,
        )
        if not prev_session_ticks.empty
        else None
    )

    mins = minutes_into_session(decision_ts, instrument)
    last_window = session_ticks.tail(20) if len(session_ticks) >= 1 else session_ticks
    recent_high = float(last_window["ltp"].max()) if not last_window.empty else spot
    recent_low = float(last_window["ltp"].min()) if not last_window.empty else spot
    intraday_high = float(session_ticks["ltp"].max()) if not session_ticks.empty else spot
    intraday_low = float(session_ticks["ltp"].min()) if not session_ticks.empty else spot
    last_ret = 0.0
    if len(last_window) >= 2:
        first = float(last_window["ltp"].iloc[0])
        if first > 0:
            last_ret = (spot - first) / first

    enabled = set(settings.signal.setup_families)
    candidates: list[Candidate] = []
    if "va_rejection" in enabled:
        c = detect_va_rejection(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, recent_high, recent_low)
        if c: candidates.append(c)
    if "va_acceptance" in enabled:
        c = detect_va_acceptance(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, mins)
        if c: candidates.append(c)
    if "ib_breakout" in enabled:
        c = detect_ib_breakout(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, mins)
        if c: candidates.append(c)
    if "lvn_rejection" in enabled:
        c = detect_lvn_rejection(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, last_ret)
        if c: candidates.append(c)
    if "poc_magnet" in enabled:
        c = detect_poc_magnet(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, mins)
        if c: candidates.append(c)
    if "failed_auction" in enabled:
        c = detect_failed_auction(decision_ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, intraday_high, intraday_low)
        if c: candidates.append(c)

    if not candidates:
        return []

    # Build feature vector once per decision moment.
    fv = build_feature_vector(
        instrument=instrument,
        decision_ts=decision_ts,
        session_ticks=session_ticks,
        prev_session_ticks=prev_session_ticks,
        spot=spot,
    )
    x = feature_vector_to_array(fv, model.feature_order)
    p_win, exp_R = predict_one(model, x)

    out: list[ScoredSignal] = []
    for cand in candidates:
        # If the candidate is short, flip p_win (we trained on "p_win for the
        # candidate's intended direction"; if the trained model is direction-
        # agnostic this becomes the identity). We keep it simple: model output
        # is interpreted as probability the candidate trade succeeds.
        gate_decision, gate_reason = _gate(p_win, exp_R, instrument.model_in_distribution, cand, settings)
        out.append(
            ScoredSignal(
                candidate=cand,
                p_win=p_win,
                expected_net_R=exp_R,
                in_distribution=instrument.model_in_distribution,
                gate_decision=gate_decision,
                gate_reason=gate_reason,
                feature_values=fv.values,
            )
        )
    return out
