"""Commodity hot-payload de-dup + signal_audit cap (audit 2026-07-18).

/overview and /strategy-agent/status weighed ~846KB/825KB: signal_audit was
~455KB (600 records) and two key pairs were byte-identical duplicates
(trade_history vs historical_trades, watchlist vs futures_watchlist). The UI
(frontend-v2 only; legacy /frontend retired 2026-06-07) reads futures_watchlist
and trade_history/today_trades — the dropped keys were unread duplicates, and
signal_audit is never read from the hot payload. Measured on the live 07-18
payloads the slimming shrinks status 841,979 → 282,979 bytes (-66.4%) and
overview 862,994 → 303,994 bytes (-64.8%).
"""
from __future__ import annotations

import asyncio
import json

import api.routers.commodity as commodity_router
from api.routers.commodity import _HOT_SIGNAL_AUDIT_CAP, _slim_agent_status
from paper_engine.commodity_strategy_agent import CommodityStrategyAgent


def _fat_status(audit_count: int = 600) -> dict:
    watch_rows = [{"symbol": f"SYM{i}", "ltp": 100.0 + i, "blob": "x" * 200} for i in range(8)]
    trades = [{"trade_id": i, "symbol": "GOLDM", "pnl": i * 1.5} for i in range(256)]
    return {
        "running": True,
        "summary": {"total_equity": 100000.0},
        "watchlist": watch_rows,
        "futures_watchlist": watch_rows,
        "trade_history": trades,
        "today_trades": trades[:4],
        "historical_trades": trades[4:],
        "signal_audit": [
            {"audit_ts": f"2026-07-18T{i % 24:02d}:00:00", "detail": "y" * 700}
            for i in range(audit_count)
        ],
    }


def test_slim_drops_duplicate_keys_and_caps_signal_audit() -> None:
    fat = _fat_status()
    slim = _slim_agent_status(fat)

    # Pure duplicates removed; the UI-read keys survive.
    assert "watchlist" not in slim
    assert "historical_trades" not in slim
    assert slim["futures_watchlist"] == fat["futures_watchlist"]
    assert slim["trade_history"] == fat["trade_history"]
    assert slim["today_trades"] == fat["today_trades"]

    # signal_audit capped to the most recent N with additive metadata.
    assert len(slim["signal_audit"]) == _HOT_SIGNAL_AUDIT_CAP
    assert slim["signal_audit"] == fat["signal_audit"][:_HOT_SIGNAL_AUDIT_CAP]
    assert slim["signal_audit_total"] == 600
    assert slim["signal_audit_capped"] is True

    # Measured serialized reduction (600 audit records + dup keys dominate).
    before = len(json.dumps(fat, separators=(",", ":")))
    after = len(json.dumps(slim, separators=(",", ":")))
    assert after < before * 0.5


def test_slim_is_noop_below_cap() -> None:
    fat = _fat_status(audit_count=10)
    slim = _slim_agent_status(fat)
    assert slim["signal_audit"] == fat["signal_audit"]
    assert slim["signal_audit_total"] == 10
    assert slim["signal_audit_capped"] is False


def test_status_and_overview_endpoints_apply_slimming(monkeypatch) -> None:
    fat = _fat_status()
    agent = commodity_router.commodity_strategy_agent
    monkeypatch.setattr(agent, "get_status", lambda **_kw: dict(fat))
    monkeypatch.setattr(agent, "get_control_state", lambda: {"active": False})
    monkeypatch.setattr(agent, "get_orders", lambda: [])
    monkeypatch.setattr(agent, "get_positions", lambda: [])
    monkeypatch.setattr(agent, "get_reports", lambda: [])

    status_payload = asyncio.run(commodity_router.commodity_strategy_status())
    assert "watchlist" not in status_payload
    assert "historical_trades" not in status_payload
    assert len(status_payload["signal_audit"]) == _HOT_SIGNAL_AUDIT_CAP

    overview = asyncio.run(commodity_router.commodity_overview())
    assert "watchlist" not in overview["status"]
    assert "historical_trades" not in overview["status"]
    assert len(overview["status"]["signal_audit"]) == _HOT_SIGNAL_AUDIT_CAP


def test_signal_audit_paginated_accessor_serves_full_set() -> None:
    class _Runtime:
        signal_audit = [
            {"audit_key": f"k{i}", "audit_ts": f"t{i}", "detail": f"d{i}"} for i in range(120)
        ]

    class _Stub:
        _runtime = _Runtime()

        def _refresh_state_from_store(self, **_kw):
            pass

    stub = _Stub()
    page1 = CommodityStrategyAgent.get_signal_audit(stub, offset=0, limit=50)
    page2 = CommodityStrategyAgent.get_signal_audit(stub, offset=50, limit=50)
    page3 = CommodityStrategyAgent.get_signal_audit(stub, offset=100, limit=50)

    assert page1["total"] == page2["total"] == page3["total"] == 120
    assert len(page1["records"]) == 50
    assert len(page2["records"]) == 50
    assert len(page3["records"]) == 20
    combined = page1["records"] + page2["records"] + page3["records"]
    assert [r["audit_ts"] for r in combined] == [f"t{i}" for i in range(120)]
    # audit_key is internal de-dup state — never serialized.
    assert all("audit_key" not in r for r in combined)


def test_signal_audit_endpoint_paginates(monkeypatch) -> None:
    agent = commodity_router.commodity_strategy_agent

    def fake_get_signal_audit(*, offset: int = 0, limit=None):
        records = [{"audit_ts": f"t{i}"} for i in range(120)]
        page = records[offset : offset + (limit or len(records))]
        return {"total": 120, "offset": offset, "limit": limit, "records": page}

    monkeypatch.setattr(agent, "get_signal_audit", fake_get_signal_audit)
    payload = asyncio.run(commodity_router.commodity_signal_audit(offset=100, limit=50))
    assert payload["total"] == 120
    assert len(payload["records"]) == 20
