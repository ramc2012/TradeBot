import httpx
from api.routers.auth import get_broker_token, load_persistent_credentials
load_persistent_credentials(); tok=get_broker_token("upstox")
H={"Authorization":f"Bearer {tok}"}
# live NIFTY FUT keys from master: Jun/Jul/Aug 2026
for key,exp in [("NSE_FO|62329","Jun26"),("NSE_FO|61093","Jul26"),("NSE_FO|58072","Aug26")]:
    # pull wide range; Upstox returns only what exists
    r=httpx.get(f"https://api.upstox.com/v3/historical-candle/{key}/minutes/1/2026-05-29/2025-01-01",headers=H,timeout=40)
    c=r.json().get("data",{}).get("candles",[]) if r.status_code==200 else []
    if c:
        print(f"{exp} {key}: {len(c)} 1-min candles, {c[-1][0][:10]} → {c[0][0][:10]}")
    else:
        print(f"{exp} {key}: status {r.status_code}, no candles")
