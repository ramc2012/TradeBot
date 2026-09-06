"""Cache the loaded index + bank frames so the refutation checks iterate fast."""
from __future__ import annotations
import os, sys, warnings
from datetime import date, timedelta
import pandas as pd, psycopg2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_profile import dsn, load
warnings.filterwarnings("ignore")

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
OUT = "/tmp/refute_cache"

def build():
    os.makedirs(OUT, exist_ok=True)
    start = date.today() - timedelta(days=1900)
    with psycopg2.connect(dsn()) as con:
        s = load(con, INDICES, start)
        from research.banknifty_rotation import BANKS
        bk = load(con, list(("BANKNIFTY",) + BANKS), date(2024, 9, 1))
        bk = bk[pd.to_datetime(bk["dt"]) >= pd.Timestamp("2024-09-01")]
        # raw 30m bars for the indices, to rebuild causal features
        bars = pd.read_sql(
            """SELECT underlying,(time AT TIME ZONE 'Asia/Kolkata') AS ts,
                      date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
                      open,high,low,close,volume
               FROM underlying_spot_candles
               WHERE interval='30minute' AND time >= %(start)s
                 AND underlying = ANY(%(names)s)
                 AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
               ORDER BY underlying, ts""",
            con, params={"start": start, "names": INDICES})
    s.to_pickle(f"{OUT}/idx.pkl"); bk.to_pickle(f"{OUT}/bank.pkl")
    bars.to_pickle(f"{OUT}/bars.pkl")
    print("idx", s.shape, "bank", bk.shape, "bars", bars.shape)

def get():
    if not os.path.exists(f"{OUT}/idx.pkl"):
        build()
    return (pd.read_pickle(f"{OUT}/idx.pkl"),
            pd.read_pickle(f"{OUT}/bank.pkl"),
            pd.read_pickle(f"{OUT}/bars.pkl"))

if __name__ == "__main__":
    build()
