"""Directional lane expiry discipline (2026-07-27).

THE DEFECT, verified live on the morning of 2026-07-27: ``directional_paper_
positions`` held six open rows, five of them on the 2026-07-28 expiry, four
of which had not been touched since 2026-07-21/22.  Three compounding causes:

  1. the lane never imported ``core.expiry_policy.forced_close_check`` — it
     was the ONLY option-holding lane exempt from the owner's 2-trading-day
     compulsory-closure rule that every MACD surface obeys;
  2. its only expiry exit was ``dte <= expiry_guard_days`` with a guard of
     0.8 CALENDAR days, so it could not fire until expiry day itself;
  3. that exit lives inside ``sync_snapshot``'s per-underlying loop, and the
     runner cycles 40-53 of ~217 names a session — on 2026-07-24 only BEL
     and LT of the six held names were cycled at all.  A held name that stops
     being selected is never re-examined and rides through expiry.

Plus: ``dte`` was computed off ``datetime.now(timezone.utc).date()``, which
between 00:00 and 05:30 IST is YESTERDAY, biasing the close LATE.

Every test below fails against the pre-fix code (``expiry_exit_decision`` /
``sweep_expiry_closures`` did not exist; the 2TD gate was never consulted;
the sync pass used the UTC date).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.config import settings
from directional_options.paper import (
    DirectionalOptionsPaperStore,
    _ist_today,
    expiry_exit_decision,
)


# ── harness ──────────────────────────────────────────────────────────────────


def _isolate(store: DirectionalOptionsPaperStore, monkeypatch, rows: list[dict]):
    state: dict[str, list] = {
        "open_positions": [dict(r) for r in rows],
        "closed_positions": [],
    }
    journal: list[dict] = []

    async def _load_positions() -> dict[str, list]:
        return {
            "open_positions": [dict(r) for r in state["open_positions"]],
            "closed_positions": [dict(r) for r in state["closed_positions"]],
        }

    async def _save_positions(payload: dict) -> None:
        state["open_positions"] = [dict(r) for r in payload.get("open_positions", [])]
        state["closed_positions"] = [dict(r) for r in payload.get("closed_positions", [])]

    async def _append_journal(payload: dict) -> None:
        journal.append(dict(payload))

    async def _noop(*_a, **_k):
        return None

    def _no_db():
        raise RuntimeError("DB disabled in test")

    monkeypatch.setattr(store, "_load_positions", _load_positions)
    monkeypatch.setattr(store, "_save_positions", _save_positions)
    monkeypatch.setattr(store, "_append_journal", _append_journal)
    monkeypatch.setattr("directional_options.paper.AsyncSessionLocal", _no_db)
    monkeypatch.setattr("directional_options.paper.paper_trade_recorder.record_event", _noop)
    return state, journal


def _row(
    *,
    position_id: str,
    underlying: str,
    expiry: str,
    entry_premium: float = 28.5,
    latest_premium: float | None = 36.0,
    quantity_units: int = 425,
    option_type: str = "PE",
    updated_at: str | None = None,
    mark_time: str | None = None,
) -> dict:
    stamp = updated_at or (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    return {
        "position_id": position_id,
        "status": "open",
        "underlying": underlying,
        "positional": True,
        "direction": option_type,
        "option_type": option_type,
        "trading_symbol": f"{underlying}{expiry.replace('-', '')}{option_type}",
        "instrument_key": f"NSE|{underlying}|{expiry}|{option_type}",
        "expiry": expiry,
        "strike": 1440.0,
        "quantity_lots": 1,
        "quantity_units": quantity_units,
        "entry_premium": entry_premium,
        "latest_premium": latest_premium,
        "entry_spot": 1500.0,
        "latest_spot": 1490.0,
        "opened_at": stamp,
        "updated_at": stamp,
        "mark_time": mark_time or stamp,
        "price_source": "option_premium_candles",
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    }


@pytest.fixture()
def policy_flags_on(monkeypatch):
    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", True, raising=False)


# ── 1. the shared 2TD gate is now consulted at all ───────────────────────────


def test_shared_2td_gate_fires_for_a_stock_two_trading_days_out(policy_flags_on) -> None:
    """The exact boundary the MACD lanes obey: the 2026-07-28 (Tue) expiry is
    compulsorily closed from Friday 2026-07-24, four calendar days out.

    Pre-fix the lane's only rule was ``dte <= 0.8`` — on 07-24 dte=4, so
    nothing fired.  This is the whole point of wiring the shared gate.
    """
    reason, detail = expiry_exit_decision(
        _row(position_id="p", underlying="CIPLA", expiry="2026-07-28"),
        today=date(2026, 7, 24),
        expiry_guard_days=0.8,
    )
    assert reason == "forced_expiry_roll_2td"
    assert detail["forced_close"]["trading_days_to_expiry"] == 2
    assert detail["forced_close"]["boundary_trading_days"] == 2


def test_shared_2td_gate_still_fires_when_already_late(policy_flags_on) -> None:
    """The boundary is a CEILING, not a single-day trigger — a position that
    slipped past 07-24 must still close on 07-27 and on expiry day itself."""
    for today in (date(2026, 7, 27), date(2026, 7, 28)):
        reason, _ = expiry_exit_decision(
            _row(position_id="p", underlying="CIPLA", expiry="2026-07-28"),
            today=today,
            expiry_guard_days=0.8,
        )
        assert reason == "forced_expiry_roll_2td", today


def test_far_expiry_is_left_alone(policy_flags_on) -> None:
    """LT 2026-08-25 is 21 trading days out — nothing may touch it. Guards
    against a sweep that just closes the book."""
    reason, _ = expiry_exit_decision(
        _row(position_id="p", underlying="LT", expiry="2026-08-25"),
        today=date(2026, 7, 27),
        expiry_guard_days=0.8,
    )
    assert reason is None


def test_index_falls_back_to_the_calendar_backstop(policy_flags_on) -> None:
    """Indices are CASH settled, so the shared gate is disabled for them
    (INDEX boundary = 0). The lane's own calendar guard must therefore stay,
    or an index position could ride into expiry with the gate 'on'."""
    row = _row(position_id="p", underlying="NIFTY", expiry="2026-07-28")
    assert expiry_exit_decision(row, today=date(2026, 7, 27), expiry_guard_days=0.8)[0] is None
    reason, detail = expiry_exit_decision(row, today=date(2026, 7, 28), expiry_guard_days=0.8)
    assert reason == "expiry_roll"
    assert detail["forced_close"]["must_close"] is False
    assert detail["calendar_dte"] == 0


def test_calendar_backstop_survives_the_policy_flags_being_off(monkeypatch) -> None:
    """With EXPIRY_POLICY_FORCED_CLOSE_ENABLED down the shared gate returns
    None. A position must STILL never reach expiry."""
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", False, raising=False)
    row = _row(position_id="p", underlying="CIPLA", expiry="2026-07-28")
    reason, detail = expiry_exit_decision(row, today=date(2026, 7, 28), expiry_guard_days=0.8)
    assert reason == "expiry_roll"
    assert "forced_close" not in detail


# ── 2. the UTC/IST date bug ──────────────────────────────────────────────────


def test_days_to_expiry_uses_the_ist_date_not_utc() -> None:
    """01:00 IST on 2026-07-28 is 19:30 UTC on 07-27. The old
    ``datetime.now(timezone.utc).date()`` read 07-27 there, making dte one too
    HIGH and skipping the close for that cycle."""
    at_0100_ist = datetime(2026, 7, 27, 19, 30, tzinfo=timezone.utc)
    assert at_0100_ist.date() == date(2026, 7, 27)          # the old, wrong answer
    assert _ist_today(at_0100_ist) == date(2026, 7, 28)     # the exchange date


# ── 3. the global sweep — the actual guarantee ───────────────────────────────


@pytest.mark.asyncio
async def test_sweep_closes_a_position_whose_underlying_was_never_cycled(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """THE regression. CIPLA was not in any scan batch on 2026-07-24, so the
    per-underlying pass in sync_snapshot never ran for it. The sweep reads the
    whole book and closes it anyway — pre-fix there was no such code path and
    ``expiry_roll`` appeared exactly once in the entire codebase."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, journal = _isolate(
        store,
        monkeypatch,
        [
            _row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28"),
            _row(position_id="lt", underlying="LT", expiry="2026-08-25", entry_premium=125.5),
        ],
    )

    report = await store.sweep_expiry_closures(today=date(2026, 7, 27))

    assert report["closed"] == 1
    assert report["open_evaluated"] == 2
    assert [r["position_id"] for r in state["open_positions"]] == ["lt"]
    closed = state["closed_positions"][0]
    assert closed["position_id"] == "cipla"
    assert closed["close_reason"] == "forced_expiry_roll_2td"
    assert closed["status"] == "closed"
    assert closed["expiry_sweep"] is True
    assert any(rec.get("event") == "expiry_sweep_close" for rec in journal)


@pytest.mark.asyncio
async def test_sweep_labels_a_stale_mark_exit_and_prices_it_at_the_last_observation(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """CIPLA 1440 PE's last premium bar is 2026-07-21 — the strike fell off the
    ATM collection set as spot drifted. With no live quote the exit is booked
    at the LAST OBSERVED premium and labelled, never silently passed off as a
    current fill."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    observed_at = "2026-07-21T05:51:00+00:00"
    state, _ = _isolate(
        store,
        monkeypatch,
        [
            _row(
                position_id="cipla",
                underlying="CIPLA",
                expiry="2026-07-28",
                entry_premium=28.5,
                latest_premium=36.0,
                updated_at=observed_at,
            )
        ],
    )

    async def _no_quote(_row_payload):
        return None

    report = await store.sweep_expiry_closures(
        today=date(2026, 7, 27),
        mark_resolver=_no_quote,
        now="2026-07-27T04:00:00+00:00",
    )

    closed = state["closed_positions"][0]
    assert closed["exit_premium"] == 36.0          # the last OBSERVED premium
    assert closed["exit_price_quality"] == "stale_mark"
    assert closed["stale_mark_exit"] is True
    assert closed["exit_mark_observed_at"] == observed_at
    assert closed["exit_mark_age_seconds"] > 5 * 24 * 3600 * 0.9
    assert closed["exit_price_source"].startswith("carried:")
    assert report["closures"][0]["exit_price_quality"] == "stale_mark"


@pytest.mark.asyncio
async def test_sweep_prefers_a_real_refetched_quote_over_the_carried_mark(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, _ = _isolate(
        store,
        monkeypatch,
        [_row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28")],
    )
    seen: list[str] = []

    async def _live_quote(row_payload):
        seen.append(str(row_payload.get("position_id")))
        return {
            "premium": 11.25,
            "spot": 1505.0,
            "mark_time": "2026-07-27T04:00:00+00:00",
            "price_source": "chain_cache_live",
        }

    await store.sweep_expiry_closures(today=date(2026, 7, 27), mark_resolver=_live_quote)

    assert seen == ["cipla"]
    closed = state["closed_positions"][0]
    assert closed["exit_premium"] == 11.25
    assert closed["exit_price_quality"] == "live_quote"
    assert closed["stale_mark_exit"] is False
    # 425 units, entry 28.50 → exit 11.25 is a real loss, net of charges.
    assert closed["realized_pnl"] < -7000.0


@pytest.mark.asyncio
async def test_a_live_chain_quote_with_no_timestamp_is_not_aged_off_the_carried_mark(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """The REAL live-quote branch returns ``mark_time=None``.

    ``service.resolve_position_mark`` falls back to
    ``chain_analytics.chain_strike_mark``, which reads ``oc:<sym>:<expiry>``
    from Redis — written with ``OC_TTL = 60`` seconds, so a HIT is at most a
    minute old, but the payload carries no per-strike observation time. The
    resolver therefore returns a genuine current premium with
    ``mark_time=None``, and this is the branch these five positions are most
    likely to take (their strikes long ago fell off the ATM watchlist).

    The first cut fell back to the ROW's carried ``mark_time`` in that case,
    which on the live book produced a single closure asserting BOTH
    ``exit_price_quality="live_quote" / stale_mark_exit=False`` AND
    ``exit_mark_age_seconds=597491`` (6.9 days) — self-contradictory
    attribution on the one lane the fix exists to make attributable, and the
    closed row kept advertising the abandoned 2026-07-21 ``mark_time``
    alongside a freshly fetched ``exit_premium``.
    """
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, _ = _isolate(
        store,
        monkeypatch,
        [
            _row(
                position_id="cipla",
                underlying="CIPLA",
                expiry="2026-07-28",
                mark_time="2026-07-21T05:17:47+00:00",  # 6 days stale, as live
            )
        ],
    )

    async def _chain_cache_quote(_row_payload):
        # byte-for-byte the shape resolve_position_mark returns on the
        # chain_strike_mark branch
        return {
            "premium": 12.34,
            "spot": 1423.7,
            "mark_time": None,
            "price_source": "chain_cache_live",
        }

    await store.sweep_expiry_closures(
        today=date(2026, 7, 28),
        mark_resolver=_chain_cache_quote,
        now="2026-07-28T04:00:00+00:00",
    )

    closed = state["closed_positions"][0]
    assert closed["exit_price_quality"] == "live_quote"
    assert closed["stale_mark_exit"] is False
    assert closed["exit_premium"] == 12.34
    # the observation is the FETCH, bounded by the 60s cache TTL — NOT the
    # abandoned carried mark.
    assert closed["exit_mark_observed_at"] == "2026-07-28T04:00:00+00:00"
    assert closed["exit_mark_age_seconds"] == 0.0
    # and it is flagged as bounded rather than exactly timestamped
    assert closed["exit_mark_time_exact"] is False
    # the row's own mark_time must not stay at the abandoned observation
    assert closed["mark_time"] == "2026-07-28T04:00:00+00:00"


@pytest.mark.asyncio
async def test_a_timestamped_live_quote_keeps_its_own_observation_time(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """The opposite guard: when the resolver DOES supply a timestamp it wins,
    and the closure is marked as exactly timestamped."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, _ = _isolate(
        store,
        monkeypatch,
        [_row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28")],
    )

    async def _timestamped(_row_payload):
        return {
            "premium": 12.34,
            "spot": 1423.7,
            "mark_time": "2026-07-28T03:59:15+00:00",
            "price_source": "local_watchlist",
        }

    await store.sweep_expiry_closures(
        today=date(2026, 7, 28),
        mark_resolver=_timestamped,
        now="2026-07-28T04:00:00+00:00",
    )

    closed = state["closed_positions"][0]
    assert closed["exit_mark_observed_at"] == "2026-07-28T03:59:15+00:00"
    assert closed["exit_mark_age_seconds"] == 45.0
    assert closed["exit_mark_time_exact"] is True


@pytest.mark.asyncio
async def test_sweep_refuses_to_invent_a_price_and_leaves_the_row_open(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """No live quote AND no observed premium ever ⇒ the position stays open
    and is reported as unpriceable. Booking a fabricated fill would be worse
    than a visibly stuck row."""
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, _ = _isolate(
        store,
        monkeypatch,
        [
            _row(
                position_id="ghost",
                underlying="CIPLA",
                expiry="2026-07-28",
                entry_premium=0.0,
                latest_premium=0.0,
            )
        ],
    )

    report = await store.sweep_expiry_closures(today=date(2026, 7, 27))

    assert report["closed"] == 0
    assert report["skipped"][0]["skipped"] == "unpriceable"
    assert [r["position_id"] for r in state["open_positions"]] == ["ghost"]


@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_a_no_op_on_a_clean_book(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, journal = _isolate(
        store,
        monkeypatch,
        [_row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28")],
    )
    first = await store.sweep_expiry_closures(today=date(2026, 7, 27))
    second = await store.sweep_expiry_closures(today=date(2026, 7, 27))
    assert first["closed"] == 1
    assert second["closed"] == 0
    assert len(state["closed_positions"]) == 1
    assert len([r for r in journal if r.get("event") == "expiry_sweep_close"]) == 1


# ── 4. the per-underlying pass now obeys the same rule ───────────────────────


@pytest.mark.asyncio
async def test_sync_snapshot_expiry_pass_now_honours_the_2td_gate(
    tmp_path: Path, monkeypatch, policy_flags_on
) -> None:
    """Even on a degraded (execution_ready=False) cycle, a synced underlying's
    held row is closed at the 2TD boundary. Pre-fix this pass only closed at
    ``dte <= 0.8`` — with dte=4 on 2026-07-24 it did nothing."""
    import directional_options.paper as paper_mod

    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    state, _ = _isolate(
        store,
        monkeypatch,
        [_row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28")],
    )
    monkeypatch.setattr(paper_mod, "_ist_today", lambda *_a, **_k: date(2026, 7, 24))

    await store.sync_snapshot(
        {
            "selection": {"underlying": "CIPLA", "timeframe": "3minute"},
            "snapshot": {
                "as_of": "2026-07-24T05:00:00+00:00",
                "underlying": "CIPLA",
                "spot_price": 1490.0,
                "data_status": {"execution_ready": False},
            },
        }
    )

    assert state["open_positions"] == []
    assert state["closed_positions"][0]["close_reason"] == "forced_expiry_roll_2td"


@pytest.mark.asyncio
async def test_a_closed_row_can_never_be_reopened_by_a_stale_concurrent_writer(
    tmp_path: Path, monkeypatch
) -> None:
    """``closed`` must be TERMINAL in the persisted book.

    ``DirectionalOptionsPaperStore._lock`` is an ``asyncio.Lock``: it
    serialises writers inside ONE process and gives nothing across processes.
    The directional lane runs in TWO — ``LANESET=all`` in ``nomadcurie_backend``
    and ``LANESET=strategies`` in ``nomadcurie_backend_strategies`` (verified
    live on 2026-07-27: both started the ``directional_options`` runner within
    one second of each other at 09:22:3x, and ``runner:directional_options`` is
    in the strategies plane of ``core.laneset``).

    Every ``_save_positions`` call rewrites the WHOLE open list, not just the
    rows it touched. So process B, holding a list read a moment before process
    A committed a close, upserts that row straight back to ``open``. Confirmed
    against this Postgres with the pre-fix statement::

        INSERT ... VALUES ('p1','closed',...,'forced_expiry_roll_2td')   -- A
        INSERT ... VALUES ('p1','open',...,NULL) ON CONFLICT DO UPDATE   -- B
        =>  p1 | open | (null)

    and with the guarded statement the same sequence leaves ``p1 | closed |
    forced_expiry_roll_2td`` while a genuine later close (``stop_loss``) still
    applies. That is the difference between the expiry sweep working and it
    being silently undone with no error anywhere.

    This asserts the guard is in the statement actually handed to Postgres, by
    driving the real ``_save_positions``; the SQL semantics themselves were
    proven against the live database as above (they cannot be executed here —
    the suite runs with Postgres unreachable and the statement is JSONB).
    """
    store = DirectionalOptionsPaperStore(tmp_path / "paper")
    captured: list[str] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, statement, params=None):
            captured.append(str(statement))
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(store, "_maybe_seed_from_file", lambda: _noop_coro())
    monkeypatch.setattr("directional_options.paper.AsyncSessionLocal", _Session)

    await store._save_positions(
        {
            "open_positions": [_row(position_id="cipla", underlying="CIPLA", expiry="2026-07-28")],
            "closed_positions": [],
        }
    )

    assert captured, "no statement reached the session"
    sql = " ".join(captured[0].split())
    assert "ON CONFLICT (position_id) DO UPDATE" in sql
    assert (
        "WHERE directional_paper_positions.status IS DISTINCT FROM 'closed' "
        "OR EXCLUDED.status = 'closed'" in sql
    ), f"the conflict predicate no longer makes 'closed' terminal: {sql}"


async def _noop_coro():
    return None
