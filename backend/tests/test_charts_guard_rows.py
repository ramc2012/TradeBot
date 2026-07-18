"""Chart serve-path guard — `_guard_rows` drops cross-symbol-contaminated bars.

Ground-truth fixture: the live NIFTY/30minute contamination (07-09/07-14/07-15)
where whole foreign frames (BANKNIFTY ~57.8k, REALTY ~928, half-value ~48545)
bled into an index's O/H/L/C. The band alone lets the 48545 close through (it
sits inside NIFTY's wide absolute band), so the guard must seed the ±20%
reference; the continuity net is the backstop when the reference is absent.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.routers import charts
from market_data import index_band_guard as g


@pytest.fixture(autouse=True)
def _clean_refs():
    g.clear_reference_closes()
    yield
    g.clear_reference_closes()


def _row(t, o, h, l, c):
    return {
        "time": datetime(2026, 7, 14, t, 0, tzinfo=timezone.utc),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 100.0,
    }


# The live-verified corrupt NIFTY/30minute rows + clean neighbours.
_CLEAN = [
    _row(0, 24020.0, 24050.0, 24000.0, 24040.0),
    _row(1, 24040.0, 24080.0, 24030.0, 24060.0),
    _row(2, 24060.0, 24090.0, 24050.0, 24075.0),
    _row(9, 24062.0, 24080.0, 24034.0, 24052.0),
]
_CORRUPT = [
    _row(3, 24027.0, 56871.0, 23906.0, 24024.0),   # high corrupt
    _row(4, 24004.0, 57832.0, 928.0, 24143.0),     # high + low (REALTY 928)
    _row(5, 24101.0, 57477.0, 24059.0, 48545.0),   # high + CLOSE (~2x, in-band)
    _row(6, 48574.0, 57488.0, 24068.0, 57488.0),   # 3 legs corrupt
]


def test_guard_drops_contaminated_index_bars_and_keeps_clean():
    g.set_reference_close("NSE:NIFTY50-INDEX", 24050.0)  # seed ±20% reference
    rows = _CLEAN[:3] + _CORRUPT + _CLEAN[3:]
    kept = charts._guard_rows("NSE:NIFTY50-INDEX", rows, underlying="NIFTY")

    kept_closes = {r["close"] for r in kept}
    assert kept_closes == {24040.0, 24060.0, 24075.0, 24052.0}
    # No survivor carries a foreign leg.
    for r in kept:
        for leg in ("open", "high", "low", "close"):
            assert 19000.0 < r[leg] < 29000.0


def test_continuity_net_catches_in_band_2x_without_reference():
    # No reference seeded ⇒ 48545 passes the absolute band (12k-50k). The
    # continuity net (median of surviving closes ~24k) must still drop it.
    rows = _CLEAN + [_row(5, 24101.0, 24120.0, 24059.0, 48545.0)]
    kept = charts._guard_rows("NSE:NIFTY50-INDEX", rows, underlying="NIFTY")
    assert all(r["close"] < 29000.0 for r in kept)
    assert len(kept) == len(_CLEAN)


def test_non_guarded_symbol_passes_through_untouched():
    # A stock (app_symbol None) must never be filtered — its price scale is
    # its own. Pass rows that would look "out of band" for an index.
    rows = [_row(0, 5000.0, 5100.0, 4900.0, 5050.0), _row(1, 100.0, 120.0, 90.0, 110.0)]
    kept = charts._guard_rows(None, rows, underlying="RELIANCE")
    assert kept == rows


def test_empty_and_all_corrupt():
    assert charts._guard_rows("NSE:NIFTY50-INDEX", [], underlying="NIFTY") == []
    g.set_reference_close("NSE:NIFTY50-INDEX", 24050.0)
    kept = charts._guard_rows("NSE:NIFTY50-INDEX", _CORRUPT, underlying="NIFTY")
    assert kept == []
