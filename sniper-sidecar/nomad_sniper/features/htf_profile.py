"""Family A2 — higher-timeframe profile features (spec v2 §7, §15).

Swing/positional DIRECTION is read from higher-timeframe auction structure, not intraday MP.
Per the spec: 1-week forecasts key on WEEKLY + MONTHLY profiles; 1-month forecasts add
QUARTERLY + YEARLY profiles and value migration. This builds, leak-free at decision time `t`,
for each period kind ∈ {week, month, quarter, year}:

  - prior completed-period volume profile (POC/VAH/VAL/high/low),
  - the DEVELOPING current-period profile using only bars strictly before `t`,
  - distances from current price to those levels in ATR units (signed → directional),
  - value-area location (above/below/inside),
  - POC value-migration slope (developing POC vs prior-period POC, signed → directional),

plus a cross-timeframe **value-stack score**: the net number of higher timeframes whose value
price sits above vs below — a single bounded directional-regime feature (when week, month,
quarter, year all agree, that's a strong trend read).

All outputs are ATR-normalized / categorical / bounded (prefix ``u_htf_``) — contract §2. Raw
levels are computed internally via `build_volume_profile` and never leave this module.
`data_available_at` for a completed-period feature is that period's last bar close (≤ t).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.features.market_profile import build_volume_profile
from nomad_sniper.utils.timeutil import ensure_ist

# Period kinds, longest-context last. 'week'/'month' drive 1w forecasts; 'quarter'/'year' drive 1m.
_KINDS = ("week", "month", "quarter", "year")


def _names() -> tuple[str, ...]:
    out: list[str] = []
    for k in _KINDS:
        out += [f"u_htf_dist_prev_{k}_poc_atr", f"u_htf_dist_prev_{k}_vah_atr",
                f"u_htf_dist_prev_{k}_val_atr", f"u_htf_dist_dev_{k}_poc_atr",
                f"u_htf_{k}_location", f"u_htf_{k}_value_migration_atr"]
    out += ["u_htf_week_month_value_aligned", "u_htf_value_stack_score",
            # §15 directional: alignment / conflict / compression / slopes
            "u_htf_all_tf_bullish", "u_htf_all_tf_bearish", "u_htf_timeframe_conflict",
            "u_htf_week_month_aligned_signed",
            "u_htf_profile_compression", "u_htf_profile_expansion",
            "u_htf_poc_shift_rate", "u_htf_vah_slope", "u_htf_val_slope"]
    return tuple(out)


_HTF_FEATURE_NAMES = _names()
HTF_CATEGORICALS = tuple(f"u_htf_{k}_location" for k in _KINDS)


def _period_keys(idx, kind: str):
    """Period label per timestamp: week 'YYYY-Www', month 'YYYY-MM', quarter 'YYYY-Qn', year 'YYYY'."""
    ts = pd.DatetimeIndex(idx)
    if kind == "week":
        iso = ts.isocalendar()
        return [f"{y}-W{w:02d}" for y, w in zip(iso.year, iso.week)]
    if kind == "month":
        return [f"{t.year}-{t.month:02d}" for t in ts]
    if kind == "quarter":
        return [f"{t.year}-Q{(t.month - 1) // 3 + 1}" for t in ts]
    return [f"{t.year}" for t in ts]


def build_htf_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    atr_ref: float | None,
    *,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    """Family A2 weekly/monthly/quarterly/yearly profile features at `decision_time` (leak-free)."""
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    def _an(points: float | None) -> float | None:
        if points is None or atr_ref is None or atr_ref <= 0:
            return None
        return points / atr_ref

    hist = bars[bars.index <= decision_time]
    if hist.empty or atr_ref is None:
        for n in _HTF_FEATURE_NAMES:
            snapshot.add(Feature(n, None, decision_time, "mp"))
        return snapshot

    price = float(hist.iloc[-1]["close"])
    last_close = ensure_ist(hist.index[-1].to_pydatetime())

    # Build HTF profiles from DAILY-aggregated bars, not raw intraday bars: a weekly/monthly/
    # quarterly/yearly POC is a daily-price-distribution concept, and aggregating first is ~100×
    # fewer bars per profile (essential for minute data over multi-year windows) with effectively
    # identical POC/VAH/VAL. The last daily bar is the developing current day (bars ≤ t).
    daily = hist.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    if daily.empty:
        daily = hist

    out: dict[str, object] = {}
    locations: dict[str, str | None] = {}
    profs: dict[str, tuple] = {}      # kind -> (prev_profile|None, dev_profile)

    for kind in _KINDS:
        keys = _period_keys(daily.index, kind)
        frame = daily.assign(_pk=keys)
        cur_key = keys[-1]
        period_order = list(dict.fromkeys(keys))
        cur_idx = period_order.index(cur_key)

        # Prior COMPLETED period.
        prev = None
        if cur_idx >= 1:
            prev_bars = frame[frame["_pk"] == period_order[cur_idx - 1]].drop(columns="_pk")
            prev = build_volume_profile(prev_bars)
            avail = ensure_ist(prev_bars.index[-1].to_pydatetime())
            out[f"u_htf_dist_prev_{kind}_poc_atr"] = (_an(price - prev.poc), avail)
            out[f"u_htf_dist_prev_{kind}_vah_atr"] = (_an(price - prev.vah), avail)
            out[f"u_htf_dist_prev_{kind}_val_atr"] = (_an(price - prev.val), avail)
            prev_poc = prev.poc
        else:
            for s in ("poc", "vah", "val"):
                out[f"u_htf_dist_prev_{kind}_{s}_atr"] = (None, decision_time)
            prev_poc = None

        # Developing CURRENT period (bars strictly within current period, up to t).
        dev_bars = frame[frame["_pk"] == cur_key].drop(columns="_pk")
        dev = build_volume_profile(dev_bars)
        profs[kind] = (prev, dev)
        out[f"u_htf_dist_dev_{kind}_poc_atr"] = (_an(price - dev.poc), last_close)
        loc = "above" if price > dev.vah else ("below" if price < dev.val else "inside")
        out[f"u_htf_{kind}_location"] = (loc, last_close)
        locations[kind] = loc
        out[f"u_htf_{kind}_value_migration_atr"] = (
            (_an(dev.poc - prev_poc) if prev_poc is not None else None), last_close)

    # Week/month alignment (kept for back-compat).
    wk, mo = locations["week"], locations["month"]
    out["u_htf_week_month_value_aligned"] = (
        int(wk == mo and wk in ("above", "below")), last_close)

    # Cross-timeframe value-stack score ∈ [-1, 1]: net higher-TFs with price above vs below value.
    above = sum(1 for k in _KINDS if locations[k] == "above")
    below = sum(1 for k in _KINDS if locations[k] == "below")
    out["u_htf_value_stack_score"] = ((above - below) / len(_KINDS), last_close)

    # §15 directional: alignment / conflict / compression / slopes
    out["u_htf_all_tf_bullish"] = (float(above == len(_KINDS)), last_close)
    out["u_htf_all_tf_bearish"] = (float(below == len(_KINDS)), last_close)
    out["u_htf_timeframe_conflict"] = (min(above, below) / (len(_KINDS) / 2), last_close)
    out["u_htf_week_month_aligned_signed"] = (
        (1.0 if wk == mo == "above" else (-1.0 if wk == mo == "below" else 0.0)), last_close)

    def _clip(x, lo, hi):
        return None if x is None else float(min(hi, max(lo, x)))

    prev_w, dev_w = profs.get("week", (None, None))
    if dev_w is not None and prev_w is not None:
        pw_prev = max(1e-9, prev_w.vah - prev_w.val)
        pw_dev = max(1e-9, dev_w.vah - dev_w.val)
        ratio = pw_dev / pw_prev
        out["u_htf_profile_compression"] = (_clip(1.0 - ratio, 0, 1), last_close)
        out["u_htf_profile_expansion"] = (_clip(ratio - 1.0, 0, 4), last_close)
        out["u_htf_poc_shift_rate"] = (_clip(abs(dev_w.poc - prev_w.poc) / atr_ref, 0, 10), last_close)
        out["u_htf_vah_slope"] = (_clip((dev_w.vah - prev_w.vah) / atr_ref, -10, 10), last_close)
        out["u_htf_val_slope"] = (_clip((dev_w.val - prev_w.val) / atr_ref, -10, 10), last_close)
    else:
        for n in ("u_htf_profile_compression", "u_htf_profile_expansion", "u_htf_poc_shift_rate",
                  "u_htf_vah_slope", "u_htf_val_slope"):
            out[n] = (None, last_close)

    for name in _HTF_FEATURE_NAMES:
        val, avail = out.get(name, (None, decision_time))
        snapshot.add(Feature(name, val, avail, "mp"))
    return snapshot
