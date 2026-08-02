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

3. OpenBLAS single-thread + main-thread LAPACK warm-up.
   numpy links a pthread-based OpenBLAS whose worker-pool initialisation can
   deadlock when the process's *first* LAPACK call happens on a non-main thread.
   The directional-options service runs its policy pick via ``asyncio.to_thread``
   → ``np.linalg.inv``, so ``tests/test_directional_options.py`` hung forever when
   run standalone (nothing had touched BLAS yet when the worker thread called
   ``inv``), while the full suite passed only because earlier test files happened
   to do main-thread linear algebra first. Pin OpenBLAS to one thread (no worker
   pool at all — the suite's matrices are tiny, threading only adds overhead) and
   do a trivial ``inv`` on the main thread at conftest import, so a single-file
   run initialises BLAS exactly like the full suite does.
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

# Point the sector-interaction ingestion store at a throwaway dir: the REAL
# store (backend/runtime/sector_interaction, bind-mounted) holds live
# observations since 2026-08-02, so without this the suite both READS
# production data (flipping runtime-handoff assertions) and could WRITE test
# observations into the live store.
os.environ["SECTOR_INGESTION_ROOT"] = str(_TEST_STATE_DIR / "sector_interaction")

# NOTE: must be set before numpy is first imported anywhere in the process —
# OpenBLAS reads it once, when the shared library initialises (see docstring #3).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# The suite is written against the single-process default (ALL supervisor
# runners scheduled). Since the Phase-1 process split went live (2026-07-28)
# the repo-root .env pins LANESET=core for the deployed core container, and
# pydantic-settings reads that .env on the host too — which silently dropped
# every strategy-plane runner from the supervisor tests. A real env var beats
# the .env file, so pin the suite back to the all-plane default here; the
# laneset-split tests monkeypatch settings.LANESET themselves.
os.environ["LANESET"] = "all"

import numpy as _np
import pytest

# Main-thread BLAS/LAPACK warm-up (docstring #3): harmless when OpenBLAS is
# single-threaded, and the safety net if OPENBLAS_NUM_THREADS was overridden or
# numpy was already imported before the env var above could take effect.
_np.linalg.inv(_np.eye(2))


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


@pytest.fixture(autouse=True)
def _isolate_runtime_case_root(monkeypatch, tmp_path):
    """agentic_rag.sources.collect_runtime_trade_cases() globs paper_positions.json /
    paper_journal.jsonl out of the real backend/runtime/ tree — which the live trading
    system rewrites continuously. Any test that aggregates runtime cases (e.g. the
    context-gate expectancy check) would otherwise read a moving production target and
    flake. Point RUNTIME_ROOT at an empty per-test dir so those tests see only the
    cases they explicitly inject. No test reads real runtime data, so this is safe
    suite-wide.
    """
    try:
        import agentic_rag.sources as _rag_sources

        monkeypatch.setattr(_rag_sources, "RUNTIME_ROOT", tmp_path / "runtime", raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _atm_live_refresh_window_not_enforced(monkeypatch):
    """The ATM watchlist demotes live_refresh → cached outside 07:00–16:35 IST
    on NSE session days (the 2026-07-16 overnight-rebuild fix). Left enforced,
    every live_refresh=True test would silently flip behavior depending on the
    wall-clock the suite runs at. Disable enforcement suite-wide; the window
    tests exercise `_live_refresh_allowed` with explicit datetimes by turning
    the flag back on via monkeypatch."""
    try:
        import market_data.atm_watchlist as _atm

        monkeypatch.setattr(_atm, "_LIVE_REFRESH_WINDOW_ENFORCED", False, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _stub_telegram_singleton_network(request, monkeypatch):
    """S1 trade alerts now route through the unified notifications singleton
    (`notifications.telegram_agent.telegram_agent`). Tests that set fake bot
    creds (e.g. auth-persistence) would otherwise make the singleton attempt a
    REAL network post, and its accumulated failure health then leaks into the
    system-health payload of unrelated tests. Stub the singleton's send to a
    no-op suite-wide; tests that exercise TelegramAgent behavior construct
    their own instances (see test_telegram_agent.py) and are unaffected.
    Tests marked with `telegram_singleton_live` opt out (they patch httpx
    themselves and assert on the singleton).
    """
    if request.node.get_closest_marker("telegram_singleton_live"):
        yield
        return
    try:
        from notifications import telegram_agent as _ta

        async def _noop_send(*_a, **_k):
            return False

        # Patch the CLASS, not the instance: polluter tests leak background
        # supervisor loops that keep sending BETWEEN tests (when an instance
        # patch is already undone) — with fake creds those were REAL 401s to
        # api.telegram.org that flipped the notifications health to critical
        # mid-test. A class-level stub catches every instance on every loop.
        monkeypatch.setattr(_ta.TelegramAgent, "send", _noop_send)
        # Also reset the singleton's health counters: system-health tests read
        # the REAL notifications service, so failure state leaked from any
        # earlier test would flip their expected summary status.
        agent = _ta.telegram_agent
        agent._sent_ok = 0
        agent._failed_http = 0
        agent._failed_auth = 0
        agent._failed_transport = 0
        agent._suppressed_rate_limit = 0
        agent._suppressed_dedup = 0
        agent._suppressed_no_creds = 0
        agent._last_success_at = None
        agent._last_failure_at = None
        agent._last_error_status = None
        agent._consecutive_failures = 0
        agent._auth_alert_date = None
    except Exception:
        pass
    yield
