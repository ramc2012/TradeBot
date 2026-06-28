"""Paper journal and position book for directional live snapshots."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy import text

from analysis.instruments import normalize_index_contract_expiry
from core.paper_trade_recorder import paper_trade_recorder
from core.trading_calendar import trading_calendar
from db.database import AsyncSessionLocal
from directional_options.config import DEFAULT_CONFIG, DIRECTIONAL_INITIAL_CAPITAL
from directional_options.exits import evaluate_exit
from directional_options.policy import DirectionalPolicy


_TIMEFRAME_MINUTES = {
    "1minute": 1, "1m": 1,
    "3minute": 3, "3m": 3,
    "5minute": 5, "5m": 5,
    "15minute": 15, "15m": 15,
    "30minute": 30, "30m": 30,
    "60minute": 60, "1hour": 60, "1h": 60,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _parse_date_only(value: Any) -> date | None:
    ts = _parse_iso(value)
    return ts.date() if ts is not None else None


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_dict(payload: Any) -> dict[str, Any]:
    """JSONB columns come back as a dict (asyncpg) or a JSON string — normalise."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return {}


def _has_satisfied_min_hold(
    position: dict[str, Any],
    *,
    min_hold_bars: int,
    timeframe: str | None,
) -> bool:
    if min_hold_bars <= 0:
        return True
    opened = _parse_iso(position.get("opened_at"))
    if opened is None:
        return True
    minutes_per_bar = _TIMEFRAME_MINUTES.get(str(timeframe or "").lower(), 5)
    elapsed = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
    return elapsed >= float(min_hold_bars * minutes_per_bar)


def _normalize_symbol(value: str | None) -> str:
    return str(value or "").upper().strip()


def _with_current_expiry_calendar(position: dict[str, Any]) -> dict[str, Any]:
    row = dict(position)
    raw_expiry = row.get("expiry")
    normalized_expiry = normalize_index_contract_expiry(row.get("underlying"), raw_expiry)
    if normalized_expiry is None or normalized_expiry.isoformat() == str(raw_expiry or "")[:10]:
        return row

    row["raw_expiry"] = raw_expiry
    row["expiry"] = normalized_expiry.isoformat()
    row["expiry_kind"] = "monthly"
    row["expiry_correction_reason"] = "current_index_monthly_expiry_calendar"
    return row


def _same_contract(position: dict[str, Any], contract: dict[str, Any]) -> bool:
    position_key = str(position.get("instrument_key") or "").strip()
    contract_key = str(contract.get("instrument_key") or "").strip()
    if position_key and contract_key:
        return position_key == contract_key
    position_symbol = str(position.get("trading_symbol") or "").strip()
    contract_symbol = str(contract.get("trading_symbol") or "").strip()
    if position_symbol and contract_symbol:
        return position_symbol == contract_symbol
    return (
        str(position.get("option_type") or "") == str(contract.get("option_type") or "")
        and str(position.get("expiry") or "") == str(contract.get("expiry") or "")
        and float(position.get("strike") or 0.0) == float(contract.get("strike") or 0.0)
    )


class DirectionalOptionsPaperStore:
    # How long the "actionable=False" signal must persist before a flat_signal
    # close fires. The directional strategy re-evaluates every ~60 s; without
    # this, a single noisy cycle (brief spread widening / data gap / regime
    # uncertainty) churns the position. Five minutes of confirmation prevents
    # ~90 % churn observed on 2026-05-20 (71 opens / 64 closes in a session).
    FLAT_CONFIRMATION_SECONDS: float = 300.0

    def __init__(
        self,
        root: Path | str,
        *,
        min_hold_bars: int = 3,
        one_position_per_symbol: bool = True,
        policy: DirectionalPolicy | None = None,
        exit_config: dict[str, Any] | None = None,
        cost_config: dict[str, Any] | None = None,
        positional: dict[str, Any] | None = None,
    ):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        # Exit thresholds (stop / target / trail / expiry-guard) and the
        # spread+slippage cost model. Default to the package config so the live
        # book and the backtest stay in lockstep, but allow the service to pass
        # its own resolved config.
        self.exit_config = dict(exit_config or DEFAULT_CONFIG["risk"])
        self.cost_config = dict(cost_config or DEFAULT_CONFIG["execution"])
        # Legacy file paths — retained only to seed the DB once (one-time
        # import) and for reset_account archival. The book now lives in
        # directional_paper_positions / directional_paper_journal.
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self._lock = asyncio.Lock()
        self._db_seeded = False  # one-time file→DB import guard
        self.min_hold_bars = int(min_hold_bars)
        self.one_position_per_symbol = bool(one_position_per_symbol)
        self.policy = policy
        # 1-2 day positional tunables (None → legacy 5-min intraday behaviour).
        # Set only when settings.DIRECTIONAL_POSITIONAL_MODE_ENABLED is on; the
        # service passes config['positional'] in that case. Gates the
        # session-clock held-time, confirmed-flip, and ATR adaptive exits.
        self.positional = dict(positional) if positional else None

    async def list_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        records = await self._load_journal()
        normalized = _normalize_symbol(symbol)
        if normalized:
            records = [row for row in records if _normalize_symbol(row.get("underlying")) == normalized]
        records.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
        return {
            "symbol_filter": normalized or None,
            "count": len(records),
            "records": records[:limit],
        }

    async def list_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        state = await self._load_positions()
        normalized = _normalize_symbol(symbol)
        open_positions = [_with_current_expiry_calendar(row) for row in state.get("open_positions", [])]
        closed_positions = [_with_current_expiry_calendar(row) for row in state.get("closed_positions", [])]
        if normalized:
            open_positions = [row for row in open_positions if _normalize_symbol(row.get("underlying")) == normalized]
            closed_positions = [row for row in closed_positions if _normalize_symbol(row.get("underlying")) == normalized]
        open_positions.sort(key=lambda row: str(row.get("opened_at") or ""), reverse=True)
        closed_positions.sort(key=lambda row: str(row.get("closed_at") or row.get("updated_at") or ""), reverse=True)
        if status == "open":
            closed_positions = []
        elif status == "closed":
            open_positions = []

        # Live-mark overlay (Bug B, 2026-06-04): a held option's specific
        # contract often isn't on the WS premium feed, so its stored
        # latest_premium freezes at entry (a SENSEX CE sat at its 1423.4 entry
        # with uPnL=0 indefinitely). The chain poll keeps EVERY strike's LTP
        # fresh (~30s) — overlay it here so the served P/L streams. Directional
        # is long-premium, so uPnL = (mark − entry) × units. Best-effort; also
        # ensures the position's expiry chain is tracked so the cache has data.
        if open_positions:
            try:
                from directional_options.chain_analytics import (
                    chain_strike_mark,
                    ensure_chain_tracked,
                )
                for row in open_positions:
                    try:
                        und = str(row.get("underlying") or "")
                        exp = str(row.get("expiry") or "")
                        strike = float(row.get("strike") or 0.0)
                        otype = str(row.get("option_type") or "")
                        if not (und and exp and strike and otype):
                            continue
                        await ensure_chain_tracked(und, exp)
                        mark = await chain_strike_mark(und, exp, strike, otype)
                        if mark is not None and mark > 0:
                            qty = float(row.get("quantity_units") or 0)
                            entry = float(row.get("entry_premium") or 0.0)
                            row["latest_premium"] = round(mark, 2)
                            row["unrealized_pnl"] = round((mark - entry) * qty, 2)
                            row["mark_time"] = _utc_now()
                            row["price_source"] = "chain_cache_live"
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                pass

        return {
            "symbol_filter": normalized or None,
            "status": status,
            "summary": await self._summary(open_positions, closed_positions),
            "open_positions": open_positions[:limit],
            "closed_positions": closed_positions[:limit],
        }

    async def sync_snapshot(
        self,
        snapshot_payload: dict[str, Any],
        *,
        position_marks: dict[str, dict[str, Any]] | None = None,
        allow_entries: bool = True,
    ) -> dict[str, Any]:
        # allow_entries gates ONLY the opening of NEW positions — existing
        # positions are still marked + managed (mark-to-market, two-stage
        # close). The caller passes False outside the session so an after-hours
        # API poll / request can't open a position on the frozen post-close
        # heartbeat (a SENSEX CE opened 15:45 IST on 2026-06-03 this way).
        snapshot = dict(snapshot_payload.get("snapshot") or {})
        selection = dict(snapshot_payload.get("selection") or {})
        underlying = _normalize_symbol(selection.get("underlying") or snapshot.get("underlying"))
        signal = dict(snapshot.get("signal") or {})
        contract = dict(snapshot.get("selected_contract") or {})
        risk = dict(snapshot.get("risk") or {})
        data_status = dict(snapshot.get("data_status") or {})
        rag_context = dict(snapshot.get("rag_context") or {})
        recorded_at = str(snapshot.get("as_of") or _utc_now())
        execution_ready = bool(data_status.get("execution_ready"))
        actionable = bool(signal and contract and risk.get("approved") and execution_ready)
        latest_spot = float(snapshot.get("spot_price") or 0.0)
        latest_mark = float(contract.get("option_price") or 0.0) if contract else 0.0

        journal_entry = {
            "recorded_at": recorded_at,
            "underlying": underlying,
            "timeframe": selection.get("timeframe"),
            "regime": (snapshot.get("regime") or {}).get("label"),
            "direction": signal.get("direction"),
            "confidence": signal.get("confidence"),
            "expected_move": signal.get("expected_move"),
            "expected_horizon_bars": signal.get("expected_horizon_bars"),
            "selection_reason": snapshot.get("selection_reason"),
            "approved": bool(risk.get("approved")),
            "execution_ready": execution_ready,
            "trading_symbol": contract.get("trading_symbol"),
            "instrument_key": contract.get("instrument_key"),
            "option_type": contract.get("option_type"),
            "expiry": contract.get("expiry"),
            "strike": contract.get("strike"),
            "latest_premium": latest_mark or None,
            "latest_spot": latest_spot,
            "expected_pnl": contract.get("expected_pnl"),
            "rag_context": rag_context,
            "data_status": data_status,
        }
        await self._append_journal(journal_entry)

        async with self._lock:
            state = await self._load_positions()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))
            matching = [row for row in open_positions if _normalize_symbol(row.get("underlying")) == underlying]
            marks = position_marks or {}

            for row in matching:
                mark = marks.get(str(row.get("position_id") or "")) or {}
                if mark:
                    latest_value = float(mark.get("premium") or row.get("latest_premium") or row.get("entry_premium") or 0.0)
                    latest_spot_value = float(mark.get("spot") or row.get("latest_spot") or row.get("entry_spot") or 0.0)
                    row["updated_at"] = recorded_at
                    row["latest_premium"] = latest_value
                    row["latest_spot"] = latest_spot_value
                    row["price_source"] = mark.get("price_source") or row.get("price_source")
                    row["mark_time"] = mark.get("mark_time") or row.get("mark_time")
                    entry_premium = float(row.get("entry_premium") or latest_value or 0.0)
                    quantity = int(row.get("quantity_units") or 0)
                    row["unrealized_pnl"] = round((latest_value - entry_premium) * quantity, 2)
                    prev_peak = float(row.get("peak_premium") or row.get("entry_premium") or latest_value or 0.0)
                    row["peak_premium"] = round(max(prev_peak, latest_value), 2)

            # Protective exit ladder — premium stop / underlying invalidation /
            # target / trailing take-profit / expiry guard / time stop — enforced
            # on EVERY cycle that has a fresh mark, independent of whether a new
            # actionable signal exists or entries are allowed. This is the same
            # ladder the backtest runs (directional_options.exits). Without it a
            # long option had no stop and could bleed to zero, held only until
            # the signal went flat or flipped. Stops never fire on stale data:
            # we require a fresh mark this cycle and never fabricate a price.
            now_dt = _parse_iso(recorded_at) or datetime.now(timezone.utc)
            exited_rows: list[dict[str, Any]] = []
            for row in list(matching):
                mark = marks.get(str(row.get("position_id") or "")) or {}
                current_premium = _safe_float_or_none(mark.get("premium"))
                if (current_premium is None or current_premium <= 0) and mark:
                    current_premium = _safe_float_or_none(row.get("latest_premium"))
                if not mark or current_premium is None or current_premium <= 0:
                    continue
                reason = self._evaluate_position_exit(row, current_premium=current_premium, now_dt=now_dt)
                if reason and self._close_position(
                    row, mark=mark, close_time=recorded_at, close_reason=reason
                ):
                    exited_rows.append(row)
            for row in exited_rows:
                if row in open_positions:
                    open_positions.remove(row)
                if row in matching:
                    matching.remove(row)
                closed_positions.append(row)

            if not execution_ready:
                await self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return await self._summary(open_positions, closed_positions)

            if not actionable:
                # Two-stage close: require persistent flat signal before
                # exiting. The strategy re-evaluates every ~60 s and any
                # one of (signal/contract/risk/exec_ready) flipping to
                # False sets actionable=False — but a single noisy cycle
                # shouldn't churn a held position. Track when each row
                # last saw an actionable=True cycle; only fire flat_signal
                # close after FLAT_CONFIRMATION_SECONDS of sustained flat.
                now_dt = datetime.now(timezone.utc)
                position_timeframe = str(selection.get("timeframe") or "")
                for row in list(matching):
                    if not _has_satisfied_min_hold(
                        row,
                        min_hold_bars=self.min_hold_bars,
                        timeframe=row.get("timeframe") or position_timeframe,
                    ):
                        # Held too briefly — refuse to flatten on a single
                        # noisy bar. Keep the position open; it will close
                        # naturally on stop / target / a later flat-signal.
                        continue
                    last_actionable_iso = row.get("last_actionable_at") or row.get("opened_at")
                    last_actionable_dt = _parse_iso(last_actionable_iso)
                    if last_actionable_dt is not None:
                        flat_for = (now_dt - last_actionable_dt).total_seconds()
                        if flat_for < self.FLAT_CONFIRMATION_SECONDS:
                            # Position recently green-lit; brief flat is
                            # noise. Don't churn the position.
                            continue
                    if self._close_position(
                        row,
                        mark=marks.get(str(row.get("position_id") or "")) or {},
                        close_time=recorded_at,
                        close_reason="flat_signal",
                    ):
                        open_positions.remove(row)
                        closed_positions.append(row)
                await self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return await self._summary(open_positions, closed_positions)

            refreshed = False
            position_timeframe = str(selection.get("timeframe") or "")
            for row in list(matching):
                if _same_contract(row, contract) and str(row.get("direction") or "") == str(signal.get("direction") or ""):
                    row["updated_at"] = recorded_at
                    # Mark this cycle as green-lit so the flat-signal
                    # confirmation timer above resets every time we get a
                    # fresh actionable=True for the same contract.
                    row["last_actionable_at"] = recorded_at
                    row["latest_premium"] = latest_mark
                    row["latest_spot"] = latest_spot
                    row["confidence"] = float(signal.get("confidence") or row.get("confidence") or 0.0)
                    row["expected_move"] = float(signal.get("expected_move") or row.get("expected_move") or 0.0)
                    row["regime"] = (snapshot.get("regime") or {}).get("label") or row.get("regime")
                    row["selection_reason"] = snapshot.get("selection_reason") or row.get("selection_reason")
                    entry_premium = float(row.get("entry_premium") or latest_mark or 0.0)
                    quantity = int(row.get("quantity_units") or 0)
                    row["unrealized_pnl"] = round((latest_mark - entry_premium) * quantity, 2)
                    refreshed = True
                    continue
                if not _has_satisfied_min_hold(
                    row,
                    min_hold_bars=self.min_hold_bars,
                    timeframe=row.get("timeframe") or position_timeframe,
                ):
                    # Held too briefly — keep position open through a single
                    # noisy regime flip. Will reassess on the next bar.
                    continue
                if self._close_position(
                    row,
                    mark=marks.get(str(row.get("position_id") or "")) or {},
                    close_time=recorded_at,
                    close_reason="signal_flip",
                ):
                    open_positions.remove(row)
                    closed_positions.append(row)

            # Hard one-position-per-symbol guard. If the existing position
            # on this symbol was REFRESHED above (same contract+direction)
            # we never get here. Otherwise: if there is still ANY open
            # position on this underlying after the signal-flip pass (e.g.
            # min_hold not yet satisfied), suppress the new open.
            symbol_already_open = any(
                _normalize_symbol(row.get("underlying")) == underlying
                and str(row.get("status") or "open") == "open"
                for row in open_positions
            )
            if self.one_position_per_symbol and symbol_already_open and not refreshed:
                await self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return await self._summary(open_positions, closed_positions)

            if not refreshed and allow_entries:
                new_position_id = uuid4().hex
                try:
                    await paper_trade_recorder.record_event(
                        strategy="directional_options",
                        event="open",
                        underlying=underlying,
                        instrument_key=contract.get("instrument_key"),
                        option_type=contract.get("option_type"),
                        strike=float(contract.get("strike") or 0.0),
                        expiry=str(contract.get("expiry") or ""),
                        quantity=int(risk.get("quantity_units") or 0),
                        entry_premium=latest_mark,
                        latest_premium=latest_mark,
                        position_id=new_position_id,
                        reason=str(snapshot.get("selection_reason") or ""),
                        extra={"direction": signal.get("direction"), "timeframe": selection.get("timeframe")},
                    )
                except Exception:
                    pass
                # Register the open with the RL policy so the realised
                # R-multiple flows back into the value posterior on close.
                # Chain analytics captured at ENTRY — that's the context
                # the policy will be credited/debited against, not the
                # chain state at close time.
                policy_payload = dict(snapshot.get("policy") or {})
                size_multiplier = float(policy_payload.get("size_multiplier") or 1.0)
                ai_model_payload = dict(contract.get("ai_model") or {})
                if self.policy is not None:
                    try:
                        self.policy.register_open(
                            position_id=new_position_id,
                            signal=signal,
                            candidate=contract,
                            regime=dict(snapshot.get("regime") or {}),
                            size_multiplier=size_multiplier,
                            risk_budget=float(risk.get("risk_budget") or 0.0),
                            chain=dict(snapshot.get("chain_analytics") or {}) or None,
                        )
                    except Exception:
                        pass
                open_positions.append(
                    {
                        "position_id": new_position_id,
                        "status": "open",
                        "opened_at": recorded_at,
                        "updated_at": recorded_at,
                        # Anchor for the flat-signal confirmation timer
                        # (FLAT_CONFIRMATION_SECONDS). Initialized to open
                        # time so the position has a full grace window
                        # before any single noisy cycle can close it.
                        "last_actionable_at": recorded_at,
                        "closed_at": None,
                        "underlying": underlying,
                        "timeframe": selection.get("timeframe"),
                        "direction": signal.get("direction"),
                        "regime": (snapshot.get("regime") or {}).get("label"),
                        "confidence": float(signal.get("confidence") or 0.0),
                        "expected_move": float(signal.get("expected_move") or 0.0),
                        "trading_symbol": contract.get("trading_symbol"),
                        "instrument_key": contract.get("instrument_key"),
                        "option_type": contract.get("option_type"),
                        "expiry": contract.get("expiry"),
                        "expiry_kind": contract.get("expiry_kind"),
                        "strike": float(contract.get("strike") or 0.0),
                        "quantity_lots": int(risk.get("quantity_lots") or 0),
                        "quantity_units": int(risk.get("quantity_units") or 0),
                        "risk_budget": float(risk.get("risk_budget") or 0.0),
                        "max_loss": float(risk.get("max_loss") or 0.0),
                        "premium_at_risk": float(risk.get("premium_at_risk") or 0.0),
                        "premium_cap": risk.get("premium_cap"),
                        # Exit-ladder state (mirrors backtest PositionState) so
                        # the shared exit rules (directional_options.exits) can
                        # protect this position: trailing peak, expected horizon,
                        # underlying-invalidation level, and the candidate's
                        # liquidity for the spread/slippage cost model on close.
                        "peak_premium": latest_mark,
                        "expected_horizon_bars": int(signal.get("expected_horizon_bars") or 0),
                        "stop_underlying": (
                            (latest_spot - float(signal.get("expected_move") or 0.0) * 0.55)
                            if str(signal.get("direction") or contract.get("option_type") or "CE") == "CE"
                            else (latest_spot + float(signal.get("expected_move") or 0.0) * 0.55)
                        ),
                        "spread_pct": float(contract.get("spread_pct") or 0.0),
                        "slippage_pct": float(contract.get("slippage_pct") or 0.0),
                        "entry_premium": latest_mark,
                        "latest_premium": latest_mark,
                        "exit_premium": None,
                        "entry_spot": latest_spot,
                        "latest_spot": latest_spot,
                        "exit_spot": None,
                        "unrealized_pnl": 0.0,
                        "realized_pnl": 0.0,
                        "expected_pnl": float(contract.get("expected_pnl") or 0.0),
                        "selection_reason": snapshot.get("selection_reason"),
                        "rag_context": rag_context,
                        "price_source": contract.get("price_source") or "local_watchlist",
                        "mark_time": recorded_at,
                        "policy_size_multiplier": size_multiplier,
                        "policy_sampled_value": policy_payload.get("sampled_value"),
                        "policy_n_seen_at_open": policy_payload.get("n_seen"),
                        "ai_rule_score": ai_model_payload.get("score"),
                        "ai_rule_setup": ai_model_payload.get("setup"),
                        "ai_rule_blockers": ai_model_payload.get("blockers") or [],
                    }
                )

            await self._save_positions(
                {
                    "last_synced_at": recorded_at,
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                }
            )
            return await self._summary(open_positions, closed_positions)

    async def close_position(
        self,
        position_id: str,
        *,
        premium: float | None = None,
        spot: float | None = None,
        reason: str = "operator_close",
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Manually close one open paper position and feed RL reward.

        The store remains the single close path so operator exits, signal
        flips, and flat-signal exits all book costs and train the policy in
        the same way.
        """
        normalized_id = str(position_id or "").strip()
        if not normalized_id:
            raise ValueError("position_id is required")

        close_time = _utc_now()
        async with self._lock:
            state = await self._load_positions()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))
            position = next(
                (
                    row for row in open_positions
                    if str(row.get("position_id") or "") == normalized_id
                    and str(row.get("status") or "open") == "open"
                ),
                None,
            )
            if position is None:
                raise ValueError(f"Open directional paper position not found: {normalized_id}")

            mark_premium = premium
            price_source = "operator_mark"
            if mark_premium is None:
                try:
                    from directional_options.chain_analytics import chain_strike_mark, ensure_chain_tracked

                    underlying = str(position.get("underlying") or "")
                    expiry = str(position.get("expiry") or "")
                    strike = float(position.get("strike") or 0.0)
                    option_type = str(position.get("option_type") or "")
                    if underlying and expiry and strike and option_type:
                        await ensure_chain_tracked(underlying, expiry)
                        chain_mark = await chain_strike_mark(underlying, expiry, strike, option_type)
                        if chain_mark is not None and chain_mark > 0:
                            mark_premium = float(chain_mark)
                            price_source = "chain_cache_live"
                except Exception:  # noqa: BLE001
                    mark_premium = None

            # Do NOT fabricate a price from the entry premium here — that booked
            # a ₹0 breakeven. When no live price is available, _close_position
            # (force=True) settles at intrinsic if the option has expired, else
            # at the last-known price flagged data_missing (excluded from RL).
            mark: dict[str, Any] = {
                "spot": float(
                    spot
                    if spot is not None
                    else position.get("latest_spot")
                    or position.get("entry_spot")
                    or 0.0
                ),
                "mark_time": close_time,
            }
            if mark_premium is not None and float(mark_premium) > 0:
                mark["premium"] = float(mark_premium)
                mark["price_source"] = price_source

            close_reason = (reason or "operator_close").strip() or "operator_close"
            self._close_position(
                position,
                mark=mark,
                close_time=close_time,
                close_reason=close_reason,
                force=True,
            )
            position["operator_actor"] = actor
            position["operator_closed"] = True
            open_positions.remove(position)
            closed_positions.append(position)

            await self._append_journal(
                {
                    "recorded_at": close_time,
                    "underlying": position.get("underlying"),
                    "timeframe": position.get("timeframe"),
                    "direction": position.get("direction"),
                    "approved": False,
                    "execution_ready": True,
                    "trading_symbol": position.get("trading_symbol"),
                    "instrument_key": position.get("instrument_key"),
                    "option_type": position.get("option_type"),
                    "expiry": position.get("expiry"),
                    "strike": position.get("strike"),
                    "latest_premium": mark["premium"],
                    "latest_spot": mark["spot"],
                    "selection_reason": close_reason,
                    "operator_actor": actor,
                    "event": "manual_close",
                    "position_id": normalized_id,
                    "realized_pnl": position.get("realized_pnl"),
                    "policy_r_multiple": position.get("policy_r_multiple"),
                }
            )
            await self._save_positions(
                {
                    "last_synced_at": close_time,
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                }
            )
            summary = await self._summary(open_positions, closed_positions)

        return {
            "closed": True,
            "position": position,
            "summary": summary,
        }

    def _exit_thresholds(self) -> dict[str, float]:
        ec = self.exit_config
        _r = DEFAULT_CONFIG["risk"]
        return {
            "planned_stop_pct": float(ec.get("planned_stop_pct", _r["planned_stop_pct"])),
            "profit_target_pct": float(ec.get("profit_target_pct", _r["profit_target_pct"])),
            "trail_giveback_pct": float(ec.get("trail_giveback_pct", _r["trail_giveback_pct"])),
            "expiry_guard_days": float(ec.get("expiry_guard_days", _r["expiry_guard_days"])),
        }

    def _per_side_cost_pct(self, position: dict[str, Any]) -> float:
        """Per-side spread+slippage fraction of premium, mirroring the backtest
        (spread_pct/2 + slippage_pct). Uses the candidate's liquidity-derived
        figures stored at open; falls back to the execution config for legacy
        positions that predate this field."""
        spread_pct = _safe_float_or_none(position.get("spread_pct"))
        slippage_pct = _safe_float_or_none(position.get("slippage_pct"))
        if spread_pct is not None and spread_pct > 0:
            sl = slippage_pct if (slippage_pct is not None and slippage_pct >= 0) else spread_pct * 0.28
            return max(0.0, spread_pct / 2.0 + sl)
        cc = self.cost_config
        fb_spread = float(cc.get("fallback_spread_pct", 0.02))
        fb_slip = (
            float(cc.get("entry_slippage_pct", 0.0075)) + float(cc.get("exit_slippage_pct", 0.006))
        ) / 2.0
        return max(0.0, fb_spread / 2.0 + fb_slip)

    def _evaluate_position_exit(
        self, position: dict[str, Any], *, current_premium: float, now_dt: datetime
    ) -> str | None:
        """Run the shared exit ladder against a live position dict."""
        entry_premium = _safe_float_or_none(position.get("entry_premium")) or 0.0
        if entry_premium <= 0:
            return None
        peak = _safe_float_or_none(position.get("peak_premium")) or entry_premium
        current_spot = (
            _safe_float_or_none(position.get("latest_spot"))
            or _safe_float_or_none(position.get("entry_spot"))
            or 0.0
        )
        stop_underlying = _safe_float_or_none(position.get("stop_underlying"))
        opened = _parse_iso(position.get("opened_at"))
        tf_min = _TIMEFRAME_MINUTES.get(str(position.get("timeframe") or ""), 5)
        held_bars = 0
        if opened is not None:
            if self.positional:
                # 1-2 day hold: count only TRADING-SESSION minutes so the
                # overnight / weekend / holiday gap can't inflate the horizon and
                # force a spurious close at the next session open. (now_dt is
                # UTC; the calendar normalises both ends to IST.)
                session_min = trading_calendar.trading_minutes_between("NSE", opened, now_dt)
                held_bars = max(0, int(session_min // max(tf_min, 1)))
            else:
                held_bars = max(0, int((now_dt - opened).total_seconds() // 60 // max(tf_min, 1)))
        max_horizon_bars = _safe_int_or_none(position.get("expected_horizon_bars")) or 0
        expiry_d = _parse_date_only(position.get("expiry"))
        expiry_days_left = max((expiry_d - now_dt.date()).days, 0) if expiry_d is not None else None
        return evaluate_exit(
            option_type=str(position.get("option_type") or "CE"),
            current_premium=float(current_premium),
            entry_basis_premium=entry_premium,
            return_basis_premium=entry_premium,
            peak_premium=peak,
            current_spot=current_spot,
            stop_underlying=stop_underlying,
            expiry_days_left=expiry_days_left,
            held_bars=held_bars,
            max_horizon_bars=max_horizon_bars,
            **self._exit_thresholds(),
        )

    def _mark_is_fresh_for_close(
        self, position: dict[str, Any], mark: dict[str, Any], *, trust_live_source: bool
    ) -> bool:
        """Is this mark a genuinely tradeable post-entry price?

        A nonzero premium is only a valid close if its mark is stamped AT OR
        AFTER the position opened AND within the staleness window of now. A stale
        watchlist LTP stamped *before* opened_at (the watchlist writer fell
        behind) fabricates a P&L on a price that never traded after entry — which
        booked ~₹59k of phantom slippage across 80/115 closes and, worse, trained
        the RL value posterior on losses that never economically occurred.
        Current-cycle live-cache / operator marks are fresh by construction.
        """
        if trust_live_source and str((mark or {}).get("price_source") or "") in {
            "chain_cache_live", "operator_mark", "live_mark"
        }:
            return True
        mark_time = _parse_iso((mark or {}).get("mark_time"))
        if mark_time is None:
            return False  # unknown age → not trustworthy for a tradeable close
        opened = _parse_iso(position.get("opened_at"))
        # The load-bearing invariant: a close price must be stamped AT OR AFTER
        # entry. All 80/115 fabricated closes had mark_time strictly < opened_at
        # (the watchlist writer had fallen behind, so the "latest" row predated
        # the position). Freshness-vs-now is owned upstream by service.py, which
        # swaps a stale/pre-entry watchlist mark for the live chain cache before
        # it ever reaches here — so this gate only enforces the post-entry rule.
        if opened is not None and mark_time < opened:
            return False  # mark predates entry — cannot be a post-entry close
        return True

    def _resolve_close_price(
        self, position: dict[str, Any], mark: dict[str, Any], *, force: bool
    ) -> tuple[float, str, bool] | None:
        """Resolve the exit premium for a close.

        Returns (exit_premium, price_source, settled_economic) or None to signal
        "no trustworthy price — keep the position open". `settled_economic` is
        False only for a forced operator close with no real price (excluded from
        RL training so it can't poison the policy with a fabricated reward).
        """
        # 1) Current-cycle mark — only if fresh & post-entry (closes the
        #    stale-watchlist-LTP hole that fabricated 80/115 phantom losses).
        m = _safe_float_or_none((mark or {}).get("premium"))
        if m is not None and m > 0 and self._mark_is_fresh_for_close(
            position, mark, trust_live_source=True
        ):
            return m, str((mark or {}).get("price_source") or "live_mark"), True
        # 2) Stored prior-cycle mark — same freshness contract. A frozen
        #    watchlist LTP that merely "moved off entry" is NOT a real post-entry
        #    price; require a live source or a fresh post-entry mark_time.
        latest = _safe_float_or_none(position.get("latest_premium"))
        src = str(position.get("price_source") or "")
        if latest is not None and latest > 0 and self._mark_is_fresh_for_close(
            position,
            {"mark_time": position.get("mark_time"), "price_source": src},
            trust_live_source=True,
        ):
            return latest, src or "stored_mark", True
        # No live price. If the option is at/after expiry, settle at intrinsic.
        expiry_d = _parse_date_only(position.get("expiry"))
        today = datetime.now(timezone.utc).date()
        if expiry_d is not None and expiry_d <= today:
            spot = (
                _safe_float_or_none(position.get("latest_spot"))
                or _safe_float_or_none(position.get("entry_spot"))
                or 0.0
            )
            strike = _safe_float_or_none(position.get("strike")) or 0.0
            otype = str(position.get("option_type") or "CE")
            intrinsic = max(0.0, spot - strike) if otype == "CE" else max(0.0, strike - spot)
            return intrinsic, "expiry_intrinsic", True
        if force:
            # Operator explicitly closing with no live price — settle at last
            # known, flagged so it is excluded from RL training.
            return (latest or entry or 0.0), "stale_forced", False
        return None

    def _close_position(
        self,
        position: dict[str, Any],
        *,
        mark: dict[str, Any],
        close_time: str,
        close_reason: str,
        force: bool = False,
    ) -> bool:
        """Close one position, book costs, feed RL reward. Returns True if closed.

        On the automatic (signal/stop) paths this returns False — leaving the
        position OPEN — when no trustworthy live price exists, instead of
        fabricating a ₹0 breakeven at the entry premium (which hid losses and
        trained the policy on a fake r=0). `force=True` (operator close) always
        closes, settling at intrinsic when expired.
        """
        resolved = self._resolve_close_price(position, mark, force=force)
        if resolved is None:
            return False
        latest_premium, price_source, settled_economic = resolved
        latest_spot = float(
            mark.get("spot")
            or position.get("latest_spot")
            or position.get("entry_spot")
            or 0.0
        )
        quantity = int(position.get("quantity_units") or 0)
        entry_premium = float(position.get("entry_premium") or latest_premium or 0.0)
        position["status"] = "closed"
        position["updated_at"] = close_time
        position["closed_at"] = close_time
        position["close_reason"] = close_reason
        position["exit_premium"] = latest_premium
        position["latest_premium"] = latest_premium
        position["exit_spot"] = latest_spot
        position["latest_spot"] = latest_spot
        position["price_source"] = price_source or mark.get("price_source") or position.get("price_source")
        position["mark_time"] = mark.get("mark_time") or position.get("mark_time")
        position["unrealized_pnl"] = 0.0
        position["data_missing"] = not settled_economic
        realized_gross = round((latest_premium - entry_premium) * quantity, 2)
        # Deduct real round-trip charges (brokerage + STT + exchange txn + SEBI
        # + GST + stamp) so paper P&L is NET, not gross — paper used to overstate
        # live by the entire charge stack (STT alone is 0.10% of sell-side
        # premium on index options). WS-1.4 paper fidelity. The directional book
        # uses its own paper store (NOT PaperPortfolio), so the shared
        # portfolio.py cost wiring doesn't reach it — apply the same shared
        # paper_engine.costs model here. Directional is long-premium (BUY entry).
        try:
            from paper_engine.costs import round_trip_charges
            txn_cost = round_trip_charges(
                symbol=str(
                    position.get("trading_symbol")
                    or position.get("instrument_key")
                    or position.get("underlying")
                    or ""
                ),
                instrument_type=str(position.get("option_type") or "CE"),
                entry_price=entry_premium,
                exit_price=latest_premium,
                qty=quantity,
                entry_action="BUY",
            )
        except Exception:  # noqa: BLE001
            txn_cost = 0.0
        # Bid/ask spread + slippage on BOTH fills — the dominant cost on index
        # options, previously ignored on the live book (only charges were taken,
        # making paper P&L optimistic vs the backtest, which models fills). Only
        # charged against a real tradeable mark; intrinsic/stale settlements
        # carry no spread.
        slippage_cost = 0.0
        if settled_economic and price_source != "expiry_intrinsic":
            per_side_pct = self._per_side_cost_pct(position)
            slippage_cost = round((abs(entry_premium) + abs(latest_premium)) * per_side_pct * quantity, 2)
        realized = round(realized_gross - txn_cost - slippage_cost, 2)
        position["realized_pnl"] = realized
        position["realized_pnl_gross"] = realized_gross
        position["transaction_cost"] = round(txn_cost, 2)
        position["slippage_cost"] = slippage_cost
        # Feed realized PnL to the RL policy — but NOT for a fabricated/stale
        # exit (data_missing), since a fake reward biases the value posterior.
        # Genuine intrinsic settlements are real and DO train.
        if self.policy is not None and settled_economic:
            try:
                r_multiple = self.policy.record_close(
                    position_id=str(position.get("position_id") or ""),
                    realized_pnl=float(realized),
                )
                if r_multiple is not None:
                    position["policy_r_multiple"] = round(float(r_multiple), 4)
            except Exception:
                pass
        try:
            asyncio.create_task(
                paper_trade_recorder.record_event(
                    strategy="directional_options",
                    event="close",
                    underlying=position.get("underlying"),
                    instrument_key=position.get("instrument_key"),
                    option_type=position.get("option_type"),
                    strike=position.get("strike"),
                    expiry=position.get("expiry"),
                    quantity=quantity,
                    entry_premium=entry_premium,
                    exit_premium=latest_premium,
                    realized=realized,
                    position_id=position.get("position_id"),
                    reason=close_reason,
                )
            )
        except RuntimeError:
            # No running event loop (e.g. unit tests) — skip event logging.
            pass
        return True

    async def realized_windows(self) -> dict[str, float]:
        """Realized P&L for the current IST day and ISO week from the closed book.

        Feeds the lane-local daily/weekly loss-cap kill-switch in risk.approve —
        the live entry path previously passed daily_realized=0, so the cap (₹60k/
        day at default 4R) could never fire. Falls back to 0 on any DB error.
        """
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(ist).date()
        week_start = today - timedelta(days=today.weekday())
        today_realized = 0.0
        week_realized = 0.0
        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT (payload->>'realized_pnl')::float8 AS pnl, "
                            "payload->>'closed_at' AS closed_at "
                            "FROM directional_paper_positions "
                            "WHERE status = 'closed' AND payload->>'closed_at' IS NOT NULL"
                        )
                    )
                ).all()
            for pnl, closed_at in rows:
                ts = _parse_iso(closed_at)
                if ts is None or pnl is None:
                    continue
                closed_date = ts.astimezone(ist).date()
                if closed_date == today:
                    today_realized += float(pnl)
                if closed_date >= week_start:
                    week_realized += float(pnl)
        except Exception:  # noqa: BLE001 — kill-switch plumbing must never break the cycle
            return {"today_realized": 0.0, "week_realized": 0.0}
        return {"today_realized": round(today_realized, 2), "week_realized": round(week_realized, 2)}

    async def _summary(self, open_positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
        # Lifetime realized from the DB-wide SUM over ALL closed positions — not the
        # capped in-memory list (LIMIT 500 on load + [-250:] on save), which silently
        # understated realized/equity once closed-trade history outgrew the cap.
        # Falls back to the in-memory sum if the DB is unavailable (tests/degraded).
        realized: float | None = None
        try:
            async with AsyncSessionLocal() as session:
                val = (await session.execute(text(
                    "SELECT COALESCE(SUM((payload->>'realized_pnl')::float8), 0.0) "
                    "FROM directional_paper_positions WHERE status = 'closed'"
                ))).scalar()
            realized = float(val if val is not None else 0.0)
        except Exception:
            realized = None
        if realized is None:
            realized = sum(float(row.get("realized_pnl") or 0.0) for row in closed_positions)
        realized = round(realized, 2)
        unrealized = round(sum(float(row.get("unrealized_pnl") or 0.0) for row in open_positions), 2)

        # Capital accounting matches the AI/FMP/S1/S2 canonical shape so the
        # frontend portfolio panel can render uniformly across all lanes.
        # Premium × quantity is the cash locked against each open long-option.
        initial_capital = DIRECTIONAL_INITIAL_CAPITAL
        reserved_margin = round(
            sum(
                float(p.get("entry_premium") or 0.0) * float(p.get("quantity_units") or 0)
                for p in open_positions
            ),
            2,
        )
        open_premium_value = round(
            sum(
                float(p.get("latest_premium") or p.get("entry_premium") or 0.0)
                * float(p.get("quantity_units") or 0)
                for p in open_positions
            ),
            2,
        )
        open_risk_budget = round(
            sum(
                float(p.get("max_loss") or p.get("risk_budget") or 0.0)
                for p in open_positions
            ),
            2,
        )
        largest_position_value = 0.0
        if open_positions:
            largest_position_value = max(
                float(p.get("latest_premium") or p.get("entry_premium") or 0.0)
                * float(p.get("quantity_units") or 0)
                for p in open_positions
            )
        largest_position_value = round(largest_position_value, 2)
        total_equity = round(initial_capital + realized + unrealized, 2)
        available_capital = round(initial_capital + realized - reserved_margin, 2)
        total_return_pct = round(
            ((total_equity - initial_capital) / initial_capital) * 100.0, 4
        ) if initial_capital else 0.0

        closed_sorted = sorted(
            closed_positions,
            key=lambda r: str(r.get("closed_at") or r.get("updated_at") or ""),
        )
        running_equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        trade_returns_pct: list[float] = []
        r_multiples: list[float] = []
        gross_profit = 0.0
        gross_loss = 0.0
        best_trade = 0.0
        worst_trade = 0.0
        wins = 0
        losses = 0
        for row in closed_sorted:
            pnl = float(row.get("realized_pnl") or 0.0)
            best_trade = max(best_trade, pnl)
            worst_trade = min(worst_trade, pnl)
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
                gross_profit += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
            r_multiple = _safe_float_or_none(row.get("policy_r_multiple"))
            if r_multiple is not None:
                r_multiples.append(float(r_multiple))

        sharpe = 0.0
        if len(trade_returns_pct) >= 2:
            mean = sum(trade_returns_pct) / len(trade_returns_pct)
            var = sum((r - mean) ** 2 for r in trade_returns_pct) / max(len(trade_returns_pct) - 1, 1)
            stdev = var ** 0.5
            if stdev > 0:
                sharpe = round(mean / stdev, 4)

        win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0
        total_trades = wins + losses
        avg_win = (gross_profit / wins) if wins else 0.0
        avg_loss = -(gross_loss / losses) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        avg_r_multiple = (sum(r_multiples) / len(r_multiples)) if r_multiples else 0.0
        realized_r_total = sum(r_multiples) if r_multiples else 0.0

        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 2),
            "initial_capital": initial_capital,
            "available_capital": available_capital,
            "reserved_margin": reserved_margin,
            "entry_premium_value": reserved_margin,
            "open_premium_value": open_premium_value,
            "open_risk_budget": open_risk_budget,
            "open_risk_R": round(unrealized / open_risk_budget, 4) if open_risk_budget else 0.0,
            "unrealized_return_pct": round((unrealized / reserved_margin) * 100.0, 4) if reserved_margin else 0.0,
            "capital_deployed_pct": round((reserved_margin / initial_capital) * 100.0, 4) if initial_capital else 0.0,
            "open_exposure_pct": round((open_premium_value / total_equity) * 100.0, 4) if total_equity else 0.0,
            "largest_position_value": largest_position_value,
            "largest_position_pct": round((largest_position_value / total_equity) * 100.0, 4) if total_equity else 0.0,
            "total_equity": total_equity,
            "total_return_pct": total_return_pct,
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": sharpe,
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "policy_trades": len(r_multiples),
            "avg_r_multiple": round(avg_r_multiple, 4),
            "realized_r_total": round(realized_r_total, 4),
        }

    async def reset_account(self, *, actor: str | None = None) -> dict[str, Any]:
        """Archive current state and wipe positions+journal back to the
        funded baseline. Mirrors `archive_and_reset_paper_account` on S1/S2.
        Idempotent — a second call on an already-empty book is a no-op."""
        async with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_dir = self.root / "archive" / stamp
            archive_dir.mkdir(parents=True, exist_ok=True)
            if self.positions_path.exists():
                self.positions_path.replace(archive_dir / "paper_positions.json")
            if self.journal_path.exists():
                self.journal_path.replace(archive_dir / "paper_journal.jsonl")
            # Wipe the DB book (the upsert-based _save_positions can't clear
            # rows by passing empty lists — it only inserts/updates).
            async with AsyncSessionLocal() as session:
                await session.execute(text("DELETE FROM directional_paper_positions"))
                await session.execute(text("DELETE FROM directional_paper_journal"))
                await session.commit()
        return {
            "reset": True,
            "actor": actor,
            "archived_to": str(archive_dir.relative_to(self.root.parent)) if archive_dir.exists() else None,
            "initial_capital": DIRECTIONAL_INITIAL_CAPITAL,
        }

    async def capital_status(self) -> dict[str, Any]:
        state = await self._load_positions()
        return await self._summary(
            list(state.get("open_positions", [])),
            list(state.get("closed_positions", [])),
        )

    # ── DB-backed persistence (directional_paper_positions / _journal) ───────
    #
    # The book used to be JSON files (paper_positions.json / paper_journal.jsonl)
    # — no durable history, frozen on container recreate, closed trades capped
    # at 250 then lost. It now lives in the DB: each position is one row keyed
    # by position_id with the FULL dict in a JSONB `payload` (so the open/close
    # logic is untouched — it still operates on the same in-memory dict lists)
    # plus extracted key columns for analysis. Closed positions ACCUMULATE.

    @staticmethod
    def _ts(value: Any) -> datetime | None:
        return _parse_iso(value)

    def _position_db_params(self, payload: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "position_id": str(payload.get("position_id") or ""),
            "status": status,
            "underlying": payload.get("underlying"),
            "expiry": str(payload.get("expiry") or "") or None,
            "strike": _safe_float_or_none(payload.get("strike")),
            "option_type": payload.get("option_type"),
            "direction": payload.get("direction"),
            "quantity_units": _safe_int_or_none(payload.get("quantity_units")),
            "entry_premium": _safe_float_or_none(payload.get("entry_premium")),
            "latest_premium": _safe_float_or_none(payload.get("latest_premium")),
            "exit_premium": _safe_float_or_none(payload.get("exit_premium")),
            "unrealized_pnl": _safe_float_or_none(payload.get("unrealized_pnl")),
            "realized_pnl": _safe_float_or_none(payload.get("realized_pnl")),
            "opened_at": self._ts(payload.get("opened_at")),
            "updated_at": self._ts(payload.get("updated_at")),
            "closed_at": self._ts(payload.get("closed_at")),
            "close_reason": payload.get("close_reason"),
            "payload": json.dumps(payload, default=str),
        }

    def _load_positions_file(self) -> dict[str, Any]:
        if not self.positions_path.exists():
            return {"open_positions": [], "closed_positions": [], "last_synced_at": _utc_now()}
        try:
            state = json.loads(self.positions_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DirPaper] legacy position read failed: {exc}")
            state = {}
        return {
            "open_positions": list(state.get("open_positions") or []),
            "closed_positions": list(state.get("closed_positions") or []),
            "last_synced_at": state.get("last_synced_at") or _utc_now(),
        }

    def _load_journal_file(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()[-1000:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DirPaper] legacy journal read failed: {exc}")
        return rows

    async def _maybe_seed_from_file(self) -> None:
        """One-time import of the legacy JSON book into the DB (only when the
        DB is empty and a file exists). Idempotent + best-effort."""
        if self._db_seeded:
            return
        self._db_seeded = True
        try:
            async with AsyncSessionLocal() as session:
                count = (await session.execute(
                    text("SELECT count(*) FROM directional_paper_positions")
                )).scalar() or 0
            if count > 0:
                return
            if self.positions_path.exists():
                try:
                    state = json.loads(self.positions_path.read_text(encoding="utf-8"))
                    if state.get("open_positions") or state.get("closed_positions"):
                        await self._save_positions(state)
                        logger.info("[DirPaper] seeded positions from legacy file into DB")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DirPaper] file→DB position seed failed: {exc}")
            if self.journal_path.exists():
                try:
                    lines = self.journal_path.read_text(encoding="utf-8").splitlines()[-2000:]
                    for line in lines:
                        line = line.strip()
                        if line:
                            try:
                                await self._append_journal(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                    logger.info("[DirPaper] seeded journal from legacy file into DB")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DirPaper] file→DB journal seed failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DirPaper] seed check failed: {exc}")

    async def _append_journal(self, payload: dict[str, Any]) -> None:
        async with AsyncSessionLocal() as session:
            approved = payload.get("approved")
            await session.execute(
                text(
                    """
                    INSERT INTO directional_paper_journal
                        (recorded_at, underlying, approved, payload)
                    VALUES (:recorded_at, :underlying, :approved, CAST(:payload AS JSONB))
                    """
                ),
                {
                    "recorded_at": self._ts(payload.get("recorded_at")),
                    "underlying": payload.get("underlying"),
                    "approved": bool(approved) if approved is not None else None,
                    "payload": json.dumps(payload, default=str),
                },
            )
            await session.commit()

    async def _load_journal(self) -> list[dict[str, Any]]:
        await self._maybe_seed_from_file()
        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    text(
                        "SELECT payload FROM directional_paper_journal "
                        "ORDER BY recorded_at DESC NULLS LAST, id DESC LIMIT 1000"
                    )
                )).mappings().all()
            return [_as_dict(r["payload"]) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DirPaper] DB journal read failed; using legacy file fallback: {exc}")
            return self._load_journal_file()

    async def _load_positions(self) -> dict[str, Any]:
        await self._maybe_seed_from_file()
        try:
            async with AsyncSessionLocal() as session:
                open_rows = (await session.execute(
                    text("SELECT payload FROM directional_paper_positions WHERE status = 'open'")
                )).mappings().all()
                closed_rows = (await session.execute(
                    text(
                        "SELECT payload FROM directional_paper_positions WHERE status = 'closed' "
                        "ORDER BY closed_at DESC NULLS LAST LIMIT 500"
                    )
                )).mappings().all()
            return {
                "open_positions": [_as_dict(r["payload"]) for r in open_rows],
                "closed_positions": [_as_dict(r["payload"]) for r in closed_rows],
                "last_synced_at": _utc_now(),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DirPaper] DB position read failed; using legacy file fallback: {exc}")
            return self._load_positions_file()

    async def _save_positions(self, state: dict[str, Any]) -> None:
        """Upsert the open + closed positions by position_id. Closed positions
        accumulate (never deleted) → durable trade history. A position moves
        open→closed when it appears in the closed list (the logic always closes
        before it leaves the open list, so no stale-open rows linger)."""
        rows = [(p, "open") for p in (state.get("open_positions") or [])]
        rows += [(p, "closed") for p in (state.get("closed_positions") or [])]
        params = [self._position_db_params(p, s) for p, s in rows if p.get("position_id")]
        if not params:
            return
        upsert = text(
            """
            INSERT INTO directional_paper_positions
                (position_id, status, underlying, expiry, strike, option_type,
                 direction, quantity_units, entry_premium, latest_premium,
                 exit_premium, unrealized_pnl, realized_pnl, opened_at,
                 updated_at, closed_at, close_reason, payload)
            VALUES
                (:position_id, :status, :underlying, :expiry, :strike, :option_type,
                 :direction, :quantity_units, :entry_premium, :latest_premium,
                 :exit_premium, :unrealized_pnl, :realized_pnl, :opened_at,
                 :updated_at, :closed_at, :close_reason, CAST(:payload AS JSONB))
            ON CONFLICT (position_id) DO UPDATE SET
                status=EXCLUDED.status, underlying=EXCLUDED.underlying,
                expiry=EXCLUDED.expiry, strike=EXCLUDED.strike,
                option_type=EXCLUDED.option_type, direction=EXCLUDED.direction,
                quantity_units=EXCLUDED.quantity_units,
                entry_premium=EXCLUDED.entry_premium,
                latest_premium=EXCLUDED.latest_premium,
                exit_premium=EXCLUDED.exit_premium,
                unrealized_pnl=EXCLUDED.unrealized_pnl,
                realized_pnl=EXCLUDED.realized_pnl, opened_at=EXCLUDED.opened_at,
                updated_at=EXCLUDED.updated_at, closed_at=EXCLUDED.closed_at,
                close_reason=EXCLUDED.close_reason, payload=EXCLUDED.payload
            """
        )
        async with AsyncSessionLocal() as session:
            await session.execute(upsert, params)
            await session.commit()
