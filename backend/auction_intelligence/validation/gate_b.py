from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from math import inf
from typing import Any

from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.config import clone_default_config
from auction_intelligence.schemas import (
    AgentContext,
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.validation.schemas import ValidationArtifact, ValidationCheck, ValidationReport


class GateBValidator:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.service = AuctionIntelligenceService(self.config)
        gate_config = self.config.get("validation", {}).get("gate_b", {})
        self.observation_bars = int(gate_config.get("observation_bars", 4))
        self.min_sessions = int(gate_config.get("min_sessions", 5))
        self.min_trades = int(gate_config.get("min_trades", 2))
        self.walk_forward_windows = int(gate_config.get("walk_forward_windows", 3))
        self.min_profit_factor = float(gate_config.get("min_profit_factor", 1.0))
        self.min_positive_window_ratio = float(gate_config.get("min_positive_window_ratio", 0.5))
        self.max_setup_concentration = float(gate_config.get("max_setup_concentration", 0.75))
        self.required_positive_regimes = int(gate_config.get("required_positive_regimes", 2))
        self.required_positive_setups = int(gate_config.get("required_positive_setups", 2))
        self.slippage_bps = float(gate_config.get("slippage_bps", self.config["mvp_scope"].get("slippage_bps", 1.5)))
        self.commission_bps = float(gate_config.get("commission_bps", self.config["mvp_scope"].get("commission_bps", 0.5)))

    def validate(
        self,
        *,
        symbol: str,
        sessions: list[list[MarketBar]],
        mode: str = "live",
        source: str = "unknown",
    ) -> ValidationReport:
        ordered_sessions = [sorted(session, key=lambda item: item.timestamp) for session in sessions if session]
        trades: list[dict[str, Any]] = []
        artifacts: list[ValidationArtifact] = []
        skipped_sessions = 0

        for index in range(1, len(ordered_sessions)):
            prior_session = ordered_sessions[index - 1]
            current_session = ordered_sessions[index]
            session_date = current_session[-1].timestamp.date().isoformat()
            if len(current_session) <= self.observation_bars:
                skipped_sessions += 1
                artifacts.append(
                    ValidationArtifact(
                        artifact_type="gate_b_session",
                        artifact_key=session_date,
                        payload={
                            "session_date": session_date,
                            "status": "skipped",
                            "skip_reason": "insufficient_current_bars",
                            "current_bar_count": len(current_session),
                            "prior_bar_count": len(prior_session),
                        },
                    )
                )
                continue

            observed_bars = current_session[: self.observation_bars]
            future_bars = current_session[self.observation_bars :]
            if not future_bars:
                skipped_sessions += 1
                artifacts.append(
                    ValidationArtifact(
                        artifact_type="gate_b_session",
                        artifact_key=session_date,
                        payload={
                            "session_date": session_date,
                            "status": "skipped",
                            "skip_reason": "no_future_bars",
                            "observed_bar_count": len(observed_bars),
                            "future_bar_count": len(future_bars),
                        },
                    )
                )
                continue

            prior_profile = self.service.market_profile.build_profile(symbol, prior_session)
            current_profile = self.service.market_profile.build_profile(
                symbol,
                observed_bars,
                prior_profile=prior_profile,
            )
            quote = self._quote_from_bars(observed_bars)
            depth = self._depth_from_bars(observed_bars)
            trades_input = self._trades_from_bars(observed_bars)
            order_flow = self.service.order_flow.compute(
                quote=quote,
                trades=trades_input,
                depth=depth,
                tick_size=current_profile.tick_size,
            )
            regime = self.service.regime.classify(
                current=current_profile,
                prior=prior_profile,
                order_flow=order_flow,
            )
            context = AgentContext(
                session=SessionContext(
                    symbol=symbol,
                    session_date=current_session[-1].timestamp.date(),
                    last_price=observed_bars[-1].close,
                    stale_data_seconds=0.0,
                    minutes_to_close=max(0, 375 - (len(observed_bars) * 30)),
                    broker_connected=True,
                ),
                portfolio=PortfolioSnapshot(),
                current_profile=current_profile,
                prior_profile=prior_profile,
                order_flow=order_flow,
                regime=regime,
                config=self.config,
            )
            decision = self.service.swing_agent.evaluate(context)
            if decision is None or decision.action == "FLAT" or decision.entry_price is None:
                skipped_sessions += 1
                artifacts.append(
                    ValidationArtifact(
                        artifact_type="gate_b_session",
                        artifact_key=session_date,
                        payload={
                            "session_date": session_date,
                            "status": "skipped",
                            "skip_reason": "flat_decision",
                            "flat_reason": None if decision is None else decision.metadata.get("flat_reason"),
                            "blocking_reasons": [] if decision is None else decision.metadata.get("blocking_reasons", []),
                            "observed_bar_count": len(observed_bars),
                            "future_bar_count": len(future_bars),
                            "regime_label": regime.label,
                            "decision_action": "FLAT" if decision is None else decision.action,
                            "candidate_action": None if decision is None else decision.metadata.get("candidate_action"),
                            "decision_confidence": 0.0 if decision is None else float(decision.confidence),
                            "computed_confidence": None if decision is None else decision.metadata.get("computed_confidence"),
                            "min_confidence": None if decision is None else decision.metadata.get("min_confidence"),
                            "setup_name": None if decision is None else decision.metadata.get("setup_name"),
                            "rationale": [] if decision is None else list(decision.rationale),
                            "diagnostics": {} if decision is None else decision.metadata.get("diagnostics", {}),
                            "close_price": current_profile.close_price,
                            "vah": current_profile.vah,
                            "val": current_profile.val,
                            "delta": round(order_flow.delta, 4),
                        },
                    )
                )
                continue

            simulation = self._simulate_trade(
                session_date=session_date,
                decision=decision,
                future_bars=future_bars,
            )
            simulation["regime_label"] = regime.label
            trades.append(simulation)
            artifacts.append(
                ValidationArtifact(
                    artifact_type="gate_b_session",
                    artifact_key=session_date,
                    payload={
                        "session_date": session_date,
                        "status": "evaluated",
                        "skip_reason": None,
                        "flat_reason": None,
                        "blocking_reasons": [],
                        "observed_bar_count": len(observed_bars),
                        "future_bar_count": len(future_bars),
                        "regime_label": regime.label,
                        "decision_action": decision.action,
                        "candidate_action": decision.metadata.get("candidate_action"),
                        "decision_confidence": float(decision.confidence),
                        "computed_confidence": decision.metadata.get("computed_confidence"),
                        "min_confidence": decision.metadata.get("min_confidence"),
                        "setup_name": decision.metadata.get("setup_name"),
                        "rationale": list(decision.rationale),
                        "diagnostics": decision.metadata.get("diagnostics", {}),
                        "close_price": current_profile.close_price,
                        "vah": current_profile.vah,
                        "val": current_profile.val,
                        "delta": round(order_flow.delta, 4),
                        "trade": simulation,
                    },
                )
            )
            artifacts.append(
                ValidationArtifact(
                    artifact_type="gate_b_trade",
                    artifact_key=f"{session_date}:{decision.metadata.get('setup_name', 'swing_rule')}",
                    payload=simulation,
                )
            )

        metrics = self._compute_metrics(
            trades=trades,
            artifacts=artifacts,
            total_sessions=len(ordered_sessions),
            skipped_sessions=skipped_sessions,
            mode=mode,
            source=source,
        )
        checks = self._build_checks(metrics)
        error_checks = [check for check in checks if check.severity == "error"]
        score = round(
            (sum(1 for check in error_checks if check.passed) / len(error_checks)) if error_checks else 1.0,
            4,
        )
        passed = all(check.passed for check in error_checks)

        return ValidationReport(
            gate="gate_b",
            label="Rule engine and walk-forward",
            passed=passed,
            score=score,
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            checks=checks,
            metrics=metrics,
            pending_checks=[
                "Shadow-mode reconciliation and simulated-versus-observed fill drift remain Gate C requirements.",
                "Options mapping is intentionally excluded until the futures structural sleeve passes this gate.",
                "Multi-agent allocator validation remains out of scope until the single swing sleeve is stable.",
            ],
            artifacts=artifacts,
        )

    def _quote_from_bars(self, bars: list[MarketBar]) -> QuoteSnapshot:
        last_bar = bars[-1]
        tick_size = float(self.config["market_profile"].get("tick_size", 0.5))
        bid_size = max(last_bar.volume / 12.0, 50.0)
        ask_size = max(last_bar.volume / 14.0, 50.0)
        return QuoteSnapshot(
            timestamp=last_bar.timestamp,
            bid=round(last_bar.close - tick_size, 4),
            ask=round(last_bar.close + tick_size, 4),
            bid_size=bid_size,
            ask_size=ask_size,
        )

    def _depth_from_bars(self, bars: list[MarketBar]) -> DepthSnapshot:
        quote = self._quote_from_bars(bars)
        tick_size = float(self.config["market_profile"].get("tick_size", 0.5))
        return DepthSnapshot(
            timestamp=quote.timestamp,
            bids=[
                DepthLevel(price=round(quote.bid - (tick_size * level), 4), quantity=max(quote.bid_size * (1 - (0.18 * level)), 1.0))
                for level in range(3)
            ],
            asks=[
                DepthLevel(price=round(quote.ask + (tick_size * level), 4), quantity=max(quote.ask_size * (1 - (0.18 * level)), 1.0))
                for level in range(3)
            ],
        )

    def _trades_from_bars(self, bars: list[MarketBar]) -> list[TradePrint]:
        trades: list[TradePrint] = []
        for index, bar in enumerate(bars[-8:]):
            side = "unknown"
            if bar.close > bar.open:
                side = "buy"
            elif bar.close < bar.open:
                side = "sell"
            trades.append(
                TradePrint(
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=max(bar.volume / 12.0, 1.0) + index,
                    aggressor_side=side,
                )
            )
        return trades

    def _simulate_trade(self, *, session_date: str, decision, future_bars: list[MarketBar]) -> dict[str, Any]:
        entry = float(decision.entry_price or future_bars[0].open)
        stop = float(decision.stop_price or entry)
        target = float(decision.target_price or entry)
        qty = int(decision.quantity or 0)
        side = decision.action
        setup_name = str(decision.metadata.get("setup_name", decision.metadata.get("regime", "swing_rule")))
        exit_price = future_bars[-1].close
        exit_reason = "session_close"

        mae = 0.0
        mfe = 0.0
        for bar in future_bars:
            if side == "LONG":
                mae = min(mae, bar.low - entry)
                mfe = max(mfe, bar.high - entry)
                if bar.low <= stop:
                    exit_price = stop
                    exit_reason = "stop"
                    break
                if bar.high >= target:
                    exit_price = target
                    exit_reason = "target"
                    break
            else:
                mae = min(mae, entry - bar.high)
                mfe = max(mfe, entry - bar.low)
                if bar.high >= stop:
                    exit_price = stop
                    exit_reason = "stop"
                    break
                if bar.low <= target:
                    exit_price = target
                    exit_reason = "target"
                    break

        direction = 1.0 if side == "LONG" else -1.0
        gross_pnl = (exit_price - entry) * direction * qty
        notional = max(entry * qty, 1.0)
        cost = notional * ((self.slippage_bps + self.commission_bps) / 10_000.0)
        net_pnl = gross_pnl - cost
        return {
            "session_date": session_date,
            "setup_name": setup_name,
            "side": side,
            "entry_price": round(entry, 4),
            "exit_price": round(exit_price, 4),
            "quantity": qty,
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "mae": round(mae, 4),
            "mfe": round(mfe, 4),
            "exit_reason": exit_reason,
            "confidence": float(decision.confidence),
        }

    def _compute_metrics(
        self,
        *,
        trades: list[dict[str, Any]],
        artifacts: list[ValidationArtifact],
        total_sessions: int,
        skipped_sessions: int,
        mode: str,
        source: str,
    ) -> dict[str, Any]:
        total_net = round(sum(item["net_pnl"] for item in trades), 4)
        total_gross = round(sum(item["gross_pnl"] for item in trades), 4)
        wins = [item for item in trades if item["net_pnl"] > 0]
        losses = [item for item in trades if item["net_pnl"] < 0]
        win_rate = round((len(wins) / len(trades)) if trades else 0.0, 4)
        expectancy = round((total_net / len(trades)) if trades else 0.0, 4)
        gross_profit = round(sum(item["net_pnl"] for item in wins), 4)
        gross_loss = round(abs(sum(item["net_pnl"] for item in losses)), 4)
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else (inf if gross_profit > 0 else 0.0)
        turnover = round(sum(abs(item["entry_price"] * item["quantity"]) for item in trades), 4)
        max_drawdown = self._max_drawdown(trades)
        setup_attribution = self._attribution(trades, "setup_name")
        regime_attribution = self._attribution(trades, "regime_label")
        windows = self._walk_forward_windows(trades)
        positive_window_ratio = round(
            (sum(1 for window in windows if window["net_pnl"] > 0) / len(windows)) if windows else 0.0,
            4,
        )
        largest_setup_net = max((abs(values["net_pnl"]) for values in setup_attribution.values()), default=0.0)
        setup_concentration = round((largest_setup_net / max(abs(total_net), 1e-9)) if trades and total_net != 0 else 0.0, 4)
        positive_regimes = sum(1 for values in regime_attribution.values() if values["net_pnl"] > 0)
        positive_setups = sum(1 for values in setup_attribution.values() if values["net_pnl"] > 0)
        skip_reason_attribution = self._artifact_breakdown(artifacts, "skip_reason")
        flat_reason_attribution = self._artifact_breakdown(artifacts, "flat_reason")
        blocker_attribution = self._artifact_list_breakdown(artifacts, "blocking_reasons")

        return {
            "mode": mode,
            "source": source,
            "session_count": total_sessions,
            "evaluated_trades": len(trades),
            "skipped_sessions": skipped_sessions,
            "skip_reason_attribution": skip_reason_attribution,
            "flat_reason_attribution": flat_reason_attribution,
            "blocking_reason_attribution": blocker_attribution,
            "win_rate": win_rate,
            "expectancy": expectancy,
            "gross_pnl": total_gross,
            "net_pnl": total_net,
            "profit_factor": profit_factor if profit_factor != inf else "inf",
            "avg_win": round((gross_profit / len(wins)) if wins else 0.0, 4),
            "avg_loss": round((sum(item["net_pnl"] for item in losses) / len(losses)) if losses else 0.0, 4),
            "max_drawdown": max_drawdown,
            "turnover": turnover,
            "positive_window_ratio": positive_window_ratio,
            "walk_forward_windows": windows,
            "setup_concentration": setup_concentration,
            "positive_regime_buckets": positive_regimes,
            "positive_setup_buckets": positive_setups,
            "setup_attribution": setup_attribution,
            "regime_attribution": regime_attribution,
            "trades": trades,
        }

    def _artifact_breakdown(self, artifacts: list[ValidationArtifact], field_name: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for artifact in artifacts:
            if artifact.artifact_type != "gate_b_session":
                continue
            value = artifact.payload.get(field_name)
            if value:
                counts[str(value)] += 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _artifact_list_breakdown(self, artifacts: list[ValidationArtifact], field_name: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for artifact in artifacts:
            if artifact.artifact_type != "gate_b_session":
                continue
            values = artifact.payload.get(field_name) or []
            for value in values:
                if value:
                    counts[str(value)] += 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _attribution(self, trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
        buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0})
        for trade in trades:
            bucket = str(trade.get(key, "unknown"))
            buckets[bucket]["trades"] += 1
            buckets[bucket]["net_pnl"] += float(trade["net_pnl"])
            if float(trade["net_pnl"]) > 0:
                buckets[bucket]["wins"] += 1
        return {
            bucket: {
                "trades": values["trades"],
                "net_pnl": round(values["net_pnl"], 4),
                "win_rate": round(values["wins"] / values["trades"], 4) if values["trades"] else 0.0,
                "expectancy": round(values["net_pnl"] / values["trades"], 4) if values["trades"] else 0.0,
            }
            for bucket, values in buckets.items()
        }

    def _walk_forward_windows(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not trades:
            return []
        ordered = sorted(trades, key=lambda item: item["session_date"])
        window_size = max(len(ordered) // self.walk_forward_windows, 1)
        windows: list[dict[str, Any]] = []
        for index in range(0, len(ordered), window_size):
            window_trades = ordered[index : index + window_size]
            if not window_trades:
                continue
            net_pnl = round(sum(item["net_pnl"] for item in window_trades), 4)
            expectancy = round(net_pnl / len(window_trades), 4)
            windows.append(
                {
                    "window_index": len(windows) + 1,
                    "trade_count": len(window_trades),
                    "net_pnl": net_pnl,
                    "expectancy": expectancy,
                    "start_session": window_trades[0]["session_date"],
                    "end_session": window_trades[-1]["session_date"],
                }
            )
        return windows

    def _max_drawdown(self, trades: list[dict[str, Any]]) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade in sorted(trades, key=lambda item: item["session_date"]):
            equity += float(trade["net_pnl"])
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return round(max_drawdown, 4)

    def _build_checks(self, metrics: dict[str, Any]) -> list[ValidationCheck]:
        profit_factor = metrics["profit_factor"]
        if profit_factor == "inf":
            profit_factor_pass = True
            profit_factor_observed: Any = "inf"
        else:
            profit_factor_pass = float(profit_factor) >= self.min_profit_factor
            profit_factor_observed = profit_factor

        return [
            ValidationCheck(
                key="session_count",
                label="Validation session count",
                passed=int(metrics["session_count"]) >= self.min_sessions,
                observed=metrics["session_count"],
                threshold=f">= {self.min_sessions}",
                detail="Gate B requires multiple completed sessions before setup-level conclusions are trusted.",
            ),
            ValidationCheck(
                key="trade_count",
                label="Executed trade count",
                passed=int(metrics["evaluated_trades"]) >= self.min_trades,
                observed=metrics["evaluated_trades"],
                threshold=f">= {self.min_trades}",
                detail="The deterministic sleeve must generate enough evaluated trades to make expectancy meaningful.",
            ),
            ValidationCheck(
                key="expectancy",
                label="Positive expectancy after costs",
                passed=float(metrics["expectancy"]) > 0,
                observed=metrics["expectancy"],
                threshold="> 0",
                detail="Net expectancy must stay positive after the configured cost model.",
            ),
            ValidationCheck(
                key="profit_factor",
                label="Profit factor threshold",
                passed=profit_factor_pass,
                observed=profit_factor_observed,
                threshold=f">= {self.min_profit_factor}",
                detail="Gross winners must outweigh gross losers after costs.",
            ),
            ValidationCheck(
                key="walk_forward",
                label="Positive walk-forward windows",
                passed=float(metrics["positive_window_ratio"]) >= self.min_positive_window_ratio,
                observed=metrics["positive_window_ratio"],
                threshold=f">= {self.min_positive_window_ratio}",
                detail="The rule sleeve should remain profitable across rolling validation windows.",
            ),
            ValidationCheck(
                key="setup_concentration",
                label="Setup concentration cap",
                passed=float(metrics["setup_concentration"]) <= self.max_setup_concentration,
                observed=metrics["setup_concentration"],
                threshold=f"<= {self.max_setup_concentration}",
                severity="warning",   # advisory: real diversity gates are breadth + regime checks
                detail="No single deterministic setup should carry the whole sleeve.",
            ),
            ValidationCheck(
                key="regime_breadth",
                label="Positive regime breadth",
                passed=int(metrics["positive_regime_buckets"]) >= self.required_positive_regimes,
                observed=metrics["positive_regime_buckets"],
                threshold=f">= {self.required_positive_regimes}",
                severity="warning",
                detail="Broader regime coverage is required before promotion to paper or live.",
            ),
            ValidationCheck(
                key="setup_breadth",
                label="Positive setup breadth",
                passed=int(metrics["positive_setup_buckets"]) >= self.required_positive_setups,
                observed=metrics["positive_setup_buckets"],
                threshold=f">= {self.required_positive_setups}",
                severity="warning",
                detail="Multiple setups should contribute positive expectancy before the sleeve is promoted.",
            ),
        ]
