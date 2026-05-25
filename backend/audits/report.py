from __future__ import annotations

import json

from audits.base import AuditResult, InvariantResult


_LIGHT = {"pass": "🟢", "fail": "🔴", "na": "🟡"}


def _light(inv) -> str:
    return _LIGHT.get(inv.status, "⚪")


def render_markdown(r: AuditResult) -> str:
    lines: list[str] = []
    lines.append(f"# Lane audit — {r.lane.upper()} ({r.metadata.get('label', '')})")
    lines.append("")
    lines.append(f"**Audit date:** {r.audit_date.isoformat()}  ")
    lines.append(f"**Window:** {r.window_start.isoformat()} → {r.window_end.isoformat()}  ")
    lines.append(f"**Overall:** {_overall_badge(r.overall_status)}")
    lines.append("")
    lines.append("## Invariants")
    lines.append("")
    lines.append("| # | Invariant | Status |")
    lines.append("|---|-----------|--------|")
    for i, inv in enumerate(r.invariants, 1):
        lines.append(f"| {i} | {inv.name} | {_light(inv)} |")
    lines.append("")
    for inv in r.invariants:
        lines.append(f"### {_light(inv)} {inv.name}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(inv.detail, indent=2, default=str))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _overall_badge(status: str) -> str:
    return {"green": "🟢 GREEN", "yellow": "🟡 YELLOW", "red": "🔴 RED"}.get(status, status)
