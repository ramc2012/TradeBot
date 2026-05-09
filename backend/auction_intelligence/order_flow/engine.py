from __future__ import annotations

from statistics import pstdev
from typing import Any, Iterable, Optional

from auction_intelligence.schemas import DepthSnapshot, OrderFlowSnapshot, QuoteSnapshot, TradePrint


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _signed_imbalance(bids: float, asks: float) -> float:
    denom = bids + asks
    return (bids - asks) / denom if denom else 0.0


class OrderFlowEngine:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.trade_lookback = int(config.get("trade_lookback", 50))
        self.baseline_window = int(config.get("baseline_window", 120))
        self.quote_lookback = int(config.get("quote_lookback", max(self.trade_lookback * 2, 100)))
        self.spread_normalizer_ticks = float(config.get("spread_normalizer_ticks", 2.0))
        self.volatility_burst_threshold = float(config.get("volatility_burst_threshold", 1.5))

    def compute(
        self,
        quote: QuoteSnapshot,
        trades: list[TradePrint],
        depth: Optional[DepthSnapshot] = None,
        tick_size: float = 0.5,
        quote_history: Optional[list[QuoteSnapshot]] = None,
    ) -> OrderFlowSnapshot:
        recent_trades = trades[-self.trade_lookback :]
        recent_quotes = (quote_history or [quote])[-self.quote_lookback :]
        spread = max(quote.ask - quote.bid, 0.0)
        mid_price = (quote.bid + quote.ask) / 2.0 if quote.ask and quote.bid else max(quote.ask, quote.bid)

        top_imbalance = _signed_imbalance(quote.bid_size, quote.ask_size)
        depth_imbalance = self._depth_imbalance(depth)
        aggressive_buy_volume = sum(
            trade.quantity for trade in recent_trades if trade.aggressor_side == "buy"
        )
        aggressive_sell_volume = sum(
            trade.quantity for trade in recent_trades if trade.aggressor_side == "sell"
        )
        delta = aggressive_buy_volume - aggressive_sell_volume
        cumulative_delta = sum(
            trade.quantity if trade.aggressor_side == "buy" else -trade.quantity if trade.aggressor_side == "sell" else 0.0
            for trade in trades
        )

        vwap = self._vwap(recent_trades, fallback=mid_price)
        vwap_drift = (recent_trades[-1].price if recent_trades else mid_price) - vwap
        micro_price = self._micro_price(quote)
        quoted_spread_bps = self._spread_bps(spread=spread, mid_price=mid_price)
        micro_price_offset_bps = self._spread_bps(
            spread=abs(micro_price - mid_price),
            mid_price=mid_price,
        )
        trade_imbalance = self._trade_imbalance(
            aggressive_buy_volume=aggressive_buy_volume,
            aggressive_sell_volume=aggressive_sell_volume,
        )
        order_flow_imbalance = self._order_flow_imbalance(recent_quotes)
        book_pressure = _clamp(
            (0.45 * top_imbalance)
            + (0.30 * depth_imbalance)
            + (0.25 * _clamp(order_flow_imbalance, -1.0, 1.0)),
            -1.0,
            1.0,
        )
        queue_pressure = (0.5 * top_imbalance) + (0.25 * depth_imbalance) + (0.25 * book_pressure)
        trade_intensity = self._trade_intensity(recent_trades)
        quote_repricing_rate = self._quote_repricing_rate(recent_quotes)
        volatility_burst = self._volatility_burst(trades)

        spread_norm = spread / max(tick_size * self.spread_normalizer_ticks, tick_size)
        signal_push = abs(delta) / max(aggressive_buy_volume + aggressive_sell_volume, 1.0)
        toxicity_score = _clamp(
            (0.28 * abs(order_flow_imbalance))
            + (0.22 * abs(trade_imbalance))
            + (0.15 * min(abs(micro_price_offset_bps) / max(quoted_spread_bps + 1.0, 1.0), 1.0))
            + (0.20 * max(volatility_burst - 1.0, 0.0))
            + (0.15 * min(quoted_spread_bps / 12.0, 1.0)),
            0.0,
            1.0,
        )
        adverse_selection_risk = _clamp(
            (0.25 * abs(vwap_drift) / max(spread + tick_size, tick_size))
            + (0.20 * max(volatility_burst - 1.0, 0.0))
            + (0.20 * abs(queue_pressure))
            + (0.20 * toxicity_score)
            + (0.15 * min(abs(micro_price_offset_bps) / max(quoted_spread_bps + 1.0, 1.0), 1.0)),
            0.0,
            1.0,
        )

        passive_fill_probability = _clamp(
            0.70
            + (0.14 * max(book_pressure, 0.0))
            + (0.08 * max(queue_pressure, 0.0))
            - (0.10 * spread_norm)
            - (0.22 * adverse_selection_risk),
            0.0,
            1.0,
        )
        aggressive_fill_probability = _clamp(
            0.90
            - (0.08 * spread_norm)
            + (0.05 * signal_push)
            + (0.04 * abs(order_flow_imbalance))
            - (0.06 * toxicity_score),
            0.0,
            1.0,
        )
        timing_confidence = _clamp(
            0.38
            + (0.16 * signal_push)
            + (0.12 * abs(queue_pressure))
            + (0.12 * abs(order_flow_imbalance))
            + (0.08 * abs(trade_imbalance))
            + (0.08 * min(volatility_burst, 2.0))
            + (0.06 * min(trade_intensity / 12.0, 1.0))
            + (0.05 * min(quote_repricing_rate / 10.0, 1.0))
            - (0.10 * spread_norm)
            - (0.08 * toxicity_score),
            0.0,
            1.0,
        )

        execution_aggression = "PASSIVE"
        if volatility_burst >= self.volatility_burst_threshold or adverse_selection_risk >= 0.75 or toxicity_score >= 0.78:
            execution_aggression = "AGGRESSIVE"
        elif timing_confidence < 0.45:
            execution_aggression = "WAIT"

        return OrderFlowSnapshot(
            spread=round(spread, 4),
            mid_price=round(mid_price, 4),
            micro_price=round(micro_price, 4),
            top_imbalance=round(top_imbalance, 4),
            depth_imbalance=round(depth_imbalance, 4),
            aggressive_buy_volume=round(aggressive_buy_volume, 4),
            aggressive_sell_volume=round(aggressive_sell_volume, 4),
            delta=round(delta, 4),
            cumulative_delta=round(cumulative_delta, 4),
            vwap=round(vwap, 4),
            vwap_drift=round(vwap_drift, 4),
            queue_pressure=round(queue_pressure, 4),
            volatility_burst=round(volatility_burst, 4),
            passive_fill_probability=round(passive_fill_probability, 4),
            aggressive_fill_probability=round(aggressive_fill_probability, 4),
            adverse_selection_risk=round(adverse_selection_risk, 4),
            timing_confidence=round(timing_confidence, 4),
            execution_aggression=execution_aggression,
            micro_stop_distance=round(max(spread * 1.5, tick_size), 4),
            trade_imbalance=round(trade_imbalance, 4),
            order_flow_imbalance=round(order_flow_imbalance, 4),
            book_pressure=round(book_pressure, 4),
            micro_price_offset_bps=round(micro_price_offset_bps, 4),
            trade_intensity_per_minute=round(trade_intensity, 4),
            quote_repricing_rate=round(quote_repricing_rate, 4),
            toxicity_score=round(toxicity_score, 4),
        )

    def _depth_imbalance(self, depth: Optional[DepthSnapshot]) -> float:
        if depth is None:
            return 0.0
        bid_qty = sum(level.quantity for level in depth.bids)
        ask_qty = sum(level.quantity for level in depth.asks)
        return _signed_imbalance(bid_qty, ask_qty)

    def _micro_price(self, quote: QuoteSnapshot) -> float:
        denom = quote.bid_size + quote.ask_size
        if denom <= 0:
            return (quote.bid + quote.ask) / 2.0
        return ((quote.ask * quote.bid_size) + (quote.bid * quote.ask_size)) / denom

    def _vwap(self, trades: Iterable[TradePrint], fallback: float) -> float:
        total_qty = 0.0
        total_value = 0.0
        for trade in trades:
            total_qty += trade.quantity
            total_value += trade.price * trade.quantity
        return total_value / total_qty if total_qty else fallback

    def _trade_imbalance(
        self,
        *,
        aggressive_buy_volume: float,
        aggressive_sell_volume: float,
    ) -> float:
        total_volume = aggressive_buy_volume + aggressive_sell_volume
        if total_volume <= 0:
            return 0.0
        return (aggressive_buy_volume - aggressive_sell_volume) / total_volume

    def _order_flow_imbalance(self, quotes: list[QuoteSnapshot]) -> float:
        if len(quotes) < 2:
            return 0.0

        imbalance = 0.0
        depth_scale = 0.0
        prev = quotes[0]
        for current in quotes[1:]:
            prev_bid = float(prev.bid or 0.0)
            prev_ask = float(prev.ask or 0.0)
            prev_bid_qty = float(prev.bid_size or 0.0)
            prev_ask_qty = float(prev.ask_size or 0.0)
            current_bid = float(current.bid or 0.0)
            current_ask = float(current.ask or 0.0)
            current_bid_qty = float(current.bid_size or 0.0)
            current_ask_qty = float(current.ask_size or 0.0)

            imbalance += (
                (current_bid_qty if current_bid >= prev_bid else 0.0)
                - (prev_bid_qty if current_bid <= prev_bid else 0.0)
                - (current_ask_qty if current_ask <= prev_ask else 0.0)
                + (prev_ask_qty if current_ask >= prev_ask else 0.0)
            )
            depth_scale += max(
                (prev_bid_qty + prev_ask_qty + current_bid_qty + current_ask_qty) / 4.0,
                1.0,
            )
            prev = current

        if depth_scale <= 0:
            return 0.0
        normalized = imbalance / depth_scale
        return _clamp(normalized, -1.0, 1.0)

    def _trade_intensity(self, trades: list[TradePrint]) -> float:
        if len(trades) < 2:
            return float(len(trades))
        elapsed_seconds = max(
            (trades[-1].timestamp - trades[0].timestamp).total_seconds(),
            1.0,
        )
        return (len(trades) * 60.0) / elapsed_seconds

    def _quote_repricing_rate(self, quotes: list[QuoteSnapshot]) -> float:
        if len(quotes) < 2:
            return 0.0
        repricings = 0
        for prev, current in zip(quotes, quotes[1:]):
            if current.bid != prev.bid or current.ask != prev.ask:
                repricings += 1
        elapsed_seconds = max(
            (quotes[-1].timestamp - quotes[0].timestamp).total_seconds(),
            1.0,
        )
        return (repricings * 60.0) / elapsed_seconds

    def _spread_bps(self, *, spread: float, mid_price: float) -> float:
        if mid_price <= 0:
            return 0.0
        return (spread / mid_price) * 10_000.0

    def _volatility_burst(self, trades: list[TradePrint]) -> float:
        if len(trades) < 6:
            return 1.0
        prices = [trade.price for trade in trades]
        returns = [
            (prices[index] - prices[index - 1]) / prices[index - 1]
            for index in range(1, len(prices))
            if prices[index - 1]
        ]
        if len(returns) < 4:
            return 1.0

        recent = returns[-min(10, len(returns)) :]
        baseline = returns[-min(self.baseline_window, len(returns)) :]
        recent_std = pstdev(recent) if len(recent) > 1 else 0.0
        baseline_std = pstdev(baseline) if len(baseline) > 1 else 0.0
        if baseline_std <= 0:
            return 1.0
        return recent_std / baseline_std
