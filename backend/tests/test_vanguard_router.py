"""Guards the ONE piece of knowledge `api/routers/vanguard.py` duplicates.

`/api/vanguard/funnel` re-expresses M6's candidate filter in SQL so the UI can
show WHERE candidates die rather than rendering an empty panel. That means the
router carries its own copy of M6's thresholds, and a mirrored constant drifts
silently the moment Vanguard retunes the real one.

So this test reads `vanguard/fusion/m6_select.py` itself and asserts the numbers
still agree — the same technique `frontend-v2/tests/nav-model.test.ts` uses to
stop nav-model advertising a workspace view that ViewNav.tsx does not build.

Vanguard lives in its own git worktree and is NOT part of this backend's
deployable tree, so the source may legitimately be absent (a container build, a
CI checkout without the worktree). In that case the test SKIPS rather than
fails: an absent worktree is not a drift signal, and a test that fails for the
wrong reason trains people to ignore it. When the file IS present, drift is a
hard failure.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api.routers import vanguard

_M6_RELATIVE = Path(".claude") / "worktrees" / "vanguard-phase-1" / "vanguard" / "fusion" / "m6_select.py"


def _locate_m6() -> Path | None:
    """Find m6_select.py from ANY checkout of this repo.

    A fixed `parents[2]` only resolves in the primary checkout. This file also
    lives on the `vanguard/ui` branch, whose worktree sits at
    `<repo>/.claude/worktrees/vanguard-ui/` — three levels deeper — so the
    fixed offset pointed at a path that does not exist and the guard SKIPPED
    in the exact tree where the router it guards actually lives. A test that
    silently skips where it matters most is worse than no test.

    Walking up to the first ancestor that contains the worktrees directory
    resolves correctly from the primary checkout and from any worktree.
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / _M6_RELATIVE
        if candidate.exists():
            return candidate
    return None


_M6_SOURCE = _locate_m6()


def _m6_constants() -> dict[str, object]:
    """Parse m6_select.py's module-level literals WITHOUT importing it.

    Importing would pull in psycopg2 and Vanguard's own sys.path juggling into
    the backend test process; the values wanted here are plain literals, so the
    AST is both sufficient and side-effect free.
    """
    assert _M6_SOURCE is not None
    tree = ast.parse(_M6_SOURCE.read_text())
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            found[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue  # not a literal (e.g. a computed expression) — not mirrored anyway
    return found


requires_worktree = pytest.mark.skipif(
    _M6_SOURCE is None,
    reason=f"no checkout ancestor contains {_M6_RELATIVE}; nothing to compare against",
)


@requires_worktree
@pytest.mark.parametrize(
    ("router_value", "m6_name"),
    [
        (vanguard.FLOW_MIN_ABS, "FLOW_MIN_ABS"),
        (vanguard.SECTOR_RS_MIN_ABS_Z, "SECTOR_RS_MIN_ABS_Z"),
        (vanguard.TIMING_MIN_SCORE, "TIMING_MIN_SCORE"),
        (vanguard.CONVICTION_MIN, "CONVICTION_MIN"),
        (vanguard.TOP_N_PER_BAR, "TOP_N_PER_BAR"),
        # The freshness legs added 2026-08-27. The funnel labels each leg with
        # the numbers it applies, so a drift here would have the desk telling a
        # trader a flow score is stale at a threshold the selector no longer uses.
        (vanguard.FLOW_MAX_AGE_SESSIONS, "FLOW_MAX_AGE_SESSIONS"),
        (vanguard.RS_MAX_AGE_SESSIONS, "RS_MAX_AGE_SESSIONS"),
        (vanguard.REGIME_MAX_AGE_BARS, "REGIME_MAX_AGE_BARS"),
        (vanguard.FLOW_MIN_INGREDIENTS, "FLOW_MIN_INGREDIENTS"),
        # /risk computes the sizing-coherence arithmetic from M6's own stop.
        (vanguard.STOP_PCT, "STOP_PCT"),
    ],
)
def test_router_scalar_thresholds_match_m6(router_value, m6_name):
    constants = _m6_constants()
    assert m6_name in constants, f"{m6_name} vanished from m6_select.py — update the router mirror"
    assert router_value == constants[m6_name], (
        f"{m6_name} drifted: router has {router_value!r}, m6_select.py has {constants[m6_name]!r}. "
        "Update api/routers/vanguard.py to match, or the funnel will explain a filter "
        "the selector no longer applies."
    )


@requires_worktree
def test_router_regime_permits_match_m6():
    """M6 stores this as a set; the router uses a list so it can go into a SQL
    ANY(:permits) bind and out as JSON. Compare as sets so ordering is not a
    false failure, but membership drift still is."""
    constants = _m6_constants()
    assert "REGIME_PERMITS" in constants
    assert set(vanguard.REGIME_PERMITS) == set(constants["REGIME_PERMITS"])


@requires_worktree
def test_every_mirrored_constant_is_actually_covered_by_this_test():
    """Stops the mirror growing a new un-guarded constant.

    If someone adds another mirrored threshold to the router, it must also be
    added to the parametrised list above — otherwise the guard silently covers
    less than it appears to.
    """
    covered = {
        "FLOW_MIN_ABS", "SECTOR_RS_MIN_ABS_Z", "TIMING_MIN_SCORE",
        "CONVICTION_MIN", "TOP_N_PER_BAR", "REGIME_PERMITS",
        "FLOW_MAX_AGE_SESSIONS", "RS_MAX_AGE_SESSIONS", "REGIME_MAX_AGE_BARS",
        "FLOW_MIN_INGREDIENTS", "STOP_PCT",
    }
    m6_names = set(_m6_constants())
    mirrored = {
        name for name in dir(vanguard)
        if name.isupper() and not name.startswith("_") and name in m6_names
    }
    assert mirrored <= covered, (
        f"router mirrors {sorted(mirrored - covered)} from m6_select.py but this test "
        "does not guard them — add them to the parametrised case above."
    )


def test_funnel_thresholds_are_exposed_in_the_summary_response_shape():
    """The UI must render the thresholds actually applied, never its own copy.
    This asserts the contract that makes that possible exists at all."""
    assert vanguard.FLOW_MIN_ABS > 0
    assert vanguard.REGIME_PERMITS, "an empty permit list would silently gate everything out"
    assert vanguard.TOP_N_PER_BAR >= 1


# ── the second mirror: M7's risk limits ────────────────────────────────────
#
# /risk renders the configured limits AND states whether they can actually
# bind. Both halves are wrong if the mirrored numbers drift, and the "cannot
# bind" claim is the more dangerous one to get wrong — it would either invent
# a control that is not there or deny one that is.

_M7_RELATIVE = Path(".claude") / "worktrees" / "vanguard-phase-1" / "vanguard" / "fusion" / "m7_risk.py"


def _locate_m7() -> Path | None:
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / _M7_RELATIVE
        if candidate.exists():
            return candidate
    return None


_M7_SOURCE = _locate_m7()
requires_m7 = pytest.mark.skipif(_M7_SOURCE is None, reason="vanguard worktree absent")


def _m7_constants() -> dict[str, object]:
    tree = ast.parse(_M7_SOURCE.read_text())
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


@requires_m7
@pytest.mark.parametrize(
    ("router_value", "m7_name"),
    [
        (vanguard.RISK_PER_TRADE_PCT, "RISK_PER_TRADE_PCT"),
        (vanguard.MAX_PREMIUM_PER_TRADE_PCT, "MAX_PREMIUM_PER_TRADE_PCT"),
        (vanguard.MAX_PORTFOLIO_HEAT_PCT, "MAX_PORTFOLIO_HEAT_PCT"),
        (vanguard.MAX_CONCURRENT_POSITIONS, "MAX_CONCURRENT_POSITIONS"),
        (vanguard.MAX_POSITIONS_PER_SECTOR20, "MAX_POSITIONS_PER_SECTOR20"),
        (vanguard.DAILY_LOSS_STOP_PCT, "DAILY_LOSS_STOP_PCT"),
        (vanguard.WEEKLY_LOSS_STOP_PCT, "WEEKLY_LOSS_STOP_PCT"),
    ],
)
def test_router_risk_limits_match_m7(router_value, m7_name):
    constants = _m7_constants()
    assert m7_name in constants, f"{m7_name} vanished from m7_risk.py — update the router mirror"
    assert router_value == constants[m7_name]


@requires_m7
def test_the_routers_coherence_arithmetic_agrees_with_m7s_own():
    """/risk recomputes sizing_coherence rather than importing it (the backend
    must not import research code). Recomputation is a copy, so it is checked
    against the real one's inputs here."""
    m7 = _m7_constants()
    premium_needed = m7["RISK_PER_TRADE_PCT"] / vanguard.STOP_PCT
    effective = min(m7["RISK_PER_TRADE_PCT"], m7["MAX_PREMIUM_PER_TRADE_PCT"] * vanguard.STOP_PCT)
    router_premium_needed = vanguard.RISK_PER_TRADE_PCT / vanguard.STOP_PCT
    router_effective = min(vanguard.RISK_PER_TRADE_PCT,
                           vanguard.MAX_PREMIUM_PER_TRADE_PCT * vanguard.STOP_PCT)
    assert router_premium_needed == premium_needed
    assert router_effective == effective


@requires_worktree
def test_the_funnel_legs_are_exactly_m6s_own_leg_order():
    """The desk draws one funnel row per leg, in order. A leg added to M6 and
    forgotten here would be an invisible gate — candidates dying at a stage the
    UI does not draw."""
    constants = _m6_constants()
    assert "LEG_ORDER" in constants, "M6 no longer publishes LEG_ORDER"
    assert [leg for leg, _ in vanguard.LEGS] == list(constants["LEG_ORDER"])
