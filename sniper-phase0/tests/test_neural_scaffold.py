from __future__ import annotations

import pytest

from nomad_sniper.models.neural import NeuralAlphaConfig, build_neural_alpha_model


def test_neural_scaffold_requires_or_builds_torch_model():
    cfg = NeuralAlphaConfig(mp_dim=3, of_dim=3, htf_dim=3, context_dim=2)
    try:
        model = build_neural_alpha_model(cfg)
    except ModuleNotFoundError as exc:
        assert "torch is required" in str(exc)
    else:
        assert hasattr(model, "mp_encoder")
