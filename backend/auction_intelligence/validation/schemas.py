from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ValidationSeverity = Literal["error", "warning", "info"]


@dataclass
class ValidationCheck:
    key: str
    label: str
    passed: bool
    observed: Any
    threshold: Any | None = None
    severity: ValidationSeverity = "error"
    detail: str = ""


@dataclass
class ValidationArtifact:
    artifact_type: str
    artifact_key: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    gate: str
    label: str
    passed: bool
    score: float
    generated_at: str
    checks: list[ValidationCheck] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    pending_checks: list[str] = field(default_factory=list)
    artifacts: list[ValidationArtifact] = field(default_factory=list)
