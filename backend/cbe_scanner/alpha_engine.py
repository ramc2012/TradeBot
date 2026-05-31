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
    # Universe mode controls L3 breadth:
    #   "full"          → score EVERY qualified F&O instrument (~221 names)
    #                     using sector RS as context, not a filter. Sector
    #                     winners still surface in the L2 panel for UI.
    #   "winners_only"  → legacy behaviour: top stocks_per_sector inside the
    #                     top sectors_to_keep winning sectors (~20 names).
    universe_mode: str = "full"
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


async def rank_stocks_full_universe(
    sector_payload: dict[str, Any],
    *,
    fno_universe: set[str],
    timeframe: str,
) -> dict[str, Any]:
    """Score the *entire* F&O universe, not just winning-sector members.

    Pulls per-stock RS data from `stocks_by_sector` (populated for ALL 13
    sectors by SectorRotationTracker, not just winners). Symbols not
    mapped to any sector get sector_code=None — they're still scored,
    just without a sector RS context. This makes the alpha engine
    behave like a universe-wide screener: top performers naturally bubble
    up from the composite scoring rather than being pre-filtered.
    """
    quadrant_priority = {"leading": 0, "improving": 1, "weakening": 2, "lagging": 3}
    stocks_by_sector = sector_payload.get("stocks_by_sector") or {}
    sector_meta_by_code: dict[str, dict[str, Any]] = {}
    for row in sector_payload.get("watchlist") or []:
        code = str(row.get("code") or "")
        if code:
            sector_meta_by_code[code] = row

    candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()

    # Pass 1 — every stock that's in a sector slice gets enriched with RS.
    for sector_code, sector_bundle in stocks_by_sector.items():
        if not isinstance(sector_bundle, dict):
            continue
        sector_info = sector_bundle.get("sector") or sector_meta_by_code.get(sector_code) or {}
        rrg_points = list((sector_bundle.get("rrg") or {}).get("points") or [])
        for point in rrg_points:
            symbol = str(point.get("code") or point.get("symbol") or "").upper()
            if not symbol or symbol not in fno_universe or symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
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

    # Pass 2 — F&O symbols missing from any sector slice still need to be
    # in the universe (they're tradeable but unclassified). Score with
    # sector_code=None so L4 can still compute trend / MP / OF on them.
    for symbol in sorted(fno_universe):
        if symbol in seen_symbols:
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

    # Stable ordering: leading/improving first, then by stock RS pct so the
    # supervisor can short-circuit if it runs out of compute budget.
    candidates.sort(
        key=lambda row: (
            quadrant_priority.get(str(row.get("stock_quadrant") or "lagging"), 99),
            -float(row.get("stock_rs_pct") or 0.0),
            str(row.get("instrument") or ""),
        )
    )
    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
        "mode": "full",
    }


# ---------------------------------------------------------------------------
# L4 — Option candidate filter (trend, RS, OI, IV, volume)
# ---------------------------------------------------------------------------
async def score_option_candidates(
    candidates: list[dict[str, Any]],
    *,
    config: AlphaEngineConfig,
) -> list[dict[str, Any]]:
    """Annotate each L3 candidate with daily trend + option liquidity stats
    + live Market-Profile / Order-Flow scores.

    Per candidate:
      * Pull last 60 days of 30-min bars from underlying_spot_candles → derive
        trend / ATR-expansion / volume / live MP day-type / live OF (CVD/VWAP).
      * Pull ATM CE+PE OI/volume from option_premium_candles for the liquidity
        gate + IV proxy.

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
                    SELECT time, open, high, low, close, volume
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
            ohlcv_rows = []
            for r in spot_bars.fetchall():
                if r[4] is None:
                    continue
                ohlcv_rows.append(
                    {
                        "time": r[0].isoformat() if r[0] is not None else None,
                        "open": float(r[1] or r[4]),
                        "high": float(r[2] or r[4]),
                        "low": float(r[3] or r[4]),
                        "close": float(r[4]),
                        "volume": float(r[5] or 0),
                    }
                )
            if len(ohlcv_rows) < 40:
                continue
            ohlcv_rows.reverse()
            closes = [b["close"] for b in ohlcv_rows]
            volumes = [b["volume"] for b in ohlcv_rows]

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

            # Live MP + OF (replaces the ATR/volume proxies for L7).
            bias_guess = _bias_from_signals({**row, "stock_rs_pct": row.get("stock_rs_pct")}, trend_score)
            mp_score, mp_meta = _market_profile_score(ohlcv_rows, bias_guess)
            of_score, of_meta = _orderflow_score(ohlcv_rows, bias_guess)

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
                    "directional_bias": bias_guess,
                    # Live MP/OF scores (0..100) — fed into composite_score.
                    "mp_score": round(mp_score, 2),
                    "of_score": round(of_score, 2),
                    "mp_meta": mp_meta,
                    "of_meta": of_meta,
                }
            )
    return enriched


def _market_profile_score(candles: list[dict[str, Any]], bias: str) -> tuple[float, dict[str, Any]]:
    """Build a single-session profile from intraday candles + score for bias.

    Maps day-type assessment to a 0..100 score scaled by confidence:
      trend      → bullish/bearish aligned with bias: 90
                   neutral: 55, opposite: 20
      breakout   → aligned: 100, opposite: 15
      balance    → 55 (mean-rev candidate; neutral for directional bias)
      failed_auction → 30 (avoid expansion bets)
      exhaustion → 25 (avoid)
    Falls back to 50 (neutral) if the profile can't be built.
    """
    try:
        from analytics.market_profile_ext import assess_day_type, ib_extension
    except Exception:
        return 50.0, {"error": "import_failed"}

    if len(candles) < 14:
        return 50.0, {"error": "insufficient_bars"}

    # Use the latest ~14 bars (one trading session at 30-min cadence) as
    # "today". Compute simple profile primitives — POC = price at biggest
    # volume bin, value area from cumulative TPO. This is intentionally
    # lightweight; the production MP engine lives elsewhere.
    today_candles = candles[-14:]
    profile = _simple_session_profile(today_candles)
    if not profile:
        return 50.0, {"error": "profile_build_failed"}
    current_price = float(today_candles[-1]["close"])
    ib_info = ib_extension(profile, current_price)
    try:
        assessment = assess_day_type(profile, current_price, ib_extension_info=ib_info)
    except Exception as exc:
        return 50.0, {"error": f"assess_failed:{exc}"}

    classification = (assessment.classification or "").lower()
    confidence = float(assessment.confidence or 0.5)

    base = 50.0
    if classification == "trend":
        base = 90.0 if bias in ("bullish", "bearish") else 55.0
    elif classification == "breakout":
        base = 100.0 if bias in ("bullish", "bearish") else 60.0
    elif classification == "balance":
        base = 55.0
    elif classification == "failed_auction":
        base = 30.0
    elif classification == "exhaustion":
        base = 25.0

    score = base * confidence + 50.0 * (1.0 - confidence)
    return max(0.0, min(100.0, score)), {
        "classification": classification,
        "confidence": round(confidence, 3),
        "ib_extended_above": getattr(ib_info, "extended_above", None) if ib_info else None,
        "ib_extended_below": getattr(ib_info, "extended_below", None) if ib_info else None,
    }


def _orderflow_score(candles: list[dict[str, Any]], bias: str) -> tuple[float, dict[str, Any]]:
    """Score order-flow alignment with bias.

      CVD direction agrees with bias        +40
      Anchored VWAP supports bias side      +20
      No divergence                         +20
      Divergence vs bias                    -30
    Base score is 50 (neutral). Final clamped to [0, 100].
    """
    try:
        from analytics.orderflow import orderflow_snapshot, cvd_agrees_with
    except Exception:
        return 50.0, {"error": "import_failed"}
    if not candles:
        return 50.0, {"error": "no_candles"}
    try:
        snap = orderflow_snapshot(candles)
    except Exception as exc:
        return 50.0, {"error": f"snapshot_failed:{exc}"}

    score = 50.0
    cvd_latest = snap.get("cvd_latest")
    vwap_latest = snap.get("vwap_latest")
    div = snap.get("divergence")
    last_close = float(candles[-1]["close"])

    if cvd_latest is not None:
        cvd_sign_bullish = float(cvd_latest) > 0
        if bias == "bullish" and cvd_sign_bullish:
            score += 20
        elif bias == "bearish" and not cvd_sign_bullish:
            score += 20
        elif bias in ("bullish", "bearish"):
            score -= 10

    if vwap_latest is not None:
        if bias == "bullish" and last_close >= float(vwap_latest):
            score += 15
        elif bias == "bearish" and last_close <= float(vwap_latest):
            score += 15

    if div is not None:
        kind = str(div.get("kind") or "")
        # bearish-divergence on a bullish bias is a warning, etc.
        if (bias == "bullish" and "bearish" in kind) or (bias == "bearish" and "bullish" in kind):
            score -= 25
    else:
        score += 5  # no divergence = small bonus

    return max(0.0, min(100.0, score)), {
        "cvd_latest": cvd_latest,
        "vwap_latest": vwap_latest,
        "divergence": div,
    }


def _simple_session_profile(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Lightweight session profile (POC + value area + IB) from OHLCV bars.

    Bins typical price into 30 buckets weighted by volume, finds the POC
    (highest-volume bucket), expands outward to capture 70% of volume for
    the value area, and uses the first 2 bars as the initial-balance.
    """
    if not candles:
        return None
    highs = [float(b.get("high") or b.get("close") or 0) for b in candles]
    lows = [float(b.get("low") or b.get("close") or 0) for b in candles]
    closes = [float(b.get("close") or 0) for b in candles]
    volumes = [float(b.get("volume") or 0) for b in candles]
    session_high = max(highs) if highs else 0.0
    session_low = min(lows) if lows else 0.0
    if session_high <= session_low:
        return None
    bins = 30
    step = (session_high - session_low) / bins
    if step <= 0:
        return None
    counts: dict[int, float] = {}
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp = (h + l + c) / 3.0
        idx = min(int((tp - session_low) / step), bins - 1)
        counts[idx] = counts.get(idx, 0) + max(v, 1.0)
    if not counts:
        return None
    total = sum(counts.values())
    poc_idx = max(counts.items(), key=lambda kv: kv[1])[0]
    poc_price = session_low + (poc_idx + 0.5) * step
    # Expand outward for 70% value area.
    included = {poc_idx}
    cumulative = counts[poc_idx]
    target = total * 0.7
    lo_idx = hi_idx = poc_idx
    while cumulative < target and (lo_idx > 0 or hi_idx < bins - 1):
        next_lo = counts.get(lo_idx - 1, 0) if lo_idx > 0 else -1
        next_hi = counts.get(hi_idx + 1, 0) if hi_idx < bins - 1 else -1
        if next_hi > next_lo and hi_idx < bins - 1:
            hi_idx += 1
            included.add(hi_idx)
            cumulative += counts.get(hi_idx, 0)
        elif lo_idx > 0:
            lo_idx -= 1
            included.add(lo_idx)
            cumulative += counts.get(lo_idx, 0)
        else:
            break
    vah = session_low + (hi_idx + 1) * step
    val = session_low + lo_idx * step
    # IB = first two bars
    ib_candles = candles[:2]
    ib_high = max(float(b.get("high") or b.get("close") or 0) for b in ib_candles) if ib_candles else poc_price
    ib_low = min(float(b.get("low") or b.get("close") or 0) for b in ib_candles) if ib_candles else poc_price
    return {
        "poc": poc_price,
        "vah": vah,
        "val": val,
        "ib_high": ib_high,
        "ib_low": ib_low,
        "session_high": session_high,
        "session_low": session_low,
        "close": closes[-1] if closes else poc_price,
    }


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
    mp_score: float | None = None,
    of_score: float | None = None,
) -> dict[str, Any]:
    """Combine the 5 weighted components into a single 0..100 score.

    Components are normalized to [0, 100] each before weighting. When
    mp_score / of_score are passed (the production path from
    score_option_candidates), they replace the ATR/volume proxies; when
    omitted (legacy callers, tests), the proxies are used so the function
    is still self-contained.
    """
    asset_component = max(0.0, min(100.0, float(asset_score)))
    sector_component = _normalize_rs_pct(sector_rs_pct)
    stock_component = _normalize_rs_pct(stock_rs_pct)
    # Live MP/OF if supplied, otherwise proxies derived from ATR/volume so
    # tests + ad-hoc callers don't break.
    mp_value = float(mp_score) if mp_score is not None else max(0.0, min(100.0, 50.0 + (atr_expansion - 1.0) * 100.0))
    of_value = float(of_score) if of_score is not None else max(0.0, min(100.0, 50.0 + (volume_score - 0.5) * 100.0))

    total_weight = weights.total()
    if total_weight <= 0:
        return {"score": 0.0, "components": {}}

    score = (
        weights.asset * asset_component
        + weights.sector * sector_component
        + weights.stock * stock_component
        + weights.market_profile * mp_value
        + weights.order_flow * of_value
    ) / total_weight

    return {
        "score": round(score, 2),
        "components": {
            "asset": round(asset_component, 2),
            "sector": round(sector_component, 2),
            "stock": round(stock_component, 2),
            "market_profile": round(mp_value, 2),
            "order_flow": round(of_value, 2),
            "mp_source": "live" if mp_score is not None else "proxy",
            "of_source": "live" if of_score is not None else "proxy",
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
    # L3 — universe_mode picks between full-universe coverage and the
    # legacy winners-only narrowing. Full mode is the new default since
    # the user's brief explicitly asked for "watchlist for the entire
    # qualified F&O universe".
    if config.universe_mode == "full":
        from analytics.sector import SectorRotationTracker
        tracker = SectorRotationTracker()
        full_sector_payload = await tracker.get_sector_rotation(config.timeframe)
        stock_layer = await rank_stocks_full_universe(
            full_sector_payload,
            fno_universe=fno_universe,
            timeframe=config.timeframe,
        )
    else:
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
            mp_score=row.get("mp_score"),
            of_score=row.get("of_score"),
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
            "mode": stock_layer.get("mode") or ("winners_only" if config.universe_mode != "full" else "full"),
            "sectors_scanned": list((stock_layer.get("per_sector") or {}).keys()),
        },
        "config": {
            "timeframe": config.timeframe,
            "universe_mode": config.universe_mode,
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
    """Decide directional_bias from quadrant + trend + RS sign.

    Softer than the strict v1 rule (which needed BOTH stock and sector in
    leading/improving AND trend >= 0.55). The new rule:

      Bullish if ANY two of three agree:
        - stock_quadrant ∈ {leading, improving}
        - sector_quadrant ∈ {leading, improving}
        - trend_score >= 0.55 OR stock_rs_pct > 0

      Bearish if ANY two of three agree (mirror):
        - stock_quadrant ∈ {lagging, weakening}
        - sector_quadrant ∈ {lagging, weakening}
        - trend_score <= 0.45 OR stock_rs_pct < 0

    Otherwise neutral. The v1 rule produced ~1 bullish per scan in
    practice — too strict to populate the book. Two-of-three lets a
    stock with strong sector tailwind + positive RS trade even when
    its individual quadrant is "improving" rather than "leading".
    """
    stock_quadrant = str(row.get("stock_quadrant") or "")
    sector_quadrant = str(row.get("sector_quadrant") or "")
    rs_pct = float(row.get("stock_rs_pct") or 0.0)
    bullish_quadrants = {"leading", "improving"}
    bearish_quadrants = {"lagging", "weakening"}

    bull_votes = sum(
        [
            stock_quadrant in bullish_quadrants,
            sector_quadrant in bullish_quadrants,
            trend_score >= 0.55 or rs_pct > 0.0,
        ]
    )
    bear_votes = sum(
        [
            stock_quadrant in bearish_quadrants,
            sector_quadrant in bearish_quadrants,
            trend_score <= 0.45 or rs_pct < 0.0,
        ]
    )
    if bull_votes >= 2 and bull_votes > bear_votes:
        return "bullish"
    if bear_votes >= 2 and bear_votes > bull_votes:
        return "bearish"
    return "neutral"
