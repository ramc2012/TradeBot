"""WS-0.1b — absolute per-index magnitude guard (market_data.index_band_guard).

Covers the cross-symbol contamination defence that replaced the poisonable
rolling median: an absolute band that is correct from tick one and cannot be
dragged by any run of contaminating prints, an optional tighter prior-session
band, widened sector coverage, and the candle-level O/H/L/C check used by the
backfill path.
"""
from __future__ import annotations

import pytest

from market_data import index_band_guard as g


@pytest.fixture(autouse=True)
def _clean_refs():
    g.clear_reference_closes()
    yield
    g.clear_reference_closes()


def test_absolute_band_rejects_gross_cross_symbol_contamination():
    sym = "NSE:NIFTY50-INDEX"
    assert g.passes(sym, 24075.0) is True          # real NIFTY
    assert g.passes(sym, 57826.0) is False         # BANKNIFTY frame (~2.4x)
    assert g.passes(sym, 906.0) is False           # REALTY frame (~0.04x)
    assert g.passes(sym, 22400.0) is True          # legit -7% move
    assert g.passes(sym, 25300.0) is True          # legit +5% move


def test_guard_cannot_be_poisoned_by_a_run_of_bad_prints():
    sym = "NSE:NIFTY50-INDEX"
    # A sustained burst of the SAME contaminating value: the old median would
    # have flipped once the burst won the window; the absolute band has no window.
    for _ in range(200):
        assert g.passes(sym, 57826.0) is False
    assert g.passes(sym, 24075.0) is True          # legit print unaffected


def test_prior_session_reference_tightens_the_band():
    sym = "NSE:NIFTY50-INDEX"
    # MIDCPNIFTY (~14.8k) contaminating NIFTY sits INSIDE the wide absolute band
    # but is -38% from the true level — only the reference band catches it.
    assert g.passes(sym, 14800.0) is True          # inside abs band, no ref yet
    g.set_reference_close(sym, 24100.0)
    assert g.passes(sym, 14800.0) is False         # -38% > 20% tol → rejected
    assert g.passes(sym, 39346.0) is False         # ENERGY level, +63% → rejected
    assert g.passes(sym, 24500.0) is True          # +1.7% → accepted
    assert g.passes(sym, 26000.0) is True          # +7.9% within 20% tol → accepted


def test_sector_indices_are_guarded_closing_the_coverage_gap():
    # The old gate only ran for the 5 DISPLAY_NAMES; sector indices were ingested
    # to market_ticks with NO guard at all.
    realty = "NSE:NIFTYREALTY-INDEX"
    assert g.is_guarded(realty) is True
    assert g.passes(realty, 908.0) is True         # real REALTY
    assert g.passes(realty, 57826.0) is False      # BANKNIFTY frame
    assert g.passes(realty, 26080.0) is False      # NIFTY-cluster frame (>2200)

    itx = "NSE:NIFTYIT-INDEX"
    assert g.is_guarded(itx) is True
    assert g.passes(itx, 28787.0) is True
    assert g.passes(itx, 906.0) is False


def test_non_index_symbols_are_never_guarded():
    for sym in ("NSE:NIFTY2520024000CE", "NSE:RELIANCE-EQ", "MCX:GOLD26AUGFUT"):
        assert g.is_guarded(sym) is False
        assert g.passes(sym, 5.0) is True          # tiny option premium
        assert g.passes(sym, 250000.0) is True     # large commodity notional


def test_check_ohlc_rejects_a_bar_with_one_contaminated_leg():
    sym = "NSE:NIFTY50-INDEX"
    # Clean bar passes.
    assert g.check_ohlc(sym, 24050.0, 24090.0, 24010.0, 24075.0) is True
    # Close is clean but the minute's HIGH was a contaminating 57.8k tick.
    assert g.check_ohlc(sym, 24050.0, 57826.0, 24010.0, 24075.0) is False
    # Contaminated LOW.
    assert g.check_ohlc(sym, 24050.0, 24090.0, 906.0, 24075.0) is False
    # Missing legs (None/0) don't force a rejection on their own.
    assert g.check_ohlc(sym, None, 24090.0, 0.0, 24075.0) is True
    # Non-guarded symbol always passes.
    assert g.check_ohlc("NSE:RELIANCE-EQ", 1.0, 999999.0, 0.1, 2.0) is True


def test_app_symbol_for_underlying_maps_db_names():
    assert g.app_symbol_for_underlying("NIFTY") == "NSE:NIFTY50-INDEX"
    assert g.app_symbol_for_underlying("banknifty") == "NSE:BANKNIFTY-INDEX"
    assert g.app_symbol_for_underlying("SENSEX") == "BSE:SENSEX-INDEX"
    assert g.app_symbol_for_underlying("GOLD") is None
    assert g.app_symbol_for_underlying("") is None


def test_all_five_tradeable_indices_separated_by_reference_band():
    # With references seeded, each index's true level passes and every OTHER
    # index's true level (all >=10% apart) is rejected — proving the guard would
    # have caught the recorded interleaving.
    levels = {
        "NSE:NIFTY50-INDEX": 24075.0,
        "NSE:BANKNIFTY-INDEX": 57582.0,
        "NSE:FINNIFTY-INDEX": 26570.0,
        "NSE:MIDCPNIFTY-INDEX": 14810.0,
        "BSE:SENSEX-INDEX": 77350.0,
    }
    for sym, lvl in levels.items():
        g.set_reference_close(sym, lvl)
    for sym, lvl in levels.items():
        assert g.passes(sym, lvl) is True
        for other, other_lvl in levels.items():
            if other == sym:
                continue
            # NIFTY↔FINNIFTY are only ~10% apart — the reference tol is 20%, so
            # that single near pair is (by design) routing's job, not the band's.
            if abs(other_lvl - lvl) / lvl <= g.REL_TOL:
                continue
            assert g.passes(sym, other_lvl) is False, f"{other_lvl} leaked into {sym}"
