"""Bounded PG extraction for the HTF-regime study option tape.

PG QUERY RULE (non-negotiable, inherited): the range predicate on `time` is
two literal UTC timestamps applied DIRECTLY to the partitioning column — no
cast, no function, no bind-parameter interval — so TimescaleDB plan-time
chunk exclusion holds. Half-month windows, streamed via COPY straight to
disk, one at a time; no large result set is ever held in PG memory.

INHERITED DEFECT FIXES (contrast with older extracts in cascade/ and
panel_2d3d/ which carried the bugs):
  - NO moneyness predicate (D1: it deleted winners twice).
  - NO `underlying_price IS NOT NULL` predicate (D2: the column is unwritten
    by the live writers; it deleted 42% of contracts). `underlying_price` is
    not even SELECTed — the read layer joins spot itself.
  - `source` IS selected, because dedup (D4) happens in the read layer at
    the contract level, never by summing across brokers.

Spot is NOT re-extracted: panel_2d3d/data/spot_*.csv already carries the 30m
spot tape 2025-01 -> 2026-07-21 (verified row-for-row against PG in the
moves_rs pass) and load_spot_csvs() consumes it directly.

DEFAULT RUN = SMOKE WINDOWS ONLY (June-July 2026, ~1.6M rows total, covers
the 2026-06-30 + 2026-07-28 full-life monthlies and the newly backfilled
2026-08-25). The FULL plan (2025-03-15 onward, option tape start ~2025-03-20)
is listed but gated behind --full: run it OFF-HOURS, one window at a time —
PG has been OOM-killed twice in 48h and a cleanup workflow queries
concurrently. For the measurement pass proper, prefer the SIGNAL-DRIVEN
variant: compute timer entries from spot first, then extract only the
(underlying, expiry-month) pairs actually entered, exactly as
cascade/ver_full_tape.py did with instrument keys.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "opt")
os.makedirs(DATA, exist_ok=True)
DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

SMOKE_WINDOWS = [
    ("2026-06-01", "2026-06-16"),
    ("2026-06-16", "2026-07-01"),
    ("2026-07-01", "2026-07-11"),
    ("2026-07-11", "2026-07-22"),
]

FULL_WINDOWS = [
    (f"{y}-{m:02d}-{d}", nxt)
    for (y, m, d), nxt in []  # populated below
]
# half-month windows 2025-03-15 .. 2026-06-01 (SMOKE covers the rest)
_edges = []
for y in (2025, 2026):
    for m in range(1, 13):
        _edges += [f"{y}-{m:02d}-01", f"{y}-{m:02d}-16"]
_edges = [e for e in _edges if "2025-03-15" <= e < "2026-06-01"]
FULL_WINDOWS = list(zip(["2025-03-15"] + _edges, _edges + ["2026-06-01"]))
FULL_WINDOWS = [(a, b) for a, b in FULL_WINDOWS if a < b]

SQL = """
COPY (
  SELECT time, underlying, expiry, strike, option_type,
         open, high, low, close, volume, oi, iv, source
  FROM option_premium_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
    AND strike IS NOT NULL AND expiry IS NOT NULL
) TO STDOUT WITH CSV HEADER
"""

# spot WITH source, so the read layer can dedup by the declared priority
# instead of the max-volume proxy the legacy panel CSVs force.
SPOT_SQL = """
COPY (
  SELECT time, underlying, open, high, low, close, volume, source
  FROM underlying_spot_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND close IS NOT NULL AND close > 0
) TO STDOUT WITH CSV HEADER
"""


def run(a: str, b: str, sql: str, prefix: str) -> None:
    out = os.path.join(DATA, f"{prefix}_{a}.csv")
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out)
        return
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + DOCKER_PATH}
    with open(out, "w") as fh:
        p = subprocess.run(
            ["docker", "exec", "nomadcurie_db", "psql", "-U", "nomadcurie",
             "-v", "ON_ERROR_STOP=1", "-c", sql.format(a=a, b=b)],
            stdout=fh, stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        print(p.stderr.decode()[:800], file=sys.stderr)
        raise SystemExit(1)
    print("wrote", out, os.path.getsize(out), flush=True)


if __name__ == "__main__":
    windows = FULL_WINDOWS + SMOKE_WINDOWS if "--full" in sys.argv \
        else SMOKE_WINDOWS
    for a, b in windows:
        run(a, b, SQL, "opt")
        run(a, b, SPOT_SQL, "spot")
