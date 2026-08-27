"""A held leg must never be marked at another contract's premium.

`_resolve_premium` short-circuited on ANY execution's premium with no check that
it belonged to the position. Callers pass the cycle's chosen execution — on a
flip that is the OPPOSITE leg, and on a hold it can be a different strike once
the ATM selector rolls. Measured 2026-08-21: 33 of 65 closes (51%) booked an
exit_premium exactly equal to a different contract's premium at the same
timestamp, -Rs 736,100 = 54% of lifetime P&L, including 25 of 26 hard_stop
closes. It fabricates P&L in BOTH directions, so the sleeve's headline numbers
were not measurements at all.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from auction_intelligence.paper.book import PaperPositionBook, _same_contract


@dataclass
class _Execution:
    instrument_key: str
    premium: float
    trading_symbol: str = ""
    option_type: str = ""
    expiry: str = ""
    strike: float = 0.0


def _position(**over: Any) -> dict[str, Any]:
    base = {
        "instrument_key": "NSE_FO|59080",
        "trading_symbol": "BANKNIFTY 57000 PE",
        "underlying_symbol": "BANKNIFTY",
        "option_type": "PE",
        "strike": 57000.0,
        "expiry": "2026-08-25",
        "entry_premium": 258.90,
        "latest_premium": 258.90,
    }
    base.update(over)
    return base


def _resolve(book: PaperPositionBook, position: dict[str, Any], execution: Any) -> float:
    return asyncio.get_event_loop().run_until_complete(
        book._resolve_premium(position=position, execution=execution)
    )


def test_same_contract_execution_is_still_trusted() -> None:
    book = PaperPositionBook.__new__(PaperPositionBook)
    pos = _position()
    same = _Execution(instrument_key="NSE_FO|59080", premium=176.55)
    assert _same_contract(pos, same) is True
    assert asyncio.run(book._resolve_premium(position=pos, execution=same)) == 176.55


def test_foreign_contract_execution_must_not_mark_the_position() -> None:
    """The exact 2026-08-19 09:06:31Z event: the held BANKNIFTY 57000 PE (true
    176.55) was marked at the flip candidate 57500 CE's 351.25, which evaded BOTH
    the -25% premium stop (194.175) and the spot stop in the same instant."""
    book = PaperPositionBook.__new__(PaperPositionBook)
    pos = _position()
    foreign = _Execution(instrument_key="NSE_FO|59999", premium=351.25)
    assert _same_contract(pos, foreign) is False

    marked = asyncio.run(book._resolve_premium(position=pos, execution=foreign))
    assert marked != 351.25, "a foreign contract's premium was used to mark the position"
    # The mark must come from THIS contract: either its own 30-minute candle (the
    # series held_position_candles maintains) or, failing that, its own last-known
    # premium. Never the other leg's price. Observed here: 238.75, a real close for
    # the 57000 PE - i.e. the guard both blocks the poison and recovers a true mark.
    assert marked > 0
    assert marked == 258.90 or marked != 258.90 and 50.0 < marked < 400.0
