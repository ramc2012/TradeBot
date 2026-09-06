"""Build and cache the session frames used by the refutation."""
import os, sys, time
sys.path.insert(0, "/vanguard")
import pandas as pd, psycopg2
from datetime import date, timedelta
from research.mp_profile import dsn, load
from research.banknifty_rotation import BANKS

CACHE = "/vanguard/research/_refute_cache"
os.makedirs(CACHE, exist_ok=True)
start = date.today() - timedelta(days=700)
conn = psycopg2.connect(dsn())

# --- bank universe, exactly the claim's ------------------------------------
bank = ("BANKNIFTY",) + BANKS
p = f"{CACHE}/bank.pkl"
if not os.path.exists(p):
    t0 = time.time()
    s = load(conn, list(bank), start)
    s.to_pickle(p)
    print("bank", s.shape, f"{time.time()-t0:.0f}s", flush=True)

# --- out-of-universe: every non-bank stock with the same coverage ----------
q = """
SELECT underlying, count(distinct date(time AT TIME ZONE 'Asia/Kolkata')) nd
FROM underlying_spot_candles
WHERE interval='30minute'
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND time >= '2025-03-01'
GROUP BY 1 HAVING count(distinct date(time AT TIME ZONE 'Asia/Kolkata')) >= 340
"""
names = pd.read_sql(q, conn)["underlying"].tolist()
INDEXY = {"NIFTY","BANKNIFTY","FINNIFTY","SENSEX","MIDCPNIFTY","NIFTYNXT50",
          "SILVERM","NATURALGAS","COPPER","ZINCMINI","ALUMINI","GOLD","CRUDEOIL","NICKEL"}
others = sorted(set(names) - set(bank) - INDEXY)
print("non-bank names:", len(others), flush=True)
p = f"{CACHE}/other.pkl"
if not os.path.exists(p):
    frames = []
    for i in range(0, len(others), 40):
        chunk = others[i:i+40]
        t0 = time.time()
        frames.append(load(conn, chunk, start))
        print("  chunk", i, len(chunk), f"{time.time()-t0:.0f}s", flush=True)
    s = pd.concat(frames, ignore_index=True)
    s.to_pickle(p)
    print("other", s.shape, flush=True)
conn.close()
print("done")
