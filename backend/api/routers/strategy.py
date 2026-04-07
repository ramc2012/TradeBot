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

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "runtime" / "index_analytics_data"

IST = timezone(timedelta(hours=5, minutes=30))


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
async def get_data_status() -> list[dict]:
    """Pipeline health: spot candles, option contracts, MP params, trade results."""
    sources: list[dict] = []

    # 1. Spot 1-min candles
    import pandas as pd
    spot_path = DATA_ROOT / "spot" / "underlying=SENSEX" / "1minute.csv.gz"
    if spot_path.exists():
        df_last = pd.read_csv(gzip.open(spot_path, "rt"), usecols=["time"])
        last_ts = df_last["time"].iloc[-1] if len(df_last) > 0 else "?"
        sources.append({
            "name": "SENSEX Spot 1-min",
            "status": "ok", "rows": len(df_last),
            "last_date": str(last_ts)[:10],
            "detail": f"{len(df_last):,} candles",
        })
    else:
        sources.append({
            "name": "SENSEX Spot 1-min",
            "status": "missing", "rows": 0,
            "last_date": "—", "detail": "File not found",
        })

    # 2. Contract index
    ci_path = DATA_ROOT / "contract_index.json"
    if ci_path.exists():
        ci = json.loads(ci_path.read_text())
        sensex_weekly = [m for m in ci.values()
                         if m.get("underlying") == "SENSEX" and m.get("expiry_kind") == "weekly"]
        expiries = set(m["expiry"] for m in sensex_weekly if m.get("expiry"))
        sources.append({
            "name": "SENSEX Weekly Contracts",
            "status": "ok", "rows": len(sensex_weekly),
            "last_date": max(expiries) if expiries else "—",
            "detail": f"{len(sensex_weekly)} contracts, {len(expiries)} expiries",
        })
    else:
        sources.append({
            "name": "SENSEX Weekly Contracts",
            "status": "missing", "rows": 0,
            "last_date": "—", "detail": "contract_index.json not found",
        })

    # 3. Daily MP params
    mp_path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    if mp_path.exists():
        rows = _safe_read_csv(mp_path)
        last = rows[-1]["date"] if rows else "—"
        sources.append({
            "name": "Daily Market Profile",
            "status": "ok", "rows": len(rows),
            "last_date": last, "detail": f"{len(rows)} trading days",
        })
    else:
        sources.append({
            "name": "Daily Market Profile",
            "status": "missing", "rows": 0,
            "last_date": "—", "detail": "Run market_profile_analysis.py",
        })

    # 4. Enriched MP with failure scores
    enr_path = DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    if enr_path.exists():
        rows = _safe_read_csv(enr_path)
        sources.append({
            "name": "MP Failure Scores",
            "status": "ok", "rows": len(rows),
            "last_date": rows[-1].get("date", "—") if rows else "—",
            "detail": f"Buyer/seller failure scores for {len(rows)} days",
        })
    else:
        sources.append({
            "name": "MP Failure Scores",
            "status": "warning", "rows": 0,
            "last_date": "—", "detail": "Run mp_failure_research.py",
        })

    # 5. S2 Trade results
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if tr_path.exists():
        rows = _safe_read_csv(tr_path)
        sensex = [r for r in rows if r.get("underlying") == "SENSEX"]
        sources.append({
            "name": "S2 Trade Results",
            "status": "ok", "rows": len(sensex),
            "last_date": sensex[-1].get("entry_time", "—")[:10] if sensex else "—",
            "detail": f"{len(sensex)} SENSEX trades across strategies",
        })
    else:
        sources.append({
            "name": "S2 Trade Results",
            "status": "missing", "rows": 0,
            "last_date": "—", "detail": "Run option_mp_analysis.py",
        })

    # 6. MP+VWAP trades (experimental)
    vwap_path = DATA_ROOT / "mp_vwap" / "mp_vwap_trades.csv"
    if vwap_path.exists():
        rows = _safe_read_csv(vwap_path)
        sources.append({
            "name": "MP+VWAP Trades",
            "status": "ok", "rows": len(rows),
            "last_date": rows[-1].get("trade_date", "—") if rows else "—",
            "detail": f"{len(rows)} experimental trades",
        })
    else:
        sources.append({
            "name": "MP+VWAP Trades",
            "status": "warning", "rows": 0,
            "last_date": "—", "detail": "Run mp_vwap_strategy.py",
        })

    return sources


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


# ══════════════════════════════════════════════════════════════════════════════
#  3. AGENT COMMENTARY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/agent-comments")
async def get_agent_comments(limit: int = 20) -> list[dict]:
    """
    Generate contextual comments based on latest MP data and trade outcomes.
    This is the 'agent reasoning' panel — what the strategy sees and why.
    """
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

            # Day summary
            comments.append({
                "time": d,
                "type": "day_summary",
                "level": "info",
                "message": f"{d}: {day_type} day, SENSEX {move:+.0f} pts. "
                           f"Buyer fail={bf:.0f}, Seller fail={sf:.0f}.",
            })

            # Direction call
            if bf >= 4 and sf < 2:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "bearish",
                    "message": f"Buyers failed hard (score {bf:.0f}). "
                               f"PE bias for next session. Watch for VWAP confirmation on PE premium.",
                })
            elif sf >= 4 and bf < 2:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "bullish",
                    "message": f"Sellers failed hard (score {sf:.0f}). "
                               f"CE bias for next session. Wait for premium > VWAP to enter.",
                })
            elif bf >= 2 and sf >= 2:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "warning",
                    "message": f"CONFLICT — both sides failing (BF={bf:.0f}, SF={sf:.0f}). "
                               f"Choppy market likely. Skip or reduce allocation.",
                })
            else:
                comments.append({
                    "time": d, "type": "signal",
                    "level": "neutral",
                    "message": f"No strong failure signal. Balanced auction. "
                               f"Wait for clearer MP structure.",
                })

            # IB analysis
            ib_up = str(latest.get("ib_broken_up", "")).lower() == "true"
            ib_dn = str(latest.get("ib_broken_dn", "")).lower() == "true"
            close = float(latest.get("close_price", 0))
            ibh = float(latest.get("ibh", 0))
            ibl = float(latest.get("ibl", 0))
            if ib_up and close < (ibh + ibl) / 2:
                comments.append({
                    "time": d, "type": "ib_analysis",
                    "level": "bearish",
                    "message": f"IB extension UP failed — buyers broke ₹{ibh:.0f} but closed below IB mid. "
                               f"Seller conviction intact.",
                })
            if ib_dn and close > (ibh + ibl) / 2:
                comments.append({
                    "time": d, "type": "ib_analysis",
                    "level": "bullish",
                    "message": f"IB extension DN failed — sellers broke ₹{ibl:.0f} but closed above IB mid. "
                               f"Buyer conviction intact.",
                })

            # Poor high/low from enriched data
            poor_h = str(latest.get("poor_high", "")).lower() == "true"
            poor_l = str(latest.get("poor_low", "")).lower() == "true"
            if poor_h:
                comments.append({
                    "time": d, "type": "profile",
                    "level": "bearish",
                    "message": "Poor High detected — single-print at top, no buyer commitment. "
                               "Likely to revisit.",
                })
            if poor_l:
                comments.append({
                    "time": d, "type": "profile",
                    "level": "bullish",
                    "message": "Poor Low detected — single-print at bottom, no seller commitment. "
                               "Likely to revisit.",
                })

    # Load recent trade outcomes for commentary
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if tr_path.exists():
        import csv
        with open(tr_path) as f:
            rows = list(csv.DictReader(f))
        sensex = [r for r in rows
                   if r.get("underlying") == "SENSEX" and r.get("strategy") == "target_50pct"]
        recent = sensex[-5:]
        wins = sum(1 for r in recent if float(r.get("blended_return", 0)) > 0)
        if recent:
            streak = "winning" if wins >= 3 else "mixed" if wins >= 2 else "losing"
            comments.append({
                "time": recent[-1].get("entry_time", "")[:10],
                "type": "streak",
                "level": "info" if streak == "winning" else "warning" if streak == "losing" else "neutral",
                "message": f"Last 5 S2 trades: {wins}/5 wins ({streak} streak). "
                           f"Latest: {recent[-1].get('option_type','')} "
                           f"{float(recent[-1].get('blended_return', 0)):+.1f}%.",
            })

    return comments[-limit:]


# ══════════════════════════════════════════════════════════════════════════════
#  4. TRADE BOOK
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trades")
async def get_strategy_trades(
    strategy: str = "target_50pct",
    underlying: str = "SENSEX",
    limit: int = 50,
) -> dict:
    """Return S2 trades + expansion module trades."""
    trades = []

    # S2 baseline trades
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if tr_path.exists():
        rows = _safe_read_csv(tr_path)
        s2 = [r for r in rows
              if r.get("underlying") == underlying and r.get("strategy") == strategy]

        # Get POC alloc
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

    # Expansion module trades
    exp_path = DATA_ROOT / "expansion" / "expansion_module_trades.csv"
    if exp_path.exists():
        for r in _safe_read_csv(exp_path):
            trades.append({
                "source": r.get("module", "expansion"),
                "underlying": underlying,
                "expiry": r.get("expiry", ""),
                "option_type": r.get("option_type", ""),
                "entry_time": r.get("entry_time", ""),
                "entry_price": _flt(r, "entry_price"),
                "exit_time": r.get("exit_time", ""),
                "exit_price": _flt(r, "exit_price"),
                "exit_reason": r.get("exit_reason", ""),
                "blended_return": _flt(r, "blended_return"),
                "alloc": _flt(r, "alloc"),
            })

    # Sort by entry time desc, return most recent
    trades.sort(key=lambda t: t.get("entry_time", ""), reverse=True)
    return {
        "total": len(trades),
        "trades": trades[:limit],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  5. PORTFOLIO STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/portfolio")
async def get_portfolio_stats(underlying: str = "SENSEX") -> dict:
    """
    Compute portfolio statistics from S2 trade results:
    equity curve, win rate, drawdown, monthly P&L, Sharpe-like ratio.
    """
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    if not tr_path.exists():
        raise HTTPException(404, "Trade results not found")

    rows = _safe_read_csv(tr_path)
    s2 = [r for r in rows
          if r.get("underlying") == underlying and r.get("strategy") == "target_50pct"]

    if not s2:
        raise HTTPException(404, f"No trades for {underlying}")

    # POC alloc lookup
    poc_lookup = {}
    poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    if poc_path.exists():
        for r in _safe_read_csv(poc_path):
            poc_lookup[r.get("entry_time", "")] = float(r.get("poc_alloc", 0.2))

    # Build equity curve
    eq = 100_000.0
    curve = [{"trade": 0, "equity": eq, "date": ""}]
    rets = []
    monthly_pnl: dict[str, float] = defaultdict(float)
    monthly_trades: dict[str, list] = defaultdict(list)
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
        monthly_pnl[month] += alloc * capped_ret
        monthly_trades[month].append(ret)

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

    # Monthly breakdown
    monthly = []
    for m in sorted(monthly_pnl.keys()):
        mt = monthly_trades[m]
        monthly.append({
            "month": m,
            "trades": len(mt),
            "wins": sum(1 for r in mt if r > 0),
            "avg_return": round(float(np.mean(mt)), 2) if mt else 0,
            "eq_change_pct": round(monthly_pnl[m], 2),
            "win_rate": round(sum(1 for r in mt if r > 0) / len(mt) * 100, 1) if mt else 0,
        })

    # Catastrophic trades
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
    }


# ══════════════════════════════════════════════════════════════════════════════
#  6. ORDER BOOK (current open / pending signals)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/open-signals")
async def get_open_signals(underlying: str = "SENSEX") -> dict:
    """
    Return signals that are actionable for the next trading session.
    Based on the latest MP day's failure scores and day type.
    """
    enr_path = DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    if not enr_path.exists():
        return {"signals": [], "message": "No enriched MP data available"}

    rows = _safe_read_csv(enr_path)
    if not rows:
        return {"signals": [], "message": "No MP data"}

    latest = rows[-1]
    bf = float(latest.get("buyer_fail_score", 0))
    sf = float(latest.get("seller_fail_score", 0))
    day_type = _classify_from_row(latest)
    d = latest.get("date", "")

    signals = []

    # Determine direction from day type + failure scores
    direction = None
    strength = "base"
    reason = day_type

    if day_type == "TREND_UP":
        direction = "CE"
        strength = "strong"
    elif day_type == "TREND_DN":
        direction = "PE"
        strength = "strong"
    elif day_type == "NORMAL_VAR_UP":
        direction = "CE"
    elif day_type == "NORMAL_VAR_DN":
        direction = "PE"
    elif day_type in ("FAILED_AUCTION",):
        fa_up = str(latest.get("fa_up", "")).lower() == "true"
        fa_dn = str(latest.get("fa_dn", "")).lower() == "true"
        if fa_up and not fa_dn:
            direction = "PE"
            reason = "FA_UP"
        elif fa_dn and not fa_up:
            direction = "CE"
            reason = "FA_DN"

    # Failure score override
    if bf >= 4 and sf < 2:
        direction = "PE"
        strength = "strong"
        reason += f"+BF{bf:.0f}"
    elif sf >= 4 and bf < 2:
        direction = "CE"
        strength = "strong"
        reason += f"+SF{sf:.0f}"

    # Conflict filter
    if bf >= 2 and sf >= 2 and day_type not in ("TREND_UP", "TREND_DN"):
        direction = None
        reason += "+CONFLICT"

    alloc = 0.35 if strength == "strong" else 0.20

    if direction:
        signals.append({
            "signal_date": d,
            "trade_date": "next session",
            "direction": direction,
            "reason": reason,
            "strength": strength,
            "alloc": alloc,
            "buyer_fail": bf,
            "seller_fail": sf,
            "day_type": day_type,
            "status": "pending_vwap_confirm",
            "instruction": f"Enter {direction} when premium > VWAP after 09:30. "
                           f"VWAP stop after 60-min grace. Hard SL at -50%.",
        })

    return {
        "as_of": d,
        "signals": signals,
        "skip_reason": reason if not direction else None,
    }
