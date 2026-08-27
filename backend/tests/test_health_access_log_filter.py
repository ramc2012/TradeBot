"""GET /health polling must not evict real business-log lines from Docker's
capped retention (5 x 50m = 250MB). On 2026-08-26, 41% of a container's
retained log (30,906 of 75,132 lines) was "GET /health" access-log noise,
which rotated out the commodity-strategy decision lines needed to diagnose a
same-day trigger question before anyone could read them.
"""
from __future__ import annotations

import logging


def _install_filter():
    # Mirrors the filter installed in main.py without importing main (which
    # pulls in the full app + broker stack).
    import importlib.util
    import pathlib

    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("class _QuietPolledEndpoints")
    end = source.index("\n\n\n", start)
    namespace: dict = {"logging": logging, "_logging": logging}
    exec(source[start:end], namespace)  # noqa: S102 - test-only, trusted source file
    return namespace["_QuietPolledEndpoints"]()


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )


def test_health_polling_is_dropped():
    f = _install_filter()
    assert f.filter(_record('127.0.0.1:1234 - "GET /health HTTP/1.1" 200')) is False


def test_pools_polling_is_dropped():
    f = _install_filter()
    assert f.filter(_record('127.0.0.1:1234 - "GET /api/system/pools HTTP/1.1" 200')) is False


def test_other_routes_still_log():
    f = _install_filter()
    assert f.filter(_record('127.0.0.1:1234 - "GET /api/commodity/strategy-agent/status HTTP/1.1" 200')) is True
    assert f.filter(_record('127.0.0.1:1234 - "PUT /api/commodity/strategy-agent/config HTTP/1.1" 200')) is True
