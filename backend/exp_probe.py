import httpx, json
from api.routers.auth import get_broker_token, load_persistent_credentials
load_persistent_credentials(); tok=get_broker_token("upstox")
H={"Authorization":f"Bearer {tok}","Accept":"application/json"}
def g(url,**p):
    try:
        r=httpx.get(url,headers=H,params=p,timeout=30)
        ct=r.headers.get("content-type","")
        return r.status_code,(r.json() if ct.startswith("application/json") else r.text[:160])
    except Exception as e: return "ERR",str(e)[:160]
NIFTY="NSE_INDEX|Nifty 50"
# try several documented shapes for expired FUT expiries
tests=[
 ("v2 expiries FUT", "https://api.upstox.com/v2/expired-instruments/expiries", {"instrument_key":NIFTY,"instrument_type":"FUT"}),
 ("v2 expiries FUTURE","https://api.upstox.com/v2/expired-instruments/expiries", {"instrument_key":NIFTY,"instrument_type":"FUTURE"}),
 ("v2 expiries noType","https://api.upstox.com/v2/expired-instruments/expiries", {"instrument_key":NIFTY}),
]
for name,url,p in tests:
    sc,body=g(url,**p)
    print(name,"→",sc, (body if sc!=200 else body.get("data",body))[:6] if isinstance(body,(list,)) else (body.get("data") if isinstance(body,dict) else body))
