"""Top-level feature builder. Iterates trades, computes snapshots, returns (features_df, availability_df)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from sniper_phase0.data.book import book_snapshot_at_or_before, load_book
from sniper_phase0.data.mp_state import MPState, compute_mp_state, compute_session_mp
from sniper_phase0.data.ticks import load_ticks
from sniper_phase0.features.base import FeatureSnapshot, assert_no_leakage
from sniper_phase0.features.context import add_context_features
from sniper_phase0.features.mp import add_mp_features, add_prior_session_mp_features
from sniper_phase0.features.of import add_of_features, slice_ticks_before
from sniper_phase0.utils.settings import Settings


def _instrument_root(symbol: str) -> str:
    if symbol.startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if symbol.startswith("NIFTY"):
        return "NIFTY"
    return symbol


def _prev_business_day(d: pd.Timestamp) -> pd.Timestamp:
    """Previous weekday — good enough for Phase 0 (does not honour NSE holidays)."""
    p = d - pd.Timedelta(days=1)
    while p.weekday() >= 5:
        p -= pd.Timedelta(days=1)
    return p


def build_features(trades: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict] = []
    avail_rows: list[dict] = []

    # Per-(instrument, date) cache of completed-session MPState.
    prev_mp_cache: dict[tuple[str, pd.Timestamp], MPState | None] = {}

    def get_prev_session_mp(instrument: str, day: pd.Timestamp) -> MPState | None:
        prev_day = _prev_business_day(day).normalize()
        key = (instrument, prev_day)
        if key in prev_mp_cache:
            return prev_mp_cache[key]
        prev_ticks = load_ticks(
            settings.paths.underlying_ticks,
            instrument,
            prev_day,
            prev_day + pd.Timedelta(days=1),
        )
        prev_mp_cache[key] = compute_session_mp(prev_ticks, prev_day) if not prev_ticks.empty else None
        return prev_mp_cache[key]

    by_day = trades.groupby(trades["entry_ts"].dt.date)
    for day, day_trades in by_day:
        day_ts = pd.Timestamp(day)
        for instrument in settings.instruments:
            ticks = load_ticks(
                settings.paths.underlying_ticks,
                instrument,
                day_ts,
                day_ts + pd.Timedelta(days=1),
            )
            book = load_book(
                settings.paths.book_snapshots,
                instrument,
                day_ts,
                day_ts + pd.Timedelta(days=1),
            )
            prev_mp = get_prev_session_mp(instrument, day_ts)

            mask = day_trades["symbol"].str.contains(instrument, na=False)
            for _, trade in day_trades[mask].iterrows():
                snap = FeatureSnapshot(
                    trade_id=int(trade["trade_id"]),
                    decision_ts=trade["entry_ts"],
                    instrument=instrument,
                    side=trade["side"],
                )

                spot = float(trade["entry_price"])
                mp = compute_mp_state(ticks, snap.decision_ts) if not ticks.empty else None
                add_mp_features(snap, mp, spot)
                add_prior_session_mp_features(snap, prev_mp, spot)

                ticks_5s = slice_ticks_before(ticks, snap.decision_ts, 5)
                ticks_30s = slice_ticks_before(ticks, snap.decision_ts, 30)
                ticks_300s = slice_ticks_before(ticks, snap.decision_ts, 300)
                book_snap = book_snapshot_at_or_before(book, snap.decision_ts) if not book.empty else None
                add_of_features(snap, ticks_5s, ticks_30s, ticks_300s, book_snap)

                add_context_features(
                    snap,
                    expiry_date=None,
                    prior_close=None,
                    today_open=None,
                    atr_14d=None,
                )

                row = snap.to_row()
                avail = {
                    "trade_id": snap.trade_id,
                    "decision_ts": snap.decision_ts,
                    **snap.data_available_at,
                }
                rows.append(row)
                avail_rows.append(avail)

    features_df = pd.DataFrame(rows)
    availability_df = pd.DataFrame(avail_rows)
    assert_no_leakage(features_df, availability_df)
    return features_df, availability_df
