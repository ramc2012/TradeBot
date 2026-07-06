"""Daily option-positioning feed for the positional directional lane.

The positional strategy's EDGE is option positioning CHANGE — call-vs-put OI build
(oi_build_bias), PCR-OI level, and ATM-IV trend (d_atm_iv, the mandatory long-
premium vol gate). The live lane only sees a single chain snapshot per cycle, so
the day-over-day change is invisible to it. This module computes per-underlying
DAILY positioning from option_premium_candles (front-month chain, EOD snapshot)
and persists it to `directional_positioning_daily`, so the live decision path can
read the latest positioning + its change. Write-once/idempotent (upsert by
(underlying, date)); a post-close runner appends each session.

CLI:  python -m directional_options.positioning_feed NIFTY BANKNIFTY COALINDIA ...
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

_DDL = """
CREATE TABLE IF NOT EXISTS directional_positioning_daily (
    underlying      text NOT NULL,
    d               date NOT NULL,
    ce_oi           double precision,
    pe_oi           double precision,
    pcr_oi          double precision,
    oi_build_bias   double precision,
    atm_iv          double precision,
    d_atm_iv        double precision,
    d_pcr_oi        double precision,
    spot            double precision,
    htf_up          boolean,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (underlying, d)
)
"""


def _bs_price(spot: float, strike: float, t: float, sigma: float, is_call: bool, r: float = 0.065) -> float:
    import math
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2.0) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    n = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    if is_call:
        return spot * n(d1) - strike * math.exp(-r * t) * n(d2)
    return strike * math.exp(-r * t) * n(-d2) - spot * n(-d1)


def _implied_vol(premium: float, spot: float, strike: float, t: float, is_call: bool) -> float | None:
    """ATM implied vol by bisection. The broker iv column died 2026-06-23, so the
    feed derives IV uniformly from the EOD ATM premium — one convention across the
    whole series, which is what d_atm_iv (a day-over-day diff) needs."""
    if premium <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    lo, hi = 0.01, 3.0
    if not (_bs_price(spot, strike, t, lo, is_call) <= premium <= _bs_price(spot, strike, t, hi, is_call)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _bs_price(spot, strike, t, mid, is_call) < premium:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0 * 100.0, 4)  # percent, matching broker iv convention


async def _ensure_table() -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(text(_DDL))
        # idempotent add for an already-created table from the first revision
        await s.execute(text("ALTER TABLE directional_positioning_daily ADD COLUMN IF NOT EXISTS spot double precision"))
        await s.execute(text("ALTER TABLE directional_positioning_daily ADD COLUMN IF NOT EXISTS htf_up boolean"))
        await s.commit()


async def compute_and_store(underlying: str, *, lookback_sessions: int = 120) -> dict:
    """Compute daily positioning for `underlying` over the recent window and upsert.

    Front-month chain (smallest expiry with DTE in [3,45]) per day; EOD snapshot
    (last 30-min bar per contract). Returns a small report.
    """
    u = underlying.upper()
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            SELECT timezone('Asia/Kolkata', time)::date AS d, expiry, strike, option_type,
                   oi, close, underlying_price,
                   row_number() OVER (PARTITION BY timezone('Asia/Kolkata', time)::date, expiry, strike, option_type
                                      ORDER BY time DESC) AS rn
            FROM option_premium_candles
            WHERE underlying = :u AND interval = '30minute' AND oi IS NOT NULL
            """
        ), {"u": u})).all()
        # Canonical daily spot closes. The option rows' underlying_price column
        # is best-effort (it stopped populating 2026-06-22, which froze the EMA
        # backbone and nulled d_atm_iv via the NaN ATM-distance); the spot store
        # is the authoritative series, option-scraped spot only a fallback.
        spot_rows = (await s.execute(text(
            """
            SELECT DISTINCT ON (timezone('Asia/Kolkata', time)::date)
                   timezone('Asia/Kolkata', time)::date AS d, close
            FROM underlying_spot_candles
            WHERE underlying = :u AND interval = '30minute' AND close IS NOT NULL
            ORDER BY timezone('Asia/Kolkata', time)::date, time DESC
            """
        ), {"u": u})).all()
    if not rows:
        return {"underlying": u, "stored": 0, "reason": "no option data"}
    spot_by_day = {r.d: float(r.close) for r in spot_rows}
    df = pd.DataFrame(rows, columns=["d", "expiry", "strike", "option_type", "oi", "close", "spot", "rn"])
    df = df[df["rn"] == 1].drop(columns=["rn"])  # EOD snapshot per contract
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0.0)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["strike"] = df["strike"].astype(float)
    df["spot"] = df["spot"].astype(float)
    df["dte"] = (pd.to_datetime(df["expiry"]) - pd.to_datetime(df["d"])).dt.days

    recs = []
    for d, day in df.groupby("d"):
        front = day[(day["dte"] >= 3) & (day["dte"] <= 45)]
        if front.empty:
            continue
        exp = front.sort_values("dte")["expiry"].iloc[0]
        chain = front[front["expiry"] == exp]
        ce_oi = float(chain[chain["option_type"] == "CE"]["oi"].sum())
        pe_oi = float(chain[chain["option_type"] == "PE"]["oi"].sum())
        if ce_oi <= 0 and pe_oi <= 0:
            continue
        scraped = float(chain["spot"].iloc[0])
        spot = spot_by_day.get(d, scraped if np.isfinite(scraped) else np.nan)
        if not np.isfinite(spot):
            continue  # no usable spot for the day — a NaN here poisons the EMA backbone
        ch = chain.copy()
        ch["dd"] = (ch["strike"] - spot).abs()
        # ATM IV via BS inversion from the EOD ATM premium — uniform convention
        # for the whole series (the broker iv column stopped populating 06-23).
        atm_iv = np.nan
        ch_px = ch.dropna(subset=["close", "dd"])
        if not ch_px.empty:
            atm_strike = float(ch_px.sort_values("dd")["strike"].iloc[0])
            dte_days = int(ch_px[ch_px["strike"] == atm_strike]["dte"].iloc[0])
            t_years = max(dte_days, 1) / 365.0
            legs = [
                _implied_vol(float(leg["close"]), spot, atm_strike, t_years, str(leg["option_type"]) == "CE")
                for _, leg in ch_px[ch_px["strike"] == atm_strike].iterrows()
            ]
            legs = [v for v in legs if v is not None]
            if legs:
                atm_iv = float(np.mean(legs))
        pcr = pe_oi / ce_oi if ce_oi > 0 else np.nan
        recs.append({"d": d, "ce_oi": ce_oi, "pe_oi": pe_oi, "pcr_oi": pcr, "atm_iv": atm_iv, "spot": spot})
    if not recs:
        return {"underlying": u, "stored": 0, "reason": "no front-month chain days"}
    pos = pd.DataFrame(recs).sort_values("d").reset_index(drop=True).tail(lookback_sessions + 5)
    denom = (pos["ce_oi"] + pos["pe_oi"]).replace(0, np.nan)
    pos["oi_build_bias"] = ((pos["ce_oi"].diff() - pos["pe_oi"].diff()) / denom).astype(float)
    pos["d_atm_iv"] = pos["atm_iv"].diff()
    pos["d_pcr_oi"] = pos["pcr_oi"].diff()
    # Daily HTF trend backbone (sanity/holdability, not the alpha): EMA20 vs EMA50
    # on the daily underlying close — research: trend is direction-context, the
    # positioning is the edge.
    pos["ema20"] = pos["spot"].ewm(span=20, min_periods=10).mean()
    pos["ema50"] = pos["spot"].ewm(span=50, min_periods=20).mean()
    pos["htf_up"] = pos["ema20"] > pos["ema50"]
    pos = pos.tail(lookback_sessions).replace({np.nan: None})

    payload = [
        {
            "u": u, "d": r["d"], "ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"], "pcr_oi": r["pcr_oi"],
            "oib": r["oi_build_bias"], "aiv": r["atm_iv"], "daiv": r["d_atm_iv"], "dpcr": r["d_pcr_oi"],
            "spot": r["spot"], "htf": (None if r["htf_up"] is None else bool(r["htf_up"])),
        }
        for _, r in pos.iterrows()
    ]
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                """
                INSERT INTO directional_positioning_daily
                    (underlying, d, ce_oi, pe_oi, pcr_oi, oi_build_bias, atm_iv, d_atm_iv, d_pcr_oi, spot, htf_up, updated_at)
                VALUES (:u, :d, :ce_oi, :pe_oi, :pcr_oi, :oib, :aiv, :daiv, :dpcr, :spot, :htf, now())
                ON CONFLICT (underlying, d) DO UPDATE SET
                    ce_oi=EXCLUDED.ce_oi, pe_oi=EXCLUDED.pe_oi, pcr_oi=EXCLUDED.pcr_oi,
                    oi_build_bias=EXCLUDED.oi_build_bias, atm_iv=EXCLUDED.atm_iv,
                    d_atm_iv=EXCLUDED.d_atm_iv, d_pcr_oi=EXCLUDED.d_pcr_oi,
                    spot=EXCLUDED.spot, htf_up=EXCLUDED.htf_up, updated_at=now()
                """
            ),
            payload,
        )
        await s.commit()
    return {"underlying": u, "stored": len(payload), "first": str(payload[0]["d"]), "last": str(payload[-1]["d"])}


def _positioning_is_stale(row_d) -> bool:
    """True when the stored positioning date lags the most-recent FINALIZED
    NSE session by more than DIRECTIONAL_POSITIONAL_MAX_STALE_SESSIONS sessions.

    Today counts as finalized only after ~15:35 IST, so during a live session
    the prior session's row is correctly considered fresh. Calendar-based, so
    weekends/holidays never trip it. FAILS CLOSED: an error here marks the feed
    stale, which declines NEW positional entries (held positions still exit on
    stop/target/DTE) — the gate exists precisely for degraded-data conditions,
    so failing open would negate it when it matters most.
    """
    try:
        from datetime import datetime, timedelta, time as _dtime, timezone as _tz
        from core.trading_calendar import trading_calendar
        from core.config import settings as _settings

        ist = _tz(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        last_completed = None
        for off in range(0, 16):
            d = now.date() - timedelta(days=off)
            if trading_calendar.has_exchange_session("NSE", d) and (
                d < now.date() or now.time() >= _dtime(15, 35)
            ):
                last_completed = d
                break
        if last_completed is None or row_d >= last_completed:
            return False
        max_stale = int(getattr(_settings, "DIRECTIONAL_POSITIONAL_MAX_STALE_SESSIONS", 1))
        gap, d = 0, last_completed
        while d > row_d and gap <= max_stale + 2:
            if trading_calendar.has_exchange_session("NSE", d):
                gap += 1
            d -= timedelta(days=1)
        return gap > max_stale
    except Exception:
        return True


async def latest(underlying: str) -> dict | None:
    """Latest stored positioning row for `underlying` (for the live decision path).

    Carries `is_stale` so the signal layer can decline NEW positional entries on
    a stale feed WITHOUT silently reverting to legacy intraday momentum.
    """
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            """
            SELECT d, pcr_oi, oi_build_bias, atm_iv, d_atm_iv, d_pcr_oi, htf_up
            FROM directional_positioning_daily WHERE underlying = :u ORDER BY d DESC LIMIT 1
            """
        ), {"u": underlying.upper()})).first()
    if row is None:
        return None
    return {"d": row.d.isoformat(), "pcr_oi": row.pcr_oi, "oi_build_bias": row.oi_build_bias,
            "atm_iv": row.atm_iv, "d_atm_iv": row.d_atm_iv, "d_pcr_oi": row.d_pcr_oi, "htf_up": row.htf_up,
            "is_stale": _positioning_is_stale(row.d)}


async def main() -> None:
    await _ensure_table()
    names = [a.upper() for a in sys.argv[1:]] or ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    for u in names:
        rep = await compute_and_store(u)
        print(rep)
        lat = await latest(u)
        if lat:
            print(f"   latest {u}: pcr={lat['pcr_oi']}, oi_build={lat['oi_build_bias']}, atm_iv={lat['atm_iv']}, d_atm_iv={lat['d_atm_iv']}")


if __name__ == "__main__":
    asyncio.run(main())
