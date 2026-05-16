"""Daily F&O risk ingestion: MWPL utilisation + ban-list.

Pulls two NSE-published CSVs once per day, normalises them and upserts
into `fo_mwpl_snapshot` and `fo_security_ban`. These feed the Stage 6
(Risk & Margin) view the blueprint requires:

  * MWPL utilisation %  — how close each F&O underlying is to its
                          market-wide position-limit cap. ≥ 80% means
                          new positions will likely be restricted on
                          the next print; ≥ 95% triggers the ban list.
  * Ban list            — symbols where utilisation ≥ 95% on a given
                          day. The exchange disallows fresh F&O
                          positions on those symbols (only closing
                          trades allowed). Critical pre-trade check.

The ingester is async, idempotent and tolerant of CSV format drift —
fields are matched on header substrings rather than fixed column
indices because NSE has historically reshuffled column ordering.

Daily run cadence is enforced by the caller (research-sync scheduler).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal


# NSE rotates these URLs every couple of years. We try each candidate in
# order — first 200 with a non-empty body wins. The files refresh once
# per trading day, usually a few minutes after the EOD calc (~18:30 IST).
MWPL_URL_CANDIDATES = [
    # 2024+ archives host with the newer combined CSV that includes both
    # market-wide and client-wise positions.
    "https://nsearchives.nseindia.com/archives/nsccl/mwpl/combine_mwpl_cmpos.csv",
    # Older path that still works for some files on the archives host.
    "https://nsearchives.nseindia.com/content/nsccl/fao_mwpl.csv",
    # Pre-2023 main-site path; kept as a fallback in case the archives
    # host has another scheduled maintenance window.
    "https://www.nseindia.com/content/nsccl/fao_mwpl.csv",
]
SECURITY_BAN_URL_CANDIDATES = [
    "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban.csv",
    "https://nsearchives.nseindia.com/content/nsccl/fao_security_ban.csv",
    "https://www.nseindia.com/content/nsccl/fao_security_ban.csv",
]
# JSON fallback when the CSV endpoints all 404 (NSE's most reliable path
# during URL transitions, but rate-limited and cookie-sensitive).
NSE_FO_RESTRICTIONS_JSON = "https://www.nseindia.com/api/equity-fno-restrictions"

# Mimic a normal browser User-Agent so NSE doesn't 403 the bot.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


@dataclass
class MwplRow:
    symbol: str
    market_wide_position_limit: Optional[int]
    open_interest: Optional[int]
    utilisation_pct: Optional[float]


@dataclass
class BanRow:
    symbol: str
    reason: Optional[str]


@dataclass
class IngestSummary:
    snapshot_date: date
    mwpl_rows: int = 0
    mwpl_inserted: int = 0
    ban_rows: int = 0
    ban_inserted: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "mwpl_rows": self.mwpl_rows,
            "mwpl_inserted": self.mwpl_inserted,
            "ban_rows": self.ban_rows,
            "ban_inserted": self.ban_inserted,
            "errors": list(self.errors),
        }


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text_value = str(value).strip().replace(",", "")
    if not text_value:
        return None
    try:
        return int(float(text_value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text_value = str(value).strip().replace(",", "").rstrip("%")
    if not text_value:
        return None
    try:
        return float(text_value)
    except (TypeError, ValueError):
        return None


def _match_column(headers: list[str], *needles: str) -> Optional[int]:
    """Return the index of the first header whose normalised name
    contains any of the needles (case-insensitive, whitespace-stripped).
    """
    normalised = [str(h).strip().lower().replace("_", " ") for h in headers]
    for needle in needles:
        n = needle.strip().lower().replace("_", " ")
        for i, header in enumerate(normalised):
            if n in header:
                return i
    return None


def parse_mwpl_csv(body: str) -> list[MwplRow]:
    """Parse fao_mwpl.csv tolerating column-order drift.

    NSE has historically used a few formats here. Columns are matched by
    header substring rather than positionally so the parser survives
    minor renames.
    """
    reader = csv.reader(io.StringIO(body))
    header: Optional[list[str]] = None
    rows: list[MwplRow] = []
    for raw in reader:
        if not raw:
            continue
        # The MWPL file sometimes prefixes with a metadata banner row; the
        # real header always contains "SYMBOL" or "Symbol".
        if header is None:
            if any("symbol" in str(cell).strip().lower() for cell in raw):
                header = raw
            continue
        if not any(cell.strip() for cell in raw):
            continue
        symbol_idx = _match_column(header, "symbol")
        mwpl_idx = _match_column(header, "mwpl", "market wide position limit")
        oi_idx = _match_column(header, "open interest", "client wise", "oi as on")
        util_idx = _match_column(header, "utilisation", "utilization", "%")
        if symbol_idx is None:
            continue
        symbol = str(raw[symbol_idx] or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            MwplRow(
                symbol=symbol,
                market_wide_position_limit=_safe_int(raw[mwpl_idx]) if mwpl_idx is not None and mwpl_idx < len(raw) else None,
                open_interest=_safe_int(raw[oi_idx]) if oi_idx is not None and oi_idx < len(raw) else None,
                utilisation_pct=_safe_float(raw[util_idx]) if util_idx is not None and util_idx < len(raw) else None,
            )
        )
    return rows


def parse_ban_csv(body: str) -> list[BanRow]:
    """Parse fao_security_ban.csv.

    The file is typically a single-column list of symbols under a
    "Sr.No.,Security" header. Some days it embeds a date banner first.
    """
    reader = csv.reader(io.StringIO(body))
    header: Optional[list[str]] = None
    rows: list[BanRow] = []
    for raw in reader:
        if not raw:
            continue
        if header is None:
            if any("security" in str(cell).strip().lower() for cell in raw):
                header = raw
            continue
        if not any(cell.strip() for cell in raw):
            continue
        sec_idx = _match_column(header, "security", "symbol")
        if sec_idx is None or sec_idx >= len(raw):
            continue
        symbol = str(raw[sec_idx] or "").strip().upper()
        if not symbol or symbol in {"SECURITY", "SYMBOL"}:
            continue
        # Some bulletins include an explanatory reason column.
        reason_idx = _match_column(header, "reason", "remarks")
        reason = (
            str(raw[reason_idx]).strip()
            if reason_idx is not None and reason_idx < len(raw) and raw[reason_idx]
            else None
        )
        rows.append(BanRow(symbol=symbol, reason=reason))
    return rows


async def _fetch_csv(url: str, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> str:
    """Fetch one CSV. The optional `client` is reused for cookie state
    across the candidate URL chain to avoid repeating the cookie-priming
    hit on every attempt.
    """
    own_client = client is None
    http = client or httpx.AsyncClient(
        headers=_DEFAULT_HEADERS, timeout=timeout, follow_redirects=True
    )
    try:
        response = await http.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text
    finally:
        if own_client:
            await http.aclose()


async def _fetch_ban_via_json_api() -> Optional[str]:
    """JSON-API fallback for the ban list. NSE often keeps this endpoint
    working even when the static CSV files 404. Returns the data
    reshaped into the same Sr.No,Security CSV format the CSV parser
    already understands.
    """
    try:
        async with httpx.AsyncClient(
            headers={**_DEFAULT_HEADERS, "Accept": "application/json"},
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            await client.get("https://www.nseindia.com", timeout=8.0)
            response = await client.get(NSE_FO_RESTRICTIONS_JSON, timeout=15.0)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.debug(f"[FoRiskIngest] JSON ban-list fallback failed: {exc}")
        return None
    # The JSON shape we've historically seen:
    #   {"data": [{"symbol": "IDEA", "remark": "Banned ..."}], "timestamp": "..."}
    items = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("fo") or []
    elif isinstance(data, list):
        items = data
    if not items:
        return None
    lines = ["Sr.No.,Security,Remarks"]
    for idx, item in enumerate(items, start=1):
        if isinstance(item, dict):
            symbol = item.get("symbol") or item.get("scrip") or item.get("security")
            remark = item.get("remark") or item.get("reason") or ""
        else:
            symbol, remark = str(item), ""
        if symbol:
            lines.append(f"{idx},{symbol},{remark}")
    return "\n".join(lines)


async def _fetch_first_available(
    urls: list[str],
    *,
    label: str,
    timeout: float = 20.0,
) -> tuple[Optional[str], list[str]]:
    """Try each URL until one returns a non-empty body. Returns
    (body, errors) — body is None when every candidate failed.
    """
    errors: list[str] = []
    async with httpx.AsyncClient(
        headers=_DEFAULT_HEADERS, timeout=timeout, follow_redirects=True
    ) as client:
        # Prime cookies once for the session.
        try:
            await client.get("https://www.nseindia.com", timeout=8.0)
        except Exception as exc:
            errors.append(f"cookie_prime_failed: {exc}")
        for url in urls:
            try:
                body = await _fetch_csv(url, timeout=timeout, client=client)
                if body and body.strip():
                    logger.info(f"[FoRiskIngest] {label} fetched from {url} ({len(body)} bytes)")
                    return body, errors
                errors.append(f"empty_body: {url}")
            except Exception as exc:
                errors.append(f"{url}: {exc}")
    return None, errors


async def ingest_fo_risk_snapshot(
    *,
    snapshot_date: Optional[date] = None,
) -> IngestSummary:
    """Fetch and persist today's MWPL + ban-list snapshot.

    Returns a summary even when one of the two fetches fails — the
    errors list captures the per-source detail so the daily run can
    keep going if NSE returns 5xx on one endpoint.
    """
    snap_date = snapshot_date or datetime.now(timezone.utc).date()
    summary = IngestSummary(snapshot_date=snap_date)

    # ── MWPL ─────────────────────────────────────────────────────────
    try:
        mwpl_body, fetch_errors = await _fetch_first_available(MWPL_URL_CANDIDATES, label="MWPL")
        if mwpl_body is None:
            raise RuntimeError(f"all_candidates_failed: {fetch_errors[-3:]}")
        mwpl_rows = parse_mwpl_csv(mwpl_body)
        summary.mwpl_rows = len(mwpl_rows)
        if mwpl_rows:
            payload = [
                {
                    "snapshot_date": snap_date,
                    "symbol": row.symbol,
                    "market_wide_position_limit": row.market_wide_position_limit,
                    "open_interest": row.open_interest,
                    "utilisation_pct": row.utilisation_pct,
                }
                for row in mwpl_rows
            ]
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO fo_mwpl_snapshot (
                            snapshot_date, symbol,
                            market_wide_position_limit, open_interest, utilisation_pct
                        ) VALUES (
                            :snapshot_date, :symbol,
                            :market_wide_position_limit, :open_interest, :utilisation_pct
                        )
                        ON CONFLICT (snapshot_date, symbol) DO UPDATE
                        SET market_wide_position_limit = EXCLUDED.market_wide_position_limit,
                            open_interest = EXCLUDED.open_interest,
                            utilisation_pct = EXCLUDED.utilisation_pct,
                            synced_at = NOW()
                        """
                    ),
                    payload,
                )
                await session.commit()
            summary.mwpl_inserted = len(payload)
            logger.info(f"[FoRiskIngest] MWPL upserted: {summary.mwpl_inserted} rows for {snap_date}")
    except Exception as exc:
        summary.errors.append(f"mwpl_fetch_failed: {exc}")
        logger.warning(f"[FoRiskIngest] MWPL fetch failed: {exc}")

    # ── Ban list ─────────────────────────────────────────────────────
    try:
        ban_body, fetch_errors = await _fetch_first_available(SECURITY_BAN_URL_CANDIDATES, label="Ban list")
        if ban_body is None:
            # JSON fallback through the NSE API.
            ban_body = await _fetch_ban_via_json_api()
        if ban_body is None:
            raise RuntimeError(f"all_candidates_failed: {fetch_errors[-3:]}")
        ban_rows = parse_ban_csv(ban_body)
        summary.ban_rows = len(ban_rows)
        if ban_rows:
            payload = [
                {
                    "snapshot_date": snap_date,
                    "symbol": row.symbol,
                    "reason": row.reason,
                }
                for row in ban_rows
            ]
            async with AsyncSessionLocal() as session:
                # Clear today's prior rows first — the ban list is
                # authoritative as a *full* snapshot each day, not a
                # delta. If a symbol drops off, we want it gone.
                await session.execute(
                    text("DELETE FROM fo_security_ban WHERE snapshot_date = :d"),
                    {"d": snap_date},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO fo_security_ban (snapshot_date, symbol, reason)
                        VALUES (:snapshot_date, :symbol, :reason)
                        """
                    ),
                    payload,
                )
                await session.commit()
            summary.ban_inserted = len(payload)
            logger.info(f"[FoRiskIngest] Ban-list upserted: {summary.ban_inserted} rows for {snap_date}")
        else:
            # No rows on the ban list still means: "today's snapshot was
            # empty" — that's valid (most days have zero banned
            # securities). Persist a tombstone delete only.
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("DELETE FROM fo_security_ban WHERE snapshot_date = :d"),
                    {"d": snap_date},
                )
                await session.commit()
    except Exception as exc:
        summary.errors.append(f"ban_fetch_failed: {exc}")
        logger.warning(f"[FoRiskIngest] Ban-list fetch failed: {exc}")

    return summary


async def latest_fo_risk_snapshot(
    *,
    top_utilisation: int = 30,
) -> dict[str, Any]:
    """Read the latest MWPL + ban-list snapshot for the dashboard payload.

    Returns:
      {
        "snapshot_date": "2026-05-16",
        "mwpl": {
          "row_count": 213,
          "high_utilisation": [{"symbol", "utilisation_pct", ...}],
          "any_above_80_pct": int,
          "any_above_95_pct": int,
        },
        "ban_list": {
          "count": int,
          "symbols": ["IDEA", ...]
        }
      }
    """
    async with AsyncSessionLocal() as session:
        # Latest snapshot date with rows
        result = await session.execute(
            text("SELECT MAX(snapshot_date) FROM fo_mwpl_snapshot")
        )
        latest_mwpl = result.scalar()
        result = await session.execute(
            text("SELECT MAX(snapshot_date) FROM fo_security_ban")
        )
        latest_ban = result.scalar()

        mwpl_payload: dict[str, Any] = {
            "row_count": 0,
            "high_utilisation": [],
            "above_80_pct_count": 0,
            "above_95_pct_count": 0,
            "snapshot_date": latest_mwpl.isoformat() if latest_mwpl else None,
        }
        if latest_mwpl is not None:
            result = await session.execute(
                text(
                    """
                    SELECT symbol, market_wide_position_limit, open_interest, utilisation_pct
                    FROM fo_mwpl_snapshot
                    WHERE snapshot_date = :d
                    """
                ),
                {"d": latest_mwpl},
            )
            rows = list(result.mappings().all())
            mwpl_payload["row_count"] = len(rows)
            mwpl_payload["above_80_pct_count"] = sum(
                1 for row in rows if (row.get("utilisation_pct") or 0.0) >= 80.0
            )
            mwpl_payload["above_95_pct_count"] = sum(
                1 for row in rows if (row.get("utilisation_pct") or 0.0) >= 95.0
            )
            ordered = sorted(
                rows,
                key=lambda row: row.get("utilisation_pct") or 0.0,
                reverse=True,
            )[:top_utilisation]
            mwpl_payload["high_utilisation"] = [
                {
                    "symbol": row.get("symbol"),
                    "market_wide_position_limit": row.get("market_wide_position_limit"),
                    "open_interest": row.get("open_interest"),
                    "utilisation_pct": round(float(row.get("utilisation_pct") or 0.0), 2),
                }
                for row in ordered
            ]

        ban_payload: dict[str, Any] = {
            "count": 0,
            "symbols": [],
            "snapshot_date": latest_ban.isoformat() if latest_ban else None,
        }
        if latest_ban is not None:
            result = await session.execute(
                text(
                    """
                    SELECT symbol, reason
                    FROM fo_security_ban
                    WHERE snapshot_date = :d
                    ORDER BY symbol
                    """
                ),
                {"d": latest_ban},
            )
            rows = list(result.mappings().all())
            ban_payload["count"] = len(rows)
            ban_payload["symbols"] = [str(row.get("symbol")) for row in rows]
            # Surface a reason map only if the file populated it.
            reasons = {
                str(row.get("symbol")): str(row.get("reason"))
                for row in rows
                if row.get("reason")
            }
            if reasons:
                ban_payload["reasons"] = reasons

    return {
        "snapshot_date": (latest_mwpl or latest_ban).isoformat() if (latest_mwpl or latest_ban) else None,
        "mwpl": mwpl_payload,
        "ban_list": ban_payload,
    }


__all__ = [
    "IngestSummary",
    "MwplRow",
    "BanRow",
    "ingest_fo_risk_snapshot",
    "latest_fo_risk_snapshot",
    "parse_ban_csv",
    "parse_mwpl_csv",
]
