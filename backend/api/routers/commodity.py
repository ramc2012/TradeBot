"""Commodity strategy routes — MP+OF futures-only sleeve.

The options sleeve and ATM-watchlist service have been deprecated; the
endpoints that backed them (`/atm-watchlist`, `/atm-watchlist/expiries`,
`PUT /strategy-agent/contracts`) are gone.

`/strategy-agent/contracts` and `/watchlist-snapshot` are retained as thin
catalog endpoints driven entirely from `COMMODITY_CONTRACT_SPECS`, so the
frontend's contract table keeps working without any expiry-discovery
backend behind it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import commodity_strategy_agent

router = APIRouter(prefix="/api/commodity", tags=["commodity"])


def _normalized_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _build_contract_catalog(symbols: list[str]) -> dict[str, object]:
    """Build the slim, options-free contract catalog the UI consumes.

    No broker calls — everything is sourced from the static specs.
    """
    contracts: list[dict[str, object]] = []
    for symbol in _normalized_symbols(symbols):
        spec = get_commodity_contract_spec(symbol)
        contracts.append(
            {
                "symbol": symbol,
                "underlying": spec.root,
                "display_name": spec.display_name,
                "lookup_symbol": symbol,
                "active_lookup_symbol": symbol,
                "default_lookup_symbol": symbol,
                "lot_size": spec.futures_lot_size,
                "tick_size": spec.mp_tick_size,
                "contract_unit_label": spec.contract_unit_label,
                "quote_unit_label": spec.quote_unit_label,
                "strategy_title": spec.futures_label,
                "has_options": False,
                "selection_policy": "futures_only",
                "selection_locked": True,
                "detail": "MP+OF futures-only sleeve; options were deprecated.",
            }
        )
    return {
        "source": "static_specs",
        "build_status": "ready",
        "detail": "MP+OF futures-only catalog (static specs; no broker discovery).",
        "contracts": contracts,
        "rows": contracts,
        "summary": {
            "total_symbols": len(contracts),
            "contracts_ready": len(contracts),
            "active_selections": len(contracts),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class CommodityConfigRequest(BaseModel):
    symbols: list[str]
    # Legacy field accepted but ignored — options sleeve is deprecated.
    selected_option_expiries: dict[str, str] | None = None


class KillSwitchRequest(BaseModel):
    active: bool


class ResetPaperRequest(BaseModel):
    confirm: str
    actor: Optional[str] = None


@router.post("/strategy-agent/reset-paper")
async def reset_commodity_paper_account(body: ResetPaperRequest):
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(
            status_code=400,
            detail=(
                "Paper reset is destructive. POST `{\"confirm\": \"RESET\"}` "
                "in the body to proceed."
            ),
        )
    actor = (body.actor or "manual").strip() or "manual"
    return await commodity_strategy_agent.archive_and_reset_paper_account(actor=actor)


# ── Hot-payload slimming (audit 2026-07-18) ──────────────────────────────────
# /overview + /strategy-agent/status weighed ~846KB/825KB: signal_audit alone
# was ~455KB (600 records) and two key pairs were byte-identical duplicates.
# UI field audit (frontend-v2 only — legacy /frontend was retired 2026-06-07):
#   * futures_watchlist is READ (page.tsx reads `status.futures_watchlist ??
#     status.watchlist`) → keep futures_watchlist, DROP the `watchlist` dup.
#   * trade_history is READ (page.tsx, reports-ledger.ts, strategy-position-
#     ledger.ts); historical_trades appears only in type decls and as a
#     LOWER-priority fallback (`trade_history || historical_trades`) → keep
#     trade_history + today_trades, DROP the `historical_trades` dup slice.
#   * signal_audit is never read from this payload (type decl only) → cap the
#     hot copy to the most recent N; the FULL history moves to the paginated
#     /strategy-agent/signal-audit endpoint below.
# Additive keys signal_audit_total/_capped let any consumer detect the cap.
_HOT_SIGNAL_AUDIT_CAP = 50


def _slim_agent_status(status: dict) -> dict:
    slim = dict(status or {})
    slim.pop("watchlist", None)  # exact duplicate of futures_watchlist
    slim.pop("historical_trades", None)  # duplicate slice of trade_history
    audit = slim.get("signal_audit")
    if isinstance(audit, list):
        slim["signal_audit_total"] = len(audit)
        slim["signal_audit_capped"] = len(audit) > _HOT_SIGNAL_AUDIT_CAP
        if len(audit) > _HOT_SIGNAL_AUDIT_CAP:
            slim["signal_audit"] = audit[:_HOT_SIGNAL_AUDIT_CAP]
    return slim


@router.get("/strategy-agent/status")
async def commodity_strategy_status():
    return _slim_agent_status(commodity_strategy_agent.get_status())


@router.get("/strategy-agent/signal-audit")
async def commodity_signal_audit(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=600),
):
    """Paginated FULL signal-audit history (the hot status payload carries only
    the most recent records — see _slim_agent_status)."""
    return commodity_strategy_agent.get_signal_audit(offset=offset, limit=limit)


@router.get("/overview")
async def commodity_overview():
    return {
        "status": _slim_agent_status(commodity_strategy_agent.get_status()),
        "kill_switch_state": commodity_strategy_agent.get_control_state(),
        "orders": commodity_strategy_agent.get_orders()[:40],
        "positions": commodity_strategy_agent.get_positions(),
        "reports": commodity_strategy_agent.get_reports()[:24],
    }


@router.post("/strategy-agent/start")
async def start_commodity_strategy_agent():
    return await commodity_strategy_agent.start_loop()


@router.post("/strategy-agent/run-once")
async def run_commodity_strategy_once(force: bool = True):
    return await commodity_strategy_agent.run_once(force=force)


@router.put("/strategy-agent/config")
async def update_commodity_strategy_config(body: CommodityConfigRequest):
    return commodity_strategy_agent.update_symbols(
        body.symbols,
        selected_option_expiries=body.selected_option_expiries,  # ignored
    )


@router.get("/strategy-agent/contracts")
async def commodity_strategy_contracts():
    symbols = commodity_strategy_agent.get_symbols()
    return _build_contract_catalog(symbols)


@router.get("/kill-switch")
async def commodity_kill_switch_state():
    return commodity_strategy_agent.get_control_state()


@router.put("/kill-switch")
async def update_commodity_kill_switch(body: KillSwitchRequest):
    return await commodity_strategy_agent.set_kill_switch(body.active)


@router.get("/watchlist-snapshot")
async def commodity_watchlist_snapshot(
    expiry: Optional[str] = Query(None),  # legacy arg, ignored
    live_refresh: bool = Query(False),  # legacy arg, ignored
):
    symbols = commodity_strategy_agent.get_symbols()
    status = commodity_strategy_agent.get_status() or {}
    return {
        "contract_catalog": _build_contract_catalog(symbols),
        # Full watchlist rows INCLUDING the heavy display fields (mp_tpo_letters,
        # mp_tpo_counts, prior_session_profile) that the 2s overview socket strips
        # for frame size. The detail-modal TPO chart reads these from here. The
        # 8s commodity_watchlist WS attaches the same; include it on REST too so
        # the chart populates on the initial load / poll and when the socket
        # isn't streaming (e.g. between scans / market closed).
        "futures_watchlist": (
            status.get("futures_watchlist") or status.get("watchlist") or []
        ),
        # Field retained for frontend backwards-compat; always empty.
        "atm_watchlist": {
            "rows": [],
            "source": "deprecated",
            "detail": "Commodity options sleeve removed; this field is intentionally empty.",
            "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
        },
    }


@router.get("/orders")
async def commodity_orders(limit: Optional[int] = None):
    orders = commodity_strategy_agent.get_orders()
    if limit is not None and limit >= 0:
        return orders[:limit]
    return orders


@router.get("/positions")
async def commodity_positions():
    return commodity_strategy_agent.get_positions()


@router.get("/reports")
async def commodity_reports(limit: Optional[int] = None):
    reports = commodity_strategy_agent.get_reports()
    if limit is not None and limit >= 0:
        return reports[:limit]
    return reports


@router.get("/profile-history/{root}")
async def commodity_profile_history(root: str):
    """Return the prior-period profile references for an underlying.

    Today's profile streams in the regular overview payload; this endpoint
    is the *historical* counterpart and serves yesterday + this/last week +
    this/last month aggregates so the detail modal's timeline can render
    Y / W / M references next to the live TPO chart.

    Built from snapshots persisted by the strategy agent each session
    (``backend/runtime/commodity_profiles/<ROOT>/<YYYY-MM-DD>.json``).
    Missing periods return ``null`` for that field so the UI can render a
    'coming soon' placeholder.
    """
    from paper_engine.commodity_profile_store import historical_timeline

    return historical_timeline(str(root or "").strip().upper())


# ── Index futures MP+OF (read-only; reuses existing index spot candles) ──────
# NIFTY / BANKNIFTY have no dedicated index-futures feed yet, so we drive Market
# Profile + (approximated) Order Flow off the underlying_spot_candles the app
# already records. Pure read + compute — no change to the live commodity agent.
# Note: index SPOT has little/no traded volume, so MP (time-based TPO) is always
# meaningful while OF (CVD/VWAP, volume-weighted) is flagged when volume exists.

# Explicit profile tick for the common indices; ANY other instrument gets an
# auto-derived "nice" tick sized to its session range (≈30-50 TPO rows).
_INDEX_MPOF_SPECS: dict[str, float] = {"NIFTY": 5.0, "BANKNIFTY": 10.0}
_INDEX_MPOF_TF = ("5minute", "15minute", "30minute")


def _nice_tick(day_range: float, price: float) -> float:
    """A readable profile bucket: round range/40 to a 1/2/2.5/5 × 10ⁿ step."""
    import math

    raw = (day_range / 40.0) if day_range > 0 else max(price * 5e-5, 0.05)
    if raw <= 0:
        return 0.05
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 2.5, 5.0):
        if raw <= m * mag:
            return round(m * mag, 6)
    return round(10 * mag, 6)


@router.get("/index-mpof")
async def commodity_index_mpof(
    symbol: str = Query(..., description="Any instrument with spot candles — NIFTY, BANKNIFTY, RELIANCE, …"),
    timeframe: str = Query("30minute"),
    sessions: int = Query(5, ge=1, le=20),
) -> dict:
    """Market Profile + Order Flow for any instrument, computed from existing spot candles."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from sqlalchemy import text
    from db.database import AsyncSessionLocal
    from auction_intelligence.schemas import MarketBar
    from auction_intelligence.market_profile.engine import MarketProfileEngine
    from analytics.orderflow import anchored_cvd, vwap_bands

    ist = ZoneInfo("Asia/Kolkata")
    symbol = symbol.upper().strip()
    timeframe = timeframe.lower().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    if timeframe not in _INDEX_MPOF_TF:
        raise HTTPException(400, f"Unsupported timeframe: {timeframe}. Supported: {', '.join(_INDEX_MPOF_TF)}")
    explicit_tick = _INDEX_MPOF_SPECS.get(symbol)

    days = sessions * 2 + 5
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE underlying = :u AND interval = :tf
                  AND time >= now() - make_interval(days => :days)
                ORDER BY time ASC
                """
            ),
            {"u": symbol, "tf": timeframe, "days": days},
        )
        rows = [dict(r._mapping) for r in result.fetchall()]

    if not rows:
        return {"symbol": symbol, "timeframe": timeframe, "bars": [], "detail": f"No spot candle history for {symbol} ({timeframe})."}

    bars = [
        {
            "time": (r["time"].isoformat() if hasattr(r["time"], "isoformat") else str(r["time"])),
            "open": float(r["open"] or 0.0),
            "high": float(r["high"] or 0.0),
            "low": float(r["low"] or 0.0),
            "close": float(r["close"] or 0.0),
            "volume": float(r["volume"] or 0.0),
        }
        for r in rows
        if r["close"] is not None
    ]

    def _ist_day(iso: str):
        return _dt.fromisoformat(iso).astimezone(ist).date()

    # Group by IST day and pick the latest REAL trading session — skip frozen
    # weekend/after-hours days whose bars are all one flat price (range ≈ 0),
    # which would otherwise yield a degenerate 1-row profile.
    from collections import OrderedDict

    days_map: "OrderedDict[object, list]" = OrderedDict()
    for b in bars:
        days_map.setdefault(_ist_day(b["time"]), []).append(b)
    last_day = next(reversed(days_map))
    session_bars = days_map[last_day]
    for day in reversed(days_map):
        db = days_map[day]
        if len(db) >= 3:
            rng = max(x["high"] for x in db) - min(x["low"] for x in db)
            px = db[-1]["close"] or 1.0
            if rng > px * 0.0005:  # skip frozen/flat (weekend) sessions
                last_day, session_bars = day, db
                break

    # Profile tick: explicit for known indices, else auto-sized to the session.
    sess_range = max(x["high"] for x in session_bars) - min(x["low"] for x in session_bars)
    sess_price = session_bars[-1]["close"] or 1.0
    tick_size = explicit_tick if explicit_tick else _nice_tick(sess_range, sess_price)

    # Market Profile on the latest session.
    mbars = [
        MarketBar(
            timestamp=_dt.fromisoformat(b["time"]),
            open=b["open"], high=b["high"], low=b["low"], close=b["close"], volume=b["volume"],
        )
        for b in session_bars
    ]
    engine = MarketProfileEngine(
        {"period_minutes": 30, "tick_size": tick_size, "value_area_pct": 0.70, "initial_balance_periods": 2}
    )
    snap = engine.build_profile(symbol, mbars)
    tpo = sorted(
        ({"price": float(p), "count": int(c)} for p, c in (snap.tpo_counts or {}).items()),
        key=lambda x: x["price"],
        reverse=True,
    )

    # Order flow on the session (anchored at the session open). Meaningful only
    # when the series carries volume (index spot frequently doesn't).
    session_volume = sum(b["volume"] for b in session_bars)
    has_volume = session_volume > 0
    cvd = anchored_cvd(session_bars, 0)
    vb = vwap_bands(session_bars, 0)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "tick_size": tick_size,
        "session_date": last_day.isoformat(),
        "last_price": session_bars[-1]["close"],
        "profile": {
            "poc": snap.poc,
            "vah": snap.vah,
            "val": snap.val,
            "ib_high": snap.initial_balance_high,
            "ib_low": snap.initial_balance_low,
            "day_high": snap.high_price,
            "day_low": snap.low_price,
            "single_prints": snap.single_prints,
            "poor_high": snap.poor_high,
            "poor_low": snap.poor_low,
            "tpo": tpo,
        },
        "orderflow": {
            "available": has_volume,
            "cvd": (cvd[-1] if cvd else None),
            "cvd_series": cvd,
            "vwap": (vb["vwap"][-1] if vb["vwap"] else None),
            "vwap_series": vb["vwap"],
            "vwap_upper": vb["upper"],
            "vwap_lower": vb["lower"],
        },
        "session_bars": session_bars,
        "bars": bars[-(sessions * 14):],
    }


# ── Index monitor rows (read-only watchlist context) ───────────────────────
# NIFTY / BANKNIFTY shown alongside the MCX futures in the desk watchlist, as
# MONITOR-ONLY rows: this lane never trades them (it places MCX futures orders).
# Each row is the NSE Strategy-2 1-min MP+OF evaluation — the same engine the
# index options lane runs — shaped like a commodity watchlist row and tagged
# `monitor_only` / `tradeable=False` so the UI renders it without entry actions.
_INDEX_MONITOR_SYMBOLS: tuple[str, ...] = ("NIFTY", "BANKNIFTY")


async def _latest_index_spot(symbol: str) -> dict:
    """Latest 1-min close (price) + the prior session's last close, for change."""
    from sqlalchemy import text
    from db.database import AsyncSessionLocal

    price = prev_close = bar_time = None
    try:
        async with AsyncSessionLocal() as session:
            latest = await session.execute(
                text(
                    """
                    SELECT close, time FROM underlying_spot_candles
                    WHERE underlying = :u AND interval = '1minute'
                    ORDER BY time DESC LIMIT 1
                    """
                ),
                {"u": symbol},
            )
            lr = latest.fetchone()
            if lr is not None:
                price = float(lr.close) if lr.close is not None else None
                bar_time = lr.time.isoformat() if hasattr(lr.time, "isoformat") else str(lr.time)
            prev = await session.execute(
                text(
                    """
                    SELECT close FROM underlying_spot_candles
                    WHERE underlying = :u AND interval = '1minute'
                      AND time < (
                        SELECT max(time)::date
                        FROM underlying_spot_candles
                        WHERE underlying = :u AND interval = '1minute'
                      )
                    ORDER BY time DESC LIMIT 1
                    """
                ),
                {"u": symbol},
            )
            pr = prev.fetchone()
            if pr is not None and pr.close is not None:
                prev_close = float(pr.close)
    except Exception:  # noqa: BLE001 — monitor rows are best-effort
        pass
    return {"price": price, "previous_close": prev_close, "bar_time": bar_time}


@router.get("/index-monitor")
async def commodity_index_monitor() -> dict:
    """Read-only MP+OF rows for NIFTY / BANKNIFTY to surface in the desk watchlist.

    Pure read + compute; does not touch the live commodity agent and never
    places orders. Index execution lives in the NSE options lane — this is
    monitoring context only.
    """
    from paper_engine.strategy2_mp_of import evaluate_strategy2_mp_of

    rows: list[dict] = []
    for underlying in _INDEX_MONITOR_SYMBOLS:
        base = {
            "symbol": underlying,
            "underlying": underlying,
            "display_name": underlying,
            "indicator_timeframe": "1minute",
            "monitor_only": True,
            "tradeable": False,
            "kind": "index",
        }
        try:
            res = await evaluate_strategy2_mp_of(underlying=underlying, persist=False)
        except Exception as exc:  # noqa: BLE001
            rows.append({**base, "mp_status": "error", "reason": f"monitor_error:{exc}"})
            continue
        spot = await _latest_index_spot(underlying)
        price = spot.get("price")
        prev = spot.get("previous_close")
        change = (price - prev) if (price is not None and prev) else None
        change_pct = ((change / prev) * 100.0) if (change is not None and prev) else None
        rows.append(
            {
                **res,
                **base,
                "price": price,
                "previous_close": prev,
                "change": change,
                "change_pct": change_pct,
                "bar_time": res.get("bar_time") or spot.get("bar_time"),
            }
        )
    return {"rows": rows, "as_of": datetime.now(timezone.utc).isoformat()}
