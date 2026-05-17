"""Walk-forward replay for the commodity futures sleeve.

The live commodity futures agent enters only when a 15-minute MACD zero-line
signal agrees with the intraday Market Profile gate. This module replays that
same decision shape on historical MCX futures candles and writes reusable
runtime artifacts for review and RAG bootstrapping.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import (
    FUTURES_BREAK_EVEN_R_MULTIPLIER,
    FUTURES_CONTINUATION_LOOKBACK_BARS,
    FUTURES_MAX_POSITIONS,
    FUTURES_MIN_HOLD_BARS,
    FUTURES_MIN_STOP_PCT,
    FUTURES_TARGET_ARM_R_MULTIPLIER,
    FUTURES_TIMEFRAME,
    FUTURES_TRAIL_ATR_MULTIPLIER,
    CommodityStrategyAgent,
    evaluate_commodity_signal,
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


def _session_rows(rows: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    current_time = pd.Timestamp(rows[index]["time"])
    current_date = current_time.date()
    session = []
    for row in rows[: index + 1]:
        try:
            row_time = pd.Timestamp(row["time"])
        except Exception:
            continue
        if row_time.date() == current_date:
            session.append(row)
    return session


def _candidate_from_analysis(analysis: dict[str, Any]) -> tuple[Optional[str], str, Optional[str]]:
    signal = analysis.get("signal")
    if signal in {"BUY", "SELL"}:
        return str(signal), str(analysis.get("reason") or "macd_zero_cross"), "fresh_cross"
    continuation = analysis.get("continuation_signal")
    if continuation in {"BUY", "SELL"}:
        return (
            str(continuation),
            str(analysis.get("continuation_reason") or analysis.get("reason") or "macd_continuation"),
            "continuation",
        )
    return None, str(analysis.get("reason") or "no_cross"), None


def _entry_row(
    agent: CommodityStrategyAgent,
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    index: int,
) -> Optional[dict[str, Any]]:
    history = rows[: index + 1]
    analysis = evaluate_commodity_signal(history, symbol=symbol, timeframe=FUTURES_TIMEFRAME)
    candidate_signal, candidate_reason, entry_style = _candidate_from_analysis(analysis)
    if candidate_signal not in {"BUY", "SELL"}:
        return None

    latest_close = _safe_float(analysis.get("latest_close"))
    if latest_close is None or latest_close <= 0:
        return None

    session = _session_rows(rows, index)
    profile = agent._build_market_profile(symbol, session)  # noqa: SLF001 - replaying live gate.
    if profile is None or len(session) < 8:
        return None

    mp_direction, mp_day_type, mp_reason = agent._classify_market_profile(  # noqa: SLF001 - replaying live gate.
        profile=profile,
        current_price=latest_close,
        session_rows=session,
    )
    if entry_style == "continuation" and mp_day_type not in {"trend_up", "trend_down"}:
        return None
    if candidate_signal != mp_direction:
        return None

    return {
        "symbol": symbol,
        "underlying": get_commodity_contract_spec(symbol).root,
        "signal": candidate_signal,
        "reason": f"{candidate_reason}_{mp_reason}",
        "entry_style": entry_style,
        "price": latest_close,
        "atr": _safe_float(analysis.get("atr")) or 0.0,
        "bar_time": str(analysis.get("bar_time") or rows[index].get("time") or ""),
        "raw_signal": analysis.get("signal"),
        "mp_day_type": mp_day_type,
        "mp_reason": mp_reason,
        "mp_poc": _round_or_none(getattr(profile, "poc", None), 2),
        "mp_vah": _round_or_none(getattr(profile, "vah", None), 2),
        "mp_val": _round_or_none(getattr(profile, "val", None), 2),
        "mp_ib_high": _round_or_none(getattr(profile, "initial_balance_high", None), 2),
        "mp_ib_low": _round_or_none(getattr(profile, "initial_balance_low", None), 2),
        "macd": analysis.get("macd"),
        "macd_histogram": analysis.get("macd_histogram"),
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
        for index in range(40, len(rows)):
            row = rows[index]
            analysis = evaluate_commodity_signal(rows[: index + 1], symbol=symbol, timeframe=FUTURES_TIMEFRAME)
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

            entry = _entry_row(self.agent, symbol=symbol, rows=rows, index=index)
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
