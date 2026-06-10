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


@pytest.mark.asyncio
async def test_desk_snapshot_payload_passes_plain_values_not_query_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the WS wrapper calls the desk route shims directly, so any
    parameter it leaves unfilled keeps its ``fastapi.Query(...)`` SENTINEL as
    the value (FastAPI only resolves defaults through dependency injection).
    ``lookback_sessions`` leaked this way and crashed the directional stream on
    every refresh ("'>' not supported between instances of 'Query' and 'int'").
    Every route parameter must arrive as a plain python value."""
    import fastapi.params

    import api.routers.directional_options as directional_router
    import api.routers.gann_tp_delta as gann_router

    captured: dict[str, dict] = {}

    async def fake_directional(**kwargs):
        captured["directional"] = kwargs
        return {"ok": True}

    async def fake_gann(**kwargs):
        captured["gann"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(directional_router, "live_snapshot", fake_directional)
    monkeypatch.setattr(gann_router, "live_snapshot", fake_gann)

    await ticks._desk_snapshot_payload("directional", "NIFTY", None)
    await ticks._desk_snapshot_payload("gann", "BANKNIFTY", None)

    for desk, kwargs in captured.items():
        for name, value in kwargs.items():
            assert not isinstance(value, fastapi.params.Param), (
                f"{desk}.{name} leaked a FastAPI Query sentinel into the service call"
            )

    assert captured["directional"]["lookback_sessions"] == 16
    assert isinstance(captured["directional"]["lookback_sessions"], int)
    assert captured["directional"]["timeframe"] == "5minute"
    assert captured["gann"]["lookback_sessions"] == 60
    assert captured["gann"]["anchor_mode"] == "auto_pivot"
    assert captured["gann"]["h_mode"] == "median_tpd"
    assert captured["gann"]["manual_h"] is None


@pytest.mark.asyncio
async def test_desk_snapshot_payload_unknown_desk() -> None:
    payload = await ticks._desk_snapshot_payload("nope", "NIFTY", None)
    assert payload == {"error": "unknown desk: nope"}
