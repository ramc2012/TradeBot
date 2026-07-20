"""(VERIFY 2b) Pull the UNTRUNCATED option tape for every contract the pyramid
study actually traded.

setups_2d3d/extract.py applies a PER-BAR predicate
    abs(strike - underlying_price) <= 0.08 * underlying_price
so a contract's bars vanish from the CSV the moment spot moves >8% away from
the strike.  A pyramided winner drives its contract deep ITM, i.e. straight
through that wall, so the winners are precisely the trades whose exit bar is
missing and gets silently priced off a stale earlier bar.

This pull is identical EXCEPT the moneyness predicate is dropped and the row
set is restricted to the 5,443 instrument_keys the study used.  PG query rule
respected: `time` is bounded DIRECTLY by two literal UTC timestamps, no
function or cast on the partitioning column, one window at a time.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"
SCRATCH = ("/private/tmp/claude-501/-Users-ramachandran-CLAUDE-PROJECTS-"
           "Nomad-Curie/2002ef9f-c3f0-4f0f-99e5-8f23a37bb313/scratchpad")

WINDOWS = [
    ("2025-01-01", "2025-04-01", "2025-01-01", "2025-05-15"),
    ("2025-04-01", "2025-07-01", "2025-04-01", "2025-08-15"),
    ("2025-07-01", "2025-10-01", "2025-07-01", "2025-11-15"),
    ("2025-10-01", "2026-01-01", "2025-10-01", "2026-02-15"),
    ("2026-01-01", "2026-04-01", "2026-01-01", "2026-05-15"),
    ("2026-04-01", "2026-07-01", "2026-04-01", "2026-08-15"),
    ("2026-07-01", "2026-07-21", "2026-07-01", "2026-09-15"),
]

SQL = """
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
    AND instrument_key IN ({keys})
) TO STDOUT WITH CSV HEADER
"""


def main() -> None:
    keys = [k.strip() for k in
            open(os.path.join(SCRATCH, "keys.txt")).read().split("\n") if k.strip()]
    klist = ",".join("'" + k.replace("'", "''") + "'" for k in keys)
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + DOCKER_PATH}
    for a, b, ea, eb in WINDOWS:
        out = os.path.join(DATA, f"fulltape_{a}.csv")
        if os.path.exists(out) and os.path.getsize(out) > 200:
            print("skip", out)
            continue
        sql = SQL.format(a=a, b=b, ea=ea, eb=eb, keys=klist)
        f = os.path.join(SCRATCH, "q.sql")
        open(f, "w").write(sql)
        with open(out, "w") as fh:
            p = subprocess.run(
                ["docker", "exec", "-i", "nomadcurie_db", "psql", "-U",
                 "nomadcurie", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                stdin=open(f), stdout=fh, stderr=subprocess.PIPE, env=env)
        if p.returncode != 0:
            print(p.stderr.decode()[:2000], file=sys.stderr)
            raise SystemExit(1)
        print("wrote", out, os.path.getsize(out), flush=True)


if __name__ == "__main__":
    main()
