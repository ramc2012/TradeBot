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
