"""
MP + VWAP Strategy — No MACD
==============================

Direction from Market Profile:
  - Day type (TREND_UP/DN, NORMAL_VAR_UP/DN, DOUBLE_DIST, FAILED_AUCTION)
  - IB extension / IB extension failure
  - POC reversion (spot vs previous POC)
  - Buyer/Seller failure composite score

Execution from Option VWAP:
  - Entry: option premium must be ABOVE its intraday VWAP (being accumulated)
  - Exit:  option premium drops BELOW VWAP (control lost) = dynamic stop
  - No entry if premium < VWAP at signal time

Test on SENSEX weekly options, full 246-day dataset.
"""
from __future__ import annotations

import csv, gzip, json, math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_ROOT   = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "mp_vwap"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

UNDERLYING   = "SENSEX"
BUCKET_SIZE  = 50
TPO_MINUTES  = 30
FLOOR_PCT    = -50.0

# ── Data loading ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _load_1m(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df

@lru_cache(maxsize=4)
def _spot_df() -> pd.DataFrame:
    return _load_1m(f"spot/underlying={UNDERLYING}/1minute.csv.gz")

def _load_daily_mp() -> pd.DataFrame:
    path = DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("poc", "vah", "val", "var", "ibh", "ibl", "ibr",
              "session_high", "session_low", "open_price", "close_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


# ── Series descriptors ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Desc:
    series_id:   str
    underlying:  str
    expiry_kind: str
    expiry:      str
    strike:      float
    ce_path:     str
    pe_path:     str
    pair_start:  str

def _build_descs() -> list[Desc]:
    raw = json.loads((DATA_ROOT / "contract_index.json").read_text())
    metas = [m for m in raw.values()
             if m.get("file_path") and m.get("candle_count") and
             m.get("earliest_candle") and m.get("strike") is not None and
             m.get("option_type") and m.get("expiry_kind") == "weekly"
             and m.get("underlying") == UNDERLYING]

    by_group: dict = defaultdict(list)
    for m in metas:
        by_group[(m["underlying"], m["expiry_kind"], m["expiry"])].append(m)

    descs = []
    spot = _spot_df().set_index("time").sort_index()

    for (und, ek, exp), grp in sorted(by_group.items()):
        ce_map = {float(m["strike"]): m for m in grp if m["option_type"] == "CE"}
        pe_map = {float(m["strike"]): m for m in grp if m["option_type"] == "PE"}
        common = sorted(set(ce_map) & set(pe_map))
        if not common:
            continue
        candidates = []
        for st in common:
            ce = ce_map[st]; pe = pe_map[st]
            ps = max(pd.Timestamp(ce["earliest_candle"]), pd.Timestamp(pe["earliest_candle"]))
            pe2 = min(pd.Timestamp(ce["latest_candle"]), pd.Timestamp(pe["latest_candle"]))
            if pe2 > ps:
                candidates.append((st, ps, ce, pe))
        if not candidates:
            continue
        start_day = min(p for _, p, _, _ in candidates).date()
        first_ts = min(p for _, p, _, _ in candidates)
        before = spot.loc[:first_ts]
        if before.empty:
            continue
        sp = float(before.iloc[-1]["close"])
        eligible = [c for c in candidates if c[1].date() == start_day] or candidates
        strike, pair_start, ce_m, pe_m = min(eligible, key=lambda c: (abs(c[0] - sp), c[1], c[0]))
        descs.append(Desc(
            series_id=f"{und}|{ek}|{exp}",
            underlying=und, expiry_kind=ek, expiry=exp,
            strike=float(strike),
            ce_path=ce_m["file_path"], pe_path=pe_m["file_path"],
            pair_start=pair_start.isoformat(),
        ))
    return descs


# ── Day classification ────────────────────────────────────────────────────────

def _classify_day(r) -> str:
    sr = r["session_high"] - r["session_low"]
    ibr = r["ibr"]
    if ibr <= 0 or sr <= 0:
        return "UNKNOWN"
    rr = sr / ibr
    cp = (r["close_price"] - r["session_low"]) / sr
    ib_up, ib_dn = r["ib_broken_up"], r["ib_broken_dn"]
    if (ib_up != ib_dn) and rr >= 2.0:
        if ib_up and cp >= 0.70:
            return "TREND_UP"
        if ib_dn and cp <= 0.30:
            return "TREND_DN"
    if ib_up and ib_dn and rr >= 1.5:
        return "DOUBLE_DIST"
    if (ib_up != ib_dn) and rr >= 1.2:
        return "NORMAL_VAR_UP" if ib_up else "NORMAL_VAR_DN"
    if r["fa_up"] or r["fa_dn"]:
        return "FAILED_AUCTION"
    return "NORMAL"


# ── Buyer/Seller failure score ────────────────────────────────────────────────

def _compute_failure_scores(mp: pd.DataFrame, spot_1m: pd.DataFrame) -> pd.DataFrame:
    """Compute buyer/seller failure scores from MP + TPO profile."""
    spot_by_date = {d: g.reset_index(drop=True)
                    for d, g in spot_1m.groupby(spot_1m["time"].dt.date)}
    mp = mp.copy()
    mp["buyer_fail"] = 0.0
    mp["seller_fail"] = 0.0

    for idx, row in mp.iterrows():
        d = row["date"]
        day_df = spot_by_date.get(d)
        sr = row["session_high"] - row["session_low"]
        ibr = row["ibr"]
        ibh = row["ibh"]
        ibl = row["ibl"]
        if sr <= 0 or ibr <= 0:
            continue

        close_pct = (row["close_price"] - row["session_low"]) / sr
        ib_mid = (ibh + ibl) / 2
        bf = 0.0
        sf = 0.0

        # Failed auction
        if row["fa_up"]:
            bf += 2
        if row["fa_dn"]:
            sf += 2

        # IB extension failure
        if row["ib_broken_up"] and row["close_price"] < ib_mid:
            bf += 2
        if row["ib_broken_dn"] and row["close_price"] > ib_mid:
            sf += 2

        # IB extension reversal (extreme)
        if row["ib_broken_up"] and row["close_price"] < ibl:
            bf += 3
        if row["ib_broken_dn"] and row["close_price"] > ibh:
            sf += 3

        # Close position contradiction
        if row["close_price"] > row["open_price"] and close_pct < 0.30:
            bf += 2  # up day but close in bottom = buyers failed to hold
        if row["close_price"] < row["open_price"] and close_pct > 0.70:
            sf += 2  # down day but close in top = sellers failed to hold

        # TPO tails (need 1-min data)
        if day_df is not None and len(day_df) >= 120:
            tpo_counts = defaultdict(int)
            tpo_periods = day_df.set_index("time").resample(f"{TPO_MINUTES}min",
                          label="left", closed="left").agg({"high": "max", "low": "min"}).dropna()
            for _, trow in tpo_periods.iterrows():
                lo_b = math.floor(float(trow["low"]) / BUCKET_SIZE) * BUCKET_SIZE
                hi_b = math.floor(float(trow["high"]) / BUCKET_SIZE) * BUCKET_SIZE
                b = lo_b
                while b <= hi_b:
                    tpo_counts[b] += 1
                    b += BUCKET_SIZE

            if tpo_counts:
                sorted_b = sorted(tpo_counts.keys())
                # Poor high
                top2 = sorted_b[-2:] if len(sorted_b) >= 2 else sorted_b[-1:]
                if sum(tpo_counts[b] for b in top2) <= 2:
                    bf += 1  # no buying conviction at top
                # Poor low
                bot2 = sorted_b[:2] if len(sorted_b) >= 2 else sorted_b[:1]
                if sum(tpo_counts[b] for b in bot2) <= 2:
                    sf += 1  # no selling conviction at bottom
                # Excess tail high (seller rejection)
                tail_h = 0
                for b in reversed(sorted_b):
                    if tpo_counts[b] <= 1:
                        tail_h += 1
                    else:
                        break
                if tail_h >= 3:
                    bf += 1
                # Excess tail low (buyer rejection)
                tail_l = 0
                for b in sorted_b:
                    if tpo_counts[b] <= 1:
                        tail_l += 1
                    else:
                        break
                if tail_l >= 3:
                    sf += 1

            # Late session reversal
            last_30 = day_df.iloc[-30:]
            last_move = float(last_30["close"].iloc[-1]) - float(last_30["open"].iloc[0])
            day_move = row["close_price"] - row["open_price"]
            if day_move > 0 and last_move < 0 and abs(last_move) > abs(day_move) * 0.3:
                bf += 1
            if day_move < 0 and last_move > 0 and abs(last_move) > abs(day_move) * 0.3:
                sf += 1

        mp.at[idx, "buyer_fail"] = bf
        mp.at[idx, "seller_fail"] = sf

    return mp


# ── VWAP computation on option 1-min data ─────────────────────────────────────

def _compute_intraday_vwap(opt_1m_day: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative VWAP column to a single day of option 1-min data."""
    df = opt_1m_day.copy()
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["tp_vol"] = df["tp"] * df["volume"]
    df["cum_tp_vol"] = df["tp_vol"].cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"] = np.where(df["cum_vol"] > 0,
                          df["cum_tp_vol"] / df["cum_vol"],
                          df["close"])
    return df


# ── MP Signal Generation ──────────────────────────────────────────────────────

def generate_mp_signals(mp: pd.DataFrame) -> list[dict]:
    """
    Generate daily CE/PE/SKIP signals from MP parameters.

    Signal logic (end of day D → trade on day D+1):

    SELL-SIDE FAILED (→ buy CE next day):
      - TREND_UP day                              → strong CE
      - NORMAL_VAR_UP day                         → CE
      - FA_DN (sellers broke IBL, closed inside)  → CE
      - IB extension down failure                 → CE
      - seller_fail score ≥ 4                     → strong CE
      - POC reversion: spot < prev_POC on up day  → CE boost (alloc)

    BUY-SIDE FAILED (→ buy PE next day):
      - TREND_DN day                              → strong PE
      - NORMAL_VAR_DN day                         → PE
      - FA_UP (buyers broke IBH, closed inside)   → PE
      - IB extension up failure                   → PE
      - buyer_fail score ≥ 4                      → strong PE
      - POC reversion: spot > prev_POC on dn day  → PE boost (alloc)

    SKIP:
      - NORMAL day (balanced, no edge)
      - DOUBLE_DIST (both sides active, conflict)
      - Both buyer_fail ≥ 2 AND seller_fail ≥ 2 (conflict)

    Allocation:
      - Base: 20%
      - Strong signal (trend day or fail≥4): 35%
      - POC reversion confirms: +15% boost (cap 35%)
      - Weak/marginal: 10%
    """
    mp = mp.copy().sort_values("date").reset_index(drop=True)
    mp["day_type"] = mp.apply(_classify_day, axis=1)

    # Build prev-day context
    mp["prev_poc"] = mp["poc"].shift(1)
    mp["prev_vah"] = mp["vah"].shift(1)
    mp["prev_val"] = mp["val"].shift(1)
    mp["prev_close"] = mp["close_price"].shift(1)

    sorted_dates = list(mp["date"])
    date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

    signals = []

    for i in range(1, len(mp)):
        row = mp.iloc[i]  # today's MP (computed at EOD)
        d = row["date"]
        day_type = row["day_type"]
        bf = row["buyer_fail"]
        sf = row["seller_fail"]
        conflict = bf >= 2 and sf >= 2

        # Determine signal direction
        direction = None  # CE or PE
        strength = "base"  # base, strong, weak
        reason = ""

        # ── Day type signals ──────────────────────────────────────────
        if day_type == "TREND_UP":
            direction = "CE"
            strength = "strong"
            reason = "TREND_UP"
        elif day_type == "TREND_DN":
            direction = "PE"
            strength = "strong"
            reason = "TREND_DN"
        elif day_type == "NORMAL_VAR_UP":
            direction = "CE"
            strength = "base"
            reason = "NORMAL_VAR_UP"
        elif day_type == "NORMAL_VAR_DN":
            direction = "PE"
            strength = "base"
            reason = "NORMAL_VAR_DN"
        elif day_type == "FAILED_AUCTION":
            # FA direction: which side failed?
            if row["fa_up"] and not row["fa_dn"]:
                direction = "PE"  # buyers failed
                strength = "base"
                reason = "FA_UP"
            elif row["fa_dn"] and not row["fa_up"]:
                direction = "CE"  # sellers failed
                strength = "base"
                reason = "FA_DN"
            else:
                # Both FA → conflict
                reason = "FA_BOTH"
        elif day_type == "DOUBLE_DIST":
            # Both sides active — check failure scores for tiebreak
            if bf >= 4 and sf < 2:
                direction = "PE"
                strength = "base"
                reason = "DD+BF"
            elif sf >= 4 and bf < 2:
                direction = "CE"
                strength = "base"
                reason = "DD+SF"
            else:
                reason = "DD_SKIP"
        elif day_type == "NORMAL":
            # Balanced — check failure scores
            if bf >= 4 and sf < 2:
                direction = "PE"
                strength = "weak"
                reason = "NORM+BF"
            elif sf >= 4 and bf < 2:
                direction = "CE"
                strength = "weak"
                reason = "NORM+SF"
            else:
                reason = "NORM_SKIP"

        # ── IB extension failure override ─────────────────────────────
        ib_mid = (row["ibh"] + row["ibl"]) / 2
        if row["ib_broken_up"] and row["close_price"] < ib_mid:
            # Buyers broke out then failed
            if direction is None or direction == "PE":
                direction = "PE"
                if strength == "weak":
                    strength = "base"
                reason = reason + "+IB_UP_FAIL" if reason else "IB_UP_FAIL"
        if row["ib_broken_dn"] and row["close_price"] > ib_mid:
            # Sellers broke out then failed
            if direction is None or direction == "CE":
                direction = "CE"
                if strength == "weak":
                    strength = "base"
                reason = reason + "+IB_DN_FAIL" if reason else "IB_DN_FAIL"

        # ── Failure score boost ───────────────────────────────────────
        if direction == "PE" and bf >= 4:
            strength = "strong"
            reason += "+BF4"
        if direction == "CE" and sf >= 4:
            strength = "strong"
            reason += "+SF4"

        # ── Conflict filter ───────────────────────────────────────────
        if conflict and day_type not in ("TREND_UP", "TREND_DN"):
            direction = None
            reason += "+CONFLICT"

        if direction is None:
            signals.append({
                "signal_date": d, "direction": "SKIP", "reason": reason,
                "day_type": day_type, "buyer_fail": bf, "seller_fail": sf,
                "alloc": 0, "poc_rev": False,
            })
            continue

        # ── POC reversion (allocation boost) ──────────────────────────
        prev_poc = row.get("prev_poc", 0)
        poc_rev = False
        if prev_poc and prev_poc > 0:
            if direction == "CE" and row["close_price"] < prev_poc:
                poc_rev = True  # spot below prev POC → reversion play for CE
            elif direction == "PE" and row["close_price"] > prev_poc:
                poc_rev = True  # spot above prev POC → reversion play for PE

        # ── Allocation ────────────────────────────────────────────────
        if strength == "strong":
            alloc = 0.35
        elif strength == "base":
            alloc = 0.20
        else:  # weak
            alloc = 0.10

        if poc_rev and alloc < 0.35:
            alloc = min(alloc + 0.15, 0.35)

        signals.append({
            "signal_date": d,
            "direction": direction,
            "reason": reason,
            "day_type": day_type,
            "buyer_fail": bf,
            "seller_fail": sf,
            "alloc": alloc,
            "poc_rev": poc_rev,
            "strength": strength,
        })

    return signals


# ── VWAP-based trade execution ────────────────────────────────────────────────

def execute_vwap_trades(signals: list[dict], descs: list[Desc],
                        mp: pd.DataFrame,
                        vwap_grace_min: int = 60,
                        vwap_persist: int = 5,
                        vwap_cushion_pct: float = 0.0,
                        hard_sl_pct: float = -50.0,
                        target_pct: float = 50.0,
                        label: str = "") -> list[dict]:
    """
    For each MP signal on day D, execute trade on day D+1:
      1. Load ATM CE or PE 1-min data for D+1
      2. Compute intraday VWAP
      3. Entry: first candle where premium > VWAP (after 09:30, min 15 bars)
      4. Exit hierarchy:
         a. Target hit (premium up target_pct %)
         b. Hard SL (premium down hard_sl_pct %) — ACTUAL STOP LOSS
         c. VWAP stop: premium < VWAP*(1-cushion) for vwap_persist consecutive
            candles, BUT only after grace period (vwap_grace_min from entry)
         d. Session close
    """
    mp_dates = sorted(mp["date"].values)
    date_to_next = {}
    for i in range(len(mp_dates) - 1):
        date_to_next[mp_dates[i]] = mp_dates[i + 1]

    date_to_desc = {}
    for desc in descs:
        exp_date = pd.Timestamp(desc.expiry).date()
        pair_date = pd.Timestamp(desc.pair_start).date()
        for d in mp_dates:
            if pair_date <= d <= exp_date:
                if d not in date_to_desc:
                    date_to_desc[d] = desc

    trades = []
    skip_reasons = defaultdict(int)

    for sig in signals:
        if sig["direction"] == "SKIP":
            skip_reasons[sig["reason"]] += 1
            continue

        trade_date = date_to_next.get(sig["signal_date"])
        if trade_date is None:
            continue

        desc = date_to_desc.get(trade_date)
        if desc is None:
            skip_reasons["no_series"] += 1
            continue

        opt_type = sig["direction"]
        path = desc.ce_path if opt_type == "CE" else desc.pe_path

        try:
            opt_1m = _load_1m(path)
        except FileNotFoundError:
            skip_reasons["file_not_found"] += 1
            continue

        opt_1m = opt_1m[opt_1m["time"] >= pd.Timestamp(desc.pair_start)].copy()
        day_data = opt_1m[opt_1m["time"].dt.date == trade_date].copy().reset_index(drop=True)

        if len(day_data) < 30:
            skip_reasons["insufficient_data"] += 1
            continue

        if "volume" not in day_data.columns or day_data["volume"].sum() == 0:
            skip_reasons["no_volume"] += 1
            continue

        day_data = _compute_intraday_vwap(day_data)

        # ── ENTRY: premium > VWAP after 15 min ───────────────────────
        entry_idx = None
        entry_price = None
        entry_time = None
        entry_vwap = None

        for j in range(15, len(day_data)):
            bar = day_data.iloc[j]
            if bar["cum_vol"] < 100:
                continue
            if float(bar["close"]) > float(bar["vwap"]):
                entry_idx = j
                entry_price = float(bar["close"])
                entry_time = bar["time"]
                entry_vwap = float(bar["vwap"])
                break

        if entry_idx is None:
            skip_reasons["no_vwap_entry"] += 1
            continue

        if entry_price <= 1.0:
            skip_reasons["worthless_premium"] += 1
            continue

        # ── EXIT LOGIC ────────────────────────────────────────────────
        target_price = entry_price * (1.0 + target_pct / 100.0)
        sl_price = entry_price * (1.0 + hard_sl_pct / 100.0)
        grace_end = entry_time + pd.Timedelta(minutes=vwap_grace_min)

        below_count = 0
        exit_idx = None
        exit_price = None
        exit_time = None
        exit_reason = "session_close"
        peak_price = entry_price

        for j in range(entry_idx + 1, len(day_data)):
            bar = day_data.iloc[j]
            close_j = float(bar["close"])
            high_j = float(bar["high"])
            low_j = float(bar["low"])
            vwap_j = float(bar["vwap"])
            bar_time = bar["time"]
            peak_price = max(peak_price, high_j)

            # a. Target hit
            if high_j >= target_price:
                exit_idx = j
                exit_price = target_price
                exit_time = bar_time
                exit_reason = "target_hit"
                break

            # b. Hard stop loss
            if low_j <= sl_price:
                exit_idx = j
                exit_price = sl_price
                exit_time = bar_time
                exit_reason = "hard_sl"
                break

            # c. VWAP stop (only after grace period)
            if bar_time >= grace_end:
                vwap_threshold = vwap_j * (1.0 - vwap_cushion_pct / 100.0)
                if close_j < vwap_threshold:
                    below_count += 1
                else:
                    below_count = 0

                if below_count >= vwap_persist:
                    exit_idx = j
                    exit_price = close_j
                    exit_time = bar_time
                    exit_reason = "vwap_stop"
                    break

        # d. Session close
        if exit_idx is None:
            exit_idx = len(day_data) - 1
            exit_bar = day_data.iloc[exit_idx]
            exit_price = float(exit_bar["close"])
            exit_time = exit_bar["time"]

        ret = (exit_price - entry_price) / entry_price * 100.0

        exp_dt = pd.Timestamp(desc.expiry).date()
        tte = (exp_dt - trade_date).days

        trades.append({
            "signal_date": str(sig["signal_date"]),
            "trade_date": str(trade_date),
            "expiry": desc.expiry,
            "opt_type": opt_type,
            "direction": sig["direction"],
            "mp_reason": sig["reason"],
            "day_type": sig["day_type"],
            "alloc": sig["alloc"],
            "poc_rev": sig["poc_rev"],
            "buyer_fail": sig["buyer_fail"],
            "seller_fail": sig["seller_fail"],
            "entry_time": str(entry_time),
            "entry_price": round(entry_price, 2),
            "entry_vwap": round(entry_vwap, 2),
            "exit_time": str(exit_time),
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "blended_return": round(ret, 4),
            "peak_price": round(peak_price, 2),
            "tte": tte,
            "month": pd.Timestamp(trade_date).strftime("%Y-%m"),
            "label": label,
        })

    return trades, dict(skip_reasons)


# ── Compounding ───────────────────────────────────────────────────────────────

def _compound(trades, floor=FLOOR_PCT, start=100_000.0):
    eq = float(start)
    curve = [eq]
    for t in trades:
        ret = max(t["blended_return"], floor)
        eq = eq + eq * t["alloc"] * ret / 100.0
        curve.append(eq)
    return eq, curve


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(trades, skip_reasons, s2_eq=None):
    print("\n" + "=" * 100)
    print("  MP + VWAP STRATEGY RESULTS — SENSEX WEEKLY OPTIONS")
    print("=" * 100)

    if not trades:
        print("  No trades generated.")
        return

    trades_sorted = sorted(trades, key=lambda t: t["entry_time"])
    n = len(trades_sorted)
    rets = [t["blended_return"] for t in trades_sorted]
    wins = sum(1 for r in rets if r > 0)
    losses = sum(1 for r in rets if r < 0)
    wr = wins / n * 100
    avg = np.mean(rets)
    med = np.median(rets)
    cat = sum(1 for r in rets if r < -50)

    eq, curve = _compound(trades_sorted)

    print(f"\n  Trades: {n}  |  Wins: {wins}  Losses: {losses}  |  WR: {wr:.1f}%")
    print(f"  Avg Return: {avg:+.2f}%  |  Median: {med:+.2f}%  |  Catastrophic (<-50%): {cat}")
    print(f"  Final Equity: ₹{eq:,.0f} (₹{eq/1e5:.2f}L from ₹1L)")
    if s2_eq:
        print(f"  S2 MACD baseline: ₹{s2_eq:,.0f} (₹{s2_eq/1e5:.2f}L)")
        print(f"  Difference: {(eq - s2_eq) / s2_eq * 100:+.1f}%")

    # ── By exit reason ────────────────────────────────────────────────
    print(f"\n  BY EXIT REASON:")
    print(f"  {'Reason':<20} {'n':>4} {'WR%':>6} {'Avg%':>8} {'Med%':>8}")
    print("  " + "-" * 50)
    for reason in sorted(set(t["exit_reason"] for t in trades_sorted)):
        sub = [t for t in trades_sorted if t["exit_reason"] == reason]
        sub_wr = sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100
        sub_avg = np.mean([t["blended_return"] for t in sub])
        sub_med = np.median([t["blended_return"] for t in sub])
        print(f"  {reason:<20} {len(sub):>4} {sub_wr:>5.1f}% {sub_avg:>+7.2f}% {sub_med:>+7.2f}%")

    # ── By direction ──────────────────────────────────────────────────
    print(f"\n  BY DIRECTION:")
    print(f"  {'Type':<6} {'n':>4} {'WR%':>6} {'Avg%':>8} {'Cat':>4} {'Equity':>12}")
    print("  " + "-" * 45)
    for ot in ["CE", "PE"]:
        sub = [t for t in trades_sorted if t["opt_type"] == ot]
        if not sub:
            continue
        sub_wr = sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100
        sub_avg = np.mean([t["blended_return"] for t in sub])
        sub_cat = sum(1 for t in sub if t["blended_return"] < -50)
        sub_eq, _ = _compound(sub)
        print(f"  {ot:<6} {len(sub):>4} {sub_wr:>5.1f}% {sub_avg:>+7.2f}% {sub_cat:>4} ₹{sub_eq/1e5:>9.2f}L")

    # ── By MP reason ──────────────────────────────────────────────────
    print(f"\n  BY MP SIGNAL REASON:")
    print(f"  {'Reason':<30} {'n':>4} {'WR%':>6} {'Avg%':>8} {'Alloc Avg':>10}")
    print("  " + "-" * 65)
    reasons = sorted(set(t["mp_reason"] for t in trades_sorted))
    for reason in reasons:
        sub = [t for t in trades_sorted if t["mp_reason"] == reason]
        sub_wr = sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100
        sub_avg = np.mean([t["blended_return"] for t in sub])
        alloc_avg = np.mean([t["alloc"] for t in sub])
        print(f"  {reason:<30} {len(sub):>4} {sub_wr:>5.1f}% {sub_avg:>+7.2f}% {alloc_avg:>9.2f}")

    # ── By month ──────────────────────────────────────────────────────
    print(f"\n  MONTHLY BREAKDOWN:")
    print(f"  {'Month':<9} {'n':>4} {'CE':>3} {'PE':>3} {'WR%':>6} {'Avg%':>8} {'EqΔ%':>8} {'Cat':>4}")
    print("  " + "-" * 55)
    months = sorted(set(t["month"] for t in trades_sorted))
    for m in months:
        sub = [t for t in trades_sorted if t["month"] == m]
        n_ce = sum(1 for t in sub if t["opt_type"] == "CE")
        n_pe = sum(1 for t in sub if t["opt_type"] == "PE")
        sub_wr = sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100
        sub_avg = np.mean([t["blended_return"] for t in sub])
        eq_delta = sum(t["alloc"] * max(t["blended_return"], FLOOR_PCT) / 100.0 for t in sub) * 100
        sub_cat = sum(1 for t in sub if t["blended_return"] < -50)
        print(f"  {m:<9} {len(sub):>4} {n_ce:>3} {n_pe:>3} {sub_wr:>5.1f}% {sub_avg:>+7.2f}% "
              f"{eq_delta:>+7.1f}% {sub_cat:>4}")

    # ── Skip reasons ──────────────────────────────────────────────────
    if skip_reasons:
        print(f"\n  SKIP REASONS:")
        for reason, cnt in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<30} {cnt:>4}")

    # ── VWAP stop effectiveness ───────────────────────────────────────
    print(f"\n  VWAP STOP ANALYSIS:")
    vwap_exits = [t for t in trades_sorted if t["exit_reason"] == "vwap_stop"]
    session_exits = [t for t in trades_sorted if t["exit_reason"] == "session_close"]
    if vwap_exits:
        # How much did VWAP stop save vs holding to session close?
        vwap_avg = np.mean([t["blended_return"] for t in vwap_exits])
        # Peak unrealized return for VWAP exits
        peak_rets = [(t["peak_price"] - t["entry_price"]) / t["entry_price"] * 100 for t in vwap_exits]
        avg_peak = np.mean(peak_rets)
        print(f"    VWAP stop trades: {len(vwap_exits)}, Avg exit return: {vwap_avg:+.2f}%, "
              f"Avg peak before exit: {avg_peak:+.2f}%")
    if session_exits:
        ses_avg = np.mean([t["blended_return"] for t in session_exits])
        print(f"    Session close trades: {len(session_exits)}, Avg return: {ses_avg:+.2f}%")

    return trades_sorted, eq, curve


def plot_dashboard(trades, eq, curve):
    """Generate visualization dashboard."""
    fig = plt.figure(figsize=(22, 16))
    fig.suptitle("MP + VWAP Strategy — SENSEX Weekly Options\nNo MACD | MP Direction + VWAP Entry/Exit",
                 fontsize=14, fontweight="bold")
    gs = gridspec.GridSpec(3, 3, hspace=0.40, wspace=0.30)

    # 1. Equity curve
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(curve, color="#2ecc71", linewidth=2)
    ax1.axhline(100_000, color="gray", ls="--", alpha=0.3)
    ax1.set_title(f"Equity Curve — ₹{eq/1e5:.2f}L from ₹1L", fontweight="bold")
    ax1.set_xlabel("Trade #")
    ax1.set_ylabel("Equity (₹)")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e5:.0f}L"))
    ax1.grid(True, alpha=0.3)

    # 2. Return distribution
    ax2 = fig.add_subplot(gs[1, 0])
    rets = [t["blended_return"] for t in trades]
    ax2.hist(rets, bins=40, color="#3498db", alpha=0.7, edgecolor="white")
    ax2.axvline(0, color="black", ls="-", alpha=0.3)
    ax2.axvline(np.mean(rets), color="red", ls="--", alpha=0.7, label=f"Avg={np.mean(rets):+.1f}%")
    ax2.set_title("Return Distribution", fontweight="bold")
    ax2.set_xlabel("Return %")
    ax2.legend(fontsize=8)

    # 3. By direction
    ax3 = fig.add_subplot(gs[1, 1])
    ce_rets = [t["blended_return"] for t in trades if t["opt_type"] == "CE"]
    pe_rets = [t["blended_return"] for t in trades if t["opt_type"] == "PE"]
    ax3.boxplot([ce_rets, pe_rets], labels=["CE", "PE"])
    ax3.axhline(0, color="gray", ls="--", alpha=0.3)
    ax3.set_title("Returns by Direction", fontweight="bold")
    ax3.set_ylabel("Return %")

    # 4. By exit reason
    ax4 = fig.add_subplot(gs[1, 2])
    reasons = sorted(set(t["exit_reason"] for t in trades))
    reason_avgs = [np.mean([t["blended_return"] for t in trades if t["exit_reason"] == r]) for r in reasons]
    reason_counts = [sum(1 for t in trades if t["exit_reason"] == r) for r in reasons]
    colors = ["#e74c3c" if a < 0 else "#2ecc71" for a in reason_avgs]
    bars = ax4.bar(reasons, reason_avgs, color=colors, alpha=0.85)
    for i, (cnt, avg) in enumerate(zip(reason_counts, reason_avgs)):
        ax4.text(i, avg + 0.5, f"n={cnt}", ha="center", fontsize=8)
    ax4.set_title("Avg Return by Exit Reason", fontweight="bold")
    ax4.set_ylabel("Avg Return %")

    # 5. Monthly equity change
    ax5 = fig.add_subplot(gs[2, 0])
    months = sorted(set(t["month"] for t in trades))
    month_eq = []
    for m in months:
        sub = [t for t in trades if t["month"] == m]
        delta = sum(t["alloc"] * max(t["blended_return"], FLOOR_PCT) / 100.0 for t in sub) * 100
        month_eq.append(delta)
    ax5.bar(months, month_eq, color=["#2ecc71" if v > 0 else "#e74c3c" for v in month_eq], alpha=0.85)
    ax5.set_title("Monthly Equity Change %", fontweight="bold")
    ax5.tick_params(axis="x", labelsize=7, rotation=45)

    # 6. By MP reason
    ax6 = fig.add_subplot(gs[2, 1:])
    reasons_mp = sorted(set(t["mp_reason"] for t in trades))
    r_data = []
    for r in reasons_mp:
        sub = [t for t in trades if t["mp_reason"] == r]
        r_data.append((r, len(sub), np.mean([t["blended_return"] for t in sub]),
                        sum(1 for t in sub if t["blended_return"] > 0) / len(sub) * 100))
    r_data.sort(key=lambda x: -x[2])
    r_names = [d[0][:25] for d in r_data]
    r_avgs = [d[2] for d in r_data]
    r_colors = ["#2ecc71" if a > 0 else "#e74c3c" for a in r_avgs]
    ax6.barh(r_names, r_avgs, color=r_colors, alpha=0.85)
    for i, (name, cnt, avg, wr) in enumerate(r_data):
        ax6.text(avg + 0.3, i, f"n={cnt} WR={wr:.0f}%", va="center", fontsize=7)
    ax6.set_title("Avg Return by MP Signal Reason", fontweight="bold", fontsize=10)
    ax6.set_xlabel("Avg Return %")

    plt.savefig(OUTPUT_ROOT / "mp_vwap_dashboard.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Dashboard: {OUTPUT_ROOT / 'mp_vwap_dashboard.png'}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    print("=" * 100)
    print("  MP + VWAP STRATEGY — NO MACD")
    print("  SENSEX Weekly Options | Apr'25 — Apr'26")
    print("=" * 100)

    # 1. Load data
    print("\n[1] Loading data …")
    descs = _build_descs()
    mp = _load_daily_mp()
    spot_1m = _spot_df()
    print(f"    {len(descs)} weekly series, {len(mp)} trading days, {len(spot_1m)} spot candles")

    # 2. Compute failure scores
    print("\n[2] Computing buyer/seller failure scores …")
    mp = _compute_failure_scores(mp, spot_1m)
    bf_days = (mp["buyer_fail"] >= 2).sum()
    sf_days = (mp["seller_fail"] >= 2).sum()
    print(f"    Buyer failure days (≥2): {bf_days}")
    print(f"    Seller failure days (≥2): {sf_days}")

    # 3. Generate MP signals
    print("\n[3] Generating MP signals …")
    signals = generate_mp_signals(mp)
    trade_sigs = [s for s in signals if s["direction"] != "SKIP"]
    skip_sigs = [s for s in signals if s["direction"] == "SKIP"]
    ce_sigs = sum(1 for s in trade_sigs if s["direction"] == "CE")
    pe_sigs = sum(1 for s in trade_sigs if s["direction"] == "PE")
    print(f"    Total signals: {len(signals)}")
    print(f"    Trade signals: {len(trade_sigs)} (CE={ce_sigs}, PE={pe_sigs})")
    print(f"    Skipped: {len(skip_sigs)}")

    # Show signal distribution
    print(f"\n    Signal distribution by day type:")
    for dt in sorted(set(s["day_type"] for s in signals)):
        sub = [s for s in signals if s["day_type"] == dt]
        trade = sum(1 for s in sub if s["direction"] != "SKIP")
        print(f"      {dt:<20} {len(sub):>4} days → {trade:>3} trade signals")

    # 4. Execute trades with VWAP
    print("\n[4] Executing trades with option VWAP …")
    trades, skip_reasons = execute_vwap_trades(signals, descs, mp)
    print(f"    Executed: {len(trades)} trades")

    # 5. Load S2 baseline for comparison
    print("\n[5] Loading S2 MACD baseline for comparison …")
    tr_path = DATA_ROOT / "staggered_exit" / "trade_results.csv"
    df_tr = pd.read_csv(tr_path)
    s2 = df_tr[(df_tr["underlying"] == UNDERLYING) & (df_tr["strategy"] == "target_50pct")].copy()
    poc_path = DATA_ROOT / "option_mp" / "final_strategy_trades.csv"
    poc_lookup = {}
    if poc_path.exists():
        df_poc = pd.read_csv(poc_path)
        for _, row in df_poc.iterrows():
            poc_lookup[row["entry_time"]] = row["poc_alloc"]
    s2_trades = []
    for _, row in s2.iterrows():
        alloc = poc_lookup.get(row["entry_time"], 0.20)
        s2_trades.append({"blended_return": row["blended_return"], "alloc": alloc})
    s2_eq, _ = _compound(s2_trades)
    print(f"    S2 baseline: {len(s2_trades)} trades, ₹{s2_eq/1e5:.2f}L")

    # 6. Report
    trades_sorted, eq, curve = report(trades, skip_reasons, s2_eq)

    # 7. Save trades
    if trades_sorted:
        csv_path = OUTPUT_ROOT / "mp_vwap_trades.csv"
        keys = sorted(set().union(*(t.keys() for t in trades_sorted)))
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades_sorted)
        print(f"\n  Trades CSV: {csv_path}")

    # 8. Dashboard
    if trades_sorted:
        plot_dashboard(trades_sorted, eq, curve)

    print("\n" + "=" * 100)
    print("  COMPLETE")
    print("=" * 100)

    return trades_sorted


if __name__ == "__main__":
    run()
