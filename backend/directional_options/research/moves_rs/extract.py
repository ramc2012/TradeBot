"""(A) PG extraction fallback for the monthly-move study.

Normally NOT run: build_daily.py reuses the 30-minute spot CSVs already on
disk from the panel_2d3d pass (verified row-for-row against PG for
2026-06: 67,312 bars / 224 names in both). This script exists so the study is
reproducible from a cold database.

PG QUERY RULE (non-negotiable): the range predicate on `time` is two literal
UTC timestamps applied DIRECTLY to the partitioning column -- no cast, no
function, no bind-parameter interval -- so TimescaleDB plan-time chunk
exclusion holds. `interval` is pinned. One MONTH per query, written straight
to disk, so no large multi-instrument result set is ever held in memory.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "raw30m")
os.makedirs(DATA, exist_ok=True)
DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

MONTHS = [
    ("2025-03-01", "2025-04-01"), ("2025-04-01", "2025-05-01"),
    ("2025-05-01", "2025-06-01"), ("2025-06-01", "2025-07-01"),
    ("2025-07-01", "2025-08-01"), ("2025-08-01", "2025-09-01"),
    ("2025-09-01", "2025-10-01"), ("2025-10-01", "2025-11-01"),
    ("2025-11-01", "2025-12-01"), ("2025-12-01", "2026-01-01"),
    ("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01"),
    ("2026-03-01", "2026-04-01"), ("2026-04-01", "2026-05-01"),
    ("2026-05-01", "2026-06-01"), ("2026-06-01", "2026-07-01"),
    ("2026-07-01", "2026-08-01"),
]

SQL = """
COPY (
  SELECT time, underlying, open, high, low, close, volume
  FROM underlying_spot_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
) TO STDOUT WITH CSV HEADER
"""


def run(a: str, b: str) -> None:
    out = os.path.join(DATA, f"spot_{a}.csv")
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out)
        return
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + DOCKER_PATH}
    with open(out, "w") as fh:
        p = subprocess.run(
            ["docker", "exec", "nomadcurie_db", "psql", "-U", "nomadcurie",
             "-c", SQL.format(a=a, b=b)],
            stdout=fh, stderr=subprocess.PIPE, env=env,
        )
    if p.returncode != 0:
        print(p.stderr.decode()[:800], file=sys.stderr)
        raise SystemExit(1)
    print("wrote", out, os.path.getsize(out))


if __name__ == "__main__":
    for a, b in MONTHS:
        run(a, b)
