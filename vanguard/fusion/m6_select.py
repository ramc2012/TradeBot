"""M6 -- Fusion & Trade Selection.

Combines M2 (flow_score), M3 (GEX regime), M4 (sector20 RS + lead-lag), M5
(timing) into one candidate list and, subject to M7's risk gate, a handful
of trade tickets. Per the spec's own framing (doctrine #2: "The system's
default answer is NO TRADE. It must justify acting, never justify
waiting.") this is expected to emit ZERO tickets on most evaluations -- see
the empirical skip-rate this module itself prints on every run.

CADENCE, and why the four inputs are joined the way they are: features_flow
and sector_rs/leadlag are computed once per SESSION (EOD); regime and timing
are computed per 30-MINUTE bar intraday. M6 evaluates at each timing bar
(the intraday trigger) and joins the most recent EOD readings available
BEFORE that bar's own day -- never that day's own not-yet-computed EOD
figures. This mirrors the exact no-lookahead join m5_timing.py's own
sector_direction_as_of() already uses for sector_rs; M6 applies the same
discipline to features_flow and leadlag too.

CANDIDATE FILTER, direction-first (spec Section 3 M6, literal):
  direction   := sign(flow_score)                      -- bullish if > 0
  |flow_score| >= 60                                     (M2)
  sign(rs_z20) == direction AND |rs_z20| >= 1             (M4, "sector RS confirms")
  regime permits: a GEX regime bucket is a market-STRUCTURE reading, not a
    view-direction one -- negative/neutral dealer gamma is momentum-
    friendly regardless of which way the candidate itself points (positive
    gamma dampens realized moves; negative gamma amplifies them, per the
    spec's own M3 policy hook: "momentum/breakout candidates require
    regime <= NEUTRAL"). Vanguard's signals here (IV spread/skew, RVOL
    surge, IGNITION) are unambiguously momentum-type, so regime permits
    when the bucket is STRONG_NEG/NEG/NEUTRAL, not POS/STRONG_POS.
  timing fires: timing_score >= 70 AND timing_state == 'IGNITION'          (M5)

CONVICTION -- each component rescaled to [0,100] BEFORE the spec's own
0.35/0.20/0.20/0.15/0.10 weights, direction-aligned so a confirming bearish
reading scores the same as a confirming bullish one:
  flow      := |flow_score|                                    (already 0-100)
  sectorRS  := (clip(direction * rs_z20, -3, 3) + 3) / 6 * 100
  timing    := timing_score                                    (already 0-100)
  regime    := {STRONG_NEG:100, NEG:75, NEUTRAL:50, POS:25, STRONG_POS:0}[bucket]
               (direction-agnostic -- a structural reading, not a view)
  leadlag   := 50 if best_lag <= 0 else 50 + min(50, corr*100)
               ("a bonus" per spec -- neutral baseline, lift only for a
               genuine positive-lag laggard, which is the case spec's own
               M4 acceptance note calls out as the interesting one)
  conviction = 0.35*flow + 0.20*sectorRS + 0.20*timing + 0.15*regime + 0.10*leadlag

GATE: conviction >= 85 AND rank <= 3 (by conviction, within this bar's
candidate set) AND M7.risk_check() allows. Every candidate that clears the
FILTER is recorded in `tickets` with emitted=false and a gated_reason when
it fails the GATE or risk check -- an audit trail of near-misses, not just
winners; doctrine #5 ("everything measurable").

INSTRUMENT/PRICING: Vanguard trades the underlying's current ATM option
(CE for bullish, PE for bearish) -- see the scope note in fusion/m7_risk.py.
entry is the option's own last traded premium (from option_premium_candles,
same EOD-dedup discipline as m2_flow.load_chain_eod); stop/targets are
percent-of-premium, using the 15/20/10 (stop/trail-activation/trail)
configuration this SAME research programme found materially outperforms a
naive 30% stop on real August 2026 option data (see MACD mini's own
exit-overlay research, paired t=10.4) -- target1 is the trail-activation
level (+20%), target2 a further +50% extension. This is doctrine-compliant:
a percentage is a ratio, not a raw price.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fusion.m7_risk import load_risk_state, risk_check  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

FLOW_MIN_ABS = 60.0
SECTOR_RS_MIN_ABS_Z = 1.0
TIMING_MIN_SCORE = 70.0
REGIME_PERMITS = {"STRONG_NEG", "NEG", "NEUTRAL"}
REGIME_SCORE = {"STRONG_NEG": 100.0, "NEG": 75.0, "NEUTRAL": 50.0, "POS": 25.0, "STRONG_POS": 0.0}
WEIGHTS = {"flow": 0.35, "sector_rs": 0.20, "timing": 0.20, "regime": 0.15, "leadlag": 0.10}
CONVICTION_MIN = 85.0
TOP_N_PER_BAR = 3
STOP_PCT = 0.15
TARGET1_PCT = 0.20
TARGET2_PCT = 0.50
MIN_PREMIUM = 5.0


@dataclass
class Candidate:
    symbol: str
    ts: datetime
    direction: str
    flow_score: float
    rs_z20: float
    sector20: str | None
    regime: str | None
    timing_score: float
    timing_state: str
    best_lag: int | None
    corr: float | None
    conviction: float
    components: dict


def load_candidates_at(connection, as_of_ts: datetime) -> list[Candidate]:
    day = as_of_ts.date()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tm.symbol, tm.timing_score, tm.timing_state,
                   fl.flow_score,
                   sr.rs_z20, st.sector20,
                   rg.regime,
                   ll.best_lag, ll.corr
            FROM timing tm
            LEFT JOIN sector_taxonomy st ON st.symbol = tm.symbol
            LEFT JOIN LATERAL (
                SELECT flow_score FROM features_flow f
                WHERE f.symbol = tm.symbol AND f.ts::date < %(day)s
                ORDER BY f.ts DESC LIMIT 1
            ) fl ON true
            LEFT JOIN LATERAL (
                SELECT rs_z20 FROM sector_rs s
                WHERE s.sector20 = st.sector20 AND s.ts::date < %(day)s
                ORDER BY s.ts DESC LIMIT 1
            ) sr ON true
            LEFT JOIN LATERAL (
                SELECT regime FROM regime r
                WHERE r.symbol = tm.symbol AND r.ts <= tm.ts
                ORDER BY r.ts DESC LIMIT 1
            ) rg ON true
            LEFT JOIN LATERAL (
                SELECT best_lag, corr FROM leadlag l
                WHERE l.symbol = tm.symbol AND l.dt < %(day)s
                ORDER BY l.dt DESC LIMIT 1
            ) ll ON true
            WHERE tm.ts = %(ts)s
              AND fl.flow_score IS NOT NULL
              AND ABS(fl.flow_score) >= %(flow_min)s
            """,
            {"ts": as_of_ts, "day": day, "flow_min": FLOW_MIN_ABS},
        )
        rows = cursor.fetchall()

    candidates = []
    for (symbol, timing_score, timing_state, flow_score, rs_z20, sector20,
         regime, best_lag, corr) in rows:
        if flow_score is None:
            continue
        direction = "bullish" if flow_score > 0 else "bearish"
        sign = 1.0 if direction == "bullish" else -1.0

        if rs_z20 is None or abs(rs_z20) < SECTOR_RS_MIN_ABS_Z or (rs_z20 > 0) != (sign > 0):
            continue  # sector RS must confirm, same direction, |z| >= 1
        if regime not in REGIME_PERMITS:
            continue
        if timing_state != "IGNITION" or timing_score is None or timing_score < TIMING_MIN_SCORE:
            continue

        aligned_rs = max(-3.0, min(3.0, sign * float(rs_z20)))
        sector_rs_component = (aligned_rs + 3.0) / 6.0 * 100.0
        regime_component = REGIME_SCORE.get(regime, 50.0)
        if best_lag is not None and best_lag > 0 and corr is not None:
            leadlag_component = 50.0 + min(50.0, abs(float(corr)) * 100.0)
        else:
            leadlag_component = 50.0
        components = {
            "flow": abs(float(flow_score)),
            "sector_rs": sector_rs_component,
            "timing": float(timing_score),
            "regime": regime_component,
            "leadlag": leadlag_component,
        }
        conviction = sum(WEIGHTS[k] * v for k, v in components.items())

        candidates.append(Candidate(
            symbol=symbol, ts=as_of_ts, direction=direction,
            flow_score=float(flow_score), rs_z20=float(rs_z20), sector20=sector20,
            regime=regime, timing_score=float(timing_score), timing_state=timing_state,
            best_lag=best_lag, corr=float(corr) if corr is not None else None,
            conviction=conviction, components=components,
        ))
    return sorted(candidates, key=lambda c: -c.conviction)


def resolve_instrument(connection, symbol: str, direction: str, as_of_ts: datetime):
    """The underlying's current front-series ATM CE/PE: symbol, premium, lot_size.

    EXPIRY IS THE PRIMARY SORT KEY, and expired/expiring series are excluded
    outright (`expiry > ts::date`). Without both, an adversarial review
    confirmed two real mis-selections on live data: for RELIANCE at
    2026-07-28 the nearest strike existed ONLY on the series expiring that
    same afternoon, resolving a near-worthless 0.80 contract that MIN_PREMIUM
    then rejected -- killing a candidate for which a perfectly tradable
    next-series contract existed; and at another spot the query returned the
    SEPTEMBER contract in preference to August purely on an arbitrary
    timestamp tie, pricing an intraday 30-minute signal off a far-month
    option. Rolling off the expiring series matches the sibling MACD mini
    project's own rollover rule.

    The spot query is pinned to interval='30minute' for the same reason the
    chain query is: underlying_spot_candles stores five intervals, so an
    unpinned "latest close" is whichever interval happened to print last --
    a nondeterministic spot, which moves the ATM strike."""
    option_type = "CE" if direction == "bullish" else "PE"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.underlying, s.close AS spot
            FROM underlying_spot_candles s
            WHERE s.underlying = %(symbol)s AND s.time <= %(ts)s
              AND s.interval = '30minute'
            ORDER BY s.time DESC LIMIT 1
            """,
            {"symbol": symbol, "ts": as_of_ts},
        )
        row = cursor.fetchone()
        spot = float(row[1]) if row else None
        if spot is None:
            return None

        cursor.execute(
            """
            SELECT strike, close, expiry
            FROM option_premium_candles
            WHERE underlying = %(symbol)s AND option_type = %(option_type)s
              AND interval = '30minute'
              AND time <= %(ts)s AND time >= %(ts)s - interval '2 days'
              AND close IS NOT NULL
              AND expiry > %(ts)s::date
            ORDER BY expiry ASC, ABS(strike - %(spot)s) ASC, time DESC
            LIMIT 1
            """,
            {"symbol": symbol, "option_type": option_type, "ts": as_of_ts, "spot": spot},
        )
        chain_row = cursor.fetchone()
        if chain_row is None:
            return None
        strike, premium, expiry = chain_row

        cursor.execute("SELECT lot_size FROM fo_underlying_catalog WHERE symbol = %(symbol)s",
                       {"symbol": symbol})
        lot_row = cursor.fetchone()
        lot_size = int(lot_row[0]) if lot_row and lot_row[0] else None

    if premium is None or float(premium) < MIN_PREMIUM or lot_size is None:
        return None
    return {
        "instrument": f"{symbol}{expiry.strftime('%y%b').upper()}{int(strike)}{option_type}",
        "premium": float(premium), "strike": float(strike), "expiry": expiry,
        "option_type": option_type, "lot_size": lot_size,
    }


def build_tickets(connection, as_of_ts: datetime, capital: float) -> list[dict]:
    candidates = load_candidates_at(connection, as_of_ts)
    state = load_risk_state(connection, as_of_ts, capital)
    results = []
    for rank, candidate in enumerate(candidates, start=1):
        row = {
            "ts": as_of_ts, "symbol": candidate.symbol, "direction": candidate.direction,
            "conviction": candidate.conviction, "rank_in_session": rank,
            "regime_at_ts": candidate.regime,
            "evidence": {
                "flow_score": candidate.flow_score, "rs_z20": candidate.rs_z20,
                "timing_score": candidate.timing_score, "timing_state": candidate.timing_state,
                "best_lag": candidate.best_lag, "corr": candidate.corr,
                "component_scores": candidate.components,
            },
        }
        if candidate.conviction < CONVICTION_MIN:
            row.update(emitted=False, gated_reason=f"conviction {candidate.conviction:.1f} < {CONVICTION_MIN}")
            results.append(row)
            continue
        if rank > TOP_N_PER_BAR:
            row.update(emitted=False, gated_reason=f"rank {rank} > top-{TOP_N_PER_BAR}")
            results.append(row)
            continue

        instrument = resolve_instrument(connection, candidate.symbol, candidate.direction, as_of_ts)
        if instrument is None:
            row.update(emitted=False, gated_reason="no tradable ATM contract resolved")
            results.append(row)
            continue

        entry = instrument["premium"]
        stop = round(entry * (1 - STOP_PCT), 4)
        target1 = round(entry * (1 + TARGET1_PCT), 4)
        target2 = round(entry * (1 + TARGET2_PCT), 4)
        sizing = risk_check(
            state, connection, symbol=candidate.symbol, sector20=candidate.sector20,
            entry_premium=entry, stop_premium=stop, lot_size=instrument["lot_size"],
            as_of=as_of_ts.date(),
        )
        row.update(
            instrument=instrument["instrument"],
            strike=instrument["strike"], option_type=instrument["option_type"],
            expiry=instrument["expiry"], lot_size=instrument["lot_size"],
            entry_zone_low=round(entry * 0.98, 4), entry_zone_high=round(entry * 1.02, 4),
            stop=stop, target1=target1, target2=target2,
        )
        if not sizing.allowed:
            row.update(emitted=False, gated_reason=f"M7: {sizing.reason}")
            results.append(row)
            continue
        row.update(
            emitted=True, gated_reason=None,
            sizing_lots=sizing.lots, sizing_notional=sizing.notional,
            sizing_risk_rupees=sizing.risk_rupees, sizing_method=sizing.method,
        )
        results.append(row)
        # An emitted ticket consumes risk budget for the REST of this bar's
        # evaluation -- refresh state's open_positions view so a second
        # candidate at the same bar cannot double-count the same headroom.
        state.open_positions.append({
            "ticket_id": -1, "symbol": candidate.symbol, "sector20": candidate.sector20,
            "risk_rupees": sizing.risk_rupees,
        })
        state.__post_init__()
    return results


def persist_tickets(connection, rows: list[dict]) -> list[int]:
    ids = []
    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute(
                """INSERT INTO tickets
                   (ts, symbol, instrument, direction, entry_zone_low, entry_zone_high,
                    stop, target1, target2, conviction, rank_in_session, regime_at_ts,
                    evidence, sizing_lots, sizing_notional, sizing_risk_rupees, sizing_method,
                    emitted, gated_reason, strike, option_type, expiry, lot_size)
                   VALUES (%(ts)s, %(symbol)s, %(instrument)s, %(direction)s,
                           %(entry_zone_low)s, %(entry_zone_high)s, %(stop)s, %(target1)s, %(target2)s,
                           %(conviction)s, %(rank_in_session)s, %(regime_at_ts)s,
                           %(evidence)s, %(sizing_lots)s, %(sizing_notional)s,
                           %(sizing_risk_rupees)s, %(sizing_method)s, %(emitted)s, %(gated_reason)s,
                           %(strike)s, %(option_type)s, %(expiry)s, %(lot_size)s)
                   RETURNING id""",
                {
                    "instrument": row.get("instrument"), "entry_zone_low": row.get("entry_zone_low"),
                    "entry_zone_high": row.get("entry_zone_high"), "stop": row.get("stop"),
                    "target1": row.get("target1"), "target2": row.get("target2"),
                    "sizing_lots": row.get("sizing_lots"), "sizing_notional": row.get("sizing_notional"),
                    "sizing_risk_rupees": row.get("sizing_risk_rupees"), "sizing_method": row.get("sizing_method"),
                    "evidence": psycopg2.extras.Json(row["evidence"]),
                    "strike": row.get("strike"), "option_type": row.get("option_type"),
                    "expiry": row.get("expiry"), "lot_size": row.get("lot_size"),
                    **{k: row[k] for k in ("ts", "symbol", "direction", "conviction", "rank_in_session",
                                            "regime_at_ts", "emitted", "gated_reason")},
                },
            )
            ids.append(cursor.fetchone()[0])
    return ids


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ts", type=datetime.fromisoformat, default=None,
                        help="ISO timestamp to evaluate; default = latest timing bar")
    parser.add_argument("--capital", type=float, default=None,
                        help="default = M9's own evolving paper equity (paper_capital_daily), "
                             "not a static number -- sizing should track what the account "
                             "actually has, not always reset to a fixed figure")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        as_of_ts = args.ts
        if as_of_ts is None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT MAX(ts) FROM timing")
                as_of_ts = cursor.fetchone()[0]
        print(f"evaluating M6 at ts={as_of_ts.isoformat()}")

        capital = args.capital
        if capital is None:
            from paper.engine import current_capital   # local: paper.engine imports this module
            capital = current_capital(connection, as_of_ts.date())
        print(f"capital: Rs{capital:,.0f}")

        rows = build_tickets(connection, as_of_ts, capital)
        emitted = [r for r in rows if r["emitted"]]
        print(f"candidates evaluated (filter passed): {len(rows)}")
        print(f"tickets emitted: {len(emitted)}  (skip rate {100*(1-len(emitted)/max(1,len(rows))):.1f}% "
              f"of filtered candidates, {100*(1-len(emitted)/218):.1f}% of the ~218-symbol universe)")
        for row in rows:
            tag = "EMIT" if row["emitted"] else "skip"
            print(f"  [{tag}] {row['symbol']:<14} {row['direction']:<8} "
                  f"conviction={row['conviction']:5.1f} rank={row['rank_in_session']}"
                  + (f"  -- {row['gated_reason']}" if row.get("gated_reason") else
                     f"  {row.get('instrument')} lots={row.get('sizing_lots')} "
                     f"risk=Rs{row.get('sizing_risk_rupees', 0):,.0f}"))
        if args.write and rows:
            ids = persist_tickets(connection, rows)
            print(f"wrote {len(ids)} ticket rows (ids {ids[0]}..{ids[-1]})")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
