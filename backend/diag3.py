import httpx, json
from api.routers.auth import load_persistent_credentials, _broker_credentials
load_persistent_credentials()
uc=_broker_credentials.get("upstox",{})
for label in ("analytics_token","access_token"):
    tok=uc.get(label)
    if not tok: continue
    H={"Authorization":f"Bearer {tok}","Accept":"application/json"}
    # a known EXPIRED option key from contract_index (NIFTY, 03-04-2025 weekly)
    key="NSE_FO|54466|03-04-2025"
    # expired historical-candle endpoint variants
    for ver in ("v2","v3"):
        url=f"https://api.upstox.com/{ver}/expired-instruments/historical-candle/{key}/1minute/2025-04-03/2025-04-03"
        if ver=="v3": url=f"https://api.upstox.com/v3/expired-instruments/historical-candle/{key}/minutes/1/2025-04-03/2025-04-03"
        try:
            r=httpx.get(url,headers=H,timeout=30); 
            n=len(r.json().get("data",{}).get("candles",[])) if r.status_code==200 else 0
            print(f"{label} {ver} expired-candle: {r.status_code} candles={n} {r.text[:90] if r.status_code!=200 else ''}")
        except Exception as e: print(label,ver,"ERR",str(e)[:80])
