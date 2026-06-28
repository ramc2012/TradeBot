"""Measured ride/exit framework over the fade entry -> OOS walk-forward.

Builds on backtest_fade (same fade entry, same costs) but replaces the naive
time-stop/target/trail with the EXIT signals the IC study measured as predictive,
each toggleable so we can ablate and walk-forward them:
  * return-at-horizon time-stop (IC +0.81): at horizon bar, cut ONLY if not
    adequately in profit (vs the blunt always-cut time-stop);
  * delta-drift HOLD (IC +0.17): if |delta| has risen (drifting ITM) the trade is
    working -> suppress the time-stop, let it ride;
  * IV-crush HOLD-override: a losing bar that is IV-driven (iv < entry_iv) while
    still directional (|delta| intact) tends to recover -> skip protective exits;
  * theta-velocity exit (IC -0.26 at 0DTE): cut when |theta|/premium per bar is
    too high (decay dominates).

CLI:  python -m directional_options.backtest_fade_exits            # named-config compare
      python -m directional_options.backtest_fade_exits '<json>'   # one config -> OOS stats JSON
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.backtest_fade import (
    ATR_WIN, EXT_GATE, IST, IV_GATE_PCT, LOT, OPEN_WINDOW_BARS, SESSION_END,
    SESSION_START, _atr, _round_trip_cost, _stats,
)


async def _load(u: str) -> pd.DataFrame:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, expiry, strike, option_type, close, delta, iv, theta, underlying_price
                    FROM option_premium_candles
                    WHERE underlying = :u AND interval = '30minute'
                      AND close IS NOT NULL AND close > 0 AND delta IS NOT NULL AND underlying_price IS NOT NULL
                    ORDER BY time
                    """
                ),
                {"u": u},
            )
        ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "expiry", "strike", "option_type", "close", "delta", "iv", "theta", "spot"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    for c in ("strike", "close", "delta", "iv", "theta", "spot"):
        df[c] = df[c].astype(float)
    df["date"] = df["time"].dt.date
    t = df["time"].dt.time
    return df[(t >= SESSION_START) & (t <= SESSION_END)]


def _entries(df: pd.DataFrame) -> list[dict]:
    groups = {k: g.sort_values("time") for k, g in df.groupby(["date", "option_type"])}
    spot = df.groupby(["date", "time"])["spot"].first().reset_index().sort_values("time")
    spot["atr"] = _atr(spot["spot"], ATR_WIN)
    recs, iv_hist = [], []
    for d, ds in spot.groupby("date"):
        ds = ds.sort_values("time").reset_index(drop=True)
        if len(ds) < OPEN_WINDOW_BARS + 2:
            continue
        so = ds["spot"].iloc[0]
        ow = ds.iloc[:OPEN_WINDOW_BARS].copy()
        ow["ext"] = (ow["spot"] - so) / ow["atr"].replace(0, np.nan)
        ow = ow.dropna(subset=["ext"])
        if ow.empty:
            continue
        r = ow.loc[ow["ext"].abs().idxmax()]
        if abs(r["ext"]) < EXT_GATE:
            continue
        et, side = r["time"], ("PE" if r["ext"] > 0 else "CE")
        g = groups.get((d, side))
        if g is None or g.empty:
            continue
        ae = g[g["time"] == et]
        if ae.empty:
            continue
        exp = ae["expiry"].min()
        cand = ae[ae["expiry"] == exp].copy()
        cand["dd"] = (cand["delta"].abs() - 0.5).abs()
        pk = cand.loc[cand["dd"].idxmin()]
        strike, epx, ed, eiv = pk["strike"], float(pk["close"]), float(pk["delta"]), float(pk["iv"])
        if epx <= 0:
            continue
        path = g[(g["expiry"] == exp) & (g["strike"] == strike) & (g["time"] > et)].sort_values("time")
        steps = [(float(b["close"]), float(b["delta"]), float(b["iv"]), float(b["theta"])) for _, b in path.iterrows()]
        iv_pct = float(np.mean([1.0 if eiv >= h else 0.0 for h in iv_hist])) if len(iv_hist) >= 50 else 0.5
        iv_hist.append(eiv)
        recs.append({"date": str(d), "entry_px": epx, "entry_delta": ed, "entry_iv": eiv, "iv_pct": iv_pct, "path": steps})
    return recs


def _nz(x: float) -> bool:
    return x == x  # not NaN


def _sim(rec: dict, cfg: dict) -> tuple[float, float]:
    e, ed, eiv = rec["entry_px"], abs(rec["entry_delta"]), rec["entry_iv"]
    peak = exit_px = e
    for i, (px, dl, iv, th) in enumerate(rec["path"], start=1):
        peak = max(peak, px)
        exit_px = px
        ret = (px - e) / e
        ddrift = (abs(dl) - ed) if _nz(dl) else 0.0
        iv_crush = bool(cfg.get("iv_crush_hold")) and ret < 0 and _nz(iv) and iv < eiv and (_nz(dl) and abs(dl) >= cfg.get("iv_crush_min_delta", 0.35))
        if cfg.get("target") and ret >= cfg["target"]:
            break
        if cfg.get("stop") and ret <= -cfg["stop"] and not iv_crush:
            break
        if cfg.get("theta_knee") and _nz(th) and px > 0 and abs(th) / px > cfg["theta_knee"] and not iv_crush:
            break
        if cfg.get("trail") and peak >= e * (1 + cfg.get("trail_arm", 0.20)) and px <= peak * (1 - cfg["trail"]):
            break
        if cfg.get("hard_time") and i >= cfg["hard_time"]:
            break  # blunt time-stop (naive baseline)
        if cfg.get("horizon") and i >= cfg["horizon"]:
            working = bool(cfg.get("delta_drift_hold")) and ddrift > 0
            if ret < cfg.get("horizon_min", 0.08) and not working and not iv_crush:
                break  # return-at-horizon: cut what isn't working
            if i >= cfg.get("max_hold", 10):
                break
    return e, exit_px


def _eval(recs: list[dict], cfg: dict, u: str, split) -> tuple[dict, dict]:
    lot = LOT.get(u, 75)
    tr, te = [], []
    for r in recs:
        if r["iv_pct"] > IV_GATE_PCT:
            continue
        e, x = _sim(r, cfg)
        net = (x - e) * lot - _round_trip_cost(e, x, lot, u)
        (tr if pd.Timestamp(r["date"]).date() < split else te).append({"net": net})
    return _stats(tr), _stats(te)


CONFIGS = {
    "naive_ts2 (shown)":   {"hard_time": 2, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "ret_stop":            {"horizon": 2, "horizon_min": 0.08, "max_hold": 8, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "ret_stop+ddrift":     {"horizon": 2, "horizon_min": 0.08, "max_hold": 8, "delta_drift_hold": True, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "ret_stop+ivcrush":    {"horizon": 2, "horizon_min": 0.08, "max_hold": 8, "iv_crush_hold": True, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "ret_stop+theta":      {"horizon": 2, "horizon_min": 0.08, "max_hold": 8, "theta_knee": 0.10, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "measured_full":       {"horizon": 2, "horizon_min": 0.08, "max_hold": 8, "delta_drift_hold": True, "iv_crush_hold": True, "theta_knee": 0.10, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "measured_h3":         {"horizon": 3, "horizon_min": 0.08, "max_hold": 10, "delta_drift_hold": True, "iv_crush_hold": True, "theta_knee": 0.10, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "measured_hmin12":     {"horizon": 2, "horizon_min": 0.12, "max_hold": 8, "delta_drift_hold": True, "iv_crush_hold": True, "theta_knee": 0.10, "target": 0.40, "stop": 0.35, "trail": 0.20},
    "measured_hmin05":     {"horizon": 2, "horizon_min": 0.05, "max_hold": 8, "delta_drift_hold": True, "iv_crush_hold": True, "theta_knee": 0.12, "target": 0.40, "stop": 0.35, "trail": 0.15},
}


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] not in ("compare",) else None
    one_cfg = json.loads(arg) if arg else None
    for u in ("NIFTY", "BANKNIFTY"):
        df = await _load(u)
        if df.empty:
            continue
        recs = _entries(df)
        dates = sorted({pd.Timestamp(r["date"]).date() for r in recs})
        split = dates[int(len(dates) * 0.6)]
        if one_cfg is not None:
            s_tr, s_te = _eval(recs, one_cfg, u, split)
            print(json.dumps({"underlying": u, "split": str(split), "train": s_tr, "test": s_te}))
            continue
        print(f"\n==== {u}  (train/test split {split}, n_entries={len(recs)}) ====")
        rob = []
        for name, cfg in CONFIGS.items():
            s_tr, s_te = _eval(recs, cfg, u, split)
            if s_te.get("n", 0) > 20:
                rob.append(s_te["profit_factor"])
            print(f"  {name:20} train PF={s_tr.get('profit_factor',0):.2f} (n{s_tr.get('n',0)}) | "
                  f"TEST PF={s_te.get('profit_factor',0):.2f} tot={s_te.get('total','?')} win={s_te.get('win_pct','?')}% (n{s_te.get('n',0)})")
        if rob:
            print(f"  robust: {sum(1 for p in rob if p > 1)}/{len(rob)} configs TEST PF>1; "
                  f"median {np.median(rob):.2f}, max {max(rob):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
