"""
Auction-Intelligence (market-profile + order-flow swing agent) validation on the
local 5y Alpaca data — off-prod. This is the platform's FLAGSHIP lane, and unlike
the option lanes it trades the UNDERLYING directly (per the lane map), so the deep
SPY data is valid test input and the platform's OWN trade simulation can be used.

We drive the real GateBValidator: it observes each session's initial balance,
builds the market profile (POC/VAH/VAL/IB), classifies regime, runs the swing agent
to a LONG/SHORT/FLAT decision, then simulates the trade on the session's remaining
bars. We extract those native trade outcomes and normalise to R using the
initial-balance range (the canonical market-profile risk unit), then push through
the same six-gate harness. The tuning lever is agents.swing.min_confidence.

  python -m auction_intelligence.validate_local SPY QQQ
"""
from __future__ import annotations

import dataclasses as dc
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

from analysis.alpaca_data import load_alpaca_rth
from analysis.safe_candles import guard_ohlc
from analysis.walk_forward import validate_strategy
from analytics.regime_hmm import label_daily_regimes
from auction_intelligence.config import clone_default_config
from auction_intelligence.market_profile.engine import MarketBar
from auction_intelligence.validation.gate_b import GateBValidator

OUT_DIR = Path(os.environ.get("AI_OUT", "runtime/validation"))
IS_DAYS = int(os.environ.get("AI_IS_DAYS", "504"))
OOS_DAYS = int(os.environ.get("AI_OOS_DAYS", "126"))
STRIDE_DAYS = int(os.environ.get("AI_STRIDE_DAYS", "126"))

# Tuned grid (Run-2 fixes): regime_gate=True restricts to medium-vol sessions where
# the MP edge concentrates (smoke: +0.125R vs +0.052R) AND gives CSCV two genuinely
# different perf columns so PBO isn't degenerate. (tol_mult tested non-discriminating
# at SPY scale, so dropped — kept lean to keep MinBTL low.)
GRID = {"min_confidence": [0.50, 0.65], "regime_gate": [False, True]}

# NOTE: the swing agent now scales its *_points tolerances natively by price
# (auction_intelligence/agents/base.py::_bounded_tolerance, anchored at NIFTY ~23000),
# so this adapter no longer rescales the config — doing so would double-scale. The
# `_NIFTY_REF_PRICE` constant is kept only for reporting the implied scale.
_NIFTY_REF_PRICE = 23000.0
_TOL_SCALE = 1.0  # reported only; the agent scales internally now

_asd = lambda x: dc.asdict(x) if dc.is_dataclass(x) else x
# session-list cache per frame-slice (building MarketBar sessions is the cost)
_SESS_CACHE: dict = {}
_REGIME_CACHE: dict = {}


def _regime_by_date(frame: pd.DataFrame) -> dict:
    """Map each session date -> vol-regime label (low/medium/high) for the frame slice."""
    t = pd.to_datetime(frame["time"], utc=True)
    key = (t.iloc[0].value, t.iloc[-1].value, len(frame))
    if key in _REGIME_CACHE:
        return _REGIME_CACHE[key]
    daily = frame.assign(_t=t).set_index("_t")["close"].resample("1D").last().dropna()
    try:
        labels = label_daily_regimes(daily)
        out = {pd.Timestamp(idx).tz_convert("America/New_York").date() if pd.Timestamp(idx).tzinfo
               else pd.Timestamp(idx).date(): lab for idx, lab in labels.items()}
    except Exception:  # noqa: BLE001
        out = {}
    _REGIME_CACHE[key] = out
    return out


def _scale_point_tolerances(node, scale: float):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and str(k).endswith("_points"):
                node[k] = float(v) * scale
            else:
                _scale_point_tolerances(v, scale)
    elif isinstance(node, list):
        for v in node:
            _scale_point_tolerances(v, scale)


def _sessions_from_frame(frame: pd.DataFrame):
    t = pd.to_datetime(frame["time"], utc=True)
    key = (t.iloc[0].value, t.iloc[-1].value, len(frame))
    if key in _SESS_CACHE:
        return _SESS_CACHE[key]
    f = frame.copy()
    f["day"] = t.dt.tz_convert("America/New_York").dt.date
    sessions = []
    for _, grp in f.groupby("day"):
        bars = [MarketBar(timestamp=pd.Timestamp(tt).to_pydatetime(), open=float(o), high=float(h),
                          low=float(l), close=float(c), volume=float(v))
                for tt, o, h, l, c, v in zip(grp["time"], grp["open"], grp["high"], grp["low"], grp["close"], grp["volume"])]
        if len(bars) > 30:
            sessions.append(bars)
    _SESS_CACHE[key] = sessions
    return sessions


def _config(min_conf: float) -> dict:
    cfg = clone_default_config()
    # Tolerances scale natively inside the swing agent now (by close_price); no
    # external rescale here (that would double-scale).
    cfg.setdefault("agents", {}).setdefault("swing", {})["min_confidence"] = float(min_conf)
    cfg["agents"]["swing"]["rl_max_min_confidence"] = float(min_conf)
    return cfg


def run_fn(frame: pd.DataFrame, p: dict) -> dict:
    sessions = _sessions_from_frame(frame)
    if not sessions:
        return {"events": [], "summary": {"trades": 0}}
    regime_gate = bool(p.get("regime_gate"))
    reg_by_date = _regime_by_date(frame) if regime_gate else {}
    rep = GateBValidator(_config(p["min_confidence"])).validate(
        symbol="UND", sessions=sessions, mode="backtest", source="alpaca")
    events = []
    for art in rep.artifacts:
        a = _asd(art)
        if a.get("artifact_type") != "gate_b_session":
            continue
        pl = a.get("payload", {})
        tr = pl.get("trade")
        if not tr or pl.get("status") == "skipped":
            continue
        sd = pl.get("session_date")
        if regime_gate:
            try:
                d = pd.Timestamp(sd).date()
            except Exception:  # noqa: BLE001
                d = None
            if reg_by_date.get(d) != "medium":
                continue
        side = tr.get("side")
        dirn = 1 if side == "LONG" else -1
        entry = float(tr.get("entry_price", 0.0)); exit_ = float(tr.get("exit_price", 0.0))
        diag = pl.get("diagnostics", {})
        ib = diag.get("initial_balance_range")
        if ib in (None, 0):
            ib = abs(float(diag.get("initial_balance_high", 0)) - float(diag.get("initial_balance_low", 0)))
        if not ib or ib <= 0:
            ib = max(abs(float(pl.get("vah", entry)) - float(pl.get("val", entry))), entry * 1e-4)
        r = (exit_ - entry) * dirn / ib
        exit_time = (pd.Timestamp(sd, tz="America/New_York") + timedelta(hours=16)).tz_convert("UTC").isoformat() if sd else None
        events.append({"exit_time": exit_time, "side": side, "setup": tr.get("setup_name"),
                       "entry": entry, "exit": exit_, "exit_reason": tr.get("exit_reason"),
                       "r_multiple": round(float(r), 4)})
    rs = [e["r_multiple"] for e in events]
    summary = {"trades": len(events), "total_r": round(sum(rs), 2) if rs else 0.0,
               "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
               "win_rate_pct": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1) if rs else 0.0}
    return {"events": events, "summary": summary}


def _ret(res):
    return [float(e.get("r_multiple", 0.0)) for e in (res.get("events") or [])]


def _times(res):
    return [e.get("exit_time") for e in (res.get("events") or [])]


def validate(symbol: str) -> dict | None:
    global _TOL_SCALE
    _SESS_CACHE.clear(); _REGIME_CACHE.clear()
    raw = load_alpaca_rth(symbol)
    if raw.empty or len(raw) < 20000:
        print(f"{symbol}: SKIP — {len(raw)} bars", flush=True); return None
    raw = guard_ohlc(raw, rth=False)
    _TOL_SCALE = float(raw["close"].median()) / _NIFTY_REF_PRICE
    span = (raw["time"].max() - raw["time"].min()).days
    print(f"{symbol}: {len(raw)} RTH 1m bars, span {span}d ({span/365.25:.2f}y); "
          f"tol_scale {_TOL_SCALE:.4f}; IS{IS_DAYS}/OOS{OOS_DAYS}/stride{STRIDE_DAYS}", flush=True)
    daily_closes = raw.set_index(pd.to_datetime(raw["time"], utc=True))["close"].resample("1D").last().dropna()
    rep = validate_strategy(
        raw, run_fn, GRID, _ret, extract_exit_times=_times, daily_closes=daily_closes,
        time_col="time", is_days=IS_DAYS, oos_days=OOS_DAYS, stride_days=STRIDE_DAYS,
        select="total", target_sr_annual=1.0,
    )
    rep["symbol"] = symbol; rep["strategy"] = "auction_mp_swing"
    return rep


def _summary(rep: dict):
    g = rep["gates"]; wf = rep["walk_forward"]
    print(f"\n=== {rep['symbol']} auction-MP-swing — verdict {rep['verdict']} "
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
    print(f"=== auction-MP-swing LOCAL validation (Alpaca 5y) grid {GRID} ===", flush=True)
    for s in syms:
        try:
            rep = validate(s)
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"{s}: ERROR {exc}", flush=True); continue
        if rep is None:
            continue
        (OUT_DIR / f"auction_{s}_local.json").write_text(json.dumps(rep, indent=1, default=str))
        _summary(rep)
    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    main()
