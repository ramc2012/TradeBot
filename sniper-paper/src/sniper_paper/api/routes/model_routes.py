"""Model + feature visualization endpoints.

Reads the currently-active LightGBM artifact and the stored signal feature
snapshots to expose:

  /api/model/info             — artifact id, walk-forward report, feature list
  /api/model/importance       — feature importance (gain + split)
  /api/model/predictions      — predicted score distribution from recent signals
  /api/features/list          — feature names
  /api/features/distribution  — bins for one feature across recent signals
  /api/features/timeseries    — feature value over time for one instrument
  /api/signals/{id}/explain   — per-feature contribution for one signal (LightGBM pred_contrib)

Visualizations are minimal-dep: clients (the dashboard HTML) plot with Chart.js.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from sniper_paper.common.settings import Settings
from sniper_paper.model.loader import load_active
from sniper_paper.persistence.db import get_pool

router = APIRouter()


def _settings() -> Settings:
    return Settings.load("configs/paper.yaml")


def _load_model():
    s = _settings()
    return load_active(s.model.active_model_pointer)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _nifty_mp_artifact_dir() -> Path:
    configured = Path(_settings().model.artifact_dir) / "nifty_underlying_mp_current"
    if configured.exists():
        return configured
    return _repo_root() / "sniper-phase0" / "artifacts" / "nifty_underlying_mp_current"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def _read_csv_records(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if limit is not None:
        df = df.head(limit)
    return df.replace({np.nan: None}).to_dict(orient="records")


_MODEL_TYPE_LABELS = {
    "lightgbm_candle_v0": "LightGBM (gradient-boosted trees) — v0 baseline.",
    "sniper_net_v1": "SniperNet (multi-head feed-forward NN): shared encoder → 4 heads (p_win, expected_R, MFE, MAE).",
}


@router.get("/api/model/nifty-mp")
async def nifty_mp_artifact() -> dict[str, Any]:
    """Return promoted NIFTY-underlying MP model report artifacts for dashboard charts."""
    root = _nifty_mp_artifact_dir()
    if not root.exists():
        return {
            "available": False,
            "artifact_dir": str(root),
            "reason": "NIFTY MP artifact directory does not exist.",
        }

    feature_importance = _read_csv_records(root / "feature_importance.csv")
    feature_importance.sort(key=lambda r: float(r.get("gain") or 0.0), reverse=True)

    daily = _read_csv_records(root / "daily_pnl.csv")
    monthly = _read_csv_records(root / "monthly_pnl.csv")
    threshold = _read_csv_records(root / "report_threshold_sweep.csv")
    acted = _read_csv_records(root / "oos_acted_trades.csv", limit=500)

    configured_period = Path(_settings().model.artifact_dir) / "nifty_atr_period_sweep"
    period_root = (
        configured_period
        if configured_period.exists()
        else _repo_root() / "sniper-phase0" / "artifacts" / "nifty_atr_period_sweep"
    )
    period_summary = _read_csv_records(period_root / "period_summary.csv")
    period_predictions = _read_csv_records(period_root / "period_predictions.csv", limit=1000)

    return {
        "available": True,
        "artifact_dir": str(root),
        "summary": _read_json(root / "summary.json"),
        "backtest_report": _read_json(root / "backtest_report.json"),
        "feature_importance": feature_importance,
        "daily_pnl": daily,
        "monthly_pnl": monthly,
        "threshold_sweep": threshold,
        "acted_trades": acted,
        "period_sweep": {
            "available": bool(period_summary),
            "artifact_dir": str(period_root),
            "summary": period_summary,
            "predictions": period_predictions,
        },
    }


# ─── Model info / metadata ──────────────────────────────────────────
@router.get("/api/model/info")
async def model_info() -> dict[str, Any]:
    s = _settings()
    m = _load_model()
    artifact_dir = Path(s.model.artifact_dir) / m.artifact_id
    wf = artifact_dir / "walk_forward_report.json"
    metadata = artifact_dir / "metadata.json"
    meta = json.loads(metadata.read_text()) if metadata.exists() else {}
    return {
        "artifact_id": m.artifact_id,
        "feature_order": list(m.feature_order),
        "backend": "neural_network" if m.is_nn else "lightgbm",
        "model_type": _MODEL_TYPE_LABELS.get(m.model_type, m.model_type),
        "model_type_raw": m.model_type,
        "architecture": meta.get("architecture"),
        "hyperparams": meta.get("hyperparams"),
        "walk_forward_report": json.loads(wf.read_text()) if wf.exists() else None,
        "metadata": meta or None,
    }


# ─── Feature importance ─────────────────────────────────────────────
@router.get("/api/model/importance")
async def model_importance() -> dict[str, Any]:
    m = _load_model()
    if m.is_nn:
        return _nn_importance(m)
    booster = m.classifier  # the LightGBM Booster used for p_win
    try:
        gain = booster.feature_importance(importance_type="gain").tolist()
        split = booster.feature_importance(importance_type="split").tolist()
    except Exception as e:
        raise HTTPException(500, f"Could not read importance: {e}") from e
    rows = []
    for name, g, s in zip(m.feature_order, gain, split, strict=False):
        rows.append({"feature": name, "gain": float(g), "split": int(s)})
    rows.sort(key=lambda r: r["gain"], reverse=True)
    return {"backend": "lightgbm", "head": "classifier (p_win)", "importance_type": "gain", "features": rows}


def _nn_importance(m) -> dict[str, Any]:
    """Input sensitivity = L2 norm of the first encoder layer's weight column
    for each input feature. A cheap, deterministic proxy for how much each input
    drives the first hidden representation (no data needed)."""
    torch = m._torch
    first_linear = m.net.encoder[0].linear  # nn.Linear(n_features, hidden)
    W = first_linear.weight.detach().cpu().numpy()  # [hidden, n_features]
    col_norm = np.linalg.norm(W, axis=0)            # [n_features]
    total = float(col_norm.sum()) or 1.0
    rows = [
        {"feature": name, "gain": float(c), "weight_share": float(c / total)}
        for name, c in zip(m.feature_order, col_norm, strict=False)
    ]
    rows.sort(key=lambda r: r["gain"], reverse=True)
    return {
        "backend": "neural_network",
        "head": "encoder layer 1 (input → hidden)",
        "importance_type": "first_layer_weight_norm",
        "features": rows,
    }


# ─── Recent prediction distribution ─────────────────────────────────
@router.get("/api/model/predictions")
async def predictions_summary(limit: int = Query(default=500, ge=10, le=5000)) -> dict[str, Any]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT p_win, expected_net_R, gate_decision, instrument
        FROM sniper_paper_signals
        WHERE p_win IS NOT NULL AND expected_net_R IS NOT NULL
        ORDER BY decision_ts DESC
        LIMIT $1
        """,
        limit,
    )
    if not rows:
        return {"n": 0, "p_win_hist": [], "ev_hist": [], "by_instrument": {}}

    p = np.array([r["p_win"] for r in rows], dtype=float)
    ev = np.array([r["expected_net_R"] for r in rows], dtype=float)

    def _hist(arr: np.ndarray, bins: int = 20) -> list[dict]:
        if arr.size == 0:
            return []
        counts, edges = np.histogram(arr, bins=bins)
        return [
            {"x_lo": float(edges[i]), "x_hi": float(edges[i + 1]), "count": int(counts[i])}
            for i in range(len(counts))
        ]

    by_inst: dict[str, dict[str, int]] = {}
    for r in rows:
        inst = r["instrument"]
        by_inst.setdefault(inst, {"take": 0, "skip": 0})
        by_inst[inst][r["gate_decision"]] = by_inst[inst].get(r["gate_decision"], 0) + 1

    return {
        "n": int(p.size),
        "p_win_hist": _hist(p),
        "ev_hist": _hist(ev),
        "p_win_mean": float(p.mean()),
        "p_win_std": float(p.std()),
        "ev_mean": float(ev.mean()),
        "ev_std": float(ev.std()),
        "by_instrument": by_inst,
    }


# ─── Feature list + distribution ────────────────────────────────────
@router.get("/api/features/list")
async def feature_list() -> dict[str, Any]:
    m = _load_model()
    return {"features": list(m.feature_order)}


@router.get("/api/features/distribution")
async def feature_distribution(
    feature: str,
    instrument: str | None = None,
    limit: int = Query(default=2000, ge=10, le=20000),
) -> dict[str, Any]:
    pool = await get_pool()
    q = """
        SELECT features, instrument
        FROM sniper_paper_signals
        WHERE features ? $1
    """
    args: list[Any] = [feature]
    if instrument:
        q += " AND instrument = $2"
        args.append(instrument)
    q += f" ORDER BY decision_ts DESC LIMIT {int(limit)}"
    rows = await pool.fetch(q, *args)

    values: list[float] = []
    for r in rows:
        feats = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"])
        v = feats.get(feature)
        if v is None:
            continue
        try:
            fv = float(v)
            if np.isfinite(fv):
                values.append(fv)
        except (TypeError, ValueError):
            continue

    if not values:
        return {"feature": feature, "n": 0, "bins": []}

    arr = np.array(values)
    counts, edges = np.histogram(arr, bins=24)
    bins = [
        {"x_lo": float(edges[i]), "x_hi": float(edges[i + 1]), "count": int(counts[i])}
        for i in range(len(counts))
    ]
    return {
        "feature": feature,
        "instrument": instrument,
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "p05": float(np.quantile(arr, 0.05)),
        "p50": float(np.quantile(arr, 0.50)),
        "p95": float(np.quantile(arr, 0.95)),
        "bins": bins,
    }


@router.get("/api/features/timeseries")
async def feature_timeseries(
    feature: str,
    instrument: str,
    hours: int = Query(default=24, ge=1, le=240),
) -> dict[str, Any]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT decision_ts, features
        FROM sniper_paper_signals
        WHERE instrument = $1 AND decision_ts > NOW() - ($2 || ' hours')::INTERVAL
              AND features ? $3
        ORDER BY decision_ts ASC
        """,
        instrument, str(hours), feature,
    )
    series: list[dict] = []
    for r in rows:
        feats = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"])
        v = feats.get(feature)
        if v is None:
            continue
        try:
            fv = float(v)
            if not np.isfinite(fv):
                continue
        except (TypeError, ValueError):
            continue
        series.append({"ts": r["decision_ts"].isoformat(), "value": fv})
    return {"feature": feature, "instrument": instrument, "series": series}


# ─── SHAP-style single-signal explanation ───────────────────────────
@router.get("/api/signals/{signal_id}/explain")
async def explain_signal(signal_id: int) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT signal_id, decision_ts, instrument, setup_name, side,
               entry_price, stop_price, target_price,
               p_win, expected_net_R, in_distribution,
               gate_decision, gate_reason, features, model_artifact
        FROM sniper_paper_signals
        WHERE signal_id = $1
        """,
        signal_id,
    )
    if row is None:
        raise HTTPException(404, f"signal_id {signal_id} not found")

    m = _load_model()
    feats = row["features"] if isinstance(row["features"], dict) else json.loads(row["features"])

    # Build the input row in the model's expected feature order.
    X = np.array(
        [[float(feats.get(n, np.nan)) if feats.get(n) is not None else np.nan
          for n in m.feature_order]],
        dtype=float,
    )

    explanation = _nn_explain(m, X, feats) if m.is_nn else _lgbm_explain(m, X, feats)
    signal_dict = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in dict(row).items() if k != "features"
    }
    return {"signal": signal_dict, "features": feats, "explanation": explanation}


def _lgbm_explain(m, X: np.ndarray, feats: dict) -> dict[str, Any]:
    try:
        contrib = m.classifier.predict(X, pred_contrib=True)[0]
    except Exception as e:
        raise HTTPException(500, f"Could not compute SHAP contributions: {e}") from e
    bias = float(contrib[-1])
    feature_contribs = [
        {"feature": n, "value": (float(feats.get(n)) if feats.get(n) is not None else None),
         "contribution_to_logit": float(c)}
        for n, c in zip(m.feature_order, contrib[:-1], strict=False)
    ]
    feature_contribs.sort(key=lambda r: abs(r["contribution_to_logit"]), reverse=True)
    return {
        "method": "lightgbm_pred_contrib (exact SHAP for trees)",
        "head": "classifier (logit of p_win)",
        "bias_logit": bias,
        "logit_sum": bias + float(sum(c["contribution_to_logit"] for c in feature_contribs)),
        "feature_contributions": feature_contribs,
    }


def _nn_explain(m, X: np.ndarray, feats: dict) -> dict[str, Any]:
    """Saliency: gradient of p_win w.r.t. each standardised input × input value.
    This is gradient×input attribution — a standard NN explanation."""
    torch = m._torch
    xs = (np.nan_to_num(X, nan=0.0) - m.scaler_mean) / m.scaler_scale
    xt = torch.tensor(xs.astype(np.float32), requires_grad=True)
    out = m.net(xt)
    p = out["p_win"][0]
    m.net.zero_grad()
    p.backward()
    grad = xt.grad.detach().cpu().numpy()[0]          # d p_win / d x_std
    attribution = grad * xs[0]                          # gradient × input
    full = predict_full_safe(m, X)
    feature_contribs = [
        {"feature": n,
         "value": (float(feats.get(n)) if feats.get(n) is not None else None),
         "saliency": float(g),
         "attribution": float(a)}
        for n, g, a in zip(m.feature_order, grad, attribution, strict=False)
    ]
    feature_contribs.sort(key=lambda r: abs(r["attribution"]), reverse=True)
    return {
        "method": "gradient × input saliency (NN)",
        "head": "p_win (post-sigmoid probability)",
        "heads_output": full,
        "feature_contributions": feature_contribs,
    }


def predict_full_safe(m, X: np.ndarray) -> dict:
    from sniper_paper.model.loader import predict_full
    out = predict_full(m, X[0])
    # Drop bulky latent/activations from the explain payload.
    return {k: v for k, v in out.items() if k not in ("latent", "activations")}


# ─── NN internals: training history, architecture, activation trace ─
@router.get("/api/model/nn/history")
async def nn_history() -> dict[str, Any]:
    s = _settings()
    m = _load_model()
    if not m.is_nn:
        return {"backend": "lightgbm", "history": None,
                "note": "Training-loss curves are only available for the NN backend."}
    hist_path = Path(s.model.artifact_dir) / m.artifact_id / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    return {"backend": "neural_network", "artifact_id": m.artifact_id, "history": history}


@router.get("/api/model/nn/architecture")
async def nn_architecture() -> dict[str, Any]:
    m = _load_model()
    if not m.is_nn:
        return {"backend": "lightgbm", "layers": None}
    layers = []
    for name, p in m.net.named_parameters():
        layers.append({"param": name, "shape": list(p.shape), "n": int(p.numel())})
    arch = m.metadata.get("architecture", {})
    return {
        "backend": "neural_network",
        "summary": arch,
        "layers": layers,
        "graph": [
            {"id": "input", "label": f"Input ({len(m.feature_order)})", "kind": "input"},
            *[{"id": f"enc{i}", "label": f"Encoder block {i+1} → {arch.get('hidden')}", "kind": "hidden"}
              for i in range(arch.get("encoder_blocks", 0))],
            {"id": "latent", "label": f"Latent ({arch.get('latent')})", "kind": "latent"},
            {"id": "h_pwin", "label": "p_win (sigmoid)", "kind": "head"},
            {"id": "h_er", "label": "expected_R (linear)", "kind": "head"},
            {"id": "h_mfe", "label": "MFE (softplus)", "kind": "head"},
            {"id": "h_mae", "label": "MAE (softplus)", "kind": "head"},
        ],
    }


@router.get("/api/signals/{signal_id}/activations")
async def signal_activations(signal_id: int) -> dict[str, Any]:
    """Forward the stored feature vector through the NN and return per-layer
    activations + all four head outputs — the internal trace for one decision."""
    m = _load_model()
    if not m.is_nn:
        raise HTTPException(400, "Activation trace is only available for the NN backend.")
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT features FROM sniper_paper_signals WHERE signal_id = $1", signal_id
    )
    if row is None:
        raise HTTPException(404, f"signal_id {signal_id} not found")
    feats = row["features"] if isinstance(row["features"], dict) else json.loads(row["features"])
    X = np.array([[float(feats.get(n, np.nan)) if feats.get(n) is not None else np.nan
                   for n in m.feature_order]], dtype=float)
    from sniper_paper.model.loader import predict_full
    out = predict_full(m, X[0])
    return {
        "signal_id": signal_id,
        "heads": {k: out[k] for k in ("p_win", "expected_R", "mfe", "mae")},
        "latent": out.get("latent"),
        "activations": out.get("activations"),
    }
