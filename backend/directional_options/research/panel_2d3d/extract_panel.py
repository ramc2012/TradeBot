"""Extract bounded EOD panels for the 2-3 day horizon study.

PG QUERY RULE: every query bounds `time` DIRECTLY with literal UTC timestamps so
TimescaleDB chunk exclusion works. No function is ever applied to the
partitioning column in the range predicate.

Writes CSVs into ./data/.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

WINDOWS = [
    ("2025-01-01", "2025-04-01"),
    ("2025-04-01", "2025-07-01"),
    ("2025-07-01", "2025-10-01"),
    ("2025-10-01", "2026-01-01"),
    ("2026-01-01", "2026-04-01"),
    ("2026-04-01", "2026-07-01"),
    ("2026-07-01", "2026-07-21"),
]

# 15:15 IST bar = 09:45 UTC -> minute-of-day 585. 09:45 IST open bar = 225.
OPT_SQL = """
COPY (
  SELECT time, underlying, expiry, strike, option_type,
         open, high, low, close, volume, oi, iv, delta,
         underlying_price, instrument_key
  FROM option_premium_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND (extract(hour from time)*60 + extract(minute from time)) IN (225, 585)
    AND close IS NOT NULL AND close > 0
) TO STDOUT WITH CSV HEADER
"""

SPOT_SQL = """
COPY (
  SELECT time, underlying, open, high, low, close, volume
  FROM underlying_spot_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
) TO STDOUT WITH CSV HEADER
"""


def run(sql: str, out: str) -> None:
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out)
        return
    with open(out, "w") as fh:
        p = subprocess.run(
            ["docker", "exec", "nomadcurie_db", "psql", "-U", "nomadcurie", "-c", sql],
            stdout=fh,
            stderr=subprocess.PIPE,
            env={**os.environ, "PATH": os.environ["PATH"] + ":/Applications/Docker.app/Contents/Resources/bin"},
        )
    if p.returncode != 0:
        print(p.stderr.decode()[:500], file=sys.stderr)
        raise SystemExit(1)
    print("wrote", out, os.path.getsize(out))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    for a, b in WINDOWS:
        if which in ("both", "opt"):
            run(OPT_SQL.format(a=a, b=b), os.path.join(DATA, f"opt_{a}.csv"))
        if which in ("both", "spot"):
            run(SPOT_SQL.format(a=a, b=b), os.path.join(DATA, f"spot_{a}.csv"))
