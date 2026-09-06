from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from math import isfinite
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from analysis.macd_engine import compute_ema
from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from core.config import settings
from auction_intelligence.options.ntm_volx import NTMVolXAnalyzer
from auction_intelligence.schemas import AgentDecision, ExecutionInstruction, NTMVolXSnapshot, SessionContext
from brokers.base import BrokerAdapter, OptionChain, OptionChainEntry
from db.database import AsyncSessionLocal
from market_data.atm_watchlist import atm_watchlist_service
from market_data.option_chain import option_chain_service
from market_data.option_history import option_history_service
from market_data.symbols import DISPLAY_NAMES, to_broker_symbol, to_fyers_symbol


_UNDERLYING_TO_APP_SYMBOL = {
    str(display).upper(): app_symbol
    for app_symbol, display in DISPLAY_NAMES.items()
}
_SIGNAL_TO_OPTION_TYPE = {
    "LONG": "CE",
    "SHORT": "PE",
}
_DEFAULT_CONFIG = {
    "enabled": True,
    "min_premium": 2.0,
    "max_premium": 500.0,
    "history_interval": "5minute",
    "history_limit": 60,
    "require_option_ma20": True,
    "min_volume": 1.0,
    "min_oi": 1.0,
    "max_spread_pct": 0.15,
    "enable_relaxed_fallback": True,
    "relaxed_max_premium": 1500.0,
    "relaxed_min_volume": 0.0,
    "relaxed_min_oi": 0.0,
    "relaxed_max_spread_pct": 0.35,
    "strong_confidence": 0.78,
    "very_strong_confidence": 0.86,
    "same_day_close_buffer_minutes": {
        "scalp": 45,
        "swing": 120,
        "positional": 240,
    },
    "min_dte_by_agent": {
        "scalp": 0,
        "swing": 2,
        "positional": 5,
    },
    "target_delta_by_agent": {
        "scalp": 0.50,
        "swing": 0.60,
        "positional": 0.70,
    },
    "preferred_moneyness": {
        "scalp": ["ATM", "OTM1", "ITM1", "OTM2", "ITM2"],
        "swing": ["ATM", "ITM1", "OTM1", "ITM2", "OTM2"],
        "swing_strong": ["ITM1", "ATM", "ITM2", "OTM1", "OTM2"],
        "positional": ["ITM1", "ATM", "ITM2", "OTM1", "OTM2"],
        "positional_strong": ["ITM2", "ITM1", "ATM", "OTM1", "OTM2"],
    },
}


class OptionStrategyMapper:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        contract_specs: dict[str, Any] | None = None,
    ):
        self.config = {
            **_DEFAULT_CONFIG,
            **(config or {}),
        }
        self.contract_specs = contract_specs or {}
        self.ntm_volx = NTMVolXAnalyzer(self.config.get("ntm_volx"))
        self._chain_cache: dict[tuple[str, str], OptionChain] = {}

    async def map_execution_plan(
        self,
        *,
        session: SessionContext,
        decisions: list[AgentDecision],
        execution_plan: list[ExecutionInstruction],
        ntm_volx: NTMVolXSnapshot | None = None,
    ) -> list[ExecutionInstruction]:
        if not self.config.get("enabled", True):
            return execution_plan

        decision_by_agent = {
            decision.agent_name: decision
            for decision in decisions
            if decision.action != "FLAT"
        }
        mapped: list[ExecutionInstruction] = []
        for instruction in execution_plan:
            decision = decision_by_agent.get(instruction.agent_name)
            if decision is None:
                continue
            mapped_instruction = await self._map_instruction(
                session=session,
                decision=decision,
                instruction=instruction,
                ntm_volx=ntm_volx,
            )
            if mapped_instruction is not None:
                mapped.append(mapped_instruction)
        return mapped

    async def build_ntm_volx(
        self,
        *,
        session: SessionContext,
        agent_name: str = "swing",
    ) -> NTMVolXSnapshot | None:
        underlying = self._extract_underlying(session.symbol)
        app_symbol = _UNDERLYING_TO_APP_SYMBOL.get(underlying)
        if not app_symbol:
            return None

        upstox_adapter, fyers_adapter = await self._get_option_adapters()
        expiries = await self._discover_expiries(
            underlying=underlying,
            app_symbol=app_symbol,
            upstox_adapter=upstox_adapter,
            fyers_adapter=fyers_adapter,
        )
        if not expiries:
            return None

        expiry = self._select_expiry(
            expiries=expiries,
            session=session,
            agent_name=agent_name,
        )
        if expiry is None:
            return None

        chain = await self._load_chain(
            app_symbol=app_symbol,
            expiry=expiry,
            upstox_adapter=upstox_adapter,
            fyers_adapter=fyers_adapter,
        )
        if chain is None or not chain.entries:
            return None

        return self.ntm_volx.analyze_chain(
            underlying=underlying,
            expiry=expiry.isoformat(),
            chain=chain,
        )

    async def _map_instruction(
        self,
        *,
        session: SessionContext,
        decision: AgentDecision,
        instruction: ExecutionInstruction,
        ntm_volx: NTMVolXSnapshot | None = None,
    ) -> Optional[ExecutionInstruction]:
        underlying = self._extract_underlying(session.symbol)
        app_symbol = _UNDERLYING_TO_APP_SYMBOL.get(underlying)
        option_type = _SIGNAL_TO_OPTION_TYPE.get(decision.action)
        if not app_symbol or not option_type:
            return None

        upstox_adapter, fyers_adapter = await self._get_option_adapters()
        expiries = await self._discover_expiries(
            underlying=underlying,
            app_symbol=app_symbol,
            upstox_adapter=upstox_adapter,
            fyers_adapter=fyers_adapter,
        )
        if not expiries:
            logger.info(f"[AuctionIQ] No option expiries available for {underlying}")
            return None

        expiry = self._select_expiry(
            expiries=expiries,
            session=session,
            agent_name=decision.agent_name,
        )
        if expiry is None:
            logger.info(f"[AuctionIQ] No eligible expiry for {underlying} {decision.agent_name}")
            return None

        chain = await self._load_chain(
            app_symbol=app_symbol,
            expiry=expiry,
            upstox_adapter=upstox_adapter,
            fyers_adapter=fyers_adapter,
        )
        if chain is None or not chain.entries:
            logger.info(f"[AuctionIQ] No option chain available for {underlying} {expiry.isoformat()}")
            return None

        contracts = await self._load_contract_rows(
            underlying=underlying,
            expiry=expiry,
            app_symbol=app_symbol,
            upstox_adapter=upstox_adapter,
        )
        selection = await self._select_contract(
            session=session,
            decision=decision,
            instruction=instruction,
            underlying=underlying,
            expiry=expiry,
            chain=chain,
            contracts=contracts,
            option_type=option_type,
            expiries=expiries,
            ntm_volx=ntm_volx,
        )
        if selection is None:
            logger.info(
                f"[AuctionIQ] No tradable option candidate for {underlying} "
                f"{decision.agent_name} {decision.action}"
            )
            return None

        return replace(
            instruction,
            symbol=selection["symbol"],
            limit_price=selection["limit_price"],
            quantity=selection["quantity"],
            broker_action="BUY",
            underlying_symbol=underlying,
            instrument_type=selection["option_type"],
            expiry=expiry.isoformat(),
            strike=selection["strike"],
            option_type=selection["option_type"],
            instrument_key=selection["instrument_key"],
            trading_symbol=selection["trading_symbol"],
            lot_size=selection["lot_size"],
            premium=selection["premium"],
            spot_price=selection["spot_price"],
            moneyness=selection["moneyness"],
            expiry_kind=selection["expiry_kind"],
            days_to_expiry=selection["days_to_expiry"],
            selection_reason=selection["selection_reason"],
            premium_ma20=selection["premium_ma20"],
            premium_ma50=selection["premium_ma50"],
            above_premium_ma20=selection["above_premium_ma20"],
            above_premium_ma50=selection["above_premium_ma50"],
            decision_confidence=round(float(decision.confidence), 4),
            rationale=[*instruction.rationale, *selection["rationale"]],
        )

    async def _get_option_adapters(self) -> tuple[Optional[BrokerAdapter], Optional[BrokerAdapter]]:
        if self.config.get("local_data_only", True) or settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY:
            return None, None
        upstox_adapter = get_active_adapter("upstox")
        fyers_adapter = get_active_adapter("fyers")
        if upstox_adapter is None and await ensure_upstox_session():
            upstox_adapter = get_active_adapter("upstox")
        if fyers_adapter is None and await ensure_fyers_session():
            fyers_adapter = get_active_adapter("fyers")
        return upstox_adapter, fyers_adapter

    async def _discover_expiries(
        self,
        *,
        underlying: str,
        app_symbol: str,
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter],
    ) -> list[date]:
        raw_expiries: list[str] = []
        broker_symbol = to_broker_symbol(app_symbol)
        fyers_symbol = to_fyers_symbol(app_symbol)

        if upstox_adapter is not None:
            try:
                raw_expiries = sorted(
                    {
                        str(item.get("expiry"))
                        for item in await upstox_adapter.get_option_contracts(broker_symbol)
                        if item.get("expiry")
                    }
                )
            except Exception as exc:
                logger.debug(f"[AuctionIQ] Upstox expiry discovery failed for {underlying}: {exc}")

        if not raw_expiries and fyers_adapter is not None:
            try:
                raw_expiries = sorted(
                    {
                        str(item.get("expiry"))
                        for item in await fyers_adapter.get_option_contracts(fyers_symbol)
                        if item.get("expiry")
                    }
                )
            except Exception as exc:
                logger.debug(f"[AuctionIQ] Fyers expiry discovery failed for {underlying}: {exc}")

        if not raw_expiries:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT DISTINCT expiry
                        FROM fo_contract_catalog
                        WHERE underlying = :underlying
                          AND expiry >= CURRENT_DATE
                        ORDER BY expiry ASC
                        """
                    ),
                    {"underlying": underlying},
                )
                raw_expiries = [
                    row.expiry.isoformat()
                    for row in result.fetchall()
                    if getattr(row, "expiry", None) is not None
                ]

        expiries: list[date] = []
        for item in raw_expiries:
            try:
                expiries.append(date.fromisoformat(str(item)))
            except ValueError:
                continue
        return sorted({item for item in expiries if item is not None})

    def _select_expiry(
        self,
        *,
        expiries: list[date],
        session: SessionContext,
        agent_name: str,
    ) -> Optional[date]:
        session_date = self._coerce_session_date(session.session_date)
        min_dte = int(self.config.get("min_dte_by_agent", {}).get(agent_name, 0))
        close_buffer = int(self.config.get("same_day_close_buffer_minutes", {}).get(agent_name, 0))

        eligible = [
            expiry
            for expiry in expiries
            if (expiry - session_date).days >= min_dte
            and not ((expiry - session_date).days == 0 and session.minutes_to_close < close_buffer)
        ]
        if eligible:
            return eligible[0]

        return None  # Insufficient DTE is a refusal, never a nearer-expiry substitution.

    @staticmethod
    def _coerce_session_date(value: date | str) -> date:
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    async def _load_chain(
        self,
        *,
        app_symbol: str,
        expiry: date,
        upstox_adapter: Optional[BrokerAdapter],
        fyers_adapter: Optional[BrokerAdapter],
    ) -> Optional[OptionChain]:
        broker_symbol = to_broker_symbol(app_symbol)
        fyers_symbol = to_fyers_symbol(app_symbol)
        expiry_iso = expiry.isoformat()
        cache_key = (app_symbol, expiry_iso)

        cached = self._chain_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            cached_payload = await option_chain_service.get_cached(app_symbol, expiry_iso)
        except Exception as exc:
            logger.debug(f"[AuctionIQ] Local option-chain cache lookup failed for {app_symbol} {expiry_iso}: {exc}")
            cached_payload = None
        if cached_payload and self._fresh_quote(cached_payload.get("timestamp")) and (cached_payload.get("data_quality") or {}).get("execution_ready") is not False:
            chain = OptionChain(
                symbol=str(cached_payload.get("symbol") or app_symbol),
                expiry=expiry_iso,
                spot_price=float(cached_payload.get("spot_price") or 0.0),
                entries=[
                    OptionChainEntry(
                        strike=float(item.get("strike") or 0.0),
                        option_type=str(item.get("option_type") or "").upper(),
                        ltp=float(item.get("ltp") or 0.0),
                        oi=int(item.get("oi") or 0),
                        volume=int(item.get("volume") or 0),
                        bid=float(item.get("bid") or 0.0),
                        ask=float(item.get("ask") or 0.0),
                        iv=float(item["iv"]) if item.get("iv") is not None else None,
                        delta=float(item["delta"]) if item.get("delta") is not None else None,
                        gamma=float(item["gamma"]) if item.get("gamma") is not None else None,
                        theta=float(item["theta"]) if item.get("theta") is not None else None,
                        vega=float(item["vega"]) if item.get("vega") is not None else None,
                        prev_oi=float(item["prev_oi"]) if item.get("prev_oi") is not None else None,
                        prev_close=float(item["prev_close"]) if item.get("prev_close") is not None else None,
                        instrument_key=str(item.get("instrument_key") or "").strip() or None,
                    )
                    for item in list(cached_payload.get("entries") or [])
                ],
            )
            if chain.entries:
                self._chain_cache[cache_key] = chain
                return chain

        if self.config.get("local_data_only", True) or settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY:
            fallback_chain = await self._load_local_atm_watchlist_chain(app_symbol, expiry_iso)
            if fallback_chain is not None:
                self._chain_cache[cache_key] = fallback_chain
                return fallback_chain
            return None

        if upstox_adapter is not None:
            try:
                chain = await upstox_adapter.get_option_chain(broker_symbol, expiry_iso)
                if chain and chain.entries:
                    self._chain_cache[cache_key] = chain
                    return chain
            except Exception as exc:
                logger.debug(f"[AuctionIQ] Upstox option chain failed for {broker_symbol} {expiry_iso}: {exc}")

        if fyers_adapter is not None:
            try:
                chain = await fyers_adapter.get_option_chain(fyers_symbol, expiry_iso)
                if chain and chain.entries:
                    self._chain_cache[cache_key] = chain
                    return chain
            except Exception as exc:
                logger.debug(f"[AuctionIQ] Fyers option chain failed for {fyers_symbol} {expiry_iso}: {exc}")
        return None

    async def _load_local_atm_watchlist_chain(
        self,
        app_symbol: str,
        expiry_iso: str,
    ) -> Optional[OptionChain]:
        underlying = str(DISPLAY_NAMES.get(app_symbol) or app_symbol.split(":")[-1].removesuffix("-EQ")).upper().strip()
        if not underlying:
            return None
        payload = await atm_watchlist_service.get_watchlist(
            expiry=expiry_iso,
            symbols=[underlying],
            live_refresh=False,
        )
        row = next(
            (
                item
                for item in list(payload.get("rows") or [])
                if str(item.get("underlying") or "").upper() == underlying
            ),
            None,
        )
        if row is None:
            return None

        entries: list[OptionChainEntry] = []
        for side_key, option_type in (("ce", "CE"), ("pe", "PE")):
            side = row.get(side_key) or {}
            ltp = float(side.get("ltp") or 0.0)
            if not self._fresh_quote(side.get("as_of")):
                continue
            if ltp <= 0:
                continue
            entries.append(
                OptionChainEntry(
                    strike=float(side.get("strike") or row.get("atm_strike") or 0.0),
                    option_type=option_type,
                    ltp=ltp,
                    oi=int(side.get("oi") or 0),
                    volume=int(side.get("volume") or 0),
                    bid=float(side.get("bid") or 0),
                    ask=float(side.get("ask") or 0),
                    iv=float(side["iv"]) if side.get("iv") is not None else None,
                    delta=float(side["delta"]) if side.get("delta") is not None else None,
                    gamma=float(side["gamma"]) if side.get("gamma") is not None else None,
                    theta=float(side["theta"]) if side.get("theta") is not None else None,
                    vega=float(side["vega"]) if side.get("vega") is not None else None,
                    prev_oi=float(side["prev_oi"]) if side.get("prev_oi") is not None else None,
                    prev_close=float(side["prev_close"]) if side.get("prev_close") is not None else None,
                    instrument_key=str(side.get("instrument_key") or "").strip() or None,
                )
            )

        if not entries:
            return None
        return OptionChain(
            symbol=app_symbol,
            expiry=expiry_iso,
            spot_price=float(row.get("spot_price") or 0.0),
            entries=entries,
        )

    async def _load_contract_rows(
        self,
        *,
        underlying: str,
        expiry: date,
        app_symbol: str,
        upstox_adapter: Optional[BrokerAdapter],
    ) -> dict[tuple[float, str], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        broker_symbol = to_broker_symbol(app_symbol)
        expiry_iso = expiry.isoformat()
        if upstox_adapter is not None:
            try:
                rows = [
                    {
                        "instrument_key": item.get("instrument_key"),
                        "trading_symbol": item.get("trading_symbol"),
                        "strike": float(item.get("strike_price") or 0.0),
                        "option_type": str(item.get("instrument_type") or "").upper(),
                        "lot_size": item.get("lot_size"),
                    }
                    for item in await upstox_adapter.get_option_contracts(broker_symbol, expiry_iso)
                    if item.get("instrument_key")
                ]
            except Exception as exc:
                logger.debug(f"[AuctionIQ] Upstox contract lookup failed for {underlying} {expiry_iso}: {exc}")

        if not rows:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT instrument_key, trading_symbol, strike, option_type, lot_size
                        FROM fo_contract_catalog
                        WHERE underlying = :underlying
                          AND expiry = :expiry
                        ORDER BY updated_at DESC NULLS LAST
                        """
                    ),
                    {
                        "underlying": underlying,
                        "expiry": expiry,
                    },
                )
                rows = [
                    {
                        "instrument_key": row.instrument_key,
                        "trading_symbol": row.trading_symbol,
                        "strike": float(row.strike or 0.0),
                        "option_type": str(row.option_type or "").upper(),
                        "lot_size": row.lot_size,
                    }
                    for row in result.fetchall()
                ]

        return {
            (float(row["strike"]), str(row["option_type"]).upper()): row
            for row in rows
            if row.get("option_type") in {"CE", "PE"}
        }

    async def _select_contract(
        self,
        *,
        session: SessionContext,
        decision: AgentDecision,
        instruction: ExecutionInstruction,
        underlying: str,
        expiry: date,
        chain: OptionChain,
        contracts: dict[tuple[float, str], dict[str, Any]],
        option_type: str,
        expiries: list[date],
        ntm_volx: NTMVolXSnapshot | None = None,
    ) -> Optional[dict[str, Any]]:
        option_entries = [
            entry
            for entry in chain.entries
            if str(entry.option_type or "").upper() == option_type and float(entry.ltp or 0.0) > 0
        ]
        if not option_entries:
            return None

        strikes = sorted({float(entry.strike) for entry in option_entries})
        if not strikes:
            return None
        spot_price = float(chain.spot_price or session.last_price or 0.0)
        atm_strike = min(strikes, key=lambda strike: abs(strike - spot_price))
        preferred_moneyness = self._preferred_moneyness(decision.agent_name, decision.confidence)
        target_delta = float(self.config.get("target_delta_by_agent", {}).get(decision.agent_name, 0.55))
        min_premium = float(self.config.get("min_premium", 2.0))
        max_premium = float(self.config.get("max_premium", 500.0))
        min_volume = float(self.config.get("min_volume", 1.0))
        min_oi = float(self.config.get("min_oi", 1.0))
        max_spread_pct = float(self.config.get("max_spread_pct", 0.15))
        scored = self._score_candidates(
            option_entries=option_entries,
            contracts=contracts,
            option_type=option_type,
            atm_strike=atm_strike,
            strikes=strikes,
            preferred_moneyness=preferred_moneyness,
            target_delta=target_delta,
            ntm_volx=ntm_volx,
            min_premium=min_premium,
            max_premium=max_premium,
            min_volume=min_volume,
            min_oi=min_oi,
            max_spread_pct=max_spread_pct,
            relaxed_filters=False,
        )

        if not scored and self.config.get("enable_relaxed_fallback", True):
            scored = self._score_candidates(
                option_entries=option_entries,
                contracts=contracts,
                option_type=option_type,
                atm_strike=atm_strike,
                strikes=strikes,
                preferred_moneyness=preferred_moneyness,
                target_delta=target_delta,
                ntm_volx=ntm_volx,
                min_premium=min_premium,
                max_premium=float(self.config.get("relaxed_max_premium", max_premium)),
                min_volume=float(self.config.get("relaxed_min_volume", min_volume)),
                min_oi=float(self.config.get("relaxed_min_oi", min_oi)),
                max_spread_pct=float(self.config.get("relaxed_max_spread_pct", max_spread_pct)),
                relaxed_filters=True,
            )

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        for _, candidate in scored[:5]:
            selected = await self._finalize_candidate(
                session=session,
                decision=decision,
                instruction=instruction,
                underlying=underlying,
                expiry=expiry,
                option_type=option_type,
                candidate=candidate,
                spot_price=spot_price,
                expiries=expiries,
                ntm_volx=ntm_volx,
            )
            if selected is not None:
                return selected
        return None

    async def _finalize_candidate(
        self,
        *,
        session: SessionContext,
        decision: AgentDecision,
        instruction: ExecutionInstruction,
        underlying: str,
        expiry: date,
        option_type: str,
        candidate: dict[str, Any],
        spot_price: float,
        expiries: list[date],
        ntm_volx: NTMVolXSnapshot | None = None,
    ) -> Optional[dict[str, Any]]:
        entry: OptionChainEntry = candidate["entry"]
        contract = candidate["contract"]
        strike = float(entry.strike)
        instrument_key = str(contract.get("instrument_key") or entry.instrument_key or "").strip() or None
        trading_symbol = str(contract.get("trading_symbol") or "").strip() or None
        lot_size = await self._resolve_lot_size(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
            contract=contract,
        )
        if not lot_size:
            return None

        premium = float(candidate["premium"])
        premium_ma20, premium_ma50, above_ma20, above_ma50 = await self._load_option_ma_context(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
            premium=premium,
        )
        relaxed_filters = bool(candidate.get("relaxed_filters"))
        if self.config.get("require_option_ma20", True) and premium_ma20 is not None and not above_ma20 and not relaxed_filters:
            return None

        contract_lot_size = max(int(lot_size), 1)
        lots = max(1, int(decision.quantity // contract_lot_size))
        quantity = lots * contract_lot_size

        mid_price = self._mid_price(entry)
        limit_price = round(mid_price, 2) if instruction.order_type == "LIMIT" and mid_price > 0 else None
        expiry_kind = self._expiry_kind(expiry, expiries)
        session_date = self._coerce_session_date(session.session_date)
        dte = max((expiry - session_date).days, 0)
        symbol = trading_symbol or instrument_key or f"{underlying} {expiry.isoformat()} {int(strike)} {option_type}"
        selection_reason = (
            f"{decision.agent_name} {decision.action} mapped to {option_type} "
            f"{candidate['moneyness']} {int(strike)} for {expiry_kind} expiry"
        )

        rationale = [
            f"Buy {option_type} instead of shorting the underlying; broker action stays BUY.",
            f"Selected {candidate['moneyness']} {int(strike)} at premium {premium:.2f} "
            f"(spread {candidate['spread_pct'] * 100:.1f}%).",
            f"Expiry {expiry.isoformat()} ({expiry_kind}, DTE={dte}) aligned to {decision.agent_name} sleeve.",
        ]
        ntm_level = candidate.get("ntm_level")
        if ntm_volx is not None:
            rationale.append(
                f"NTM VolX {ntm_volx.dominant_side.lower()} bias {ntm_volx.vxr:.2f}x with net pressure {ntm_volx.net_pressure:+.0%}."
            )
            if ntm_level is not None:
                strike_pressure = ntm_level["call_pressure"] if option_type == "CE" else ntm_level["put_pressure"]
                rationale.append(
                    f"Strike {int(strike)} contributes {strike_pressure:.0f} NTM pressure on the selected side."
                )
        if premium_ma20 is not None:
            ma50_text = f"{premium_ma50:.2f}" if premium_ma50 is not None else "n/a"
            rationale.append(
                f"Premium MA20={premium_ma20:.2f}, MA50={ma50_text}, "
                f"above_MA20={'yes' if above_ma20 else 'no'}."
            )
        else:
            rationale.append("Premium MA20 unavailable from saved history; using live premium and liquidity checks only.")
        if relaxed_filters:
            rationale.append(
                "Used the relaxed paper-trade fallback because no contract passed the primary premium/liquidity filter stack."
            )

        return {
            "symbol": symbol,
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "option_type": option_type,
            "strike": strike,
            "lot_size": lot_size,
            "quantity": quantity,
            "premium": round(premium, 2),
            "spot_price": round(spot_price, 2),
            "limit_price": limit_price,
            "moneyness": candidate["moneyness"],
            "expiry_kind": expiry_kind,
            "days_to_expiry": dte,
            "selection_reason": selection_reason,
            "premium_ma20": premium_ma20,
            "premium_ma50": premium_ma50,
            "above_premium_ma20": above_ma20,
            "above_premium_ma50": above_ma50,
            "rationale": rationale,
        }

    def _score_candidates(
        self,
        *,
        option_entries: list[OptionChainEntry],
        contracts: dict[tuple[float, str], dict[str, Any]],
        option_type: str,
        atm_strike: float,
        strikes: list[float],
        preferred_moneyness: list[str],
        target_delta: float,
        ntm_volx: NTMVolXSnapshot | None,
        min_premium: float,
        max_premium: float,
        min_volume: float,
        min_oi: float,
        max_spread_pct: float,
        relaxed_filters: bool,
    ) -> list[tuple[float, dict[str, Any]]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in option_entries:
            if not (isfinite(float(entry.ask or 0)) and 0 < float(entry.bid or 0) <= float(entry.ask or 0)):
                continue
            strike = float(entry.strike)
            contract = contracts.get((strike, option_type), {})
            premium = self._buy_touch_price(entry)
            if premium < min_premium or premium > max_premium:
                continue
            if float(entry.volume or 0.0) < min_volume:
                continue
            if float(entry.oi or 0.0) < min_oi:
                continue

            spread_pct = self._spread_pct(entry, premium)
            if spread_pct > max_spread_pct:
                continue

            moneyness = self._moneyness_label(
                option_type=option_type,
                strike=strike,
                atm_strike=atm_strike,
                strikes=strikes,
            )
            preference_penalty = self._preference_penalty(moneyness, preferred_moneyness)
            delta_penalty = 0.0
            if entry.delta is not None and isfinite(float(entry.delta)):
                delta_penalty = abs(abs(float(entry.delta)) - target_delta)
            ntm_level = self._ntm_level(ntm_volx, strike)
            pressure_bonus = 0.0
            if ntm_level is not None:
                dominant_pressure = max(float(ntm_volx.call_pressure), float(ntm_volx.put_pressure), 1.0)
                strike_pressure = float(ntm_level["call_pressure"] if option_type == "CE" else ntm_level["put_pressure"])
                pressure_bonus = (strike_pressure / dominant_pressure) * 5.0

            score = (
                (float(entry.volume or 0.0) / 1000.0)
                + (float(entry.oi or 0.0) / 10000.0)
                - (spread_pct * 50.0)
                - (preference_penalty * 2.5)
                - (delta_penalty * 5.0)
                + pressure_bonus
            )
            if relaxed_filters:
                score -= 0.35
            scored.append(
                (
                    score,
                    {
                        "entry": entry,
                        "contract": contract,
                        "premium": premium,
                        "spread_pct": spread_pct,
                        "moneyness": moneyness,
                        "ntm_level": ntm_level,
                        "relaxed_filters": relaxed_filters,
                    },
                )
            )
        return scored

    def _ntm_level(
        self,
        ntm_volx: NTMVolXSnapshot | None,
        strike: float,
    ) -> dict[str, float] | None:
        if ntm_volx is None:
            return None
        for level in ntm_volx.pressure_ladder:
            if abs(float(level.strike) - float(strike)) <= 0.001:
                return {
                    "call_pressure": float(level.call_pressure),
                    "put_pressure": float(level.put_pressure),
                    "net_pressure": float(level.net_pressure),
                }
        return None

    async def _resolve_lot_size(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
        contract: dict[str, Any],
    ) -> Optional[int]:
        raw_lot = contract.get("lot_size")
        if raw_lot:
            try:
                return int(raw_lot)
            except (TypeError, ValueError):
                pass
        return await option_history_service.resolve_lot_size(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
        )

    async def _load_option_ma_context(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        instrument_key: Optional[str],
        premium: float,
    ) -> tuple[Optional[float], Optional[float], Optional[bool], Optional[bool]]:
        candles = await option_history_service.load_candles(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            instrument_key=instrument_key,
            interval=str(self.config.get("history_interval", "5minute")),
            limit=int(self.config.get("history_limit", 60)),
            allow_broker_refresh=not (settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY),
        )
        closes = [float(item["close"]) for item in candles if item.get("close") is not None]
        ma20 = compute_ema(closes, 20)[-1] if len(closes) >= 20 else None
        ma50 = compute_ema(closes, 50)[-1] if len(closes) >= 50 else None
        above_ma20 = None if ma20 is None else premium >= ma20
        above_ma50 = None if ma50 is None else premium >= ma50
        return (
            round(float(ma20), 2) if ma20 is not None else None,
            round(float(ma50), 2) if ma50 is not None else None,
            above_ma20,
            above_ma50,
        )

    def _preferred_moneyness(self, agent_name: str, confidence: float) -> list[str]:
        strong_confidence = float(self.config.get("strong_confidence", 0.78))
        very_strong_confidence = float(self.config.get("very_strong_confidence", 0.86))
        lookup_key = agent_name
        if agent_name == "positional" and confidence >= very_strong_confidence:
            lookup_key = "positional_strong"
        elif agent_name == "swing" and confidence >= strong_confidence:
            lookup_key = "swing_strong"
        return list(self.config.get("preferred_moneyness", {}).get(lookup_key, ["ATM", "ITM1", "OTM1"]))

    def _preference_penalty(self, moneyness: str, preferences: list[str]) -> int:
        try:
            return preferences.index(moneyness)
        except ValueError:
            return len(preferences) + 2

    def _moneyness_label(
        self,
        *,
        option_type: str,
        strike: float,
        atm_strike: float,
        strikes: list[float],
    ) -> str:
        try:
            atm_index = strikes.index(atm_strike)
            strike_index = strikes.index(strike)
        except ValueError:
            return "ATM"

        offset = strike_index - atm_index
        if offset == 0:
            return "ATM"
        if option_type == "CE":
            return f"ITM{abs(offset)}" if offset < 0 else f"OTM{abs(offset)}"
        return f"ITM{abs(offset)}" if offset > 0 else f"OTM{abs(offset)}"

    def _expiry_kind(self, expiry: date, expiries: list[date]) -> str:
        month_expiries = [item for item in expiries if item.year == expiry.year and item.month == expiry.month]
        if month_expiries and expiry == max(month_expiries):
            return "monthly"
        return "weekly"

    @staticmethod
    def _fresh_quote(value: Any, *, max_age: float = 120) -> bool:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return -5 <= age <= max_age
        except (TypeError, ValueError):
            return False

    def _extract_underlying(self, symbol: str) -> str:
        normalized = str(symbol or "").upper().replace(" INDEX", "").replace(" FUT", "").strip()
        if ":" in normalized:
            normalized = normalized.split(":")[-1]
        return normalized

    def _buy_touch_price(self, entry: OptionChainEntry) -> float:
        ask = float(entry.ask or 0.0)
        ltp = float(entry.ltp or 0.0)
        bid = float(entry.bid or 0.0)
        if ask > 0:
            return ask
        if ltp > 0:
            return ltp
        if bid > 0:
            return bid
        return 0.0

    def _mid_price(self, entry: OptionChainEntry) -> float:
        bid = float(entry.bid or 0.0)
        ask = float(entry.ask or 0.0)
        if bid > 0 and ask > 0 and ask >= bid:
            return round((bid + ask) / 2.0, 2)
        return round(float(entry.ltp or ask or bid or 0.0), 2)

    def _spread_pct(self, entry: OptionChainEntry, premium: float) -> float:
        bid = float(entry.bid or 0.0)
        ask = float(entry.ask or 0.0)
        if premium <= 0 or ask <= 0 or bid <= 0 or ask < bid:
            return 0.0
        return (ask - bid) / premium
