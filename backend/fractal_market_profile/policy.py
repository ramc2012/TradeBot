"""Online policy for Fractal Market Profile trade packets.

The policy is a compact Bayesian linear contextual bandit. It consumes the
FMP signal plus the rule-model payload, returns act/skip, and updates from
closed paper-trade PnL. The deterministic rule model handles hard guards;
this layer learns which otherwise-valid setups deserve capital.
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

from fractal_market_profile.config import PAPER_ROOT, POLICY_CONFIG


KNOWN_SETUPS: tuple[str, ...] = (
    "hourly_ib_breakout_call",
    "hourly_ib_breakout_put",
    "trend_pullback_call",
    "trend_pullback_put",
    "daily_balance_mean_reversion_call",
    "daily_balance_mean_reversion_put",
    "daily_balance_extreme_reversion_call",
    "daily_balance_extreme_reversion_put",
    "daily_balance_breakout_call",
    "daily_balance_breakout_put",
)
KNOWN_HORIZONS: tuple[str, ...] = ("scalp", "swing", "positional")
KNOWN_INSTRUMENTS: tuple[str, ...] = ("CE", "PE", "FUT")
FEATURE_VERSION = 1


def _float(source: Any, key: str, default: float = 0.0) -> float:
    try:
        value = source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)
        if value is None:
            return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return numeric
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_tanh(value: Any, scale: float) -> float:
    try:
        return float(np.tanh(float(value) / max(scale, 1e-9)))
    except (TypeError, ValueError):
        return 0.0


def _one_hot(value: str, labels: tuple[str, ...]) -> list[float]:
    normalized = str(value or "").lower()
    return [1.0 if normalized == label.lower() else 0.0 for label in labels]


def _risk_reward(signal: dict[str, Any]) -> float:
    entry = _float(signal, "entry_trigger")
    stop = _float(signal, "stop_level")
    target = _float(signal, "target_level")
    action = str(signal.get("action") or "").upper()
    if action == "LONG":
        risk = max(entry - stop, 1e-9)
        reward = max(target - entry, 0.0)
    else:
        risk = max(stop - entry, 1e-9)
        reward = max(entry - target, 0.0)
    return reward / risk if risk > 0 else 0.0


def _featurize(signal: dict[str, Any]) -> np.ndarray:
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    options = signal.get("options") if isinstance(signal.get("options"), dict) else {}
    ai_model = signal.get("ai_model") if isinstance(signal.get("ai_model"), dict) else {}
    components = ai_model.get("components") if isinstance(ai_model.get("components"), dict) else {}
    features = ai_model.get("features") if isinstance(ai_model.get("features"), dict) else {}

    action = str(signal.get("action") or "").upper()
    sign = 1.0 if action == "LONG" else -1.0
    instrument = str(options.get("instrument_type") or options.get("option_type") or "").upper()
    flow_direction = str(metadata.get("order_flow_direction") or "").lower()
    daily_direction = str(metadata.get("daily_direction") or "").lower()
    option_type = str(options.get("option_type") or "").upper()
    if instrument == "FUT":
        option_type = "FUT"

    rule_score = _float(ai_model, "score", 50.0) / 100.0
    confidence = _clip(_float(signal, "confidence"))
    va_score = sign * _float(signal, "value_migration_score")
    order_flow_match = 1.0 if (
        (action == "LONG" and flow_direction == "bullish")
        or (action == "SHORT" and flow_direction == "bearish")
    ) else 0.0
    daily_match = 1.0 if (
        (action == "LONG" and daily_direction == "bullish")
        or (action == "SHORT" and daily_direction == "bearish")
    ) else 0.0
    pcr = _float(options, "pcr_oi", 1.0)
    pcr_edge = (pcr - 1.0) if action == "LONG" else (1.0 - pcr)
    premium = _float(options, "premium")
    days_to_expiry = _float(options, "days_to_expiry")
    iv_rank = _float(options, "iv_rank")
    oi_change = _float(options, "oi_change")
    volume = _float(options, "volume")
    india_vix = _float(metadata, "india_vix")

    cont = [
        1.0,
        confidence,
        _clip(rule_score),
        1.0 if action == "LONG" else 0.0,
        1.0 if action == "SHORT" else 0.0,
        _safe_tanh(va_score, 3.0),
        _clip(_float(metadata, "order_flow_alignment")),
        order_flow_match,
        daily_match,
        _clip(_float(signal, "hourly_number") / 8.0),
        _safe_tanh((india_vix or 16.0) - 18.0, 10.0),
        1.0 if instrument == "FUT" else 0.0,
        _safe_tanh(premium, 250.0),
        _safe_tanh(oi_change, 75_000.0),
        _safe_tanh(volume, 250_000.0),
        _safe_tanh(pcr_edge, 0.55),
        _clip(1.0 - iv_rank / 65.0) if iv_rank > 0 else 0.55,
        _clip(days_to_expiry / 10.0),
        _clip((_risk_reward(signal) - 0.75) / 2.5),
        1.0 if bool(ai_model.get("allowed", True)) else 0.0,
        _float(components, "profile_alignment", 0.5),
        _float(components, "auction_structure", 0.5),
        _float(components, "order_flow_confirmation", 0.5),
        _float(components, "instrument_quality", 0.5),
        _float(components, "volatility_risk", 0.5),
        _float(components, "execution_timing", 0.5),
        _float(components, "data_quality", 0.5),
        1.0 if bool(features.get("execution_ready", True)) else 0.0,
    ]
    vec = (
        cont
        + _one_hot(str(signal.get("setup_name") or ""), KNOWN_SETUPS)
        + _one_hot(str(signal.get("horizon") or ""), KNOWN_HORIZONS)
        + _one_hot(option_type, KNOWN_INSTRUMENTS)
    )
    return np.asarray(vec, dtype=np.float64)


EXPECTED_FEATURE_DIM = 28 + len(KNOWN_SETUPS) + len(KNOWN_HORIZONS) + len(KNOWN_INSTRUMENTS)


@dataclass
class BayesianRidge:
    dim: int
    alpha: float = 14.0
    beta: float = 5.0
    S_inv: np.ndarray = field(default=None)  # type: ignore[assignment]
    b: np.ndarray = field(default=None)  # type: ignore[assignment]
    n_seen: int = 0

    def __post_init__(self) -> None:
        if self.S_inv is None:
            self.S_inv = self.alpha * np.eye(self.dim, dtype=np.float64)
        if self.b is None:
            self.b = np.zeros(self.dim, dtype=np.float64)

    def _posterior(self) -> tuple[np.ndarray, np.ndarray]:
        try:
            sigma = np.linalg.inv(self.S_inv)
            mu = np.dot(sigma, self.b)
        except Exception:
            self.S_inv = self.alpha * np.eye(self.dim, dtype=np.float64)
            self.b = np.zeros(self.dim, dtype=np.float64)
            sigma = np.linalg.inv(self.S_inv)
            mu = np.dot(sigma, self.b)
        if not np.all(np.isfinite(sigma)) or not np.all(np.isfinite(mu)):
            self.S_inv = self.alpha * np.eye(self.dim, dtype=np.float64)
            self.b = np.zeros(self.dim, dtype=np.float64)
            sigma = np.linalg.inv(self.S_inv)
            mu = np.dot(sigma, self.b)
        return mu, sigma

    def predict_mean_var(self, x: np.ndarray) -> tuple[float, float]:
        mu, sigma = self._posterior()
        mean = float(np.dot(mu, x))
        var = float(np.dot(x, np.dot(sigma, x)) + 1.0 / self.beta)
        if not math.isfinite(mean):
            mean = 0.0
        if not math.isfinite(var):
            var = 1.0 / max(self.beta, 1e-9)
        return mean, max(var, 1e-6)

    def sample(self, x: np.ndarray, rng: np.random.Generator) -> float:
        mean, var = self.predict_mean_var(x)
        return float(np.clip(rng.normal(mean, math.sqrt(var)), -4.0, 6.0))

    def update(self, x: np.ndarray, reward: float) -> None:
        self.S_inv = self.S_inv + self.beta * np.outer(x, x)
        self.b = self.b + self.beta * reward * x
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
        dim = int(state["dim"])
        S_inv = np.asarray(state["S_inv"], dtype=np.float64)
        b = np.asarray(state["b"], dtype=np.float64)
        if dim != EXPECTED_FEATURE_DIM or S_inv.shape != (dim, dim) or b.shape != (dim,):
            raise ValueError("FMP policy state has invalid shape")
        if not np.all(np.isfinite(S_inv)) or not np.all(np.isfinite(b)):
            raise ValueError("FMP policy state contains non-finite values")
        return cls(
            dim=dim,
            alpha=float(state.get("alpha", 14.0)),
            beta=float(state.get("beta", 5.0)),
            S_inv=S_inv,
            b=b,
            n_seen=int(state.get("n_seen", 0)),
        )


@dataclass(frozen=True)
class FMPPolicyDecision:
    act: bool
    sampled_value: float
    posterior_mean: float
    posterior_var: float
    reason: str
    feature_dim: int
    n_seen: int
    warmup: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "act": self.act,
            "sampled_value": round(self.sampled_value, 4),
            "posterior_mean": round(self.posterior_mean, 4),
            "posterior_var": round(self.posterior_var, 6),
            "reason": self.reason,
            "feature_dim": self.feature_dim,
            "n_seen": self.n_seen,
            "warmup": self.warmup,
        }


class FMPPolicy:
    def __init__(self, state_path: Path | str, *, seed: int | None = None, config: dict[str, Any] | None = None):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = {**POLICY_CONFIG, **(config or {})}
        self._lock = threading.Lock()
        self._rng = np.random.default_rng(seed if seed is not None else int.from_bytes(os.urandom(4), "little"))
        self._model = BayesianRidge(dim=EXPECTED_FEATURE_DIM)
        self._pending: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if int(payload.get("feature_version", 0)) != FEATURE_VERSION:
                return
            self._model = BayesianRidge.from_state(payload["value_model"])
            pending = payload.get("pending") or {}
            if isinstance(pending, dict):
                self._pending = {str(key): dict(value) for key, value in pending.items()}
        except Exception:
            return

    def _persist(self) -> None:
        payload = {
            "feature_version": FEATURE_VERSION,
            "value_model": self._model.to_state(),
            "pending": self._pending,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.state_path)

    def decide(self, *, signal: dict[str, Any]) -> FMPPolicyDecision:
        x = _featurize(signal)
        with self._lock:
            sampled = self._model.sample(x, self._rng)
            mean, var = self._model.predict_mean_var(x)
            n_seen = self._model.n_seen

        warmup = n_seen < int(self.config.get("warmup_trades", 12))
        min_sampled_r = float(self.config.get("min_sampled_r", 0.0))
        act = bool(warmup or sampled > min_sampled_r)
        if warmup:
            reason = f"policy warm-up: collecting outcomes ({n_seen}/{int(self.config.get('warmup_trades', 12))})"
        elif act:
            reason = f"policy sampled R={sampled:.3f} above gate {min_sampled_r:.3f}"
        else:
            reason = f"policy sampled R={sampled:.3f} below gate {min_sampled_r:.3f}"
        return FMPPolicyDecision(
            act=act,
            sampled_value=sampled,
            posterior_mean=mean,
            posterior_var=var,
            reason=reason,
            feature_dim=EXPECTED_FEATURE_DIM,
            n_seen=n_seen,
            warmup=warmup,
        )

    def register_open(self, *, position_id: str, signal: dict[str, Any], risk_basis: float) -> None:
        if not position_id:
            return
        x = _featurize(signal)
        with self._lock:
            self._pending[position_id] = {
                "features": x.tolist(),
                "risk_basis": float(max(risk_basis, 1.0)),
            }
            self._persist()

    def record_close(self, *, position_id: str, realized_pnl: float) -> Optional[float]:
        if not position_id:
            return None
        with self._lock:
            pending = self._pending.pop(position_id, None)
        if not pending:
            return None
        risk_basis = float(pending.get("risk_basis") or 1.0)
        raw_reward = float(realized_pnl) / max(risk_basis, 1.0)
        reward = float(np.clip(
            raw_reward,
            float(self.config.get("reward_clip_low", -3.0)),
            float(self.config.get("reward_clip_high", 5.0)),
        ))
        x = np.asarray(pending["features"], dtype=np.float64)
        with self._lock:
            self._model.update(x, reward)
            self._persist()
        return reward

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "feature_version": FEATURE_VERSION,
                "feature_dim": EXPECTED_FEATURE_DIM,
                "n_seen": self._model.n_seen,
                "pending_positions": list(self._pending.keys()),
            }


_GLOBAL_POLICY: Optional[FMPPolicy] = None
_GLOBAL_LOCK = threading.Lock()


def get_policy(state_path: Path | str | None = None) -> FMPPolicy:
    global _GLOBAL_POLICY
    with _GLOBAL_LOCK:
        if _GLOBAL_POLICY is None:
            _GLOBAL_POLICY = FMPPolicy(state_path or (PAPER_ROOT / "policy_state.json"))
        return _GLOBAL_POLICY


def reset_policy_for_tests() -> None:
    global _GLOBAL_POLICY
    with _GLOBAL_LOCK:
        _GLOBAL_POLICY = None
