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

# Forward-leaning composite weights — must sum to 1.0. Designed to surface
# new fast runners (acceleration + RRG trajectory + confirmed divergence)
# alongside continuation plays (existing winners still accelerating), not
# just whichever asset has the highest 12-month return.
WEIGHT_ACCEL:         float = 0.25  # vol-adj 20d-vs-120d slope diff
WEIGHT_RRG_TRAJ:      float = 0.20  # rising/falling RS slope velocity
WEIGHT_CONFIRMATION:  float = 0.15  # MACD-hist sign / volume z / above 50d MA
WEIGHT_MOM_3M:        float = 0.15  # short-window historical
WEIGHT_MOM_6M:        float = 0.10  # medium-window historical
WEIGHT_ABOVE_30W_MA:  float = 0.10  # binary regime
WEIGHT_MOM_12M:       float = 0.05  # long-term persistence anchor (deprioritized)


# Asset-class verdicts that drive position sizing.
VERDICT_ACCELERATE = "ACCELERATE"  # winner + accel positive  → 100% size
VERDICT_MAINTAIN   = "MAINTAIN"    # winner + accel flat       → 70%
VERDICT_REDUCE     = "REDUCE"      # was-winner, accel negative → 30%
VERDICT_PROBE      = "PROBE"       # lagging but accel turning  → 30%
VERDICT_AVOID      = "AVOID"       # composite low + accel flat → 0%


@dataclass
class AssetMomentum:
    asset: str
    symbol: str
    momentum_3m: float | None
    momentum_6m: float | None
    momentum_12m: float | None
    above_30w_ma: bool | None
    rs_trend: float | None
    # Forward-leaning fields
    acceleration: float | None      # vol-adj (20d slope − 120d slope)
    rs_velocity: float | None       # change in RS line over last 4 weeks
    rrg_quadrant: str | None        # leading/improving/weakening/lagging
    bullish_divergence: bool | None
    confirmation_count: int         # 0..3 (MACD-hist / above-50d / volume-z)
    confirmation_flags: dict        # which confirmations fired
    score: float
    verdict: str                    # ACCELERATE/MAINTAIN/REDUCE/PROBE/AVOID
    bars_available: int


async def fetch_asset_history(symbol: str, *, lookback_days: int = 400) -> list[float]:
    """Return chronological daily-close list for the symbol.

    Prefers `interval='day'` rows (ETF backfill writes here). Falls back
    to 30-min resampling for symbols like NIFTY where intraday is the
    only source available.
    """
    from db.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        # Pass 1 — proper daily bars (ETFs after ingest_etf_history script).
        daily_result = await session.execute(
            text(
                """
                SELECT close FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = 'day'
                  AND time >= NOW() - (:lookback_days || ' days')::interval
                ORDER BY time ASC
                """
            ),
            {"underlying": symbol, "lookback_days": str(int(lookback_days))},
        )
        rows = [float(r[0]) for r in daily_result.fetchall() if r[0] is not None]
        if rows:
            return rows

        # Pass 2 — fall back to 30-min resampled to daily (NIFTY/BANKNIFTY etc.).
        intraday_result = await session.execute(
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
            {"underlying": symbol, "lookback_days": str(int(lookback_days))},
        )
        return [float(r[1]) for r in intraday_result.fetchall() if r[1] is not None]


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


# ─── Forward-leaning indicator helpers ─────────────────────────────────────
def _realized_vol(series: list[float], window: int = 60) -> float:
    """Annualized realized vol (stdev of log returns × √252) over `window` bars."""
    if len(series) < window + 2:
        return 0.0
    import math
    window_series = series[-(window + 1):]
    rets = [
        math.log(window_series[i] / window_series[i - 1])
        for i in range(1, len(window_series))
        if window_series[i - 1] > 0 and window_series[i] > 0
    ]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return (var ** 0.5) * (252 ** 0.5)


def _acceleration(series: list[float]) -> float | None:
    """Vol-adjusted (short_slope − long_slope). Positive = accelerating.

    short_slope  = average daily return over last 20 bars
    long_slope   = average daily return over last 120 bars
    Result is in units of "daily-return percentage points per day, divided by
    annualized vol". Higher = the recent trend is sharply outpacing the
    long-term trend, after correcting for the asset's natural volatility.
    """
    if len(series) < 125 or series[-21] == 0 or series[-121] == 0:
        return None
    short_ret = (series[-1] / series[-21]) ** (1 / 20) - 1.0
    long_ret = (series[-1] / series[-121]) ** (1 / 120) - 1.0
    diff = (short_ret - long_ret) * 100.0
    vol = _realized_vol(series, 60)
    if vol <= 0:
        return diff
    return diff / vol


def _rs_velocity(asset_series: list[float], benchmark_series: list[float]) -> float | None:
    """Change in the RS ratio over the last 20 trading days. Positive = RS
    line trending up (asset is gaining vs benchmark)."""
    if len(asset_series) < 25 or len(benchmark_series) < 25:
        return None
    align = min(len(asset_series), len(benchmark_series))
    a = asset_series[-align:]
    b = benchmark_series[-align:]
    ratio_now = a[-1] / b[-1] if b[-1] else None
    ratio_then = a[-20] / b[-20] if b[-20] else None
    if not ratio_now or not ratio_then or ratio_then == 0:
        return None
    return (ratio_now / ratio_then - 1.0) * 100.0


def _rrg_quadrant(rs_pct: float | None, rs_velocity: float | None) -> str:
    """Classify into the four RRG quadrants:
      leading    = RS strong, velocity strong → continuation candidate
      improving  = RS weak, velocity strong   → THE NEW FAST RUNNERS
      weakening  = RS strong, velocity weak   → exit candidate
      lagging    = RS weak, velocity weak     → avoid
    """
    r = float(rs_pct or 0.0)
    v = float(rs_velocity or 0.0)
    if r >= 0 and v >= 0:
        return "leading"
    if r < 0 and v >= 0:
        return "improving"
    if r >= 0 and v < 0:
        return "weakening"
    return "lagging"


def _bullish_divergence(series: list[float]) -> bool | None:
    """Crude bullish divergence: in the last 30 bars, did price make a lower
    low while a 14-period RSI made a higher low? True signals an early bottom.
    """
    if len(series) < 45:
        return None
    window = series[-30:]
    # Find local lows in the price window (simple — actual lows, not pivots)
    half = 15
    p1_low = min(window[:half])
    p2_low = min(window[half:])
    if p2_low >= p1_low:
        return False  # not a lower-low setup
    # Compute RSI(14) on the full series; check RSI at the two lows
    rsi_series = _rsi_series(series, 14)
    if len(rsi_series) < 30:
        return None
    rsi_window = rsi_series[-30:]
    rsi_p1 = min(rsi_window[:half])
    rsi_p2 = min(rsi_window[half:])
    return rsi_p2 > rsi_p1


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI series. Used for divergence detection."""
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out: list[float] = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        out.append(100.0 - (100.0 / (1.0 + rs)) if avg_loss else 100.0)
    return out


def _confirmation_overlay(series: list[float]) -> tuple[int, dict]:
    """Three independent confirmation signals. Returns (count, flags).

      - macd_hist_positive_cross:  MACD histogram crossed above zero in last 5 bars
      - above_50d_ma_rising:       close > 50d MA AND 50d MA above its 10-day-ago value
      - volume_proxy_strong:       no volume in spot series — approximated by
                                   recent realized range / trailing range > 1.2
                                   (institutional accumulation widens range)
    """
    flags = {"macd_hist_positive_cross": False, "above_50d_ma_rising": False, "range_expansion": False}

    # MACD histogram (12, 26, 9) — recent cross above zero?
    if len(series) >= 35:
        ema12 = _ema(series, 12)
        ema26 = _ema(series, 26)
        macd_line = [a - b for a, b in zip(ema12, ema26)]
        signal_line = _ema(macd_line[-50:], 9)
        # Align lengths
        n = min(len(macd_line), len(signal_line))
        macd_line = macd_line[-n:]
        signal_line = signal_line[-n:]
        hist = [m - s for m, s in zip(macd_line, signal_line)]
        # Did hist transition from <=0 to >0 in last 5 bars?
        recent = hist[-5:]
        if any(prev <= 0 and curr > 0 for prev, curr in zip(recent[:-1], recent[1:])):
            flags["macd_hist_positive_cross"] = True

    # 50-day MA above its 10-day-ago value AND price above MA
    if len(series) >= 60:
        ma50 = sum(series[-50:]) / 50.0
        ma50_then = sum(series[-60:-10]) / 50.0
        if series[-1] > ma50 and ma50 > ma50_then:
            flags["above_50d_ma_rising"] = True

    # Range expansion as volume proxy (we don't have ETF volume reliably)
    if len(series) >= 60:
        recent_range = max(series[-20:]) - min(series[-20:])
        trailing_range = max(series[-60:-20]) - min(series[-60:-20])
        if trailing_range > 0 and recent_range / trailing_range > 1.2:
            flags["range_expansion"] = True

    return sum(1 for v in flags.values() if v), flags


def _ema(series: list[float], period: int) -> list[float]:
    if not series or period <= 0:
        return []
    k = 2 / (period + 1)
    out = [series[0]]
    last = series[0]
    for value in series[1:]:
        last = value * k + last * (1 - k)
        out.append(last)
    return out


def _classify_verdict(*, composite: float, accel: float | None, rrg_quadrant: str, confirmation_count: int) -> str:
    """Map (composite, acceleration, quadrant, confirmations) → verdict.

      ACCELERATE  composite ≥ 60 AND accel ≥ 0.05 AND quadrant in (leading, improving)
      MAINTAIN    composite ≥ 60 AND accel between [-0.05, 0.05]
      REDUCE      composite ≥ 60 AND accel < -0.05  (was-winner cooling off)
      PROBE       composite < 60 AND accel ≥ 0.05 AND confirmation_count >= 2 AND quadrant = improving
      AVOID       everything else
    """
    a = float(accel or 0.0)
    if composite >= 60.0:
        if a >= 0.05 and rrg_quadrant in ("leading", "improving"):
            return VERDICT_ACCELERATE
        if a < -0.05:
            return VERDICT_REDUCE
        return VERDICT_MAINTAIN
    # Composite below the leadership floor — only PROBE if showing real turn-up.
    if a >= 0.05 and rrg_quadrant == "improving" and confirmation_count >= 2:
        return VERDICT_PROBE
    return VERDICT_AVOID


# Verdict → equity-exposure-pct mapping when verdict applies to EQUITIES.
_VERDICT_EXPOSURE: dict[str, float] = {
    VERDICT_ACCELERATE: 100.0,
    VERDICT_MAINTAIN:    70.0,
    VERDICT_REDUCE:      30.0,
    VERDICT_PROBE:       30.0,
    VERDICT_AVOID:       20.0,
}


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
        # Historical anchors (kept but down-weighted)
        m3 = _pct_change(series, 63)
        m6 = _pct_change(series, 126)
        m12 = _pct_change(series, 252)
        above_ma = _above_simple_ma(series, 150)
        rs = _rs_trend(series, benchmark_series) if asset != "EQUITIES" else 0.0
        # Forward-leaning components
        accel = _acceleration(series)
        rs_vel = _rs_velocity(series, benchmark_series) if asset != "EQUITIES" else 0.0
        rrg_q = _rrg_quadrant(rs, rs_vel)
        bull_div = _bullish_divergence(series)
        conf_count, conf_flags = _confirmation_overlay(series)

        # Per-component sub-scores normalized to [0, 100]
        s_accel = _normalize_to_100(accel, mid=0.0, scale=0.20)         # ±0.20 vol-units → ±100
        s_traj  = _normalize_to_100(rs_vel, mid=0.0, scale=5.0)         # ±5% RS velocity
        s_conf  = (conf_count / 3.0) * 100.0                            # 0..3 → 0..100
        s_3m    = _normalize_to_100(m3,  mid=0.0, scale=10.0)
        s_6m    = _normalize_to_100(m6,  mid=0.0, scale=15.0)
        s_12m   = _normalize_to_100(m12, mid=0.0, scale=25.0)
        s_ma    = 75.0 if above_ma else 25.0

        total = (
            s_accel * WEIGHT_ACCEL
            + s_traj  * WEIGHT_RRG_TRAJ
            + s_conf  * WEIGHT_CONFIRMATION
            + s_3m    * WEIGHT_MOM_3M
            + s_6m    * WEIGHT_MOM_6M
            + s_12m   * WEIGHT_MOM_12M
            + s_ma    * WEIGHT_ABOVE_30W_MA
        )

        verdict = _classify_verdict(
            composite=total,
            accel=accel,
            rrg_quadrant=rrg_q,
            confirmation_count=conf_count,
        )

        momentum.append(
            AssetMomentum(
                asset=asset,
                symbol=ASSET_TO_SYMBOL[asset],
                momentum_3m=m3,
                momentum_6m=m6,
                momentum_12m=m12,
                above_30w_ma=above_ma,
                rs_trend=rs,
                acceleration=accel,
                rs_velocity=rs_vel,
                rrg_quadrant=rrg_q,
                bullish_divergence=bull_div,
                confirmation_count=conf_count,
                confirmation_flags=conf_flags,
                score=round(total, 2),
                verdict=verdict,
                bars_available=len(series),
            )
        )

    momentum.sort(key=lambda m: m.score, reverse=True)
    winner = momentum[0].asset if momentum else "EQUITIES"

    # Forward-leaning score_for_engine: drive paper-book sizing from the
    # EQUITIES verdict, NOT just its rank. A REDUCE on equities means cut
    # exposure now even if equities is still the absolute-score winner;
    # a PROBE on equities (currently weak but turning up) earns 30% — a
    # small starter position to catch the inflection.
    equities = next((m for m in momentum if m.asset == "EQUITIES"), None)
    equities_exposure = _VERDICT_EXPOSURE.get(
        equities.verdict if equities else "AVOID", 20.0
    )

    return {
        "winner": winner,
        "asset_rank": [
            {
                "asset": m.asset,
                "symbol": m.symbol,
                # Historical anchors
                "momentum_3m": round(m.momentum_3m, 2) if m.momentum_3m is not None else None,
                "momentum_6m": round(m.momentum_6m, 2) if m.momentum_6m is not None else None,
                "momentum_12m": round(m.momentum_12m, 2) if m.momentum_12m is not None else None,
                "above_30w_ma": m.above_30w_ma,
                "rs_trend": round(m.rs_trend, 2) if m.rs_trend is not None else None,
                # Forward-leaning
                "acceleration": round(m.acceleration, 4) if m.acceleration is not None else None,
                "rs_velocity": round(m.rs_velocity, 2) if m.rs_velocity is not None else None,
                "rrg_quadrant": m.rrg_quadrant,
                "bullish_divergence": m.bullish_divergence,
                "confirmation_count": m.confirmation_count,
                "confirmation_flags": m.confirmation_flags,
                "verdict": m.verdict,
                "score": m.score,
                "bars": m.bars_available,
            }
            for m in momentum
        ],
        "score_for_engine": equities.score if equities else 50.0,
        "equity_verdict": equities.verdict if equities else "AVOID",
        "equity_exposure_pct_hint": equities_exposure,
        "stub": False,
        "scoring_version": "forward_leaning_v1",
    }
