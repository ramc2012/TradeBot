"""Day-type scoring (spec §9.2).

Continuous scores (not hard labels) for the developing session shape, from IB and range
extension. Computed only on bars up to the decision time, so leak-free.

  - trend_day_score:   high when range extends well beyond the initial balance one-sidedly.
  - balanced_day_score: high when most of the range stays within the IB (rotation).
  - neutral_day_score:  high when the session extends on BOTH sides of the IB.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nomad_sniper.profiles.profile import MarketProfile


def day_type_scores(profile: MarketProfile) -> dict:
    """Return trend/balance/neutral scores in [0,1] from a developing-session profile."""
    out = {"trend_day_score": 0.0, "balanced_day_score": 0.0, "neutral_day_score": 0.0}
    if profile.ib_high is None or profile.ib_low is None:
        return out
    ib_range = profile.ib_high - profile.ib_low
    total_range = profile.high - profile.low
    if total_range <= 0 or ib_range <= 0:
        return out

    ext_up = profile.range_extension_up
    ext_down = profile.range_extension_down
    ext_total = ext_up + ext_down

    # Fraction of the day's range that lies outside the IB
    outside_frac = float(np.clip(ext_total / total_range, 0.0, 1.0))
    # One-sidedness of the extension
    if ext_total > 0:
        one_sided = abs(ext_up - ext_down) / ext_total
    else:
        one_sided = 0.0

    out["trend_day_score"] = float(np.clip(outside_frac * one_sided, 0.0, 1.0))
    out["neutral_day_score"] = float(np.clip(outside_frac * (1.0 - one_sided), 0.0, 1.0))
    out["balanced_day_score"] = float(np.clip(1.0 - outside_frac, 0.0, 1.0))
    return out
