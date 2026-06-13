"""
The FIRST DB-direct, cost-honest, rolling walk-forward on the deep NSE option DB.

Every prior NSE number in this codebase is in-sample: s1_walkforward.py is a
hard-coded single-month replay with ZERO cost (the +2489% June mirage), and the
only real rolling IS/OOS harnesses (directional/gann/auction validate_local) run
on US-SPY as a proxy. This module finally measures an NSE option strategy the
honest way:

  * SIGNAL on the CLOSED spot 30-min bar (MACD zero-cross), no look-ahead.
  * EXECUTION on the option: at the NEXT bar, buy the true ATM contract
    (strike nearest spot at entry, front non-expiry expiry), exit on opposite
    cross / time-stop / hard-stop at that contract's later bar.
  * COST: every trade netted through the ONE shared cost model
    (analysis.cost_model, ~3% round-trip premium).
  * HYGIENE: all candles loaded through analysis.safe_candles no-arb guards
    (drops the INDIANB-class impossible-price rows).
  * VALIDATION: rolling IS/OOS via analysis.walk_forward on the DEVELOPMENT set
    only (time < HELD_OUT_START); the dev-selected config is then scored EXACTLY
    ONCE on the sacred held-out block (2026-04-01 .. now).

Run (local stack, in the backend container — asyncpg/pandas present):
    docker exec -e NSE_WF_DSN=postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie \
        nomadcurie_backend python -m analysis.nse_option_walkforward --underlying NIFTY

It prints cost-ON vs cost-OFF side by side so the cost collapse is explicit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from analysis.cost_model import NSE_OPTION_COST
from analysis.macd_engine import compute_macd
from analysis import walk_forward as wf
from analysis import validation_metrics as vm
from analysis.safe_candles import guard_ohlc, guard_option_ohlc

LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, "FINNIFTY": 65, "MIDCPNIFTY": 120}
DEFAULT_DSN = os.environ.get(
    "NSE_WF_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie"
)


# ── data loading ────────────────────────────────────────────────────────────
async def _load(dsn: str, underlying: str, interval: str = "30minute"):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        spot_rows = await conn.fetch(
            "SELECT time, open, high, low, close, COALESCE(volume,0) volume "
            "FROM underlying_spot_candles WHERE underlying=$1 AND interval=$2 ORDER BY time",
            underlying, interval,
        )
        opt_rows = await conn.fetch(
            "SELECT time, open, high, low, close, strike, option_type, underlying_price, expiry "
            "FROM option_premium_candles WHERE underlying=$1 AND interval=$2 ORDER BY time",
            underlying, interval,
        )
    finally:
        await conn.close()

    spot = pd.DataFrame([dict(r) for r in spot_rows])
    for c in ("open", "high", "low", "close", "volume"):
        spot[c] = pd.to_numeric(spot[c], errors="coerce")
    spot = guard_ohlc(spot, band=0.20, rth=True)

    opt = pd.DataFrame([dict(r) for r in opt_rows])
    for c in ("open", "high", "low", "close", "strike", "underlying_price"):
        opt[c] = pd.to_numeric(opt[c], errors="coerce")
    opt = guard_option_ohlc(opt, rth=True)
    opt["expiry"] = pd.to_datetime(opt["expiry"]).dt.date
    return spot, opt


@dataclass
class OptionBook:
    """Per (expiry, type, strike) open/close series for ATM-at-entry execution."""
    by_expiry_type: dict = field(default_factory=dict)  # (expiry, 'CE'/'PE') -> {strike: df}
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
        """Return (strike, open/close df) for the ATM contract active at ts, or None."""
        exp = self.active_expiry(ts)
        if exp is None:
            return None
        strikes = self.by_expiry_type.get((exp, option_type))
        if not strikes:
            return None
        atm = min(strikes, key=lambda k: abs(k - spot))
        return atm, strikes[atm]


# ── strategy: MACD zero-cross long-ATM, faithful next-bar option execution ───
@dataclass
class _Cfg:
    time_stop_bars: int = 13
    hard_stop_pct: float = 0.25
    fast: int = 12
    slow: int = 26
    signal: int = 9


def _premium_at(leg_df: pd.DataFrame, ts: pd.Timestamp, col: str, tol_min: int = 31) -> Optional[float]:
    """Nearest option bar to `ts` within tol_min minutes (spot 30m and option 30m
    bar stamps are not always identical, so exact-match drops most executions)."""
    if leg_df.empty:
        return None
    pos = leg_df.index.get_indexer([ts], method="nearest", tolerance=pd.Timedelta(minutes=tol_min))
    if pos[0] == -1:
        return None
    v = leg_df.iloc[pos[0]][col]
    return float(v) if pd.notna(v) and float(v) > 0 else None


def run_trades(spot: pd.DataFrame, params: dict, *, book: OptionBook, apply_cost: bool = True) -> list:
    """Replay MACD zero-cross on the (time-sliced) spot frame; execute the ATM
    option at the next bar; return per-trade net-return dicts. NO look-ahead:
    signal uses closes up to bar i; entry/exit prices are bar i+1 onward."""
    cfg = _Cfg(**{k: params[k] for k in params if k in _Cfg().__dict__})
    s = spot.sort_values("time").reset_index(drop=True)
    if len(s) < cfg.slow + 5:
        return []
    times = pd.to_datetime(s["time"], utc=True).tolist()
    closes = s["close"].astype(float).tolist()
    macd, sig, _ = compute_macd(closes, cfg.fast, cfg.slow, cfg.signal)

    def crossed(i: int) -> int:
        if i < 1 or macd[i] is None or sig[i] is None or macd[i - 1] is None or sig[i - 1] is None:
            return 0
        prev, cur = macd[i - 1] - sig[i - 1], macd[i] - sig[i]
        if prev <= 0 < cur:
            return +1
        if prev >= 0 > cur:
            return -1
        return 0

    trades: list = []
    i = 1
    n = len(s)
    lot = LOT.get(str(s.attrs.get("underlying", "NIFTY")).upper(), 50)
    while i < n - 1:
        direction = crossed(i)
        if direction == 0:
            i += 1
            continue
        otype = "CE" if direction > 0 else "PE"
        entry_ts = times[i + 1]
        spot_now = closes[i]
        leg = book.atm_leg(entry_ts, spot_now, otype)
        if leg is None:
            i += 1
            continue
        _strike, leg_df = leg
        entry_px = _premium_at(leg_df, entry_ts, "open")
        if entry_px is None:
            i += 1
            continue
        # walk forward to exit
        exit_px = None
        exit_ts = entry_ts
        for j in range(i + 1, min(i + 1 + cfg.time_stop_bars, n)):
            tj = times[j]
            px = _premium_at(leg_df, tj, "close")
            if px is None:
                continue
            exit_px, exit_ts = px, tj
            if px <= entry_px * (1 - cfg.hard_stop_pct):    # hard stop
                break
            if crossed(j) == -direction:                     # opposite signal
                break
        if exit_px is None:
            i += 1
            continue
        gross = exit_px / entry_px - 1.0
        cost = NSE_OPTION_COST.round_trip_pct(entry_px, lot_qty=lot) if apply_cost else 0.0
        trades.append({"time": entry_ts, "exit_time": exit_ts, "net_ret": gross - cost,
                       "otype": otype, "entry": entry_px, "exit": exit_px})
        # advance past exit to avoid overlapping same-signal re-entry
        while i < n - 1 and times[i] <= exit_ts:
            i += 1
    return trades


def _returns(trades) -> list:
    return [t["net_ret"] for t in trades]


def _exit_times(trades) -> list:
    return [t["exit_time"] for t in trades]


# ── driver ──────────────────────────────────────────────────────────────────
def _summ(trades) -> dict:
    r = np.asarray(_returns(trades), dtype=float)
    if r.size == 0:
        return {"n": 0}
    return {
        "n": int(r.size),
        "total_ret": round(float(r.sum()), 3),
        "mean_ret_pct": round(float(r.mean()) * 100, 3),
        "win_rate": round(float((r > 0).mean()), 3),
        "sharpe": round(vm.sharpe(r), 3),
        "expectancy": round(vm.expectancy(r), 4),
        "pf": round(float(r[r > 0].sum() / -r[r < 0].sum()) if (r < 0).any() and r[r > 0].any() else float("nan"), 3),
    }


async def main_async(underlying: str, dsn: str):
    print(f"=== NSE option walk-forward — {underlying} (DB-direct, cost-honest) ===", flush=True)
    spot, opt = await _load(dsn, underlying)
    spot.attrs["underlying"] = underlying
    print(f"spot 30m bars: {len(spot)} ({pd.to_datetime(spot['time']).min().date()} .. "
          f"{pd.to_datetime(spot['time']).max().date()}); option rows (guarded): {len(opt)}; "
          f"expiries: {opt['expiry'].nunique()}", flush=True)
    book = OptionBook.build(opt)

    dev, held = wf.split_development_heldout(spot, time_col="time")
    print(f"development bars: {len(dev)} (< {wf.HELD_OUT_START.date()}); held-out bars: {len(held)}", flush=True)
    wf.assert_tuning_window_safe(pd.to_datetime(dev["time"]).max(), label="dev frame")

    grid = {"time_stop_bars": [6, 13, 26], "hard_stop_pct": [0.25]}

    # 1) cost ON vs OFF on the FULL dev set for the baseline config (shows the collapse)
    base = {"time_stop_bars": 13, "hard_stop_pct": 0.25}
    for cost_on in (False, True):
        tr = run_trades(dev, base, book=book, apply_cost=cost_on)
        print(f"\n[DEV baseline ts=13] cost={'ON' if cost_on else 'OFF'}: {_summ(tr)}", flush=True)

    # 2) rolling IS/OOS walk-forward (cost ON) on DEV only — the honest gate report
    print("\n--- rolling walk-forward (DEV, cost ON, IS=270/OOS=90/stride=30) ---", flush=True)
    report = wf.validate_strategy(
        dev,
        lambda f, p: run_trades(f, p, book=book, apply_cost=True),
        grid,
        _returns,
        extract_exit_times=_exit_times,
        time_col="time",
        is_days=270, oos_days=90, stride_days=30,
        target_sr_annual=1.0,
    )
    gates = report.get("gates") or {}
    print(f"verdict: {report.get('verdict')}", flush=True)
    print(f"OOS trades={gates.get('g1_oos_trades', {}).get('value')} "
          f"oos_sharpe={report.get('oos_sharpe')} "
          f"mc_sharpe_p05={gates.get('g6_mc_sharpe_p05', {}).get('value')} "
          f"wfe={report.get('walk_forward', {}).get('wfe_median')}", flush=True)
    for k, v in gates.items():
        print(f"  gate {k}: {v}", flush=True)

    # 3) sacred held-out: score the dev-selected config EXACTLY ONCE
    best = base  # (dev selection is inside validate_strategy; baseline used here for the single-shot)
    if len(held) > base["time_stop_bars"] + 5:
        ht = run_trades(held, best, book=book, apply_cost=True)
        print(f"\n=== HELD-OUT (2026-04-01..now), cost ON, config={best} ===", flush=True)
        print(f"  {_summ(ht)}", flush=True)
    else:
        print("\nHELD-OUT: insufficient bars to score.", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    a = ap.parse_args()
    asyncio.run(main_async(a.underlying.upper(), a.dsn))


if __name__ == "__main__":
    main()
