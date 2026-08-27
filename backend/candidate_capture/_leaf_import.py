"""Load a LEAF module by file path, without executing its package __init__.

Why this exists
───────────────
Two modules elsewhere in this repo hold logic this package must not duplicate:

  paper_engine/costs.py              the only current statutory option rates
  directional_options/calibration.py the only isotonic (PAV) calibrator

Both files are themselves clean leaves — stdlib imports only. But importing
either the normal way executes its PACKAGE `__init__`, and both packages mount
things this observer must never be able to reach:
`paper_engine/__init__` imports `PaperOrderBook`, and
`directional_options/__init__` imports the whole service, which carries a paper
position book.

There is no central kill switch in this codebase — whether code can reach a
book is decided purely by what it imports — so `candidate_capture`'s read-only
guarantee is an assertion about its import list, enforced in
`tests/test_candidate_capture_service.py`. Copying the logic instead would give
this repo a fifth rate table and a second calibrator, which is precisely how the
existing four-cost-models problem arose.

Loading the leaf directly satisfies both constraints at once: one source of
truth for the logic, and no package side effect reachable from here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_CACHE: dict[str, ModuleType] = {}

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def load_leaf(relative_path: str, *, alias: str) -> ModuleType:
    """Import `<backend>/<relative_path>` as a standalone module named `alias`.

    `alias` is deliberately not the module's real dotted name, so the loaded
    copy can never be mistaken for (or collide with) a normal import of the
    same file elsewhere in the process.
    """
    cached = _CACHE.get(alias)
    if cached is not None:
        return cached

    path = BACKEND_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ImportError(f"could not load leaf module {relative_path} from {path}")
    module = importlib.util.module_from_spec(spec)
    # REGISTER BEFORE EXECUTING. `dataclasses` resolves string annotations via
    # `sys.modules[cls.__module__]`, so a module that defines a dataclass and is
    # not registered raises `AttributeError: 'NoneType' object has no attribute
    # '__dict__'` at import — which is exactly what the isotonic calibrator does.
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(alias, None)
        raise
    _CACHE[alias] = module
    return module


def statutory_rates() -> Any:
    """`paper_engine/costs.py` — the current option STT / exchange / GST rates."""
    return load_leaf("paper_engine/costs.py", alias="_cc_statutory_rates")


def isotonic() -> Any:
    """`directional_options/calibration.py` — PAV isotonic + Brier scoring."""
    return load_leaf("directional_options/calibration.py", alias="_cc_isotonic")
