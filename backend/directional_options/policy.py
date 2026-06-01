"""Online RL policy for directional long-options.

Replaces the hand-tuned hurdles (min_confidence, REGIME_BLOCKED,
DELTA_BUCKET_BLOCKED, min_expected_edge_pct, optimizer rejection reasons)
with a Bayesian linear contextual bandit. The policy learns three things
from realized paper-trade outcomes:

  1. Whether to take the trade at all (act vs skip).
  2. Which candidate strike to pick from the top-K surfaced by the
     selector — the policy scores each candidate on the same feature set
     and picks the argmax (with exploration).
  3. A size multiplier in {0.5×, 1.0×, 1.5×, 2.0×} of the base risk
     budget, chosen by Thompson sampling on per-bucket reward posteriors.

Reward = realized PnL / risk_budget (R-multiple), clipped to [-3, +5].
Risk budget at entry is config.risk.risk_pct × equity, BEFORE any
multiplier — that way the multiplier itself is part of the action and the
reward signal is unconfounded.

The policy is two Bayesian Ridge models (Gaussian conjugate priors on a
linear weight vector):

  * `value_model`   — predicts E[R] for (state, "act") tuples.
  * `skip_baseline` — predicts E[R | skip] ≡ 0 with low variance prior;
                      we keep it cheap (just a constant zero baseline)
                      because the alternative (training on every skip
                      with reward=0) would dwarf the act-signal.

Decision rule:

    sampled_r = value_model.sample(state_features)
    if sampled_r > 0:   take the trade
    else:               skip

This is Thompson sampling: when the posterior over R is uncertain (early
in training, or in a regime we've barely seen), the sampler will spread
across positive and negative draws and naturally explore. As the model
sees more data the posterior tightens and the policy converges to
exploit.

`min_confidence` is no longer a config knob — it emerges from the model.
A signal with low `signal.confidence` gets a low value posterior; if the
posterior mean is negative the policy skips. Effectively the threshold
is learned per-regime, per-delta-bucket, per-expiry-kind.

State is persisted to runtime/directional_options/policy_state.json so
the model warms up across restarts. On first run the policy boots with
a weakly-informative prior — every action gets a small positive sampled
R, so the system starts firing signals immediately and learns from the
results.
"""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


# Reward shaping
REWARD_CLIP_LOW = -3.0
REWARD_CLIP_HIGH = 5.0

# Size multiplier action space (× base risk_pct). One-position-per-symbol
# is enforced at the paper layer; the multiplier controls intensity per
# trade, not whether to stack.
SIZE_MULTIPLIERS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)

# Regimes the engine emits today. Unknown labels still work (they project
# onto the bias term), but enumerating known labels keeps the feature
# vector stable.
KNOWN_REGIMES: tuple[str, ...] = ("trend", "breakout", "micro_trend", "exploration", "chop", "risk_off")
KNOWN_DELTA_BUCKETS: tuple[str, ...] = ("lottery", "convex", "core", "linear", "deep")
KNOWN_EXPIRY_KINDS: tuple[str, ...] = ("weekly", "monthly")
KNOWN_DIRECTIONS: tuple[str, ...] = ("CE", "PE")


def _safe_tanh(value: Any, scale: float) -> float:
    try:
        if value is None:
            return 0.0
        v = float(value)
        return float(np.tanh(v / max(scale, 1e-9)))
    except (TypeError, ValueError):
        return 0.0


def _featurize(
    signal: dict[str, Any],
    candidate: dict[str, Any],
    regime: dict[str, Any],
    chain: Optional[dict[str, Any]] = None,
) -> np.ndarray:
    """Build a fixed-length feature vector from signal + candidate +
    regime, plus optional chain-level analytics.

    Order matters and must be stable across restarts because the
    persisted posterior is in this basis. New features get appended at
    the end, never inserted, and `EXPECTED_FEATURE_DIM` is bumped.
    `_load()` pads the persisted posterior into the new basis with
    block-diagonal prior blocks so historical learning is preserved.
    """
    # Continuous scalars from the signal / candidate
    confidence = float(signal.get("confidence") or 0.0)
    expected_move_pct = float(signal.get("expected_move_pct") or 0.0)
    horizon_bars = float(signal.get("expected_horizon_bars") or 0.0)
    jump_score = float(signal.get("jump_score") or 0.0)
    timing_precision = float(signal.get("timing_precision") or 0.0)
    tail_probability = float(signal.get("tail_probability") or 0.0)
    model_uncertainty = float(signal.get("model_uncertainty") or 0.0)
    p_up = float(signal.get("p_up") or 0.5)

    delta_abs = abs(float(candidate.get("delta") or 0.0))
    # IMPORTANT: p_trading_edge and p_terminal_edge come out of the
    # selector as absolute ₹ amounts (option_price units). Left raw they
    # dominate the feature vector — e.g. p_trading_edge=2175 dwarfs every
    # other O(1) feature, blowing the predictive variance into the
    # millions and making the cold-start Thompson sampler hyper-volatile.
    # Normalize by option_price so the feature is "edge as fraction of
    # premium" which lives naturally in roughly [-2, +5].
    option_price = max(float(candidate.get("option_price") or 0.0), 1.0)
    p_trading_edge = float(candidate.get("p_trading_edge") or 0.0) / option_price
    p_terminal_edge = float(candidate.get("p_terminal_edge") or 0.0) / option_price
    p_minus_q_tail = float(candidate.get("p_minus_q_tail") or 0.0)
    probability_of_profit = float(candidate.get("probability_of_profit") or 0.0)
    skew_tax = float(candidate.get("skew_tax") or 0.0)
    timing_fit = float(candidate.get("timing_fit") or 0.0)
    expected_return_on_premium = float(candidate.get("expected_return_on_premium") or 0.0)
    liquidity_score = float(candidate.get("liquidity_score") or 0.0)
    contract_score = float(candidate.get("contract_score") or 0.0)

    # Regime confidence is a useful additional scalar (separate from the
    # regime one-hot — it captures within-regime conviction).
    regime_conf = float(regime.get("confidence") or 0.0)

    cont = [
        1.0,  # bias                                            [v1, idx 0]
        confidence,                                           # [v1, idx 1]
        expected_move_pct,                                    # [v1, idx 2]
        horizon_bars / 12.0,  # rescale                       # [v1, idx 3]
        jump_score,                                           # [v1, idx 4]
        timing_precision,                                     # [v1, idx 5]
        tail_probability,                                     # [v1, idx 6]
        model_uncertainty,                                    # [v1, idx 7]
        p_up,                                                 # [v1, idx 8]
        delta_abs,                                            # [v1, idx 9]
        p_trading_edge,                                       # [v1, idx 10]
        p_terminal_edge,                                      # [v1, idx 11]
        p_minus_q_tail,                                       # [v1, idx 12]
        probability_of_profit,                                # [v1, idx 13]
        skew_tax,                                             # [v1, idx 14]
        timing_fit,                                           # [v1, idx 15]
        expected_return_on_premium,                           # [v1, idx 16]
        liquidity_score,                                      # [v1, idx 17]
        contract_score / 100.0,  # rescale                    # [v1, idx 18]
        regime_conf,                                          # [v1, idx 19]
    ]

    # ───────── v2 additions: per-candidate option analytics ──────────
    # Greeks beyond delta (delta_abs is already at v1 idx 9). theta/vega
    # normalised by option_price so the scale matches "fraction of
    # premium" — the same trick we used for p_trading_edge.
    gamma_raw = float(candidate.get("gamma") or 0.0)
    theta_raw = float(candidate.get("theta") or 0.0)
    vega_raw = float(candidate.get("vega") or 0.0)
    iv_raw = float(candidate.get("implied_vol") or 0.0)
    oi_change_pct = float(candidate.get("oi_change_pct") or 0.0)  # already a %
    spread_pct = float(candidate.get("spread_pct") or 0.0)
    cont.extend([
        gamma_raw * 100.0,                                    # [v2, idx 20]  gamma ×100 (typ 0–2)
        theta_raw / option_price,                             # [v2, idx 21]  theta as fraction of premium
        vega_raw / option_price,                              # [v2, idx 22]  vega as fraction of premium
        iv_raw,                                               # [v2, idx 23]  IV ratio (0–1 typ)
        oi_change_pct / 100.0,                                # [v2, idx 24]  OI change normalised
        spread_pct,                                           # [v2, idx 25]  bid-ask spread %
    ])

    # ───────── v2 additions: chain-level analytics ──────────────────
    # Chain context — None when no chain payload (cold-start, pre-market).
    # We feed sentinel zeros in that case; the policy learns the
    # "chain-feature-absent" pattern via the bias term + regime label.
    chain = chain or {}
    pcr_oi = float(chain.get("pcr_oi") or 0.0)
    pcr_oi_change = float(chain.get("pcr_oi_change") or 0.0)
    atm_iv = float(chain.get("atm_iv") or 0.0)
    iv_skew_norm = float(chain.get("iv_skew_25d_norm") or 0.0)
    gex_total = chain.get("gex_total")
    dex_calls = float(chain.get("dex_calls") or 0.0)
    dex_puts = float(chain.get("dex_puts") or 0.0)
    dex_net = float(chain.get("dex_net") or 0.0)
    dex_denom = max(abs(dex_calls) + abs(dex_puts), 1.0)
    atm_call_oi_chg = float(chain.get("atm_call_oi_change") or 0.0)
    atm_put_oi_chg = float(chain.get("atm_put_oi_change") or 0.0)
    atm_call_ltp_chg_pct = float(chain.get("atm_call_ltp_change_pct") or 0.0)
    atm_put_ltp_chg_pct = float(chain.get("atm_put_ltp_change_pct") or 0.0)
    cont.extend([
        # PCR — clipped via tanh to keep the cold-start variance bounded.
        # tanh((pcr - 1) / 0.5) maps 0.5 → -0.46, 1 → 0, 2 → +0.76.
        float(np.tanh((pcr_oi - 1.0) / 0.5)) if pcr_oi > 0 else 0.0,  # [v2, idx 26]
        _safe_tanh(pcr_oi_change, 0.2),                              # [v2, idx 27]  PCR Δ
        atm_iv,                                                       # [v2, idx 28]  ATM IV (0-1)
        _safe_tanh(iv_skew_norm, 0.5),                                # [v2, idx 29]  IV skew norm
        _safe_tanh(gex_total, 1e8),                                   # [v2, idx 30]  GEX (tanh-bounded)
        float(np.clip(dex_net / dex_denom, -1.0, 1.0)),              # [v2, idx 31]  DEX net ratio
        _safe_tanh(atm_call_oi_chg, 1e5),                             # [v2, idx 32]
        _safe_tanh(atm_put_oi_chg, 1e5),                              # [v2, idx 33]
        _safe_tanh(atm_call_ltp_chg_pct, 5.0),                        # [v2, idx 34]
        _safe_tanh(atm_put_ltp_chg_pct, 5.0),                         # [v2, idx 35]
    ])

    # One-hot encodings (positions unchanged from v1 — they remain at
    # the tail; the v1→v2 padder maps them to the same indices).
    regime_label = str(regime.get("label") or "").lower()
    regime_oh = [1.0 if regime_label == lbl else 0.0 for lbl in KNOWN_REGIMES]
    delta_bucket = str(candidate.get("delta_bucket") or "").lower()
    delta_oh = [1.0 if delta_bucket == lbl else 0.0 for lbl in KNOWN_DELTA_BUCKETS]
    expiry_kind = str(candidate.get("expiry_kind") or "").lower()
    expiry_oh = [1.0 if expiry_kind == lbl else 0.0 for lbl in KNOWN_EXPIRY_KINDS]
    direction = str(signal.get("direction") or "").upper()
    dir_oh = [1.0 if direction == lbl else 0.0 for lbl in KNOWN_DIRECTIONS]

    vec = cont + regime_oh + delta_oh + expiry_oh + dir_oh
    return np.asarray(vec, dtype=np.float64)


# Number of continuous features (including bias) in each version.
CONT_DIM_V1 = 20
CONT_DIM_V2 = 36  # 20 v1 + 6 candidate greeks + 10 chain features
ONE_HOT_DIM = len(KNOWN_REGIMES) + len(KNOWN_DELTA_BUCKETS) + len(KNOWN_EXPIRY_KINDS) + len(KNOWN_DIRECTIONS)

# Feature version. v1 = 35 dims (the original deployment). v2 adds
# greeks + chain analytics. Bump this any time the feature layout
# changes; `_load()` will detect the version mismatch and pad the
# persisted posterior into the new basis (block-diagonal extension).
FEATURE_VERSION = 2
EXPECTED_FEATURE_DIM = CONT_DIM_V2 + ONE_HOT_DIM  # = 51
LEGACY_FEATURE_DIMS = {
    1: CONT_DIM_V1 + ONE_HOT_DIM,  # = 35
}


def _extend_v1_to_current(state_v1: dict[str, Any]) -> dict[str, Any]:
    """Block-diagonal embed a v1 posterior (35-D) inside the current
    basis. Old continuous features keep their indices [0..19]; new
    continuous features get prior-only (alpha I) blocks. The one-hot
    tail moves from indices [20..34] to [36..50]; we copy that block
    into its new home.

    This preserves every closed-trade update so we don't lose the 32
    trades of learning from the v1 deployment.
    """
    alpha = float(state_v1.get("alpha", 1.0))
    S_inv_old = np.asarray(state_v1["S_inv"], dtype=np.float64)
    b_old = np.asarray(state_v1["b"], dtype=np.float64)
    if S_inv_old.shape != (LEGACY_FEATURE_DIMS[1], LEGACY_FEATURE_DIMS[1]):
        # Shape doesn't match — bail and let the caller fall back to a
        # fresh prior. Better to lose history than corrupt the math.
        return state_v1
    new_dim = EXPECTED_FEATURE_DIM
    n_new_cont = CONT_DIM_V2 - CONT_DIM_V1  # = 16
    S_inv_new = alpha * np.eye(new_dim, dtype=np.float64)
    b_new = np.zeros(new_dim, dtype=np.float64)
    # Map v1 indices into v2 indices:
    #   v1 [0..19]          (cont) → v2 [0..19]
    #   v1 [20..34]         (oh)   → v2 [36..50]
    # v2 [20..35] (new cont) stays at the alpha-I prior.
    v1_cont = slice(0, CONT_DIM_V1)
    v1_oh   = slice(CONT_DIM_V1, LEGACY_FEATURE_DIMS[1])
    v2_cont = slice(0, CONT_DIM_V1)
    v2_oh   = slice(CONT_DIM_V2, EXPECTED_FEATURE_DIM)
    # Copy continuous-continuous block.
    S_inv_new[v2_cont, v2_cont] = S_inv_old[v1_cont, v1_cont]
    # Copy one-hot-one-hot block.
    S_inv_new[v2_oh, v2_oh] = S_inv_old[v1_oh, v1_oh]
    # Copy off-diagonal cont↔oh cross-terms.
    S_inv_new[v2_cont, v2_oh] = S_inv_old[v1_cont, v1_oh]
    S_inv_new[v2_oh, v2_cont] = S_inv_old[v1_oh, v1_cont]
    # b vector.
    b_new[v2_cont] = b_old[v1_cont]
    b_new[v2_oh] = b_old[v1_oh]
    return {
        "dim": new_dim,
        "alpha": alpha,
        "beta": float(state_v1.get("beta", 1.0)),
        "S_inv": S_inv_new.tolist(),
        "b": b_new.tolist(),
        "n_seen": int(state_v1.get("n_seen", 0)),
    }


@dataclass
class BayesianRidge:
    """Conjugate Bayesian linear regression for R-multiple prediction.

    Prior: w ~ N(0, alpha^-1 I), noise precision beta.
    Posterior: w | data ~ N(mu, Sigma) where
        Sigma = (alpha I + beta X^T X)^-1
        mu    = beta Sigma X^T y

    We maintain S_inv = alpha I + beta X^T X and b = beta X^T y so updates
    are O(d^2) per observation, no batch refit.

    Prior tuning rationale: alpha=10 + beta=4 gives a cold-start
    predictive variance of roughly (||x||^2 / 10) + 0.25 ≈ 1.5 for the
    typical 51-D feature vector (||x||^2 ~ 12), so Thompson samples
    cluster in [-2σ, +2σ] = [-2.5, +2.5] — natural R-multiple range.

    The earlier alpha=beta=1 prior gave a cold-start σ of ~5-15 (we saw
    σ=218 once when the chain-feature scaling went wrong), so a freshly
    bootstrapped policy almost always sampled deep negative on the
    first draw → SKIP forever until something updated the posterior.
    """
    dim: int
    alpha: float = 10.0
    beta: float = 4.0
    S_inv: np.ndarray = field(default=None)  # type: ignore[assignment]
    b: np.ndarray = field(default=None)  # type: ignore[assignment]
    n_seen: int = 0

    def __post_init__(self) -> None:
        if self.S_inv is None:
            self.S_inv = self.alpha * np.eye(self.dim, dtype=np.float64)
        if self.b is None:
            self.b = np.zeros(self.dim, dtype=np.float64)

    def _posterior(self) -> tuple[np.ndarray, np.ndarray]:
        Sigma = np.linalg.inv(self.S_inv)
        mu = Sigma @ self.b
        return mu, Sigma

    def predict_mean_var(self, x: np.ndarray) -> tuple[float, float]:
        mu, Sigma = self._posterior()
        mean = float(mu @ x)
        var = float(x @ Sigma @ x + 1.0 / self.beta)
        return mean, max(var, 1e-6)

    def sample(self, x: np.ndarray, rng: np.random.Generator) -> float:
        """Thompson sample, then clip to the R-multiple realisable range.

        Cold-start posteriors with high σ otherwise generate Thompson
        draws like -184 or +37 (way outside any plausible R-multiple).
        That makes act/skip decisions look near-deterministic on a
        single draw. Clipping to [REWARD_CLIP_LOW-1, REWARD_CLIP_HIGH+1]
        keeps the exploration band sane — the policy still explores
        because the tail probability around 0 is preserved — but a
        single extreme draw can no longer override the bias term.
        """
        mean, var = self.predict_mean_var(x)
        raw = float(rng.normal(mean, math.sqrt(var)))
        return float(np.clip(raw, REWARD_CLIP_LOW - 1.0, REWARD_CLIP_HIGH + 1.0))

    def update(self, x: np.ndarray, y: float) -> None:
        self.S_inv = self.S_inv + self.beta * np.outer(x, x)
        self.b = self.b + self.beta * y * x
        self.n_seen += 1

    def to_state(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "alpha": self.alpha,
            "beta": self.beta,
            "S_inv": self.S_inv.tolist(),
            "b": self.b.tolist(),
            "n_seen": self.n_seen,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "BayesianRidge":
        return cls(
            dim=int(state["dim"]),
            alpha=float(state.get("alpha", 1.0)),
            beta=float(state.get("beta", 1.0)),
            S_inv=np.asarray(state["S_inv"], dtype=np.float64),
            b=np.asarray(state["b"], dtype=np.float64),
            n_seen=int(state.get("n_seen", 0)),
        )


@dataclass
class SizeBucket:
    """Per-multiplier Beta-like running posterior for size selection.

    We track (sum_r, sum_r_sq, n) per multiplier and Thompson-sample from
    a Normal posterior on the mean R-multiple. Cheap, robust, and respects
    the natural ordering (1.0× starts at a slight advantage so the policy
    doesn't oscillate between 0.5× and 2.0× before it has evidence).
    """
    multiplier: float
    sum_r: float = 0.0
    sum_r_sq: float = 0.0
    n: int = 0

    def update(self, r: float) -> None:
        self.sum_r += r
        self.sum_r_sq += r * r
        self.n += 1

    def sample(self, rng: np.random.Generator) -> float:
        # Posterior on the mean: t-style stdev = sqrt(var / n) shrinks
        # like 1/sqrt(n), which is what we want — but we also add a
        # forced-exploration term `prior_var / (n + prior_strength)` that
        # decays slowly and keeps under-sampled buckets in the running.
        # Without this, a single lucky early draw can collapse the
        # Thompson sampler onto one multiplier (observed empirically in
        # the first walk-forward run: 2.0× ran away with 209/223 trades).
        prior_var = 0.25  # σ ≈ 0.5 on R — covers the realistic range
        prior_strength = 4.0  # equivalent observations of the prior
        if self.n == 0:
            # Slight 1.0× prior so we start at base sizing. ~0 elsewhere.
            prior_mean = 0.05 if abs(self.multiplier - 1.0) < 1e-6 else 0.0
            return float(rng.normal(prior_mean, math.sqrt(prior_var)))
        mean = self.sum_r / self.n
        if self.n < 2:
            sample_var = prior_var
        else:
            sample_var = max((self.sum_r_sq / self.n) - mean * mean, 1e-4)
        # Effective variance: posterior on the mean + persistent forced-
        # exploration term that shrinks like 1/(n + prior_strength).
        eff_var = (sample_var / self.n) + (prior_var / (self.n + prior_strength))
        return float(rng.normal(mean, math.sqrt(eff_var)))


@dataclass
class PolicyDecision:
    act: bool
    sampled_value: float
    posterior_mean: float
    posterior_var: float
    size_multiplier: float
    size_samples: dict[float, float]
    reason: str
    feature_dim: int
    n_seen: int


class DirectionalPolicy:
    """Persistent online RL policy for the directional options engine."""

    def __init__(self, state_path: Path | str, *, seed: int | None = None):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rng = np.random.default_rng(seed if seed is not None else int.from_bytes(os.urandom(4), "little"))
        self._value_model = BayesianRidge(dim=EXPECTED_FEATURE_DIM)
        self._size_buckets: dict[float, SizeBucket] = {
            m: SizeBucket(multiplier=m) for m in SIZE_MULTIPLIERS
        }
        # Track outstanding actions by position_id so we can attribute
        # the realized reward on close.
        self._pending: dict[str, dict[str, Any]] = {}
        self._load()

    # ----- persistence -----------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text())
        except Exception:
            return
        value_state = payload.get("value_model")
        persisted_version = int(payload.get("feature_version", 1))
        if not value_state:
            return
        old_dim = int(value_state.get("dim", 0))
        try:
            if old_dim == EXPECTED_FEATURE_DIM:
                self._value_model = BayesianRidge.from_state(value_state)
            elif old_dim == LEGACY_FEATURE_DIMS.get(1) and persisted_version <= 1:
                # v1 → current basis (block-diagonal pad). Preserves the
                # n_seen counter + the learning on the original 35-D
                # features; new feature dims start with the prior.
                extended = _extend_v1_to_current(value_state)
                self._value_model = BayesianRidge.from_state(extended)
            # else: unknown schema — silently start fresh.
        except Exception:
            pass
        size_states = payload.get("size_buckets") or {}
        for key, bucket_state in size_states.items():
            try:
                m = float(key)
                if m in self._size_buckets:
                    self._size_buckets[m] = SizeBucket(
                        multiplier=m,
                        sum_r=float(bucket_state.get("sum_r", 0.0)),
                        sum_r_sq=float(bucket_state.get("sum_r_sq", 0.0)),
                        n=int(bucket_state.get("n", 0)),
                    )
            except Exception:
                continue
        pending = payload.get("pending") or {}
        if isinstance(pending, dict):
            self._pending = {str(k): dict(v) for k, v in pending.items()}

    def _persist(self) -> None:
        payload = {
            "feature_version": FEATURE_VERSION,
            "value_model": self._value_model.to_state(),
            "size_buckets": {
                f"{m:.2f}": {"sum_r": b.sum_r, "sum_r_sq": b.sum_r_sq, "n": b.n}
                for m, b in self._size_buckets.items()
            },
            "pending": self._pending,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.state_path)

    # ----- public API ------------------------------------------------------
    def decide(
        self,
        *,
        signal: dict[str, Any],
        candidate: dict[str, Any],
        regime: dict[str, Any],
        chain: Optional[dict[str, Any]] = None,
    ) -> PolicyDecision:
        """Pick act/skip + size multiplier for a single candidate.

        `chain` is the optional chain-analytics dict from
        `directional_options.chain_analytics.fetch_chain_analytics`. When
        absent the chain features fall back to sentinel zeros — the
        policy still works, just with less context.
        """
        x = _featurize(signal, candidate, regime, chain)
        with self._lock:
            sampled = self._value_model.sample(x, self._rng)
            mean, var = self._value_model.predict_mean_var(x)
            size_samples = {m: bucket.sample(self._rng) for m, bucket in self._size_buckets.items()}
        chosen_size = max(size_samples.items(), key=lambda kv: kv[1])[0]
        act = sampled > 0.0
        if act:
            reason = (
                f"policy: sampled R={sampled:.3f} (mean={mean:.3f} ± {math.sqrt(var):.2f}); "
                f"size {chosen_size:.1f}× chosen from {size_samples}"
            )
        else:
            reason = (
                f"policy: sampled R={sampled:.3f} ≤ 0 (mean={mean:.3f} ± {math.sqrt(var):.2f}); skip"
            )
        return PolicyDecision(
            act=act,
            sampled_value=sampled,
            posterior_mean=mean,
            posterior_var=var,
            size_multiplier=chosen_size,
            size_samples=size_samples,
            reason=reason,
            feature_dim=EXPECTED_FEATURE_DIM,
            n_seen=self._value_model.n_seen,
        )

    def rank_candidates(
        self,
        *,
        signal: dict[str, Any],
        candidates: list[dict[str, Any]],
        regime: dict[str, Any],
        chain: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], list[float]]:
        """Score every candidate, return (best_idx, samples_per_candidate).

        Uses the value posterior (Thompson sampling) so the policy can
        explore — early on it may swap which strike it likes, but as the
        posterior tightens it converges to the strike with the best
        learned features. The same `chain` analytics flow into every
        candidate's feature vector (chain context is per-symbol, not
        per-strike).
        """
        if not candidates:
            return None, []
        samples: list[float] = []
        with self._lock:
            for c in candidates:
                x = _featurize(signal, c, regime, chain)
                samples.append(self._value_model.sample(x, self._rng))
        if not samples:
            return None, []
        best_idx = int(np.argmax(samples))
        return best_idx, samples

    def register_open(
        self,
        *,
        position_id: str,
        signal: dict[str, Any],
        candidate: dict[str, Any],
        regime: dict[str, Any],
        size_multiplier: float,
        risk_budget: float,
        chain: Optional[dict[str, Any]] = None,
    ) -> None:
        """Stash the feature vector so we can credit reward on close.

        The features captured here are what the model will be trained on
        — chain analytics at entry are recorded with the trade so the
        posterior update on close reflects the conditions that drove
        the decision, not whatever the chain looks like at close time.
        """
        if not position_id:
            return
        x = _featurize(signal, candidate, regime, chain)
        with self._lock:
            self._pending[position_id] = {
                "features": x.tolist(),
                "size_multiplier": float(size_multiplier),
                "risk_budget": float(max(risk_budget, 1.0)),
            }
            self._persist()

    def record_close(self, *, position_id: str, realized_pnl: float) -> Optional[float]:
        """Convert realized PnL to R-multiple and apply to the model."""
        if not position_id:
            return None
        with self._lock:
            entry = self._pending.pop(position_id, None)
        if not entry:
            return None
        risk_budget = float(entry.get("risk_budget") or 1.0)
        size_mult = float(entry.get("size_multiplier") or 1.0)
        # The position was sized at risk_budget × size_mult, so the
        # natural R-multiple is realized / (risk_budget × size_mult).
        denom = max(risk_budget * size_mult, 1.0)
        r = float(realized_pnl) / denom
        r_clipped = float(np.clip(r, REWARD_CLIP_LOW, REWARD_CLIP_HIGH))
        x = np.asarray(entry["features"], dtype=np.float64)
        with self._lock:
            self._value_model.update(x, r_clipped)
            bucket = self._size_buckets.get(size_mult)
            if bucket is not None:
                bucket.update(r_clipped)
            self._persist()
        return r_clipped

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "n_seen": self._value_model.n_seen,
                "feature_dim": EXPECTED_FEATURE_DIM,
                "feature_version": FEATURE_VERSION,
                "size_buckets": {
                    f"{m:.2f}": {
                        "mean_R": (b.sum_r / b.n) if b.n else None,
                        "n": b.n,
                    }
                    for m, b in self._size_buckets.items()
                },
                "pending_positions": list(self._pending.keys()),
            }


_GLOBAL_POLICY: Optional[DirectionalPolicy] = None
_GLOBAL_LOCK = threading.Lock()


def get_policy(state_path: Path | str | None = None) -> DirectionalPolicy:
    """Process-wide singleton so the service, paper layer, and tests
    all read/write the same posterior."""
    global _GLOBAL_POLICY
    with _GLOBAL_LOCK:
        if _GLOBAL_POLICY is None:
            if state_path is None:
                from directional_options.config import RUNTIME_ROOT  # local import to avoid cycles
                state_path = RUNTIME_ROOT / "policy_state.json"
            _GLOBAL_POLICY = DirectionalPolicy(state_path)
        return _GLOBAL_POLICY


def reset_policy_for_tests() -> None:
    """Test hook — wipe the singleton so each test gets a fresh policy."""
    global _GLOBAL_POLICY
    with _GLOBAL_LOCK:
        _GLOBAL_POLICY = None
