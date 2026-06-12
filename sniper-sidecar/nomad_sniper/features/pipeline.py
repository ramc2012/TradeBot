"""Pipeline orchestrator — stitch families A+B+C+D+E at a decision time, and over a grid.

Primary entry point is `build_features_for_grid` (contract §3): every grid point on every
session and every underlying produces one feature row. The trade-driven path is retired
(realized trades are a validation overlay only — contract §7).
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from tqdm import tqdm

from nomad_sniper.data.option_bars import AtmSeries, resolve_atm_series
from nomad_sniper.features.base import FeatureSnapshot, assert_no_leakage
from nomad_sniper.features.context import build_context_features
from nomad_sniper.features.htf_profile import build_htf_features
from nomad_sniper.features.market_profile import build_mp_features
from nomad_sniper.features.auction_state import build_auction_state_features
from nomad_sniper.features.vwap import build_vwap_features
from nomad_sniper.features.option_structure import build_option_features
from nomad_sniper.features.order_flow import build_of_features
from nomad_sniper.features.order_flow_live import build_order_flow_live_features
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import decision_grid, ensure_ist

log = get_logger()


def build_all_features(
    decision_time: datetime,
    bars_u: pd.DataFrame,
    atm_series: AtmSeries | None,
    atr_ref: float | None,
    *,
    india_vix: pd.Series | None = None,
    spot_bars: pd.DataFrame | None = None,
    of_snapshot: dict | None = None,
) -> FeatureSnapshot:
    """Build the full A+B+C+D+E snapshot for one decision time.

    Args:
        decision_time: IST-aware grid point.
        bars_u:        Underlying minute bars (≥ prior sessions through t).
        atm_series:    Resolved ATM CE/PE/straddle for the session (or None / unavailable).
        atr_ref:       Prior-close 14-session ATR (points) — the normalizer for family A.
        india_vix:     Optional IST-indexed VIX close series for family E.
    """
    decision_time = ensure_ist(decision_time)
    snap = FeatureSnapshot(decision_time=decision_time)
    snap = build_mp_features(decision_time, bars_u, atr_ref, snapshot=snap)          # A
    snap = build_htf_features(decision_time, bars_u, atr_ref, snapshot=snap)         # A2 (HTF wk/mo/q/y)
    snap = build_auction_state_features(decision_time, bars_u, atr_ref, snapshot=snap)  # A3 (auction state)
    _mig = {k: next((f.value for f in snap.features
                     if f.name == f"u_htf_{k}_value_migration_atr"), None)
            for k in ("week", "month", "quarter")}
    snap = build_vwap_features(decision_time, bars_u, atr_ref, snapshot=snap, htf_migration=_mig)  # A4 (VWAP)
    snap = build_of_features(decision_time, bars_u, snapshot=snap)                   # B (OHLCV-inferred)
    snap = build_order_flow_live_features(decision_time, of_snapshot=of_snapshot, snapshot=snap)  # B2 (live tick/depth)
    snap = build_option_features(decision_time, atm_series, bars_u, snapshot=snap,
                                 spot_bars=spot_bars, atr_ref=atr_ref)              # C+D (spot-ref)
    snap = build_context_features(decision_time, bars_u, snapshot=snap, india_vix=india_vix)  # E

    assert_no_leakage(snap, decision_time)
    return snap


def build_features_for_grid(
    session_dates: list[date],
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    atm_by_underlying: dict[str, dict[date, AtmSeries]] | None = None,
    grid_minutes: int = 5,
    grid_start: str = "09:30",
    grid_end: str = "15:00",
    include_underlying_column: bool = False,
    vix_by_underlying: dict[str, pd.Series] | None = None,
    spot_by_underlying: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Build a pooled feature matrix over the decision grid for every session × underlying.

    This is the primary entry point (replaces the trade-driven builder). One row per
    (underlying, session, grid_point). `underlying` is dropped from the emitted row unless
    `include_underlying_column=True` (ablation flag, contract §3) — the model learns
    structure, not instrument identity. A `decision_time` column is always kept for joining
    with labels and for walk-forward splits.

    Args:
        session_dates:        Sessions to iterate.
        bars_by_underlying:   Map underlying → IST-indexed minute bars.
        atm_by_underlying:    Optional map underlying → {session_date → AtmSeries}. When a
                              session is missing, ATM is resolved lazily (or degrades to null).
        include_underlying_column: Ablation only; keep `underlying` as a column/feature.
    """
    atm_by_underlying = atm_by_underlying or {}
    vix_by_underlying = vix_by_underlying or {}
    spot_by_underlying = spot_by_underlying or {}
    rows: list[dict] = []
    skipped = 0

    from nomad_sniper.utils.barindex import session_frames

    for underlying, bars in bars_by_underlying.items():
        vix = vix_by_underlying.get(underlying)
        spot = spot_by_underlying.get(underlying)  # index spot for the option family's ref
        _, day_frames = session_frames(bars)  # cached once per underlying
        for sdate in tqdm(session_dates, desc=f"grid:{underlying}", leave=False):
            atr_ref = atr_reference(bars, sdate)
            atm = _resolve_atm(underlying, sdate, bars, atm_by_underlying, spot)
            day = day_frames.get(sdate)
            if day is None or day.empty:
                continue
            for dt in decision_grid(sdate, grid_minutes=grid_minutes, start=grid_start, end=grid_end):
                # Only build if we have at least one underlying bar at/ before dt this session.
                if day[day.index <= dt].empty:
                    continue
                try:
                    snap = build_all_features(dt, bars, atm, atr_ref, india_vix=vix, spot_bars=spot)
                    row = snap.to_row(strict=True)
                    row["underlying_key"] = underlying  # bookkeeping (not a model feature)
                    if include_underlying_column:
                        row["underlying"] = underlying
                    rows.append(row)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"grid feature build failed {underlying} {dt}: {e}")
                    skipped += 1

    log.info(f"Built {len(rows)} grid feature rows, skipped {skipped}")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Stable, contract-ordered index by (underlying, decision_time).
    df = df.reset_index(drop=True)
    return df


def _resolve_atm(
    underlying: str,
    sdate: date,
    bars: pd.DataFrame,
    atm_by_underlying: dict[str, dict[date, AtmSeries]],
    spot_bars: pd.DataFrame | None = None,
) -> AtmSeries | None:
    cached = atm_by_underlying.get(underlying, {}).get(sdate)
    if cached is not None:
        return cached
    try:
        return resolve_atm_series(underlying, sdate, bars, spot_bars=spot_bars)
    except Exception as e:  # noqa: BLE001
        log.info(f"ATM resolve failed for {underlying} {sdate}: {e}; option families null.")
        return None
