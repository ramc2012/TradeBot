"""Tests for the MACD pre-open watchlist: unbiased liquidity strike selection,
the frozen pre-open build, sticky strikes across a simulated restart, and the
history warm-up contract.

Owner spec 2026-07-20. These tests assert BEHAVIOUR CHANGES the owner asked
for, so several of them would fail against the legacy selector by design.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytest

from market_data import atm_watchlist as aw
from market_data import macd_watchlist as mw

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class FakeEntry:
    strike: float
    option_type: str
    oi: float = 0.0
    volume: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    instrument_key: Optional[str] = None
    trading_symbol: Optional[str] = None


@dataclass
class FakeChain:
    entries: list
    spot_price: float = 0.0


def _chain(spot: float, rows: dict[float, tuple[float, float]]) -> list[FakeEntry]:
    """rows: {strike: (oi, today_volume)} — same numbers on both sides."""
    out: list[FakeEntry] = []
    for strike, (oi, volume) in rows.items():
        for side in ("CE", "PE"):
            out.append(FakeEntry(strike=strike, option_type=side, oi=oi, volume=volume))
    return out


# ══════════════════════════════════════════════════════════════════════
# (3b) strike selection — liquidity decides, no ITM/OTM bias
# ══════════════════════════════════════════════════════════════════════
def test_window_spans_spot_on_both_sides() -> None:
    assert aw._spot_spanning_window([100, 200, 300, 400, 500], 250) == [200, 300, 400]
    # Spot exactly on a strike still looks BELOW it — the point of the change.
    assert aw._spot_spanning_window([100, 200, 300, 400], 300) == [200, 300, 400]


def test_liquidity_may_pick_an_ITM_strike_for_both_sides() -> None:
    """The legacy selector hard-anchored CE at/above spot and PE at/below.
    The owner removed that bias: liquidity decides and the winner MAY be ITM."""
    entries = _chain(250.0, {200: (5000, 0), 300: (100, 0), 400: (100, 0)})
    prior = {"CE": {200.0: 900.0, 300.0: 10.0}, "PE": {200.0: 900.0, 300.0: 10.0}}
    picks, diag = aw.select_liquid_strikes_unbiased(
        strikes=[200.0, 300.0, 400.0],
        spot_price=250.0,
        chain_entries=entries,
        min_oi=200.0,
        min_flow=25.0,
        max_rel_spread=0.05,
        prior_volume=prior,
    )
    # 200 is ITM for the CE and OTM for the PE — it wins BOTH on liquidity.
    assert picks == {"CE": 200.0, "PE": 200.0}
    assert all(item.flow_source in {"prior_session", "unmeasurable"} for item in diag["CE"])


def _legacy_pick(entries, strikes, spot):
    return aw._select_liquid_atm_strikes(
        strikes=strikes, spot_price=spot, chain_entries=entries
    )


def test_ce_picks_ITM_where_the_legacy_selector_provably_picks_OTM() -> None:
    """The discriminating case. Today's volume is 0 everywhere (the pre-open
    condition), so the LEGACY selector falls back to its oi/100 proxy, anchors
    CE at/above spot and keeps 300 (OTM) because the ITM neighbour's OI is not
    1.5x the anchor's. The new selector ranks on HISTORICAL volume and picks
    200 — an ITM call. Same inputs, different answer: that is the bias removal
    and the history-not-snapshot rule, proven together."""
    entries = _chain(250.0, {200: (5000, 0), 300: (9999, 0), 400: (9999, 0)})
    strikes = [200.0, 300.0, 400.0]
    assert _legacy_pick(entries, strikes, 250.0)["CE"] == 300.0     # OTM, today
    prior = {"CE": {200.0: 900.0, 300.0: 30.0, 400.0: 30.0}, "PE": {}}
    picks, _ = aw.select_liquid_strikes_unbiased(
        strikes=strikes, spot_price=250.0, chain_entries=entries,
        min_oi=200.0, min_flow=25.0, max_rel_spread=0.05, prior_volume=prior,
    )
    assert picks["CE"] == 200.0                                     # ITM, new


def test_pe_picks_ITM_where_the_legacy_selector_provably_picks_OTM() -> None:
    """Mirror of the above: an ITM PUT sits ABOVE spot. Legacy anchors PE
    at/below spot and keeps 200; the new selector picks 300."""
    entries = _chain(250.0, {200: (9999, 0), 300: (5000, 0), 400: (100, 0)})
    strikes = [200.0, 300.0, 400.0]
    assert _legacy_pick(entries, strikes, 250.0)["PE"] == 200.0     # OTM, today
    prior = {"CE": {}, "PE": {200.0: 30.0, 300.0: 900.0, 400.0: 5.0}}
    picks, _ = aw.select_liquid_strikes_unbiased(
        strikes=strikes, spot_price=250.0, chain_entries=entries,
        min_oi=200.0, min_flow=25.0, max_rel_spread=0.05, prior_volume=prior,
    )
    assert picks["PE"] == 300.0                                     # ITM, new


def test_todays_volume_cannot_influence_the_pick() -> None:
    """Liquidity is read from HISTORY, full stop. A contract with a huge live
    volume but no prior-session history is `unmeasurable` and excluded, even
    though the legacy selector would have crowned it."""
    entries = _chain(250.0, {200: (5000, 0), 300: (5000, 999999), 400: (5000, 0)})
    assert _legacy_pick(entries, [200.0, 300.0, 400.0], 250.0)["CE"] == 300.0
    picks, diag = aw.select_liquid_strikes_unbiased(
        strikes=[200.0, 300.0, 400.0], spot_price=250.0, chain_entries=entries,
        min_oi=200.0, min_flow=25.0, max_rel_spread=0.05,
        prior_volume={"CE": {200.0: 400.0}, "PE": {200.0: 400.0}},
    )
    assert picks["CE"] == 200.0
    scored = {item.strike: item for item in diag["CE"]}
    assert scored[300.0].flow == 0.0 and scored[300.0].flow_source == "unmeasurable"
    assert scored[300.0].liquid is False


def test_warmup_band_covers_the_whole_selection_window() -> None:
    ordered = [100.0, 200.0, 300.0, 400.0, 500.0]
    window = aw._spot_spanning_window(ordered, 250.0)
    assert mw._warmup_band_strikes(ordered, 250.0, 1) == window          # exactly
    assert mw._warmup_band_strikes(ordered, 250.0, 2) == ordered         # +1 each side


def test_strike_with_no_history_is_excluded_not_scored_zero() -> None:
    entries = _chain(250.0, {200: (9999, 0), 300: (9999, 0), 400: (9999, 0)})
    picks, diag = aw.select_liquid_strikes_unbiased(
        strikes=[200.0, 300.0, 400.0],
        spot_price=250.0,
        chain_entries=entries,
        min_oi=200.0,
        min_flow=25.0,
        max_rel_spread=0.05,
        prior_volume={"CE": {}, "PE": {}},
    )
    assert picks == {"CE": None, "PE": None}
    reasons = {item.reject_reason for item in diag["CE"]}
    assert reasons == {"no prior-session volume history (unmeasurable)"}


def test_oi_and_volume_are_ANDed_not_substituted() -> None:
    # 200: huge OI, never traded  → fails on volume (can't get out)
    # 300: heavily traded, no standing OI → fails on OI (nobody is there)
    # 400: both floors cleared → the only liquid contract
    entries = _chain(250.0, {200: (9999, 0), 300: (10, 0), 400: (500, 0)})
    prior = {
        "CE": {200.0: 0.0, 300.0: 9999.0, 400.0: 40.0},
        "PE": {200.0: 0.0, 300.0: 9999.0, 400.0: 40.0},
    }
    picks, _ = aw.select_liquid_strikes_unbiased(
        strikes=[200.0, 300.0, 400.0],
        spot_price=250.0,
        chain_entries=entries,
        min_oi=200.0,
        min_flow=25.0,
        max_rel_spread=0.05,
        prior_volume=prior,
    )
    assert picks == {"CE": 400.0, "PE": 400.0}


def test_spread_veto_is_skipped_when_the_book_is_untestable() -> None:
    entries = [FakeEntry(300.0, "CE", oi=5000, bid=None, ask=None)]
    _, diag = aw.select_liquid_strikes_unbiased(
        strikes=[300.0],
        spot_price=300.0,
        chain_entries=entries,
        min_oi=200.0,
        min_flow=25.0,
        max_rel_spread=0.05,
        prior_volume={"CE": {300.0: 100.0}, "PE": {}},
    )
    item = diag["CE"][0]
    assert item.spread_untestable is True
    assert item.spread_rel is None
    assert item.liquid is True


def test_wide_spread_vetoes_when_the_book_IS_testable() -> None:
    entries = [FakeEntry(300.0, "CE", oi=5000, bid=10.0, ask=20.0)]
    picks, _ = aw.select_liquid_strikes_unbiased(
        strikes=[300.0],
        spot_price=300.0,
        chain_entries=entries,
        min_oi=200.0,
        min_flow=25.0,
        max_rel_spread=0.05,
        prior_volume={"CE": {300.0: 100.0}, "PE": {}},
    )
    assert picks["CE"] is None


def test_resolve_row_strikes_flag_off_is_the_legacy_selector(monkeypatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", False, raising=False)
    entries = _chain(250.0, {200: (5000, 500), 300: (5000, 500), 400: (5000, 500)})
    picks, meta = aw.resolve_row_strikes(
        symbol="RELIANCE", kind="STOCK", strikes=[200.0, 300.0, 400.0],
        spot_price=250.0, chain_entries=entries,
    )
    assert meta["mode"] == "legacy_asymmetric"
    # Legacy bias: CE at/above spot, PE at/below.
    assert picks["CE"] == 300.0
    assert picks["PE"] == 200.0


def test_resolve_row_strikes_without_history_refuses_loudly(monkeypatch) -> None:
    """D1 guard: the liquidity rule is defined on HISTORY. Called without it,
    every candidate would be `unmeasurable` and the whole universe would be
    excluded — a wiring bug, not a market condition."""
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", True, raising=False)
    entries = _chain(250.0, {200: (5000, 500), 300: (5000, 500), 400: (5000, 500)})
    picks, meta = aw.resolve_row_strikes(
        symbol="RELIANCE", kind="STOCK", strikes=[200.0, 300.0, 400.0],
        spot_price=250.0, chain_entries=entries, prior_volume=None,
    )
    assert meta["reason"] == "missing_prior_volume"
    assert picks["CE"] is not None and picks["PE"] is not None


def test_no_liquid_strike_is_a_terminal_exclusion(monkeypatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", True, raising=False)
    entries = _chain(250.0, {200: (1, 0), 300: (1, 0), 400: (1, 0)})
    picks, meta = aw.resolve_row_strikes(
        symbol="THINCO", kind="STOCK", strikes=[200.0, 300.0, 400.0],
        spot_price=250.0, chain_entries=entries,
        prior_volume={"CE": {200.0: 1.0}, "PE": {200.0: 1.0}},
    )
    # NO arithmetic fallback — that is the behaviour change.
    assert picks == {"CE": None, "PE": None}
    assert set(meta["no_liquid_sides"]) == {"CE", "PE"}


def test_index_and_stock_do_not_share_liquidity_floors(monkeypatch) -> None:
    idx_oi, idx_flow, _ = aw.strike_liquidity_floors("INDEX")
    stk_oi, stk_flow, _ = aw.strike_liquidity_floors("STOCK")
    assert idx_oi > stk_oi and idx_flow > stk_flow


# ══════════════════════════════════════════════════════════════════════
# (4) warm-up requirement — DERIVED from the indicator params
# ══════════════════════════════════════════════════════════════════════
def test_warmup_requirement_is_derived_from_macd_params() -> None:
    req = mw.warmup_requirement()
    assert (req.macd_fast, req.macd_slow, req.macd_signal) == (12, 26, 9)
    assert req.interval == "30minute"
    # slow + signal, exactly what analytics.technicals refuses to go below.
    from analytics.technicals import MACD_MIN_BARS

    assert req.min_bars == req.macd_slow + req.macd_signal == MACD_MIN_BARS
    assert req.bars_per_session == 13          # NSE 09:15-15:30 at 30m
    assert req.target_bars >= req.min_bars
    assert 2.5 < req.min_sessions < 3.0        # ~2.7 sessions


def test_warm_up_marks_a_short_series_not_ready_and_never_pads(monkeypatch) -> None:
    calls: list[bool] = []

    class _Svc:
        async def load_candles(self, **kwargs):
            calls.append(bool(kwargs["allow_broker_refresh"]))
            return [{"close": 1.0}] * 4  # a genuinely thin, untraded strike

    monkeypatch.setattr(
        "market_data.option_history.option_history_service", _Svc(), raising=False
    )
    result = asyncio.run(
        mw.warm_up_strike(
            underlying="THINCO", expiry=date(2026, 7, 28), strike=100.0, option_type="CE"
        )
    )
    assert result.status == mw.WARMUP_NOT_READY
    assert result.bars == 4          # the ACTUAL count, not a padded one
    assert result.path == "insufficient"
    assert calls == [False, True]    # DB first, then the broker top-up


def test_warm_up_ready_from_db_only_makes_no_broker_call(monkeypatch) -> None:
    calls: list[bool] = []

    class _Svc:
        async def load_candles(self, **kwargs):
            calls.append(bool(kwargs["allow_broker_refresh"]))
            return [{"close": 1.0}] * 80

    monkeypatch.setattr(
        "market_data.option_history.option_history_service", _Svc(), raising=False
    )
    result = asyncio.run(
        mw.warm_up_strike(
            underlying="NIFTY", expiry=date(2026, 7, 28), strike=25000.0, option_type="CE"
        )
    )
    assert result.status == mw.WARMUP_READY
    assert result.path == "db_only"
    assert calls == [False]


# ══════════════════════════════════════════════════════════════════════
# (2) pre-open build + (3) sticky strikes
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture
def preopen_env(monkeypatch):
    """Stub every DB touchpoint so the build is deterministic and offline."""
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MACD_STICKY_STRIKES_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MACD_WARMUP_ENABLED", False, raising=False)

    persisted: list[dict] = []

    async def _persist(rows):
        persisted.extend(rows)
        return len(rows)

    async def _anchor(*, underlying, session_date=None, preopen_ltp=None):
        return mw.PriceAnchorResult(underlying, 250.0, mw.ANCHOR_PREV_CLOSE, None)

    async def _prior(**kwargs):
        return {
            "CE": {200.0: 900.0, 300.0: 40.0, 400.0: 40.0},
            "PE": {200.0: 900.0, 300.0: 40.0, 400.0: 40.0},
        }

    async def _chain_loader(symbol, kind, expiry):
        return FakeChain(entries=_chain(250.0, {200: (5000, 0), 300: (5000, 0), 400: (5000, 0)}))

    monkeypatch.setattr(mw, "persist_rows", _persist)
    monkeypatch.setattr(mw, "resolve_price_anchor", _anchor)
    monkeypatch.setattr(mw, "load_prior_volume", _prior)
    return persisted, _chain_loader


def test_preopen_build_freezes_one_row_per_side(monkeypatch, preopen_env) -> None:
    persisted, chain_loader = preopen_env

    async def _no_pins():
        return {}

    monkeypatch.setattr(mw, "load_open_position_pins", _no_pins)
    report = asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 21),
        )
    )
    assert report.built == 2
    assert {row["option_type"] for row in persisted} == {"CE", "PE"}
    # Liquidity, not geometry: 200 is ITM for the CE and still wins.
    assert {row["strike"] for row in persisted} == {200.0}
    # The anchor label is written on EVERY row — never silently mixed.
    assert {row["price_anchor"] for row in persisted} == {mw.ANCHOR_PREV_CLOSE}
    assert report.anchors == {mw.ANCHOR_PREV_CLOSE: 1}
    assert all(row["frozen_at"] is not None for row in persisted)


def test_sticky_pin_skips_selection_entirely(monkeypatch, preopen_env) -> None:
    persisted, chain_loader = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )

    async def _pins():
        return {("RELIANCE", "CE"): pin}

    monkeypatch.setattr(mw, "load_open_position_pins", _pins)
    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 21),
        )
    )
    rows = {row["option_type"]: row for row in persisted}
    # The pinned side keeps ITS strike/expiry however the liquidity picture moves.
    assert rows["CE"]["strike"] == 400.0
    assert rows["CE"]["expiry"] == date(2026, 7, 28)
    assert rows["CE"]["pinned_position_id"] == "pos-1"
    # The unpinned side is picked normally.
    assert rows["PE"]["strike"] == 200.0
    assert rows["PE"]["pinned_position_id"] is None


def test_no_anchor_price_excludes_the_instrument(monkeypatch, preopen_env) -> None:
    persisted, chain_loader = preopen_env

    async def _no_anchor(*, underlying, session_date=None, preopen_ltp=None):
        return mw.PriceAnchorResult(underlying, None, mw.ANCHOR_NONE, None)

    async def _no_pins():
        return {}

    monkeypatch.setattr(mw, "resolve_price_anchor", _no_anchor)
    monkeypatch.setattr(mw, "load_open_position_pins", _no_pins)
    report = asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 21),
        )
    )
    assert report.built == 0
    assert report.excluded_no_anchor == 2
    assert persisted == []   # we never anchor a ladder on a guessed price


def test_no_liquid_strike_row_is_written_and_excluded(monkeypatch, preopen_env) -> None:
    persisted, _ = preopen_env

    async def _no_pins():
        return {}

    async def _thin_chain(symbol, kind, expiry):
        return FakeChain(entries=_chain(250.0, {200: (1, 0), 300: (1, 0), 400: (1, 0)}))

    monkeypatch.setattr(mw, "load_open_position_pins", _no_pins)
    report = asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("THINCO", "STOCK")],
            chain_loader=_thin_chain,
            session_date=date(2026, 7, 21),
        )
    )
    assert report.excluded_no_liquid == 2
    assert {row["strike_status"] for row in persisted} == {mw.STATUS_NO_LIQUID}
    assert all(row["strike"] is None for row in persisted)


# ══════════════════════════════════════════════════════════════════════
# (3) restart safety
# ══════════════════════════════════════════════════════════════════════
def test_ladder_and_pins_reconstruct_after_a_simulated_restart(monkeypatch, preopen_env) -> None:
    """Both sources are DURABLE (Postgres + the paper JSON), so dropping all
    in-process state and reloading must reproduce the same strikes and the same
    repick_seq. Nothing here reads memory."""
    persisted, chain_loader = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )

    async def _pins():
        return {("RELIANCE", "CE"): pin}

    monkeypatch.setattr(mw, "load_open_position_pins", _pins)
    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 21),
        )
    )
    before = {(r["underlying"], r["option_type"]): (r["strike"], r["repick_seq"]) for r in persisted}

    # ── restart: every in-process structure is discarded; only the DB survives ──
    stored = {
        (r["underlying"], r["option_type"]): dict(r) for r in persisted
    }

    async def _reload(session_date=None):
        return stored

    monkeypatch.setattr(mw, "load_session_watchlist", _reload)
    reloaded = asyncio.run(mw.load_session_watchlist(date(2026, 7, 21)))
    after = {key: (row["strike"], row["repick_seq"]) for key, row in reloaded.items()}
    assert after == before
    assert reloaded[("RELIANCE", "CE")]["pinned_position_id"] == "pos-1"


def test_repick_after_close_uses_spot_at_that_time(monkeypatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MACD_WARMUP_ENABLED", False, raising=False)
    saved: list[dict] = []

    async def _persist(rows):
        saved.extend(rows)
        return len(rows)

    async def _existing(session_date=None):
        return {("RELIANCE", "CE"): {"repick_seq": 2, "frozen_at": None}}

    async def _prior(**kwargs):
        return {"CE": {500.0: 900.0, 600.0: 10.0}, "PE": {}}

    monkeypatch.setattr(mw, "persist_rows", _persist)
    monkeypatch.setattr(mw, "load_session_watchlist", _existing)
    monkeypatch.setattr(mw, "load_prior_volume", _prior)

    entries = _chain(550.0, {500: (5000, 0), 600: (5000, 0), 700: (5000, 0)})
    row = asyncio.run(
        mw.repick_after_close(
            underlying="RELIANCE", option_type="CE", kind="STOCK",
            spot_price=550.0, chain_entries=entries, expiry=date(2026, 7, 28),
            session_date=date(2026, 7, 21),
        )
    )
    assert row["strike"] == 500.0
    assert row["price_anchor"] == "live_spot_at_close"
    assert row["anchor_price"] == 550.0
    assert row["repick_seq"] == 3          # monotonic across the session
    assert row["pinned_position_id"] is None
    assert saved and saved[0] is row


# ══════════════════════════════════════════════════════════════════════
# misc contracts
# ══════════════════════════════════════════════════════════════════════
def test_sticky_holds_through_spot_drift(monkeypatch, preopen_env) -> None:
    """Spot moves two strikes away; the pinned side does not budge."""
    persisted, _ = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )

    async def _pins():
        return {("RELIANCE", "CE"): pin}

    async def _drifted_anchor(*, underlying, session_date=None, preopen_ltp=None):
        return mw.PriceAnchorResult(underlying, 205.0, mw.ANCHOR_PREV_CLOSE, None)

    async def _drifted_chain(symbol, kind, expiry):
        return FakeChain(entries=_chain(205.0, {200: (5000, 0), 300: (5000, 0), 400: (5000, 0)}))

    monkeypatch.setattr(mw, "load_open_position_pins", _pins)
    monkeypatch.setattr(mw, "resolve_price_anchor", _drifted_anchor)
    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")], chain_loader=_drifted_chain,
            session_date=date(2026, 7, 21),
        )
    )
    rows = {row["option_type"]: row for row in persisted}
    assert rows["CE"]["strike"] == 400.0          # unmoved despite the drift
    assert rows["PE"]["strike"] == 200.0          # unpinned side follows spot


# ══════════════════════════════════════════════════════════════════════
# _build_row integration: terminal exclusion + sticky release
# ══════════════════════════════════════════════════════════════════════
def _svc(monkeypatch, rows, pins=None):
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_PREOPEN_WATCHLIST_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MACD_STICKY_STRIKES_ENABLED", True, raising=False)
    svc = aw.ATMWatchlistService()

    async def _frozen():
        return rows

    async def _pins():
        return pins or {}

    monkeypatch.setattr(svc, "_frozen_session_rows", _frozen)
    monkeypatch.setattr(svc, "_open_position_pins", _pins)
    return svc


def test_no_liquid_frozen_row_excludes_the_instrument_instead_of_falling_back(monkeypatch) -> None:
    """The defect this fixes: strike=NULL used to leave the legacy arithmetic
    pick in place, i.e. the 'terminal exclusion' silently became a fallback."""
    rows = {
        ("THINCO", "CE"): {"strike": None, "strike_status": "no_liquid_strike", "expiry": None,
                           "notes": "thin"},
        ("THINCO", "PE"): {"strike": None, "strike_status": "no_liquid_strike", "expiry": None,
                           "notes": "thin"},
    }
    svc = _svc(monkeypatch, rows)
    meta = aw.UnderlyingMeta(
        symbol="THINCO", kind="STOCK", spot_instrument_key="NSE_EQ|X",
        underlying_key="NSE_EQ|X",
    )
    out = asyncio.run(
        svc._build_row(meta, "2026-07-28", date(2026, 7, 28), None, None)
    )
    assert out is None      # excluded for the session, no arithmetic fallback


def test_release_stale_pin_repicks_only_after_the_position_closes(monkeypatch) -> None:
    row = {"strike": 400.0, "strike_status": "ok", "pinned_position_id": "pos-1",
           "expiry": date(2026, 7, 28)}
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )
    meta = aw.UnderlyingMeta(
        symbol="RELIANCE", kind="STOCK", spot_instrument_key="NSE_EQ|X",
        underlying_key="NSE_EQ|X",
    )
    entries = _chain(550.0, {500: (5000, 0), 600: (5000, 0)})

    # ── still open → sticky holds, no re-pick ──
    svc = _svc(monkeypatch, {}, {("RELIANCE", "CE"): pin})
    held = asyncio.run(
        svc._release_stale_pin(meta, "CE", dict(row), entries, 550.0, date(2026, 7, 28))
    )
    assert held["strike"] == 400.0

    # ── closed → re-picked from the spot AT THAT TIME ──
    calls: list[dict] = []

    async def _repick(**kwargs):
        calls.append(kwargs)
        return {"strike": 500.0, "strike_status": "ok", "pinned_position_id": None}

    monkeypatch.setattr("market_data.macd_watchlist.repick_after_close", _repick)
    svc2 = _svc(monkeypatch, {}, {})
    released = asyncio.run(
        svc2._release_stale_pin(meta, "CE", dict(row), entries, 550.0, date(2026, 7, 28))
    )
    assert released["strike"] == 500.0
    assert released["pinned_position_id"] is None
    assert calls[0]["spot_price"] == 550.0

    # ── a FAILED open-position read must never release a pin ──
    async def _boom():
        raise RuntimeError("pg down")

    svc3 = _svc(monkeypatch, {}, {})
    monkeypatch.setattr(svc3, "_open_position_pins", _boom)
    kept = asyncio.run(
        svc3._release_stale_pin(meta, "CE", dict(row), entries, 550.0, date(2026, 7, 28))
    )
    assert kept["strike"] == 400.0


def test_preopen_window_defaults_to_after_the_call_auction(monkeypatch) -> None:
    base = datetime(2026, 7, 21, tzinfo=IST)
    assert mw.preopen_window_now(base.replace(hour=9, minute=0)) is False  # auction running
    assert mw.preopen_window_now(base.replace(hour=9, minute=6)) is True
    assert mw.preopen_window_now(base.replace(hour=9, minute=20)) is False


def test_prior_trading_sessions_skips_weekends(monkeypatch) -> None:
    from core.expiry_policy import expiry_policy

    monkeypatch.setattr(expiry_policy, "_is_trading_day", lambda d: d.weekday() < 5)
    days = mw.prior_trading_sessions(5, today=date(2026, 7, 20))  # a Monday
    assert days == [
        date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15),
        date(2026, 7, 16), date(2026, 7, 17),
    ]


def test_stock_roll_horizon_is_flag_gated_not_a_restart_side_effect(monkeypatch) -> None:
    """The owner's 3 → 5 trading-day stock roll must arrive WITH the pass, under
    EXPIRY_POLICY_ENABLED — not silently on the next restart. On 2026-07-21 the
    July monthly is exactly 5 trading days out, so an unconditional 5 would have
    rolled the whole stock watchlist to August while 75 open July stock
    positions were still live."""
    from agent.strategy_profiles import S1_CONTRACT_PROFILE
    from core.config import settings

    assert S1_CONTRACT_PROFILE.stock_rollover_td == 3          # literal stays legacy
    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", False, raising=False)
    assert aw._stock_roll_horizon(S1_CONTRACT_PROFILE.stock_rollover_td) == 3
    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS", 5, raising=False)
    assert aw._stock_roll_horizon(S1_CONTRACT_PROFILE.stock_rollover_td) == 5


# ══════════════════════════════════════════════════════════════════════
# Held-position roll split (owner rule 2026-07-21)
#   * un-held instruments roll at 5TD;
#   * a PINNED instrument keeps its own expiry across that roll;
#   * the pin RELEASES only after the position actually closes, and the
#     replacement lands on the NEW (post-roll) expiry;
#   * an unreadable refined paper book FAILS CLOSED.
# ══════════════════════════════════════════════════════════════════════
def _pin_env(monkeypatch, preopen_env, pin):
    async def _pins():
        return {(pin.underlying, pin.option_type): pin}

    monkeypatch.setattr(mw, "load_open_position_pins", _pins)
    return preopen_env


def test_sticky_pin_survives_the_5td_roll(monkeypatch, preopen_env) -> None:
    """On 2026-07-24 the stock universe has rolled to 2026-08-25, but the pinned
    CE is still open on 2026-07-28 — it keeps ITS contract, and the row says so
    (rolled=False + the HELD reason) instead of looking like a row the roll
    forgot. The un-held PE of the SAME underlying rolls: held-ness is per
    contract, not per symbol."""
    from core.expiry_policy import HELD_POSITION_ROLL_REASON

    persisted, chain_loader = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )
    _pin_env(monkeypatch, preopen_env, pin)

    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 24),
        )
    )
    rows = {row["option_type"]: row for row in persisted}
    assert rows["CE"]["expiry"] == date(2026, 7, 28)     # held contract kept
    assert rows["CE"]["expiry_rolled"] is False
    assert HELD_POSITION_ROLL_REASON in rows["CE"]["notes"]
    assert rows["CE"]["pinned_position_id"] == "pos-1"
    # …while the un-held side of the same name rolled to the next monthly.
    assert rows["PE"]["expiry"] == date(2026, 8, 25)
    assert rows["PE"]["expiry_rolled"] is True


def test_pin_is_not_released_on_the_forced_close_day_itself(monkeypatch, preopen_env) -> None:
    """2026-07-24 IS the 2TD boundary. The exit cascade closes the position that
    day, but the watchlist must NOT pre-emptively drop the pin — releasing a pin
    while the position is still open is exactly how a row gets orphaned."""
    from core.config import settings

    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", True, raising=False)
    persisted, chain_loader = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )
    _pin_env(monkeypatch, preopen_env, pin)

    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 24),
        )
    )
    row = {r["option_type"]: r for r in persisted}["CE"]
    assert row["pinned_position_id"] == "pos-1"
    assert row["strike"] == 400.0
    assert row["expiry"] == date(2026, 7, 28)
    # …but the row is LEGIBLE about what is about to happen to it.
    assert "forced_expiry_roll_2td_due" in row["notes"]


def test_pin_release_after_forced_closure_repicks_on_the_new_expiry(monkeypatch) -> None:
    """The release. `expiry=None` ⇒ resolve the CURRENT policy expiry, which on
    2026-07-24 is the post-roll 2026-08-25. Same PK row, pin cleared — the row
    is overwritten in place, never orphaned or duplicated."""
    from core.config import settings

    monkeypatch.setattr(settings, "MACD_LIQUID_STRIKE_SELECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "MACD_WARMUP_ENABLED", False, raising=False)
    saved: list[dict] = []

    async def _persist(rows):
        saved.extend(rows)
        return len(rows)

    async def _existing(session_date=None):
        return {("RELIANCE", "CE"): {"repick_seq": 0, "frozen_at": None,
                                     "pinned_position_id": "pos-1"}}

    async def _prior(**kwargs):
        return {"CE": {500.0: 900.0, 600.0: 10.0}, "PE": {}}

    monkeypatch.setattr(mw, "persist_rows", _persist)
    monkeypatch.setattr(mw, "load_session_watchlist", _existing)
    monkeypatch.setattr(mw, "load_prior_volume", _prior)

    entries = _chain(550.0, {500: (5000, 0), 600: (5000, 0), 700: (5000, 0)})
    row = asyncio.run(
        mw.repick_after_close(
            underlying="RELIANCE", option_type="CE", kind="STOCK",
            spot_price=550.0, chain_entries=entries,
            session_date=date(2026, 7, 24),   # expiry deliberately NOT passed
        )
    )
    assert row["expiry"] == date(2026, 8, 25)   # the NEW expiry, post-roll
    assert row["pinned_position_id"] is None    # the release itself
    assert row["underlying"] == "RELIANCE" and row["option_type"] == "CE"
    assert len(saved) == 1                      # ONE row, upserted in place


def test_unreadable_refined_paper_book_fails_closed(monkeypatch, tmp_path) -> None:
    """A book we could not parse is NOT an empty book. Treating it as empty
    would silently drop every refined pin and drift the ladder off live
    positions, so the read RAISES and the caller leaves the ladder alone."""
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "paper_positions.json").write_text("{ this is not json")
    monkeypatch.setattr("macd_refined.config.RUNTIME_ROOT", str(tmp_path), raising=False)

    with pytest.raises(mw.RefinedPaperBookUnreadable):
        mw._load_refined_paper_pins()


def test_missing_refined_paper_book_is_genuinely_empty(monkeypatch, tmp_path) -> None:
    """A book that was never written is a different fact from one we failed to
    read — only this one is safe to treat as no pins."""
    monkeypatch.setattr("macd_refined.config.RUNTIME_ROOT", str(tmp_path), raising=False)
    assert mw._load_refined_paper_pins() == []


def test_refined_paper_pins_read_the_key_the_book_actually_writes(monkeypatch, tmp_path) -> None:
    """macd_refined.paper writes `open_positions`; reading only
    `positions`/`open` silently returned [] for the real book."""
    import json

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "paper_positions.json").write_text(
        json.dumps(
            {
                "open_positions": [
                    {
                        "position_id": "ref-1", "status": "open", "underlying": "NBCC",
                        "option_type": "PE", "strike": 105.0, "expiry": "2026-07-28",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr("macd_refined.config.RUNTIME_ROOT", str(tmp_path), raising=False)
    pins = mw._load_refined_paper_pins()
    assert [(p.underlying, p.option_type, p.strike, p.expiry) for p in pins] == [
        ("NBCC", "PE", 105.0, date(2026, 7, 28))
    ]
    assert pins[0].source == "macd_refined_paper"


# ══════════════════════════════════════════════════════════════════════
# Adversarial verification pass (2026-07-21) — two defects the build's own
# tests could not see, because each test exercised a path production never
# takes.
# ══════════════════════════════════════════════════════════════════════
def test_forced_close_note_is_gated_by_the_same_flag_as_the_closure(
    monkeypatch, preopen_env
) -> None:
    """DISCRIMINATING. The pinned row's note claims "the exit cascade
    force-closes it today". That is only true when
    EXPIRY_POLICY_FORCED_CLOSE_ENABLED is up. The build called
    ``must_force_close`` directly — which reads only the *horizon* setting, not
    the enable flag — so with the feature OFF the row was still annotated with a
    closure that would never happen. Asserted OFF first: this fails against the
    pre-fix code."""
    from core.config import settings

    persisted, chain_loader = preopen_env
    pin = mw.PositionPin(
        underlying="RELIANCE", option_type="CE", strike=400.0,
        expiry=date(2026, 7, 28), position_id="pos-1", source="agent_positions",
    )
    _pin_env(monkeypatch, preopen_env, pin)

    # ── flags DOWN: no claim of a closure that cannot happen ──
    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", False, raising=False)
    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 24),      # the 2TD boundary itself
        )
    )
    row = {r["option_type"]: r for r in persisted}["CE"]
    assert "forced_expiry_roll_2td_due" not in row["notes"]
    assert row["pinned_position_id"] == "pos-1"  # the pin itself is untouched

    # ── flags UP: the row says what is about to happen ──
    persisted.clear()
    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", True, raising=False)
    asyncio.run(
        mw.build_preopen_watchlist(
            universe=[("RELIANCE", "STOCK")],
            chain_loader=chain_loader,
            session_date=date(2026, 7, 24),
        )
    )
    row = {r["option_type"]: r for r in persisted}["CE"]
    assert "forced_expiry_roll_2td_due" in row["notes"]


def test_release_repicks_on_the_cycle_expiry_not_the_held_one(monkeypatch) -> None:
    """DISCRIMINATING, through the REAL production caller.

    ``_build_row`` overwrites its cycle expiry with the FROZEN row's expiry, and
    the frozen row of a pinned position is the pre-roll month. It then handed
    that overwritten value to ``_release_stale_pin`` → ``repick_after_close``,
    so a released pin re-picked straight back onto 2026-07-28 — the contract the
    position had just been closed out of — instead of the post-roll 2026-08-25.
    The build's own release test never saw this: it called
    ``repick_after_close`` directly with the expiry omitted, a path the only
    production caller never takes.
    """
    from core.config import settings

    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    rows = {
        # frozen CE still pinned to the HELD, pre-roll July contract
        ("RELIANCE", "CE"): {"strike": 400.0, "strike_status": "ok",
                             "pinned_position_id": "pos-1", "expiry": date(2026, 7, 28)},
    }
    svc = _svc(monkeypatch, rows, {})       # no open pins ⇒ the position CLOSED

    class _Adapter:
        async def get_option_chain(self, key, expiry):
            return FakeChain(
                entries=_chain(550.0, {500: (5000, 0), 600: (5000, 0)}),
                spot_price=550.0,
            )

    async def _contracts(meta, expiry, adapter):
        return []

    async def _payload(*args, **kwargs):
        return None

    monkeypatch.setattr(svc, "_get_contracts_for_expiry", _contracts)
    monkeypatch.setattr(svc, "_build_option_payload", _payload)

    calls: list[dict] = []

    async def _repick(**kwargs):
        calls.append(kwargs)
        return {"strike": 500.0, "strike_status": "ok", "pinned_position_id": None,
                "expiry": kwargs.get("expiry")}

    monkeypatch.setattr("market_data.macd_watchlist.repick_after_close", _repick)

    meta = aw.UnderlyingMeta(
        symbol="RELIANCE", kind="STOCK", spot_instrument_key="NSE_EQ|X",
        underlying_key="NSE_EQ|X",
    )
    # The cycle resolves the post-roll August contract; the frozen row is July.
    asyncio.run(
        svc._build_row(meta, "2026-08-25", date(2026, 8, 25), _Adapter(), None)
    )
    assert calls, "the release never ran — the test would prove nothing"
    assert calls[0]["expiry"] == date(2026, 8, 25)   # NOT the held 2026-07-28
