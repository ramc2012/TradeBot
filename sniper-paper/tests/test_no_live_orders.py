"""Non-negotiable lint rule: no live broker order placement code.

Scans the entire src/sniper_paper tree for forbidden imports or function calls
that would suggest live order placement.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


FORBIDDEN_PATTERNS = [
    # Fyers SDK trading endpoints
    r"\bplace_order\b",
    r"\bmodify_order\b",
    r"\bexit_positions\b",
    r"\bcancel_order\b",
    # OpenAlgo / generic execution
    r"\bplace_market_order\b",
    r"\bplace_limit_order\b",
    r"from\s+fyers_apiv3\.fyersModel",   # the order-placement model
]

SRC = Path(__file__).resolve().parents[1] / "src" / "sniper_paper"


def test_no_live_order_code():
    offenders = []
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        for pat in FORBIDDEN_PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text[: m.start()].count("\n") + 1
                offenders.append(f"{py.relative_to(SRC)}:{line_no}: matches {pat!r}")
    assert not offenders, (
        "Forbidden live-order code in paper-only codebase:\n  " + "\n  ".join(offenders)
    )
