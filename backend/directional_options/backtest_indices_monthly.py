"""Forward OPTION-return backtest — INDICES, MONTHLY ATM, positioning entry.

The gate before wiring the positional indices long-options strategy (owner Option 1):
ATM CE/PE only, MONTHLY contract, single position per underlying, 30% hard stop,
partial-book into the excursion, structure stop, force-exit by DTE~7. Entry = HTF
direction + option-positioning confirmation (OI-build aligned + d_atm_iv>=0 vol
gate). Measures realized OPTION premium P&L NET of costs (the only test that
matters — spot IC already shown not to survive on stocks). Run:
  python -m directional_options.backtest_indices_monthly
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from statistics import mean

import numpy as np
import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal

PER_SIDE_SPREAD = 0.005      # 0.5%/leg of premium — index ATM monthly (tighter than stocks)
HARD_STOP = 0.30             # -30% premium (owner)
PARTIAL_AT = 0.30            # book half at +30%
DTE_MIN, DTE_MAX = 8, 22     # holdable monthly window at entry
DTE_FORCE_EXIT = 7           # roll/exit by DTE~7
STRUCT_LOOKBACK = 3          # lower-low / higher-high over prior N daily bars


def _is_monthly(expiry: date) -> bool:
    return expiry.weekday() == 3 and (expiry + timedelta(days=7)).month != expiry.month


async def _load(u: str) -> pd.DataFrame:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            SELECT timezone('Asia/Kolkata', time)::date AS d, expiry, strike, option_type,
                   close, oi, iv, underlying_price
            FROM option_premium_candles
            WHERE underlying = :u AND interval = '30minute'
              AND close IS NOT NULL AND close > 0 AND underlying_price IS NOT NULL
            ORDER BY time
            """
        ), {"u": u})).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["d", "expiry", "strike", "option_type", "close", "oi", "iv", "spot"])
    for c in ("strike", "close", "spot"):
        df[c] = df[c].astype(float)
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0.0)
    df["iv"] = pd.to_numeric(df["iv"], errors="coerce")
    df = df[df["expiry"].map(_is_monthly)]
    return df


def _round_trip(entry: float, exit_: float, qty: int, u: str) -> float:
    try:
        from paper_engine.costs import round_trip_charges
        chg = round_trip_charges(symbol=u, instrument_type="CE", entry_price=entry, exit_price=exit_, qty=qty, entry_action="BUY")
    except Exception:
        chg = 0.0
    return chg + (abs(entry) + abs(exit_)) * PER_SIDE_SPREAD * qty


def _backtest(df: pd.DataFrame, u: str, *, use_positioning: bool, moneyness: str = "ATM") -> list[dict]:
    # EOD snapshot per contract per day.
    eod = df.sort_values("d").groupby(["d", "expiry", "strike", "option_type"], as_index=False).last()
    # daily underlying + EMAs + structure
    und = eod.groupby("d")["spot"].last().reset_index().sort_values("d").reset_index(drop=True)
    und["ema20"] = und["spot"].ewm(span=20).mean()
    und["ema50"] = und["spot"].ewm(span=50).mean()
    und["ll"] = und["spot"].rolling(STRUCT_LOOKBACK).min().shift(1)
    und["hh"] = und["spot"].rolling(STRUCT_LOOKBACK).max().shift(1)
    und = und.set_index("d")
    days = list(und.index)
    # daily positioning on the monthly chain
    ce = eod[eod["option_type"] == "CE"].groupby("d")["oi"].sum()
    pe = eod[eod["option_type"] == "PE"].groupby("d")["oi"].sum()
    posn = pd.DataFrame({"ce_oi": ce, "pe_oi": pe}).fillna(0.0)
    posn["oi_build"] = ((posn["ce_oi"].diff() - posn["pe_oi"].diff()) / (posn["ce_oi"] + posn["pe_oi"]).replace(0, np.nan)).fillna(0.0)
    # ATM iv per day (nearest strike to spot)
    def _atm_iv(day):
        sub = eod[(eod["d"] == day)]
        if sub.empty:
            return np.nan
        spot = und.loc[day, "spot"]
        sub = sub.assign(dd=(sub["strike"] - spot).abs())
        row = sub.loc[sub["dd"].idxmin()]
        return row["iv"]
    atm_iv = pd.Series({day: _atm_iv(day) for day in days})
    d_atm_iv = atm_iv.diff()

    # contract daily-close lookup
    cmap: dict[tuple, dict] = {}
    for _, r in eod.iterrows():
        cmap.setdefault((r["expiry"], r["strike"], r["option_type"]), {})[r["d"]] = (float(r["close"]), float(r["iv"]) if pd.notna(r["iv"]) else np.nan)

    trades = []
    open_until: date | None = None
    for i, day in enumerate(days):
        if open_until is not None and day <= open_until:
            continue
        u_row = und.loc[day]
        if pd.isna(u_row["ema50"]) or pd.isna(u_row["ll"]):
            continue
        htf_up = u_row["ema20"] > u_row["ema50"]
        oib = posn["oi_build"].get(day, 0.0)
        div = d_atm_iv.get(day, np.nan)
        if pd.isna(div) or div < 0:   # mandatory vol gate
            continue
        side = None
        if htf_up and (not use_positioning or oib > 0):
            side = "CE"
        elif (not htf_up) and (not use_positioning or oib < 0):
            side = "PE"
        if side is None:
            continue
        # pick monthly expiry with DTE in window, ATM strike
        cand = eod[(eod["d"] == day) & (eod["option_type"] == side)].copy()
        cand["dte"] = (pd.to_datetime(cand["expiry"]) - pd.Timestamp(day)).dt.days
        cand = cand[(cand["dte"] >= DTE_MIN) & (cand["dte"] <= DTE_MAX)]
        if cand.empty:
            continue
        exp = cand.sort_values("dte")["expiry"].iloc[0]   # front monthly in window
        cc = cand[cand["expiry"] == exp].copy()
        if moneyness == "ITM":
            # slightly-ITM: nearest strike on the in-the-money side (CE: <=spot, PE: >=spot)
            side_cc = cc[cc["strike"] <= u_row["spot"]] if side == "CE" else cc[cc["strike"] >= u_row["spot"]]
            cc = side_cc if not side_cc.empty else cc
        cc = cc.assign(dd=(cc["strike"] - u_row["spot"]).abs())
        pick = cc.loc[cc["dd"].idxmin()]
        strike, entry_px = pick["strike"], float(pick["close"])
        if entry_px <= 0:
            continue
        path_map = cmap.get((exp, strike, side), {})
        # simulate hold from next day
        qty = 1
        peak = entry_px
        booked = 0.0   # realized from the partial leg (premium points * 0.5 qty)
        half_booked = False
        exit_px = entry_px
        held = 0
        for j in range(i + 1, len(days)):
            dj = days[j]
            dte_j = (pd.Timestamp(exp) - pd.Timestamp(dj)).days
            if dj not in path_map:
                continue
            held += 1
            px = path_map[dj][0]
            peak = max(peak, px)
            ret = (px - entry_px) / entry_px
            exit_px = px
            # partial book half at +PARTIAL_AT
            if (not half_booked) and ret >= PARTIAL_AT:
                booked = 0.5 * (px - entry_px)
                half_booked = True
            # hard stop
            if ret <= -HARD_STOP:
                break
            # structure stop (CE: underlying lower-low; PE: higher-high)
            uu = und.loc[dj]
            if side == "CE" and uu["spot"] < uu["ll"]:
                break
            if side == "PE" and uu["spot"] > uu["hh"]:
                break
            if dte_j <= DTE_FORCE_EXIT:
                break
        rem_qty = 0.5 if half_booked else 1.0
        gross = booked * qty + rem_qty * (exit_px - entry_px) * qty  # premium points
        # costs: full entry notional + exit on remaining + partial leg
        cost = _round_trip(entry_px, exit_px, qty, u)
        net = gross - cost
        trades.append({"d": str(day), "side": side, "net": net, "ret": gross / entry_px, "held": held, "entry": entry_px})
        open_until = days[min(j, len(days) - 1)]
    return trades


def _stats(trades):
    if not trades:
        return {"n": 0}
    nets = np.array([t["net"] for t in trades])
    wins = nets[nets > 0]; losses = nets[nets <= 0]
    eq = np.cumsum(nets); dd = (eq - np.maximum.accumulate(eq)).min()
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    held = [t["held"] for t in trades]
    return {"n": len(trades), "total_pts": round(float(nets.sum()), 1), "win%": round(float((nets > 0).mean()) * 100, 1),
            "PF": round(float(pf), 2), "avg_pts": round(float(nets.mean()), 1), "maxDD_pts": round(float(dd), 1),
            "avg_hold_d": round(mean(held), 1), "median_hold_d": int(np.median(held))}


async def main():
    for u in ("NIFTY", "BANKNIFTY"):
        df = await _load(u)
        if df.empty:
            print(f"{u}: no monthly data"); continue
        print(f"\n==== {u} (monthly, 30% stop, partial@+30%, struct/DTE7 exit; net of costs) ====")
        print(f"  ATM positioning-gated : {_stats(_backtest(df, u, use_positioning=True, moneyness='ATM'))}")
        print(f"  ITM positioning-gated : {_stats(_backtest(df, u, use_positioning=True, moneyness='ITM'))}")
        print(f"  ATM HTF-only baseline : {_stats(_backtest(df, u, use_positioning=False, moneyness='ATM'))}")


if __name__ == "__main__":
    asyncio.run(main())
