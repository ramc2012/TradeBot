import httpx, json
from api.routers.auth import load_persistent_credentials, _broker_credentials
load_persistent_credentials()
uc=_broker_credentials.get("upstox",{})
atok=uc.get("analytics_token"); rtok=uc.get("access_token")
print("analytics_token len:", len(atok) if atok else 0, "| access(read-only) len:", len(rtok) if rtok else 0)
def g(url, tok, **p):
    r=httpx.get(url, headers={"Authorization":f"Bearer {tok}","Accept":"application/json"}, params=p, timeout=30)
    try: b=r.json()
    except: b=r.text[:120]
    return r.status_code,b
NIFTY="NSE_INDEX|Nifty 50"
for label,tok in [("analytics",atok),("access",rtok)]:
    if not tok: print(label,"= none"); continue
    sc,b=g("https://api.upstox.com/v2/expired-instruments/expiries", tok, instrument_key=NIFTY, instrument_type="FUT")
    data=b.get("data") if isinstance(b,dict) else b
    print(f"{label} → expiries {sc}:", (data[:12] if isinstance(data,list) else json.dumps(b)[:160]))
