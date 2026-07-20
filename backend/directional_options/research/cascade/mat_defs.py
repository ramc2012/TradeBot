"""(C-cascade, study 3) MATURITY / SATURATION signal definitions — fixed A PRIORI.

Owner's model (verbatim): "...then sustained large move happens - move
matures/saturates - stock goes to consolidation mode ... We exit similarly as
trade matures."

The question this module exists to answer is NOT "which exit makes the most
money" (that would be a sweep). It is: **is maturity detectable causally and in
time to act?** So every rule below is
  * computed on the DAILY frame from sessions <= s only (causal filter, same
    contract as ../setups_2d3d/features.py, proven by test_maturity_causality),
  * fired on the CLOSE of session s, and
  * ACTIONABLE only at the OPEN of the first 30m bar of session s+1
    (the daily bar of s does not close until 15:30 IST).

Each rule is written from the owner's own vocabulary (ATR expansion then
contraction, ADX peak-and-roll, MACD histogram divergence, distance-from-MA
extension, range compression) with round-number thresholds. NOTHING here is
swept: one threshold per rule, chosen before any statistic was computed, and
the sensitivity grid at the bottom is reported in full with multiplicity
applied rather than used to select.

State machine per episode (entry at session s0, direction `side`):
  * an "armed" precondition must be satisfied at some session in [s0, s] —
    this is what makes each rule a MATURITY rule (something must first have
    matured) rather than just a weak-tape filter;
  * the fire condition is then evaluated at s.

Baselines the rules are measured against:
  fix_3 / fix_5 / fix_10  : fixed-time exits (the prior study's exit family)
  state_off               : "hold to consolidation" — exit when the daily
                            higher-timeframe state that defines a confirmed
                            trend turns off (this IS the owner's
                            "goes to consolidation mode")
  oracle_mfe              : ex-post upper bound = the maximum favourable
                            excursion. Not tradeable; it is the denominator
                            that turns every other number into a CAPTURE
                            fraction.
"""
from __future__ import annotations

import numpy as np

# --- fixed a priori constants --------------------------------------------
HORIZON_SESSIONS = 20        # spot study horizon (regime study: moves run 13-18 sessions)
STOP_ATR = 1.0               # hard protective stop, in entry-session daily ATR
ADX_ARM = 25.0               # Wilder's own "trend present" threshold
ADX_DROP = 4.0               # points off the post-entry ADX peak
ADX_DOWN_BARS = 2            # consecutive down sessions
ATR_ARM_MULT = 1.15          # ATR must first EXPAND 15% over the entry level
ATR_LOOKBACK = 3             # ... then be below its value 3 sessions ago
MACD_FADE_FRAC = 0.5         # histogram back under half its post-entry peak
MACD_DOWN_BARS = 2
EXT_ATR = 3.0                # close 3 ATR beyond SMA20 = extended
COMPRESS_SESSIONS = 3
COMPRESS_ATR = 1.5           # 3-session range < 1.5 ATR = compressed

RULES = ("adx_roll", "atr_contract", "macd_fade", "ma_ext", "range_compress",
         "state_off")
BASELINES = ("fix_3", "fix_5", "fix_10", "oracle_mfe")

# one-parameter sensitivity grid, REPORTED not selected
GRID = {
    "adx_roll": ("ADX_DROP", (2.0, 4.0, 6.0)),
    "atr_contract": ("ATR_ARM_MULT", (1.05, 1.15, 1.25)),
    "macd_fade": ("MACD_FADE_FRAC", (0.3, 0.5, 0.7)),
    "ma_ext": ("EXT_ATR", (2.0, 3.0, 4.0)),
    "range_compress": ("COMPRESS_ATR", (1.0, 1.5, 2.0)),
}


def maturity_fire_session(D: dict, s0: int, side: int, rule: str,
                          horizon: int = HORIZON_SESSIONS,
                          **over) -> int:
    """First session index s in (s0 .. s0+horizon] at which `rule` fires.

    `D` is a per-underlying dict of daily arrays indexed by session index
    position (see mat_run.DailyArrays). Returns -1 if the rule never fires
    inside the horizon.

    ONLY values at positions <= s are ever read. The caller converts the
    returned session into an execution bar at session s+1's open.
    """
    adx_drop = over.get("ADX_DROP", ADX_DROP)
    atr_arm = over.get("ATR_ARM_MULT", ATR_ARM_MULT)
    macd_frac = over.get("MACD_FADE_FRAC", MACD_FADE_FRAC)
    ext_atr = over.get("EXT_ATR", EXT_ATR)
    comp_atr = over.get("COMPRESS_ATR", COMPRESS_ATR)

    n = D["n"]
    i0 = s0                       # positions are session indices rebased by caller
    hi_end = min(n - 1, s0 + horizon)
    if hi_end <= i0:
        return -1

    adx = D["adx"]
    atr = D["atr"]
    hist = D["hist"] * side
    close = D["close"]
    sma20 = D["sma20"]
    high = D["high"]
    low = D["low"]
    state = D["state_long"] if side > 0 else D["state_short"]

    adx_peak = -np.inf
    armed_adx = False
    atr0 = atr[i0] if np.isfinite(atr[i0]) else np.nan
    armed_atr = False
    hist_peak = -np.inf
    armed_macd = False
    armed_state = False

    for s in range(i0, hi_end + 1):
        a = adx[s]
        if np.isfinite(a):
            if a >= ADX_ARM:
                armed_adx = True
            adx_peak = max(adx_peak, a)
        if np.isfinite(atr[s]) and np.isfinite(atr0) and atr0 > 0:
            if atr[s] >= atr_arm * atr0:
                armed_atr = True
        h = hist[s]
        if np.isfinite(h) and h > 0:
            hist_peak = max(hist_peak, h)
            armed_macd = True
        if bool(state[s]):
            armed_state = True

        if s == i0:
            continue            # a rule may not fire on the entry session itself

        if rule == "adx_roll":
            if armed_adx and np.isfinite(a) and a < adx_peak - adx_drop:
                if s - ADX_DOWN_BARS >= 0 and all(
                    np.isfinite(adx[s - k]) and np.isfinite(adx[s - k - 1])
                    and adx[s - k] < adx[s - k - 1] for k in range(ADX_DOWN_BARS)
                ):
                    return s
        elif rule == "atr_contract":
            if armed_atr and s - ATR_LOOKBACK >= 0:
                if np.isfinite(atr[s]) and np.isfinite(atr[s - ATR_LOOKBACK]) \
                        and atr[s] < atr[s - ATR_LOOKBACK]:
                    return s
        elif rule == "macd_fade":
            if armed_macd and hist_peak > 0 and np.isfinite(h) \
                    and h < macd_frac * hist_peak and s - MACD_DOWN_BARS >= 0:
                if all(np.isfinite(hist[s - k]) and np.isfinite(hist[s - k - 1])
                       and hist[s - k] < hist[s - k - 1] for k in range(MACD_DOWN_BARS)):
                    return s
        elif rule == "ma_ext":
            if np.isfinite(close[s]) and np.isfinite(sma20[s]) and np.isfinite(atr[s]) \
                    and atr[s] > 0:
                if side * (close[s] - sma20[s]) / atr[s] >= ext_atr:
                    return s
        elif rule == "range_compress":
            k = COMPRESS_SESSIONS - 1
            if s - k >= 0 and np.isfinite(atr[s]) and atr[s] > 0:
                rng = np.nanmax(high[s - k:s + 1]) - np.nanmin(low[s - k:s + 1])
                if np.isfinite(rng) and rng < comp_atr * atr[s]:
                    return s
        elif rule == "state_off":
            if armed_state and not bool(state[s]):
                return s
        else:
            raise ValueError(rule)
    return -1
