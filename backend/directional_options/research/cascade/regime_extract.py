"""(1) Regime-duration study — PG extraction of long-history 30m spot bars.

The 2-3 day study (../panel_2d3d/data/spot_*.csv) already holds every
underlying from 2025-01-01 onward.  Regime *duration* needs more history than
that, and the indices + MCX roots carry 30m bars back to 2021-06-21, so this
script pulls ONLY the pre-2025 tail and leaves the 2025+ CSVs alone.

PG QUERY RULE (non-negotiable): `time` is bounded DIRECTLY by two literal UTC
timestamps.  No function, cast, date_trunc or bind-parameter interval ever
touches the partitioning column in a WHERE clause, so TimescaleDB chunk
exclusion holds.  Windows are quarterly so no huge result set is ever held.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

# quarterly windows covering the pre-2025 history (indices + MCX only exist here)
WINDOWS = [
    ("2021-06-01", "2021-10-01"),
    ("2021-10-01", "2022-01-01"),
    ("2022-01-01", "2022-04-01"),
    ("2022-04-01", "2022-07-01"),
    ("2022-07-01", "2022-10-01"),
    ("2022-10-01", "2023-01-01"),
    ("2023-01-01", "2023-04-01"),
    ("2023-04-01", "2023-07-01"),
    ("2023-07-01", "2023-10-01"),
    ("2023-10-01", "2024-01-01"),
    ("2024-01-01", "2024-04-01"),
    ("2024-04-01", "2024-07-01"),
    ("2024-07-01", "2024-10-01"),
    ("2024-10-01", "2025-01-01"),
]

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
    for a, b in WINDOWS:
        run(SPOT_SQL.format(a=a, b=b), os.path.join(DATA, f"spot_{a}.csv"))
