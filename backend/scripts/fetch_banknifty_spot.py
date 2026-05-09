"""
Fetch BANKNIFTY 1-min spot data from Fyers and save to index_analytics_data.
Fetches in 60-day chunks (Fyers 1-min limit) from 2025-04-03 to today.
"""
from __future__ import annotations
import csv, gzip, json, os, sys, time
from datetime import date, timedelta
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

CREDS_FILE = Path("/app/credentials.json")
DATA_ROOT = Path("/app/runtime/index_analytics_data")
OUT_DIR = DATA_ROOT / "spot" / "underlying=BANKNIFTY"
OUT_FILE = OUT_DIR / "1minute.csv.gz"

SYMBOL = "NSE:NIFTYBANK-INDEX"
RESOLUTION = "1"
CHUNK_DAYS = 60
FROM_DATE = date(2025, 4, 3)
TO_DATE = date.today()


def load_fyers_token() -> str:
    creds = json.loads(CREDS_FILE.read_text())
    token = creds.get("fyers", {}).get("access_token", "").strip()
    if not token:
        raise RuntimeError("Fyers access_token not found in credentials.json")
    return token


def fetch_chunk(fyers_obj, from_dt: date, to_dt: date) -> list[dict]:
    from fyers_apiv3 import fyersModel
    payload = {
        "symbol": SYMBOL,
        "resolution": RESOLUTION,
        "date_format": "1",
        "range_from": from_dt.isoformat(),
        "range_to": to_dt.isoformat(),
        "cont_flag": "1",
    }
    resp = fyers_obj.history(payload)
    candles = resp.get("candles", [])
    rows = []
    for c in candles:
        if not c or len(c) < 6:
            continue
        from datetime import datetime, UTC, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        ts = datetime.fromtimestamp(int(c[0]), IST)
        rows.append({
            "time": ts.isoformat(),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": int(c[5] or 0),
            "oi": 0,
        })
    return rows


def main():
    token = load_fyers_token()
    from fyers_apiv3 import fyersModel
    app_id = os.environ.get("FYERS_APP_ID", "")
    fyers = fyersModel.FyersModel(client_id=app_id, is_async=False, token=token, log_path="")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    current = FROM_DATE
    total_chunks = 0
    while current < TO_DATE:
        chunk_end = min(current + timedelta(days=CHUNK_DAYS), TO_DATE)
        logger.info(f"Fetching {SYMBOL} {current} → {chunk_end} ...")
        try:
            rows = fetch_chunk(fyers, current, chunk_end)
            all_rows.extend(rows)
            total_chunks += 1
            logger.info(f"  Got {len(rows)} candles (total so far: {len(all_rows)})")
        except Exception as e:
            logger.error(f"  Chunk failed: {e}")
        current = chunk_end + timedelta(days=1)
        time.sleep(0.5)  # be polite

    if not all_rows:
        logger.error("No candles fetched. Exiting.")
        sys.exit(1)

    # Deduplicate and sort by time
    seen = set()
    deduped = []
    for r in all_rows:
        if r["time"] not in seen:
            seen.add(r["time"])
            deduped.append(r)
    deduped.sort(key=lambda x: x["time"])

    # Write gzipped CSV
    FIELDS = ["time", "open", "high", "low", "close", "volume", "oi"]
    with gzip.open(OUT_FILE, "wt", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(deduped)

    logger.success(f"Saved {len(deduped):,} candles to {OUT_FILE}")
    logger.success(f"Date range: {deduped[0]['time'][:10]} to {deduped[-1]['time'][:10]}")


if __name__ == "__main__":
    main()
