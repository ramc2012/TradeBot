"""M1 collector: NSE participant-wise open interest (FII/DII/Pro/Client).

Pulls one CSV per trading day from NSE's public archive and upserts into
`participant_oi`. No existing table in the live schema carries this data --
unlike bars, option-chain PCR/OI and the MWPL/ban list, which are already
collected under other names and are read directly rather than re-fetched
(see db/migrations/001_schema.sql for the inventory).

URL and header pattern follow backend/market_data/fo_risk_ingest.py, the
live app's own NSE-archive fetcher, which already solved the two things that
break a naive fetch: NSE 403s a request without a browser User-Agent, and the
archive path has moved before, so a fixed URL is not durable. Verified live
2026-08-26: `fao_participant_oi_DDMMYYYY.csv`, browser UA + Referer, 200 on
trading days, 404 on non-trading days.

Doctrine #5 (everything measurable): every run -- including a weekend 404 --
writes one row to `ingest_log`, so "5 consecutive sessions with zero missed
EOD feeds" (the P1 acceptance gate) has real evidence to check rather than
an assertion.

    python vanguard/ingest/m1_participant_oi.py                  # today
    python vanguard/ingest/m1_participant_oi.py --date 2026-08-25
    python vanguard/ingest/m1_participant_oi.py --backfill-days 10
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

URL_TEMPLATE = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# CSV column header -> (participant-agnostic bucket, side). Matched by
# substring rather than fixed position: NSE has reshuffled this file's
# columns before (same caution backend/market_data/fo_risk_ingest.py takes).
COLUMN_BUCKETS = [
    ("Future Index Long", "fut_index", "long"),
    ("Future Index Short", "fut_index", "short"),
    ("Future Stock Long", "fut_stock", "long"),
    ("Future Stock Short", "fut_stock", "short"),
    ("Option Index Call Long", "opt_index_call", "long"),
    ("Option Index Put Long", "opt_index_put", "long"),
    ("Option Index Call Short", "opt_index_call", "short"),
    ("Option Index Put Short", "opt_index_put", "short"),
    ("Option Stock Call Long", "opt_stock_call", "long"),
    ("Option Stock Put Long", "opt_stock_put", "long"),
    ("Option Stock Call Short", "opt_stock_call", "short"),
    ("Option Stock Put Short", "opt_stock_put", "short"),
]
PARTICIPANT_ROWS = {"Client", "DII", "FII", "Pro"}


@dataclass
class FetchResult:
    status: str          # ok | empty | error | not_a_trading_day
    rows: list[dict]
    detail: str = ""


def fetch(target: date, client: httpx.Client) -> FetchResult:
    url = URL_TEMPLATE.format(ddmmyyyy=target.strftime("%d%m%Y"))
    try:
        response = client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except httpx.HTTPError as exc:
        return FetchResult("error", [], f"request failed: {exc}")
    if response.status_code == 404:
        return FetchResult("not_a_trading_day", [], f"404 at {url}")
    if response.status_code != 200:
        return FetchResult("error", [], f"HTTP {response.status_code} at {url}")
    try:
        rows = parse(response.text)
    except ValueError as exc:
        return FetchResult("error", [], str(exc))
    if not rows:
        return FetchResult("empty", [], "parsed zero participant rows")
    return FetchResult("ok", rows)


def parse(text: str) -> list[dict]:
    """CSV in -> [{participant, bucket, long_contracts, short_contracts}, ...].

    The file has a banner title row, then a header row, then one row per
    participant plus a TOTAL row (dropped -- it is a derived sum, not a
    fourth participant, and summing FII+DII+Pro+Client ourselves is the
    correctness check that a stored TOTAL would only ever mask).
    """
    reader = list(csv.reader(io.StringIO(text)))
    header_index = next((i for i, row in enumerate(reader)
                        if row and row[0].strip().lower() == "client type"), None)
    if header_index is None:
        raise ValueError("no 'Client Type' header row found -- NSE may have changed the format")
    header = [cell.strip() for cell in reader[header_index]]
    column_for = {}
    for label, bucket, side in COLUMN_BUCKETS:
        matches = [i for i, cell in enumerate(header) if label.lower() in cell.lower()]
        if not matches:
            raise ValueError(f"column '{label}' not found in header: {header}")
        column_for[(bucket, side)] = matches[0]

    long_short: dict[tuple[str, str], dict[str, int]] = {}
    for row in reader[header_index + 1:]:
        if not row or not row[0].strip():
            continue
        participant = row[0].strip()
        if participant not in PARTICIPANT_ROWS:
            continue  # skips the TOTAL row and any trailing blank/footer lines
        for (bucket, side), column in column_for.items():
            raw = row[column].strip().replace(",", "") if column < len(row) else ""
            value = int(raw) if raw.lstrip("-").isdigit() else 0
            entry = long_short.setdefault((participant, bucket), {"long": 0, "short": 0})
            entry[side] = value

    return [
        {"participant": participant, "bucket": bucket,
         "long_contracts": sides["long"], "short_contracts": sides["short"]}
        for (participant, bucket), sides in long_short.items()
    ]


def upsert(connection, target: date, rows: list[dict]) -> int:
    payload = [
        (target, row["participant"], row["bucket"], row["long_contracts"], row["short_contracts"])
        for row in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO participant_oi (dt, participant, bucket, long_contracts, short_contracts)
               VALUES %s
               ON CONFLICT (dt, participant, bucket) DO UPDATE SET
                 long_contracts = EXCLUDED.long_contracts,
                 short_contracts = EXCLUDED.short_contracts,
                 synced_at = now()""",
            payload,
        )
    return len(payload)


def log_run(connection, target: date, result: FetchResult, rows_written: int) -> None:
    """One evidence row per (collector, day) for the "5 clean sessions" gate.

    ingest_log's partial unique index enforces at most one status='ok' row
    per (collector, target_date); a re-run of an already-ok day must UPDATE
    that row, not insert a second one, or a routine re-run (idempotent by
    design -- see upsert()) would crash on the very idempotency it exists to
    support. A re-run of a failed/empty day is allowed to insert a fresh row,
    which is the retry history the acceptance gate wants to see.
    """
    status = "ok" if result.status == "ok" else (
        "empty" if result.status in ("empty", "not_a_trading_day") else "error")
    with connection.cursor() as cursor:
        if status == "ok":
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (collector, target_date) WHERE status = 'ok' DO UPDATE SET
                     run_at = now(), rows_written = EXCLUDED.rows_written, detail = EXCLUDED.detail""",
                ("m1_participant_oi", target, status, rows_written, result.detail),
            )
        else:
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("m1_participant_oi", target, status, rows_written, result.detail),
            )


def run(target: date, dsn: str) -> FetchResult:
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        with httpx.Client() as client:
            result = fetch(target, client)
        rows_written = upsert(connection, target, result.rows) if result.status == "ok" else 0
        log_run(connection, target, result, rows_written)
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="also attempt this many prior calendar days")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    target = args.date or datetime.now(timezone.utc).date()
    targets = [target - timedelta(days=offset) for offset in range(args.backfill_days + 1)]

    exit_code = 0
    for day in targets:
        result = run(day, args.dsn)
        print(f"{day.isoformat()}: {result.status}"
              + (f" -- {result.detail}" if result.detail else "")
              + (f" ({len(result.rows)} participant/bucket rows)" if result.rows else ""))
        if result.status == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
