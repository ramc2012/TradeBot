"""Option-chain analytics for the directional-options RL policy.

Aggregates the existing `option_chain_service` payload into a flat dict
of features the policy can consume. The base payload already has PCR,
max pain, ATM IV, GEX, and per-strike OI/IV/greeks — this module adds
the metrics that aren't pre-computed:

  - **IV skew** = IV(25Δ put) − IV(25Δ call), normalised by ATM IV.
    Positive skew = puts richer (downside hedging demand).
  - **DEX** (delta exposure) = Σ strike × delta × OI × lot_size, summed
    separately for calls (positive) and puts (negative). Approximates
    the dealer-net delta the chain implies if dealers are short
    customer-bought options.
  - **Gamma curve** — gamma × OI per strike, in a 5-strike window each
    side of ATM. Higher gamma near spot → pinning pressure.
  - **OI distribution** — top-3 strikes by OI on each side, with the
    distance from spot in % terms.
  - **OI build classification** — calls long-build / short-build /
    long-unwind / short-cover by combining LTP-change and OI-change
    sign (Σ over the chain).

Every metric is reported as a finite float or `None`. The policy
featurizer treats `None` as "feature unavailable" — typically by
filling with a sentinel value and letting the model learn the
"absent" pattern.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from market_data.option_chain import option_chain_service
from market_data.symbols import DISPLAY_NAMES, to_app_symbol


# Cache-warming approach: we don't refresh the chain ourselves (that
# was destabilising the backend — _refresh holds a lock and computes
# greeks for 200+ strikes per call). Instead we ensure_chain_tracked()
# adds the (symbol, expiry) to option_chain_service._tracked. The
# service's own poll loop refreshes every POLL_INTERVAL (30s) into a
# Redis key with OC_TTL=60s, so once tracked the cache stays warm
# without any per-request work.
#
# First snapshot for a (symbol, expiry): schedules track, returns
# None for chain (policy gets zero chain features for this cycle).
# Within ~30s the poll loop populates Redis. Subsequent snapshots
# hit cache fast and the policy sees the full 16 chain features.


async def ensure_chain_tracked(underlying: str, expiry: str) -> None:
    """Best-effort: register (app_symbol, expiry) with the
    option_chain_service so its poll loop keeps Redis warm.

    First-time setup per (symbol, expiry):
      1. Acquire broker via _get_market_adapter() if not set.
      2. Add (app_symbol, expiry) to the service's tracked list.
      3. Start the poll loop if it isn't running yet.
      4. Fire one immediate `_refresh` so the cache is warm within
         seconds rather than waiting POLL_INTERVAL (30s).

    Subsequent calls for the same (symbol, expiry) early-out fast
    without any broker work — the poll loop maintains freshness from
    then on.

    Failures are silent: if the broker can't be acquired we still add
    to the tracked list (the next time set_broker fires elsewhere, the
    poll loop will pick this entry up).
    """
    if not expiry or not underlying:
        return
    try:
        app_symbol = to_app_symbol(underlying) or underlying
    except Exception:
        app_symbol = underlying
    key = (app_symbol, expiry)
    # Fast path: already tracked — nothing to do.
    if key in getattr(option_chain_service, "_tracked", []):
        return
    # First-time setup. Acquire broker if missing.
    if getattr(option_chain_service, "_broker", None) is None:
        try:
            from api.routers.market import _get_market_adapter
            adapter, _ = await _get_market_adapter()
            if adapter is not None:
                option_chain_service.set_broker(adapter)
                logger.info("[chain_analytics] set broker on option_chain_service")
        except Exception as exc:
            logger.debug(f"[chain_analytics] couldn't acquire broker: {exc}")
    option_chain_service.track(app_symbol, expiry)
    logger.info(
        f"[chain_analytics] tracking {app_symbol} {expiry} "
        f"({len(option_chain_service._tracked)} total)"
    )
    # Start the poll loop if it's not running. Guarded so concurrent
    # callers don't spawn multiple loops — _task is set when start()
    # is called.
    task = getattr(option_chain_service, "_task", None)
    if task is None or (hasattr(task, "done") and task.done()):
        try:
            await option_chain_service.start()
            logger.info("[chain_analytics] started option_chain_service poll loop")
        except Exception as exc:
            logger.warning(f"[chain_analytics] start() failed: {exc}")
    # Fire one immediate refresh so the cache is warm within seconds.
    # Bounded by a 12s timeout (broker cold-call); failures are silent.
    if getattr(option_chain_service, "_broker", None) is not None:
        try:
            asyncio.create_task(_one_shot_refresh(app_symbol, expiry))
        except RuntimeError:
            pass


async def _one_shot_refresh(app_symbol: str, expiry: str) -> None:
    """Bounded one-time _refresh on first track. Failures are silent."""
    try:
        await asyncio.wait_for(
            option_chain_service._refresh(app_symbol, expiry),
            timeout=12.0,
        )
        logger.info(f"[chain_analytics] initial refresh complete for {app_symbol} {expiry}")
    except asyncio.TimeoutError:
        logger.warning(f"[chain_analytics] initial refresh timed out for {app_symbol} {expiry}")
    except Exception as exc:
        logger.warning(f"[chain_analytics] initial refresh failed for {app_symbol} {expiry}: {exc}")


# Lot sizes for index options. Used for DEX/GEX absolute scale. If a
# future expiry changes these we should pull from the contract catalog,
# but they're stable enough day-to-day to hardcode here.
INDEX_LOT_SIZE = {
    "NIFTY": 75,
    "BANKNIFTY": 35,
    "SENSEX": 20,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "BANKEX": 30,
}
_LOT_SIZE_CACHE: dict[str, int] = {}

DEFAULT_RISK_FREE_RATE = 0.065
MIN_TTE_YEARS = 1.0 / (365.0 * 6.5)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        v = float(value)
        if not (v == v):  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


def _net_gex(value: Any) -> Optional[float]:
    """Net gamma exposure as a single scalar.

    The option-chain builder stores `gamma_exposure` as a PER-STRIKE dict
    `{strike: sign·gamma·OI·spot}` (CE +, PE −) — the repo convention. The old
    code did `_safe_float(dict)` which is always None, so GEX was null for every
    underlying (NIFTY, BANKNIFTY, SENSEX) on the panel AND as policy feature 30.
    Net GEX is just the signed sum across strikes. Tolerates a scalar too."""
    if value is None:
        return None
    if isinstance(value, dict):
        total = 0.0
        seen = False
        for v in value.values():
            f = _safe_float(v)
            if f is not None:
                total += f
                seen = True
        return round(total, 2) if seen else None
    return _safe_float(value)


def _round_or_none(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_d1(spot: float, strike: float, tte_years: float, rate: float, sigma: float) -> Optional[float]:
    if spot <= 0 or strike <= 0 or tte_years <= 0 or sigma <= 0:
        return None
    return (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tte_years) / (
        sigma * math.sqrt(tte_years)
    )


def _bs_delta(spot: float, strike: float, tte_years: float, rate: float, sigma: float, option_type: str) -> float:
    d1 = _bs_d1(spot, strike, tte_years, rate, sigma)
    if d1 is None:
        return 0.0
    if option_type.upper() == "PE":
        return _normal_cdf(d1) - 1.0
    return _normal_cdf(d1)


def _bs_gamma(spot: float, strike: float, tte_years: float, rate: float, sigma: float) -> float:
    d1 = _bs_d1(spot, strike, tte_years, rate, sigma)
    if d1 is None:
        return 0.0
    return _normal_pdf(d1) / (spot * sigma * math.sqrt(tte_years))


def _bs_price(spot: float, strike: float, tte_years: float, rate: float, sigma: float, option_type: str) -> float:
    d1 = _bs_d1(spot, strike, tte_years, rate, sigma)
    if d1 is None:
        return max(0.0, spot - strike) if option_type.upper() == "CE" else max(0.0, strike - spot)
    d2 = d1 - sigma * math.sqrt(tte_years)
    discount = math.exp(-rate * tte_years)
    if option_type.upper() == "PE":
        return strike * discount * _normal_cdf(-d2) - spot * _normal_cdf(-d1)
    return spot * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)


def _parse_expiry_date(expiry: Any) -> Optional[date]:
    if isinstance(expiry, date) and not isinstance(expiry, datetime):
        return expiry
    raw = str(expiry or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %b %y"):
        try:
            return datetime.strptime(raw.upper(), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _expiry_state(expiry: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    expiry_date = _parse_expiry_date(expiry)
    if expiry_date is None:
        return {
            "days_to_expiry": None,
            "time_to_expiry_years": MIN_TTE_YEARS,
            "is_expiry_day": False,
            "expiry_mode": "unknown",
            "theta_clock_pct": None,
        }
    today = (now or datetime.now(timezone.utc)).date()
    days = max((expiry_date - today).days, 0)
    tte_years = max(days / 365.0, MIN_TTE_YEARS)
    if days == 0:
        mode = "0dte"
        theta_clock = 1.0
    elif days <= 2:
        mode = "expiry_week"
        theta_clock = max(0.0, min(1.0, (3 - days) / 3))
    else:
        mode = "normal"
        theta_clock = 0.0
    return {
        "days_to_expiry": days,
        "time_to_expiry_years": tte_years,
        "is_expiry_day": days == 0,
        "expiry_mode": mode,
        "theta_clock_pct": theta_clock,
    }


def _interpolate_iv_at_delta(
    entries: list[dict],
    target_delta_abs: float,
    option_type: str,
) -> Optional[float]:
    """Find the IV at a target |delta| by linear interpolation across
    strikes. Used to compute 25-delta skew without needing a full
    surface fit."""
    legs: list[tuple[float, float]] = []
    for e in entries:
        if str(e.get("option_type") or "").upper() != option_type:
            continue
        delta = _safe_float(e.get("delta"))
        iv = _safe_float(e.get("iv"))
        if delta is None or iv is None or iv <= 0.0:
            continue
        legs.append((abs(delta), iv))
    if len(legs) < 2:
        return None
    legs.sort(key=lambda kv: kv[0])
    # If the target sits outside our sampled range, clamp to the nearest leg.
    if target_delta_abs <= legs[0][0]:
        return legs[0][1]
    if target_delta_abs >= legs[-1][0]:
        return legs[-1][1]
    for i in range(len(legs) - 1):
        d0, iv0 = legs[i]
        d1, iv1 = legs[i + 1]
        if d0 <= target_delta_abs <= d1:
            if d1 == d0:
                return (iv0 + iv1) / 2.0
            t = (target_delta_abs - d0) / (d1 - d0)
            return iv0 + t * (iv1 - iv0)
    return None


def _gamma_curve(entries: list[dict], spot: float, window_strikes: int = 5) -> list[dict[str, Any]]:
    """Per-strike gamma × OI in a window centred on the ATM strike."""
    # Resolve ATM via the strike closest to spot among CE entries.
    strikes = sorted({_safe_float(e.get("strike")) for e in entries if _safe_float(e.get("strike")) is not None})
    if not strikes:
        return []
    atm_strike = min(strikes, key=lambda k: abs(k - spot))
    try:
        idx = strikes.index(atm_strike)
    except ValueError:
        return []
    lo = max(0, idx - window_strikes)
    hi = min(len(strikes), idx + window_strikes + 1)
    band = strikes[lo:hi]
    by_strike: dict[float, dict[str, float]] = {k: {"ce_gamma_oi": 0.0, "pe_gamma_oi": 0.0} for k in band}
    for e in entries:
        strike = _safe_float(e.get("strike"))
        if strike is None or strike not in by_strike:
            continue
        gamma = _safe_float(e.get("gamma")) or 0.0
        oi = _safe_float(e.get("oi")) or 0.0
        contribution = abs(gamma) * oi
        side = "ce_gamma_oi" if str(e.get("option_type") or "").upper() == "CE" else "pe_gamma_oi"
        by_strike[strike][side] += contribution
    return [
        {
            "strike": strike,
            "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
            "ce_gamma_oi": by_strike[strike]["ce_gamma_oi"],
            "pe_gamma_oi": by_strike[strike]["pe_gamma_oi"],
            "total_gamma_oi": by_strike[strike]["ce_gamma_oi"] + by_strike[strike]["pe_gamma_oi"],
        }
        for strike in band
    ]


def _top_oi_strikes(entries: list[dict], spot: float, option_type: str, n: int = 3) -> list[dict[str, Any]]:
    rows: list[tuple[float, float]] = []
    for e in entries:
        if str(e.get("option_type") or "").upper() != option_type:
            continue
        strike = _safe_float(e.get("strike"))
        oi = _safe_float(e.get("oi"))
        if strike is None or oi is None:
            continue
        rows.append((strike, oi))
    rows.sort(key=lambda kv: kv[1], reverse=True)
    return [
        {
            "strike": strike,
            "oi": oi,
            "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
        }
        for strike, oi in rows[:n]
    ]


def _classify_oi_build(entries: list[dict], option_type: str) -> dict[str, int]:
    """Aggregate the chain's OI-change signal per option-type into the
    standard four buckets. Strikes where either LTP or OI is missing
    are skipped."""
    counts = {"long_buildup": 0, "short_buildup": 0, "long_unwind": 0, "short_cover": 0}
    for e in entries:
        if str(e.get("option_type") or "").upper() != option_type:
            continue
        ltp_chg = _safe_float(e.get("ltp_change"))
        oi_chg = _safe_float(e.get("oi_change"))
        if ltp_chg is None or oi_chg is None:
            continue
        if ltp_chg > 0 and oi_chg > 0:
            counts["long_buildup"] += 1
        elif ltp_chg < 0 and oi_chg > 0:
            counts["short_buildup"] += 1
        elif ltp_chg < 0 and oi_chg < 0:
            counts["long_unwind"] += 1
        elif ltp_chg > 0 and oi_chg < 0:
            counts["short_cover"] += 1
    return counts


def _compute_dex(entries: list[dict], lot_size: int) -> tuple[float, float, float]:
    """Return (dex_calls, dex_puts, dex_net).

    DEX_calls = +Σ strike × delta × OI × lot     (calls have +ve delta)
    DEX_puts  = +Σ strike × |delta| × OI × lot  (puts have -ve delta;
                                                we report magnitude)
    DEX_net   = DEX_calls − DEX_puts (positive = call-heavy)

    These approximate the dealer notional delta if dealers are short
    customer-bought options.
    """
    dex_calls = 0.0
    dex_puts = 0.0
    for e in entries:
        strike = _safe_float(e.get("strike"))
        delta = _safe_float(e.get("delta"))
        oi = _safe_float(e.get("oi"))
        if strike is None or delta is None or oi is None:
            continue
        contribution = strike * abs(delta) * oi * lot_size
        if str(e.get("option_type") or "").upper() == "CE":
            dex_calls += contribution
        else:
            dex_puts += contribution
    return dex_calls, dex_puts, dex_calls - dex_puts


def _entry_iv(entry: dict[str, Any], atm_iv: Optional[float]) -> Optional[float]:
    iv = _safe_float(entry.get("iv"))
    if iv is None or iv <= 0:
        iv = atm_iv
    if iv is None or iv <= 0:
        return None
    # Some feeds report IV as a percent. The repo's own greeks path uses
    # decimal vols, but this guard makes external broker quirks harmless.
    if iv > 3.0:
        iv /= 100.0
    return max(0.0001, min(iv, 5.0))


async def _resolve_lot_size(underlying: str) -> int:
    """Resolve index and stock lot sizes for absolute exposure scaling.

    Index lots are stable enough to keep in-process. Stock lots move across
    contract revisions, so prefer the broker-populated catalogs when present.
    """
    symbol = str(underlying or "").strip().upper()
    if not symbol:
        return 1
    if symbol in INDEX_LOT_SIZE:
        return int(INDEX_LOT_SIZE[symbol])
    if symbol in _LOT_SIZE_CACHE:
        return _LOT_SIZE_CACHE[symbol]
    lot = 1
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT COALESCE(
                        (
                            SELECT lot_size
                            FROM fo_underlying_catalog
                            WHERE symbol = :symbol
                              AND lot_size IS NOT NULL
                            LIMIT 1
                        ),
                        (
                            SELECT lot_size
                            FROM fo_contract_catalog
                            WHERE underlying = :symbol
                              AND lot_size IS NOT NULL
                              AND expiry >= CURRENT_DATE
                            ORDER BY expiry ASC, updated_at DESC NULLS LAST
                            LIMIT 1
                        )
                    ) AS lot_size
                    """
                ),
                {"symbol": symbol},
            )
            row = result.first()
            resolved = int(float(row.lot_size)) if row and row.lot_size is not None else 1
            if resolved > 0:
                lot = resolved
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[chain_analytics] lot-size lookup failed for {symbol}: {exc}")
    _LOT_SIZE_CACHE[symbol] = lot
    return lot


def _display_underlying(underlying: str, cache_symbol: str | None = None) -> str:
    raw = str(underlying or "").strip().upper()
    if cache_symbol and cache_symbol in DISPLAY_NAMES:
        return DISPLAY_NAMES[cache_symbol].upper()
    if raw in DISPLAY_NAMES:
        return DISPLAY_NAMES[raw].upper()
    if raw.endswith("-INDEX"):
        return raw.rsplit(":", 1)[-1].replace("NIFTY50-INDEX", "NIFTY").replace("-INDEX", "")
    if raw.endswith("-EQ"):
        return raw.rsplit(":", 1)[-1].replace("-EQ", "")
    return raw


async def _catalog_expiries(underlying: str, cache_symbol: str) -> list[str]:
    """Best-effort active expiry ladder from the local F&O catalog."""
    symbol = _display_underlying(underlying, cache_symbol)
    if not symbol:
        return []
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT expiry
                    FROM fo_contract_catalog
                    WHERE underlying = :symbol
                      AND expiry >= CURRENT_DATE
                    ORDER BY expiry ASC
                    LIMIT 12
                    """
                ),
                {"symbol": symbol},
            )
            return [
                row.expiry.isoformat()
                for row in result.fetchall()
                if getattr(row, "expiry", None) is not None
            ]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[chain_analytics] catalog expiry lookup failed for {symbol}: {exc}")
        return []


async def chain_cache_status(underlying: str, expiry: Optional[str] = None) -> dict[str, Any]:
    """Expose cache/expiry diagnostics for the UI and refresh guardrails."""
    try:
        cache_symbol = to_app_symbol(underlying) or underlying
    except Exception:
        cache_symbol = underlying
    known = []
    try:
        known = await option_chain_service.known_expiries(cache_symbol)
    except Exception:
        tracked = list(getattr(option_chain_service, "_tracked", []) or [])
        known = sorted({exp for sym, exp in tracked if sym == cache_symbol})
    catalog = await _catalog_expiries(underlying, cache_symbol)
    combined = list(dict.fromkeys([*(known or []), *(catalog or [])]))
    return {
        "underlying": underlying,
        "cache_symbol": cache_symbol,
        "requested_expiry": expiry,
        "known_expiries": combined,
        "cached_or_tracked_expiries": known,
        "catalog_expiries": catalog,
        "default_expiry": combined[0] if combined else None,
        "tracked_count": len(getattr(option_chain_service, "_tracked", []) or []),
        "poll_running": bool(
            getattr(option_chain_service, "_task", None)
            and not getattr(option_chain_service, "_task").done()
        ),
    }


async def warm_chain_cache(
    underlying: str,
    expiry: Optional[str] = None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Track and synchronously warm a chain cache entry within a small budget."""
    try:
        cache_symbol = to_app_symbol(underlying) or underlying
    except Exception:
        cache_symbol = underlying
    status = await chain_cache_status(underlying, expiry)
    resolved_expiry = expiry or status.get("default_expiry")
    if not resolved_expiry:
        return {**status, "warmed": False, "reason": "no_expiry_available"}

    if getattr(option_chain_service, "_broker", None) is None:
        try:
            from api.routers.market import _get_market_adapter
            adapter, _ = await _get_market_adapter()
            if adapter is not None:
                option_chain_service.set_broker(adapter)
        except Exception as exc:  # noqa: BLE001
            return {**status, "expiry": resolved_expiry, "warmed": False, "reason": f"broker_unavailable: {exc}"}

    option_chain_service.track(cache_symbol, str(resolved_expiry))
    try:
        await option_chain_service.ensure_running()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[chain_analytics] ensure_running failed for {cache_symbol}: {exc}")

    if getattr(option_chain_service, "_broker", None) is None:
        return {**status, "expiry": resolved_expiry, "warmed": False, "reason": "broker_unavailable"}

    try:
        await asyncio.wait_for(
            option_chain_service._refresh(cache_symbol, str(resolved_expiry)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {**status, "expiry": resolved_expiry, "warmed": False, "reason": "refresh_timeout"}
    except Exception as exc:  # noqa: BLE001
        return {**status, "expiry": resolved_expiry, "warmed": False, "reason": f"refresh_failed: {exc}"}
    cached = await option_chain_service.get_cached(cache_symbol, str(resolved_expiry))
    return {
        **await chain_cache_status(underlying, str(resolved_expiry)),
        "expiry": str(resolved_expiry),
        "warmed": bool(cached and cached.get("entries")),
        "reason": None if cached and cached.get("entries") else "empty_after_refresh",
    }


def _strike_band(entries: list[dict], spot: float, each_side: int = 2) -> list[float]:
    strikes = sorted({_safe_float(e.get("strike")) for e in entries if _safe_float(e.get("strike")) is not None})
    if not strikes:
        return []
    if spot <= 0:
        return strikes[: (each_side * 2) + 1]
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    idx = strikes.index(atm)
    lo = max(0, idx - each_side)
    hi = min(len(strikes), idx + each_side + 1)
    return strikes[lo:hi]


def _entry_by_strike_side(entries: list[dict]) -> dict[float, dict[str, dict[str, Any]]]:
    by_strike: dict[float, dict[str, dict[str, Any]]] = {}
    for e in entries:
        strike = _safe_float(e.get("strike"))
        option_type = str(e.get("option_type") or "").upper()
        if strike is None or option_type not in {"CE", "PE"}:
            continue
        by_strike.setdefault(strike, {})[option_type] = e
    return by_strike


def _price_oi_state(entry: Optional[dict[str, Any]]) -> str:
    if not entry:
        return "none"
    ltp_change = _safe_float(entry.get("ltp_change")) or 0.0
    oi_change = _safe_float(entry.get("oi_change")) or 0.0
    oi_change_pct = _safe_float(entry.get("oi_change_pct"))
    if oi_change_pct is not None and oi_change_pct <= -25:
        return "wall_collapse"
    if ltp_change > 0 and oi_change > 0:
        return "long_buildup"
    if ltp_change < 0 and oi_change > 0:
        return "short_buildup"
    if ltp_change < 0 and oi_change < 0:
        return "long_unwind"
    if ltp_change > 0 and oi_change < 0:
        return "short_cover"
    if oi_change > 0:
        return "building"
    if oi_change < 0:
        return "unwinding"
    return "holding"


def _entry_payload(entry: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not entry:
        return {
            "ltp": None,
            "ltp_change_pct": None,
            "oi": 0.0,
            "oi_change": 0.0,
            "oi_change_pct": None,
            "volume": 0.0,
            "iv": None,
            "bid": None,
            "ask": None,
            "spread_pct": None,
            "volume_to_oi": 0.0,
            "state": "none",
        }
    ltp = _safe_float(entry.get("ltp"))
    bid = _safe_float(entry.get("bid"))
    ask = _safe_float(entry.get("ask"))
    oi = _safe_float(entry.get("oi")) or 0.0
    volume = _safe_float(entry.get("volume")) or 0.0
    spread_pct = None
    if bid is not None and ask is not None and ltp and ltp > 0 and ask >= bid:
        spread_pct = (ask - bid) / ltp
    return {
        "ltp": ltp,
        "ltp_change_pct": _safe_float(entry.get("ltp_change_pct")),
        "oi": oi,
        "oi_change": _safe_float(entry.get("oi_change")) or 0.0,
        "oi_change_pct": _safe_float(entry.get("oi_change_pct")),
        "volume": volume,
        "iv": _safe_float(entry.get("iv")),
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "volume_to_oi": volume / max(oi, 1.0),
        "state": _price_oi_state(entry),
    }


def _straddle_and_sigma(
    entries: list[dict],
    spot: float,
    atm_strike: Optional[float],
    atm_iv: Optional[float],
    tte_years: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_strike = _entry_by_strike_side(entries)
    strike = atm_strike
    if strike is None and by_strike and spot > 0:
        strike = min(by_strike, key=lambda k: abs(k - spot))
    call = by_strike.get(strike or 0.0, {}).get("CE") if strike is not None else None
    put = by_strike.get(strike or 0.0, {}).get("PE") if strike is not None else None
    call_ltp = _safe_float((call or {}).get("ltp"))
    put_ltp = _safe_float((put or {}).get("ltp"))
    atm_straddle = None
    if call_ltp is not None and put_ltp is not None:
        atm_straddle = call_ltp + put_ltp

    iv_move = None
    if spot > 0 and atm_iv and atm_iv > 0:
        iv = atm_iv / 100.0 if atm_iv > 3.0 else atm_iv
        iv_move = spot * iv * math.sqrt(max(tte_years, MIN_TTE_YEARS))
    expected_move = atm_straddle if atm_straddle is not None and atm_straddle > 0 else iv_move
    one_sigma = iv_move if iv_move is not None else expected_move

    straddle = {
        "atm_strike": strike,
        "call_ltp": call_ltp,
        "put_ltp": put_ltp,
        "atm_straddle": atm_straddle,
        "expected_move": expected_move,
        "expected_move_pct": (expected_move / spot) if spot > 0 and expected_move is not None else None,
        "upper": spot + expected_move if spot > 0 and expected_move is not None else None,
        "lower": spot - expected_move if spot > 0 and expected_move is not None else None,
        "source": "atm_straddle" if atm_straddle is not None else "atm_iv" if iv_move is not None else "unavailable",
    }
    sigma = {
        "one_sigma": one_sigma,
        "minus_one_sigma": spot - one_sigma if spot > 0 and one_sigma is not None else None,
        "plus_one_sigma": spot + one_sigma if spot > 0 and one_sigma is not None else None,
        "two_sigma": (2.0 * one_sigma) if one_sigma is not None else None,
        "minus_two_sigma": spot - (2.0 * one_sigma) if spot > 0 and one_sigma is not None else None,
        "plus_two_sigma": spot + (2.0 * one_sigma) if spot > 0 and one_sigma is not None else None,
        "source": "atm_iv" if iv_move is not None else straddle["source"],
    }
    return straddle, sigma


def _ntm_volx(entries: list[dict], spot: float, band: list[float]) -> dict[str, Any]:
    by_strike = _entry_by_strike_side(entries)
    ce_volume = pe_volume = ce_oi = pe_oi = ce_oi_change = pe_oi_change = 0.0
    rows: list[dict[str, Any]] = []
    for strike in band:
        ce = _entry_payload(by_strike.get(strike, {}).get("CE"))
        pe = _entry_payload(by_strike.get(strike, {}).get("PE"))
        ce_volume += ce["volume"]
        pe_volume += pe["volume"]
        ce_oi += ce["oi"]
        pe_oi += pe["oi"]
        ce_oi_change += ce["oi_change"]
        pe_oi_change += pe["oi_change"]
        rows.append(
            {
                "strike": strike,
                "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
                "ce_volume": ce["volume"],
                "pe_volume": pe["volume"],
                "ce_oi": ce["oi"],
                "pe_oi": pe["oi"],
                "ce_oi_change": ce["oi_change"],
                "pe_oi_change": pe["oi_change"],
            }
        )
    total_volume = ce_volume + pe_volume
    total_oi = ce_oi + pe_oi
    volume_imbalance = (ce_volume - pe_volume) / total_volume if total_volume > 0 else None
    oi_imbalance = (pe_oi - ce_oi) / total_oi if total_oi > 0 else None
    vxr = abs(volume_imbalance) if volume_imbalance is not None else None
    if volume_imbalance is None:
        control = "unknown"
    elif volume_imbalance >= 0.12:
        control = "call_volume_control"
    elif volume_imbalance <= -0.12:
        control = "put_volume_control"
    else:
        control = "balanced_range_control"
    pressure = "expanding" if (vxr or 0.0) >= 0.35 else "controlled" if (vxr or 0.0) <= 0.12 else "watch"
    return {
        "band_strikes": band,
        "ce_volume": round(ce_volume, 2),
        "pe_volume": round(pe_volume, 2),
        "ce_oi": round(ce_oi, 2),
        "pe_oi": round(pe_oi, 2),
        "ce_oi_change": round(ce_oi_change, 2),
        "pe_oi_change": round(pe_oi_change, 2),
        "volume_imbalance": _round_or_none(volume_imbalance, 4),
        "oi_imbalance": _round_or_none(oi_imbalance, 4),
        "vxr": _round_or_none(vxr, 4),
        "control": control,
        "pressure": pressure,
        "rows": rows,
    }


def _spectrum(
    entries: list[dict],
    spot: float,
    *,
    row_limit: int = 21,
) -> dict[str, Any]:
    by_strike = _entry_by_strike_side(entries)
    strikes = sorted(by_strike)
    if spot > 0 and len(strikes) > row_limit:
        atm = min(strikes, key=lambda strike: abs(strike - spot))
        idx = strikes.index(atm)
        half = row_limit // 2
        lo = max(0, idx - half)
        hi = min(len(strikes), lo + row_limit)
        lo = max(0, hi - row_limit)
        strikes = strikes[lo:hi]
    rows: list[dict[str, Any]] = []
    for strike in strikes:
        ce = _entry_payload(by_strike.get(strike, {}).get("CE"))
        pe = _entry_payload(by_strike.get(strike, {}).get("PE"))
        rows.append(
            {
                "strike": strike,
                "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
                "ce_oi": ce["oi"],
                "pe_oi": pe["oi"],
                "ce_oi_change": ce["oi_change"],
                "pe_oi_change": pe["oi_change"],
                "ce_volume": ce["volume"],
                "pe_volume": pe["volume"],
                "ce_state": ce["state"],
                "pe_state": pe["state"],
                "net_oi_change": ce["oi_change"] - pe["oi_change"],
                "wall_balance": pe["oi"] - ce["oi"],
            }
        )
    call_wall = max(rows, key=lambda r: r["ce_oi"], default=None)
    put_wall = max(rows, key=lambda r: r["pe_oi"], default=None)
    ce_change = sum(r["ce_oi_change"] for r in rows)
    pe_change = sum(r["pe_oi_change"] for r in rows)
    if ce_change > pe_change * 1.15:
        pressure_side = "call_writing_building"
    elif pe_change > ce_change * 1.15:
        pressure_side = "put_writing_building"
    else:
        pressure_side = "balanced"
    return {
        "rows": rows,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "ce_oi_change": round(ce_change, 2),
        "pe_oi_change": round(pe_change, 2),
        "pressure_side": pressure_side,
    }


def _options_table_rows(entries: list[dict], spot: float, *, row_limit: int = 15) -> list[dict[str, Any]]:
    by_strike = _entry_by_strike_side(entries)
    strikes = sorted(by_strike)
    if spot > 0 and len(strikes) > row_limit:
        atm = min(strikes, key=lambda strike: abs(strike - spot))
        idx = strikes.index(atm)
        half = row_limit // 2
        lo = max(0, idx - half)
        hi = min(len(strikes), lo + row_limit)
        lo = max(0, hi - row_limit)
        strikes = strikes[lo:hi]

    def acceptance(payload: dict[str, Any]) -> str:
        state = str(payload.get("state") or "none")
        volume_to_oi = float(payload.get("volume_to_oi") or 0.0)
        if state == "long_buildup" and volume_to_oi >= 0.15:
            return "new_business"
        if state == "short_buildup" and volume_to_oi >= 0.15:
            return "writing_acceptance"
        if state == "short_cover":
            return "covering_pressure"
        if state == "wall_collapse":
            return "wall_collapse"
        if volume_to_oi >= 0.35:
            return "volume_probe"
        return state

    rows: list[dict[str, Any]] = []
    for strike in strikes:
        ce = _entry_payload(by_strike.get(strike, {}).get("CE"))
        pe = _entry_payload(by_strike.get(strike, {}).get("PE"))
        rows.append(
            {
                "strike": strike,
                "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
                "ce": {**ce, "acceptance": acceptance(ce)},
                "pe": {**pe, "acceptance": acceptance(pe)},
            }
        )
    return rows


def _writer_cash_proxy(entries: list[dict], lot_size: int) -> dict[str, Any]:
    ce_cash = pe_cash = ce_unwind = pe_unwind = 0.0
    for e in entries:
        option_type = str(e.get("option_type") or "").upper()
        ltp = _safe_float(e.get("ltp")) or 0.0
        oi_change = _safe_float(e.get("oi_change")) or 0.0
        cash = abs(oi_change) * ltp * lot_size
        if option_type == "CE":
            if oi_change >= 0:
                ce_cash += cash
            else:
                ce_unwind += cash
        elif option_type == "PE":
            if oi_change >= 0:
                pe_cash += cash
            else:
                pe_unwind += cash
    return {
        "ce_add_cash": round(ce_cash, 2),
        "pe_add_cash": round(pe_cash, 2),
        "ce_unwind_cash": round(ce_unwind, 2),
        "pe_unwind_cash": round(pe_unwind, 2),
        "net_writer_cash": round(pe_cash - ce_cash, 2),
        "dominant_side": "put_writers" if pe_cash > ce_cash * 1.15 else "call_writers" if ce_cash > pe_cash * 1.15 else "balanced",
    }


def _gamma_density_summary(trace_exposures: list[dict[str, Any]], spot: float) -> dict[str, Any]:
    if not trace_exposures:
        return {
            "peak_strike": None,
            "convexity": "unknown",
            "left_tail": 0.0,
            "right_tail": 0.0,
            "skew": None,
        }
    peak = max(trace_exposures, key=lambda row: abs(float(row.get("net_gamma_exposure") or 0.0)))
    left_tail = sum(abs(float(row.get("net_gamma_exposure") or 0.0)) for row in trace_exposures if float(row.get("strike") or 0.0) < spot)
    right_tail = sum(abs(float(row.get("net_gamma_exposure") or 0.0)) for row in trace_exposures if float(row.get("strike") or 0.0) > spot)
    total = left_tail + right_tail
    skew = (right_tail - left_tail) / total if total > 0 else None
    if skew is None:
        convexity = "balanced"
    elif skew >= 0.18:
        convexity = "call_side_tail"
    elif skew <= -0.18:
        convexity = "put_side_tail"
    else:
        convexity = "balanced"
    return {
        "peak_strike": peak.get("strike"),
        "peak_gamma_exposure": peak.get("net_gamma_exposure"),
        "convexity": convexity,
        "left_tail": round(left_tail, 2),
        "right_tail": round(right_tail, 2),
        "skew": _round_or_none(skew, 4),
    }


def _dealer_gex_for_spot(
    entries: list[dict],
    grid_spot: float,
    *,
    lot_size: int,
    tte_years: float,
    atm_iv: Optional[float],
    rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    total = 0.0
    if grid_spot <= 0:
        return total
    scale = lot_size * grid_spot * grid_spot * 1e-4
    for e in entries:
        strike = _safe_float(e.get("strike"))
        oi = _safe_float(e.get("oi"))
        if strike is None or oi is None or oi <= 0:
            continue
        sigma = _entry_iv(e, atm_iv)
        gamma = _bs_gamma(grid_spot, strike, tte_years, rate, sigma) if sigma else (_safe_float(e.get("gamma")) or 0.0)
        sign = 1.0 if str(e.get("option_type") or "").upper() == "CE" else -1.0
        total += sign * gamma * oi * scale
    return total


def _repriced_gamma_profile(
    entries: list[dict],
    spot: float,
    *,
    lot_size: int,
    tte_years: float,
    atm_iv: Optional[float],
    steps: int = 41,
    width_pct: float = 0.10,
) -> tuple[list[dict[str, float]], Optional[float]]:
    if spot <= 0 or not entries:
        return [], None
    lo = spot * (1.0 - width_pct)
    hi = spot * (1.0 + width_pct)
    points: list[dict[str, float]] = []
    for idx in range(max(steps, 2)):
        grid_spot = lo + ((hi - lo) * idx) / (max(steps, 2) - 1)
        gex = _dealer_gex_for_spot(
            entries,
            grid_spot,
            lot_size=lot_size,
            tte_years=tte_years,
            atm_iv=atm_iv,
        )
        points.append({"spot": round(grid_spot, 2), "gex": round(gex, 2)})

    crossings: list[float] = []
    for prev, cur in zip(points, points[1:]):
        g0 = prev["gex"]
        g1 = cur["gex"]
        if g0 == 0:
            crossings.append(prev["spot"])
        elif (g0 < 0 < g1) or (g0 > 0 > g1):
            weight = abs(g0) / max(abs(g0) + abs(g1), 1e-12)
            crossings.append(prev["spot"] + (cur["spot"] - prev["spot"]) * weight)
    if not crossings:
        return points, None
    return points, round(min(crossings, key=lambda level: abs(level - spot)), 2)


def _per_strike_exposure(
    entries: list[dict],
    spot: float,
    *,
    lot_size: int,
    tte_years: float,
    atm_iv: Optional[float],
) -> list[dict[str, Any]]:
    by_strike: dict[float, dict[str, float]] = {}
    if spot <= 0:
        return []
    for e in entries:
        strike = _safe_float(e.get("strike"))
        oi = _safe_float(e.get("oi")) or 0.0
        if strike is None or oi <= 0:
            continue
        option_type = str(e.get("option_type") or "").upper()
        sigma = _entry_iv(e, atm_iv)
        delta = _safe_float(e.get("delta"))
        gamma = _safe_float(e.get("gamma"))
        if sigma:
            delta = _bs_delta(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma, option_type)
            gamma = _bs_gamma(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma)
        delta = delta or 0.0
        gamma = gamma or 0.0
        row = by_strike.setdefault(
            strike,
            {
                "ce_oi": 0.0,
                "pe_oi": 0.0,
                "net_delta_exposure": 0.0,
                "net_gamma_exposure": 0.0,
                "net_vanna_exposure": 0.0,
                "net_charm_exposure": 0.0,
                "net_volga_exposure": 0.0,
            },
        )
        if option_type == "CE":
            row["ce_oi"] += oi
            side_sign = 1.0
        else:
            row["pe_oi"] += oi
            side_sign = -1.0

        row["net_delta_exposure"] += delta * oi * lot_size * spot * 1e-2
        row["net_gamma_exposure"] += side_sign * gamma * oi * lot_size * spot * spot * 1e-4
        if sigma:
            vol_step = max(0.005, min(0.02, sigma * 0.1))
            sigma_lo = max(0.0001, sigma - vol_step)
            sigma_hi = sigma + vol_step
            delta_hi = _bs_delta(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma_hi, option_type)
            delta_lo = _bs_delta(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma_lo, option_type)
            vanna = (delta_hi - delta_lo) / (sigma_hi - sigma_lo)

            next_tte = max(tte_years - (1.0 / 365.0), MIN_TTE_YEARS)
            charm = _bs_delta(spot, strike, next_tte, DEFAULT_RISK_FREE_RATE, sigma, option_type) - delta

            price = _bs_price(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma, option_type)
            price_hi = _bs_price(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma_hi, option_type)
            price_lo = _bs_price(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, sigma_lo, option_type)
            volga = (price_hi - (2.0 * price) + price_lo) / ((sigma_hi - sigma) ** 2)

            row["net_vanna_exposure"] += side_sign * vanna * oi * lot_size * spot * 1e-2
            row["net_charm_exposure"] += side_sign * charm * oi * lot_size * spot * 1e-2
            row["net_volga_exposure"] += side_sign * volga * oi * lot_size * 1e-2

    out = []
    for strike, row in sorted(by_strike.items()):
        magnitude = max(
            abs(row["net_gamma_exposure"]),
            abs(row["net_delta_exposure"]) / max(spot, 1.0),
            abs(row["net_vanna_exposure"]),
            abs(row["net_charm_exposure"]),
            abs(row["net_volga_exposure"]) / max(spot, 1.0),
        )
        out.append(
            {
                "strike": strike,
                "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
                "ce_oi": round(row["ce_oi"], 2),
                "pe_oi": round(row["pe_oi"], 2),
                "net_delta_exposure": round(row["net_delta_exposure"], 2),
                "net_gamma_exposure": round(row["net_gamma_exposure"], 2),
                "net_vanna_exposure": round(row["net_vanna_exposure"], 2),
                "net_charm_exposure": round(row["net_charm_exposure"], 2),
                "net_volga_exposure": round(row["net_volga_exposure"], 2),
                "magnitude_score": round(magnitude, 2),
            }
        )
    return out


def _key_levels(
    entries: list[dict],
    spot: float,
    *,
    lot_size: int,
    tte_years: float,
    atm_iv: Optional[float],
    max_pain: Optional[float],
) -> dict[str, Any]:
    exposures = _per_strike_exposure(
        entries,
        spot,
        lot_size=lot_size,
        tte_years=tte_years,
        atm_iv=atm_iv,
    )
    gamma_profile, zero_gamma = _repriced_gamma_profile(
        entries,
        spot,
        lot_size=lot_size,
        tte_years=tte_years,
        atm_iv=atm_iv,
    )
    if spot <= 0:
        return {
            "call_wall": None,
            "put_wall": None,
            "abs_gamma": None,
            "zero_gamma": zero_gamma,
            "vol_trigger": zero_gamma,
            "gamma_regime": "unknown",
            "gamma_profile": gamma_profile,
        }

    call_rows = [r for r in exposures if r["strike"] >= spot and r["ce_oi"] > 0]
    put_rows = [r for r in exposures if r["strike"] <= spot and r["pe_oi"] > 0]
    if not call_rows:
        call_rows = [r for r in exposures if r["ce_oi"] > 0]
    if not put_rows:
        put_rows = [r for r in exposures if r["pe_oi"] > 0]

    call_wall = max(call_rows, key=lambda r: abs(r["net_gamma_exposure"])) if call_rows else None
    put_wall = max(put_rows, key=lambda r: abs(r["net_gamma_exposure"])) if put_rows else None
    abs_gamma = max(exposures, key=lambda r: abs(r["net_gamma_exposure"])) if exposures else None
    dealer_gex_total = gamma_profile[min(range(len(gamma_profile)), key=lambda i: abs(gamma_profile[i]["spot"] - spot))]["gex"] if gamma_profile else None
    gamma_regime = (
        "positive_gamma_pinning"
        if (dealer_gex_total or 0.0) >= 0.0
        else "negative_gamma_trend_amplifying"
    )

    def level_payload(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not row:
            return None
        return {
            "strike": row["strike"],
            "distance_pct": ((row["strike"] - spot) / spot) if spot > 0 else 0.0,
            "net_gamma_exposure": row["net_gamma_exposure"],
        }

    return {
        "call_wall": level_payload(call_wall),
        "put_wall": level_payload(put_wall),
        "abs_gamma": level_payload(abs_gamma),
        "zero_gamma": zero_gamma,
        "vol_trigger": zero_gamma,
        "max_pain": max_pain,
        "dealer_gex_total": _round_or_none(dealer_gex_total, 2),
        "gamma_regime": gamma_regime,
        "gamma_profile": gamma_profile,
    }


def _unusual_activity(entries: list[dict], spot: float, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e in entries:
        strike = _safe_float(e.get("strike"))
        oi = _safe_float(e.get("oi")) or 0.0
        volume = _safe_float(e.get("volume")) or 0.0
        oi_change = _safe_float(e.get("oi_change")) or 0.0
        oi_change_pct = _safe_float(e.get("oi_change_pct"))
        ltp_change_pct = _safe_float(e.get("ltp_change_pct"))
        if strike is None:
            continue
        volume_to_oi = volume / max(oi, 1.0)
        score = (
            min(volume_to_oi, 5.0) * 2.0
            + min(abs(oi_change_pct or 0.0) / 25.0, 4.0)
            + min(abs(ltp_change_pct or 0.0) / 10.0, 3.0)
            + min(abs(oi_change) / max(oi, 1.0), 2.0)
        )
        flags = []
        if volume_to_oi >= 0.5:
            flags.append("volume_vs_oi")
        if oi_change_pct is not None and abs(oi_change_pct) >= 25.0:
            flags.append("large_oi_change")
        if ltp_change_pct is not None and abs(ltp_change_pct) >= 10.0:
            flags.append("price_dislocation")
        if not flags and score < 1.0:
            continue
        rows.append(
            {
                "strike": strike,
                "option_type": str(e.get("option_type") or "").upper(),
                "moneyness_pct": ((strike - spot) / spot) if spot > 0 else 0.0,
                "ltp": _safe_float(e.get("ltp")),
                "volume": volume,
                "oi": oi,
                "oi_change": oi_change,
                "oi_change_pct": oi_change_pct,
                "ltp_change_pct": ltp_change_pct,
                "volume_to_oi": round(volume_to_oi, 4),
                "score": round(score, 3),
                "flags": flags,
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit]


@dataclass(frozen=True)
class ChainAnalyticsPayload:
    """Flat view used by the policy + UI. All fields tolerate None."""
    underlying: str
    expiry: Optional[str]
    spot: Optional[float]
    atm_strike: Optional[float]
    atm_iv: Optional[float]
    # PCR (put/call ratios) — both OI and volume; with OI-change deltas.
    pcr_oi: Optional[float]
    pcr_volume: Optional[float]
    pcr_oi_change: Optional[float]
    # IV skew = (IV at 25Δ-put) - (IV at 25Δ-call), normalised by ATM IV.
    iv_skew_25d: Optional[float]
    iv_skew_25d_norm: Optional[float]
    risk_reversal_25d: Optional[float]
    iv_25d_call: Optional[float]
    iv_25d_put: Optional[float]
    # Net exposures.
    gex_total: Optional[float]
    dealer_gex_total: Optional[float]
    dex_calls: Optional[float]
    dex_puts: Optional[float]
    dex_net: Optional[float]
    # Chain totals + ATM OI dynamics.
    total_ce_oi: Optional[float]
    total_pe_oi: Optional[float]
    total_ce_oi_change: Optional[float]
    total_pe_oi_change: Optional[float]
    atm_call_oi_change: Optional[float]
    atm_put_oi_change: Optional[float]
    atm_call_ltp_change_pct: Optional[float]
    atm_put_ltp_change_pct: Optional[float]
    max_pain: Optional[float]
    # Distribution + classification.
    top_ce_oi: list[dict[str, Any]]
    top_pe_oi: list[dict[str, Any]]
    oi_build_ce: dict[str, int]
    oi_build_pe: dict[str, int]
    gamma_curve: list[dict[str, Any]]
    key_levels: dict[str, Any]
    gamma_profile: list[dict[str, Any]]
    trace_exposures: list[dict[str, Any]]
    unusual_activity: list[dict[str, Any]]
    expiry_state: dict[str, Any]
    ntm_volx: dict[str, Any]
    spectrum: dict[str, Any]
    straddle: dict[str, Any]
    sigma_bands: dict[str, Any]
    gamma_density: dict[str, Any]
    options_table_rows: list[dict[str, Any]]
    writer_cash_proxy: dict[str, Any]


def _as_dict(payload: ChainAnalyticsPayload) -> dict[str, Any]:
    # Use __dict__ since dataclass is frozen — asdict() works too, but
    # we want to keep nested lists/dicts intact (not deep-copied).
    return {
        "underlying": payload.underlying,
        "expiry": payload.expiry,
        "spot": payload.spot,
        "atm_strike": payload.atm_strike,
        "atm_iv": payload.atm_iv,
        "pcr_oi": payload.pcr_oi,
        "pcr_volume": payload.pcr_volume,
        "pcr_oi_change": payload.pcr_oi_change,
        "iv_skew_25d": payload.iv_skew_25d,
        "iv_skew_25d_norm": payload.iv_skew_25d_norm,
        "risk_reversal_25d": payload.risk_reversal_25d,
        "iv_25d_call": payload.iv_25d_call,
        "iv_25d_put": payload.iv_25d_put,
        "gex_total": payload.gex_total,
        "dealer_gex_total": payload.dealer_gex_total,
        "dex_calls": payload.dex_calls,
        "dex_puts": payload.dex_puts,
        "dex_net": payload.dex_net,
        "total_ce_oi": payload.total_ce_oi,
        "total_pe_oi": payload.total_pe_oi,
        "total_ce_oi_change": payload.total_ce_oi_change,
        "total_pe_oi_change": payload.total_pe_oi_change,
        "atm_call_oi_change": payload.atm_call_oi_change,
        "atm_put_oi_change": payload.atm_put_oi_change,
        "atm_call_ltp_change_pct": payload.atm_call_ltp_change_pct,
        "atm_put_ltp_change_pct": payload.atm_put_ltp_change_pct,
        "max_pain": payload.max_pain,
        "top_ce_oi": payload.top_ce_oi,
        "top_pe_oi": payload.top_pe_oi,
        "oi_build_ce": payload.oi_build_ce,
        "oi_build_pe": payload.oi_build_pe,
        "gamma_curve": payload.gamma_curve,
        "key_levels": payload.key_levels,
        "gamma_profile": payload.gamma_profile,
        "trace_exposures": payload.trace_exposures,
        "unusual_activity": payload.unusual_activity,
        "expiry_state": payload.expiry_state,
        "ntm_volx": payload.ntm_volx,
        "spectrum": payload.spectrum,
        "straddle": payload.straddle,
        "sigma_bands": payload.sigma_bands,
        "gamma_density": payload.gamma_density,
        "options_table_rows": payload.options_table_rows,
        "writer_cash_proxy": payload.writer_cash_proxy,
    }


async def chain_strike_mark(
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    *,
    timeout: float = 1.0,
) -> Optional[float]:
    """Latest LTP for a single (strike, option_type) from the cached chain.

    Used to live-mark held option positions whose specific contract isn't on
    the WS premium feed (so their stored mark freezes at entry). The chain
    poll keeps EVERY strike's LTP fresh (~30s), so reading it here streams
    the position's P/L. Best-effort, bounded — returns None on cache miss.
    """
    if not underlying or not expiry or not strike or not option_type:
        return None
    try:
        cache_symbol = to_app_symbol(underlying) or underlying
    except Exception:
        cache_symbol = underlying
    try:
        cached = await asyncio.wait_for(
            option_chain_service.get_cached(cache_symbol, expiry),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, Exception):
        cached = None
    if not cached or not cached.get("entries"):
        return None
    want = str(option_type or "").upper()
    for entry in cached.get("entries") or []:
        try:
            if (
                str(entry.get("option_type") or "").upper() == want
                and abs(float(entry.get("strike") or 0.0) - float(strike)) < 0.01
            ):
                ltp = entry.get("ltp")
                return float(ltp) if ltp is not None else None
        except (TypeError, ValueError):
            continue
    return None


async def fetch_chain_analytics(
    underlying: str,
    expiry: Optional[str] = None,
    timeout: float = 2.0,
    allow_expiry_fallback: bool = True,
) -> Optional[dict[str, Any]]:
    """Pull the cached option chain for an underlying + expiry and
    return the policy-ready feature payload. Returns `None` when no
    chain is cached (typical pre-market / cold-start) OR when the
    Redis lookup takes longer than `timeout` seconds — never blocks
    the caller indefinitely.

    Saturday-evening observation: when broker WS connections are down
    the option-chain service can leave `get_cached` waiting on a lock
    held by a stalled refresh task. A hard timeout here keeps the
    directional snapshot/endpoint snappy regardless of that backpressure.
    """
    # Normalise the underlying to the same app_symbol form the market
    # endpoint uses when it writes to Redis ("NSE:NIFTY50-INDEX",
    # "NSE:BANKNIFTY-INDEX", "BSE:SENSEX-INDEX"). Without this we miss
    # every cache hit — the market endpoint writes under the app_symbol
    # form and we'd be reading under "NIFTY" / "BANKNIFTY" / "SENSEX".
    try:
        cache_symbol = to_app_symbol(underlying) or underlying
    except Exception:
        cache_symbol = underlying
    status = await chain_cache_status(underlying, expiry)
    expiry_candidates: list[str] = []
    if expiry:
        expiry_candidates.append(str(expiry))
    if allow_expiry_fallback:
        expiry_candidates.extend(str(exp) for exp in status.get("known_expiries") or [] if exp)
    expiry_candidates = list(dict.fromkeys(expiry_candidates))
    if not expiry_candidates:
        return None
    cached = None
    resolved_expiry = None
    for candidate_expiry in expiry_candidates:
        try:
            candidate_cached = await asyncio.wait_for(
                option_chain_service.get_cached(cache_symbol, candidate_expiry),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, Exception):
            candidate_cached = None
        if candidate_cached and candidate_cached.get("entries"):
            cached = candidate_cached
            resolved_expiry = candidate_expiry
            break
    if not cached or not cached.get("entries"):
        # Cache miss — return None. Whatever populates the chain
        # (market-intel runtime, poll loop, /api/market/option-chain
        # callers) will fill it in due course. Policy gets zero chain
        # features this cycle and learns the "chain-absent" pattern.
        return None

    entries = list(cached.get("entries") or [])
    spot = _safe_float(cached.get("spot_price"))
    atm_strike = _safe_float(cached.get("atm_strike"))
    atm_iv = _safe_float(cached.get("atm_iv"))

    iv_25d_call = _interpolate_iv_at_delta(entries, 0.25, "CE")
    iv_25d_put = _interpolate_iv_at_delta(entries, 0.25, "PE")
    iv_skew = None
    iv_skew_norm = None
    risk_reversal = None
    if iv_25d_call is not None and iv_25d_put is not None:
        iv_skew = iv_25d_put - iv_25d_call
        risk_reversal = iv_25d_call - iv_25d_put
        if atm_iv and atm_iv > 0:
            iv_skew_norm = iv_skew / atm_iv

    lot = await _resolve_lot_size(underlying)
    dex_calls, dex_puts, dex_net = _compute_dex(entries, lot)
    expiry_meta = _expiry_state(cached.get("expiry") or expiry)
    tte_years = float(expiry_meta.get("time_to_expiry_years") or MIN_TTE_YEARS)
    spot_value = spot or 0.0
    max_pain = _safe_float(cached.get("max_pain"))
    ntm_band = _strike_band(entries, spot_value, each_side=2)
    straddle, sigma_bands = _straddle_and_sigma(entries, spot_value, atm_strike, atm_iv, tte_years)
    key_levels = _key_levels(
        entries,
        spot_value,
        lot_size=lot,
        tte_years=tte_years,
        atm_iv=atm_iv,
        max_pain=max_pain,
    )
    trace_exposures = _per_strike_exposure(
        entries,
        spot_value,
        lot_size=lot,
        tte_years=tte_years,
        atm_iv=atm_iv,
    )
    dealer_gex_total = key_levels.get("dealer_gex_total")
    ntm_volx = _ntm_volx(entries, spot_value, ntm_band)
    spectrum = _spectrum(entries, spot_value, row_limit=21)
    gamma_density = _gamma_density_summary(trace_exposures, spot_value)

    payload = ChainAnalyticsPayload(
        underlying=underlying,
        expiry=cached.get("expiry") or resolved_expiry,
        spot=spot,
        atm_strike=atm_strike,
        atm_iv=atm_iv,
        pcr_oi=_safe_float(cached.get("pcr_oi")),
        pcr_volume=_safe_float(cached.get("pcr_volume")),
        pcr_oi_change=_safe_float(cached.get("pcr_oi_change")),
        iv_skew_25d=iv_skew,
        iv_skew_25d_norm=iv_skew_norm,
        risk_reversal_25d=risk_reversal,
        iv_25d_call=iv_25d_call,
        iv_25d_put=iv_25d_put,
        gex_total=_net_gex(cached.get("gamma_exposure")),
        dealer_gex_total=dealer_gex_total,
        dex_calls=dex_calls,
        dex_puts=dex_puts,
        dex_net=dex_net,
        total_ce_oi=_safe_float(cached.get("total_ce_oi")),
        total_pe_oi=_safe_float(cached.get("total_pe_oi")),
        total_ce_oi_change=_safe_float(cached.get("total_ce_oi_change")),
        total_pe_oi_change=_safe_float(cached.get("total_pe_oi_change")),
        atm_call_oi_change=_safe_float(cached.get("atm_call_oi_change")),
        atm_put_oi_change=_safe_float(cached.get("atm_put_oi_change")),
        atm_call_ltp_change_pct=_safe_float(cached.get("atm_call_ltp_change_pct")),
        atm_put_ltp_change_pct=_safe_float(cached.get("atm_put_ltp_change_pct")),
        max_pain=max_pain,
        top_ce_oi=_top_oi_strikes(entries, spot_value, "CE", n=3),
        top_pe_oi=_top_oi_strikes(entries, spot_value, "PE", n=3),
        oi_build_ce=_classify_oi_build(entries, "CE"),
        oi_build_pe=_classify_oi_build(entries, "PE"),
        gamma_curve=_gamma_curve(entries, spot_value, window_strikes=5),
        key_levels=key_levels,
        gamma_profile=list(key_levels.get("gamma_profile") or []),
        trace_exposures=trace_exposures,
        unusual_activity=_unusual_activity(entries, spot_value, limit=8),
        expiry_state=expiry_meta,
        ntm_volx=ntm_volx,
        spectrum=spectrum,
        straddle=straddle,
        sigma_bands=sigma_bands,
        gamma_density=gamma_density,
        options_table_rows=_options_table_rows(entries, spot_value, row_limit=15),
        writer_cash_proxy=_writer_cash_proxy(entries, lot),
    )
    out = _as_dict(payload)
    out["requested_expiry"] = expiry
    out["cache_status"] = {
        **status,
        "resolved_expiry": out.get("expiry"),
        "used_fallback_expiry": bool(expiry and out.get("expiry") and str(out.get("expiry")) != str(expiry)),
    }
    return out


# Synchronous wrapper for callers in non-async contexts.
def fetch_chain_analytics_sync(underlying: str, expiry: Optional[str] = None) -> Optional[dict[str, Any]]:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're already inside an async context — caller should use the async version.
            return None
        return loop.run_until_complete(fetch_chain_analytics(underlying, expiry))
    except RuntimeError:
        return asyncio.run(fetch_chain_analytics(underlying, expiry))
