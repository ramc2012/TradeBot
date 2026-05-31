"""L1 — Asset rotation across Equities / Gold / Silver / Bonds / Cash.

Design
------
The alpha engine's L7 composite scorer needs an *asset_score* ∈ [0, 100]
that reflects whether equities are the right asset class to be in right
now. The L1 layer compares 5 broad asset proxies vs the Nifty50
benchmark on multi-horizon momentum and emits:

  {
    "winner":           "EQUITIES" | "GOLD" | "SILVER" | "BONDS" | "CASH",
    "asset_rank":       [ {asset, momentum_3m, momentum_6m, momentum_12m,
                           above_30w_ma, rs_trend, score} ],
    "score_for_engine": 0..100,
    "stub":             bool,
  }

Score weights (per user spec):
  3-month momentum: 30%
  6-month momentum: 30%
  12-month momentum: 20%
  above 30W MA:     10%
  RS trend:         10%

Universe (NSE-listed ETFs to avoid commodity-futures roll quirks):
  GOLDBEES   — Nippon India Gold ETF        proxy for GOLD
  SILVERBEES — Nippon India Silver ETF      proxy for SILVER
  BBETF      — Bharat Bond ETF (Apr 2031)   proxy for BONDS (long duration)
  LIQUIDBEES — Nippon India Liquid ETF      proxy for CASH (~7% risk-free)
  NIFTY      — Nifty 50 index               benchmark + EQUITIES proxy

Status (this commit)
--------------------
File-scaffold only. Real ingestion + ranking is gated behind the
presence of rows in `underlying_spot_candles` for the ETF symbols.
Until those rows exist, `rank_asset_classes_live()` returns
`{stub: True, winner: "EQUITIES", score_for_engine: 100}` so the
alpha engine continues to run.

When ETF spot data is ingested:
  1. Hardcoded Upstox instrument_keys below get replaced with a lookup
     from `fo_contract_catalog` if/when these ETFs are added there.
  2. `MarketIntelligenceRuntime.gap_fill_spot_history(symbols=ETF_UNIVERSE)`
     will populate daily bars exactly the way it does for stocks today.
  3. Switch `rank_asset_classes()` in alpha_engine.py to call
     `rank_asset_classes_live()` instead of the stub.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Upstox instrument keys for the ETF universe. These are publicly known
# NSE_EQ tickers; keys may need verification against the actual Upstox
# instrument master before going live.
ETF_INSTRUMENT_KEYS: dict[str, str] = {
    "GOLDBEES":   "NSE_EQ|INE528G01035",
    "SILVERBEES": "NSE_EQ|INF204KB14I2",
    "BBETF":      "NSE_EQ|INF917L01EC7",
    "LIQUIDBEES": "NSE_EQ|INF204KA1AA2",
    # GILTBEES alt: long-dated gilt ETF; uncomment when needed.
    # "GILTBEES":   "NSE_EQ|INE731K01017",
}

ASSET_TO_SYMBOL: dict[str, str] = {
    "EQUITIES": "NIFTY",     # Already ingested as an index
    "GOLD":     "GOLDBEES",
    "SILVER":   "SILVERBEES",
    "BONDS":    "BBETF",
    "CASH":     "LIQUIDBEES",
}

# Score component weights — must sum to 1.0.
WEIGHT_MOM_3M:        float = 0.30
WEIGHT_MOM_6M:        float = 0.30
WEIGHT_MOM_12M:       float = 0.20
WEIGHT_ABOVE_30W_MA:  float = 0.10
WEIGHT_RS_TREND:      float = 0.10


@dataclass
class AssetMomentum:
    asset: str
    symbol: str
    momentum_3m: float | None
    momentum_6m: float | None
    momentum_12m: float | None
    above_30w_ma: bool | None
    rs_trend: float | None
    score: float
    bars_available: int


async def fetch_asset_history(symbol: str, *, lookback_days: int = 400) -> list[float]:
    """Return chronological daily-close list for the symbol.

    Pulls from `underlying_spot_candles` at the 30-min interval and
    resamples to last-bar-of-day for the daily series. Returns empty
    list when no bars exist (caller falls back to stub).
    """
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                WITH daily AS (
                    SELECT DATE(time) AS d,
                           (ARRAY_AGG(close ORDER BY time DESC))[1] AS close
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '30minute'
                      AND time >= NOW() - (:lookback_days || ' days')::interval
                    GROUP BY DATE(time)
                )
                SELECT d, close FROM daily ORDER BY d ASC
                """
            ),
            {"underlying": symbol, "lookback_days": lookback_days},
        )
        rows = [float(r[1]) for r in result.fetchall() if r[1] is not None]
        return rows


def _pct_change(series: list[float], periods: int) -> float | None:
    if len(series) <= periods or series[-periods - 1] == 0:
        return None
    return (series[-1] / series[-periods - 1] - 1.0) * 100.0


def _above_simple_ma(series: list[float], window_bars: int) -> bool | None:
    if len(series) < window_bars:
        return None
    ma = sum(series[-window_bars:]) / window_bars
    return series[-1] > ma


def _rs_trend(asset_series: list[float], benchmark_series: list[float], lookback: int = 60) -> float | None:
    if len(asset_series) < lookback + 1 or len(benchmark_series) < lookback + 1:
        return None
    align = min(len(asset_series), len(benchmark_series))
    a = asset_series[-align:]
    b = benchmark_series[-align:]
    ratio = [(a_v / b_v) if b_v else 1.0 for a_v, b_v in zip(a, b)]
    recent = sum(ratio[-20:]) / 20.0
    baseline = sum(ratio[-lookback:-lookback + 20]) / 20.0
    if baseline == 0:
        return None
    return (recent / baseline - 1.0) * 100.0


def _normalize_to_100(value: float | None, *, mid: float = 0.0, scale: float = 10.0) -> float:
    """Map a percent value to [0, 100] with `mid` → 50."""
    if value is None:
        return 50.0
    raw = 50.0 + (value - mid) * (50.0 / scale)
    return max(0.0, min(100.0, raw))


async def rank_asset_classes_live() -> dict[str, Any]:
    """Live asset-rotation ranking. Returns stub if any ETF data missing."""
    series_by_asset: dict[str, list[float]] = {}
    for asset, symbol in ASSET_TO_SYMBOL.items():
        try:
            series = await fetch_asset_history(symbol, lookback_days=400)
        except Exception as exc:
            logger.warning(f"[L1] fetch failed for {asset}/{symbol}: {exc}")
            series = []
        series_by_asset[asset] = series

    # Need at least Nifty + one other asset to compute RS. If any asset
    # lacks a meaningful series, fall back to the stub.
    sufficient = all(len(series) >= 200 for series in series_by_asset.values())
    if not sufficient:
        missing = [
            asset for asset, series in series_by_asset.items()
            if len(series) < 200
        ]
        return {
            "winner": "EQUITIES",
            "asset_rank": [
                {"asset": asset, "symbol": ASSET_TO_SYMBOL[asset], "bars": len(series)}
                for asset, series in series_by_asset.items()
            ],
            "score_for_engine": 100.0,
            "stub": True,
            "stub_reason": f"insufficient daily history for: {', '.join(missing)}",
        }

    benchmark_series = series_by_asset["EQUITIES"]
    momentum: list[AssetMomentum] = []
    for asset in ASSET_TO_SYMBOL:
        series = series_by_asset[asset]
        m3 = _pct_change(series, 63)   # ~3 months of trading days
        m6 = _pct_change(series, 126)
        m12 = _pct_change(series, 252)
        above_ma = _above_simple_ma(series, 150)  # 30 weeks ≈ 150 trading days
        rs = _rs_trend(series, benchmark_series) if asset != "EQUITIES" else 0.0
        score_3m  = _normalize_to_100(m3,  mid=0.0, scale=10.0) * WEIGHT_MOM_3M
        score_6m  = _normalize_to_100(m6,  mid=0.0, scale=15.0) * WEIGHT_MOM_6M
        score_12m = _normalize_to_100(m12, mid=0.0, scale=25.0) * WEIGHT_MOM_12M
        score_ma  = (75.0 if above_ma else 25.0) * WEIGHT_ABOVE_30W_MA
        score_rs  = _normalize_to_100(rs, mid=0.0, scale=10.0) * WEIGHT_RS_TREND
        total = score_3m + score_6m + score_12m + score_ma + score_rs
        momentum.append(
            AssetMomentum(
                asset=asset,
                symbol=ASSET_TO_SYMBOL[asset],
                momentum_3m=m3,
                momentum_6m=m6,
                momentum_12m=m12,
                above_30w_ma=above_ma,
                rs_trend=rs,
                score=round(total, 2),
                bars_available=len(series),
            )
        )

    momentum.sort(key=lambda m: m.score, reverse=True)
    winner = momentum[0].asset if momentum else "EQUITIES"

    # The score_for_engine surfaces equities' standing — what L7 actually
    # weights. When equities are winning, score is high (100). When losing
    # badly to gold/bonds, score drops accordingly.
    equities_score = next((m.score for m in momentum if m.asset == "EQUITIES"), 50.0)

    return {
        "winner": winner,
        "asset_rank": [
            {
                "asset": m.asset,
                "symbol": m.symbol,
                "momentum_3m": round(m.momentum_3m, 2) if m.momentum_3m is not None else None,
                "momentum_6m": round(m.momentum_6m, 2) if m.momentum_6m is not None else None,
                "momentum_12m": round(m.momentum_12m, 2) if m.momentum_12m is not None else None,
                "above_30w_ma": m.above_30w_ma,
                "rs_trend": round(m.rs_trend, 2) if m.rs_trend is not None else None,
                "score": m.score,
                "bars": m.bars_available,
            }
            for m in momentum
        ],
        "score_for_engine": equities_score,
        "stub": False,
    }
