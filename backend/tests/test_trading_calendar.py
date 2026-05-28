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


def test_trading_calendar_update_can_add_exchange_closure(tmp_path) -> None:
    calendar = TradingCalendar(path=tmp_path / "calendar.json")
    payload = calendar.serialize()
    payload["exchanges"]["MCX"]["exceptions"].append(
        {"date": "2026-05-29", "name": "manual gate", "status": "closed", "sessions": []}
    )

    calendar.update(payload)

    assert calendar.is_exchange_open("MCX", datetime(2026, 5, 29, 18, 0, tzinfo=IST)) is False
