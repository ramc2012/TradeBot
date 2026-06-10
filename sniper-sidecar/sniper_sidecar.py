"""Sniper shadow SIDECAR — isolated from the prod backend.

Runs in its OWN short-lived container (not via `docker exec` inside the prod backend, which
OOM-kills the heavy feature build and triggers prod container recreates that wipe state). It reads
recent 1-minute spot bars + the ATM option chain DIRECTLY from TimescaleDB, and pulls the live
OrderFlowSnapshot from the backend over HTTP — it does NOT import `auction_intelligence`, so it
never triggers the broker/WebSocket credential bootstrap.

Per symbol each cycle:
  - spot bars  ← underlying_spot_candles (1minute), RTH-filtered, close-stamped (match training)
  - ATM chain  ← option_premium_candles (nearest expiry, ATM strike CE/PE) → families C/D (o_*)
  - order flow ← GET /api/auction-intelligence/live-snapshot → analysis.order_flow → family B2 (u_of_*)
  - ExcursionEstimator.predict → append prediction (incl. has_live_of) to host-mounted JSONL.

The current model has o_* features (they now populate live, no retrain) but NOT the u_of_* B2
features (all-null historically → dropped). Logging the live order flow accumulates the OF-bearing
dataset a future retrain needs to add the B2 family.

Run (inside the sidecar container):  python sniper_sidecar.py ALL   (or NIFTY/BANKNIFTY/SENSEX)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, time as dtime, timezone

import asyncpg
import pandas as pd

from nomad_sniper.data.bars import close_stamp
from nomad_sniper.data.option_bars import AtmSeries
from nomad_sniper.integration.ai_lane import SniperEstimatorLane
from nomad_sniper.utils.normalize import atr_reference

DSN = os.environ.get("SNIPER_DB_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
MODEL = os.environ.get("SNIPER_MODEL", "/sniper/sniper_artifacts/excursion_estimator_sensex.joblib")
LOG = os.environ.get("SNIPER_LOG", "/sniper/sniper_shadow.jsonl")
BACKEND_URL = os.environ.get("SNIPER_BACKEND_URL", "http://nomadcurie_backend:8000")
LOOKBACK_DAYS = int(os.environ.get("SNIPER_LOOKBACK_DAYS", "75"))
ATM_REF = dtime(9, 20)  # pick ATM strike at 09:20 IST and hold it for the session (match training)
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")

# --- AI decision-engine overlay push ------------------------------------------
# After each prediction the sidecar POSTs a reduced directional signal to the
# backend so the Auction Intelligence agents can use the sniper alpha live
# (AuctionIntelligenceService._apply_sniper_overlay). Graceful: any failure is
# logged and ignored; the shadow JSONL is written regardless.
POST_SIGNALS = os.environ.get("SNIPER_POST_SIGNALS", "1") not in ("0", "false", "False", "")
# First horizon (in preference order) that carries a finite signed_move wins.
SIGNAL_HORIZONS = [h.strip() for h in os.environ.get(
    "SNIPER_SIGNAL_HORIZONS", "1d,eod,120m,90m,60m").split(",") if h.strip()]
# |signed_move| (ATR units) that maps to ~0.66 confidence via tanh(mag/scale).
CONF_SCALE = float(os.environ.get("SNIPER_SIGNAL_CONF_SCALE", "0.8"))


def _append(rec: dict) -> None:
    rec = {**rec, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _finite(value) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _signal_from_prediction(out: dict) -> dict | None:
    """Reduce the per-horizon estimator output to ONE directional signal.

    Picks the first horizon in SIGNAL_HORIZONS whose head carries a finite
    ``signed_move`` (signed expected excursion in ATR units). direction =
    sign(signed_move); magnitude_atr = |signed_move|; confidence = tanh(mag/scale).
    """
    for tf in SIGNAL_HORIZONS:
        head = out.get(tf)
        if not isinstance(head, dict):
            continue
        sm = _finite(head.get("signed_move"))
        if sm is None:
            continue
        mag = abs(sm)
        direction = "LONG" if sm > 0 else "SHORT" if sm < 0 else "FLAT"
        confidence = math.tanh(mag / max(1e-6, CONF_SCALE)) if mag > 0 else 0.0
        return {
            "direction": direction,
            "magnitude_atr": round(mag, 5),
            "confidence": round(confidence, 5),
            "horizon": tf,
            "signed_move": round(sm, 5),
            "up_atr": _finite(head.get("up_excursion") if "up_excursion" in head else head.get("up")),
            "down_atr": _finite(head.get("down_excursion") if "down_excursion" in head else head.get("down")),
        }
    return None


def _post_sniper_signal(symbol: str, out: dict, decision_time) -> None:
    """POST the reduced signal to the backend's sniper-signal ingest endpoint."""
    if not POST_SIGNALS:
        return
    sig = _signal_from_prediction(out)
    if sig is None:
        return
    dt = getattr(decision_time, "isoformat", lambda: str(decision_time))()
    payload = {"symbol": symbol, "decision_time": dt, "model": os.path.basename(MODEL), **sig}
    url = f"{BACKEND_URL}/api/auction-intelligence/sniper-signal"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
        print(f"  {symbol} -> AI overlay: {sig['direction']} mag={sig['magnitude_atr']:.2f}ATR "
              f"conf={sig['confidence']:.2f} @{sig['horizon']} (stored={resp.get('stored')})")
    except Exception as e:  # noqa: BLE001
        print(f"  {symbol} sniper-signal POST failed: {repr(e)[:90]}")


def _drop_price_outliers(df: pd.DataFrame, tol: float = 0.20) -> pd.DataFrame:
    """Drop garbage prints: rows whose o/h/l/c deviate > tol from the per-session MEDIAN close.

    `underlying_spot_candles` carries occasional corrupt rows (e.g. a NIFTY close of 53,362 when
    spot is ~23,300 — likely a cross-symbol ingest bug). A single such print inflates the session
    high/low → ATR explodes → every ATR-normalized feature is poisoned. An index almost never moves
    >20% intraday (circuit breakers), so a per-session ±20% band around the median is a safe filter.
    """
    if df.empty:
        return df
    med = df.groupby(df.index.date)["close"].transform("median")
    lo, hi = med * (1 - tol), med * (1 + tol)
    keep = (df["close"].between(lo, hi) & df["high"].between(lo, hi)
            & df["low"].between(lo, hi) & df["open"].between(lo, hi))
    return df[keep.values]


def _rth_close_stamp(df: pd.DataFrame, cols: list[str], *, outlier_tol: float | None = None) -> pd.DataFrame:
    """IST-index, RTH-filter (09:15–15:30), dedup, (optional) outlier-drop, close-stamp."""
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[cols]
    mins = df.index.hour * 60 + df.index.minute
    df = df[(mins >= 9 * 60 + 15) & (mins <= 15 * 60 + 30)]
    df = df[~df.index.duplicated(keep="last")]
    if outlier_tol is not None:
        df = _drop_price_outliers(df, outlier_tol)
    return close_stamp(df) if not df.empty else df


async def _fetch_bars(conn: asyncpg.Connection, underlying: str) -> pd.DataFrame | None:
    rows = await conn.fetch(
        """select time, open, high, low, close, volume
             from underlying_spot_candles
            where underlying = $1 and interval = '1minute'
              and time >= now() - ($2::int * interval '1 day')
            order by time""",
        underlying, LOOKBACK_DAYS,
    )
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("time").sort_index()
    df = _rth_close_stamp(df, ["open", "high", "low", "close", "volume"], outlier_tol=0.20)
    return df if not df.empty else None


def _ref_spot(spot_bars: pd.DataFrame, session_date) -> float | None:
    day = spot_bars[spot_bars.index.date == session_date]
    if day.empty:
        return None
    ref_ts = pd.Timestamp.combine(session_date, ATM_REF).tz_localize("Asia/Kolkata")
    before = day[day.index <= ref_ts]
    return float((before if not before.empty else day)["close"].iloc[-1 if not before.empty else 0])


async def _fetch_atm_series(conn, underlying: str, session_date, spot_bars) -> AtmSeries | None:
    """Build an AtmSeries (ATM CE/PE/straddle) for the session from option_premium_candles.

    Picks the nearest expiry >= session_date, the densest interval available for it, and the ATM
    strike nearest the 09:20 spot. Returns None if no option data; a partial AtmSeries (no ce/pe) if
    the chosen ATM strike lacks both legs — families C/D then degrade to null, as designed.
    """
    rows = await conn.fetch(
        """select time, interval, expiry, strike, option_type, open, high, low, close, volume, oi, iv
             from option_premium_candles
            where underlying = $1 and time::date = $2
            order by time""",
        underlying, session_date,
    )
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time", "interval", "expiry", "strike", "option_type",
                                     "open", "high", "low", "close", "volume", "oi", "iv"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["strike"] = df["strike"].astype(float)
    df["option_type"] = df["option_type"].str.upper()

    ref = _ref_spot(spot_bars, session_date)
    if ref is None:
        return None

    def _leg(di: pd.DataFrame, atm: float, ot: str) -> pd.DataFrame | None:
        d = di[(di["strike"] == atm) & (di["option_type"] == ot)].copy().set_index("time").sort_index()
        if d.empty:
            return None
        return _rth_close_stamp(d, ["open", "high", "low", "close", "volume", "oi", "iv"])

    # Walk eligible expiries NEAREST-first (match training's nearest-expiry tenor), and within each,
    # the densest interval first. The episodic option ingest leaves the expiring contract thin on
    # expiry day, so require the ATM strike to actually carry BOTH legs (≥3 bars) — else fall to the
    # next expiry. Keeps tenor as short as the data supports without returning an empty chain.
    expiries = sorted(set(df["expiry"]))
    eligible = [e for e in expiries if e >= session_date] or expiries
    for expiry in eligible:
        de = df[df["expiry"] == expiry]
        for interval in de["interval"].value_counts().index:
            di = de[de["interval"] == interval]
            strikes = sorted(set(di["strike"]))
            if not strikes:
                continue
            atm = min(strikes, key=lambda s: abs(s - ref))
            ce, pe = _leg(di, atm, "CE"), _leg(di, atm, "PE")
            if ce is not None and pe is not None and len(ce) >= 3 and len(pe) >= 3:
                joined = ce[["close"]].join(pe[["close"]], lsuffix="_ce", rsuffix="_pe", how="inner")
                straddle = (joined["close_ce"] + joined["close_pe"]).rename("straddle")
                return AtmSeries(underlying=underlying, session_date=session_date, strike=float(atm),
                                 expiry=expiry, ce=ce, pe=pe, straddle=straddle)
    return None


def _fetch_order_flow(symbol: str) -> dict | None:
    """GET the backend's live order flow (analysis.order_flow). None on any failure (graceful)."""
    url = f"{BACKEND_URL}/api/auction-intelligence/live-snapshot?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read().decode())
        of = (d.get("analysis") or {}).get("order_flow") or d.get("order_flow")
        return of or None
    except Exception as e:  # noqa: BLE001
        print(f"  {symbol} order_flow fetch failed: {repr(e)[:90]}")
        return None


async def main(symbols: tuple[str, ...]) -> int:
    lane = SniperEstimatorLane(MODEL, shadow_sink=_append)
    conn = await asyncpg.connect(DSN)
    n_ok = 0
    try:
        for s in symbols:
            try:
                bars = await _fetch_bars(conn, s)
                if bars is None or bars.empty:
                    print(f"{s}: no bars"); continue
                t = bars.index[-1]
                atr = atr_reference(bars, t.date())
                atm = await _fetch_atm_series(conn, s, t.date(), bars)
                of = _fetch_order_flow(s)
                out = lane.predict(decision_time=t, bars=bars, atr_ref=atr, atm_series=atm,
                                   of_snapshot=of, spot_bars=bars, symbol=s)
                sm = out.get("1d", {}).get("signed_move")
                print(f"{s}: bars={len(bars)} last={t} atr={round(atr,1) if atr else atr} "
                      f"opt={'Y' if (atm and atm.available) else 'n'} of={'Y' if of else 'n'} "
                      f"1d_signed={sm}")
                _post_sniper_signal(s, out, t)
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"{s}: ERR {repr(e)[:160]}")
    finally:
        await conn.close()
    print(f"done ok={n_ok}/{len(symbols)} log={LOG}")
    return 0 if n_ok else 1


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "ALL").upper()
    syms = SYMBOLS if arg == "ALL" else (arg,)
    raise SystemExit(asyncio.run(main(syms)))
