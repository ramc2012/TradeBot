"""Paper option agent driven by Gann TP Delta confluence signals."""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from market_data.atm_watchlist import atm_watchlist_service


UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class GannTPDeltaPaperAgent:
    """Stateful paper-only option agent.

    The agent scans the ATM F&O watchlist, asks the Gann service for a live
    signal per underlying, and paper-buys the ATM option in the signal
    direction. It intentionally does not use broker order APIs.
    """

    def __init__(self, service: Any, config: dict[str, Any]):
        self.service = service
        self.config = config
        paper_root = Path(config["paper"]["journal_root"])
        self.state_path = paper_root / "agent_positions.json"
        self.journal_path = paper_root / "paper_agent_journal.jsonl"

    def status(self, limit: int = 50) -> dict[str, Any]:
        state = self._load_state()
        return self._status_payload(state, limit=limit)

    async def run_once(
        self,
        *,
        timeframe: str | None = None,
        lookback_sessions: int | None = None,
        anchor_mode: str | None = None,
        h_mode: str | None = None,
        live_refresh: bool | None = None,
        max_underlyings: int | None = None,
    ) -> dict[str, Any]:
        cfg = self.config.get("paper_agent", {})
        if cfg.get("enabled") is False:
            state = self._load_state()
            state["last_message"] = "Gann paper agent is disabled in configuration."
            state["last_scan_at"] = _now()
            self._save_state(state)
            return self._status_payload(state)

        timeframe = timeframe or str(cfg.get("timeframe") or "15minute")
        lookback_sessions = int(lookback_sessions or cfg.get("lookback_sessions") or 60)
        anchor_mode = anchor_mode or str(cfg.get("anchor_mode") or "auto_pivot")
        h_mode = h_mode or str(cfg.get("h_mode") or "median_tpd")
        live_refresh = bool(cfg.get("live_refresh") if live_refresh is None else live_refresh)
        max_underlyings = int(max_underlyings or 0)
        max_positions = max(1, int(cfg.get("max_positions") or 20))
        min_score = int(cfg.get("min_score") or self.config.get("signals", {}).get("score_threshold") or 3)

        state = self._load_state()
        try:
            watchlist = await atm_watchlist_service.get_watchlist(live_refresh=live_refresh)
        except Exception as exc:
            logger.exception("[GannPaperAgent] watchlist load failed")
            state["last_scan_at"] = _now()
            state["last_message"] = f"Watchlist load failed: {exc}"
            state["last_run"] = {"scanned": 0, "opened": 0, "closed": 0, "errors": 1}
            self._save_state(state)
            self._journal({"event": "scan_error", "message": state["last_message"]})
            return self._status_payload(state)

        rows = [row for row in watchlist.get("rows") or [] if isinstance(row, dict) and row.get("underlying")]
        rows = self._dedupe_rows(rows)
        # Restrict to the configured universe — Gann geometry needs spot
        # history that's only synced for indices and a handful of MCX
        # commodities. Scanning all ~215 F&O stocks wastes cycles and
        # pollutes the rejection counters with `no_gann_setup` results
        # that aren't true rejections, just missing-data noise.
        configured_universe = {
            str(item).upper() for item in self.config.get("universe") or []
        }
        if configured_universe:
            rows = [
                row
                for row in rows
                if str(row.get("underlying") or "").upper() in configured_universe
            ]
        if max_underlyings > 0:
            rows = rows[:max_underlyings]

        row_by_underlying = {str(row.get("underlying") or "").upper(): row for row in rows}
        closed_count = await self._refresh_open_positions(
            state,
            row_by_underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            anchor_mode=anchor_mode,
            h_mode=h_mode,
        )

        semaphore = asyncio.Semaphore(max(1, int(cfg.get("scan_concurrency") or 6)))

        async def _scan(row: dict[str, Any]) -> dict[str, Any]:
            underlying = str(row.get("underlying") or "").upper()
            async with semaphore:
                try:
                    snapshot = await self.service.live_snapshot(
                        underlying,
                        timeframe,
                        lookback_sessions,
                        anchor_mode,
                        h_mode,
                    )
                except Exception as exc:
                    logger.warning(f"[GannPaperAgent] signal scan failed for {underlying}: {exc}")
                    return {"underlying": underlying, "decision": "error", "reason": str(exc)}
            decision = self._scan_decision(row, snapshot, min_score=min_score)
            if decision.get("reason") == "missing_option_quote" and decision.get("option_type"):
                option = await self._option_from_store(
                    underlying=underlying,
                    option_type=str(decision.get("option_type") or ""),
                    spot_price=_safe_float(decision.get("spot_price"), 0.0) or 0.0,
                    cfg=cfg,
                )
                if option:
                    decision.update(
                        {
                            "decision": "open",
                            "reason": "gann_setup",
                            "direction": "long_call" if option["option_type"] == "CE" else "long_put",
                            "option": option,
                        }
                    )
            return decision

        scan_results = await asyncio.gather(*[_scan(row) for row in rows])

        opened = 0
        skipped = 0
        for decision in scan_results:
            if decision.get("decision") != "open":
                skipped += 1
                continue
            if len(self._open_positions(state)) >= max_positions:
                decision["decision"] = "skip"
                decision["reason"] = "max_positions_reached"
                skipped += 1
                continue
            underlying = str(decision.get("underlying") or "").upper()
            if self._position_for_underlying(state, underlying):
                decision["decision"] = "skip"
                decision["reason"] = "position_already_open"
                skipped += 1
                continue
            position = self._build_position(decision, cfg)
            if position is None:
                decision["decision"] = "skip"
                decision["reason"] = "missing_option_quote"
                skipped += 1
                continue
            state.setdefault("open_positions", []).append(position)
            self._journal({"event": "open", "position": position})
            opened += 1

        state["last_scan_at"] = _now()
        state["last_message"] = f"Scanned {len(rows)} F&O underlyings, opened {opened}, closed {closed_count}."
        state["last_run"] = {
            "scanned": len(rows),
            "opened": opened,
            "closed": closed_count,
            "skipped": skipped,
            "errors": sum(1 for item in scan_results if item.get("decision") == "error"),
            "timeframe": timeframe,
            "lookback_sessions": lookback_sessions,
            "anchor_mode": anchor_mode,
            "h_mode": h_mode,
            "live_refresh": live_refresh,
        }
        state["signals"] = scan_results[-200:]
        self._save_state(state)
        self._journal({"event": "scan_complete", "summary": state["last_run"]})
        return self._status_payload(state)

    async def _refresh_open_positions(
        self,
        state: dict[str, Any],
        rows: dict[str, dict[str, Any]],
        *,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str,
        h_mode: str,
    ) -> int:
        closed = 0
        remaining: list[dict[str, Any]] = []
        for position in self._open_positions(state):
            row = rows.get(str(position.get("underlying") or "").upper())
            mark = self._mark_from_row(row, str(position.get("option_type") or "")) if row else None
            if mark is None:
                mark = await self._latest_mark(position)
            if mark and mark.get("ltp") is not None:
                position["current_price"] = mark["ltp"]
                position["updated_at"] = mark.get("as_of") or _now()

            close_reason = self._exit_reason(position)
            if close_reason is None:
                close_reason = await self._opposite_signal_reason(
                    position,
                    timeframe=timeframe,
                    lookback_sessions=lookback_sessions,
                    anchor_mode=anchor_mode,
                    h_mode=h_mode,
                )

            if close_reason:
                closed_position = self._close_position(position, close_reason)
                state.setdefault("closed_positions", []).append(closed_position)
                self._journal({"event": "close", "position": closed_position})
                closed += 1
            else:
                remaining.append(position)
        state["open_positions"] = remaining
        state["closed_positions"] = list(state.get("closed_positions") or [])[-500:]
        return closed

    def _scan_decision(self, row: dict[str, Any], snapshot: dict[str, Any], *, min_score: int) -> dict[str, Any]:
        underlying = str(row.get("underlying") or snapshot.get("underlying") or "").upper()
        signal = snapshot.get("signal") or {}
        state = str(signal.get("state") or "")
        score = _safe_int(signal.get("score"), 0)
        option_type = "CE" if state == "bullish_setup" else "PE" if state == "bearish_setup" else ""
        decision = {
            "underlying": underlying,
            "spot_price": snapshot.get("spot_price") or row.get("spot_price"),
            "signal_state": state,
            "signal_bias": signal.get("bias"),
            "signal_score": score,
            "signal_threshold": signal.get("threshold"),
            "signal_reasons": signal.get("reasons") or [],
            "as_of": snapshot.get("as_of") or row.get("as_of") or _now(),
            "decision": "skip",
            "reason": "no_gann_setup",
        }
        if not option_type:
            return decision
        decision["option_type"] = option_type
        if score < min_score:
            decision["reason"] = "score_below_agent_minimum"
            return decision
        option = self._option_from_row(row, option_type)
        if option is None:
            decision["reason"] = "missing_option_quote"
            return decision
        decision.update({"decision": "open", "reason": "gann_setup", "direction": "long_call" if option_type == "CE" else "long_put", "option_type": option_type, "option": option})
        return decision

    async def _option_from_store(self, *, underlying: str, option_type: str, spot_price: float, cfg: dict[str, Any]) -> dict[str, Any] | None:
        try:
            rows = await self.service.store.directional_store.list_live_contract_snapshots(
                underlying=underlying,
                option_type=option_type,
                spot_price=spot_price,
                max_days_to_expiry=float(cfg.get("max_days_to_expiry") or 45.0),
                limit=1,
            )
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        ltp = _safe_float(row.get("ltp"))
        if ltp is None or ltp <= 0:
            return None
        return {
            "underlying": str(row.get("underlying") or underlying).upper(),
            "expiry": row.get("expiry"),
            "strike": row.get("strike"),
            "option_type": str(row.get("option_type") or option_type).upper(),
            "instrument_key": row.get("instrument_key"),
            "trading_symbol": row.get("trading_symbol"),
            "ltp": round(ltp, 2),
            "as_of": row.get("time"),
            "lot_size": row.get("lot_size") or 1,
            "source": row.get("source_broker") or "live_contract_snapshot",
        }

    def _build_position(self, decision: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
        option = decision.get("option") if isinstance(decision.get("option"), dict) else None
        if not option:
            return None
        entry_price = _safe_float(option.get("ltp"))
        if entry_price is None or entry_price <= 0:
            return None
        lot_size = max(1, _safe_int(option.get("lot_size"), 1))
        lots = max(1, _safe_int(cfg.get("lots"), 1))
        stop_loss_pct = max(1.0, _safe_float(cfg.get("stop_loss_pct"), 35.0) or 35.0)
        target_pct = max(1.0, _safe_float(cfg.get("target_pct"), 50.0) or 50.0)
        now = _now()
        return {
            "position_id": uuid.uuid4().hex,
            "status": "open",
            "opened_at": now,
            "updated_at": now,
            "underlying": decision.get("underlying"),
            "direction": decision.get("direction"),
            "option_type": decision.get("option_type"),
            "expiry": option.get("expiry"),
            "strike": option.get("strike"),
            "instrument_key": option.get("instrument_key"),
            "trading_symbol": option.get("trading_symbol") or option.get("instrument_key"),
            "lot_size": lot_size,
            "qty_lots": lots,
            "qty_units": lot_size * lots,
            "entry_price": round(entry_price, 2),
            "current_price": round(entry_price, 2),
            "stop_price": round(entry_price * (1.0 - stop_loss_pct / 100.0), 2),
            "target_price": round(entry_price * (1.0 + target_pct / 100.0), 2),
            "signal_state": decision.get("signal_state"),
            "signal_bias": decision.get("signal_bias"),
            "signal_score": decision.get("signal_score"),
            "signal_threshold": decision.get("signal_threshold"),
            "signal_reasons": decision.get("signal_reasons") or [],
            "spot_price": decision.get("spot_price"),
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
        }

    def _close_position(self, position: dict[str, Any], reason: str) -> dict[str, Any]:
        exit_price = _safe_float(position.get("current_price"), _safe_float(position.get("entry_price"), 0.0)) or 0.0
        entry = _safe_float(position.get("entry_price"), 0.0) or 0.0
        qty_units = max(1, _safe_int(position.get("qty_units"), 1))
        realized = round((exit_price - entry) * qty_units, 2)
        closed = {**position}
        closed.update(
            {
                "status": "closed",
                "closed_at": _now(),
                "updated_at": _now(),
                "exit_price": round(exit_price, 2),
                "close_reason": reason,
                "realized_pnl": realized,
                "unrealized_pnl": 0.0,
            }
        )
        return closed

    def _exit_reason(self, position: dict[str, Any]) -> str | None:
        current = _safe_float(position.get("current_price"))
        stop = _safe_float(position.get("stop_price"))
        target = _safe_float(position.get("target_price"))
        if current is None:
            return None
        if stop is not None and current <= stop:
            return "option_stop_loss"
        if target is not None and current >= target:
            return "option_target"
        return None

    async def _opposite_signal_reason(
        self,
        position: dict[str, Any],
        *,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str,
        h_mode: str,
    ) -> str | None:
        try:
            snapshot = await self.service.live_snapshot(
                str(position.get("underlying") or ""),
                timeframe,
                lookback_sessions,
                anchor_mode,
                h_mode,
            )
        except Exception:
            return None
        signal_state = str((snapshot.get("signal") or {}).get("state") or "")
        option_type = str(position.get("option_type") or "").upper()
        if option_type == "CE" and signal_state == "bearish_setup":
            return "opposite_gann_bearish_setup"
        if option_type == "PE" and signal_state == "bullish_setup":
            return "opposite_gann_bullish_setup"
        return None

    async def _latest_mark(self, position: dict[str, Any]) -> dict[str, Any] | None:
        try:
            mark, as_of, source = await self.service.store.directional_store.latest_local_option_mark(
                underlying=str(position.get("underlying") or ""),
                expiry=str(position.get("expiry") or ""),
                strike=float(position.get("strike") or 0.0),
                option_type=str(position.get("option_type") or ""),
                instrument_key=str(position.get("instrument_key") or ""),
            )
        except Exception:
            return None
        if mark is None:
            return None
        return {"ltp": float(mark), "as_of": as_of, "source": source}

    def _option_from_row(self, row: dict[str, Any], option_type: str) -> dict[str, Any] | None:
        leg = row.get(option_type.lower())
        if not isinstance(leg, dict):
            return None
        ltp = _safe_float(leg.get("ltp"))
        if ltp is None or ltp <= 0:
            return None
        return {
            "underlying": str(row.get("underlying") or "").upper(),
            "expiry": leg.get("expiry") or row.get("expiry"),
            "strike": leg.get("strike") or row.get(f"{option_type.lower()}_atm_strike") or row.get("atm_strike"),
            "option_type": option_type,
            "instrument_key": leg.get("instrument_key"),
            "trading_symbol": leg.get("trading_symbol"),
            "ltp": round(ltp, 2),
            "as_of": leg.get("as_of") or row.get("as_of"),
            "lot_size": row.get("lot_size") or leg.get("lot_size") or 1,
            "source": row.get("live_source") or leg.get("source_broker") or "atm_watchlist",
        }

    def _mark_from_row(self, row: dict[str, Any] | None, option_type: str) -> dict[str, Any] | None:
        if not row:
            return None
        option = self._option_from_row(row, option_type.upper())
        if option is None:
            return None
        return {"ltp": option["ltp"], "as_of": option.get("as_of"), "source": option.get("source")}

    def _dedupe_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for row in rows:
            underlying = str(row.get("underlying") or "").upper()
            if not underlying or underlying in seen:
                continue
            seen.add(underlying)
            deduped.append(row)
        return deduped

    def _position_for_underlying(self, state: dict[str, Any], underlying: str) -> dict[str, Any] | None:
        return next((item for item in self._open_positions(state) if str(item.get("underlying") or "").upper() == underlying.upper()), None)

    def _open_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in state.get("open_positions") or [] if isinstance(item, dict) and item.get("status") == "open"]

    def _status_payload(self, state: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
        open_positions = self._open_positions(state)
        closed_positions = [item for item in state.get("closed_positions") or [] if isinstance(item, dict)]
        for position in open_positions:
            entry = _safe_float(position.get("entry_price"), 0.0) or 0.0
            current = _safe_float(position.get("current_price"), entry) or entry
            qty_units = max(1, _safe_int(position.get("qty_units"), 1))
            position["unrealized_pnl"] = round((current - entry) * qty_units, 2)
        realized = round(sum(_safe_float(item.get("realized_pnl"), 0.0) or 0.0 for item in closed_positions), 2)
        unrealized = round(sum(_safe_float(item.get("unrealized_pnl"), 0.0) or 0.0 for item in open_positions), 2)
        limit = max(1, min(int(limit), 200))
        return {
            "mode": "paper",
            "last_scan_at": state.get("last_scan_at"),
            "last_message": state.get("last_message"),
            "last_run": state.get("last_run") or {},
            "summary": {
                "open_positions": len(open_positions),
                "closed_positions": len(closed_positions),
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "total_pnl": round(realized + unrealized, 2),
            },
            "open_positions": list(reversed(open_positions))[:limit],
            "closed_positions": list(reversed(closed_positions))[:limit],
            "recent_signals": list(reversed(state.get("signals") or []))[:limit],
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "last_scan_at": None,
            "last_message": "Paper agent has not run yet.",
            "last_run": {},
            "open_positions": [],
            "closed_positions": [],
            "signals": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._default_state()
        state = self._default_state()
        state.update(payload if isinstance(payload, dict) else {})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _journal(self, payload: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"recorded_at": _now(), **payload}
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
