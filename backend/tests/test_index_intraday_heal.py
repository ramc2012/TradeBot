from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import market_data.stock_spot_sweeper as sweeper


def _patch(monkeypatch, present: dict[str, int], candles: list[list]) -> dict:
    calls: dict = {"urls": [], "upserts": []}

    async def fake_keys(symbols):
        return [(s, f"NSE_INDEX|{s}") for s in symbols]

    async def fake_present(symbols, start, end):
        calls["window"] = (start, end)
        return present

    async def fake_get(client, url):
        calls["urls"].append(url)
        return candles

    async def fake_upsert(symbol, key, interval, rows, *, source):
        calls["upserts"].append((symbol, interval, len(rows), source))
        return len(rows)

    monkeypatch.setattr(sweeper, "_index_keys", fake_keys)
    monkeypatch.setattr(sweeper, "_session_minutes_present", fake_present)
    monkeypatch.setattr(sweeper, "_get_candles", fake_get)
    monkeypatch.setattr(sweeper, "_upsert", fake_upsert)
    sweeper._INDEX_HEAL_COOLDOWN.clear()
    return calls


# 2026-09-16 12:00 IST (a trading Wednesday)
NOON_IST = datetime(2026, 9, 16, 6, 30, tzinfo=timezone.utc)


def test_heal_fetches_only_the_short_index_and_keeps_session_bars(monkeypatch) -> None:
    expected = 165 - 3  # 09:15 -> 11:57 settled horizon
    calls = _patch(
        monkeypatch,
        present={"NIFTY": expected, "SENSEX": 20},
        candles=[
            ["2026-09-16T09:15:00+05:30", 1, 2, 0.5, 1.5, 0, 0],
            ["2026-09-16T09:16:00+05:30", 1, 2, 0.5, 1.5, 0, 0],
            ["2026-09-16T09:14:00+05:30", 1, 2, 0.5, 1.5, 0, 0],  # pre-open: dropped
        ],
    )

    result = asyncio.run(
        sweeper.heal_index_intraday_gaps(("NIFTY", "SENSEX"), now=NOON_IST)
    )

    assert result["expected"] == expected
    assert len(calls["urls"]) == 1 and "SENSEX" in calls["urls"][0]
    assert "/historical-candle/intraday/" in calls["urls"][0]
    assert calls["upserts"] == [("SENSEX", "1minute", 2, sweeper.INDEX_HEAL_SOURCE)]


def test_heal_is_cooldown_gated_and_skips_closed_days(monkeypatch) -> None:
    calls = _patch(monkeypatch, present={}, candles=[])
    asyncio.run(sweeper.heal_index_intraday_gaps(("NIFTY",), now=NOON_IST))
    asyncio.run(sweeper.heal_index_intraday_gaps(("NIFTY",), now=NOON_IST))
    assert len(calls["urls"]) == 1

    holiday = datetime(2026, 9, 14, 6, 30, tzinfo=timezone.utc)  # Ganesh Chaturthi
    assert asyncio.run(sweeper.heal_index_intraday_gaps(("NIFTY",), now=holiday))["status"] == "skipped_no_session"


def test_heal_never_raises(monkeypatch) -> None:
    _patch(monkeypatch, present={}, candles=[])

    async def boom(symbols):
        raise RuntimeError("db down")

    monkeypatch.setattr(sweeper, "_index_keys", boom)
    assert asyncio.run(sweeper.heal_index_intraday_gaps(now=NOON_IST))["status"] == "error"
