"""Offline tests for the OI positioning assembly.

The two things worth locking down are the two that were actually wrong on
first run: the four-state conjunction's mapping, and what happens on days the
exchange publishes OI but does not trade.
"""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1]))

from features.m2_flow import classify_oi_state  # noqa: E402
from features.m_oi_positioning import build  # noqa: E402


def _mwpl(rows):
    return pd.DataFrame(rows, columns=["dt", "symbol", "total_oi", "mwpl"])


def _closes(rows):
    return pd.DataFrame(rows, columns=["symbol", "dt", "close"])


EMPTY_CHAIN = pd.DataFrame(columns=["symbol", "dt", "ce_oi", "pe_oi"])


# ── the conjunction itself ─────────────────────────────────────────────────

def test_the_four_states_map_the_standard_way_round():
    """Price up + OI up is fresh longs; price up + OI down is shorts leaving.
    Getting these two the wrong way round would invert the entire read."""
    assert classify_oi_state(+1, +1) == "long_buildup"
    assert classify_oi_state(-1, +1) == "short_covering"
    assert classify_oi_state(+1, -1) == "short_buildup"
    assert classify_oi_state(-1, -1) == "long_unwind"


def test_a_flat_leg_yields_no_state_rather_than_a_default():
    for oi, px in ((0, 1), (1, 0), (0, 0), (None, 1), (1, None)):
        assert classify_oi_state(oi, px) is None


# ── assembly ───────────────────────────────────────────────────────────────

def test_a_rising_price_on_rising_oi_is_marked_long_buildup():
    mwpl = _mwpl([
        (date(2026, 8, 25), "TCS", 1_000_000, 5_000_000),
        (date(2026, 8, 26), "TCS", 1_100_000, 5_000_000),
    ])
    closes = _closes([("TCS", date(2026, 8, 25), 100.0), ("TCS", date(2026, 8, 26), 104.0)])
    out = build(mwpl, EMPTY_CHAIN, closes).set_index("dt")
    row = out.loc[date(2026, 8, 26)]
    assert row["oi_state"] == "long_buildup"
    assert round(row["d_oi_pct"], 4) == 10.0
    assert round(row["d_price_pct"], 4) == 4.0
    assert round(row["mwpl_pct"], 2) == 22.0


def test_a_non_trading_day_does_not_blank_the_next_sessions_read():
    """THE BUG THIS LOCKS DOWN. fo_mwpl_snapshot publishes on days the equity
    market did not trade (2026-08-23 was a Sunday and carried 207 rows). Those
    rows joined into the grid with a real OI and no close, so shifting `close`
    over the raw grid put a NaN in front of the next real session and silently
    blanked its d_price_pct -- and with it its oi_state. Confirmed live: 24-Aug
    had 207 OI rows and ZERO positioning reads."""
    mwpl = _mwpl([
        (date(2026, 8, 21), "TCS", 1_000_000, 5_000_000),
        (date(2026, 8, 23), "TCS", 1_020_000, 5_000_000),   # Sunday — published, not traded
        (date(2026, 8, 24), "TCS", 1_100_000, 5_000_000),
    ])
    closes = _closes([("TCS", date(2026, 8, 21), 100.0), ("TCS", date(2026, 8, 24), 104.0)])
    out = build(mwpl, EMPTY_CHAIN, closes).set_index("dt")

    monday = out.loc[date(2026, 8, 24)]
    assert monday["oi_state"] == "long_buildup"
    assert round(monday["d_price_pct"], 4) == 4.0
    # The OI delta must span the same interval the price delta does — Friday to
    # Monday — not Sunday to Monday.
    assert round(monday["d_oi_pct"], 4) == 10.0

    sunday = out.loc[date(2026, 8, 23)]
    assert sunday["oi_state"] is None
    assert pd.isna(sunday["d_price_pct"])


def test_oi_deltas_are_never_taken_across_a_change_of_source():
    """mwpl OI and chain-summed OI are different measurements. Differencing one
    against the other would render a collection gap as a position unwind."""
    mwpl = _mwpl([(date(2026, 8, 26), "TCS", 1_100_000, 5_000_000)])
    chain = pd.DataFrame(
        [("TCS", date(2026, 8, 25), 400_000, 600_000)],
        columns=["symbol", "dt", "ce_oi", "pe_oi"],
    )
    closes = _closes([("TCS", date(2026, 8, 25), 100.0), ("TCS", date(2026, 8, 26), 104.0)])
    out = build(mwpl, chain, closes).set_index("dt")
    assert out.loc[date(2026, 8, 25), "oi_source"] == "chain_sum"
    assert out.loc[date(2026, 8, 26), "oi_source"] == "mwpl"
    assert pd.isna(out.loc[date(2026, 8, 26), "d_oi"])
    assert out.loc[date(2026, 8, 26), "oi_state"] is None


def test_pcr_comes_from_the_chain_split_and_survives_a_missing_side():
    chain = pd.DataFrame(
        [("TCS", date(2026, 8, 26), 400_000, 600_000),
         ("INFY", date(2026, 8, 26), 0, 600_000)],
        columns=["symbol", "dt", "ce_oi", "pe_oi"],
    )
    out = build(pd.DataFrame(columns=["dt", "symbol", "total_oi", "mwpl"]), chain,
                _closes([("TCS", date(2026, 8, 26), 100.0)])).set_index("symbol")
    assert round(out.loc["TCS", "oi_pcr"], 4) == 1.5
    # A zero call side is a divide-by-zero, not a PCR of infinity.
    assert pd.isna(out.loc["INFY", "oi_pcr"])


def test_returns_are_computed_over_trading_sessions_only():
    days = [date(2026, 8, d) for d in (17, 18, 19, 20, 21, 24)]
    closes = _closes([("TCS", d, 100.0 + i) for i, d in enumerate(days)])
    out = build(pd.DataFrame(columns=["dt", "symbol", "total_oi", "mwpl"]),
                EMPTY_CHAIN, closes).set_index("dt")
    # 5 sessions back from the 6th row is the 1st: 105 vs 100.
    assert round(out.loc[date(2026, 8, 24), "ret_5d"], 4) == 5.0


def test_an_entirely_empty_input_returns_an_empty_frame_not_a_crash():
    assert build(pd.DataFrame(columns=["dt", "symbol", "total_oi", "mwpl"]),
                 EMPTY_CHAIN, pd.DataFrame(columns=["symbol", "dt", "close"])).empty
