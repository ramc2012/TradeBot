"""Tests for the calendar-driven expiry policy (owner spec 2026-07-20).

Covers the rules the owner actually stated:
  * indices trade until their expiry (cash-settled, no roll);
  * stocks roll to the NEXT monthly once <= 5 TRADING days remain, because
    Indian single-stock options are PHYSICALLY SETTLED and we will not open a
    new position inside the compulsory-delivery window;
  * holidays shift a monthly expiry EARLIER, never later, and are counted in
    the roll horizon (the legacy bare-Mon–Fri count fired the roll a day late);
  * a calendar-vs-exchange disagreement is LOUD and the exchange wins;
  * a broker outage still yields a usable calendar expiry.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from core.expiry_policy import (
    STOCK_ROLL_TRADING_DAYS,
    ExpiryAnchor,
    ExpiryPolicy,
)


def _policy(monkeypatch, holidays: set[date] | None = None) -> ExpiryPolicy:
    """An ExpiryPolicy with a deterministic, injectable holiday table."""
    holidays = holidays or set()
    policy = ExpiryPolicy()
    monkeypatch.setattr(
        policy,
        "_is_trading_day",
        lambda d: d.weekday() < 5 and d not in holidays,
    )
    return policy


# ── monthly expiry derivation ────────────────────────────────────────────
def test_monthly_expiry_is_last_board_weekday(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    # July 2026: last Tuesday is the 28th.
    assert policy.monthly_expiry("RELIANCE", 2026, 7) == date(2026, 7, 28)


def test_monthly_expiry_walks_backward_past_a_holiday(monkeypatch) -> None:
    """A holiday can only pull an expiry EARLIER — never later."""
    policy = _policy(monkeypatch, holidays={date(2026, 7, 28)})
    assert policy.monthly_expiry("RELIANCE", 2026, 7) == date(2026, 7, 27)


def test_november_2026_regression(monkeypatch) -> None:
    """The legacy _KNOWN_HOLIDAYS table said 2026-11-24; the ops calendar says
    the 24th is a holiday, so the real monthly is 2026-11-23. This date is in
    the FUTURE, which is why EXPIRY_POLICY_ENABLED=False is a same-day revert
    and not a resting state."""
    policy = _policy(monkeypatch, holidays={date(2026, 11, 24)})
    assert policy.monthly_expiry("NIFTY", 2026, 11) == date(2026, 11, 23)


# ── trading-day counting ─────────────────────────────────────────────────
def test_trading_days_until_is_holiday_aware(monkeypatch) -> None:
    plain = _policy(monkeypatch)
    # Mon 2026-07-20 → Tue 2026-07-28 inclusive = 6 weekdays (21..28 minus w/e).
    assert plain.trading_days_until(date(2026, 7, 28), today=date(2026, 7, 20)) == 6
    with_holiday = _policy(monkeypatch, holidays={date(2026, 7, 22)})
    assert with_holiday.trading_days_until(date(2026, 7, 28), today=date(2026, 7, 20)) == 5


# ── the rules ────────────────────────────────────────────────────────────
def test_index_never_rolls(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    # One trading day before expiry — an index still trades the near month.
    decision = policy.decide("NIFTY", "INDEX", today=date(2026, 7, 27))
    assert decision.current_expiry == date(2026, 7, 28)
    assert decision.rolled is False
    assert decision.roll_reason is None
    assert decision.anchor is ExpiryAnchor.CALENDAR


def test_stock_does_not_roll_at_six_trading_days(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    decision = policy.decide("RELIANCE", "STOCK", today=date(2026, 7, 20))
    assert decision.trading_days_to_current == 6 > STOCK_ROLL_TRADING_DAYS
    assert decision.current_expiry == date(2026, 7, 28)
    assert decision.rolled is False


def test_stock_rolls_at_exactly_five_trading_days(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    decision = policy.decide("RELIANCE", "STOCK", today=date(2026, 7, 21))
    assert decision.current_expiry == date(2026, 8, 25)  # last Tuesday of Aug
    assert decision.rolled is True
    # The REASON is encoded, because it is the whole point: physical settlement.
    assert decision.roll_reason == "physical_settlement_roll_5td"


def test_stock_stays_rolled_at_four_and_zero_trading_days(monkeypatch) -> None:
    """Once inside the delivery-risk window the roll must not oscillate back."""
    policy = _policy(monkeypatch)
    for today, ttd in ((date(2026, 7, 22), 4), (date(2026, 7, 27), 1), (date(2026, 7, 28), 0)):
        assert policy.trading_days_until(date(2026, 7, 28), today=today) == ttd
        decision = policy.decide("RELIANCE", "STOCK", today=today)
        assert decision.current_expiry == date(2026, 8, 25), today
        assert decision.rolled is True


def test_index_on_its_own_expiry_day_still_trades_that_expiry(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    decision = policy.decide("NIFTY", "INDEX", today=date(2026, 7, 28))
    assert decision.current_expiry == date(2026, 7, 28)
    assert decision.trading_days_to_current == 0
    assert decision.next_expiry == date(2026, 8, 25)
    assert decision.rolled is False


def test_month_boundary_rolls_the_ladder_forward(monkeypatch) -> None:
    """The day AFTER an expiry, the ladder must start at the next month for
    BOTH kinds — and the stock must not be double-rolled to September."""
    policy = _policy(monkeypatch)
    assert policy.expiry_ladder("RELIANCE", today=date(2026, 7, 29))[:2] == [
        date(2026, 8, 25), date(2026, 9, 29)
    ]
    assert policy.decide("NIFTY", "INDEX", today=date(2026, 7, 29)).current_expiry == (
        date(2026, 8, 25)
    )
    stock = policy.decide("RELIANCE", "STOCK", today=date(2026, 7, 29))
    assert stock.current_expiry == date(2026, 8, 25)
    assert stock.rolled is False


def test_a_holiday_chain_walks_back_over_the_weekend(monkeypatch) -> None:
    policy = _policy(monkeypatch, holidays={date(2026, 8, 25), date(2026, 8, 24)})
    assert policy.monthly_expiry("RELIANCE", 2026, 8) == date(2026, 8, 21)  # Friday


def test_holiday_week_fires_the_stock_roll_a_day_earlier(monkeypatch) -> None:
    """The regression the legacy weekday count produced: a holiday inside the
    window means five trading days are reached one CALENDAR day sooner, so a
    naive Mon–Fri count would still be sitting on the near month."""
    naive = _policy(monkeypatch)
    assert naive.decide("RELIANCE", "STOCK", today=date(2026, 7, 20)).rolled is False
    holiday_aware = _policy(monkeypatch, holidays={date(2026, 7, 22)})
    rolled = holiday_aware.decide("RELIANCE", "STOCK", today=date(2026, 7, 20))
    assert rolled.rolled is True
    assert rolled.current_expiry == date(2026, 8, 25)


def test_decide_many_keys_by_symbol(monkeypatch) -> None:
    policy = _policy(monkeypatch)
    out = policy.decide_many(
        [("NIFTY", "INDEX"), ("RELIANCE", "STOCK")], today=date(2026, 7, 21)
    )
    assert out["NIFTY"].rolled is False
    assert out["RELIANCE"].rolled is True


# ── exchange validation ──────────────────────────────────────────────────
def test_exchange_agreement_confirms(monkeypatch) -> None:
    policy = _policy(monkeypatch)

    async def probe(symbol, kind):
        return [date(2026, 7, 28), date(2026, 8, 25)]

    report = asyncio.run(
        policy.validate_against_exchange(
            probe=probe, symbols=[("NIFTY", "INDEX")], today=date(2026, 7, 20)
        )
    )
    assert report.ok is True
    assert report.confirmed == ["NIFTY"]
    assert policy.validated_decision("NIFTY").anchor is ExpiryAnchor.EXCHANGE_CONFIRMED


def test_exchange_mismatch_is_loud_and_the_exchange_wins(monkeypatch, caplog) -> None:
    policy = _policy(monkeypatch)
    persisted: dict[str, object] = {}
    monkeypatch.setattr(
        policy,
        "_persist_mismatch_marker",
        lambda report: _record(persisted, report),
    )

    async def probe(symbol, kind):
        return [date(2026, 7, 27), date(2026, 8, 25)]  # exchange says the 27th

    report = asyncio.run(
        policy.validate_against_exchange(
            probe=probe, symbols=[("NIFTY", "INDEX")], today=date(2026, 7, 20)
        )
    )
    assert report.ok is False
    mismatch = report.mismatches[0]
    # BOTH values are named — never a silent fallback.
    assert mismatch["calendar"] == "2026-07-28"
    assert mismatch["exchange"] == "2026-07-27"
    decided = policy.validated_decision("NIFTY")
    assert decided.current_expiry == date(2026, 7, 27)
    assert decided.anchor is ExpiryAnchor.EXCHANGE_OVERRIDE
    # ...and a durable marker is written so it survives log rotation.
    assert persisted["report"] is report


async def _record(sink, report):
    sink["report"] = report


def test_broker_outage_still_returns_a_usable_calendar_expiry(monkeypatch) -> None:
    """Inverting the dependency is the point: the broker probe that produced 405
    swallowed TimeoutErrors on 2026-07-20 may now fail without blinding us."""
    policy = _policy(monkeypatch)

    async def probe(symbol, kind):
        raise TimeoutError("broker down")

    report = asyncio.run(
        policy.validate_against_exchange(
            probe=probe, symbols=[("RELIANCE", "STOCK")], today=date(2026, 7, 20)
        )
    )
    assert report.unavailable == ["RELIANCE"]
    decision = policy.resolve("RELIANCE", "STOCK", today=date(2026, 7, 20))
    assert decision.current_expiry == date(2026, 7, 28)
    assert decision.anchor is ExpiryAnchor.CALENDAR


def test_session_cache_is_scoped_to_its_date(monkeypatch) -> None:
    policy = _policy(monkeypatch)

    async def probe(symbol, kind):
        return [date(2026, 7, 28)]

    asyncio.run(
        policy.validate_against_exchange(
            probe=probe, symbols=[("NIFTY", "INDEX")], today=date(2026, 7, 20)
        )
    )
    assert policy.resolve("NIFTY", "INDEX", today=date(2026, 7, 20)).anchor is (
        ExpiryAnchor.EXCHANGE_CONFIRMED
    )
    # A different session date must NOT reuse yesterday's validation.
    assert policy.resolve("NIFTY", "INDEX", today=date(2026, 7, 21)).anchor is (
        ExpiryAnchor.CALENDAR
    )
    policy.reset_session()
    assert policy.session_cache_state()["session_date"] is None


def test_out_of_range_roll_setting_falls_back_loudly(monkeypatch) -> None:
    from core.config import settings
    import core.expiry_policy as mod

    monkeypatch.setattr(settings, "EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS", 99, raising=False)
    assert mod._stock_roll_trading_days() == STOCK_ROLL_TRADING_DAYS


@pytest.mark.parametrize("kind", ["INDEX", "IDX", "INDICES"])
def test_index_kind_aliases(monkeypatch, kind: str) -> None:
    policy = _policy(monkeypatch)
    assert policy.decide("NIFTY", kind, today=date(2026, 7, 27)).kind == "INDEX"
