"""Strategy Dashboard API — MACD + MP + VWAP signals, trades, portfolio.

Serves the /strategy frontend page with:
  - Data pipeline status (spot candles, option contracts, MP params)
  - Signal generation (current MACD zero-cross signals across underlyings)
  - Agent commentary (reasoning for current decisions)
  - Order book / trade book
  - Portfolio statistics (equity curve, win rate, drawdown, monthly P&L)
"""
from __future__ import annotations

import asyncio
import gzip
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from paper_engine.strategy_agent import paper_strategy_agent
from paper_engine.strategy_learning import strategy_learning_service

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "runtime" / "index_analytics_data"

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY2_INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
STRATEGY1_FOCUS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_read_csv(path: Path) -> list[dict]:
    """Read CSV to list of dicts, return [] if missing."""
    if not path.exists():
        return []
    import csv
    with open(path) as f:
        return list(csv.DictReader(f))


def _count_gz_rows(path: Path) -> int:
    """Count data rows in a .csv.gz file."""
    if not path.exists():
        return 0
    import csv
    with gzip.open(path, "rt") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return sum(1 for _ in reader)


def _inspect_gz_csv(path: Path, field: str) -> tuple[int, str]:
    """Return gzipped CSV row count and latest field value in one pass."""
    if not path.exists():
        return 0, "—"
    import csv

    count = 0
    last_value = "—"
    with gzip.open(path, "rt") as handle:
        for row in csv.DictReader(handle):
            count += 1
            last_value = row.get(field, last_value)
    return count, last_value


def _parse_dateish(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw or raw == "—":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _format_dateish(value: Any) -> str:
    parsed = _parse_dateish(value)
    if parsed is None:
        return str(value or "—")
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%d %H:%M")


def _staleness_status(
    value: Any,
    *,
    row_count: int = 0,
    max_age_hours: Optional[float] = None,
    max_age_days: Optional[int] = None,
) -> str:
    if row_count <= 0:
        return "missing"
    parsed = _parse_dateish(value)
    if parsed is None:
        return "warning"
    now = datetime.now(IST)
    age = now - parsed
    if max_age_hours is not None and age > timedelta(hours=max_age_hours):
        return "warning"
    if max_age_days is not None and age > timedelta(days=max_age_days):
        return "warning"
    return "ok"


def _status_icon_detail(status: str) -> str:
    if status == "ok":
        return "live"
    if status == "warning":
        return "stale"
    return "missing"


def _build_source(
    *,
    name: str,
    rows: int,
    last_date: Any,
    detail: str,
    max_age_hours: Optional[float] = None,
    max_age_days: Optional[int] = None,
) -> dict[str, Any]:
    status = _staleness_status(
        last_date,
        row_count=rows,
        max_age_hours=max_age_hours,
        max_age_days=max_age_days,
    )
    return {
        "name": name,
        "status": status,
        "rows": rows,
        "last_date": _format_dateish(last_date),
        "detail": detail,
        "freshness": _status_icon_detail(status),
    }


def _strategy2_mp_path(underlying: str) -> Path:
    return DATA_ROOT / "market_profile" / f"underlying={underlying}" / "enriched_mp_with_failures.csv"


def _strategy2_spot_path(underlying: str) -> Path:
    return DATA_ROOT / "spot" / f"underlying={underlying}" / "1minute.csv.gz"


def _find_strategy(agent_status: dict[str, Any], key: str) -> dict[str, Any]:
    for strategy in agent_status.get("strategies", []) or []:
        if strategy.get("key") == key:
            return strategy
    return {}


def _empty_portfolio_stats(underlying: str, source: str) -> dict[str, Any]:
    return {
        "underlying": underlying,
        "strategy": "No portfolio data yet",
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "avg_return": 0.0,
        "median_return": 0.0,
        "sharpe_ratio": 0.0,
        "max_drawdown_pct": 0.0,
        "catastrophic_trades": 0,
        "final_equity": 100_000,
        "final_equity_lakhs": 1.0,
        "start_capital": 100_000,
        "total_return_pct": 0.0,
        "equity_curve": [{"trade": 0, "equity": 100_000, "date": ""}],
        "monthly": [],
        "source": source,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  1. DATA STATUS
# ══════════════════════════════════════════════════════════════════════════════

class DataSourceStatus(BaseModel):
    name: str
    status: str  # ok | warning | missing
    rows: int
    last_date: str
    detail: str


@router.get("/data-status")
async def get_data_status() -> dict[str, Any]:
    """Pipeline health separated into live execution, Strategy 2 live runtime, and archive lanes."""
    live_pipeline: list[dict[str, Any]] = []
    strategy2_pipeline: list[dict[str, Any]] = []
    archive_pipeline: list[dict[str, Any]] = []

    agent_status = paper_strategy_agent.get_status(refresh=False)
    strat1 = _find_strategy(agent_status, "macd_strategy")
    strat2 = _find_strategy(agent_status, "index_mp_strategy")
    n_pos = strat1.get("summary", {}).get("open_positions", 0)
    n_trades = strat1.get("summary", {}).get("total_trades", 0)
    live_pipeline.append(
        _build_source(
            name="Strategy 1 Agent Runtime",
            rows=max(n_pos, n_trades, 1 if agent_status.get("last_run_at") else 0),
            last_date=strat1.get("last_scan_at") or agent_status.get("last_run_at"),
            detail=f"{n_pos} open positions · {n_trades} closed trades",
            max_age_hours=0.2,
        )
    )

    try:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            for table, label, max_age_hours in [
                ("option_premium_candles", "Strategy 1 Option Premium Candles", 24.0),
                ("underlying_spot_candles", "Strategy 1 Spot Candles", 12.0),
                ("macd_signals", "Strategy 1 Stored MACD Signals", 48.0),
                ("orders", "Strategy 1 Paper Orders", 48.0),
            ]:
                try:
                    result = await session.execute(
                        text(
                            f"SELECT COUNT(*) as cnt, MAX(time)::text as latest FROM {table}"
                            if table in ("option_premium_candles", "underlying_spot_candles", "macd_signals")
                            else f"SELECT COUNT(*) as cnt, MAX(created_at)::text as latest FROM {table}"
                        )
                    )
                    row = result.fetchone()
                    live_pipeline.append(
                        _build_source(
                            name=label,
                            rows=int(row.cnt if row else 0),
                            last_date=row.latest if row else None,
                            detail=f"{int(row.cnt if row else 0):,} rows in DB",
                            max_age_hours=max_age_hours,
                        )
                    )
                except Exception:
                    live_pipeline.append(
                        {
                            "name": label,
                            "status": "missing",
                            "rows": 0,
                            "last_date": "—",
                            "detail": "Table query failed",
                            "freshness": "missing",
                        }
                    )
    except Exception as exc:
        logger.debug(f"[Strategy] DB status check failed: {exc}")

    strategy2_live = strat2.get("meta", {}).get("pipeline", []) or []
    if strategy2_live:
        strategy2_pipeline.extend(strategy2_live)
    else:
        strategy2_pipeline.append(
            _build_source(
                name="Strategy 2 Runtime",
                rows=1 if strat2.get("last_scan_at") else 0,
                last_date=strat2.get("last_scan_at"),
                detail=strat2.get("last_message") or "Waiting for live 5-minute index scan.",
                max_age_hours=0.2,
            )
        )

    mp_path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    mp_rows = _safe_read_csv(mp_path)
    archive_pipeline.append(
        _build_source(
            name="Archive Daily MP Research",
            rows=len(mp_rows),
            last_date=mp_rows[-1].get("date", "—") if mp_rows else "—",
            detail="SENSEX/Baseline research archive",
            max_age_days=14,
        )
    )

    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    trade_rows = _safe_read_csv(tr_path)
    sensex = [row for row in trade_rows if row.get("underlying") == "SENSEX"]
    archive_pipeline.append(
        _build_source(
            name="Archive S2 Backtest Trades",
            rows=len(sensex),
            last_date=sensex[-1].get("entry_time", "—") if sensex else "—",
            detail="Backtest trade archive, not live execution",
            max_age_days=30,
        )
    )

    return {
        "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "live_pipeline": live_pipeline,
        "strategy2_pipeline": strategy2_pipeline,
        "archive_pipeline": archive_pipeline,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  2. SIGNAL GENERATION — Current MACD + MP signals
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/signals")
async def get_signals(underlying: str = "SENSEX", limit: int = 30) -> dict:
    """
    Return recent MP signals + MACD zero-cross signals.
    Combines:
      - Daily MP params (day type, IB, POC, VA)
      - Buyer/seller failure scores
      - MACD zero-cross events from option_mp trades
    """
    # Load enriched MP
    enr_path = DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    mp_path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"

    mp_signals = []
    if enr_path.exists():
        rows = _safe_read_csv(enr_path)
        for r in rows[-limit:]:
            bf = float(r.get("buyer_fail_score", 0))
            sf = float(r.get("seller_fail_score", 0))
            # Determine signal direction
            direction = "NEUTRAL"
            if bf >= 4 and sf < 2:
                direction = "PE"
            elif sf >= 4 and bf < 2:
                direction = "CE"
            elif bf >= 2 and sf >= 2:
                direction = "CONFLICT"

            mp_signals.append({
                "date": r.get("date", ""),
                "day_type": _classify_from_row(r),
                "poc": _flt(r, "poc"),
                "vah": _flt(r, "vah"),
                "val": _flt(r, "val"),
                "ibh": _flt(r, "ibh"),
                "ibl": _flt(r, "ibl"),
                "ibr": _flt(r, "ibr"),
                "buyer_fail": bf,
                "seller_fail": sf,
                "net_failure": bf - sf,
                "mp_direction": direction,
                "close": _flt(r, "close_price"),
                "daily_move": _flt(r, "daily_move"),
            })
    elif mp_path.exists():
        rows = _safe_read_csv(mp_path)
        for r in rows[-limit:]:
            mp_signals.append({
                "date": r.get("date", ""),
                "day_type": _classify_from_mp_row(r),
                "poc": _flt(r, "poc"),
                "vah": _flt(r, "vah"),
                "val": _flt(r, "val"),
                "ibh": _flt(r, "ibh"),
                "ibl": _flt(r, "ibl"),
                "ibr": _flt(r, "ibr"),
                "buyer_fail": 0,
                "seller_fail": 0,
                "net_failure": 0,
                "mp_direction": "NEUTRAL",
                "close": _flt(r, "close_price"),
                "daily_move": float(r.get("close_price", 0)) - float(r.get("open_price", 0))
                    if r.get("close_price") and r.get("open_price") else 0,
            })

    # Load recent MACD-based trades (S2)
    macd_signals = []
    fst_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    if fst_path.exists():
        rows = _safe_read_csv(fst_path)
        for r in rows[-limit:]:
            macd_signals.append({
                "expiry": r.get("exp", ""),
                "opt_type": r.get("opt_type", ""),
                "entry_date": r.get("entry_date", ""),
                "entry_time": r.get("entry_time", ""),
                "entry_price": _flt(r, "ep"),
                "ibr_target_pct": _flt(r, "ibr_tgt_pct"),
                "poc_alloc": _flt(r, "poc_alloc"),
                "spot_poc_rev": r.get("spot_poc_rev", ""),
                "return_A": _flt(r, "rA"),
                "return_B": _flt(r, "rB"),
                "return_C": _flt(r, "rC"),
            })

    return {
        "underlying": underlying,
        "mp_signals": mp_signals,
        "macd_signals": macd_signals,
        "latest_mp": mp_signals[-1] if mp_signals else None,
    }


def _flt(row: dict, key: str) -> float:
    try:
        return float(row.get(key, 0))
    except (ValueError, TypeError):
        return 0.0


def _round_or_none(value: Any, digits: int = 2) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _classify_from_row(r: dict) -> str:
    """Classify day type from enriched MP row."""
    fa_up = str(r.get("fa_up", "")).lower() == "true"
    fa_dn = str(r.get("fa_dn", "")).lower() == "true"
    ib_up = str(r.get("ib_broken_up", "")).lower() == "true"
    ib_dn = str(r.get("ib_broken_dn", "")).lower() == "true"
    sh = _flt(r, "session_high") if "session_high" in r else 0
    sl = _flt(r, "session_low") if "session_low" in r else 0
    ibr = _flt(r, "ibr")
    close = _flt(r, "close_price") if "close_price" in r else _flt(r, "close")
    sr = sh - sl
    if sr <= 0 or ibr <= 0:
        return "UNKNOWN"
    rr = sr / ibr
    cp = (close - sl) / sr if sr > 0 else 0.5
    if (ib_up != ib_dn) and rr >= 2.0:
        if ib_up and cp >= 0.70:
            return "TREND_UP"
        if ib_dn and cp <= 0.30:
            return "TREND_DN"
    if ib_up and ib_dn and rr >= 1.5:
        return "DOUBLE_DIST"
    if (ib_up != ib_dn) and rr >= 1.2:
        return "NORMAL_VAR_UP" if ib_up else "NORMAL_VAR_DN"
    if fa_up or fa_dn:
        return "FAILED_AUCTION"
    return "NORMAL"


def _classify_from_mp_row(r: dict) -> str:
    return _classify_from_row(r)


def _build_strategy1_live_signals(agent_status: dict[str, Any]) -> list[dict[str, Any]]:
    strategies = agent_status.get("strategies", [])
    if not strategies:
        return []
    signals: list[dict[str, Any]] = []
    for pos in strategies[0].get("positions", []):
        signals.append(
            {
                "strategy": "Strategy 1",
                "source": "live_paper",
                "underlying": pos.get("underlying", ""),
                "signal_date": (pos.get("entered_at") or "")[:10],
                "trade_date": "live",
                "as_of": _format_dateish(agent_status.get("last_run_at")),
                "direction": pos.get("option_type", "CE"),
                "reason": pos.get("signal_reason", "macd_zero_cross"),
                "strength": "active",
                "status": f"open ({pos.get('phase', 'phase1')})",
                "freshness": _status_icon_detail(
                    _staleness_status(agent_status.get("last_run_at"), row_count=1, max_age_hours=0.2)
                ),
                "instruction": (
                    f"{pos.get('underlying', '')} {pos.get('option_type', '')} {pos.get('strike', '')} "
                    f"@ {pos.get('entry_price', 0):.1f} → LTP {pos.get('current_price', 0):.1f} "
                    f"({pos.get('return_pct', 0):+.1f}%)"
                ),
            }
        )
    return signals


def _strategy1_snapshot_priority(row: dict[str, Any]) -> float:
    status = str(row.get("status") or "")
    macd = abs(float(row.get("macd") or 0.0))
    histogram = abs(float(row.get("macd_histogram") or 0.0))
    if status == "entry-ready":
        return 100.0 + macd + histogram
    if status == "trend-aligned":
        return 60.0 + macd + histogram
    if status == "waiting-cross":
        return 25.0 + histogram
    return -20.0


async def _build_strategy1_snapshot_watchlist_signals(limit: int = 500) -> list[dict[str, Any]]:
    try:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            latest_day = await session.scalar(
                text(
                    """
                    SELECT MAX(timezone('Asia/Kolkata', time)::date)
                    FROM atm_option_watchlist_snapshots
                    WHERE ltp IS NOT NULL
                    """
                )
            )
            if latest_day is None:
                return []

            result = await session.execute(
                text(
                    """
                    WITH bucketed AS (
                        SELECT *,
                               COALESCE(
                                   NULLIF(instrument_key, ''),
                                   underlying || ':' || expiry::text || ':' || strike::text || ':' || option_type
                               ) AS option_key,
                               (
                                   date_trunc('hour', timezone('Asia/Kolkata', time))
                                   + (
                                       floor(date_part('minute', timezone('Asia/Kolkata', time)) / 30)::int
                                       * interval '30 minutes'
                                   )
                               ) AS macd_bucket,
                               ROW_NUMBER() OVER (
                                   PARTITION BY COALESCE(
                                                    NULLIF(instrument_key, ''),
                                                    underlying || ':' || expiry::text || ':' || strike::text || ':' || option_type
                                                ),
                                                (
                                                    date_trunc('hour', timezone('Asia/Kolkata', time))
                                                    + (
                                                        floor(date_part('minute', timezone('Asia/Kolkata', time)) / 30)::int
                                                        * interval '30 minutes'
                                                    )
                                                )
                                   ORDER BY time DESC
                               ) AS bucket_rn
                        FROM atm_option_watchlist_snapshots
                        WHERE timezone('Asia/Kolkata', time)::date = :latest_day
                    ),
                    ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY option_key
                                   ORDER BY macd_bucket DESC
                               ) AS rn
                        FROM bucketed
                        WHERE bucket_rn = 1
                    )
                    SELECT option_key,
                           underlying,
                           kind,
                           expiry::text AS expiry,
                           strike,
                           option_type,
                           source_broker,
                           instrument_key,
                           trading_symbol,
                           underlying_price,
                           ltp,
                           change_pct,
                           oi,
                           volume,
                           iv,
                           macd,
                           macd_signal,
                           macd_histogram,
                           rsi,
                           time,
                           macd_bucket,
                           rn
                    FROM ranked
                    WHERE rn <= 2
                    ORDER BY underlying ASC, option_type ASC, rn ASC
                    """
                ),
                {"latest_day": latest_day},
            )
            snapshot_rows = result.mappings().all()
    except Exception as exc:
        logger.debug(f"[Strategy] Strategy 1 snapshot classifier unavailable: {exc}")
        return []

    grouped: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for raw in snapshot_rows:
        option_key = str(raw.get("option_key") or "").strip()
        if not option_key:
            continue
        try:
            rank = int(raw.get("rn") or 0)
        except (TypeError, ValueError):
            continue
        grouped[option_key][rank] = dict(raw)

    learning_scores = await strategy_learning_service.load_scores("macd_strategy")
    signals: list[dict[str, Any]] = []
    for option_key, ranks in grouped.items():
        current = ranks.get(1) or {}
        previous = ranks.get(2) or {}
        underlying = str(current.get("underlying") or "").strip()
        direction = str(current.get("option_type") or "").upper().strip()
        if not underlying or direction not in {"CE", "PE"}:
            continue

        def _flt_value(mapping: dict[str, Any], key: str) -> Optional[float]:
            try:
                value = mapping.get(key)
                if value is None or value == "":
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        current_macd = _flt_value(current, "macd")
        previous_macd = _flt_value(previous, "macd")
        current_hist = _flt_value(current, "macd_histogram")
        ltp = _flt_value(current, "ltp")
        iv = _flt_value(current, "iv")
        iv_pct = (iv * 100.0 if iv is not None and iv <= 1.0 else iv)
        bucket_time = current.get("macd_bucket") or current.get("time")
        as_of = current.get("time") or bucket_time

        if current_macd is None:
            status = "missing-indicators"
            reason = "macd_unavailable"
            strength = "avoid"
            instruction = (
                f"{underlying} {direction}: latest ATM snapshot has no MACD yet. "
                "Need enough 30-minute premium history for this contract."
            )
        elif direction == "CE" and previous_macd is not None and previous_macd <= 0 < current_macd:
            status = "entry-ready"
            reason = "ce_macd_zero_cross"
            strength = "strong"
            instruction = f"{underlying} CE: fresh MACD zero-cross from {previous_macd:.4f} to {current_macd:.4f}."
        elif direction == "PE" and previous_macd is not None and previous_macd >= 0 > current_macd:
            status = "entry-ready"
            reason = "pe_macd_zero_cross"
            strength = "strong"
            instruction = f"{underlying} PE: fresh MACD zero-cross from {previous_macd:.4f} to {current_macd:.4f}."
        elif direction == "CE" and current_macd > 0:
            status = "trend-aligned"
            reason = "ce_macd_already_above_zero"
            strength = "monitoring"
            instruction = f"{underlying} CE: MACD is already above zero; wait for a new live zero-cross before entry."
        elif direction == "PE" and current_macd < 0:
            status = "trend-aligned"
            reason = "pe_macd_already_below_zero"
            strength = "monitoring"
            instruction = f"{underlying} PE: MACD is already below zero; wait for a new live zero-cross before entry."
        else:
            status = "waiting-cross"
            reason = f"{direction.lower()}_macd_not_crossed"
            strength = "standby"
            instruction = f"{underlying} {direction}: MACD has not crossed into the actionable side yet."

        signal = {
            "strategy": "Strategy 1",
            "source": "persisted_atm_snapshot",
            "symbol": f"{underlying} {direction}",
            "underlying": underlying,
            "signal_date": str(latest_day),
            "trade_date": "latest persisted session",
            "as_of": _format_dateish(as_of),
            "direction": direction,
            "reason": reason,
            "strength": strength,
            "status": status,
            "freshness": _status_icon_detail(
                _staleness_status(as_of, row_count=1, max_age_days=3)
            ),
            "instruction": instruction,
            "expiry": current.get("expiry"),
            "atm_strike": _round_or_none(current.get("strike"), 2),
            "strike": _round_or_none(current.get("strike"), 2),
            "ltp": _round_or_none(ltp, 2),
            "iv_pct": _round_or_none(iv_pct, 2),
            "macd": _round_or_none(current_macd, 4),
            "previous_macd": _round_or_none(previous_macd, 4),
            "macd_histogram": _round_or_none(current_hist, 4),
            "rsi": _round_or_none(current.get("rsi"), 2),
            "priority_score": 0.0,
            "option_last_bar_time": _format_dateish(bucket_time),
            "spot_last_time": _format_dateish(as_of),
            "indicator_bucket": status,
            "instrument_key": current.get("instrument_key"),
            "trading_symbol": current.get("trading_symbol"),
        }
        score = strategy_learning_service.pick_score(
            learning_scores,
            strategy_key="macd_strategy",
            underlying=underlying,
            option_type=direction,
            signal_reason=reason,
        )
        strategy_learning_service.annotate_payload(signal, score)
        signal["priority_score"] = _round_or_none(
            _strategy1_snapshot_priority(signal) + float(signal.get("learning_score") or 0.0),
            4,
        )
        signals.append(signal)

    best_by_side: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in signals:
        key = (str(signal.get("underlying") or ""), str(signal.get("direction") or ""))
        existing = best_by_side.get(key)
        if existing is None:
            best_by_side[key] = signal
            continue
        current_rank = (
            float(signal.get("priority_score") or 0.0),
            _parse_dateish(signal.get("as_of")) or datetime.min.replace(tzinfo=IST),
        )
        existing_rank = (
            float(existing.get("priority_score") or 0.0),
            _parse_dateish(existing.get("as_of")) or datetime.min.replace(tzinfo=IST),
        )
        if current_rank > existing_rank:
            best_by_side[key] = signal

    deduped = list(best_by_side.values())
    deduped.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
    return deduped[:limit]


def _build_strategy1_watchlist_signals(agent_status: dict[str, Any]) -> list[dict[str, Any]]:
    strat = _find_strategy(agent_status, "macd_strategy")
    prepared_watchlist = (strat.get("meta", {}) or {}).get("prepared_watchlist") or []
    if prepared_watchlist:
        return [dict(item) for item in prepared_watchlist]

    regime_summary = agent_status.get("regime_summary", {}) or {}
    signals: list[dict[str, Any]] = []
    as_of = agent_status.get("last_run_at")
    for underlying in STRATEGY1_FOCUS:
        regime = regime_summary.get(underlying)
        if regime not in {"bullish", "bearish"}:
            continue
        direction = "CE" if regime == "bullish" else "PE"
        signals.append(
            {
                "strategy": "Strategy 1",
                "source": "live_scan",
                "underlying": underlying,
                "signal_date": (as_of or "")[:10],
                "trade_date": "scanning",
                "as_of": _format_dateish(as_of),
                "direction": direction,
                "reason": f"regime_{regime}",
                "strength": "monitoring",
                "status": "watching",
                "freshness": _status_icon_detail(
                    _staleness_status(as_of, row_count=1, max_age_hours=0.2)
                ),
                "instruction": f"{underlying}: {regime} regime on 30m ATM options, waiting for a fresh MACD zero-cross.",
            }
        )
    return signals


async def _build_strategy1_watchlist_signals_live(agent_status: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_watchlist = await _build_strategy1_snapshot_watchlist_signals()
    if snapshot_watchlist:
        return snapshot_watchlist
    return _build_strategy1_watchlist_signals(agent_status)


def _build_strategy2_live_signals(agent_status: dict[str, Any]) -> list[dict[str, Any]]:
    strat = _find_strategy(agent_status, "index_mp_strategy")
    positions = {pos.get("underlying"): pos for pos in strat.get("positions", [])}
    signals: list[dict[str, Any]] = []
    for raw in strat.get("signals", []) or []:
        signal = dict(raw)
        underlying = signal.get("underlying", "")
        position = positions.get(underlying)
        if position:
            signal["status"] = "active"
            signal["instruction"] = (
                f"{underlying}: live {position.get('option_type')} position open at "
                f"{position.get('entry_price', 0):.1f} → {position.get('current_price', 0):.1f} "
                f"({position.get('return_pct', 0):+.1f}%)."
            )
            signal["direction"] = position.get("option_type", signal.get("direction"))
        signal["as_of"] = _format_dateish(signal.get("as_of"))
        signal["option_last_bar_time"] = _format_dateish(signal.get("option_last_bar_time"))
        signal["spot_last_time"] = _format_dateish(signal.get("spot_last_time"))
        signals.append(signal)
    return signals


def _build_strategy2_signal(underlying: str) -> dict[str, Any]:
    rows = _safe_read_csv(_strategy2_mp_path(underlying))
    if not rows:
        return {
            "strategy": "Strategy 2",
            "source": "research_snapshot",
            "underlying": underlying,
            "signal_date": "",
            "trade_date": "research snapshot",
            "as_of": "—",
            "direction": None,
            "reason": "pipeline_missing",
            "strength": "unavailable",
            "status": "not-ready",
            "freshness": "missing",
            "instruction": f"{underlying}: no MP research snapshot is available for the 5-minute index workflow.",
        }

    latest = rows[-1]
    bf = float(latest.get("buyer_fail_score", 0) or 0)
    sf = float(latest.get("seller_fail_score", 0) or 0)
    day_type = _classify_from_row(latest)
    signal_date = latest.get("date", "")
    direction: Optional[str] = None
    strength = "base"
    reason = day_type
    fa_up = str(latest.get("fa_up", "")).lower() == "true"
    fa_dn = str(latest.get("fa_dn", "")).lower() == "true"

    if day_type == "TREND_UP":
        direction, strength = "CE", "strong"
    elif day_type == "TREND_DN":
        direction, strength = "PE", "strong"
    elif day_type == "NORMAL_VAR_UP":
        direction = "CE"
    elif day_type == "NORMAL_VAR_DN":
        direction = "PE"
    elif day_type == "FAILED_AUCTION":
        if fa_up and not fa_dn:
            direction, reason = "PE", "FA_UP"
        elif fa_dn and not fa_up:
            direction, reason = "CE", "FA_DN"

    if bf >= 4 and sf < 2:
        direction, strength, reason = "PE", "strong", f"{reason}+BF{bf:.0f}"
    elif sf >= 4 and bf < 2:
        direction, strength, reason = "CE", "strong", f"{reason}+SF{sf:.0f}"

    if bf >= 2 and sf >= 2 and day_type not in ("TREND_UP", "TREND_DN"):
        direction = None
        reason = f"{reason}+CONFLICT"

    freshness = _status_icon_detail(
        _staleness_status(signal_date, row_count=1, max_age_days=2)
    )
    if direction:
        instruction = (
            f"{underlying}: latest MP snapshot points to {direction}. "
            "This repo has the research signal only; no live 5-minute execution loop is active."
        )
        status = "research-only"
    else:
        instruction = (
            f"{underlying}: latest MP snapshot is not actionable. "
            "No live 5-minute execution loop is active in this app."
        )
        status = "standby"

    return {
        "strategy": "Strategy 2",
        "source": "research_snapshot",
        "underlying": underlying,
        "signal_date": signal_date,
        "trade_date": "research snapshot",
        "as_of": _format_dateish(signal_date),
        "direction": direction,
        "reason": reason,
        "strength": strength,
        "status": status,
        "freshness": freshness,
        "instruction": instruction,
    }


def _build_strategy2_signals() -> list[dict[str, Any]]:
    return [_build_strategy2_signal(underlying) for underlying in STRATEGY2_INDICES]


# ══════════════════════════════════════════════════════════════════════════════
#  3. AGENT COMMENTARY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/agent-comments")
async def get_agent_comments(limit: int = 20) -> list[dict]:
    """
    Live agent commentary from the paper strategy agent.
    Falls back to static MP-based comments when agent has none.
    """
    # Primary source: live agent commentary
    agent_status = paper_strategy_agent.get_status()
    live_commentary = agent_status.get("commentary", [])

    if live_commentary:
        # Map agent commentary format to frontend format
        comments = []
        for entry in live_commentary[-limit:]:
            tone = entry.get("tone", "info")
            # Map tone → level for frontend
            level_map = {
                "success": "bullish",
                "warning": "warning",
                "error": "bearish",
                "info": "info",
                "idle": "neutral",
            }
            comments.append({
                "time": entry.get("time", ""),
                "type": entry.get("scope", "agent"),
                "level": level_map.get(tone, "info"),
                "message": entry.get("message", ""),
            })
        return comments

    # Fallback: static MP-based commentary from CSV files
    comments = []

    # Load latest MP data
    enr_path = DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    if enr_path.exists():
        rows = _safe_read_csv(enr_path)
        if rows:
            latest = rows[-1]
            bf = float(latest.get("buyer_fail_score", 0))
            sf = float(latest.get("seller_fail_score", 0))
            d = latest.get("date", "")
            move = float(latest.get("daily_move", 0))
            day_type = _classify_from_row(latest)

            comments.append({
                "time": d,
                "type": "day_summary",
                "level": "info",
                "message": f"{d}: {day_type} day, SENSEX {move:+.0f} pts. "
                           f"Buyer fail={bf:.0f}, Seller fail={sf:.0f}.",
            })

            if bf >= 4 and sf < 2:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "bearish",
                    "message": f"Buyers failed hard (score {bf:.0f}). "
                               f"PE bias for next session.",
                })
            elif sf >= 4 and bf < 2:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "bullish",
                    "message": f"Sellers failed hard (score {sf:.0f}). "
                               f"CE bias for next session.",
                })

    return comments[-limit:]


# ══════════════════════════════════════════════════════════════════════════════
#  4. TRADE BOOK
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trades")
async def get_strategy_trades(
    strategy: str = "target_50pct",
    underlying: str = "SENSEX",
    limit: int = 0,
    source: str = Query("live", description="'live' for agent only (default), 'csv' for backtest only, 'auto' for both"),
) -> dict:
    """Return strategy-triggered trades split into today vs history.

    Default 'source=live' returns only trades fired by the live paper agents
    (no backtest contamination). Set source='auto' or 'csv' to include the
    historical CSV backtest. Use limit=0 (default) for the full list.
    """
    trades: list[dict] = []

    # Live trades from the paper strategy agent (closed trade_history only;
    # don't re-emit exit events — they duplicate trades already in the history
    # and surface as rows with blank entry_time / entry_price).
    if source in ("auto", "live"):
        agent_status = paper_strategy_agent.get_status()
        strats = agent_status.get("strategies", [])
        for strat in strats:
            for t in strat.get("trade_history", []):
                entry_price = t.get("entry_price", 0) or 0
                exit_price = t.get("exit_price", 0) or 0
                pnl = t.get("pnl", 0) or 0
                blended_return = (
                    ((exit_price - entry_price) / entry_price * 100.0)
                    if entry_price > 0 else 0
                )
                sym = t.get("symbol") or ""
                trades.append({
                    "source": f"LIVE_{strat.get('key', 'paper').upper()}",
                    "underlying": sym.split(":")[1] if ":" in sym else sym,
                    "expiry": t.get("expiry", ""),
                    "option_type": t.get("option_type", ""),
                    "entry_time": t.get("entry_time", ""),
                    "entry_price": entry_price,
                    "exit_time": t.get("exit_time", ""),
                    "exit_price": exit_price,
                    "exit_reason": t.get("instrument_type", ""),
                    "blended_return": round(blended_return, 2),
                    "pnl": round(pnl, 2),
                    "alloc": 0.2,
                })

    # CSV backtested trades — only when explicitly requested.
    if source in ("auto", "csv"):
        tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
        if tr_path.exists():
            rows = _safe_read_csv(tr_path)
            s2 = [r for r in rows
                  if r.get("underlying") == underlying and r.get("strategy") == strategy]

            poc_lookup = {}
            poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
            if poc_path.exists():
                for r in _safe_read_csv(poc_path):
                    poc_lookup[r.get("entry_time", "")] = float(r.get("poc_alloc", 0.2))

            for r in s2:
                trades.append({
                    "source": "S2_MACD",
                    "underlying": underlying,
                    "expiry": r.get("expiry", ""),
                    "option_type": r.get("option_type", ""),
                    "entry_time": r.get("entry_time", ""),
                    "entry_price": _flt(r, "entry_price"),
                    "exit_time": r.get("exit_time", ""),
                    "exit_price": _flt(r, "exit_price"),
                    "exit_reason": r.get("exit_reason", ""),
                    "blended_return": _flt(r, "blended_return"),
                    "max_possible_return": _flt(r, "max_possible_return"),
                    "alloc": poc_lookup.get(r.get("entry_time", ""), 0.2),
                })

    # Sort recent-first. Prefer exit_time (when it exists) so closed trades
    # land in chronological order of when they completed.
    trades.sort(
        key=lambda t: t.get("exit_time") or t.get("entry_time") or "",
        reverse=True,
    )

    # Split into today vs history using IST session date.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _IST = _tz(_td(hours=5, minutes=30))
    today_date = _dt.now(_IST).date()
    today: list[dict] = []
    history: list[dict] = []
    for t in trades:
        ts_text = t.get("exit_time") or t.get("entry_time") or ""
        try:
            ts = _dt.fromisoformat(str(ts_text).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_IST)
            bucket_date = ts.astimezone(_IST).date()
        except Exception:
            bucket_date = None
        (today if bucket_date == today_date else history).append(t)

    capped = trades if limit <= 0 else trades[:limit]
    return {
        "total": len(trades),
        "today_count": len(today),
        "history_count": len(history),
        "today": today,
        "history": history,
        "trades": capped,  # backward-compat: combined recent-first list
    }


# ══════════════════════════════════════════════════════════════════════════════
#  5. PORTFOLIO STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/portfolio")
async def get_portfolio_stats(
    underlying: str = "SENSEX",
    source: str = Query("auto", description="'live' for agent only, 'csv' for backtest only, 'auto' prefers live"),
) -> dict:
    """
    Portfolio statistics — prioritizes live paper strategy agent data,
    falls back to S2 CSV backtest results.
    """
    # Try live portfolio from paper strategy agent
    if source in ("auto", "live"):
        agent_status = paper_strategy_agent.get_status()
        strats = agent_status.get("strategies", [])
        if strats:
            initial = sum(float(strat.get("summary", {}).get("initial_capital") or 0.0) for strat in strats) or 1_000_000
            final_equity = sum(float(strat.get("summary", {}).get("total_equity") or 0.0) for strat in strats) or initial
            total_trades = sum(int(strat.get("summary", {}).get("total_trades") or 0) for strat in strats)
            wins = sum(
                round((strat.get("summary", {}).get("win_rate") or 0) * (strat.get("summary", {}).get("total_trades") or 0))
                for strat in strats
            )
            losses = max(total_trades - wins, 0)
            avg_return_values = [float(strat.get("summary", {}).get("avg_win") or 0.0) for strat in strats]
            sharpe_values = [float(strat.get("summary", {}).get("sharpe_ratio") or 0.0) for strat in strats]
            drawdown_values = [float(strat.get("summary", {}).get("max_drawdown") or 0.0) for strat in strats]
            all_trade_history = [
                trade
                for strat in strats
                for trade in strat.get("trade_history", [])
            ]
            all_trade_history.sort(key=lambda trade: trade.get("exit_time") or trade.get("entry_time") or "")

            curve = [{"trade": 0, "equity": initial, "date": ""}]
            running_equity = initial
            for idx, trade in enumerate(all_trade_history, start=1):
                running_equity += float(trade.get("pnl") or 0.0)
                curve.append({
                    "trade": idx,
                    "equity": round(running_equity, 0),
                    "date": (trade.get("exit_time") or trade.get("entry_time") or "")[:16],
                })

            total_return_pct = round((final_equity - initial) / initial * 100, 1) if initial > 0 else 0
            win_rate_frac = wins / total_trades if total_trades else 0

            return {
                "underlying": "ALL (Live Paper)",
                "strategy": "Strategy 1 + Strategy 2 (Live)",
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate_frac * 100, 1),
                "avg_return": round(sum(avg_return_values) / max(len(avg_return_values), 1), 2),
                "median_return": 0,
                "sharpe_ratio": round(sum(sharpe_values) / max(len(sharpe_values), 1), 2),
                "max_drawdown_pct": round(max(drawdown_values or [0.0]) * 100, 2),
                "catastrophic_trades": 0,
                "final_equity": round(final_equity, 0),
                "final_equity_lakhs": round(final_equity / 1e5, 2),
                "start_capital": round(initial, 0),
                "total_return_pct": total_return_pct,
                "equity_curve": curve,
                "monthly": [],
                "source": "live_paper",
            }

    # Fallback: CSV backtest portfolio
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if not tr_path.exists():
        return _empty_portfolio_stats(underlying, "empty")

    rows = _safe_read_csv(tr_path)
    s2 = [r for r in rows
          if r.get("underlying") == underlying and r.get("strategy") == "target_50pct"]

    if not s2:
        return _empty_portfolio_stats(underlying, "empty")

    poc_lookup = {}
    poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    if poc_path.exists():
        for r in _safe_read_csv(poc_path):
            poc_lookup[r.get("entry_time", "")] = float(r.get("poc_alloc", 0.2))

    eq = 100_000.0
    curve = [{"trade": 0, "equity": eq, "date": ""}]
    rets = []
    monthly_pnl_map: dict[str, float] = defaultdict(float)
    monthly_trades_map: dict[str, list] = defaultdict(list)
    wins = 0
    losses = 0
    max_eq = eq
    max_dd = 0.0
    floor = -50.0

    for r in s2:
        ret = float(r.get("blended_return", 0))
        alloc = poc_lookup.get(r.get("entry_time", ""), 0.2)
        capped_ret = max(ret, floor)
        eq = eq + eq * alloc * capped_ret / 100.0
        rets.append(ret)

        entry_ts = r.get("entry_time", "")
        month = entry_ts[:7] if len(entry_ts) >= 7 else "?"
        monthly_pnl_map[month] += alloc * capped_ret
        monthly_trades_map[month].append(ret)

        if ret > 0:
            wins += 1
        else:
            losses += 1

        max_eq = max(max_eq, eq)
        dd = (max_eq - eq) / max_eq * 100
        max_dd = max(max_dd, dd)

        curve.append({
            "trade": len(rets),
            "equity": round(eq, 0),
            "date": entry_ts[:10],
        })

    import numpy as np
    avg_ret = float(np.mean(rets)) if rets else 0
    med_ret = float(np.median(rets)) if rets else 0
    std_ret = float(np.std(rets)) if rets else 1
    sharpe = avg_ret / std_ret if std_ret > 0 else 0

    monthly = []
    for m in sorted(monthly_pnl_map.keys()):
        mt = monthly_trades_map[m]
        monthly.append({
            "month": m,
            "trades": len(mt),
            "wins": sum(1 for r in mt if r > 0),
            "avg_return": round(float(np.mean(mt)), 2) if mt else 0,
            "eq_change_pct": round(monthly_pnl_map[m], 2),
            "win_rate": round(sum(1 for r in mt if r > 0) / len(mt) * 100, 1) if mt else 0,
        })

    cat_trades = sum(1 for r in rets if r < -50)

    return {
        "underlying": underlying,
        "strategy": "D★ (target_50pct + POC alloc)",
        "total_trades": len(rets),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(rets) * 100, 1) if rets else 0,
        "avg_return": round(avg_ret, 2),
        "median_return": round(med_ret, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "catastrophic_trades": cat_trades,
        "final_equity": round(eq, 0),
        "final_equity_lakhs": round(eq / 1e5, 2),
        "start_capital": 100_000,
        "total_return_pct": round((eq - 100_000) / 100_000 * 100, 1),
        "equity_curve": curve,
        "monthly": monthly,
        "source": "csv_backtest",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  6. ORDER BOOK (current open / pending signals)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/open-signals")
async def get_open_signals(underlying: str = "SENSEX") -> dict:
    await paper_strategy_agent.ensure_recovered_state()
    agent_status = paper_strategy_agent.get_status()
    # S2 deleted (2026-06-02): only Strategy 1 live positions + watchlist
    # are surfaced. strategy2_signals / strategy2_mode kept as empty
    # constants for one release so any not-yet-updated client doesn't
    # KeyError; they carry no data and should be dropped by consumers.
    live_positions = _build_strategy1_live_signals(agent_status)
    strategy1_watchlist = await _build_strategy1_watchlist_signals_live(agent_status)

    flattened = [
        *live_positions,
        *strategy1_watchlist,
    ]

    return {
        "as_of": _format_dateish(agent_status.get("last_run_at") or datetime.now(IST).isoformat()),
        "live_positions": live_positions,
        "strategy1_watchlist": strategy1_watchlist,
        "strategy2_signals": [],
        "strategy2_mode": "deleted",
        "signals": flattened[:12],
        "skip_reason": None if flattened else "No live entries or aligned strategy signals right now.",
    }


@router.get("/learning-summary")
async def get_learning_summary(
    refresh: bool = Query(False, description="Refresh persisted learning scores before returning the summary"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Return learned NSE strategy priors used for ranking and sizing."""
    return await strategy_learning_service.summary(refresh=refresh, limit=limit)


@router.post("/learning-refresh")
async def refresh_learning_scores(
    lookback_days: int = Query(120, ge=7, le=730),
) -> dict:
    """Recompute learned Strategy 1/2 scores from persisted signal/trade observations."""
    return await strategy_learning_service.refresh_scores(lookback_days=lookback_days)
