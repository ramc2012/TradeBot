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
from market_data.commodity_contract_specs import get_commodity_contract_spec


UTC = timezone.utc


def _commodity_spec(underlying: str):
    """Return the commodity contract spec if `underlying` is an MCX commodity,
    else None. Indices/stocks fall through to the option path."""
    try:
        spec = get_commodity_contract_spec(underlying)
    except Exception:
        return None
    if spec and getattr(spec, "root", "UNKNOWN") not in ("", "UNKNOWN"):
        return spec
    return None


def _today_ist_date() -> str:
    # IST = UTC+5:30; the option expiry check only needs the calendar date.
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date().isoformat()


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
        risk_cfg = self.config.get("risk", {})
        max_positions = max(1, int(risk_cfg.get("max_portfolio_positions") or cfg.get("max_positions") or 12))
        min_score = int(cfg.get("min_score") or self.config.get("signals", {}).get("score_threshold") or 3)

        state = self._load_state()
        # Daily realised-loss circuit breaker — once today's booked losses hit
        # the cap, stop OPENING new trades (existing positions still manage out).
        daily_loss_cap = float(risk_cfg.get("daily_loss_cap") or 0.0)
        today_utc = datetime.now(UTC).date().isoformat()
        realized_today = sum(
            _safe_float(p.get("realized_pnl"), 0.0) or 0.0
            for p in (state.get("closed_positions") or [])
            if isinstance(p, dict) and str(p.get("closed_at") or "")[:10] == today_utc
        )
        loss_capped = daily_loss_cap > 0.0 and realized_today <= -daily_loss_cap

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
        # Commodities aren't in the NSE ATM watchlist — inject synthetic rows so
        # the Gann engine still scans them. They execute as FUTURES (commodity
        # options are no longer ingested), priced off the live commodity spot
        # frame the Gann data layer already loads.
        present = {str(r.get("underlying") or "").upper() for r in rows}
        for sym in sorted(configured_universe):
            if sym in present:
                continue
            if _commodity_spec(sym) is not None:
                rows.append({"underlying": sym, "is_commodity": True})
        if max_underlyings > 0:
            rows = rows[:max_underlyings]

        row_by_underlying = {str(row.get("underlying") or "").upper(): row for row in rows}
        # Per-run snapshot cache. The scan builds a live snapshot for every
        # universe underlying; open-position management then REUSES those rather
        # than re-loading a deep frame per held position (that double-load made
        # run_once blow past the supervisor's 120s timeout). Cleared each run.
        self._run_snapshot_cache = {}

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
            self._run_snapshot_cache[underlying] = snapshot
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
                            "instrument_type": "OPTION",
                            "direction": "long_call" if option["option_type"] == "CE" else "long_put",
                            "option": option,
                        }
                    )
            return decision

        scan_results = await asyncio.gather(*[_scan(row) for row in rows])

        # Manage open positions AFTER the scan so it can reuse cached snapshots.
        closed_count = await self._refresh_open_positions(
            state,
            row_by_underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            anchor_mode=anchor_mode,
            h_mode=h_mode,
        )

        opened = 0
        skipped = 0
        for decision in scan_results:
            if decision.get("decision") != "open":
                skipped += 1
                continue
            if loss_capped:
                decision["decision"] = "skip"
                decision["reason"] = "daily_loss_cap_reached"
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
        risk_cfg = self.config.get("risk", {})
        rev_min = float(self.config.get("strategy", {}).get("reversal_min_conviction", 6.5))
        closed = 0
        remaining: list[dict[str, Any]] = []
        for position in self._open_positions(state):
            underlying = str(position.get("underlying") or "").upper()
            instrument_type = str(position.get("instrument_type") or "OPTION")

            # Reuse the snapshot the scan already built for this underlying
            # (current spot + live signal for the opposite-exit check); only
            # load a fresh one if it wasn't scanned this run.
            snapshot = getattr(self, "_run_snapshot_cache", {}).get(underlying)
            if not snapshot:
                try:
                    snapshot = await self.service.live_snapshot(
                        underlying, timeframe, lookback_sessions, anchor_mode, h_mode
                    )
                except Exception:
                    snapshot = {}
            spot = _safe_float((snapshot or {}).get("spot_price"))
            signal = (snapshot or {}).get("signal") or {}

            # Mark the traded instrument.
            if instrument_type == "FUTURES":
                if spot is not None:
                    position["current_price"] = round(spot, 2)
                    position["updated_at"] = (snapshot or {}).get("as_of") or _now()
            else:
                row = rows.get(underlying)
                mark = self._mark_from_row(row, str(position.get("option_type") or "")) if row else None
                if mark is None:
                    mark = await self._latest_mark(position)
                if mark and mark.get("ltp") is not None:
                    position["current_price"] = mark["ltp"]
                    position["updated_at"] = mark.get("as_of") or _now()

            # Track the underlying, advance break-even / trailing stop, then
            # decide the exit off Gann levels on the underlying.
            self._update_underlying_tracking(position, spot, risk_cfg)
            close_reason = self._risk_exit_reason(position, spot, signal, risk_cfg=risk_cfg, rev_min=rev_min)

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
        side = str(signal.get("side") or "")            # long | short | ""
        archetype = signal.get("archetype")             # continuation | reversal | None
        conviction = _safe_float(signal.get("conviction"), 0.0) or 0.0
        spot = snapshot.get("spot_price") or row.get("spot_price")
        decision = {
            "underlying": underlying,
            "spot_price": spot,
            "signal_state": state,
            "signal_bias": signal.get("bias"),
            "signal_score": _safe_int(round(conviction), 0),
            "signal_threshold": signal.get("threshold"),
            "signal_reasons": signal.get("reasons") or [],
            "regime": signal.get("regime"),
            "archetype": archetype,
            "conviction": round(conviction, 3),
            "size_factor": _safe_float(signal.get("size_factor"), 1.0) or 1.0,
            "stop_underlying": signal.get("stop_underlying"),
            "targets_underlying": signal.get("targets_underlying") or [],
            "risk_per_unit": signal.get("risk_per_unit"),
            "thesis_side": side,
            "as_of": snapshot.get("as_of") or row.get("as_of") or _now(),
            "decision": "skip",
            "reason": "no_gann_setup",
        }
        # The engine only emits a setup state once its (archetype-specific)
        # conviction bar is cleared, so reaching here already means "actionable".
        if archetype not in ("continuation", "reversal") or side not in ("long", "short"):
            return decision

        # Extra conviction floor on top of the engine's continuation bar — the
        # max of the commodity floor (commodities over-trade / are negative-EV
        # at the index bar) and any per-underlying override (e.g. BANKNIFTY).
        # All tuned from the offline 150-day sweep.
        strat_cfg = self.config.get("strategy", {})
        spec = _commodity_spec(underlying)
        extra_floor = float((strat_cfg.get("per_underlying_min_conviction") or {}).get(underlying, 0.0) or 0.0)
        if spec is not None:
            extra_floor = max(extra_floor, float(strat_cfg.get("commodity_min_conviction", 0.0) or 0.0))
        if extra_floor > 0.0 and conviction < extra_floor:
            decision["reason"] = "conviction_floor"
            return decision

        if spec is not None:
            # ── Commodity → FUTURES (options no longer ingested) ────────────
            price = _safe_float(spot)
            if price is None or price <= 0:
                decision["reason"] = "missing_spot_price"
                return decision
            decision.update({
                "decision": "open",
                "reason": "gann_setup",
                "instrument_type": "FUTURES",
                "direction": side,  # long | short
                "futures": {
                    "underlying": underlying,
                    "lot_size": int(getattr(spec, "futures_lot_size", 1) or 1),
                    "price": round(price, 2),
                    "trading_symbol": f"{underlying} FUT",
                    "tick_size": float(getattr(spec, "mp_tick_size", 0.05) or 0.05),
                },
            })
            return decision

        # ── Index → ATM OPTION ──────────────────────────────────────────────
        option_type = "CE" if side == "long" else "PE"
        decision["option_type"] = option_type
        option = self._option_from_row(row, option_type)
        if option is None:
            decision["reason"] = "missing_option_quote"
            return decision
        decision.update({
            "decision": "open",
            "reason": "gann_setup",
            "instrument_type": "OPTION",
            "direction": "long_call" if option_type == "CE" else "long_put",
            "option_type": option_type,
            "option": option,
        })
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
        risk_cfg = self.config.get("risk", {})
        instrument_type = str(decision.get("instrument_type") or "OPTION")
        size_factor = max(0.1, _safe_float(decision.get("size_factor"), 1.0) or 1.0)
        now = _now()
        entry_underlying = _safe_float(decision.get("spot_price"))
        stop_underlying = _safe_float(decision.get("stop_underlying"))
        risk_per_unit = _safe_float(decision.get("risk_per_unit"))
        targets_underlying = [float(t) for t in (decision.get("targets_underlying") or []) if _safe_float(t) is not None]
        thesis_side = str(decision.get("thesis_side") or "")
        eu = round(entry_underlying, 2) if entry_underlying else None

        common: dict[str, Any] = {
            "position_id": uuid.uuid4().hex,
            "status": "open",
            "opened_at": now,
            "updated_at": now,
            "underlying": decision.get("underlying"),
            "instrument_type": instrument_type,
            "archetype": decision.get("archetype"),
            "regime": decision.get("regime"),
            "conviction": decision.get("conviction"),
            "size_factor": size_factor,
            "thesis_side": thesis_side,
            "entry_underlying": eu,
            "stop_underlying": round(stop_underlying, 2) if stop_underlying else None,
            "init_stop_underlying": round(stop_underlying, 2) if stop_underlying else None,
            "targets_underlying": targets_underlying,
            "risk_per_unit": round(risk_per_unit, 2) if risk_per_unit else None,
            "current_underlying": eu,
            "peak_underlying": eu,
            "trough_underlying": eu,
            "bars_held": 0,
            "be_done": False,
            "trail_active": False,
            "signal_state": decision.get("signal_state"),
            "signal_bias": decision.get("signal_bias"),
            "signal_score": decision.get("signal_score"),
            "signal_reasons": decision.get("signal_reasons") or [],
            "spot_price": decision.get("spot_price"),
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
        }

        if instrument_type == "FUTURES":
            fut = decision.get("futures") or {}
            entry_price = _safe_float(fut.get("price"))
            if entry_price is None or entry_price <= 0:
                return None
            lot_size = max(1, _safe_int(fut.get("lot_size"), 1))
            target_notional = float(risk_cfg.get("futures_notional_target", 1500000.0))
            lots = max(1, int(round((target_notional / max(lot_size * entry_price, 1.0)) * size_factor)))
            common.update({
                "direction": str(decision.get("direction") or thesis_side),  # long | short
                "trading_symbol": fut.get("trading_symbol"),
                "tick_size": fut.get("tick_size"),
                "lot_size": lot_size,
                "qty_lots": lots,
                "qty_units": lot_size * lots,
                "entry_price": round(entry_price, 2),
                "current_price": round(entry_price, 2),
                "notional": round(lot_size * lots * entry_price, 2),
                "stop_price": round(stop_underlying, 2) if stop_underlying else None,
                "target_price": round(targets_underlying[0], 2) if targets_underlying else None,
            })
            return common

        # OPTION (index) — always long the premium; thesis side via CE/PE.
        option = decision.get("option") if isinstance(decision.get("option"), dict) else None
        if not option:
            return None
        entry_price = _safe_float(option.get("ltp"))
        if entry_price is None or entry_price <= 0:
            return None
        lot_size = max(1, _safe_int(option.get("lot_size"), 1))
        premium_budget = float(risk_cfg.get("option_premium_budget", 50000.0))
        lots = max(1, int(round((premium_budget / max(lot_size * entry_price, 1.0)) * size_factor)))
        hard_stop_pct = float(risk_cfg.get("option_premium_hard_stop_pct", 55.0))
        common.update({
            "direction": decision.get("direction"),  # long_call | long_put
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
            "premium_hard_stop": round(entry_price * (1.0 - hard_stop_pct / 100.0), 2),
            "stop_price": round(entry_price * (1.0 - hard_stop_pct / 100.0), 2),
            "target_price": None,  # exits run off the underlying Gann levels
        })
        return common

    def _close_position(self, position: dict[str, Any], reason: str) -> dict[str, Any]:
        exit_price = _safe_float(position.get("current_price"), _safe_float(position.get("entry_price"), 0.0)) or 0.0
        entry = _safe_float(position.get("entry_price"), 0.0) or 0.0
        qty_units = max(1, _safe_int(position.get("qty_units"), 1))
        # Futures can be short; options are always long the premium.
        if str(position.get("instrument_type") or "OPTION") == "FUTURES" and str(position.get("direction") or "long") == "short":
            realized = round((entry - exit_price) * qty_units, 2)
        else:
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

    def _update_underlying_tracking(self, position: dict[str, Any], spot: float | None, risk_cfg: dict[str, Any]) -> None:
        """Advance bar count, peak/trough, break-even and trailing stop on the
        UNDERLYING (in R units, so it works identically for options & futures)."""
        position["bars_held"] = _safe_int(position.get("bars_held"), 0) + 1
        if spot is None:
            return
        position["current_underlying"] = round(spot, 2)
        peak = _safe_float(position.get("peak_underlying"), spot) or spot
        trough = _safe_float(position.get("trough_underlying"), spot) or spot
        position["peak_underlying"] = round(max(peak, spot), 2)
        position["trough_underlying"] = round(min(trough, spot), 2)

        entry = _safe_float(position.get("entry_underlying"))
        R = _safe_float(position.get("risk_per_unit"))
        stop = _safe_float(position.get("stop_underlying"))
        if entry is None or R is None or R <= 0 or stop is None:
            return
        side = str(position.get("thesis_side") or "long")
        be_at = float(risk_cfg.get("breakeven_at_r", 1.0))
        trail_start = float(risk_cfg.get("trail_start_r", 1.5))
        if side == "long":
            r_now = (spot - entry) / R
            if not position.get("be_done") and r_now >= be_at:
                stop = max(stop, entry)
                position["stop_underlying"] = round(stop, 2)
                position["be_done"] = True
            peak_r = (position["peak_underlying"] - entry) / R
            if peak_r >= trail_start:
                new_stop = entry + (peak_r - 1.0) * R   # lock 1R behind the high-water mark
                if new_stop > stop:
                    position["stop_underlying"] = round(new_stop, 2)
                    position["trail_active"] = True
        else:
            r_now = (entry - spot) / R
            if not position.get("be_done") and r_now >= be_at:
                stop = min(stop, entry)
                position["stop_underlying"] = round(stop, 2)
                position["be_done"] = True
            trough_r = (entry - position["trough_underlying"]) / R
            if trough_r >= trail_start:
                new_stop = entry - (trough_r - 1.0) * R
                if new_stop < stop:
                    position["stop_underlying"] = round(new_stop, 2)
                    position["trail_active"] = True

    def _risk_exit_reason(
        self,
        position: dict[str, Any],
        spot: float | None,
        signal: dict[str, Any],
        *,
        risk_cfg: dict[str, Any],
        rev_min: float,
    ) -> str | None:
        side = str(position.get("thesis_side") or "long")
        instrument_type = str(position.get("instrument_type") or "OPTION")
        stop = _safe_float(position.get("stop_underlying"))
        targets = position.get("targets_underlying") or []
        entry = _safe_float(position.get("entry_underlying"))
        R = _safe_float(position.get("risk_per_unit"))

        # 1) Gann stop / target on the underlying (BE/trail already applied).
        if spot is not None and stop is not None:
            if side == "long" and spot <= stop:
                return "gann_stop"
            if side == "short" and spot >= stop:
                return "gann_stop"
        if spot is not None and targets:
            t0 = _safe_float(targets[0])
            if t0 is not None:
                if side == "long" and spot >= t0:
                    return "gann_target"
                if side == "short" and spot <= t0:
                    return "gann_target"

        # 2) Option-only backstops: theta hard-stop + expiry-day flat.
        if instrument_type == "OPTION":
            prem = _safe_float(position.get("current_price"))
            hard = _safe_float(position.get("premium_hard_stop"))
            if prem is not None and hard is not None and prem <= hard:
                return "option_premium_stop"
            if risk_cfg.get("option_expiry_day_exit", True):
                exp = str(position.get("expiry") or "")[:10]
                if exp and exp <= _today_ist_date():
                    return "option_expiry"

        # 3) Time stop — drop a position going nowhere.
        time_stop_bars = int(risk_cfg.get("time_stop_bars", 26))
        min_r = float(risk_cfg.get("time_stop_min_r", 0.5))
        if (
            _safe_int(position.get("bars_held"), 0) >= time_stop_bars
            and entry is not None and R is not None and R > 0 and spot is not None
        ):
            r_now = (spot - entry) / R if side == "long" else (entry - spot) / R
            if r_now < min_r:
                return "time_stop"

        # 4) Opposite signal — ONLY a high-conviction (reversal-grade) one. This
        #    is the whipsaw fix: a routine pivot flip no longer closes us out.
        opp_side = str(signal.get("side") or "")
        opp_conv = _safe_float(signal.get("conviction"), 0.0) or 0.0
        if signal.get("archetype") and opp_side and opp_side != side and opp_conv >= rev_min:
            return "opposite_high_conviction"
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
            if str(position.get("instrument_type") or "OPTION") == "FUTURES" and str(position.get("direction") or "long") == "short":
                position["unrealized_pnl"] = round((entry - current) * qty_units, 2)
            else:
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
