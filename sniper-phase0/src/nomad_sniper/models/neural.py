"""Optional neural multi-encoder scaffold.

This module implements the architecture boundary from the full spec without making torch a hard
dependency. If torch is installed, `build_neural_alpha_model` returns a PyTorch module with MP,
OF, HTF, and context encoders plus multi-head outputs. If torch is absent, callers get a clear
dependency error while the LightGBM Phase-0 path remains usable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NeuralAlphaConfig:
    mp_dim: int
    of_dim: int
    htf_dim: int
    context_dim: int
    hidden_dim: int = 64
    dropout: float = 0.10


def build_neural_alpha_model(config: NeuralAlphaConfig):
    torch, nn = _require_torch()

    class NeuralAlphaModel(nn.Module):
        def __init__(self, cfg: NeuralAlphaConfig):
            super().__init__()
            self.mp_encoder = _block(nn, cfg.mp_dim, cfg.hidden_dim, cfg.dropout)
            self.of_encoder = _block(nn, cfg.of_dim, cfg.hidden_dim, cfg.dropout)
            self.htf_encoder = _block(nn, cfg.htf_dim, cfg.hidden_dim, cfg.dropout)
            self.context_encoder = _block(nn, cfg.context_dim, cfg.hidden_dim, cfg.dropout)
            self.fusion = nn.Sequential(
                nn.Linear(cfg.hidden_dim * 4, cfg.hidden_dim * 2),
                nn.ReLU(),
                nn.Dropout(cfg.dropout),
                nn.Linear(cfg.hidden_dim * 2, cfg.hidden_dim),
                nn.ReLU(),
            )
            self.action_head = nn.Linear(cfg.hidden_dim, 4)  # long/short/no_trade/wait
            self.expected_r_head = nn.Linear(cfg.hidden_dim, 1)
            self.mfe_head = nn.Linear(cfg.hidden_dim, 1)
            self.mae_head = nn.Linear(cfg.hidden_dim, 1)
            self.regime_head = nn.Linear(cfg.hidden_dim, 8)
            self.size_head = nn.Sequential(nn.Linear(cfg.hidden_dim, 1), nn.Sigmoid())

        def forward(self, mp, of, htf, context):
            fused = self.fusion(
                torch.cat(
                    [
                        self.mp_encoder(mp),
                        self.of_encoder(of),
                        self.htf_encoder(htf),
                        self.context_encoder(context),
                    ],
                    dim=-1,
                )
            )
            return {
                "action_logits": self.action_head(fused),
                "expected_r": self.expected_r_head(fused).squeeze(-1),
                "mfe": self.mfe_head(fused).squeeze(-1),
                "mae": self.mae_head(fused).squeeze(-1),
                "regime_logits": self.regime_head(fused),
                "size_multiplier": self.size_head(fused).squeeze(-1),
            }

    return NeuralAlphaModel(config)


def multitask_loss(outputs, targets, *, weights: dict[str, float] | None = None):
    torch, nn = _require_torch()
    weights = weights or {}
    ce = nn.CrossEntropyLoss()
    mse = nn.MSELoss()
    loss = weights.get("action", 1.0) * ce(outputs["action_logits"], targets["action"])
    loss = loss + weights.get("expected_r", 1.0) * mse(outputs["expected_r"], targets["expected_r"])
    loss = loss + weights.get("mfe", 0.5) * mse(outputs["mfe"], targets["mfe"])
    loss = loss + weights.get("mae", 0.5) * mse(outputs["mae"], targets["mae"])
    if "regime" in targets:
        loss = loss + weights.get("regime", 0.3) * ce(outputs["regime_logits"], targets["regime"])
    if "size_multiplier" in targets:
        loss = loss + weights.get("size", 0.2) * mse(outputs["size_multiplier"], targets["size_multiplier"])
    return loss


def _block(nn, in_dim: int, hidden_dim: int, dropout: float):
    return nn.Sequential(
        nn.Linear(in_dim, hidden_dim),
        nn.ReLU(),
        nn.Dropout(dropout),
        nn.Linear(hidden_dim, hidden_dim),
        nn.ReLU(),
    )


def _require_torch():
    try:
        import torch
        from torch import nn
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "torch is required for the optional neural alpha model. Install torch to use "
            "nomad_sniper.models.neural; the LightGBM validation path does not require it."
        ) from exc
    return torch, nn
