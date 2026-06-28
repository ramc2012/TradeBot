import asyncio
from datetime import datetime, timezone
from auction_intelligence import live as L
async def main():
    from sqlalchemy import text
    from db.database import AsyncSessionLocal
    # bar rows (minute) -> _infer_trade_prints uses open/close/volume
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT time,open,high,low,close,volume FROM underlying_spot_candles WHERE symbol='NSE:NIFTY50-INDEX' AND time::date='2026-06-24' ORDER BY time LIMIT 60"))
        mm=r.mappings().all()
    if not mm:
        # try alternate table names
        for tbl in ('index_candles','spot_candles','underlying_candles','market_candles'):
            try:
                async with AsyncSessionLocal() as s:
                    r = await s.execute(text(f"SELECT time,open,high,low,close,volume FROM {tbl} WHERE symbol='NSE:NIFTY50-INDEX' AND time::date='2026-06-24' ORDER BY time LIMIT 60"))
                    mm=r.mappings().all()
                if mm:
                    print('using table', tbl); break
            except Exception as e:
                pass
    rows=[{'time':str(m['time']),'open':float(m['open']),'high':float(m['high']),'low':float(m['low']),'close':float(m['close']),'volume':float(m['volume'] or 0)} for m in mm]
    print('bar rows', len(rows), 'vol_sample', [r['volume'] for r in rows[:5]] if rows else None)
    tp = L._infer_trade_prints(rows)
    sides={}
    for t in tp: sides[t['aggressor_side']]=sides.get(t['aggressor_side'],0)+1
    print('infer_trade_prints', len(tp), 'sides', sides, 'qty_sample', [t['quantity'] for t in tp[:5]])
asyncio.run(main())
