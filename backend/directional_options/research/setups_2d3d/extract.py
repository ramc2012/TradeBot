"""(C) Labelled setup dataset — PG extraction.

Pulls the two raw inputs the harness needs:

  optintra_<window>.csv : ALL 30-minute option bars inside the tradeable
                          moneyness band (|strike/underlying_price - 1| <= 8%),
                          so a triple barrier can be resolved at 30m
                          resolution rather than on daily extremes.
  (spot 30m bars are reused from ../panel_2d3d/data/spot_*.csv)

PG QUERY RULE (non-negotiable): the range predicate on `time` is always two
literal UTC timestamps applied DIRECTLY to the partitioning column. No
function, no cast, no bind-parameter interval anywhere in the range bound, so
TimescaleDB chunk exclusion holds. `interval` is pinned too. The `expiry`
filter uses literal dates on a NON-partitioning column, which is safe.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

# (window_start_utc, window_end_utc, expiry_lo, expiry_hi)
WINDOWS = [
    ("2025-01-01", "2025-04-01", "2025-01-01", "2025-05-15"),
    ("2025-04-01", "2025-07-01", "2025-04-01", "2025-08-15"),
    ("2025-07-01", "2025-10-01", "2025-07-01", "2025-11-15"),
    ("2025-10-01", "2026-01-01", "2025-10-01", "2026-02-15"),
    ("2026-01-01", "2026-04-01", "2026-01-01", "2026-05-15"),
    ("2026-04-01", "2026-07-01", "2026-04-01", "2026-08-15"),
    ("2026-07-01", "2026-07-21", "2026-07-01", "2026-09-15"),
]

OPT_INTRA_SQL = """
COPY (
  SELECT time, underlying, expiry, strike, option_type,
         open, high, low, close, volume, oi, iv, delta,
         underlying_price, instrument_key
  FROM option_premium_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
    AND underlying_price IS NOT NULL AND underlying_price > 0
    AND strike IS NOT NULL
    AND expiry >= DATE '{ea}' AND expiry <= DATE '{eb}'
    AND abs(strike - underlying_price) <= 0.08 * underlying_price
) TO STDOUT WITH CSV HEADER
"""


def run(sql: str, out: str) -> None:
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out)
        return
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + DOCKER_PATH}
    with open(out, "w") as fh:
        p = subprocess.run(
            ["docker", "exec", "nomadcurie_db", "psql", "-U", "nomadcurie", "-c", sql],
            stdout=fh, stderr=subprocess.PIPE, env=env,
        )
    if p.returncode != 0:
        print(p.stderr.decode()[:1000], file=sys.stderr)
        raise SystemExit(1)
    print("wrote", out, os.path.getsize(out))


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for a, b, ea, eb in WINDOWS:
        if only and only != a:
            continue
        run(OPT_INTRA_SQL.format(a=a, b=b, ea=ea, eb=eb),
            os.path.join(DATA, f"optintra_{a}.csv"))
