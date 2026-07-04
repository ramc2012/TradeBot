from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from db import database
from paper_engine.commodity_mp_history import _load_session_bars


@pytest.mark.asyncio
async def test_history_loader_selects_one_ranked_source_per_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Mappings:
        @staticmethod
        def all():
            return [
                {
                    "session_date": date(2026, 7, 3),
                    "time": datetime(2026, 7, 3, 4, 0, tzinfo=timezone.utc),
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 10,
                }
            ]

    class _Result:
        @staticmethod
        def mappings():
            return _Mappings()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return _Result()

    monkeypatch.setattr(database, "AsyncSessionLocal", _Session)
    sessions = await _load_session_bars("GOLD", limit=7)

    sql = str(captured["sql"])
    assert "COUNT(DISTINCT time)" in sql
    assert "source = 'fyers_mcx_cont'" in sql
    assert "source_rank = 1" in sql
    assert captured["params"] == {"root": "GOLD", "limit": 7, "min_bars": 20}
    assert len(sessions[date(2026, 7, 3)]) == 1
