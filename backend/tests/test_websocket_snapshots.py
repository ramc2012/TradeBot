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
async def test_strategy_snapshot_directional_uses_concrete_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from directional_options.service import directional_options_service

    captured: dict[str, object] = {}

    class _FakeSocket:
        query_params = {"desk": "directional", "symbol": "NIFTY", "timeframe": "5minute"}

    async def fake_live_snapshot(underlying: str, timeframe: str, lookback_sessions: int) -> dict[str, object]:
        captured.update(
            underlying=underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
        )
        return {"ok": True}

    async def fake_stream_snapshot(_websocket, *, channel, interval_seconds, payload_factory, **_kwargs) -> None:
        captured["channel"] = channel
        captured["interval_seconds"] = interval_seconds
        captured["payload"] = await payload_factory()

    monkeypatch.setattr(directional_options_service, "live_snapshot", fake_live_snapshot)
    monkeypatch.setattr(ticks, "_stream_snapshot", fake_stream_snapshot)

    await ticks.ws_strategy_snapshot(_FakeSocket())

    assert captured["underlying"] == "NIFTY"
    assert captured["timeframe"] == "5minute"
    assert captured["lookback_sessions"] == 16
    assert isinstance(captured["lookback_sessions"], int)
    assert captured["payload"] == {"ok": True}


@pytest.mark.asyncio
async def test_strategy_snapshot_gann_uses_concrete_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from gann_tp_delta.service import gann_tp_delta_service

    captured: dict[str, object] = {}

    class _FakeSocket:
        query_params = {"desk": "gann", "symbol": "BANKNIFTY"}

    async def fake_live_snapshot(
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str = "auto_pivot",
        h_mode: str = "median_tpd",
        manual_h: float | None = None,
    ) -> dict[str, object]:
        captured.update(
            underlying=underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            anchor_mode=anchor_mode,
            h_mode=h_mode,
            manual_h=manual_h,
        )
        return {"ok": True}

    async def fake_stream_snapshot(_websocket, *, channel, interval_seconds, payload_factory, **_kwargs) -> None:
        captured["channel"] = channel
        captured["interval_seconds"] = interval_seconds
        captured["payload"] = await payload_factory()

    monkeypatch.setattr(gann_tp_delta_service, "live_snapshot", fake_live_snapshot)
    monkeypatch.setattr(ticks, "_stream_snapshot", fake_stream_snapshot)

    await ticks.ws_strategy_snapshot(_FakeSocket())

    assert captured["underlying"] == "BANKNIFTY"
    assert captured["timeframe"] == "15minute"
    assert captured["lookback_sessions"] == 60
    assert captured["anchor_mode"] == "auto_pivot"
    assert captured["h_mode"] == "median_tpd"
    assert captured["manual_h"] is None
    assert isinstance(captured["lookback_sessions"], int)
    assert captured["payload"] == {"ok": True}
