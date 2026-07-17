"""OptionChainService consecutive-failure eviction (2026-07-17).

50 stock (symbol, expiry) pairs pinned via the ad-hoc /market/option-chain
endpoint 400-stormed the Upstox budget every 30s poll (invalid stock
instrument key) with no way out of the poll set — ~83% of the 1800/30min
budget burned, S1 stock universe starved from 10:30 IST. The tracker now
evicts a pair after EVICT_AFTER_CONSECUTIVE_FAILURES straight failures,
resets the counter on any success, and starts clean on a deliberate
re-track.
"""
from __future__ import annotations

import pytest

from market_data.option_chain import OptionChainService


class _AlwaysFailBroker:
    broker_name = "upstox"

    async def get_option_chain(self, *args, **kwargs):
        raise RuntimeError("Upstox /option/chain failed (400): Invalid Instrument key")


@pytest.mark.asyncio
async def test_permanent_failure_evicts_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    service = OptionChainService()
    service.set_broker(_AlwaysFailBroker())
    service.track("RELIANCE", "2026-07-28")

    for _ in range(service.EVICT_AFTER_CONSECUTIVE_FAILURES):
        await service._refresh("RELIANCE", "2026-07-28")

    assert ("RELIANCE", "2026-07-28") not in service._tracked
    assert ("RELIANCE", "2026-07-28") not in service._refresh_failures


@pytest.mark.asyncio
async def test_failures_below_threshold_keep_pair_tracked() -> None:
    service = OptionChainService()
    service.set_broker(_AlwaysFailBroker())
    service.track("NIFTY", "2026-07-21")

    for _ in range(service.EVICT_AFTER_CONSECUTIVE_FAILURES - 1):
        await service._refresh("NIFTY", "2026-07-21")

    assert ("NIFTY", "2026-07-21") in service._tracked
    assert service._refresh_failures[("NIFTY", "2026-07-21")] == (
        service.EVICT_AFTER_CONSECUTIVE_FAILURES - 1
    )


def test_retrack_after_eviction_resets_counter() -> None:
    service = OptionChainService()
    key = ("NIFTY", "2026-07-21")
    service._refresh_failures[key] = 7

    service.track(*key)

    # A transient outage that nearly evicted the pair must not carry its
    # failure history into the fresh tracking session.
    assert service._refresh_failures.get(key) is None
    assert key in service._tracked
