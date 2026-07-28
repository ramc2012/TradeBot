from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from core.config import settings
from core.paper_trade_recorder import paper_trade_recorder
from auction_intelligence.paper.journal import resolve_journal_root
from auction_intelligence.schemas import AnalysisBundle, PaperPositionRecord
from market_data.option_history import option_history_service


# Notional paper-account capital for the Auction Intelligence lane. The
# summary surface reports total_equity, available_capital, drawdown, and
# Sharpe against this anchor. Raised to ₹50L to match the unbounded commodity
# book (DEFAULT_COMMODITY_INITIAL_CAPITAL) so the AI lane has headroom to deploy
# many concurrent option-buy positions without the account-capital reservation
# becoming the binding constraint. Override via env AI_INITIAL_CAPITAL.
AI_INITIAL_CAPITAL = float(os.environ.get("AI_INITIAL_CAPITAL", 5_000_000.0))


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


def _page_int(value: Any, default: int, *, floor: int = 0) -> int:
    """Coerce a paging argument defensively.

    The positions-overview WebSocket (api/websockets/ticks.py) calls the ROUTER
    functions directly as plain Python, so a FastAPI ``Query(...)`` default
    arrives here as a Query OBJECT, not an int — ``int(offset)`` raised and
    knocked this whole lane out of the WS frame every ~2s. Same hazard and same
    guard as directional_options/paper.py::_page_int.
    """
    try:
        return max(floor, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class PaperPositionBook:
    def __init__(self, root: Path | str, *, limits: dict[str, Any] | None = None):
        self.root = resolve_journal_root(root)
        self.path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()
        if limits is None:
            try:
                from auction_intelligence.config import clone_default_config

                limits = clone_default_config().get("position_limits", {})
            except Exception:
                limits = {}
        # Position-discipline knobs (see config defaults.json::position_limits):
        #   one_position_per_symbol  -> at most ONE open position per underlying
        #                               (the 3 agents collapse to the best decision;
        #                               opposite direction flips, no CE+PE concurrently).
        #   max_symbol_capital_fraction -> clamp qty so premium outgo <= frac * capital.
        #   hard_stop_premium_fraction  -> exit when the option premium falls this far.
        self.limits = dict(limits or {})

    def _exit_signal_confirmed(self, position: dict[str, Any], action: str, *, now: str) -> bool:
        """Require minimum hold time and repeated flat/flip observations."""
        opened = _parse_time(position.get("opened_at"))
        current = _parse_time(now) or datetime.now(timezone.utc)
        min_hold = max(0.0, float(self.limits.get("min_hold_seconds", 900) or 0))
        if opened is not None and (current - opened).total_seconds() < min_hold:
            return False

        normalized = str(action or "").upper()
        prior = str(position.get("pending_exit_action") or "").upper()
        count = int(position.get("pending_exit_count") or 0) + 1 if prior == normalized else 1
        position["pending_exit_action"] = normalized
        position["pending_exit_count"] = count
        required = max(1, int(self.limits.get("exit_confirmation_cycles", 2) or 1))
        return count >= required

    @staticmethod
    def _clear_pending_exit(position: dict[str, Any]) -> None:
        position.pop("pending_exit_action", None)
        position.pop("pending_exit_count", None)

    async def list_positions(
        self,
        *,
        symbol: str | None = None,
        status: str = "all",
        limit: int = 50,
        offset: int = 0,
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

        # Page each book independently, then report a combined envelope. offset=0
        # reproduces the historical (unpaged) response byte-for-byte.
        offset = _page_int(offset, 0)
        limit = _page_int(limit, 50, floor=1)
        open_total = len(open_positions)
        closed_total = len(closed_positions)
        open_positions = open_positions[offset : offset + limit]
        closed_positions = closed_positions[offset : offset + limit]

        return {
            "symbol_filter": normalized or None,
            "status": status,
            "summary": self._summary(state, symbol=normalized),
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            # pagination envelope (additive)
            "limit": limit,
            "offset": offset,
            "count": len(open_positions) + len(closed_positions),
            "total": open_total + closed_total,
            "has_more": bool(
                (offset + len(open_positions)) < open_total
                or (offset + len(closed_positions)) < closed_total
            ),
            "pagination": {
                "limit": limit,
                "offset": offset,
                "open": {
                    "total": open_total,
                    "returned": len(open_positions),
                    "has_more": (offset + len(open_positions)) < open_total,
                },
                "closed": {
                    "total": closed_total,
                    "returned": len(closed_positions),
                    "has_more": (offset + len(closed_positions)) < closed_total,
                },
            },
            "updated_at": state.get("last_synced_at"),
        }

    @staticmethod
    def _trade_record(position: dict[str, Any]) -> dict[str, Any]:
        """Flat (CSV-able) row for one CLOSED paper position.

        Pure RE-SHAPING of the persisted record: every money/size figure
        (quantity, entry_premium, exit_premium, realized_pnl, spots, stop,
        target) is passed through VERBATIM as persisted by ``_close_position``.
        Nothing here recomputes P&L. ``duration_minutes`` is the only derived
        field and it is plain arithmetic over the two stored timestamps.
        """
        opened = _parse_time(position.get("opened_at"))
        closed = _parse_time(position.get("closed_at") or position.get("updated_at"))
        duration_minutes = (
            round((closed - opened).total_seconds() / 60.0, 2)
            if opened is not None and closed is not None
            else None
        )
        return {
            "trade_id": position.get("position_id"),
            "position_id": position.get("position_id"),
            "underlying_symbol": position.get("underlying_symbol"),
            "symbol": position.get("symbol"),
            "trading_symbol": position.get("trading_symbol"),
            "instrument_key": position.get("instrument_key"),
            "agent_name": position.get("agent_name"),
            "signal_action": position.get("signal_action"),
            "broker_action": position.get("broker_action"),
            "instrument_type": position.get("instrument_type"),
            "option_type": position.get("option_type"),
            "strike": position.get("strike"),
            "expiry": position.get("expiry"),
            "expiry_kind": position.get("expiry_kind"),
            "days_to_expiry": position.get("days_to_expiry"),
            "moneyness": position.get("moneyness"),
            "execution_style": position.get("execution_style"),
            "regime_entry": position.get("regime_entry"),
            "regime_last": position.get("regime_last"),
            "opened_at": position.get("opened_at"),
            "closed_at": position.get("closed_at"),
            "duration_minutes": duration_minutes,
            "quantity": position.get("quantity"),
            "lot_size": position.get("lot_size"),
            "entry_premium": position.get("entry_premium"),
            "exit_premium": position.get("exit_premium"),
            "entry_spot_price": position.get("entry_spot_price"),
            "exit_spot_price": position.get("exit_spot_price"),
            "stop_price": position.get("stop_price"),
            "target_price": position.get("target_price"),
            "entry_confidence": position.get("entry_confidence"),
            "close_reason": position.get("close_reason"),
            "realized_pnl": position.get("realized_pnl"),
        }

    async def list_trades(
        self,
        *,
        symbol: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Closed-trade book: flat rows derived from ``closed_positions``.

        Read-only. Reuses the existing ``_summary`` aggregation so no P&L
        number is computed a second (different) way.
        """
        state = await self._load_state()
        normalized = _normalize_underlying(symbol)
        closed_positions = self._filter_positions(state.get("closed_positions", []), symbol=normalized)
        closed_positions.sort(
            key=lambda item: str(item.get("closed_at") or item.get("updated_at") or ""),
            reverse=True,
        )
        offset = _page_int(offset, 0)
        limit = _page_int(limit, 200, floor=1)
        total = len(closed_positions)
        page = closed_positions[offset : offset + limit]
        records = [self._trade_record(position) for position in page]
        return {
            "symbol_filter": normalized or None,
            "trades": records,
            "count": len(records),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(records)) < total,
            "summary": self._summary(state, symbol=normalized),
            "updated_at": state.get("last_synced_at"),
            "source": str(self.path),
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

            # Unconditional expiry sweep BEFORE the per-symbol sync. The
            # decision-driven exit (_maybe_exit -> "expired_contract") only ever
            # touches THIS cycle's bundle underlying, so a position in an
            # underlying we stopped scanning never gets closed — the SENSEX 76000
            # CE zombie (expired 2026-06-11) sat open for days. Sweep the whole
            # book each cycle so an expired contract can never linger.
            await self._sweep_expired_positions(
                bundle=bundle,
                open_positions=open_positions,
                closed_positions=closed_positions,
                now=now,
            )

            if bool(self.limits.get("one_position_per_symbol", True)):
                await self._sync_one_per_symbol(
                    bundle=bundle,
                    underlying=underlying,
                    decisions_by_agent=decisions_by_agent,
                    executions_by_agent=executions_by_agent,
                    open_positions=open_positions,
                    closed_positions=closed_positions,
                    now=now,
                )
            else:
                await self._sync_per_agent(
                    bundle=bundle,
                    underlying=underlying,
                    decisions_by_agent=decisions_by_agent,
                    executions_by_agent=executions_by_agent,
                    open_positions=open_positions,
                    closed_positions=closed_positions,
                    now=now,
                )

            closed_positions.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
            state = {
                "open_positions": open_positions,
                "closed_positions": closed_positions[:250],
                "last_synced_at": now,
            }
            await self._save_state(state)
            return self._summary(state, symbol=underlying)

    async def _sync_one_per_symbol(
        self,
        *,
        bundle: AnalysisBundle,
        underlying: str,
        decisions_by_agent: dict[str, Any],
        executions_by_agent: dict[str, Any],
        open_positions: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
        now: str,
    ) -> None:
        """One open position per underlying. The 3 agents (positional/swing/scalp)
        all evaluate the SAME single-symbol bundle, so we collapse them to the one
        best actionable decision and manage a single position keyed by underlying
        (not by agent). Opposite direction FLIPS (close then open the other leg);
        same direction HOLDS (no contract roll → no churn, never CE+PE together);
        a dominant FLAT closes. Quantity / hard-stop limits are applied at open /
        exit respectively.
        """
        matching = [
            position
            for position in open_positions
            if _normalize_underlying(position.get("underlying_symbol")) == underlying
        ]
        primary = matching[0] if matching else None
        for extra in matching[1:]:  # repair any pre-existing duplicates for this symbol
            await self._close_position(
                position=extra, bundle=bundle, now=now, reason="dedupe_repair", execution=None
            )
            if extra in open_positions:
                open_positions.remove(extra)
            closed_positions.append(extra)

        actionable = [
            (decision, executions_by_agent.get(agent_name))
            for agent_name, decision in decisions_by_agent.items()
            if decision.action != "FLAT" and executions_by_agent.get(agent_name) is not None
        ]
        if actionable:
            chosen_decision, chosen_execution = max(
                actionable, key=lambda pair: float(pair[0].confidence or 0.0)
            )
        else:
            chosen_decision, chosen_execution = None, None
        best_any = (
            max(decisions_by_agent.values(), key=lambda d: float(d.confidence or 0.0))
            if decisions_by_agent
            else None
        )

        if primary is None:
            if chosen_decision is not None:
                open_positions.append(
                    self._open_position(
                        bundle=bundle,
                        decision=chosen_decision,
                        execution=chosen_execution,
                        now=now,
                        underlying=underlying,
                    )
                )
            return

        # We already hold a position on this underlying.
        if chosen_decision is None:
            if best_any is not None and best_any.action == "FLAT":
                await self._refresh_open_position(
                    position=primary, bundle=bundle, decision=best_any, now=now, execution=None
                )
                if not self._exit_signal_confirmed(primary, "FLAT", now=now):
                    await self._maybe_exit(
                        primary,
                        bundle=bundle,
                        now=now,
                        execution=None,
                        open_positions=open_positions,
                        closed_positions=closed_positions,
                    )
                    return
                await self._close_position(
                    position=primary, bundle=bundle, now=now, reason="flat_signal", execution=None
                )
                if primary in open_positions:
                    open_positions.remove(primary)
                closed_positions.append(primary)
                return
            if best_any is not None:
                await self._refresh_open_position(
                    position=primary, bundle=bundle, decision=best_any, now=now, execution=None
                )
                await self._maybe_exit(
                    primary,
                    bundle=bundle,
                    now=now,
                    execution=None,
                    open_positions=open_positions,
                    closed_positions=closed_positions,
                )
            return

        if str(primary.get("signal_action") or "").upper() == str(chosen_decision.action or "").upper():
            # SAME direction → HOLD the single position (no roll → no churn, no CE+PE).
            self._clear_pending_exit(primary)
            await self._refresh_open_position(
                position=primary,
                bundle=bundle,
                decision=chosen_decision,
                now=now,
                execution=chosen_execution,
            )
            await self._maybe_exit(
                primary,
                bundle=bundle,
                now=now,
                execution=chosen_execution,
                open_positions=open_positions,
                closed_positions=closed_positions,
            )
            return

        # OPPOSITE direction → FLIP (close the existing leg, open the other side).
        await self._refresh_open_position(
            position=primary,
            bundle=bundle,
            decision=chosen_decision,
            now=now,
            execution=chosen_execution,
        )
        if not self._exit_signal_confirmed(primary, str(chosen_decision.action), now=now):
            await self._maybe_exit(
                primary,
                bundle=bundle,
                now=now,
                execution=chosen_execution,
                open_positions=open_positions,
                closed_positions=closed_positions,
            )
            return
        await self._close_position(
            position=primary, bundle=bundle, now=now, reason="signal_flip", execution=None
        )
        if primary in open_positions:
            open_positions.remove(primary)
        closed_positions.append(primary)
        open_positions.append(
            self._open_position(
                bundle=bundle,
                decision=chosen_decision,
                execution=chosen_execution,
                now=now,
                underlying=underlying,
            )
        )

    async def _sweep_expired_positions(
        self,
        *,
        bundle: AnalysisBundle,
        open_positions: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
        now: str,
    ) -> None:
        """Force-close EVERY open position whose contract has expired, regardless
        of whether this cycle's bundle is for that underlying. Reuses the same
        _close_position path as the decision-driven "expired_contract" exit (which
        prices the exit off the contract's own last candle, not the cross-symbol
        bundle). This is the safety net that stops an expired option from ever
        lingering as "open" once the lane stops scanning its underlying."""
        session_date = self._session_date(bundle)
        if session_date is None:
            return
        for position in list(open_positions):
            if str(position.get("status") or "") != "open":
                continue
            expiry = self._position_expiry(position)
            if expiry is not None and expiry < session_date:
                await self._close_position(
                    position=position, bundle=bundle, now=now, reason="expired_contract", execution=None
                )
                if position in open_positions:
                    open_positions.remove(position)
                closed_positions.append(position)

    async def _maybe_exit(
        self,
        position: dict[str, Any],
        *,
        bundle: AnalysisBundle,
        now: str,
        execution: Any | None,
        open_positions: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
    ) -> bool:
        exit_reason = self._exit_reason_for_position(position, bundle=bundle)
        if exit_reason is None:
            return False
        await self._close_position(
            position=position, bundle=bundle, now=now, reason=exit_reason, execution=execution
        )
        if position in open_positions:
            open_positions.remove(position)
        closed_positions.append(position)
        return True

    async def _sync_per_agent(
        self,
        *,
        bundle: AnalysisBundle,
        underlying: str,
        decisions_by_agent: dict[str, Any],
        executions_by_agent: dict[str, Any],
        open_positions: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
        now: str,
    ) -> None:
        """Legacy behaviour: one position PER AGENT per underlying (can stack
        positional+swing+scalp on the same symbol). Kept behind the
        one_position_per_symbol=false config toggle."""
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
                exit_reason = self._exit_reason_for_position(primary, bundle=bundle)
                if exit_reason is not None:
                    await self._close_position(
                        position=primary,
                        bundle=bundle,
                        now=now,
                        reason=exit_reason,
                        execution=None,
                    )
                    open_positions.remove(primary)
                    closed_positions.append(primary)
                continue

            if primary.get("signal_action") == decision.action and _same_contract(primary, execution):
                await self._refresh_open_position(
                    position=primary,
                    bundle=bundle,
                    decision=decision,
                    now=now,
                    execution=execution,
                )
                exit_reason = self._exit_reason_for_position(primary, bundle=bundle)
                if exit_reason is not None:
                    await self._close_position(
                        position=primary,
                        bundle=bundle,
                        now=now,
                        reason=exit_reason,
                        execution=execution,
                    )
                    open_positions.remove(primary)
                    closed_positions.append(primary)
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
        if position.get("stop_price") is None and decision.stop_price is not None:
            position["stop_price"] = decision.stop_price
        if position.get("target_price") is None and decision.target_price is not None:
            position["target_price"] = decision.target_price
        position["execution_style"] = getattr(execution, "style", None) or position.get("execution_style")
        entry_premium = float(position.get("entry_premium") or latest_premium or 0.0)
        quantity = int(position.get("quantity") or 0)
        position["unrealized_pnl"] = round((latest_premium - entry_premium) * quantity, 2)

    def _exit_reason_for_position(
        self,
        position: dict[str, Any],
        *,
        bundle: AnalysisBundle,
    ) -> Optional[str]:
        session_date = self._session_date(bundle)
        expiry = self._position_expiry(position)
        if expiry is not None and session_date is not None and expiry < session_date:
            return "expired_contract"

        latest_premium = _as_float(position.get("latest_premium"))
        if latest_premium is not None and latest_premium <= 0:
            return "premium_zero"

        # Hard stop on PREMIUM drawdown (symmetric for CE & PE — both are option BUYS,
        # so a falling premium is the loss either way). Capital at risk per position is
        # capped to this fraction of the premium paid.
        hard_stop_fraction = float(self.limits.get("hard_stop_premium_fraction", 0.25))
        entry_premium = _as_float(position.get("entry_premium"))
        if (
            hard_stop_fraction > 0
            and entry_premium is not None
            and entry_premium > 0
            and latest_premium is not None
            and latest_premium <= entry_premium * (1.0 - hard_stop_fraction)
        ):
            return "hard_stop"

        latest_spot = _as_float(position.get("latest_spot_price"))
        action = str(position.get("signal_action") or "").upper()
        stop = _as_float(position.get("stop_price"))
        target = _as_float(position.get("target_price"))
        premium_based = self._risk_levels_are_premium_based(position, stop=stop, target=target)
        latest_value = latest_premium if premium_based else latest_spot
        if latest_value is None:
            return None
        if action == "LONG":
            if stop is not None and latest_value <= stop:
                return "stop_loss"
            if target is not None and latest_value >= target:
                return "target_hit"
        elif action == "SHORT":
            if stop is not None and latest_value >= stop:
                return "stop_loss"
            if target is not None and latest_value <= target:
                return "target_hit"
        return None

    @staticmethod
    def _risk_levels_are_premium_based(
        position: dict[str, Any],
        *,
        stop: Optional[float],
        target: Optional[float],
    ) -> bool:
        levels = [value for value in (stop, target) if value is not None and value > 0]
        if not levels:
            return False
        entry_premium = _as_float(position.get("entry_premium"))
        entry_spot = _as_float(position.get("entry_spot_price"))
        if entry_premium is None or entry_premium <= 0 or entry_spot is None or entry_spot <= 0:
            return False
        return max(levels) <= max(entry_premium * 5.0, entry_spot * 0.25)

    @staticmethod
    def _session_date(bundle: AnalysisBundle) -> Optional[date]:
        raw = getattr(bundle.market_profile, "session_date", None)
        if isinstance(raw, date):
            return raw
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                return None
        return None

    @staticmethod
    def _position_expiry(position: dict[str, Any]) -> Optional[date]:
        raw = position.get("expiry")
        if isinstance(raw, date):
            return raw
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                return None
        return None

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
        try:
            await paper_trade_recorder.record_event(
                strategy="auction_intelligence",
                event="close",
                underlying=position.get("underlying_symbol"),
                instrument_key=position.get("instrument_key"),
                option_type=position.get("option_type"),
                strike=position.get("strike"),
                expiry=str(position.get("expiry") or ""),
                quantity=quantity,
                entry_premium=entry_premium,
                exit_premium=exit_premium,
                realized=position["realized_pnl"],
                position_id=position.get("position_id"),
                reason=reason,
            )
        except Exception:
            pass

    def _open_position(
        self,
        *,
        bundle: AnalysisBundle,
        decision: Any,
        execution: Any,
        now: str,
        underlying: str,
    ) -> dict[str, Any]:
        record_dict = self._build_open_position(
            bundle=bundle, decision=decision, execution=execution, now=now, underlying=underlying
        )
        try:
            asyncio.create_task(
                paper_trade_recorder.record_event(
                    strategy="auction_intelligence",
                    event="open",
                    underlying=record_dict.get("underlying_symbol"),
                    instrument_key=record_dict.get("instrument_key"),
                    option_type=record_dict.get("option_type"),
                    strike=record_dict.get("strike"),
                    expiry=str(record_dict.get("expiry") or ""),
                    quantity=int(record_dict.get("quantity") or 0),
                    entry_premium=record_dict.get("entry_premium"),
                    latest_premium=record_dict.get("latest_premium"),
                    position_id=record_dict.get("position_id"),
                    reason=str(record_dict.get("selection_reason") or ""),
                    extra={"agent": record_dict.get("agent_name"), "action": record_dict.get("signal_action")},
                )
            )
        except RuntimeError:
            pass
        return record_dict

    def _clamp_quantity_to_symbol_cap(
        self,
        quantity: int,
        premium: float,
        lot_size: Any,
    ) -> int:
        """Cap a new position's size so its PREMIUM OUTGO (qty x premium — the real
        capital at risk for an option buy) stays within ``max_symbol_capital_fraction``
        of the account, floored to whole lots."""
        fraction = float(self.limits.get("max_symbol_capital_fraction", 0.0) or 0.0)
        if fraction <= 0 or quantity <= 0 or premium is None or premium <= 0:
            return quantity
        cap_capital = fraction * AI_INITIAL_CAPITAL
        max_qty = int(cap_capital // premium)
        try:
            lot = int(lot_size or 0)
        except (TypeError, ValueError):
            lot = 0
        if lot > 0:
            max_qty = (max_qty // lot) * lot  # floor to whole lots
        if max_qty <= 0:
            # One lot already exceeds the cap — keep a single lot rather than skip,
            # so the symbol can still be traded (only relevant at very small capital).
            return lot if lot > 0 else 0
        return min(quantity, max_qty)

    def _build_open_position(
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
        raw_quantity = int(getattr(execution, "quantity", None) or decision.quantity or 0)
        lot_size = getattr(execution, "lot_size", None)
        quantity = self._clamp_quantity_to_symbol_cap(raw_quantity, entry_premium, lot_size)
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
            quantity=quantity,
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
                    allow_broker_refresh=not (settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY),
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

        # Capital accounting — turns AI from "PnL ticker" into a funded
        # paper-trading lane matching S1/S2/Commodity/FMP (all ₹10L).
        # Premium × quantity is the cash locked against each open option.
        initial_capital = AI_INITIAL_CAPITAL
        reserved_margin = round(
            sum(
                float(p.get("entry_premium") or 0.0) * float(p.get("quantity") or 0)
                for p in open_positions
            ),
            2,
        )
        total_equity = round(initial_capital + realized + unrealized, 2)
        available_capital = round(initial_capital + realized - reserved_margin, 2)
        total_return_pct = round(
            ((total_equity - initial_capital) / initial_capital) * 100.0, 4
        ) if initial_capital else 0.0

        # Equity curve walk over closed trades — simple deterministic drawdown
        # without any snapshot loop. Per-trade returns drive a rough Sharpe.
        closed_sorted = sorted(
            closed_positions,
            key=lambda r: str(r.get("closed_at") or r.get("updated_at") or ""),
        )
        running_equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        trade_returns_pct: list[float] = []
        wins = 0
        losses = 0
        for row in closed_sorted:
            pnl = float(row.get("realized_pnl") or 0.0)
            pre_equity = running_equity if running_equity > 0 else initial_capital
            running_equity = max(0.0, running_equity + pnl)
            if running_equity > peak:
                peak = running_equity
            if peak > 0:
                dd = (peak - running_equity) / peak
                max_dd = max(max_dd, dd)
            if pre_equity > 0:
                trade_returns_pct.append((pnl / pre_equity) * 100.0)
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        sharpe = 0.0
        if len(trade_returns_pct) >= 2:
            mean = sum(trade_returns_pct) / len(trade_returns_pct)
            var = sum((r - mean) ** 2 for r in trade_returns_pct) / max(
                len(trade_returns_pct) - 1, 1
            )
            stdev = var ** 0.5
            if stdev > 0:
                sharpe = round(mean / stdev, 4)

        win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0

        return {
            # legacy fields (kept for backward compatibility)
            "symbol_filter": symbol or None,
            "open_count": len(open_positions),
            "closed_count": len(closed_positions),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "latest_opened_at": open_positions[0].get("opened_at") if open_positions else None,
            "latest_closed_at": closed_positions[0].get("closed_at") if closed_positions else None,
            "last_synced_at": state.get("last_synced_at"),
            # new capital fields — mirror S1/S2/Commodity/FMP
            "initial_capital": initial_capital,
            "available_capital": available_capital,
            "reserved_margin": reserved_margin,
            "total_equity": total_equity,
            "total_return_pct": total_return_pct,
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": sharpe,
            "total_trades": wins + losses,
            "win_rate": round(win_rate, 4),
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
