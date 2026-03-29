"""Deterministic paper-trading agent driven by 30-minute option research data."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import httpx
import pandas as pd
from loguru import logger

from analysis.instruments import get_monthly_expiry
from analysis.macd_engine import compute_macd
from analytics.greeks_sync import compute_greeks_sync_frame
from analytics.technicals import latest_macd_rsi
from core.config import settings
from market_data import atm_watchlist_service, option_history_service
from paper_engine.order_book import PaperOrderBook
from paper_engine.portfolio import PaperPortfolio

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


def _in_market_hours(now: Optional[datetime] = None) -> bool:
    current = now or _now_ist()
    if current.weekday() >= 5:
        return False
    open_time = time(9, 15)
    close_time = time(15, 30)
    return open_time <= current.time() <= close_time


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric == numeric or numeric in (float("inf"), float("-inf")):
        return None
    return round(numeric, digits)


def _contract_symbol(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"OPT:{underlying}:{expiry}:{int(round(strike))}:{option_type}"


def _report_interval_seconds(value: str) -> int:
    mapping = {
        "30m": 30 * 60,
        "1h": 60 * 60,
        "4h": 4 * 60 * 60,
        "daily": 24 * 60 * 60,
    }
    return mapping.get(str(value or "1h"), 60 * 60)


def detect_macd_zero_cross(closes: list[float]) -> tuple[bool, Optional[float], Optional[str]]:
    if len(closes) < 35:
        return False, None, None
    macd_line, _, _ = compute_macd(closes)
    current = macd_line[-1]
    previous = macd_line[-2]
    if current is None or previous is None:
        return False, None, None
    should_enter = previous <= 0 < current
    return should_enter, float(current), "macd_zero_cross" if should_enter else None


def detect_greeks_signal(
    candles: list[dict[str, Any]],
    option_type: str,
) -> tuple[bool, Optional[float], Optional[str]]:
    if len(candles) < 20:
        return False, None, None
    frame = pd.DataFrame(candles)
    if frame.empty:
        return False, None, None
    scored = compute_greeks_sync_frame(frame, option_type)
    latest = scored.iloc[-1]
    if bool(latest.get("greeks_sync_signal")):
        return True, float(latest.get("greeks_sync_score", 0.0)), "greeks_sync_signal"
    return False, float(latest.get("greeks_sync_score", 0.0)), None


@dataclass
class StrategyPosition:
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    instrument_key: Optional[str]
    trading_symbol: Optional[str]
    qty: int
    entry_price: float
    current_price: float
    peak_price: float
    entry_bar_time: str
    entered_at: str
    signal_reason: str
    signal_strength: Optional[float] = None
    latest_rsi: Optional[float] = None
    trailing_active: bool = False
    trailing_stop: Optional[float] = None

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((self.current_price - self.entry_price) / self.entry_price) * 100.0


@dataclass
class StrategyEvent:
    time: str
    event: str
    symbol: str
    underlying: str
    option_type: str
    strike: float
    price: float
    qty: int
    reason: str
    signal_strength: Optional[float] = None
    pnl: Optional[float] = None


@dataclass
class CommentaryEntry:
    time: str
    scope: str
    tone: str
    message: str


@dataclass
class StrategyRuntime:
    key: str
    label: str
    portfolio: PaperPortfolio
    order_book: PaperOrderBook
    positions: dict[str, StrategyPosition] = field(default_factory=dict)
    processed_signals: dict[str, str] = field(default_factory=dict)
    recent_events: list[StrategyEvent] = field(default_factory=list)
    entries: int = 0
    exits: int = 0
    last_scan_at: Optional[str] = None
    last_message: Optional[str] = None


class PaperStrategyAgent:
    scan_interval_seconds = 60
    capital_per_trade_pct = 0.05
    max_lots_per_trade = 5
    max_positions_per_strategy = 10
    trailing_activation_pct = 20.0
    trailing_drawdown_pct = 10.0

    def __init__(self) -> None:
        self._strategies = {
            "macd_zero_cross": self._build_runtime("macd_zero_cross", "MACD Zero Cross"),
            "greeks_sync": self._build_runtime("greeks_sync", "Greeks Sync"),
        }
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._enabled = True
        self._running = False
        self._last_run_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_message: str = "Waiting for first strategy scan."
        self._last_expiry: Optional[str] = None
        self._telegram_last_sent_at: Optional[datetime] = None
        self._commentary: list[CommentaryEntry] = []

    def _build_runtime(self, key: str, label: str) -> StrategyRuntime:
        session_id = f"{key}-paper"
        portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
        order_book = PaperOrderBook(on_fill=portfolio.on_fill)
        return StrategyRuntime(
            key=key,
            label=label,
            portfolio=portfolio,
            order_book=order_book,
        )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._enabled = True
        self._task = asyncio.create_task(self._loop(), name="paper-strategy-agent")

    async def stop(self) -> None:
        self._enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Paper strategy agent error: {exc}"
                logger.exception("[Paper agent] loop failure")
            await asyncio.sleep(self.scan_interval_seconds)

    def _target_monthly_expiry(self, today: date) -> str:
        expiry = get_monthly_expiry(today.year, today.month)
        if today > expiry:
            next_month = today.replace(day=28) + timedelta(days=4)
            expiry = get_monthly_expiry(next_month.year, next_month.month)
        return expiry.isoformat()

    def _humanize_reason(self, reason: Optional[str]) -> str:
        mapping = {
            "macd_zero_cross": "MACD crossed above the zero line on the latest 30-minute bar.",
            "greeks_sync_signal": "delta, gamma and volatility lined up strongly enough to trigger the Greeks sync entry.",
            "rsi_above_80": "RSI moved above 80, so the agent locked gains instead of waiting for mean reversion.",
            "trailing_stoploss": "price slipped back through the trailing stop after the position had already moved in favour.",
        }
        if not reason:
            return "the rule set was satisfied."
        return mapping.get(reason, reason.replace("_", " "))

    def _append_commentary(self, scope: str, message: str, tone: str = "info") -> None:
        if not message:
            return
        previous = self._commentary[0] if self._commentary else None
        if previous and previous.scope == scope and previous.message == message and previous.tone == tone:
            return
        self._commentary.insert(
            0,
            CommentaryEntry(
                time=_now_ist().isoformat(),
                scope=scope,
                tone=tone,
                message=message,
            ),
        )
        del self._commentary[40:]

    async def _send_telegram_text(self, message: str) -> None:
        if not settings.TELEGRAM_REPORTS_ENABLED:
            return
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": settings.TELEGRAM_CHAT_ID,
                        "text": message,
                    },
                )
                response.raise_for_status()
        except Exception as exc:
            logger.warning(f"[Paper agent] Telegram send failed: {exc}")

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if self._lock.locked() and not force:
            return self.get_status()

        async with self._lock:
            self._running = True
            started_at = _now_ist()
            self._last_error = None
            try:
                if not force and not _in_market_hours(started_at):
                    self._last_message = "Waiting for NSE market hours."
                    self._append_commentary(
                        "System",
                        "The autonomous paper agent is idle because NSE market hours have not started yet.",
                        tone="idle",
                    )
                    return self.get_status()

                expiry = self._target_monthly_expiry(started_at.date())
                self._last_expiry = expiry
                watchlist = await atm_watchlist_service.get_watchlist(expiry)
                rows = list(watchlist.get("rows") or [])
                if not rows:
                    self._last_message = "ATM watchlist is empty for the active monthly expiry."
                    self._append_commentary(
                        "System",
                        f"The agent could not find any ATM watchlist rows for monthly expiry {expiry}, so it skipped trading for this scan.",
                        tone="warning",
                    )
                    return self.get_status()

                self._append_commentary(
                    "System",
                    f"Market is open. The agent is scanning {len(rows)} ATM watchlist rows for monthly expiry {expiry}.",
                    tone="info",
                )
                for runtime in self._strategies.values():
                    await self._process_strategy(runtime, rows)
                await self._maybe_send_telegram_report()

                self._last_run_at = _now_ist().isoformat()
                self._last_message = f"Processed {len(rows)} watchlist rows for {expiry}."
                self._append_commentary(
                    "System",
                    f"Completed the latest autonomous pass across {len(rows)} rows. Open positions now total {sum(len(runtime.positions) for runtime in self._strategies.values())}.",
                    tone="success",
                )
                return self.get_status()
            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Paper strategy agent failed: {exc}"
                self._append_commentary(
                    "System",
                    f"The autonomous paper agent hit an error and paused this scan: {exc}",
                    tone="error",
                )
                raise
            finally:
                self._running = False

    async def _process_strategy(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
    ) -> None:
        runtime.last_scan_at = _now_ist().isoformat()
        await self._update_existing_positions(runtime)

        capacity = self.max_positions_per_strategy - len(runtime.positions)
        if capacity <= 0:
            runtime.last_message = "Position cap reached."
            self._append_commentary(
                runtime.label,
                f"{runtime.label} is holding the maximum allowed positions, so it is only managing exits for now.",
                tone="warning",
            )
            return

        candidates: list[dict[str, Any]] = []
        reviewed_sides = 0
        missing_history = 0
        for row in rows:
            for side_key in ("ce", "pe"):
                side = row.get(side_key)
                if not side:
                    continue
                if self._has_underlying_side_position(runtime, row["underlying"], side["option_type"]):
                    continue
                reviewed_sides += 1
                candles = await option_history_service.load_candles(
                    underlying=row["underlying"],
                    expiry=date.fromisoformat(row["expiry"]),
                    strike=float(side["strike"]),
                    option_type=str(side["option_type"]),
                    instrument_key=side.get("instrument_key"),
                    interval="30minute",
                    limit=80,
                )
                if len(candles) < 35:
                    missing_history += 1
                    continue
                closes = [float(item["close"]) for item in candles if item.get("close") is not None]
                if len(closes) < 35:
                    missing_history += 1
                    continue

                if runtime.key == "macd_zero_cross":
                    should_enter, strength, reason = detect_macd_zero_cross(closes)
                else:
                    should_enter, strength, reason = detect_greeks_signal(candles, str(side["option_type"]))
                if not should_enter or not reason:
                    continue

                latest_bar_time = str(candles[-1]["time"])
                signal_key = f"{row['underlying']}:{side['option_type']}"
                if runtime.processed_signals.get(signal_key) == latest_bar_time:
                    continue

                indicators = latest_macd_rsi(closes)
                candidates.append(
                    {
                        "row": row,
                        "side": side,
                        "candles": candles,
                        "latest_close": closes[-1],
                        "latest_bar_time": latest_bar_time,
                        "signal_key": signal_key,
                        "strength": strength or 0.0,
                        "reason": reason,
                        "rsi": indicators.get("rsi"),
                    }
                )

        candidates.sort(key=lambda item: float(item["strength"]), reverse=True)
        opened = 0
        for candidate in candidates[:capacity]:
            await self._open_position(runtime, candidate)
            opened += 1

        if candidates:
            top = candidates[0]
            top_side = top["side"]
            top_message = (
                f"{runtime.label} reviewed {reviewed_sides} option sides, skipped {missing_history} for shallow history, "
                f"and shortlisted {len(candidates)} setups. The strongest setup was {top['row']['underlying']} "
                f"{top_side['option_type']} {int(round(float(top_side['strike'])))} because {self._humanize_reason(str(top['reason']))} "
                f"Signal strength was {float(top['strength']):.2f} and RSI was {float(top['rsi'] or 0):.1f}."
            )
            self._append_commentary(runtime.label, top_message, tone="info")
        else:
            self._append_commentary(
                runtime.label,
                f"{runtime.label} reviewed {reviewed_sides} option sides and found no fresh entry that met the rules on the latest 30-minute bar.",
                tone="idle",
            )

        runtime.last_message = (
            f"Opened {opened} new trades and now holds {len(runtime.positions)} open positions."
            if runtime.positions or opened
            else "No open positions and no fresh entry signals."
        )

    def _has_underlying_side_position(self, runtime: StrategyRuntime, underlying: str, option_type: str) -> bool:
        return any(
            position.underlying == underlying and position.option_type == option_type
            for position in runtime.positions.values()
        )

    async def _open_position(self, runtime: StrategyRuntime, candidate: dict[str, Any]) -> None:
        row = candidate["row"]
        side = candidate["side"]
        latest_close = float(candidate["latest_close"])
        if latest_close <= 0:
            return

        expiry = row["expiry"]
        strike = float(side["strike"])
        option_type = str(side["option_type"])
        symbol = _contract_symbol(row["underlying"], expiry, strike, option_type)
        lot_size = await option_history_service.resolve_lot_size(
            underlying=row["underlying"],
            expiry=date.fromisoformat(expiry),
            strike=strike,
            option_type=option_type,
            instrument_key=side.get("instrument_key"),
        )
        lot_size = lot_size or PaperPortfolio.DEFAULT_LOT_SIZE
        allocation = max(runtime.portfolio.total_equity * self.capital_per_trade_pct, latest_close * lot_size)
        lots = max(1, int(allocation // max(latest_close * lot_size, 1.0)))
        lots = min(lots, self.max_lots_per_trade)
        qty = lot_size * lots

        order = runtime.order_book.place_order(
            symbol=symbol,
            action="BUY",
            order_type="MARKET",
            qty=qty,
            instrument_type=option_type,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            ltp=latest_close,
        )
        runtime.positions[symbol] = StrategyPosition(
            symbol=symbol,
            underlying=row["underlying"],
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=side.get("instrument_key"),
            trading_symbol=side.get("trading_symbol"),
            qty=qty,
            entry_price=float(order.fill_price or latest_close),
            current_price=float(order.fill_price or latest_close),
            peak_price=float(order.fill_price or latest_close),
            entry_bar_time=str(candidate["latest_bar_time"]),
            entered_at=_now_ist().isoformat(),
            signal_reason=str(candidate["reason"]),
            signal_strength=_round_or_none(float(candidate["strength"]), 2),
            latest_rsi=_round_or_none(candidate.get("rsi"), 2),
        )
        runtime.entries += 1
        runtime.processed_signals[candidate["signal_key"]] = str(candidate["latest_bar_time"])
        self._append_event(
            runtime,
            StrategyEvent(
                time=_now_ist().isoformat(),
                event="entry",
                symbol=symbol,
                underlying=row["underlying"],
                option_type=option_type,
                strike=strike,
                price=float(order.fill_price or latest_close),
                qty=qty,
                reason=str(candidate["reason"]),
                signal_strength=_round_or_none(float(candidate["strength"]), 2),
            ),
        )
        reasoning = self._humanize_reason(str(candidate["reason"]))
        self._append_commentary(
            runtime.label,
            f"{runtime.label} bought {row['underlying']} {option_type} {int(round(strike))} at {float(order.fill_price or latest_close):.2f}. "
            f"It chose this trade because {reasoning} Position size was {qty} and RSI was {float(candidate.get('rsi') or 0):.1f}.",
            tone="trade",
        )
        await self._send_telegram_text(
            "\n".join(
                [
                    f"Nomad Curie | {runtime.label} ENTRY",
                    f"Contract: {row['underlying']} {option_type} {int(round(strike))} | Expiry {expiry}",
                    f"Qty: {qty} | Entry: {float(order.fill_price or latest_close):.2f}",
                    f"Logic: {reasoning}",
                    f"Signal strength: {float(candidate['strength']):.2f} | RSI: {float(candidate.get('rsi') or 0):.1f}",
                ]
            )
        )

    async def _update_existing_positions(self, runtime: StrategyRuntime) -> None:
        if not runtime.positions:
            return

        latest_prices: dict[str, float] = {}
        managed_positions = 0
        for symbol, position in list(runtime.positions.items()):
            managed_positions += 1
            candles = await option_history_service.load_candles(
                underlying=position.underlying,
                expiry=date.fromisoformat(position.expiry),
                strike=position.strike,
                option_type=position.option_type,
                instrument_key=position.instrument_key,
                interval="30minute",
                limit=80,
            )
            if not candles:
                continue
            closes = [float(item["close"]) for item in candles if item.get("close") is not None]
            if not closes:
                continue
            latest_close = float(closes[-1])
            latest_prices[symbol] = latest_close
            position.current_price = latest_close
            position.peak_price = max(position.peak_price, latest_close)
            indicators = latest_macd_rsi(closes)
            position.latest_rsi = _round_or_none(indicators.get("rsi"), 2)

            activation_price = position.entry_price * (1.0 + self.trailing_activation_pct / 100.0)
            if position.peak_price >= activation_price:
                position.trailing_active = True
                position.trailing_stop = _round_or_none(
                    position.peak_price * (1.0 - self.trailing_drawdown_pct / 100.0),
                    2,
                )

            if position.latest_rsi is not None and position.latest_rsi >= 80.0:
                await self._close_position(runtime, position, latest_close, "rsi_above_80")
                continue
            if position.trailing_active and position.trailing_stop is not None and latest_close <= position.trailing_stop:
                await self._close_position(runtime, position, latest_close, "trailing_stoploss")

        runtime.portfolio.update_prices(latest_prices)
        if managed_positions and runtime.positions:
            self._append_commentary(
                runtime.label,
                f"{runtime.label} is managing {len(runtime.positions)} live positions and trailing stops are being refreshed from the latest 30-minute closes.",
                tone="info",
            )

    async def _close_position(
        self,
        runtime: StrategyRuntime,
        position: StrategyPosition,
        exit_price: float,
        reason: str,
    ) -> None:
        if position.symbol not in runtime.positions:
            return
        runtime.order_book.place_order(
            symbol=position.symbol,
            action="SELL",
            order_type="MARKET",
            qty=position.qty,
            instrument_type=position.option_type,
            expiry=position.expiry,
            strike=position.strike,
            option_type=position.option_type,
            ltp=exit_price,
        )
        pnl = (exit_price - position.entry_price) * position.qty
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
                qty=position.qty,
                reason=reason,
                signal_strength=position.signal_strength,
                pnl=_round_or_none(pnl, 2),
            ),
        )
        reasoning = self._humanize_reason(reason)
        self._append_commentary(
            runtime.label,
            f"{runtime.label} exited {position.underlying} {position.option_type} {int(round(position.strike))} at {exit_price:.2f}. "
            f"The exit was triggered because {reasoning} Realized PnL was {pnl:.2f}.",
            tone="trade",
        )
        await self._send_telegram_text(
            "\n".join(
                [
                    f"Nomad Curie | {runtime.label} EXIT",
                    f"Contract: {position.underlying} {position.option_type} {int(round(position.strike))} | Expiry {position.expiry}",
                    f"Qty: {position.qty} | Exit: {exit_price:.2f} | PnL: {pnl:.2f}",
                    f"Logic: {reasoning}",
                ]
            )
        )

    def _append_event(self, runtime: StrategyRuntime, event: StrategyEvent) -> None:
        runtime.recent_events.insert(0, event)
        del runtime.recent_events[12:]

    async def _maybe_send_telegram_report(self) -> None:
        if not settings.TELEGRAM_REPORTS_ENABLED:
            return
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return
        now = _now_ist()
        interval_seconds = _report_interval_seconds(settings.TELEGRAM_REPORT_INTERVAL)
        if self._telegram_last_sent_at and (now - self._telegram_last_sent_at).total_seconds() < interval_seconds:
            return

        message_lines = [
            f"Nomad Curie paper strategies | {now.strftime('%d %b %Y %I:%M %p IST')}",
            f"Monthly expiry: {self._last_expiry or '--'}",
        ]
        for runtime in self._strategies.values():
            summary = runtime.portfolio.get_summary()
            message_lines.extend(
                [
                    "",
                    runtime.label,
                    f"Equity: Rs {summary.get('total_equity') or 0}",
                    f"Realized PnL: Rs {summary.get('realized_pnl') or 0}",
                    f"Unrealized PnL: Rs {summary.get('unrealized_pnl') or 0}",
                    f"Trades: {summary.get('total_trades') or 0} | Open: {len(runtime.positions)}",
                ]
            )

        try:
            await self._send_telegram_text("\n".join(message_lines))
            self._telegram_last_sent_at = now
        except Exception as exc:
            logger.warning(f"[Paper agent] Telegram report failed: {exc}")

    def get_status(self) -> dict[str, Any]:
        strategies = []
        for runtime in self._strategies.values():
            summary = runtime.portfolio.get_summary()
            strategies.append(
                {
                    "key": runtime.key,
                    "label": runtime.label,
                    "summary": {
                        **summary,
                        "open_positions": len(runtime.positions),
                        "entries": runtime.entries,
                        "exits": runtime.exits,
                    },
                    "positions": [
                        {
                            **asdict(position),
                            "unrealized_pnl": _round_or_none(position.unrealized_pnl, 2),
                            "return_pct": _round_or_none(position.return_pct, 2),
                        }
                        for position in runtime.positions.values()
                    ],
                    "recent_events": [asdict(event) for event in runtime.recent_events],
                    "trade_history": [
                        {
                            "symbol": trade.symbol,
                            "action": trade.action,
                            "qty": trade.qty,
                            "entry_price": _round_or_none(trade.entry_price, 2),
                            "exit_price": _round_or_none(trade.exit_price, 2),
                            "pnl": _round_or_none(trade.pnl, 2),
                            "entry_time": trade.entry_time.isoformat(),
                            "exit_time": trade.exit_time.isoformat(),
                            "instrument_type": trade.instrument_type,
                            "expiry": trade.expiry,
                            "strike": trade.strike,
                            "option_type": trade.option_type,
                        }
                        for trade in reversed(runtime.portfolio._trade_history[-20:])
                    ],
                    "last_scan_at": runtime.last_scan_at,
                    "last_message": runtime.last_message,
                }
            )

        return {
            "enabled": self._enabled,
            "running": self._running,
            "scan_interval_seconds": self.scan_interval_seconds,
            "last_run_at": self._last_run_at,
            "last_error": self._last_error,
            "last_message": self._last_message,
            "target_expiry": self._last_expiry,
            "next_scan_at": (
                (_now_ist() + timedelta(seconds=self.scan_interval_seconds)).isoformat()
                if self._running or self._last_run_at is None
                else (
                    datetime.fromisoformat(self._last_run_at) + timedelta(seconds=self.scan_interval_seconds)
                ).isoformat()
            ),
            "telegram": {
                "enabled": settings.TELEGRAM_REPORTS_ENABLED,
                "report_interval": settings.TELEGRAM_REPORT_INTERVAL,
                "configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
                "last_sent_at": self._telegram_last_sent_at.isoformat() if self._telegram_last_sent_at else None,
            },
            "commentary": [asdict(entry) for entry in self._commentary],
            "strategies": strategies,
        }


paper_strategy_agent = PaperStrategyAgent()
