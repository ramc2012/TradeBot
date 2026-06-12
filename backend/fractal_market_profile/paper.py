from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import text

from core.paper_trade_recorder import paper_trade_recorder
from db.database import AsyncSessionLocal
from fractal_market_profile.config import FMP_INITIAL_CAPITAL, PAPER_ROOT
from fractal_market_profile.schemas import FMPPaperPositionRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _parse_iso_date(value: Any) -> Optional[date]:
    """Best-effort date parser used to detect expired contracts."""
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class FMPPaperStore:
    def __init__(self, root: Path | str = PAPER_ROOT, *, policy: Any | None = None):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parent.parent / self.root
        self.journal_path = self.root / "paper_journal.jsonl"
        self.positions_path = self.root / "paper_positions.json"
        self.policy = policy
        self._lock = asyncio.Lock()

    async def list_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        records = self._load_journal()
        normalized = str(symbol or "").upper().strip()
        if normalized:
            records = [row for row in records if str(row.get("underlying") or "").upper() == normalized]
        records.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
        return {
            "symbol_filter": normalized or None,
            "count": len(records),
            "records": records[:limit],
        }

    async def list_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        state = self._load_positions()
        normalized = str(symbol or "").upper().strip()
        open_positions = list(state.get("open_positions", []))
        closed_positions = list(state.get("closed_positions", []))
        if normalized:
            open_positions = [row for row in open_positions if str(row.get("underlying") or "").upper() == normalized]
            closed_positions = [row for row in closed_positions if str(row.get("underlying") or "").upper() == normalized]
        open_positions.sort(key=lambda row: str(row.get("opened_at") or ""), reverse=True)
        closed_positions.sort(key=lambda row: str(row.get("closed_at") or row.get("updated_at") or ""), reverse=True)
        if status == "open":
            closed_positions = []
        elif status == "closed":
            open_positions = []
        return {
            "symbol_filter": normalized or None,
            "status": status,
            "summary": self._summary(open_positions, closed_positions),
            "open_positions": open_positions[:limit],
            "closed_positions": closed_positions[:limit],
        }

    async def record_signal(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Record an FMP signal snapshot and reconcile paper positions.

        Drives the full paper-trade lifecycle: refresh marks, apply
        auto-exits (stop/target/expiry/premium-zero), close on FLAT or
        signal-flip, dedupe duplicates, then open a fresh position when
        the signal is actionable. Mirrors `auction_intelligence/paper/book.py`
        so both desks behave the same way.
        """
        signal = snapshot.get("current_signal") or {}
        recorded_at = _utc_now()
        session_date = _parse_iso_date(snapshot.get("session", {}).get("session_date"))
        spot_price = _as_float((snapshot.get("session") or {}).get("last_price"))
        entry = {
            "recorded_at": recorded_at,
            "underlying": snapshot.get("symbol_code"),
            "session_date": snapshot.get("session", {}).get("session_date"),
            "hourly_number": signal.get("hourly_number"),
            "setup_name": signal.get("setup_name"),
            "action": signal.get("action"),
            "confidence": signal.get("confidence"),
            "horizon": signal.get("horizon"),
            "daily_shape": signal.get("daily_shape"),
            "hourly_shape": signal.get("hourly_shape"),
            "entry_trigger": signal.get("entry_trigger"),
            "stop_level": signal.get("stop_level"),
            "target_level": signal.get("target_level"),
            "filters": signal.get("filters") or [],
            "rationale": signal.get("rationale") or [],
            "options": signal.get("options"),
            "order_flow_bias": signal.get("order_flow_bias"),
            "actionable": bool(signal.get("actionable")),
            "ai_model": signal.get("ai_model"),
            "policy": signal.get("policy"),
        }
        self._append_journal(entry)

        async with self._lock:
            state = self._load_positions()
            open_positions = list(state.get("open_positions", []))
            closed_positions = list(state.get("closed_positions", []))
            underlying = str(snapshot.get("symbol_code") or "").upper().strip()
            matching = [row for row in open_positions if str(row.get("underlying") or "").upper() == underlying]

            # Always refresh latest_premium for every open position before we
            # consider closing — otherwise exit_premium gets stamped with stale
            # entry-time premium and realized_pnl collapses to zero.
            await self._refresh_open_premiums(matching, snapshot=snapshot)
            # Carry the latest underlying spot forward; helpful for context
            # and for risk-level detection (premium-based vs spot-based).
            if spot_price is not None:
                for row in matching:
                    row["latest_spot_price"] = spot_price

            # Overlay the new signal's option premium onto matching rows
            # BEFORE auto-exit so stop/target judgements use the freshest
            # mark. The DB-driven refresh above can lag the live snapshot
            # by a few seconds and would otherwise mask a stop hit.
            new_options = signal.get("options") or {}
            new_options_premium = _as_float(new_options.get("premium"))
            if new_options_premium is not None and new_options_premium >= 0:
                for row in matching:
                    if self._is_same_contract(row, new_options):
                        row["latest_premium"] = new_options_premium
                        row["unrealized_pnl"] = round(self._pnl(row, new_options_premium), 2)

            # ── Stage 1: auto-exits (independent of new signal) ──────────
            # Stop / target / expiry / premium-zero can trip even when the
            # snapshot carries no fresh signal or the signal would otherwise
            # refresh the position. When we auto-exit a position for the
            # *same* contract+action carried by the incoming signal we must
            # also block re-entry in Stage 3 — getting stopped out and
            # immediately re-buying the same option in the same cycle is a
            # bug, not a feature.
            same_contract_action_exited = False
            for row in list(matching):
                exit_reason = self._auto_exit_reason(row, session_date=session_date)
                if not exit_reason:
                    continue
                if (
                    new_options
                    and self._is_same_contract(row, new_options)
                    and str(row.get("action") or "") == str(signal.get("action") or "")
                ):
                    same_contract_action_exited = True
                await self._mark_closed(
                    row,
                    recorded_at=recorded_at,
                    exit_premium=row.get("latest_premium"),
                    reason=exit_reason,
                )
                open_positions.remove(row)
                closed_positions.append(row)
                matching.remove(row)

            # ── Stage 2: FLAT or no actionable options → close remainder ─
            # Two guard rails before closing on FLAT — both targeting the
            # "zero-PnL trade" pathology we observed:
            #
            #   1. Minimum hold: refuse to close a position opened less
            #      than 5 minutes ago. The strategy occasionally flickers
            #      actionable → FLAT in successive snapshots; bouncing
            #      a fresh paper trade in 30s adds noise and never wins.
            #   2. Stalled premium refresh: if `latest_premium` is still
            #      exactly the entry premium, the live quote source
            #      likely hasn't reached us yet. Closing at entry would
            #      stamp a fake "0 PnL" exit. Skip and wait for the next
            #      snapshot — the auto-exit logic (stop/target/expiry)
            #      will still trigger when real data arrives.
            if str(signal.get("action") or "").upper() == "FLAT" or not signal.get("options"):
                min_hold_seconds = 5 * 60
                for row in matching:
                    opened_at = row.get("opened_at")
                    try:
                        held_seconds = (
                            datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
                            - datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                        ).total_seconds()
                    except (TypeError, ValueError):
                        held_seconds = float("inf")
                    entry_p = _as_float(row.get("entry_premium")) or 0.0
                    latest_p = _as_float(row.get("latest_premium")) or 0.0
                    if held_seconds < min_hold_seconds:
                        # Too fresh — hold through the brief FLAT blip.
                        continue
                    if entry_p > 0 and abs(latest_p - entry_p) < 1e-9:
                        # Premium hasn't moved at all since entry — almost
                        # certainly a refresh stall, not a flat exit at
                        # break-even. Wait one more cycle.
                        continue
                    await self._mark_closed(
                        row,
                        recorded_at=recorded_at,
                        exit_premium=latest_p,
                        reason="flat_snapshot",
                    )
                    open_positions.remove(row)
                    closed_positions.append(row)
                self._save_positions(
                    {
                        "last_synced_at": recorded_at,
                        "open_positions": open_positions,
                        "closed_positions": closed_positions[-250:],
                    }
                )
                return self._summary(open_positions, closed_positions)

            options = signal["options"]
            lot_size = int(options.get("lot_size") or 1)
            quantity = lot_size
            latest_premium = float(options.get("premium") or 0.0)

            # ── Stage 3: refresh existing matching position OR signal-flip ─
            # Sticky stop/target: keep prior risk level if the new snapshot
            # didn't include one (`signal.stop_level == 0` is treated as
            # "no update", not "wipe to zero").
            new_stop = _as_float(signal.get("stop_level"))
            new_target = _as_float(signal.get("target_level"))
            refreshed = False
            for row in list(matching):
                same_contract = (
                    str(row.get("instrument_key") or "") == str(options.get("instrument_key") or "")
                    or (
                        str(row.get("option_type") or "") == str(options.get("option_type") or "")
                        and float(row.get("strike") or 0.0) == float(options.get("strike") or 0.0)
                        and str(row.get("expiry") or "") == str(options.get("expiry") or "")
                    )
                )
                same_action = str(row.get("action") or "") == str(signal.get("action") or "")
                if same_contract and same_action:
                    if refreshed:
                        # Dedupe: any further matches are stale duplicates.
                        await self._mark_closed(
                            row,
                            recorded_at=recorded_at,
                            exit_premium=row.get("latest_premium") or latest_premium,
                            reason="dedupe_repair",
                        )
                        open_positions.remove(row)
                        closed_positions.append(row)
                        continue
                    row["updated_at"] = recorded_at
                    row["latest_premium"] = latest_premium
                    row["unrealized_pnl"] = round(self._pnl(row, latest_premium), 2)
                    row["confidence"] = float(signal.get("confidence") or row.get("confidence") or 0.0)
                    if new_stop and new_stop > 0:
                        row["stop_level"] = new_stop
                    if new_target and new_target > 0:
                        row["target_level"] = new_target
                    row["daily_shape"] = str(signal.get("daily_shape") or row.get("daily_shape") or "")
                    row["hourly_shape"] = str(signal.get("hourly_shape") or row.get("hourly_shape") or "")
                    refreshed = True
                    continue

                # Different contract/action → close as signal_flip or roll.
                close_reason = "signal_flip" if not same_action else "contract_roll"
                await self._mark_closed(
                    row,
                    recorded_at=recorded_at,
                    exit_premium=latest_premium,
                    reason=close_reason,
                )
                open_positions.remove(row)
                closed_positions.append(row)

            if (
                not refreshed
                and not same_contract_action_exited
                and signal.get("actionable")
            ):
                new_position = asdict(
                    FMPPaperPositionRecord(
                        position_id=uuid4().hex,
                        status="open",
                        opened_at=recorded_at,
                        updated_at=recorded_at,
                        closed_at=None,
                        underlying=underlying,
                        setup_name=str(signal.get("setup_name") or ""),
                        action=str(signal.get("action") or ""),
                        horizon=str(signal.get("horizon") or "swing"),
                        trading_symbol=options.get("trading_symbol"),
                        instrument_key=options.get("instrument_key"),
                        instrument_type=options.get("instrument_type"),
                        option_type=options.get("option_type"),
                        strike=float(options.get("strike") or 0.0),
                        expiry=str(options.get("expiry") or ""),
                        quantity=quantity,
                        lot_size=lot_size,
                        entry_premium=latest_premium,
                        latest_premium=latest_premium,
                        exit_premium=None,
                        realized_pnl=0.0,
                        unrealized_pnl=0.0,
                        stop_level=float(signal.get("stop_level") or 0.0),
                        target_level=float(signal.get("target_level") or 0.0),
                        confidence=float(signal.get("confidence") or 0.0),
                        daily_shape=str(signal.get("daily_shape") or ""),
                        hourly_shape=str(signal.get("hourly_shape") or ""),
                    )
                )
                ai_model = signal.get("ai_model") if isinstance(signal.get("ai_model"), dict) else {}
                policy_payload = signal.get("policy") if isinstance(signal.get("policy"), dict) else {}
                if ai_model:
                    new_position["ai_rule_score"] = ai_model.get("score")
                    new_position["ai_rule_setup"] = ai_model.get("setup")
                    new_position["ai_rule_blockers"] = list(ai_model.get("blockers") or [])
                if policy_payload:
                    new_position["policy_act"] = bool(policy_payload.get("act"))
                    new_position["policy_sampled_r"] = policy_payload.get("sampled_value")
                    new_position["policy_expected_r"] = policy_payload.get("posterior_mean")
                    new_position["policy_warmup"] = bool(policy_payload.get("warmup"))
                if spot_price is not None:
                    new_position["latest_spot_price"] = spot_price
                    new_position["entry_spot_price"] = spot_price
                open_positions.append(new_position)
                if self.policy is not None:
                    try:
                        self.policy.register_open(
                            position_id=str(new_position.get("position_id") or ""),
                            signal=signal,
                            risk_basis=self._risk_basis(new_position),
                        )
                    except Exception:
                        pass
                try:
                    await paper_trade_recorder.record_event(
                        strategy="fractal_market_profile",
                        event="open",
                        underlying=new_position.get("underlying"),
                        instrument_key=new_position.get("instrument_key"),
                        option_type=new_position.get("option_type"),
                        strike=new_position.get("strike"),
                        expiry=new_position.get("expiry"),
                        quantity=int(new_position.get("quantity") or 0),
                        entry_premium=new_position.get("entry_premium"),
                        latest_premium=new_position.get("latest_premium"),
                        position_id=new_position.get("position_id"),
                        reason=str(new_position.get("setup_name") or ""),
                        extra={"action": new_position.get("action")},
                    )
                except Exception:
                    pass

            self._save_positions(
                {
                    "last_synced_at": recorded_at,
                    "open_positions": open_positions,
                    "closed_positions": closed_positions[-250:],
                }
            )
            return self._summary(open_positions, closed_positions)

    # ── Auto-exit helpers ────────────────────────────────────────────────

    def _auto_exit_reason(
        self,
        row: dict[str, Any],
        *,
        session_date: Optional[date],
    ) -> Optional[str]:
        """Decide whether `row` should be auto-closed before any new
        signal handling. Mirrors AI book's `_exit_reason_for_position`.

        Order is intentional:
          1. `expired_contract` — position past its expiry date.
          2. `premium_zero`    — option has been crushed to ≤ 0.
          3. `stop_loss`       — premium / spot hit the stop.
          4. `target_hit`      — premium / spot reached the target.
        """
        expiry = _parse_iso_date(row.get("expiry"))
        if expiry is not None and session_date is not None and expiry < session_date:
            return "expired_contract"

        latest_premium = _as_float(row.get("latest_premium"))
        if latest_premium is not None and latest_premium <= 0:
            # FUT shorts can legitimately have negative pnl without a zero
            # premium; only treat ≤ 0 as a zero-out signal for options.
            instr = str(row.get("instrument_type") or row.get("option_type") or "").upper()
            if instr != "FUT":
                return "premium_zero"

        stop = _as_float(row.get("stop_level"))
        target = _as_float(row.get("target_level"))
        if (stop is None or stop <= 0) and (target is None or target <= 0):
            return None

        latest_spot = _as_float(row.get("latest_spot_price"))
        premium_based = self._risk_levels_are_premium_based(row, stop=stop, target=target)
        # For premium-based stops compare against the option premium; for
        # spot-based stops compare against the underlying. If we know the
        # stops are spot-based but have no spot price yet, we cannot judge
        # — bail out rather than falsely compare a spot-level stop against
        # an option premium and auto-close the trade on the next snapshot.
        if premium_based:
            latest_value = latest_premium
        elif latest_spot is not None:
            latest_value = latest_spot
        else:
            return None
        if latest_value is None:
            return None

        action = str(row.get("action") or "").upper()
        if action == "LONG":
            if stop is not None and stop > 0 and latest_value <= stop:
                return "stop_loss"
            if target is not None and target > 0 and latest_value >= target:
                return "target_hit"
        elif action == "SHORT":
            if stop is not None and stop > 0 and latest_value >= stop:
                return "stop_loss"
            if target is not None and target > 0 and latest_value <= target:
                return "target_hit"
        return None

    @staticmethod
    def _is_same_contract(row: dict[str, Any], options: dict[str, Any]) -> bool:
        """True when the row references the same contract as the new
        signal's `options` payload. Matches by instrument_key when both
        have it, otherwise falls back to (option_type, strike, expiry)."""
        row_key = str(row.get("instrument_key") or "")
        new_key = str(options.get("instrument_key") or "")
        if row_key and new_key:
            return row_key == new_key
        return (
            str(row.get("option_type") or "") == str(options.get("option_type") or "")
            and float(row.get("strike") or 0.0) == float(options.get("strike") or 0.0)
            and str(row.get("expiry") or "") == str(options.get("expiry") or "")
        )

    @staticmethod
    def _risk_levels_are_premium_based(
        row: dict[str, Any],
        *,
        stop: Optional[float],
        target: Optional[float],
    ) -> bool:
        """Heuristic: risk levels in the same order of magnitude as the
        entry premium are premium-based; risk levels much larger than the
        entry premium (typical of underlying-spot stops e.g. NIFTY 22505
        for a ₹186 option) are spot-based.

        FMP signals carry both kinds: option strategies attach premium-
        based exits, futures/spot strategies attach spot-based exits. We
        must not auto-close an option position when its spot-level stop
        sits 100× above the premium.
        """
        levels = [v for v in (stop, target) if v is not None and v > 0]
        if not levels:
            return False
        entry_premium = _as_float(row.get("entry_premium"))
        entry_spot = _as_float(row.get("entry_spot_price")) or _as_float(row.get("latest_spot_price"))
        max_level = max(levels)
        if entry_premium is not None and entry_premium > 0:
            # If a level is more than 5× the entry premium it almost
            # certainly references the underlying, not the option.
            if max_level > entry_premium * 5.0:
                return False
        if entry_spot is not None and entry_spot > 0:
            # Defensive check: a level within 25% of spot is also a spot
            # reference (covers the rare case where entry_premium is
            # missing but spot is known).
            if max_level >= entry_spot * 0.5:
                return False
        # Default: treat as premium-based, matching the historical FMP
        # behavior for option-only setups.
        return True

    async def _mark_closed(
        self,
        row: dict[str, Any],
        *,
        recorded_at: str,
        exit_premium: Any,
        reason: str,
    ) -> None:
        """Stamp a row as closed and notify the centralised recorder."""
        row["status"] = "closed"
        row["updated_at"] = recorded_at
        row["closed_at"] = recorded_at
        row["close_reason"] = reason
        if exit_premium is not None:
            row["exit_premium"] = exit_premium
        row["realized_pnl"] = self._pnl(row, row.get("exit_premium"))
        row["unrealized_pnl"] = 0.0
        if self.policy is not None:
            try:
                reward = self.policy.record_close(
                    position_id=str(row.get("position_id") or ""),
                    realized_pnl=float(row.get("realized_pnl") or 0.0),
                )
                if reward is not None:
                    row["policy_reward_r"] = round(float(reward), 6)
            except Exception:
                pass
        try:
            await paper_trade_recorder.record_event(
                strategy="fractal_market_profile",
                event="close",
                underlying=row.get("underlying"),
                instrument_key=row.get("instrument_key"),
                option_type=row.get("option_type"),
                strike=row.get("strike"),
                expiry=row.get("expiry"),
                quantity=int(row.get("quantity") or 0),
                entry_premium=row.get("entry_premium"),
                exit_premium=row.get("exit_premium"),
                realized=row.get("realized_pnl"),
                position_id=row.get("position_id"),
                reason=reason,
            )
        except Exception:
            pass

    async def _refresh_open_premiums(self, rows: list[dict[str, Any]], *, snapshot: dict[str, Any] | None = None) -> None:
        if not rows:
            return
        futures_rows = [
            row
            for row in rows
            if str(row.get("instrument_type") or row.get("option_type") or "").upper() == "FUT"
        ]
        snapshot_price = float(((snapshot or {}).get("session") or {}).get("last_price") or 0.0)
        for row in futures_rows:
            if snapshot_price > 0:
                row["latest_premium"] = snapshot_price
                row["unrealized_pnl"] = self._pnl(row, snapshot_price)
        option_rows = [row for row in rows if row not in futures_rows]
        keys = [str(row.get("instrument_key") or "") for row in option_rows if row.get("instrument_key")]
        if not keys:
            return
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (instrument_key)
                            instrument_key, ltp, time
                        FROM atm_option_watchlist_snapshots
                        WHERE instrument_key = ANY(:keys)
                          AND time >= NOW() - INTERVAL '2 days'
                        ORDER BY instrument_key, time DESC
                        """
                    ),
                    {"keys": keys},
                )
                latest = {str(r.instrument_key): float(r.ltp or 0.0) for r in result.fetchall()}
        except Exception as exc:
            logger.warning("fmp.paper.refresh_premium_failed error={}", exc)
            return
        for row in rows:
            ltp = latest.get(str(row.get("instrument_key") or ""))
            if ltp is not None and ltp > 0:
                row["latest_premium"] = ltp
                row["unrealized_pnl"] = round(
                    self._pnl(row, ltp),
                    2,
                )

    def _pnl(self, row: dict[str, Any], latest_price: Any) -> float:
        try:
            latest = float(latest_price or 0.0)
            entry = float(row.get("entry_premium") or 0.0)
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            return 0.0
        if str(row.get("instrument_type") or row.get("option_type") or "").upper() == "FUT" and str(row.get("action") or "").upper() == "SHORT":
            return round((entry - latest) * quantity, 2)
        return round((latest - entry) * quantity, 2)

    def _risk_basis(self, row: dict[str, Any]) -> float:
        try:
            entry = float(row.get("entry_premium") or 0.0)
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            return 1.0
        if str(row.get("instrument_type") or row.get("option_type") or "").upper() == "FUT":
            stop = _as_float(row.get("stop_level"))
            if stop is not None and stop > 0 and entry > 0:
                return max(abs(entry - stop) * quantity, 1.0)
            return max(entry * quantity * 0.01, 1.0)
        return max(entry * quantity, 1.0)

    def _summary(self, open_positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
        realized = round(sum(float(row.get("realized_pnl") or 0.0) for row in closed_positions), 2)
        unrealized = round(sum(float(row.get("unrealized_pnl") or 0.0) for row in open_positions), 2)

        # Capital accounting — turns FMP into a "funded" paper desk so its
        # numbers align with S1/S2/Commodity. We treat the open option
        # premium as cash reserved against the position (you paid premium
        # × qty to enter a long option; that capital is locked until close).
        initial_capital = FMP_INITIAL_CAPITAL
        reserved_margin = round(
            sum(
                float(row.get("entry_premium") or 0.0) * int(row.get("quantity") or 0)
                for row in open_positions
            ),
            2,
        )
        # equity = initial + all booked PnL + mark-to-market of open positions.
        total_equity = round(initial_capital + realized + unrealized, 2)
        # cash on hand = initial + realized − margin currently locked.
        available_capital = round(initial_capital + realized - reserved_margin, 2)
        total_return_pct = round(
            ((total_equity - initial_capital) / initial_capital) * 100.0, 4
        ) if initial_capital else 0.0

        # Equity curve walk over closed trades (chronological) — gives us a
        # simple, deterministic max-drawdown without needing a separate
        # snapshot loop. Sharpe needs daily returns; approximate from
        # per-trade returns expressed in percent.
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

        # Trade-return Sharpe (unannualized) — a rough quality signal, not
        # statistical truth. With more than a handful of trades it stabilises.
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
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 2),
            # new capital fields — mirror S1/S2/Commodity surface
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

    def _append_journal(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    def _load_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _load_positions(self) -> dict[str, Any]:
        if not self.positions_path.exists():
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}
        try:
            return json.loads(self.positions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"open_positions": [], "closed_positions": [], "last_synced_at": None}

    def _save_positions(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.positions_path.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
