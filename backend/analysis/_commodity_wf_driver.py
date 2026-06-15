"""Ad-hoc driver: deep-DB commodity MP+OF walk-forward (NET of futures cost).

Bypasses the live broker `_load_history` (which only pulls ~21 days and needs
broker auth) by loading 1-minute MCX futures candles DIRECTLY from
underlying_spot_candles, then runs the SAME per-bar signal evaluator the live
agent uses (via commodity_walkforward.simulate_signal_backtest) and feeds the
resulting trades through analysis.walk_forward.validate_strategy.

Cost honesty: simulate_signal_backtest executes on the underlying futures price
via analysis.signal_backtest.simulate_underlying, which charges cost_bps on
(entry+exit). We DO NOT use the 3% option premium model here (this is a futures
lane). We report at the default 2 bps and at a stressed 5 bps (realistic MCX
futures round-trip incl brokerage+STT+slippage).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

from analysis import commodity_walkforward as cwf
from analysis import walk_forward as wf
from analysis.signal_backtest import simulate_underlying

DSN = os.environ.get("NSE_WF_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
ROOTS = [r.strip() for r in os.environ.get(
    "CWF_ROOTS", "GOLD,SILVERM,CRUDEOIL,NATURALGAS").split(",") if r.strip()]


def load_rows(root: str) -> list[dict]:
    conn = psycopg2.connect(DSN)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM underlying_spot_candles
            WHERE underlying = %s AND interval = '1minute'
            ORDER BY time
            """,
            (root,),
        )
        rows = []
        for t, o, h, l, c, v in cur.fetchall():
            rows.append(
                {
                    "time": pd.Timestamp(t).tz_convert("UTC").isoformat()
                    if pd.Timestamp(t).tzinfo
                    else pd.Timestamp(t, tz="UTC").isoformat(),
                    "open": float(o) if o is not None else None,
                    "high": float(h) if h is not None else None,
                    "low": float(l) if l is not None else None,
                    "close": float(c) if c is not None else None,
                    "volume": float(v) if v is not None else 0.0,
                }
            )
        return rows
    finally:
        conn.close()


def events_to_frame(events: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [e["exit_time"] for e in events],
            "r": [float(e["r_multiple"]) for e in events],
        }
    )


def main() -> None:
    out = {"roots": {}, "note": ""}
    for root in ROOTS:
        rows = load_rows(root)
        norm = cwf.CommodityFuturesWalkForwardRunner._normalize_rows(rows)
        n_sessions = 0
        if norm:
            ist = pd.to_datetime([r["time"] for r in norm], utc=True).tz_convert("Asia/Kolkata")
            n_sessions = len(set(ist.date))
        first = norm[0]["time"] if norm else None
        last = norm[-1]["time"] if norm else None

        # Build the per-bar signal frame ONCE.  The live commodity agent only ever
        # loads ~21 days (DEFAULT_COMMODITY_HISTORY_DAYS) into `closed`, and the MP
        # signal only references the latest session profile + the immediately prior
        # session + ATR(14) + the 09:00 IST anchor — so capping the trailing history
        # window is faithful to live behaviour AND removes the O(n^2) full-history
        # recompute that makes a 30k-bar replay intractable.
        WINDOW = int(os.environ.get("CWF_WINDOW", "6000"))  # ~ last 6 sessions of 1-min bars
        res_default = None
        res_stress = None
        sm2 = {}
        stressed = None
        if norm and len(norm) > 45:
            agent = cwf.CommodityStrategyAgent()
            recs = []
            for index, r in enumerate(norm):
                sig = 0
                atr = 0.0
                if index >= 40:
                    lo = max(0, index - WINDOW + 1)
                    win = norm[lo : index + 1]
                    local_idx = len(win) - 1
                    analysis = cwf._evaluate_mp(
                        agent, symbol=root, rows=win, index=local_idx, prior_cache={}
                    )
                    raw = analysis.get("signal")
                    sig = 1 if raw == "BUY" else (-1 if raw == "SELL" else 0)
                    atr = cwf._safe_float(analysis.get("atr")) or 0.0
                recs.append(
                    {
                        "time": r["time"],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "atr": float(atr),
                        "sig": int(sig),
                    }
                )
            fr = pd.DataFrame.from_records(recs)
            a = fr["atr"].astype(float).where(fr["atr"].astype(float) > 0)
            a = a.ffill().bfill().fillna(fr["close"].astype(float) * 0.01)
            fr["atr"] = a.astype(float)
            # default 2 bps (as the harness ships) and stressed 5 bps (realistic MCX
            # futures round-trip incl brokerage+STT+slippage) — both on the SAME frame.
            res_default = simulate_underlying(fr, sl_atr=1.5, tp_atr=3.0, cost_bps=2.0,
                                              atr_col="atr", signal_col="sig")
            res_stress = simulate_underlying(fr, sl_atr=1.5, tp_atr=3.0, cost_bps=5.0,
                                             atr_col="atr", signal_col="sig")
            sm2 = res_default.get("summary", {})
            stressed = res_stress.get("summary", {})
            stressed["events"] = res_stress.get("events", [])

        # 3) Rolling walk-forward feasibility + a lightweight chronological OOS split.
        #    The validate_strategy harness uses is_days=270 / oos_days=90 by default;
        #    with only ~6 weeks of data that yields ZERO windows, so a rolling
        #    walk-forward is statistically impossible. We record that, and instead do
        #    the only honest thing the depth allows: a single chronological 70/30
        #    in-sample / out-of-sample split of the (cost-5bps) trade series.
        rolling_wf = {"feasible": False,
                      "reason": "only ~6wk MCX 1-min depth; validate_strategy needs >=270d IS -> 0 windows"}
        oos_split = None
        if stressed and stressed.get("events"):
            ev = stressed["events"]
            ev_sorted = sorted(ev, key=lambda e: e["exit_time"])
            r_all = [float(e["r_multiple"]) for e in ev_sorted]
            n = len(r_all)
            if n >= 6:
                cut = int(n * 0.7)
                is_r = r_all[:cut]
                oos_r = r_all[cut:]
                def _stats(xs):
                    if not xs:
                        return {"n": 0, "mean_r": None, "total_r": None, "win_rate_pct": None}
                    arr = np.array(xs, dtype=float)
                    return {"n": len(xs),
                            "mean_r": round(float(arr.mean()), 4),
                            "total_r": round(float(arr.sum()), 2),
                            "win_rate_pct": round(100 * float((arr > 0).mean()), 2)}
                oos_split = {"split_point_time": ev_sorted[cut]["exit_time"] if cut < n else None,
                             "in_sample": _stats(is_r), "out_of_sample": _stats(oos_r)}
        wf_report = {"rolling": rolling_wf, "chrono_70_30": oos_split}
        wf_error = None

        # 4) held-out split sanity (HELD_OUT_START=2026-04-01): all our data is AFTER
        #    that date, so the "development" set would be empty — record that fact.
        heldout_note = None
        if norm:
            t = pd.to_datetime([r["time"] for r in norm], utc=True)
            n_dev = int((t < wf.HELD_OUT_START).sum())
            n_held = int((t >= wf.HELD_OUT_START).sum())
            heldout_note = {"dev_bars": n_dev, "heldout_bars": n_held,
                            "held_out_start": str(wf.HELD_OUT_START.date())}

        out["roots"][root] = {
            "candles": len(norm),
            "sessions": n_sessions,
            "first": first,
            "last": last,
            "cost_2bps": {
                "trades": sm2.get("trades"),
                "win_rate_pct": sm2.get("win_rate_pct"),
                "total_r": sm2.get("total_r"),
                "expectancy_r": sm2.get("expectancy_r"),
            },
            "cost_5bps": (
                {
                    "trades": stressed.get("trades"),
                    "win_rate_pct": stressed.get("win_rate_pct"),
                    "total_r": stressed.get("total_r"),
                    "expectancy_r": stressed.get("expectancy_r"),
                }
                if stressed
                else None
            ),
            "walk_forward": wf_report,
            "walk_forward_error": wf_error,
            "heldout_split": heldout_note,
        }
        sys.stderr.write(f"[done] {root}: {len(norm)} bars / {n_sessions} sessions / "
                         f"{sm2.get('trades')} trades @2bps\n")
        sys.stderr.flush()

    payload = json.dumps(out, indent=2, default=str)
    dest = os.environ.get("CWF_OUT", "/tmp/cwf_result.json")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(payload)
    sys.stderr.write(f"[written] {dest}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
