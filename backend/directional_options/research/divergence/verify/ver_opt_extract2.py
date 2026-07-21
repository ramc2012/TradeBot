"""VERIFY-D1: re-extract the CE option tape with NO underlying_price predicate.

DEFECT UNDER TEST. div_opt_extract.py filters `underlying_price IS NOT NULL
AND underlying_price > 0`. In our store that column is populated only by the
post-expiry backfill writer (`upstox_expired`) and by the 5 index symbols on
fyers -- it is NULL on 100% of live `upstox` and stock `fyers` rows. The
predicate therefore silently deletes 42% of CE rows and 4,657 of 11,072
distinct CE contracts, including 100% of the owner's own PNB 2026-07-28 tape.
That is the same FAMILY of bug as the +-8% moneyness band the study was told
not to inherit: an incidental predicate that removes contracts.

FIX: drop the predicate; supply the spot from our own 30m spot panel
(cascade/data/intra.parquet) joined on (underlying, time).

PG QUERY RULE: `time` bounded DIRECTLY by literal UTC timestamps, quarterly.
"""
from __future__ import annotations
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
DOCKER_PATH = "/Applications/Docker.app/Contents/Resources/bin"

WINDOWS = [("2025-01-01","2025-04-01"),("2025-04-01","2025-07-01"),
           ("2025-07-01","2025-10-01"),("2025-10-01","2026-01-01"),
           ("2026-01-01","2026-04-01"),("2026-04-01","2026-07-01"),
           ("2026-07-01","2026-07-21")]

SQL = """
COPY (
  SELECT time, underlying, expiry, strike, option_type,
         open, high, low, close, volume, oi, underlying_price,
         instrument_key, source
  FROM option_premium_candles
  WHERE time >= TIMESTAMPTZ '{a} 00:00:00+00'
    AND time <  TIMESTAMPTZ '{b} 00:00:00+00'
    AND interval = '30minute'
    AND option_type = 'CE'
    AND close IS NOT NULL AND close > 0
    AND strike IS NOT NULL
) TO STDOUT WITH CSV HEADER
"""

def run(sql, out):
    if os.path.exists(out) and os.path.getsize(out) > 200:
        print("skip", out); return
    env = {**os.environ, "PATH": os.environ.get("PATH","") + ":" + DOCKER_PATH}
    with open(out, "w") as fh:
        p = subprocess.run(["docker","exec","nomadcurie_db","psql","-U","nomadcurie","-c",sql],
                           stdout=fh, stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        print(p.stderr.decode()[:800], file=sys.stderr); raise SystemExit(1)
    print("wrote", out, os.path.getsize(out))

if __name__ == "__main__":
    for a,b in WINDOWS:
        run(SQL.format(a=a,b=b), os.path.join(DATA, f"opt2_{a}.csv"))
