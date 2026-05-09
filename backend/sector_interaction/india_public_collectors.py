"""Approved public-source collectors for India sector interaction indicators."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from sector_interaction.ingestion import SectorObservation, sector_ingestion_store


NPCI_UPI_MONTHLY_URL = (
    "https://www.npci.org.in/api/product-statistic/tab/detail"
    "?product_name=upi&tab_name=product-statistics-upi&year_range={year_range}"
    "&excel_type=monthly&page_no=1&page_size=12&locale=en"
)
PPAC_IMPORT_EXPORT_URL = "https://ppac.gov.in/AjaxController/getImportExports"

MONTH_NAMES = {
    name.lower(): index
    for index, name in enumerate(calendar.month_name)
    if name
}


@dataclass(frozen=True)
class IndiaCollectorResult:
    observations: list[SectorObservation]
    blocked_connectors: list[dict[str, Any]]
    errors: list[str]


class IndiaPublicDataCollector:
    """Collects real India public indicators without synthetic fallback."""

    collector_version = "sector-ingestion-india-public-v1"

    def collect(self, *, config: Any, indicators: list[Any], run_id: str, timeout_seconds: float = 8.0) -> IndiaCollectorResult:
        observations: list[SectorObservation] = []
        blocked: list[dict[str, Any]] = []
        errors: list[str] = []
        indicators_by_code = {indicator.code: indicator for indicator in indicators}

        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=self._headers()) as client:
            for indicator in indicators:
                if indicator.source_status != "open_data":
                    continue
                try:
                    if indicator.code == "upi_spend_growth":
                        observations.extend(self._collect_upi_spend_growth(client, config, indicator, run_id))
                    elif indicator.code == "crude_import_pressure":
                        observations.extend(self._collect_crude_import_pressure(client, config, indicator, run_id))
                    else:
                        blocked.append(
                            {
                                "indicator_code": indicator.code,
                                "label": indicator.label,
                                "source_status": indicator.source_status,
                                "reason": "real public collector not yet configured for this source",
                            }
                        )
                except Exception as exc:
                    errors.append(f"{indicator.code}: {exc}")
                    blocked.append(
                        {
                            "indicator_code": indicator.code,
                            "label": indicator.label,
                            "source_status": indicator.source_status,
                            "reason": str(exc),
                        }
                    )

        for code, indicator in indicators_by_code.items():
            if indicator.source_status == "open_data" and not any(row.indicator_code == code for row in observations):
                if not any(row.get("indicator_code") == code for row in blocked):
                    blocked.append(
                        {
                            "indicator_code": code,
                            "label": indicator.label,
                            "source_status": indicator.source_status,
                            "reason": "collector returned no parseable observations",
                        }
                    )
        return IndiaCollectorResult(observations=observations, blocked_connectors=blocked, errors=errors)

    def _collect_upi_spend_growth(self, client: httpx.Client, config: Any, indicator: Any, run_id: str) -> list[SectorObservation]:
        payloads = self._fetch_upi_payloads(client)
        rows: list[dict[str, Any]] = []
        for payload in payloads:
            rows.extend(self._parse_upi_rows(payload))
        rows = self._dedupe_metric_rows(rows, value_key="value_cr")
        rows.sort(key=lambda row: row["date"])
        values = [float(row["value_cr"]) for row in rows]
        observations: list[SectorObservation] = []
        created_at = sector_ingestion_store.now_iso()
        for index, row in enumerate(rows):
            prior = values[index - 1] if index > 0 else None
            if prior is None or abs(prior) <= 1e-9:
                continue
            growth_pct = ((float(row["value_cr"]) / prior) - 1.0) * 100.0
            observations.extend(
                self._sector_observations(
                    config=config,
                    indicator=indicator,
                    run_id=run_id,
                    created_at=created_at,
                    observation_date=row["date"],
                    base_value=growth_pct,
                    metadata={
                        "mode": "official_public_source",
                        "source_url": NPCI_UPI_MONTHLY_URL.format(year_range=self._short_year_range(str(row.get("financial_year") or self._current_financial_year()))),
                        "metric_key": "upi_value_mom_pct",
                        "financial_year": row.get("financial_year"),
                        "month": row["month"],
                        "banks": row.get("banks"),
                        "volume_mn": row.get("volume_mn"),
                        "value_cr": row.get("value_cr"),
                    },
                )
            )
        if not observations:
            raise RuntimeError("NPCI UPI API returned rows but no month-over-month values")
        return observations

    def _collect_crude_import_pressure(self, client: httpx.Client, config: Any, indicator: Any, run_id: str) -> list[SectorObservation]:
        payloads = self._fetch_ppac_import_exports(client)
        rows: list[dict[str, Any]] = []
        for payload in payloads:
            rows.extend(self._parse_ppac_crude_rows(payload))
        rows = self._dedupe_metric_rows(rows, value_key="crude_import")
        rows.sort(key=lambda row: row["date"])
        values = [float(row["crude_import"]) for row in rows]
        observations: list[SectorObservation] = []
        created_at = sector_ingestion_store.now_iso()
        for index, row in enumerate(rows):
            prior = values[index - 1] if index > 0 else None
            if prior is None or abs(prior) <= 1e-9:
                continue
            crude_import_mom_pct = ((float(row["crude_import"]) / prior) - 1.0) * 100.0
            less_pressure_score = -crude_import_mom_pct
            observations.extend(
                self._sector_observations(
                    config=config,
                    indicator=indicator,
                    run_id=run_id,
                    created_at=created_at,
                    observation_date=row["date"],
                    base_value=less_pressure_score,
                    metadata={
                        "mode": "official_public_source",
                        "source_url": PPAC_IMPORT_EXPORT_URL,
                        "metric_key": "negative_crude_import_mom_pct",
                        "financial_year": row.get("financial_year"),
                        "month": row["month"],
                        "crude_import_000_mt": row["crude_import"],
                    },
                )
            )
        if not observations:
            raise RuntimeError("PPAC import/export endpoint returned rows but no month-over-month values")
        return observations

    def _sector_observations(
        self,
        *,
        config: Any,
        indicator: Any,
        run_id: str,
        created_at: str,
        observation_date: str,
        base_value: float,
        metadata: dict[str, Any],
    ) -> list[SectorObservation]:
        observations = []
        for sector, exposure in indicator.sector_weights.items():
            if sector not in config.sectors:
                continue
            exposure_value = float(exposure)
            observations.append(
                SectorObservation(
                    date=observation_date,
                    country=config.code,
                    indicator_code=indicator.code,
                    sector=sector,
                    value=round(float(base_value) * exposure_value, 6),
                    quality_score=indicator.quality_score,
                    source=indicator.production_source,
                    source_status=indicator.source_status,
                    collector_version=self.collector_version,
                    run_id=run_id,
                    created_at=created_at,
                    metadata={
                        "category": indicator.category,
                        "cadence": indicator.cadence,
                        "metric_definition": indicator.metric_definition,
                        "configured_exposure": exposure_value,
                        **metadata,
                    },
                )
            )
        return observations

    def _fetch_upi_payloads(self, client: httpx.Client) -> list[dict[str, Any]]:
        payloads = []
        errors = []
        for year_range in self._candidate_financial_years():
            try:
                response = client.get(NPCI_UPI_MONTHLY_URL.format(year_range=self._short_year_range(year_range)))
                response.raise_for_status()
                payload = response.json()
                if int(payload.get("status") or 0) == 200:
                    payload["financial_year"] = year_range
                    payloads.append(payload)
                    continue
                errors.append(f"{year_range}: NPCI API status {payload.get('status')}")
            except Exception as exc:
                errors.append(f"{year_range}: {exc}")
        if payloads:
            return payloads
        raise RuntimeError("; ".join(errors) or "NPCI API returned no usable financial year")

    def _fetch_ppac_import_exports(self, client: httpx.Client) -> list[dict[str, Any]]:
        payloads = []
        errors = []
        for year_range in self._candidate_financial_years():
            try:
                response = client.post(
                    PPAC_IMPORT_EXPORT_URL,
                    data={"financialYear": year_range, "reportBy": "1", "pageId": "14"},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                response.raise_for_status()
                payload = response.json()
                result = payload.get("result")
                if isinstance(result, dict) and result:
                    payload["financial_year"] = year_range
                    payloads.append(payload)
                    continue
                errors.append(f"{year_range}: empty PPAC result")
            except Exception as exc:
                errors.append(f"{year_range}: {exc}")
        if payloads:
            return payloads
        raise RuntimeError("; ".join(errors) or "PPAC response does not contain a result object")

    def _parse_upi_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        results = (((payload.get("data") or {}).get("results")) or [])
        rows: list[dict[str, Any]] = []
        for row in results:
            if not isinstance(row, dict):
                continue
            month_label = str(row.get("month") or "").strip()
            observation_date = self._month_label_to_end_date(month_label)
            if observation_date is None:
                continue
            rows.append(
                {
                    "date": observation_date,
                    "month": month_label,
                    "financial_year": payload.get("financial_year"),
                    "banks": self._to_float(row.get("no_of_banks_live_on_upi")),
                    "volume_mn": self._to_float(row.get("volume_in_mn")),
                    "value_cr": self._to_float(row.get("value_in_cr")),
                }
            )
        if len(rows) < 2:
            raise RuntimeError("NPCI UPI API returned fewer than two parseable monthly rows")
        return rows

    def _parse_ppac_crude_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("result") or {}
        crude_row = next(
            (
                row
                for row in result.values()
                if isinstance(row, dict) and self._strip_markup(row.get("title")).upper() == "CRUDE OIL"
            ),
            None,
        )
        if crude_row is None:
            raise RuntimeError("PPAC response did not include a CRUDE OIL row")
        year_range = str(payload.get("financial_year") or self._current_financial_year())
        start_year = int(year_range.split("-", 1)[0])
        rows = []
        fiscal_months = list(range(4, 13)) + list(range(1, 4))
        for month_index in fiscal_months:
            month_name = calendar.month_name[month_index]
            key = month_name.lower()
            if key not in crude_row:
                continue
            value = self._to_float(crude_row.get(key))
            year = start_year if month_index >= 4 else start_year + 1
            rows.append(
                {
                    "date": self._month_end_date(year, month_index),
                    "month": f"{month_name}-{year}",
                    "financial_year": year_range,
                    "crude_import": value,
                }
            )
        if len(rows) < 2:
            raise RuntimeError("PPAC response returned fewer than two crude import monthly values")
        return rows

    def _current_financial_year(self) -> str:
        today = date.today()
        start_year = today.year if today.month >= 4 else today.year - 1
        return f"{start_year}-{start_year + 1}"

    def _candidate_financial_years(self) -> list[str]:
        current = self._current_financial_year()
        start_year = int(current.split("-", 1)[0])
        return [f"{year}-{year + 1}" for year in range(start_year, start_year - 4, -1)]

    def _short_year_range(self, year_range: str) -> str:
        start, end = year_range.split("-", 1)
        return f"{start}-{end[-2:]}"

    def _month_label_to_end_date(self, label: str) -> str | None:
        match = re.match(r"^([A-Za-z]+)-(\d{4})$", label.strip())
        if not match:
            return None
        month = MONTH_NAMES.get(match.group(1).lower())
        if month is None:
            return None
        return self._month_end_date(int(match.group(2)), month)

    def _month_end_date(self, year: int, month: int) -> str:
        day = calendar.monthrange(year, month)[1]
        return date(year, month, day).isoformat()

    def _to_float(self, value: Any) -> float:
        text = self._strip_markup(value).replace(",", "").strip()
        if not text:
            return 0.0
        return float(text)

    def _dedupe_metric_rows(self, rows: list[dict[str, Any]], *, value_key: str) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            date_key = str(row.get("date") or "")
            if not date_key:
                continue
            if not row.get(value_key):
                continue
            deduped[date_key] = row
        return list(deduped.values())

    def _strip_markup(self, value: Any) -> str:
        return re.sub(r"<[^>]*>", "", str(value or "")).strip()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 sector-interaction-public-collector/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.npci.org.in/",
        }


india_public_data_collector = IndiaPublicDataCollector()
