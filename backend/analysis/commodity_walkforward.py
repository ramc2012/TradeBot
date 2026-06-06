"""Walk-forward replay for the commodity futures sleeve.

The live commodity futures agent (``_analyze_futures_symbol``) enters when the
intraday Market-Profile + order-flow signal (``evaluate_commodity_mp_signal``)
fires one of its triggers (open_drive / ib_break / failed_auction / va_migration /
lvn_fade). This module replays that same per-bar decision on historical MCX futures
candles and writes reusable runtime artifacts, and exposes a harness-ready
R-multiple backtest (:func:`simulate_signal_backtest`).

PREREQUISITE — DATA DEPTH: meaningful (MinBTL-satisfying) validation needs >= ~2y of
1-minute commodity history in ``underlying_spot_candles``. The live agent only loads
``DEFAULT_COMMODITY_HISTORY_DAYS`` (~21 days) from the broker, which is far too
shallow for walk-forward statistics. Until a deeper 1-minute commodity backfill
exists, this module's output is a smoke/plumbing artifact, not a validated edge.
See docs/STRATEGY_TESTING_RESULTS.md Run 3.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import (
    IST,
    FUTURES_BREAK_EVEN_R_MULTIPLIER,
    FUTURES_MAX_POSITIONS,
    FUTURES_MIN_HOLD_BARS,
    FUTURES_MIN_STOP_PCT,
    FUTURES_TARGET_ARM_R_MULTIPLIER,
    FUTURES_TIMEFRAME,
    FUTURES_TRAIL_ATR_MULTIPLIER,
    CommodityStrategyAgent,
    _compute_atr_series,
    _infer_09ist_anchor,
    _latest_session_rows,
    _parse_iso_timestamp,
    evaluate_commodity_mp_signal,
)


DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "commodity_walkforward"
DEFAULT_SYMBOLS = (
    "MCX:GOLD26JUNFUT",
    "MCX:SILVERM26JUNFUT",
    "MCX:CRUDEOIL26JUNFUT",
    "MCX:NATURALGAS26MAYFUT",
)


@dataclass
class ReplayPosition:
    symbol: str
    underlying: str
    action: str
    entry_time: str
    entry_price: float
    initial_stop_price: float
    stop_price: float
    target_price: float
    qty: int
    lot_size: int
    lots: int
    atr: float
    peak_price: float
    target_reached: bool
    entry_reason: str
    entry_style: str
    mp_day_type: str
    mp_reason: str
    mp_poc: Optional[float]
    mp_vah: Optional[float]
    mp_val: Optional[float]
    entry_macd: Optional[float]
    entry_histogram: Optional[float]
    entry_index: int


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _round_or_none(value: Any, digits: int = 4) -> Optional[float]:
    number = _safe_float(value)
    return round(number, digits) if number is not None else None


def _row_session_date(row: dict[str, Any]) -> Optional[_date]:
    parsed = _parse_iso_timestamp(row.get("time"))
    if parsed is None:
        return None
    try:
        return parsed.astimezone(IST).date()
    except Exception:
        return parsed.date()


def _prior_profile(
    agent: CommodityStrategyAgent,
    *,
    symbol: str,
    history: list[dict[str, Any]],
    session_date: Optional[_date],
    cache: dict,
):
    """Build the profile for the session immediately before ``session_date``.

    Mirrors ``CommodityStrategyAgent._load_prior_session_profile`` but off the
    in-memory walk-forward history (no broker call). Cached per session date.
    """
    if session_date in cache:
        return cache[session_date]
    prior_profile = None
    if session_date is not None:
        prior_rows = [r for r in history if (d := _row_session_date(r)) is not None and d < session_date]
        if prior_rows:
            prior_session, _ = _latest_session_rows(prior_rows)
            if prior_session:
                prior_profile = agent._build_market_profile(symbol, prior_session)  # noqa: SLF001
    cache[session_date] = prior_profile
    return prior_profile


def _evaluate_mp(
    agent: CommodityStrategyAgent,
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    index: int,
    prior_cache: dict,
) -> dict[str, Any]:
    """Replay the live ``evaluate_commodity_mp_signal`` decision for bar ``index``.

    Builds the same arguments the live ``_analyze_futures_symbol`` constructs:
    today_profile (current session), prior_profile (previous session),
    cvd_anchor_index (09:00 IST anchor), atr_1m. Operates only on closed bars up
    to and including ``index`` (no look-ahead).
    """
    history = rows[: index + 1]
    session_rows, session_date = _latest_session_rows(history)
    today_profile = agent._build_market_profile(symbol, session_rows)  # noqa: SLF001
    prior_profile = _prior_profile(
        agent, symbol=symbol, history=history, session_date=session_date, cache=prior_cache
    )
    cvd_anchor_index = _infer_09ist_anchor(history)
    atr_1m = _compute_atr_series(history, period=14)
    return evaluate_commodity_mp_signal(
        history,
        symbol=symbol,
        today_profile=today_profile,
        prior_profile=prior_profile,
        cvd_anchor_index=cvd_anchor_index,
        atr_1m=atr_1m,
    )


def _candidate_from_analysis(analysis: dict[str, Any]) -> tuple[Optional[str], str, Optional[str]]:
    """Map the MP+OF signal row to (BUY/SELL, reason, entry_style).

    The MP signal already fuses the Market-Profile gate and the order-flow
    confirmation, so a fired ``signal`` is the actionable candidate — there is no
    separate continuation channel like the legacy MACD evaluator had.
    """
    signal = analysis.get("signal")
    if signal in {"BUY", "SELL"}:
        return (
            str(signal),
            str(analysis.get("reason") or "mp_trigger"),
            str(analysis.get("entry_style") or "mp_trigger"),
        )
    return None, str(analysis.get("reason") or "no_trigger"), None


def _entry_row(
    agent: CommodityStrategyAgent,
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    index: int,
    prior_cache: dict,
    analysis: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    if analysis is None:
        analysis = _evaluate_mp(agent, symbol=symbol, rows=rows, index=index, prior_cache=prior_cache)
    candidate_signal, candidate_reason, entry_style = _candidate_from_analysis(analysis)
    if candidate_signal not in {"BUY", "SELL"}:
        return None

    # The MP signal does not echo the bar close, so anchor entry on the bar itself.
    price = _safe_float(rows[index].get("close"))
    if price is None or price <= 0:
        return None

    return {
        "symbol": symbol,
        "underlying": get_commodity_contract_spec(symbol).root,
        "signal": candidate_signal,
        "reason": candidate_reason,
        "entry_style": entry_style,
        "price": price,
        "atr": _safe_float(analysis.get("atr")) or 0.0,
        "bar_time": str(analysis.get("bar_time") or rows[index].get("time") or ""),
        "raw_signal": analysis.get("signal"),
        "mp_day_type": str(analysis.get("mp_day_type") or analysis.get("regime") or ""),
        "mp_reason": str(analysis.get("mp_reason") or ""),
        "mp_poc": _round_or_none(analysis.get("mp_poc"), 2),
        "mp_vah": _round_or_none(analysis.get("mp_vah"), 2),
        "mp_val": _round_or_none(analysis.get("mp_val"), 2),
        "mp_ib_high": _round_or_none(analysis.get("mp_ib_high"), 2),
        "mp_ib_low": _round_or_none(analysis.get("mp_ib_low"), 2),
        "confidence": _round_or_none(analysis.get("confidence"), 3),
        "stop_hint": _round_or_none(analysis.get("stop_hint"), 4),
        "target_hint": _round_or_none(analysis.get("target_hint"), 4),
        "macd": None,
        "macd_histogram": None,
    }


def _open_position(row: dict[str, Any], *, index: int, lots: int) -> Optional[ReplayPosition]:
    symbol = str(row["symbol"])
    spec = get_commodity_contract_spec(symbol)
    price = float(row.get("price") or 0.0)
    atr = float(row.get("atr") or 0.0)
    if price <= 0 or atr <= 0:
        return None

    min_stop_distance = max(atr, price * FUTURES_MIN_STOP_PCT)
    if row.get("signal") == "BUY":
        stop_candidates = [price - min_stop_distance]
        for level in (row.get("mp_val"), row.get("mp_ib_low")):
            level_value = _safe_float(level)
            if level_value is not None and level_value < price and (price - level_value) >= min_stop_distance:
                stop_candidates.append(level_value)
        stop_price = max(stop_candidates)
        target_price = price + ((price - stop_price) * 2.0)
    else:
        stop_candidates = [price + min_stop_distance]
        for level in (row.get("mp_vah"), row.get("mp_ib_high")):
            level_value = _safe_float(level)
            if level_value is not None and level_value > price and (level_value - price) >= min_stop_distance:
                stop_candidates.append(level_value)
        stop_price = min(stop_candidates)
        target_price = price - ((stop_price - price) * 2.0)

    lot_size = int(spec.futures_lot_size or 1)
    safe_lots = max(int(lots or 1), 1)
    return ReplayPosition(
        symbol=symbol,
        underlying=spec.root,
        action=str(row.get("signal") or "BUY"),
        entry_time=str(row.get("bar_time") or ""),
        entry_price=round(price, 4),
        initial_stop_price=round(stop_price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        qty=lot_size * safe_lots,
        lot_size=lot_size,
        lots=safe_lots,
        atr=atr,
        peak_price=round(price, 4),
        target_reached=False,
        entry_reason=str(row.get("reason") or "futures_signal"),
        entry_style=str(row.get("entry_style") or "fresh_cross"),
        mp_day_type=str(row.get("mp_day_type") or ""),
        mp_reason=str(row.get("mp_reason") or ""),
        mp_poc=_round_or_none(row.get("mp_poc"), 2),
        mp_vah=_round_or_none(row.get("mp_vah"), 2),
        mp_val=_round_or_none(row.get("mp_val"), 2),
        entry_macd=_round_or_none(row.get("macd"), 4),
        entry_histogram=_round_or_none(row.get("macd_histogram"), 4),
        entry_index=index,
    )


def _close_trade(position: ReplayPosition, *, row: dict[str, Any], index: int, reason: str) -> dict[str, Any]:
    exit_price = float(row.get("close") or position.entry_price)
    multiplier = 1 if position.action == "BUY" else -1
    pnl = multiplier * (exit_price - position.entry_price) * position.qty
    return_pct = (
        multiplier * ((exit_price - position.entry_price) / position.entry_price) * 100.0
        if position.entry_price > 0
        else 0.0
    )
    return {
        "strategy_key": "commodity_futures",
        "symbol": position.symbol,
        "underlying": position.underlying,
        "action": position.action,
        "entry_time": position.entry_time,
        "exit_time": str(row.get("time") or ""),
        "entry_price": round(position.entry_price, 4),
        "exit_price": round(exit_price, 4),
        "qty": position.qty,
        "lot_size": position.lot_size,
        "lots": position.lots,
        "pnl": round(pnl, 2),
        "return_pct": round(return_pct, 4),
        "exit_reason": reason,
        "holding_bars": max(index - position.entry_index, 0),
        "entry_reason": position.entry_reason,
        "entry_style": position.entry_style,
        "mp_day_type": position.mp_day_type,
        "mp_reason": position.mp_reason,
        "mp_poc": position.mp_poc,
        "mp_vah": position.mp_vah,
        "mp_val": position.mp_val,
        "entry_macd": position.entry_macd,
        "entry_histogram": position.entry_histogram,
        "initial_stop_price": round(position.initial_stop_price, 4),
        "target_price": round(position.target_price, 4),
    }


def _exit_reason(position: ReplayPosition, *, row: dict[str, Any], raw_signal: Optional[str]) -> Optional[str]:
    current_price = float(row.get("close") or position.entry_price)
    if position.action == "BUY":
        position.peak_price = max(position.peak_price, current_price)
    else:
        position.peak_price = min(position.peak_price, current_price)

    risk_distance = abs(position.target_price - position.entry_price) / FUTURES_TARGET_ARM_R_MULTIPLIER
    if risk_distance <= 0:
        risk_distance = abs(position.entry_price - position.stop_price)

    trailing_label: Optional[str] = None
    if risk_distance > 0:
        if position.action == "BUY":
            if current_price - position.entry_price >= risk_distance * FUTURES_BREAK_EVEN_R_MULTIPLIER:
                position.stop_price = max(position.stop_price, position.entry_price)
            if not position.target_reached and current_price >= position.target_price:
                position.target_reached = True
                position.stop_price = max(position.stop_price, position.entry_price + (risk_distance * 0.5))
            if position.target_reached:
                trail_buffer = max(position.atr * FUTURES_TRAIL_ATR_MULTIPLIER, risk_distance)
                position.stop_price = max(position.stop_price, position.peak_price - trail_buffer)
                trailing_label = "trail_stop"
            if current_price <= position.stop_price:
                return trailing_label or "stop_loss"
            if raw_signal == "SELL":
                return "macd_reversal"
        else:
            if position.entry_price - current_price >= risk_distance * FUTURES_BREAK_EVEN_R_MULTIPLIER:
                position.stop_price = min(position.stop_price, position.entry_price)
            if not position.target_reached and current_price <= position.target_price:
                position.target_reached = True
                position.stop_price = min(position.stop_price, position.entry_price - (risk_distance * 0.5))
            if position.target_reached:
                trail_buffer = max(position.atr * FUTURES_TRAIL_ATR_MULTIPLIER, risk_distance)
                position.stop_price = min(position.stop_price, position.peak_price + trail_buffer)
                trailing_label = "trail_stop"
            if current_price >= position.stop_price:
                return trailing_label or "stop_loss"
            if raw_signal == "BUY":
                return "macd_reversal"
    return None


def _summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(trade.get("pnl") or 0.0) for trade in trades]
    returns = [float(trade.get("return_pct") or 0.0) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
    }


class CommodityFuturesWalkForwardRunner:
    def __init__(
        self,
        *,
        output_root: Path = OUTPUT_ROOT,
        symbols: Optional[list[str]] = None,
        lookback_days: int = 21,
        lots: int = 1,
    ) -> None:
        self.output_root = output_root
        self.lookback_days = int(lookback_days)
        self.lots = max(int(lots or 1), 1)
        self.agent = CommodityStrategyAgent()
        configured = self.agent.get_symbols()
        self.symbols = symbols or configured or list(DEFAULT_SYMBOLS)

    async def run(self) -> dict[str, Any]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        all_trades: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        for symbol in self.symbols:
            rows = await self.agent._load_history(  # noqa: SLF001 - uses the live agent's Upstox/Fyers bridge.
                symbol,
                interval=FUTURES_TIMEFRAME,
                lookback_days=self.lookback_days,
            )
            rows = self._normalize_rows(rows)
            trades = self._simulate_symbol(symbol, rows)
            all_trades.extend(trades)
            coverage.append(
                {
                    "symbol": symbol,
                    "candles": len(rows),
                    "first_time": rows[0]["time"] if rows else None,
                    "last_time": rows[-1]["time"] if rows else None,
                    "trades": len(trades),
                    "status": "ok" if rows else "no_history",
                }
            )

        result = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "timeframe": FUTURES_TIMEFRAME,
            "lookback_days": self.lookback_days,
            "position_cap": FUTURES_MAX_POSITIONS,
            "symbols": self.symbols,
            "coverage": coverage,
            "overall": _summarize(all_trades),
            "by_symbol": {
                symbol: _summarize([trade for trade in all_trades if trade.get("symbol") == symbol])
                for symbol in self.symbols
            },
        }
        self._write_outputs(result, all_trades)
        return result

    @staticmethod
    def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for row in rows:
            time_value = row.get("time")
            close = _safe_float(row.get("close"))
            if not time_value or close is None:
                continue
            normalized.append(
                {
                    "time": pd.Timestamp(time_value).isoformat(),
                    "open": _safe_float(row.get("open")) or close,
                    "high": _safe_float(row.get("high")) or close,
                    "low": _safe_float(row.get("low")) or close,
                    "close": close,
                    "volume": int(_safe_float(row.get("volume")) or 0),
                    "oi": int(_safe_float(row.get("oi")) or 0),
                }
            )
        normalized.sort(key=lambda item: item["time"])
        return normalized

    def _simulate_symbol(self, symbol: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(rows) < 45:
            return []
        trades: list[dict[str, Any]] = []
        position: ReplayPosition | None = None
        prior_cache: dict = {}
        for index in range(40, len(rows)):
            row = rows[index]
            analysis = _evaluate_mp(self.agent, symbol=symbol, rows=rows, index=index, prior_cache=prior_cache)
            if position is not None:
                hold_bars = index - position.entry_index
                reason = None
                if hold_bars >= FUTURES_MIN_HOLD_BARS:
                    reason = _exit_reason(position, row=row, raw_signal=analysis.get("signal"))
                else:
                    reason = _exit_reason(position, row=row, raw_signal=None)
                if reason:
                    trades.append(_close_trade(position, row=row, index=index, reason=reason))
                    position = None
                continue

            entry = _entry_row(
                self.agent, symbol=symbol, rows=rows, index=index, prior_cache=prior_cache, analysis=analysis
            )
            if not entry:
                continue
            position = _open_position(entry, index=index, lots=self.lots)

        if position is not None and rows:
            trades.append(_close_trade(position, row=rows[-1], index=len(rows) - 1, reason="hold_to_end"))
        return trades

    def _write_outputs(self, result: dict[str, Any], trades: list[dict[str, Any]]) -> None:
        (self.output_root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        trades_path = self.output_root / "trades.csv"
        fieldnames = sorted({key for trade in trades for key in trade.keys()})
        with trades_path.open("w", newline="", encoding="utf-8") as handle:
            if fieldnames:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(trades)
            else:
                handle.write("")
        lines = [
            "# Commodity Futures Walk-Forward",
            "",
            f"Generated: {result['generated_at']}",
            f"Timeframe: `{result['timeframe']}`",
            f"Lookback days: {result['lookback_days']}",
            "",
            "## Overall",
            "",
            f"- Trades: {result['overall']['trades']}",
            f"- Win rate: {result['overall']['win_rate'] * 100:.2f}%",
            f"- Total P&L: {result['overall']['total_pnl']:.2f}",
            f"- Avg return: {result['overall']['avg_return_pct']:.2f}%",
            f"- Profit factor: {result['overall']['profit_factor']}",
            "",
            "## Coverage",
            "",
        ]
        for row in result["coverage"]:
            lines.append(
                f"- {row['symbol']}: {row['candles']} candles, {row['trades']} trades, status={row['status']}"
            )
        (self.output_root / "report.md").write_text("\n".join(lines), encoding="utf-8")


def simulate_signal_backtest(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    agent: Optional[CommodityStrategyAgent] = None,
    sl_atr: float = 1.5,
    tp_atr: float = 3.0,
    warmup_bars: int = 40,
) -> dict[str, Any]:
    """Harness-ready R-multiple backtest of the MP+OF commodity signal.

    Maps the per-bar ``evaluate_commodity_mp_signal`` decision to an integer signal
    (+1 BUY / -1 SELL / 0 flat) and runs it through the shared neutral ATR-stop
    executor (:func:`analysis.signal_backtest.simulate_underlying`, stop = -1R), so
    the result plugs straight into ``analysis.walk_forward.validate_strategy`` via
    ``extract_returns``/``extract_exit_times``. Trades the underlying futures price.

    Returns ``{"events": [{r_multiple, exit_time, ...}], "summary": {...}}``.
    """
    from analysis.signal_backtest import simulate_underlying

    agent = agent or CommodityStrategyAgent()
    rows = CommodityFuturesWalkForwardRunner._normalize_rows(rows)
    if len(rows) <= warmup_bars + 5:
        return {"events": [], "summary": {"trades": 0}}

    prior_cache: dict = {}
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        sig = 0
        atr = 0.0
        if index >= warmup_bars:
            analysis = _evaluate_mp(agent, symbol=symbol, rows=rows, index=index, prior_cache=prior_cache)
            raw = analysis.get("signal")
            sig = 1 if raw == "BUY" else (-1 if raw == "SELL" else 0)
            atr = _safe_float(analysis.get("atr")) or 0.0
        records.append(
            {
                "time": row["time"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "atr": float(atr),
                "sig": int(sig),
            }
        )

    frame = pd.DataFrame.from_records(records)
    # Give every bar a positive ATR stop unit (fall back to 1% of price if the
    # signal had not yet computed an ATR).
    atr = frame["atr"].astype(float).where(frame["atr"].astype(float) > 0)
    atr = atr.ffill().bfill().fillna(frame["close"].astype(float) * 0.01)
    frame["atr"] = atr.astype(float)
    return simulate_underlying(frame, sl_atr=sl_atr, tp_atr=tp_atr, atr_col="atr", signal_col="sig")


async def amain() -> None:
    runner = CommodityFuturesWalkForwardRunner()
    result = await runner.run()
    print(json.dumps(result["overall"], indent=2))
    for row in result["coverage"]:
        print(row)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
