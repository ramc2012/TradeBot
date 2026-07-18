"""US MACD lane honesty (audit 2026-07-18).

The Alpaca data source is not deployed here (``brokers.alpaca`` does not exist,
and analysis/alpaca_data.py points at a non-existent local parquet path), yet
/summary used to present a full ready lane payload. The lane must surface
status="unavailable" while keeping every pre-existing key additive/compatible
(the UI reads params/automation/timeframe/live_universe from /summary and
configured/ok from /data-source-health).
"""
from __future__ import annotations

import asyncio

import api.routers.us_macd as us_macd


def _fake_summary() -> dict:
    return {
        "key": "us_macd_refined",
        "label": "US MACD Refined",
        "timeframe": "30minute",
        "live_universe": ["SPY", "QQQ"],
        "params": {"ema_fast": 12},
        "automation": {"enabled": False},
    }


def test_summary_surfaces_unavailable_when_alpaca_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(us_macd._service, "summary", _fake_summary)

    async def fake_health() -> dict:
        return {"provider": "alpaca", "configured": False,
                "error": "No module named 'brokers.alpaca'"}

    monkeypatch.setattr(us_macd, "_alpaca_source_health", fake_health)
    payload = asyncio.run(us_macd.summary())

    assert payload["status"] == "unavailable"
    assert "brokers.alpaca" in payload["status_reason"]
    assert payload["data_source"]["configured"] is False
    # Pre-existing keys the UI reads stay present (additive contract).
    for key in ("params", "automation", "timeframe", "live_universe"):
        assert key in payload


def test_summary_ready_when_alpaca_configured(monkeypatch) -> None:
    monkeypatch.setattr(us_macd._service, "summary", _fake_summary)

    async def fake_health() -> dict:
        return {"provider": "alpaca", "configured": True, "ok": True}

    monkeypatch.setattr(us_macd, "_alpaca_source_health", fake_health)
    payload = asyncio.run(us_macd.summary())

    assert payload["status"] == "ready"
    assert payload["status_reason"] is None


def test_data_source_health_reports_unavailable_on_this_deployment() -> None:
    # No monkeypatching: brokers.alpaca genuinely does not exist here, so the
    # real probe must report unconfigured + unavailable.
    health = asyncio.run(us_macd.data_source_health())
    assert health["configured"] is False
    assert health["status"] == "unavailable"


# ── Per-session data-audit re-run (audit 2026-07-18, cheap item) ──────────────


import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")


def _service_with_tmp_tracking(tmp_path, market: str = "india"):
    from macd_refined.config import clone_default_config
    from macd_refined.service import MacdRefinedService

    config = clone_default_config()
    config["market"] = market
    config["paper_trading"]["journal_root"] = str(tmp_path / "paper")
    service = MacdRefinedService(config=config)
    service.live.tracking_root = tmp_path / "tracking"
    service.live.tracking_root.mkdir(parents=True, exist_ok=True)
    return service


@pytest.mark.asyncio
async def test_session_data_audit_schedules_once_when_stale(tmp_path, monkeypatch) -> None:
    service = _service_with_tmp_tracking(tmp_path)
    runs = []

    async def fake_audit(**_kw):
        runs.append(1)
        return {"ok": True}

    monkeypatch.setattr(service, "data_audit", fake_audit)
    late = datetime(2026, 7, 17, 15, 0, tzinfo=_IST)
    # Stale report (prior date) → schedule.
    (tmp_path / "data_audit_latest.json").write_text(
        json.dumps({"ran_at": "2026-07-16 09:30:00+00:00"})
    )
    assert service._maybe_schedule_session_data_audit(now_ist=late) is True
    await service._session_audit_task
    assert runs == [1]
    # Same session again → no second run (report now marked... simulate fresh).
    (tmp_path / "data_audit_latest.json").write_text(
        json.dumps({"ran_at": "2026-07-17 09:35:00+00:00"})
    )
    assert service._maybe_schedule_session_data_audit(now_ist=late) is False


@pytest.mark.asyncio
async def test_session_data_audit_gated_before_1445_and_for_us(tmp_path, monkeypatch) -> None:
    service = _service_with_tmp_tracking(tmp_path)
    monkeypatch.setattr(service, "data_audit", lambda **_kw: (_ for _ in ()).throw(AssertionError))
    early = datetime(2026, 7, 17, 10, 0, tzinfo=_IST)
    assert service._maybe_schedule_session_data_audit(now_ist=early) is False

    us_service = _service_with_tmp_tracking(tmp_path, market="us")
    late = datetime(2026, 7, 17, 15, 0, tzinfo=_IST)
    assert us_service._maybe_schedule_session_data_audit(now_ist=late) is False
