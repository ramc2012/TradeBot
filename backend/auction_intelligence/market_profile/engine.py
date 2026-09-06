from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from math import floor
from typing import Any, Optional

from auction_intelligence.schemas import MarketBar, MarketProfileSnapshot


def _round_down(value: float, tick_size: float) -> float:
    return round(floor(value / tick_size) * tick_size, 6)


def _round_up(value: float, tick_size: float) -> float:
    return round(_round_down(value + tick_size - 1e-9, tick_size), 6)


def _price_ladder(low: float, high: float, tick_size: float) -> list[float]:
    start = _round_down(low, tick_size)
    end = _round_up(high, tick_size)
    steps = max(int(round((end - start) / tick_size)), 0)
    return [round(start + (index * tick_size), 6) for index in range(steps + 1)]


def _overlap(a_low: float, a_high: float, b_low: float, b_high: float) -> float:
    overlap = max(0.0, min(a_high, b_high) - max(a_low, b_low))
    union = max(a_high, b_high) - min(a_low, b_low)
    return overlap / union if union > 0 else 0.0


class MarketProfileEngine:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.period_minutes = int(config.get("period_minutes", 30))
        self.tick_size = float(config.get("tick_size", 0.5))
        self.value_area_pct = float(config.get("value_area_pct", 0.70))
        self.initial_balance_periods = int(config.get("initial_balance_periods", 2))
        self.min_tail_tpos = int(config.get("min_tail_tpos", 2))
        self.balance_overlap_min = float(config.get("balance_overlap_min", 0.65))

    def build_profile(
        self,
        symbol: str,
        bars: list[MarketBar],
        prior_profile: Optional[MarketProfileSnapshot] = None,
    ) -> MarketProfileSnapshot:
        from dataclasses import asdict
        from mp_core.cache import cached_json
        data = cached_json("auction-profile-v2", [{k: v for k, v in vars(self).items() if k != "config"}, symbol, bars, prior_profile],
                           lambda: asdict(self._build_profile(symbol, bars, prior_profile)))
        for key in ("tpo_counts", "tpo_letters"):
            data[key] = {float(price): value for price, value in data[key].items()}
        return MarketProfileSnapshot(**data)

    def _build_profile(self, symbol, bars, prior_profile=None):
        if not bars:
            raise ValueError("MarketProfileEngine requires at least one bar")

        ordered_bars = sorted(bars, key=lambda item: item.timestamp)
        period_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        period_map: dict[int, list[MarketBar]] = defaultdict(list)

        # Input bars are session anchored (09:15 for NSE, 09:00 for MCX).
        # Rounding 09:15 down to 09:00 splits the initial balance incorrectly.
        anchor = ordered_bars[0].timestamp.replace(second=0, microsecond=0)

        for bar in ordered_bars:
            diff_minutes = int((bar.timestamp - anchor).total_seconds() // 60)
            bucket = max(diff_minutes // self.period_minutes, 0)
            period_map[bucket].append(bar)

        tpo_letters: dict[float, list[str]] = defaultdict(list)
        total_volume = 0.0
        period_count = 0
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []

        # Ladder hard cap. The GIL-bound TPO build is O(session_range /
        # tick_size): a too-fine tick (NIFTY at 0.05) or one contaminated bar
        # (garbage cross-symbol high/low print) used to explode this loop into
        # hundreds of thousands of levels and seize the event loop for minutes
        # per call (observed live 2026-07-13, commodity_index_monitor endpoint).
        # Coarsen the tick so the whole session never exceeds MAX_LADDER_LEVELS
        # — a wrong tick now costs profile granularity, never the process.
        MAX_LADDER_LEVELS = 2000
        session_span = max(
            max(item.high for item in ordered_bars) - min(item.low for item in ordered_bars),
            0.0,
        )
        ladder_tick = self.tick_size
        if session_span > 0 and session_span / max(ladder_tick, 1e-9) > MAX_LADDER_LEVELS:
            ladder_tick = session_span / MAX_LADDER_LEVELS

        for bucket, bucket_bars in sorted(period_map.items()):
            period_count += 1
            letter = period_letters[bucket % len(period_letters)]
            period_high = max(item.high for item in bucket_bars)
            period_low = min(item.low for item in bucket_bars)
            highs.append(period_high)
            lows.append(period_low)
            closes.append(sorted(bucket_bars, key=lambda item: item.timestamp)[-1].close)
            total_volume += sum(item.volume for item in bucket_bars)

            for price in _price_ladder(period_low, period_high, ladder_tick):
                tpo_letters[price].append(letter)

        tpo_counts = {price: len(letters) for price, letters in tpo_letters.items()}
        prices = sorted(tpo_counts)
        total_tpos = sum(tpo_counts.values())
        midpoint = (ordered_bars[0].open + ordered_bars[-1].close) / 2.0

        poc_candidates = [price for price, count in tpo_counts.items() if count == max(tpo_counts.values())]
        poc = min(poc_candidates, key=lambda price: abs(price - midpoint))
        vah, val = self._value_area(prices, tpo_counts, poc)

        ib_high = max(highs[: self.initial_balance_periods])
        ib_low = min(lows[: self.initial_balance_periods])
        high_price = max(item.high for item in ordered_bars)
        low_price = min(item.low for item in ordered_bars)
        close_price = ordered_bars[-1].close
        open_price = ordered_bars[0].open
        day_range = max(high_price - low_price, self.tick_size)

        single_prints = [price for price, count in tpo_counts.items() if count == 1]
        buying_tail = self._tail_from_extreme(prices, tpo_counts, reverse=False)
        selling_tail = self._tail_from_extreme(prices, tpo_counts, reverse=True)
        poor_high = len(prices) >= 2 and tpo_counts[prices[-1]] > 1 and tpo_counts[prices[-2]] > 1
        poor_low = len(prices) >= 2 and tpo_counts[prices[0]] > 1 and tpo_counts[prices[1]] > 1

        last_period = period_map[max(period_map)]
        last_high = max(item.high for item in last_period)
        last_low = min(item.low for item in last_period)
        spike_direction = "none"
        spike_price: Optional[float] = None
        if last_low > vah:
            spike_direction = "up"
            spike_price = last_low
        elif last_high < val:
            spike_direction = "down"
            spike_price = last_high

        rotation_by_period: list[int] = []
        for idx in range(1, period_count):
            score = 0
            if highs[idx] > highs[idx - 1]:
                score += 1
            elif highs[idx] < highs[idx - 1]:
                score -= 1
            if lows[idx] > lows[idx - 1]:
                score += 1
            elif lows[idx] < lows[idx - 1]:
                score -= 1
            rotation_by_period.append(score)
        rotation_factor = sum(rotation_by_period)
        rotation_intensity = rotation_factor / max(2 * (period_count - 1), 1)

        consecutive_in_prior_value = 0
        if prior_profile is not None:
            for period_close in reversed(closes):
                if prior_profile.val <= period_close <= prior_profile.vah:
                    consecutive_in_prior_value += 1
                else:
                    break

        snapshot = MarketProfileSnapshot(
            symbol=symbol,
            session_date=ordered_bars[-1].timestamp.date().isoformat(),
            period_minutes=self.period_minutes,
            tick_size=self.tick_size,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            total_volume=total_volume,
            tpo_counts=tpo_counts,
            tpo_letters={price: "".join(letters) for price, letters in tpo_letters.items()},
            poc=poc,
            vah=vah,
            val=val,
            initial_balance_high=ib_high,
            initial_balance_low=ib_low,
            initial_balance_range=max(ib_high - ib_low, self.tick_size),
            day_range=day_range,
            range_extension_up=max(0.0, high_price - ib_high),
            range_extension_down=max(0.0, ib_low - low_price),
            single_prints=single_prints,
            buying_tail=buying_tail,
            selling_tail=selling_tail,
            poor_high=poor_high,
            poor_low=poor_low,
            excess_high=len(selling_tail) * self.tick_size,
            excess_low=len(buying_tail) * self.tick_size,
            spike_direction=spike_direction,
            spike_price=spike_price,
            period_count=period_count,
            sample_count=total_tpos,
            rotation_factor=rotation_factor,
            rotation_intensity=rotation_intensity,
            rotation_factors_by_period=rotation_by_period,
            consecutive_periods_in_prior_value=consecutive_in_prior_value,
        )
        return self.with_comparatives(snapshot, prior_profile)

    def build_composite_profile(
        self,
        symbol: str,
        sessions: list[list[MarketBar]],
    ) -> MarketProfileSnapshot:
        bars = [bar for session in sessions for bar in session]
        return self.build_profile(symbol=symbol, bars=bars)

    def with_comparatives(
        self,
        current: MarketProfileSnapshot,
        prior: Optional[MarketProfileSnapshot],
    ) -> MarketProfileSnapshot:
        if prior is None:
            return current

        overlap = _overlap(current.val, current.vah, prior.val, prior.vah)
        poc_shift = current.poc - prior.poc
        value_migration = (
            ((current.vah + current.val) / 2.0) - ((prior.vah + prior.val) / 2.0)
        )
        prior_poc_untouched = not (current.low_price <= prior.poc <= current.high_price)
        bracket_state = "balanced" if overlap >= self.balance_overlap_min else "expanding"

        return replace(
            current,
            value_area_overlap=round(overlap, 4),
            poc_shift=round(poc_shift, 4),
            value_migration=round(value_migration, 4),
            prior_poc_untouched=prior_poc_untouched,
            bracket_state=bracket_state,
        )

    def _value_area(
        self,
        prices: list[float],
        tpo_counts: dict[float, int],
        poc: float,
    ) -> tuple[float, float]:
        target = max(int(sum(tpo_counts.values()) * self.value_area_pct), 1)
        poc_index = prices.index(poc)
        lower = upper = poc_index
        covered = tpo_counts[poc]

        while covered < target and (lower > 0 or upper < len(prices) - 1):
            next_low = tpo_counts[prices[lower - 1]] if lower > 0 else -1
            next_high = tpo_counts[prices[upper + 1]] if upper < len(prices) - 1 else -1

            if next_high > next_low:
                upper += 1
                covered += next_high
            elif next_low > next_high:
                lower -= 1
                covered += next_low
            else:
                if upper < len(prices) - 1:
                    upper += 1
                    covered += max(next_high, 0)
                if covered < target and lower > 0:
                    lower -= 1
                    covered += max(next_low, 0)

        return prices[upper], prices[lower]

    def _tail_from_extreme(
        self,
        prices: list[float],
        tpo_counts: dict[float, int],
        *,
        reverse: bool,
    ) -> list[float]:
        ordered = list(reversed(prices)) if reverse else prices
        tail: list[float] = []
        for price in ordered:
            if tpo_counts[price] != 1:
                break
            tail.append(price)
        if len(tail) < self.min_tail_tpos:
            return []
        return sorted(tail)
