"""Shared pytest configuration for the TradeBot backend test suite.

Test-harness hygiene handled here:

1. Runtime state isolation.
   Several backend modules resolve their on-disk state paths *at import time* — e.g.
   ``paper_engine.strategy_agent_state._resolve_strategy_state_file()`` reads the
   ``NSE_STRATEGY_STATE_FILE`` env var when the module is first imported and caches it
   in ``_NSE_STRATEGY_STATE_FILE``. If we don't intervene, the suite reads (and can
   overwrite) the real production ``nse_strategy_state.json`` — hundreds of KB of live
   paper-trading state. We repoint that env var at a throwaway directory *before* any
   test module triggers those imports, and give every test an empty state file so no
   test inherits state persisted by an earlier one (the source of the previously
   order-dependent ``test_paper_strategy_agent`` flake).

2. Event-loop isolation.
   Tests that drive coroutines through ``asyncio.get_event_loop()`` (rather than
   ``asyncio.run()`` or ``@pytest.mark.asyncio``) used to inherit whatever loop the
   ambient thread had — which a prior test's ``asyncio.run()`` leaves as *no current
   loop*, raising "There is no current event loop". Those two modules
   (``test_cbe_paper_book`` and ``test_live_marks_guard``) now own a dedicated
   persistent loop, so each is self-isolating without a global fixture that could
   fight pytest-asyncio's own loop management.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# NOTE: this runs at conftest import (before test modules, hence before the backend
# modules they import), so _resolve_strategy_state_file() sees the throwaway path.
_TEST_STATE_DIR = Path(tempfile.mkdtemp(prefix="tradebot-test-state-"))
_TEST_NSE_STATE_FILE = _TEST_STATE_DIR / "nse_strategy_state.json"
os.environ["NSE_STRATEGY_STATE_FILE"] = str(_TEST_NSE_STATE_FILE)

import pytest


@pytest.fixture(autouse=True)
def _clean_nse_strategy_state_file():
    """Start every test from an empty on-disk NSE strategy state file so persisted
    state written by one test never leaks into another."""
    try:
        if _TEST_NSE_STATE_FILE.exists():
            _TEST_NSE_STATE_FILE.unlink()
    except OSError:
        pass
    yield
