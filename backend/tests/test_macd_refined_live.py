from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pandas as pd

from macd_refined.config import clone_default_config
from macd_refined.live import MacdRefinedLiveEngine


class _PaperStub:
    def capital_status(self) -> dict[str, float]:
        return {"total_equity": 5_000_000.0}

    def list_positions(self, **_kwargs) -> dict[str, list]:
        return {"open_positions": []}

    def sync_cycle(self, **_kwargs) -> dict[str, int]:
        return {"open_positions": 0}


class _StoreStub:
    def lot_size_for(self, _underlying: str) -> int:
        return 50


def _engine(tmp_path: Path) -> MacdRefinedLiveEngine:
    config = clone_default_config()
    config["live_universe_mode"] = "list"
    config["live_universe"] = ["NIFTY"]
    config["live"]["volume_store_root"] = str(tmp_path / "volume_tracking")
    return MacdRefinedLiveEngine(_StoreStub(), _PaperStub(), config)


def test_default_config_retains_non_gating_iv_mapping_fallbacks() -> None:
    filters = clone_default_config()["filters"]

    assert filters["iv_gate_enabled"] is False
    assert filters["iv_below_median_ratio"] == 0.80
    assert filters["iv_below_realized_vol"] is True


def test_snapshot_parquet_store_appends_without_losing_history(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first = [{"captured_at": "2026-06-30T04:00:00+00:00", "underlying": "NIFTY", "ltp": 100.0}]
    second = [{"captured_at": "2026-06-30T04:05:00+00:00", "underlying": "NIFTY", "ltp": 101.0}]

    assert engine._persist_snapshots("NIFTY", first) == 1
    assert engine._persist_snapshots("NIFTY", second) == 1

    stored = pd.read_parquet(engine._tracking_path("NIFTY"))
    assert stored["ltp"].tolist() == [100.0, 101.0]


def test_live_cycle_fails_once_on_missing_parquet_engine(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)

    async def _adapter():
        return object()

    async def _universe():
        return ["NIFTY", "BANKNIFTY"]

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "_parquet_storage_error", lambda: "Parquet engine missing")

    result = asyncio.run(engine.run_cycle(allow_entries=False))

    assert result["broker_ready"] is True
    assert result["storage_ready"] is False
    assert result["snapshots_persisted"] == 0
    assert result["failures"] == {"storage": "Parquet engine missing"}


def test_live_cycle_processes_names_with_bounded_concurrency(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.config["live"]["max_concurrent_names"] = 3
    active = 0
    max_active = 0

    class _Adapter:
        async def get_option_chain(self, _symbol: str, _expiry: str):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return object()

    async def _adapter():
        return _Adapter()

    async def _universe():
        return [f"SYM{i}" for i in range(6)]

    async def _evaluate(_adapter, _underlying, _expiries, diag, _signal_updates):
        diag["legs_evaluated"] = 2
        return []

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "resolve_expiries", lambda *_args, **_kwargs: [date(2026, 7, 28)])
    monkeypatch.setattr(engine, "_chain_to_rows", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(engine, "_evaluate", _evaluate)

    result = asyncio.run(engine.run_cycle(allow_entries=False))

    assert max_active == 3
    assert len(result["fetched"]) == 6
    assert result["failures"] == {}
    assert result["funnel"]["legs_evaluated"] == 12


def test_live_cycle_commits_completed_name_before_slow_name_finishes(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    syncs: list[list[dict]] = []

    class _Paper(_PaperStub):
        def sync_cycle(self, **kwargs):
            syncs.append(list(kwargs.get("proposals") or []))
            return {"open_positions": len(syncs)}

    engine.paper = _Paper()

    class _Adapter:
        async def get_option_chain(self, symbol: str, _expiry: str):
            if "SLOW" in symbol:
                await asyncio.sleep(1.0)
            return object()

    async def _adapter():
        return _Adapter()

    async def _universe():
        return ["FAST", "SLOW"]

    async def _evaluate(_adapter, underlying, _expiries, _diag, signal_updates):
        signal_updates[f"{underlying}|signal"] = "2026-07-13T03:30:00+00:00"
        return [{"underlying": underlying, "option_type": "CE", "quantity_units": 1}]

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "resolve_expiries", lambda *_args, **_kwargs: [date(2026, 7, 28)])
    monkeypatch.setattr(engine, "_chain_to_rows", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(engine, "_evaluate", _evaluate)

    async def _run_with_timeout():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(engine.run_cycle(allow_entries=True), timeout=0.1)

    import pytest
    asyncio.run(_run_with_timeout())

    assert syncs == [[{"underlying": "FAST", "option_type": "CE", "quantity_units": 1}]]
    assert engine._signal_state["FAST|signal"] == "2026-07-13T03:30:00+00:00"
    assert "SLOW|signal" not in engine._signal_state


def test_live_cycle_times_out_one_name_without_losing_full_universe_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path)
    engine.config["live"]["name_timeout_seconds"] = 0.03

    class _Adapter:
        async def get_option_chain(self, symbol: str, _expiry: str):
            if "SLOW" in symbol:
                await asyncio.sleep(1.0)
            return object()

    async def _adapter():
        return _Adapter()

    async def _universe():
        return ["FAST", "SLOW"]

    async def _evaluate(_adapter, _underlying, _expiries, _diag, _signal_updates):
        return []

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "resolve_expiries", lambda *_args, **_kwargs: [date(2026, 7, 28)])
    monkeypatch.setattr(engine, "_chain_to_rows", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(engine, "_evaluate", _evaluate)

    result = asyncio.run(engine.run_cycle(allow_entries=False))

    assert list(result["fetched"]) == ["FAST"]
    assert result["failures"] == {"SLOW": "name scan timed out after 0.03s"}


def test_queued_names_are_not_starved_by_the_per_name_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: the per-name timeout must not consume time spent QUEUED.

    Every name-task is created up-front, so with `max_concurrent_names` slots
    the rest sit on the semaphore.  When the timeout wrapped the semaphore
    acquisition, a queued name's clock ran while it held no slot, so names past
    roughly `slots * (timeout / per_name_work)` were reported as timed out
    having done no work — in production 203-215 of a 216-name universe failed
    every cycle and the same first ~30 names won every time.

    Here 8 names each need 0.10s of work through a single slot (0.80s serial)
    while the per-name timeout is 0.30s.  If the timeout covered queue time,
    every name after the third would fail; all 8 must succeed.
    """
    engine = _engine(tmp_path)
    engine.config["live"]["name_timeout_seconds"] = 0.30
    engine.config["live"]["max_concurrent_names"] = 1

    names = [f"NAME{i}" for i in range(8)]

    class _Adapter:
        async def get_option_chain(self, _symbol: str, _expiry: str):
            await asyncio.sleep(0.10)   # real work, comfortably inside 0.30s
            return object()

    async def _adapter():
        return _Adapter()

    async def _universe():
        return list(names)

    async def _evaluate(_adapter, _underlying, _expiries, _diag, _signal_updates):
        return []

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "resolve_expiries", lambda *_a, **_k: [date(2026, 7, 28)])
    monkeypatch.setattr(engine, "_chain_to_rows", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(engine, "_evaluate", _evaluate)

    result = asyncio.run(engine.run_cycle(allow_entries=False))

    assert result["failures"] == {}, f"queued names starved: {result['failures']}"
    assert sorted(result["fetched"]) == sorted(names)


def test_universe_rotates_so_the_tail_is_not_starved(tmp_path: Path, monkeypatch) -> None:
    """Regression: consecutive cycles must not re-scan the same leading names.

    A full sweep does not fit in one cycle (Fyers 200 req/min shared, CLASS_BULK
    capped at 25% -> ~50 req/min; ~4 calls per name over 216 names). The order
    used to be identical every cycle, so the leading names consumed the whole
    budget and the tail was NEVER reached — failure_count sat at 203-215/216.

    Here only 2 of 6 names can be served per cycle. Across three cycles the
    lane must cover DIFFERENT names and eventually all of them.
    """
    engine = _engine(tmp_path)
    engine.config["live"]["max_concurrent_names"] = 1
    names = [f"N{i}" for i in range(6)]
    served: list[str] = []
    budget = {"left": 0}

    class _Adapter:
        async def get_option_chain(self, symbol: str, _expiry: str):
            if budget["left"] <= 0:
                await asyncio.sleep(5)          # starved: will hit the name timeout
            budget["left"] -= 1
            return object()

    async def _adapter():
        return _Adapter()

    async def _universe():
        return list(names)

    async def _evaluate(_a, underlying, _e, _d, _s):
        served.append(underlying)
        return []

    monkeypatch.setattr(engine, "_adapter", _adapter)
    monkeypatch.setattr(engine, "_resolve_universe", _universe)
    monkeypatch.setattr(engine, "resolve_expiries", lambda *_a, **_k: [date(2026, 7, 28)])
    monkeypatch.setattr(engine, "_chain_to_rows", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(engine, "_evaluate", _evaluate)
    engine.config["live"]["name_timeout_seconds"] = 0.25

    covered_per_cycle = []
    for _ in range(3):
        budget["left"] = 4                      # 4 chain calls = 2 names/cycle
        served.clear()
        result = asyncio.run(engine.run_cycle(allow_entries=False))
        covered_per_cycle.append(sorted(result["fetched"]))

    # Cycle 2 must not repeat cycle 1 — that was the starvation bug.
    assert covered_per_cycle[0] != covered_per_cycle[1], (
        f"universe did not rotate: {covered_per_cycle}"
    )
    seen = {n for cycle in covered_per_cycle for n in cycle}
    assert len(seen) > len(covered_per_cycle[0]), (
        f"rotation covered no new names across cycles: {covered_per_cycle}"
    )
