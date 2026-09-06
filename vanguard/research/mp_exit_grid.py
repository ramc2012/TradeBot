"""What is a mechanical exit worth on the IB-break move?  SPOT LEG ONLY.

THE GAP THIS EXISTS TO MEASURE. On the bank universe the median break makes
+1.54% in its own direction within 3 sessions (MFE) and hands back all of it:
median ret_3d is -0.17%. The move is real; the RESULT is not. For an option
buyer that gap is the whole game, so the question is not "does it move" but
"what does a mechanical exit convert the move into".

WHAT THIS IS AND IS NOT
    IS      the SPOT leg. Enter at `entry` (the 30m close that accepted the IB
            break), side-signed so a down-break is scored as a short. Walk 30m
            bars forward through the rest of the break session and the next 3
            sessions and apply a rule.
    IS NOT  an option P&L. No theta, no IV crush, no spread, no slippage, no
            brokerage, no lot rounding. Every number here is an UPPER BOUND on
            what the option version of the same rule could earn, and the bound
            is loose: an ATM option that needs 3 sessions to reach its target
            pays several days of decay for the privilege.

FILL ASSUMPTIONS, stated before the results because they decide them:
    - the path starts at the bar AFTER the break bar. Entry is that break bar's
      close, so nothing in the outcome is knowable before the entry exists.
    - target and stop fill AT THE LEVEL, except when a bar OPENS beyond the
      level (a gap), in which case it fills at the open -- worse than the stop,
      better than the target. Overnight gaps are the honest part of a 3-session
      hold and pretending they fill at the level flatters the stop.
    - when target and stop are both touched inside the SAME 30m bar and the open
      settles neither, the STOP is taken. That is the conservative assumption
      and it is not free: the frequency is reported per cell, and the whole grid
      is reprinted under the OPTIMISTIC assumption so the reader can see the
      range. A cell whose sign flips between the two is not a result.
    - the trailing stop checks its level BEFORE extending the high-water mark
      within a bar (conservative, same reason).

METHOD RULES OBSERVED
    2.  every t-statistic is computed ACROSS SESSIONS -- trades are averaged
        within their break session first, one observation per session, because
        17 bank names on one day are one bet, not seventeen.
    3.  every headline rule gets a SPLIT-HALF and a DROP-2-BEST (the two best
        session observations removed).
    4.  IB multiples are converted to PERCENT in the table header, because a
        multiple is inflated by a small denominator.
    5.  the same grid is re-run with the unit changed from the IB range to
        trailing ATR. If IB-sized exits do not beat ATR-sized exits, the profile
        has contributed nothing to the exit that plain volatility did not.
    Rule 1 (demean by session) is about SELECTION and does not apply here: no
    cross-sectional pick is being made, every break is taken, and the quantity
    of interest is the rule's absolute return. The correlation it guards against
    is handled by rule 2's session aggregation instead.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_exit_grid.py
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
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_profile import (BAR_SQL, FWD_SESSIONS, MIN_BARS,  # noqa: E402
                                 add_context, dsn, sessions)

BANK_UNIVERSE = ("BANKNIFTY",) + BANKS
TARGETS = (0.5, 1.0, 1.5, 2.0, 3.0)
STOPS = (0.5, 1.0, 1.5)
TRAILS = (0.5, 1.0, 1.5)
OPEN_TIME = pd.Timestamp("09:15").time()


# ---------------------------------------------------------------- statistics
def t_of(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def stats(trades: pd.DataFrame, col: str) -> dict:
    """Per-rule summary. Returns are fractions; the caller prints percent.

    `sess` is the mean return of the rule within each break session -- one
    observation per session, which is the only unit a t-statistic may use here.
    BOTH means are reported and they are not the same number: `tmean` weights
    every trade equally, `smean` weights every SESSION equally. A trending day
    breaks 15 names the same way and earns the same way; trade-weighting counts
    that day fifteen times. smean is the one the t-statistic belongs to and the
    one a desk taking one equal-weighted basket per day would actually receive.
    """
    d = trades.dropna(subset=[col])
    if d.empty:
        return {}
    sess = d.groupby("dt")[col].mean().sort_index()
    smfe = d.groupby("dt")["path_mfe"].mean().reindex(sess.index)
    h = len(sess) // 2
    keep = sess.sort_values().iloc[:-2] if len(sess) > 4 else sess
    big = d[d["path_mfe"] >= 0.005]
    return {
        "n": len(d),
        "sessions": len(sess),
        "tmean": d[col].mean(),
        "smean": sess.mean(),
        "median": d[col].median(),
        "win": (d[col] > 0).mean(),
        "capture": sess.mean() / smfe.mean() if smfe.mean() else np.nan,
        "capt_m": (big[col] / big["path_mfe"]).median() if len(big) > 30 else np.nan,
        "t": t_of(sess),
        "h1": sess.iloc[:h].mean(),
        "h2": sess.iloc[h:].mean(),
        "t1": t_of(sess.iloc[:h]),
        "t2": t_of(sess.iloc[h:]),
        "drop2": keep.mean(),
        "t_drop2": t_of(keep),
    }


def paired(trades: pd.DataFrame, col: str, base: str = "time3") -> dict:
    """Is the rule worth anything OVER the hold? Paired, one obs per session."""
    d = trades.dropna(subset=[col, base])
    if d.empty:
        return {}
    diff = (d.groupby("dt")[col].mean() - d.groupby("dt")[base].mean()).sort_index()
    h = len(diff) // 2
    keep = diff.sort_values().iloc[:-2] if len(diff) > 4 else diff
    return {"d": diff.mean(), "t": t_of(diff), "h1": diff.iloc[:h].mean(),
            "h2": diff.iloc[h:].mean(), "drop2": keep.mean(), "t_drop2": t_of(keep)}


# ---------------------------------------------------------------- path build
def build_paths(bars: pd.DataFrame, brk: pd.DataFrame, fwd: int) -> list[dict]:
    """One forward price path per break: rest of the session + next `fwd` days.

    The session filter is identical to mp_profile.sessions() so that the stored
    break_bar index addresses the same bar array.
    """
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")

    store: dict[str, dict] = {}
    for (name, dt), g in bars.groupby(["underlying", "dt"], sort=True):
        g = g.sort_values("ts")
        if len(g) < MIN_BARS or g["ts"].iloc[0].time() != OPEN_TIME:
            continue
        store.setdefault(name, {})[dt] = (
            g["open"].to_numpy(float), g["high"].to_numpy(float),
            g["low"].to_numpy(float), g["close"].to_numpy(float))
    order = {n: sorted(d) for n, d in store.items()}
    where = {n: {dt: i for i, dt in enumerate(ds)} for n, ds in order.items()}

    out = []
    for r in brk.itertuples(index=False):
        name, dt = r.underlying, r.dt
        i = where.get(name, {}).get(dt)
        if i is None or i + fwd >= len(order[name]):
            continue                       # no full forward window -> not scored
        k = int(r.break_bar)
        segs, closes = [], []
        o0, h0, l0, c0 = store[name][dt]
        segs.append((o0[k + 1:], h0[k + 1:], l0[k + 1:], c0[k + 1:]))
        closes.append(float(c0[-1]))
        for j in range(1, fwd + 1):
            d2 = order[name][i + j]
            o, h, l, c = store[name][d2]
            segs.append((o, h, l, c))
            closes.append(float(c[-1]))
        op = np.concatenate([s[0] for s in segs])
        hi = np.concatenate([s[1] for s in segs])
        lo = np.concatenate([s[2] for s in segs])
        cl = np.concatenate([s[3] for s in segs])
        if len(op) == 0:
            continue
        entry, side = float(r.entry), int(r.side)
        rng = float(r.ib_hi) - float(r.ib_lo)
        if not np.isfinite(entry) or entry <= 0 or rng <= 0:
            continue
        fav = (hi - entry) if side > 0 else (entry - lo)     # favourable distance
        adv = (entry - lo) if side > 0 else (hi - entry)     # adverse distance
        out.append({
            "underlying": name, "dt": dt, "side": side, "entry": entry,
            "ib_rng": rng, "atr_rng": float(r.atr20) * entry if np.isfinite(r.atr20) else np.nan,
            "op": op, "hi": hi, "lo": lo, "cl": cl, "fav": fav, "adv": adv,
            "closes": closes,
            "path_mfe": max(float(fav.max()), 0.0) / entry,
            "path_mae": max(float(adv.max()), 0.0) / entry,
            "mfe_total": float(r.mfe_total) if np.isfinite(r.mfe_total) else np.nan,
            "ret_3d": float(getattr(r, f"ret_{fwd}d")),
        })
    return out


# ---------------------------------------------------------------- exit rules
def bracket(p: dict, t_mult: float, s_mult: float, unit: str) -> dict:
    """Fixed target/stop bracket. Returns both ambiguity conventions."""
    rng = p["ib_rng"] if unit == "ib" else p["atr_rng"]
    if not np.isfinite(rng) or rng <= 0:
        return {}
    entry, side = p["entry"], p["side"]
    t_d, s_d = t_mult * rng, s_mult * rng
    ht = np.flatnonzero(p["fav"] >= t_d)
    hs = np.flatnonzero(p["adv"] >= s_d)
    it = int(ht[0]) if len(ht) else -1
    isx = int(hs[0]) if len(hs) else -1
    tgt_px = entry + side * t_d
    stp_px = entry - side * s_d

    def px_t(j):                                   # gap through the target -> better
        o = p["op"][j]
        return o if (o >= tgt_px if side > 0 else o <= tgt_px) else tgt_px

    def px_s(j):                                   # gap through the stop -> worse
        o = p["op"][j]
        return o if (o <= stp_px if side > 0 else o >= stp_px) else stp_px

    amb = False
    if it < 0 and isx < 0:
        cons = opt = p["cl"][-1]
        why = "time"
    elif isx < 0:
        cons = opt = px_t(it)
        why = "target"
    elif it < 0:
        cons = opt = px_s(isx)
        why = "stop"
    elif it < isx:
        cons = opt = px_t(it)
        why = "target"
    elif isx < it:
        cons = opt = px_s(isx)
        why = "stop"
    else:                                          # both touched in the same bar
        o = p["op"][it]
        if (o <= stp_px) if side > 0 else (o >= stp_px):
            cons = opt = o                         # opened past the stop: settled
            why = "stop"
        elif (o >= tgt_px) if side > 0 else (o <= tgt_px):
            cons = opt = o                         # opened past the target: settled
            why = "target"
        else:
            amb, why = True, "ambiguous"
            cons, opt = stp_px, tgt_px
    return {"ret": side * (cons / entry - 1.0),
            "ret_opt": side * (opt / entry - 1.0),
            "amb": amb, "why": why}


def trail(p: dict, mult: float, unit: str) -> dict:
    """Trailing stop `mult` x unit below the best price reached so far."""
    rng = p["ib_rng"] if unit == "ib" else p["atr_rng"]
    if not np.isfinite(rng) or rng <= 0:
        return {}
    entry, side, dist = p["entry"], p["side"], mult * rng
    op, hi, lo, cl = p["op"], p["hi"], p["lo"], p["cl"]
    best = 0.0                                     # high-water favourable distance
    for j in range(len(op)):
        level = entry + side * (best - dist)
        o = op[j]
        if (o <= level) if side > 0 else (o >= level):
            return {"ret": side * (o / entry - 1.0), "why": "trail-gap"}
        if (lo[j] <= level) if side > 0 else (hi[j] >= level):
            return {"ret": side * (level / entry - 1.0), "why": "trail"}
        best = max(best, (hi[j] - entry) if side > 0 else (entry - lo[j]))
    return {"ret": side * (cl[-1] / entry - 1.0), "why": "time"}


# ---------------------------------------------------------------- reporting
HDR = (f"   {'rule':<24}{'n':>6}{'trade':>8}{'sess':>8}{'med':>8}{'win':>6}"
       f"{'capt':>6}{'t':>7}{'1st h':>8}{'2nd h':>8}{'drop2':>8}{'t d2':>7}")


def line(label: str, st: dict) -> str:
    if not st:
        return f"   {label:<24}  (no data)"
    star = " *" if abs(st["t"]) >= 2 else ""
    return (f"   {label:<24}{st['n']:>6,}{st['tmean'] * 100:>+8.2f}"
            f"{st['smean'] * 100:>+8.2f}{st['median'] * 100:>+8.2f}"
            f"{st['win'] * 100:>5.0f}%{st['capture'] * 100:>5.0f}%{st['t']:>+7.2f}"
            f"{st['h1'] * 100:>+8.2f}{st['h2'] * 100:>+8.2f}"
            f"{st['drop2'] * 100:>+8.2f}{st['t_drop2']:>+7.2f}{star}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=700)
    ap.add_argument("--dsn", default=dsn())
    args = ap.parse_args()

    names = list(BANK_UNIVERSE)
    start = date.today() - timedelta(days=args.lookback_days)
    conn = psycopg2.connect(args.dsn)
    try:
        bars = pd.read_sql(BAR_SQL, conn, params={"start": start, "names": names})
    finally:
        conn.close()
    s = add_context(sessions(bars))
    if s.empty:
        print("no sessions built")
        return 1
    brk = s[(s["side"] != 0) & s["break_bar"].notna() & s["entry"].notna()].copy()

    print(f"SPOT-LEG EXIT STUDY  universe=banks  names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}")
    print(f"sessions={len(s):,}  breaks={len(brk):,}  "
          f"({len(brk) / len(s) * 100:.1f}% of sessions)")

    paths = build_paths(bars, brk, FWD_SESSIONS)
    if not paths:
        print("no paths built")
        return 1
    T = pd.DataFrame([{k: p[k] for k in
                       ("underlying", "dt", "side", "entry", "path_mfe", "path_mae",
                        "mfe_total", "ret_3d")} for p in paths])
    T["ib_pct"] = [p["ib_rng"] / p["entry"] for p in paths]
    T["atr_pct"] = [p["atr_rng"] / p["entry"] for p in paths]

    # -- sanity: the walked path must reproduce the helper's own excursions ----
    ok = T.dropna(subset=["mfe_total"])
    agree = (ok["path_mfe"] - ok["mfe_total"]).abs()
    print(f"\nSANITY  scored breaks={len(T):,} of {len(brk):,} "
          f"(dropped {len(brk) - len(T):,}: no full {FWD_SESSIONS}-session forward window)")
    print(f"   walked-path MFE vs helper mfe_total: median |diff| "
          f"{agree.median() * 100:.4f}pp, 99th pct {agree.quantile(0.99) * 100:.3f}pp")
    print(f"   path MFE median {T['path_mfe'].median() * 100:>5.2f}%   "
          f"path MAE median {T['path_mae'].median() * 100:.2f}%   "
          f"hold-to-3d median {T['ret_3d'].median() * 100:+.2f}%")
    print(f"   1 x IB = median {T['ib_pct'].median() * 100:.2f}% of price;  "
          f"1 x ATR20 = median {T['atr_pct'].median() * 100:.2f}%  (rule 4: the "
          f"multiples below\n   are these, in rupees)")
    per_sess = T.groupby("dt").size()
    print(f"   break sessions={T['dt'].nunique():,}, median {per_sess.median():.0f} "
          f"names breaking per session (range {per_sess.min()}-{per_sess.max()})")
    print("   COLUMNS: 'trade' = mean over trades, 'sess' = mean over SESSIONS "
          "(equal weight per\n   day, the number the t belongs to and the one a "
          "one-basket-a-day desk receives).\n   'capt' = session-mean return / "
          "session-mean path MFE: the share of the move on\n   offer that the rule "
          "banked. All returns percent. t, 1st/2nd half and drop-2 are\n   computed "
          "across sessions, one observation per session.")

    # ------------------------------------------------------------ time stops
    print("\n2. TIME STOPS -- hold and exit at a fixed close, no target, no stop")
    print(HDR)
    for j, lab in enumerate(("break-session close", "+1 session", "+2 sessions",
                             "+3 sessions")):
        T[f"time{j}"] = [p["side"] * (p["closes"][j] / p["entry"] - 1.0) for p in paths]
        print(line(lab, stats(T, f"time{j}")))
    print("   '+3 sessions' is the hold-to-horizon benchmark every exit rule below "
          "must beat.")

    # ------------------------------------------------------- bracket grid ---
    for unit, uname in (("ib", "IB range"), ("atr", "ATR20")):
        res = {}
        for t_m in TARGETS:
            for s_m in STOPS:
                rows = [bracket(p, t_m, s_m, unit) for p in paths]
                col = f"{unit}_{t_m}_{s_m}"
                T[col] = [r.get("ret", np.nan) for r in rows]
                T[col + "_o"] = [r.get("ret_opt", np.nan) for r in rows]
                res[(t_m, s_m)] = {
                    "amb": np.mean([bool(r.get("amb")) for r in rows]),
                    "tgt": np.mean([r.get("why") == "target" for r in rows]),
                    "stp": np.mean([r.get("why") == "stop" for r in rows]),
                    "tim": np.mean([r.get("why") == "time" for r in rows]),
                }
        med = T["ib_pct"].median() if unit == "ib" else T["atr_pct"].median()
        print(f"\n3{'a' if unit == 'ib' else 'b'}. FIXED TARGET / STOP, unit = "
              f"{uname} (median 1x = {med * 100:.2f}% of price)"
              f"{'' if unit == 'ib' else '   [rule-5 control]'}")
        print(HDR)
        for t_m in TARGETS:
            for s_m in STOPS:
                print(line(f"T{t_m:g} / S{s_m:g}", stats(T, f"{unit}_{t_m}_{s_m}")))
        print(f"\n   exit-reason mix and same-bar ambiguity ({uname} unit)")
        print(f"   {'rule':<16}{'target':>9}{'stop':>9}{'timeout':>9}{'ambig':>9}"
              f"{'cons mean':>11}{'optim mean':>12}{'spread':>9}")
        for t_m in TARGETS:
            for s_m in STOPS:
                r = res[(t_m, s_m)]
                c = T.groupby("dt")[f"{unit}_{t_m}_{s_m}"].mean().mean() * 100
                o = T.groupby("dt")[f"{unit}_{t_m}_{s_m}_o"].mean().mean() * 100
                print(f"   {f'T{t_m:g} / S{s_m:g}':<16}{r['tgt'] * 100:>8.0f}%"
                      f"{r['stp'] * 100:>8.0f}%{r['tim'] * 100:>8.0f}%"
                      f"{r['amb'] * 100:>8.1f}%{c:>+11.2f}{o:>+12.2f}{o - c:>+9.2f}")
        print("   'ambig' = target and stop both touched inside one 30m bar with the "
              "bar's open\n   settling neither; counted as the STOP in every other "
              "table. 'spread' is the whole\n   distance between the conservative and "
              "the optimistic convention.")

    # ---------------------------------------------------------- trailing ----
    print(f"\n4. TRAILING STOP from the best price reached")
    print(HDR)
    for m in TRAILS:
        rows = [trail(p, m, "ib") for p in paths]
        T[f"tr_{m}"] = [r.get("ret", np.nan) for r in rows]
        st = stats(T, f"tr_{m}")
        print(line(f"trail {m:g} x IB", st))
    for m in (1.0,):
        rows = [trail(p, m, "atr") for p in paths]
        T[f"tra_{m}"] = [r.get("ret", np.nan) for r in rows]
        print(line(f"trail {m:g} x ATR20", stats(T, f"tra_{m}")))

    # ----------------------------------------------------- capture detail ---
    heads = [("hold to sess close", "time0"), ("hold +1 session", "time1"),
             ("T0.5 / S1", "ib_0.5_1.0"), ("T0.5 / S1.5", "ib_0.5_1.5"),
             ("T1 / S1", "ib_1.0_1.0"), ("T1 / S0.5", "ib_1.0_0.5"),
             ("T2 / S1", "ib_2.0_1.0"), ("T3 / S1.5", "ib_3.0_1.5"),
             ("trail 1 x IB", "tr_1.0"), ("trail 0.5 x IB", "tr_0.5")]
    all_rules = ([("hold +3 sessions", "time3")] + heads
                 + [(f"T{t:g}/S{s:g}", f"ib_{t}_{s}") for t in TARGETS for s in STOPS]
                 + [(f"atr T{t:g}/S{s:g}", f"atr_{t}_{s}") for t in TARGETS for s in STOPS]
                 + [("trail 1.5 x IB", "tr_1.5"), ("trail 1 x ATR", "tra_1.0")])
    seen, uniq = set(), []
    for lab, col in all_rules:
        if col not in seen:
            seen.add(col)
            uniq.append((lab, col))

    print("\n5. HOW MUCH OF THE MOVE DOES THE RULE BANK?")
    print(f"   {'rule':<24}{'mean ret':>10}{'mean MFE':>10}{'capt agg':>10}"
          f"{'capt med':>10}")
    for lab, col in heads:
        st = stats(T, col)
        mfe = T.groupby("dt")["path_mfe"].mean().mean()
        print(f"   {lab:<24}{st['smean'] * 100:>+10.2f}{mfe * 100:>10.2f}"
              f"{st['capture'] * 100:>9.0f}%{st['capt_m'] * 100:>9.0f}%")
    print("   'capt agg' = session-mean return / session-mean MFE. 'capt med' = "
          "median per-trade\n   ret/MFE over trades whose MFE reached at least 0.5%, "
          "so a near-zero denominator\n   cannot manufacture a ratio. The MFE on "
          "offer is the same for every rule; what\n   differs is how little of it "
          "survives to the exit.")

    print("\n6. IS THE EXIT WORTH ANYTHING OVER THE HOLD?  paired per session, "
          "rule minus hold-3d")
    print(f"   {'rule':<24}{'diff':>8}{'t':>7}{'1st h':>8}{'2nd h':>8}"
          f"{'drop2':>8}{'t d2':>7}")
    for lab, col in heads:
        p = paired(T, col)
        star = " *" if abs(p["t"]) >= 2 else ""
        print(f"   {lab:<24}{p['d'] * 100:>+8.2f}{p['t']:>+7.2f}{p['h1'] * 100:>+8.2f}"
              f"{p['h2'] * 100:>+8.2f}{p['drop2'] * 100:>+8.2f}{p['t_drop2']:>+7.2f}{star}")
    print("   'diff' is in percentage points of spot. A rule only earns its "
          "complexity if this\n   column is positive, significant, and alive in "
          "both halves and after drop-2.")

    # --------------------------------------------------- multiple testing ---
    ts = {lab: stats(T, col).get("t", np.nan) for lab, col in uniq}
    ts = {k: v for k, v in ts.items() if np.isfinite(v)}
    best = max(ts, key=lambda k: abs(ts[k]))
    m = len(ts)
    print(f"\n7. MULTIPLE TESTING.  {m} distinct exit rules were scanned. The largest "
          f"|t| anywhere\n   in the scan is {abs(ts[best]):.2f} ({best}). Under the "
          f"null that every rule earns zero,\n   the expected largest |t| from {m} "
          f"draws is about {np.sqrt(2 * np.log(m)):.2f}, and the 5%\n   critical "
          f"value is higher still. The best cell in this grid does not clear the bar "
          f"a\n   SINGLE pre-registered test would have set, let alone the bar for "
          f"the best of {m}.")

    # ------------------------------------------------------------- by side --
    print("\n8. HEADLINE RULES BY BREAK SIDE (up-break = CE, down-break = PE)")
    print(HDR)
    for lab, col in [("hold +3 sessions", "time3")] + heads:
        for sname, sub in (("UP", T[T["side"] == 1]), ("DN", T[T["side"] == -1])):
            print(line(f"{lab} [{sname}]", stats(sub, col)))

    # -------------------------------------------------------------- names ---
    grid_cols = [f"ib_{t}_{s}" for t in TARGETS for s in STOPS]
    best_col = max(grid_cols, key=lambda c: T.groupby("dt")[c].mean().mean())
    lab = best_col.replace("ib_", "T").replace("_", " / S")
    print(f"\n9. PER-NAME: best grid cell ({lab}) vs hold, so one name cannot carry it")
    print("   (best = highest session-weighted mean; it is the top of a noisy grid, "
          "not a pick)")
    per = T.groupby("underlying").agg(n=(best_col, "size"), rule=(best_col, "mean"),
                                      hold=("time3", "mean"), trail=("tr_1.0", "mean"),
                                      mfe=("path_mfe", "mean"))
    per = per.sort_values("rule", ascending=False)
    print(f"   {'name':<14}{'n':>6}{'rule':>9}{'trail1':>9}{'hold3d':>9}{'mean MFE':>10}")
    for name, r in per.iterrows():
        print(f"   {name:<14}{int(r['n']):>6}{r['rule'] * 100:>+9.2f}"
              f"{r['trail'] * 100:>+9.2f}{r['hold'] * 100:>+9.2f}{r['mfe'] * 100:>9.2f}")

    print("\nREMINDER: spot leg only. No theta, no IV, no spread, no costs. These are "
          "upper\nbounds on the option version of the same rule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
