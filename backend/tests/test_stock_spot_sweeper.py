"""Guards on the F&O stock spot sweeper's endpoint choice and payload parsing.

Two defects lived here, and both were invisible: the sweep targeted a broker
this deployment does not use and returned `skipped_no_broker` on every
scheduled pass while the supervisor logged "completed"; and the only writer
left standing called an endpoint that never returns the current session, so
"today" did not exist until it became "yesterday".
"""
from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path

import pytest

from market_data import stock_spot_sweeper as sweeper


# ── payload parsing ────────────────────────────────────────────────────────

def test_upstox_positional_candles_are_parsed():
    """Upstox returns [ts, o, h, l, c, volume, oi] arrays, not dicts. Parsing
    them as dicts drops every bar silently — the sweep reports ok and stores
    nothing."""
    raw = [
        ["2026-08-27T09:15:00+05:30", 100.0, 102.0, 99.5, 101.0, 12345, 0],
        ["2026-08-27T09:45:00+05:30", 101.0, 103.0, 100.5, 102.5, 9876, 0],
    ]
    rows = sweeper._normalize(raw)
    assert len(rows) == 2
    assert rows[0]["open"] == 100.0 and rows[0]["volume"] == 12345
    assert rows[0]["time"] < rows[1]["time"]


def test_dict_shaped_candles_are_still_accepted():
    rows = sweeper._normalize([
        {"time": "2026-08-27T09:15:00+05:30", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 10},
    ])
    assert len(rows) == 1 and rows[0]["close"] == 1.5


def test_overlapping_bars_from_the_two_endpoints_are_deduplicated():
    """A window spanning today calls BOTH endpoints, and they can overlap at the
    session boundary. Without dedup the same bar is written twice."""
    stamp = "2026-08-27T09:15:00+05:30"
    rows = sweeper._normalize([
        [stamp, 100.0, 102.0, 99.5, 101.0, 100, 0],
        [stamp, 100.0, 102.0, 99.5, 101.0, 100, 0],
    ])
    assert len(rows) == 1


def test_a_malformed_row_is_skipped_without_losing_the_rest():
    rows = sweeper._normalize([
        ["not-a-timestamp", 1, 2, 3, 4, 5],
        ["2026-08-27T09:15:00+05:30", 1, 2, 0.5, 1.5, 10],
    ])
    assert len(rows) == 1


def test_candles_are_returned_in_chronological_order():
    rows = sweeper._normalize([
        ["2026-08-27T10:15:00+05:30", 1, 2, 0.5, 1.5, 10],
        ["2026-08-27T09:15:00+05:30", 1, 2, 0.5, 1.5, 10],
    ])
    assert rows[0]["time"] < rows[1]["time"]


# ── endpoint selection: the defect that hid "today" ────────────────────────

class _RecordingClient:
    def __init__(self, fail: str | None = None):
        self.urls = []
        self.fail = fail

    async def get(self, url, headers=None):  # noqa: ARG002
        self.urls.append(url)
        if self.fail and self.fail in url:
            raise RuntimeError("simulated endpoint failure")
        return _EmptyResponse()


class _EmptyResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"data": {"candles": []}}


async def _routed_urls(from_date, to_date, today, fail=None):
    client = _RecordingClient(fail=fail)
    try:
        await sweeper._fetch_window(client, "NSE_EQ|INE002A01018", "30minute",
                                    from_date, to_date, today)
    except RuntimeError:
        pass
    return client.urls


@pytest.mark.asyncio
async def test_a_window_containing_today_calls_the_intraday_endpoint():
    """THE DEFECT. /historical-candle NEVER returns the current session — asking
    it for 25-Aug..27-Aug on 27-Aug returned 26 candles covering only the 25th
    and 26th. A window that includes today MUST also call the intraday path."""
    urls = await _routed_urls(date(2026, 8, 27), date(2026, 8, 27), date(2026, 8, 27))
    assert any("/historical-candle/intraday/" in u for u in urls)


@pytest.mark.asyncio
async def test_a_history_only_window_does_not_call_the_intraday_endpoint():
    urls = await _routed_urls(date(2026, 8, 20), date(2026, 8, 26), date(2026, 8, 27))
    assert urls and all("/intraday/" not in u for u in urls)


@pytest.mark.asyncio
async def test_a_window_spanning_both_calls_both_and_never_asks_history_for_today():
    """History is asked only up to yesterday. Asking it for today is not merely
    useless — it is the call whose empty answer was mistaken for 'no data'."""
    urls = await _routed_urls(date(2026, 8, 25), date(2026, 8, 27), date(2026, 8, 27))
    history = [u for u in urls if "/intraday/" not in u]
    assert len(history) == 1
    assert history[0].endswith("/2026-08-26/2026-08-25")
    assert any("/intraday/" in u for u in urls)


# ── the broker-dependency regression ───────────────────────────────────────

_SOURCE = Path(sweeper.__file__).read_text()


def test_the_sweeper_no_longer_depends_on_a_fyers_session():
    """It skipped EVERY scheduled pass with 'no Fyers session' while the
    supervisor logged 'completed'. This deployment has been Upstox-only since a
    deliberate 12-Aug-2026 decision, so a Fyers dependency here is a permanent
    no-op, not a degraded mode."""
    assert "ensure_fyers_session" not in _SOURCE
    assert "get_active_adapter" not in _SOURCE


def test_the_public_endpoints_are_reached_without_requiring_a_token():
    """Both candle endpoints answer with no Authorization header. A token is
    sent when configured, but its absence must never gate the pass."""
    headers = sweeper._headers()
    assert headers.get("Accept") == "application/json"


def test_the_universe_skips_rows_that_are_not_upstox_instrument_keys():
    """A Fyers-style 'NSE:RELIANCE-EQ' 404s against these endpoints. Skipping is
    correct; guessing a key is how a silent 100% failure rate starts."""
    assert '"|" not in key' in _SOURCE


def test_the_sweep_still_runs_under_the_bulk_broker_class():
    """These calls spend no authenticated quota, but CLASS_BULK also carries the
    admission rule that keeps a 211-symbol sweep behind any queued CRITICAL
    waiter, and that protection is still wanted."""
    assert "broker_class(CLASS_BULK)" in _SOURCE


# ── the supervisor wiring ──────────────────────────────────────────────────

_SUPERVISOR = Path(sweeper.__file__).parents[1] / "core" / "market_hours_paper_supervisor.py"


def _runner_configs() -> dict[str, dict]:
    """key -> {kwarg: literal} for every RunnerConfig in the supervisor."""
    tree = ast.parse(_SUPERVISOR.read_text())
    out: dict[str, dict] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "RunnerConfig":
            kwargs = {}
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant):
                    kwargs[kw.arg] = kw.value.value
                elif isinstance(kw.value, ast.Name):
                    kwargs[kw.arg] = kw.value.id
                elif isinstance(kw.value, ast.Attribute):
                    kwargs[kw.arg] = kw.value.attr
            if "key" in kwargs:
                out[kwargs["key"]] = kwargs
    return out


def _runner_keys() -> set[str]:
    return set(_runner_configs())


def test_both_an_intraday_and_a_post_close_sweep_are_registered():
    """The post-close pass is the once-a-day backstop; the intraday pass is what
    makes in-session lanes read the CURRENT session instead of the previous one.
    Neither replaces the other."""
    keys = _runner_keys()
    assert "stock_spot_sweep" in keys
    assert "stock_spot_intraday" in keys


@pytest.mark.asyncio
async def test_a_failing_history_leg_does_not_cost_this_symbol_todays_bars():
    """Today is the scarce data: history can be re-fetched on any later pass,
    the current session cannot once it has gone stale. So the legs fail
    independently and only a total failure propagates."""
    urls = await _routed_urls(date(2026, 8, 25), date(2026, 8, 27), date(2026, 8, 27),
                              fail="/historical-candle/NSE")
    assert any("/intraday/" in u for u in urls), "intraday must still be attempted"


@pytest.mark.asyncio
async def test_a_total_failure_still_raises_so_the_symbol_is_counted_as_failed():
    client = _RecordingClient(fail="upstox.com")
    with pytest.raises(RuntimeError):
        await sweeper._fetch_window(client, "NSE_EQ|INE002A01018", "30minute",
                                    date(2026, 8, 27), date(2026, 8, 27), date(2026, 8, 27))


def test_the_intraday_sweep_is_on_the_core_plane_like_its_sibling():
    """MEASURED FAILURE. `RunnerConfig.plane` defaults to "strategies", and this
    container boots with LANESET=core. Left at the default the runner was
    silently dropped from the built list — the live supervisor showed ten
    runners with `stock_spot_sweep` present and `stock_spot_intraday` absent —
    so a sweep that looked registered in source would never have fired.

    Data-plane maintenance belongs on the plane that owns the data plane."""
    configs = _runner_configs()
    assert configs["stock_spot_intraday"].get("plane") == "core"
    assert configs["stock_spot_sweep"].get("plane") == "core"


def test_the_intraday_sweep_runs_in_session_and_its_sibling_does_not():
    """The whole point of the pair: one fills the CURRENT session, one is the
    once-a-day backstop after the close."""
    configs = _runner_configs()
    assert configs["stock_spot_intraday"].get("market_hours_fn") == "_in_nse_market_hours"
    assert configs["stock_spot_sweep"].get("market_hours_fn") == "_never_in_session"
