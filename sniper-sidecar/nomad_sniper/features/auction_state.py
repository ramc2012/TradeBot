"""Family A3 — auction-state & profile-shape features (spec v2 §14 auction state + shape).

These are the DIRECTIONAL Market-Profile signals the basic distance features miss. MP direction
is regime-conditional (balance → fade to POC; trend → follow) and read from acceptance vs
rejection at value. This module encodes that, leak-free at decision time `t`, from the
developing session profile + prior-day value + initial balance — all from minute OHLCV:

  acceptance_above/below_value, rejection_from_value : is price ACCEPTING beyond prior value
      (holding, building volume) or REJECTING it (probing then returning)?  → continuation vs reversal
  value_migration_up/down_score                       : which way the developing POC is migrating
  range_extension_up/down_atr                         : extension beyond the initial balance
  open_drive / open_test_drive / open_rejection_reverse : the opening auction's character
  balanced / trend / neutral _day_score               : the day-type regime (gates fade vs follow)
  profile_skew / kurtosis, single_print_density,
  poor_high/low_flag, excess_high/low_score           : auction-completion shape

All outputs are ATR-normalized / bounded / categorical (prefix ``u_``) — contract §2.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.features.market_profile import build_volume_profile
from nomad_sniper.utils.timeutil import ensure_ist, session_start

_AS_NAMES = (
    "u_acceptance_above_value", "u_acceptance_below_value", "u_rejection_from_value",
    "u_value_migration_up_score", "u_value_migration_down_score",
    "u_range_ext_up_atr", "u_range_ext_down_atr",
    "u_open_drive_score", "u_open_test_drive_score", "u_open_rejection_reverse_score",
    "u_balanced_day_score", "u_trend_day_score", "u_neutral_day_score",
    "u_dist_dev_poc_pw", "u_dist_dev_vah_pw", "u_dist_dev_val_pw",
    "u_inside_current_value", "u_above_current_value", "u_below_current_value",
    "u_profile_skew", "u_profile_kurtosis", "u_single_print_density",
    "u_poor_high_flag", "u_poor_low_flag", "u_excess_high_score", "u_excess_low_score",
)


def _binned_profile(bars: pd.DataFrame, tick: float):
    """Volume-at-price histogram → (centers, weights, poc, vah, val)."""
    lo = float(bars["low"].min()); hi = float(bars["high"].max())
    if hi <= lo:
        return None
    typ = ((bars["high"] + bars["low"] + bars["close"]) / 3.0).to_numpy(float)
    vol = bars["volume"].to_numpy(float)
    if vol.sum() <= 0:
        vol = np.ones_like(vol)
    n = max(4, int(np.ceil((hi - lo) / tick)))
    edges = np.linspace(lo, hi, n + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    w, _ = np.histogram(typ, bins=edges, weights=vol)
    if w.sum() <= 0:
        return None
    poc_i = int(w.argmax()); poc = float(centers[poc_i])
    target = 0.7 * w.sum(); cap = w[poc_i]; loi = hii = poc_i
    while cap < target and (loi > 0 or hii < len(w) - 1):
        nl = w[loi - 1] if loi > 0 else -1
        nh = w[hii + 1] if hii < len(w) - 1 else -1
        if nh >= nl:
            hii += 1; cap += w[hii]
        else:
            loi -= 1; cap += w[loi]
    return centers, w, poc, float(centers[hii]), float(centers[loi])


def _tick_size(price: float) -> float:
    # instrument-agnostic: ~1bp of price, so bin count is comparable across instruments
    return max(price * 1e-4, 1e-6)


def build_auction_state_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    atr_ref: float | None,
    *,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    def emit_nulls():
        for n in _AS_NAMES:
            snapshot.add(Feature(n, None, decision_time, "mp"))

    if atr_ref is None or atr_ref <= 0:
        emit_nulls(); return snapshot

    from nomad_sniper.utils.barindex import prior_session_dates, session_frames

    today = decision_time.date()
    _, day_frames = session_frames(bars)
    cur = day_frames.get(today)
    if cur is None:
        emit_nulls(); return snapshot
    dev = cur[cur.index <= decision_time]
    if len(dev) < 3:
        emit_nulls(); return snapshot
    avail = ensure_ist(dev.index[-1].to_pydatetime())
    price = float(dev["close"].iloc[-1])
    open_px = float(dev["open"].iloc[0])

    prior = prior_session_dates(bars, today, 1)
    prev = day_frames.get(prior[-1]) if prior else None
    pv = build_volume_profile(prev) if (prev is not None and not prev.empty) else None

    out: dict[str, float | None] = {n: None for n in _AS_NAMES}

    def _an(x):
        return None if x is None else float(x / atr_ref)

    def _clip(x, lo=-1.0, hi=1.0):
        return float(min(hi, max(lo, x)))

    # ── acceptance / rejection vs PRIOR value (directional) ──
    if pv is not None:
        vol = dev["volume"].to_numpy(float)
        if vol.sum() <= 0:
            vol = np.ones(len(dev))
        typ = ((dev["high"] + dev["low"] + dev["close"]) / 3.0).to_numpy(float)
        tot = vol.sum()
        out["u_acceptance_above_value"] = float(vol[typ > pv.vah].sum() / tot)
        out["u_acceptance_below_value"] = float(vol[typ < pv.val].sum() / tot)
        # rejection: probed beyond value (wick) but little volume accepted there → returned
        probed_up = max(0.0, float(dev["high"].max()) - pv.vah)
        probed_dn = max(0.0, pv.val - float(dev["low"].min()))
        acc_up = out["u_acceptance_above_value"]; acc_dn = out["u_acceptance_below_value"]
        rej = 0.0
        if probed_up > 0 and acc_up < 0.10:
            rej += min(1.0, probed_up / atr_ref)
        if probed_dn > 0 and acc_dn < 0.10:
            rej += min(1.0, probed_dn / atr_ref)
        out["u_rejection_from_value"] = _clip(rej, 0.0, 2.0)
        mig = (build_volume_profile(dev).poc - pv.poc) / atr_ref
        out["u_value_migration_up_score"] = float(max(0.0, mig))
        out["u_value_migration_down_score"] = float(max(0.0, -mig))

    # ── initial balance + range extension ──
    ib_end = session_start(today) + timedelta(minutes=60)
    ib = dev[dev.index <= ib_end]
    if not ib.empty and decision_time >= ib_end:
        ib_hi = float(ib["high"].max()); ib_lo = float(ib["low"].min())
        out["u_range_ext_up_atr"] = float(max(0.0, (float(dev["high"].max()) - ib_hi)) / atr_ref)
        out["u_range_ext_down_atr"] = float(max(0.0, (ib_lo - float(dev["low"].min()))) / atr_ref)
        ext_up = out["u_range_ext_up_atr"]; ext_dn = out["u_range_ext_down_atr"]
        # ── day-type regime (gates fade vs follow) ──
        dev_range = max(1e-9, float(dev["high"].max()) - float(dev["low"].min()))
        close_loc = (price - float(dev["low"].min())) / dev_range  # 0..1
        one_sided = (ext_up > 0.3 and ext_dn < 0.1) or (ext_dn > 0.3 and ext_up < 0.1)
        out["u_trend_day_score"] = _clip(
            (min(1.0, max(ext_up, ext_dn)) * abs(2 * close_loc - 1)) if one_sided else 0.0, 0, 1)
        ib_range = max(1e-9, ib_hi - ib_lo)
        out["u_balanced_day_score"] = _clip(1.0 - min(1.0, (ext_up + ext_dn)) , 0, 1) \
            if dev_range < 1.5 * ib_range else 0.0
        out["u_neutral_day_score"] = _clip(1.0 - abs(2 * close_loc - 1)
                                           - 0.5 * (ext_up + ext_dn), 0, 1)

    # ── opening auction character (signed) ──
    ib_for_open = dev[dev.index <= ib_end] if not dev.empty else dev
    if len(ib_for_open) >= 2:
        o = float(ib_for_open["open"].iloc[0])
        hi = float(ib_for_open["high"].max()); lo = float(ib_for_open["low"].min())
        last = float(ib_for_open["close"].iloc[-1])
        rng = max(1e-9, hi - lo)
        drive = (last - o) / atr_ref
        out["u_open_drive_score"] = _clip(drive)
        # test-drive: came back through open then extended (open near middle, closed at extreme)
        open_loc = (o - lo) / rng
        out["u_open_test_drive_score"] = _clip((2 * ((last - lo) / rng) - 1)
                                               * (1 - abs(2 * open_loc - 1)))
        # rejection-reverse: drove one way (extreme) but closed back toward/through open
        max_run = (hi - o) if (hi - o) >= (o - lo) else (o - lo)
        give_back = (hi - last) if (hi - o) >= (o - lo) else (last - lo)
        out["u_open_rejection_reverse_score"] = _clip((give_back - 0.5 * max_run) / atr_ref) \
            if max_run > 0.2 * atr_ref else 0.0

    # ── developing-profile distances in PROFILE-WIDTH units + current-value location ──
    dprof = build_volume_profile(dev)
    pw = max(1e-9, dprof.vah - dprof.val)
    out["u_dist_dev_poc_pw"] = _clip((price - dprof.poc) / pw, -5, 5)
    out["u_dist_dev_vah_pw"] = _clip((price - dprof.vah) / pw, -5, 5)
    out["u_dist_dev_val_pw"] = _clip((price - dprof.val) / pw, -5, 5)
    out["u_inside_current_value"] = float(dprof.val <= price <= dprof.vah)
    out["u_above_current_value"] = float(price > dprof.vah)
    out["u_below_current_value"] = float(price < dprof.val)

    # ── profile shape (auction completion) ──
    bp = _binned_profile(dev, _tick_size(price))
    if bp is not None:
        centers, w, bpoc, bvah, bval = bp
        p = w / w.sum()
        mu = float((centers * p).sum())
        var = float((p * (centers - mu) ** 2).sum())
        sd = var ** 0.5
        if sd > 0:
            out["u_profile_skew"] = _clip(float((p * ((centers - mu) / sd) ** 3).sum()), -5, 5)
            out["u_profile_kurtosis"] = _clip(
                float((p * ((centers - mu) / sd) ** 4).sum()) - 3.0, -5, 10)
        thin = w < (0.1 * w.max())
        out["u_single_print_density"] = float(thin.mean())
        # poor high/low: extreme bin carries real volume (flat, unfinished) → likely revisit
        out["u_poor_high_flag"] = float(w[-1] > 0.5 * w.max())
        out["u_poor_low_flag"] = float(w[0] > 0.5 * w.max())
        # excess: thin tail (single prints) at the extreme = finished/rejected auction
        tail = max(1, len(w) // 10)
        out["u_excess_high_score"] = _clip(1.0 - float(w[-tail:].mean()) / w.max(), 0, 1)
        out["u_excess_low_score"] = _clip(1.0 - float(w[:tail].mean()) / w.max(), 0, 1)

    for n in _AS_NAMES:
        snapshot.add(Feature(n, out[n], avail, "mp"))
    return snapshot
