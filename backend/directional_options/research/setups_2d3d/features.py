"""Spot indicator features for the 2-3 day setup study.

CAUSALITY CONTRACT (enforced by test_causality.py):
  Every function here is a CAUSAL FILTER: the value at row i depends only on
  rows 0..i of its input. Implementation rules that guarantee it --
    * only .rolling(...), .ewm(...), .shift(+k) with k > 0, .cumsum(), and
      elementwise arithmetic are used;
    * `center=True` is never passed;
    * no negative shift, no .bfill(), no reverse iteration, no groupby on a
      reversed frame;
    * no column is normalised by a full-sample statistic (no z-score against
      the whole series, no min-max over the whole series).
  The test proves it empirically by recomputing on truncated prefixes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    # Wilder smoothing == ewm(alpha=1/n)
    ru = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = ru / rd.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.where(rd.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return line, signal, line - signal


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14):
    """Wilder ADX / +DI / -DI. Returns (adx, plus_di, minus_di)."""
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)
    tr = true_range(high, low, close)
    atr_n = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean(), pdi, mdi


def donchian_high(high: pd.Series, n: int) -> pd.Series:
    """Highest high of the n bars ENDING AT THE PREVIOUS BAR (shift(1) first)."""
    return high.shift(1).rolling(n, min_periods=n).max()


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    return low.shift(1).rolling(n, min_periods=n).min()


# --------------------------------------------------------------------------
# frame-level builders
# --------------------------------------------------------------------------

def add_intraday_features(df: pd.DataFrame) -> pd.DataFrame:
    """30-minute bar features for ONE underlying, sorted by time ascending."""
    c, h, l = df["close"], df["high"], df["low"]
    out = df.copy()
    out["m_ema20"] = ema(c, 20)
    out["m_ema50"] = ema(c, 50)
    out["m_rsi14"] = rsi(c, 14)
    ml, ms, mh = macd(c)
    out["m_macd"], out["m_macd_sig"], out["m_macd_hist"] = ml, ms, mh
    a, pdi, mdi = adx(h, l, c, 14)
    out["m_adx14"], out["m_pdi"], out["m_mdi"] = a, pdi, mdi
    out["m_atr14"] = atr(h, l, c, 14)
    out["m_dc_hi40"] = donchian_high(h, 40)
    out["m_dc_lo40"] = donchian_low(l, 40)
    # lagged copies for cross detection (value at the PREVIOUS closed bar)
    for col in ("m_ema20", "m_ema50", "m_macd", "m_macd_sig", "m_rsi14", "m_adx14"):
        out[col + "_p"] = out[col].shift(1)
    return out


def add_daily_features(d: pd.DataFrame) -> pd.DataFrame:
    """Daily (session-aggregated) features for ONE underlying, ascending."""
    c, h, l = d["s_close"], d["s_high"], d["s_low"]
    out = d.copy()
    out["d_sma20"] = sma(c, 20)
    out["d_sma50"] = sma(c, 50)
    out["d_rsi14"] = rsi(c, 14)
    dl, ds, dh = macd(c)
    out["d_macd"], out["d_macd_sig"], out["d_macd_hist"] = dl, ds, dh
    da, dpdi, dmdi = adx(h, l, c, 14)
    out["d_adx14"], out["d_pdi"], out["d_mdi"] = da, dpdi, dmdi
    out["d_atr14"] = atr(h, l, c, 14)
    out["d_atr_pct"] = out["d_atr14"] / c
    return out
