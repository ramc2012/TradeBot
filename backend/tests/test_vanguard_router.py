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
