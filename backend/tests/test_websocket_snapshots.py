from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketState

from api.websockets import ticks


@pytest.fixture(autouse=True)
def clear_snapshot_cache() -> None:
    ticks._snapshot_cache.clear()


@pytest.mark.asyncio
async def test_snapshot_payload_deduplicates_concurrent_refreshes() -> None:
    calls = 0

    async def payload_factory() -> dict[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"value": calls}

    results = await asyncio.gather(
        *[
            ticks._get_snapshot_payload(
                channel="market_watchlist:test",
                payload_factory=payload_factory,
                cache_ttl_seconds=1.0,
            )
            for _ in range(3)
        ]
    )

    assert calls == 1
    assert {encoded for _, encoded in results} == {'{"value":1}'}


@pytest.mark.asyncio
async def test_snapshot_payload_reuses_last_good_value_after_refresh_failure() -> None:
    async def ok_factory() -> dict[str, str]:
        return {"status": "ok"}

    payload, encoded = await ticks._get_snapshot_payload(
        channel="commodity_watchlist",
        payload_factory=ok_factory,
        cache_ttl_seconds=1.0,
    )

    assert payload == {"status": "ok"}
    assert encoded == '{"status":"ok"}'

    ticks._snapshot_cache["commodity_watchlist"].expires_at = 0.0

    async def failing_factory() -> dict[str, str]:
        raise RuntimeError("temporary failure")

    recovered_payload, recovered_encoded = await ticks._get_snapshot_payload(
        channel="commodity_watchlist",
        payload_factory=failing_factory,
        cache_ttl_seconds=1.0,
    )

    assert recovered_payload == payload
    assert recovered_encoded == encoded


@pytest.mark.asyncio
async def test_stream_snapshot_exits_cleanly_when_socket_is_already_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        async def accept(self) -> None:
            return None

        async def send_text(self, _payload: str) -> None:
            raise RuntimeError('Cannot call "send" once a close message has been sent')

    async def payload_factory() -> dict[str, str]:
        return {"status": "ok"}

    monkeypatch.setattr(ticks, "authenticate_websocket_client", lambda _ws: {"scope": "websocket"})

    await ticks._stream_snapshot(
        _FakeSocket(),
        channel="commodity_overview",
        interval_seconds=0.01,
        payload_factory=payload_factory,
    )
