"""Discover the correct Upstox instrument_key for Bharat Bond ETF + Liquid ETF.

Pulls Upstox's NSE instrument master (gzip-compressed CSV) and greps for
candidate symbol names. Prints matching rows so we can pick the right
instrument_key for ingestion.
"""
from __future__ import annotations

import gzip
import io
import sys

import httpx


# Upstox's canonical NSE instrument master, refreshed daily by Upstox.
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# Symbol candidates we want to resolve.
SEARCH_TERMS = [
    "BHARATBOND",
    "BBETF",
    "LIQUIDBEES",
    "LIQUID",
    "GILTBEES",
    "GOLDBEES",
    "SILVERBEES",
]


def main() -> None:
    print(f"Fetching {MASTER_URL} …")
    try:
        resp = httpx.get(MASTER_URL, timeout=60.0, follow_redirects=True)
    except Exception as exc:
        print(f"network error: {exc}")
        return
    if resp.status_code != 200:
        print(f"http {resp.status_code} {resp.text[:200]}")
        return
    raw = resp.content
    try:
        decompressed = gzip.decompress(raw)
    except Exception:
        decompressed = raw
    import json
    try:
        catalog = json.loads(decompressed.decode("utf-8"))
    except Exception as exc:
        print(f"could not parse JSON: {exc}")
        return
    print(f"loaded {len(catalog)} instrument rows from Upstox master")

    for term in SEARCH_TERMS:
        matches = []
        for row in catalog:
            ts = str(row.get("trading_symbol") or "")
            nm = str(row.get("name") or "")
            if term in ts.upper() or term in nm.upper():
                matches.append(row)
        if not matches:
            print(f"\n=== {term}: NO MATCHES ===")
            continue
        print(f"\n=== {term}: {len(matches)} matches ===")
        for m in matches[:8]:
            print(
                f"  ik={m.get('instrument_key'):30}  "
                f"ts={str(m.get('trading_symbol') or ''):20}  "
                f"name={(m.get('name') or '')[:50]}  "
                f"isin={m.get('isin')}  "
                f"seg={m.get('segment')}  "
                f"type={m.get('instrument_type')}"
            )


if __name__ == "__main__":
    main()
