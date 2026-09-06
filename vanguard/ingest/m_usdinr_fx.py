"""M-cross-asset collector: USDINR daily close, via Fyers currency futures.

Genuinely absent from the live schema -- confirmed by grep across backend/
for USDINR/10Y/g_sec/bond_yield (all false-positive substring hits on
"lag"/"seconds") and by querying `underlying_spot_candles` /
`index_futures_candles` for anything INR/USD-shaped: nothing. See
db/migrations/001_schema.sql's inventory comment for the five feeds that
*did* already exist (bars, option-chain snapshots, PCR, universe, ban list)
and are read directly rather than duplicated -- USDINR is not one of them.

NSE does not publish a USDINR spot archive; USD/INR is quoted here via the
front-month NSE Currency Derivatives future (`NSE:USDINR<expiry>FUT`),
fetched through the SAME already-authenticated Fyers session this whole
Vanguard build uses -- MACD mini's daily access token
(`~/CLAUDE PROJECTS/MACD mini/runtime/credentials.json`, read-only; see
README.md's "Data source" decision). The history-endpoint call (path,
params, `Authorization: {client_id}:{access_token}` header) mirrors
`MACD mini/src/macd_trader/brokers.py`'s `FyersBroker.history_range()`
exactly.

Two defensive patterns copied from `backend/market_data/fo_risk_ingest.py`
(the live app's own NSE-archive fetcher):
  1. Browser User-Agent headers -- NSE-family hosts 403 a bare requests/httpx
     UA on some endpoints; applied here too even though public.fyers.in did
     not require it in live testing, on the same "don't rely on today's
     leniency" reasoning fo_risk_ingest.py documents for MWPL_URL_CANDIDATES.
  2. A URL-candidates list rather than one hardcoded path for the symbol
     master, structured so a second host can be added later without a
     rewrite -- currently only one confirmed-working URL is known, so the
     list has one entry rather than fabricated untested alternates.

Front-month contract selection is dynamic (parse the NSE_CD symbol master,
pick the USDINR *FUT row with the nearest not-yet-expired epoch), not a
hardcoded "USDINR26AUGFUT" string that would silently go stale next month
-- unlike `backend/market_data/commodity_runtime_history.py`'s static
per-month MCX mapping, which this deliberately does NOT copy for that
reason. `cont_flag=1` on the history call asks Fyers to back-adjust across
the contract's own rollover, the same continuous-series behaviour already
verified live for the MCX commodity feeds (`fyers_mcx_cont` in
`underlying_spot_candles`).

No look-ahead (doctrine #3): Fyers' own `D`-resolution candle for the
current session was verified NOT present when this collector was run at
22:19 IST on 2026-08-26 (evening currency session closes 19:15 IST) -- the
last row returned was still 2026-08-25. The collector trusts whatever Fyers
returns rather than assuming "today" is available, and logs every run
(including a same-day re-run) with the wall-clock fetch time via
`ingest_log.run_at`, honouring the "log it as of when YOU fetched it" rule.

    python vanguard/ingest/m_usdinr_fx.py                     # last 365 days
    python vanguard/ingest/m_usdinr_fx.py --lookback-days 90
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
CREDENTIALS_PATH = os.path.expanduser(
    os.environ.get("VANGUARD_FYERS_CREDENTIALS", "~/CLAUDE PROJECTS/MACD mini/runtime/credentials.json")
)

MASTER_URL_CANDIDATES = [
    "https://public.fyers.in/sym_details/NSE_CD.csv",
]
HISTORY_URL = "https://api-t1.fyers.in/data/history"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.fyers.in/",
}

COLLECTOR = "m_usdinr_fx"


@dataclass
class FetchResult:
    status: str          # ok | empty | error
    rows: list[dict] = field(default_factory=list)
    detail: str = ""
    symbol: str = ""


def load_fyers_credentials(path: str = CREDENTIALS_PATH) -> tuple[str, str]:
    import json
    with open(path) as handle:
        creds = json.load(handle)
    client_id = creds.get("fyers_client_id", "")
    token = creds.get("fyers_access_token", "")
    if not client_id or not token:
        raise RuntimeError(f"{path} is missing fyers_client_id/fyers_access_token")
    return client_id, token


def pick_front_month_symbol(client: httpx.Client, as_of: float | None = None) -> str:
    """Nearest not-yet-expired NSE:USDINR<expiry>FUT row in the symbol master.

    Dynamic on purpose -- USDINR currently has both weekly and monthly
    expiries live on NSE_CD (verified 2026-08-26), so a hardcoded
    "this month's" string is not even well-defined, let alone durable.
    """
    as_of = as_of if as_of is not None else time.time()
    last_error: Exception | None = None
    for url in MASTER_URL_CANDIDATES:
        try:
            response = client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        candidates: list[tuple[float, str]] = []
        for row in csv.reader(io.StringIO(response.text)):
            if len(row) < 14:
                continue
            root = row[13].strip()
            description = row[1]
            if root != "USDINR" or "FUT" not in description:
                continue
            try:
                expiry = float(row[8])
            except ValueError:
                continue
            if expiry >= as_of:
                candidates.append((expiry, row[9]))
        if not candidates:
            last_error = RuntimeError(f"master at {url} had no live USDINR *FUT row")
            continue
        candidates.sort()
        return candidates[0][1]
    raise RuntimeError(f"could not resolve a USDINR front-month symbol: {last_error}")


def fetch(client: httpx.Client, auth: str, symbol: str, start: date, end: date) -> FetchResult:
    params = {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": start.isoformat(),
        "range_to": end.isoformat(),
        "cont_flag": "1",
    }
    try:
        response = client.get(HISTORY_URL, params=params,
                              headers={"Authorization": auth}, timeout=30)
    except httpx.HTTPError as exc:
        return FetchResult("error", [], f"request failed: {exc}", symbol)
    if response.status_code != 200:
        return FetchResult("error", [], f"HTTP {response.status_code}: {response.text[:200]}", symbol)
    payload = response.json()
    if payload.get("s") == "error":
        return FetchResult("error", [], payload.get("message", "Fyers history failed"), symbol)
    candles = payload.get("candles", [])
    if not candles:
        return FetchResult("empty", [], "zero candles returned", symbol)
    rows = [
        {
            "dt": datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).date(),
            "open": float(candle[1]), "high": float(candle[2]),
            "low": float(candle[3]), "close": float(candle[4]),
            "volume": int(candle[5]),
        }
        for candle in candles
    ]
    return FetchResult("ok", rows, symbol=symbol)


def upsert(connection, symbol: str, rows: list[dict]) -> int:
    payload = [
        (row["dt"], row["open"], row["high"], row["low"], row["close"], row["volume"],
         f"fyers_nse_cd_cont:{symbol}")
        for row in rows
    ]
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """INSERT INTO usdinr_daily (dt, open, high, low, close, volume, source)
               VALUES %s
               ON CONFLICT (dt) DO UPDATE SET
                 open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                 close = EXCLUDED.close, volume = EXCLUDED.volume,
                 source = EXCLUDED.source, synced_at = now()""",
            payload,
        )
    return len(payload)


def log_run(connection, target_date: date | None, result: FetchResult, rows_written: int) -> None:
    """Same upsert-on-ok / insert-fresh-on-fail pattern as m1_participant_oi.py.

    ingest_log's partial unique index allows only one status='ok' row per
    (collector, target_date); re-running an already-'ok' day must UPDATE
    that row in place, or the very idempotency this exists to support would
    crash the re-run. Fresh rows on empty/error preserve retry history.
    """
    status = "ok" if result.status == "ok" else ("empty" if result.status == "empty" else "error")
    detail = result.detail or (f"symbol={result.symbol}" if result.symbol else "")
    with connection.cursor() as cursor:
        if status == "ok":
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (collector, target_date) WHERE status = 'ok' DO UPDATE SET
                     run_at = now(), rows_written = EXCLUDED.rows_written, detail = EXCLUDED.detail""",
                (COLLECTOR, target_date, status, rows_written, detail),
            )
        else:
            cursor.execute(
                """INSERT INTO ingest_log (collector, target_date, status, rows_written, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                (COLLECTOR, target_date, status, rows_written, detail),
            )


def run(dsn: str, lookback_days: int) -> FetchResult:
    client_id, token = load_fyers_credentials()
    auth = f"{client_id}:{token}"
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)

    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        with httpx.Client() as client:
            symbol = pick_front_month_symbol(client)
            result = fetch(client, auth, symbol, start, end)
        rows_written = upsert(connection, symbol, result.rows) if result.status == "ok" else 0
        target_date = max((row["dt"] for row in result.rows), default=None)
        log_run(connection, target_date, result, rows_written)
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=365,
                        help="calendar days of history to request (default 365 -- "
                             "Fyers caps 1D-resolution ranges at 366 days per call; "
                             "60-day rolling beta only needs ~90 trading days' margin)")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    result = run(args.dsn, args.lookback_days)
    print(f"{result.symbol}: {result.status}"
          + (f" -- {result.detail}" if result.detail else "")
          + (f" ({len(result.rows)} daily rows, {min(r['dt'] for r in result.rows)} to "
             f"{max(r['dt'] for r in result.rows)})" if result.rows else ""))
    return 1 if result.status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
