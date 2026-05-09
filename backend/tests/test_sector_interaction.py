from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sector_interaction.ingestion import SectorIngestionStore, SectorObservation
from sector_interaction.india_public_collectors import IndiaCollectorResult, IndiaPublicDataCollector
from sector_interaction.india_live import SECTOR_LABELS, india_live_sector_service, sector_for_symbol, sector_label
from sector_interaction.nse_constituents import NSEConstituentService
import sector_interaction.service as sector_service_module
from sector_interaction.service import SectorInteractionService


def test_sector_interaction_model_returns_us_network() -> None:
    service = SectorInteractionService()

    payload = service.model("US", periods=96, max_lag=2, alpha=0.1)

    assert payload["country"] == "US"
    assert payload["source_mode"] == "synthetic"
    assert len(payload["sectors"]) == 11
    assert payload["correlation_matrix"]["labels"] == payload["sectors"]
    assert len(payload["correlation_matrix"]["values"]) == 11
    assert "nodes" in payload["network"]
    assert "edges" in payload["network"]
    assert payload["rankings"]["leaders"]


def test_sector_interaction_model_returns_india_network() -> None:
    service = SectorInteractionService()

    payload = service.model("IN", periods=96, max_lag=2, alpha=0.1)

    assert payload["country"] == "IN"
    assert len(payload["sectors"]) == 9
    assert any(node["sector"] == "Nifty Bank" for node in payload["network"]["nodes"])


def test_acquisition_plan_includes_compliance_and_sources() -> None:
    service = SectorInteractionService()

    plan = service.acquisition_plan()

    assert plan["source_categories"]
    assert any("official APIs" in control for control in plan["legal_controls"])
    assert any(category["name"] == "Search and attention" for category in plan["source_categories"])


def test_sector_rag_documents_are_stable() -> None:
    service = SectorInteractionService()

    docs = service.rag_documents()

    assert {doc["id"] for doc in docs} == {
        "playbook-sector-interaction-var-granger",
        "playbook-sector-alternative-data-pipeline",
    }


def test_sector_source_map_exposes_indicator_contracts() -> None:
    service = SectorInteractionService()

    payload = service.source_map("US")

    assert payload["country"] == "US"
    assert payload["indicators"]
    assert "required_columns" in payload["data_contract"]
    assert any(indicator["code"] == "search_cloud_ai" for indicator in payload["indicators"])


def test_sector_signals_rank_mapped_sectors() -> None:
    service = SectorInteractionService()

    payload = service.signals("IN", periods=96)

    assert payload["country"] == "IN"
    assert payload["runtime_handoff"]["active"] is False
    assert payload["rankings"]
    assert payload["indicator_latest"]
    assert {"overweight", "underweight", "neutral"} >= {payload["rankings"][0]["stance"]}


def test_extended_network_includes_indicator_edges() -> None:
    service = SectorInteractionService()

    payload = service.extended_network("US", periods=96, max_lag=2, alpha=0.1)

    assert payload["summary"]["indicator_count"] > 0
    assert payload["summary"]["indicator_edge_count"] > 0
    assert any(edge.get("source_type") == "indicator" for edge in payload["indicator_edges"])


def test_validation_backtest_returns_curve_and_summary() -> None:
    service = SectorInteractionService()

    payload = service.validation_backtest("US", periods=96)

    assert payload["summary"]["observations"] > 0
    assert "information_ratio" in payload["summary"]
    assert payload["equity_curve"]


def test_pipeline_status_exposes_connector_readiness() -> None:
    service = SectorInteractionService()

    payload = service.pipeline_status("IN")

    assert payload["country"] == "IN"
    assert payload["summary"]["connector_count"] == len(payload["connectors"])
    assert payload["data_layers"]
    assert payload["execution_controls"]
    assert any(connector["source_status"] == "open_data" for connector in payload["connectors"])
    assert any(connector["source_status"] == "internal_live_market_data" for connector in payload["connectors"])


def test_sector_report_summarizes_signals_and_blockers() -> None:
    service = SectorInteractionService()

    payload = service.sector_report("US", periods=96)

    assert payload["country"] == "US"
    assert payload["headline"]
    assert payload["summary_bullets"]
    assert payload["strongest_indicator_edges"]
    assert payload["next_actions"]


def test_ingestion_status_exposes_runtime_store_contract() -> None:
    service = SectorInteractionService()

    payload = service.ingestion_status("US")

    assert payload["country"] == "US"
    assert "runtime_summary" in payload
    assert payload["storage_status"]["backend"] in {"jsonl_local", "postgres_runtime_state+jsonl"}
    assert payload["connectors"]
    assert payload["promotion_rules"]


def test_run_ingestion_dry_run_generates_open_data_observations(monkeypatch) -> None:
    service = SectorInteractionService()
    sample = SectorObservation(
        date="2026-03-31",
        country="IN",
        indicator_code="upi_spend_growth",
        sector="Nifty Bank",
        value=1.5,
        quality_score=0.74,
        source="NPCI UPI statistics",
        source_status="open_data",
        collector_version="test",
        run_id="test-run",
        created_at="2026-05-03T00:00:00+00:00",
        metadata={"configured_exposure": 0.45},
    )

    def _fake_collect(*, config, indicators, run_id, timeout_seconds=8.0):
        return IndiaCollectorResult(
            observations=[sample],
            blocked_connectors=[
                {
                    "indicator_code": "gst_auto_pulse",
                    "label": "Auto GST and registration pulse",
                    "source_status": "open_data",
                    "reason": "not configured in test",
                }
            ],
            errors=[],
        )

    monkeypatch.setattr("sector_interaction.india_public_collectors.india_public_data_collector.collect", _fake_collect)

    payload = service.run_ingestion("IN", dry_run=True)

    assert payload["country"] == "IN"
    assert payload["dry_run"] is True
    assert payload["generated_observations"] == 1
    assert payload["stored_observations"] == 0
    assert payload["preview_observations"]
    assert payload["blocked_connectors"]
    assert payload["preview_observations"][0]["collector_version"] == "test"


def test_india_public_collector_parses_npci_upi_rows() -> None:
    collector = IndiaPublicDataCollector()
    payload = {
        "status": 200,
        "data": {
            "results": [
                {
                    "month": "March-2026",
                    "no_of_banks_live_on_upi": "705",
                    "volume_in_mn": "22,641.11",
                    "value_in_cr": "29,52,542.05",
                },
                {
                    "month": "February-2026",
                    "no_of_banks_live_on_upi": "694",
                    "volume_in_mn": "20,394.18",
                    "value_in_cr": "26,84,229.29",
                },
            ]
        },
    }

    rows = collector._parse_upi_rows(payload)

    assert rows[0]["date"] == "2026-03-31"
    assert rows[0]["value_cr"] == 2952542.05
    assert rows[1]["date"] == "2026-02-28"


def test_india_public_collector_parses_ppac_crude_rows(monkeypatch) -> None:
    collector = IndiaPublicDataCollector()
    monkeypatch.setattr(collector, "_current_financial_year", lambda: "2025-2026")
    payload = {
        "result": {
            "3": {
                "title": "<b>CRUDE OIL</b>",
                "april": "<b>20986</b>",
                "may": "21329",
                "june": "20314",
                "july": "18889",
                "august": "19603",
                "september": "20208",
                "october": "21005",
                "november": "21236",
                "december": "21588",
                "january": "21094",
                "february": "20128",
                "march": "19002",
            }
        }
    }

    rows = collector._parse_ppac_crude_rows(payload)

    assert rows[0]["date"] == "2025-04-30"
    assert rows[-1]["date"] == "2026-03-31"
    assert rows[0]["crude_import"] == 20986.0


def test_india_live_taxonomy_maps_fno_watchlist_symbols() -> None:
    assert sector_for_symbol("HDFCBANK") == "nifty_private_bank"
    assert sector_for_symbol("INFY") == "nifty_it"
    assert sector_for_symbol("ULTRACEMCO") == "nifty_cement"
    assert sector_for_symbol("ETERNAL") == "nifty_consumer_services"
    assert sector_label("nifty_cement") == SECTOR_LABELS["nifty_cement"]


def test_nse_constituent_parser_reads_official_csv_symbols() -> None:
    service = NSEConstituentService()

    payload = "Company Name,Industry,Symbol,Series,ISIN Code\nInfosys Ltd.,IT,INFY,EQ,INE009A01021\n"

    assert service._parse_symbols(payload) == ["INFY"]


def test_nse_constituent_parser_extracts_page_constituent_links() -> None:
    service = NSEConstituentService()

    urls = service._extract_constituent_urls(
        '<a href="/../../IndexConstituent/ind_niftyindiadefence_list.csv">Index Constituent</a>',
        "https://www.niftyindices.com/indices/equity/thematic-indices/nifty-india-defence",
    )

    assert urls == ["https://www.niftyindices.com/IndexConstituent/ind_niftyindiadefence_list.csv"]


def test_india_live_taxonomy_prefers_official_nse_overlay(monkeypatch) -> None:
    monkeypatch.setattr(
        "sector_interaction.india_live.nse_constituent_service.sector_for_symbol",
        lambda symbol: "nifty_it" if symbol == "HDFCBANK" else None,
    )

    assert sector_for_symbol("HDFCBANK") == "nifty_it"


def test_india_sector_alt_data_includes_live_metrics(monkeypatch) -> None:
    monkeypatch.setattr(
        "sector_interaction.india_live.nse_constituent_service.status",
        lambda: {"sectors": [{"sector_key": "nifty_it", "constituents": 10}]},
    )
    items = [
        {
            "kind": "STOCK",
            "change_pct": 1.2,
            "oi_signal": 0.4,
            "volume": 200_000,
            "iv": 32.0,
            "rsi": 61,
        },
        {
            "kind": "STOCK",
            "change_pct": -0.3,
            "oi_signal": 0.1,
            "volume": 80_000,
            "iv": 28.0,
            "rsi": 49,
        },
    ]

    rows = india_live_sector_service._sector_alt_data("nifty_it", items)

    assert rows[0]["name"] == "ATM option-flow breadth"
    assert rows[0]["status"] == "live_atm_watchlist"
    assert rows[0]["value"] == 50.0
    assert next(row for row in rows if row["name"] == "Average IV")["state"] == "normal"
    assert any(row["status"] == "official_niftyindices_overlay" for row in rows)


def test_india_live_market_ingestion_uses_live_observations(monkeypatch) -> None:
    service = SectorInteractionService()

    async def _fake_observations(config, run_id):
        return [
            SectorObservation(
                date="2026-05-03",
                country=config.code,
                indicator_code="india_live_leadership_score",
                sector="Nifty IT",
                value=1.25,
                quality_score=0.82,
                source="F&O/ATM watchlist snapshots",
                source_status="internal_live_market_data",
                collector_version="test",
                run_id=run_id,
                created_at="2026-05-03T00:00:00+00:00",
                metadata={"configured_exposure": 1.0},
            )
        ]

    monkeypatch.setattr(service, "_build_india_live_market_observations", _fake_observations)

    payload = asyncio.run(service.run_india_live_market_ingestion(dry_run=True))

    assert payload["country"] == "IN"
    assert payload["generated_observations"] == 1
    assert payload["preview_observations"][0]["source_status"] == "internal_live_market_data"


def test_signals_promote_runtime_observations_when_history_is_sufficient(monkeypatch, tmp_path) -> None:
    store = SectorIngestionStore(tmp_path)
    monkeypatch.setattr(sector_service_module, "sector_ingestion_store", store)
    run_id = store.build_run_id()
    observations = []
    for offset in range(24):
        date = f"2024-01-{offset + 1:02d}"
        observations.append(
            SectorObservation(
                date=date,
                country="US",
                indicator_code="energy_inventory_pressure",
                sector="Energy",
                value=0.1 + offset * 0.01,
                quality_score=0.69,
                source="EIA inventory and production statistics",
                source_status="open_data",
                collector_version="test",
                run_id=f"{run_id}-{offset}",
                created_at=f"{date}T00:00:00+00:00",
                metadata={"configured_exposure": -0.75},
            )
        )
    store.append_observations(observations)
    service = SectorInteractionService()

    payload = service.signals("US", periods=96)

    assert payload["source_mode"] == "runtime_alternative_data"
    assert payload["runtime_handoff"]["active"] is True
    assert payload["runtime_handoff"]["observed_dates"] == 24
