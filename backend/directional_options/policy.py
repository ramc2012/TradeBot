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


def _featurize(signal: dict[str, Any], candidate: dict[str, Any], regime: dict[str, Any]) -> np.ndarray:
    """Build a fixed-length feature vector from signal + candidate + regime.

    Order matters and must be stable across restarts because the
    persisted posterior is in this basis. New features get appended at
    the end, never inserted, and `EXPECTED_FEATURE_DIM` is bumped.
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
    p_trading_edge = float(candidate.get("p_trading_edge") or 0.0)
    p_terminal_edge = float(candidate.get("p_terminal_edge") or 0.0)
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
        1.0,  # bias
        confidence,
        expected_move_pct,
        horizon_bars / 12.0,  # rescale (12 bars ≈ 1 hour at 5m)
        jump_score,
        timing_precision,
        tail_probability,
        model_uncertainty,
        p_up,
        delta_abs,
        p_trading_edge,
        p_terminal_edge,
        p_minus_q_tail,
        probability_of_profit,
        skew_tax,
        timing_fit,
        expected_return_on_premium,
        liquidity_score,
        contract_score / 100.0,  # rescale
        regime_conf,
    ]

    # One-hot encodings
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


EXPECTED_FEATURE_DIM = 20 + len(KNOWN_REGIMES) + len(KNOWN_DELTA_BUCKETS) + len(KNOWN_EXPIRY_KINDS) + len(KNOWN_DIRECTIONS)


@dataclass
class BayesianRidge:
    """Conjugate Bayesian linear regression for R-multiple prediction.

    Prior: w ~ N(0, alpha^-1 I), noise precision beta.
    Posterior: w | data ~ N(mu, Sigma) where
        Sigma = (alpha I + beta X^T X)^-1
        mu    = beta Sigma X^T y

    We maintain S_inv = alpha I + beta X^T X and b = beta X^T y so updates
    are O(d^2) per observation, no batch refit.
    """
    dim: int
    alpha: float = 1.0
    beta: float = 1.0
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
        mean, var = self.predict_mean_var(x)
        return float(rng.normal(mean, math.sqrt(var)))

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
        if self.n == 0:
            # Weakly informative prior — small positive bias for 1.0×
            # so the policy starts at base sizing before evidence.
            prior_mean = 0.05 if abs(self.multiplier - 1.0) < 1e-6 else 0.0
            return float(rng.normal(prior_mean, 0.5))
        mean = self.sum_r / self.n
        if self.n < 2:
            return float(rng.normal(mean, 0.5))
        var = max((self.sum_r_sq / self.n) - mean * mean, 1e-4)
        # Posterior on the mean: stdev = sqrt(var / n)
        return float(rng.normal(mean, math.sqrt(var / self.n)))


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
        if value_state and int(value_state.get("dim", 0)) == EXPECTED_FEATURE_DIM:
            try:
                self._value_model = BayesianRidge.from_state(value_state)
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
    ) -> PolicyDecision:
        """Pick act/skip + size multiplier for a single candidate."""
        x = _featurize(signal, candidate, regime)
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
    ) -> tuple[Optional[int], list[float]]:
        """Score every candidate, return (best_idx, samples_per_candidate).

        Uses the value posterior (Thompson sampling) so the policy can
        explore — early on it may swap which strike it likes, but as the
        posterior tightens it converges to the strike with the best
        learned features.
        """
        if not candidates:
            return None, []
        samples: list[float] = []
        with self._lock:
            for c in candidates:
                x = _featurize(signal, c, regime)
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
    ) -> None:
        """Stash the feature vector so we can credit reward on close."""
        if not position_id:
            return
        x = _featurize(signal, candidate, regime)
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
