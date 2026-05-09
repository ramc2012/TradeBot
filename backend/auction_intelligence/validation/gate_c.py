from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from statistics import median
from typing import Any

from auction_intelligence.config import clone_default_config
from auction_intelligence.validation.schemas import ValidationArtifact, ValidationCheck, ValidationReport


class GateCValidator:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        gate_config = self.config.get("validation", {}).get("gate_c", {})
        self.min_sessions = int(gate_config.get("min_sessions", 20))
        self.target_sessions = int(gate_config.get("target_sessions", 30))
        self.max_stale_signal_ratio = float(gate_config.get("max_stale_signal_ratio", 0.005))
        self.max_median_fill_drift_ticks = float(gate_config.get("max_median_fill_drift_ticks", 2.0))
        self.max_p95_fill_drift_ticks = float(gate_config.get("max_p95_fill_drift_ticks", 8.0))
        self.max_non_critical_incidents_per_week = int(gate_config.get("max_non_critical_incidents_per_week", 1))
        self.min_kill_switch_drills = int(gate_config.get("min_kill_switch_drills", 2))
        self.position_mismatch_timeout_seconds = float(gate_config.get("position_mismatch_timeout_seconds", 300.0))

    def validate(
        self,
        *,
        symbol: str,
        records: list[dict[str, Any]],
        session_limit: int = 30,
    ) -> ValidationReport:
        normalized_records = self._normalize_records(records)
        ordered_sessions = sorted({row["session_date"] for row in normalized_records})
        selected_sessions = ordered_sessions[-session_limit:] if session_limit > 0 else ordered_sessions
        filtered_records = [row for row in normalized_records if row["session_date"] in selected_sessions]
        metrics = self._compute_metrics(filtered_records, symbol=symbol, selected_sessions=selected_sessions)
        checks = self._build_checks(metrics)
        error_checks = [check for check in checks if check.severity == "error"]
        score = round(
            (sum(1 for check in error_checks if check.passed) / len(error_checks)) if error_checks else 1.0,
            4,
        )
        passed = all(check.passed for check in error_checks)
        return ValidationReport(
            gate="gate_c",
            label="Shadow mode and divergence control",
            passed=passed,
            score=score,
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            checks=checks,
            metrics=metrics,
            pending_checks=[
                "Paper-trading promotion remains blocked until this shadow journal accumulates the full observation window.",
                "Live canary remains blocked until Gate D confirms paper/live divergence and operational stability together.",
                "Weekly-options mapping remains shadow-only until futures Gate B and Gate C both stay green.",
            ],
            artifacts=self._build_artifacts(filtered_records),
        )

    def _normalize_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in records:
            raw_session_date = row.get("session_date")
            if not raw_session_date:
                continue
            session_date = raw_session_date
            if isinstance(raw_session_date, str):
                session_date = date.fromisoformat(raw_session_date)
            recorded_at = row.get("recorded_at")
            if isinstance(recorded_at, str):
                recorded_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            normalized.append(
                {
                    **row,
                    "session_date": session_date,
                    "recorded_at": recorded_at,
                    "fill_drift_ticks": None if row.get("fill_drift_ticks") is None else float(row["fill_drift_ticks"]),
                    "stale_signal": bool(row.get("stale_signal", False)),
                    "quantity": int(row.get("quantity") or 0),
                    "confidence": float(row.get("confidence") or 0.0),
                    "mismatch_duration_seconds": float(row.get("mismatch_duration_seconds") or 0.0),
                    "tick_size": float(row.get("tick_size") or 0.5),
                }
            )
        normalized.sort(
            key=lambda row: (
                row["session_date"],
                row["recorded_at"] or datetime.min.replace(tzinfo=UTC),
                str(row.get("agent_name") or ""),
            )
        )
        return normalized

    def _compute_metrics(
        self,
        records: list[dict[str, Any]],
        *,
        symbol: str,
        selected_sessions: list[date],
    ) -> dict[str, Any]:
        signal_records = [row for row in records if str(row.get("action") or "FLAT") != "FLAT"]
        stale_signals = [row for row in signal_records if row["stale_signal"]]
        fill_records = [row for row in signal_records if row.get("fill_drift_ticks") is not None]
        fill_drifts = sorted(float(row["fill_drift_ticks"]) for row in fill_records)
        unresolved_position_drifts = [
            row
            for row in records
            if str(row.get("reconciliation_status") or "") == "position_mismatch"
            and float(row.get("mismatch_duration_seconds") or 0.0) > self.position_mismatch_timeout_seconds
        ]
        critical_incidents = [
            row for row in records if str(row.get("reconciliation_status") or "") == "critical_incident"
        ]
        non_critical_incidents = [
            row for row in records if str(row.get("reconciliation_status") or "") == "non_critical_incident"
        ]
        non_critical_by_week: Counter[str] = Counter()
        for row in non_critical_incidents:
            iso_year, iso_week, _ = row["session_date"].isocalendar()
            non_critical_by_week[f"{iso_year}-W{iso_week:02d}"] += 1

        session_summaries: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "record_count": 0,
                "signal_count": 0,
                "stale_signal_count": 0,
                "fill_drift_ticks": [],
                "critical_incidents": 0,
                "non_critical_incidents": 0,
            }
        )
        for row in records:
            session_key = row["session_date"].isoformat()
            summary = session_summaries[session_key]
            summary["record_count"] += 1
            if str(row.get("action") or "FLAT") != "FLAT":
                summary["signal_count"] += 1
            if row["stale_signal"]:
                summary["stale_signal_count"] += 1
            if row.get("fill_drift_ticks") is not None:
                summary["fill_drift_ticks"].append(float(row["fill_drift_ticks"]))
            if str(row.get("reconciliation_status") or "") == "critical_incident":
                summary["critical_incidents"] += 1
            if str(row.get("reconciliation_status") or "") == "non_critical_incident":
                summary["non_critical_incidents"] += 1

        dashboard_covered = any(bool(row.get("dashboard_checked", False)) for row in records)
        alerts_covered = any(bool(row.get("alerts_checked", False)) for row in records)
        manual_override_covered = any(bool(row.get("manual_override_tested", False)) for row in records)
        successful_kill_switch_drills = sum(
            1 for row in records if bool(row.get("kill_switch_tested")) and bool(row.get("kill_switch_passed"))
        )
        median_fill_drift = median(fill_drifts) if fill_drifts else None
        p95_fill_drift = self._percentile(fill_drifts, 0.95)
        return {
            "symbol": symbol,
            "session_count": len(selected_sessions),
            "session_dates": [item.isoformat() for item in selected_sessions],
            "record_count": len(records),
            "signal_count": len(signal_records),
            "stale_signal_count": len(stale_signals),
            "stale_signal_ratio": round(len(stale_signals) / len(signal_records), 4) if signal_records else 1.0,
            "fill_drift_sample_count": len(fill_drifts),
            "fill_drift_median_ticks": None if median_fill_drift is None else round(float(median_fill_drift), 4),
            "fill_drift_p95_ticks": None if p95_fill_drift is None else round(float(p95_fill_drift), 4),
            "unresolved_position_drifts": len(unresolved_position_drifts),
            "critical_incidents": len(critical_incidents),
            "non_critical_incidents": len(non_critical_incidents),
            "max_non_critical_incidents_per_week": max(non_critical_by_week.values(), default=0),
            "weekly_non_critical_incidents": dict(non_critical_by_week),
            "successful_kill_switch_drills": successful_kill_switch_drills,
            "dashboard_covered": dashboard_covered,
            "alerts_covered": alerts_covered,
            "manual_override_covered": manual_override_covered,
            "session_summaries": {
                session_key: {
                    **summary,
                    "fill_drift_ticks": [round(item, 4) for item in summary["fill_drift_ticks"]],
                }
                for session_key, summary in session_summaries.items()
            },
        }

    def _build_checks(self, metrics: dict[str, Any]) -> list[ValidationCheck]:
        fill_sample_count = int(metrics.get("fill_drift_sample_count", 0))
        median_fill_drift = metrics.get("fill_drift_median_ticks")
        p95_fill_drift = metrics.get("fill_drift_p95_ticks")
        operator_covered = bool(metrics.get("dashboard_covered")) and bool(metrics.get("alerts_covered")) and bool(metrics.get("manual_override_covered"))
        return [
            ValidationCheck(
                key="observation_period",
                label="Observation period",
                passed=int(metrics.get("session_count", 0)) >= self.min_sessions,
                observed=metrics.get("session_count", 0),
                threshold=f">={self.min_sessions} sessions",
                detail=f"Target observation window is {self.target_sessions} sessions.",
            ),
            ValidationCheck(
                key="position_drift",
                label="Position drift",
                passed=int(metrics.get("unresolved_position_drifts", 0)) == 0,
                observed=metrics.get("unresolved_position_drifts", 0),
                threshold=0,
                detail="Counts broker/internal mismatches persisting longer than five minutes.",
            ),
            ValidationCheck(
                key="stale_signals",
                label="Stale-signal count",
                passed=float(metrics.get("stale_signal_ratio", 1.0)) < self.max_stale_signal_ratio,
                observed=metrics.get("stale_signal_ratio", 1.0),
                threshold=f"< {self.max_stale_signal_ratio}",
            ),
            ValidationCheck(
                key="fill_drift",
                label="Simulated vs observed fill drift",
                passed=(
                    fill_sample_count > 0
                    and median_fill_drift is not None
                    and p95_fill_drift is not None
                    and float(median_fill_drift) <= self.max_median_fill_drift_ticks
                    and float(p95_fill_drift) <= self.max_p95_fill_drift_ticks
                ),
                observed={
                    "sample_count": fill_sample_count,
                    "median_ticks": median_fill_drift,
                    "p95_ticks": p95_fill_drift,
                },
                threshold={
                    "median_ticks": self.max_median_fill_drift_ticks,
                    "p95_ticks": self.max_p95_fill_drift_ticks,
                },
                detail="Fails automatically until at least one observed fill-drift sample exists.",
            ),
            ValidationCheck(
                key="reconciliation_incidents",
                label="Reconciliation incidents",
                passed=(
                    int(metrics.get("critical_incidents", 0)) == 0
                    and int(metrics.get("max_non_critical_incidents_per_week", 0)) <= self.max_non_critical_incidents_per_week
                ),
                observed={
                    "critical_incidents": metrics.get("critical_incidents", 0),
                    "max_non_critical_incidents_per_week": metrics.get("max_non_critical_incidents_per_week", 0),
                },
                threshold={
                    "critical_incidents": 0,
                    "max_non_critical_incidents_per_week": self.max_non_critical_incidents_per_week,
                },
            ),
            ValidationCheck(
                key="kill_switch_drills",
                label="Kill switch drills",
                passed=int(metrics.get("successful_kill_switch_drills", 0)) >= self.min_kill_switch_drills,
                observed=metrics.get("successful_kill_switch_drills", 0),
                threshold=f">={self.min_kill_switch_drills}",
            ),
            ValidationCheck(
                key="operator_coverage",
                label="Operator coverage",
                passed=operator_covered,
                observed={
                    "dashboard_covered": metrics.get("dashboard_covered", False),
                    "alerts_covered": metrics.get("alerts_covered", False),
                    "manual_override_covered": metrics.get("manual_override_covered", False),
                },
                threshold={"dashboard": True, "alerts": True, "manual_override": True},
            ),
        ]

    def _build_artifacts(self, records: list[dict[str, Any]]) -> list[ValidationArtifact]:
        artifacts: list[ValidationArtifact] = []
        session_summaries: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "records": 0,
                "signals": 0,
                "stale_signals": 0,
                "fill_drifts": [],
                "kill_switch_drills": 0,
            }
        )
        for row in records:
            session_key = row["session_date"].isoformat()
            summary = session_summaries[session_key]
            summary["records"] += 1
            if str(row.get("action") or "FLAT") != "FLAT":
                summary["signals"] += 1
            if row.get("stale_signal"):
                summary["stale_signals"] += 1
            if row.get("fill_drift_ticks") is not None:
                summary["fill_drifts"].append(float(row["fill_drift_ticks"]))
            if bool(row.get("kill_switch_tested")) and bool(row.get("kill_switch_passed")):
                summary["kill_switch_drills"] += 1

        for session_key, summary in sorted(session_summaries.items()):
            artifacts.append(
                ValidationArtifact(
                    artifact_type="gate_c_session",
                    artifact_key=session_key,
                    payload={
                        "session_date": session_key,
                        "record_count": summary["records"],
                        "signal_count": summary["signals"],
                        "stale_signal_count": summary["stale_signals"],
                        "fill_drift_ticks": [round(item, 4) for item in summary["fill_drifts"]],
                        "kill_switch_drills": summary["kill_switch_drills"],
                    },
                )
            )
        return artifacts

    def _percentile(self, values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        if len(values) == 1:
            return float(values[0])
        rank = (len(values) - 1) * percentile
        lower = int(rank)
        upper = min(lower + 1, len(values) - 1)
        weight = rank - lower
        return float(values[lower] + ((values[upper] - values[lower]) * weight))
