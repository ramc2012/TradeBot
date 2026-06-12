"""Family B2 — REAL order-flow features (spec §16 tick/depth), fed by a live snapshot.

Family B (order_flow.py) infers flow from OHLCV — a proxy. This family carries the genuine
tick/depth microstructure the spec asks for (book imbalance, microprice, sweeps, absorption,
trade intensity, toxicity). We do NOT have this historically, so in offline OHLCV builds every
feature is null (schema stays stable). LIVE, Auction Intelligence's `OrderFlowSnapshot` provides
it — the `of_snapshot` dict below maps 1:1 onto AI's fields, so wiring the estimator into AI's
OF lane (see docs) populates these in real time and the model learns on real flow.

All outputs are already instrument-independent (imbalances/ratios ∈[-1,1], bps, bounded scores)
— §2-compliant by construction; we only clip to be safe.
"""

from __future__ import annotations

from datetime import datetime

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.timeutil import ensure_ist

# sniper feature name → key in AI's OrderFlowSnapshot (schemas.OrderFlowSnapshot)
_OF_MAP = {
    "u_of_top_imbalance": "top_imbalance",
    "u_of_depth_imbalance": "depth_imbalance",
    "u_of_order_flow_imbalance": "order_flow_imbalance",
    "u_of_trade_imbalance": "trade_imbalance",
    "u_of_book_pressure": "book_pressure",
    "u_of_queue_pressure": "queue_pressure",
    "u_of_microprice_offset_bps": "micro_price_offset_bps",
    "u_of_vwap_drift": "vwap_drift",
    "u_of_volatility_burst": "volatility_burst",
    "u_of_adverse_selection": "adverse_selection_risk",
    "u_of_toxicity_score": "toxicity_score",
    "u_of_quote_repricing_rate": "quote_repricing_rate",
}
# derived / clipped specially
_OF_DERIVED = (
    "u_of_aggressive_buy_ratio",   # aggr_buy / (aggr_buy + aggr_sell) ∈ [0,1]
    "u_of_trade_intensity_log",    # log1p(trades/min), bounded
)
OF_LIVE_FEATURE_NAMES = tuple(_OF_MAP.keys()) + _OF_DERIVED


def _clip(x, lo=-1.0, hi=1.0):
    try:
        return float(min(hi, max(lo, float(x))))
    except (TypeError, ValueError):
        return None


def build_order_flow_live_features(
    decision_time: datetime,
    *,
    of_snapshot: dict | None = None,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    """Map a live OrderFlowSnapshot (dict) to §2-normalized features. Null when absent (offline)."""
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    if not of_snapshot:
        for n in OF_LIVE_FEATURE_NAMES:
            snapshot.add(Feature(n, None, decision_time, "of"))
        return snapshot

    # passthrough fields — already bounded/instrument-independent, clip wider where bps
    for name, key in _OF_MAP.items():
        v = of_snapshot.get(key)
        hi = 100.0 if name == "u_of_microprice_offset_bps" else 1.0
        snapshot.add(Feature(name, _clip(v, -hi, hi), decision_time, "of"))

    ab = of_snapshot.get("aggressive_buy_volume")
    asl = of_snapshot.get("aggressive_sell_volume")
    ratio = None
    if ab is not None and asl is not None and (ab + asl) > 0:
        ratio = float(ab / (ab + asl))
    snapshot.add(Feature("u_of_aggressive_buy_ratio", ratio, decision_time, "of"))

    ti = of_snapshot.get("trade_intensity_per_minute")
    til = None
    if ti is not None and ti >= 0:
        import math
        til = float(min(5.0, math.log1p(ti)))
    snapshot.add(Feature("u_of_trade_intensity_log", til, decision_time, "of"))
    return snapshot
