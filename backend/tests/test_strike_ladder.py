"""Strike-ladder snapping, lossless strike tokens, and the fail-closed guard.

Regression cover for the ITC 288 PE incident (2026-08-04): a leg opened on a
strike the exchange does not list, unable to resolve a tradeable symbol, frozen
at 0.0 P&L with no reachable exit. See market_data/strike_ladder.py.
"""
from __future__ import annotations

import asyncio

import pytest

from market_data import strike_ladder as sl
from market_data import option_subscription_manager as osm
from paper_engine.strategy_agent import _contract_symbol


# ITC 2026-08-25 — the real 2.5-wide ladder from fo_contract_catalog.
ITC_LADDER = [
    {"strike": 280.0, "instrument_key": "NSE_FO|37277", "trading_symbol": "ITC 280 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 282.5, "instrument_key": "NSE_FO|117949", "trading_symbol": "ITC 282.5 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 285.0, "instrument_key": "NSE_FO|37281", "trading_symbol": "ITC 285 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 287.5, "instrument_key": "NSE_FO|117951", "trading_symbol": "ITC 287.5 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 290.0, "instrument_key": "NSE_FO|37283", "trading_symbol": "ITC 290 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 292.5, "instrument_key": "NSE_FO|117953", "trading_symbol": "ITC 292.5 PE 25 AUG 26", "lot_size": 1725},
    {"strike": 295.0, "instrument_key": "NSE_FO|37285", "trading_symbol": "ITC 295 PE 25 AUG 26", "lot_size": 1725},
]

ITC_STRIKES = [row["strike"] for row in ITC_LADDER]


@pytest.fixture(autouse=True)
def _clear_cache():
    sl.clear_ladder_cache()
    yield
    sl.clear_ladder_cache()


# ── format_strike: the lossless token ────────────────────────────────────────

def test_format_strike_keeps_half_rungs() -> None:
    # The whole incident in one assertion: int(round(287.5)) was 288.
    assert sl.format_strike(287.5) == "287.5"


def test_format_strike_renders_integers_without_decimal() -> None:
    assert sl.format_strike(288.0) == "288"
    assert sl.format_strike(76000) == "76000"


def test_format_strike_handles_finer_increments() -> None:
    # NSE:CANBK26JUL125.8PE is a real broker-fed Fyers symbol.
    assert sl.format_strike(125.8) == "125.8"


def test_format_strike_strips_float_noise() -> None:
    assert sl.format_strike(287.49999999999994) == "287.5"


def test_format_strike_never_rounds_half_to_even() -> None:
    # Python's banker's rounding made the old code lossy in BOTH directions:
    # round(287.5) == 288 but round(282.5) == 282.
    assert sl.format_strike(282.5) == "282.5"
    assert sl.format_strike(287.5) == "287.5"


# ── ladder_step: derived, never assumed ──────────────────────────────────────

def test_ladder_step_derives_itc_increment() -> None:
    assert sl.ladder_step(ITC_STRIKES) == 2.5


def test_ladder_step_uses_modal_gap_not_minimum() -> None:
    # Real ladders widen in the wings; the near-spot increment must win.
    widened = [100.0, 110.0, 120.0, 130.0, 140.0, 165.0, 190.0]
    assert sl.ladder_step(widened) == 10.0


def test_ladder_step_handles_index_increments() -> None:
    assert sl.ladder_step([76000.0, 76100.0, 76200.0, 76300.0]) == 100.0
    assert sl.ladder_step([24000.0, 24050.0, 24100.0, 24150.0]) == 50.0


def test_ladder_step_none_when_too_short() -> None:
    assert sl.ladder_step([287.5]) is None
    assert sl.ladder_step([]) is None


# ── snap_strike ──────────────────────────────────────────────────────────────

def test_snap_repairs_the_itc_rounding_artifact() -> None:
    assert sl.snap_strike(288.0, ITC_STRIKES) == (287.5, "snapped")


def test_snap_repairs_downward_artifact() -> None:
    # round(282.5) == 282 under banker's rounding.
    assert sl.snap_strike(282.0, ITC_STRIKES) == (282.5, "snapped")


def test_snap_leaves_a_listed_strike_alone() -> None:
    assert sl.snap_strike(285.0, ITC_STRIKES) == (285.0, "exact")
    assert sl.snap_strike(287.5, ITC_STRIKES) == (287.5, "exact")


def test_snap_refuses_beyond_half_a_step() -> None:
    # 286.9 is 0.6 from 287.5 — inside half a step (1.25), so it snaps...
    assert sl.snap_strike(286.9, ITC_STRIKES)[1] == "snapped"
    # ...but a strike from a different contract entirely does not.
    assert sl.snap_strike(5000.0, ITC_STRIKES) == (None, "off_ladder")


def test_snap_refuses_on_empty_ladder() -> None:
    assert sl.snap_strike(287.5, []) == (None, "no_ladder")


# ── resolve_contract: the fail-closed verdict ────────────────────────────────

def _stub_ladder(monkeypatch, rows) -> None:
    async def _fake(*, underlying, expiry, option_type):  # noqa: ARG001
        return rows

    monkeypatch.setattr(sl, "load_strike_ladder", _fake)


def test_resolve_snaps_and_carries_catalog_identity(monkeypatch) -> None:
    _stub_ladder(monkeypatch, ITC_LADDER)
    verdict = asyncio.run(
        sl.resolve_contract(underlying="ITC", expiry="2026-08-25", strike=288, option_type="PE")
    )
    assert verdict["ok"] is True
    assert verdict["strike"] == 287.5
    assert verdict["outcome"] == "snapped"
    # The catalog's identity must travel with the snapped rung — a leg with a
    # NULL instrument_key is exactly what could never be priced.
    assert verdict["instrument_key"] == "NSE_FO|117951"
    assert verdict["trading_symbol"] == "ITC 287.5 PE 25 AUG 26"
    assert verdict["lot_size"] == 1725


def test_resolve_rejects_off_ladder_strike(monkeypatch) -> None:
    _stub_ladder(monkeypatch, ITC_LADDER)
    verdict = asyncio.run(
        sl.resolve_contract(underlying="ITC", expiry="2026-08-25", strike=5000, option_type="PE")
    )
    assert verdict["ok"] is False
    assert verdict["reason"] == "strike_not_in_catalog"


def test_resolve_distinguishes_catalog_outage_from_off_ladder(monkeypatch) -> None:
    # An empty ladder is a contract-sync failure, not a thin strike list, and
    # must be diagnosable as such from the reason alone.
    _stub_ladder(monkeypatch, [])
    verdict = asyncio.run(
        sl.resolve_contract(underlying="ITC", expiry="2026-08-25", strike=287.5, option_type="PE")
    )
    assert verdict["ok"] is False
    assert verdict["reason"] == "catalog_empty"


def test_resolve_without_snap_is_exact_match_only(monkeypatch) -> None:
    _stub_ladder(monkeypatch, ITC_LADDER)
    verdict = asyncio.run(
        sl.resolve_contract(
            underlying="ITC", expiry="2026-08-25", strike=288, option_type="PE", snap=False
        )
    )
    assert verdict["ok"] is False


def test_resolve_rejects_unparseable_strike(monkeypatch) -> None:
    _stub_ladder(monkeypatch, ITC_LADDER)
    verdict = asyncio.run(
        sl.resolve_contract(underlying="ITC", expiry="2026-08-25", strike="n/a", option_type="PE")
    )
    assert verdict["ok"] is False
    assert verdict["reason"] == "unparseable_strike"


# ── downstream symbol construction ───────────────────────────────────────────

def test_contract_symbol_preserves_half_rung() -> None:
    # The book key that was written as OPT:ITC:2026-08-25:288:PE.
    assert (
        _contract_symbol("ITC", "2026-08-25", 287.5, "PE")
        == "OPT:ITC:2026-08-25:287.5:PE"
    )


def test_contract_symbol_unchanged_for_integer_strikes() -> None:
    # Every existing whole-number key must be byte-identical to before.
    assert (
        _contract_symbol("NIFTY", "2026-06-30", 24000.0, "CE")
        == "OPT:NIFTY:2026-06-30:24000:CE"
    )


def test_contract_symbol_roundtrips_through_the_recovery_parser() -> None:
    # strategy_agent's historical recovery re-parses the strike out of the
    # symbol. That is only safe because the token is now lossless.
    symbol = _contract_symbol("ITC", "2026-08-25", 287.5, "PE")
    assert float(symbol.split(":")[3]) == 287.5


def test_fyers_symbol_preserves_half_rung() -> None:
    # Was NSE:ITC26AUG288PE — a contract that does not exist, so the WS
    # subscription for the held leg never ticked.
    assert (
        osm._build_fyers_monthly_option_symbol("ITC", "2026-08-25", 287.5, "PE")
        == "NSE:ITC26AUG287.5PE"
    )


def test_fyers_symbol_matches_broker_observed_decimal_format() -> None:
    # Both captured from real Fyers-sourced data in this repo.
    assert (
        osm._build_fyers_monthly_option_symbol("ONGC", "2026-07-28", 247.5, "CE")
        == "NSE:ONGC26JUL247.5CE"
    )
    assert (
        osm._build_fyers_monthly_option_symbol("CANBK", "2026-07-28", 125.8, "PE")
        == "NSE:CANBK26JUL125.8PE"
    )


# ── the DB bind: what every mock-based test above cannot see ─────────────────
#
# 2026-08-06. Every resolve_* test stubs load_strike_ladder, so the one thing
# that actually broke went uncovered: the real function bound `expiry` as an
# ISO *string* against a DATE column. asyncpg types the parameter from the
# column and rejects a str outright ("'str' object has no attribute
# 'toordinal'"), so load_strike_ladder raised for EVERY series. Through the
# fail-closed guard in resolve_contract that refused every Strategy-1 entry —
# two sessions, 2026-08-04 and 08-05, with zero entries booked. The suite
# stayed green throughout.
#
# These tests assert on the bind parameters themselves, with no live DB.


class _CapturingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _CapturingSession:
    """Records the parameters handed to session.execute()."""

    def __init__(self, sink, rows):
        self._sink = sink
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt, params=None):
        self._sink.append(params or {})
        return _CapturingResult(self._rows)


def _capture_ladder_params(monkeypatch, rows=None):
    sink: list[dict] = []
    monkeypatch.setattr(
        sl, "AsyncSessionLocal", lambda: _CapturingSession(sink, rows if rows is not None else [])
    )
    return sink


def test_ladder_binds_expiry_as_a_date_not_a_string(monkeypatch) -> None:
    import datetime as _dt

    sink = _capture_ladder_params(monkeypatch)
    asyncio.run(sl.load_strike_ladder(underlying="SENSEX", expiry="2026-08-27", option_type="PE"))

    assert len(sink) == 1
    bound = sink[0]["expiry"]
    # A `str` here is the exact defect: asyncpg raises on it against a DATE
    # column, and the fail-closed caller turns that into a refused entry.
    assert isinstance(bound, _dt.date), f"expiry must bind as a date, got {type(bound).__name__}"
    assert bound == _dt.date(2026, 8, 27)


def test_ladder_binds_a_date_object_through_unchanged(monkeypatch) -> None:
    import datetime as _dt

    sink = _capture_ladder_params(monkeypatch)
    asyncio.run(
        sl.load_strike_ladder(underlying="NIFTY", expiry=_dt.date(2026, 8, 25), option_type="CE")
    )

    assert sink[0]["expiry"] == _dt.date(2026, 8, 25)
    assert isinstance(sink[0]["expiry"], _dt.date)


def test_resolve_reports_the_underlying_error_not_a_bare_empty_ladder(monkeypatch) -> None:
    """A failed catalog READ must not be reported as 'no such contract'."""

    async def _boom(**_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sl, "load_strike_ladder", _boom)
    verdict = asyncio.run(
        sl.resolve_contract(underlying="ITC", expiry="2026-08-25", strike=287.5, option_type="PE")
    )

    assert verdict["ok"] is False
    assert verdict["reason"] == "catalog_unavailable"
    # The cause must survive into the verdict — this is what the log prints,
    # and its absence is what made the outage read as a data gap for a day.
    assert "connection refused" in verdict["error"]


def test_refusal_log_distinguishes_read_failure_from_an_empty_catalog(monkeypatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        sl.logger, "error", lambda msg, *a, **k: messages.append(msg.format(*a) if a else msg)
    )

    sl.log_verdict(
        {"ok": False, "requested": 287.5, "outcome": "no_ladder",
         "reason": "catalog_unavailable", "ladder": [], "error": "DBAPIError: boom"},
        underlying="ITC", expiry="2026-08-25", option_type="PE", context="test",
    )
    assert "the catalog read FAILED" in messages[-1]
    assert "catalog has no rows" not in messages[-1]

    messages.clear()
    sl.log_verdict(
        {"ok": False, "requested": 287.5, "outcome": "no_ladder",
         "reason": "catalog_empty", "ladder": []},
        underlying="ITC", expiry="2026-08-25", option_type="PE", context="test",
    )
    assert "catalog has no rows" in messages[-1]
