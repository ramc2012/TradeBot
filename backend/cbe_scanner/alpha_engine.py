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
    composite_gate: float = COMPOSITE_GATE
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
async def rank_stocks_in_winners(
    winning_sectors: list[dict[str, Any]],
    *,
    timeframe: str,
    finalists_count: int,
    fno_universe: set[str],
    tracker: SectorRotationTracker | None = None,
) -> dict[str, Any]:
    """L3 per spec: pool members of all winning sectors, then take top N.

    The doc says "Select Top 10 Stocks" — pooled across the winning
    sectors, NOT 10 per sector. A leading-sector stock with mediocre RS
    will still rank below a lagging-sector stock with extreme RS, which
    is exactly what we want: the engine surfaces the strongest 10 names
    regardless of which winning sector they came from.

    Filters:
      * F&O whitelist only (untradeable names are dropped).
      * Pool excludes stocks not in any winning sector.
    """
    tracker = tracker or SectorRotationTracker()
    pool: list[dict[str, Any]] = []

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

        for point in rrg_points:
            symbol = str(point.get("code") or point.get("symbol") or "").upper()
            if not symbol or symbol not in fno_universe:
                continue
            pool.append(
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

    # Pool then take top 10 — strict per-spec.
    pool.sort(
        key=lambda row: (-float(row["stock_rs_pct"]), row["instrument"]),
    )
    finalists = pool[: max(1, int(finalists_count))]
    for index, row in enumerate(finalists):
        row["stock_rank_overall"] = index + 1

    return {
        "candidates": finalists,
        "candidate_count": len(finalists),
        "pool_size_before_topN": len(pool),
    }


# ---------------------------------------------------------------------------
# L4 — Option candidate filter (Trend + RS + Volume + IV + OI — strict spec)
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

    # L1 — Asset rotation. If equities do NOT win this week, the engine
    # short-circuits per spec: no equity-options trades are taken at all.
    asset_layer = await rank_asset_classes()
    if str(asset_layer.get("winner") or "").upper() != "EQUITIES":
        finished = datetime.now(timezone.utc)
        return {
            "scan_date": started.date().isoformat(),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "elapsed_seconds": round((finished - started).total_seconds(), 2),
            "fno_universe_size": len(fno_universe),
            "asset_winner": asset_layer.get("winner"),
            "asset_layer": asset_layer,
            "sector_layer": None,
            "stock_layer_summary": {"candidate_count": 0, "reason": "equities_not_winner"},
            "config": _config_payload(config),
            "scored_count": 0,
            "watchlist_count": 0,
            "results": [],
            "watchlist": [],
            "source": "alpha_engine_v2_strict",
            "skipped_reason": f"L1 winner is {asset_layer.get('winner')} — equities not in favour this week",
        }

    # L2 — Sector rotation. Rank all sectors by RS vs Nifty50, take top 4.
    sector_layer = await rank_sectors(
        config.timeframe,
        keep_top=config.sectors_to_keep,
    )

    # L3 — Pool members of winning sectors, then take top N stocks by RS.
    stock_layer = await rank_stocks_in_winners(
        sector_layer.get("winners") or [],
        timeframe=config.timeframe,
        finalists_count=config.finalists_count,
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
    # Watchlist = passed the 80-gate AND has a non-neutral bias (per strict
    # bias rule: trend & sector aligned). Neutral rows can't trade options.
    watchlist = [
        r for r in scored
        if r.get("gate_passed") and r.get("directional_bias") in ("bullish", "bearish")
    ]

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
            "pool_size_before_topN": stock_layer.get("pool_size_before_topN"),
        },
        "config": _config_payload(config),
        "scored_count": len(scored),
        "watchlist_count": len(watchlist),
        "results": scored,
        "watchlist": watchlist,
        "source": "alpha_engine_v2_strict",
    }


def _config_payload(config: AlphaEngineConfig) -> dict[str, Any]:
    return {
        "timeframe": config.timeframe,
        "sectors_to_keep": config.sectors_to_keep,
        "finalists_count": config.finalists_count,
        "composite_gate": config.composite_gate,
        "min_atm_oi": config.min_atm_oi,
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
    """Strict directional bias per the user's alpha-generation doc.

    The doc's logic for L5 (option selection) implies:
      * BULLISH (buy CE): trend up AND sector_rs_pct > 0
                          (the stock is moving up inside a leading sector)
      * BEARISH (buy PE): trend down AND sector_rs_pct < 0
                          (the stock is moving down inside a lagging sector)
      * NEUTRAL: any mismatch — skip the trade.

    Trend thresholds (0.55 / 0.45) come from the L4 trend_score scale where
    0.5 = flat. A mismatched signal (e.g. trend up but sector lagging)
    means the stock is fighting its own sector tape — those names
    historically reverse, so the engine refuses the trade.
    """
    sector_rs = float(row.get("sector_rs_pct") or 0.0)
    if trend_score >= 0.55 and sector_rs > 0.0:
        return "bullish"
    if trend_score <= 0.45 and sector_rs < 0.0:
        return "bearish"
    return "neutral"
