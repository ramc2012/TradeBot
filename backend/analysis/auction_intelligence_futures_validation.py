from __future__ import annotations

import asyncio
import csv
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routers.auth import ensure_fyers_session, get_active_adapter
from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.validation.engine import GateAValidator
from auction_intelligence.validation.gate_b import GateBValidator


IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
OUTPUT_ROOT = BACKEND_ROOT / "runtime" / "auction_intelligence" / "futures_validation"
SYMBOLS = {
    "NIFTY": {"lot_size": 65},
    "BANKNIFTY": {"lot_size": 30},
}


def _cont_fyers_symbol(symbol_code: str, as_of: date) -> str:
    return f"NSE:{symbol_code.upper()}{as_of.strftime('%y')}{as_of.strftime('%b').upper()}FUT"


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(IST)


async def _fetch_chunked_history(
    *,
    fyers_symbol: str,
    resolution: str,
    from_date: date,
    to_date: date,
    chunk_days: int,
) -> list[dict[str, Any]]:
    await ensure_fyers_session()
    adapter = get_active_adapter("fyers")
    if adapter is None:
        raise RuntimeError("Fyers session is not available.")

    merged: dict[str, dict[str, Any]] = {}
    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), to_date)
        rows = await adapter.get_historical_candles(
            fyers_symbol,
            resolution,
            chunk_start.isoformat(),
            chunk_end.isoformat(),
            cont_flag=1,
        )
        for row in rows:
            merged[str(row["time"])] = {
                "time": str(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
        chunk_start = chunk_end + timedelta(days=1)
    return [merged[key] for key in sorted(merged)]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["time", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)


def _to_market_bars(rows: list[dict[str, Any]]) -> list[MarketBar]:
    return [
        MarketBar(
            timestamp=_parse_timestamp(str(row["time"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0) or 0.0),
        )
        for row in rows
    ]


def _group_sessions(rows: list[dict[str, Any]]) -> dict[str, list[MarketBar]]:
    grouped: dict[str, list[MarketBar]] = {}
    for bar in _to_market_bars(rows):
        local_time = bar.timestamp.astimezone(IST)
        if local_time.time() < SESSION_OPEN or local_time.time() > SESSION_CLOSE:
            continue
        grouped.setdefault(local_time.date().isoformat(), []).append(bar)
    for bars in grouped.values():
        bars.sort(key=lambda item: item.timestamp)
    return grouped


def _data_quality_summary(rows: list[dict[str, Any]], sessions: dict[str, list[MarketBar]]) -> dict[str, Any]:
    timestamps = [str(row["time"]) for row in rows]
    bar_counts = {session_date: len(bars) for session_date, bars in sessions.items()}
    incomplete = [
        {"session_date": session_date, "bar_count": bar_count}
        for session_date, bar_count in bar_counts.items()
        if bar_count != 13
    ]
    distribution = Counter(bar_counts.values())
    return {
        "row_count": len(rows),
        "duplicate_timestamps": len(timestamps) - len(set(timestamps)),
        "session_count": len(sessions),
        "first_bar_time": rows[0]["time"] if rows else None,
        "last_bar_time": rows[-1]["time"] if rows else None,
        "bar_count_distribution": dict(sorted(distribution.items())),
        "incomplete_sessions": incomplete[:20],
        "incomplete_session_count": len(incomplete),
    }


def _build_latest_analysis(symbol_code: str, sessions: dict[str, list[MarketBar]]) -> dict[str, Any]:
    session_dates = sorted(sessions)
    if len(session_dates) < 2:
        return {"available": False, "reason": "At least two sessions are required."}

    current_bars = sessions[session_dates[-1]]
    prior_bars = sessions[session_dates[-2]]
    validator = GateBValidator()
    quote = validator._quote_from_bars(current_bars)
    depth = validator._depth_from_bars(current_bars)
    trades = validator._trades_from_bars(current_bars)

    service = AuctionIntelligenceService()
    bundle = service.analyze(
        session=SessionContext(
            symbol=f"{symbol_code} FUT",
            session_date=date.fromisoformat(session_dates[-1]),
            last_price=current_bars[-1].close,
            stale_data_seconds=0.0,
            minutes_to_close=0,
            broker_connected=True,
        ),
        bars=current_bars,
        prior_bars=prior_bars,
        quote=QuoteSnapshot(**asdict(quote)),
        trades=[TradePrint(**asdict(item)) for item in trades],
        depth=DepthSnapshot(
            timestamp=depth.timestamp,
            bids=[DepthLevel(**asdict(level)) for level in depth.bids],
            asks=[DepthLevel(**asdict(level)) for level in depth.asks],
        ),
        portfolio=PortfolioSnapshot(),
    )

    swing_decision = next((item for item in bundle.agent_decisions if item.agent_name == "swing"), None)
    return {
        "available": True,
        "session_date": session_dates[-1],
        "regime": bundle.regime.label,
        "regime_confidence": bundle.regime.confidence,
        "risk_allowed": bundle.risk.allowed,
        "kill_switch": bundle.risk.kill_switch,
        "swing_action": None if swing_decision is None else swing_decision.action,
        "swing_confidence": None if swing_decision is None else swing_decision.confidence,
        "swing_setup": None if swing_decision is None else swing_decision.metadata.get("setup_name"),
        "execution_plan_count": len(bundle.execution_plan),
    }


def _run_gate_a(symbol_code: str, sessions: dict[str, list[MarketBar]]) -> dict[str, Any]:
    session_dates = sorted(sessions)
    if len(session_dates) < 2:
        return {"available": False, "reason": "At least two sessions are required."}

    report = GateAValidator().validate(
        session=SessionContext(
            symbol=f"{symbol_code} FUT",
            session_date=date.fromisoformat(session_dates[-1]),
            last_price=sessions[session_dates[-1]][-1].close,
            stale_data_seconds=0.0,
            minutes_to_close=0,
            broker_connected=True,
        ),
        bars=sessions[session_dates[-1]],
        prior_bars=sessions[session_dates[-2]],
    )
    return {
        "available": True,
        "session_date": session_dates[-1],
        "passed": report.passed,
        "score": report.score,
        "metrics": report.metrics,
        "failed_checks": [asdict(item) for item in report.checks if not item.passed],
    }


def _run_gate_b(symbol_code: str, sessions: dict[str, list[MarketBar]]) -> dict[str, Any]:
    ordered_sessions = [sessions[key] for key in sorted(sessions)]
    report = GateBValidator().validate(
        symbol=f"{symbol_code} FUT",
        sessions=ordered_sessions,
        mode="historical_year",
        source="fyers_continuous_futures",
    )
    blocker_summary = {
        "skip_reason_attribution": report.metrics.get("skip_reason_attribution", {}),
        "flat_reason_attribution": report.metrics.get("flat_reason_attribution", {}),
        "blocking_reason_attribution": report.metrics.get("blocking_reason_attribution", {}),
    }
    return {
        "passed": report.passed,
        "score": report.score,
        "metrics": report.metrics,
        "blocker_summary": blocker_summary,
        "failed_checks": [asdict(item) for item in report.checks if not item.passed],
        "artifact_count": len(report.artifacts),
        "artifact_preview": [asdict(item) for item in report.artifacts[:10]],
    }


async def _validate_symbol(
    *,
    symbol_code: str,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    fyers_symbol = _cont_fyers_symbol(symbol_code, to_date)
    rows_30m = await _fetch_chunked_history(
        fyers_symbol=fyers_symbol,
        resolution="30",
        from_date=from_date,
        to_date=to_date,
        chunk_days=85,
    )
    rows_day = await _fetch_chunked_history(
        fyers_symbol=fyers_symbol,
        resolution="D",
        from_date=from_date,
        to_date=to_date,
        chunk_days=300,
    )

    file_prefix = f"{symbol_code.lower()}_fut_{from_date.isoformat()}_{to_date.isoformat()}"
    path_30m = OUTPUT_ROOT / f"{file_prefix}_30m.csv"
    path_day = OUTPUT_ROOT / f"{file_prefix}_day.csv"
    _write_csv(path_30m, rows_30m)
    _write_csv(path_day, rows_day)

    sessions = _group_sessions(rows_30m)
    return {
        "symbol": symbol_code,
        "fyers_continuous_symbol": fyers_symbol,
        "window_start": from_date.isoformat(),
        "window_end": to_date.isoformat(),
        "files": {
            "30m_csv": str(path_30m),
            "day_csv": str(path_day),
        },
        "thirty_minute_quality": _data_quality_summary(rows_30m, sessions),
        "daily_quality": {
            "row_count": len(rows_day),
            "first_bar_time": rows_day[0]["time"] if rows_day else None,
            "last_bar_time": rows_day[-1]["time"] if rows_day else None,
        },
        "gate_a_latest_session": _run_gate_a(symbol_code, sessions),
        "gate_b_year": _run_gate_b(symbol_code, sessions),
        "latest_analysis": _build_latest_analysis(symbol_code, sessions),
    }


async def main() -> None:
    to_date = date(2026, 4, 4)
    from_date = date(2025, 4, 5)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "window_start": from_date.isoformat(),
        "window_end": to_date.isoformat(),
        "symbols": {},
    }
    for symbol_code in SYMBOLS:
        report["symbols"][symbol_code] = await _validate_symbol(
            symbol_code=symbol_code,
            from_date=from_date,
            to_date=to_date,
        )

    summary_path = OUTPUT_ROOT / f"validation_summary_{from_date.isoformat()}_{to_date.isoformat()}.json"
    summary_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nSaved summary to {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
