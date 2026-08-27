"""Feature vectors for the ranker — leak-free by construction.

THE ONE RULE
────────────
A feature may only use information that existed AT the decision instant. That
sounds obvious and is where lookahead almost always enters, so it is enforced
structurally rather than by care: `build_features` takes ONLY a
`candidate_snapshots` row, and outcome rows are never passed in. Any field
computed after the anchor — every column of `candidate_outcomes` — is
unreachable from here, so a leak would require changing this signature.

That matters specifically here. This repo already distrusts one of its own
backtests as "lookahead-tinged", and the labeller's barrier width had to be
built strictly backward-looking for the same reason.

NORMALISATION IS ALSO A LEAK
────────────────────────────
Scaling a feature by a statistic computed over the whole dataset leaks the
future into the past, because the mean of a period includes days the model is
supposed to be predicting. So nothing here is z-scored against a global. Every
feature is either already scale-free (a ratio, a fraction, a step count) or is
normalised against a value from the SAME ROW (spread as a fraction of its own
mid, cost as a fraction of its own premium). That makes a row's features
computable in isolation, which is also what makes live scoring identical to
training.

CATEGORICALS ARE EXPLICIT
─────────────────────────
One-hot rather than ordinal codes. `moneyness` is not a number and giving it one
would tell the model DEEP_ITM is four times NEAR_ITM. The vocabularies are fixed
constants rather than learned from the data, so a class absent from one training
window does not silently shift every column index in the next.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

# Fixed vocabularies: a column's meaning must not depend on which values
# happened to appear in a particular training window.
MONEYNESS_VOCAB = (
    "DEEP_ITM", "ITM", "NEAR_ITM", "ATM", "NEAR_OTM", "OTM", "DEEP_OTM",
)
LIQUIDITY_VOCAB = ("TOP", "HIGH", "MID", "LOW")
EXPIRY_CLASS_VOCAB = ("WEEKLY", "MONTHLY", "QUARTERLY", "LONG_DATED")
OPTION_TYPE_VOCAB = ("CE", "PE")

# An UNKNOWN category is deliberately NOT given a column. A row whose class
# could not be determined is a data-quality event, not a class to learn about;
# it drops out of the one-hot block entirely (all zeros), which is honest and
# keeps UNKNOWN from acquiring a fitted coefficient of its own.


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _one_hot(value: Any, vocab: Sequence[str]) -> list[float]:
    token = str(value or "").upper()
    return [1.0 if token == v else 0.0 for v in vocab]


def _safe_log1p(value: Optional[float]) -> float:
    """log1p of a non-negative quantity; 0.0 when absent.

    OI and volume span several orders of magnitude across a chain, so the raw
    values would let one deep-ITM strike dominate a linear model. Zero for a
    missing value is safe HERE only because a companion `*_missing` indicator
    carries the absence — see `feature_names`.
    """
    v = _finite(value)
    if v is None or v < 0:
        return 0.0
    return math.log1p(v)


def feature_names() -> list[str]:
    """Column names, in the exact order `build_features` emits them.

    Stored on every model version so a fitted artifact can never be applied to a
    differently-ordered vector — the silent failure where a model keeps scoring
    and every coefficient means something else.
    """
    names: list[str] = [
        # ── contract geometry ──
        "moneyness_steps",
        "abs_moneyness_steps",
        "days_to_expiry",
        "log_hours_to_expiry",
        "expiry_day_flag",
        "monthly_expiry_week_flag",
        # ── quote quality (each measured on the row itself) ──
        "spread_pct",
        "log_spread_pct",
        "has_two_sided_quote",
        # ── activity ──
        "log_oi",
        "log_volume",
        "oi_missing",
        "volume_missing",
        "liquidity_percentile",
        # ── option pricing ──
        "iv",
        "iv_missing",
        "delta",
        "abs_delta",
        "gamma_scaled",
        "theta_per_premium",
        "vega_per_premium",
        "greeks_missing",
        "log_premium",
        # ── chain context (same instant, from the row's features blob) ──
        "pcr_oi",
        "pcr_volume",
        "dist_to_max_pain_pct",
        "atm_iv",
        "iv_minus_atm_iv",
        "india_vix",
        "vix_missing",
    ]
    names += [f"moneyness__{v}" for v in MONEYNESS_VOCAB]
    names += [f"liquidity__{v}" for v in LIQUIDITY_VOCAB]
    names += [f"expiry_class__{v}" for v in EXPIRY_CLASS_VOCAB]
    names += [f"option_type__{v}" for v in OPTION_TYPE_VOCAB]
    return names


def build_features(snapshot: Mapping[str, Any]) -> list[float]:
    """One `candidate_snapshots` row → a fixed-length feature vector.

    Takes the snapshot ALONE. No outcome, no forward price, no session
    aggregate — the signature is the leak guard.
    """
    features_blob = snapshot.get("features") or {}
    if not isinstance(features_blob, Mapping):
        features_blob = {}

    bid = _finite(snapshot.get("bid"))
    ask = _finite(snapshot.get("ask"))
    ltp = _finite(snapshot.get("ltp"))
    premium = (
        (bid + ask) / 2.0 if bid and ask and ask >= bid else (ltp if ltp else None)
    )

    spread_pct = _finite(snapshot.get("spread_pct"))
    two_sided = 1.0 if (bid and ask and ask >= bid) else 0.0

    steps = _finite(snapshot.get("moneyness_steps"))
    dte = _finite(snapshot.get("days_to_expiry"))
    hours = _finite(snapshot.get("hours_to_expiry"))

    iv = _finite(snapshot.get("iv"))
    delta = _finite(snapshot.get("delta"))
    gamma = _finite(snapshot.get("gamma"))
    theta = _finite(snapshot.get("theta"))
    vega = _finite(snapshot.get("vega"))
    spot = _finite(snapshot.get("spot"))

    atm_iv = _finite(features_blob.get("atm_iv"))
    max_pain = _finite(features_blob.get("max_pain"))
    vix = _finite(features_blob.get("india_vix"))

    # Greeks are scaled by the row's own premium/spot so they are comparable
    # across a 5-rupee wing and a 700-rupee deep ITM.
    theta_per_premium = (theta / premium) if (theta is not None and premium) else 0.0
    vega_per_premium = (vega / premium) if (vega is not None and premium) else 0.0
    gamma_scaled = (gamma * spot) if (gamma is not None and spot) else 0.0

    dist_max_pain = (
        (spot - max_pain) / spot if (spot and max_pain is not None and spot > 0) else 0.0
    )

    row: list[float] = [
        steps if steps is not None else 0.0,
        abs(steps) if steps is not None else 0.0,
        dte if dte is not None else 0.0,
        # Hours can be negative after the close on expiry day; log1p of a
        # clamped value keeps the transform defined without inventing a value.
        math.log1p(max(hours, 0.0)) if hours is not None else 0.0,
        1.0 if snapshot.get("expiry_day_flag") else 0.0,
        1.0 if snapshot.get("monthly_expiry_week_flag") else 0.0,

        spread_pct if spread_pct is not None else 0.0,
        math.log1p(spread_pct) if spread_pct is not None and spread_pct >= 0 else 0.0,
        two_sided,

        _safe_log1p(snapshot.get("oi")),
        _safe_log1p(snapshot.get("volume")),
        1.0 if _finite(snapshot.get("oi")) is None else 0.0,
        1.0 if _finite(snapshot.get("volume")) is None else 0.0,
        _finite(snapshot.get("liquidity_percentile")) or 0.0,

        iv if iv is not None else 0.0,
        1.0 if iv is None else 0.0,
        delta if delta is not None else 0.0,
        abs(delta) if delta is not None else 0.0,
        gamma_scaled,
        theta_per_premium,
        vega_per_premium,
        1.0 if (delta is None or gamma is None or theta is None or vega is None) else 0.0,
        math.log1p(premium) if premium and premium > 0 else 0.0,

        _finite(features_blob.get("pcr_oi")) or 0.0,
        _finite(features_blob.get("pcr_volume")) or 0.0,
        dist_max_pain,
        atm_iv if atm_iv is not None else 0.0,
        (iv - atm_iv) if (iv is not None and atm_iv is not None) else 0.0,
        vix if vix is not None else 0.0,
        1.0 if vix is None else 0.0,
    ]

    row += _one_hot(snapshot.get("moneyness"), MONEYNESS_VOCAB)
    row += _one_hot(snapshot.get("liquidity_bucket"), LIQUIDITY_VOCAB)
    row += _one_hot(snapshot.get("expiry_class"), EXPIRY_CLASS_VOCAB)
    row += _one_hot(snapshot.get("option_type"), OPTION_TYPE_VOCAB)

    expected = len(feature_names())
    if len(row) != expected:  # pragma: no cover - guards a future edit
        raise ValueError(
            f"feature vector has {len(row)} values but feature_names() declares "
            f"{expected}; the two must be edited together"
        )
    return row


# ── targets ────────────────────────────────────────────────────────────────
TARGET_NET_POSITIVE = "net_return_positive"
TARGET_BEATS_BREAKEVEN = "mfe_clears_breakeven"

TARGETS = (TARGET_NET_POSITIVE, TARGET_BEATS_BREAKEVEN)


def build_target(outcome: Mapping[str, Any], target: str) -> Optional[int]:
    """The supervised label for one outcome row, or None when unusable.

    Returns None rather than 0 for anything that is not a real observation —
    an unlabellable row, a NO_TRADE row, a mark where no trade arrived. A 0 says
    "this trade lost"; None says "this is not evidence". Collapsing the two is
    how a dataset acquires a majority class made of measurement failures.
    """
    if str(outcome.get("label_status")) != "ok":
        return None
    # LTP is a print, not a mark: a forward price with no trade behind it is not
    # an observation of what the position was worth.
    if outcome.get("trade_arrived") is False:
        return None

    if target == TARGET_NET_POSITIVE:
        net = _finite(outcome.get("option_net_return_pct"))
        return None if net is None else int(net > 0)

    if target == TARGET_BEATS_BREAKEVEN:
        mfe = _finite(outcome.get("option_mfe_pct"))
        breakeven = _finite(outcome.get("breakeven_move_pct"))
        if mfe is None or breakeven is None:
            return None
        # max(mfe, 0): only a FAVOURABLE excursion can pay for a long option.
        return int(max(mfe, 0.0) >= breakeven)

    raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
