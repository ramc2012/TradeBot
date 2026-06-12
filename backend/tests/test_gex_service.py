"""End-to-end test for the async GEX service adapter (cached chain → engine →
payload). Validates the exact cached-chain key names the service reads."""
from __future__ import annotations

from datetime import datetime

import pytest

from directional_options import gex_service
from directional_options.gex_service import fetch_gex_analytics


def _synthetic_cached(expiry: str) -> dict:
    spot = 23366.7
    strikes = range(23000, 23701, 50)
    entries = []
    for k in strikes:
        # crude monotone premia around spot so IV solves and GEX is finite
        ce = max(2.0, spot - k + 120) if k <= spot else max(2.0, 250 - (k - spot) * 0.35)
        pe = max(2.0, k - spot + 120) if k >= spot else max(2.0, 250 - (spot - k) * 0.35)
        entries.append({"strike": float(k), "option_type": "CE", "ltp": ce, "oi": 500000, "oi_change": 1000})
        entries.append({"strike": float(k), "option_type": "PE", "ltp": pe, "oi": 480000, "oi_change": -500})
    return {
        "symbol": "NSE:NIFTY50-INDEX",
        "expiry": expiry,
        "spot_price": spot,
        "entries": entries,
        "total_ce_oi": 500000 * len(list(strikes)),
        "total_pe_oi": 480000 * len(list(strikes)),
    }


@pytest.mark.asyncio
async def test_fetch_gex_analytics_assembles_payload(monkeypatch):
    async def fake_get_cached(symbol, expiry):
        return _synthetic_cached(expiry)

    monkeypatch.setattr(gex_service.option_chain_service, "get_cached", fake_get_cached)

    now = datetime(2026, 6, 8, 11, 0, 0)
    payload = await fetch_gex_analytics(
        "NIFTY", ["2026-06-12", "2026-06-26"], warm=False, now=now, timeout=5.0
    )

    assert payload["available"] is True
    assert len(payload["per_expiry"]) == 2
    meta = payload["per_expiry"][0]["meta"]
    # Engine produced the dealer-positioning fields from the cached chain keys.
    for key in ("net_gex", "net_dex", "gamma_flip", "max_pain", "atm_iv", "pcr", "call_wall", "put_wall"):
        assert key in meta
    assert meta["net_gex"] is not None
    assert payload["per_expiry"][0]["rows"], "per-strike GEX profile present"
    # Term structure spans both expiries.
    assert payload["term"] is not None
    assert len(payload["term"]["labels"]) == 2


@pytest.mark.asyncio
async def test_fetch_gex_analytics_unavailable_on_cache_miss(monkeypatch):
    async def fake_get_cached(symbol, expiry):
        return None

    monkeypatch.setattr(gex_service.option_chain_service, "get_cached", fake_get_cached)
    payload = await fetch_gex_analytics("NIFTY", ["2026-06-12"], warm=False)
    assert payload["available"] is False
    assert payload["per_expiry"] == []
