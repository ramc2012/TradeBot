"""Strategy exit management for the NSE paper strategy runtime."""
from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Optional

from analysis.macd_engine import compute_ema, compute_macd
from agent.macd_quadrant import check_macd_death_signal
from agent.strategy_config import EXIT, FIRST_PULLBACK_IGNORE_BARS, MACD_FAST, MACD_MIN_BARS, MACD_SIGNAL, MACD_SLOW, OPTION_ENTRY_MA_FAST, REGIME_DEAD
from analytics.technicals import latest_macd_rsi
from market_data import option_history_service
from paper_engine.base_strategy_agent import _now_ist, _round_or_none
from paper_engine.strategy_agent_state import StrategyEvent, StrategyPosition, StrategyRuntime

if TYPE_CHECKING:
    from paper_engine.strategy_agent import PaperStrategyAgent


class StrategyExitMixin:
    async def _manage_exits(
        self: "PaperStrategyAgent",
        runtime: StrategyRuntime,
        rows: Optional[list[dict[str, object]]] = None,
    ) -> None:
        if not runtime.positions:
            return

        row_map: dict[str, dict[str, object]] = {}
        if rows:
            for row in rows:
                if isinstance(row, dict):
                    row_map[str(row.get("underlying", ""))] = row

        for symbol, pos in list(runtime.positions.items()):
            candles = await option_history_service.load_candles(
                underlying=pos.underlying,
                expiry=date.fromisoformat(pos.expiry),
                strike=pos.strike,
                option_type=pos.option_type,
                instrument_key=pos.instrument_key,
                interval="30minute",
                limit=80,
            )
            closes = [float(c["close"]) for c in candles if c.get("close")] if candles else []

            live_ltp: Optional[float] = None
            wl_row = row_map.get(pos.underlying)
            if wl_row:
                side_key = "ce" if pos.option_type == "CE" else "pe"
                wl_side = wl_row.get(side_key) or {}
                raw_ltp = wl_side.get("ltp") if isinstance(wl_side, dict) else None
                if raw_ltp:
                    try:
                        live_ltp = float(raw_ltp)
                    except (TypeError, ValueError):
                        live_ltp = None

            if closes:
                latest_close = closes[-1]
                if live_ltp and live_ltp > 0 and abs(latest_close - live_ltp) / max(live_ltp, 1.0) > 0.10:
                    latest_close = live_ltp
            elif live_ltp and live_ltp > 0:
                latest_close = live_ltp
            else:
                continue

            pos.current_price = latest_close
            pos.peak_price = max(pos.peak_price, latest_close)

            if len(closes) >= MACD_MIN_BARS:
                macd_line, _, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                pos.macd_line = macd_line

            indicators = latest_macd_rsi(closes)
            pos.latest_rsi = _round_or_none(indicators.get("rsi"), 2)
            option_ma20 = compute_ema(closes, OPTION_ENTRY_MA_FAST)[-1] if len(closes) >= OPTION_ENTRY_MA_FAST else None
            pos.option_ma20 = _round_or_none(option_ma20, 2)
            pos.above_option_ma20 = bool(option_ma20 is not None and latest_close >= option_ma20)
            bars_open = self._bars_since_entry(candles, pos.entered_at)
            return_pct = pos.return_pct

            if return_pct <= -EXIT.hard_stop_pct:
                await self._close_position(runtime, pos, latest_close, "hard_stop", qty=pos.qty)
                continue

            if pos.window_end:
                window_end = date.fromisoformat(pos.window_end)
                if _now_ist().date() >= (window_end - timedelta(days=EXIT.window_end_buffer_days)):
                    await self._close_position(runtime, pos, latest_close, "window_end", qty=pos.qty)
                    continue

            quadrant = self._regime_cache.get(pos.underlying)
            if quadrant and quadrant.regime == REGIME_DEAD:
                await self._close_position(runtime, pos, latest_close, "dead_zone_exit", qty=pos.qty)
                continue

            if return_pct >= EXIT.macd_death_min_profit_pct and pos.macd_line:
                if check_macd_death_signal(pos.macd_line, pos.option_type):
                    await self._close_position(runtime, pos, latest_close, "macd_death_signal", qty=pos.qty)
                    continue

            if pos.phase == self.PHASE_1 and return_pct >= EXIT.target_pct:
                exit_qty = max(1, int(pos.qty * EXIT.target_exit_fraction))
                await self._close_position(runtime, pos, latest_close, "target_50pct", qty=exit_qty, partial=True)
                pos.qty -= exit_qty
                pos.phase = self.PHASE_2
                self._append_commentary(
                    runtime.label,
                    f"TARGET HIT {pos.underlying} {pos.option_type} +{return_pct:.0f}%. "
                    f"Exited {exit_qty}, holding {pos.qty} as runner.",
                    tone="trade",
                )
                continue

            if pos.phase in (self.PHASE_2, self.PHASE_TRAILING) and return_pct >= EXIT.trail_activation_pct:
                pos.phase = self.PHASE_TRAILING
                pos.trailing_stop = _round_or_none(pos.peak_price * (1.0 - EXIT.trail_drawdown_pct / 100.0), 2)

            ma20_pullback = bool(pos.phase == self.PHASE_TRAILING and option_ma20 is not None and latest_close <= option_ma20)
            if ma20_pullback:
                if bars_open is not None and bars_open <= FIRST_PULLBACK_IGNORE_BARS and not pos.first_pullback_ignored_at:
                    pos.first_pullback_ignored_at = _now_ist().isoformat()
                    self._append_commentary(
                        runtime.label,
                        f"Ignoring first MA20 pullback for {pos.underlying} {pos.option_type}. "
                        f"Bars open={bars_open}, MA20={option_ma20:.2f}, last={latest_close:.2f}.",
                        tone="info",
                    )
                else:
                    await self._close_position(runtime, pos, latest_close, "ma20_pullback_exit", qty=pos.qty)
                    continue

            if pos.phase == self.PHASE_TRAILING and pos.trailing_stop and latest_close <= pos.trailing_stop:
                await self._close_position(runtime, pos, latest_close, "trailing_stoploss", qty=pos.qty)
                continue

        latest_prices = {sym: position.current_price for sym, position in runtime.positions.items()}
        if latest_prices:
            runtime.portfolio.update_prices(latest_prices)

    def _refresh_prices_from_watchlist(
        self: "PaperStrategyAgent",
        runtime: StrategyRuntime,
        rows: list[dict[str, object]],
    ) -> None:
        if not runtime.positions:
            return
        row_map: dict[str, dict[str, object]] = {str(row["underlying"]): row for row in rows}
        now_str = _now_ist().isoformat()
        for pos in runtime.positions.values():
            row = row_map.get(pos.underlying)
            if not row:
                continue
            side = "ce" if pos.option_type == "CE" else "pe"
            opt = row.get(side) or {}
            ltp = opt.get("ltp") if isinstance(opt, dict) else None
            if ltp:
                try:
                    price = float(ltp)
                    if price > 0:
                        pos.current_price = price
                        pos.price_updated_at = now_str
                        if price > pos.peak_price:
                            pos.peak_price = price
                except (TypeError, ValueError):
                    pass
        latest_prices = {symbol: position.current_price for symbol, position in runtime.positions.items()}
        if latest_prices:
            runtime.portfolio.update_prices(latest_prices)

    async def _close_position(
        self: "PaperStrategyAgent",
        runtime: StrategyRuntime,
        position: StrategyPosition,
        exit_price: float,
        reason: str,
        qty: Optional[int] = None,
        partial: bool = False,
    ) -> None:
        close_qty = qty or position.qty
        if position.symbol not in runtime.positions:
            return

        runtime.order_book.place_order(
            symbol=position.symbol,
            action="SELL",
            order_type="MARKET",
            qty=close_qty,
            instrument_type=position.option_type,
            expiry=position.expiry,
            strike=position.strike,
            option_type=position.option_type,
            ltp=exit_price,
        )
        pnl = (exit_price - position.entry_price) * close_qty

        if not partial:
            runtime.positions.pop(position.symbol, None)
            runtime.exits += 1

        self._append_event(
            runtime,
            StrategyEvent(
                time=_now_ist().isoformat(),
                event="exit",
                symbol=position.symbol,
                underlying=position.underlying,
                option_type=position.option_type,
                strike=position.strike,
                price=exit_price,
                qty=close_qty,
                reason=reason,
                signal_strength=position.signal_strength,
                pnl=_round_or_none(pnl, 2),
                phase=position.phase,
            ),
        )

        ret_pct = ((exit_price - position.entry_price) / position.entry_price * 100) if position.entry_price > 0 else 0
        exit_type = "PARTIAL EXIT" if partial else "EXIT"
        self._append_commentary(
            runtime.label,
            f"{exit_type} {position.underlying} {position.option_type} {int(position.strike)} "
            f"@{exit_price:.2f} | Qty={close_qty} | Return={ret_pct:.1f}% | "
            f"PnL=₹{pnl:.0f} | Reason={reason}",
            tone="trade",
        )
        await self._send_telegram_text(
            f"{exit_type} | {position.underlying} {position.option_type} {int(position.strike)} "
            f"@{exit_price:.2f}\nQty: {close_qty} | PnL: ₹{pnl:.0f} | Reason: {reason}"
        )

        await self._persist_order(
            runtime,
            position.symbol,
            "SELL",
            close_qty,
            exit_price,
            position.expiry,
            position.strike,
            position.option_type,
            reason,
        )
        await self._persist_macd_signal(
            underlying=position.underlying,
            expiry=position.expiry,
            strike=position.strike,
            option_type=position.option_type,
            macd_value=float(position.macd_line[-1] or 0) if position.macd_line else 0,
            signal_value=None,
            histogram=None,
            signal_type=f"exit_{reason}",
            premium_at_signal=exit_price,
        )
        if not partial:
            closed_pos = StrategyPosition(
                signal_id=position.signal_id,
                symbol=position.symbol,
                underlying=position.underlying,
                expiry=position.expiry,
                strike=position.strike,
                option_type=position.option_type,
                instrument_key=None,
                trading_symbol=None,
                qty=0,
                initial_qty=position.initial_qty,
                entry_price=position.entry_price,
                current_price=exit_price,
                peak_price=position.peak_price,
                entry_bar_time=position.entry_bar_time,
                entered_at=position.entered_at,
                signal_reason=position.signal_reason,
                regime=position.regime,
                spot_setup=position.spot_setup,
                entry_iv_pct=position.entry_iv_pct,
                option_ma20=position.option_ma20,
                option_ma50=position.option_ma50,
                above_option_ma20=position.above_option_ma20,
                above_option_ma50=position.above_option_ma50,
                first_pullback_ignored_at=position.first_pullback_ignored_at,
            )
            await self._persist_position(runtime, closed_pos, realized_pnl=pnl)
            await self._persist_agent_signal(
                runtime,
                closed_pos,
                status="closed",
                metadata={
                    "exit_reason": reason,
                    "closed_qty": close_qty,
                    "exit_price": exit_price,
                    "realized_pnl": pnl,
                    "setup_type": position.spot_setup,
                    "entry_iv_pct": position.entry_iv_pct,
                    "regime": position.regime,
                },
            )
        else:
            await self._persist_position(runtime, position)
            await self._persist_agent_signal(
                runtime,
                position,
                status="partial_exit",
                metadata={
                    "exit_reason": reason,
                    "closed_qty": close_qty,
                    "exit_price": exit_price,
                    "realized_pnl": pnl,
                    "setup_type": position.spot_setup,
                    "entry_iv_pct": position.entry_iv_pct,
                    "regime": position.regime,
                },
            )
