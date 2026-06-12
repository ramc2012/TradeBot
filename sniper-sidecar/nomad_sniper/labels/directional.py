"""Directional grid labeler (contract §4) — the training target.

For every grid point on the underlying we:
  1. place symmetric ± m·ATR barriers and resolve which is hit first within horizon H
     (reusing `triple_barrier.label_triple_barrier` geometry, long-framed),
  2. pass the candidate through the option-economics gate (`profitability_gate`),
  3. emit the five target heads (contract §4.3) + a `sample_weight` placeholder.

One label row per (underlying, decision_time). Heads `magnitude_atr`, `time_to_target`,
`mae_atr` are only meaningful on `is_move == 1` rows (train regression heads on that subset).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from tqdm import tqdm

from nomad_sniper.data.option_bars import AtmSeries, resolve_atm_series
from nomad_sniper.labels.profitability_gate import (
    GateContext,
    ProfitabilityGate,
    make_gate,
)
from nomad_sniper.labels.triple_barrier import label_triple_barrier
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import decision_grid, ensure_ist

log = get_logger()


@dataclass
class DirectionalLabel:
    underlying_key: str
    decision_time: datetime
    direction: str            # up / down / none  (after the gate)
    is_move: int              # direction != none
    magnitude_atr: float      # MFE along the path, ATR units
    time_to_target: float     # minutes to barrier (H if timeout)
    mae_atr: float            # max adverse excursion, ATR units
    sample_weight: float = 1.0
    raw_candidate: str = "none"   # pre-gate outcome (diagnostics)


def _entry_price(bars: pd.DataFrame, entry_time: datetime) -> float | None:
    forward = bars[bars.index > entry_time]
    if forward.empty:
        return None
    return float(forward.iloc[0]["open"])


def label_grid_point(
    decision_time: datetime,
    bars_u: pd.DataFrame,
    atr_ref: float | None,
    gate: ProfitabilityGate,
    *,
    m: float = 1.0,
    horizon_minutes: int = 60,
    atm: AtmSeries | None = None,
    iv_estimate: float | None = None,
    cost_inr_per_unit: float = 4.0,
    underlying_key: str = "",
) -> DirectionalLabel | None:
    """Label one grid point. Returns None if there isn't enough forward path or no ATR."""
    decision_time = ensure_ist(decision_time)
    if atr_ref is None or atr_ref <= 0:
        return None
    entry = _entry_price(bars_u, decision_time)
    if entry is None:
        return None

    barrier = m * atr_ref
    lab = label_triple_barrier(
        bars_u,
        decision_time,
        "long",  # symmetric framing: target=up barrier, stop=down barrier
        stop_price=entry - barrier,
        target_price=entry + barrier,
        max_holding=timedelta(minutes=horizon_minutes),
    )
    if lab is None:
        return None

    # Map long-framed barrier result → direction + ATR-unit excursions.
    # mfe_r / mae_r are in barrier units (risk = m·ATR), so ×m gives ATR units.
    up_excursion_atr = lab.mfe_r * m
    down_excursion_atr = lab.mae_r * m
    if lab.exit_reason == "target":
        candidate, magnitude_atr, adverse_atr = "up", up_excursion_atr, down_excursion_atr
    elif lab.exit_reason == "stop":
        candidate, magnitude_atr, adverse_atr = "down", down_excursion_atr, up_excursion_atr
    else:  # timeout → no tradeable move
        candidate = "none"
        magnitude_atr = max(up_excursion_atr, down_excursion_atr)
        adverse_atr = min(up_excursion_atr, down_excursion_atr)

    time_to_target = (lab.exit_time - lab.entry_time).total_seconds() / 60.0
    if candidate == "none":
        time_to_target = float(horizon_minutes)

    # Option-economics gate.
    ctx = GateContext(
        candidate=candidate,
        entry_time=lab.entry_time,
        exit_time=lab.exit_time,
        entry_price=lab.entry_price,
        exit_price=lab.exit_price,
        mfe_atr=magnitude_atr,
        atr_ref=atr_ref,
        horizon_minutes=horizon_minutes,
        iv_estimate=iv_estimate,
        atm_ce=atm.ce if atm and atm.available else None,
        atm_pe=atm.pe if atm and atm.available else None,
        cost_inr_per_unit=cost_inr_per_unit,
    )
    final_direction = gate.apply(ctx)

    return DirectionalLabel(
        underlying_key=underlying_key,
        decision_time=decision_time,
        direction=final_direction,
        is_move=int(final_direction != "none"),
        magnitude_atr=float(magnitude_atr),
        time_to_target=float(time_to_target),
        mae_atr=float(adverse_atr),
        raw_candidate=candidate,
    )


def build_labels_for_grid(
    session_dates: list[date],
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    gate_mode: str = "atr_proxy",
    m_breakeven: float = 0.6,
    m: float = 1.0,
    horizon_minutes: int = 60,
    grid_minutes: int = 5,
    grid_start: str = "09:30",
    grid_end: str = "15:00",
    atm_by_underlying: dict[str, dict[date, AtmSeries]] | None = None,
    cost_inr_per_unit: float = 4.0,
) -> pd.DataFrame:
    """Label every grid point on every session × underlying. Mirrors `build_features_for_grid`
    so the two join on (underlying_key, decision_time)."""
    gate = make_gate(gate_mode, m_breakeven=m_breakeven, cost_inr_per_unit=cost_inr_per_unit)
    atm_by_underlying = atm_by_underlying or {}
    rows: list[dict] = []

    for underlying, bars in bars_by_underlying.items():
        for sdate in tqdm(session_dates, desc=f"labels:{underlying}", leave=False):
            atr_ref = atr_reference(bars, sdate)
            if atr_ref is None:
                continue
            atm = atm_by_underlying.get(underlying, {}).get(sdate)
            if atm is None:
                try:
                    atm = resolve_atm_series(underlying, sdate, bars)
                except Exception:  # noqa: BLE001
                    atm = None
            iv_est = _session_iv_estimate(atm)
            for dt in decision_grid(sdate, grid_minutes=grid_minutes, start=grid_start, end=grid_end):
                lab = label_grid_point(
                    dt, bars, atr_ref, gate,
                    m=m, horizon_minutes=horizon_minutes, atm=atm,
                    iv_estimate=iv_est, cost_inr_per_unit=cost_inr_per_unit,
                    underlying_key=underlying,
                )
                if lab is not None:
                    rows.append(asdict(lab))

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    counts = df["direction"].value_counts().to_dict()
    log.info(f"Built {len(df)} grid labels — direction counts: {counts}")
    return df


def _session_iv_estimate(atm: AtmSeries | None) -> float | None:
    """Best-effort ATM IV estimate for the BS-proxy gate; None if unavailable."""
    if atm is None or not atm.available or atm.ce is None:
        return None
    if "iv" in atm.ce.columns and atm.ce["iv"].notna().any():
        return float(atm.ce["iv"].dropna().iloc[0])
    return None
