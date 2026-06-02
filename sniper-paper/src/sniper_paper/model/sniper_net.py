"""SniperNet — multi-head feed-forward network for the expectancy engine.

Design (v1, tabular):
    input  : standardised feature vector  [F]
        │
    shared encoder : Linear→BN→ReLU→Dropout  (×2 blocks)   → latent [H]
        │
    ┌───┴─────────────────────────────────────────────────────────┐
    │           │            │             │            │          │
  p_win     expected_R     MFE           MAE         (latent exposed for viz)
 (sigmoid)  (linear)     (softplus)   (softplus)
        │
        └─ expected_R is the EV head used by the signal gate.

We deliberately keep this an MLP, not a CNN/LSTM. The historical training data
is candle-derived tabular features with no usable temporal tick sequence yet.
The sequence encoder is future work (Phase 2) once live tick capture matures —
the multi-head structure here is designed so the encoder can be swapped without
touching the heads.

Heads return per-sample tensors. `forward(..., return_latent=True)` also returns
the latent activations + per-layer activations for the visualisation endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn as nn


@dataclass
class SniperNetConfig:
    n_features: int
    hidden: int = 64
    latent: int = 32
    dropout: float = 0.15
    n_encoder_blocks: int = 2


class _Block(nn.Module):
    def __init__(self, in_dim: int, out_dim: int, dropout: float):
        super().__init__()
        self.linear = nn.Linear(in_dim, out_dim)
        self.bn = nn.BatchNorm1d(out_dim)
        self.act = nn.ReLU()
        self.drop = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.drop(self.act(self.bn(self.linear(x))))


class SniperNet(nn.Module):
    def __init__(self, cfg: SniperNetConfig):
        super().__init__()
        self.cfg = cfg

        blocks: list[nn.Module] = []
        dim = cfg.n_features
        for _ in range(cfg.n_encoder_blocks):
            blocks.append(_Block(dim, cfg.hidden, cfg.dropout))
            dim = cfg.hidden
        self.encoder = nn.ModuleList(blocks)
        self.to_latent = nn.Linear(dim, cfg.latent)

        # Heads.
        self.head_pwin = nn.Linear(cfg.latent, 1)        # logit → sigmoid
        self.head_exp_r = nn.Linear(cfg.latent, 1)       # expected net R (regression)
        self.head_mfe = nn.Linear(cfg.latent, 1)         # softplus (>=0)
        self.head_mae = nn.Linear(cfg.latent, 1)         # softplus (>=0)

    def forward(
        self, x: torch.Tensor, return_internals: bool = False
    ) -> dict[str, torch.Tensor]:
        activations: list[torch.Tensor] = []
        h = x
        for block in self.encoder:
            h = block(h)
            if return_internals:
                activations.append(h.detach())
        latent = self.to_latent(h)
        if return_internals:
            activations.append(latent.detach())

        out = {
            "p_win": torch.sigmoid(self.head_pwin(latent)).squeeze(-1),
            "expected_R": self.head_exp_r(latent).squeeze(-1),
            "mfe": torch.nn.functional.softplus(self.head_mfe(latent)).squeeze(-1),
            "mae": torch.nn.functional.softplus(self.head_mae(latent)).squeeze(-1),
        }
        if return_internals:
            out["latent"] = latent.detach()
            out["activations"] = activations  # type: ignore[assignment]
        return out


def multitask_loss(
    out: dict[str, torch.Tensor],
    targets: dict[str, torch.Tensor],
    weights: dict[str, float] | None = None,
    turnover_penalty: float = 0.0,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Combined loss.

      L = w_cls·BCE(p_win, win)
        + w_reg·MSE(expected_R, net_R)
        + w_mfe·MSE(mfe, mfe_R)
        + w_mae·MSE(mae, mae_R)
        + turnover_penalty · mean(sigmoid-ish trade propensity)

    The turnover term discourages the EV head from being optimistic everywhere
    (which would make the gate fire too often). It penalises the mean positive
    part of expected_R, nudging the model to be selective.
    """
    w = {"cls": 1.0, "reg": 1.0, "mfe": 0.3, "mae": 0.3}
    if weights:
        w.update(weights)

    p = torch.clamp(out["p_win"], 1e-6, 1.0 - 1e-6)  # numerical safety for BCE
    bce = nn.functional.binary_cross_entropy(p, targets["win"])
    mse_r = nn.functional.mse_loss(out["expected_R"], targets["net_R"])
    mse_mfe = nn.functional.mse_loss(out["mfe"], targets["mfe_R"])
    mse_mae = nn.functional.mse_loss(out["mae"], targets["mae_R"])

    turnover = torch.clamp(out["expected_R"], min=0.0).mean() * turnover_penalty

    total = w["cls"] * bce + w["reg"] * mse_r + w["mfe"] * mse_mfe + w["mae"] * mse_mae + turnover
    parts = {
        "bce": float(bce.detach()),
        "mse_R": float(mse_r.detach()),
        "mse_mfe": float(mse_mfe.detach()),
        "mse_mae": float(mse_mae.detach()),
        "turnover": float(turnover.detach()),
        "total": float(total.detach()),
    }
    return total, parts
