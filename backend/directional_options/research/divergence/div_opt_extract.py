"""(D) OPTION TAPE EXTRACTION -- NO MONEYNESS BAND.

../setups_2d3d/extract.py filters `abs(strike - underlying_price) <= 8% *
underlying_price`. That deletes a contract's tape exactly as it goes deep ITM,
i.e. exactly on the trades that won. It corrupted two prior studies. It is NOT
inherited here: this extraction has no moneyness predicate at all, and the
whole-tape row counts are printed so the deletion can be quantified.

PG QUERY RULE (non-negotiable): `time` is bounded DIRECTLY by two literal UTC
timestamps. No function, cast, date_trunc or bind-parameter interval touches
the partitioning column, so TimescaleDB chunk exclusion holds. Windows are
quarterly, and the whole unbanded tape is ~0.56M rows per quarter, so no query
is large.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

WINDOWS = [
    ("2025-01-01", "2025-04-01"), ("2025-04-01", "2025-07-01"),
    ("2025-07-01", "2025-10-01"), ("2025-10-01", "2026-01-01"),
    ("2026-01-01", "2026-04-01"), ("2026-04-01", "2026-07-01"),
    ("2026-07-01", "2026-07-21"),
]

SQL = """
COPY (
  SELECT time, underlying, expiry, strike, option_type,
         open, high, low, close, volume, oi, iv, delta,
         underlying_price, instrument_key, source
  FROM option_premium_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
    AND underlying_price IS NOT NULL AND underlying_price > 0
    AND strike IS NOT NULL
) TO STDOUT WITH CSV HEADER
"""


def run(sql: str, out: str) -> None:
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out)
        return
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + DOCKER_PATH}
    with open(out, "w") as fh:
        p = subprocess.run(["docker", "exec", "nomadcurie_db", "psql", "-U", "nomadcurie",
                            "-c", sql], stdout=fh, stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        print(p.stderr.decode()[:800], file=sys.stderr)
        raise SystemExit(1)
    print("wrote", out, os.path.getsize(out))


if __name__ == "__main__":
    for a, b in WINDOWS:
        run(SQL.format(a=a, b=b), os.path.join(DATA, f"optfull_{a}.csv"))
