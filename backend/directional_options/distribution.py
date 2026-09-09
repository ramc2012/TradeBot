"""Read-only distribution desk over the canonical index-vol substrate.

No broker calls and no refits. Content-addressed derived curves are shared by
API workers. Refused/latest fits stay visible; older good data never passes
for the latest surface. Density is risk-neutral and truncated, not a forecast.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.vol.blackscholes import black76_greeks, black76_price
from directional_options.vol.svi import SVIParams, gatheral_g
from mp_core.cache import cached_json, fingerprint

INDICES = {"NIFTY", "BANKNIFTY", "SENSEX"}


def surface_curves(row: dict[str, Any]) -> dict[str, Any]:
    """Gatheral density on observed support only; never renormalise tails."""
    empty = {"status": "unavailable", "smile": [], "scenarios": [], "greeks": None,
             "density_mass": None, "reason": row.get("reason") or "Latest surface did not pass quality checks"}
    if row.get("fit_status") != "ok" or row.get("butterfly_ok") is not True or row.get("calendar_ok") is False:
        return empty
    try:
        vals = [float(row[k]) for k in ("svi_a", "svi_b", "svi_rho", "svi_m", "svi_s", "forward", "tenor_years", "k_min", "k_max")]
        if not all(map(math.isfinite, vals)):
            return empty
        a, b, rho, m, s, forward, T, lo, hi = vals
        if T <= 0 or forward <= 0 or lo >= hi or s <= 0 or b < 0 or abs(rho) >= 1:
            return empty
        params = SVIParams(a, b, rho, m, s)
        k = np.linspace(lo, hi, 161)
        w = np.asarray(params.total_variance(k))
        g = gatheral_g(params, k)
        if np.any(w <= 0) or np.any(g < -1e-10) or not np.all(np.isfinite(g)):
            return {**empty, "reason": "Density check failed on display grid"}
        strikes = forward * np.exp(k)
        d2 = -k / np.sqrt(w) - np.sqrt(w) / 2
        density = np.maximum(g, 0) * np.exp(-d2*d2/2) / (strikes * np.sqrt(2*math.pi*w))
        mass = float(np.trapezoid(density, strikes) if hasattr(np, "trapezoid") else np.trapz(density, strikes))
        if not math.isfinite(mass) or mass > 1.005:
            return {**empty, "reason": "Integrated density exceeds one; quarantined for display"}
        smile = [{"strike": float(K), "moneyness_pct": float((K/forward-1)*100),
                  "iv_pct": float(math.sqrt(v/T)*100), "density": float(q)} for K, v, q in zip(strikes, w, density)]
        result = {"status": "ok", "reason": "Checks cover the fitted strike range; no global arbitrage guarantee",
                  "smile": smile, "density_mass": mass, "scenarios": [], "greeks": None}
        # ATM scenarios are hypothetical unit calls/puts, never held-book MTM.
        if lo <= 0 <= hi:
            iv = float(params.implied_vol(0, T))
            result["greeks"] = {kind: asdict(black76_greeks(forward, forward, T, iv, kind)) for kind in ("CE", "PE")}
            for move in (-3, -2, -1, 0, 1, 2, 3):
                for vol in (-2, 0, 2):
                    future_iv = iv + vol / 100
                    if future_iv <= 0:
                        continue
                    cell = {"move_pct": move, "vol_points": vol}
                    for kind in ("CE", "PE"):
                        base = black76_price(forward, forward, T, iv, kind)
                        future = black76_price(forward*(1+move/100), forward, max(T-1/365, 0), future_iv, kind)
                        cell[kind] = round(future-base, 4)
                    result["scenarios"].append(cell)
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        return empty


async def distribution_desk(underlying: str, days: int = 30) -> dict[str, Any]:
    symbol = underlying.strip().upper()
    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {"underlying": symbol, "generated_at": now.isoformat(),
        "execution_mode": "paper", "allow_live_orders": False,
        "source": "shared_persisted_index_vol", "surface_scope": "index_only",
        "slices": [], "series": [], "quarantine": [], "participant_oi": [],
        "curves": surface_curves({}), "as_of": None, "status": "no_data",
        "limitations": ["Risk-neutral density is not a physical probability forecast; omitted tails are not normalised.",
            "Scenarios: hypothetical ATM, one calendar day, sticky-strike IV, zero rates; per unit before costs.",
            "Front IV rolls with expiry. Only grid/30 is a constant 30-day coordinate.",
            "Leg-linked flow, counterparty identity and synthetic MBO are unavailable from this feed.",
            "Minimum-variance delta needs a validated conditional volatility model; BS delta is not relabelled as MV."]}
    if symbol not in INDICES:
        result.update(status="unsupported_stock_surface", source="stock_spot_futures_and_payoff_geometry")
        return result
    async with AsyncSessionLocal() as session:
        # Substrate is optional. Its absence must not break the paper-book UI.
        exists = (await session.execute(text("SELECT to_regclass('public.index_vol_surface_slices')"))).scalar()
        if not exists:
            return result
        params = {"u": symbol, "since": now-timedelta(days=days), "now": now}
        slices = [dict(r) for r in (await session.execute(text("""
            SELECT * FROM index_vol_surface_slices
            WHERE underlying=:u AND ts=(SELECT max(ts) FROM index_vol_surface_slices WHERE underlying=:u AND ts<=:now)
            ORDER BY expiry
        """), params)).mappings()]
        series = [dict(r) for r in (await session.execute(text("""
            SELECT ts, tenor_days, tenor_kind, atm_iv, rr_25, bf_25, realized_vol,
                   rv_window_days, rv_estimator, rv_percentile, vrp_spread, status, reason
            FROM index_vol_tenor_metrics WHERE underlying=:u AND ts>=:since AND ts<=:now
            AND (tenor_kind='front' OR (tenor_kind='grid' AND tenor_days=30))
            ORDER BY ts, tenor_kind
        """), params)).mappings()]
        quarantine = [dict(r) for r in (await session.execute(text("""
            SELECT stage, status, count(*) AS n FROM index_vol_quarantine
            WHERE underlying=:u AND ts>=:since AND ts<=:now GROUP BY stage,status ORDER BY n DESC
        """), params)).mappings()]
        if (await session.execute(text("SELECT to_regclass('public.participant_oi')"))).scalar():
            result["participant_oi"] = [dict(r) for r in (await session.execute(text("""
                SELECT dt, participant, bucket, long_contracts, short_contracts, source
                FROM participant_oi WHERE dt=(SELECT max(dt) FROM participant_oi WHERE dt <= CURRENT_DATE)
                ORDER BY participant,bucket
            """))).mappings()]
    result.update(slices=slices, series=series, quarantine=quarantine,
                  participant_scope="NSE market-wide instrument classes; not per symbol and not BSE SENSEX")
    if slices:
        stamp = slices[0]["ts"]
        age = (now-stamp).total_seconds()
        usable = next((r for r in slices if r["fit_status"] == "ok" and r["butterfly_ok"] and r["calendar_ok"] is not False), slices[0])
        curves = await asyncio.to_thread(cached_json, "directional-distribution-v1", usable, lambda: surface_curves(usable))
        result.update(as_of=stamp.isoformat(), age_seconds=round(age), curves=curves,
                      selected_expiry=str(usable["expiry"]), snapshot_id=fingerprint(usable),
                      status="historical" if age > 3600 else ("available" if curves["status"] == "ok" else "quarantined"))
    return result
