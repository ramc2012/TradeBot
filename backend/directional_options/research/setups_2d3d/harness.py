"""(C) Labelled setup dataset — causal research harness.

Pipeline
--------
  1. 30-minute SPOT bars (underlying_spot_candles) -> intraday indicators
     (EMA20/50, RSI14, MACD 12/26/9, ADX14/+DI/-DI, ATR14, Donchian-40).
  2. Session-aggregated DAILY bars -> daily indicators, then LAGGED ONE SESSION
     so that any bar inside session s only ever sees daily values through
     session s-1 (session s's own daily bar is not closed yet).
  3. Setup detection on the 30m decision bar (higher-timeframe agreement
     required from the lagged daily frame). Decision bars are restricted to
     09:15..14:45 IST starts so the entry bar is inside the SAME session.
  4. Contract selection happens at the 15:15 bar of the PRIOR session (a real
     lane maintains a tracked contract per name per day). Holdable band from
     the (B) panel: MONTHLY expiry, DTE 8-22 at entry, ITM.
  5. Entry at the OPEN of the bar AFTER the decision bar (both spot and option).
  6. ATR triple barrier, monitored on 30m spot high/low:
        target = entry_spot + TGT_ATR * daily_ATR14(prior session) * side
        stop   = entry_spot - STP_ATR * daily_ATR14(prior session) * side
        time   = last bar of (entry session + HOLD_SESSIONS)
     First 30m bar to touch wins. If BOTH touch inside the same 30m bar the
     STOP is assumed (conservative). Exit fill = that bar's CLOSE (one-bar
     execution lag baked in); a +1 bar variant is also recorded.
  7. Option P&L net of a round-trip cost in % of premium.

Barrier multiples, hold length, moneyness band and DTE band are FIXED A PRIORI
from the (A) external-practice pass and the (B) panel. Nothing here is swept.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import add_daily_features, add_intraday_features  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SPOT_DIR = os.path.join(os.path.dirname(HERE), "panel_2d3d", "data")
IST = pd.Timedelta(hours=5, minutes=30)

SESSION_LO, SESSION_HI = 225, 585      # UTC minute-of-day: 09:15 .. 15:15 IST
DECISION_HI = 555                      # last decision bar start = 14:45 IST
MIN_BARS_PER_SESSION = 10

# --- fixed a priori design constants -------------------------------------
HOLD_SESSIONS = 3
TGT_ATR = 1.5
STP_ATR = 1.0
DTE_LO, DTE_HI = 8, 22                 # at entry session
MNY_BANDS = {                          # signed moneyness, <0 = ITM
    "deep_itm": (-0.060, -0.030),
    "slight_itm": (-0.030, -0.0075),
}
COST_RT = {"optimistic": 0.006, "base": 0.016, "pessimistic": 0.040}
NOTIONAL = 25_000.0                    # Rs premium per leg


# =========================================================================
# spot
# =========================================================================

def load_spot() -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    for p in sorted(glob.glob(os.path.join(SPOT_DIR, "spot_*.csv"))):
        frames.append(pd.read_csv(p, usecols=["time", "underlying", "open", "high",
                                              "low", "close", "volume"]))
    s = pd.concat(frames, ignore_index=True)
    s["time"] = pd.to_datetime(s["time"], utc=True)
    s["mins"] = s["time"].dt.hour * 60 + s["time"].dt.minute
    s = s[(s["mins"] >= SESSION_LO) & (s["mins"] <= SESSION_HI)]
    s = s[(s["close"] > 0) & (s["high"] >= s["low"])]
    s["session"] = (s["time"] + IST).dt.date
    s = s.sort_values(["underlying", "time"]).drop_duplicates(["underlying", "time"])

    # drop partial sessions -- barrier monitoring needs a full tape
    cnt = s.groupby(["underlying", "session"])["close"].size().rename("nbars")
    s = s.merge(cnt, on=["underlying", "session"])
    s = s[s["nbars"] >= MIN_BARS_PER_SESSION].copy()

    daily = (
        s.groupby(["underlying", "session"])
        .agg(s_open=("open", "first"), s_high=("high", "max"),
             s_low=("low", "min"), s_close=("close", "last"))
        .reset_index()
        .sort_values(["underlying", "session"])
    )
    daily = pd.concat(
        [add_daily_features(g.reset_index(drop=True)) for _, g in
         daily.groupby("underlying", sort=False)], ignore_index=True
    )
    daily["sidx"] = daily.groupby("underlying").cumcount()

    # contamination guard (Fyers cross-symbol ticks blow ATR / returns up)
    dret = daily.groupby("underlying")["s_close"].pct_change()
    bad = (daily["d_atr_pct"] > 0.15) | (daily["d_atr_pct"] <= 0.0005) | (dret.abs() > 0.25)
    daily.loc[bad, "d_atr14"] = np.nan

    # LAG the whole daily feature block by one session -> "prior daily"
    dcols = [c for c in daily.columns if c.startswith("d_")] + ["s_close", "s_high", "s_low"]
    lag = daily[["underlying", "session", "sidx"]].copy()
    for c in dcols:
        lag["p" + c] = daily.groupby("underlying")[c].shift(1)

    s = s.merge(lag, on=["underlying", "session"], how="left")

    intra = pd.concat(
        [add_intraday_features(g.reset_index(drop=True)) for _, g in
         s.groupby("underlying", sort=False)], ignore_index=True
    )
    intra["bidx"] = intra.groupby("underlying").cumcount()
    return intra, daily


# =========================================================================
# setups  (side: +1 long call, -1 long put)
# =========================================================================

def detect_setups(x: pd.DataFrame) -> pd.DataFrame:
    """Return long-form (row, family, side). All conditions use bar t and t-1
    intraday values plus PRIOR-session daily values only."""
    up_d = x["ps_close"] > x["pd_sma20"]
    dn_d = x["ps_close"] < x["pd_sma20"]
    out = []

    def emit(name, mask_long, mask_short):
        for side, m in ((1, mask_long), (-1, mask_short)):
            idx = x.index[m.fillna(False)]
            if len(idx):
                out.append(pd.DataFrame({"row": idx, "family": name, "side": side}))

    # 1. EMA20/50 cross on 30m, confirmed by daily side of SMA20
    emit("ma_cross",
         (x["m_ema20"] > x["m_ema50"]) & (x["m_ema20_p"] <= x["m_ema50_p"]) & up_d,
         (x["m_ema20"] < x["m_ema50"]) & (x["m_ema20_p"] >= x["m_ema50_p"]) & dn_d)

    # 2. ADX-confirmed trend: ADX crosses above 25 and rising, DI ordering = side
    adx_on = (x["m_adx14"] > 25) & (x["m_adx14_p"] <= 25)
    emit("adx_trend",
         adx_on & (x["m_pdi"] > x["m_mdi"]) & up_d,
         adx_on & (x["m_mdi"] > x["m_pdi"]) & dn_d)

    # 3. MACD signal cross on 30m, daily MACD histogram agreeing
    emit("macd_cross",
         (x["m_macd"] > x["m_macd_sig"]) & (x["m_macd_p"] <= x["m_macd_sig_p"]) & (x["pd_macd_hist"] > 0),
         (x["m_macd"] < x["m_macd_sig"]) & (x["m_macd_p"] >= x["m_macd_sig_p"]) & (x["pd_macd_hist"] < 0))

    # 4. RSI mean-reversion (fade): 30m RSI exits oversold / overbought
    emit("rsi_fade",
         (x["m_rsi14"] > 30) & (x["m_rsi14_p"] <= 30),
         (x["m_rsi14"] < 70) & (x["m_rsi14_p"] >= 70))

    # 5. Donchian-40 (~3 sessions) breakout on 30m
    emit("donchian_break",
         (x["close"] > x["m_dc_hi40"]) & (x["m_dc_hi40"].notna()),
         (x["close"] < x["m_dc_lo40"]) & (x["m_dc_lo40"].notna()))

    # 6. Trend + pullback: daily trend up/down, 30m RSI leaves the pullback zone
    emit("trend_pullback",
         up_d & (x["pd_adx14"] > 20) & (x["m_rsi14"] > 40) & (x["m_rsi14_p"] <= 40),
         dn_d & (x["pd_adx14"] > 20) & (x["m_rsi14"] < 60) & (x["m_rsi14_p"] >= 60))

    # 7-9. CONTROLS with no signal content whatsoever. Deterministic sampling
    # of bars by a stable hash so the control is reproducible and is exposed to
    # exactly the same instrument selection, barriers and costs.
    #   control_random : coin-flip side  -> the pure cost/carry floor
    #   control_long   : always long     -> unconditional-long benchmark
    #   control_short  : always short    -> unconditional-short benchmark
    # control_long is the benchmark ANY long-biased family must beat: in a
    # rising sample, "always long" looks like edge and is not.
    h = pd.util.hash_pandas_object(
        x["underlying"].astype(str) + "|" + x["time"].astype(str), index=False
    ).to_numpy()
    false = pd.Series(False, index=x.index)
    ctrl = (h % 200 == 0)
    emit("control_random", pd.Series(ctrl & (h % 400 == 0), index=x.index),
         pd.Series(ctrl & (h % 400 != 0), index=x.index))
    # dense versions so the index sub-sample (only ~6 names) has enough
    # control observations to compare a family against
    emit("control_long", pd.Series(h % 40 == 7, index=x.index), false)
    emit("control_short", false, pd.Series(h % 40 == 11, index=x.index))

    if not out:
        return pd.DataFrame(columns=["row", "family", "side"])
    return pd.concat(out, ignore_index=True)


# =========================================================================
# options
# =========================================================================

OPT_COLS = ["time", "underlying", "expiry", "strike", "option_type", "open",
            "high", "low", "close", "volume", "oi", "iv", "delta",
            "underlying_price", "instrument_key"]


def load_options() -> pd.DataFrame:
    frames = []
    for p in sorted(glob.glob(os.path.join(DATA, "optintra_*.csv"))):
        frames.append(pd.read_csv(p, usecols=OPT_COLS))
    o = pd.concat(frames, ignore_index=True)
    o["time"] = pd.to_datetime(o["time"], utc=True)
    o["mins"] = o["time"].dt.hour * 60 + o["time"].dt.minute
    o = o[(o["mins"] >= SESSION_LO) & (o["mins"] <= SESSION_HI)]
    o["session"] = (o["time"] + IST).dt.date
    o["expiry"] = pd.to_datetime(o["expiry"]).dt.date
    o = o.dropna(subset=["expiry", "strike", "option_type", "instrument_key"])
    o = o.drop_duplicates(["instrument_key", "time"], keep="last")
    # monthly = last expiry of its calendar month for that underlying
    ex = o[["underlying", "expiry"]].drop_duplicates()
    ex["ym"] = pd.to_datetime(ex["expiry"]).dt.to_period("M")
    last = ex.groupby(["underlying", "ym"])["expiry"].max().rename("le").reset_index()
    ex = ex.merge(last, on=["underlying", "ym"])
    ex["is_monthly"] = ex["expiry"] == ex["le"]
    o = o.merge(ex[["underlying", "expiry", "is_monthly"]], on=["underlying", "expiry"], how="left")
    return o


def build_selection(o: pd.DataFrame, band: tuple[float, float]) -> pd.DataFrame:
    """Tracked contract per (underlying, side, TARGET SESSION).

    Chosen from the 15:15 snapshot of the PRIOR session, so it is known before
    the target session opens. DTE band is evaluated as-of the target session,
    approximated by requiring DTE_LO+1 .. DTE_HI+1 calendar days at selection
    time (>= 1 calendar day to the next session).
    """
    snap = o[(o["mins"] == SESSION_HI) & (o["is_monthly"])].copy()
    snap["dte_sel"] = (pd.to_datetime(snap["expiry"]) - pd.to_datetime(snap["session"])).dt.days
    snap = snap[(snap["dte_sel"] >= DTE_LO + 1) & (snap["dte_sel"] <= DTE_HI + 1)]
    m = (snap["strike"] - snap["underlying_price"]) / snap["underlying_price"]
    snap["mny"] = np.where(snap["option_type"] == "CE", m, -m)
    lo, hi = band
    snap = snap[(snap["mny"] >= lo) & (snap["mny"] <= hi)]
    snap["side"] = np.where(snap["option_type"] == "CE", 1, -1)
    mid = (lo + hi) / 2.0
    snap["dist"] = (snap["mny"] - mid).abs()
    snap = snap.sort_values(["underlying", "side", "session", "dist"])
    sel = snap.groupby(["underlying", "side", "session"], as_index=False).first()
    return sel[["underlying", "side", "session", "instrument_key", "expiry",
                "strike", "mny", "oi", "iv", "dte_sel"]].rename(
        columns={"session": "sel_session", "mny": "sel_mny", "iv": "sel_iv"})


# =========================================================================
# triple barrier
# =========================================================================

def run_trades(intra: pd.DataFrame, daily: pd.DataFrame, setups: pd.DataFrame,
               sel: pd.DataFrame, opt: pd.DataFrame, band_name: str) -> pd.DataFrame:
    # per-underlying bar arrays for fast forward scanning
    und_bars: dict[str, dict] = {}
    for u, g in intra.groupby("underlying", sort=False):
        g = g.sort_values("time")
        und_bars[u] = {
            "time": g["time"].to_numpy(),
            "open": g["open"].to_numpy(float),
            "high": g["high"].to_numpy(float),
            "low": g["low"].to_numpy(float),
            "close": g["close"].to_numpy(float),
            "session": g["session"].to_numpy(),
            "bidx": g["bidx"].to_numpy(),
        }
    sidx_map = {(r.underlying, r.session): r.sidx for r in
                daily[["underlying", "session", "sidx"]].itertuples()}

    x = intra
    su = setups.copy()
    su = su.join(x[["underlying", "time", "session", "mins", "bidx",
                    "pd_atr14", "ps_close", "m_atr14", "pd_atr_pct"]], on="row")
    su = su[su["mins"] <= DECISION_HI]
    su = su[su["pd_atr14"].notna() & (su["pd_atr14"] > 0)]

    # prior-session tracked contract for the setup's session and side.
    # Join on session index so the contract is ALWAYS the one chosen at the
    # 15:15 snapshot of the immediately preceding session (never a later one).
    su["sidx"] = [sidx_map.get((u, s), np.nan) for u, s in zip(su["underlying"], su["session"])]
    su = su[su["sidx"].notna()]
    su["sel_sidx"] = su["sidx"] - 1
    sel = sel.copy()
    sel["sel_sidx"] = [sidx_map.get((u, s), np.nan)
                       for u, s in zip(sel["underlying"], sel["sel_session"])]
    sel = sel[sel["sel_sidx"].notna()]
    su = su.merge(sel.drop(columns=["sel_session"]),
                  on=["underlying", "side", "sel_sidx"], how="inner")
    if su.empty:
        return pd.DataFrame()

    # option price series per used contract
    used = set(su["instrument_key"].dropna().unique())
    op = opt[opt["instrument_key"].isin(used)]
    ser: dict[str, dict] = {}
    for k, g in op.groupby("instrument_key", sort=False):
        g = g.sort_values("time")
        ser[k] = {"time": g["time"].to_numpy(),
                  "open": g["open"].to_numpy(float),
                  "close": g["close"].to_numpy(float),
                  "iv": g["iv"].to_numpy(float),
                  "delta": g["delta"].to_numpy(float),
                  "underlying_price": g["underlying_price"].to_numpy(float),
                  "strike": float(g["strike"].iloc[0]),
                  "expiry": g["expiry"].iloc[0]}

    rows = []
    for r in su.itertuples():
        u = r.underlying
        B = und_bars[u]
        i = int(np.searchsorted(B["bidx"], r.bidx))
        if i + 1 >= len(B["time"]):
            continue
        e = i + 1                                   # entry bar
        if B["session"][e] != B["session"][i]:      # entry must stay in-session
            continue
        S = ser.get(r.instrument_key)
        if S is None:
            continue
        j = int(np.searchsorted(S["time"], B["time"][e]))
        if j >= len(S["time"]) or S["time"][j] != B["time"][e]:
            continue
        entry_prem = S["open"][j]
        if not np.isfinite(entry_prem) or entry_prem <= 0:
            entry_prem = S["close"][j]
        if not np.isfinite(entry_prem) or entry_prem < 1.0:
            continue
        entry_spot = B["open"][e]
        side = int(r.side)
        atr_abs = float(r.pd_atr14)
        tgt = entry_spot + side * TGT_ATR * atr_abs
        stp = entry_spot - side * STP_ATR * atr_abs

        # time barrier = last bar of (entry session + HOLD_SESSIONS)
        s0 = sidx_map.get((u, B["session"][e]))
        if s0 is None:
            continue
        limit_sidx = s0 + HOLD_SESSIONS
        k = e
        n = len(B["time"])
        hit = "time"
        exit_i = None
        while k < n:
            sk = sidx_map.get((u, B["session"][k]))
            if sk is None or sk > limit_sidx:
                exit_i = k - 1
                break
            hi_, lo_ = B["high"][k], B["low"][k]
            t_hit = hi_ >= tgt if side > 0 else lo_ <= tgt
            s_hit = lo_ <= stp if side > 0 else hi_ >= stp
            if t_hit and s_hit:
                hit, exit_i = "stop", k          # conservative tie-break
                break
            if s_hit:
                hit, exit_i = "stop", k
                break
            if t_hit:
                hit, exit_i = "target", k
                break
            k += 1
        if exit_i is None:
            exit_i = min(k, n - 1)
        if exit_i <= e:
            exit_i = e
        # never let a truncated tape masquerade as a completed hold
        last_sidx = sidx_map.get((u, B["session"][exit_i]))
        if hit == "time" and (last_sidx is None or last_sidx < limit_sidx):
            continue

        def prem_at(t, field):
            p = int(np.searchsorted(S["time"], t))
            if p < len(S["time"]) and S["time"][p] == t:
                v = S[field][p]
                if np.isfinite(v) and v > 0:
                    return v, 0
            p = int(np.searchsorted(S["time"], t, side="right")) - 1
            if p >= 0:
                v = S["close"][p]
                if np.isfinite(v) and v > 0:
                    return v, 1
            return np.nan, 2

        exit_prem, stale = prem_at(B["time"][exit_i], "close")
        if not np.isfinite(exit_prem):
            continue
        # +1 bar execution-lag variant
        lag_i = min(exit_i + 1, n - 1)
        exit_prem_lag, _ = prem_at(B["time"][lag_i], "close")
        if not np.isfinite(exit_prem_lag):
            exit_prem_lag = exit_prem

        gross = exit_prem / entry_prem - 1.0
        gross_lag = exit_prem_lag / entry_prem - 1.0
        rows.append({
            "underlying": u, "family": r.family, "side": side, "band": band_name,
            "decision_time": B["time"][i], "entry_time": B["time"][e],
            "exit_time": B["time"][exit_i], "outcome": hit,
            "bars_held": exit_i - e,
            "entry_spot": entry_spot, "exit_spot": B["close"][exit_i],
            "spot_ret": (B["close"][exit_i] / entry_spot - 1.0) * side,
            "atr_pct": float(r.pd_atr_pct) if np.isfinite(r.pd_atr_pct) else np.nan,
            "contract": r.instrument_key, "sel_mny": r.sel_mny,
            "dte_entry": (pd.Timestamp(S["expiry"]) - pd.Timestamp(B["session"][e])).days,
            "entry_prem": entry_prem, "exit_prem": exit_prem,
            "gross": gross, "gross_lag1": gross_lag,
            "stale_exit_quote": stale,
            "entry_iv": S["iv"][j], "entry_delta": S["delta"][j],
        })
    return pd.DataFrame(rows)


def main() -> None:
    print("loading spot ...", flush=True)
    intra, daily = load_spot()
    print("  bars", len(intra), "underlyings", intra["underlying"].nunique(),
          "sessions", daily["session"].nunique())
    print("detecting setups ...", flush=True)
    setups = detect_setups(intra)
    print("  raw setups", len(setups))
    print(setups.groupby("family").size())
    print("loading options ...", flush=True)
    opt = load_options()
    print("  option bars", len(opt), "contracts", opt["instrument_key"].nunique())

    all_tr = []
    for band_name, band in MNY_BANDS.items():
        sel = build_selection(opt, band)
        print(f"selection[{band_name}] rows", len(sel), flush=True)
        tr = run_trades(intra, daily, setups, sel, opt, band_name)
        print(f"  trades[{band_name}]", len(tr), flush=True)
        all_tr.append(tr)
    trades = pd.concat([t for t in all_tr if len(t)], ignore_index=True)
    trades["quarter"] = pd.PeriodIndex(pd.to_datetime(trades["entry_time"]).dt.tz_localize(None), freq="Q").astype(str)
    for name, c in COST_RT.items():
        trades["net_" + name] = trades["gross"] - c
    trades["net_base_lag1"] = trades["gross_lag1"] - COST_RT["base"]
    trades.to_parquet(os.path.join(DATA, "trades.parquet"))
    print("wrote trades.parquet", len(trades))


if __name__ == "__main__":
    main()
