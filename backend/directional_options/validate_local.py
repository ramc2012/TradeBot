"""
Directional-options SIGNAL validation on the local 5y Alpaca data (off-prod).

The full DirectionalOptions backtester needs NSE option premium candles, so it
cannot run on SPY. But the ALPHA — DirectionalSignalEngine.predict — is
instrument-agnostic on underlying OHLC features (EMA spread / ADX / DI / breakout /
momentum / regime). We drive that signal engine over the deep underlying data and
execute it with the neutral ATR-stop model (analysis.signal_backtest), pushing the
result through the same six-gate harness. This tests whether the directional signal
carries an out-of-sample edge at statistical depth — decoupled from the (separately
validated, NSE-specific) option-selection/risk layer.

  python -m directional_options.validate_local SPY QQQ
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

from analysis.alpaca_data import load_alpaca_rth
from analysis.safe_candles import guard_ohlc
from analysis.signal_backtest import simulate_underlying
from analysis.walk_forward import validate_strategy
from directional_options.config import clone_default_config
from directional_options.features import FeatureEngine
from directional_options.regime import RegimeClassifier
from directional_options.signals import DirectionalSignalEngine

TF = os.environ.get("DIR_TF", "15minute")
OUT_DIR = Path(os.environ.get("DIR_OUT", "runtime/validation"))
IS_DAYS = int(os.environ.get("DIR_IS_DAYS", "504"))
OOS_DAYS = int(os.environ.get("DIR_OOS_DAYS", "126"))
STRIDE_DAYS = int(os.environ.get("DIR_STRIDE_DAYS", "126"))

GRID = {
    "min_confidence": [0.62, 0.70, 0.78],  # within the live conf distribution (p50=.70, p90=.85)
    "sl_atr": [1.0, 1.5],
    "tp_atr": [2.0, 3.0],
    "regime_gate": [False, True],
}

_CFG = clone_default_config()
_FE = FeatureEngine(_CFG["feature_engine"])
_RC = RegimeClassifier()
_SE = DirectionalSignalEngine(_CFG["signal_engine"])
_BASE_CACHE: dict = {}


def _base_signal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Feature-build + per-bar predict, cached per frame-slice (params-independent)."""
    t = pd.to_datetime(frame["time"], utc=True)
    key = (t.iloc[0].value, t.iloc[-1].value, len(frame))
    cached = _BASE_CACHE.get(key)
    if cached is not None:
        return cached
    ff = _FE.build_frame(frame, TF)
    base_dir, conf, allowed = [], [], []
    for _, row in ff.iterrows():
        reg = _RC.classify(row, timeframe=TF)
        sig = _SE.predict(row, reg, TF)
        if sig is None:
            base_dir.append(0); conf.append(0.0); allowed.append(bool(getattr(reg, "trade_allowed", True)))
        else:
            base_dir.append(1 if sig.direction == "CE" else -1)
            conf.append(float(sig.confidence)); allowed.append(bool(getattr(reg, "trade_allowed", True)))
    ff = ff.assign(_dir=base_dir, _conf=conf, _allowed=allowed)
    _BASE_CACHE[key] = ff
    return ff


def run_fn(frame: pd.DataFrame, p: dict) -> dict:
    ff = _base_signal_frame(frame).copy()
    gate = ff["_conf"] >= p["min_confidence"]
    if p.get("regime_gate"):
        gate = gate & ff["_allowed"]
    ff["sig"] = (ff["_dir"] * gate.astype(int)).astype(int)
    return simulate_underlying(ff, sl_atr=p["sl_atr"], tp_atr=p["tp_atr"], atr_col="atr")


def _ret(res):
    return [float(e.get("r_multiple", 0.0)) for e in (res.get("events") or [])]


def _times(res):
    return [e.get("exit_time") for e in (res.get("events") or [])]


def validate(symbol: str) -> dict | None:
    _BASE_CACHE.clear()
    raw = load_alpaca_rth(symbol)
    if raw.empty or len(raw) < 20000:
        print(f"{symbol}: SKIP — {len(raw)} bars", flush=True); return None
    raw = guard_ohlc(raw, rth=False)
    span = (raw["time"].max() - raw["time"].min()).days
    print(f"{symbol}: {len(raw)} RTH 1m bars, span {span}d ({span/365.25:.2f}y), tf {TF}; "
          f"IS{IS_DAYS}/OOS{OOS_DAYS}/stride{STRIDE_DAYS}", flush=True)
    daily_closes = raw.set_index(pd.to_datetime(raw["time"], utc=True))["close"].resample("1D").last().dropna()
    rep = validate_strategy(
        raw, run_fn, GRID, _ret, extract_exit_times=_times, daily_closes=daily_closes,
        time_col="time", is_days=IS_DAYS, oos_days=OOS_DAYS, stride_days=STRIDE_DAYS,
        select="total", target_sr_annual=1.0,
    )
    rep["symbol"] = symbol; rep["strategy"] = "directional_signal"; rep["timeframe"] = TF
    return rep


def _summary(rep: dict):
    g = rep["gates"]; wf = rep["walk_forward"]
    print(f"\n=== {rep['symbol']} directional-signal {TF} — verdict {rep['verdict']} "
          f"({rep['gates_passed']}/{rep['gates_total']}) ===", flush=True)
    print(f"  OOS trades {rep['metrics']['trades']} | WFE {wf['wfe_median']} | DSR {rep['deflated_sharpe']:.3f} "
          f"| PBO {rep['pbo'].get('pbo')} | MC_SR_p05 {rep['monte_carlo'].get('sharpe_p05')} "
          f"| MinBTL {rep['min_backtest_length_years']:.2f}y vs have {rep['backtest_years']}y "
          f"| n_trials {rep['n_trials']} | windows {wf['n_windows']}", flush=True)
    print("  OOS metrics:", {k: rep['metrics'].get(k) for k in ('trades', 'sharpe', 'total', 'win_rate', 'profit_factor', 'expectancy')}, flush=True)
    print("  per-window IS-best:", [w['best_params'] for w in wf['per_window']], flush=True)
    for k, gg in g.items():
        print(f"    {k:<18} {'PASS' if gg['pass'] else 'fail'}  {gg.get('value')}", flush=True)


def main():
    syms = sys.argv[1:] or ["SPY"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== directional-signal LOCAL validation (Alpaca 5y, tf {TF}) grid {GRID} ===", flush=True)
    for s in syms:
        try:
            rep = validate(s)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"{s}: ERROR {exc}", flush=True); continue
        if rep is None:
            continue
        (OUT_DIR / f"directional_{s}_local.json").write_text(json.dumps(rep, indent=1, default=str))
        _summary(rep)
    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    main()
