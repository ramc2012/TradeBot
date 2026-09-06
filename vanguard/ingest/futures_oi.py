"""Futures candles + open interest ingest into `stock_futures_daily`.

Closes the gap documented in features/m2_flow.py: stock-futures OI has no
source in this schema. Upstox candle arrays are POSITIONAL —
[ts, open, high, low, close, volume, oi] — and every existing collector in
the main app drops index 6; this one keeps it.

Three modes:

    python vanguard/ingest/futures_oi.py --eod [--days 7]
        Front + next active contract per underlying via the PUBLIC v2 daily
        endpoint (which EXCLUDES today — the current session only exists on
        the intraday endpoint; verified 2026-08-27, see README).

    python vanguard/ingest/futures_oi.py --live
        Today's running OI/volume/close per front contract via the PUBLIC v3
        intraday endpoint, upserted as today's row (source=
        'upstox_intraday_live'). The next --eod pass overwrites it with the
        settled candle.

    python vanguard/ingest/futures_oi.py --backfill --from-date 2024-06-01
        One-time history pull through the expired-instruments endpoints,
        which REQUIRE a Bearer token (env UPSTOX_ACCESS_TOKEN) unlike the
        candle endpoints above. Resumable: contracts whose window already has
        rows are safely upserted; rerun after any failure.

Contract keys come from the public instrument master `complete.json.gz`
(the per-exchange NSE_FO endpoint 403s; verified 2026-08-21 in
backend/market_data/futures_instrument_keys.py). The master carries the
current AND next contracts, so steady-state runs need no token at all.
Expiry on the master is epoch millis.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.parse
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

UPSTOX_V2 = "https://api.upstox.com/v2"
UPSTOX_V3 = "https://api.upstox.com/v3"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
MASTER_CACHE = Path(os.environ.get("VANGUARD_CACHE_DIR", "/tmp")) / "upstox_complete_master.json"
MASTER_TTL_SECONDS = 6 * 60 * 60

# Index underlyings and their Upstox underlying keys for expired-contract
# discovery (backend/analysis/instruments.py INDEX_INSTRUMENT_KEYS).
INDEX_UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

THROTTLE_SECONDS = 0.4
_last_call = 0.0
# Upstox standard APIs: 2,000 requests / 30 minutes, per API and user.
# https://upstox.com/developer/api-documentation/rate-limiting/
# Leave headroom for other readers; the fast interval alone misses this cap.
RATE_WINDOW_SECONDS = 1800
RATE_WINDOW_CALLS = 1800
_api_calls = defaultdict(deque)


def _window_throttle(url):
    path = urllib.parse.urlsplit(url).path
    # Candle URLs include the contract/window; all share one API quota.
    key = path.split("/historical-candle")[0] + "/historical-candle" if "/historical-candle" in path else path
    calls = _api_calls[key]
    while True:
        now = time.monotonic()
        while calls and calls[0] <= now - RATE_WINDOW_SECONDS:
            calls.popleft()
        if len(calls) < RATE_WINDOW_CALLS:
            calls.append(now)
            return
        time.sleep(min(60, max(0.01, calls[0] + RATE_WINDOW_SECONDS - now)))



def _throttle() -> None:
    global _last_call
    elapsed = time.monotonic() - _last_call
    if elapsed < THROTTLE_SECONDS:
        time.sleep(THROTTLE_SECONDS - elapsed)
    _last_call = time.monotonic()


@dataclass
class Contract:
    symbol: str
    expiry: date
    instrument_key: str
    expired: bool = False


# --------------------------------------------------------------------------
# Instrument master
# --------------------------------------------------------------------------
def load_master(client: httpx.Client) -> list[dict]:
    if MASTER_CACHE.exists() and time.time() - MASTER_CACHE.stat().st_mtime < MASTER_TTL_SECONDS:
        return json.loads(MASTER_CACHE.read_text())
    response = client.get(MASTER_URL, headers={"Accept-Encoding": "gzip"}, timeout=120)
    response.raise_for_status()
    raw = response.content
    rows = json.loads((gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8"))
    rows = [r for r in rows if isinstance(r, dict)]
    try:
        MASTER_CACHE.write_text(json.dumps(rows))
    except OSError:
        pass
    return rows


def _master_expiry(raw: object) -> date | None:
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date()
        return date.fromisoformat(str(raw)[:10])
    except Exception:
        return None


def active_fut_contracts(master: list[dict], underlyings: list[str], as_of: date) -> dict[str, list[Contract]]:
    """Map underlying -> unexpired FUT contracts sorted by expiry (front first)."""
    wanted = {u.upper() for u in underlyings}
    out: dict[str, list[Contract]] = {u: [] for u in wanted}
    for row in master:
        if str(row.get("instrument_type") or "").upper() != "FUT":
            continue
        name = str(row.get("underlying_symbol") or row.get("asset_symbol") or row.get("name") or "").upper()
        if name not in wanted:
            continue
        key = str(row.get("instrument_key") or "").strip()
        expiry = _master_expiry(row.get("expiry"))
        if not key or expiry is None or expiry < as_of:
            continue
        out[name].append(Contract(symbol=name, expiry=expiry, instrument_key=key))
    for name in out:
        out[name].sort(key=lambda c: c.expiry)
    return out


def underlying_keys_for_discovery(master: list[dict], underlyings: list[str]) -> dict[str, str]:
    """Underlying instrument keys for the expired-contract discovery endpoints:
    NSE_EQ rows for stocks, the fixed index map for indices."""
    keys = dict(INDEX_UNDERLYING_KEYS)
    wanted = {u.upper() for u in underlyings} - set(keys)
    for row in master:
        if (str(row.get("segment") or "").upper() != "NSE_EQ"
                or str(row.get("instrument_type") or "").upper() != "EQ"):
            continue
        symbol = str(row.get("trading_symbol") or "").upper()
        if symbol in wanted:
            key = str(row.get("instrument_key") or "").strip()
            if key:
                keys[symbol] = key
                wanted.discard(symbol)
    return keys


# --------------------------------------------------------------------------
# Upstox candle fetches
# --------------------------------------------------------------------------
def _get_json(client: httpx.Client, url: str, *, params: dict | None = None,
              token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(7):
        _window_throttle(url)
        _throttle()
        response = client.get(url, params=params or {}, headers=headers, timeout=30)
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 6:
            break
        try:
            delay = max(float(response.headers.get("Retry-After", 0)), min(60, 2 ** (attempt+1)))
        except ValueError:
            delay = min(60, 2 ** (attempt+1))
        print(f"[futures-oi] HTTP {response.status_code}; retry {attempt+1}/6 in {delay:.0f}s", flush=True)
        time.sleep(delay)
    payload = response.json()
    if response.status_code != 200:
        raise RuntimeError(f"Upstox HTTP {response.status_code} for {url}: {str(payload)[:200]}")
    return payload


def normalize_candles(candles: list) -> list[dict]:
    """Positional Upstox candles -> dict rows, KEEPING oi (index 6)."""
    rows: list[dict] = []
    for c in reversed(candles or []):
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        stamp = datetime.fromisoformat(str(c[0]).replace("Z", "+00:00"))
        rows.append({
            "ts": stamp.date(),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]),
            "volume": int(c[5] or 0),
            "oi": int(c[6] or 0) if len(c) > 6 else 0,
        })
    return rows


def fetch_daily(client: httpx.Client, contract: Contract, start: date, end: date,
                token: str | None = None) -> list[dict]:
    encoded = urllib.parse.quote(contract.instrument_key, safe="")
    prefix = "expired-instruments/historical-candle" if contract.expired else "historical-candle"
    url = f"{UPSTOX_V2}/{prefix}/{encoded}/day/{end.isoformat()}/{start.isoformat()}"
    payload = _get_json(client, url, token=token if contract.expired else None)
    return normalize_candles((payload.get("data") or {}).get("candles"))


def fetch_intraday_today(client: httpx.Client, contract: Contract) -> dict | None:
    """Collapse today's 30-minute intraday bars into one running daily row."""
    encoded = urllib.parse.quote(contract.instrument_key, safe="")
    url = f"{UPSTOX_V3}/historical-candle/intraday/{encoded}/minutes/30"
    payload = _get_json(client, url)
    bars = normalize_candles((payload.get("data") or {}).get("candles"))
    if not bars:
        return None
    return {
        "ts": bars[-1]["ts"],
        "open": bars[0]["open"],
        "high": max(b["high"] for b in bars),
        "low": min(b["low"] for b in bars),
        "close": bars[-1]["close"],
        "volume": sum(b["volume"] for b in bars),
        # OI is a level, not a flow: the latest bar's OI is the running total.
        "oi": bars[-1]["oi"],
    }


# --------------------------------------------------------------------------
# Expired-contract discovery (token-gated; backfill only)
# --------------------------------------------------------------------------
def expired_contracts(client: httpx.Client, symbol: str, underlying_key: str,
                      from_date: date, token: str) -> list[Contract]:
    payload = _get_json(client, f"{UPSTOX_V2}/expired-instruments/expiries",
                        params={"instrument_key": underlying_key}, token=token)
    expiries = sorted({
        d for d in (_master_expiry(item) for item in payload.get("data") or [])
        if d is not None and d >= from_date
    })
    contracts: list[Contract] = []
    for expiry in expiries:
        payload = _get_json(client, f"{UPSTOX_V2}/expired-instruments/future/contract",
                            params={"instrument_key": underlying_key,
                                    "expiry_date": expiry.isoformat()}, token=token)
        rows = list(payload.get("data") or [])
        rows.sort(key=lambda r: str(r.get("instrument_type") or "").upper() != "FUT")
        for row in rows[:1]:
            key = str(row.get("instrument_key") or "").strip()
            if key:
                contracts.append(Contract(symbol=symbol, expiry=expiry,
                                          instrument_key=key, expired=True))
    return contracts


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def load_universe(connection) -> list[str]:
    with connection.cursor() as cur:
        cur.execute("SELECT symbol FROM sector_taxonomy WHERE instrument_type = 'Equity'")
        stocks = sorted({row[0] for row in cur.fetchall()})
    return stocks + list(INDEX_UNDERLYING_KEYS)


def upsert_rows(connection, contract: Contract, rows: list[dict], source: str) -> int:
    if not rows:
        return 0
    rows = list({r["ts"]: r for r in rows}.values())
    values = [
        (r["ts"], contract.symbol, contract.expiry, contract.instrument_key,
         r["open"], r["high"], r["low"], r["close"], r["volume"], r["oi"], source)
        for r in rows
    ]
    with connection.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO stock_futures_daily
                (ts, symbol, expiry, instrument_key, open, high, low, close, volume, oi, source)
            VALUES %s
            ON CONFLICT (symbol, expiry, ts) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume, oi = EXCLUDED.oi,
                source = EXCLUDED.source, synced_at = now()
            """,
            values,
        )
    connection.commit()
    return len(values)


def existing_row_count(connection, contract: Contract) -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM stock_futures_daily WHERE symbol = %s AND expiry = %s",
            (contract.symbol, contract.expiry),
        )
        return int(cur.fetchone()[0])


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def run_eod(connection, client: httpx.Client, days: int) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    universe = load_universe(connection)
    contracts = active_fut_contracts(load_master(client), universe, today)
    total, failures = 0, []
    for symbol in universe:
        for contract in contracts.get(symbol, [])[:2]:  # front + next
            try:
                total += upsert_rows(connection, contract,
                                     fetch_daily(client, contract, start, today),
                                     "upstox_daily")
            except Exception as exc:  # noqa: BLE001
                connection.rollback()
                failures.append(f"{symbol}/{contract.expiry}: {exc}")
    print(f"[futures-oi eod] upserted {total} rows for {len(universe)} underlyings; "
          f"{len(failures)} failures")
    for line in failures[:10]:
        print(f"  FAIL {line}")


def run_live(connection, client: httpx.Client) -> None:
    today = date.today()
    universe = load_universe(connection)
    contracts = active_fut_contracts(load_master(client), universe, today)
    total, failures = 0, []
    for symbol in universe:
        fronts = contracts.get(symbol, [])[:1]
        for contract in fronts:
            try:
                row = fetch_intraday_today(client, contract)
                if row and row["ts"] == today:
                    total += upsert_rows(connection, contract, [row], "upstox_intraday_live")
            except Exception as exc:  # noqa: BLE001
                connection.rollback()
                failures.append(f"{symbol}: {exc}")
    print(f"[futures-oi live] upserted {total} live rows; {len(failures)} failures")
    for line in failures[:10]:
        print(f"  FAIL {line}")


def run_backfill(connection, client: httpx.Client, from_date: date, token: str,
                 symbols: list[str] | None = None) -> dict:
    today = date.today()
    universe = load_universe(connection)
    if symbols is not None:
        universe = [symbol for symbol in universe if symbol in set(symbols)]
    master = load_master(client)
    discovery_keys = underlying_keys_for_discovery(master, universe)
    active = active_fut_contracts(master, universe, today)
    total, skipped, failures = 0, 0, []
    for index, symbol in enumerate(universe, start=1):
        underlying_key = discovery_keys.get(symbol)
        if not underlying_key:
            failures.append(f"{symbol}: no underlying instrument key on the master")
            continue
        try:
            contracts = expired_contracts(client, symbol, underlying_key, from_date, token)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{symbol} discovery: {exc}")
            continue
        contracts += active.get(symbol, [])
        for contract in contracts:
            try:
                # Always request the full window. Upserts are idempotent;
                # a nonzero row count cannot prove a partial import complete.
                start = max(from_date, contract.expiry - timedelta(days=120))
                end = min(contract.expiry, today)
                total += upsert_rows(connection, contract,
                                     fetch_daily(client, contract, start, end,
                                                 token=token),
                                     "upstox_expired" if contract.expired else "upstox_daily")
            except Exception as exc:  # noqa: BLE001
                connection.rollback()
                failures.append(f"{symbol}/{contract.expiry}: {exc}")
        if index % 1 == 0:
            print(f"[futures-oi backfill] {index}/{len(universe)} underlyings, "
                  f"{total} rows, {skipped} contracts skipped, {len(failures)} failures")
    print(f"[futures-oi backfill] DONE: {total} rows, {skipped} contracts skipped, "
          f"{len(failures)} failures")
    for line in failures:
        print(f"  FAIL {line}")
    return {"rows": total, "symbols": len(universe), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--eod", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--backfill", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--from-date", type=lambda s: date.fromisoformat(s),
                        default=date(2024, 6, 1))
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        with httpx.Client(follow_redirects=True) as client:
            if args.eod:
                run_eod(connection, client, args.days)
            elif args.live:
                run_live(connection, client)
            else:
                token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
                if not token:
                    print("[futures-oi] --backfill needs UPSTOX_ACCESS_TOKEN for the "
                          "expired-instruments discovery endpoints", file=sys.stderr)
                    return 2
                result = run_backfill(connection, client, args.from_date, token)
                if result["failures"]:
                    return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
