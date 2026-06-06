"""Strategy exit management for the NSE paper strategy runtime."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

from analysis.indicators_agent import IndicatorContext, indicators_agent
from analysis.macd_engine import compute_ema, compute_macd  # noqa: F401  (back-compat; calls now go via indicators_agent)
# Exit cascade (2026-06-02) trimmed to the canonical S1 design:
#   ── keep ──
#     #1 hard_stop          (-25% on premium, fires anytime, no gate)
#     #5 target_50pct       (partial half-exit at +50% → PHASE_2)
#     #6 trail activation   (arms trailing_stop at +60%, ratchets up)
#   ── new ──
#     #X macd_reversal_30m  (intra-bar opposite zero-cross on 30m option MACD,
#                            CE→close-on-MACD-crossing-down-through-zero,
#                            PE→close-on-MACD-crossing-up-through-zero.
#                            300m is a soft guideline ONLY — not enforced as a
#                            hold floor. Flip allowed: closing on opposite
#                            cross lets the same scan re-enter on the other
#                            side without any cooldown.)
#   ── deleted (were over-firing on noise) ──
#     #2 window_end         (premature expiry-fade close)
#     #3 dead_zone_exit     (regime-flip kill — this was today's BDL killer)
#     #4 macd_death_signal  (profit-skim)
#     #7 ma20_pullback_exit (MA-touch kill on the runner)
#     #8 trailing_stoploss  (premium-trail exit; replaced by macd_reversal as
#                            the natural exit; trailing_stop level still
#                            computed by #6 for diagnostic / UI visibility)
from agent.strategy_config import EXIT, MACD_FAST, MACD_MIN_BARS, MACD_SIGNAL, MACD_SLOW, OPTION_ENTRY_MA_FAST
from analytics.technicals import latest_macd_rsi
from core.config import settings
from db.database import AsyncSessionLocal
from market_data import option_history_service
from paper_engine.base_strategy_agent import IST, _now_ist, _parse_iso_timestamp, _round_or_none
from paper_engine.strategy_agent_state import StrategyEvent, StrategyPosition, StrategyRuntime
from sqlalchemy import text

if TYPE_CHECKING:
    from paper_engine.strategy_agent import PaperStrategyAgent


def _atr_from_closes(closes: list[float], period: int = 14) -> Optional[float]:
    """Close-to-close ATR proxy: mean absolute first-difference over the last
    `period` bars. We don't have OHL per option premium bar in the in-memory
    closes list, so this is a tight upper-bound on true ATR — fine as a
    trailing-stop floor where we want some sensitivity but not over-reaction.
    """
    if not closes or len(closes) < 2:
        return None
    window = closes[-(period + 1):]
    diffs = [abs(window[i] - window[i - 1]) for i in range(1, len(window))]
    if not diffs:
        return None
    return sum(diffs) / len(diffs)


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
        live_quotes = await self._latest_position_quote_map(list(runtime.positions.values()))

        for symbol, pos in list(runtime.positions.items()):
            candles = await option_history_service.load_candles(
                underlying=pos.underlying,
                expiry=date.fromisoformat(pos.expiry),
                strike=pos.strike,
                option_type=pos.option_type,
                instrument_key=pos.instrument_key,
                interval="30minute",
                limit=80,
                allow_broker_refresh=not (settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY),
            )
            closes = [float(c["close"]) for c in candles if c.get("close")] if candles else []

            live_ltp: Optional[float] = None
            live_observed_at: Optional[str] = None
            direct_quote = live_quotes.get(pos.symbol)
            if direct_quote:
                live_ltp, live_observed_at = direct_quote
            wl_row = row_map.get(pos.underlying)
            if live_ltp is None and wl_row:
                side_key = "ce" if pos.option_type == "CE" else "pe"
                wl_side = wl_row.get(side_key) or {}
                if not self._watchlist_side_matches_position(pos, wl_row, wl_side):
                    wl_side = {}
                raw_ltp = wl_side.get("ltp") if isinstance(wl_side, dict) else None
                if raw_ltp:
                    try:
                        live_ltp = float(raw_ltp)
                        live_observed_at = str(
                            wl_side.get("as_of")
                            or wl_side.get("time")
                            or wl_row.get("as_of")
                            or wl_row.get("time")
                            or ""
                        ) or None
                    except (TypeError, ValueError):
                        live_ltp = None

            if live_ltp and live_ltp > 0:
                latest_close = live_ltp
            elif closes:
                latest_close = closes[-1]
            else:
                continue

            pos.current_price = latest_close
            pos.peak_price = max(pos.peak_price, latest_close)
            pos.price_updated_at = live_observed_at or _now_ist().isoformat()

            # Cache MACD + EMA via indicators_agent so the entries side and
            # exit side of the same scan cycle don't recompute on the same
            # closes. Key includes symbol + interval + last bar time, so
            # subsequent bars correctly invalidate.
            ind_ctx = IndicatorContext(
                symbol=str(pos.symbol or pos.live_symbol or pos.underlying or ""),
                timeframe="30minute",
                last_bar_time=str((candles[-1] if candles else {}).get("time") or ""),
            )
            if len(closes) >= MACD_MIN_BARS:
                macd_result = indicators_agent.macd(
                    ctx=ind_ctx,
                    closes=closes,
                    fast=MACD_FAST,
                    slow=MACD_SLOW,
                    signal=MACD_SIGNAL,
                )
                pos.macd_line = macd_result.macd

            indicators = latest_macd_rsi(closes)
            pos.latest_rsi = _round_or_none(indicators.get("rsi"), 2)
            option_ma20 = (
                indicators_agent.ema(ctx=ind_ctx, closes=closes, period=OPTION_ENTRY_MA_FAST)[-1]
                if len(closes) >= OPTION_ENTRY_MA_FAST
                else None
            )
            pos.option_ma20 = _round_or_none(option_ma20, 2)
            pos.above_option_ma20 = bool(option_ma20 is not None and latest_close >= option_ma20)
            bars_open = self._bars_since_entry(candles, pos.entered_at)
            return_pct = pos.return_pct

            # ── #1 HARD STOP at -25% — fires anytime, no hold gate ──
            if return_pct <= -EXIT.hard_stop_pct:
                await self._close_position(runtime, pos, latest_close, "hard_stop", qty=pos.qty)
                continue

            # ── #5 target_50pct partial — half off at +50%, runner stays ──
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

            # ── #6 trail activation @ +60% — computes pos.trailing_stop for
            # visibility; trail no longer EXITS the position (that role moves
            # to the opposite MACD zero-cross). Kept so the UI can show the
            # ratcheting floor on the runner.
            if pos.phase in (self.PHASE_2, self.PHASE_TRAILING) and return_pct >= EXIT.trail_activation_pct:
                pos.phase = self.PHASE_TRAILING
                pct_floor = pos.peak_price * (1.0 - EXIT.trail_drawdown_pct / 100.0)
                premium_atr = _atr_from_closes(closes) if closes else None
                atr_floor = (
                    pos.peak_price - (premium_atr * EXIT.trail_atr_multiplier)
                    if premium_atr and premium_atr > 0
                    else None
                )
                new_stop = max(pct_floor, atr_floor) if atr_floor is not None else pct_floor
                if pos.trailing_stop is None or new_stop > pos.trailing_stop:
                    pos.trailing_stop = _round_or_none(new_stop, 2)

            # ── macd_reversal_30m — exit on the OPTION PREMIUM MACD rolling over ──
            # MACD here is on the option's OWN premium, and entries buy on the
            # premium MACD crossing UP through zero (for BOTH CE and PE — see
            # strategy_agent_entries). So the symmetric exit for BOTH sides is the
            # premium MACD crossing DOWN through zero (prev ≥ 0 and curr < 0): the
            # premium's momentum has rolled over. The PE branch previously used an
            # UP-cross, which mirrored the (now-fixed) inverted PE entry and would
            # have made a corrected PE exit on its own entry condition.
            # macd_line[-1] reflects the in-flight bar's close (live LTP) so this
            # fires INTRA-bar, no wait for bar close. 300m hold is a guideline only.
            if pos.macd_line and len(pos.macd_line) >= 2:
                prev_macd = pos.macd_line[-2]
                curr_macd = pos.macd_line[-1]
                opposite_cross = (
                    prev_macd is not None and curr_macd is not None
                    and prev_macd >= 0 and curr_macd < 0
                )
                if opposite_cross:
                    await self._close_position(runtime, pos, latest_close, "macd_reversal_30m", qty=pos.qty)
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
            if not self._watchlist_side_matches_position(pos, row, opt):
                continue
            ltp = opt.get("ltp") if isinstance(opt, dict) else None
            if ltp:
                try:
                    observed_at = _parse_iso_timestamp(
                        opt.get("as_of")
                        or opt.get("time")
                        or row.get("as_of")
                        or row.get("time")
                    )
                    current_at = _parse_iso_timestamp(pos.price_updated_at)
                    if current_at is not None and (observed_at is None or observed_at < current_at):
                        continue
                    price = float(ltp)
                    if price > 0:
                        pos.current_price = price
                        pos.price_updated_at = observed_at.isoformat() if observed_at else now_str
                        if price > pos.peak_price:
                            pos.peak_price = price
                except (TypeError, ValueError):
                    pass
        latest_prices = {symbol: position.current_price for symbol, position in runtime.positions.items()}
        if latest_prices:
            runtime.portfolio.update_prices(latest_prices)

    @staticmethod
    def _watchlist_side_matches_position(
        pos: StrategyPosition,
        row: dict[str, object],
        opt: object,
    ) -> bool:
        if not isinstance(opt, dict):
            return False
        opt_key = str(opt.get("instrument_key") or "").strip()
        if pos.instrument_key and opt_key and opt_key == pos.instrument_key:
            return True
        expiry = str(opt.get("expiry") or row.get("expiry") or "").strip()
        option_type = str(opt.get("option_type") or "").upper().strip()
        if not option_type:
            option_type = "CE" if opt is row.get("ce") else "PE" if opt is row.get("pe") else ""
        try:
            strike = float(opt.get("strike") or row.get("atm_strike") or 0.0)
        except (TypeError, ValueError):
            strike = 0.0
        return bool(
            expiry == pos.expiry
            and option_type == pos.option_type
            and strike > 0
            and abs(strike - float(pos.strike)) < 0.01
        )

    async def _latest_position_quote_map(
        self: "PaperStrategyAgent",
        positions: list[StrategyPosition],
    ) -> dict[str, tuple[float, str]]:
        """Return freshest contract-level mark rows for open paper positions."""
        quotes: dict[str, tuple[float, str]] = {}
        if not positions:
            return quotes
        try:
            async with AsyncSessionLocal() as session:
                for pos in positions:
                    row = (
                        await session.execute(
                            text(
                                """
                                SELECT price, time
                                FROM (
                                    SELECT ltp::float8 AS price, time
                                    FROM atm_option_watchlist_snapshots
                                    WHERE underlying = :underlying
                                      AND expiry = :expiry
                                      AND strike = :strike
                                      AND option_type = :option_type
                                      AND ltp IS NOT NULL
                                    UNION ALL
                                    SELECT close::float8 AS price, time
                                    FROM option_premium_candles
                                    WHERE underlying = :underlying
                                      AND expiry = :expiry
                                      AND strike = :strike
                                      AND option_type = :option_type
                                      AND close IS NOT NULL
                                ) marks
                                ORDER BY time DESC
                                LIMIT 1
                                """
                            ),
                            {
                                "underlying": pos.underlying,
                                "expiry": date.fromisoformat(pos.expiry),
                                "strike": pos.strike,
                                "option_type": pos.option_type,
                            },
                        )
                    ).mappings().first()
                    if not row:
                        continue
                    try:
                        ltp = float(row.get("price") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if ltp <= 0:
                        continue
                    observed_at = row.get("time")
                    if isinstance(observed_at, datetime):
                        observed = observed_at.astimezone(IST).isoformat()
                    else:
                        observed = _parse_iso_timestamp(observed_at)
                        observed = observed.isoformat() if observed else _now_ist().isoformat()
                    quotes[pos.symbol] = (ltp, observed)
        except Exception:
            return quotes
        return quotes

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

        from agentic_rag.trade_memory import build_strategy_trade_case, record_trade_case

        trade_case = build_strategy_trade_case(
            runtime_key=runtime.key,
            runtime_label=runtime.label,
            position=position,
            exit_price=exit_price,
            reason=reason,
            close_qty=close_qty,
            pnl=pnl,
            partial=partial,
            source=f"paper_strategy_agent:{runtime.key}",
        )
        await record_trade_case(trade_case)
