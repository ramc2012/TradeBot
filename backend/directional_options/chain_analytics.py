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
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from market_data.option_chain import option_chain_service
from market_data.symbols import to_app_symbol


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
    "BANKEX": 30,
}


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
    # Net exposures.
    gex_total: Optional[float]
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
        "gex_total": payload.gex_total,
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
    if not expiry:
        return None
    # Normalise the underlying to the same app_symbol form the market
    # endpoint uses when it writes to Redis ("NSE:NIFTY50-INDEX",
    # "NSE:BANKNIFTY-INDEX", "BSE:SENSEX-INDEX"). Without this we miss
    # every cache hit — the market endpoint writes under the app_symbol
    # form and we'd be reading under "NIFTY" / "BANKNIFTY" / "SENSEX".
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
    if iv_25d_call is not None and iv_25d_put is not None:
        iv_skew = iv_25d_put - iv_25d_call
        if atm_iv and atm_iv > 0:
            iv_skew_norm = iv_skew / atm_iv

    lot = INDEX_LOT_SIZE.get(underlying.upper(), 1)
    dex_calls, dex_puts, dex_net = _compute_dex(entries, lot)

    payload = ChainAnalyticsPayload(
        underlying=underlying,
        expiry=cached.get("expiry"),
        spot=spot,
        atm_strike=atm_strike,
        atm_iv=atm_iv,
        pcr_oi=_safe_float(cached.get("pcr_oi")),
        pcr_volume=_safe_float(cached.get("pcr_volume")),
        pcr_oi_change=_safe_float(cached.get("pcr_oi_change")),
        iv_skew_25d=iv_skew,
        iv_skew_25d_norm=iv_skew_norm,
        gex_total=_net_gex(cached.get("gamma_exposure")),
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
        max_pain=_safe_float(cached.get("max_pain")),
        top_ce_oi=_top_oi_strikes(entries, spot or 0.0, "CE", n=3),
        top_pe_oi=_top_oi_strikes(entries, spot or 0.0, "PE", n=3),
        oi_build_ce=_classify_oi_build(entries, "CE"),
        oi_build_pe=_classify_oi_build(entries, "PE"),
        gamma_curve=_gamma_curve(entries, spot or 0.0, window_strikes=5),
    )
    return _as_dict(payload)


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
