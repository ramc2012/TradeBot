import asyncio, json
from datetime import datetime, timezone
from auction_intelligence import live as L
async def main():
    from sqlalchemy import text
    from db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("SELECT time,ltp,bid,ask,bid_qty,ask_qty,total_buy_qty,total_sell_qty,volume,oi FROM market_ticks WHERE symbol='NSE:NIFTY50-INDEX' AND time::date='2026-06-24' ORDER BY time LIMIT 400"))
        mm=r.mappings().all()
    rows=[{'timestamp':m['time'].astimezone(timezone.utc),'ltp':float(m['ltp'] or 0),'bid':float(m['bid'] or 0),'ask':float(m['ask'] or 0),'bid_qty':float(m['bid_qty'] or 0),'ask_qty':float(m['ask_qty'] or 0),'total_buy_qty':float(m['total_buy_qty'] or 0),'total_sell_qty':float(m['total_sell_qty'] or 0),'volume':float(m['volume'] or 0),'oi':float(m['oi'] or 0)} for m in mm]
    print('rows',len(rows),'raw bid/ask/bidq/vol', rows[0]['bid'],rows[0]['ask'],rows[0]['bid_qty'],rows[0]['volume'])
    qh = L._build_quote_history_from_ticks(rows, tick_size=0.5)
    print('quote_hist',len(qh),'sample', {k:qh[5][k] for k in ('bid','ask','bid_size','ask_size','total_buy_qty')})
    trades = L._build_trade_prints_from_ticks(rows, tick_size=0.5)
    sides={}
    qtys=set()
    for t in trades:
        sides[t['aggressor_side']]=sides.get(t['aggressor_side'],0)+1
        qtys.add(t['quantity'])
    print('trades',len(trades),'side_counts',sides,'distinct_qty',sorted(qtys)[:6])
    from auction_intelligence.order_flow.engine import OrderFlowEngine
    from auction_intelligence.schemas import QuoteSnapshot, TradePrint, DepthSnapshot, DepthLevel
    from auction_intelligence.config.loader import load_default_config
    cfg = load_default_config().get('order_flow',{})
    eng = OrderFlowEngine(cfg)
    qsnaps=[QuoteSnapshot(timestamp=datetime.fromisoformat(q['timestamp']),bid=q['bid'],ask=q['ask'],bid_size=q['bid_size'],ask_size=q['ask_size']) for q in qh]
    tprints=[TradePrint(timestamp=datetime.fromisoformat(t['timestamp']),price=t['price'],quantity=t['quantity'],aggressor_side=t['aggressor_side']) for t in trades]
    dd = L._build_depth_from_tick_history(qh, tick_size=0.5)
    ts = dd['timestamp'] if isinstance(dd['timestamp'],datetime) else datetime.fromisoformat(str(dd['timestamp']))
    depth = DepthSnapshot(timestamp=ts, bids=[DepthLevel(price=b['price'],quantity=b['quantity']) for b in dd['bids']], asks=[DepthLevel(price=a['price'],quantity=a['quantity']) for a in dd['asks']])
    snap = eng.compute(qsnaps[-1], tprints, depth=depth, tick_size=0.5, quote_history=qsnaps)
    print('OF top_imb',snap.top_imbalance,'depth_imb',snap.depth_imbalance,'ofi',snap.order_flow_imbalance,'book_p',snap.book_pressure,'queue_p',snap.queue_pressure)
    print('OF spread',snap.spread,'delta',snap.delta,'abv',snap.aggressive_buy_volume,'asv',snap.aggressive_sell_volume,'cum',snap.cumulative_delta)
    print('OF tox',snap.toxicity_score,'tconf',snap.timing_confidence,'exec',snap.execution_aggression,'vwap_drift',snap.vwap_drift)
asyncio.run(main())
