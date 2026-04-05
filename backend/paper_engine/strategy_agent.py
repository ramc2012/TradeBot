"""Deterministic paper-trading agent driven by STRATEGY_DOCUMENT.md.

Implements the full MACD zero-cross strategy on 30-minute ATM option
premium candles with:
- Physical-delivery trading window (prev_expiry−7 to current_expiry−7)
- MACD quadrant regime filter (bullish/bearish/dead zone)
- Layered exit management (target +50%, runner, trail, hard stop)
- Spot MA context classification (breakout/trend/reversal)
- IV filtering and Kelly-based position sizing
- MACD death signal exit
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import httpx
import pandas as pd
from loguru import logger

from analysis.macd_engine import (
    compute_macd,
    compute_spot_ma_context,
    check_iv_filter,
)
from agent.macd_quadrant import (
    QuadrantResult,
    compute_quadrant,
    check_macd_death_signal,
)
from agent.strategy_config import (
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    MACD_MIN_BARS,
    MIN_TTE_DAYS,
    MIN_PREMIUM,
    MAX_PREMIUM,
    MAX_ENTRY_IV_PCT,
    HARD_MAX_IV_PCT,
    MIN_CANDLE_BARS,
    KELLY_FRACTION,
    KELLY_PREMIUM_FRACTION,
    KELLY_CAUTIOUS_FRACTION,
    MAX_SIMULTANEOUS_POSITIONS,
    MAX_PER_UNDERLYING,
    CASH_RESERVE_PCT,
    EXIT,
    REGIME_BULLISH,
    REGIME_BEARISH,
    REGIME_DEAD,
    SETUP_BREAKOUT,
    SETUP_PREMIUM,
    SPOT_MA_FAST,
    SPOT_MA_SLOW,
    EXCLUDED_UNDERLYINGS,
    COMMENTARY_MAX,
)
from agent.window_calculator import (
    get_all_active_windows,
    days_remaining_in_window,
)
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
    return time(9, 15) <= current.time() <= time(15, 30)


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    return round(numeric, digits)


def _contract_symbol(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"OPT:{underlying}:{expiry}:{int(round(strike))}:{option_type}"


def _report_interval_seconds(value: str) -> int:
    mapping = {"30m": 1800, "1h": 3600, "4h": 14400, "daily": 86400}
    return mapping.get(str(value or "1h"), 3600)


def detect_macd_zero_cross(closes: list[float], option_type: str = "CE") -> tuple[bool, Optional[float], Optional[str]]:
    """Detect MACD zero-line crossover on option premium closes.

    CE: MACD crosses from ≤0 to >0 (bullish)
    PE: MACD crosses from ≥0 to <0 (bearish — put premium rising)
    """
    if len(closes) < MACD_MIN_BARS:
        return False, None, None
    macd_line, _, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    current = macd_line[-1]
    previous = macd_line[-2]
    if current is None or previous is None:
        return False, None, None

    if option_type == "CE":
        should_enter = previous <= 0 < current
    else:
        should_enter = previous >= 0 > current

    return should_enter, float(current), "macd_zero_cross" if should_enter else None


def detect_greeks_signal(
    candles: list[dict[str, Any]],
    option_type: str = "CE",
) -> tuple[bool, Optional[float], Optional[str]]:
    """Backward-compatible composite signal used by legacy tests.

    The runtime strategy is MACD-led now, but a lightweight Greeks sync helper
    keeps the historical test contract intact for supportive premium/Greeks
    breakouts.
    """
    if len(candles) < 12:
        return False, None, None

    series = candles[-12:]
    latest = series[-1]
    previous = series[-2]
    baseline = series[:-1]

    def _num(row: dict[str, Any], key: str, *, absolute: bool = False) -> Optional[float]:
        value = row.get(key)
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if absolute:
            numeric = abs(numeric)
        return numeric if numeric == numeric else None

    latest_close = _num(latest, "close")
    previous_close = _num(previous, "close")
    latest_iv = _num(latest, "iv")
    latest_delta = _num(latest, "delta", absolute=True)
    previous_delta = _num(previous, "delta", absolute=True)
    latest_gamma = _num(latest, "gamma", absolute=True)
    latest_vega = _num(latest, "vega", absolute=True)
    latest_underlying = _num(latest, "underlying_price")
    previous_underlying = _num(previous, "underlying_price")

    if None in {
        latest_close,
        previous_close,
        latest_iv,
        latest_delta,
        previous_delta,
        latest_gamma,
        latest_vega,
        latest_underlying,
        previous_underlying,
    }:
        return False, None, None

    baseline_highs = [_num(row, "high") for row in baseline]
    baseline_closes = [_num(row, "close") for row in baseline]
    baseline_ivs = [_num(row, "iv") for row in baseline]
    baseline_gammas = [_num(row, "gamma", absolute=True) for row in baseline]
    baseline_vegas = [_num(row, "vega", absolute=True) for row in baseline]

    if any(value is None for value in baseline_highs + baseline_closes + baseline_ivs):
        return False, None, None

    breakout_high = max(value for value in baseline_highs if value is not None)
    mean_iv = sum(value for value in baseline_ivs if value is not None) / max(len(baseline_ivs), 1)
    mean_gamma = sum(value for value in baseline_gammas if value is not None) / max(len(baseline_gammas), 1)
    mean_vega = sum(value for value in baseline_vegas if value is not None) / max(len(baseline_vegas), 1)

    score = 0.0

    if latest_close > breakout_high and latest_close > previous_close:
        score += 35.0
    elif latest_close > previous_close:
        score += 20.0

    if latest_iv >= mean_iv * 1.1:
        score += 15.0

    if latest_delta >= max(previous_delta + 0.05, 0.45):
        score += 20.0

    if latest_gamma >= max(mean_gamma * 2.0, 0.01):
        score += 15.0

    if latest_vega >= max(mean_vega * 1.5, 10.0):
        score += 10.0

    if option_type == "CE":
        if latest_underlying > previous_underlying:
            score += 5.0
    elif latest_underlying < previous_underlying:
        score += 5.0

    strength = round(score, 2)
    should_enter = strength >= 70.0
    return should_enter, strength, "greeks_sync_signal" if should_enter else None


# ── Position Phases ──────────────────────────────────────────────────────────

PHASE_1 = "phase1"           # full position, awaiting target +50%
PHASE_2 = "phase2"           # half exited at target, runner held
PHASE_TRAILING = "trailing"  # runner with active trail after +100%
PHASE_EXITED = "exited"


@dataclass
class StrategyPosition:
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    instrument_key: Optional[str]
    trading_symbol: Optional[str]
    qty: int                    # current qty (may decrease on partial exit)
    initial_qty: int            # qty at entry
    entry_price: float
    current_price: float
    peak_price: float
    entry_bar_time: str
    entered_at: str
    signal_reason: str
    signal_strength: Optional[float] = None
    latest_rsi: Optional[float] = None
    phase: str = PHASE_1
    trailing_stop: Optional[float] = None
    entry_iv_pct: Optional[float] = None
    spot_setup: Optional[str] = None
    window_end: Optional[str] = None    # ISO date string for exit deadline
    macd_line: Optional[list] = field(default=None, repr=False)

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
    phase: Optional[str] = None


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
    """Autonomous paper-trading agent implementing STRATEGY_DOCUMENT.md."""

    scan_interval_seconds = 60
    max_positions = MAX_SIMULTANEOUS_POSITIONS

    def __init__(self) -> None:
        self._strategy = self._build_runtime("macd_strategy", "MACD Zero-Cross Strategy")
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._enabled = True
        self._auto_run_enabled = False
        self._kill_switch_active = False
        self._running = False
        self._last_run_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_message: str = "Waiting for first strategy scan."
        self._last_expiry: Optional[str] = None
        self._last_candidate_expiries: list[str] = []
        self._telegram_last_sent_at: Optional[datetime] = None
        self._commentary: list[CommentaryEntry] = []
        self._active_windows: list[dict] = []
        self._regime_cache: dict[str, QuadrantResult] = {}

    def _build_runtime(self, key: str, label: str) -> StrategyRuntime:
        session_id = f"{key}-paper"
        portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
        order_book = PaperOrderBook(on_fill=portfolio.on_fill)
        return StrategyRuntime(key=key, label=label, portfolio=portfolio, order_book=order_book)

    def _select_candidate_expiries(self, as_of: date, expiries: list[str]) -> list[str]:
        """Legacy expiry chooser retained for compatibility with older tests."""
        parsed: list[date] = []
        for raw in expiries:
            try:
                expiry = date.fromisoformat(str(raw))
            except ValueError:
                continue
            if expiry >= as_of:
                parsed.append(expiry)

        parsed = sorted(set(parsed))
        if not parsed:
            return []

        selected = [parsed[0]]
        if len(parsed) > 1 and (parsed[0] - as_of).days <= 3:
            selected.append(parsed[1])
        return [item.isoformat() for item in selected]

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._enabled = True
        if not self._auto_run_enabled or (self._task and not self._task.done()):
            return
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
                logger.exception("[Strategy] loop failure")
            await asyncio.sleep(self.scan_interval_seconds)

    # ── Main Scan ────────────────────────────────────────────────────────────

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
                    self._append_commentary("System", "Market closed. Agent idle.", tone="idle")
                    return self.get_status()

                # 1. Get active trading windows
                self._active_windows = await get_all_active_windows(as_of=started_at.date())
                if not self._active_windows:
                    self._last_message = "No active trading windows."
                    self._append_commentary("System", "No active prev_expiry−7 to current_expiry−7 windows found.", tone="warning")
                    return self.get_status()

                self._last_candidate_expiries = list({
                    str(w["expiry"]) for w in self._active_windows
                })
                self._last_expiry = self._last_candidate_expiries[0] if self._last_candidate_expiries else None

                # 2. Get ATM watchlist rows for each active expiry
                expiries = list({str(w["expiry"]) for w in self._active_windows})
                watchlists = await asyncio.gather(
                    *(atm_watchlist_service.get_watchlist(exp) for exp in expiries),
                    return_exceptions=True,
                )
                rows = []
                for wl in watchlists:
                    if isinstance(wl, dict):
                        rows.extend(wl.get("rows") or [])

                if not rows:
                    self._last_message = "ATM watchlist empty for active windows."
                    self._append_commentary("System", "No ATM watchlist data available.", tone="warning")
                    return self.get_status()

                # 3. Build window lookup
                window_map = {w["underlying"]: w for w in self._active_windows}

                # 4. Process: manage exits first, then scan for entries
                runtime = self._strategy
                await self._manage_exits(runtime)
                await self._scan_entries(runtime, rows, window_map)
                await self._maybe_send_telegram_report()

                self._last_run_at = _now_ist().isoformat()
                n_pos = len(runtime.positions)
                self._last_message = (
                    f"Scanned {len(rows)} instruments across {len(expiries)} expiries. "
                    f"{n_pos} open positions."
                )
                self._append_commentary(
                    "System",
                    f"Scan complete. {len(rows)} rows, {n_pos} positions.",
                    tone="success",
                )
                return self.get_status()

            except Exception as exc:
                self._last_error = str(exc)
                self._last_message = f"Agent error: {exc}"
                self._append_commentary("System", f"Error: {exc}", tone="error")
                raise
            finally:
                self._running = False

    # ── Entry Scanning ───────────────────────────────────────────────────────

    async def _scan_entries(
        self,
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        window_map: dict[str, dict],
    ) -> None:
        capacity = self.max_positions - len(runtime.positions)
        if capacity <= 0:
            self._append_commentary(runtime.label, "Position cap reached. Managing exits only.", tone="warning")
            return

        candidates: list[dict[str, Any]] = []

        for row in rows:
            underlying = row.get("underlying", "")
            expiry_str = row.get("expiry", "")

            # Skip excluded underlyings
            if underlying in EXCLUDED_UNDERLYINGS:
                continue

            # Must have an active window
            window = window_map.get(underlying)
            if not window:
                continue

            # Check TTE
            tte = days_remaining_in_window(window, as_of=_now_ist().date())
            if tte < MIN_TTE_DAYS:
                continue

            # Skip if already holding this underlying
            if self._has_underlying_position(runtime, underlying):
                continue

            # Load CE and PE candles for quadrant check
            ce_side = row.get("ce")
            pe_side = row.get("pe")
            if not ce_side or not pe_side:
                continue

            ce_candles = await self._load_candles(row, ce_side)
            pe_candles = await self._load_candles(row, pe_side)

            ce_closes = [float(c["close"]) for c in ce_candles if c.get("close")] if ce_candles else []
            pe_closes = [float(c["close"]) for c in pe_candles if c.get("close")] if pe_candles else []

            # Quadrant regime check
            quadrant = compute_quadrant(
                ce_closes, pe_closes,
                underlying=underlying,
                expiry=expiry_str,
            )
            self._regime_cache[underlying] = quadrant

            # Dead zone = no trade
            if quadrant.regime == REGIME_DEAD:
                continue

            # Determine which side to check based on regime
            if quadrant.regime == REGIME_BULLISH and quadrant.ce_has_zero_cross:
                side = ce_side
                candles = ce_candles
                closes = ce_closes
                opt_type = "CE"
            elif quadrant.regime == REGIME_BEARISH and quadrant.pe_has_zero_cross:
                side = pe_side
                candles = pe_candles
                closes = pe_closes
                opt_type = "PE"
            else:
                # No fresh zero-cross in the correct regime
                continue

            if len(closes) < MACD_MIN_BARS:
                continue

            # Verify the zero-cross on this specific side
            should_enter, strength, reason = detect_macd_zero_cross(closes, opt_type)
            if not should_enter or not reason:
                continue

            # Entry premium filter
            latest_close = closes[-1]
            if latest_close < MIN_PREMIUM or latest_close > MAX_PREMIUM:
                continue

            # IV filter
            latest_candle = candles[-1] if candles else {}
            iv_pct = None
            iv_raw = latest_candle.get("iv")
            if iv_raw is not None:
                iv_val = float(iv_raw)
                iv_pct = iv_val * 100.0 if iv_val < 1.0 else iv_val  # handle decimal vs pct
            iv_status = check_iv_filter(iv_pct, MAX_ENTRY_IV_PCT, HARD_MAX_IV_PCT)
            if iv_status == "reject":
                continue

            # Deduplicate signal
            latest_bar_time = str(candles[-1]["time"]) if candles else ""
            signal_key = f"{underlying}:{opt_type}"
            if runtime.processed_signals.get(signal_key) == latest_bar_time:
                continue

            # Spot MA context
            spot_context = await self._compute_spot_context(underlying, window)
            setup = spot_context.get("setup", "unknown")

            # Compute MACD line for exit monitoring
            macd_line, _, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

            indicators = latest_macd_rsi(closes)
            candidates.append({
                "row": row,
                "side": side,
                "candles": candles,
                "closes": closes,
                "latest_close": latest_close,
                "latest_bar_time": latest_bar_time,
                "signal_key": signal_key,
                "strength": strength or 0.0,
                "reason": reason,
                "rsi": indicators.get("rsi"),
                "opt_type": opt_type,
                "iv_pct": iv_pct,
                "iv_status": iv_status,
                "spot_setup": setup,
                "quadrant": quadrant,
                "window": window,
                "tte_days": tte,
                "macd_line": macd_line,
            })

        # Rank by: premium setup > breakout > trend > reversal, then by IV (lower better)
        setup_rank = {SETUP_PREMIUM: 0, SETUP_BREAKOUT: 1, "trend": 2, "reversal": 3, "unknown": 4}
        candidates.sort(key=lambda c: (
            setup_rank.get(c["spot_setup"], 4),
            c.get("iv_pct") or 999,
            -(c["strength"] or 0),
        ))

        if self._kill_switch_active:
            if candidates:
                self._append_commentary(
                    runtime.label,
                    f"NSE kill switch active. {len(candidates)} candidate signals observed, but new entries are blocked.",
                    tone="warning",
                )
            return

        opened = 0
        for candidate in candidates[:capacity]:
            await self._open_position(runtime, candidate)
            opened += 1

        if candidates:
            top = candidates[0]
            self._append_commentary(
                runtime.label,
                f"Found {len(candidates)} signals. Best: {top['row']['underlying']} "
                f"{top['opt_type']} (setup={top['spot_setup']}, IV={top.get('iv_pct', '?'):.0f}%, "
                f"regime={top['quadrant'].regime}). Opened {opened}.",
                tone="info",
            )

    # ── Position Entry ───────────────────────────────────────────────────────

    async def _open_position(self, runtime: StrategyRuntime, candidate: dict[str, Any]) -> None:
        row = candidate["row"]
        side = candidate["side"]
        latest_close = float(candidate["latest_close"])
        opt_type = candidate["opt_type"]
        window = candidate["window"]

        if latest_close <= 0:
            return

        expiry = row["expiry"]
        strike = float(side["strike"])
        symbol = _contract_symbol(row["underlying"], expiry, strike, opt_type)

        lot_size = await option_history_service.resolve_lot_size(
            underlying=row["underlying"],
            expiry=date.fromisoformat(expiry),
            strike=strike,
            option_type=opt_type,
            instrument_key=side.get("instrument_key"),
        )
        lot_size = lot_size or PaperPortfolio.DEFAULT_LOT_SIZE

        # Kelly-based position sizing
        sizing_mode = self._get_sizing_mode(candidate)
        if sizing_mode == "premium":
            fraction = KELLY_PREMIUM_FRACTION
        elif sizing_mode == "cautious":
            fraction = KELLY_CAUTIOUS_FRACTION
        else:
            fraction = KELLY_FRACTION

        allocation = max(runtime.portfolio.total_equity * fraction, latest_close * lot_size)
        lots = max(1, int(allocation // max(latest_close * lot_size, 1.0)))
        lots = min(lots, 5)
        qty = lot_size * lots

        order = runtime.order_book.place_order(
            symbol=symbol, action="BUY", order_type="MARKET", qty=qty,
            instrument_type=opt_type, expiry=expiry, strike=strike,
            option_type=opt_type, ltp=latest_close,
        )

        fill_price = float(order.fill_price or latest_close)
        runtime.positions[symbol] = StrategyPosition(
            symbol=symbol,
            underlying=row["underlying"],
            expiry=expiry,
            strike=strike,
            option_type=opt_type,
            instrument_key=side.get("instrument_key"),
            trading_symbol=side.get("trading_symbol"),
            qty=qty,
            initial_qty=qty,
            entry_price=fill_price,
            current_price=fill_price,
            peak_price=fill_price,
            entry_bar_time=str(candidate["latest_bar_time"]),
            entered_at=_now_ist().isoformat(),
            signal_reason=str(candidate["reason"]),
            signal_strength=_round_or_none(float(candidate["strength"]), 2),
            latest_rsi=_round_or_none(candidate.get("rsi"), 2),
            phase=PHASE_1,
            entry_iv_pct=_round_or_none(candidate.get("iv_pct"), 1),
            spot_setup=candidate.get("spot_setup"),
            window_end=str(window["window_end"]),
            macd_line=candidate.get("macd_line"),
        )
        runtime.entries += 1
        runtime.processed_signals[candidate["signal_key"]] = str(candidate["latest_bar_time"])

        self._append_event(runtime, StrategyEvent(
            time=_now_ist().isoformat(), event="entry", symbol=symbol,
            underlying=row["underlying"], option_type=opt_type, strike=strike,
            price=fill_price, qty=qty, reason=str(candidate["reason"]),
            signal_strength=_round_or_none(float(candidate["strength"]), 2),
            phase=PHASE_1,
        ))

        self._append_commentary(
            runtime.label,
            f"ENTRY {row['underlying']} {opt_type} {int(strike)} @{fill_price:.2f} | "
            f"Qty={qty} | Setup={candidate.get('spot_setup')} | "
            f"IV={candidate.get('iv_pct', '?'):.0f}% | TTE={candidate['tte_days']}d | "
            f"Regime={candidate['quadrant'].regime}",
            tone="trade",
        )
        await self._send_telegram_text(
            f"ENTRY | {row['underlying']} {opt_type} {int(strike)} @{fill_price:.2f}\n"
            f"Qty: {qty} | Setup: {candidate.get('spot_setup')} | "
            f"IV: {candidate.get('iv_pct', '?'):.0f}% | Regime: {candidate['quadrant'].regime}"
        )

    def _get_sizing_mode(self, candidate: dict) -> str:
        """Determine sizing mode based on setup quality and IV."""
        setup = candidate.get("spot_setup")
        iv_status = candidate.get("iv_status", "unknown")

        # Premium setup (option below MA50) or breakout with low IV → aggressive
        if setup in (SETUP_PREMIUM, SETUP_BREAKOUT) and iv_status == "preferred":
            return "premium"
        # High IV or reversal → cautious
        if iv_status == "acceptable" or setup == "reversal":
            return "cautious"
        return "normal"

    # ── Exit Management ──────────────────────────────────────────────────────

    async def _manage_exits(self, runtime: StrategyRuntime) -> None:
        """Evaluate exit priority chain for all open positions."""
        if not runtime.positions:
            return

        for symbol, pos in list(runtime.positions.items()):
            # Refresh current price
            candles = await option_history_service.load_candles(
                underlying=pos.underlying,
                expiry=date.fromisoformat(pos.expiry),
                strike=pos.strike,
                option_type=pos.option_type,
                instrument_key=pos.instrument_key,
                interval="30minute",
                limit=80,
            )
            if not candles:
                continue

            closes = [float(c["close"]) for c in candles if c.get("close")]
            if not closes:
                continue

            latest_close = closes[-1]
            pos.current_price = latest_close
            pos.peak_price = max(pos.peak_price, latest_close)

            # Update MACD line for death signal detection
            if len(closes) >= MACD_MIN_BARS:
                macd_line, _, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                pos.macd_line = macd_line

            indicators = latest_macd_rsi(closes)
            pos.latest_rsi = _round_or_none(indicators.get("rsi"), 2)

            return_pct = pos.return_pct

            # ── Priority 1: HARD STOP — exit 100% at -25% ──
            if return_pct <= -EXIT.hard_stop_pct:
                await self._close_position(runtime, pos, latest_close, "hard_stop", qty=pos.qty)
                continue

            # ── Priority 2: WINDOW END — exit 1 day before deadline ──
            if pos.window_end:
                window_end = date.fromisoformat(pos.window_end)
                if _now_ist().date() >= (window_end - timedelta(days=EXIT.window_end_buffer_days)):
                    await self._close_position(runtime, pos, latest_close, "window_end", qty=pos.qty)
                    continue

            # ── Priority 3: DEAD ZONE — both CE+PE MACD go negative ──
            quadrant = self._regime_cache.get(pos.underlying)
            if quadrant and quadrant.regime == REGIME_DEAD:
                await self._close_position(runtime, pos, latest_close, "dead_zone_exit", qty=pos.qty)
                continue

            # ── Priority 4: MACD DEATH SIGNAL — after +30% profit ──
            if return_pct >= EXIT.macd_death_min_profit_pct and pos.macd_line:
                if check_macd_death_signal(pos.macd_line, pos.option_type):
                    await self._close_position(runtime, pos, latest_close, "macd_death_signal", qty=pos.qty)
                    continue

            # ── Priority 5: TARGET +50% — exit 50% of position (Layer 1) ──
            if pos.phase == PHASE_1 and return_pct >= EXIT.target_pct:
                exit_qty = max(1, int(pos.qty * EXIT.target_exit_fraction))
                await self._close_position(runtime, pos, latest_close, "target_50pct", qty=exit_qty, partial=True)
                pos.qty -= exit_qty
                pos.phase = PHASE_2
                self._append_commentary(
                    runtime.label,
                    f"TARGET HIT {pos.underlying} {pos.option_type} +{return_pct:.0f}%. "
                    f"Exited {exit_qty}, holding {pos.qty} as runner.",
                    tone="trade",
                )
                continue

            # ── Priority 6: TRAILING STOP — after +100% on runner (Layer 2) ──
            if pos.phase in (PHASE_2, PHASE_TRAILING) and return_pct >= EXIT.trail_activation_pct:
                pos.phase = PHASE_TRAILING
                pos.trailing_stop = _round_or_none(
                    pos.peak_price * (1.0 - EXIT.trail_drawdown_pct / 100.0), 2
                )

            if pos.phase == PHASE_TRAILING and pos.trailing_stop and latest_close <= pos.trailing_stop:
                await self._close_position(runtime, pos, latest_close, "trailing_stoploss", qty=pos.qty)
                continue

        # Update portfolio prices
        latest_prices = {
            sym: pos.current_price
            for sym, pos in runtime.positions.items()
        }
        if latest_prices:
            runtime.portfolio.update_prices(latest_prices)

    # ── Position Close ───────────────────────────────────────────────────────

    async def _close_position(
        self,
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
            symbol=position.symbol, action="SELL", order_type="MARKET",
            qty=close_qty, instrument_type=position.option_type,
            expiry=position.expiry, strike=position.strike,
            option_type=position.option_type, ltp=exit_price,
        )
        pnl = (exit_price - position.entry_price) * close_qty

        if not partial:
            runtime.positions.pop(position.symbol, None)
            runtime.exits += 1

        self._append_event(runtime, StrategyEvent(
            time=_now_ist().isoformat(), event="exit", symbol=position.symbol,
            underlying=position.underlying, option_type=position.option_type,
            strike=position.strike, price=exit_price, qty=close_qty,
            reason=reason, signal_strength=position.signal_strength,
            pnl=_round_or_none(pnl, 2), phase=position.phase,
        ))

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

    # ── Helper Methods ───────────────────────────────────────────────────────

    async def _load_candles(self, row: dict, side: dict) -> list[dict]:
        return await option_history_service.load_candles(
            underlying=row["underlying"],
            expiry=date.fromisoformat(row["expiry"]),
            strike=float(side["strike"]),
            option_type=str(side["option_type"]),
            instrument_key=side.get("instrument_key"),
            interval="30minute",
            limit=80,
        )

    async def _compute_spot_context(self, underlying: str, window: dict) -> dict:
        """Load spot candles and classify the MA setup."""
        try:
            from db.database import async_session
            import asyncpg
            conn = await asyncpg.connect(
                str(settings.DATABASE_URL).replace("+asyncpg", "")
            )
            try:
                rows = await conn.fetch(
                    """
                    SELECT close FROM underlying_spot_candles
                    WHERE underlying = $1 AND interval = '30minute'
                      AND time::date BETWEEN ($2::date - INTERVAL '60 days')::date AND $3
                    ORDER BY time
                    """,
                    underlying,
                    window["window_start"],
                    window["window_end"],
                )
            finally:
                await conn.close()

            if len(rows) < SPOT_MA_SLOW + 10:
                return {"setup": "unknown"}

            spot_closes = [float(r["close"]) for r in rows]
            return compute_spot_ma_context(spot_closes, SPOT_MA_FAST, SPOT_MA_SLOW)
        except Exception as exc:
            logger.debug(f"[Strategy] Spot context failed for {underlying}: {exc}")
            return {"setup": "unknown"}

    def _has_underlying_position(self, runtime: StrategyRuntime, underlying: str) -> bool:
        return any(p.underlying == underlying for p in runtime.positions.values())

    def _append_event(self, runtime: StrategyRuntime, event: StrategyEvent) -> None:
        runtime.recent_events.insert(0, event)
        del runtime.recent_events[20:]

    def _append_commentary(self, scope: str, message: str, tone: str = "info") -> None:
        if not message:
            return
        prev = self._commentary[0] if self._commentary else None
        if prev and prev.scope == scope and prev.message == message:
            return
        self._commentary.insert(0, CommentaryEntry(
            time=_now_ist().isoformat(), scope=scope, tone=tone, message=message,
        ))
        del self._commentary[COMMENTARY_MAX:]

    # ── Telegram ─────────────────────────────────────────────────────────────

    async def _get_broker_status_summary(self) -> str | None:
        try:
            from api.routers.auth import (
                format_broker_status_summary,
                get_broker_connection_snapshot,
            )

            snapshot = await get_broker_connection_snapshot()
            return format_broker_status_summary(snapshot)
        except Exception as exc:
            logger.debug(f"[Strategy] broker status summary failed: {exc}")
            return None

    async def _send_telegram_text(self, message: str) -> None:
        if not settings.TELEGRAM_REPORTS_ENABLED:
            return
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return
        text = message.strip()
        broker_status = await self._get_broker_status_summary()
        if broker_status:
            text = f"{text}\n{broker_status}"
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text})
        except Exception as exc:
            logger.warning(f"[Strategy] Telegram failed: {exc}")

    async def _maybe_send_telegram_report(self) -> None:
        if not settings.TELEGRAM_REPORTS_ENABLED or not settings.TELEGRAM_BOT_TOKEN:
            return
        now = _now_ist()
        interval = _report_interval_seconds(settings.TELEGRAM_REPORT_INTERVAL)
        if self._telegram_last_sent_at and (now - self._telegram_last_sent_at).total_seconds() < interval:
            return

        runtime = self._strategy
        summary = runtime.portfolio.get_summary()
        lines = [
            f"Nomad Curie | {now.strftime('%d %b %Y %I:%M %p IST')}",
            f"Windows: {len(self._active_windows)} active",
            f"Equity: ₹{summary.get('total_equity', 0):,.0f}",
            f"Realized PnL: ₹{summary.get('realized_pnl', 0):,.0f}",
            f"Open: {len(runtime.positions)} | Entries: {runtime.entries} | Exits: {runtime.exits}",
        ]
        for pos in runtime.positions.values():
            lines.append(
                f"  {pos.underlying} {pos.option_type} {int(pos.strike)} "
                f"@{pos.entry_price:.2f} → {pos.current_price:.2f} "
                f"({pos.return_pct:.1f}%) [{pos.phase}]"
            )
        try:
            await self._send_telegram_text("\n".join(lines))
            self._telegram_last_sent_at = now
        except Exception:
            pass

    def set_kill_switch(self, active: bool) -> dict[str, Any]:
        runtime = self._strategy
        self._kill_switch_active = bool(active)
        cancelled_orders = 0
        for order in list(runtime.order_book.get_open_orders(runtime.portfolio.session_id)):
            if runtime.order_book.cancel_order(order.order_id):
                cancelled_orders += 1

        if self._kill_switch_active:
            self._last_message = "NSE kill switch active. New entries are blocked until it is released."
            self._append_commentary("System", self._last_message, tone="warning")
        else:
            self._last_message = "NSE kill switch released. Manual scans can open new entries again."
            self._append_commentary("System", self._last_message, tone="success")

        return self.get_control_state(cancelled_orders=cancelled_orders)

    def get_control_state(self, *, cancelled_orders: int = 0) -> dict[str, Any]:
        return {
            "market": "nse",
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "cancelled_orders": cancelled_orders,
        }

    # ── Status API ───────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        runtime = self._strategy
        summary = runtime.portfolio.get_summary()

        return {
            "enabled": self._enabled,
            "auto_run_enabled": self._auto_run_enabled,
            "kill_switch_active": self._kill_switch_active,
            "running": self._running,
            "scan_interval_seconds": self.scan_interval_seconds,
            "last_run_at": self._last_run_at,
            "last_error": self._last_error,
            "last_message": self._last_message,
            "target_expiry": self._last_expiry,
            "candidate_expiries": self._last_candidate_expiries,
            "active_windows": len(self._active_windows),
            "regime_summary": {
                und: q.regime for und, q in self._regime_cache.items()
            } if self._regime_cache else {},
            "telegram": {
                "enabled": settings.TELEGRAM_REPORTS_ENABLED,
                "report_interval": settings.TELEGRAM_REPORT_INTERVAL,
                "configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
                "last_sent_at": self._telegram_last_sent_at.isoformat() if self._telegram_last_sent_at else None,
            },
            "commentary": [asdict(entry) for entry in self._commentary],
            "strategies": [
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
                            **{k: v for k, v in asdict(pos).items() if k != "macd_line"},
                            "unrealized_pnl": _round_or_none(pos.unrealized_pnl, 2),
                            "return_pct": _round_or_none(pos.return_pct, 2),
                        }
                        for pos in runtime.positions.values()
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
            ],
        }


paper_strategy_agent = PaperStrategyAgent()
