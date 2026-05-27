"""Order-flow primitives for bar data.

Indian retail brokers don't push public trade prints to subscribers, so
"true" Lee-Ready CVD (each trade tagged buy/sell by aggressor) isn't
available. These functions approximate the same intuitions from OHLCV
bars + L1 snapshots, which is the industry-standard fallback.

All functions are pure: they take primitive lists and return primitive
lists/dicts. No I/O, no broker calls, no DB access.

Conventions
-----------
* `candles` is `list[dict]` with at least `open`, `high`, `low`, `close`,
  `volume` keys. Optional `time` (str ISO or datetime).
* Functions never mutate their input.
* Numeric outputs use `float`; "unknown" cells are `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


# ─── Helpers ────────────────────────────────────────────────────────────────

def _f(value: Any, default: float = 0.0) -> float:
    """Coerce to float, fall back on bad/missing input."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _candle_close(candle: dict) -> float:
    return _f(candle.get("close"))


def _candle_open(candle: dict) -> float:
    return _f(candle.get("open"))


def _candle_high(candle: dict) -> float:
    return _f(candle.get("high"))


def _candle_low(candle: dict) -> float:
    return _f(candle.get("low"))


def _candle_volume(candle: dict) -> float:
    return _f(candle.get("volume"))


def _typical_price(candle: dict) -> float:
    high = _candle_high(candle)
    low = _candle_low(candle)
    close = _candle_close(candle)
    return (high + low + close) / 3.0 if (high or low or close) else 0.0


# ─── 1. Bar-level Cumulative Volume Delta ───────────────────────────────────

def bar_signed_volume(candles: Sequence[dict]) -> list[float]:
    """Sign each bar's volume by direction using the Lee-Ready tick rule
    adapted to bars: close > prior close → buyer-initiated (+vol),
    close < prior close → seller-initiated (−vol), unchanged → use bar
    body sign (close vs open) as tiebreaker, otherwise 0.

    Returns one signed-volume value per candle. First bar has no prior
    close so we use body sign.
    """
    out: list[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        close = _candle_close(c)
        vol = _candle_volume(c)
        if vol <= 0 or close == 0:
            out.append(0.0)
            prev_close = close if close else prev_close
            continue
        sign = 0
        if prev_close is None:
            body = close - _candle_open(c)
            sign = 1 if body > 0 else -1 if body < 0 else 0
        elif close > prev_close:
            sign = 1
        elif close < prev_close:
            sign = -1
        else:
            body = close - _candle_open(c)
            sign = 1 if body > 0 else -1 if body < 0 else 0
        out.append(sign * vol)
        prev_close = close
    return out


def bar_cvd(candles: Sequence[dict]) -> list[float]:
    """Cumulative Volume Delta from signed bar volumes.

    `bar_cvd(candles)[-1]` is the running cumulative since the first bar.
    Pair with `anchored_cvd` if you want to reset at a session boundary.
    """
    signed = bar_signed_volume(candles)
    running = 0.0
    out: list[float] = []
    for v in signed:
        running += v
        out.append(running)
    return out


def anchored_cvd(candles: Sequence[dict], anchor_index: int = 0) -> list[float]:
    """CVD that resets to zero at `anchor_index`.

    Bars before the anchor are zero so the output length matches input.
    Use the index of the session-open bar (or any chosen anchor).
    """
    if anchor_index < 0 or anchor_index >= len(candles):
        return [0.0] * len(candles)
    signed = bar_signed_volume(candles)
    out = [0.0] * len(candles)
    running = 0.0
    for i in range(anchor_index, len(candles)):
        running += signed[i]
        out[i] = running
    return out


# ─── 2. VWAP + bands ────────────────────────────────────────────────────────

def anchored_vwap(candles: Sequence[dict], anchor_index: int = 0) -> list[Optional[float]]:
    """Anchored VWAP starting from `anchor_index`.

    Bars before the anchor are None. Returns list aligned with `candles`.
    """
    if not candles:
        return []
    out: list[Optional[float]] = [None] * len(candles)
    if anchor_index < 0 or anchor_index >= len(candles):
        return out
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(anchor_index, len(candles)):
        tp = _typical_price(candles[i])
        v = _candle_volume(candles[i])
        cum_pv += tp * v
        cum_v += v
        out[i] = (cum_pv / cum_v) if cum_v > 0 else None
    return out


def vwap_bands(
    candles: Sequence[dict],
    anchor_index: int = 0,
    n_std: float = 1.0,
) -> dict[str, list[Optional[float]]]:
    """Anchored VWAP + ±n_std bands using volume-weighted variance.

    Returns dict with keys `vwap`, `upper`, `lower` — each a list of
    length `len(candles)` aligned with the input. Cells before the anchor
    or with insufficient volume are None.
    """
    vwap = anchored_vwap(candles, anchor_index)
    upper: list[Optional[float]] = [None] * len(candles)
    lower: list[Optional[float]] = [None] * len(candles)
    if anchor_index < 0 or anchor_index >= len(candles):
        return {"vwap": vwap, "upper": upper, "lower": lower}
    cum_v = 0.0
    cum_pv2 = 0.0  # Σ v·(p − μ)² accumulated incrementally is fiddly;
    cum_pv = 0.0  # use Σ v·p and Σ v·p² and derive variance directly.
    cum_v_p2 = 0.0
    for i in range(anchor_index, len(candles)):
        tp = _typical_price(candles[i])
        v = _candle_volume(candles[i])
        cum_v += v
        cum_pv += tp * v
        cum_v_p2 += tp * tp * v
        if cum_v > 0 and vwap[i] is not None:
            mean = vwap[i]
            # Var = E[p²] − (E[p])² (volume-weighted)
            variance = max((cum_v_p2 / cum_v) - mean * mean, 0.0)
            sigma = variance ** 0.5
            upper[i] = mean + n_std * sigma
            lower[i] = mean - n_std * sigma
    return {"vwap": vwap, "upper": upper, "lower": lower}


# ─── 3. CVD divergence detector ─────────────────────────────────────────────

@dataclass
class CVDDivergence:
    kind: str  # 'bullish' | 'bearish' | None
    price_swing: tuple[int, int]  # (start_idx, end_idx)
    cvd_swing: tuple[int, int]
    strength: float  # 0..1 — magnitude of disagreement


def cvd_divergence(
    candles: Sequence[dict],
    cvd: Sequence[float],
    lookback: int = 20,
    min_swing_pct: float = 0.002,
) -> Optional[CVDDivergence]:
    """Detect classic price/CVD divergence over the last `lookback` bars.

    Bullish divergence: price makes a lower low, CVD makes a higher low.
    Bearish divergence: price makes a higher high, CVD makes a lower high.

    `min_swing_pct` is the relative move required to register as a swing
    (otherwise tiny noise looks like swings).

    Returns the most recent qualifying divergence, or None.
    """
    n = len(candles)
    if n < 4 or len(cvd) != n:
        return None
    lookback = max(2, min(lookback, n))
    window = list(range(n - lookback, n))
    closes = [_candle_close(candles[i]) for i in window]
    cvds = [cvd[i] for i in window]

    if not closes or not cvds:
        return None
    price_max_i = max(range(len(closes)), key=lambda i: closes[i])
    price_min_i = min(range(len(closes)), key=lambda i: closes[i])
    cvd_max_i = max(range(len(cvds)), key=lambda i: cvds[i])
    cvd_min_i = min(range(len(cvds)), key=lambda i: cvds[i])

    # Need a clear swing magnitude
    last_close = closes[-1]
    swing_thresh = max(abs(last_close) * min_swing_pct, 1e-9)

    # Bearish: price made a recent higher high while CVD made a lower high
    if (
        price_max_i > price_min_i
        and closes[price_max_i] - closes[price_min_i] > swing_thresh
        and cvd_max_i < price_max_i  # CVD peaked earlier than price
        and cvds[cvd_max_i] > cvds[price_max_i]
    ):
        denom = max(abs(cvds[cvd_max_i] - cvds[cvd_min_i]), 1e-9)
        strength = min(abs(cvds[cvd_max_i] - cvds[price_max_i]) / denom, 1.0)
        return CVDDivergence(
            kind="bearish",
            price_swing=(window[price_min_i], window[price_max_i]),
            cvd_swing=(window[cvd_min_i], window[cvd_max_i]),
            strength=strength,
        )

    # Bullish: price made a recent lower low while CVD made a higher low
    if (
        price_min_i > price_max_i
        and closes[price_max_i] - closes[price_min_i] > swing_thresh
        and cvd_min_i < price_min_i
        and cvds[cvd_min_i] < cvds[price_min_i]
    ):
        denom = max(abs(cvds[cvd_max_i] - cvds[cvd_min_i]), 1e-9)
        strength = min(abs(cvds[price_min_i] - cvds[cvd_min_i]) / denom, 1.0)
        return CVDDivergence(
            kind="bullish",
            price_swing=(window[price_max_i], window[price_min_i]),
            cvd_swing=(window[cvd_max_i], window[cvd_min_i]),
            strength=strength,
        )

    return None


def cvd_agrees_with(signal: str, cvd_window: Sequence[float]) -> bool:
    """Quick consistency check used by entry filters.

    `signal` is "BUY" or "SELL". Returns True if CVD over the window is
    moving in the same direction as the signal (i.e. trend agrees).
    Uses simple first-vs-last comparison — robust to single-bar noise.
    """
    if not cvd_window or len(cvd_window) < 2 or signal not in {"BUY", "SELL"}:
        return False
    delta = cvd_window[-1] - cvd_window[0]
    if signal == "BUY":
        return delta > 0
    return delta < 0


# ─── 4. Volume node density (HVN / LVN) ────────────────────────────────────

def volume_node_density(
    candles: Sequence[dict],
    bins: int = 24,
) -> list[dict[str, float]]:
    """Build a price-binned volume histogram (volume-by-price).

    Returns a list of dicts with `price_low`, `price_high`, `volume`,
    sorted by price ascending. Useful for identifying HVN (high-volume
    nodes — support/resistance) and LVN (low-volume nodes — fast-move
    zones).

    Volume is distributed proportionally over the bar's high-low range
    so a 10-tick bar with 100 contracts contributes 10 ticks × 10
    contracts each, not 100 to a single bin.
    """
    if not candles or bins < 2:
        return []
    highs = [_candle_high(c) for c in candles]
    lows = [_candle_low(c) for c in candles]
    h = max(highs) if highs else 0.0
    l = min(lows) if lows else 0.0
    if h <= l:
        return []
    bin_size = (h - l) / bins
    histogram = [0.0] * bins
    for c in candles:
        hi = _candle_high(c)
        lo = _candle_low(c)
        vol = _candle_volume(c)
        if vol <= 0 or hi <= lo:
            continue
        span = hi - lo
        # Distribute volume across bins this bar touches
        start_bin = max(int((lo - l) / bin_size), 0)
        end_bin = min(int((hi - l) / bin_size), bins - 1)
        if start_bin == end_bin:
            histogram[start_bin] += vol
            continue
        per_bin = vol / max(end_bin - start_bin + 1, 1)
        for b in range(start_bin, end_bin + 1):
            histogram[b] += per_bin
    return [
        {
            "price_low": l + i * bin_size,
            "price_high": l + (i + 1) * bin_size,
            "volume": v,
        }
        for i, v in enumerate(histogram)
    ]


def hvn_lvn(histogram: Sequence[dict[str, float]]) -> dict[str, list[dict[str, float]]]:
    """Classify each bin as HVN (top 25%) / LVN (bottom 25%) / mid."""
    if not histogram:
        return {"hvn": [], "lvn": []}
    # Include zero-vol bins — they ARE legitimate low-volume nodes.
    vols = sorted(float(b.get("volume") or 0) for b in histogram)
    if not vols:
        return {"hvn": [], "lvn": []}
    n = len(vols)
    # Clamp to valid indices in case of rounding at small n.
    hvn_idx = min(int(n * 0.75), n - 1)
    lvn_idx = max(int(n * 0.25) - 1, 0)
    hvn_threshold = vols[hvn_idx]
    lvn_threshold = vols[lvn_idx]
    return {
        "hvn": [b for b in histogram if b.get("volume", 0) >= hvn_threshold],
        "lvn": [b for b in histogram if b.get("volume", 0) <= lvn_threshold],
    }


# ─── 5. L1 depth pressure ───────────────────────────────────────────────────

def l1_depth_pressure(bid_qty: float, ask_qty: float) -> float:
    """Bid-vs-ask size imbalance at the inside book. Range [−1, 1].

    +1 = all size on bid (buy pressure)
    −1 = all size on ask (sell pressure)
    """
    bid = max(_f(bid_qty), 0.0)
    ask = max(_f(ask_qty), 0.0)
    total = bid + ask
    if total <= 0:
        return 0.0
    return (bid - ask) / total


def l1_pressure_series(ticks: Iterable[dict]) -> list[float]:
    """Apply `l1_depth_pressure` to each tick. Ticks should have
    `bid_qty` / `ask_qty` keys (matches brokers.base.Tick fields).
    """
    return [l1_depth_pressure(t.get("bid_qty"), t.get("ask_qty")) for t in ticks]


# ─── 6. Convenience: full snapshot for status payloads ─────────────────────

def orderflow_snapshot(
    candles: Sequence[dict],
    anchor_index: Optional[int] = None,
    *,
    histogram_bins: int = 24,
    divergence_lookback: int = 20,
) -> dict[str, Any]:
    """One-shot snapshot of everything for inclusion in API status payloads.

    Returns a dict with `cvd_latest`, `cvd_anchored_latest`, `vwap_latest`,
    `vwap_upper`, `vwap_lower`, `divergence`, `volume_profile`,
    `hvn_count`, `lvn_count`.

    `anchor_index` defaults to the most-recent date boundary if `time`
    keys are present; else falls back to the first bar.
    """
    if not candles:
        return {
            "cvd_latest": None,
            "cvd_anchored_latest": None,
            "vwap_latest": None,
            "vwap_upper_latest": None,
            "vwap_lower_latest": None,
            "divergence": None,
            "volume_profile": [],
            "hvn_count": 0,
            "lvn_count": 0,
        }

    if anchor_index is None:
        anchor_index = _infer_session_anchor(candles)

    cvd_all = bar_cvd(candles)
    cvd_anc = anchored_cvd(candles, anchor_index)
    bands = vwap_bands(candles, anchor_index)
    div = cvd_divergence(candles, cvd_all, lookback=divergence_lookback)
    histogram = volume_node_density(candles, bins=histogram_bins)
    nodes = hvn_lvn(histogram)
    return {
        "cvd_latest": cvd_all[-1] if cvd_all else None,
        "cvd_anchored_latest": cvd_anc[-1] if cvd_anc else None,
        "vwap_latest": bands["vwap"][-1],
        "vwap_upper_latest": bands["upper"][-1],
        "vwap_lower_latest": bands["lower"][-1],
        "divergence": (
            {
                "kind": div.kind,
                "price_swing": list(div.price_swing),
                "cvd_swing": list(div.cvd_swing),
                "strength": round(div.strength, 3),
            }
            if div is not None
            else None
        ),
        "volume_profile_bins": len(histogram),
        "hvn_count": len(nodes["hvn"]),
        "lvn_count": len(nodes["lvn"]),
        "anchor_index": anchor_index,
    }


def _infer_session_anchor(candles: Sequence[dict]) -> int:
    """Find the index of the first bar of the most-recent session.

    Uses `time` field if present (parses ISO string or datetime). Looks
    for a date boundary going backwards. Falls back to 0 if no time data.
    """
    from datetime import datetime, date

    last_date: Optional[date] = None
    last_idx = 0
    for i in range(len(candles) - 1, -1, -1):
        t = candles[i].get("time")
        d: Optional[date] = None
        if isinstance(t, datetime):
            d = t.date()
        elif isinstance(t, str) and len(t) >= 10:
            try:
                d = datetime.fromisoformat(t.replace("Z", "+00:00")).date()
            except ValueError:
                d = None
        if d is None:
            continue
        if last_date is None:
            last_date = d
            last_idx = i
        elif d == last_date:
            last_idx = i
        else:
            return last_idx
    return last_idx
