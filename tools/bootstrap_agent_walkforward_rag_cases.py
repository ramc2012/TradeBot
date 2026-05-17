#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agentic_rag.schemas import TradeCaseRecord
from agentic_rag.service import rag_service
from analysis.commodity_walkforward import CommodityFuturesWalkForwardRunner
from analysis.index_option_walkforward import IndexOptionWalkForwardRunner


NSE_TRADES_PATH = BACKEND_ROOT / "runtime" / "index_analytics_data" / "walkforward_macd_rsi" / "oos_trades.csv"
COMMODITY_TRADES_PATH = BACKEND_ROOT / "runtime" / "index_analytics_data" / "commodity_walkforward" / "trades.csv"


def _result_from_value(value: float) -> str:
    if value > 0:
        return "win"
    if value < 0:
        return "loss"
    return "neutral"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _nse_case(row: dict[str, Any]) -> TradeCaseRecord:
    underlying = str(row.get("underlying") or "").upper()
    symbol = str(row.get("symbol") or underlying)
    entry_time = str(row.get("entry_time") or "")
    exit_time = str(row.get("exit_time") or "")
    return_pct = _float(row.get("return_pct")) or 0.0
    option_type = str(row.get("option_type") or "").upper()
    setup_name = str(row.get("chosen_variant_key") or row.get("exit_strategy") or "macd_zero_cross")
    case_id = f"walkforward:nse_macd:{underlying}:{symbol}:{entry_time}:{setup_name}"
    features = {
        "return_pct": return_pct,
        "max_possible_return_pct": _float(row.get("max_possible_return_pct")),
        "entry_price": _float(row.get("entry_price")),
        "exit_price": _float(row.get("exit_price")),
        "strike": _float(row.get("strike")),
        "expiry": row.get("expiry"),
        "expiry_kind": row.get("expiry_kind"),
        "timeframe": row.get("timeframe"),
        "rsi_filter": row.get("rsi_filter"),
        "exit_strategy": row.get("exit_strategy"),
        "walkforward_group": row.get("walkforward_group"),
        "walkforward_window": row.get("walkforward_window"),
        "train_consistency_score": _float(row.get("train_consistency_score")),
        "entry_macd": _float(row.get("entry_macd")),
        "entry_rsi": _float(row.get("entry_rsi")),
        "holding_minutes": _float(row.get("holding_minutes")),
        "bars_to_max": _float(row.get("bars_to_max")),
    }
    return TradeCaseRecord(
        id=case_id,
        strategy_key="macd_strategy",
        underlying=underlying,
        symbol=symbol,
        setup_name=setup_name,
        regime=str(row.get("walkforward_group") or row.get("expiry_kind") or ""),
        direction=option_type,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=_float(row.get("entry_price")),
        exit_price=_float(row.get("exit_price")),
        pnl=round(return_pct, 4),
        result=_result_from_value(return_pct),
        tags=[
            "nse",
            "index_options",
            "walkforward",
            option_type,
            str(row.get("timeframe") or ""),
            str(row.get("exit_strategy") or ""),
            _result_from_value(return_pct),
        ],
        features={key: value for key, value in features.items() if value not in (None, "")},
        lesson=(
            f"NSE out-of-sample MACD zero-cross case returned {return_pct:.2f}% "
            f"using {row.get('chosen_variant_key') or row.get('exit_strategy')}."
        ),
        source="walkforward_macd_rsi",
    )


def _commodity_case(row: dict[str, Any]) -> TradeCaseRecord:
    underlying = str(row.get("underlying") or "").upper()
    symbol = str(row.get("symbol") or underlying)
    entry_time = str(row.get("entry_time") or "")
    pnl = _float(row.get("pnl")) or 0.0
    action = str(row.get("action") or "").upper()
    setup_name = str(row.get("entry_reason") or row.get("entry_style") or "commodity_futures_signal")
    case_id = f"walkforward:commodity_futures:{symbol}:{entry_time}:{setup_name}"
    features = {
        "return_pct": _float(row.get("return_pct")),
        "entry_price": _float(row.get("entry_price")),
        "exit_price": _float(row.get("exit_price")),
        "qty": _float(row.get("qty")),
        "lot_size": _float(row.get("lot_size")),
        "lots": _float(row.get("lots")),
        "exit_reason": row.get("exit_reason"),
        "entry_style": row.get("entry_style"),
        "mp_day_type": row.get("mp_day_type"),
        "mp_reason": row.get("mp_reason"),
        "mp_poc": _float(row.get("mp_poc")),
        "mp_vah": _float(row.get("mp_vah")),
        "mp_val": _float(row.get("mp_val")),
        "entry_macd": _float(row.get("entry_macd")),
        "entry_histogram": _float(row.get("entry_histogram")),
        "holding_bars": _float(row.get("holding_bars")),
        "target_price": _float(row.get("target_price")),
        "initial_stop_price": _float(row.get("initial_stop_price")),
    }
    return TradeCaseRecord(
        id=case_id,
        strategy_key="commodity_futures",
        underlying=underlying,
        symbol=symbol,
        setup_name=setup_name,
        regime=str(row.get("mp_day_type") or ""),
        direction=action,
        entry_time=entry_time,
        exit_time=str(row.get("exit_time") or ""),
        entry_price=_float(row.get("entry_price")),
        exit_price=_float(row.get("exit_price")),
        pnl=round(pnl, 2),
        result=_result_from_value(pnl),
        tags=[
            "commodity",
            "mcx",
            "futures",
            "walkforward",
            action,
            str(row.get("entry_style") or ""),
            str(row.get("exit_reason") or ""),
            _result_from_value(pnl),
        ],
        features={key: value for key, value in features.items() if value not in (None, "")},
        lesson=(
            f"Commodity futures MACD plus Market Profile case closed with P&L {pnl:.2f}; "
            f"entry={setup_name}, exit={row.get('exit_reason')}."
        ),
        source="commodity_walkforward",
    )


def _append_deduped(cases: list[TradeCaseRecord]) -> tuple[int, int]:
    existing = {case.id for case in rag_service.store.load_trade_cases()}
    appended = 0
    skipped = 0
    for case in cases:
        if case.id in existing:
            skipped += 1
            continue
        rag_service.add_trade_case(case)
        existing.add(case.id)
        appended += 1
    return appended, skipped


def _remove_existing_sources(sources: set[str]) -> int:
    path = rag_service.store.trade_cases_path
    if not path.exists():
        return 0
    kept: list[dict[str, Any]] = []
    removed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append({"_raw": line})
                continue
            if str(row.get("source") or "") in sources:
                removed += 1
                continue
            kept.append(row)
    with path.open("w", encoding="utf-8") as handle:
        for row in kept:
            raw = row.pop("_raw", None)
            if raw is not None:
                handle.write(str(raw) + "\n")
            else:
                handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    return removed


async def _run_commodity(args: argparse.Namespace) -> None:
    symbols = args.commodity_symbol or None
    runner = CommodityFuturesWalkForwardRunner(
        symbols=symbols,
        lookback_days=args.commodity_lookback_days,
        lots=args.commodity_lots,
    )
    await runner.run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap RAG trade cases from NSE and commodity walk-forward outputs.")
    parser.add_argument("--run-nse", action="store_true", help="Regenerate NSE index option walk-forward outputs first.")
    parser.add_argument("--run-commodity", action="store_true", help="Fetch/replay commodity futures history first.")
    parser.add_argument("--commodity-symbol", action="append", default=[], help="Limit commodity replay to a symbol; repeatable.")
    parser.add_argument("--commodity-lookback-days", type=int, default=21)
    parser.add_argument("--commodity-lots", type=int, default=1)
    parser.add_argument("--max-nse-cases", type=int, default=500)
    parser.add_argument("--max-commodity-cases", type=int, default=250)
    parser.add_argument("--replace-existing", action="store_true", help="Replace existing walk-forward cases from these sources before appending.")
    args = parser.parse_args()

    if args.run_nse:
        IndexOptionWalkForwardRunner().run()
    if args.run_commodity:
        asyncio.run(_run_commodity(args))

    nse_rows = _read_csv(NSE_TRADES_PATH)[-args.max_nse_cases :]
    commodity_rows = _read_csv(COMMODITY_TRADES_PATH)[-args.max_commodity_cases :]
    cases = [_nse_case(row) for row in nse_rows if row.get("entry_time")]
    cases.extend(_commodity_case(row) for row in commodity_rows if row.get("entry_time"))

    removed = 0
    if args.replace_existing:
        removed = _remove_existing_sources({"walkforward_macd_rsi", "commodity_walkforward"})
    before = rag_service.health().get("trade_cases", 0)
    appended, skipped = _append_deduped(cases)
    after = rag_service.health().get("trade_cases", 0)
    print(
        "Bootstrapped walk-forward RAG trade cases: "
        f"candidates={len(cases)} appended={appended} skipped_existing={skipped} "
        f"removed_existing={removed} before={before} after={after}"
    )
    print(f"NSE source rows={len(nse_rows)} path={NSE_TRADES_PATH}")
    print(f"Commodity source rows={len(commodity_rows)} path={COMMODITY_TRADES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
