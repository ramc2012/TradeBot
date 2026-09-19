"""Read-only exact-contract CE/PE benchmark. Hindsight leaders never select trades.

Run inside the VANGUARD container with --session YYYY-MM-DD. Output is JSON;
only an explicitly supplied output artifact is written, never database rows.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras

IST = timezone(timedelta(hours=5, minutes=30))
COST = .01

RUN_SQL = """SELECT * FROM vanguard_watchlist_runs
WHERE source_session >= %(start)s AND track_session <= %(session)s
ORDER BY source_session"""
PREDICTION_SQL = """SELECT p.*, ce.rvol, ce.timing_score, ce.flow_score, ce.rs_z20
FROM vanguard_model_predictions p
LEFT JOIN candidate_evaluations ce ON ce.ts=p.ts AND ce.symbol=p.symbol
WHERE p.ts=%(ts)s AND p.model_version=%(version)s
  AND p.timing_policy='completed_eod_direction_1_2d_v1'
  AND p.source_mark_ts=p.ts"""
QUOTE_SQL = """SELECT DISTINCT ON (underlying,expiry,strike,option_type)
underlying AS symbol,expiry,strike,option_type,close,volume,oi,instrument_key,source
FROM option_premium_candles
WHERE time=%(ts)s AND interval='30minute' AND option_type IN ('CE','PE')
ORDER BY underlying,expiry,strike,option_type,(source='upstox') DESC,source,instrument_key"""


def identity(row):
    return (row['symbol'], str(row['expiry']), float(row['strike']), row['option_type'])


def at(day, hour, minute):
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def quotes(connection, stamp):
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(QUOTE_SQL, {'ts': stamp})
        return {identity(r): dict(r) for r in cur.fetchall() if r['strike'] is not None}


def choose(rows, score='ranking_score', limit=10, liquid=False, balanced=False):
    """Select using source-time fields only; never look at outcome availability."""
    eligible = [r for r in rows if not liquid or (r.get('source_volume') or 0) > 0]
    eligible.sort(key=lambda r: (-float(r.get(score) or 0), r['symbol'], r['option_type']))
    picked, seen, sides = [], set(), defaultdict(int)
    for row in eligible:
        if row['symbol'] in seen or (balanced and sides[row['option_type']] >= limit//2):
            continue
        picked.append(row)
        seen.add(row['symbol'])
        sides[row['option_type']] += 1
        if len(picked) >= limit:
            break
    return picked


def metrics(rows):
    valid = [r for r in rows if r.get('return') is not None]
    values = [r['return']-COST for r in valid]
    return {'selected': len(rows), 'resolved': len(valid),
            'wins': sum(v > 0 for v in values),
            'hit_rate': sum(v > 0 for v in values)/len(values) if values else None,
            'mean_net': sum(values)/len(values) if values else None,
            'ce': sum(r['option_type']=='CE' for r in rows),
            'pe': sum(r['option_type']=='PE' for r in rows)}


def benchmark(connection, session, start=date(2026, 9, 2)):
    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(RUN_SQL, {'start': start, 'session': session})
        runs = [dict(r) for r in cur.fetchall()]
    result = {'session': str(session), 'cost_pct': COST, 'days': [], 'leaders': {},
              'policy': '09:45 to 15:15 IST completed-candle close; exact contracts; shadow only',
              'caveat': 'Archived coverage, not every exchange contract; hindsight leaders are not executable selection skill. Zero chain-snapshot volume is unknown trade activity, not proof of illiquidity. The model target is 1-2-session underlying direction; this benchmark measures one-session option premium.'}
    for run in runs:
        entry = quotes(connection, at(run['track_session'], 9, 15))
        final = quotes(connection, at(run['track_session'], 14, 45))
        source = quotes(connection, run['prediction_ts'])
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(PREDICTION_SQL, {'ts': run['prediction_ts'], 'version': run['model_version']})
            predictions = [dict(r) for r in cur.fetchall()]
            cur.execute('SELECT * FROM vanguard_watchlist_items WHERE source_session=%s ORDER BY rank',
                        (run['source_session'],))
            frozen = [dict(r) for r in cur.fetchall()]
        for row in predictions+frozen:
            key = identity(row)
            e, f, s = entry.get(key), final.get(key), source.get(key)
            row['source_volume'] = s.get('volume') if s else None
            row['source_oi'] = s.get('oi') if s else None
            row['return'] = float(f['close']/e['close']-1) if (
                e and f and e['close'] and e['close']>0 and f['close'] and f['close']>0) else None
            row['entry_volume'] = e.get('volume') if e else None
        day = {'source_session': str(run['source_session']), 'track_session': str(run['track_session']),
               'candidate_symbols': len({r['symbol'] for r in predictions}),
               'candidate_contracts': len(predictions),
               'frozen': metrics(frozen), 'candidates': metrics(predictions),
               'frozen_before_entry': run['generated_at'] <= at(run['track_session'],9,45),
               'by_side': {s: metrics([r for r in predictions if r['option_type']==s]) for s in ('CE','PE')},
               'challengers': {}}
        for name, kwargs in [('margin',{}), ('source_traded',{'liquid':True}),
                              ('median',{'score':'q50_return'}),
                              ('balanced',{'balanced':True}),
                              ('source_traded_balanced',{'liquid':True,'balanced':True})]:
            day['challengers'][name] = metrics(choose(predictions, **kwargs))
        if run['track_session'] == session:
            model_by_key = {identity(r): r for r in predictions}
            frozen_by_key = {identity(r): r for r in frozen}
            market = []
            for key,e in entry.items():
                f=final.get(key)
                # Eligibility is entry-time only; positive volume is a minimal
                # trade-observation check, not bid/ask or depth certification.
                if (not f or e['close'] is None or e['close']<5 or not f['close'] or
                        f['close']<=0 or not e['expiry'] or e['expiry']<session or
                        not e['volume'] or e['volume']<=0):
                    continue
                model = model_by_key.get(key)
                selected = frozen_by_key.get(key)
                market.append({'symbol':e['symbol'],'expiry':str(e['expiry']), 'strike':float(e['strike']),
                               'option_type':e['option_type'],'entry':float(e['close']),
                               'exit':float(f['close']),'return':float(f['close']/e['close']-1),
                               'model_scored':model is not None,
                               'model_score':float(model['ranking_score']) if model else None,
                               'frozen_rank':selected['rank'] if selected else None})
            for side in ('CE','PE'):
                side_rows = sorted([r for r in market if r['option_type']==side], key=lambda r:-r['return'])
                result['leaders'][side] = side_rows[:10]
                result.setdefault('market',{})[side] = metrics(side_rows)
            result['selected'] = [{k:r.get(k) for k in ('rank','symbol','option_type','strike','expiry',
                                    'ranking_score','q50_return','source_volume','entry_volume','return')} for r in frozen]
            result['scored_leaders'] = {s: [{k:r.get(k) for k in ('symbol','option_type','strike','expiry','return','ranking_score')}
                for r in sorted([r for r in predictions if r['option_type']==s and r['return'] is not None],
                                key=lambda r:-r['return'])[:10]] for s in ('CE','PE')}
            selected_keys = {identity(r) for r in frozen}
            result['overlap_at_10'] = {s: sum(identity(r) in selected_keys for r in result['scored_leaders'][s])
                                       for s in ('CE','PE')}
        result['days'].append(day)
    # The split is a diagnostic holdout, not an untouched final model test.
    # No rule or parameter is learned from either partition by this script.
    result['chronological_comparison'] = {}
    for period,predicate in [('earlier',lambda d:d['source_session']<'2026-09-15'),
                             ('later',lambda d:d['source_session']>='2026-09-15')]:
        days=[d for d in result['days'] if predicate(d) and d['candidate_contracts'] and d['frozen_before_entry']]
        aggregates={}
        for name in ('margin','median','balanced','source_traded','source_traded_balanced'):
            cells=[d['challengers'][name] for d in days]
            n=sum(c['resolved'] for c in cells)
            wins=sum(c['wins'] for c in cells)
            aggregates[name]={'sessions':len(days),'selected':sum(c['selected'] for c in cells),
                              'resolved':n,'wins':wins,'hit_rate':wins/n if n else None,
                              'mean_net':sum((c['mean_net'] or 0)*c['resolved'] for c in cells)/n if n else None}
        result['chronological_comparison'][period]=aggregates
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--session',type=date.fromisoformat,required=True)
    parser.add_argument('--output')
    args=parser.parse_args()
    connection=psycopg2.connect(os.environ['VANGUARD_DATABASE_URL'])
    connection.set_session(readonly=True)
    with connection.cursor() as cur:
        cur.execute("SET statement_timeout='90s'")
    try:
        result=benchmark(connection,args.session)
    finally:
        connection.close()
    rendered=json.dumps(result,default=str,indent=2,allow_nan=False)
    if args.output:
        Path(args.output).write_text(rendered+'\n')
    print(rendered)


if __name__=='__main__': main()
