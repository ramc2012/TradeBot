# Directional and VANGUARD review — 18 September 2026 IST

Reviewed after the NSE session; implementation and verification finished on 19 September. Paper only. This is a dated session review, not a claim about a subsequent session.

## Results and limitations

| Population | Session result | Qualification |
| --- | --- | --- |
| Directional paper closes | 1 close, net -₹3,176.31 | SENSEX 74500 CE, opened 09:19 IST and closed 09:46 IST on flat signal; gross -₹3,040 and costs ₹136.31 |
| Directional open book | 4 positions; recorded unrealized -₹26,560 | Marks are stale; not reliable closing valuation or daily MTM |
| VANGUARD paper book | ₹0 daily realized; no new emitted tickets | 2,581 candidates gated; lifetime 14 closes, ₹17,165 realized |
| VANGUARD neural next-session shadow list | 10/10 resolved, 4 positive, mean gross +9.8908% | Frozen 17 September, tracked 18 September; equal-weight option returns, not capital-weighted paper P&L |
| Same shadow list, exit replay | Mean net +7.5931% | Existing runner policy with 1% assumed round-trip cost; hold-to-scheduled-close net +8.8908%; one session cannot establish superiority |

Directional lifetime recorded realized is -₹93,303.01 across 13 closes (2 winners). Recorded total including stale unrealized marks is -₹119,863.01. Neither the headline total nor its morning-to-evening change is presented as true daily MTM.

The oldest held observation is MARUTI on 9 September; BAJAJFINSV is from 10 September. TATACONSUM last updated its mark at 09:48 IST and ITC at 14:17 IST on 18 September. Closed-market marks naturally age, but multi-day missing marks are an operational gap.

Of 1,107 Directional decision rows, 868 recorded selected-contract quote staleness. Two approved decision rows are not two trades: the book contains only one new trade. The one SENSEX loss is insufficient to justify a parameter search or disabling the chop regime.

VANGUARD candidate refusals: 1,382 conviction below threshold; 1,160 rank outside top three; 28 wrong actionable universe for the preclose swing lane; 11 without a tradable ATM contract. Gates and sizing were not relaxed.

## Repairs

1. Added Directional's exact held contracts to the shared option subscription refresh. The lane reads timestamped shared ticks before the rotating ATM board; stale/future ticks remain ineligible. No new consumer-side broker downloader, historical execution mark, or forced stale-price exit was added.
2. Added IST-session realized P&L and held-mark freshness fields to the paper summary. The UI distinguishes realized today, open P&L since entry, and provisional totals.
3. Closed VANGUARD runs with missing outcomes now retry for seven calendar days when the exact scheduled 14:45 IST bar arrives. Contract identity and tracking session are fixed; already resolved items are skipped. The repair records reconciliation provenance and retains the existing audit payload.
4. Reconciled 44 missing outcomes across five recent sessions, including all ten for 18 September. A second pass updated zero items. This only updates derived shadow outcomes, never paper trades or frozen list membership.
5. Added `latest_evaluated` to distinguish the newest closed session even when outcomes are incomplete; the performance view no longer silently substitutes an older successful session. The UI names the separate follow-through model cohort.

## Verification

- Backend targeted suite: 60 passed, 24 skipped. Skips are retired commodity tests and cross-worktree VANGUARD source checks unavailable inside the backend container.
- VANGUARD path/exit/longitudinal suite: 30 passed.
- Frontend production build, lint and type checks passed; deployed image/source hashes match.
- Frontend broad tests using bundled Node: 171 passed, 5 failed in unchanged areas (missing v1-orderflow directory, market-canvas query-count expectation, and three index-swing navigation declarations). Container Node 20 cannot directly execute the repository's TypeScript test script. These broader issues were not changed in this task.
- Directional rendered UI showed the correct 18 September realized result, four stale marks, oldest observation, and provisional warning. VANGUARD rendered UI showed the 18 September outcome session, 10/10 coverage, 9.9% shadow return, separate paper P&L, and HOLD SHADOW. Both reported API UP; no feed-liveness claim follows from that badge.
- Backend and strategy processes were restarted; frontend image rebuilt/recreated. A standalone resolver check mapped all four Directional holdings to exact monthly option symbols. It is not evidence of live tick delivery: the NSE market was closed. Fresh shared-tick receipt and protective-exit behavior must still be observed in the next active session.
- API retains `execution_mode=paper` and `allow_live_orders=false`. No model was retrained or promoted, and no gate/size setting was loosened.

Before/after preservation checks matched:

| Dataset | Rows | Digest |
| --- | ---: | --- |
| Directional payloads | 17 | eed42b7ecf20f66134ecc2c33ddc9078 |
| VANGUARD tickets/evidence | 39953 | 65b6b278c146b069d560d2c7fb4dc59c |
| Frozen list session/rank/symbol/contract/model | unchanged | f4f257e0fe506c0d7608476933e5bab3 |

## Reproducible evidence

Runtime sources: `/api/directional-options/paper-summary`, `/api/vanguard/summary`, `/api/vanguard/watchlist?sessions=20`; PostgreSQL database `nomadcurie` in container `nomadcurie_db`.

```sql
select count(*) as closed_today, sum(realized_pnl) as realized_today
from directional_paper_positions
where status='closed'
  and (closed_at at time zone 'Asia/Kolkata')::date='2026-09-18';

select count(*) as resolved,
       count(*) filter(where close_return_pct>0) as winners,
       avg(close_return_pct)*100 as mean_gross_pct,
       avg((exit_analysis->'runner'->>'net_return_pct')::numeric)*100 as runner_mean_net_pct
from vanguard_watchlist_items where source_session='2026-09-17';

select count(*),md5(string_agg(position_id||payload::text,'|' order by position_id))
from directional_paper_positions;
select count(*),md5(string_agg(id::text||evidence::text,'|' order by id)) from tickets;
select md5(string_agg(source_session::text||rank::text||symbol||instrument||model_version,
                     '|' order by source_session,rank))
from vanguard_watchlist_items join vanguard_watchlist_runs using(source_session);
```

The earlier safeguard/UI work was committed and pushed as `b72d8a5c`. New repairs are selectively staged separately; unrelated research and universe edits remain in the working tree.
