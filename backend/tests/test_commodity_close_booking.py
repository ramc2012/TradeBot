"""Guards the commodity futures close-booking fix.

The bug: the main stop/target/macd-reversal exit (inline in
CommodityStrategyAgent._manage_positions) placed a close order and popped the
position, relying entirely on order_book→on_fill to record the trade. But after
any restart the portfolio's VirtualPositions are NOT restored (only the agent's
runtime positions are), so on_fill finds no opposing position to close → it
opens a PHANTOM instead of booking → no TradeRecord, realized P&L stuck at 0,
yet the position vanished. The "~6-day futures-ledger freeze".

The fix routes the inline exit through the same self-healing book_close that
_close_futures_position uses. These tests prove book_close records the trade +
realized P&L from the agent's own data even when no open VirtualPosition exists.
"""
from __future__ import annotations

from paper_engine.portfolio import PaperPortfolio


def test_book_close_records_trade_without_open_virtualposition_short():
    pf = PaperPortfolio(initial_capital=1_000_000.0, session_id="t")
    assert len(pf._trade_history) == 0
    # SHORT closed in profit: entry 9090 → exit 9000, qty 200.
    pf.book_close(
        symbol="MCX:CRUDEOIL26JUNFUT", entry_action="SELL", qty=200,
        entry_price=9090.0, exit_price=9000.0, instrument_type="FUT",
    )
    assert len(pf._trade_history) == 1
    t = pf._trade_history[-1]
    assert t.symbol == "MCX:CRUDEOIL26JUNFUT"
    assert t.exit_price == 9000.0 and t.entry_price == 9090.0
    assert abs(t.pnl - (9090.0 - 9000.0) * 200) < 1e-6   # +18,000 short


def test_book_close_records_trade_long_pnl_sign():
    pf = PaperPortfolio(initial_capital=1_000_000.0, session_id="t")
    pf.book_close(
        symbol="MCX:NATURALGAS26JUNFUT", entry_action="BUY", qty=5000,
        entry_price=310.0, exit_price=311.0, instrument_type="FUT",
    )
    assert len(pf._trade_history) == 1
    assert abs(pf._trade_history[-1].pnl - (311.0 - 310.0) * 5000) < 1e-6   # +5,000 long


def test_book_close_books_realized_pnl_into_daily_ledger():
    pf = PaperPortfolio(initial_capital=1_000_000.0, session_id="t")
    pf.book_close(
        symbol="MCX:NICKEL26JUNFUT", entry_action="SELL", qty=1500,
        entry_price=1809.0, exit_price=1819.0, instrument_type="FUT",
    )
    # short that lost: (1809-1819)*1500 = -15,000
    total_realized = sum(pf._daily_pnl.values())
    assert abs(total_realized - (-15000.0)) < 1e-6
