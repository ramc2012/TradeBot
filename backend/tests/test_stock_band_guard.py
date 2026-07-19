"""F1 — NSE equity spot magnitude guard (market_data.stock_band_guard).

Covers the 2026-07-17 defect: whole, internally coherent frames belonging to
OTHER instruments delivered under an equity symbol label by the Fyers WS
(13847 / 949.30 / 254.50 arriving under NSE:BHEL-EQ at a true ~443 level).
Structural tick validation cannot see those; only a magnitude test against an
anchor external to the tape can.
"""
from __future__ import annotations

import pytest

from market_data import stock_band_guard as g


@pytest.fixture(autouse=True)
def _clean():
    g.clear_reference_closes()
    yield
    g.clear_reference_closes()


def test_symbol_helpers():
    assert g.is_equity_symbol("NSE:BHEL-EQ") is True
    assert g.is_equity_symbol("NSE:NIFTY50-INDEX") is False
    assert g.is_equity_symbol("MCX:GOLD25AUGFUT") is False
    assert g.underlying_for_symbol("NSE:BHEL-EQ") == "BHEL"
    assert g.app_symbol_for_underlying("reliance") == "NSE:RELIANCE-EQ"


def test_rejects_the_observed_bhel_contamination():
    g.set_reference_close("BHEL", 443.85)
    sym = "NSE:BHEL-EQ"
    assert g.passes(sym, 443.95) is True        # real BHEL
    assert g.passes(sym, 13847.0) is False      # ~M&M frame (31x)
    assert g.passes(sym, 949.30) is False       # ~HDFCLIFE frame (2.1x)
    assert g.passes(sym, 254.50) is False       # foreign frame (0.57x)
    assert g.passes(sym, 26556.70) is False     # observed absurd close


def test_rejects_the_observed_reliance_and_jiofin_contamination():
    g.set_reference_close("RELIANCE", 1322.0)
    g.set_reference_close("JIOFIN", 244.5)
    assert g.passes("NSE:RELIANCE-EQ", 11791.0) is False
    assert g.passes("NSE:RELIANCE-EQ", 162.96) is False
    assert g.passes("NSE:RELIANCE-EQ", 1328.8) is True
    assert g.passes("NSE:JIOFIN-EQ", 57582.25) is False
    assert g.passes("NSE:JIOFIN-EQ", 99.35) is False
    assert g.passes("NSE:JIOFIN-EQ", 246.98) is True


def test_legitimate_moves_are_never_rejected():
    g.set_reference_close("BHEL", 443.85)
    # A full 20% circuit move in either direction stays inside the band.
    assert g.passes("NSE:BHEL-EQ", 443.85 * 1.20) is True
    assert g.passes("NSE:BHEL-EQ", 443.85 * 0.80) is True


def test_anchor_cannot_be_dragged_by_a_run_of_bad_prints():
    """The anchor is external — it never comes from the tape being policed."""
    g.set_reference_close("BHEL", 443.85)
    for _ in range(500):
        assert g.passes("NSE:BHEL-EQ", 949.30) is False
    assert g.passes("NSE:BHEL-EQ", 443.95) is True


def test_unanchored_symbol_fails_open_and_is_not_guarded():
    # No external reference ⇒ we cannot judge; guard must not blind the name.
    assert g.is_guarded("NSE:NEWCO-EQ") is False
    assert g.passes("NSE:NEWCO-EQ", 999999.0) is True


def test_non_equity_symbols_are_untouched():
    assert g.passes("NSE:NIFTY50-INDEX", 57826.0) is True
    assert g.passes("MCX:GOLD25AUGFUT", 222400.0) is True


def test_nonpositive_price_rejected_when_anchored():
    g.set_reference_close("BHEL", 443.85)
    assert g.passes("NSE:BHEL-EQ", 0.0) is False
    assert g.passes("NSE:BHEL-EQ", -5.0) is False


def test_check_ohlc_rejects_a_bar_with_one_poisoned_leg():
    g.set_reference_close("BHEL", 443.85)
    assert g.check_ohlc("BHEL", 443.0, 446.15, 420.80, 445.0) is True
    # A single contaminating print inside the minute poisons only the high.
    assert g.check_ohlc("BHEL", 443.0, 13847.0, 420.80, 445.0) is False
    assert g.check_ohlc("BHEL", 443.0, 446.0, 26.5, 445.0) is False
    # Unanchored underlying ⇒ bar passes through.
    assert g.check_ohlc("NEWCO", 1.0, 999999.0, 1.0, 1.0) is True


def test_note_symbol_queues_pending_and_set_reference_clears_it():
    g.note_symbol("NSE:BHEL-EQ")
    assert "BHEL" in g._pending
    g.set_reference_close("BHEL", 443.85)
    assert "BHEL" not in g._pending
    # Already anchored ⇒ not re-queued.
    g.note_symbol("NSE:BHEL-EQ")
    assert "BHEL" not in g._pending


def test_reject_logging_is_rate_limited_but_counts_everything():
    loud_count = 0
    for _ in range(1000):
        loud, total = g.note_reject("NSE:BHEL-EQ")
        if loud:
            loud_count += 1
    assert loud_count == 1                       # one WARNING per minute per name
    assert g.reject_counts()["BHEL"] == 1000     # full volume still observable


def test_guard_never_fabricates_or_clamps():
    """The API is a predicate. There is no code path that returns a price."""
    g.set_reference_close("BHEL", 443.85)
    assert g.passes("NSE:BHEL-EQ", 13847.0) is False
    # Anchor is unchanged by the rejected print; nothing was written back.
    assert g._ref_close["BHEL"] == pytest.approx(443.85)


# ─── Anchor seeding (the external, non-tape reference) ────────────────────


class _FakeRow:
    def __init__(self, underlying: str, close: float) -> None:
        self.underlying = underlying
        self.close = close


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self._sink.append((str(statement), params))
        return _FakeResult(self._rows)


def _factory(rows, sink):
    def _make():
        return _FakeSession(rows, sink)

    return _make


@pytest.mark.asyncio
async def test_refresh_seeds_anchors_only_for_pending_names():
    sink: list = []
    g.note_symbol("NSE:BHEL-EQ")
    g.note_symbol("NSE:RELIANCE-EQ")
    seeded = await g.refresh_reference_closes(
        _factory([_FakeRow("BHEL", 443.85), _FakeRow("RELIANCE", 1322.0)], sink)
    )
    assert seeded == 2
    assert g.is_guarded("NSE:BHEL-EQ") is True
    assert g.passes("NSE:BHEL-EQ", 13847.0) is False

    sql, params = sink[0]
    # Bounded by name, by source (never the tape being policed) and by time.
    assert params["names"] == ["BHEL", "RELIANCE"]
    assert "live_tick" not in params["sources"]
    assert "source = ANY(:sources)" in sql
    assert "TIMESTAMPTZ '" in sql          # literal ⇒ plan-time chunk exclusion


@pytest.mark.asyncio
async def test_refresh_is_a_noop_with_nothing_pending():
    sink: list = []
    assert await g.refresh_reference_closes(_factory([], sink)) == 0
    assert sink == []


@pytest.mark.asyncio
async def test_unresolved_names_are_not_re_queried_every_cycle():
    sink: list = []
    g.note_symbol("NSE:NOHISTORY-EQ")
    assert await g.refresh_reference_closes(_factory([], sink)) == 0
    assert len(sink) == 1
    # Name found no anchor ⇒ backed off, not re-queued on the next tick.
    g.note_symbol("NSE:NOHISTORY-EQ")
    assert await g.refresh_reference_closes(_factory([], sink)) == 0
    assert len(sink) == 1


@pytest.mark.asyncio
async def test_refresh_failure_leaves_the_guard_fail_open_and_never_raises():
    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("pg down")

        async def __aexit__(self, *exc):
            return False

    g.note_symbol("NSE:BHEL-EQ")
    assert await g.refresh_reference_closes(lambda: _Boom()) == 0
    assert g.is_guarded("NSE:BHEL-EQ") is False
    assert g.passes("NSE:BHEL-EQ", 13847.0) is True   # cannot judge ⇒ must not blind
