from __future__ import annotations

from datetime import date

from core.config import auction_front_month_book_symbols
from data.index_futures_backfill import (
    chunk_dates,
    fyers_front_month_symbol,
    normalize_underlyings,
    normalize_upstox_candles,
)


def test_normalize_underlyings_defaults_to_required_index_futures() -> None:
    assert normalize_underlyings(None) == ["NIFTY", "BANKNIFTY", "SENSEX"]


def test_fyers_front_month_symbol_rolls_after_monthly_expiry() -> None:
    assert fyers_front_month_symbol("NIFTY", date(2026, 5, 30)) == "NSE:NIFTY26JUNFUT"
    assert fyers_front_month_symbol("BANKNIFTY", date(2026, 5, 30)) == "NSE:BANKNIFTY26JUNFUT"
    assert fyers_front_month_symbol("SENSEX", date(2026, 5, 30)) == "BSE:SENSEX26JUNFUT"


def test_auction_order_flow_books_roll_by_calendar(monkeypatch) -> None:
    monkeypatch.setattr("core.config.settings.AUCTION_OF_BOOK_AUTO_ENABLED", True)
    monkeypatch.setattr("core.config.settings.AUCTION_OF_BOOK_SYMBOLS", "")

    may = auction_front_month_book_symbols(date(2026, 5, 20))
    june = auction_front_month_book_symbols(date(2026, 5, 30))

    assert may["NSE:NIFTY50-INDEX"] == "NSE:NIFTY26MAYFUT"
    assert june["NSE:NIFTY50-INDEX"] == "NSE:NIFTY26JUNFUT"
    assert june["NSE:BANKNIFTY-INDEX"] == "NSE:BANKNIFTY26JUNFUT"
    assert june["BSE:SENSEX-INDEX"] == "BSE:SENSEX26JUNFUT"


def test_chunk_dates_inclusive_windows() -> None:
    assert chunk_dates(date(2024, 1, 1), date(2024, 1, 5), 2) == [
        (date(2024, 1, 1), date(2024, 1, 2)),
        (date(2024, 1, 3), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 5)),
    ]


def test_normalize_upstox_candles_returns_chronological_utc_rows() -> None:
    rows = normalize_upstox_candles(
        [
            ["2026-05-30T09:16:00+05:30", 2, 3, 1, 2.5, 200, 0],
            ["2026-05-30T09:15:00+05:30", 1, 2, 0.5, 1.5, 100, 0],
        ]
    )

    assert [row["time"] for row in rows] == [
        "2026-05-30T03:45:00+00:00",
        "2026-05-30T03:46:00+00:00",
    ]
    assert rows[0]["volume"] == 100


def test_front_month_uses_current_expiry_weekdays_where_they_diverge() -> None:
    """July 2026: NSE last-Tuesday = Jul 28, BSE last-Thursday = Jul 30. On the
    Wednesday between, NIFTY has already rolled to AUG while SENSEX is still in
    JUL. The previous (inverted) weekdays got both of these wrong — subscribing
    the auction order-flow feed to an expired NIFTY future for ~2 sessions/month
    and rolling SENSEX ~2 days early into the thin next-month book."""
    between = date(2026, 7, 29)
    assert fyers_front_month_symbol("NIFTY", between) == "NSE:NIFTY26AUGFUT"
    assert fyers_front_month_symbol("BANKNIFTY", between) == "NSE:BANKNIFTY26AUGFUT"
    assert fyers_front_month_symbol("SENSEX", between) == "BSE:SENSEX26JULFUT"
