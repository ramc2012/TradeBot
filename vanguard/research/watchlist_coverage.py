"""Did the swing watchlist pick the market's movers? Re-run this every session.

The question the lane exists to answer is "top ten CE and top ten PE
opportunities". This measures the answer against what the market actually did:
for each source session, every resolvable contract in the day's own universe is
ranked by its realised next-session return, and the published picks are located
inside that ranking.

Measured 2026-09-15 over five sessions (04, 08, 09, 10, 11-Sep):

  * ZERO top-10 hits on either side, in any session.
  * Picks sat at the 32nd-72nd percentile of their own side's ranking.
  * Winners were cheap convexity — |delta| 0.11-0.25 at Rs 10-60 — on names that
    moved 3-5%. The picks carried |delta| 0.44-0.58.
  * NAME selection was the binding constraint, not strike: buying ~0.20 delta on
    the same names would have changed the median result by -3.6% to +0.8%,
    because those names did not move.
  * 10-20 of 20 picks could not be marked at the exit bar at all on four of the
    five sessions, so every published performance number was computed on a
    survivorship-biased subsample. `held_position_candles` now maintains these
    contracts; the `unmarkable` line below is how that fix is verified.

Nothing here writes. It reads the same tables the lane publishes.

    docker exec nomadcurie_vanguard_cycle python research/watchlist_coverage.py
    docker exec nomadcurie_vanguard_cycle python research/watchlist_coverage.py --sessions 20
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from statistics import median

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DSN = os.environ.get(
    "VANGUARD_DATABASE_URL", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie"
)

UNIVERSE_SQL = """
WITH entry AS (
  SELECT DISTINCT ON (underlying, expiry, strike, option_type)
         underlying, expiry, strike, option_type, close px, oi, delta
  FROM option_premium_candles
  WHERE interval='30minute' AND time=%s AND close>=5 AND expiry>=%s
    AND option_type IN ('CE','PE')
  ORDER BY underlying, expiry, strike, option_type,
           (source='upstox') DESC, source, synced_at DESC
), exit_bar AS (
  SELECT DISTINCT ON (underlying, expiry, strike, option_type)
         underlying, expiry, strike, option_type, close px
  FROM option_premium_candles
  WHERE interval='30minute' AND time=%s
  ORDER BY underlying, expiry, strike, option_type,
           (source='upstox') DESC, source, synced_at DESC
)
SELECT entry.*, exit_bar.px exit_px, (exit_bar.px/entry.px - 1.0) ret
FROM entry JOIN exit_bar USING (underlying, expiry, strike, option_type)
WHERE entry.px > 0
"""

SPOT_SQL = """
SELECT DISTINCT ON (underlying) underlying, close
FROM underlying_spot_candles
WHERE interval='30minute' AND time=%s
ORDER BY underlying, (source='upstox') DESC, source, synced_at DESC
"""


def _med(values, default=None):
    return median(values) if values else default


def _sessions(cursor, limit: int) -> list[dict]:
    cursor.execute(
        """SELECT source_session, min(entry_ts) entry_ts
           FROM vanguard_swing_watchlist_items
           GROUP BY 1 HAVING min(entry_ts) IS NOT NULL
           ORDER BY 1 DESC LIMIT %s""",
        (limit,),
    )
    return list(reversed(cursor.fetchall()))


def _exit_bar(cursor, entry_ts):
    cursor.execute(
        """SELECT min((time AT TIME ZONE 'Asia/Kolkata')::date) d
           FROM option_premium_candles
           WHERE interval='30minute' AND time > %s AND time < %s
             AND (time AT TIME ZONE 'Asia/Kolkata')::date
                 > (%s AT TIME ZONE 'Asia/Kolkata')::date""",
        (entry_ts, entry_ts + timedelta(days=6), entry_ts),
    )
    nxt = cursor.fetchone()["d"]
    if nxt is None:
        return None
    cursor.execute(
        """SELECT max(time) t FROM option_premium_candles
           WHERE interval='30minute' AND time >= %s AND time < %s
             AND (time AT TIME ZONE 'Asia/Kolkata')::date = %s""",
        (entry_ts, entry_ts + timedelta(days=6), nxt),
    )
    return cursor.fetchone()["t"]


def report(dsn: str, limit: int) -> int:
    connection = psycopg2.connect(dsn)
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    for row in _sessions(cursor, limit):
        session, entry_ts = row["source_session"], row["entry_ts"]
        exit_ts = _exit_bar(cursor, entry_ts)
        if exit_ts is None:
            print(f"\n=== {session}: next session not available yet")
            continue
        cursor.execute(UNIVERSE_SQL, (entry_ts, session, exit_ts))
        universe = cursor.fetchall()
        cursor.execute(SPOT_SQL, (entry_ts,))
        spot_in = {r["underlying"]: float(r["close"]) for r in cursor.fetchall() if r["close"]}
        cursor.execute(SPOT_SQL, (exit_ts,))
        spot_out = {r["underlying"]: float(r["close"]) for r in cursor.fetchall() if r["close"]}
        moves = {k: spot_out[k] / spot_in[k] - 1.0 for k in spot_in if k in spot_out and spot_in[k]}
        cursor.execute(
            """SELECT symbol, option_type, strike, expiry, combined_score
               FROM vanguard_swing_watchlist_items WHERE source_session=%s""",
            (session,),
        )
        chosen = cursor.fetchall()
        keys = {(c["symbol"], c["option_type"], float(c["strike"]), c["expiry"]) for c in chosen}
        print(f"\n=== {session}  entry {entry_ts:%H:%M}Z -> exit {exit_ts:%d-%b %H:%M}Z "
              f"| universe {len(universe)} contracts | spots {len(moves)}")

        marked = sum(
            1 for c in chosen
            if (c["symbol"], c["option_type"], float(c["strike"]), c["expiry"])
            in {(u["underlying"], u["option_type"], float(u["strike"]), u["expiry"]) for u in universe}
        )
        print(f"  markable at BOTH ends: {marked}/{len(chosen)} picks")

        for side in ("CE", "PE"):
            rows = sorted([u for u in universe if u["option_type"] == side], key=lambda r: -r["ret"])
            if not rows:
                continue
            n = len(rows)
            picks = [(i, r) for i, r in enumerate(rows, 1)
                     if (r["underlying"], side, float(r["strike"]), r["expiry"]) in keys]
            top10 = rows[:10]
            decile = max(1, n // 10)
            print(f"  {side}: resolved {len(picks)} | top-10 hits {sum(1 for i, _ in picks if i <= 10)}"
                  f" | top-decile {sum(1 for i, _ in picks if i <= decile)}"
                  f" | median pctile {_med([100 * (1 - (i - 1) / n) for i, _ in picks], 0):.0f}th"
                  f" | picked med {100 * _med([r['ret'] for _, r in picks], 0):+.1f}%"
                  f" | universe med {100 * median([r['ret'] for r in rows]):+.1f}%"
                  f" | top-10 mean {100 * sum(r['ret'] for r in top10) / 10:+.1f}%")
            print(f"      winners: premium Rs{_med([r['px'] for r in top10]):.0f}, |delta| "
                  f"{_med([abs(float(r['delta'])) for r in top10 if r['delta'] is not None], float('nan')):.2f}"
                  f", underlying move "
                  f"{100 * _med([moves[r['underlying']] for r in top10 if r['underlying'] in moves], 0):+.2f}%")
            if not picks:
                continue
            print(f"      ours:    premium Rs{_med([r['px'] for _, r in picks]):.0f}, |delta| "
                  f"{_med([abs(float(r['delta'])) for _, r in picks if r['delta'] is not None], float('nan')):.2f}"
                  f", underlying move "
                  f"{100 * _med([moves[r['underlying']] for _, r in picks if r['underlying'] in moves], 0):+.2f}%")
            if moves:
                order = sorted(moves.items(), key=lambda kv: -kv[1] if side == "CE" else kv[1])
                rank = {name: i for i, (name, _) in enumerate(order, 1)}
                name_pct = [100 * (1 - (rank[r["underlying"]] - 1) / len(order))
                            for _, r in picks if r["underlying"] in rank]
                print(f"      name quality: median {_med(name_pct, 0):.0f}th pctile of {len(order)} names "
                      f"(100th = best mover for this side)")
            best_on_name: dict = {}
            for r in rows:
                if r["underlying"] not in best_on_name or r["ret"] > best_on_name[r["underlying"]]["ret"]:
                    best_on_name[r["underlying"]] = r
            gaps = [100 * (best_on_name[r["underlying"]]["ret"] - r["ret"])
                    for _, r in picks if r["underlying"] in best_on_name]
            print(f"      contract gap: {_med(gaps, 0):+.1f}% median vs the best strike on the same name")
            alt = []
            for _, r in picks:
                same = [x for x in rows if x["underlying"] == r["underlying"] and x["delta"] is not None]
                if same:
                    pick = min(same, key=lambda x: abs(abs(float(x["delta"])) - 0.20))
                    alt.append(100 * (pick["ret"] - r["ret"]))
            if alt:
                print(f"      ~0.20 delta on the same names: {_med(alt):+.1f}% median difference")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--sessions", type=int, default=10, help="most recent N source sessions")
    args = parser.parse_args()
    return report(args.dsn, args.sessions)


if __name__ == "__main__":
    raise SystemExit(main())
