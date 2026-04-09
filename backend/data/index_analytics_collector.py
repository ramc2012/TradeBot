from __future__ import annotations

import asyncio
import csv
import gzip
import json
import os
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from analysis.backtest import MACDBacktester, UpstoxAuthError
from analysis.instruments import INDEX_INSTRUMENT_KEYS


SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")
SUPPORTED_OPTION_TYPES = ("CE", "PE")
DEFAULT_INTERVAL = "1minute"
DEFAULT_WEEKLY_LOOKBACK_DAYS = 14
DEFAULT_MONTHLY_LOOKBACK_DAYS = 45
CATALOG_FILE_NAME = "contract_index.json"
SUMMARY_FILE_NAME = "summary.json"
SPOT_EXPIRY_KIND = "spot"


class RateLimitExhaustedError(RuntimeError):
    """Raised when a candle window keeps returning 429 after all retries."""


def _resolve_index_analytics_root() -> Path:
    env_path = os.environ.get("INDEX_ANALYTICS_DATA_DIR", "").strip()
    if env_path:
        return Path(env_path)
    docker_root = Path("/app/runtime/index_analytics_data")
    if docker_root.parent.is_dir():
        return docker_root
    return Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"


INDEX_ANALYTICS_DATA_ROOT = _resolve_index_analytics_root()


def _sort_expiry_keys(values: list[date]) -> dict[date, str]:
    monthly_max: dict[tuple[int, int], date] = {}
    for expiry in values:
        key = (expiry.year, expiry.month)
        previous = monthly_max.get(key)
        if previous is None or expiry > previous:
            monthly_max[key] = expiry
    return {
        expiry: ("monthly" if monthly_max[(expiry.year, expiry.month)] == expiry else "weekly")
        for expiry in values
    }


def _normalize_underlyings(values: list[str]) -> list[str]:
    allowed = set(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().upper()
        if value not in allowed or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _parse_expiry(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt, length in (
        ("%Y-%m-%dT%H:%M:%S.%f", 26),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(text[:length], fmt).date()
        except ValueError:
            continue
    return None


def _safe_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _build_contract_window(expiry: date, expiry_kind: str, global_from: date, global_to: date) -> tuple[date, date]:
    lookback_days = DEFAULT_MONTHLY_LOOKBACK_DAYS if expiry_kind == "monthly" else DEFAULT_WEEKLY_LOOKBACK_DAYS
    from_date = max(global_from, expiry - timedelta(days=lookback_days))
    to_date = min(global_to, expiry)
    return from_date, to_date


def _normalize_contract(contract: dict[str, Any], *, underlying: str, expiry: date, expiry_kind: str) -> Optional[dict[str, Any]]:
    option_type = str(contract.get("instrument_type") or contract.get("option_type") or "").upper()
    if option_type not in SUPPORTED_OPTION_TYPES:
        return None

    instrument_key = str(contract.get("instrument_key") or "").strip()
    trading_symbol = str(
        contract.get("trading_symbol")
        or contract.get("tradingsymbol")
        or contract.get("instrument_name")
        or instrument_key
    ).strip()
    if not instrument_key or not trading_symbol:
        return None

    try:
        strike = float(contract.get("strike_price") or contract.get("strike") or 0.0)
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None

    return {
        "underlying": underlying,
        "expiry": expiry.isoformat(),
        "expiry_kind": expiry_kind,
        "option_type": option_type,
        "strike": strike,
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "lot_size": int(contract.get("lot_size") or 0) or None,
        "tick_size": float(contract.get("tick_size") or 0.0) or None,
    }


def _contract_output_path(root: Path, contract: dict[str, Any]) -> Path:
    return (
        root
        / "contracts"
        / f"underlying={contract['underlying']}"
        / f"expiry_kind={contract['expiry_kind']}"
        / f"expiry={contract['expiry']}"
        / contract["option_type"]
        / f"{contract['trading_symbol']}.csv.gz"
    )


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _build_summary_rows(catalog_rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    total_candles = 0
    total_files = 0
    total_contracts = 0

    for row in catalog_rows:
        if not row.get("file_path"):
            continue
        dataset_type = str(row.get("dataset_type") or "options")
        expiry_kind = str(row.get("expiry_kind") or "weekly")
        key = (str(row.get("underlying") or ""), expiry_kind)
        bucket = grouped.setdefault(
            key,
            {
                "underlying": key[0],
                "expiry_kind": key[1],
                "dataset_type": dataset_type,
                "contracts": 0,
                "expiries": set(),
                "files": 0,
                "candles": 0,
                "earliest": None,
                "latest": None,
            },
        )
        candle_count = int(row.get("candle_count") or 0)
        total_contracts += 1
        total_files += 1
        total_candles += candle_count
        if dataset_type == "spot":
            bucket["contracts"] = 1
        else:
            bucket["contracts"] += 1
        bucket["files"] += 1
        bucket["candles"] += candle_count
        if dataset_type != "spot":
            bucket["expiries"].add(str(row.get("expiry") or ""))
        earliest = _safe_iso(row.get("earliest_candle"))
        latest = _safe_iso(row.get("latest_candle"))
        if earliest and (bucket["earliest"] is None or earliest < bucket["earliest"]):
            bucket["earliest"] = earliest
        if latest and (bucket["latest"] is None or latest > bucket["latest"]):
            bucket["latest"] = latest

    rows = []
    for bucket in grouped.values():
        rows.append(
            {
                "underlying": bucket["underlying"],
                "expiry_kind": bucket["expiry_kind"],
                "dataset_type": bucket["dataset_type"],
                "contracts": bucket["contracts"],
                "expiries": len([value for value in bucket["expiries"] if value]),
                "files": bucket["files"],
                "candles": bucket["candles"],
                "earliest": bucket["earliest"],
                "latest": bucket["latest"],
            }
        )

    rows.sort(key=lambda row: (row["underlying"], row["expiry_kind"]))
    return {
        "rows": rows,
        "summary": {
            "contracts": total_contracts,
            "files": total_files,
            "candles": total_candles,
        },
    }


@dataclass
class IndexAnalyticsProgress:
    task_id: str
    status: str = "pending"
    current_stage: str = "pending"
    underlyings: list[str] = None  # type: ignore[assignment]
    interval: str = DEFAULT_INTERVAL
    total_spot_series: int = 0
    processed_spot_series: int = 0
    total_expiries: int = 0
    processed_expiries: int = 0
    total_contracts: int = 0
    processed_contracts: int = 0
    total_request_units: int = 0
    processed_request_units: int = 0
    skipped_contracts: int = 0
    stored_files: int = 0
    stored_spot_files: int = 0
    stored_candles: int = 0
    stored_spot_candles: int = 0
    incomplete_contracts: int = 0
    current_underlying: str = ""
    current_expiry: str = ""
    current_symbol: str = ""
    data_root: str = ""
    latest_file: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.underlyings is None:
            self.underlyings = []

    @property
    def pct(self) -> float:
        if self.status == "done":
            return 100.0
        progress = 0.0
        if self.total_spot_series > 0:
            progress += 10.0 * (self.processed_spot_series / self.total_spot_series)
        if self.total_expiries > 0:
            progress += 15.0 * (self.processed_expiries / self.total_expiries)
        if self.total_request_units > 0:
            progress += 75.0 * (self.processed_request_units / self.total_request_units)
        elif self.total_contracts > 0:
            progress += 75.0 * (self.processed_contracts / self.total_contracts)
        if progress <= 0 and self.status == "running":
            return 0.5
        return round(min(progress, 99.9), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "underlyings": list(self.underlyings),
            "interval": self.interval,
            "total_spot_series": self.total_spot_series,
            "processed_spot_series": self.processed_spot_series,
            "total_expiries": self.total_expiries,
            "processed_expiries": self.processed_expiries,
            "total_contracts": self.total_contracts,
            "processed_contracts": self.processed_contracts,
            "total_request_units": self.total_request_units,
            "processed_request_units": self.processed_request_units,
            "skipped_contracts": self.skipped_contracts,
            "stored_files": self.stored_files,
            "stored_spot_files": self.stored_spot_files,
            "stored_candles": self.stored_candles,
            "stored_spot_candles": self.stored_spot_candles,
            "incomplete_contracts": self.incomplete_contracts,
            "current_underlying": self.current_underlying,
            "current_expiry": self.current_expiry,
            "current_symbol": self.current_symbol,
            "data_root": self.data_root,
            "latest_file": self.latest_file,
            "error": self.error,
            "pct": self.pct,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_secs": (
                int(((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds())
                if self.started_at
                else 0
            ),
        }


class IndexAnalyticsCollector:
    def __init__(self, access_token: str, data_root: Optional[Path] = None) -> None:
        self.client = MACDBacktester(access_token=access_token)
        self.client.rate_limit_delay = 0.25
        self.data_root = data_root or INDEX_ANALYTICS_DATA_ROOT
        self.catalog_path = self.data_root / CATALOG_FILE_NAME
        self.summary_path = self.data_root / SUMMARY_FILE_NAME
        self.task_dir = self.data_root / "tasks"

    def _task_path(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def _persist_progress(self, progress: IndexAnalyticsProgress) -> None:
        progress.data_root = str(self.data_root)
        _write_json(self._task_path(progress.task_id), progress.to_dict())

    async def _fetch_active_contracts(self, underlying: str, expiry: date) -> list[dict[str, Any]]:
        underlying_key = await self.client._get_underlying_key(underlying)
        if not underlying_key:
            return []
        url = (
            "https://api.upstox.com/v2/option/contract"
            f"?instrument_key={urllib.parse.quote(underlying_key, safe='')}"
            f"&expiry_date={expiry.isoformat()}"
        )
        async with self.client._semaphore:
            await self.client._throttle()
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                self.client._record_api_call("option_contract")
                response = await http_client.get(url, headers=self.client.headers)
        if response.status_code in (401, 403):
            raise UpstoxAuthError(
                f"Active contract API rejected the Upstox token for {underlying} {expiry} "
                f"(HTTP {response.status_code})."
            )
        if response.status_code != 200:
            logger.debug(
                f"Active contracts HTTP {response.status_code} for {underlying} {expiry}: "
                f"{response.text[:160]}"
            )
            return []
        return response.json().get("data", [])

    async def _fetch_contracts_for_expiry(self, underlying: str, expiry: date, *, today: Optional[date] = None) -> list[dict[str, Any]]:
        current_day = today or date.today()
        if expiry < current_day:
            return await self.client._fetch_expired_contracts(underlying, expiry)
        return await self._fetch_active_contracts(underlying, expiry)

    async def _fetch_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
        retry_count: int = 0,
    ) -> list[dict[str, Any]]:
        encoded_key = urllib.parse.quote(instrument_key, safe="")
        is_expired_key = instrument_key.count("|") >= 2
        base_url = "https://api.upstox.com/v2/expired-instruments/historical-candle" if is_expired_key else "https://api.upstox.com/v2/historical-candle"
        url = f"{base_url}/{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"

        async with self.client._semaphore:
            await self.client._throttle()
            async with httpx.AsyncClient(timeout=45.0) as http_client:
                self.client._record_api_call(
                    "expired_historical_candle" if is_expired_key else "historical_candle"
                )
                response = await http_client.get(url, headers=self.client.headers)

        if response.status_code == 429:
            if retry_count >= 5:
                logger.warning(
                    f"Rate limit exhausted for {instrument_key} {from_date.isoformat()}->{to_date.isoformat()}"
                )
                raise RateLimitExhaustedError(
                    f"429 persisted for {instrument_key} {from_date.isoformat()}->{to_date.isoformat()}"
                )
            backoff = min(30, 5 * (retry_count + 1))
            await asyncio.sleep(backoff)
            return await self._fetch_candles(
                instrument_key,
                interval,
                from_date,
                to_date,
                retry_count=retry_count + 1,
            )

        if is_expired_key and response.status_code in (401, 403):
            raise UpstoxAuthError(
                f"Expired candle API rejected the Upstox token for {instrument_key} "
                f"(HTTP {response.status_code})."
            )

        if not is_expired_key and response.status_code in (400, 401, 403):
            async with httpx.AsyncClient(timeout=45.0) as http_client:
                self.client._record_api_call("historical_candle")
                response = await http_client.get(url, headers={"Accept": "application/json"})

        if response.status_code != 200:
            logger.debug(
                f"Candle fetch HTTP {response.status_code} for {instrument_key}: {response.text[:160]}"
            )
            return []

        raw_candles = list(reversed(response.json().get("data", {}).get("candles", [])))
        rows: list[dict[str, Any]] = []
        for candle in raw_candles:
            if not candle or len(candle) < 6:
                continue
            rows.append(
                {
                    "time": str(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5] or 0),
                    "oi": int(candle[6] or 0) if len(candle) > 6 and candle[6] is not None else 0,
                }
            )
        return rows

    @staticmethod
    def _window_key(from_date: date, to_date: date) -> str:
        return f"{from_date.isoformat()}::{to_date.isoformat()}"

    @staticmethod
    def _build_chunk_windows(from_date: date, to_date: date, chunk_days: int) -> list[tuple[date, date]]:
        windows: list[tuple[date, date]] = []
        cursor = from_date
        while cursor <= to_date:
            window_end = min(cursor + timedelta(days=chunk_days - 1), to_date)
            windows.append((cursor, window_end))
            cursor = window_end + timedelta(days=1)
        return windows

    def _load_existing_candles(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with gzip.open(path, "rt", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows.append(
                    {
                        "time": str(row.get("time") or ""),
                        "open": float(row.get("open") or 0.0),
                        "high": float(row.get("high") or 0.0),
                        "low": float(row.get("low") or 0.0),
                        "close": float(row.get("close") or 0.0),
                        "volume": int(float(row.get("volume") or 0)),
                        "oi": int(float(row.get("oi") or 0)),
                    }
                )
        return rows

    async def _fetch_chunked_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
        *,
        existing_candles: Optional[list[dict[str, Any]]] = None,
        completed_windows: Optional[list[str]] = None,
        persist_chunk: Optional[Any] = None,
        chunk_days: int = 14,
    ) -> dict[str, Any]:
        merged: dict[str, dict[str, Any]] = {
            str(candle["time"]): candle for candle in (existing_candles or [])
        }
        completed = set(completed_windows or [])
        windows = self._build_chunk_windows(from_date, to_date, chunk_days)

        for chunk_from, chunk_to in windows:
            window_key = self._window_key(chunk_from, chunk_to)
            if window_key in completed:
                continue
            try:
                candles = await self._fetch_candles(instrument_key, interval, chunk_from, chunk_to)
            except RateLimitExhaustedError as exc:
                return {
                    "candles": [merged[key] for key in sorted(merged)],
                    "completed_windows": sorted(completed),
                    "complete": False,
                    "blocked_window": window_key,
                    "error": str(exc),
                }
            for candle in candles:
                merged[str(candle["time"])] = candle
            completed.add(window_key)
            if persist_chunk:
                persist_chunk([merged[key] for key in sorted(merged)], sorted(completed))

        return {
            "candles": [merged[key] for key in sorted(merged)],
            "completed_windows": sorted(completed),
            "complete": True,
            "blocked_window": None,
            "error": "",
        }

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        payload = _load_json(self.catalog_path, {})
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def _save_catalog(self, catalog: dict[str, dict[str, Any]]) -> None:
        _write_json(self.catalog_path, catalog)
        _write_json(self.summary_path, _build_summary_rows(list(catalog.values())))

    def _write_contract_file(self, path: Path, candles: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["time", "open", "high", "low", "close", "volume", "oi"],
            )
            writer.writeheader()
            writer.writerows(candles)

    async def _fetch_spot_history(
        self,
        underlying: str,
        interval: str,
        from_date: date,
        to_date: date,
    ) -> tuple[str, list[dict[str, Any]]]:
        instrument_key = INDEX_INSTRUMENT_KEYS.get(underlying)
        if not instrument_key:
            raise ValueError(f"Unsupported underlying for spot history: {underlying}")
        result = await self._fetch_chunked_candles(
            instrument_key,
            interval,
            from_date,
            to_date,
        )
        return instrument_key, result["candles"]

    def _spot_output_path(self, underlying: str, interval: str) -> Path:
        return (
            self.data_root
            / "spot"
            / f"underlying={underlying}"
            / f"{interval}.csv.gz"
        )

    async def run(
        self,
        *,
        underlyings: list[str],
        from_date: date,
        to_date: date,
        interval: str = DEFAULT_INTERVAL,
        progress: Optional[IndexAnalyticsProgress] = None,
    ) -> IndexAnalyticsProgress:
        normalized_underlyings = _normalize_underlyings(underlyings or list(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS))
        if not normalized_underlyings:
            raise ValueError(f"At least one supported underlying required: {', '.join(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS)}")

        if progress is None:
            progress = IndexAnalyticsProgress(
                task_id=f"index-analytics-{int(datetime.now(UTC).timestamp())}",
                underlyings=normalized_underlyings,
                interval=interval,
            )

        progress.status = "running"
        progress.current_stage = "discovering_expiries"
        progress.started_at = datetime.now(UTC)
        self._persist_progress(progress)

        catalog = self._load_catalog()
        expiry_jobs: list[dict[str, Any]] = []
        stored_option_counts: dict[str, int] = {}
        for row in catalog.values():
            if (
                isinstance(row, dict)
                and row.get("dataset_type", "options") == "options"
                and row.get("file_path")
            ):
                key = str(row.get("underlying") or "")
                stored_option_counts[key] = stored_option_counts.get(key, 0) + 1

        try:
            progress.current_stage = "downloading_spot"
            progress.total_spot_series = len(normalized_underlyings)
            self._persist_progress(progress)

            for underlying in normalized_underlyings:
                progress.current_underlying = underlying
                progress.current_expiry = ""
                progress.current_symbol = f"{underlying} SPOT"
                output_path = self._spot_output_path(underlying, interval)
                spot_key = f"spot::{underlying}::{interval}"
                existing = catalog.get(spot_key)
                existing_window_from = _parse_expiry((existing or {}).get("window_from"))
                existing_window_to = _parse_expiry((existing or {}).get("window_to"))
                if (
                    output_path.exists()
                    and existing
                    and existing_window_from is not None
                    and existing_window_to is not None
                    and existing_window_from <= from_date
                    and existing_window_to >= to_date
                ):
                    progress.processed_spot_series += 1
                    progress.stored_spot_files += 1
                    progress.latest_file = str(output_path)
                    self._persist_progress(progress)
                    continue

                instrument_key, candles = await self._fetch_spot_history(
                    underlying,
                    interval,
                    from_date,
                    to_date,
                )
                if candles:
                    self._write_contract_file(output_path, candles)
                    progress.stored_files += 1
                    progress.stored_spot_files += 1
                    progress.stored_candles += len(candles)
                    progress.stored_spot_candles += len(candles)
                    progress.latest_file = str(output_path)
                    catalog[spot_key] = {
                        "dataset_type": "spot",
                        "underlying": underlying,
                        "expiry": None,
                        "expiry_kind": SPOT_EXPIRY_KIND,
                        "option_type": "SPOT",
                        "strike": None,
                        "instrument_key": instrument_key,
                        "trading_symbol": f"{underlying} SPOT",
                        "interval": interval,
                        "file_path": str(output_path.relative_to(self.data_root)),
                        "candle_count": len(candles),
                        "earliest_candle": candles[0]["time"],
                        "latest_candle": candles[-1]["time"],
                        "window_from": from_date.isoformat(),
                        "window_to": to_date.isoformat(),
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                    self._save_catalog(catalog)
                progress.processed_spot_series += 1
                self._persist_progress(progress)

            progress.current_stage = "discovering_expiries"
            self._persist_progress(progress)

            for underlying in normalized_underlyings:
                expiry_dates = await self.client._fetch_expiry_dates(underlying)
                relevant_expiries = [expiry for expiry in expiry_dates if from_date <= expiry <= to_date]
                kind_map = _sort_expiry_keys(relevant_expiries)
                progress.total_expiries += len(relevant_expiries)
                for expiry in relevant_expiries:
                    expiry_jobs.append(
                        {
                            "underlying": underlying,
                            "expiry": expiry,
                            "expiry_kind": kind_map[expiry],
                        }
                    )

            discovered_contracts: dict[str, dict[str, Any]] = {}
            progress.current_stage = "discovering_contracts"
            self._persist_progress(progress)

            for job in expiry_jobs:
                progress.current_underlying = job["underlying"]
                progress.current_expiry = job["expiry"].isoformat()
                progress.current_symbol = ""
                contracts = await self._fetch_contracts_for_expiry(job["underlying"], job["expiry"])
                for contract in contracts:
                    normalized = _normalize_contract(
                        contract,
                        underlying=job["underlying"],
                        expiry=job["expiry"],
                        expiry_kind=job["expiry_kind"],
                    )
                    if not normalized:
                        continue
                    discovered_contracts[normalized["instrument_key"]] = normalized
                progress.processed_expiries += 1
                progress.total_contracts = len(discovered_contracts)
                self._persist_progress(progress)

            ordered_contracts = sorted(
                discovered_contracts.values(),
                key=lambda row: (
                    stored_option_counts.get(str(row["underlying"]), 0),
                    row["expiry"],
                    row["underlying"],
                    row["expiry_kind"] != "monthly",
                    row["option_type"],
                    float(row["strike"]),
                ),
            )

            pending_contracts: list[dict[str, Any]] = []
            progress.total_request_units = 0
            progress.processed_request_units = 0

            for contract in ordered_contracts:
                output_path = _contract_output_path(self.data_root, contract)
                existing = catalog.get(contract["instrument_key"])
                window_from, window_to = _build_contract_window(
                    date.fromisoformat(contract["expiry"]),
                    contract["expiry_kind"],
                    from_date,
                    to_date,
                )
                all_window_keys = [
                    self._window_key(chunk_from, chunk_to)
                    for chunk_from, chunk_to in self._build_chunk_windows(window_from, window_to, 14)
                ]
                progress.total_request_units += len(all_window_keys)
                existing_window_from = _parse_expiry((existing or {}).get("window_from"))
                existing_window_to = _parse_expiry((existing or {}).get("window_to"))
                existing_complete = bool((existing or {}).get("complete", True))
                existing_completed_windows = [
                    str(value)
                    for value in (existing or {}).get("completed_windows", [])
                    if str(value)
                ]

                if (
                    existing
                    and existing_complete
                    and existing_window_from is not None
                    and existing_window_to is not None
                    and existing_window_from <= window_from
                    and existing_window_to >= window_to
                    and (
                        output_path.exists()
                        or (
                            not existing.get("file_path")
                            and int(existing.get("candle_count") or 0) == 0
                        )
                    )
                ):
                    progress.skipped_contracts += 1
                    progress.processed_contracts += 1
                    progress.processed_request_units += len(all_window_keys)
                    if output_path.exists():
                        progress.latest_file = str(output_path)
                    continue

                progress.processed_request_units += len(existing_completed_windows)
                pending_contracts.append(
                    {
                        "contract": contract,
                        "output_path": output_path,
                        "existing": existing,
                        "window_from": window_from,
                        "window_to": window_to,
                        "all_window_keys": all_window_keys,
                        "existing_completed_windows": existing_completed_windows,
                    }
                )

            progress.current_stage = "downloading_candles"
            self._persist_progress(progress)

            for item in pending_contracts:
                contract = item["contract"]
                progress.current_underlying = contract["underlying"]
                progress.current_expiry = contract["expiry"]
                progress.current_symbol = contract["trading_symbol"]
                output_path = item["output_path"]
                existing = item["existing"]
                window_from = item["window_from"]
                window_to = item["window_to"]
                all_window_keys = item["all_window_keys"]
                existing_candles = self._load_existing_candles(output_path) if output_path.exists() else []
                existing_completed_windows = item["existing_completed_windows"]
                had_output_file = output_path.exists()
                counted_candles = len(existing_candles)
                counted_completed_windows = len(existing_completed_windows)

                def persist_contract_snapshot(snapshot_candles: list[dict[str, Any]], completed_windows: list[str]) -> None:
                    nonlocal had_output_file, counted_candles, counted_completed_windows
                    if snapshot_candles:
                        self._write_contract_file(output_path, snapshot_candles)
                        if not had_output_file:
                            progress.stored_files += 1
                            had_output_file = True
                        delta = max(0, len(snapshot_candles) - counted_candles)
                        progress.stored_candles += delta
                        counted_candles = len(snapshot_candles)
                        progress.latest_file = str(output_path)
                    delta_windows = max(0, len(completed_windows) - counted_completed_windows)
                    progress.processed_request_units += delta_windows
                    counted_completed_windows = len(completed_windows)
                    pending_windows = [key for key in all_window_keys if key not in set(completed_windows)]
                    catalog[contract["instrument_key"]] = {
                        **contract,
                        "interval": interval,
                        "file_path": str(output_path.relative_to(self.data_root)) if snapshot_candles else None,
                        "candle_count": len(snapshot_candles),
                        "earliest_candle": snapshot_candles[0]["time"] if snapshot_candles else None,
                        "latest_candle": snapshot_candles[-1]["time"] if snapshot_candles else None,
                        "window_from": window_from.isoformat(),
                        "window_to": window_to.isoformat(),
                        "completed_windows": list(completed_windows),
                        "pending_windows": pending_windows,
                        "complete": len(pending_windows) == 0,
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                    self._save_catalog(catalog)
                    self._persist_progress(progress)

                fetch_result = await self._fetch_chunked_candles(
                    contract["instrument_key"],
                    interval,
                    window_from,
                    window_to,
                    existing_candles=existing_candles,
                    completed_windows=existing_completed_windows,
                    persist_chunk=persist_contract_snapshot,
                )

                candles = fetch_result["candles"]
                completed_windows = fetch_result["completed_windows"]
                pending_windows = [key for key in all_window_keys if key not in set(completed_windows)]
                catalog[contract["instrument_key"]] = {
                    **contract,
                    "interval": interval,
                    "file_path": str(output_path.relative_to(self.data_root)) if candles else None,
                    "candle_count": len(candles),
                    "earliest_candle": candles[0]["time"] if candles else None,
                    "latest_candle": candles[-1]["time"] if candles else None,
                    "window_from": window_from.isoformat(),
                    "window_to": window_to.isoformat(),
                    "completed_windows": list(completed_windows),
                    "pending_windows": pending_windows,
                    "complete": bool(fetch_result["complete"]),
                    "last_error": fetch_result["error"] or None,
                    "blocked_window": fetch_result["blocked_window"],
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                self._save_catalog(catalog)
                if not fetch_result["complete"]:
                    progress.incomplete_contracts += 1
                progress.processed_contracts += 1
                self._persist_progress(progress)

            self._save_catalog(catalog)
            progress.status = "done"
            progress.current_stage = "completed"
            progress.current_underlying = ""
            progress.current_expiry = ""
            progress.current_symbol = ""
            progress.finished_at = datetime.now(UTC)
            self._persist_progress(progress)
            return progress
        except Exception as exc:
            progress.status = "error"
            progress.error = str(exc)
            progress.finished_at = datetime.now(UTC)
            self._persist_progress(progress)
            logger.exception("[IndexAnalyticsCollector] dataset build failed")
            raise


def load_index_analytics_summary(data_root: Optional[Path] = None) -> dict[str, Any]:
    root = data_root or INDEX_ANALYTICS_DATA_ROOT
    payload = _load_json(root / SUMMARY_FILE_NAME, {"rows": [], "summary": {"contracts": 0, "files": 0, "candles": 0}})
    if not isinstance(payload, dict):
        payload = {"rows": [], "summary": {"contracts": 0, "files": 0, "candles": 0}}
    payload["data_root"] = str(root)
    return payload
