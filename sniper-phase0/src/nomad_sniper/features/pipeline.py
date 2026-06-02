"""Pipeline orchestrator — stitches MP + HTF + Order Flow + Context into one snapshot.

Computes a single leak-free `atr_ref` per decision and threads it through every builder so all
distance features share the same normalizer (spec section 3).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

import pandas as pd
from tqdm import tqdm

from nomad_sniper.data.option_bars import ATMOptionSeries
from nomad_sniper.features.base import FeatureSnapshot, assert_no_leakage
from nomad_sniper.features.context import build_context_features
from nomad_sniper.features.htf import build_htf_features
from nomad_sniper.features.market_profile import build_mp_features
from nomad_sniper.features.option_structure import build_option_structure_features
from nomad_sniper.features.order_flow import build_of_features
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import decision_grid, ensure_ist

log = get_logger()


def build_all_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    atm_series: ATMOptionSeries | None = None,
) -> FeatureSnapshot:
    """Build the full normalized feature row (MP + HTF + OF + context) for one decision time."""
    decision_time = ensure_ist(decision_time)
    atr_ref = atr_reference(bars, decision_time.date())
    snap = FeatureSnapshot(decision_time=decision_time)
    snap = build_mp_features(decision_time, bars, atr_ref=atr_ref, snapshot=snap)
    snap = build_htf_features(decision_time, bars, atr_ref=atr_ref, snapshot=snap)
    snap = build_of_features(decision_time, bars, snapshot=snap)
    snap = build_option_structure_features(decision_time, bars, atm_series, snapshot=snap)
    snap = build_context_features(decision_time, bars, snapshot=snap)
    assert_no_leakage(snap, decision_time)
    return snap


def build_features_for_grid(
    grid_points: Iterable[tuple[str, datetime]],
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    atm_by_underlying: dict[tuple[str, date], ATMOptionSeries] | None = None,
    include_underlying: bool = True,
) -> pd.DataFrame:
    """Build a feature matrix over decision-grid points (no hand-crafted setups).

    Args:
        grid_points:        iterable of (underlying, decision_time_ist).
        bars_by_underlying: map underlying -> minute bars.

    Returns DataFrame keyed by row with an `underlying` column retained for joining/diagnostics;
    drop it before training (contract section 3).
    """
    rows, skipped = [], 0
    for underlying, dt in tqdm(list(grid_points), desc="features"):
        bars = bars_by_underlying.get(underlying)
        if bars is None:
            skipped += 1
            continue
        try:
            atm = None
            if atm_by_underlying is not None:
                atm = atm_by_underlying.get((underlying, dt.date())) or atm_by_underlying.get(underlying)
            snap = build_all_features(dt, bars, atm_series=atm)
            row = snap.to_row(strict=True)
            if include_underlying:
                row["underlying"] = underlying
            row["row_id"] = f"{underlying}|{pd.Timestamp(row['decision_time']).isoformat()}"
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            log.warning(f"feature build failed @ {underlying} {dt}: {e}")
            skipped += 1
    log.info(f"Built {len(rows)} grid feature rows, skipped {skipped}")
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("row_id")


def build_features_for_sessions(
    session_dates: Iterable[date],
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    underlyings: Iterable[str] | None = None,
    atm_by_underlying: dict[tuple[str, date], ATMOptionSeries] | None = None,
    grid_minutes: int = 5,
    include_underlying: bool = False,
) -> pd.DataFrame:
    """Build feature rows for every underlying over every session decision grid."""
    selected = list(underlyings or bars_by_underlying.keys())
    points = [
        (underlying, dt)
        for session_date in session_dates
        for underlying in selected
        for dt in decision_grid(session_date, grid_minutes=grid_minutes)
    ]
    return build_features_for_grid(
        points,
        bars_by_underlying,
        atm_by_underlying=atm_by_underlying,
        include_underlying=include_underlying,
    )


def build_features_for_trades(
    entries: Iterable[tuple[str, datetime, str]],
    bars_by_underlying: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Build feature rows for realized-trade overlays.

    The primary training path is the decision grid. This wrapper preserves the CLI/smoke-test
    overlay flow by evaluating the same leak-free feature builders at each realized entry time.

    Args:
        entries: iterable of (trade_id, decision_time_ist, underlying).
        bars_by_underlying: map underlying -> minute bars.

    Returns:
        DataFrame indexed by trade_id with an `underlying` diagnostic column.
    """
    rows, skipped = [], 0
    for trade_id, dt, underlying in tqdm(list(entries), desc="trade_features"):
        bars = bars_by_underlying.get(underlying)
        if bars is None:
            skipped += 1
            continue
        try:
            snap = build_all_features(dt, bars)
            row = snap.to_row(strict=True)
            row["trade_id"] = trade_id
            row["underlying"] = underlying
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            log.warning(f"trade feature build failed @ {underlying} {dt}: {e}")
            skipped += 1
    log.info(f"Built {len(rows)} trade-overlay feature rows, skipped {skipped}")
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("trade_id")
