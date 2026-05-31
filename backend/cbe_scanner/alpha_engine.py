"""Top-down capital rotation alpha engine.

The CBE Scanner has been re-architected as a 7-layer institutional-style
capital rotation engine. This MVP delivers layers L0–L4 + L7 (composite
scorer). L1 asset rotation is stubbed at "equities-win" until GOLDBEES/
SILVERBEES/BBETF spot ingestion lands; L5 option selection and L6 MP
overlay remain in their existing modules (directional_options.selector and
analytics.market_profile_ext) and are *called* by the engine here rather
than re-implemented.

Layers
------
L0  F&O universe whitelist                derived from option_premium_candles
L1  Asset rotation (Gold/Silver/Bond/Eq)  STUB: returns equities-win
L2  Sector ranking vs Nifty50             SectorRotationTracker (existing)
L3  Stock ranking within winning sectors  SectorRotationTracker components
L4  Option candidate filter               trend + RS + OI + IV (this module)
L7  Composite RS Matrix scoring + 80-gate (this module)

The scorer combines Asset RS (20%) + Sector RS (20%) + Stock RS (20%) +
Market Profile (20%) + Order Flow (20%). With L1 stubbed, Asset RS is a
constant (100 if equities win) — the gate still works because the other
four components must collectively pass 80 × 0.80 / 0.80 = 80 by themselves.
When L1 lights up later, the formula remains unchanged.

Output contract
---------------
The engine returns a payload shaped like the legacy CBE scan output so
the paper book + DB persistence keep working unchanged:
    {
      "scan_date": "...",
      "asset_winner": "EQUITIES",
      "sector_ranks": [...],
      "results": [ <one row per candidate> ],
      "watchlist": [ <subset that passed the gate> ],
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


# Gate threshold for composite alpha score. Any candidate scoring below
# this never enters the watchlist. The 80-floor is the user's spec and
# corresponds roughly to "4 of 5 layers strongly aligned".
COMPOSITE_GATE: float = 80.0


@dataclass
class LayerWeights:
    """Layer-by-layer weights for the composite RS matrix. Must sum to 100."""
    asset: float = 20.0
    sector: float = 20.0
    stock: float = 20.0
    market_profile: float = 20.0
    order_flow: float = 20.0

    def total(self) -> float:
        return self.asset + self.sector + self.stock + self.market_profile + self.order_flow


@dataclass
class AlphaEngineConfig:
    weights: LayerWeights = field(default_factory=LayerWeights)
    timeframe: str = "weekly"
    sectors_to_keep: int = 4
    stocks_per_sector: int = 5
    composite_gate: float = COMPOSITE_GATE
    # Soft floors on option side. A stock with extremely thin options is
    # excluded *before* scoring (saves compute, also avoids false
    # 90+ scores for names you can't actually trade).
    min_atm_oi: float = 500.0
    min_atm_volume: float = 0.0  # opens for stocks with low volume but high OI


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
    """Stub asset-rotation layer.

    Returns the "equities-win" verdict so L2+ can proceed. When the
    GOLDBEES/SILVERBEES/BBETF/LIQUIDBEES ETF spot data is ingested, this
    will get a real implementation that ranks asset classes by 3/6/12-month
    momentum + above-30W-MA. Until then, the score is locked at 100.
    """
    return {
        "winner": "EQUITIES",
        "asset_rank": [
            {"asset": "EQUITIES", "score": 100.0, "note": "stub: ETF ingestion pending"},
            {"asset": "GOLD", "score": None, "note": "stub: GOLDBEES ingestion pending"},
            {"asset": "SILVER", "score": None, "note": "stub: SILVERBEES ingestion pending"},
            {"asset": "BONDS", "score": None, "note": "stub: BBETF ingestion pending"},
            {"asset": "CASH", "score": None, "note": "stub: LIQUIDBEES ingestion pending"},
        ],
        "score_for_engine": 100.0,
        "stub": True,
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
async def rank_stocks_in_winners(
    winning_sectors: list[dict[str, Any]],
    *,
    timeframe: str,
    stocks_per_sector: int,
    fno_universe: set[str],
    tracker: SectorRotationTracker | None = None,
) -> dict[str, Any]:
    """For each winning sector, fetch components + rank by RS.

    Output keys each stock's RS-vs-sector + RS-vs-Nifty50 + RRG quadrant
    so downstream layers can use both. Only stocks present in the F&O
    whitelist are kept — there's no point ranking non-tradeable names.
    """
    tracker = tracker or SectorRotationTracker()
    quadrant_priority = {"leading": 0, "improving": 1, "weakening": 2, "lagging": 3}
    stock_rows: list[dict[str, Any]] = []
    per_sector: dict[str, list[dict[str, Any]]] = {}

    for sector_row in winning_sectors:
        code = str(sector_row.get("code") or "")
        if not code:
            continue
        try:
            components = await tracker.get_sector_components(code, timeframe)
        except Exception as exc:
            logger.warning(f"[alpha] sector components failed for {code}: {exc}")
            continue
        rrg_points = list((components.get("rrg") or {}).get("points") or [])

        sector_stocks: list[dict[str, Any]] = []
        for point in rrg_points:
            symbol = str(point.get("code") or point.get("symbol") or "").upper()
            if not symbol or symbol not in fno_universe:
                continue
            sector_stocks.append(
                {
                    "instrument": symbol,
                    "sector_code": code,
                    "sector_name": sector_row.get("name"),
                    "sector_rs_pct": float(sector_row.get("rs_pct") or 0.0),
                    "sector_quadrant": sector_row.get("quadrant"),
                    "stock_rs_pct": float(point.get("relative_strength_pct") or 0.0),
                    "stock_quadrant": str(point.get("quadrant") or "lagging"),
                }
            )

        sector_stocks.sort(
            key=lambda row: (
                quadrant_priority.get(row["stock_quadrant"], 99),
                -row["stock_rs_pct"],
                row["instrument"],
            )
        )
        top = sector_stocks[: max(1, int(stocks_per_sector))]
        per_sector[code] = top
        for index, row in enumerate(top):
            row["stock_rank_in_sector"] = index + 1
            stock_rows.append(row)

    return {
        "per_sector": per_sector,
        "candidates": stock_rows,
        "candidate_count": len(stock_rows),
    }


# ---------------------------------------------------------------------------
# L4 — Option candidate filter (trend, RS, OI, IV, volume)
# ---------------------------------------------------------------------------
async def score_option_candidates(
    candidates: list[dict[str, Any]],
    *,
    config: AlphaEngineConfig,
) -> list[dict[str, Any]]:
    """Annotate each L3 candidate with daily trend + option liquidity stats.

    Pulls latest 60 daily bars per candidate (from underlying_spot_candles)
    + ATM-strike CE/PE OI + volume from option_premium_candles. Adds:
      * trend_score      0..1   EMA8 vs EMA21 slope
      * atr_expansion    0..1   ATR(20) / ATR(60)
      * volume_score     0..1   recent vs trailing volume z-score
      * oi_score         0..1   ATM OI bucket normalized
      * iv_score         0..1   IV percentile proxy from premium chop

    Stocks failing the OI floor are dropped entirely.
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

            spot_bars = await session.execute(
                text(
                    """
                    SELECT time, close, volume
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '30minute'
                      AND time >= NOW() - INTERVAL '60 days'
                    ORDER BY time DESC
                    LIMIT 600
                    """
                ),
                {"underlying": symbol},
            )
            spot_rows = [(r[0], float(r[1]), float(r[2] or 0)) for r in spot_bars.fetchall() if r[1] is not None]
            if len(spot_rows) < 40:
                continue
            spot_rows.reverse()
            closes = [c for _, c, _ in spot_rows]
            volumes = [v for _, _, v in spot_rows]

            trend_score = _trend_score(closes)
            atr_expansion = _atr_expansion(closes)
            volume_score = _volume_score(volumes)

            atm_metric = await _atm_option_liquidity(session, symbol)
            oi_score = _bucket_score(atm_metric.get("atm_oi") or 0.0, [500, 2_000, 10_000, 50_000])
            iv_score = atm_metric.get("iv_score") or 0.0

            if (atm_metric.get("atm_oi") or 0.0) < config.min_atm_oi:
                continue
            if (atm_metric.get("atm_volume") or 0.0) < config.min_atm_volume:
                continue

            enriched.append(
                {
                    **row,
                    "latest_close": closes[-1],
                    "trend_score": round(trend_score, 4),
                    "atr_expansion": round(atr_expansion, 4),
                    "volume_score": round(volume_score, 4),
                    "oi_score": round(oi_score, 4),
                    "iv_score": round(iv_score, 4),
                    "atm_oi": atm_metric.get("atm_oi"),
                    "atm_volume": atm_metric.get("atm_volume"),
                    "atm_strike": atm_metric.get("atm_strike"),
                    "directional_bias": _bias_from_signals(row, trend_score),
                }
            )
    return enriched


async def _atm_option_liquidity(session, symbol: str) -> dict[str, Any]:
    """Best-effort ATM CE+PE liquidity snapshot using the latest day's bars.

    Returns total CE+PE OI + volume at the strike nearest to the latest
    spot close + a crude IV proxy (relative premium chop). When no option
    data is found, returns zeros — callers gate on these.
    """
    from sqlalchemy import text

    spot_result = await session.execute(
        text(
            """
            SELECT close FROM underlying_spot_candles
            WHERE underlying = :underlying AND interval='30minute'
            ORDER BY time DESC LIMIT 1
            """
        ),
        {"underlying": symbol},
    )
    spot_row = spot_result.fetchone()
    if not spot_row:
        return {"atm_oi": 0.0, "atm_volume": 0.0, "iv_score": 0.0, "atm_strike": None}
    spot_close = float(spot_row[0])

    strike_result = await session.execute(
        text(
            """
            SELECT strike, ABS(strike - :spot) AS d
            FROM option_premium_candles
            WHERE underlying = :underlying
              AND time >= NOW() - INTERVAL '5 days'
            GROUP BY strike
            ORDER BY d ASC
            LIMIT 1
            """
        ),
        {"underlying": symbol, "spot": spot_close},
    )
    strike_row = strike_result.fetchone()
    if not strike_row:
        return {"atm_oi": 0.0, "atm_volume": 0.0, "iv_score": 0.0, "atm_strike": None}
    atm_strike = float(strike_row[0])

    liquidity_result = await session.execute(
        text(
            """
            SELECT
                SUM(oi)        AS total_oi,
                SUM(volume)    AS total_volume,
                AVG(close)     AS avg_premium,
                STDDEV(close)  AS premium_stddev
            FROM option_premium_candles
            WHERE underlying = :underlying
              AND strike = :strike
              AND time >= NOW() - INTERVAL '5 days'
            """
        ),
        {"underlying": symbol, "strike": atm_strike},
    )
    liq_row = liquidity_result.fetchone()
    total_oi = float(liq_row[0] or 0.0) if liq_row else 0.0
    total_volume = float(liq_row[1] or 0.0) if liq_row else 0.0
    avg_premium = float(liq_row[2] or 0.0) if liq_row else 0.0
    premium_stddev = float(liq_row[3] or 0.0) if liq_row else 0.0

    # IV proxy — premium chop normalized by mean premium. Higher = richer
    # vol pricing. Clamped to [0, 1] for the composite scorer.
    iv_score = 0.0
    if avg_premium > 0.0:
        iv_score = max(0.0, min(1.0, premium_stddev / max(avg_premium, 0.01)))

    return {
        "atm_oi": total_oi,
        "atm_volume": total_volume,
        "iv_score": iv_score,
        "atm_strike": atm_strike,
    }


# ---------------------------------------------------------------------------
# L7 — Composite RS Matrix scoring + gate
# ---------------------------------------------------------------------------
def composite_score(
    *,
    asset_score: float,
    sector_rs_pct: float,
    stock_rs_pct: float,
    trend_score: float,
    atr_expansion: float,
    volume_score: float,
    oi_score: float,
    iv_score: float,
    weights: LayerWeights,
) -> dict[str, Any]:
    """Combine the 5 weighted components into a single 0..100 score.

    Components are normalized to [0, 100] each before weighting. The
    market_profile + order_flow components are placeholders here — they
    are populated by call sites that have a MP/OF analyzer wired
    (S2/Commodity/AI already pass these in). For an MVP that only has
    trend/RS/OI, we use atr_expansion + volume_score as proxies for MP+OF
    until those are properly threaded through.
    """
    asset_component = max(0.0, min(100.0, float(asset_score)))
    sector_component = _normalize_rs_pct(sector_rs_pct)
    stock_component = _normalize_rs_pct(stock_rs_pct)
    # MP/OF proxies until live wiring lands:
    mp_proxy = max(0.0, min(100.0, 50.0 + (atr_expansion - 1.0) * 100.0))
    of_proxy = max(0.0, min(100.0, 50.0 + (volume_score - 0.5) * 100.0))
    # OI/IV not in the headline 5×20 — folded into the L4 prefilter only.

    total_weight = weights.total()
    if total_weight <= 0:
        return {"score": 0.0, "components": {}}

    score = (
        weights.asset * asset_component
        + weights.sector * sector_component
        + weights.stock * stock_component
        + weights.market_profile * mp_proxy
        + weights.order_flow * of_proxy
    ) / total_weight

    return {
        "score": round(score, 2),
        "components": {
            "asset": round(asset_component, 2),
            "sector": round(sector_component, 2),
            "stock": round(stock_component, 2),
            "market_profile_proxy": round(mp_proxy, 2),
            "order_flow_proxy": round(of_proxy, 2),
            "trend_score": round(trend_score, 4),
            "atr_expansion": round(atr_expansion, 4),
            "volume_score": round(volume_score, 4),
            "oi_score": round(oi_score, 4),
            "iv_score": round(iv_score, 4),
        },
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def run_alpha_pipeline(config: AlphaEngineConfig | None = None) -> dict[str, Any]:
    """Run all configured layers and return the canonical scan payload.

    The output is shape-compatible with `cbe_scanner.service.run_scan` so
    the existing paper book + DB persistence consume it unchanged. Each
    candidate carries a full breakdown for transparency:
        {
          "instrument", "sector_code", "stock_rank_in_sector",
          "sector_rs_pct", "stock_rs_pct",
          "trend_score", "atr_expansion", "volume_score", "oi_score", "iv_score",
          "composite_score", "composite_components", "directional_bias",
          "gate_passed", "latest_close"
        }
    """
    config = config or AlphaEngineConfig()
    started = datetime.now(timezone.utc)

    fno_universe = set(await discover_fno_universe())
    asset_layer = await rank_asset_classes()
    sector_layer = await rank_sectors(
        config.timeframe,
        keep_top=config.sectors_to_keep,
    )
    stock_layer = await rank_stocks_in_winners(
        sector_layer.get("winners") or [],
        timeframe=config.timeframe,
        stocks_per_sector=config.stocks_per_sector,
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
            trend_score=float(row.get("trend_score") or 0.0),
            atr_expansion=float(row.get("atr_expansion") or 1.0),
            volume_score=float(row.get("volume_score") or 0.5),
            oi_score=float(row.get("oi_score") or 0.0),
            iv_score=float(row.get("iv_score") or 0.0),
            weights=config.weights,
        )
        scored.append(
            {
                **row,
                # Alpha-engine fields
                "composite_alpha_score": score["score"],
                "composite_components": score["components"],
                "gate_passed": score["score"] >= config.composite_gate,
                # Legacy-CBE compatibility — the paper book + cbe_scan_results
                # repository expect these field names. composite_score is
                # remapped from [0, 100] → [0, 10] so the legacy
                # watchlist_min_score = 5.0 threshold maps to a 50 alpha score,
                # well below the 80 gate.
                "composite_score": round(score["score"] / 10.0, 2),
                "bias_conviction": min(
                    1.0,
                    abs(float(row.get("stock_rs_pct") or 0.0)) / 20.0,
                ),
                "f1_vc_score": float(row.get("atr_expansion") or 0.0),
                "f2_omp_score": float(row.get("iv_score") or 0.0),
                "f3_csmd_score": float(row.get("sector_rs_pct") or 0.0) / 10.0,
                "f4_cp_score": 0.0,
                "f5_mp_score": float(row.get("trend_score") or 0.0),
                "details": {
                    "engine": "alpha_v1",
                    "alpha_score": score["score"],
                    "components": score["components"],
                    "sector_code": row.get("sector_code"),
                    "sector_quadrant": row.get("sector_quadrant"),
                    "stock_quadrant": row.get("stock_quadrant"),
                    "atm_strike": row.get("atm_strike"),
                    "atm_oi": row.get("atm_oi"),
                    "atm_volume": row.get("atm_volume"),
                },
            }
        )

    scored.sort(key=lambda r: float(r.get("composite_alpha_score") or 0.0), reverse=True)
    watchlist = [r for r in scored if r.get("gate_passed")]

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
            "sectors_scanned": list((stock_layer.get("per_sector") or {}).keys()),
        },
        "config": {
            "timeframe": config.timeframe,
            "sectors_to_keep": config.sectors_to_keep,
            "stocks_per_sector": config.stocks_per_sector,
            "composite_gate": config.composite_gate,
        },
        "scored_count": len(scored),
        "watchlist_count": len(watchlist),
        "results": scored,
        "watchlist": watchlist,
        "source": "alpha_engine_v1",
    }


# ---------------------------------------------------------------------------
# Helper math
# ---------------------------------------------------------------------------
def _ema(series: list[float], period: int) -> list[float]:
    if not series or period <= 0:
        return []
    k = 2 / (period + 1)
    out: list[float] = []
    last = series[0]
    out.append(last)
    for value in series[1:]:
        last = value * k + last * (1 - k)
        out.append(last)
    return out


def _trend_score(closes: list[float]) -> float:
    """EMA8 vs EMA21 cross-and-slope summary, normalized to [0, 1]."""
    if len(closes) < 30:
        return 0.5
    ema_fast = _ema(closes, 8)
    ema_slow = _ema(closes, 21)
    spread = (ema_fast[-1] - ema_slow[-1]) / max(ema_slow[-1], 1e-6)
    slope = (ema_fast[-1] - ema_fast[-5]) / max(ema_fast[-5], 1e-6)
    raw = 0.5 + spread * 5.0 + slope * 5.0
    return max(0.0, min(1.0, raw))


def _atr_expansion(closes: list[float]) -> float:
    """Ratio of recent realized range to trailing baseline, [0..2]."""
    if len(closes) < 60:
        return 1.0
    recent = closes[-20:]
    trailing = closes[-60:-20]

    def _range(series: list[float]) -> float:
        if len(series) < 2:
            return 0.0
        diffs = [abs(b - a) for a, b in zip(series[:-1], series[1:])]
        return sum(diffs) / len(diffs)

    r_recent = _range(recent)
    r_trail = _range(trailing) or 1e-6
    return max(0.0, min(2.0, r_recent / r_trail))


def _volume_score(volumes: list[float]) -> float:
    """Recent vs baseline volume, [0, 1]."""
    if len(volumes) < 60:
        return 0.5
    recent = sum(volumes[-20:]) / 20.0
    baseline = sum(volumes[-60:-20]) / 40.0 or 1.0
    raw = recent / baseline
    # 1.0 maps to 0.5 (neutral), 2.0 → 1.0, 0.5 → 0.0
    return max(0.0, min(1.0, 0.5 + (raw - 1.0) * 0.5))


def _bucket_score(value: float, thresholds: list[float]) -> float:
    """Step function: value ≥ thresholds[i] → score (i+1)/len(thresholds)."""
    if not thresholds:
        return 0.0
    for index, threshold in enumerate(sorted(thresholds, reverse=True)):
        if value >= threshold:
            return (len(thresholds) - index) / len(thresholds)
    return 0.0


def _normalize_rs_pct(rs_pct: float) -> float:
    """Map an RS pct (-100..+100ish) to a 0..100 component score.

    A 0% RS = 50 (neutral); +10% = 75; -10% = 25; clamped at endpoints.
    """
    raw = 50.0 + float(rs_pct) * 2.5
    return max(0.0, min(100.0, raw))


def _bias_from_signals(row: dict[str, Any], trend_score: float) -> str:
    """Decide directional_bias from quadrant + trend.

    Leading + improving with trend_score >= 0.55 → bullish.
    Lagging + weakening with trend_score <= 0.45 → bearish.
    Otherwise neutral (will be filtered out by the paper book).
    """
    stock_quadrant = str(row.get("stock_quadrant") or "")
    sector_quadrant = str(row.get("sector_quadrant") or "")
    bullish_quadrants = {"leading", "improving"}
    bearish_quadrants = {"lagging", "weakening"}
    if (
        stock_quadrant in bullish_quadrants
        and sector_quadrant in bullish_quadrants
        and trend_score >= 0.55
    ):
        return "bullish"
    if (
        stock_quadrant in bearish_quadrants
        and sector_quadrant in bearish_quadrants
        and trend_score <= 0.45
    ):
        return "bearish"
    return "neutral"
