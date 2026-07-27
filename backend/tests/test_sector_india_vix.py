"""Regression tests for the India VIX parse in analytics.sector.

Why this file exists
--------------------
`SectorRotationTracker._get_india_vix()` matched the NSE payload row with an
exact, case-sensitive compare against the literal "India VIX". NSE actually
returns "INDIA VIX" (upper case), so the match never succeeded and the helper
silently returned {"price": 0, ...} for the lifetime of the repo.

That zero propagated:
  analytics/sector.py::_get_india_vix  -> price 0
  institutional_convergence/service.py::_load_india_vix -> None (value > 0 gate)
  institutional_convergence/engine.py::readiness_gates  -> vix_available False
  institutional_convergence/engine.py                   -> all(gates) False

which made the NSE convergence lane's gate conjunction unsatisfiable and left
it at 0 trades for its entire lifetime.

The existing convergence tests never caught this because they monkeypatch
`_load_india_vix` wholesale (see tests/test_institutional_convergence.py), so
the real parse was never exercised. These tests drive the real parse against a
realistically-shaped payload instead.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics.sector as sector_module
from analytics.sector import SectorRotationTracker


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient — no network is touched."""

    payload: dict = {}

    def __init__(self, *args, **kwargs) -> None:  # noqa: D107
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def get(self, *args, **kwargs) -> _FakeResponse:
        return _FakeResponse(type(self).payload)


def _run_with_payload(monkeypatch: pytest.MonkeyPatch, payload: dict) -> dict:
    _FakeAsyncClient.payload = payload
    monkeypatch.setattr(sector_module.httpx, "AsyncClient", _FakeAsyncClient)
    return asyncio.run(SectorRotationTracker()._get_india_vix())


# The real shape NSE /api/allIndices returns: the VIX row is upper case and
# sits among many other index rows.
_REAL_SHAPED_PAYLOAD = {
    "data": [
        {"index": "NIFTY 50", "last": 23995.95, "percentChange": 0.42},
        {"index": "NIFTY BANK", "last": 57087.2, "percentChange": -0.11},
        {"index": "INDIA VIX", "last": 12.66, "percentChange": -1.85},
    ]
}


def test_india_vix_parses_the_uppercase_row_nse_actually_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: NSE sends "INDIA VIX", not "India VIX"."""
    result = _run_with_payload(monkeypatch, _REAL_SHAPED_PAYLOAD)

    assert result["price"] == 12.66
    assert result["change_pct"] == -1.85
    # A non-zero price is precisely what institutional_convergence's
    # `_load_india_vix` requires to hand the engine a usable VIX.
    assert result["price"] > 0


@pytest.mark.parametrize(
    "label", ["INDIA VIX", "India VIX", "india vix", "  India VIX  "]
)
def test_india_vix_match_is_case_and_whitespace_insensitive(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """Casing/padding changes upstream must not silently brick a lane."""
    payload = {"data": [{"index": label, "last": 14.2, "percentChange": 0.5}]}

    assert _run_with_payload(monkeypatch, payload)["price"] == 14.2


def test_india_vix_absent_still_degrades_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No VIX row => the documented {"price": 0} fallback, not an exception."""
    payload = {"data": [{"index": "NIFTY 50", "last": 23995.95}]}

    result = _run_with_payload(monkeypatch, payload)

    assert result == {"price": 0, "change_pct": 0, "sparkline": []}


def test_convergence_load_india_vix_sees_the_uppercase_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the real parse must feed the convergence lane a usable VIX.

    This is the coupling that actually mattered — `_load_india_vix` maps a
    non-positive price to None, and the engine's `vix_available` gate is
    `vix is not None`. Driving the real parse (not a monkeypatched loader)
    proves the gate can now be satisfied.
    """
    import institutional_convergence.service as ic_service

    _FakeAsyncClient.payload = _REAL_SHAPED_PAYLOAD
    monkeypatch.setattr(sector_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(ic_service, "_VIX_CACHE", None, raising=False)

    vix = asyncio.run(ic_service._load_india_vix())

    assert vix == 12.66
    # `vix_available` is `vix is not None` — previously always False.
    assert vix is not None


def test_convergence_load_india_vix_is_none_when_the_row_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed is preserved: a genuinely absent VIX still blocks the gate."""
    import institutional_convergence.service as ic_service

    _FakeAsyncClient.payload = {"data": [{"index": "NIFTY 50", "last": 23995.95}]}
    monkeypatch.setattr(sector_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(ic_service, "_VIX_CACHE", None, raising=False)

    assert asyncio.run(ic_service._load_india_vix()) is None
