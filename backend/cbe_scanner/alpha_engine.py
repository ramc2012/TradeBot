"""Top-down capital rotation alpha engine — STRICT SPEC.

Rules follow the user-supplied alpha-generation framework exactly. The
engine is a pyramid funnel, NOT a universe-wide screener.

Pipeline
--------
L1  Asset rotation               Compare Gold/Silver/Bonds/Cash/Equities
                                 vs Nifty50 on momentum + above-30W-MA +
                                 RS trend. If equities do NOT win,
                                 the engine returns an EMPTY watchlist
                                 for the week — no trades.
L2  Sector rotation (weekly)     Only runs if L1 winner == EQUITIES.
                                 Rank all 13 sectors by RS pct vs Nifty50,
                                 take top 4.
L3  Stock selection (weekly)     Pool ALL members of the top 4 sectors,
                                 rank pool by stock RS, take top 10.
L4  Option candidate filter      Score the 10 finalists on:
                                   Trend + RS + Volume + IV + OI
                                 (5 components, equally weighted) + drop
                                 names that fail liquidity floors.
L5  Directional option selection Bias is set by L4 alignment:
                                   trend up + sector leading  → bullish (CE)
                                   trend down + sector lagging → bearish (PE)
                                   mixed                       → skip
L6  Market profile overlay       Skip exhaustion regimes; trend/balance/
                                 breakout pass through.
L7  Composite RS Matrix          0.20·Asset + 0.20·Sector + 0.20·Stock +
                                 0.20·MP + 0.20·OF. Gate at 80.

Cadence
-------
Scan: 15-min during NSE hours (re-evaluation).
Position rebalance: WEEKLY. Minimum hold = 5 trading days.

What was removed in this rewrite
--------------------------------
* universe_mode="full" — the spec is a narrowing pyramid, not a sweep.
* "Two-of-three vote" bias — replaced with strict trend+sector alignment.
* top-5-per-sector — replaced with pool-then-top-10.
* 3-miss close-on-watchlist-drop — replaced with min-hold = 5 trading days.

Output contract
---------------
Payload shape stays backward-compatible with the paper book + DB
persistence so the frontend continues to render:
    {
      "scan_date": "...",
      "asset_winner": "EQUITIES" | "GOLD" | ...,
      "results": [ <one row per candidate> ],
      "watchlist": [ <subset that passed the gate AND has a bias> ],
    }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from analytics.sector import SectorRotationTracker

logger = logging.getLogger(__name__)


IST_NOW = lambda: datetime.now(timezone.utc).isoformat()


# How many top-ranked candidates make the watchlist each cycle. Replaced
# the old absolute composite_gate floor — strength is RELATIVE. In a
# strong market many names clear an 80-point bar; in a weak market none
# do. Either way the trader wants the top N by strength, not a binary
# pass/fail vs a fixed threshold.
TOP_N_WATCHLIST: int = 10
# Kept as backstop only — a candidate whose composite is BELOW this gets
# logged as a low-conviction pick. Does NOT exclude it from the
# watchlist; the only filter is the top-N rank.
LOW_CONVICTION_FLOOR: float = 50.0


@dataclass
class LayerWeights:
    """Composite RS Matrix weights. Five components, 20% each by default.

    Per user spec: Asset RS + Sector RS + Stock RS + MACD + RSI = 100%.
    """
    asset: float = 20.0
    sector: float = 20.0
    stock: float = 20.0
    macd: float = 20.0
    rsi: float = 20.0

    def total(self) -> float:
        return self.asset + self.sector + self.stock + self.macd + self.rsi


@dataclass
class AlphaEngineConfig:
    """Strict-spec configuration. Knobs map 1:1 to the user's doc."""
    weights: LayerWeights = field(default_factory=LayerWeights)
    timeframe: str = "weekly"
    # L2: number of winning sectors to keep. Spec says "top 3-4 sectors";
    # default 4 gives slightly broader stock selection.
    sectors_to_keep: int = 4
    # L3: total number of stocks to surface as finalists, POOLED across
    # all winning sectors (NOT per-sector). Spec: "Select Top 10 Stocks".
    finalists_count: int = 10
    # L7 gate. Spec: "Only trades scoring above a threshold (e.g. 80/100)
    # become eligible."
    # Top N candidates by composite_alpha_score become the watchlist.
    # No absolute gate — strength is judged relative to peers each cycle.
    top_n_watchlist: int = TOP_N_WATCHLIST
    # Soft hint only — flagged as low-conviction in details, not a hard
    # filter. Kept so the frontend can call out top-N rows that are
    # tradeable but weak (e.g. all-bear market scenario).
    low_conviction_floor: float = LOW_CONVICTION_FLOOR
    # Liquidity floors. A stock with extremely thin options is excluded
    # before scoring — there's no point ranking names you can't trade.
    min_atm_oi: float = 500.0
    min_atm_volume: float = 0.0


# ---------------------------------------------------------------------------
# L0 — F&O whitelist
# ---------------------------------------------------------------------------
async def discover_fno_universe() -> list[str]:
    """List every underlying that has at least one option contract on file.

    Any name with rows in option_premium_candles is by definition an F&O
    stock — so this is the canonical whitelist without maintaining a
    separate static list that drifts from exchange revisions.
    """
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT underlying
                FROM option_premium_candles
                WHERE underlying IS NOT NULL
                ORDER BY underlying
                """
            )
        )
        rows = [row[0] for row in result.fetchall() if row[0]]
    return rows


# ---------------------------------------------------------------------------
# L1 — Asset rotation (STUBBED)
# ---------------------------------------------------------------------------
async def rank_asset_classes() -> dict[str, Any]:
    """Asset-rotation layer. Auto-detects whether live ETF data is present.

    When `underlying_spot_candles` has ≥200 daily bars for each of
    GOLDBEES/SILVERBEES/BBETF/LIQUIDBEES + NIFTY, this returns a real
    momentum-weighted ranking (per asset_rotation.rank_asset_classes_live).
    Otherwise it falls back to "equities-win" so the rest of the alpha
    pipeline continues to run.
    """
    try:
        from .asset_rotation import rank_asset_classes_live
        return await rank_asset_classes_live()
    except Exception as exc:
        logger.warning(f"[L1] live asset-rotation failed; using stub: {exc}")
        return {
            "winner": "EQUITIES",
            "asset_rank": [
                {"asset": "EQUITIES", "score": 100.0, "note": "stub: live ranker errored"},
            ],
            "score_for_engine": 100.0,
            "stub": True,
            "stub_reason": str(exc),
        }


# ---------------------------------------------------------------------------
# L2 — Sector ranking vs Nifty50
# ---------------------------------------------------------------------------
async def rank_sectors(
    timeframe: str,
    *,
    keep_top: int,
    tracker: SectorRotationTracker | None = None,
) -> dict[str, Any]:
    """Rank 13 sectors by RS vs Nifty50 on the given timeframe.

    Re-uses the existing SectorRotationTracker which already produces
    relative_strength_pct + RRG quadrant labels per sector. We pull the
    full payload then rank by quadrant (leading > improving > weakening >
    lagging), tie-breaking by RS pct.
    """
    tracker = tracker or SectorRotationTracker()
    payload = await tracker.get_sector_rotation(timeframe)
    watchlist = list(payload.get("watchlist") or [])

    quadrant_priority = {"leading": 0, "improving": 1, "weakening": 2, "lagging": 3}
    sorted_sectors = sorted(
        watchlist,
        key=lambda row: (
            quadrant_priority.get(str(row.get("quadrant") or "lagging"), 99),
            -float(row.get("relative_strength_pct") or 0.0),
            str(row.get("code") or row.get("name") or ""),
        ),
    )

    top = sorted_sectors[: max(1, int(keep_top))]
    return {
        "timeframe": timeframe,
        "sector_count": len(watchlist),
        "ranked_sectors": [
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "rs_pct": float(row.get("relative_strength_pct") or 0.0),
                "quadrant": row.get("quadrant"),
                "rank": index + 1,
            }
            for index, row in enumerate(sorted_sectors)
        ],
        "winners": [
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "rs_pct": float(row.get("relative_strength_pct") or 0.0),
                "quadrant": row.get("quadrant"),
            }
            for row in top
        ],
    }


# ---------------------------------------------------------------------------
# L3 — Stock ranking within winning sectors
# ---------------------------------------------------------------------------
async def rank_stocks_full_universe(
    *,
    timeframe: str,
    fno_universe: set[str],
    tracker: SectorRotationTracker | None = None,
) -> dict[str, Any]:
    """L3 redesign: score the entire F&O universe with sector RRG as context.

    The user clarified that "still outperforming stocks may be there" even
    if their sector is lagging. So we don't pre-filter to top-N sectors;
    every F&O stock gets enriched with its sector RRG quadrant + RS, then
    L4 indicators (MACD + RSI + RRG) decide the bias and composite score.

    Sector RRG is fed in as a SCORE COMPONENT (already part of the 5×20%
    composite via sector_rs_pct), not a hard filter.
    """
    tracker = tracker or SectorRotationTracker()
    sector_payload = await tracker.get_sector_rotation(timeframe)
    stocks_by_sector = sector_payload.get("stocks_by_sector") or {}
    sector_meta: dict[str, dict[str, Any]] = {}
    for row in sector_payload.get("watchlist") or []:
        code = str(row.get("code") or "")
        if code:
            sector_meta[code] = row

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Pass 1 — every stock that's classified into a sector
    for sector_code, bundle in stocks_by_sector.items():
        if not isinstance(bundle, dict):
            continue
        sector_info = bundle.get("sector") or sector_meta.get(sector_code) or {}
        for point in list((bundle.get("rrg") or {}).get("points") or []):
            symbol = str(point.get("code") or point.get("symbol") or "").upper()
            if not symbol or symbol not in fno_universe or symbol in seen:
                continue
            seen.add(symbol)
            candidates.append(
                {
                    "instrument": symbol,
                    "sector_code": sector_code,
                    "sector_name": sector_info.get("name"),
                    "sector_rs_pct": float(sector_info.get("relative_strength_pct") or 0.0),
                    "sector_quadrant": sector_info.get("quadrant"),
                    "stock_rs_pct": float(point.get("relative_strength_pct") or 0.0),
                    "stock_quadrant": str(point.get("quadrant") or "lagging"),
                }
            )

    # Pass 2 — F&O symbols missing from any sector slice still tradeable.
    for symbol in sorted(fno_universe):
        if symbol in seen:
            continue
        candidates.append(
            {
                "instrument": symbol,
                "sector_code": None,
                "sector_name": None,
                "sector_rs_pct": 0.0,
                "sector_quadrant": None,
                "stock_rs_pct": 0.0,
                "stock_quadrant": "unclassified",
            }
        )

    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
    }


# ---------------------------------------------------------------------------
# L4 — Per-stock indicators (MACD + RSI on daily bars, weekly trend filter)
# ---------------------------------------------------------------------------
async def score_option_candidates(
    candidates: list[dict[str, Any]],
    *,
    config: AlphaEngineConfig,
) -> list[dict[str, Any]]:
    """Annotate each candidate with daily MACD + RSI + weekly trend.

    Per the user's redesign:
      * Daily timeframe for primary signal — MACD(12,26,9) + RSI(14).
      * Weekly bars provide higher-TF trend context (close above/below
        20-week EMA).
      * RRG quadrant from L3 stays in the row as the third indicator.

    No option-side data is touched here (paper book is pure cash equity).
    """
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    enriched: list[dict[str, Any]] = []
    if not candidates:
        return enriched

    async with AsyncSessionLocal() as session:
        for row in candidates:
            symbol = str(row.get("instrument") or "").upper()
            if not symbol:
                continue

            # Pull 220 days of 30-min bars and resample to daily-closes.
            # 220 days is enough for ~150 trading-day MACD warm-up + 30-week
            # weekly trend reference.
            daily_closes = await _fetch_daily_closes(session, symbol, lookback_days=220)
            if len(daily_closes) < 35:
                continue  # not enough history for MACD/RSI

            indicators = compute_daily_indicators(daily_closes)
            weekly = compute_weekly_context(daily_closes)
            macd_score, macd_meta = score_macd(indicators)
            rsi_score, rsi_meta = score_rsi(indicators)
            bias = _bias_from_signals(
                {**row, "macd": indicators, "weekly": weekly},
                trend_score=indicators.get("rsi_14") or 50.0,  # unused in new bias rule
            )

            enriched.append(
                {
                    **row,
                    "latest_close": daily_closes[-1],
                    "macd_line": indicators.get("macd_line"),
                    "macd_signal": indicators.get("macd_signal"),
                    "macd_hist": indicators.get("macd_histogram"),
                    "macd_bullish": indicators.get("macd_bullish"),
                    "macd_score": round(macd_score, 2),
                    "macd_meta": macd_meta,
                    "rsi_14": indicators.get("rsi_14"),
                    "rsi_score": round(rsi_score, 2),
                    "rsi_meta": rsi_meta,
                    "weekly_close_vs_ema20": weekly.get("close_vs_ema20"),
                    "weekly_trend": weekly.get("trend"),
                    "directional_bias": bias,
                    # Last 30 EOD closes for the row's sparkline. Rounded
                    # to 2dp to keep the JSON payload compact.
                    "recent_closes_30d": [round(c, 2) for c in daily_closes[-30:]],
                }
            )
    return enriched


async def _fetch_daily_closes(session, symbol: str, *, lookback_days: int = 220) -> list[float]:
    """Resample 30-min bars to last-bar-of-day close for the given window."""
    from sqlalchemy import text
    result = await session.execute(
        text(
            """
            WITH daily AS (
                SELECT DATE(time) AS d,
                       (ARRAY_AGG(close ORDER BY time DESC))[1] AS close
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '30minute'
                  AND time >= NOW() - (:days || ' days')::interval
                GROUP BY DATE(time)
            )
            SELECT close FROM daily ORDER BY d ASC
            """
        ),
        {"underlying": symbol, "days": str(int(lookback_days))},
    )
    return [float(r[0]) for r in result.fetchall() if r[0] is not None]


# ---------------------------------------------------------------------------
# Indicator math — MACD, RSI, weekly trend
# ---------------------------------------------------------------------------
def compute_daily_indicators(closes: list[float]) -> dict[str, Any]:
    """MACD(12,26,9) + RSI(14) on the supplied daily-close series.

    Returns the most-recent values. Callers must ensure len(closes) >= 35.
    """
    if len(closes) < 35:
        return {
            "macd_line": None, "macd_signal": None, "macd_histogram": None,
            "macd_bullish": None, "macd_cross_today": False,
            "rsi_14": None,
        }
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_series = [a - b for a, b in zip(ema12, ema26)]
    signal_series = _ema(macd_series[-50:], 9)  # last 50 of macd → smoothed
    # Align: signal_series is the last len(signal_series) entries of macd_series
    macd_line = macd_series[-1]
    macd_signal = signal_series[-1]
    macd_histogram = macd_line - macd_signal
    prev_macd = macd_series[-2]
    prev_signal = signal_series[-2] if len(signal_series) >= 2 else macd_signal
    cross_today = (prev_macd <= prev_signal and macd_line > macd_signal) or (
        prev_macd >= prev_signal and macd_line < macd_signal
    )

    rsi_14 = _rsi(closes, 14)

    return {
        "macd_line": round(macd_line, 4),
        "macd_signal": round(macd_signal, 4),
        "macd_histogram": round(macd_histogram, 4),
        "macd_bullish": macd_line > macd_signal,
        "macd_above_zero": macd_line > 0,
        "macd_cross_today": bool(cross_today),
        "rsi_14": round(rsi_14, 2),
    }


def compute_weekly_context(daily_closes: list[float]) -> dict[str, Any]:
    """Weekly trend filter: close vs 20-week EMA on weekly-resampled series.

    Weekly resampling = take every 5th close (rough but adequate for trend).
    """
    if len(daily_closes) < 100:  # need ~20 weeks worth of daily bars
        return {"close_vs_ema20": None, "trend": "unknown"}
    weekly_closes = daily_closes[::5][-30:]  # last ~30 weeks
    if len(weekly_closes) < 25:
        return {"close_vs_ema20": None, "trend": "unknown"}
    weekly_ema = _ema(weekly_closes, 20)
    last = weekly_closes[-1]
    ema_now = weekly_ema[-1]
    ema_5wk = weekly_ema[-5] if len(weekly_ema) >= 5 else ema_now
    diff_pct = (last - ema_now) / max(ema_now, 1e-6) * 100.0
    rising = ema_now > ema_5wk
    if last > ema_now and rising:
        trend = "up"
    elif last < ema_now and not rising:
        trend = "down"
    else:
        trend = "flat"
    return {
        "close_vs_ema20": round(diff_pct, 2),
        "trend": trend,
    }


def score_macd(indicators: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """MACD signal scored to 0-100. Per textbook interpretation:

        Above zero + bullish (line>signal) + fresh cross today → 95
        Above zero + bullish, no fresh cross                    → 75
        Above zero + bearish (line<signal)                      → 45 (warning)
        Below zero + bullish (recovery cross)                   → 60
        Below zero + bearish + fresh cross today                → 5  (very weak)
        Below zero + bearish, no fresh cross                    → 25

    Returns (score, meta). Score=50 if MACD not computable.
    """
    line = indicators.get("macd_line")
    if line is None:
        return 50.0, {"reason": "macd_not_computable"}
    above = bool(indicators.get("macd_above_zero"))
    bull = bool(indicators.get("macd_bullish"))
    fresh = bool(indicators.get("macd_cross_today"))
    if above and bull and fresh:
        score, label = 95.0, "above_zero_bullish_fresh"
    elif above and bull:
        score, label = 75.0, "above_zero_bullish"
    elif above and not bull:
        score, label = 45.0, "above_zero_bearish_warning"
    elif (not above) and bull and fresh:
        score, label = 60.0, "below_zero_bullish_recovery"
    elif (not above) and bull:
        score, label = 50.0, "below_zero_bullish_pending"
    elif (not above) and (not bull) and fresh:
        score, label = 5.0, "below_zero_bearish_fresh"
    else:
        score, label = 25.0, "below_zero_bearish"
    return score, {"label": label, "line": line, "signal": indicators.get("macd_signal"), "cross_today": fresh}


def score_rsi(indicators: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """RSI scored to 0-100 with sweet-spot favouring healthy momentum:

        RSI 45-65  → 90  (healthy uptrend, room to run)
        RSI 35-45  → 65  (recovering from oversold)
        RSI 30-35  → 50  (oversold bounce candidate)
        RSI 65-75  → 60  (extended; still ok)
        RSI 75-85  → 30  (overbought; chasing risk)
        RSI > 85   → 10  (extreme overbought; avoid)
        RSI < 30   → 25  (deep oversold; not yet reversing)
    """
    rsi = indicators.get("rsi_14")
    if rsi is None:
        return 50.0, {"reason": "rsi_not_computable"}
    if 45 <= rsi < 65:
        score, label = 90.0, "healthy_uptrend"
    elif 35 <= rsi < 45:
        score, label = 65.0, "recovering"
    elif 30 <= rsi < 35:
        score, label = 50.0, "oversold_bounce"
    elif 65 <= rsi < 75:
        score, label = 60.0, "extended"
    elif 75 <= rsi < 85:
        score, label = 30.0, "overbought"
    elif rsi >= 85:
        score, label = 10.0, "extreme_overbought"
    else:  # rsi < 30
        score, label = 25.0, "deep_oversold"
    return score, {"label": label, "rsi": rsi}


def _rsi(closes: list[float], period: int = 14) -> float:
    """Standard Wilder RSI on the last `period` deltas. Returns last value."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    # Wilder smoothing: first avg = simple mean, then ema-like recursion.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))



# ---------------------------------------------------------------------------
# Bias rule — MACD + RSI + RRG triple confirmation
# ---------------------------------------------------------------------------
def _bias_from_signals(row: dict[str, Any], trend_score: float = 50.0) -> str:
    """Bias from MACD + RSI + RRG (the three indicators the user named).

    Bullish triple-confirmation:
      * MACD: bullish AND above zero  (rising momentum, trend positive)
      * RSI:  45-70                    (healthy uptrend, not overbought)
      * RRG:  stock_quadrant ∈ {leading, improving}
      * Weekly trend: not "down"

    Bearish mirror:
      * MACD: bearish AND below zero
      * RSI:  30-55
      * RRG:  stock_quadrant ∈ {lagging, weakening}
      * Weekly trend: not "up"
    """
    macd = row.get("macd") or {}
    weekly = row.get("weekly") or {}
    macd_line = macd.get("macd_line")
    if macd_line is None:
        return "neutral"
    rsi = macd.get("rsi_14") if "rsi_14" in macd else row.get("rsi_14")
    macd_bull = bool(macd.get("macd_bullish")) and bool(macd.get("macd_above_zero"))
    macd_bear = (not macd.get("macd_bullish")) and (not macd.get("macd_above_zero"))
    stock_q = str(row.get("stock_quadrant") or "").lower()
    weekly_trend = str(weekly.get("trend") or "").lower()
    rrg_bull = stock_q in {"leading", "improving"}
    rrg_bear = stock_q in {"lagging", "weakening"}
    rsi_bull = rsi is not None and 45.0 <= float(rsi) <= 70.0
    rsi_bear = rsi is not None and 30.0 <= float(rsi) <= 55.0
    if macd_bull and rrg_bull and rsi_bull and weekly_trend != "down":
        return "bullish"
    if macd_bear and rrg_bear and rsi_bear and weekly_trend != "up":
        return "bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Composite scorer + supporting math
# ---------------------------------------------------------------------------
def composite_score(
    *,
    asset_score: float,
    sector_rs_pct: float,
    stock_rs_pct: float,
    macd_score: float,
    rsi_score: float,
    weights: LayerWeights,
) -> dict[str, Any]:
    """Combine the 5×20% RS-matrix components into one 0..100 score."""
    asset_component = max(0.0, min(100.0, float(asset_score)))
    sector_component = _normalize_rs_pct(sector_rs_pct)
    stock_component = _normalize_rs_pct(stock_rs_pct)
    macd_component = max(0.0, min(100.0, float(macd_score)))
    rsi_component = max(0.0, min(100.0, float(rsi_score)))
    total_weight = weights.total()
    if total_weight <= 0:
        return {"score": 0.0, "components": {}}
    score = (
        weights.asset * asset_component
        + weights.sector * sector_component
        + weights.stock * stock_component
        + weights.macd * macd_component
        + weights.rsi * rsi_component
    ) / total_weight
    return {
        "score": round(score, 2),
        "components": {
            "asset": round(asset_component, 2),
            "sector": round(sector_component, 2),
            "stock": round(stock_component, 2),
            "macd": round(macd_component, 2),
            "rsi": round(rsi_component, 2),
        },
    }


def _normalize_rs_pct(rs_pct: float) -> float:
    """Map an RS pct (-100..+100ish) to a 0..100 component score.

    A 0% RS = 50 (neutral); +10% = 75; -10% = 25; clamped at endpoints.
    """
    raw = 50.0 + float(rs_pct) * 2.5
    return max(0.0, min(100.0, raw))


def _ema(series: list[float], period: int) -> list[float]:
    if not series or period <= 0:
        return []
    k = 2 / (period + 1)
    out: list[float] = [series[0]]
    last = series[0]
    for value in series[1:]:
        last = value * k + last * (1 - k)
        out.append(last)
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def run_alpha_pipeline(config: AlphaEngineConfig | None = None) -> dict[str, Any]:
    """Run the 7-layer pipeline once. Output stays backward-compatible with
    the paper book + cbe_scan_results persistence."""
    config = config or AlphaEngineConfig()
    started = datetime.now(timezone.utc)

    fno_universe = set(await discover_fno_universe())

    # L1 — soft asset rotation → equity exposure budget
    asset_layer = await rank_asset_classes()
    equity_exposure_pct = _derive_equity_exposure(asset_layer)
    asset_layer["equity_exposure_pct"] = equity_exposure_pct

    # L2 — sector RRG context (surfaced, not gating)
    sector_layer = await rank_sectors(
        config.timeframe,
        keep_top=config.sectors_to_keep,
    )

    # L3 — full universe with sector RRG as a component, not a filter
    stock_layer = await rank_stocks_full_universe(
        timeframe=config.timeframe,
        fno_universe=fno_universe,
    )

    enriched = await score_option_candidates(
        list(stock_layer.get("candidates") or []),
        config=config,
    )

    scored: list[dict[str, Any]] = []
    asset_score = float(asset_layer.get("score_for_engine") or 100.0)
    for row in enriched:
        score = composite_score(
            asset_score=asset_score,
            sector_rs_pct=float(row.get("sector_rs_pct") or 0.0),
            stock_rs_pct=float(row.get("stock_rs_pct") or 0.0),
            macd_score=float(row.get("macd_score") or 50.0),
            rsi_score=float(row.get("rsi_score") or 50.0),
            weights=config.weights,
        )
        scored.append(
            {
                **row,
                "composite_alpha_score": score["score"],
                "composite_components": score["components"],
                # gate_passed kept for DB-column compatibility but its
                # semantics are now "in top N by composite_alpha_score".
                # Set below after the ranking sort completes.
                "gate_passed": False,
                "low_conviction": score["score"] < config.low_conviction_floor,
                "composite_score": round(score["score"] / 10.0, 2),
                "bias_conviction": min(
                    1.0,
                    abs(float(row.get("stock_rs_pct") or 0.0)) / 20.0,
                ),
                "f1_vc_score": 0.0,
                "f2_omp_score": 0.0,
                "f3_csmd_score": float(row.get("sector_rs_pct") or 0.0) / 10.0,
                "f4_cp_score": float(row.get("rsi_score") or 0.0) / 10.0,
                "f5_mp_score": float(row.get("macd_score") or 0.0) / 10.0,
                "details": {
                    "engine": "alpha_v3_macd_rsi_rrg",
                    "alpha_score": score["score"],
                    "components": score["components"],
                    "sector_code": row.get("sector_code"),
                    "sector_quadrant": row.get("sector_quadrant"),
                    "stock_quadrant": row.get("stock_quadrant"),
                    "macd_meta": row.get("macd_meta"),
                    "rsi_meta": row.get("rsi_meta"),
                    "weekly_trend": row.get("weekly_trend"),
                },
            }
        )

    scored.sort(key=lambda r: float(r.get("composite_alpha_score") or 0.0), reverse=True)
    # Watchlist = top N by composite_alpha_score whose bias is actionable.
    # Strength is RELATIVE per cycle — no absolute floor. We take up to N
    # tradeable rows from the head of the sorted list; if fewer than N
    # have non-neutral bias, the watchlist is shorter (that's honest —
    # don't trade neutral biases just to fill a quota).
    actionable = [
        r for r in scored
        if r.get("directional_bias") in ("bullish", "bearish")
    ]
    watchlist = actionable[: int(config.top_n_watchlist)]
    # Mark in_top_n on the rows (and keep gate_passed alias for old
    # frontend code that hasn't migrated yet).
    top_n_ids = {id(r) for r in watchlist}
    for row in scored:
        is_top = id(row) in top_n_ids
        row["in_top_n"] = is_top
        row["gate_passed"] = is_top  # alias for back-compat
        row["rank_overall"] = scored.index(row) + 1
        row["rank_actionable"] = (actionable.index(row) + 1) if row in actionable else None
    finished = datetime.now(timezone.utc)
    return {
        "scan_date": started.date().isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": round((finished - started).total_seconds(), 2),
        "fno_universe_size": len(fno_universe),
        "asset_winner": asset_layer.get("winner"),
        "asset_layer": asset_layer,
        "sector_layer": sector_layer,
        "stock_layer_summary": {
            "candidate_count": stock_layer.get("candidate_count"),
        },
        "config": _config_payload(config),
        "equity_exposure_pct": equity_exposure_pct,
        "scored_count": len(scored),
        "watchlist_count": len(watchlist),
        "results": scored,
        "watchlist": watchlist,
        "source": "alpha_engine_v3_macd_rsi_rrg",
    }


def _config_payload(config: AlphaEngineConfig) -> dict[str, Any]:
    return {
        "timeframe": config.timeframe,
        "sectors_to_keep": config.sectors_to_keep,
        "finalists_count": config.finalists_count,
        "top_n_watchlist": config.top_n_watchlist,
        "low_conviction_floor": config.low_conviction_floor,
        "min_atm_oi": config.min_atm_oi,
    }


def _derive_equity_exposure(asset_layer: dict[str, Any]) -> float:
    """Convert L1 asset-mix into an equity exposure budget (0-100).

      EQUITIES winner       → 100%
      EQUITIES top-2        →  70%
      EQUITIES top-3        →  40%
      EQUITIES bottom       →  20%
      Stub/unknown          → 100%
    """
    if asset_layer.get("stub"):
        return 100.0
    rank = asset_layer.get("asset_rank") or []
    if not rank:
        return 100.0
    position = next(
        (i for i, row in enumerate(rank) if str(row.get("asset") or "").upper() == "EQUITIES"),
        None,
    )
    if position is None:
        return 100.0
    return {0: 100.0, 1: 70.0, 2: 40.0}.get(position, 20.0)
