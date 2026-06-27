from __future__ import annotations

from datetime import datetime

from core.trading_calendar import IST, TradingCalendar


def test_trading_calendar_blocks_nse_holiday_and_allows_mcx_evening(tmp_path) -> None:
    calendar = TradingCalendar(path=tmp_path / "calendar.json")

    nse_holiday = datetime(2026, 5, 28, 10, 0, tzinfo=IST)
    mcx_morning = datetime(2026, 5, 28, 10, 0, tzinfo=IST)
    mcx_evening = datetime(2026, 5, 28, 18, 0, tzinfo=IST)

    assert calendar.is_exchange_open("NSE", nse_holiday) is False
    assert calendar.is_exchange_open("MCX", mcx_morning) is False
    assert calendar.is_exchange_open("MCX", mcx_evening) is True
    assert calendar.next_exchange_open("NSE", nse_holiday).isoformat() == "2026-05-29T09:15:00+05:30"


def test_trading_minutes_between_excludes_overnight_and_holidays(tmp_path) -> None:
    calendar = TradingCalendar(path=tmp_path / "calendar.json")

    def t(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=IST)

    # 2026-06-23 Tue, -24 Wed, -25 Thu, -26 Fri (Muharram, NSE closed),
    # -27 Sat, -28 Sun, -29 Mon.
    fn = calendar.trading_minutes_between
    # Intraday window.
    assert fn("NSE", t(2026, 6, 23, 9, 15), t(2026, 6, 23, 10, 15)) == 60.0
    # Full regular session is 09:15-15:30 = 375 min.
    assert fn("NSE", t(2026, 6, 23, 9, 15), t(2026, 6, 23, 15, 30)) == 375.0
    # Overnight gap contributes nothing: Tue 14:00 -> Wed 09:20 = 90 + 5.
    assert fn("NSE", t(2026, 6, 23, 14, 0), t(2026, 6, 24, 9, 20)) == 95.0
    # Exactly close -> next open adds zero bars (the wall-clock bug this fixes).
    assert fn("NSE", t(2026, 6, 23, 15, 30), t(2026, 6, 24, 9, 15)) == 0.0
    # Spanning a Friday holiday + weekend: Thu 14:00 -> Mon 09:20 = 90 + 5.
    assert fn("NSE", t(2026, 6, 25, 14, 0), t(2026, 6, 29, 9, 20)) == 95.0
    # Pre-open instant is clamped to the session open.
    assert fn("NSE", t(2026, 6, 23, 8, 0), t(2026, 6, 23, 10, 0)) == 45.0
    # end <= start is guarded.
    assert fn("NSE", t(2026, 6, 23, 14, 0), t(2026, 6, 23, 13, 0)) == 0.0


def test_trading_calendar_update_can_add_exchange_closure(tmp_path) -> None:
    calendar = TradingCalendar(path=tmp_path / "calendar.json")
    payload = calendar.serialize()
    payload["exchanges"]["MCX"]["exceptions"].append(
        {"date": "2026-05-29", "name": "manual gate", "status": "closed", "sessions": []}
    )

    calendar.update(payload)

    assert calendar.is_exchange_open("MCX", datetime(2026, 5, 29, 18, 0, tzinfo=IST)) is False
