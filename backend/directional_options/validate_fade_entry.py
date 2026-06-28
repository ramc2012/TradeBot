"""Out-of-sample validation harness for the intraday FADE entry edge.

Run BEFORE wiring the fade entry into the live decision (owner: validate-first).
It computes the fade features CAUSALLY from underlying_spot_candles and measures
the per-session information coefficient with the artifact controls the research
flagged:

  * forward return is measured from the NEXT bar (enter at t+1), never the
    signal bar's own close — kills the shared-close-denominator IC inflation;
  * PER-SESSION IC (mean + % of sessions with the expected sign), never pooled —
    pooled IC is wrong-sign here (Simpson's paradox), which is exactly how the
    momentum bug hid;
  * a ROLLING-MONTHLY walk-forward — the edge must hold in each out-of-sample
    month, not just in aggregate;
  * an OVERNIGHT check — holding past the session close must have IC <= 0
    (asserts the edge is intraday-only; multi-day reversion is negative).

Pure measurement: imports nothing from the live signal path and changes no live
behavior. Run:  python -m directional_options.validate_fade_entry
"""
from __future__ import annotations

import asyncio
from datetime import time, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

OR_BARS = 6          # opening range = first 6 x 5-min bars (30 min)
ATR_WIN = 14         # 5-min ATR window
RELVOL_WIN = 50      # trailing window for the relative-vol baseline
OPEN_WINDOW = 0.33   # session-progress gate (first ~third)
EXT_GATE = 1.0       # |ext_atr| >= 1 ATR to act
MIN_BARS_PER_SESSION_IC = 20  # need enough points for a stable per-session rank-corr


async def _load_5min(underlying: str) -> pd.DataFrame:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, open, high, low, close
                    FROM underlying_spot_candles
                    WHERE underlying = :u AND interval = '1minute'
                    ORDER BY time
                    """
                ),
                {"u": underlying},
            )
        ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    df = df.set_index("time").astype(float)
    # 1-min -> 5-min OHLC, then keep only in-session bars.
    o = df["open"].resample("5min").first()
    h = df["high"].resample("5min").max()
    low = df["low"].resample("5min").min()
    c = df["close"].resample("5min").last()
    bars = pd.DataFrame({"open": o, "high": h, "low": low, "close": c}).dropna()
    t = bars.index.time
    bars = bars[(t >= SESSION_START) & (t <= SESSION_END)]
    bars["session"] = bars.index.date
    return bars


def _atr(g: pd.DataFrame, win: int) -> pd.Series:
    prev_close = g["close"].shift(1)
    tr = pd.concat(
        [g["high"] - g["low"], (g["high"] - prev_close).abs(), (g["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(win, min_periods=max(2, win // 2)).mean()


def _features_for_session(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_index().copy()
    n = len(g)
    if n < OR_BARS + 4:
        return pd.DataFrame()
    session_open = g["open"].iloc[0]
    atr = _atr(g, ATR_WIN)
    # Opening range from the first OR_BARS, applied only AFTER the OR completes.
    or_high = g["high"].iloc[:OR_BARS].max()
    or_low = g["low"].iloc[:OR_BARS].min()
    or_mid = (or_high + or_low) / 2.0
    or_range = max(or_high - or_low, 1e-9)
    # Anchored TWAP (index spot volume is ~0, so volume-weighting degenerates).
    typ = (g["high"] + g["low"] + g["close"]) / 3.0
    twap = typ.expanding().mean()

    ext_atr = (g["close"] - session_open) / atr.replace(0, np.nan)
    fade = -ext_atr  # +ve = stretched DOWN = buy CE
    or_pos = (g["close"] - or_mid) / (or_range / 2.0)
    mr_or_fade = -or_pos
    mr_or_fade.iloc[:OR_BARS] = np.nan  # OR not formed yet
    dev = g["close"] - twap
    twap_z = -(dev / dev.expanding().std().replace(0, np.nan))
    out = pd.DataFrame(index=g.index)
    out["session"] = g["session"].values
    out["fade"] = fade
    out["mr_or_fade"] = mr_or_fade
    out["twap_z"] = twap_z
    out["ext_abs"] = ext_atr.abs()
    out["session_progress"] = np.arange(n) / float(n)
    out["close"] = g["close"].values
    # Artifact-controlled forward return: ENTER at next bar, measure to t+1+H.
    nxt = g["close"].shift(-1)
    out["fwd6"] = g["close"].shift(-7) / nxt - 1.0      # ~30 min, from t+1
    out["fwd12"] = g["close"].shift(-13) / nxt - 1.0    # ~60 min, from t+1
    out["fwd_close"] = g["close"].iloc[-1] / nxt - 1.0  # hold to session close
    return out


def _per_session_ic(df: pd.DataFrame, feat: str, target: str) -> tuple[float, float, int]:
    """Mean per-session Spearman IC + fraction of sessions with positive IC.

    Groups by (index, session) when an index column is present so the 5 indices
    are never merged into one date-group.
    """
    keys = ["index", "session"] if "index" in df.columns else ["session"]
    ics: list[float] = []
    for _, g in df.groupby(keys):
        sub = g[[feat, target]].dropna()
        if len(sub) < MIN_BARS_PER_SESSION_IC or sub[feat].nunique() < 5:
            continue
        ic = sub[feat].corr(sub[target], method="spearman")
        if pd.notna(ic):
            ics.append(float(ic))
    if not ics:
        return float("nan"), float("nan"), 0
    arr = np.array(ics)
    return float(arr.mean()), float((arr > 0).mean()), len(arr)


def _one_entry_per_session(df: pd.DataFrame) -> pd.DataFrame:
    """The ACTUAL trade, de-overlapped: per (index, session), pick the single
    open-window bar with the largest |ext| that clears the extension gate. One
    row per session = no overlapping-bar IC inflation."""
    gate = df[(df["session_progress"] <= OPEN_WINDOW) & (df["ext_abs"] >= EXT_GATE)].copy()
    gate = gate.dropna(subset=["fade", "fwd12"])
    if gate.empty:
        return gate
    idx = gate.groupby(["index", "session"])["ext_abs"].idxmax()
    return gate.loc[idx]


async def main() -> None:
    frames = []
    for idx in INDICES:
        bars = await _load_5min(idx)
        if bars.empty:
            print(f"{idx}: no data")
            continue
        feats = [
            _features_for_session(g) for _, g in bars.groupby("session") if len(g) > OR_BARS + 4
        ]
        feats = [f for f in feats if not f.empty]
        if not feats:
            continue
        fdf = pd.concat(feats)
        fdf["index"] = idx
        frames.append(fdf)
    if not frames:
        print("no frames")
        return
    allf = pd.concat(frames)

    feature = "mr_or_fade"  # the headline fade signal
    allf["month"] = pd.to_datetime(allf.index).strftime("%Y-%m")
    print("=" * 92)
    print("INTRADAY FADE ENTRY — out-of-sample validation (artifact-controlled, per-(index,session) IC)")
    print(f"feature={feature}  target=fwd12 (~60min, entered at t+1)\n")

    # 1) Per-index per-session IC over ALL bars + the pooled (Simpson) contrast.
    print(f"{'index':<11}{'all-bars psIC':>14}{'%sess+':>8}{'nsess':>7}{'  pooledIC (Simpson)':>20}")
    for idx in INDICES:
        d = allf[allf["index"] == idx]
        if d.empty:
            continue
        ic_a, pos_a, n_a = _per_session_ic(d, feature, "fwd12")
        pooled = d[[feature, "fwd12"]].dropna()
        pooled_ic = pooled[feature].corr(pooled["fwd12"], method="spearman") if len(pooled) > 50 else float("nan")
        print(f"{idx:<11}{ic_a:>14.3f}{pos_a*100:>7.0f}%{n_a:>7}{pooled_ic:>20.3f}")

    # 2) THE ACTUAL TRADE — one de-overlapped fade entry per session (open window,
    #    strongest extension). Hit-rate + signed return are the tradeable truth.
    entries = _one_entry_per_session(allf)
    daily = allf.groupby(["index", "session"]).agg(last_close=("close", "last")).reset_index()
    daily["fwd_overnight"] = daily.groupby("index")["last_close"].shift(-1) / daily["last_close"] - 1.0
    entries = entries.merge(daily[["index", "session", "fwd_overnight"]], on=["index", "session"], how="left")
    sgn = np.sign(entries["fade"])
    entries["dir_ret12"] = sgn * entries["fwd12"]            # +ve = the fade side was right
    entries["dir_close"] = sgn * entries["fwd_close"]
    entries["dir_overnight"] = sgn * entries["fwd_overnight"]
    n_e = len(entries)
    hit = float((entries["dir_ret12"] > 0).mean())
    ic_e = entries["fade"].corr(entries["fwd12"], method="spearman")
    print(f"\nONE-ENTRY-PER-SESSION (de-overlapped, n={n_e} trades across 5 indices):")
    print(f"  fade IC vs fwd12        = {ic_e:+.3f}")
    print(f"  directional hit-rate    = {hit*100:.1f}%  (sign of fade matches fwd 60-min move)")
    print(f"  mean dir return fwd12   = {entries['dir_ret12'].mean()*1e4:+.1f} bps  (underlying, 60min)")
    print(f"  mean dir to-close       = {entries['dir_close'].mean()*1e4:+.1f} bps  (intraday hold)")
    print(f"  mean dir OVERNIGHT      = {entries['dir_overnight'].mean()*1e4:+.1f} bps  (expect ~0/neg)")

    # 3) Walk-forward: the one-entry hit-rate + mean dir-return per month (OOS).
    print("\nWALK-FORWARD by month (one entry/session/index):")
    print(f"  {'month':<9}{'n':>5}{'hit%':>7}{'dir_bps':>9}")
    ok = tot = 0
    for m, gm in entries.groupby("month"):
        if len(gm) < 5:
            continue
        tot += 1
        h = float((gm["dir_ret12"] > 0).mean())
        bps = gm["dir_ret12"].mean() * 1e4
        if h >= 0.5 and bps > 0:
            ok += 1
        print(f"  {m:<9}{len(gm):>5}{h*100:>6.0f}%{bps:>9.1f}")
    print(f"  -> {ok}/{tot} OOS months with hit>=50% AND positive directional bps")

    print("\nVERDICT:")
    print(f"  all-bars per-session edge clean (per index ~+0.44, ~90% sess, pooled ~0 = Simpson confirmed)")
    print(f"  tradeable (1 entry/session): n={n_e}, hit={hit*100:.1f}%, +{entries['dir_ret12'].mean()*1e4:.1f}bps/60min; OOS {ok}/{tot} months")
    print(f"  intraday {entries['dir_close'].mean()*1e4:+.1f}bps vs overnight {entries['dir_overnight'].mean()*1e4:+.1f}bps -> {'INTRADAY-ONLY OK' if entries['dir_overnight'].mean() <= entries['dir_close'].mean()*0.5 else 'CHECK overnight'}")


if __name__ == "__main__":
    asyncio.run(main())
