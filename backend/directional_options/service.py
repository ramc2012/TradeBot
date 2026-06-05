"""Service orchestrator for the directional long-options module."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import lru_cache, partial
from pathlib import Path
from time import monotonic
from typing import Any, Optional

import pandas as pd

from agentic_rag import ContextGateRequest, rag_service
from analysis.signal_classifier import classify_status_bucket
from core.config import settings
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


class DirectionalOptionsService:
    """Expose research, live snapshot, and paper-trading surfaces."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = DirectionalOptionsDataStore(self.config["data_root"])
        self.feature_engine = FeatureEngine(self.config["feature_engine"])
        self.regime = RegimeClassifier()
        self.signals = DirectionalSignalEngine(self.config["signal_engine"])
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
        )
        self.backtester = DirectionalOptionsBacktester(
            store=self.store,
            feature_engine=self.feature_engine,
            regime=self.regime,
            signals=self.signals,
            selector=self.selector,
            risk=self.risk,
            config=self.config,
        )
        self._summary_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
        self._live_cache_ttl_seconds = 30.0
        self._summary_cache_ttl_seconds = 60.0
        self._live_cache: dict[tuple[str, str, int], tuple[float, dict[str, object]]] = {}
        self._live_locks: dict[tuple[str, str, int], asyncio.Lock] = {}

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
            )
            if premium is None:
                # The held contract often isn't on the WS premium feed (e.g. a
                # monthly strike), so latest_local_option_mark returns nothing
                # and the position's mark FREEZES at entry — every trade then
                # closes at exit==entry == ₹0 realized P&L (27 such ₹0 trades
                # on 2026-06-04). Fall back to the option-chain cache, which
                # carries every strike's LTP (~30s fresh), so positions are
                # marked-to-market and closes realize real P&L.
                from directional_options.chain_analytics import chain_strike_mark
                chain_mark = await chain_strike_mark(row_underlying, row_expiry, row_strike, row_otype)
                if chain_mark is not None and chain_mark > 0:
                    premium = chain_mark
                    mark_time = None
                    price_source = "chain_cache_live"
            if premium is None:
                continue
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
        signal = self.signals.predict(row, regime, timeframe)

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
            candidates_payload = [asdict(item) for item in selection["candidates"]]
            chosen, policy_payload = self._policy_pick(
                signal=signal,
                regime=regime,
                candidates=selection["candidates"] or ([selection["best"]] if selection["best"] is not None else []),
                default=selection["best"],
            )
            size_mult = float((policy_payload or {}).get("size_multiplier", 1.0))
            policy_act = bool((policy_payload or {}).get("act", True))
            if chosen is not None:
                candidate_payload = asdict(chosen)
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
        signal = self.signals.predict(row, regime, timeframe)
        if signal is None and not int(
            strategy_health.get("watchlist_rows_today")
            or strategy_health.get("watchlist_rows_latest")
            or 0
        ):
            local_watchlist_status = await self.store.latest_live_watchlist_status(
                underlying=underlying,
                as_of=timestamp,
            )
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
                latest_option_time = max(str(item.get("time") or "") for item in snapshot_rows)
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
            candidates_payload = [asdict(item) for item in selection["candidates"]]
            # Pull chain analytics for the selected expiry. Fire-and-
            # tolerate-failure: a missing chain payload just means the
            # policy gets sentinel zeros for chain features and falls
            # back to signal+candidate context alone.
            chain_payload: dict[str, Any] | None = None
            try:
                chain_expiry = None
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
                    candidates=selection["candidates"] or ([selection["best"]] if selection["best"] is not None else []),
                    default=selection["best"],
                    chain=chain_payload,
                )
            )
            size_mult = float((policy_payload or {}).get("size_multiplier", 1.0))
            policy_act = bool((policy_payload or {}).get("act", True))
            if chosen is not None:
                candidate_payload = asdict(chosen)
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
        candidates_dicts = [asdict(c) for c in candidates]
        best_idx, samples = self.policy.rank_candidates(
            signal=signal_dict,
            candidates=candidates_dicts,
            regime=regime_dict,
            chain=chain,
        )
        if best_idx is None:
            return default, None
        chosen = candidates[best_idx]
        decision = self.policy.decide(
            signal=signal_dict,
            candidate=candidates_dicts[best_idx],
            regime=regime_dict,
            chain=chain,
        )
        payload = {
            "act": bool(decision.act),
            "size_multiplier": float(decision.size_multiplier),
            "sampled_value": float(decision.sampled_value),
            "posterior_mean": float(decision.posterior_mean),
            "posterior_var": float(decision.posterior_var),
            "reason": decision.reason,
            "n_seen": int(decision.n_seen),
            "feature_dim": int(decision.feature_dim),
            "candidate_index": int(best_idx),
            "candidate_samples": [float(s) for s in samples],
            "size_samples": {f"{m:.2f}": float(v) for m, v in decision.size_samples.items()},
        }
        return chosen, payload

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
            return (await asyncio.to_thread(rag_service.context_gate, request)).model_dump()
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
        execution_ready = bool(
            not feature_frame.empty
            and latest_spot_time
            and watchlist_rows
            and market_intelligence_ready
            and (
                using_latest_session
                or watchlist_age_seconds is None
                or float(watchlist_age_seconds) <= stale_limit
            )
            and (
                using_latest_session
                or spot_age_seconds is None
                or float(spot_age_seconds) <= stale_limit
            )
        )
        degraded_reason = None
        if feature_frame.empty:
            degraded_reason = "missing_spot_history"
        elif not latest_spot_time:
            degraded_reason = "shared_spot_store_missing_symbol"
        elif not watchlist_rows:
            degraded_reason = "local_watchlist_empty"
        elif not market_intelligence_ready:
            degraded_reason = "market_intelligence_not_ready"
        elif not using_latest_session and watchlist_age_seconds is not None and float(watchlist_age_seconds) > stale_limit:
            degraded_reason = "local_watchlist_stale"
        elif not using_latest_session and spot_age_seconds is not None and float(spot_age_seconds) > stale_limit:
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
