"""Live integration — run the ExcursionEstimator inside Auction Intelligence's order-flow lane.

Wiring (in nomad-curie auction_intelligence): at each decision tick AI supplies the underlying
minute bars (its live feed), the ATR reference, the resolved ATM option series (optional), and its
live `OrderFlowSnapshot`. This lane builds the FULL sniper feature row — including the real
tick/depth OF family (B2) populated from the live snapshot — runs the estimator, and returns
per-timeframe magnitude / direction / time-to-peak. A `shadow_sink` records every prediction so it
can later be scored against realized outcomes and the model retrained on REAL live order flow
(the historical builds never see tick/depth, so this lane is how the OF features get learned).

The estimator only ESTIMATES; sizing / entry / exit / option-roll stay with AI's agents + governor.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from nomad_sniper.features.pipeline import build_all_features
from nomad_sniper.models.excursion import ExcursionEstimator
from nomad_sniper.utils.timeutil import ensure_ist


class SniperEstimatorLane:
    def __init__(self, model_path: str, *, shadow_sink: Callable[[dict], None] | None = None):
        self.est = ExcursionEstimator.load(model_path)
        self.shadow = shadow_sink

    def predict(
        self,
        *,
        decision_time: datetime,
        bars,                      # underlying IST-indexed minute bars (AI live feed)
        atr_ref: float | None,
        atm_series=None,           # resolved ATM CE/PE for the option family (optional)
        of_snapshot=None,          # AI OrderFlowSnapshot (dataclass or dict) → real OF family B2
        spot_bars=None,
        symbol: str | None = None,
    ) -> dict[str, dict[str, float]]:
        t = ensure_ist(decision_time)
        of = asdict(of_snapshot) if is_dataclass(of_snapshot) else (of_snapshot or None)
        snap = build_all_features(t, bars, atm_series, atr_ref, spot_bars=spot_bars, of_snapshot=of)
        row = snap.to_row(strict=False)
        pred = self.est.predict(pd.DataFrame([row]))
        out: dict[str, dict[str, float]] = {}
        for tf, heads in pred.items():
            out[tf] = {k: float(v[0]) for k, v in heads.items()
                       if hasattr(v, "__len__") and len(v) == 1}
        if self.shadow is not None:
            self.shadow({"decision_time": t.isoformat(), "symbol": symbol,
                         "has_live_of": of is not None,
                         "has_options": bool(atm_series is not None and getattr(atm_series, "available", False)),
                         "features": _jsonable_row(row),  # X for the retrain loop (incl. live u_of_*)
                         "prediction": out})
        return out


def _jsonable_row(row: dict) -> dict:
    """Coerce a feature row to JSON-safe values: finite floats, categoricals as strings, NaN→None.

    Logging the feature vector is what makes each shadow record a (X, y) training example — the live
    order-flow (u_of_*) and option (o_*) values are ephemeral, so a retrain can only ever learn on
    them if they are captured here at prediction time.
    """
    import math
    out: dict = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
            continue
        try:
            f = float(v)
            out[k] = f if math.isfinite(f) else None
        except (TypeError, ValueError):
            out[k] = str(v)  # categorical
    return out
