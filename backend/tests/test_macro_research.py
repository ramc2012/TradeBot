from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_research.service import MacroResearchService


def _offline_service() -> MacroResearchService:
    service = MacroResearchService()
    service._live_macro = False
    service._live_commodities = False
    service._live_trends = False
    return service


@pytest.mark.asyncio
async def test_macro_research_overview_contains_agent_consumable_sections() -> None:
    service = _offline_service()

    overview = await service.overview(refresh=True)

    assert overview["macro_indicators"]
    assert overview["commodities"]
    assert overview["sectors"]
    assert overview["sector_leaders"]
    assert overview["budding_themes"]
    assert overview["market_read"]["agent_instruction"]


@pytest.mark.asyncio
async def test_sector_detail_exposes_research_matrix_and_prompt() -> None:
    service = _offline_service()

    detail = await service.sector_detail("AUTO", refresh=True)

    assert detail["sector"]["code"] == "AUTO"
    assert detail["research_matrix"]
    assert any("EV" in point["signal"] or "EV" in point["metric"] for point in detail["research_matrix"])
    assert "Evaluate" in detail["agent_prompt"]


@pytest.mark.asyncio
async def test_search_finds_budding_ev_theme_inside_auto_filter() -> None:
    service = _offline_service()

    result = await service.search("EV battery charging supply chain", sector_code="AUTO", refresh=True)

    assert result["results"]
    assert any(hit["scope"] == "budding_theme" for hit in result["results"])
    assert all(hit["sector_code"] == "AUTO" for hit in result["results"])


def _utc(day: int, hour: int, minute: int = 0):
    from datetime import datetime, timezone

    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_mcx_quote_change_is_prev_session_close_over_two_sessions() -> None:
    from macro_research.internal_commodities import compute_quote_from_rows

    rows = [
        # Session 1 (IST 2026-07-30): last close 8000.
        (_utc(30, 5, 0), "commodity_broker_history", 7950.0),
        (_utc(30, 17, 55), "commodity_broker_history", 8000.0),
        # Session 2 (IST 2026-07-31): last close 8100.
        (_utc(31, 5, 0), "commodity_broker_history", 8050.0),
        (_utc(31, 17, 58), "commodity_broker_history", 8100.0),
    ]
    quote = compute_quote_from_rows(rows)

    assert quote is not None
    assert quote["price"] == 8100.0
    assert quote["change_pct"] == 1.25  # 8100 vs 8000
    assert quote["change_basis"] == "prev_session_close"
    assert quote["source"] == "mcx_internal_1m"


def test_mcx_quote_single_session_uses_session_open_basis() -> None:
    from macro_research.internal_commodities import compute_quote_from_rows

    rows = [
        (_utc(31, 5, 0), "commodity_broker_history", 8000.0),
        (_utc(31, 12, 0), "commodity_broker_history", 8160.0),
    ]
    quote = compute_quote_from_rows(rows)

    assert quote is not None
    assert quote["price"] == 8160.0
    assert quote["change_pct"] == 2.0
    assert quote["change_basis"] == "session_open"


def test_mcx_quote_prefers_broker_history_over_live_tick_per_minute() -> None:
    from macro_research.internal_commodities import compute_quote_from_rows

    ts = _utc(31, 12, 0)
    rows = [
        (_utc(31, 5, 0), "commodity_broker_history", 8000.0),
        (ts, "live_tick", 9999.0),
        (ts, "commodity_broker_history", 8100.0),
    ]
    quote = compute_quote_from_rows(rows)

    assert quote is not None
    assert quote["price"] == 8100.0


def test_mcx_quote_empty_rows_returns_none() -> None:
    from macro_research.internal_commodities import compute_quote_from_rows

    assert compute_quote_from_rows([]) is None


@pytest.mark.asyncio
async def test_offline_commodity_snapshot_serves_seeds_without_db() -> None:
    service = _offline_service()

    commodities = await service._commodity_snapshot()

    assert len(commodities) == 5
    assert all(row["source"] == "offline_seed" for row in commodities)
    by_code = {row["code"]: row for row in commodities}
    assert by_code["CRUDE"]["unit"] == "INR/bbl"
    assert by_code["GOLD"]["unit"] == "INR/10g"
