import httpx, json
from api.routers.auth import get_broker_token, load_persistent_credentials
load_persistent_credentials()
tok=get_broker_token("upstox")
H={"Authorization":f"Bearer {tok}","Accept":"application/json"}
BASE="https://api.upstox.com/v2"
NIFTY="NSE_INDEX|Nifty 50"

def get(url, **p):
    r=httpx.get(url, headers=H, params=p, timeout=30)
    return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:200])

# 1) expired FUT expiries
sc,exp=get(f"{BASE}/expired-instruments/expiries", instrument_key=NIFTY, instrument_type="FUT")
print("EXPIRIES status",sc)
exps=exp.get("data",[]) if isinstance(exp,dict) else exp
print("FUT expiries:", exps[:15] if isinstance(exps,list) else exps)

# 2) resolve a contract key for one expiry (pick May 2025)
if isinstance(exps,list) and exps:
    target=[e for e in exps if str(e).startswith("2025-05")] or exps[:1]
    e=target[0]
    sc2,c=get(f"{BASE}/expired-instruments/future/contract", instrument_key=NIFTY, expiry_date=e)
    print("CONTRACT", e, "status", sc2)
    print("  data:", json.dumps(c.get("data",c))[:400] if isinstance(c,dict) else c)
