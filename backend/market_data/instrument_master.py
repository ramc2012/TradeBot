"""One instrument-master fetch for core, strategy and research consumers."""
import gzip
import json
import time
import httpx
from mp_core.cache import cached_json

URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


def load_master():
    def fetch():
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get(URL)
            response.raise_for_status()
            raw = response.content
            rows = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
            if not isinstance(rows, list) or len(rows) < 1000:
                raise ValueError("Incomplete instrument master; retain previous catalog")
            return rows
    return cached_json("instrument-master-v1", [int(time.time() // 21600)], fetch, ttl=21600)
