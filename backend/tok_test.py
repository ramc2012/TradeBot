import httpx, json
from api.routers.auth import get_broker_token, load_persistent_credentials
load_persistent_credentials()
tok=get_broker_token("upstox")
H={"Authorization":f"Bearer {tok}","Accept":"application/json"}
def g(url,**p):
    try:
        r=httpx.get(url,headers=H,params=p,timeout=20); 
        body=r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:120]
        return r.status_code, body
    except Exception as e: return "ERR", str(e)[:120]
# token validity
print("profile:", g("https://api.upstox.com/v2/user/get-profile")[0])
# v3 expired expiries
print("v3 expiries FUT:", g("https://api.upstox.com/v3/expired-instruments/expiries", instrument_key="NSE_INDEX|Nifty 50", instrument_type="FUT"))
# a known live historical (current NIFTY FUT key from master) 1-min recent
print("v3 hist live fut:", g("https://api.upstox.com/v3/historical-candle/NSE_FO|62329/minutes/1/2026-05-29/2026-05-28")[0])
