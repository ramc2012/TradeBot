from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from auction_intelligence.paper.journal import resolve_journal_root
from auction_intelligence.schemas import AnalysisBundle, PaperPositionRecord
from market_data.option_history import option_history_service


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_underlying(value: str | None) -> str:
    return str(value or "").upper().replace(" FUT", "").strip()


def _same_contract(position: dict[str, Any], execution: Any) -> bool:
    position_key = str(position.get("instrument_key") or "").strip()
    execution_key = str(getattr(execution, "instrument_key", None) or "").strip()
    if position_key and execution_key:
        return position_key == execution_key

    position_symbol = str(position.get("trading_symbol") or position.get("symbol") or "").strip()
    execution_symbol = str(getattr(execution, "trading_symbol", None) or getattr(execution, "symbol", None) or "").strip()
    if position_symbol and execution_symbol:
        return position_symbol == execution_symbol

    return (
        str(position.get("option_type") or "") == str(getattr(execution, "option_type", None) or "")
        and str(position.get("expiry") or "") == str(getattr(execution, "expiry", None) or "")
        and float(position.get("strike") or 0.0) == float(getattr(execution, "strike", None) or 0.0)
    )


class PaperPositionBook:
    def __init__(self, root: Path | str):
        self.root = resolve_journal_root(root)
        self.path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()

    async def list_positions(
        self,
        *,
        symbol: str | None = None,
        status: str = "all",
        limit: int = 50,
    ) -> dict[str, Any]:
        state = await self._load_state()
        normalized = _normalize_underlying(symbol)
        open_positions = self._filter_positions(state.get("open_positions", []), symbol=normalized)
        closed_positions = self._filter_positions(state.get("closed_positions", []), symbol=normalized)
        open_positions.sort(key=lambda item: str(item.get("opened_at") or ""), reverse=True)
        closed_positions.sort(key=lambda item: str(item.get("closed_at") or item.get("updated_at") or ""), reverse=True)

        if status == "open":
            closed_positions = []
        elif status == "closed":
            open_positions = []

        open_positions = open_positions[:limit]
        closed_positions = closed_positions[:limit]

        return {
            "symbol_filter": normalized or None,
            "status": status,
            "summary": self._summary(state, symbol=normalized),
            "open_positions": open_positions,
            "closed_positions": closed_positions,
        }

    async def sync_analysis(self, bundle: AnalysisBundle) -> dict[str, Any]:
        async with self._lock:
            state = await self._load_state()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))

            decisions_by_agent = {decision.agent_name: decision for decision in bundle.agent_decisions}
            executions_by_agent = {execution.agent_name: execution for execution in bundle.execution_plan if execution.action != "FLAT"}
            underlying = _normalize_underlying(
                next(
                    (
                        execution.underlying_symbol
                        for execution in bundle.execution_plan
                        if getattr(execution, "underlying_symbol", None)
                    ),
                    bundle.market_profile.symbol,
                )
            )
            now = _utc_now()

            for agent_name, decision in decisions_by_agent.items():
                matching = [
                    position
                    for position in open_positions
                    if position.get("agent_name") == agent_name
                    and _normalize_underlying(position.get("underlying_symbol")) == underlying
                ]
                if not matching:
                    execution = executions_by_agent.get(agent_name)
                    if execution is not None and decision.action != "FLAT":
                        open_positions.append(
                            self._open_position(
                                bundle=bundle,
                                decision=decision,
                                execution=execution,
                                now=now,
                                underlying=underlying,
                            )
                        )
                    continue

                primary = matching[0]
                for extra in matching[1:]:
                    await self._close_position(
                        position=extra,
                        bundle=bundle,
                        now=now,
                        reason="dedupe_repair",
                        execution=None,
                    )
                    open_positions.remove(extra)
                    closed_positions.append(extra)

                execution = executions_by_agent.get(agent_name)
                if decision.action == "FLAT":
                    await self._close_position(
                        position=primary,
                        bundle=bundle,
                        now=now,
                        reason="flat_signal",
                        execution=None,
                    )
                    open_positions.remove(primary)
                    closed_positions.append(primary)
                    continue

                if execution is None:
                    await self._refresh_open_position(
                        position=primary,
                        bundle=bundle,
                        decision=decision,
                        now=now,
                        execution=None,
                    )
                    continue

                if primary.get("signal_action") == decision.action and _same_contract(primary, execution):
                    await self._refresh_open_position(
                        position=primary,
                        bundle=bundle,
                        decision=decision,
                        now=now,
                        execution=execution,
                    )
                    continue

                close_reason = "signal_flip" if primary.get("signal_action") != decision.action else "contract_roll"
                await self._close_position(
                    position=primary,
                    bundle=bundle,
                    now=now,
                    reason=close_reason,
                    execution=None,
                )
                open_positions.remove(primary)
                closed_positions.append(primary)
                open_positions.append(
                    self._open_position(
                        bundle=bundle,
                        decision=decision,
                        execution=execution,
                        now=now,
                        underlying=underlying,
                    )
                )

            closed_positions.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
            state = {
                "open_positions": open_positions,
                "closed_positions": closed_positions[:250],
                "last_synced_at": now,
            }
            await self._save_state(state)
            return self._summary(state, symbol=underlying)

    async def _refresh_open_position(
        self,
        *,
        position: dict[str, Any],
        bundle: AnalysisBundle,
        decision: Any,
        now: str,
        execution: Any | None,
    ) -> None:
        latest_premium = await self._resolve_premium(position=position, execution=execution)
        latest_spot = self._spot_from_execution_or_bundle(bundle=bundle, execution=execution, fallback=position.get("latest_spot_price"))
        position["updated_at"] = now
        position["latest_confidence"] = float(decision.confidence or position.get("latest_confidence") or position.get("entry_confidence") or 0.0)
        position["latest_premium"] = latest_premium
        position["latest_spot_price"] = latest_spot
        position["regime_last"] = str(bundle.regime.label)
        position["stop_price"] = decision.stop_price
        position["target_price"] = decision.target_price
        position["execution_style"] = getattr(execution, "style", None) or position.get("execution_style")
        entry_premium = float(position.get("entry_premium") or latest_premium or 0.0)
        quantity = int(position.get("quantity") or 0)
        position["unrealized_pnl"] = round((latest_premium - entry_premium) * quantity, 2)

    async def _close_position(
        self,
        *,
        position: dict[str, Any],
        bundle: AnalysisBundle,
        now: str,
        reason: str,
        execution: Any | None,
    ) -> None:
        exit_premium = await self._resolve_premium(position=position, execution=execution)
        exit_spot = self._spot_from_execution_or_bundle(bundle=bundle, execution=execution, fallback=position.get("latest_spot_price"))
        entry_premium = float(position.get("entry_premium") or exit_premium or 0.0)
        quantity = int(position.get("quantity") or 0)
        position["status"] = "closed"
        position["updated_at"] = now
        position["closed_at"] = now
        position["close_reason"] = reason
        position["exit_premium"] = exit_premium
        position["exit_spot_price"] = exit_spot
        position["latest_premium"] = exit_premium
        position["latest_spot_price"] = exit_spot
        position["regime_last"] = str(bundle.regime.label)
        position["unrealized_pnl"] = 0.0
        position["realized_pnl"] = round((exit_premium - entry_premium) * quantity, 2)

    def _open_position(
        self,
        *,
        bundle: AnalysisBundle,
        decision: Any,
        execution: Any,
        now: str,
        underlying: str,
    ) -> dict[str, Any]:
        entry_premium = float(getattr(execution, "premium", None) or getattr(execution, "limit_price", None) or 0.0)
        spot_price = self._spot_from_execution_or_bundle(bundle=bundle, execution=execution, fallback=None)
        record = PaperPositionRecord(
            position_id=uuid4().hex,
            status="open",
            opened_at=now,
            updated_at=now,
            agent_name=decision.agent_name,
            signal_action=decision.action,
            broker_action=getattr(execution, "broker_action", None),
            underlying_symbol=underlying,
            symbol=str(getattr(execution, "symbol", None) or underlying),
            regime_entry=str(bundle.regime.label),
            regime_last=str(bundle.regime.label),
            quantity=int(getattr(execution, "quantity", None) or decision.quantity or 0),
            entry_confidence=float(decision.confidence or 0.0),
            latest_confidence=float(decision.confidence or 0.0),
            entry_premium=round(entry_premium, 2),
            latest_premium=round(entry_premium, 2),
            entry_spot_price=spot_price,
            latest_spot_price=spot_price,
            execution_style=getattr(execution, "style", None),
            instrument_type=getattr(execution, "instrument_type", None),
            expiry=getattr(execution, "expiry", None),
            strike=getattr(execution, "strike", None),
            option_type=getattr(execution, "option_type", None),
            instrument_key=getattr(execution, "instrument_key", None),
            trading_symbol=getattr(execution, "trading_symbol", None),
            lot_size=getattr(execution, "lot_size", None),
            moneyness=getattr(execution, "moneyness", None),
            expiry_kind=getattr(execution, "expiry_kind", None),
            days_to_expiry=getattr(execution, "days_to_expiry", None),
            selection_reason=getattr(execution, "selection_reason", None),
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            notes=[*decision.rationale[:3], *list(getattr(execution, "rationale", [])[:2])],
        )
        return asdict(record)

    async def _resolve_premium(self, *, position: dict[str, Any], execution: Any | None) -> float:
        execution_premium = float(getattr(execution, "premium", None) or getattr(execution, "limit_price", None) or 0.0) if execution is not None else 0.0
        if execution_premium > 0:
            return round(execution_premium, 2)

        expiry = position.get("expiry")
        option_type = position.get("option_type")
        strike = position.get("strike")
        underlying = position.get("underlying_symbol")
        instrument_key = position.get("instrument_key")
        if expiry and option_type and strike and underlying:
            try:
                candles = await option_history_service.load_candles(
                    underlying=str(underlying),
                    expiry=date.fromisoformat(str(expiry)),
                    strike=float(strike),
                    option_type=str(option_type),
                    instrument_key=str(instrument_key) if instrument_key else None,
                    interval="1minute",
                    limit=1,
                )
                if candles and candles[-1].get("close") is not None:
                    return round(float(candles[-1]["close"]), 2)
            except Exception:
                pass

        fallback = float(position.get("latest_premium") or position.get("entry_premium") or 0.0)
        return round(fallback, 2)

    def _spot_from_execution_or_bundle(
        self,
        *,
        bundle: AnalysisBundle,
        execution: Any | None,
        fallback: Any,
    ) -> Optional[float]:
        spot = getattr(execution, "spot_price", None) if execution is not None else None
        if spot is None:
            spot = bundle.market_profile.close_price if getattr(bundle.market_profile, "close_price", None) is not None else fallback
        return round(float(spot), 2) if spot is not None else None

    def _summary(self, state: dict[str, Any], *, symbol: str | None = None) -> dict[str, Any]:
        open_positions = self._filter_positions(state.get("open_positions", []), symbol=symbol)
        closed_positions = self._filter_positions(state.get("closed_positions", []), symbol=symbol)
        realized = sum(float(item.get("realized_pnl") or 0.0) for item in closed_positions)
        unrealized = sum(float(item.get("unrealized_pnl") or 0.0) for item in open_positions)
        return {
            "symbol_filter": symbol or None,
            "open_count": len(open_positions),
            "closed_count": len(closed_positions),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "latest_opened_at": open_positions[0].get("opened_at") if open_positions else None,
            "latest_closed_at": closed_positions[0].get("closed_at") if closed_positions else None,
            "last_synced_at": state.get("last_synced_at"),
        }

    def _filter_positions(self, positions: list[dict[str, Any]], *, symbol: str | None) -> list[dict[str, Any]]:
        if not symbol:
            return list(positions)
        return [
            position
            for position in positions
            if _normalize_underlying(position.get("underlying_symbol")) == symbol
        ]

    async def _load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}

        def _read(path: Path) -> dict[str, Any]:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

        try:
            return await asyncio.to_thread(_read, self.path)
        except (OSError, json.JSONDecodeError):
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}

    async def _save_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        def _write(path: Path, payload: dict[str, Any]) -> None:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)

        await asyncio.to_thread(_write, self.path, state)
