"""Option chain service — polls, caches in Redis, calculates PCR, max pain, IV rank."""
from __future__ import annotations
import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from brokers.base import BrokerAdapter, OptionChain
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.symbols import (
    resolve_upstox_option_underlying_key,
    to_fyers_option_symbol,
)
from market_data.validated_snapshots import validate_option_chain_rows


POLL_INTERVAL = 30   # seconds — Redis cache refresh cadence
OC_TTL = 60          # Redis TTL for option chain
# Durable-history persistence cadence (option_chain_snapshots). Coarser than
# the 30s Redis poll so the DB doesn't take ~80 rows × N chains every 30s, but
# fine-grained enough for intraday PCR/max-pain/GEX time-series + pre-market
# analysis. 120s ≈ 190 snapshots/contract/session.
PERSIST_INTERVAL = 120


class OptionChainService:
    """Periodically polls option chain, calculates analytics, stores in Redis."""

    # Consecutive-failure eviction (2026-07-17): a permanently-failing tracked
    # pair — e.g. a stock underlying whose Upstox chain call rejects the
    # instrument key with a hard 400 every poll — otherwise retries every
    # POLL_INTERVAL forever. 50 such stock pairs (pinned via the ad-hoc
    # /market/option-chain endpoint) burned ~83% of the entire Upstox
    # 1800/30min budget on 2026-07-17 and starved the S1 stock universe from
    # 10:30 IST. Evict after this many consecutive failures; the periodic
    # re-track paths (S2 session pick, directional per-cycle ensure) restore
    # a healthy index pair automatically after a transient outage.
    EVICT_AFTER_CONSECUTIVE_FAILURES = 10

    def __init__(self):
        self._broker: Optional[BrokerAdapter] = None
        self._tracked: List[tuple[str, str]] = []  # (symbol, expiry) pairs
        self._task: Optional[asyncio.Task] = None
        # Last DB-persist time per (symbol, expiry) — throttles the durable
        # option_chain_snapshots write to PERSIST_INTERVAL.
        self._last_persist: Dict[tuple[str, str], datetime] = {}
        # Consecutive refresh failures per (symbol, expiry) — see eviction note.
        self._refresh_failures: Dict[tuple[str, str], int] = {}

    def set_broker(self, broker: BrokerAdapter):
        self._broker = broker

    def track(self, symbol: str, expiry: str):
        if (symbol, expiry) not in self._tracked:
            self._tracked.append((symbol, expiry))
            # A deliberate re-track after an eviction starts with a clean slate.
            self._refresh_failures.pop((symbol, expiry), None)

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def ensure_running(self) -> None:
        """Acquire a broker (if missing) and start the poll loop (if not
        running). Idempotent — safe to call repeatedly. Lets callers that want
        a chain *guaranteed* tracked + persisted (e.g. the session-open pick
        registering the active index expiries) get the loop up without
        depending on a desk having started it first."""
        if self._broker is None:
            try:
                from api.routers.market import _get_market_adapter
                adapter, _ = await _get_market_adapter()
                if adapter is not None:
                    self.set_broker(adapter)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OC] ensure_running broker acquire failed: {exc}")
        if self._task is None or self._task.done():
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OC] ensure_running start failed: {exc}")

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def get_cached(self, symbol: str, expiry: str) -> Optional[dict]:
        redis = await get_redis()
        raw = await redis.get(f"oc:{symbol}:{expiry}")
        return json.loads(raw) if raw else None

    async def _poll_loop(self):
        while True:
            try:
                # A slow broker response for one expiry must not delay every
                # other tracked chain. Each refresh remains independently
                # bounded by the broker adapter while the poll cadence stays flat.
                await asyncio.gather(
                    *(self._refresh(symbol, expiry) for symbol, expiry in tuple(self._tracked))
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OC] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _refresh(self, symbol: str, expiry: str):
        if not self._broker:
            return
        try:
            broker_name = getattr(self._broker, "broker_name", "")
            if broker_name == "fyers":
                lookup_symbol = to_fyers_option_symbol(symbol)
            else:
                # Broker-canonical resolution (2026-07-18, defect 7c): indices
                # resolve via the static map exactly as before; a STOCK resolves
                # its fo_underlying_catalog.underlying_key. A stock with no
                # catalog key FAILS CLOSED here — raising inside the try counts
                # toward consecutive-failure eviction WITHOUT ever sending the
                # bare name to Upstox (guaranteed 400 "Invalid Instrument key",
                # the 2026-07-17 budget-storm shape).
                lookup_symbol = await resolve_upstox_option_underlying_key(symbol)
                if not lookup_symbol:
                    raise LookupError(
                        f"no canonical Upstox key for {symbol!r} "
                        "(fo_underlying_catalog.underlying_key missing) — "
                        "refusing known-invalid broker call"
                    )
            chain: OptionChain = await self._broker.get_option_chain(lookup_symbol, expiry)
            source = getattr(self._broker, "broker_name", "unknown")
            payload, validated_chain = await self.build_validated_payload(
                symbol=symbol,
                expiry=expiry,
                chain=chain,
                source=source,
            )
            redis = await get_redis()
            await redis.set(f"oc:{symbol}:{expiry}", json.dumps(payload), ex=OC_TTL)
            self._refresh_failures.pop((symbol, expiry), None)
            logger.debug(f"[OC] Refreshed {symbol} {expiry}")

            # Durable persistence for analysis (2026-06-04). The Redis cache is
            # ephemeral (60s TTL) — it vanishes after market close, so there was
            # no chain history in the DB for pre-market reads or PCR/OI/GEX
            # time-series. Persist the full chain to option_chain_snapshots,
            # throttled to PERSIST_INTERVAL. Best-effort: a DB error here must
            # never break the live cache / poll loop.
            now = datetime.now(timezone.utc)
            last = self._last_persist.get((symbol, expiry))
            if last is None or (now - last).total_seconds() >= PERSIST_INTERVAL:
                try:
                    await self._persist_snapshot(symbol, expiry, validated_chain, now)
                    self._last_persist[(symbol, expiry)] = now
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[OC] persist failed {symbol}/{expiry}: {exc}")
        except Exception as e:
            key = (symbol, expiry)
            count = self._refresh_failures.get(key, 0) + 1
            self._refresh_failures[key] = count
            if count >= self.EVICT_AFTER_CONSECUTIVE_FAILURES and key in self._tracked:
                self._tracked.remove(key)
                self._refresh_failures.pop(key, None)
                logger.warning(
                    f"[OC] Evicted {symbol}/{expiry} after {count} consecutive refresh "
                    f"failures — re-track to resume polling. Last error: {e}"
                )
            else:
                logger.error(f"[OC] Refresh failed {symbol}/{expiry}: {e}")

    @staticmethod
    def _serialize_entry(entry: Any) -> dict[str, Any]:
        return {
            "strike": entry.strike,
            "option_type": entry.option_type,
            "ltp": entry.ltp,
            "oi": entry.oi,
            "volume": entry.volume,
            "bid": entry.bid,
            "ask": entry.ask,
            "iv": entry.iv,
            "delta": entry.delta,
            "gamma": entry.gamma,
            "theta": entry.theta,
            "vega": entry.vega,
            "prev_oi": entry.prev_oi,
            "prev_close": entry.prev_close,
            "oi_change": round(float(entry.oi) - float(entry.prev_oi or 0.0), 2),
            "oi_change_pct": round(
                ((float(entry.oi) - float(entry.prev_oi or 0.0)) / float(entry.prev_oi or 1.0)) * 100.0,
                2,
            ) if entry.prev_oi else None,
            "ltp_change": round(float(entry.ltp) - float(entry.prev_close or 0.0), 2),
            "ltp_change_pct": round(
                ((float(entry.ltp) - float(entry.prev_close or 0.0)) / float(entry.prev_close or 1.0)) * 100.0,
                2,
            ) if entry.prev_close else None,
            "instrument_key": entry.instrument_key,
        }

    @staticmethod
    def _normalize_iv_units(chain: OptionChain, source: str) -> None:
        """Normalize entry IVs to PERCENT (Upstox convention), in place.

        Fyers chains carry IV as a FRACTION (app-side Black-Scholes sigma,
        e.g. 0.14) while Upstox serves broker-native PERCENT (e.g. 13.98).
        option_chain_snapshots has no unit column and greeks_enrichment divides
        by 100 unconditionally — so a session pinned to Fyers used to persist
        fraction-unit rows that enrichment turned into iv≈0.0014 in candles,
        permanently. One unit at this chokepoint, before cache + persist.
        A magnitude guard backstops the broker rule: real IV below 3% does not
        occur on these contracts, above 500% is garbage.
        """
        is_fraction_source = str(source or "").lower() == "fyers"
        flagged = 0
        for entry in chain.entries:
            iv = entry.iv
            if iv is None:
                continue
            try:
                iv = float(iv)
            except (TypeError, ValueError):
                entry.iv = None
                continue
            if iv <= 0:
                entry.iv = None
                continue
            if is_fraction_source:
                iv *= 100.0
            if 0 < iv < 3.0:
                iv *= 100.0
                flagged += 1
            if iv > 500.0:
                entry.iv = None
                continue
            entry.iv = round(iv, 4)
        if flagged:
            logger.warning(
                f"[OC] {flagged} {source} IV values looked fraction-unit after "
                "broker-rule normalization; magnitude guard rescaled them to percent."
            )

    async def build_validated_payload(
        self,
        *,
        symbol: str,
        expiry: str,
        chain: OptionChain,
        source: str,
    ) -> tuple[dict[str, Any], OptionChain]:
        """Build the one canonical chain payload used by every producer."""
        self._normalize_iv_units(chain, source)
        raw_entries = [self._serialize_entry(entry) for entry in chain.entries]
        received_at = datetime.now(timezone.utc)
        validation = validate_option_chain_rows(
            raw_entries,
            symbol=symbol,
            expiry=expiry,
            spot_price=chain.spot_price,
            source=source,
            observed_at=received_at,
            now=received_at,
            freshness_budget_seconds=OC_TTL,
        )
        accepted_objects = [chain.entries[index] for index in validation.accepted_indices]
        validated_chain = OptionChain(
            symbol=chain.symbol,
            expiry=chain.expiry,
            spot_price=chain.spot_price,
            entries=accepted_objects,
        )
        # Max-pain is O(strikes^2); never let it block the event loop.
        analytics = await asyncio.to_thread(self._calculate_analytics, validated_chain)
        return (
            {
                "symbol": symbol,
                "expiry": expiry,
                "spot_price": float(chain.spot_price or 0.0),
                "timestamp": received_at.isoformat(),
                "source": source,
                "entries": validation.rows,
                "data_quality": validation.quality,
                "provenance": {
                    "source": source,
                    "observed_at": validation.quality.get("observed_at"),
                    "received_at": validation.quality.get("received_at"),
                    "expiry": expiry,
                },
                **analytics,
            },
            validated_chain,
        )

    async def _persist_snapshot(
        self, symbol: str, expiry: str, chain: OptionChain, ts: datetime
    ) -> None:
        """Write the full chain to option_chain_snapshots (TimescaleDB hypertable).

        One row per strike×side: time, symbol, expiry, strike, option_type,
        ltp, oi, volume, iv, delta, gamma, theta, vega — a durable, queryable
        history for PCR / max-pain / GEX-DEX time-series and pre-market
        analysis. Short-lived session (mindful of the small connection pool).
        """
        rows = [
            {
                "time": ts,
                "symbol": symbol,
                "expiry": str(expiry),
                "strike": float(e.strike),
                "option_type": e.option_type,
                "ltp": float(e.ltp) if e.ltp is not None else None,
                "oi": int(e.oi or 0),
                "volume": int(e.volume or 0),
                "iv": float(e.iv) if e.iv is not None else None,
                "delta": float(e.delta) if e.delta is not None else None,
                "gamma": float(e.gamma) if e.gamma is not None else None,
                "theta": float(e.theta) if e.theta is not None else None,
                "vega": float(e.vega) if e.vega is not None else None,
            }
            for e in chain.entries
        ]
        if not rows:
            return
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO option_chain_snapshots
                        (time, symbol, expiry, strike, option_type, ltp, oi,
                         volume, iv, delta, gamma, theta, vega)
                    VALUES
                        (:time, :symbol, :expiry, :strike, :option_type, :ltp, :oi,
                         :volume, :iv, :delta, :gamma, :theta, :vega)
                    """
                ),
                rows,
            )
            await session.commit()

    def _calculate_analytics(self, chain: OptionChain) -> dict:
        ce_entries = [e for e in chain.entries if e.option_type == "CE"]
        pe_entries = [e for e in chain.entries if e.option_type == "PE"]

        total_ce_oi = sum(e.oi for e in ce_entries)
        total_pe_oi = sum(e.oi for e in pe_entries)
        total_ce_prev_oi = sum(float(e.prev_oi or 0.0) for e in ce_entries)
        total_pe_prev_oi = sum(float(e.prev_oi or 0.0) for e in pe_entries)
        total_ce_vol = sum(e.volume for e in ce_entries)
        total_pe_vol = sum(e.volume for e in pe_entries)

        pcr_oi = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
        pcr_vol = total_pe_vol / total_ce_vol if total_ce_vol > 0 else 1.0
        prev_pcr_oi = (
            total_pe_prev_oi / total_ce_prev_oi if total_ce_prev_oi > 0 else None
        )

        max_pain = self._calculate_max_pain(chain.entries, chain.spot_price)

        # ATM IV
        atm_strike = self._get_atm_strike(chain)
        atm_iv = 0.0
        atm_call = None
        atm_put = None
        for e in chain.entries:
            if e.strike == atm_strike and e.option_type == "CE":
                atm_iv = e.iv or 0.0
                atm_call = e
            if e.strike == atm_strike and e.option_type == "PE":
                atm_put = e

        # Gamma exposure per strike
        gamma_exposure = {}
        for e in chain.entries:
            key = str(e.strike)
            ge = gamma_exposure.get(key, 0.0)
            sign = 1 if e.option_type == "CE" else -1
            gamma_exposure[key] = ge + sign * (e.gamma or 0) * e.oi * chain.spot_price

        return {
            "pcr_oi": round(pcr_oi, 4),
            "pcr_volume": round(pcr_vol, 4),
            "pcr_prev_oi": round(prev_pcr_oi, 4) if prev_pcr_oi is not None else None,
            "pcr_oi_change": round(pcr_oi - prev_pcr_oi, 4) if prev_pcr_oi is not None else None,
            "max_pain": max_pain,
            "atm_strike": atm_strike,
            "atm_iv": round(atm_iv, 4),
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "total_ce_prev_oi": round(total_ce_prev_oi, 2),
            "total_pe_prev_oi": round(total_pe_prev_oi, 2),
            "total_ce_oi_change": round(total_ce_oi - total_ce_prev_oi, 2),
            "total_pe_oi_change": round(total_pe_oi - total_pe_prev_oi, 2),
            "total_ce_volume": total_ce_vol,
            "total_pe_volume": total_pe_vol,
            "atm_call_ltp_change": round(float(atm_call.ltp) - float(atm_call.prev_close or 0.0), 2)
            if atm_call and atm_call.prev_close
            else None,
            "atm_call_ltp_change_pct": round(
                ((float(atm_call.ltp) - float(atm_call.prev_close or 0.0)) / float(atm_call.prev_close or 1.0)) * 100.0,
                2,
            ) if atm_call and atm_call.prev_close else None,
            "atm_put_ltp_change": round(float(atm_put.ltp) - float(atm_put.prev_close or 0.0), 2)
            if atm_put and atm_put.prev_close
            else None,
            "atm_put_ltp_change_pct": round(
                ((float(atm_put.ltp) - float(atm_put.prev_close or 0.0)) / float(atm_put.prev_close or 1.0)) * 100.0,
                2,
            ) if atm_put and atm_put.prev_close else None,
            "atm_call_oi_change": round(float(atm_call.oi) - float(atm_call.prev_oi or 0.0), 2)
            if atm_call
            else None,
            "atm_put_oi_change": round(float(atm_put.oi) - float(atm_put.prev_oi or 0.0), 2)
            if atm_put
            else None,
            "gamma_exposure": gamma_exposure,
        }

    def _get_atm_strike(self, chain: OptionChain) -> float:
        if not chain.entries:
            return 0.0
        strikes = sorted(set(e.strike for e in chain.entries))
        return min(strikes, key=lambda s: abs(s - chain.spot_price))

    def _calculate_max_pain(self, entries: list, spot: float) -> float:
        """Max pain = strike where total option buyers lose most."""
        from collections import defaultdict
        strikes = sorted(set(e.strike for e in entries))
        oi_by_strike: dict = defaultdict(lambda: {"CE": 0, "PE": 0})
        for e in entries:
            oi_by_strike[e.strike][e.option_type] += e.oi

        pain = {}
        for exp_strike in strikes:
            total = 0.0
            for k, oi in oi_by_strike.items():
                # CE loss at expiry_price < strike → intrinsic 0
                ce_loss = max(0, exp_strike - k) * oi["CE"]
                pe_loss = max(0, k - exp_strike) * oi["PE"]
                total += ce_loss + pe_loss
            pain[exp_strike] = total

        return min(pain, key=pain.get) if pain else spot


option_chain_service = OptionChainService()
