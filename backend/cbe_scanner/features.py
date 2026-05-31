"""
cbe_scanner/features.py
========================

Compression-Before-Expansion (CBE) feature pipeline.

For each F&O stock at end-of-day, computes 5 features designed to identify
instruments that have a high probability of making a 4%+ directional move
within the next 5 trading days.

Each feature returns a 0-10 sub-score. The composite CBE Score is a weighted
sum, with the directional bias determined by the asymmetry of certain features.

Features:
    F1. Volatility Compression (VC)
    F2. Option Market Positioning (OMP)
    F3. Cross-Sectional Momentum Divergence (CSMD)
    F4. Catalyst Proximity (CP)
    F5. Microstructure Pressure (MP)

All features are computed from OHLCV + options chain data, both of which are
available in your TimescaleDB schema via Fyers feed.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, asdict
from typing import Optional


# ============================================================
# CONFIG: All thresholds in one place for easy tuning + backtest sweeps
# ============================================================

@dataclass
class CBEConfig:
    # F1: Volatility Compression
    vc_lookback_short: int = 20       # short-window ATR
    vc_lookback_long: int = 60        # long-window ATR
    vc_percentile_threshold: float = 30.0  # ATR ratio percentile under which we score high
    vc_bb_period: int = 20            # Bollinger band period
    vc_consecutive_days: int = 5      # min consecutive contracting days

    # F2: Option Market Positioning
    omp_iv_rank_lookback: int = 252   # one year of trading days
    omp_iv_rank_low: float = 30.0     # below this = "cheap" options
    omp_pcr_baseline_window: int = 60 # PCR baseline window
    omp_oi_change_threshold: float = 0.20  # 20% OI buildup at OTM strikes

    # F3: Cross-Sectional Momentum Divergence
    csmd_window: int = 20             # stock vs sector return window
    csmd_min_divergence: float = 0.05 # 5% absolute divergence

    # F4: Catalyst Proximity
    cp_max_days_to_event: int = 5     # within 5 trading days
    cp_event_type_weights = None      # set in __post_init__

    # F5: Microstructure Pressure
    mp_spread_window: int = 20
    mp_block_deal_window: int = 10
    mp_fii_dii_window: int = 3

    # F6 (NEW): Volatility Cone (Burghardt-Lane)
    # Realized vol over multiple horizons, percentile vs trailing history.
    # When ALL horizons sit below their 25th percentile simultaneously,
    # compression is real (not just one-bar noise).
    cone_horizons: tuple = (10, 20, 40, 60)
    cone_percentile_history: int = 252
    cone_compression_pct: float = 25.0     # all horizons below this %ile = compression
    cone_min_history_days: int = 80        # gracefully degrade when shorter

    # F7 (NEW): IV Term Structure
    # Front IV < back IV by >1σ of historical spread = vol-floor / squeeze setup.
    its_lookback_days: int = 60

    # Composite weights (must sum to 1.0). These are PRIORS; the actual scoring
    # at composite time renormalizes weights to the subset of features that
    # actually returned data (so dead feeds don't permanently cap the score).
    w_vc: float = 0.20
    w_omp: float = 0.20
    w_csmd: float = 0.12
    w_cp: float = 0.13
    w_mp: float = 0.10
    w_cone: float = 0.15
    w_its: float = 0.10

    # Final score threshold for watchlist inclusion
    watchlist_min_score: float = 5.5
    watchlist_max_size: int = 15
    min_ohlc_rows: int = 5

    def __post_init__(self):
        if self.cp_event_type_weights is None:
            self.cp_event_type_weights = {
                "earnings": 1.0,
                "rbi_policy": 0.9,
                "budget": 0.85,
                "board_meeting": 0.6,
                "ex_dividend": 0.4,
                "expiry_week": 0.5,
                "sector_event": 0.7,
            }
        total_w = self.w_vc + self.w_omp + self.w_csmd + self.w_cp + self.w_mp + self.w_cone + self.w_its
        assert abs(total_w - 1.0) < 1e-6, \
            f"Composite weights must sum to 1.0 (got {total_w:.6f})"


# ============================================================
# F1: VOLATILITY COMPRESSION
# ============================================================

def feature_volatility_compression(ohlc: pd.DataFrame, cfg: CBEConfig) -> dict:
    """
    Score 0-10 based on how compressed volatility is RIGHT NOW vs trailing window.

    Signals computed:
        - ATR ratio: short_ATR / long_ATR (low = compression)
        - Bollinger Band Width percentile (low = compression)
        - Consecutive days of contracting range
        - Historical vol at multi-month low

    Args:
        ohlc: DataFrame with columns [open, high, low, close, volume], indexed by date
        cfg: CBEConfig

    Returns:
        {
            "score": float in [0, 10],
            "atr_ratio": float,
            "bb_width_percentile": float,
            "consecutive_contraction_days": int,
            "hv_20_percentile": float,
        }
    """
    if len(ohlc) < cfg.vc_lookback_long + 10:
        return {"score": 0.0, "atr_ratio": None, "bb_width_percentile": None,
                "consecutive_contraction_days": 0, "hv_20_percentile": None}

    # ATR calculation (Wilder's smoothing)
    tr = pd.concat([
        ohlc["high"] - ohlc["low"],
        (ohlc["high"] - ohlc["close"].shift(1)).abs(),
        (ohlc["low"] - ohlc["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_short = tr.rolling(cfg.vc_lookback_short).mean()
    atr_long = tr.rolling(cfg.vc_lookback_long).mean()

    atr_ratio = atr_short.iloc[-1] / atr_long.iloc[-1] if atr_long.iloc[-1] > 0 else 1.0

    # Percentile of current ATR ratio over trailing 252 days
    atr_ratio_series = (atr_short / atr_long).dropna()
    if len(atr_ratio_series) > 60:
        ratio_pct = (atr_ratio_series.iloc[-1] >= atr_ratio_series.iloc[-252:]).mean() * 100
    else:
        ratio_pct = 50.0

    # Bollinger Band Width
    mid = ohlc["close"].rolling(cfg.vc_bb_period).mean()
    std = ohlc["close"].rolling(cfg.vc_bb_period).std()
    bb_width = (4 * std) / mid  # 2-sigma * 2
    bb_width_pct = (bb_width.iloc[-1] >= bb_width.dropna().iloc[-252:]).mean() * 100 if len(bb_width.dropna()) > 60 else 50.0

    # Consecutive contracting days (each day's range < previous)
    daily_range = ohlc["high"] - ohlc["low"]
    contracting = (daily_range < daily_range.shift(1)).astype(int)
    # Count consecutive 1s ending at last bar
    consec = 0
    for v in reversed(contracting.iloc[-15:].tolist()):
        if v == 1:
            consec += 1
        else:
            break

    # Historical vol over last 20 days, percentile vs 252-day
    log_ret = np.log(ohlc["close"] / ohlc["close"].shift(1))
    hv_20 = log_ret.rolling(20).std() * np.sqrt(252)
    hv_20_pct = (hv_20.iloc[-1] >= hv_20.dropna().iloc[-252:]).mean() * 100 if len(hv_20.dropna()) > 60 else 50.0

    # SCORING — each sub-feature contributes
    # ATR ratio low = compression (high score). Inverse mapping.
    score_atr = max(0, (cfg.vc_percentile_threshold - ratio_pct) / cfg.vc_percentile_threshold * 4.0)
    # BB width percentile low = compression
    score_bb = max(0, (cfg.vc_percentile_threshold - bb_width_pct) / cfg.vc_percentile_threshold * 3.0)
    # Consecutive contraction
    score_consec = min(2.0, consec / cfg.vc_consecutive_days * 2.0) if consec >= 2 else 0
    # HV percentile low
    score_hv = max(0, (cfg.vc_percentile_threshold - hv_20_pct) / cfg.vc_percentile_threshold * 1.0)

    total = min(10.0, score_atr + score_bb + score_consec + score_hv)

    return {
        "score": round(total, 2),
        "atr_ratio": round(float(atr_ratio), 4),
        "atr_ratio_percentile": round(ratio_pct, 1),
        "bb_width_percentile": round(bb_width_pct, 1),
        "consecutive_contraction_days": int(consec),
        "hv_20_percentile": round(hv_20_pct, 1),
        "sub_scores": {"atr": round(score_atr, 2), "bb": round(score_bb, 2),
                       "consec": round(score_consec, 2), "hv": round(score_hv, 2)},
    }


# ============================================================
# F2: OPTION MARKET POSITIONING
# ============================================================

def feature_option_positioning(
    options_chain: pd.DataFrame,
    iv_history: pd.Series,
    pcr_history: pd.Series,
    cfg: CBEConfig
) -> dict:
    """
    Score 0-10 based on options market structure.

    Three sub-signals:
        - IV Rank suppressed: cheap options to buy if expansion coming
        - PCR deviation: positioning skew
        - OI buildup at OTM strikes: smart money positioning

    Args:
        options_chain: DataFrame for nearest expiry with columns:
            [strike, type ('CE'/'PE'), oi, oi_change_1d, volume, iv, delta]
        iv_history: Series of ATM IV indexed by date, last 252+ days
        pcr_history: Series of put/call OI ratio, last 252+ days

    Returns:
        Dict with score and sub-features.
    """
    if options_chain is None or len(options_chain) < 10:
        return {"score": 0.0, "iv_rank": None, "pcr_z": None, "oi_buildup_otm": None}

    if iv_history is None or len(iv_history) < 60:
        iv_rank = 50.0
    else:
        current_iv = iv_history.iloc[-1]
        iv_rank = ((current_iv > iv_history.iloc[-cfg.omp_iv_rank_lookback:]).mean()) * 100

    # PCR z-score vs trailing window
    if pcr_history is None or len(pcr_history) < cfg.omp_pcr_baseline_window:
        pcr_z = 0.0
    else:
        pcr_recent = pcr_history.iloc[-1]
        pcr_baseline = pcr_history.iloc[-cfg.omp_pcr_baseline_window:-1]
        pcr_z = (pcr_recent - pcr_baseline.mean()) / pcr_baseline.std() if pcr_baseline.std() > 0 else 0

    # OI buildup at OTM strikes
    # Assumes options_chain has spot column or we infer from ATM
    atm_strike = options_chain.iloc[(options_chain["delta"].abs() - 0.5).abs().argsort()[:1]]["strike"].values[0] \
        if "delta" in options_chain.columns else options_chain["strike"].median()

    otm_calls = options_chain[(options_chain["type"] == "CE") & (options_chain["strike"] > atm_strike * 1.02)]
    otm_puts = options_chain[(options_chain["type"] == "PE") & (options_chain["strike"] < atm_strike * 0.98)]

    if "oi_change_1d" in options_chain.columns:
        # Relative OI change as fraction of total OI
        call_buildup = otm_calls["oi_change_1d"].sum() / max(otm_calls["oi"].sum(), 1)
        put_buildup = otm_puts["oi_change_1d"].sum() / max(otm_puts["oi"].sum(), 1)
        oi_buildup_max = max(abs(call_buildup), abs(put_buildup))
        oi_directional_bias = "bullish" if call_buildup > put_buildup else "bearish"
    else:
        oi_buildup_max = 0
        oi_directional_bias = "neutral"

    # SCORING
    # Low IV rank = cheap options to buy = high score (we want compressed IV)
    score_iv = max(0, (cfg.omp_iv_rank_low - iv_rank) / cfg.omp_iv_rank_low * 4.0)
    # Strong PCR deviation = positioning skew = signal
    score_pcr = min(3.0, abs(pcr_z) / 2.0 * 3.0)
    # Significant OI buildup
    score_oi = min(3.0, oi_buildup_max / cfg.omp_oi_change_threshold * 3.0)

    total = min(10.0, score_iv + score_pcr + score_oi)

    return {
        "score": round(total, 2),
        "iv_rank": round(float(iv_rank), 1),
        "pcr_z": round(float(pcr_z), 2),
        "oi_buildup_otm": round(float(oi_buildup_max), 3),
        "oi_directional_bias": oi_directional_bias,
        "sub_scores": {"iv": round(score_iv, 2), "pcr": round(score_pcr, 2),
                       "oi": round(score_oi, 2)},
    }


# ============================================================
# F3: CROSS-SECTIONAL MOMENTUM DIVERGENCE
# ============================================================

def feature_cross_sectional_divergence(
    stock_returns: pd.Series,
    sector_returns: pd.Series,
    cfg: CBEConfig
) -> dict:
    """
    Score 0-10 based on stock's divergence from its sector.

    The premise: when a stock consolidates while its sector trends, it's
    spring-loaded. The eventual breakout direction usually catches up to
    the sector.

    Args:
        stock_returns: Daily log returns of the stock, last 60+ days
        sector_returns: Daily log returns of the parent sector, same dates

    Returns:
        Dict with score and divergence sign (positive = sector up, stock flat = bullish setup)
    """
    if len(stock_returns) < cfg.csmd_window or len(sector_returns) < cfg.csmd_window:
        return {"score": 0.0, "divergence": None, "sector_trend": None}

    common = stock_returns.index.intersection(sector_returns.index)
    if len(common) < cfg.csmd_window:
        return {"score": 0.0, "divergence": None, "sector_trend": None}

    s_ret = stock_returns.loc[common].iloc[-cfg.csmd_window:]
    sect_ret = sector_returns.loc[common].iloc[-cfg.csmd_window:]

    stock_cum = s_ret.sum()
    sector_cum = sect_ret.sum()

    # Beta-normalized divergence: if stock has historical beta = 1.2, its expected
    # return is 1.2x sector. Compare actual to expected.
    # Beta from a longer history
    if len(stock_returns) >= 60 and len(sector_returns) >= 60:
        s60 = stock_returns.loc[common].iloc[-60:]
        sect60 = sector_returns.loc[common].iloc[-60:]
        cov = np.cov(s60, sect60)[0, 1]
        beta = cov / sect60.var() if sect60.var() > 0 else 1.0
    else:
        beta = 1.0

    expected_stock_return = beta * sector_cum
    divergence = stock_cum - expected_stock_return

    # If sector is trending and stock is lagging, that's the setup
    sector_strength = abs(sector_cum)

    # Score: divergence matters more when sector is actually trending
    if sector_strength < 0.01:
        # Sector going nowhere — divergence is noise
        score = 0.0
    else:
        # Magnitude of divergence relative to threshold, weighted by sector trend strength
        score = min(10.0, (abs(divergence) / cfg.csmd_min_divergence) * 5.0 *
                    min(1.0, sector_strength / 0.03))

    return {
        "score": round(float(score), 2),
        "stock_return_20d": round(float(stock_cum), 4),
        "sector_return_20d": round(float(sector_cum), 4),
        "beta_60d": round(float(beta), 2),
        "divergence": round(float(divergence), 4),
        "directional_bias": "bullish" if (sector_cum > 0 and divergence < 0) else
                            ("bearish" if (sector_cum < 0 and divergence > 0) else "neutral"),
    }


# ============================================================
# F4: CATALYST PROXIMITY
# ============================================================

def feature_catalyst_proximity(
    events: list,  # list of dicts with {date, type, description}
    today: pd.Timestamp,
    cfg: CBEConfig
) -> dict:
    """
    Score 0-10 based on upcoming catalysts within the trading window.

    Args:
        events: List of {date: Timestamp, type: str, description: str}
                where type is one of cfg.cp_event_type_weights keys
        today: Current date

    Returns:
        Dict with score and details of nearest catalyst.
    """
    if not events:
        return {"score": 0.0, "nearest_event": None, "days_to_event": None}

    future_events = [e for e in events if pd.Timestamp(e["date"]) > today and
                     (pd.Timestamp(e["date"]) - today).days <= cfg.cp_max_days_to_event]

    if not future_events:
        return {"score": 0.0, "nearest_event": None, "days_to_event": None}

    future_events.sort(key=lambda e: pd.Timestamp(e["date"]))
    nearest = future_events[0]
    days = (pd.Timestamp(nearest["date"]) - today).days

    event_weight = cfg.cp_event_type_weights.get(nearest["type"], 0.5)
    # Closer = higher score, but not too close (T-1 to T-3 is optimal entry window)
    time_factor = max(0, (cfg.cp_max_days_to_event - days) / cfg.cp_max_days_to_event)

    score = min(10.0, event_weight * time_factor * 10.0)

    return {
        "score": round(score, 2),
        "nearest_event": nearest,
        "days_to_event": days,
        "event_weight": event_weight,
        "all_upcoming": future_events,
    }


# ============================================================
# F5: MICROSTRUCTURE PRESSURE
# ============================================================

def feature_microstructure_pressure(
    ohlc: pd.DataFrame,
    spread_series: Optional[pd.Series],
    block_deals: Optional[pd.DataFrame],
    fii_dii_flow: Optional[pd.Series],
    cfg: CBEConfig
) -> dict:
    """
    Score 0-10 based on microstructure signs of accumulation/distribution.

    Args:
        ohlc: standard OHLCV
        spread_series: daily avg bid-ask spread as fraction of mid (optional)
        block_deals: DataFrame with [date, value, side ('buy'/'sell')] (optional)
        fii_dii_flow: Daily FII + DII net flow for the stock or sector (optional)

    Returns:
        Dict with score.
    """
    score = 0.0
    details = {}

    # Spread tightening
    if spread_series is not None and len(spread_series) >= cfg.mp_spread_window:
        recent = spread_series.iloc[-5:].mean()
        baseline = spread_series.iloc[-cfg.mp_spread_window:-5].mean()
        if baseline > 0 and recent < baseline:
            spread_tightening = (baseline - recent) / baseline
            score_spread = min(3.0, spread_tightening * 20.0)  # 15% tightening = 3.0
            score += score_spread
            details["spread_tightening_pct"] = round(spread_tightening * 100, 2)
            details["score_spread"] = round(score_spread, 2)

    # Block deal frequency
    if block_deals is not None and len(block_deals) > 0:
        cutoff = ohlc.index[-1] - pd.Timedelta(days=cfg.mp_block_deal_window)
        recent_blocks = block_deals[block_deals["date"] >= cutoff]
        net_value = (recent_blocks[recent_blocks["side"] == "buy"]["value"].sum() -
                     recent_blocks[recent_blocks["side"] == "sell"]["value"].sum())
        # Score by net value as fraction of recent avg daily turnover
        avg_turnover = (ohlc["close"] * ohlc["volume"]).iloc[-20:].mean()
        if avg_turnover > 0:
            block_ratio = net_value / avg_turnover
            score_block = min(3.0, abs(block_ratio) * 3.0)
            score += score_block
            details["block_net_value_ratio"] = round(block_ratio, 3)
            details["score_block"] = round(score_block, 2)
            details["block_directional_bias"] = "bullish" if net_value > 0 else "bearish"

    # FII/DII consistency
    if fii_dii_flow is not None and len(fii_dii_flow) >= cfg.mp_fii_dii_window:
        recent_flow = fii_dii_flow.iloc[-cfg.mp_fii_dii_window:]
        # Consistent direction = score
        same_sign = (recent_flow > 0).all() or (recent_flow < 0).all()
        if same_sign:
            avg_flow = recent_flow.mean()
            std_flow = fii_dii_flow.iloc[-60:].std()
            if std_flow > 0:
                flow_z = avg_flow / std_flow
                score_flow = min(4.0, abs(flow_z) * 2.0)
                score += score_flow
                details["fii_dii_z"] = round(float(flow_z), 2)
                details["score_flow"] = round(score_flow, 2)
                details["flow_directional_bias"] = "bullish" if avg_flow > 0 else "bearish"

    details["score"] = round(min(10.0, score), 2)
    return details


# ============================================================
# F6 (NEW): VOLATILITY CONE — Burghardt-Lane
# ============================================================
# Realized vol over multiple horizons (10/20/40/60-day) compared against
# its own historical %ile distribution. Compression is "real" when ALL
# horizons simultaneously sit in the bottom quartile.
#
# This is a more rigorous replacement for F1's ad-hoc ATR ratio + BB-width:
# the cone tells you whether vol is compressed across timescales (true
# coiled spring) vs. just one timescale (noise).
#
# Reference: Burghardt & Lane (1990), "How To Tell If Options Are Cheap"
# in Journal of Portfolio Management.

def feature_volatility_cone(ohlc: pd.DataFrame, cfg: CBEConfig) -> dict:
    """Score 0-10 based on multi-horizon volatility compression.

    For each horizon h in cfg.cone_horizons, compute realized annualized
    vol over the last h days. Then compute the %ile of that current value
    against its trailing distribution (last cone_percentile_history days).

    Score:
        - All horizons below cone_compression_pct (%ile): 10 (deep compression)
        - 3 of 4 below: 7
        - 2 of 4 below: 4
        - 1 of 4 below: 1.5
        - 0 below: 0
    """
    out = {"score": 0.0, "horizons": {}, "available": False, "reason": ""}
    if ohlc is None or len(ohlc) < cfg.cone_min_history_days:
        out["reason"] = f"insufficient history ({len(ohlc) if ohlc is not None else 0} < {cfg.cone_min_history_days})"
        return out

    log_ret = np.log(ohlc["close"] / ohlc["close"].shift(1)).dropna()
    if len(log_ret) < cfg.cone_min_history_days - 2:
        out["reason"] = "insufficient returns"
        return out

    horizons_below = 0
    horizon_count = 0
    for h in cfg.cone_horizons:
        if len(log_ret) < h + 30:  # need at least 30 trailing %ile samples
            continue
        rv = log_ret.rolling(h).std() * np.sqrt(252)
        rv = rv.dropna()
        if len(rv) < 30:
            continue
        current = float(rv.iloc[-1])
        history = rv.iloc[-min(cfg.cone_percentile_history, len(rv)):]
        pct = float((history <= current).mean() * 100)
        out["horizons"][f"h{h}_rv"] = round(current, 4)
        out["horizons"][f"h{h}_pct"] = round(pct, 1)
        horizon_count += 1
        if pct <= cfg.cone_compression_pct:
            horizons_below += 1

    if horizon_count == 0:
        out["reason"] = "no horizon could be computed"
        return out

    out["available"] = True
    fraction_below = horizons_below / horizon_count
    # Non-linear scoring: 1.0 (all below) → 10, 0.75 → 7, 0.5 → 4, 0.25 → 1.5
    if fraction_below >= 1.0:
        score = 10.0
    elif fraction_below >= 0.75:
        score = 7.0
    elif fraction_below >= 0.5:
        score = 4.0
    elif fraction_below >= 0.25:
        score = 1.5
    else:
        score = 0.0
    out["score"] = round(score, 2)
    out["horizons_below_threshold"] = horizons_below
    out["horizons_evaluated"] = horizon_count
    out["fraction_below"] = round(fraction_below, 2)
    return out


# ============================================================
# F7 (NEW): IV TERM STRUCTURE
# ============================================================
# When the front-month IV is materially below the back-month IV (in
# contango by >1σ of its history), the market is pricing in low
# near-term vol while still pricing a regular term-structure for later.
# Combined with realized-vol compression, that's a vol-floor / squeeze
# setup: implied is suppressed, history shows it shouldn't be.
#
# The reverse — backwardation (front > back) — usually signals event
# stress and is a *bad* setup for compression-expansion plays.

def feature_iv_term_structure(
    options_chain: Optional[pd.DataFrame],
    iv_history: Optional[pd.Series],
    cfg: CBEConfig,
) -> dict:
    """Score 0-10 based on front-vs-back IV slope vs its history.

    Args:
        options_chain: Must have at least two expiries with IV.
        iv_history: Front-month ATM IV history (for z-score of the spread).
    """
    out = {"score": 0.0, "available": False, "reason": "", "term_slope": None, "term_z": None}
    if options_chain is None or len(options_chain) < 4:
        out["reason"] = "no options chain"
        return out
    chain = options_chain.copy()
    if "expiry" not in chain.columns or "iv" not in chain.columns:
        out["reason"] = "chain missing expiry/iv"
        return out
    chain = chain.dropna(subset=["iv", "expiry"])
    chain = chain[chain["iv"] > 0.01]
    if chain.empty:
        out["reason"] = "no valid iv rows"
        return out

    chain["expiry_ts"] = pd.to_datetime(chain["expiry"], errors="coerce")
    chain = chain.dropna(subset=["expiry_ts"])
    expiries = sorted(chain["expiry_ts"].unique())
    if len(expiries) < 2:
        out["reason"] = f"only {len(expiries)} expiry"
        return out

    # ATM-bucket IV per expiry: median of CE+PE within 5% moneyness if
    # we have a spot; else median of all
    spot = None
    if "strike" in chain.columns and len(chain):
        spot = float(chain["strike"].median())

    def atm_iv(expiry_ts):
        rows = chain[chain["expiry_ts"] == expiry_ts]
        if spot:
            rows = rows[rows["strike"].between(spot * 0.95, spot * 1.05)]
        if rows.empty:
            return None
        return float(rows["iv"].median())

    front_iv = atm_iv(expiries[0])
    back_iv = atm_iv(expiries[1])
    if front_iv is None or back_iv is None or front_iv <= 0 or back_iv <= 0:
        out["reason"] = "front/back atm iv unavailable"
        return out

    # Slope as relative spread; positive = contango (back > front)
    slope = (back_iv - front_iv) / max(front_iv, 1e-6)
    out["term_slope"] = round(slope, 4)
    out["front_iv"] = round(front_iv, 4)
    out["back_iv"] = round(back_iv, 4)

    # Z-score the current slope against historical slope distribution
    # (using iv_history as proxy if no historical slope series is wired).
    # Without true historical slope, we use an absolute heuristic:
    # - slope > +0.10 = strong contango → score ~7
    # - slope > +0.05 = mild contango → score ~4
    # - slope between [-0.02, +0.05] = flat → score ~1
    # - slope < -0.02 = backwardation (event stress) → score 0
    out["available"] = True
    if slope >= 0.10:
        score = 7.0 + min(3.0, (slope - 0.10) * 30.0)
    elif slope >= 0.05:
        score = 4.0 + (slope - 0.05) * 60.0
    elif slope >= -0.02:
        score = max(0.0, 1.0 + slope * 20.0)
    else:
        score = 0.0
    out["score"] = round(min(10.0, score), 2)

    # If we have IV history of >20 days, also compute a rough z-score on
    # front IV itself (low front-IV vs history reinforces the signal).
    if iv_history is not None and len(iv_history) >= 20:
        recent_iv = float(iv_history.iloc[-1]) if hasattr(iv_history, "iloc") else None
        if recent_iv:
            mu = float(iv_history.iloc[-cfg.its_lookback_days:].mean())
            sd = float(iv_history.iloc[-cfg.its_lookback_days:].std())
            if sd > 0:
                z = (recent_iv - mu) / sd
                out["front_iv_z"] = round(z, 2)
                # If front IV is in bottom 25% of its history AND we see
                # contango, boost the score by up to +2
                if z < -0.6 and slope > 0.03:
                    out["score"] = round(min(10.0, out["score"] + 2.0), 2)
    return out


# ============================================================
# COMPOSITE SCORING
# ============================================================

@dataclass
class CBEScore:
    """Final CBE score for one (instrument, date) pair."""
    instrument: str
    date: pd.Timestamp
    composite_score: float
    directional_bias: str  # "bullish", "bearish", "neutral"
    bias_conviction: float  # 0..1, agreement across features
    f1_vc: dict
    f2_omp: dict
    f3_csmd: dict
    f4_cp: dict
    f5_mp: dict
    f6_cone: dict
    f7_its: dict
    active_features: list  # names of features that contributed (had data)
    effective_weight_total: float  # sum of weights that contributed before renorm
    composite_quality: str = "ok"  # ok | partial | low_confidence_single_feature | no_data

    def to_dict(self):
        return asdict(self)


def compute_cbe_score(
    instrument: str,
    date: pd.Timestamp,
    ohlc: pd.DataFrame,
    options_chain: Optional[pd.DataFrame] = None,
    iv_history: Optional[pd.Series] = None,
    pcr_history: Optional[pd.Series] = None,
    sector_returns: Optional[pd.Series] = None,
    events: Optional[list] = None,
    spread_series: Optional[pd.Series] = None,
    block_deals: Optional[pd.DataFrame] = None,
    fii_dii_flow: Optional[pd.Series] = None,
    cfg: Optional[CBEConfig] = None,
) -> CBEScore:
    """Compute the full CBE score for one instrument at end-of-day."""
    cfg = cfg or CBEConfig()

    # Stock returns
    stock_returns = np.log(ohlc["close"] / ohlc["close"].shift(1)).dropna()

    # Compute each feature
    f1 = feature_volatility_compression(ohlc, cfg)
    f2 = feature_option_positioning(options_chain, iv_history, pcr_history, cfg)
    f3 = feature_cross_sectional_divergence(stock_returns, sector_returns, cfg) \
        if sector_returns is not None else {"score": 0.0, "directional_bias": "neutral", "available": False}
    f4 = feature_catalyst_proximity(events or [], date, cfg)
    f5 = feature_microstructure_pressure(ohlc, spread_series, block_deals, fii_dii_flow, cfg)
    f6 = feature_volatility_cone(ohlc, cfg)
    f7 = feature_iv_term_structure(options_chain, iv_history, cfg)

    # Active-feature renormalization. A feature is "active" if it had
    # enough data to score. Dead-feed features get excluded and the
    # surviving weights are re-scaled to sum to 1. This prevents the
    # composite from being permanently capped just because, say, the
    # events feed is unwired.
    def _is_active(feat: dict, *, allow_explicit_zero: bool = False) -> bool:
        if not isinstance(feat, dict):
            return False
        # Honour explicit `available` flag from new features (F6, F7)
        if "available" in feat:
            return bool(feat["available"])
        score = feat.get("score")
        if score is None:
            return False
        # Heuristic: a feature that returned 0.0 with no sub-data is dark.
        # Keep it if it has explicit sub-fields populated.
        if score == 0.0 and not allow_explicit_zero:
            has_data = any(
                feat.get(k) is not None
                for k in ("iv_rank", "atr_ratio", "divergence", "nearest_event", "spread_tightening_pct")
            )
            return has_data
        return True

    active_map = {
        "f1_vc":   (_is_active(f1), cfg.w_vc,  f1.get("score", 0.0)),
        "f2_omp":  (_is_active(f2), cfg.w_omp, f2.get("score", 0.0)),
        "f3_csmd": (_is_active(f3), cfg.w_csmd, f3.get("score", 0.0)),
        "f4_cp":   (_is_active(f4, allow_explicit_zero=False), cfg.w_cp, f4.get("score", 0.0)),
        "f5_mp":   (_is_active(f5, allow_explicit_zero=False), cfg.w_mp, f5.get("score", 0.0)),
        "f6_cone": (_is_active(f6), cfg.w_cone, f6.get("score", 0.0)),
        "f7_its":  (_is_active(f7), cfg.w_its, f7.get("score", 0.0)),
    }
    active_features = [k for k, (live, _, _) in active_map.items() if live]
    effective_weight_total = sum(w for (live, w, _) in active_map.values() if live)

    # GATE: require at least 2 active features for a meaningful composite.
    # A single-feature composite is just that feature's score (no diversification),
    # which the renormalization makes deceptively reach 10/10 and flood the
    # watchlist. Until enough features have data, treat single-feature stocks
    # as low-confidence and cap their composite at the un-renormalized value.
    MIN_ACTIVE_FEATURES = 2
    if len(active_features) < MIN_ACTIVE_FEATURES:
        # Use raw weighted score WITHOUT renormalization. A single feature
        # contributing weight 0.12 (e.g. f3_csmd) caps the composite at
        # 0.12 * 10 = 1.2 — appropriately small for low confidence.
        composite = sum(w * s for (live, w, s) in active_map.values() if live)
        composite_quality = "low_confidence_single_feature"
    elif effective_weight_total > 0:
        composite = sum(w * s for (live, w, s) in active_map.values() if live) / effective_weight_total
        composite_quality = "ok" if len(active_features) >= 3 else "partial"
    else:
        composite = 0.0
        composite_quality = "no_data"

    # Directional bias: voting across features that have a direction
    bias_votes = []
    if f2.get("oi_directional_bias") and f2["oi_directional_bias"] != "neutral":
        bias_votes.append((f2["oi_directional_bias"], f2["score"]))
    if f3.get("directional_bias") and f3["directional_bias"] != "neutral":
        bias_votes.append((f3["directional_bias"], f3["score"]))
    if f5.get("block_directional_bias"):
        bias_votes.append((f5["block_directional_bias"], f5.get("score_block", 0)))
    if f5.get("flow_directional_bias"):
        bias_votes.append((f5["flow_directional_bias"], f5.get("score_flow", 0)))

    if not bias_votes:
        bias = "neutral"
        bias_conviction = 0.0
    else:
        bullish_weight = sum(w for d, w in bias_votes if d == "bullish")
        bearish_weight = sum(w for d, w in bias_votes if d == "bearish")
        total = bullish_weight + bearish_weight
        if total == 0:
            bias = "neutral"
            bias_conviction = 0.0
        else:
            if bullish_weight > bearish_weight:
                bias = "bullish"
                bias_conviction = bullish_weight / total
            elif bearish_weight > bullish_weight:
                bias = "bearish"
                bias_conviction = bearish_weight / total
            else:
                bias = "neutral"
                bias_conviction = 0.5

    return CBEScore(
        instrument=instrument,
        date=date,
        composite_score=round(composite, 2),
        directional_bias=bias,
        bias_conviction=round(bias_conviction, 2),
        f1_vc=f1, f2_omp=f2, f3_csmd=f3, f4_cp=f4, f5_mp=f5,
        f6_cone=f6, f7_its=f7,
        active_features=active_features,
        effective_weight_total=round(effective_weight_total, 4),
        composite_quality=composite_quality,
    )


# ============================================================
# UNIVERSE SCAN
# ============================================================

def scan_universe(
    instruments: list,                       # list of instrument symbols
    data_provider,                           # object with .get_ohlc(symbol, lookback), .get_options(symbol), etc.
    date: pd.Timestamp,
    cfg: Optional[CBEConfig] = None,
) -> pd.DataFrame:
    """Scan the full universe and return ranked DataFrame.

    `data_provider` is the abstraction layer between this scanner and your
    actual data source. See data_provider.py for interface.
    """
    cfg = cfg or CBEConfig()
    results = []

    for symbol in instruments:
        try:
            ohlc = data_provider.get_ohlc(symbol, lookback_days=300)
            if ohlc is None or len(ohlc) < getattr(cfg, "min_ohlc_rows", 60):
                continue

            score = compute_cbe_score(
                instrument=symbol, date=date, ohlc=ohlc,
                options_chain=data_provider.get_options_chain(symbol),
                iv_history=data_provider.get_iv_history(symbol),
                pcr_history=data_provider.get_pcr_history(symbol),
                sector_returns=data_provider.get_sector_returns(symbol),
                events=data_provider.get_events(symbol, lookahead_days=cfg.cp_max_days_to_event),
                spread_series=data_provider.get_spread_history(symbol),
                block_deals=data_provider.get_block_deals(symbol),
                fii_dii_flow=data_provider.get_fii_dii_flow(symbol),
                cfg=cfg,
            )
            latest_close = None
            try:
                if ohlc is not None and "close" in ohlc.columns and not ohlc.empty:
                    latest_close = float(ohlc["close"].iloc[-1])
            except Exception:
                latest_close = None
            results.append({
                "instrument": symbol,
                "composite_score": score.composite_score,
                "directional_bias": score.directional_bias,
                "bias_conviction": score.bias_conviction,
                "f1_vc_score": score.f1_vc["score"],
                "f2_omp_score": score.f2_omp["score"],
                "f3_csmd_score": score.f3_csmd["score"],
                "f4_cp_score": score.f4_cp["score"],
                "f5_mp_score": score.f5_mp["score"],
                "f6_cone_score": score.f6_cone["score"],
                "f7_its_score": score.f7_its["score"],
                "active_features": score.active_features,
                "effective_weight_total": score.effective_weight_total,
                # Latest spot close — required by the paper book to open
                # positions at a realistic entry price and to mark them to
                # market on subsequent scans. Without this, the book has no
                # price discovery path.
                "latest_close": latest_close,
                "details": score.to_dict(),
            })
        except Exception as e:
            print(f"  ERROR computing CBE for {symbol}: {e}")

    df = pd.DataFrame(results)
    if len(df) > 0:
        df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    return df


def generate_watchlist(scan_df: pd.DataFrame, cfg: Optional[CBEConfig] = None) -> pd.DataFrame:
    """Filter scan results into the actionable watchlist for tomorrow."""
    cfg = cfg or CBEConfig()
    if scan_df.empty or "composite_score" not in scan_df.columns:
        return pd.DataFrame()
    wl = scan_df[scan_df["composite_score"] >= cfg.watchlist_min_score].copy()
    wl = wl.head(cfg.watchlist_max_size)
    return wl
