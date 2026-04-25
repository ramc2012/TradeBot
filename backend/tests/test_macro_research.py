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
