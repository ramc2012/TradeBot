"""
Cost-HONEST, OPTION-LEG-HONEST evaluation of the Gann TP-Delta lane.

WHY THIS FILE EXISTS
--------------------
Gann is the ONLY lane in this codebase with gross-positive numbers (NIFTY
+0.246R/trade, SENSEX +0.231R gross). But its shipped backtest
(gann_tp_delta/backtest.py) measures those R-multiples on the UNDERLYING spot and
charges ZERO transaction cost. The live agent does NOT trade the underlying — for
indices it buys the ATM option, and that option leg carries the ~3% round-trip
friction wall (analysis/cost_model.py) that has killed every other lane
(32/32-OOS-negative history; index-option MACD held-out -17.7%/trade).

So Gann's gross-on-underlying edge has never been tested the honest way:
  (1) on the actual instrument traded (the ATM CE/PE premium series), and
  (2) net of the shared ~3% cost model, and
  (3) on the SACRED held-out block (>= HELD_OUT_START), scored exactly once.

This module does exactly that, WITHOUT mutating the shipped gann backtest:

  * SIGNAL: reuse gann_tp_delta.GannTPDeltaBacktester unchanged. It produces, per
    trade, the entry_time / exit_time / side it took on the underlying using the
    real evaluate_gann_signal regime-gated logic + live exit rules. We take those
    as the timing/direction truth (the underlying signal).
  * EXPRESSION: map each gann trade to the ATM option leg it actually trades —
    CE for a long, PE for a short — picked exactly like analysis/
    nse_option_walkforward.py: strike nearest spot at entry, front non-expiry
    expiry, premium read from option_premium_candles (no-arb guarded). Entry at
    the gann entry bar, exit at the gann exit bar (same timing the underlying
    trade used; the gann stop/target/trail decided WHEN to be flat).
  * COST: every trade netted through the ONE shared cost model
    (analysis.cost_model.NSE_OPTION_COST, ~3% round-trip premium).
  * VALIDATION: split the gann trade list at HELD_OUT_START (walk_forward.
    split_development_heldout) and report the held-out block net-of-cost ONCE.
    Also prints the development block and a long-ATM baseline for context.

It prints, side by side: gann GROSS-on-underlying R (sanity vs the shipped
number), gann NET-on-OPTION-leg return, and the long-ATM MACD baseline, on both
the development and the sacred held-out block.

Run (local stack, in the backend container):
  docker exec -e NSE_WF_DSN=postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie \
      -w /app -e PYTHONPATH=/app nomadcurie_backend \
      python -m analysis.gann_cost_honest_eval --underlying NIFTY

NB on data: option_premium_candles 30m coverage starts ~2024-12 for NIFTY but
only ~2026-03 for SENSEX. The gann SIGNAL still runs over the full deep spot
history; trades whose entry bar has no ATM option quote are simply unmappable and
excluded from the option-leg P&L (counted + reported so the mapping yield is
explicit). The held-out block (>= 2026-04-01) has option coverage for both names,
so the load-bearing held-out verdict is scoreable for NIFTY and SENSEX.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from analysis.cost_model import NSE_OPTION_COST
from analysis import walk_forward as wf
from analysis import validation_metrics as vm
from analysis.safe_candles import guard_ohlc, guard_option_ohlc
from analysis.macd_engine import compute_ema, compute_macd
from analytics.technicals import compute_adx
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config

LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, "FINNIFTY": 65, "MIDCPNIFTY": 120}
DEFAULT_DSN = os.environ.get("NSE_WF_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")


# ── data loading (deep spot for the gann signal; option chain for the leg) ───
async def _load(dsn: str, underlying: str):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # Deep 1-minute spot drives the gann feature frame (15m resample).
        spot_rows = await conn.fetch(
            "SELECT time, open, high, low, close, COALESCE(volume,0) volume "
            "FROM underlying_spot_candles WHERE underlying=$1 AND interval='1minute' ORDER BY time",
            underlying,
        )
        # 30-minute option premium = the densest option series in this DB.
        opt_rows = await conn.fetch(
            "SELECT time, open, high, low, close, strike, option_type, underlying_price, expiry "
            "FROM option_premium_candles WHERE underlying=$1 AND interval='30minute' ORDER BY time",
            underlying,
        )
    finally:
        await conn.close()

    spot = pd.DataFrame([dict(r) for r in spot_rows])
    for c in ("open", "high", "low", "close", "volume"):
        spot[c] = pd.to_numeric(spot[c], errors="coerce")
    spot = guard_ohlc(spot, band=0.20, rth=True)  # UTC tz-aware time out

    opt = pd.DataFrame([dict(r) for r in opt_rows])
    for c in ("open", "high", "low", "close", "strike", "underlying_price"):
        opt[c] = pd.to_numeric(opt[c], errors="coerce")
    opt = guard_option_ohlc(opt, rth=True)
    opt["expiry"] = pd.to_datetime(opt["expiry"]).dt.date
    return spot, opt


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    pc = frame["close"].shift(1)
    tr = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - pc).abs(), (frame["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean().fillna(0.0)


def build_gann_frame(spot_utc: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Build the 15-minute gann feature frame with the SAME compute_ema /
    compute_adx / ATR the live FeatureEngine + tune_sweep use, but keep the
    `time` column UTC tz-aware (tune_sweep makes it IST-naive). Keeping UTC lets
    the gann trade timestamps map straight onto the UTC option book.

    Resampling 1m→15m on UTC vs IST-naive yields IDENTICAL bins: the 5h30m IST
    offset is 330 minutes = 22 * 15, an exact multiple of 15, so the 15-minute
    bin boundaries coincide. The gann geometry is bar-index/price driven, so the
    signal is unchanged from the shipped path.
    """
    df = spot_utc.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if "oi" not in df.columns:
        df["oi"] = 0.0
    indexed = df.set_index("time").sort_index()
    f = (
        indexed.resample("15min", label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    if f.empty or len(f.index) < 60:
        return f
    fe = cfg["feature_engine"]
    closes, highs, lows = f["close"].tolist(), f["high"].tolist(), f["low"].tolist()
    f["ema_fast"] = pd.Series(compute_ema(closes, int(fe["ema_fast"])), dtype="float64")
    f["ema_slow"] = pd.Series(compute_ema(closes, int(fe["ema_slow"])), dtype="float64")
    adx, _, _ = compute_adx(highs, lows, closes, int(fe["adx_period"]))
    f["adx"] = pd.Series(adx, dtype="float64")
    f["atr"] = _atr(f, int(fe["atr_period"]))
    warmup = int(fe["warmup_bars"])
    if len(f.index) > warmup:
        f = f.iloc[warmup:].reset_index(drop=True)
    return f


# ── ATM option book (identical selection to nse_option_walkforward) ──────────
@dataclass
class OptionBook:
    by_expiry_type: dict = field(default_factory=dict)  # (expiry,'CE'/'PE') -> {strike: df}
    expiries: list = field(default_factory=list)

    @classmethod
    def build(cls, opt: pd.DataFrame) -> "OptionBook":
        book = cls()
        for (exp, typ), g in opt.groupby(["expiry", "option_type"]):
            strikes = {}
            for strike, gs in g.groupby("strike"):
                s = gs.sort_values("time").drop_duplicates("time", keep="last").set_index("time")
                strikes[float(strike)] = s[["open", "close"]]
            book.by_expiry_type[(exp, str(typ).upper())] = strikes
        book.expiries = sorted({e for (e, _) in book.by_expiry_type})
        return book

    def active_expiry(self, ts: pd.Timestamp, min_days: int = 2, max_days: int = 45):
        d = ts.date()
        cands = [e for e in self.expiries if min_days <= (e - d).days <= max_days]
        return min(cands, key=lambda e: (e - d).days) if cands else None

    def atm_leg(self, ts: pd.Timestamp, spot: float, option_type: str):
        exp = self.active_expiry(ts)
        if exp is None:
            return None
        strikes = self.by_expiry_type.get((exp, option_type))
        if not strikes:
            return None
        atm = min(strikes, key=lambda k: abs(k - spot))
        return atm, strikes[atm]


def _premium_at(leg_df: pd.DataFrame, ts: pd.Timestamp, col: str, tol_min: int = 31) -> Optional[float]:
    """Nearest option bar to `ts` within tol_min minutes. Gann signals stamp on
    15m bars and the option series is 30m, so an exact match would drop almost
    everything; nearest-within-tolerance is the same convention
    nse_option_walkforward uses (there 31 min for 30m bars)."""
    if leg_df.empty:
        return None
    pos = leg_df.index.get_indexer([ts], method="nearest", tolerance=pd.Timedelta(minutes=tol_min))
    if pos[0] == -1:
        return None
    v = leg_df.iloc[pos[0]][col]
    return float(v) if pd.notna(v) and float(v) > 0 else None


# ── map gann underlying trades → ATM option-leg net-of-cost returns ──────────
def map_trades_to_option_leg(
    gann_events: list,
    *,
    book: OptionBook,
    underlying: str,
    apply_cost: bool = True,
    opt_tol_min: int = 45,
) -> dict:
    """For each gann trade (entry_time, exit_time, side, entry-underlying-price),
    buy the ATM CE (long) / PE (short) at entry, sell at exit, net of the shared
    cost model. Returns per-trade net option returns + diagnostics.

    `entry` in the gann event is the underlying close at entry → used only to
    pick the ATM strike, never as the option premium. The premium comes from the
    option book. opt_tol_min is wider (45) than the 30m bar because the gann
    15m exit stamp can land between two 30m option bars.
    """
    lot = LOT.get(underlying.upper(), 50)
    out_trades = []
    n_total = len(gann_events)
    n_no_leg = n_no_entry = n_no_exit = 0
    for ev in gann_events:
        side = ev["side"]
        otype = "CE" if side == "long" else "PE"
        entry_ts = pd.Timestamp(ev["entry_time"])
        exit_ts = pd.Timestamp(ev["exit_time"])
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        spot_at_entry = float(ev["entry"])
        leg = book.atm_leg(entry_ts, spot_at_entry, otype)
        if leg is None:
            n_no_leg += 1
            continue
        strike, leg_df = leg
        entry_px = _premium_at(leg_df, entry_ts, "open", tol_min=opt_tol_min)
        if entry_px is None:
            n_no_entry += 1
            continue
        exit_px = _premium_at(leg_df, exit_ts, "close", tol_min=opt_tol_min)
        if exit_px is None:
            n_no_exit += 1
            continue
        gross = exit_px / entry_px - 1.0
        cost = NSE_OPTION_COST.round_trip_pct(entry_px, lot_qty=lot) if apply_cost else 0.0
        out_trades.append({
            "time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost,
            "gross_ret": gross, "side": side, "otype": otype, "strike": strike,
            "entry": entry_px, "exit": exit_px,
            "r_multiple_underlying": float(ev.get("r_multiple", 0.0)),
        })
    return {
        "trades": out_trades,
        "diag": {
            "gann_trades": n_total,
            "mapped": len(out_trades),
            "dropped_no_atm_leg": n_no_leg,
            "dropped_no_entry_quote": n_no_entry,
            "dropped_no_exit_quote": n_no_exit,
            "map_yield_pct": round(100.0 * len(out_trades) / n_total, 1) if n_total else 0.0,
        },
    }


# ── long-ATM MACD baseline on the option leg (the established benchmark) ──────
def macd_long_atm_baseline(spot_utc: pd.DataFrame, *, book: OptionBook, underlying: str,
                           time_stop_bars: int = 13, hard_stop_pct: float = 0.25) -> list:
    """The baseline every experiment is judged against: MACD zero-cross long-ATM,
    next-bar option execution on the SAME 30m grid + cost model. This is a
    self-contained re-implementation of analysis/nse_option_walkforward.run_trades
    so the comparison uses the identical 30m option book built here. Resamples the
    deep 1m spot to 30m to match the option grid."""
    df = spot_utc.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s30 = (
        df.set_index("time").sort_index()
        .resample("30min", label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna(subset=["open", "high", "low", "close"]).reset_index()
    )
    if len(s30) < 35:
        return []
    times = pd.to_datetime(s30["time"], utc=True).tolist()
    closes = s30["close"].astype(float).tolist()
    macd, sig, _ = compute_macd(closes, 12, 26, 9)
    lot = LOT.get(underlying.upper(), 50)

    def crossed(i):
        if i < 1 or macd[i] is None or sig[i] is None or macd[i - 1] is None or sig[i - 1] is None:
            return 0
        prev, cur = macd[i - 1] - sig[i - 1], macd[i] - sig[i]
        if prev <= 0 < cur:
            return +1
        if prev >= 0 > cur:
            return -1
        return 0

    trades, i, n = [], 1, len(s30)
    while i < n - 1:
        d = crossed(i)
        if d == 0:
            i += 1
            continue
        otype = "CE" if d > 0 else "PE"
        entry_ts = times[i + 1]
        leg = book.atm_leg(entry_ts, closes[i], otype)
        if leg is None:
            i += 1
            continue
        _strike, leg_df = leg
        entry_px = _premium_at(leg_df, entry_ts, "open")
        if entry_px is None:
            i += 1
            continue
        exit_px, exit_ts = None, entry_ts
        for j in range(i + 1, min(i + 1 + time_stop_bars, n)):
            px = _premium_at(leg_df, times[j], "close")
            if px is None:
                continue
            exit_px, exit_ts = px, times[j]
            if px <= entry_px * (1 - hard_stop_pct):
                break
            if crossed(j) == -d:
                break
        if exit_px is None:
            i += 1
            continue
        gross = exit_px / entry_px - 1.0
        cost = NSE_OPTION_COST.round_trip_pct(entry_px, lot_qty=lot)
        trades.append({"time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost})
        while i < n - 1 and times[i] <= exit_ts:
            i += 1
    return trades


# ── gann runner over a spot slice → trade events (uses shipped backtester) ───
def run_gann_events(gann_frame: pd.DataFrame, cfg: dict, *, entry_conviction: float) -> list:
    """Run the SHIPPED GannTPDeltaBacktester over a (time-sliced) gann feature
    frame and return its trade events (entry_time/exit_time/side/entry/r_multiple).
    max_events is lifted so the full trade list is returned, not truncated."""
    if gann_frame is None or len(gann_frame.index) < 45:
        return []
    c = clone_default_config()
    # The shipped backtester TRUNCATES the frame to the last `max_bars` 15m bars
    # (default 260 ≈ a few weeks) and caps the returned trade list at max_events.
    # For an honest full-history dev/held-out split we must lift BOTH so gann runs
    # over the entire deep frame and returns every trade.
    c.setdefault("backtest", {})["max_events"] = 1_000_000
    c["backtest"]["max_bars"] = 10_000_000
    bt = GannTPDeltaBacktester(c)
    res = bt.run(gann_frame.reset_index(drop=True), anchor_mode="auto_pivot",
                 h_mode="median_tpd", entry_conviction=entry_conviction)
    return list(res.get("events") or [])


# ── summary ──────────────────────────────────────────────────────────────────
def _summ(rets: list) -> dict:
    r = np.asarray(list(rets), dtype=float)
    if r.size == 0:
        return {"n": 0}
    pf = float("nan")
    if (r < 0).any() and (r > 0).any():
        pf = float(r[r > 0].sum() / -r[r < 0].sum())
    return {
        "n": int(r.size),
        "total_ret": round(float(r.sum()), 3),
        "mean_ret_pct": round(float(r.mean()) * 100, 3),
        "win_rate": round(float((r > 0).mean()), 3),
        "sharpe": round(vm.sharpe(r), 3),
        "expectancy": round(vm.expectancy(r), 4),
        "pf": round(pf, 3),
    }


def _split_events_by_time(events: list, key: str = "entry_time"):
    """Split gann events into (dev, held) at HELD_OUT_START on entry_time."""
    dev, held = [], []
    for ev in events:
        t = pd.Timestamp(ev[key])
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        (held if t >= wf.HELD_OUT_START else dev).append(ev)
    return dev, held


async def main_async(underlying: str, dsn: str, conviction: float):
    print(f"=== Gann COST-HONEST option-leg eval — {underlying} ===", flush=True)
    spot, opt = await _load(dsn, underlying)
    print(f"spot 1m bars (guarded): {len(spot)} "
          f"({pd.to_datetime(spot['time']).min().date()}..{pd.to_datetime(spot['time']).max().date()}); "
          f"option 30m rows (guarded): {len(opt)}; expiries: {opt['expiry'].nunique()}", flush=True)
    book = OptionBook.build(opt)

    cfg = clone_default_config()
    gann_frame = build_gann_frame(spot, cfg)
    if gann_frame.empty:
        print("gann frame empty — abort.", flush=True)
        return
    print(f"gann 15m feature bars: {len(gann_frame)} "
          f"({gann_frame['time'].min().date()}..{gann_frame['time'].max().date()})", flush=True)

    # 1) FULL-history gann run (shipped backtester, shipped 5.0 floor) → events.
    #    Sanity-check the GROSS-on-underlying number vs the documented gann edge.
    events = run_gann_events(gann_frame, cfg, entry_conviction=conviction)
    gross_R = [float(e.get("r_multiple", 0.0)) for e in events]
    print(f"\ngann trades (full history, floor={conviction}): {len(events)} | "
          f"GROSS-on-underlying mean R = {round(float(np.mean(gross_R)), 4) if gross_R else 'n/a'} "
          f"total R = {round(float(np.sum(gross_R)), 2) if gross_R else 'n/a'}", flush=True)

    # 2) Map every gann trade to its ATM option leg, net of cost.
    mapped = map_trades_to_option_leg(events, book=book, underlying=underlying, apply_cost=True)
    print(f"option-leg mapping: {mapped['diag']}", flush=True)
    opt_trades = mapped["trades"]

    # split the mapped option-leg trades + the gann gross trades at HELD_OUT_START
    dev_opt, held_opt = [], []
    for t in opt_trades:
        (held_opt if t["time"] >= wf.HELD_OUT_START else dev_opt).append(t)
    dev_ev, held_ev = _split_events_by_time(events)

    print(f"\n--- DEVELOPMENT block (entry < {wf.HELD_OUT_START.date()}) ---", flush=True)
    print(f"  gann GROSS-on-underlying R : {_summ([float(e.get('r_multiple',0.0)) for e in dev_ev])}", flush=True)
    print(f"  gann NET-on-OPTION (cost ON): {_summ([t['net_ret'] for t in dev_opt])}", flush=True)
    print(f"  gann GROSS-on-OPTION (cost OFF): {_summ([t['gross_ret'] for t in dev_opt])}", flush=True)

    # 3) SACRED held-out — score ONCE.
    print(f"\n=== HELD-OUT block (entry >= {wf.HELD_OUT_START.date()}) — scored ONCE ===", flush=True)
    print(f"  gann GROSS-on-underlying R : {_summ([float(e.get('r_multiple',0.0)) for e in held_ev])}", flush=True)
    held_net = _summ([t["net_ret"] for t in held_opt])
    print(f"  gann NET-on-OPTION (cost ON): {held_net}   <-- THE VERDICT NUMBER", flush=True)
    print(f"  gann GROSS-on-OPTION (cost OFF): {_summ([t['gross_ret'] for t in held_opt])}", flush=True)

    # 4) Long-ATM MACD baseline on the same option book, both blocks.
    base = macd_long_atm_baseline(spot, book=book, underlying=underlying)
    base_dev = [b for b in base if b["time"] < wf.HELD_OUT_START]
    base_held = [b for b in base if b["time"] >= wf.HELD_OUT_START]
    print(f"\n--- long-ATM MACD baseline (cost ON, same option book) ---", flush=True)
    print(f"  DEV     : {_summ([b['net_ret'] for b in base_dev])}", flush=True)
    print(f"  HELD-OUT: {_summ([b['net_ret'] for b in base_held])}", flush=True)

    # 5) Verdict line.
    n = held_net.get("n", 0)
    mean = held_net.get("mean_ret_pct", 0.0)
    cleared = (n >= 20) and (mean is not None) and (mean > 0)
    print(f"\nVERDICT[{underlying}]: held-out option-leg net n={n} mean/trade={mean}% "
          f"=> {'POSITIVE net-of-cost OOS' if cleared else 'NO net-of-cost OOS edge'}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--conviction", type=float, default=5.0, help="gann entry conviction floor (shipped=5.0)")
    a = ap.parse_args()
    asyncio.run(main_async(a.underlying.upper(), a.dsn, a.conviction))


if __name__ == "__main__":
    main()
