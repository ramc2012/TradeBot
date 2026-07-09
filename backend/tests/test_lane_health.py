from __future__ import annotations

from datetime import date

import pytest

from api.routers import lane_health


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        return self

    def all(self) -> list[dict]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.query_text: str | None = None
        self.params: dict | None = None

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, query, params):
        self.query_text = str(query)
        self.params = params
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_lane_history_uses_integer_date_subtraction(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession(
        [
            {
                "audit_date": date(2026, 7, 9),
                "overall_status": "red",
                "data_integrity_pass": True,
                "replay_parity_pass": False,
                "gate_attribution_pass": True,
                "backtest_parity_pass": False,
                "trade_recon_pass": True,
                "edge_persistence_pass": False,
                "signals_emitted": 4741,
                "expectancy_60d": None,
                "drift_pct": None,
            }
        ]
    )

    monkeypatch.setattr(lane_health, "AsyncSessionLocal", lambda: fake_session)

    payload = await lane_health.history("s1", days=7)

    assert payload["lane"] == "s1"
    assert payload["rows"][0]["overall_status"] == "red"
    assert fake_session.params == {"lane": "s1", "days": 7}
    assert "CURRENT_DATE - CAST(:days AS INTEGER)" in (fake_session.query_text or "")
