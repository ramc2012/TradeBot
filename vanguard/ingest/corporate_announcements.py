"""Corporate announcements + pledge (encumbrance) + insider (PIT) collector.

The handoff spec bundles "corporate announcements + insider (PIT) + pledge"
under one hourly-poll feed. `announcements` is the primary target; pledge and
insider are a genuinely different NSE feed family (own endpoints, own
payload shapes) -- both were found and verified live on 2026-08-26 and are
included as second/third tables per the spec's "if you can find and verify a
real, live, currently-working URL ... include it" instruction.

What was tried, and what actually worked (2026-08-26, all timestamps IST):

  * announcements: GET https://www.nseindia.com/api/corporate-announcements
    ?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY -- a bare
    request with browser headers (no cookies, no session) returned 200 with
    real data immediately. No anti-bot session dance was needed for this
    endpoint (unlike what the task briefing anticipated -- tried it anyway
    as a fallback path below, but the direct call already worked). Without
    from_date/to_date it silently caps at the most recent 20 rows across the
    whole market; with an explicit date range it returns the full range
    (verified: 1315 rows for a 2-day window, 3904 for a 1-week window).

  * pledge_disclosures: GET https://www.nseindia.com/api/corp-encumbrance --
    also worked bare, no session needed. This one does NOT support
    date-range filtering in practice (from_date/to_date silently produce 0
    rows) -- it is a current point-in-time snapshot of every open pledge
    disclosure NSE is carrying (2697 rows, 349 distinct symbols verified
    live). Each poll re-fetches the whole snapshot and upserts.

  * insider_trades (PIT): GET https://www.nseindia.com/api/corporates-pit
    ?index=equities&symbol=X&from_date=...&to_date=... -- works correctly
    ONLY when `symbol` is supplied. Verified live: with symbol=RELIANCE and
    an 8-month window it returned 4 real, correctly-dated disclosures.
    WITHOUT a symbol, from_date/to_date are silently ignored -- a 1-week or
    1-month window returns 0 rows, while an ~8-month window returns 4451
    rows that do not respect the requested range (confirmed by varying
    from_date alone: 01-08 -> 0 rows, 01-05 -> 3 rows, 01-02 -> 3731 rows,
    which is not consistent with any real date filter). Treat the no-symbol
    form as unreliable/broken and never use it. The collector instead loops
    Vanguard's own universe (~210 equities from `sector_taxonomy`) one
    symbol at a time -- exactly the "scope to Vanguard's own universe"
    instruction, and it also happens to be the only form of this endpoint
    that behaves correctly.

  * A session-cookie warm-up (GET https://www.nseindia.com/ first) was
    tried per the task briefing's anticipated anti-bot pattern: the home
    page itself 403s even with full browser headers, so no session cookies
    were ever obtained -- yet all three API endpoints above still returned
    200 with real data on a bare request. NSE's API-level anti-bot gate
    appears to be header-based (a real desktop-Chrome User-Agent + Referer),
    not cookie-based, for these particular endpoints. No cookie jar is used
    below because none was obtainable and none was needed.

results_calendar (already created by 001_schema.sql, previously unpopulated)
is fed from a FOURTH endpoint discovered and verified during this build,
not from `announcements` text-matching as the task briefing suggested.
Reason: `announcements`' "Financial Results" rows are the OUTCOME
notification (the results already happened -- an_dt IS the results date,
already in the past by definition). M7's event guard needs FORWARD-looking
results dates ("no fresh tickets 1 session before a results date"), which
only exists in NSE's corporate-board-meetings feed: GET
https://www.nseindia.com/api/corporate-board-meetings?index=equities&
from_date=...&to_date=... returns each company's scheduled board-meeting
date and a `bm_purpose` field; verified live, `bm_purpose` contains
"Financial Results" (alone or combined, e.g. "Financial Results/Dividend")
for genuinely upcoming dates (e.g. TECHNOCRAF 02-Sep-2026, LEAPIND
31-Aug-2026, both after this run's 26-Aug-2026 date). Using the
already-happened `announcements` outcome instead would only ever populate
results_calendar with dates already in the past -- useless for a forward
guard -- so this collector reuses the verified board-meetings endpoint
instead and documents the substitution here rather than silently doing the
less useful thing the briefing described.

Doctrine #3 (no look-ahead): every date stored here is a date NSE itself has
already published (an announcement already made, or a board meeting NSE's
own archive already lists as scheduled) -- nothing here predicts or infers a
date NSE has not itself disclosed.

    python vanguard/ingest/corporate_announcements.py                    # today's poll, full universe
    python vanguard/ingest/corporate_announcements.py --skip-insider     # announcements + pledge only (fast)
    python vanguard/ingest/corporate_announcements.py --from-date 2026-08-19 --to-date 2026-08-26
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
ENCUMBRANCE_URL = "https://www.nseindia.com/api/corp-encumbrance"
PIT_URL = "https://www.nseindia.com/api/corporates-pit"
BOARD_MEETINGS_URL = "https://www.nseindia.com/api/corporate-board-meetings"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}

# Coarse category bucket derived from NSE's own `desc`/subject text, purely
# for convenient querying (e.g. category='results'). Order matters -- first
# match wins.
CATEGORY_RULES = [
    ("results", ("financial results",)),
    ("board_meeting", ("board meeting",)),
    ("dividend", ("dividend",)),
    ("buyback", ("buyback",)),
    ("credit_rating", ("credit rating",)),
    ("corporate_action", ("acquisition", "merger", "amalgamation", "allotment of securities",
                          "scheme of arrangement", "stock split", "bonus")),
]


def classify_category(subject: str) -> str:
    lowered = subject.lower()
    for category, needles in CATEGORY_RULES:
        if any(needle in lowered for needle in needles):
            return category
    return "general"


@dataclass
class FetchResult:
    status: str          # ok | empty | error
    rows: list[dict] = field(default_factory=list)
    detail: str = ""


def _get_json(client: httpx.Client, url: str, params: dict) -> tuple[Optional[object], str]:
    try:
        response = client.get(url, params=params, headers=HEADERS, timeout=20, follow_redirects=True)
    except httpx.HTTPError as exc:
        return None, f"request failed: {exc}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code} at {response.url}"
    try:
        return response.json(), ""
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"


# ---------------------------------------------------------------------------
# announcements
# ---------------------------------------------------------------------------

def fetch_announcements(from_dt: date, to_dt: date, client: httpx.Client,
                         universe: Optional[set[str]] = None) -> FetchResult:
    params = {
        "index": "equities",
        "from_date": from_dt.strftime("%d-%m-%Y"),
        "to_date": to_dt.strftime("%d-%m-%Y"),
    }
    payload, error = _get_json(client, ANNOUNCEMENTS_URL, params)
    if error:
        return FetchResult("error", [], error)
    if not isinstance(payload, list):
        return FetchResult("error", [], f"unexpected payload shape: {type(payload).__name__}")
    rows = parse_announcements(payload, universe)
    if not rows:
        return FetchResult("empty", [], f"0 rows in universe out of {len(payload)} fetched")
    return FetchResult("ok", rows, f"{len(payload)} fetched, {len(rows)} in universe")


def parse_announcements(payload: list[dict], universe: Optional[set[str]] = None) -> list[dict]:
    rows = []
    for item in payload:
        symbol = (item.get("symbol") or "").strip()
        if not symbol:
            continue
        if universe is not None and symbol not in universe:
            continue
        an_dt = item.get("an_dt") or item.get("exchdisstime")
        if not an_dt:
            continue
        try:
            dt = datetime.strptime(an_dt.strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            continue
        subject = (item.get("desc") or "").strip()
        if not subject:
            continue
        rows.append({
            "symbol": symbol,
            "dt": dt,
            "subject": subject,
            "description": (item.get("attchmntText") or "").strip(),
            "attachment_url": (item.get("attchmntFile") or "").strip(),
            "category": classify_category(subject),
        })
    return rows


def _dedupe_last_wins(rows: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    """NSE's feeds sometimes carry more than one row for the same natural
    key within a single poll window (a correction/re-broadcast, or the same
    disclosure appearing in more than one day's page of a multi-day range).
    `ON CONFLICT ... DO UPDATE` errors (CardinalityViolation) if the same
    key appears twice in one execute_values batch, so dedupe client-side
    first -- last occurrence wins, matching "the most recent version of this
    row is what should end up in the table"."""
    deduped: dict[tuple, dict] = {}
    for row in rows:
        deduped[tuple(row[f] for f in key_fields)] = row
    return list(deduped.values())


def upsert_announcements(connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    rows = _dedupe_last_wins(rows, ("symbol", "dt", "subject"))
    payload = [
        (r["symbol"], r["dt"], r["subject"], r["description"], r["attachment_url"], r["category"])
        for r in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO announcements (symbol, dt, subject, description, attachment_url, category)
               VALUES %s
               ON CONFLICT (symbol, dt, subject) DO UPDATE SET
                 description = EXCLUDED.description,
                 attachment_url = EXCLUDED.attachment_url,
                 category = EXCLUDED.category,
                 synced_at = now()""",
            payload,
        )
    return len(payload)


# ---------------------------------------------------------------------------
# pledge_disclosures (corp-encumbrance)
# ---------------------------------------------------------------------------

def fetch_pledges(client: httpx.Client, universe: Optional[set[str]] = None) -> FetchResult:
    payload, error = _get_json(client, ENCUMBRANCE_URL, {})
    if error:
        return FetchResult("error", [], error)
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        return FetchResult("error", [], f"unexpected payload shape: {payload!r}"[:200])
    rows = parse_pledges(data, universe)
    if not rows:
        return FetchResult("empty", [], f"0 rows in universe out of {len(data)} fetched")
    return FetchResult("ok", rows, f"{len(data)} fetched, {len(rows)} in universe")


def _yes_no_to_bool(value) -> Optional[bool]:
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered == "yes":
        return True
    if lowered == "no":
        return False
    return None


def parse_pledges(data: list[dict], universe: Optional[set[str]] = None) -> list[dict]:
    rows = []
    for item in data:
        symbol = (item.get("symbol") or "").strip()
        promoter = (item.get("promoterName") or "").strip()
        if not symbol or not promoter:
            continue
        if universe is not None and symbol not in universe:
            continue
        broadcast_raw = (item.get("broadcastDate") or "").strip()
        broadcast_dt = None
        if broadcast_raw and broadcast_raw != "-":
            try:
                broadcast_dt = datetime.strptime(broadcast_raw, "%d-%b-%Y %H:%M:%S")
            except ValueError:
                broadcast_dt = None
        rows.append({
            "symbol": symbol,
            "company_name": (item.get("companyName") or "").strip(),
            "promoter_name": promoter,
            "encumbered_gt_20pct": _yes_no_to_bool(item.get("shareMoreThan20Per")),
            "encumbered_gt_50pct": _yes_no_to_bool(item.get("shareMoreThan50Per")),
            "broadcast_dt": broadcast_dt,
            "attachment_url": (item.get("attachment") or "").strip(),
        })
    return rows


def upsert_pledges(connection, rows: list[dict], universe: Optional[set[str]] = None) -> int:
    """Upsert the current snapshot, then RECONCILE: remove any (symbol,
    promoter_name) still in the table for a universe symbol but absent from
    this fetch -- a released/withdrawn pledge. NSE's endpoint returns the
    full CURRENT state each call (verified live: the in-universe row count
    genuinely moves between polls minutes apart, e.g. 338 then 325), so
    "not present now" means "no longer encumbered", not "NSE forgot to
    report it". Reconciliation only runs when `rows` is non-empty -- an
    empty/failed fetch must never be read as "everything was released"; see
    the caller, which only invokes this on FetchResult.status == "ok".
    """
    if not rows:
        return 0
    rows = _dedupe_last_wins(rows, ("symbol", "promoter_name"))
    payload = [
        (r["symbol"], r["company_name"], r["promoter_name"], r["encumbered_gt_20pct"],
         r["encumbered_gt_50pct"], r["broadcast_dt"], r["attachment_url"])
        for r in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO pledge_disclosures
               (symbol, company_name, promoter_name, encumbered_gt_20pct,
                encumbered_gt_50pct, broadcast_dt, attachment_url)
               VALUES %s
               ON CONFLICT (symbol, promoter_name) DO UPDATE SET
                 company_name = EXCLUDED.company_name,
                 encumbered_gt_20pct = EXCLUDED.encumbered_gt_20pct,
                 encumbered_gt_50pct = EXCLUDED.encumbered_gt_50pct,
                 broadcast_dt = EXCLUDED.broadcast_dt,
                 attachment_url = EXCLUDED.attachment_url,
                 synced_at = now()""",
            payload,
        )
        fetched_keys = {(r["symbol"], r["promoter_name"]) for r in rows}
        scoped_universe = universe if universe is not None else {r["symbol"] for r in rows}
        cursor.execute(
            "SELECT symbol, promoter_name FROM pledge_disclosures WHERE symbol = ANY(%s)",
            (sorted(scoped_universe),),
        )
        stale = [(symbol, promoter) for symbol, promoter in cursor.fetchall()
                 if (symbol, promoter) not in fetched_keys]
        if stale:
            psycopg2.extras.execute_values(
                cursor,
                "DELETE FROM pledge_disclosures WHERE (symbol, promoter_name) IN (VALUES %s)",
                stale,
            )
    return len(payload)


# ---------------------------------------------------------------------------
# insider_trades (corporates-pit) -- must be queried per-symbol, see module
# docstring for why the bulk/no-symbol form is unreliable.
# ---------------------------------------------------------------------------

def fetch_insider_trades_for_symbol(symbol: str, from_dt: date, to_dt: date,
                                     client: httpx.Client) -> FetchResult:
    params = {
        "index": "equities",
        "symbol": symbol,
        "from_date": from_dt.strftime("%d-%m-%Y"),
        "to_date": to_dt.strftime("%d-%m-%Y"),
    }
    payload, error = _get_json(client, PIT_URL, params)
    if error:
        return FetchResult("error", [], error)
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        return FetchResult("error", [], f"unexpected payload shape: {payload!r}"[:200])
    rows = parse_insider_trades(data)
    if not rows:
        return FetchResult("empty", [])
    return FetchResult("ok", rows)


def _to_int(value) -> Optional[int]:
    if value in (None, "", "-"):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


def _to_float(value) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _to_date(value) -> Optional[date]:
    if not value or value == "-":
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_insider_trades(data: list[dict]) -> list[dict]:
    rows = []
    for item in data:
        did = (item.get("did") or "").strip()
        symbol = (item.get("symbol") or "").strip()
        if not did or not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "acquirer_name": (item.get("acqName") or "").strip(),
            "person_category": (item.get("personCategory") or "").strip(),
            "transaction_type": (item.get("tdpTransactionType") or "").strip(),
            "security_type": (item.get("secType") or "").strip(),
            "mode": (item.get("acqMode") or "").strip(),
            "acq_from_dt": _to_date(item.get("acqfromDt")),
            "acq_to_dt": _to_date(item.get("acqtoDt")),
            "intimation_dt": _to_date(item.get("intimDt")),
            "securities_acquired": _to_int(item.get("secAcq")),
            "value_acquired": _to_float(item.get("secVal")),
            "shares_before_pct": _to_float(item.get("befAcqSharesPer")),
            "shares_after_pct": _to_float(item.get("afterAcqSharesPer")),
            "nse_disclosure_id": did,
        })
    return rows


def upsert_insider_trades(connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    rows = _dedupe_last_wins(rows, ("nse_disclosure_id",))
    payload = [
        (r["symbol"], r["acquirer_name"], r["person_category"], r["transaction_type"],
         r["security_type"], r["mode"], r["acq_from_dt"], r["acq_to_dt"], r["intimation_dt"],
         r["securities_acquired"], r["value_acquired"], r["shares_before_pct"],
         r["shares_after_pct"], r["nse_disclosure_id"])
        for r in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO insider_trades
               (symbol, acquirer_name, person_category, transaction_type, security_type,
                mode, acq_from_dt, acq_to_dt, intimation_dt, securities_acquired,
                value_acquired, shares_before_pct, shares_after_pct, nse_disclosure_id)
               VALUES %s
               ON CONFLICT (nse_disclosure_id) DO UPDATE SET
                 shares_before_pct = EXCLUDED.shares_before_pct,
                 shares_after_pct = EXCLUDED.shares_after_pct,
                 synced_at = now()""",
            payload,
        )
    return len(payload)


# ---------------------------------------------------------------------------
# results_calendar (fed from corporate-board-meetings, see module docstring)
# ---------------------------------------------------------------------------

def fetch_board_meeting_results(from_dt: date, to_dt: date, client: httpx.Client,
                                 universe: Optional[set[str]] = None) -> FetchResult:
    params = {
        "index": "equities",
        "from_date": from_dt.strftime("%d-%m-%Y"),
        "to_date": to_dt.strftime("%d-%m-%Y"),
    }
    payload, error = _get_json(client, BOARD_MEETINGS_URL, params)
    if error:
        return FetchResult("error", [], error)
    if not isinstance(payload, list):
        return FetchResult("error", [], f"unexpected payload shape: {type(payload).__name__}")
    rows = parse_board_meeting_results(payload, universe)
    if not rows:
        return FetchResult("empty", [], f"0 results-purpose rows in universe out of {len(payload)} fetched")
    return FetchResult("ok", rows, f"{len(payload)} fetched, {len(rows)} results-purpose rows in universe")


def parse_board_meeting_results(payload: list[dict], universe: Optional[set[str]] = None) -> list[dict]:
    rows = []
    for item in payload:
        purpose = (item.get("bm_purpose") or "")
        if "financial results" not in purpose.lower():
            continue
        symbol = (item.get("bm_symbol") or "").strip()
        if not symbol:
            continue
        if universe is not None and symbol not in universe:
            continue
        results_date = _to_date(item.get("bm_date"))
        if not results_date:
            continue
        rows.append({"symbol": symbol, "results_date": results_date})
    return rows


def upsert_results_calendar(connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    rows = _dedupe_last_wins(rows, ("symbol", "results_date"))
    payload = [(r["symbol"], r["results_date"]) for r in rows]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO results_calendar (symbol, results_date, source)
               VALUES %s
               ON CONFLICT (symbol, results_date) DO UPDATE SET synced_at = now()""",
            [(symbol, results_date, "nse_corporate_board_meetings_api")
             for symbol, results_date in payload],
        )
    return len(payload)


# ---------------------------------------------------------------------------
# logging + universe + orchestration
# ---------------------------------------------------------------------------

def load_universe(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT symbol FROM sector_taxonomy")
        return {row[0] for row in cursor.fetchall()}


def load_equity_universe(connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT symbol FROM sector_taxonomy WHERE instrument_type = 'Equity' ORDER BY symbol")
        return [row[0] for row in cursor.fetchall()]


def log_run(connection, collector: str, target: date, result: FetchResult, rows_written: int) -> None:
    """Same partial-unique-index-safe pattern as m1_participant_oi.log_run:
    upsert the one allowed status='ok' row per (collector, target_date);
    insert a fresh row for empty/error so retry history is preserved."""
    status = "ok" if result.status == "ok" else ("empty" if result.status == "empty" else "error")
    with connection.cursor() as cursor:
        if status == "ok":
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (collector, target_date) WHERE status = 'ok' DO UPDATE SET
                     run_at = now(), rows_written = EXCLUDED.rows_written, detail = EXCLUDED.detail""",
                (collector, target, status, rows_written, result.detail),
            )
        else:
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                (collector, target, status, rows_written, result.detail),
            )


def run(from_dt: date, to_dt: date, dsn: str, skip_insider: bool = False,
        insider_lookback_days: int = 30, request_delay: float = 0.15) -> dict:
    """Runs all feeds for the [from_dt, to_dt] window and logs each to
    ingest_log under its own collector name, tagged to `to_dt` (the poll
    date -- these are polled feeds, not single-EOD-archive feeds, so
    target_date records when the poll ran rather than one specific trading
    day). Returns a summary dict for the caller to print/report.
    """
    connection = psycopg2.connect(dsn)
    summary: dict = {}
    try:
        connection.autocommit = True
        universe = load_universe(connection)
        with httpx.Client() as client:
            ann_result = fetch_announcements(from_dt, to_dt, client, universe)
            ann_written = upsert_announcements(connection, ann_result.rows) if ann_result.status == "ok" else 0
            log_run(connection, "corporate_announcements", to_dt, ann_result, ann_written)
            summary["announcements"] = (ann_result.status, ann_written, ann_result.detail)

            pledge_result = fetch_pledges(client, universe)
            pledge_written = (upsert_pledges(connection, pledge_result.rows, universe)
                              if pledge_result.status == "ok" else 0)
            log_run(connection, "pledge_disclosures", to_dt, pledge_result, pledge_written)
            summary["pledge_disclosures"] = (pledge_result.status, pledge_written, pledge_result.detail)

            board_result = fetch_board_meeting_results(from_dt, to_dt, client, universe)
            board_written = upsert_results_calendar(connection, board_result.rows) if board_result.status == "ok" else 0
            log_run(connection, "results_calendar_board_meetings", to_dt, board_result, board_written)
            summary["results_calendar"] = (board_result.status, board_written, board_result.detail)

            if skip_insider:
                summary["insider_trades"] = ("skipped", 0, "--skip-insider")
            else:
                equities = load_equity_universe(connection)
                insider_from = to_dt - timedelta(days=insider_lookback_days)
                total_written = 0
                errors = []
                symbols_with_data = 0
                for symbol in equities:
                    result = fetch_insider_trades_for_symbol(symbol, insider_from, to_dt, client)
                    if result.status == "ok":
                        total_written += upsert_insider_trades(connection, result.rows)
                        symbols_with_data += 1
                    elif result.status == "error":
                        errors.append(f"{symbol}: {result.detail}")
                    time.sleep(request_delay)
                detail = f"{symbols_with_data}/{len(equities)} symbols had disclosures"
                if errors:
                    detail += f"; {len(errors)} errors (first: {errors[0]})"
                insider_summary_result = FetchResult(
                    "ok" if total_written or not errors else "error", [], detail)
                log_run(connection, "insider_trades", to_dt, insider_summary_result, total_written)
                summary["insider_trades"] = (insider_summary_result.status, total_written, detail)
        return summary
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", type=date.fromisoformat, default=None,
                        help="default: 7 days before --to-date")
    parser.add_argument("--to-date", type=date.fromisoformat, default=None, help="default: today")
    parser.add_argument("--skip-insider", action="store_true",
                        help="skip the per-symbol PIT loop (announcements + pledge + results only)")
    parser.add_argument("--insider-lookback-days", type=int, default=30)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    to_dt = args.to_date or datetime.now(timezone.utc).date()
    from_dt = args.from_date or (to_dt - timedelta(days=7))

    summary = run(from_dt, to_dt, args.dsn, skip_insider=args.skip_insider,
                  insider_lookback_days=args.insider_lookback_days)

    exit_code = 0
    for feed, (status, written, detail) in summary.items():
        print(f"{feed}: {status} ({written} rows)" + (f" -- {detail}" if detail else ""))
        if status == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
