from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auction_intelligence.rl.versions import RLPolicyVersionStore


class _FakeSession:
    def __init__(self) -> None:
        self.statement = None
        self.params = None
        self.committed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement, params):
        self.statement = statement
        self.params = params
        return None

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_create_version_serializes_json_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(
        "auction_intelligence.rl.versions.AsyncSessionLocal",
        lambda: fake_session,
    )

    store = RLPolicyVersionStore()
    payload = await store.create_version(
        version_name="rl-20260413T223342-startup_catchup",
        status="candidate",
        source="startup_catchup",
        symbol="NIFTY",
        trained_on=12,
        skipped=1,
        average_reward=0.42,
        metrics={"training": {"trained_on": 12}},
        qtable_snapshot={"q_values": {"state": [0.1, 0.2]}},
        promotion_reason="seed version",
    )

    assert fake_session.committed is True
    assert "CAST(:metrics AS jsonb)" in str(fake_session.statement)
    assert json.loads(fake_session.params["metrics"]) == {"training": {"trained_on": 12}}
    assert json.loads(fake_session.params["qtable_snapshot"]) == {"q_values": {"state": [0.1, 0.2]}}
    assert payload["metrics"] == {"training": {"trained_on": 12}}
