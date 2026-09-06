"""Bhavcopy + delivery-percentage collector.

Pulls NSE's daily "security-wise delivery position" archive (file name
`sec_bhavdata_full_DDMMYYYY.csv`) and upserts into `bhavcopy_delivery`.
This single NSE file already carries OHLC, previous close, traded volume,
turnover value AND delivery quantity/percentage in one row per security --
there is no separate "UDiFF bhavcopy" fetch needed on top of it (checked:
the UDiFF combined-CM file `BhavCopy_NSE_CM_..._F_0000.csv.zip` carries
OHLC/volume but not delivery data; `sec_bhavdata_full` is the one archive
that already has both, so it is the only one this collector fetches).

No existing table in the live schema carries this (checked: grepped the
repo for bhavcopy/delivery_pct/deliverable/udiff, and looked for a table by
those names in the live `nomadcurie` Postgres -- neither turned up; see
db/migrations/001_schema.sql's inventory comment for the tables that DID
already exist, none of which are this one).

URL and header pattern follow backend/market_data/fo_risk_ingest.py, the
live app's own NSE-archive fetcher (browser UA/Referer to avoid a 403, and
a list of URL candidates tried in order because NSE has moved archive paths
before). All three candidates below were verified live 2026-08-26 to
return the identical byte-for-byte file, so the fallbacks are host-level
(nsearchives / archives.nseindia.com / www.nseindia.com), not path-level.

Doctrine #3 (no look-ahead) needed one extra check this collector's sibling
(m1_participant_oi) did not: on a trading day the requested date's CSV is
returned with HTTP 200 and its own `DATE1` column matching the request, and
on a Saturday the archive 404s cleanly (same as participant_oi). But on a
SUNDAY it does NOT 404 -- it returns HTTP 200 with the *previous* trading
day's data silently re-served under that day's URL (verified live: request
for 2026-08-23, a Sunday, returned 200 with every row's `DATE1` = 21-Aug-2026,
not 23-Aug-2026; same for 2026-08-16 returning 14-Aug-2026 data). A collector
that trusted the 200 alone would silently store Friday's closes under a
Sunday `dt`. This collector reads `DATE1` out of the parsed body and treats
a mismatch against the requested date as `stale` -- logged and rejected,
never upserted -- rather than trusting the HTTP status code the way
participant_oi safely could.

Doctrine #5 (everything measurable): every run, including a clean 404 or a
stale-content rejection, writes one row to `ingest_log` via the same
upsert-on-ok / insert-fresh-on-not-ok pattern m1_participant_oi.py uses (a
plain INSERT would crash on re-running an already-'ok' day, because of
ingest_log's partial unique index on status='ok').

    python vanguard/ingest/bhavcopy_delivery.py                  # today
    python vanguard/ingest/bhavcopy_delivery.py --date 2026-08-25
    python vanguard/ingest/bhavcopy_delivery.py --backfill-days 5
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

# All three verified live 2026-08-26 to serve the identical file for the
# same date -- host-level fallbacks, not different archive generations.
URL_CANDIDATES = [
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
    "https://www.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# header label (substring, matched case/whitespace/underscore-insensitively)
# -> output field. Matched by substring rather than fixed position: NSE has
# reshuffled sibling archive files' columns before (see m1_participant_oi.py
# and fo_risk_ingest.py), and CLOSE_PRICE / PREV_CLOSE only stay unambiguous
# under substring matching because each needle below is the FULL field name,
# not a fragment ("CLOSE_PRICE" is not a substring of "PREV_CLOSE").
COLUMN_NEEDLES = [
    ("SYMBOL", "symbol"),
    ("SERIES", "series"),
    ("DATE", "date_str"),
    ("PREV_CLOSE", "prev_close"),
    ("OPEN_PRICE", "open"),
    ("HIGH_PRICE", "high"),
    ("LOW_PRICE", "low"),
    ("CLOSE_PRICE", "close"),
    ("TTL_TRD_QNTY", "volume"),
    ("TURNOVER_LACS", "turnover_lacs"),
    ("DELIV_QTY", "deliverable_qty"),
    ("DELIV_PER", "delivery_pct"),
]


@dataclass
class FetchResult:
    status: str          # ok | empty | error | not_a_trading_day | stale
    rows: list[dict]
    detail: str = ""


def _normalise(cell: str) -> str:
    return cell.strip().upper().replace(" ", "").replace("_", "")


def _find_column(header: list[str], needle: str) -> int | None:
    target = _normalise(needle)
    for i, cell in enumerate(header):
        if target in _normalise(cell):
            return i
    return None


def _num(raw: str) -> float | None:
    raw = raw.strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(raw: str) -> int | None:
    value = _num(raw)
    return int(value) if value is not None else None


def parse(text: str, universe: set[str]) -> list[dict]:
    """CSV in -> one dict per universe symbol found, with a `date` field
    (parsed from the file's own DATE1 column) for the caller to validate
    against the requested date before trusting anything else in the row.

    `universe` scopes output to Vanguard's own ~210-symbol F&O equity
    universe rather than the full multi-thousand-name NSE listing -- the
    file itself covers every listed security, series (EQ/BE/GS/SM/...)
    included; this collector only wants the F&O equity universe's EQ rows.
    """
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        raise ValueError("empty CSV body")
    header = [cell.strip() for cell in reader[0]]
    column_for: dict[str, int] = {}
    for needle, field in COLUMN_NEEDLES:
        index = _find_column(header, needle)
        if index is None:
            raise ValueError(f"column '{needle}' not found in header: {header}")
        column_for[field] = index

    series_index = _find_column(header, "SERIES")

    rows: list[dict] = []
    for row in reader[1:]:
        if not row or not row[0].strip():
            continue
        symbol = row[column_for["symbol"]].strip()
        if symbol not in universe:
            continue
        series = row[series_index].strip() if series_index is not None else ""
        if series and series != "EQ":
            continue  # universe is Equity-only; skip a symbol's non-EQ series row
        turnover_lacs = _num(row[column_for["turnover_lacs"]])
        rows.append({
            "symbol": symbol,
            "date_str": row[column_for["date_str"]].strip(),
            "open": _num(row[column_for["open"]]),
            "high": _num(row[column_for["high"]]),
            "low": _num(row[column_for["low"]]),
            "close": _num(row[column_for["close"]]),
            "prev_close": _num(row[column_for["prev_close"]]),
            "volume": _int(row[column_for["volume"]]),
            # TURNOVER_LACS is reported in lakhs of rupees; converted here to
            # rupees to match the plain "value" field the spec asks for --
            # a unit conversion of an as-reported figure, not a derived
            # feature, so Doctrine #1's normalization rule doesn't apply.
            "value": turnover_lacs * 100_000 if turnover_lacs is not None else None,
            "deliverable_qty": _int(row[column_for["deliverable_qty"]]),
            "delivery_pct": _num(row[column_for["delivery_pct"]]),
        })
    return rows


def fetch(target: date, universe: set[str], client: httpx.Client) -> FetchResult:
    ddmmyyyy = target.strftime("%d%m%Y")
    last_error = ""
    for template in URL_CANDIDATES:
        url = template.format(ddmmyyyy=ddmmyyyy)
        try:
            response = client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        except httpx.HTTPError as exc:
            last_error = f"request failed at {url}: {exc}"
            continue
        if response.status_code == 404:
            return FetchResult("not_a_trading_day", [], f"404 at {url}")
        if response.status_code != 200:
            last_error = f"HTTP {response.status_code} at {url}"
            continue

        try:
            rows = parse(response.text, universe)
        except ValueError as exc:
            return FetchResult("error", [], str(exc))
        if not rows:
            return FetchResult("empty", [], f"parsed zero universe rows from {url}")

        # NSE serves a stale (previous trading day's) file with HTTP 200 on
        # at least Sundays -- verified live, see module docstring. Reject
        # rather than upsert under the wrong dt.
        try:
            row_date = datetime.strptime(rows[0]["date_str"], "%d-%b-%Y").date()
        except ValueError:
            return FetchResult("error", [], f"unparseable DATE1 value: {rows[0]['date_str']!r}")
        if row_date != target:
            return FetchResult(
                "stale", [],
                f"requested {target.isoformat()} but archive returned {row_date.isoformat()}'s "
                f"data (200 OK, stale content) at {url}",
            )
        return FetchResult("ok", rows)
    return FetchResult("error", [], last_error or "no URL candidate returned a usable response")


def load_universe(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT symbol FROM sector_taxonomy WHERE instrument_type = 'Equity'")
        return {row[0] for row in cursor.fetchall()}


def upsert(connection, target: date, rows: list[dict]) -> int:
    payload = [
        (target, row["symbol"], row["open"], row["high"], row["low"], row["close"],
         row["prev_close"], row["volume"], row["value"], row["deliverable_qty"],
         row["delivery_pct"])
        for row in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO bhavcopy_delivery
                 (dt, symbol, open, high, low, close, prev_close, volume, value,
                  deliverable_qty, delivery_pct)
               VALUES %s
               ON CONFLICT (dt, symbol) DO UPDATE SET
                 open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                 close = EXCLUDED.close, prev_close = EXCLUDED.prev_close,
                 volume = EXCLUDED.volume, value = EXCLUDED.value,
                 deliverable_qty = EXCLUDED.deliverable_qty,
                 delivery_pct = EXCLUDED.delivery_pct, synced_at = now()""",
            payload,
        )
    return len(payload)


def log_run(connection, target: date, result: FetchResult, rows_written: int) -> None:
    """Same upsert-on-ok / insert-fresh-on-not-ok pattern as
    m1_participant_oi.py's log_run -- ingest_log's partial unique index only
    covers status='ok', so a re-run of an already-ok day must UPDATE that
    row (or it crashes on the very idempotency it exists to support), while
    a re-run of a failed/empty/stale day inserts a fresh row to preserve
    retry history for the "5 clean sessions" acceptance gate.
    """
    status = "ok" if result.status == "ok" else (
        "empty" if result.status in ("empty", "not_a_trading_day", "stale") else "error")
    with connection.cursor() as cursor:
        if status == "ok":
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (collector, target_date) WHERE status = 'ok' DO UPDATE SET
                     run_at = now(), rows_written = EXCLUDED.rows_written, detail = EXCLUDED.detail""",
                ("bhavcopy_delivery", target, status, rows_written, result.detail),
            )
        else:
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                ("bhavcopy_delivery", target, status, rows_written, result.detail),
            )


def run(target: date, dsn: str) -> FetchResult:
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        universe = load_universe(connection)
        with httpx.Client() as client:
            result = fetch(target, universe, client)
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
              + (f" ({len(result.rows)} universe rows)" if result.rows else ""))
        if result.status == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
