"""Phase-2 LANESET split hardening (2026-07-18).

Four items, each with its no-op proof (LANESET=all stays equivalent to today),
the failure-mode fallback, and the correctness fix:

  1. Shared Redis REST token-bucket — consulted only in a split boot; Redis-down
     fails OPEN to the in-process limiter.
  2. run-once / close proxy (core plane -> strategy plane over Redis) — full
     request/ack roundtrip + bounded timeout + honest error; never taken single
     process.
  3. Control-state last-writer-wins race — CAS + field-ownership merge so a
     control toggle survives a concurrent scan persist (and the heartbeat
     survives a concurrent control write).
  4. WS commodity slim — the WS reuses the shared _slim_agent_status helper; the
     slim is idempotent and the frontend-read keys survive.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

import brokers.rate_limiter as rl
from brokers.rate_limiter import AsyncRateLimiter, CLASS_STANDARD
from core import laneset
from core.config import settings


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 1 — Shared REST token-bucket
# ══════════════════════════════════════════════════════════════════════════════


class _RecordingBudget:
    def __init__(self) -> None:
        self.calls = 0

    async def reserve(self, request_class=None) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_shared_budget_noop_when_laneset_all(monkeypatch) -> None:
    # NO-OP PROOF: LANESET=all -> is_split() False -> the shared branch in
    # acquire() is never entered, so a limiter carrying a shared budget behaves
    # byte-identically to one without it.
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    budget = _RecordingBudget()
    lim = AsyncRateLimiter(windows=[(5, 1.0)], name="t-noop", shared_budget=budget)
    await lim.acquire()
    assert budget.calls == 0
    assert rl._shared_budget_active() is False


@pytest.mark.asyncio
async def test_shared_budget_consulted_when_split(monkeypatch) -> None:
    # In a split boot with the flag on, every admitted acquire draws one shared
    # token.
    monkeypatch.setattr(settings, "LANESET", "strategies", raising=False)
    monkeypatch.setattr(settings, "SHARED_REST_BUDGET_ENABLED", True, raising=False)
    assert rl._shared_budget_active() is True
    budget = _RecordingBudget()
    lim = AsyncRateLimiter(windows=[(5, 1.0)], name="t-split", shared_budget=budget)
    await lim.acquire()
    await lim.acquire()
    assert budget.calls == 2


@pytest.mark.asyncio
async def test_shared_budget_flag_off_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LANESET", "strategies", raising=False)
    monkeypatch.setattr(settings, "SHARED_REST_BUDGET_ENABLED", False, raising=False)
    assert rl._shared_budget_active() is False
    budget = _RecordingBudget()
    lim = AsyncRateLimiter(windows=[(5, 1.0)], name="t-off", shared_budget=budget)
    await lim.acquire()
    assert budget.calls == 0


@pytest.mark.asyncio
async def test_shared_budget_fails_open_when_redis_down(monkeypatch) -> None:
    # FAIL OPEN: a Redis error inside reserve() must grant immediately (never
    # block a trade) and flip the observable redis_ok flag.
    budget = rl.SharedRestBudget(broker="fyers", windows=[(9, 1.0)])

    async def _boom():
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr("db.redis_client.get_redis", _boom)
    # Should return promptly without raising.
    await asyncio.wait_for(budget.reserve(CLASS_STANDARD), timeout=1.0)
    assert budget._redis_ok is False


@pytest.mark.asyncio
async def test_shared_budget_grants_on_zero_wait(monkeypatch) -> None:
    # eval() -> 0 means "admitted into every window": reserve returns at once.
    class _FakeRedis:
        def __init__(self) -> None:
            self.evals = 0

        async def eval(self, *_a):
            self.evals += 1
            return 0

    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr("db.redis_client.get_redis", _get)
    budget = rl.SharedRestBudget(broker="upstox", windows=[(8, 1.0)])
    await asyncio.wait_for(budget.reserve(), timeout=1.0)
    assert fake.evals == 1
    assert budget._redis_ok is True


@pytest.mark.asyncio
async def test_shared_budget_waits_then_grants(monkeypatch) -> None:
    # eval() returns a positive wait once, then 0: reserve sleeps (patched) and
    # retries until granted. Proves the bounded retry loop.
    seq = iter([250, 0])  # ms wait, then grant

    class _FakeRedis:
        async def eval(self, *_a):
            return next(seq)

    async def _get():
        return _FakeRedis()

    slept: list = []

    async def _fast_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("db.redis_client.get_redis", _get)
    monkeypatch.setattr(rl.asyncio, "sleep", _fast_sleep)
    budget = rl.SharedRestBudget(broker="fyers", windows=[(9, 1.0)])
    await asyncio.wait_for(budget.reserve(), timeout=1.0)
    assert slept and slept[0] <= 0.25


@pytest.mark.asyncio
async def test_shared_budget_status_local_when_all(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LANESET", "all", raising=False)
    status = await rl.shared_budget_status()
    assert status["mode"] == "local"
    assert status["active"] is False
    assert set(status["brokers"]) == {"fyers", "upstox"}


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 2 — run-once / close proxy (Redis request/ack)
# ══════════════════════════════════════════════════════════════════════════════


class _FakeRedisRPC:
    """Minimal list/kv Redis for the proxy roundtrip."""

    def __init__(self, *, instant_timeout: bool = False) -> None:
        self.lists: dict[str, list] = defaultdict(list)
        self.kv: dict[str, str] = {}
        self.instant_timeout = instant_timeout

    async def rpush(self, key, value):
        self.lists[key].append(value)

    async def blpop(self, key, timeout=0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + float(timeout or 0)
        while True:
            if self.lists.get(key):
                return (key, self.lists[key].pop(0))
            if self.instant_timeout or loop.time() >= deadline:
                return None
            await asyncio.sleep(0.005)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def expire(self, key, ttl):
        return True


@pytest.mark.asyncio
async def test_proxy_roundtrip_delivers_result(monkeypatch) -> None:
    import db.redis_client as rc

    fake = _FakeRedisRPC()

    async def _get():
        return fake

    monkeypatch.setattr(rc, "get_redis", _get)
    monkeypatch.setattr(settings, "STRATEGY_PROXY_REQUEST_KEY", "strat:cmd:req", raising=False)

    seen: list = []

    async def _dispatch(action, args):
        seen.append((action, args))
        return {"ok": True, "action": action, "force": args.get("force")}

    stop = asyncio.Event()
    consumer = asyncio.create_task(rc.consume_strategy_commands(_dispatch, stop_event=stop))
    try:
        ack = await asyncio.wait_for(
            rc.proxy_strategy_command("nse_run_once", {"force": True}, timeout=5),
            timeout=3.0,
        )
    finally:
        stop.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    # proxy_strategy_command returns the full ack; the router helper unwraps
    # ack["result"] (and maps error/timeout sentinels to HTTP codes).
    assert ack["result"] == {"ok": True, "action": "nse_run_once", "force": True}
    assert "error" not in ack
    assert seen == [("nse_run_once", {"force": True})]


@pytest.mark.asyncio
async def test_proxy_surfaces_dispatch_error(monkeypatch) -> None:
    import db.redis_client as rc

    fake = _FakeRedisRPC()

    async def _get():
        return fake

    monkeypatch.setattr(rc, "get_redis", _get)

    async def _dispatch(action, args):
        raise ValueError("boom in strategy plane")

    stop = asyncio.Event()
    consumer = asyncio.create_task(rc.consume_strategy_commands(_dispatch, stop_event=stop))
    try:
        ack = await asyncio.wait_for(
            rc.proxy_strategy_command("commodity_run_once", {}, timeout=5), timeout=3.0
        )
    finally:
        stop.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    assert "boom in strategy plane" in ack.get("error", "")


@pytest.mark.asyncio
async def test_proxy_timeout_returns_sentinel(monkeypatch) -> None:
    # BOUNDED TIMEOUT: no consumer answers -> the caller gets the timeout
    # sentinel (never hangs).
    import db.redis_client as rc

    fake = _FakeRedisRPC(instant_timeout=True)

    async def _get():
        return fake

    monkeypatch.setattr(rc, "get_redis", _get)
    ack = await asyncio.wait_for(
        rc.proxy_strategy_command("nse_run_once", {"force": True}, timeout=1), timeout=3.0
    )
    assert ack.get(rc.PROXY_TIMEOUT) is True


@pytest.mark.asyncio
async def test_router_runs_in_process_when_not_core_only(monkeypatch) -> None:
    # NO-OP PROOF: LANESET=all -> is_core_only() False -> the router awaits the
    # in-process agent exactly as before (no proxy path).
    import api.routers.trading as trading

    monkeypatch.setattr(settings, "LANESET", "all", raising=False)

    called = {}

    async def _run_once(*, force):
        called["force"] = force
        return {"ran": "in_process"}

    monkeypatch.setattr(trading.paper_strategy_agent, "run_once", _run_once)

    def _explode(*_a, **_k):
        raise AssertionError("proxy must not be used single-process")

    monkeypatch.setattr(trading, "_proxy_to_strategy_plane", _explode)
    result = await trading.run_strategy_agent_once(force=True)
    assert result == {"ran": "in_process"}
    assert called == {"force": True}


@pytest.mark.asyncio
async def test_router_proxies_when_core_only(monkeypatch) -> None:
    import api.routers.trading as trading

    monkeypatch.setattr(settings, "LANESET", "core", raising=False)
    monkeypatch.setattr(settings, "STRATEGY_PROXY_ENABLED", True, raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("must NOT call the in-process agent on the core plane")

    monkeypatch.setattr(trading.paper_strategy_agent, "run_once", _boom)

    captured = {}

    async def _proxy(action, args):
        captured["action"] = action
        captured["args"] = args
        return {"ran": "proxied"}

    monkeypatch.setattr(trading, "_proxy_to_strategy_plane", _proxy)
    result = await trading.run_strategy_agent_once(force=False)
    assert result == {"ran": "proxied"}
    assert captured == {"action": "nse_run_once", "args": {"force": False}}


def test_dispatch_unknown_action_raises() -> None:
    from core.market_hours_paper_supervisor import _dispatch_strategy_command

    with pytest.raises(ValueError):
        asyncio.run(_dispatch_strategy_command("does_not_exist", {}))


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 3 — control-state last-writer-wins race
# ══════════════════════════════════════════════════════════════════════════════


def test_scan_persist_preserves_control_flags() -> None:
    # A scan persist (owns_control_flags=False) keeps the stored operator flags
    # and overwrites only its own heartbeat.
    from core.runtime_state import _merge_control_state

    stored = {"control": {"kill_switch_active": True, "auto_run_enabled": False,
                          "loop_heartbeat_at": "t0"}, "data": 1}
    scan_payload = {"control": {"kill_switch_active": False, "auto_run_enabled": True,
                               "loop_heartbeat_at": "t2"}, "data": 2}
    merged = _merge_control_state(
        stored, scan_payload,
        owns_control_flags=False,
        flag_keys=("kill_switch_active", "auto_run_enabled", "manual_restart_required"),
        heartbeat_key="loop_heartbeat_at",
    )
    assert merged["control"]["kill_switch_active"] is True     # stored preserved
    assert merged["control"]["auto_run_enabled"] is False      # stored preserved
    assert merged["control"]["loop_heartbeat_at"] == "t2"      # scan owns heartbeat
    assert merged["data"] == 2                                 # scan owns runtime


def test_control_write_preserves_heartbeat() -> None:
    from core.runtime_state import _merge_control_state

    stored = {"control": {"kill_switch_active": False, "loop_heartbeat_at": "t9"}}
    control_payload = {"control": {"kill_switch_active": True, "loop_heartbeat_at": None}}
    merged = _merge_control_state(
        stored, control_payload,
        owns_control_flags=True,
        flag_keys=("kill_switch_active",),
        heartbeat_key="loop_heartbeat_at",
    )
    assert merged["control"]["kill_switch_active"] is True     # control owns flag
    assert merged["control"]["loop_heartbeat_at"] == "t9"      # scan's heartbeat preserved


def test_control_toggle_survives_concurrent_scan_persist(monkeypatch) -> None:
    # THE RACE: the scan writer loads version v, a control toggle commits v+1
    # BEFORE the scan's CAS, so the scan's CAS(expected=v) conflicts, reloads,
    # and re-merges — the kill toggle survives AND the scan's fresh heartbeat
    # survives.
    import core.runtime_state as rs

    db = {
        "payload": {"control": {"kill_switch_active": False,
                                "auto_run_enabled": True,
                                "loop_heartbeat_at": "t0"}},
        "version": 5,
    }
    injected = {"done": False}

    def fake_load_versioned(_key):
        import copy
        return copy.deepcopy(db["payload"]), None, db["version"]

    def fake_cas(_key, payload, expected):
        # Simulate the concurrent operator kill landing between the scan's load
        # and its CAS, exactly once.
        if not injected["done"]:
            injected["done"] = True
            db["version"] += 1
            db["payload"] = {"control": {"kill_switch_active": True,
                                         "auto_run_enabled": True,
                                         "loop_heartbeat_at": "t0"}}
        if expected != db["version"]:
            return (False, None, None)
        db["version"] += 1
        db["payload"] = payload
        return (True, None, db["version"])

    def fake_save(_key, payload):
        db["version"] += 1
        db["payload"] = payload
        return None

    monkeypatch.setattr(rs, "load_runtime_state_versioned", fake_load_versioned)
    monkeypatch.setattr(rs, "save_runtime_state_cas", fake_cas)
    monkeypatch.setattr(rs, "save_runtime_state", fake_save)

    scan_payload = {"control": {"kill_switch_active": False,   # stale snapshot
                                "auto_run_enabled": True,
                                "loop_heartbeat_at": "t2"}}    # fresh heartbeat
    rs.save_runtime_state_control_merged(
        "nse_strategy_state", scan_payload,
        owns_control_flags=False,
        flag_keys=("kill_switch_active", "auto_run_enabled", "manual_restart_required"),
    )

    assert db["payload"]["control"]["kill_switch_active"] is True   # toggle survived
    assert db["payload"]["control"]["loop_heartbeat_at"] == "t2"    # heartbeat survived


# ══════════════════════════════════════════════════════════════════════════════
# ITEM 4 — WS commodity slim reuses the shared helper
# ══════════════════════════════════════════════════════════════════════════════


def _fat_status(audit_count: int = 120) -> dict:
    rows = [{"symbol": f"S{i}", "ltp": 1.0 + i, "mp_tpo_letters": "x" * 50} for i in range(6)]
    trades = [{"trade_id": i} for i in range(30)]
    return {
        "running": True,
        "watchlist": rows,
        "futures_watchlist": rows,
        "trade_history": trades,
        "today_trades": trades[:3],
        "historical_trades": trades[3:],
        "signal_audit": [{"audit_ts": f"t{i}", "detail": "y" * 100} for i in range(audit_count)],
    }


def test_ws_reuses_slim_helper_and_is_idempotent() -> None:
    # The WS module imports the shared helper (no drift from the REST slim).
    from api.routers.commodity import _HOT_SIGNAL_AUDIT_CAP, _slim_agent_status

    fat = _fat_status()
    once = _slim_agent_status(fat)
    twice = _slim_agent_status(once)  # WS applies it on already-slimmed input
    assert twice == once  # idempotent
    assert "watchlist" not in twice
    assert "historical_trades" not in twice
    assert len(twice["signal_audit"]) == _HOT_SIGNAL_AUDIT_CAP
    assert twice["signal_audit_total"] == 120
    assert twice["signal_audit_capped"] is True


def test_ws_overview_payload_shape() -> None:
    # Replicate the WS overview transform: slim, keep one watchlist alias, drop
    # signal_audit — the counters and frontend-read keys survive.
    from api.routers.commodity import _slim_agent_status
    from api.websockets.ticks import _slim_watchlist_rows

    status = _slim_agent_status(_fat_status())
    status["futures_watchlist"] = _slim_watchlist_rows(
        status.get("futures_watchlist") or status.get("watchlist") or []
    )
    status.pop("watchlist", None)
    status.pop("signal_audit", None)

    assert "watchlist" not in status
    assert "historical_trades" not in status
    assert "signal_audit" not in status
    # Additive cap counters survive the pops (consumer can still detect the cap).
    assert status["signal_audit_total"] == 120
    assert status["signal_audit_capped"] is True
    # Frontend-read keys survive; heavy per-row fields are stripped.
    assert "futures_watchlist" in status
    assert "trade_history" in status
    assert all("mp_tpo_letters" not in row for row in status["futures_watchlist"])
