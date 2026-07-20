"""Service orchestrator for the directional long-options module."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import lru_cache, partial
from pathlib import Path
from time import monotonic
from typing import Any, Optional

import pandas as pd
from loguru import logger

from agentic_rag import ContextGateRequest, rag_service
from analysis.signal_classifier import classify_status_bucket
from core.config import settings
from directional_options.ai_model import HybridDirectionalOptionsModel
from directional_options.backtest import DirectionalOptionsBacktester
from directional_options.chain_analytics import ensure_chain_tracked, fetch_chain_analytics
from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine
from directional_options.paper import DirectionalOptionsPaperStore
from directional_options.policy import DirectionalPolicy, get_policy
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine
from market_data.market_intelligence_runtime import market_intelligence_runtime


def rotate_batch(symbols: list[str], cursor: int, batch_size: int) -> tuple[list[str], int]:
    """Pick the next rotating batch from `symbols` starting at `cursor`.

    Returns (batch, next_cursor). Wraps around the end of the list so every
    symbol is visited once per ceil(len/batch_size) cycles regardless of
    where the cursor sits. Degenerate inputs (empty list / non-positive
    batch size) return an empty batch and cursor 0. When batch_size >= len,
    the whole list is returned and the cursor stays at 0 (single-batch
    universe — no rotation needed).
    """
    if not symbols or batch_size <= 0:
        return [], 0
    total = len(symbols)
    if batch_size >= total:
        return list(symbols), 0
    start = int(cursor) % total
    end = start + batch_size
    if end <= total:
        batch = list(symbols[start:end])
    else:
        batch = list(symbols[start:]) + list(symbols[: end - total])
    return batch, end % total


def _fresh_quote_time(value: object, *, max_age_seconds: float) -> bool:
    """Return True only for a parseable, current market quote timestamp."""
    if not value:
        return False
    try:
        quote_time = pd.Timestamp(value)
        if quote_time.tzinfo is None:
            quote_time = quote_time.tz_localize("UTC")
        age = (pd.Timestamp.now(tz="UTC") - quote_time.tz_convert("UTC")).total_seconds()
        return 0.0 <= age <= max_age_seconds
    except Exception:
        return False


class DirectionalOptionsService:
    """Expose research, live snapshot, and paper-trading surfaces."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = DirectionalOptionsDataStore(self.config["data_root"])
        self.feature_engine = FeatureEngine(self.config["feature_engine"])
        self.regime = RegimeClassifier()
        self.signals = DirectionalSignalEngine(self.config["signal_engine"])
        self.ai_model = HybridDirectionalOptionsModel(self.config.get("ai_model") or {})
        self.selector = OptionSelectionEngine(self.store, self.config["selector"])
        self.risk = DirectionalOptionsRiskEngine(self.config["risk"])
        rl_cfg = self.config.get("rl_policy") or {}
        self.policy: DirectionalPolicy | None = (
            get_policy(rl_cfg.get("state_path")) if rl_cfg.get("enabled", True) else None
        )
        self.paper = DirectionalOptionsPaperStore(
            self.config["paper_trading"]["journal_root"],
            min_hold_bars=int(self.config["paper_trading"].get("min_hold_bars", 3)),
            one_position_per_symbol=bool(
                self.config["paper_trading"].get("one_position_per_symbol", True)
            ),
            policy=self.policy,
            planned_stop_pct=float(self.config["risk"].get("planned_stop_pct", 0.30)),
            profit_target_pct=float(self.config["risk"].get("profit_target_pct", 0.45)),
            expiry_guard_days=float(self.config["risk"].get("expiry_guard_days", 0.8)),
        )
        self.backtester = DirectionalOptionsBacktester(
            store=self.store,
            feature_engine=self.feature_engine,
            regime=self.regime,
            signals=self.signals,
            selector=self.selector,
            risk=self.risk,
            config=self.config,
            ai_model=self.ai_model,
        )
        self._summary_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
        self._live_cache_ttl_seconds = 30.0
        self._summary_cache_ttl_seconds = 60.0
        self._live_cache: dict[tuple[str, str, int], tuple[float, dict[str, object]]] = {}
        self._live_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        # NIFTY-50 stock expansion (2026-07-17): resolved stock universe cache
        # (static constituents ∩ live F&O catalog) + the rotating scan cursor.
        self._stock_universe_cache: tuple[float, list[str], str] | None = None
        self._stock_universe_cache_ttl_seconds = 3600.0
        self._stock_scan_cursor = 0

    # ── NIFTY-50 stock expansion helpers (2026-07-17) ────────────────────────
    #
    # UNIVERSE SPLIT: indices (config["universe"]) keep the positional-
    # confirmation path (positioning_feed + fail-closed gate). Stocks come
    # from config["stock_universe"] (static dated NIFTY-50 list) intersected
    # with fo_underlying_catalog so every name has listed options, and are
    # evaluated by the standard signal engine only.

    def is_index_underlying(self, underlying: str | None) -> bool:
        return str(underlying or "").upper().strip() in {
            str(item).upper() for item in self.config["universe"]
        }

    async def resolve_runner_universe(self) -> dict[str, Any]:
        """Universe for the supervisor runner: indices + (flag-gated) stocks."""
        indices = [str(item).upper() for item in self.config["universe"]]
        if not settings.DIRECTIONAL_INCLUDE_STOCK_UNIVERSE:
            return {"indices": indices, "stocks": [], "stock_universe_source": "disabled"}
        stocks, source = await self._resolve_stock_universe()
        return {"indices": indices, "stocks": stocks, "stock_universe_source": source}

    async def _resolve_stock_universe(self) -> tuple[list[str], str]:
        """Static NIFTY-50 constituents ∩ live F&O catalog (TTL-cached).

        On a catalog read failure the static list is used as-is: every name
        is still protected downstream by the per-symbol readiness guard
        (no watchlist rows -> skip), so a fail-open trade on an optionless
        symbol is impossible either way.
        """
        cached = self._stock_universe_cache
        if cached is not None and cached[0] > monotonic():
            return list(cached[1]), cached[2]
        indices = {str(item).upper() for item in self.config["universe"]}
        static_list = [
            str(item).upper().strip()
            for item in (self.config.get("stock_universe") or [])
            if str(item).strip() and str(item).upper().strip() not in indices
        ]
        # De-dupe, keep order.
        static_list = list(dict.fromkeys(static_list))
        source = "static_nifty50"
        resolved = static_list
        try:
            catalog = await asyncio.wait_for(self.store.list_fo_stock_symbols(), timeout=10.0)
            if catalog:
                resolved = [symbol for symbol in static_list if symbol in catalog]
                source = "static_nifty50 ∩ fo_underlying_catalog"
        except Exception as exc:  # noqa: BLE001 — catalog outage must not kill the lane
            logger.warning(f"[Directional] F&O catalog intersection unavailable, using static list: {exc}")
            source = "static_nifty50 (catalog_unavailable)"
        self._stock_universe_cache = (
            monotonic() + self._stock_universe_cache_ttl_seconds,
            list(resolved),
            source,
        )
        return list(resolved), source

    async def filter_ready_stock_symbols(
        self, symbols: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        """Split stocks into (ready, skipped{symbol: reason}).

        Ready = fresh 1-minute spot bars AND a live ATM watchlist row inside
        the stock quote-honesty window. A readiness DB failure fails CLOSED:
        every symbol is skipped-and-reported for the cycle (indices are
        unaffected — they don't pass through this filter).
        """
        if not symbols:
            return [], {}
        spot_limit = float(self.config["paper_trading"]["stale_watchlist_seconds"])
        quote_limit = float(settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS)
        try:
            readiness = await asyncio.wait_for(
                self.store.stock_readiness(
                    symbols,
                    spot_max_age_seconds=spot_limit,
                    watchlist_max_age_seconds=quote_limit,
                ),
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Directional] stock readiness query failed; skipping stock batch: {exc}")
            return [], {str(symbol).upper(): f"readiness_check_failed: {exc}" for symbol in symbols}
        ready: list[str] = []
        skipped: dict[str, str] = {}
        for symbol in symbols:
            info = readiness.get(str(symbol).upper()) or {}
            if not info.get("latest_spot_time"):
                skipped[symbol] = "no_recent_spot_bars"
            elif not info.get("spot_fresh"):
                skipped[symbol] = f"spot_stale_{int(info.get('spot_age_seconds') or 0)}s"
            elif not int(info.get("watchlist_rows") or 0):
                skipped[symbol] = "no_atm_watchlist_rows"
            elif not info.get("watchlist_fresh"):
                skipped[symbol] = f"option_quotes_stale_{int(info.get('watchlist_age_seconds') or 0)}s"
            else:
                ready.append(symbol)
        return ready, skipped

    def next_stock_batch(self, ready_symbols: list[str]) -> list[str]:
        """Rotating per-cycle batch over the READY stocks.

        Membership can change between cycles (readiness is re-evaluated each
        pass); the cursor simply advances modulo the current list length so
        every ready name is visited within ceil(len/batch) cycles.
        """
        batch, self._stock_scan_cursor = rotate_batch(
            ready_symbols,
            self._stock_scan_cursor,
            int(settings.DIRECTIONAL_STOCK_BATCH_SIZE),
        )
        return batch

    def summary(self) -> dict[str, object]:
        cached_payload = self._summary_cache.get("payload")
        if cached_payload is not None and float(self._summary_cache.get("expires_at") or 0.0) > monotonic():
            return cached_payload

        from core.market_hours_paper_supervisor import market_hours_paper_supervisor
        from directional_options.dashboard import get_dashboard_mount_state

        # Universe is restricted to NSE index underlyings — NIFTY / BANKNIFTY
        # / SENSEX. Commodity paths were removed in the RL refactor.
        data_underlyings = set(self.store.available_underlyings())
        available = [item for item in self.config["universe"] if item in data_underlyings]
        if not available and (settings.PAPER_TRADING_ONLY or settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY):
            available = list(self.config["universe"])
        automation = market_hours_paper_supervisor.get_runner_status("directional_options")
        payload = {
            "key": "directional_long_options",
            "label": self.config["label"],
            "description": self.config["description"],
            "underlyings": available,
            "timeframes": list(self.config["timeframes"]),
            "dashboard": get_dashboard_mount_state(),
            "auto_started": bool(automation.get("enabled") and automation.get("loop_active")),
            "automation": automation,
            "coverage": [self.store.coverage_summary(underlying) for underlying in available],
            # NIFTY-50 stock expansion metadata (2026-07-17). The static list
            # here is pre-intersection; the runner resolves the tradable set
            # (∩ F&O catalog + per-symbol readiness) each cycle.
            "stock_universe": {
                "enabled": bool(settings.DIRECTIONAL_INCLUDE_STOCK_UNIVERSE),
                "static_size": len(self.config.get("stock_universe") or []),
            },
        }
        self._summary_cache = {
            "payload": payload,
            "expires_at": monotonic() + self._summary_cache_ttl_seconds,
        }
        return payload

    @staticmethod
    def _is_supported_commodity(underlying: str) -> bool:
        # Retained as a no-op for backwards compatibility with any
        # external caller; commodity branches are out of scope after
        # the RL/indices-only refactor.
        return False

    @lru_cache(maxsize=24)
    def workspace(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        summary = self.summary()
        spot = self.store.load_spot_frame(underlying)
        latest_tradeable = self.store.latest_tradeable_timestamp(underlying)
        if latest_tradeable is not None:
            spot = spot.loc[spot["time"] <= latest_tradeable].reset_index(drop=True)
        feature_frame = self.feature_engine.build_frame(
            spot,
            timeframe,
            lookback_sessions=lookback_sessions,
        )
        snapshot = self._snapshot(underlying=underlying, timeframe=timeframe, feature_frame=feature_frame)
        backtest = self.backtester.run(
            underlying=underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            feature_frame=feature_frame,
        )
        return {
            "module": summary,
            "selection": {
                "underlying": underlying,
                "timeframe": timeframe,
                "lookback_sessions": lookback_sessions,
            },
            "snapshot": snapshot,
            "backtest": backtest,
        }

    async def live_snapshot(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        cache_key = (underlying, timeframe, lookback_sessions)
        cached = self._live_cache.get(cache_key)
        if cached and cached[0] > monotonic():
            return cached[1]

        lock = self._live_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._live_cache.get(cache_key)
            if cached and cached[0] > monotonic():
                return cached[1]

            summary = self.summary()
            lookback_days = max(int(self.config["paper_trading"]["live_lookback_days"]), lookback_sessions)
            try:
                # NSE indices are local-DB hits — the 30s timeout is
                # generous to tolerate a cold-cache fetch on first launch.
                spot, history_source, history_symbol = await asyncio.wait_for(
                    self.store.load_live_spot_frame(
                        underlying,
                        lookback_days=lookback_days,
                    ),
                    timeout=30.0,
                )
                feature_frame = await asyncio.to_thread(
                    self.feature_engine.build_frame,
                    spot,
                    timeframe,
                    lookback_sessions=lookback_sessions,
                )
            except Exception as exc:
                payload = await self._degraded_live_payload(
                    summary=summary,
                    underlying=underlying,
                    timeframe=timeframe,
                    lookback_sessions=lookback_sessions,
                    reason="spot_history_unavailable",
                    detail=str(exc),
                )
                self._live_cache[cache_key] = (monotonic() + self._live_cache_ttl_seconds, payload)
                return payload
            try:
                strategy_health = await asyncio.wait_for(market_intelligence_runtime.get_strategy_health(), timeout=3.0)
            except Exception as exc:
                strategy_health = {
                    "ready": False,
                    "watchlist_rows_today": 0,
                    "latest_watchlist_time": None,
                    "watchlist_age_seconds": None,
                    "latest_spot_rows": {},
                    "error": str(exc),
                }
            snapshot = await self._live_snapshot(
                underlying=underlying,
                timeframe=timeframe,
                feature_frame=feature_frame,
                strategy_health=strategy_health,
                history_source=history_source,
                history_symbol=history_symbol,
            )
            payload = {
                "module": summary,
                "selection": {
                    "underlying": underlying,
                    "timeframe": timeframe,
                    "lookback_sessions": lookback_sessions,
                },
                "snapshot": snapshot,
                "paper_positions": await self.paper.list_positions(symbol=underlying, status="all", limit=8),
                "paper_journal": await self.paper.list_journal(symbol=underlying, limit=8),
            }
            self._live_cache[cache_key] = (monotonic() + self._live_cache_ttl_seconds, payload)
            return payload

    async def _degraded_live_payload(
        self,
        *,
        summary: dict[str, object],
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        reason: str,
        detail: str,
    ) -> dict[str, object]:
        snapshot = {
            "as_of": None,
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": None,
            "feature_snapshot": None,
            "regime": None,
            "signal": None,
            "selected_contract": None,
            "contract_candidates": [],
            "risk": None,
            "rag_context": None,
            "selection_reason": detail or reason.replace("_", " "),
            "data_status": {
                "history_source": "none",
                "history_symbol": underlying,
                "latest_watchlist_time": None,
                "watchlist_age_seconds": None,
                "watchlist_rows_today": 0,
                "watchlist_rows_latest": 0,
                "readiness_mode": "degraded",
                "latest_spot_time": None,
                "spot_age_seconds": None,
                "execution_ready": False,
                "degraded_reason": reason,
                "detail": detail,
            },
            "history_source": "none",
            "history_symbol": underlying,
        }
        return {
            "module": summary,
            "selection": {
                "underlying": underlying,
                "timeframe": timeframe,
                "lookback_sessions": lookback_sessions,
            },
            "snapshot": snapshot,
            "paper_positions": await self.paper.list_positions(symbol=underlying, status="all", limit=8),
            "paper_journal": await self.paper.list_journal(symbol=underlying, limit=8),
        }

    async def record_paper_snapshot(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        payload = await self.live_snapshot(underlying, timeframe, lookback_sessions)
        selected = dict(payload.get("snapshot", {}).get("selected_contract") or {})
        if selected:
            entry_premium, entry_mark_time, entry_source = await self.store.latest_local_option_mark(
                underlying=str(underlying),
                expiry=str(selected.get("expiry") or ""),
                strike=float(selected.get("strike") or 0.0),
                option_type=str(selected.get("option_type") or ""),
                instrument_key=str(selected.get("instrument_key") or "") or None,
                allow_history_fallback=False,
            )
            max_entry_age = float(
                settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS
                if not self.is_index_underlying(underlying)
                else self.config["paper_trading"]["stale_watchlist_seconds"]
            )
            if (
                entry_premium is None
                or entry_premium <= 0
                or not _fresh_quote_time(entry_mark_time, max_age_seconds=max_entry_age)
            ):
                snapshot = payload["snapshot"]
                snapshot["data_status"]["execution_ready"] = False
                snapshot["data_status"]["degraded_reason"] = "selected_contract_quote_stale"
                if snapshot.get("risk"):
                    snapshot["risk"]["approved"] = False
                    reasons = list(snapshot["risk"].get("reasons") or [])
                    reasons.append("Exact selected contract has no fresh executable quote.")
                    snapshot["risk"]["reasons"] = reasons
                snapshot["selection_reason"] = "Exact selected contract has no fresh executable quote."
            else:
                selected["option_price"] = float(entry_premium)
                selected["price_source"] = entry_source
                selected["quote_time"] = entry_mark_time
                payload["snapshot"]["selected_contract"] = selected

        open_positions = await self.paper.list_positions(symbol=underlying, status="open", limit=50)
        position_marks: dict[str, dict[str, object]] = {}
        for row in open_positions.get("open_positions", []):
            row_underlying = str(row.get("underlying") or underlying)
            row_expiry = str(row.get("expiry") or "")
            row_strike = float(row.get("strike") or 0.0)
            row_otype = str(row.get("option_type") or "")
            premium, mark_time, price_source = await self.store.latest_local_option_mark(
                underlying=row_underlying,
                expiry=row_expiry,
                strike=row_strike,
                option_type=row_otype,
                instrument_key=str(row.get("instrument_key") or "") or None,
                allow_history_fallback=False,
            )
            max_mark_age = float(
                settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS
                if not self.is_index_underlying(row_underlying)
                else self.config["paper_trading"]["stale_watchlist_seconds"]
            )
            if (
                premium is None
                or premium <= 0
                or not _fresh_quote_time(mark_time, max_age_seconds=max_mark_age)
            ):
                # The held contract often isn't on the fresh WS watchlist feed
                # (e.g. a monthly strike rotated out), so the local mark is
                # missing or stale. Fall back to the option-chain cache, which
                # carries every strike's LTP (~30s fresh — LIVE data, unlike
                # the history fallback excluded above). Without this the
                # position's mark FREEZES at entry — every trade then closes
                # at exit==entry == ₹0 realized P&L (27 such ₹0 trades on
                # 2026-06-04) and the protective stop/target can never fire.
                from directional_options.chain_analytics import chain_strike_mark
                try:
                    chain_mark = await chain_strike_mark(row_underlying, row_expiry, row_strike, row_otype)
                except Exception:  # noqa: BLE001
                    chain_mark = None
                if chain_mark is None or chain_mark <= 0:
                    continue
                premium = float(chain_mark)
                mark_time = None
                price_source = "chain_cache_live"
            position_marks[str(row.get("position_id") or "")] = {
                "premium": premium,
                "spot": float(payload["snapshot"].get("spot_price") or row.get("latest_spot") or 0.0),
                "mark_time": mark_time,
                "price_source": price_source,
            }
        # Bug A guard (2026-06-04): record_paper_snapshot is also reachable via
        # the (ungated) API endpoint, so a UI poll / request after 15:30 could
        # open a position on the frozen post-close heartbeat — which is how a
        # SENSEX CE opened at 15:45 IST. Only allow NEW entries when the
        # underlying's exchange is in session; existing positions are still
        # marked + managed regardless.
        from core.trading_calendar import trading_calendar
        exchange = "BSE" if str(underlying).upper() in ("SENSEX", "BANKEX") else "NSE"
        allow_entries = trading_calendar.is_exchange_open(exchange)
        paper_summary = await self.paper.sync_snapshot(
            payload, position_marks=position_marks, allow_entries=allow_entries
        )
        payload["paper_summary"] = paper_summary
        payload["paper_positions"] = await self.paper.list_positions(symbol=underlying, status="all", limit=8)
        payload["paper_journal"] = await self.paper.list_journal(symbol=underlying, limit=8)
        return payload

    async def paper_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, object]:
        return await self.paper.list_journal(symbol=symbol, limit=limit)

    async def paper_positions(
        self,
        symbol: str | None = None,
        status: str = "all",
        limit: int = 50,
    ) -> dict[str, object]:
        return await self.paper.list_positions(symbol=symbol, status=status, limit=limit)

    async def paper_summary(self) -> dict[str, object]:
        """Capital + P&L snapshot — matches AI/FMP/S1/S2 shape so the
        frontend portfolio panel renders uniformly across all lanes."""
        return await self.paper.capital_status()

    async def reset_paper_account(self, *, actor: str | None = None) -> dict[str, object]:
        """Archive the JSON book and restore the funded baseline."""
        result = await self.paper.reset_account(actor=actor)
        self._summary_cache = {"payload": None, "expires_at": 0.0}
        return result

    def _snapshot(
        self,
        *,
        underlying: str,
        timeframe: str,
        feature_frame: pd.DataFrame,
    ) -> dict[str, object]:
        if feature_frame.empty:
            return {
                "as_of": None,
                "reason": "No spot candles were available for the selected symbol/timeframe.",
            }

        row = feature_frame.iloc[-1]
        timestamp = pd.Timestamp(row["time"])
        spot_price = float(row["close"])
        feature_snapshot = self.feature_engine.snapshot(row)
        regime = self.regime.classify(row, timeframe=timeframe)
        # Research/workspace path: no positioning feed here, so with the
        # positional flag ON the index-scoped fail-closed gate yields no
        # signal for indices (unchanged); stocks route through the standard
        # engine (2026-07-17 NIFTY-50 expansion).
        signal = self.signals.predict(row, regime, timeframe, underlying=underlying)

        selection_reason = "Regime is not tradeable."
        candidate_payload: dict[str, object] | None = None
        candidates_payload: list[dict[str, object]] = []
        risk_payload: dict[str, object] | None = None
        rag_context: dict[str, Any] | None = None
        policy_payload: dict[str, Any] | None = None
        if signal is not None:
            selection = self.selector.select(
                underlying=underlying,
                timestamp=timestamp,
                spot_price=spot_price,
                row=row,
                signal=signal,
                regime=regime,
                timeframe=timeframe,
            )
            selection_reason = selection["reason"]
            chosen, policy_payload = self._policy_pick(
                signal=signal,
                regime=regime,
                row=row,
                candidates=selection["candidates"] or ([selection["best"]] if selection["best"] is not None else []),
                default=selection["best"],
            )
            candidates_payload = self._candidate_payloads(selection["candidates"], policy_payload)
            size_mult = float((policy_payload or {}).get("size_multiplier", 1.0))
            policy_act = bool((policy_payload or {}).get("act", True))
            if chosen is not None:
                candidate_payload = asdict(chosen)
                self._attach_selected_candidate_model(candidate_payload, policy_payload)
                risk_payload = asdict(
                    self.risk.approve(
                        candidate=chosen,
                        signal=signal,
                        equity=float(self.config["risk"]["starting_equity"]),
                        size_multiplier=size_mult,
                    )
                )
                if not policy_act:
                    risk_payload["approved"] = False
                    reasons = list(risk_payload.get("reasons") or [])
                    reasons.append((policy_payload or {}).get("reason") or "Policy declined to trade this state.")
                    risk_payload["reasons"] = reasons
                    selection_reason = (policy_payload or {}).get("reason") or selection_reason
                rag_context = self._build_rag_context(
                    underlying=underlying,
                    symbol=chosen.trading_symbol,
                    signal=signal,
                    regime=regime,
                    candidate=candidate_payload,
                    risk_payload=risk_payload,
                    data_context={
                        "timeframe": timeframe,
                        "spot_price": spot_price,
                        "rv_annualized": float(row.get("rv_annualized", 0.0)),
                        "rv_percentile": float(row.get("rv_percentile", 0.0)),
                    },
                )
                self._apply_rag_context_to_risk(rag_context, risk_payload)

        risk_approved = bool((risk_payload or {}).get("approved"))
        if risk_approved and candidate_payload is not None:
            lane_status = "entry-ready"
        elif signal is not None and candidate_payload is not None:
            lane_status = "trend-aligned"
        elif signal is not None:
            lane_status = "watching"
        else:
            lane_status = "waiting"
        bucket_info = classify_status_bucket(has_position=False, status=lane_status)
        return {
            "as_of": timestamp.isoformat(),
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": round(spot_price, 2),
            "feature_snapshot": asdict(feature_snapshot),
            "regime": asdict(regime),
            "signal": asdict(signal) if signal is not None else None,
            "selected_contract": candidate_payload,
            "contract_candidates": candidates_payload,
            "risk": risk_payload,
            "rag_context": rag_context,
            "policy": policy_payload,
            "selection_reason": selection_reason,
            **bucket_info,
        }

    async def _loss_cap_realized(self) -> Optional[tuple[float, float]]:
        """(today, trailing-7d) realized PnL for the risk engine's loss caps.

        60s cache — one DB query per scan cycle, not per candidate. Returns
        None on DB failure so the live path can fail SAFE (decline new
        entries) instead of passing 0.0, which can never breach a cap.
        """
        cached = getattr(self, "_loss_cap_cache", None)
        if cached is not None and monotonic() - cached[0] < 60.0:
            return cached[1]
        try:
            windows = await self.paper.realized_pnl_windows()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Directional] loss-cap realized-PnL fetch failed: {exc}")
            windows = None
        self._loss_cap_cache = (monotonic(), windows)
        return windows

    async def _live_snapshot(
        self,
        *,
        underlying: str,
        timeframe: str,
        feature_frame: pd.DataFrame,
        strategy_health: dict[str, Any],
        history_source: str,
        history_symbol: str,
    ) -> dict[str, object]:
        if feature_frame.empty:
            data_status = self._build_live_data_status(
                underlying=underlying,
                feature_frame=feature_frame,
                strategy_health=strategy_health,
                history_source=history_source,
                history_symbol=history_symbol,
            )
            data_status["execution_ready"] = False
            data_status["degraded_reason"] = "missing_spot_history"
            return {
                "as_of": None,
                "underlying": underlying,
                "timeframe": timeframe,
                "spot_price": None,
                "feature_snapshot": None,
                "regime": None,
                "signal": None,
                "selected_contract": None,
                "contract_candidates": [],
                "risk": None,
                "selection_reason": "No local spot candles were available for the selected symbol/timeframe.",
                "data_status": data_status,
            }

        row = feature_frame.iloc[-1]
        timestamp = pd.Timestamp(row["time"])
        spot_price = float(row["close"])
        feature_snapshot = self.feature_engine.snapshot(row)
        regime = self.regime.classify(row, timeframe=timeframe)
        positioning = None
        if settings.DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED and self.is_index_underlying(underlying):
            # Daily option-positioning context (PCR / oi_build / HTF / vol) that the
            # positional view confirms its side with — INDEX-ONLY by design: the
            # feed is index-scoped and the fail-closed missing-row gate must not
            # silently kill stock underlyings (they use the standard engine).
            try:
                from directional_options.positioning_feed import latest as _positioning_latest
                positioning = await _positioning_latest(underlying)
            except Exception:
                positioning = None
        signal = self.signals.predict(
            row, regime, timeframe, positioning=positioning, underlying=underlying
        )
        if signal is None and not int(
            strategy_health.get("watchlist_rows_today")
            or strategy_health.get("watchlist_rows_latest")
            or 0
        ):
            try:
                local_watchlist_status = await self.store.latest_live_watchlist_status(
                    underlying=underlying,
                    as_of=timestamp,
                )
            except Exception as exc:  # noqa: BLE001 — a DB blip must not kill the cycle
                logger.warning(f"[Directional] local watchlist status read failed: {exc}")
                local_watchlist_status = {}
            if int(local_watchlist_status.get("rows") or 0) > 0:
                latest_watchlist_time = local_watchlist_status.get("latest_time")
                strategy_health = {
                    **strategy_health,
                    "ready": True,
                    "readiness_mode": strategy_health.get("readiness_mode") or "local_watchlist_snapshot",
                    "latest_watchlist_time": strategy_health.get("latest_watchlist_time") or latest_watchlist_time,
                    "watchlist_rows_today": int(local_watchlist_status.get("rows") or 0),
                    "watchlist_rows_latest": int(local_watchlist_status.get("rows") or 0),
                }
                if latest_watchlist_time:
                    latest_ts = pd.Timestamp(latest_watchlist_time)
                    if latest_ts.tzinfo is None:
                        latest_ts = latest_ts.tz_localize("UTC")
                    strategy_health["watchlist_age_seconds"] = max(
                        0.0,
                        (pd.Timestamp.utcnow() - latest_ts).total_seconds(),
                    )
        data_status = self._build_live_data_status(
            underlying=underlying,
            feature_frame=feature_frame,
            strategy_health=strategy_health,
            history_source=history_source,
            history_symbol=history_symbol,
        )

        selection_reason = "Regime is not tradeable."
        candidate_payload: dict[str, object] | None = None
        candidates_payload: list[dict[str, object]] = []
        risk_payload: dict[str, object] | None = None
        rag_context: dict[str, Any] | None = None
        policy_payload: dict[str, Any] | None = None
        chain_payload: dict[str, Any] | None = None

        snapshot_rows: list[dict[str, Any]] = []
        option_snapshot_lookup_failed = False
        if signal is not None:
            try:
                snapshot_rows = await asyncio.wait_for(
                    self.store.list_live_contract_snapshots(
                        underlying=underlying,
                        option_type=signal.direction,
                        spot_price=spot_price,
                        as_of=timestamp,
                        max_days_to_expiry=float(self.config["selector"]["max_days_to_expiry"]),
                    ),
                    timeout=4.0,
                )
            except Exception as exc:
                option_snapshot_lookup_failed = True
                selection_reason = f"Live option snapshot lookup timed out or failed: {exc}"
                snapshot_rows = []
            if snapshot_rows and not bool(data_status.get("execution_ready")):
                # Only override the staleness gate when the snapshot rows are
                # themselves FRESH — mere row presence must not re-enable
                # entries: the snapshot query has no lower time bound, so
                # day-old rows would otherwise negate the stale-watchlist gate
                # during exactly the feed outages it was built for.
                latest_option_time = max(str(item.get("time") or "") for item in snapshot_rows)
                stale_limit = float(self.config["paper_trading"]["stale_watchlist_seconds"])
                snapshots_fresh = False
                try:
                    latest_dt = pd.Timestamp(latest_option_time)
                    if latest_dt.tzinfo is None:
                        latest_dt = latest_dt.tz_localize("UTC")
                    age = (pd.Timestamp.now(tz="UTC") - latest_dt.tz_convert("UTC")).total_seconds()
                    snapshots_fresh = 0 <= age <= stale_limit
                except Exception:
                    snapshots_fresh = False
                if snapshots_fresh:
                    data_status.update(
                        {
                            "latest_watchlist_time": latest_option_time or data_status.get("latest_watchlist_time"),
                            "watchlist_rows_latest": max(
                                int(data_status.get("watchlist_rows_latest") or 0),
                                len(snapshot_rows),
                            ),
                            "execution_ready": True,
                            "degraded_reason": None,
                        }
                    )
            if (
                snapshot_rows
                and not self.is_index_underlying(underlying)
                and bool(data_status.get("execution_ready"))
            ):
                # STOCK quote-honesty guard (2026-07-17): the global MI health
                # metrics that feed execution_ready are dominated by the ~35s
                # index watchlist refresh, but each STOCK's own rows only
                # refresh via the round-robin premium top-up. Never open a
                # stock entry against an option quote older than the stock
                # freshness bound — fail CLOSED for this cycle instead.
                quote_limit = float(settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS)
                latest_option_time = max(str(item.get("time") or "") for item in snapshot_rows)
                stock_quotes_fresh = False
                try:
                    latest_dt = pd.Timestamp(latest_option_time)
                    if latest_dt.tzinfo is None:
                        latest_dt = latest_dt.tz_localize("UTC")
                    age = (pd.Timestamp.now(tz="UTC") - latest_dt.tz_convert("UTC")).total_seconds()
                    stock_quotes_fresh = 0 <= age <= quote_limit
                except Exception:
                    stock_quotes_fresh = False
                if not stock_quotes_fresh:
                    data_status.update(
                        {
                            "execution_ready": False,
                            "degraded_reason": "stock_option_quotes_stale",
                        }
                    )

        if not bool(data_status.get("execution_ready")):
            selection_reason = (
                str(data_status.get("degraded_reason") or "Local market-intelligence data is not fresh enough for paper execution.")
                .replace("_", " ")
            )
        elif signal is not None:
            # CPU-bound contract scoring (Black-Scholes greeks +
            # distributional metrics per candidate, pure Python math).
            # Offloaded to a worker thread so the per-candidate
            # transcendental math does not block the event loop /health.
            # select_from_live_snapshots has zero awaits and only reads
            # read-only self.config + the passed snapshot_rows, so it is
            # safe on a worker thread and byte-identical in behaviour.
            selection = await asyncio.to_thread(
                partial(
                    self.selector.select_from_live_snapshots,
                    underlying=underlying,
                    timestamp=timestamp,
                    spot_price=spot_price,
                    row=row,
                    signal=signal,
                    regime=regime,
                    timeframe=timeframe,
                    snapshot_rows=snapshot_rows,
                )
            )
            if not option_snapshot_lookup_failed:
                selection_reason = selection["reason"]
            # Pull chain analytics for the selected expiry. Fire-and-
            # tolerate-failure: a missing chain payload just means the
            # policy gets sentinel zeros for chain features and falls
            # back to signal+candidate context alone.
            #
            # INDEX-ONLY (2026-07-17): registering every NIFTY-50 stock with
            # the option_chain_service poll loop would add ~50 broker chain
            # fetches per 30s — the exact REST-starvation class the broker
            # fail-safe work removed. Stocks run without chain analytics
            # (policy sees sentinel zeros, same as any chain-cache miss).
            try:
                chain_expiry = None
                if not self.is_index_underlying(underlying):
                    raise LookupError("chain analytics is index-only")
                best_cand = selection.get("best")
                if best_cand is not None:
                    chain_expiry = getattr(best_cand, "expiry", None)
                # Ensure the option_chain_service is tracking this
                # (underlying, expiry) so its 30s poll loop keeps the
                # Redis cache warm. Idempotent — second+ calls return
                # without touching the broker. Fire-and-forget so the
                # tracking call never blocks the snapshot.
                if chain_expiry:
                    try:
                        asyncio.create_task(ensure_chain_tracked(underlying, chain_expiry))
                    except RuntimeError:
                        pass
                # 6s budget — fetch_chain_analytics itself bounds the
                # Redis read at 2s and does ~1s of feature aggregation;
                # we want enough headroom to absorb backend load spikes
                # without dropping chain context from the snapshot.
                # When the backend is calm this resolves in <1s.
                chain_payload = await asyncio.wait_for(
                    fetch_chain_analytics(underlying, expiry=chain_expiry),
                    timeout=6.0,
                )
            except Exception:
                chain_payload = None
            # CPU-bound RL policy ranking + decision (per-candidate
            # BayesianRidge sampling -> numpy matrix inversion). Offloaded
            # to a worker thread; rank_candidates/decide already serialize
            # all shared-state access under self.policy._lock, so running
            # the call off-loop keeps the same lock and is safe. Thompson
            # sampling is stochastic by design, so RNG draw ordering is
            # not an observable behaviour change.
            chosen, policy_payload = await asyncio.to_thread(
                partial(
                    self._policy_pick,
                    signal=signal,
                    regime=regime,
                    row=row,
                    candidates=selection["candidates"] or ([selection["best"]] if selection["best"] is not None else []),
                    default=selection["best"],
                    chain=chain_payload,
                )
            )
            candidates_payload = self._candidate_payloads(selection["candidates"], policy_payload)
            size_mult = float((policy_payload or {}).get("size_multiplier", 1.0))
            policy_act = bool((policy_payload or {}).get("act", True))
            if chosen is not None:
                candidate_payload = asdict(chosen)
                self._attach_selected_candidate_model(candidate_payload, policy_payload)
                loss_windows = await self._loss_cap_realized()
                risk_payload = asdict(
                    self.risk.approve(
                        candidate=chosen,
                        signal=signal,
                        equity=float(self.config["risk"]["starting_equity"]),
                        size_multiplier=size_mult,
                        daily_realized=loss_windows[0] if loss_windows else 0.0,
                        weekly_realized=loss_windows[1] if loss_windows else 0.0,
                    )
                )
                # OWNER DIRECTIVE 2026-07-17 (signal validation, paper-only):
                # with the loss caps themselves skipped in risk.approve(),
                # failing CLOSED on a loss-cap DB fetch error is pointless —
                # don't decline. Set SIGNAL_VALIDATION_UNCAPPED=False to
                # restore the fail-safe decline together with the caps.
                if loss_windows is None and not settings.SIGNAL_VALIDATION_UNCAPPED:
                    risk_payload["approved"] = False
                    reasons = list(risk_payload.get("reasons") or [])
                    reasons.append("Loss-cap state unavailable (DB error); declining new entries this cycle.")
                    risk_payload["reasons"] = reasons
                if not policy_act:
                    risk_payload["approved"] = False
                    reasons = list(risk_payload.get("reasons") or [])
                    reasons.append((policy_payload or {}).get("reason") or "Policy declined to trade this state.")
                    risk_payload["reasons"] = reasons
                    selection_reason = (policy_payload or {}).get("reason") or selection_reason
                rag_context = await self._build_rag_context_async(
                    underlying=underlying,
                    symbol=chosen.trading_symbol,
                    signal=signal,
                    regime=regime,
                    candidate=candidate_payload,
                    risk_payload=risk_payload,
                    data_context={
                        "timeframe": timeframe,
                        "spot_price": spot_price,
                        "rv_annualized": float(row.get("rv_annualized", 0.0)),
                        "rv_percentile": float(row.get("rv_percentile", 0.0)),
                        "history_source": history_source,
                        "price_source": chosen.price_source,
                    },
                )
                self._apply_rag_context_to_risk(rag_context, risk_payload)

        return {
            "as_of": timestamp.isoformat(),
            "positional": bool(settings.DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED and positioning is not None),
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": round(spot_price, 2),
            "feature_snapshot": asdict(feature_snapshot),
            "regime": asdict(regime),
            "signal": asdict(signal) if signal is not None else None,
            "selected_contract": candidate_payload,
            "contract_candidates": candidates_payload,
            "risk": risk_payload,
            "rag_context": rag_context,
            "policy": policy_payload,
            "chain_analytics": chain_payload,
            "selection_reason": selection_reason,
            "data_status": data_status,
            "history_source": history_source,
            "history_symbol": history_symbol,
        }

    def _policy_pick(
        self,
        *,
        signal,
        regime,
        row,
        candidates: list,
        default,
        chain: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        """Run the RL policy over the surfaced candidates.

        Returns (chosen_candidate, policy_payload). When the policy is
        disabled or there are no candidates, returns (default, None) so
        the rest of the service degrades gracefully. `chain` is the
        chain-analytics payload (None when no chain is cached).
        """
        if self.policy is None or not candidates:
            return default, None
        signal_dict = asdict(signal)
        regime_dict = asdict(regime)
        candidates_dicts: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_dict = asdict(candidate)
            rule_payload = self.ai_model.evaluate(
                row=row,
                signal=signal_dict,
                regime=regime_dict,
                candidate=candidate_dict,
                chain=chain,
            ).to_payload()
            candidate_dict["ai_model"] = rule_payload
            candidates_dicts.append(candidate_dict)
        best_idx, samples = self.policy.rank_candidates(
            signal=signal_dict,
            candidates=candidates_dicts,
            regime=regime_dict,
            chain=chain,
        )
        if best_idx is None:
            return default, None
        eligible = [
            idx
            for idx, candidate in enumerate(candidates_dicts)
            if bool((candidate.get("ai_model") or {}).get("allowed"))
        ]
        if eligible and not bool((candidates_dicts[best_idx].get("ai_model") or {}).get("allowed")):
            best_idx = max(eligible, key=lambda idx: samples[idx] if idx < len(samples) else float("-inf"))
        chosen = candidates[best_idx]
        decision = self.policy.decide(
            signal=signal_dict,
            candidate=candidates_dicts[best_idx],
            regime=regime_dict,
            chain=chain,
        )
        rule_payload = dict(candidates_dicts[best_idx].get("ai_model") or {})
        rule_allowed = bool(rule_payload.get("allowed", True))
        act = bool(decision.act) and rule_allowed
        reason = decision.reason
        if not rule_allowed:
            blockers = ", ".join(str(item) for item in (rule_payload.get("blockers") or []))
            reason = f"rules blocked candidate ({blockers or 'rule gate'}); {decision.reason}"
        payload = {
            "act": act,
            "size_multiplier": float(decision.size_multiplier),
            "sampled_value": float(decision.sampled_value),
            "posterior_mean": float(decision.posterior_mean),
            "posterior_var": float(decision.posterior_var),
            "reason": reason,
            "n_seen": int(decision.n_seen),
            "feature_dim": int(decision.feature_dim),
            "candidate_index": int(best_idx),
            "candidate_samples": [float(s) for s in samples],
            "size_samples": {f"{m:.2f}": float(v) for m, v in decision.size_samples.items()},
            "model": {
                "type": "hybrid_rules_bayesian_bandit",
                "rule_allowed": rule_allowed,
                "rule_score": rule_payload.get("score"),
                "rule_setup": rule_payload.get("setup"),
                "rule_blockers": rule_payload.get("blockers") or [],
                "rule_components": rule_payload.get("components") or {},
            },
            "candidate_rules": [dict(item.get("ai_model") or {}) for item in candidates_dicts],
        }
        return chosen, payload

    @staticmethod
    def _candidate_payloads(candidates: list, policy_payload: dict[str, Any] | None) -> list[dict[str, object]]:
        payloads = [asdict(item) for item in candidates]
        if not policy_payload:
            return payloads
        rules = list(policy_payload.get("candidate_rules") or [])
        samples = list(policy_payload.get("candidate_samples") or [])
        selected_idx = policy_payload.get("candidate_index")
        for idx, payload in enumerate(payloads):
            if idx < len(rules):
                payload["ai_model"] = rules[idx]
            if idx < len(samples):
                payload["policy_sample"] = float(samples[idx])
            payload["policy_selected"] = idx == selected_idx
        return payloads

    @staticmethod
    def _attach_selected_candidate_model(
        candidate_payload: dict[str, object],
        policy_payload: dict[str, Any] | None,
    ) -> None:
        if not policy_payload:
            return
        selected_idx = policy_payload.get("candidate_index")
        rules = list(policy_payload.get("candidate_rules") or [])
        if isinstance(selected_idx, int) and 0 <= selected_idx < len(rules):
            candidate_payload["ai_model"] = rules[selected_idx]

    def _build_rag_context(
        self,
        *,
        underlying: str,
        symbol: str | None,
        signal,
        regime,
        candidate: dict[str, Any],
        risk_payload: dict[str, Any] | None,
        data_context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return rag_service.context_gate(
                self._rag_request(
                    underlying=underlying,
                    symbol=symbol,
                    signal=signal,
                    regime=regime,
                    candidate=candidate,
                    risk_payload=risk_payload,
                    data_context=data_context,
                )
            ).model_dump()
        except Exception as exc:
            return self._rag_unavailable(exc)

    async def _build_rag_context_async(
        self,
        *,
        underlying: str,
        symbol: str | None,
        signal,
        regime,
        candidate: dict[str, Any],
        risk_payload: dict[str, Any] | None,
        data_context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            request = self._rag_request(
                underlying=underlying,
                symbol=symbol,
                signal=signal,
                regime=regime,
                candidate=candidate,
                risk_payload=risk_payload,
                data_context=data_context,
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(rag_service.context_gate, request),
                timeout=float(
                    self.config["paper_trading"].get("rag_timeout_seconds", 1.5)
                ),
            )
            return result.model_dump()
        except Exception as exc:
            return self._rag_unavailable(exc)

    def _rag_request(
        self,
        *,
        underlying: str,
        symbol: str | None,
        signal,
        regime,
        candidate: dict[str, Any],
        risk_payload: dict[str, Any] | None,
        data_context: dict[str, Any],
    ) -> ContextGateRequest:
        ai_model = candidate.get("ai_model") if isinstance(candidate.get("ai_model"), dict) else {}
        numeric_context = {
            **data_context,
            "confidence": getattr(signal, "confidence", None),
            "expected_move": getattr(signal, "expected_move", None),
            "expected_move_pct": getattr(signal, "expected_move_pct", None),
            "expected_horizon_bars": getattr(signal, "expected_horizon_bars", None),
            "expected_iv_change": getattr(signal, "expected_iv_change", None),
            "jump_score": getattr(signal, "jump_score", None),
            "timing_precision": getattr(signal, "timing_precision", None),
            "model_uncertainty": getattr(signal, "model_uncertainty", None),
            "strike": candidate.get("strike"),
            "expiry": candidate.get("expiry"),
            "expiry_kind": candidate.get("expiry_kind"),
            "delta": candidate.get("delta"),
            "delta_bucket": candidate.get("delta_bucket"),
            "p_trading_edge": candidate.get("p_trading_edge"),
            "p_terminal_edge": candidate.get("p_terminal_edge"),
            "p_minus_q_tail": candidate.get("p_minus_q_tail"),
            "probability_of_profit": candidate.get("probability_of_profit"),
            "skew_tax": candidate.get("skew_tax"),
            "timing_fit": candidate.get("timing_fit"),
            "expected_return_on_premium": candidate.get("expected_return_on_premium"),
            "liquidity_score": candidate.get("liquidity_score"),
            "ai_rule_score": ai_model.get("score"),
            "ai_rule_allowed": ai_model.get("allowed"),
            "risk_approved": bool((risk_payload or {}).get("approved")),
        }
        query = (
            f"directional_long_options {underlying} {symbol or ''} {getattr(signal, 'direction', '')} "
            f"{getattr(signal, 'sleeve', '')} {getattr(regime, 'label', '')} "
            f"{candidate.get('expiry_kind')} {candidate.get('delta_bucket')} "
            f"iv edge skew theta timing p_minus_q"
        )
        return ContextGateRequest(
            strategy_key="directional_long_options",
            underlying=underlying,
            symbol=symbol,
            signal_direction=getattr(signal, "direction", None),
            setup_name=getattr(signal, "sleeve", None),
            regime=getattr(regime, "label", None),
            event_tags=[
                "directional_options",
                "distributional_optimizer",
                str(candidate.get("expiry_kind") or ""),
                str(candidate.get("delta_bucket") or ""),
            ],
            numeric_context={key: value for key, value in numeric_context.items() if value is not None},
            # Always tell RAG "upstream gate passed" so it evaluates on
            # its OWN evidence (resolved-case win-rate / expectancy),
            # not by echoing the policy's already-made decision.
            #
            # Old behavior: if the policy chose to SKIP we'd set
            # risk_payload.approved=False BEFORE calling RAG, then pass
            # hard_risk_passed=False here. RAG's first check is
            # `if not hard_risk_passed: return "block", ["hard_risk_failed"]`
            # which produced a misleading "RAG blocked" entry in the
            # journal even though RAG had no actual evidence and the
            # real skip reason was the policy.
            #
            # New behavior: RAG returns its evidence-based verdict
            # (allow / warn / block-with-real-reasons). If it finds
            # losses in retrieved cases it still blocks; if it finds
            # nothing, it returns "warn: insufficient_case_memory"
            # which doesn't override the policy decision. Either way
            # the journal accurately reflects WHO is gating the trade.
            hard_risk_passed=True,
            query=query,
        )

    def _apply_rag_context_to_risk(self, rag_context: dict[str, Any] | None, risk_payload: dict[str, Any] | None) -> None:
        if not rag_context or not risk_payload:
            return
        if rag_context.get("decision") != "block":
            return
        # Defensive: ignore any pure hard_risk_failed block. With
        # hard_risk_passed=True everywhere this shouldn't happen, but
        # belt-and-suspenders — that reason code is just a mirror of
        # the upstream gate and adds no signal. We only let RAG
        # OVERRIDE the policy when it has real evidence reasons
        # (negative_retrieval_conditional_expectancy /
        # low_similar_case_win_rate / policy_context_attached).
        reason_codes = list(rag_context.get("reason_codes") or [])
        meaningful = [r for r in reason_codes if r and r != "hard_risk_failed"]
        if not meaningful:
            return
        risk_payload["approved"] = False
        reasons = list(risk_payload.get("reasons") or [])
        reasons.append(f"RAG context gate blocked trade: {', '.join(meaningful)}.")
        risk_payload["reasons"] = reasons

    @staticmethod
    def _rag_unavailable(exc: Exception) -> dict[str, Any]:
        return {
            "decision": "warn",
            "confidence": 0.0,
            "summary": f"RAG context unavailable: {exc}",
            "reason_codes": ["rag_unavailable"],
            "case_stats": {"matched_cases": 0, "resolved_cases": 0},
            "retrievals": [],
            "audit_bundle": {},
        }

    def _build_live_data_status(
        self,
        *,
        underlying: str,
        feature_frame: pd.DataFrame,
        strategy_health: dict[str, Any],
        history_source: str,
        history_symbol: str,
    ) -> dict[str, object]:
        latest_watchlist_time = strategy_health.get("latest_watchlist_time")
        watchlist_age_seconds = strategy_health.get("watchlist_age_seconds")
        latest_spot_map = dict(strategy_health.get("latest_spot_rows") or {})
        latest_spot_time = latest_spot_map.get(str(underlying).upper())
        if not latest_spot_time and not feature_frame.empty:
            latest_spot_time = pd.Timestamp(feature_frame.iloc[-1]["time"]).isoformat()
        stale_limit = float(self.config["paper_trading"]["stale_watchlist_seconds"])
        spot_age_seconds: Optional[float] = None
        if latest_spot_time:
            spot_ts = pd.Timestamp(latest_spot_time)
            if spot_ts.tzinfo is None:
                spot_ts = spot_ts.tz_localize("UTC")
            spot_age_seconds = max(0.0, (pd.Timestamp.utcnow() - spot_ts).total_seconds())
        watchlist_rows = int(
            strategy_health.get("watchlist_rows_today")
            or strategy_health.get("watchlist_rows_latest")
            or 0
        )
        market_intelligence_ready = bool(strategy_health.get("ready", bool(watchlist_rows)))
        using_latest_session = str(strategy_health.get("readiness_mode") or "") == "latest_session"
        # latest_session (yesterday's rows) may only bypass the staleness gates
        # while the exchange is CLOSED. During a live-session watchlist outage
        # the MI runtime still reports latest_session — trading on it would
        # "fill" entries at yesterday-15:29 LTPs against today's live spot.
        from core.trading_calendar import trading_calendar as _cal

        _exchange = "BSE" if str(underlying).upper() in ("SENSEX", "BANKEX") else "NSE"
        latest_session_ok = using_latest_session and not _cal.is_exchange_open(_exchange)
        # ── Degenerate-bar gate (audit 2026-07-18) ────────────────────────────
        # execution_ready previously validated only presence + age, so a flat
        # zero-volume OHLC bar (high==low, volume==0 — observed live on NIFTY)
        # passed as "ready". Material now that the 50-stock NIFTY-50 universe is
        # live: illiquid names emit exactly these bars. Require the latest
        # feature row to carry market information: reject a flat zero-volume
        # bar, and reject an all-zero/NaN ATR over the recent window (no true
        # range at all). Skip-and-report (degraded_reason="degenerate_bar"),
        # consistent with the existing degraded_reason vocabulary. Checks are
        # column-defensive: frames lacking high/low/volume/atr (research paths,
        # minimal fixtures) are left to the other gates.
        degenerate_bar = False
        if not feature_frame.empty:
            _last_row = feature_frame.iloc[-1]
            _cols = set(feature_frame.columns)
            if {"high", "low", "volume"}.issubset(_cols):
                try:
                    _high = float(_last_row["high"])
                    _low = float(_last_row["low"])
                    _vol = float(_last_row["volume"])
                except (TypeError, ValueError):
                    _high = _low = _vol = float("nan")
                if _high == _low and _vol == 0.0:
                    degenerate_bar = True
            if not degenerate_bar and "atr" in _cols:
                _atr_window = pd.to_numeric(
                    feature_frame["atr"].tail(14), errors="coerce"
                )
                if _atr_window.isna().all() or float(_atr_window.fillna(0.0).abs().max()) <= 0.0:
                    degenerate_bar = True
        execution_ready = bool(
            not feature_frame.empty
            and not degenerate_bar
            and latest_spot_time
            and watchlist_rows
            and market_intelligence_ready
            and (
                latest_session_ok
                or watchlist_age_seconds is None
                or float(watchlist_age_seconds) <= stale_limit
            )
            and (
                latest_session_ok
                or spot_age_seconds is None
                or float(spot_age_seconds) <= stale_limit
            )
        )
        degraded_reason = None
        if feature_frame.empty:
            degraded_reason = "missing_spot_history"
        elif degenerate_bar:
            degraded_reason = "degenerate_bar"
        elif not latest_spot_time:
            degraded_reason = "shared_spot_store_missing_symbol"
        elif not watchlist_rows:
            degraded_reason = "local_watchlist_empty"
        elif not market_intelligence_ready:
            degraded_reason = "market_intelligence_not_ready"
        elif using_latest_session and not latest_session_ok:
            degraded_reason = "latest_session_during_live_market"
        elif not latest_session_ok and watchlist_age_seconds is not None and float(watchlist_age_seconds) > stale_limit:
            degraded_reason = "local_watchlist_stale"
        elif not latest_session_ok and spot_age_seconds is not None and float(spot_age_seconds) > stale_limit:
            degraded_reason = "shared_spot_store_stale"
        return {
            "history_source": history_source,
            "history_symbol": history_symbol,
            "latest_watchlist_time": latest_watchlist_time,
            "watchlist_age_seconds": watchlist_age_seconds,
            "watchlist_rows_today": int(strategy_health.get("watchlist_rows_today") or 0),
            "watchlist_rows_latest": int(strategy_health.get("watchlist_rows_latest") or 0),
            "readiness_mode": strategy_health.get("readiness_mode"),
            "latest_spot_time": latest_spot_time,
            "spot_age_seconds": spot_age_seconds,
            "execution_ready": execution_ready,
            "degraded_reason": degraded_reason,
        }


directional_options_service = DirectionalOptionsService()
