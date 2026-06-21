"""Coverage targets for the automatic historical-data backfill.

A *coverage target* declares, for one data class and one candle interval, how far
back the system wants continuous history. The auto-backfill coordinator
(`data.historical_backfill`) reads these targets, asks the DB what is already
stored, and pulls only the missing windows.

Targets reflect the desk requirement:
  - Spot/index : 5Y @ 30min, 1Y @ 1min
  - Options    : 5Y @ 30min, 1Y @ 1min   (indices, ATM±N band — see OPTIONS_STRIKE_BAND)
  - Commodity  : 5Y @ 30min, 2Y @ 1min

Hard broker limits (Upstox V3 historical-candle):
  - minute/hour history starts 2022-01-01; daily history goes back to 2000.
  - Per request: 1-15min → 1 month; 30min/hours → 1 quarter; day → 1 decade.
So intraday targets are clamped at UPSTOX_INTRADAY_FLOOR; the pre-floor slice of a
5Y target is served from Fyers (where connected) and/or daily candles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Upstox only serves minute/hour candles from this date forward.
UPSTOX_INTRADAY_FLOOR = date(2022, 1, 1)

# Options backfill is scoped to index underlyings, ATM ± this many strikes
# (per the agreed scope — full-universe 1-min options is not pull-able).
OPTIONS_STRIKE_BAND = 10
OPTIONS_INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")


@dataclass(frozen=True)
class CoverageTarget:
    data_class: str          # "spot" | "options" | "commodity"
    interval: str            # "1minute" | "30minute" | "day"
    lookback_days: int       # desired history depth, in days
    # When True and the desired window predates UPSTOX_INTRADAY_FLOOR, the
    # pre-floor slice is attempted from Fyers, then backfilled as daily candles.
    extend_with_fyers: bool = True
    extend_with_daily: bool = True

    def window(self, today: date) -> tuple[date, date]:
        """Full desired [start, end] window for this target."""
        return today - timedelta(days=self.lookback_days), today

    def upstox_window(self, today: date) -> tuple[date, date] | None:
        """Window served by Upstox intraday, clamped to the 2022 floor.

        Returns None if the whole desired window predates the floor (nothing to
        pull from Upstox intraday for this target)."""
        start, end = self.window(today)
        if self.interval == "day":
            return start, end  # daily has no 2022 floor
        clamped_start = max(start, UPSTOX_INTRADAY_FLOOR)
        if clamped_start > end:
            return None
        return clamped_start, end


# The desk's standing coverage requirement.
DEFAULT_TARGETS: tuple[CoverageTarget, ...] = (
    # Spot / index
    CoverageTarget("spot", "30minute", 5 * 365),
    CoverageTarget("spot", "1minute", 365),
    # Options (indices, ATM band)
    CoverageTarget("options", "30minute", 5 * 365),
    CoverageTarget("options", "1minute", 365),
    # Commodity (MCX front-month futures)
    CoverageTarget("commodity", "30minute", 5 * 365),
    CoverageTarget("commodity", "1minute", 2 * 365),
)


def targets_for(data_class: str) -> list[CoverageTarget]:
    return [t for t in DEFAULT_TARGETS if t.data_class == data_class]
