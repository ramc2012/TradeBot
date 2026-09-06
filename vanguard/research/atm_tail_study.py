"""Can anything predict a LARGE multi-day move in the ATM contract we'd buy?

WHAT WAS WRONG WITH THE PREVIOUS STUDY (option_momentum_ic.py)
--------------------------------------------------------------------------
Three things, all of which made it answer a question the desk does not ask:

  1. HORIZON. It measured 1-4 thirty-minute bars. Moves of the size that pay
     for an option develop over DAYS, not hours. Horizons here are 1, 2, 3 and
     5 TRADING SESSIONS.

  2. TARGET STATISTIC. It measured mean rank IC. A mean is the wrong statistic
     for a payoff that is decided by a tail: buying options is a lottery-ticket
     distribution, and an indicator that shifts the mean by 3% while never
     touching P(5x) is worthless for this desk. Here the target is the TAIL --
     P(2x), P(4x), P(10x), P(20x) -- reported per indicator decile, alongside
     the median so the asymmetry is visible.

  3. INSTRUMENT. It scored the OI-WEIGHTED aggregate of a whole side of the
     chain. Positions are taken on the ATM PROXY ONLY, and on a chain 20% wide
     a 5% underlying move does not reach most contracts -- so an aggregate that
     averages them dilutes exactly the move being hunted. Here the instrument
     is the single ATM contract, CHOSEN AT t AND HELD, which is the trade.

THE HELD-CONTRACT TARGET, and why it is not a rolling series
--------------------------------------------------------------------------
The INDICATOR is computed on the rolling-ATM premium series (a constant-
moneyness index per side, the standard construction -- each day's ATM, chained).
The TARGET is different on purpose: the specific contract that was ATM on day t
is then HELD, and its own premium h sessions later is what the position earns.
A rolling target would silently swap into a new contract at each step and
measure something nobody can trade.

DAYS TO EXPIRY IS THE CONDITIONING VARIABLE. A 20x on an ATM contract needs a
large underlying move AND enough convexity left to express it, so DTE is
reported as its own cut rather than pooled away -- pooling a 2-day-to-expiry
contract with a 40-day one describes neither.

    python vanguard/research/atm_tail_study.py
    python vanguard/research/atm_tail_study.py --lookback-days 500
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.option_momentum_ic import RSI_PERIOD, macd_and_rsi  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
HORIZONS = (1, 2, 3, 5, 10, 20)             # TRADING SESSIONS
# A 20x is a SERIES-LIFE event, not a 3-session one -- the move needs the whole
# expiry cycle to develop. "life" holds the contract from entry to its LAST
# available price before expiry, which is the trade actually being described.
HOLD_TO_EXPIRY = "life"
MULTIPLES = (2.0, 4.0, 10.0, 20.0)          # the tail we care about
DTE_BUCKETS = ((0, 7), (8, 20), (21, 60))   # sessions to expiry

# ── DATA QUALITY FLOORS, all of them added after the first run's "winners"
# ── turned out to be junk. Without these the cell means are meaningless.
#
# MAX_ATM_DISTANCE: "nearest available strike" is NOT at-the-money. On a sparse
# chain the nearest strike can sit 12% away (PETRONET PE 280 against spot
# 317.5; LT PE 3800 against 4290), and a far-OTM option genuinely does 20x --
# so without this the study measures OTM lottery tickets while claiming to
# measure the ATM proxy the desk actually buys.
MAX_ATM_DISTANCE = 0.03            # |strike - spot| / spot
# MIN_PREMIUM / MIN_INTRINSIC_RATIO: an ATM option with weeks left cannot be
# worth 10 paise. MANAPPURAM CE 235 printed 0.1 against spot 234.3 with 21 days
# to run, then 13 three sessions later -- a 322x that was one bad close, and on
# its own it moved the whole cell's mean. A premium below the floor, or absurdly
# below intrinsic value, is a print to discard rather than a trade to book.
MIN_PREMIUM = 1.0                  # rupees
MIN_PREMIUM_PCT_OF_SPOT = 0.001    # and at least 0.1% of spot
# Liquidity: a fill assumption needs someone on the other side.
MIN_OI = 500.0
MIN_VOLUME = 100.0

# One row per contract per session: last print of the day.
DAILY_SQL = """
WITH daily AS (
    SELECT underlying, expiry, strike, option_type,
           date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
           (array_agg(close ORDER BY time DESC))[1] AS premium,
           (array_agg(oi    ORDER BY time DESC))[1] AS oi,
           SUM(volume) AS volume
    FROM option_premium_candles
    WHERE interval = '30minute' AND close IS NOT NULL
      AND time >= %(start)s
    GROUP BY 1, 2, 3, 4, 5
), spot AS (
    SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
           (array_agg(close ORDER BY time DESC))[1] AS spot
    FROM underlying_spot_candles
    WHERE interval = '30minute' AND time >= %(start)s
    GROUP BY 1, 2
)
SELECT d.underlying, d.dt, d.option_type AS side, d.expiry, d.strike,
       d.premium, d.oi, d.volume, s.spot
FROM daily d
JOIN spot s ON s.underlying = d.underlying AND s.dt = d.dt
WHERE d.expiry >= d.dt AND d.premium > 0 AND s.spot > 0
"""


def load(connection, start: date) -> pd.DataFrame:
    frame = pd.read_sql(DAILY_SQL, connection, params={"start": start})
    for col in ("premium", "oi", "volume", "strike", "spot"):
        frame[col] = frame[col].astype(float)
    return frame


def clean(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop ENTRY rows that cannot be trades. See the DATA QUALITY FLOORS block.

    ENTRY ONLY -- this must never touch the forward leg. Every floor here is
    knowable before the trade (premium, OI, volume at entry), so filtering
    entries is selection, not hindsight. Filtering EXITS would be survivorship
    bias of the worst kind: an option decaying Rs 5 -> Rs 0.30 fails the premium
    floor on the way out, so the loss would be DELETED and only the trades that
    stayed alive would be measured. That inverts the result -- it briefly turned
    the 0-7 DTE bucket from -13.8% into +42%, which is what caught it.
    """
    before = len(frame)
    ok = (
        (frame["premium"] >= MIN_PREMIUM)
        & (frame["premium"] >= MIN_PREMIUM_PCT_OF_SPOT * frame["spot"])
        & (frame["oi"] >= MIN_OI)
        & (frame["volume"] >= MIN_VOLUME)
    )
    # An option cannot trade below intrinsic value by more than a rounding.
    intrinsic = np.where(frame["side"].eq("CE"),
                         (frame["spot"] - frame["strike"]).clip(lower=0.0),
                         (frame["strike"] - frame["spot"]).clip(lower=0.0))
    ok &= frame["premium"] >= 0.5 * intrinsic
    out = frame[ok].copy()
    print(f"  quality floors dropped {before - len(out):,} of {before:,} contract-days "
          f"({(before - len(out)) / before * 100:.1f}%)")
    return out


def pick_atm(frame: pd.DataFrame) -> pd.DataFrame:
    """The FRONT-expiry strike nearest spot -- and GENUINELY near it."""
    frame = frame.copy()
    frame["dte"] = (pd.to_datetime(frame["expiry"]) - pd.to_datetime(frame["dt"])).dt.days
    frame["moneyness"] = (frame["strike"] - frame["spot"]).abs() / frame["spot"]
    frame = frame.sort_values(["underlying", "dt", "side", "dte", "moneyness"])
    atm = frame.groupby(["underlying", "dt", "side"], as_index=False).first()
    before = len(atm)
    atm = atm[atm["moneyness"] <= MAX_ATM_DISTANCE]
    print(f"  ATM distance filter dropped {before - len(atm):,} of {before:,} "
          f"({(before - len(atm)) / before * 100:.1f}%) -- nearest strike was not ATM")
    return atm


def attach_forward(atm: pd.DataFrame, daily: pd.DataFrame,
                   horizons=HORIZONS) -> pd.DataFrame:
    """Forward premium of the HELD contract, h TRADING sessions ahead.

    Sessions, not calendar days: a Friday entry's "1 session" is Monday, and a
    calendar offset would silently drop every weekend crossing.
    """
    calendar = {d: i for i, d in enumerate(sorted(daily["dt"].unique()))}
    daily = daily.copy()
    daily["idx"] = daily["dt"].map(calendar)
    atm = atm.copy()
    atm["idx"] = atm["dt"].map(calendar)

    key = ["underlying", "expiry", "strike", "side"]
    lookup = daily[key + ["idx", "premium"]].rename(columns={"premium": "fwd_premium"})

    # Series life: the contract's final observed premium, and how many sessions
    # away that was. Entries whose series has already been fully observed only.
    last = (daily.sort_values("idx").groupby(key, as_index=False)
            .agg(life_premium=("premium", "last"), life_idx=("idx", "last")))
    atm = atm.merge(last, on=key, how="left")
    atm["life_sessions"] = atm["life_idx"] - atm["idx"]
    atm["ret_life"] = np.where(atm["life_sessions"] > 0,
                               atm["life_premium"] / atm["premium"] - 1.0, np.nan)
    for h in horizons:
        shifted = lookup.copy()
        shifted["idx"] = shifted["idx"] - h        # so it joins onto entry idx
        merged = atm.merge(shifted, on=key + ["idx"], how="left")
        # Return of the position: the same contract, h sessions later.
        # The join is on an EXACT session offset, so a contract missing from the
        # data on that session yields NaN rather than silently reaching further
        # forward -- which is what let a HINDUNILVR "3-session" return actually
        # span 12-08 to 12-23.
        atm[f"ret_{h}"] = merged["fwd_premium"].values / atm["premium"].values - 1.0
    return atm


def indicators(atm: pd.DataFrame) -> pd.DataFrame:
    """MACD/RSI on the ROLLING-ATM premium series, per (underlying, side)."""
    out = []
    for _, group in atm.sort_values("dt").groupby(["underlying", "side"]):
        group = group.reset_index(drop=True)
        block = pd.concat([group, macd_and_rsi(group["premium"])], axis=1)
        block.loc[: RSI_PERIOD - 1, ["rsi", "macd", "macd_hist"]] = np.nan
        out.append(block)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def tail_profile(frame: pd.DataFrame, component: str, h: int,
                 buckets: int = 5) -> pd.DataFrame:
    """P(multiple) and median return by indicator quintile."""
    col = f"ret_{h}"
    d = frame[[component, col]].dropna()
    if len(d) < 500 or d[component].nunique() < buckets:
        return pd.DataFrame()
    d = d.copy()
    d["q"] = pd.qcut(d[component].rank(method="first"), buckets,
                     labels=False, duplicates="drop")
    rows = []
    for q, g in d.groupby("q"):
        row = {"q": int(q), "n": len(g),
               "median_%": round(float(g[col].median()) * 100, 2),
               "mean_%": round(float(g[col].mean()) * 100, 2)}
        for m in MULTIPLES:
            row[f"P>={m:g}x"] = round(float((g[col] >= m - 1.0).mean()) * 100, 3)
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=500)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        daily = load(connection, start)
    finally:
        connection.close()
    if daily.empty:
        print("no rows")
        return 1

    # `daily` (unfiltered) is the forward-price source; only ENTRIES are cleaned.
    atm = pick_atm(clean(daily))
    atm = attach_forward(atm, daily)
    feat = indicators(atm)

    print(f"window {daily['dt'].min()} .. {daily['dt'].max()}   "
          f"contracts={len(daily):,}  ATM rows={len(atm):,}  "
          f"underlyings={atm['underlying'].nunique()}")

    # ── what the tail actually looks like, before any indicator ────────────
    print("\nUNCONDITIONAL: what does holding the ATM proxy actually do?")
    print(f"{'h':>3}{'n':>9}{'median %':>11}{'mean %':>10}"
          + "".join(f"{f'P>={m:g}x':>10}" for m in MULTIPLES))
    for h in HORIZONS:
        col = f"ret_{h}"
        d = feat[col].dropna()
        if d.empty:
            continue
        print(f"{h:>3}{len(d):>9,}{d.median() * 100:>11.2f}{d.mean() * 100:>10.2f}"
              + "".join(f"{(d >= m - 1.0).mean() * 100:>10.3f}" for m in MULTIPLES))

    # ── SELECTION, not description ────────────────────────────────────────
    # The unconditional mean answers "is buying options profitable in general"
    # -- which is theta, and always no. The lane does not trade everything; it
    # SELECTS. So the only question that matters is whether a conditioned
    # subset beats the base rate, and by how much.
    print("\nSELECTION vs BASE RATE — series-life hold (entry to expiry):")
    life = feat.dropna(subset=["ret_life"])
    life = life[life["life_sessions"] >= 5]
    def line(label, d):
        if len(d) < 200:
            print(f"  {label:<38} n={len(d):<7} (too few)")
            return
        print(f"  {label:<38} n={len(d):<7}{d['ret_life'].median() * 100:>9.1f}"
              f"{d['ret_life'].mean() * 100:>10.1f}"
              + "".join(f"{(d['ret_life'] >= m - 1.0).mean() * 100:>9.3f}" for m in MULTIPLES))
    print(f"  {'selector':<38}{'n':<8}{'median %':>8}{'mean %':>10}"
          + "".join(f"{f'P>={m:g}x':>9}" for m in MULTIPLES))
    line("ALL (base rate — trade everything)", life)
    for lo, hi in DTE_BUCKETS:
        sub = life[(life["dte"] >= lo) & (life["dte"] <= hi)]
        if len(sub) >= 200:
            q = sub["rsi"].quantile([0.2, 0.8])
            line(f"DTE {lo}-{hi}", sub)
            line(f"DTE {lo}-{hi} + RSI bottom 20%", sub[sub["rsi"] <= q.iloc[0]])
            line(f"DTE {lo}-{hi} + RSI top 20%", sub[sub["rsi"] >= q.iloc[1]])

    print("\nBy DAYS TO EXPIRY (h=3 sessions held):")
    print(f"{'dte':>10}{'n':>9}{'median %':>11}{'mean %':>10}"
          + "".join(f"{f'P>={m:g}x':>10}" for m in MULTIPLES))
    for lo, hi in DTE_BUCKETS:
        d = feat[(feat["dte"] >= lo) & (feat["dte"] <= hi)]["ret_3"].dropna()
        if d.empty:
            continue
        print(f"{f'{lo}-{hi}d':>10}{len(d):>9,}{d.median() * 100:>11.2f}"
              f"{d.mean() * 100:>10.2f}"
              + "".join(f"{(d >= m - 1.0).mean() * 100:>10.3f}" for m in MULTIPLES))

    # ── THE CONDITIONAL CUT: indicator x days-to-expiry ────────────────────
    # Pooling these describes neither. The tail lives near expiry and the bleed
    # does too, so the only useful question is which INDICATOR state, at which
    # DTE, buys the tail without paying the whole decay for it.
    print("\nrsi quintile x DTE, 3-session held return:")
    print(f"{'dte':>8}{'q':>3}{'n':>8}{'median %':>11}{'mean %':>9}"
          + "".join(f"{f'P>={m:g}x':>9}" for m in MULTIPLES))
    for lo, hi in DTE_BUCKETS:
        sub = feat[(feat["dte"] >= lo) & (feat["dte"] <= hi)]
        d = sub[["rsi", "ret_3"]].dropna()
        if len(d) < 500:
            continue
        d = d.copy()
        d["q"] = pd.qcut(d["rsi"].rank(method="first"), 5, labels=False, duplicates="drop")
        for q, g in d.groupby("q"):
            print(f"{f'{lo}-{hi}d':>8}{int(q):>3}{len(g):>8,}"
                  f"{g['ret_3'].median() * 100:>11.2f}{g['ret_3'].mean() * 100:>9.2f}"
                  + "".join(f"{(g['ret_3'] >= m - 1.0).mean() * 100:>9.3f}" for m in MULTIPLES))

    for component in ("rsi", "macd"):
        for h in (3, 5):
            prof = tail_profile(feat, component, h)
            if prof.empty:
                continue
            print(f"\n{component} quintile vs {h}-session held return "
                  f"(q0 = lowest {component}):")
            print(prof.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
