"""Bulk-deals + block-deals collector: NSE's daily large-trade disclosures.

Pulls two CSVs from NSE's public archive CDN and upserts into `bulk_block`,
tagged by kind ('bulk' | 'block'). No existing table in the live schema
carries this data (checked: no table name matching bulk/block/deal exists
in the 81-table live `nomadcurie` schema as of 2026-08-26).

## URL shape is genuinely different from m1_participant_oi's

fao_participant_oi is published as one file *per trading day*, addressed by
date in the URL (`fao_participant_oi_DDMMYYYY.csv`). Bulk/block deals are
NOT: verified live (2026-08-26) that `content/equities/bulk.csv` and
`content/equities/block.csv` always serve the CURRENT trading day only --
appending a `?date=` query string changes nothing (byte-identical response),
and a ~240-URL brute force of plausible date-suffixed filenames (folders
content/equities, content/fo, content/nsccl, archives/equities; names bulk /
bulk_deals / BULK_DEALS / bulkdeals; date formats DDMMYYYY / YYYYMMDD /
DDMONYYYY) found zero historical archive files on the no-auth CDN. NSE does
expose a date-ranged history via `www.nseindia.com/api/historical/bulk-deals`,
but that endpoint sits behind Akamai Bot Manager (confirmed: it issues
_abck/bm_sz challenge cookies and 503s a plain request even after a
same-session homepage warm-up) -- defeating that is bot-detection bypass,
which this collector will not do.

Consequence: `--date` does not select which day's file is fetched (there is
only one URL, always "today"). It selects which day the collector is
willing to ATTRIBUTE the fetched rows to. The collector reads each row's own
`Date` column (never trusts the URL or the requested date) and compares it
to the requested date:
  - match      -> status 'ok' (or 'empty' if zero rows survive the universe
                  filter -- see below), rows attributed to that date.
  - mismatch   -> status 'date_unavailable': the source can only ever serve
                  its current day, so a request for an earlier date is a
                  known, honest gap, not a parse failure or a 404.
This also keeps doctrine #3 (no look-ahead) intact: a mismatch is exactly
"the source hasn't published this day (to us, on this URL)", logged as
such, never silently attributed to the wrong date.

## Universe filter

Bulk/block deals are NSE-market-wide (small caps trade in bulk lots far more
often than most F&O names). Rows are filtered to Vanguard's F&O universe
(`fo_underlying_catalog.symbol`, the live app's own derivatives catalog --
same table README.md's inventory already reuses for lot sizes) before
storage. This is a deliberate scope choice for Vanguard, not a source
limitation: NSE's raw file usually contains 50-250 rows/day market-wide;
almost none land in the ~218-symbol F&O universe on any given day (confirmed
live: 2026-08-26's real file had 221 bulk + 53 block rows, 0 matched the
universe). A 0-row day post-filter is `status='ok', rows_written=0` per
doctrine -- correctly-empty, not a failure.

    python vanguard/ingest/bulk_block.py                  # today
    python vanguard/ingest/bulk_block.py --date 2026-08-25
    python vanguard/ingest/bulk_block.py --backfill-days 4  # today + 4 prior
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# One confirmed-working URL per kind today (the module docstring documents an
# exhaustive brute-force search across path/name/date-format permutations
# that found no working alternative). Kept as a list, not a bare string, so
# a future NSE path rotation is a one-line addition here rather than a
# reopened investigation -- matching fo_risk_ingest.py's URL_CANDIDATES shape.
SOURCE_URL_CANDIDATES = {
    "bulk": ["https://nsearchives.nseindia.com/content/equities/bulk.csv"],
    "block": ["https://nsearchives.nseindia.com/content/equities/block.csv"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


@dataclass
class FetchResult:
    status: str          # ok | empty | error | date_unavailable
    rows: list[dict]
    detail: str = ""
    raw_sample: str = ""  # first ~500 chars of one real response, for the report


def parse(text: str, kind: str) -> list[dict]:
    """CSV in -> [{dt, symbol, client_name, deal_type, kind, quantity, price}, ...].

    Bulk and block files share the same leading columns (Date, Symbol,
    Security Name, Client Name, Buy/Sell, Quantity Traded, Trade Price /
    Wght. Avg. Price); bulk has a trailing Remarks column block doesn't.
    Matched by header substring, not fixed position, same defensive stance
    as m1_participant_oi.
    """
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        raise ValueError("empty response body")
    header = [cell.strip() for cell in reader[0]]

    def find_column(label: str) -> int:
        matches = [i for i, cell in enumerate(header) if label.lower() in cell.lower()]
        if not matches:
            raise ValueError(f"column '{label}' not found in header: {header}")
        return matches[0]

    col_date = find_column("Date")
    col_symbol = find_column("Symbol")
    col_client = find_column("Client Name")
    col_side = find_column("Buy/Sell")
    col_qty = find_column("Quantity Traded")
    col_price = find_column("Trade Price")

    rows = []
    for row in reader[1:]:
        if not row or not row[0].strip():
            continue
        raw_date = row[col_date].strip() if col_date < len(row) else ""
        if not raw_date:
            continue
        try:
            dt = datetime.strptime(raw_date, "%d-%b-%Y").date()
        except ValueError:
            raise ValueError(f"unparseable Date value: {raw_date!r}")
        symbol = row[col_symbol].strip().upper() if col_symbol < len(row) else ""
        client_name = row[col_client].strip() if col_client < len(row) else ""
        side = row[col_side].strip().upper() if col_side < len(row) else ""
        if side not in ("BUY", "SELL"):
            raise ValueError(f"unexpected Buy/Sell value: {side!r}")
        qty_raw = row[col_qty].strip().replace(",", "") if col_qty < len(row) else ""
        price_raw = row[col_price].strip().replace(",", "") if col_price < len(row) else ""
        rows.append({
            "dt": dt,
            "symbol": symbol,
            "client_name": client_name,
            "deal_type": side,
            "kind": kind,
            "quantity": int(qty_raw) if qty_raw.lstrip("-").isdigit() else 0,
            "price": float(price_raw) if price_raw else 0.0,
        })
    return rows


def fetch_kind(kind: str, target: date, universe: set[str], client: httpx.Client) -> FetchResult:
    response = None
    last_error = ""
    for url in SOURCE_URL_CANDIDATES[kind]:
        try:
            response = client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        except httpx.HTTPError as exc:
            last_error = f"request failed: {exc}"
            response = None
            continue
        if response.status_code == 200:
            break
        last_error = f"HTTP {response.status_code} at {url}"
        response = None
    if response is None:
        return FetchResult("error", [], last_error or "no URL candidate succeeded")
    sample = response.text[:500]
    try:
        parsed = parse(response.text, kind)
    except ValueError as exc:
        return FetchResult("error", [], str(exc), raw_sample=sample)
    if not parsed:
        return FetchResult("empty", [], "parsed zero rows from a 200 response", raw_sample=sample)

    file_date = parsed[0]["dt"]
    if file_date != target:
        return FetchResult(
            "date_unavailable", [],
            f"source only serves its current trading day ({file_date.isoformat()}); "
            f"{target.isoformat()} is not fetchable from this no-auth URL",
            raw_sample=sample,
        )

    in_universe = [row for row in parsed if row["symbol"] in universe]
    status = "ok" if in_universe else "empty"
    detail = (f"{len(parsed)} raw {kind} rows, {len(in_universe)} in Vanguard's F&O universe"
              if in_universe else
              f"{len(parsed)} raw {kind} rows, 0 in Vanguard's F&O universe (correctly empty)")
    return FetchResult(status, in_universe, detail, raw_sample=sample)


def load_universe(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT symbol FROM fo_underlying_catalog")
        return {row[0].strip().upper() for row in cursor.fetchall()}


def upsert(connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    payload = [
        (r["dt"], r["symbol"], r["client_name"], r["deal_type"], r["kind"], r["quantity"], r["price"])
        for r in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO bulk_block (dt, symbol, client_name, deal_type, kind, quantity, price)
               VALUES %s
               ON CONFLICT (dt, symbol, client_name, deal_type, kind, quantity, price)
               DO UPDATE SET synced_at = now()""",
            payload,
        )
    return len(payload)


def log_run(connection, target: date, combined_status: str, rows_written: int, detail: str) -> None:
    """Same upsert-on-ok / insert-fresh-on-other pattern as m1_participant_oi,
    required by ingest_log's partial unique index on (collector, target_date)
    WHERE status = 'ok' -- a plain INSERT on a re-run of an already-'ok' day
    crashes on that index; see m1_participant_oi.log_run for the original fix.
    """
    status = "ok" if combined_status == "ok" else (
        "empty" if combined_status in ("empty", "date_unavailable") else "error")
    with connection.cursor() as cursor:
        if status == "ok":
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (collector, target_date) WHERE status = 'ok' DO UPDATE SET
                     run_at = now(), rows_written = EXCLUDED.rows_written, detail = EXCLUDED.detail""",
                ("bulk_block", target, status, rows_written, detail),
            )
        else:
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("bulk_block", target, status, rows_written, detail),
            )


def run(target: date, dsn: str) -> dict:
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        universe = load_universe(connection)
        with httpx.Client() as client:
            bulk = fetch_kind("bulk", target, universe, client)
            block = fetch_kind("block", target, universe, client)

        all_rows = bulk.rows + block.rows
        rows_written = upsert(connection, all_rows) if all_rows else 0

        # Combined status. 'error' always wins -- a partial error must not be
        # masked as success. Below that, 'ok' from EITHER feed wins over
        # 'empty'/'date_unavailable' from the other: block deals are far
        # rarer than bulk deals, so "bulk=ok, block=empty" is the everyday
        # case, and a max-by-severity rank (as this used to be) put 'empty'
        # ABOVE 'ok' -- masking a real, successful capture as a failed day
        # and starving the "5 clean sessions" acceptance-gate evidence of
        # 'ok' rows that had genuinely happened. Only when NEITHER feed
        # reached 'ok' does 'date_unavailable' (a more specific, informative
        # gap) win over a plain 'empty'.
        statuses = {bulk.status, block.status}
        if "error" in statuses:
            combined = "error"
        elif "ok" in statuses:
            combined = "ok"
        elif "date_unavailable" in statuses:
            combined = "date_unavailable"
        else:
            combined = "empty"
        detail = f"bulk: {bulk.detail} | block: {block.detail}"
        log_run(connection, target, combined, rows_written, detail)
        return {
            "target": target, "combined_status": combined, "rows_written": rows_written,
            "bulk": bulk, "block": block,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="also attempt this many prior calendar days")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--show-sample", action="store_true",
                        help="print the first ~500 chars of one raw response, for the report")
    args = parser.parse_args()

    target = args.date or datetime.now(timezone.utc).date()
    targets = [target - timedelta(days=offset) for offset in range(args.backfill_days + 1)]

    exit_code = 0
    shown_sample = False
    for day in targets:
        result = run(day, args.dsn)
        print(f"{day.isoformat()}: {result['combined_status']} "
              f"({result['rows_written']} rows written) -- {result['bulk'].detail} | {result['block'].detail}")
        if result["combined_status"] == "error":
            exit_code = 1
        if args.show_sample and not shown_sample:
            sample = result["bulk"].raw_sample or result["block"].raw_sample
            if sample:
                print(f"--- raw sample ({day.isoformat()}) ---\n{sample}\n--- end sample ---")
                shown_sample = True
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
