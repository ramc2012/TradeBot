import httpx, json
from api.routers.auth import get_broker_token, load_persistent_credentials, _broker_credentials
load_persistent_credentials(); tok=get_broker_token("upstox")
H={"Authorization":f"Bearer {tok}","Accept":"application/json"}
# is it a sandbox creds set?
uc=_broker_credentials.get("upstox",{})
print("upstox cred keys:", [k for k in uc.keys()])
print("sandbox flag:", uc.get("sandbox"), "| api_base hint:", uc.get("api_base") or uc.get("base_url"))
def g(url,**p):
    r=httpx.get(url,headers=H,params=p,timeout=30); 
    try: b=r.json()
    except: b=r.text[:120]
    return r.status_code,b
# same token: live candle vs expired expiries vs expired historical
print("LIVE candle:", g("https://api.upstox.com/v3/historical-candle/NSE_FO|62329/minutes/1/2026-05-29/2026-05-28")[0])
sc,b=g("https://api.upstox.com/v2/expired-instruments/expiries", instrument_key="NSE_INDEX|Nifty 50", instrument_type="FUT")
print("EXPIRED expiries:", sc, json.dumps(b)[:200])
# token meta
sc2,b2=g("https://api.upstox.com/v2/login/authorization/dialog")  # just to see auth behavior
print("token len:", len(tok), "prefix:", tok[:10])
