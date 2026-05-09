from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
import hashlib
import json
from typing import Any

from fastapi.encoders import jsonable_encoder

from auction_intelligence.config import clone_default_config
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.schemas import MarketBar, SessionContext
from auction_intelligence.validation.schemas import ValidationCheck, ValidationReport


class GateAValidator:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.market_profile = MarketProfileEngine(self.config["market_profile"])
        validation_config = self.config.get("validation", {}).get("gate_a", {})
        session_config = self.config.get("mvp_scope", {}).get("session", {})

        self.period_minutes = int(self.config["market_profile"].get("period_minutes", 30))
        self.session_open = self._parse_time(session_config.get("open", "09:15"))
        self.session_close = self._parse_time(session_config.get("close", "15:30"))
        self.min_current_bars = int(validation_config.get("min_current_bars", 4))
        self.min_prior_bars = int(validation_config.get("min_prior_bars", 4))
        self.require_prior_session = bool(validation_config.get("require_prior_session", True))
        self.max_duplicate_timestamps = int(validation_config.get("max_duplicate_timestamps", 0))
        self.max_out_of_order_bars = int(validation_config.get("max_out_of_order_bars", 0))
        self.max_current_session_gaps = int(validation_config.get("max_current_session_gaps", 0))
        self.max_prior_session_gaps = int(validation_config.get("max_prior_session_gaps", 0))
        self.max_misaligned_bars = int(validation_config.get("max_misaligned_bars", 0))

    def validate(
        self,
        *,
        session: SessionContext,
        bars: list[MarketBar],
        prior_bars: list[MarketBar] | None = None,
    ) -> ValidationReport:
        current_input = list(bars)
        prior_input = list(prior_bars or [])
        current_ordered = sorted(current_input, key=lambda item: item.timestamp)
        prior_ordered = sorted(prior_input, key=lambda item: item.timestamp)

        checks: list[ValidationCheck] = []

        current_duplicates = self._duplicate_count(current_input)
        prior_duplicates = self._duplicate_count(prior_input)
        current_out_of_order = self._out_of_order_count(current_input)
        prior_out_of_order = self._out_of_order_count(prior_input)
        current_misaligned = self._misaligned_count(current_ordered, session_date=session.session_date)
        prior_session_date = prior_ordered[-1].timestamp.date() if prior_ordered else None
        prior_misaligned = self._misaligned_count(prior_ordered, session_date=prior_session_date) if prior_session_date else 0
        current_gaps = self._session_gap_count(current_ordered, session_date=session.session_date)
        prior_gaps = self._session_gap_count(prior_ordered, session_date=prior_session_date) if prior_session_date else 0
        current_boundary_violations = self._boundary_violation_count(current_ordered, session_date=session.session_date)
        prior_boundary_violations = self._boundary_violation_count(prior_ordered, session_date=prior_session_date) if prior_session_date else 0

        checks.append(
            self._check(
                "current_bar_count",
                "Current session bar count",
                len(current_ordered) >= self.min_current_bars,
                observed=len(current_ordered),
                threshold=f">= {self.min_current_bars}",
                detail="The replay snapshot must contain enough bars to build a stable session profile.",
            )
        )
        checks.append(
            self._check(
                "prior_session_present",
                "Prior session available",
                (not self.require_prior_session) or bool(prior_ordered),
                observed=bool(prior_ordered),
                threshold=self.require_prior_session,
                detail="Comparative Market Profile references require at least one prior completed session.",
            )
        )
        checks.append(
            self._check(
                "prior_bar_count",
                "Prior session bar count",
                (not self.require_prior_session) or len(prior_ordered) >= self.min_prior_bars,
                observed=len(prior_ordered),
                threshold=f">= {self.min_prior_bars}",
                detail="The prior session must be rich enough to compute comparative value references.",
            )
        )
        checks.append(
            self._check(
                "current_duplicates",
                "Current session duplicate timestamps",
                current_duplicates <= self.max_duplicate_timestamps,
                observed=current_duplicates,
                threshold=f"<= {self.max_duplicate_timestamps}",
                detail="Duplicate bars should be deduplicated before profile computation.",
            )
        )
        checks.append(
            self._check(
                "prior_duplicates",
                "Prior session duplicate timestamps",
                prior_duplicates <= self.max_duplicate_timestamps,
                observed=prior_duplicates,
                threshold=f"<= {self.max_duplicate_timestamps}",
                detail="Prior-session duplicates break comparative references.",
            )
        )
        checks.append(
            self._check(
                "current_out_of_order",
                "Current session out-of-order bars",
                current_out_of_order <= self.max_out_of_order_bars,
                observed=current_out_of_order,
                threshold=f"<= {self.max_out_of_order_bars}",
                detail="Raw bars must preserve event order before aggregation.",
            )
        )
        checks.append(
            self._check(
                "prior_out_of_order",
                "Prior session out-of-order bars",
                prior_out_of_order <= self.max_out_of_order_bars,
                observed=prior_out_of_order,
                threshold=f"<= {self.max_out_of_order_bars}",
                detail="Prior-session bars must also be ordered for deterministic rebuilds.",
            )
        )
        checks.append(
            self._check(
                "current_session_gaps",
                "Current session silent gaps",
                current_gaps <= self.max_current_session_gaps,
                observed=current_gaps,
                threshold=f"<= {self.max_current_session_gaps}",
                detail="There should be no silent gaps between session open and the latest validated bar.",
            )
        )
        checks.append(
            self._check(
                "prior_session_gaps",
                "Prior session silent gaps",
                prior_gaps <= self.max_prior_session_gaps,
                observed=prior_gaps,
                threshold=f"<= {self.max_prior_session_gaps}",
                detail="Comparative value references are only trustworthy when the prior session is complete.",
            )
        )
        checks.append(
            self._check(
                "current_alignment",
                "Current session bar alignment",
                current_misaligned <= self.max_misaligned_bars,
                observed=current_misaligned,
                threshold=f"<= {self.max_misaligned_bars}",
                detail="All bars must align to the configured Market Profile period from session open.",
            )
        )
        checks.append(
            self._check(
                "prior_alignment",
                "Prior session bar alignment",
                prior_misaligned <= self.max_misaligned_bars,
                observed=prior_misaligned,
                threshold=f"<= {self.max_misaligned_bars}",
                detail="Prior-session bars must align to the same period ladder for reproducible comparisons.",
            )
        )
        checks.append(
            self._check(
                "current_session_boundaries",
                "Current session boundary violations",
                current_boundary_violations == 0,
                observed=current_boundary_violations,
                threshold="= 0",
                detail="Bars must stay within the configured session date and trading hours.",
            )
        )
        checks.append(
            self._check(
                "prior_session_boundaries",
                "Prior session boundary violations",
                prior_boundary_violations == 0,
                observed=prior_boundary_violations,
                threshold="= 0",
                detail="Prior-session bars must also stay within their own trading hours.",
            )
        )

        current_hashes = self._profile_rebuild_hashes(session.symbol, current_ordered)
        prior_hashes = self._profile_rebuild_hashes(session.symbol, prior_ordered) if prior_ordered else []
        checks.append(
            self._check(
                "current_rebuild_deterministic",
                "Current profile deterministic rebuild",
                len(set(current_hashes)) == 1,
                observed=current_hashes,
                detail="Repeated rebuilds from the same raw data must produce the same Market Profile snapshot.",
            )
        )
        checks.append(
            self._check(
                "prior_rebuild_deterministic",
                "Prior profile deterministic rebuild",
                (not prior_hashes) or len(set(prior_hashes)) == 1,
                observed=prior_hashes or ["not_run"],
                severity="warning" if not prior_hashes else "error",
                detail="The prior session profile must also be reproducible from raw bars.",
            )
        )

        current_profile = self.market_profile.build_profile(session.symbol, current_ordered)
        checks.extend(self._profile_invariant_checks(current_profile, prefix="current"))
        if prior_ordered:
            prior_profile = self.market_profile.build_profile(session.symbol, prior_ordered)
            checks.extend(self._profile_invariant_checks(prior_profile, prefix="prior"))
        else:
            prior_profile = None

        error_checks = [check for check in checks if check.severity == "error"]
        passed_errors = sum(1 for check in error_checks if check.passed)
        score = round((passed_errors / len(error_checks)) if error_checks else 1.0, 4)
        passed = all(check.passed for check in error_checks)

        metrics = {
            "current_bar_count": len(current_ordered),
            "prior_bar_count": len(prior_ordered),
            "current_duplicate_timestamps": current_duplicates,
            "prior_duplicate_timestamps": prior_duplicates,
            "current_out_of_order_bars": current_out_of_order,
            "prior_out_of_order_bars": prior_out_of_order,
            "current_session_gaps": current_gaps,
            "prior_session_gaps": prior_gaps,
            "current_misaligned_bars": current_misaligned,
            "prior_misaligned_bars": prior_misaligned,
            "current_boundary_violations": current_boundary_violations,
            "prior_boundary_violations": prior_boundary_violations,
            "current_profile_hash": current_hashes[0] if current_hashes else None,
            "prior_profile_hash": prior_hashes[0] if prior_hashes else None,
            "current_poc": current_profile.poc,
            "current_vah": current_profile.vah,
            "current_val": current_profile.val,
            "current_period_count": current_profile.period_count,
            "passed_checks": sum(1 for check in checks if check.passed),
            "total_checks": len(checks),
        }
        if prior_profile is not None:
            metrics["prior_poc"] = prior_profile.poc
            metrics["prior_vah"] = prior_profile.vah
            metrics["prior_val"] = prior_profile.val
            metrics["prior_period_count"] = prior_profile.period_count

        return ValidationReport(
            gate="gate_a",
            label="Data and feature engine",
            passed=passed,
            score=score,
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            checks=checks,
            metrics=metrics,
            pending_checks=[
                "Known-day labeled session tests are still manual and not automated yet.",
                "Walk-forward setup validation belongs to Gate B and is not part of this report yet.",
                "Live execution drift and reconciliation checks start in shadow-mode validation, not Gate A.",
            ],
        )

    def _profile_invariant_checks(self, profile, *, prefix: str) -> list[ValidationCheck]:
        label_prefix = "Current" if prefix == "current" else "Prior"
        return [
            self._check(
                f"{prefix}_value_area_order",
                f"{label_prefix} value area ordering",
                profile.val <= profile.poc <= profile.vah,
                observed={"val": profile.val, "poc": profile.poc, "vah": profile.vah},
                detail="The POC must sit inside the value area.",
            ),
            self._check(
                f"{prefix}_initial_balance_order",
                f"{label_prefix} initial balance ordering",
                profile.initial_balance_low <= profile.initial_balance_high,
                observed={
                    "ib_low": profile.initial_balance_low,
                    "ib_high": profile.initial_balance_high,
                },
                detail="The initial balance high must not be below the initial balance low.",
            ),
            self._check(
                f"{prefix}_range_consistency",
                f"{label_prefix} day range consistency",
                profile.day_range + 1e-9 >= profile.initial_balance_range,
                observed={
                    "day_range": profile.day_range,
                    "ib_range": profile.initial_balance_range,
                },
                detail="The full session range cannot be smaller than the initial balance range.",
            ),
        ]

    def _profile_rebuild_hashes(self, symbol: str, bars: list[MarketBar]) -> list[str]:
        if not bars:
            return []
        hashes: list[str] = []
        for _ in range(2):
            profile = self.market_profile.build_profile(symbol, bars)
            payload = json.dumps(
                jsonable_encoder(asdict(profile)),
                sort_keys=True,
                separators=(",", ":"),
            )
            hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
        return hashes

    def _duplicate_count(self, bars: list[MarketBar]) -> int:
        if not bars:
            return 0
        counts = Counter(bar.timestamp for bar in bars)
        return sum(count - 1 for count in counts.values() if count > 1)

    def _out_of_order_count(self, bars: list[MarketBar]) -> int:
        if len(bars) < 2:
            return 0
        return sum(1 for left, right in zip(bars, bars[1:]) if right.timestamp < left.timestamp)

    def _misaligned_count(self, bars: list[MarketBar], *, session_date) -> int:
        if not bars or session_date is None:
            return 0
        session_open = self._session_anchor(session_date, bars[0].timestamp.tzinfo)
        step_seconds = self.period_minutes * 60
        count = 0
        for bar in bars:
            diff_seconds = int((bar.timestamp - session_open).total_seconds())
            if diff_seconds < 0 or diff_seconds % step_seconds != 0:
                count += 1
        return count

    def _boundary_violation_count(self, bars: list[MarketBar], *, session_date) -> int:
        if not bars or session_date is None:
            return 0
        return sum(
            1
            for bar in bars
            if bar.timestamp.date() != session_date
            or bar.timestamp.timetz().replace(tzinfo=None) < self.session_open
            or bar.timestamp.timetz().replace(tzinfo=None) > self.session_close
        )

    def _session_gap_count(self, bars: list[MarketBar], *, session_date) -> int:
        if not bars or session_date is None:
            return 0
        ordered = sorted(bars, key=lambda item: item.timestamp)
        session_open = self._session_anchor(session_date, ordered[0].timestamp.tzinfo)
        latest = ordered[-1].timestamp
        if latest < session_open:
            return 0

        expected: set[datetime] = set()
        cursor = session_open
        step = timedelta(minutes=self.period_minutes)
        while cursor <= latest:
            expected.add(cursor)
            cursor += step
        observed = {bar.timestamp for bar in ordered}
        return len(expected - observed)

    def _session_anchor(self, session_date, tzinfo) -> datetime:
        return datetime.combine(session_date, self.session_open, tzinfo=tzinfo)

    def _check(
        self,
        key: str,
        label: str,
        passed: bool,
        *,
        observed: Any,
        threshold: Any | None = None,
        severity: str = "error",
        detail: str = "",
    ) -> ValidationCheck:
        return ValidationCheck(
            key=key,
            label=label,
            passed=passed,
            observed=observed,
            threshold=threshold,
            severity=severity,
            detail=detail,
        )

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":")
        return time(hour=int(hour), minute=int(minute))
