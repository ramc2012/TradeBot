"""Follow frozen observations through expiry without changing their trading horizon.

Archive-only computation, one advisory-locked writer. The API reads persisted
results. No broker calls, policy optimisation, ticket writes or model promotion.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
import psycopg2
import psycopg2.extras
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model.market_calendar import is_session

IST = ZoneInfo('Asia/Kolkata')
HORIZONS = (1, 2, 3, 5, 10)
VERSION = 'longitudinal_v1'


def available(bar):
    ts = bar['time'].astimezone(IST)
    if not time(9, 15) <= ts.time() < time(15, 30) or not is_session(ts.date()):
        return None
    minutes = int(bar.get('interval', '30minute').replace('minute', ''))
    return min(ts + timedelta(minutes=minutes), ts.replace(hour=15, minute=30, second=0, microsecond=0))


def summarize(item, option_bars, spot_bars, as_of):
    entry = item.get('entry_mark')
    entry_ts = item.get('entry_ts')
    out = {**item, 'version': VERSION, 'mode': 'shadow_observation', 'paths': [],
           'horizons': {}, 'as_of': as_of, 'status': 'awaiting_entry',
           'peak_return': None, 'adverse_return': None, 'first_reach': {}}
    if entry is None or not math.isfinite(float(entry)) or float(entry) <= 0 or not entry_ts:
        return out
    entry = float(entry)
    start = available({'time': entry_ts, 'interval': '30minute'})
    if start is None or start > as_of:
        return out
    out['entry_available_at'] = start
    expiry = item['expiry']
    last_day = min(expiry, as_of.astimezone(IST).date())
    sessions = []
    day = start.date()
    while day <= last_day:
        if is_session(day): sessions.append(day)
        day += timedelta(days=1)
    offsets = {d: n for n, d in enumerate(sessions)}
    def usable(bars):
        result = []
        for b in bars:
            stamp = available(b)
            if stamp is None or stamp > as_of or stamp.date() > expiry: continue
            if b.get('close') is None or not math.isfinite(float(b['close'])) or float(b['close']) < 0: continue
            result.append((stamp, b))
        # Finest interval wins when completed timestamps coincide.
        return sorted(result, key=lambda x:(x[0], -int(x[1].get('interval','30minute').replace('minute',''))))
    spots = usable(spot_bars)
    anchors = [(t,b) for t,b in spots if t.date()==start.date() and t<=start]
    # Do not backfill the entry spot with a later price or previous session.
    spot_entry = float(anchors[-1][1]['close']) if anchors else None
    if spot_entry is not None and spot_entry <= 0: spot_entry = None
    out['spot_entry'] = spot_entry
    spot_days = {}
    for stamp,b in spots: spot_days[stamp.date()] = (stamp,float(b['close']))
    daily = {}
    peak, adverse = 0., 0.
    peak_at = start
    bars = [(stamp,b) for stamp,b in usable(option_bars) if stamp>=start and stamp.date()>=start.date()]
    for stamp,b in bars:
        ret = float(b['close'])/entry-1
        daily[stamp.date()] = {'session':stamp.date(), 'session_offset':offsets.get(stamp.date()),
                              'mark':float(b['close']), 'mark_available_at':stamp, 'return':ret}
        # High/low only from full bars beginning at/after the observed entry.
        # Never include entry candle extremes or mix overlapping small bars.
        if b.get('interval','30minute')=='30minute' and b['time']>=start:
            hi,lo = b.get('high'),b.get('low')
            if hi is not None and lo is not None and math.isfinite(float(hi)) and math.isfinite(float(lo)) and 0<=float(lo)<=float(b['close'])<=float(hi):
                if float(hi)/entry-1 > peak: peak,peak_at = float(hi)/entry-1,stamp
                adverse = min(adverse,float(lo)/entry-1)
        # First threshold is measured at a completed close, not assumed fills.
        for threshold in (.2,.4,.6,1.):
            if ret>=threshold-1e-12: out['first_reach'].setdefault(format(threshold,'g'), {'at':stamp,'session_offset':offsets.get(stamp.date())})
    for day in sessions:
        row = daily.get(day, {'session':day,'session_offset':offsets[day], 'mark':None,'mark_available_at':None,'return':None})
        spot = spot_days.get(day)
        # Direction result is separate from option return. Only match a spot
        # close known no later than this option mark; no future spot overlay.
        candidates = [(t,b) for t,b in spots if t.date()==day and row['mark_available_at'] and t<=row['mark_available_at']]
        spot = (candidates[-1][0],float(candidates[-1][1]['close'])) if candidates else None
        row['spot_return'] = spot[1]/spot_entry-1 if spot and spot_entry else None
        row['direction_return'] = row['spot_return'] * (1 if item['option_type']=='CE' else -1) if row['spot_return'] is not None else None
        row['horizon_complete'] = bool(row['mark_available_at'] and row['mark_available_at'].time()>=time(15,15))
        session_ended = as_of >= datetime.combine(day,time(15,30),IST)
        row['data_state'] = ('observed' if row['return'] is not None else
                             'missing' if session_ended else 'pending')
        out['paths'].append(row)
    for h in HORIZONS:
        row = next((r for r in out['paths'] if r['session_offset']==h), None)
        out['horizons'][str(h)] = row if row and row['horizon_complete'] else None
    out['latest'] = next((r for r in reversed(out['paths']) if r['return'] is not None),None)
    out['peak_return'],out['adverse_return'],out['peak_at'] = peak,adverse,peak_at
    out['peak_basis'] = 'observed post-entry 30m extremes; incomplete archive can miss extremes; not executable exits'
    out['status'] = 'expiry_reached' if as_of.astimezone(IST).date()>expiry else 'following'
    out['missing_sessions'] = sum(r['data_state']=='missing' for r in out['paths'])
    out['pending_sessions'] = sum(r['data_state']=='pending' for r in out['paths'])
    return out


MEMBERS = """
SELECT 'next_session' AS lane,i.source_session,i.rank,i.symbol,i.option_type,i.strike,i.expiry,
 i.entry_ts,i.entry_mark,i.source_mark,i.source_mark_ts,r.model_version,r.generated_at AS decision_at,
 COALESCE(i.ranking_score,i.conservative_edge) AS score,
 COALESCE(i.ranking_score,i.conservative_edge)>=i.selection_threshold AS qualified,
 COALESCE((i.exit_analysis->>'entry_on_time')::boolean,true) AS entry_on_time
FROM vanguard_watchlist_items i JOIN vanguard_watchlist_runs r USING(source_session)
UNION ALL
SELECT 'swing',i.source_session,i.rank,i.symbol,i.option_type,i.strike,i.expiry,
 i.entry_ts,i.entry_mark,i.source_mark,i.source_mark_ts,
 r.direction_model_version||' / '||r.contract_model_version,r.decision_at,
 i.combined_score,i.actionable,true
FROM vanguard_swing_watchlist_items i JOIN vanguard_swing_watchlist_runs r USING(source_session)
"""


def refresh(connection, as_of=None, reconcile_all=False):
    as_of = as_of or datetime.now(timezone.utc)
    with connection.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_xact_lock(9070922)')
        if not cur.fetchone()[0]: return {'status':'another_writer_active'}
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT m.* FROM ("+MEMBERS+") m WHERE %s OR m.expiry>=%s OR NOT EXISTS (SELECT 1 FROM vanguard_observation_followup f WHERE f.lane=m.lane AND f.source_session=m.source_session AND f.rank=m.rank)",
                    (reconcile_all, as_of.astimezone(IST).date()-timedelta(days=7)))
        members = [dict(r) for r in cur.fetchall()]
        if not members: return {'observations':0}
        # Revisit every frozen observation, even if its original run is closed.
        # Bounds preserve Timescale partition pruning. No broker fetch involved.
        lo = datetime.combine(min(r['source_session'] for r in members),time(),IST)
        hi = as_of
        contracts = list({(r['symbol'],r['strike'],r['expiry'],r['option_type']) for r in members})
        values = ','.join(cur.mogrify('(%s,%s,%s,%s)',c).decode() for c in contracts)
        cur.execute(f"""WITH c(symbol,strike,expiry,option_type) AS (VALUES {values})
 SELECT DISTINCT ON(o.underlying,o.strike,o.expiry,o.option_type,o.time,o.interval)
 o.underlying,o.strike,o.expiry,o.option_type,o.time,o.interval,o.open,o.high,o.low,o.close
 FROM option_premium_candles o JOIN c ON o.underlying=c.symbol AND o.strike=c.strike
 AND o.expiry=c.expiry AND o.option_type=c.option_type
 WHERE o.time>=%s AND o.time<%s AND o.interval IN ('30minute','3minute')
 ORDER BY o.underlying,o.strike,o.expiry,o.option_type,o.time,o.interval,(o.source='upstox') DESC,o.source,o.synced_at DESC""",(lo,hi))
        options = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT DISTINCT ON(underlying,time) underlying,time,interval,close
 FROM underlying_spot_candles WHERE underlying=ANY(%s) AND time>=%s AND time<%s AND interval='30minute'
 ORDER BY underlying,time,CASE source WHEN 'upstox_spot' THEN 0 WHEN 'upstox_sweep' THEN 1 ELSE 2 END,synced_at DESC""",(list({r['symbol'] for r in members}),lo,hi))
        spots = [dict(r) for r in cur.fetchall()]
    from collections import defaultdict
    op,sp = defaultdict(list),defaultdict(list)
    for r in options: op[(r['underlying'],r['strike'],r['expiry'],r['option_type'])].append(r)
    for r in spots: sp[r['underlying']].append(r)
    records=[]
    for m in members:
        payload=summarize(m,op[(m['symbol'],m['strike'],m['expiry'],m['option_type'])],sp[m['symbol']],as_of)
        # Explicit fractional units; Decimal only crosses JSON as a float.
        def encode(v):
            if hasattr(v,'isoformat'):return v.isoformat()
            return float(v)
        records.append((m['lane'],m['source_session'],m['rank'],m['symbol'],m['expiry'],m['model_version'],json.dumps(payload,default=encode,allow_nan=False)))
    with connection.cursor() as cur:
        psycopg2.extras.execute_values(cur,"""INSERT INTO vanguard_observation_followup
 (lane,source_session,rank,symbol,expiry,model_version,payload) VALUES %s
 ON CONFLICT(lane,source_session,rank) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()""",records)
    return {'observations':len(records),'option_bars':len(options),'spot_bars':len(spots),'mode':'shadow_observation'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--write',action='store_true',required=True);parser.add_argument('--reconcile-all',action='store_true');args=parser.parse_args()
    with psycopg2.connect(os.environ.get('VANGUARD_DATABASE_URL','postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie')) as connection:
        print(json.dumps(refresh(connection,reconcile_all=args.reconcile_all)))
