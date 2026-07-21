"""(4) ANALYSIS-READY OPTION READ LAYER — the reusable artifact of the
HTF-regime study, built FIRST so every other file reads through it.

Purpose: given (underlying, session_date, side) return the tradeable contract
set with deduped bars, modelled-when-missing exit marks, and honest flags.
Research-grade, but the API is shaped so promotion into
backend/directional_options/ later is mechanical (pure functions over frames,
no PG access at query time, no global state).

INHERITED DATA-DEFECT FIXES (each is a hard rule here, never a caller option):

  D1  NO moneyness-band filter at extraction. Bands are computed HERE from the
      spot tape at selection time; the underlying extract carries the full
      strike ladder so winners that run ITM keep their tape.
  D2  NO `underlying_price IS NOT NULL` predicate anywhere. That column is
      unwritten by the live writers (~42% of contracts would vanish).
      Moneyness always comes from `underlying_spot_candles` joined by time.
  D3  Contracts that finish ITM lose their exit tape (the ATM tracker walks
      away). Exit marks with no bar are MODELLED (BS carry of the last real
      IV over the spot at exit; intrinsic floor fallback) and flagged
      `modelled_exit=True` with `model_method` recorded, so the analysis can
      report the stale/modelled-exit rate BY OUTCOME.
  D4  ~20% of bars are cross-broker duplicates. Dedup key is the CONTRACT
      (underlying, expiry, strike, option_type) + bar time — NOT
      instrument_key, which differs per broker. Rule: preferred source order
      upstox > upstox_expired > fyers; ties within a source resolved by
      MAX(volume). Volume is NEVER summed across sources.
  D5  Stock IV/greeks are ~17% populated, OI ~57-71%. Every mark carries
      `iv_present` / `oi_present` so nothing downstream silently assumes them.

API
---
    layer = OptionReadLayer(opt_frame, spot30m_frame)
    cs    = layer.contracts_for("RELIANCE", date(2026,7,15), side="CE")
            # -> DataFrame: contract_id expiry strike option_type band dte mny
    bars  = layer.bars(contract_id)              # deduped 30m bars
    mark  = layer.mark(contract_id, ts_utc)      # -> Mark(price, flags...)

All timestamps UTC (NSE 30m bars: 03:45..09:45 UTC = 09:15..15:15 IST).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

RISK_FREE = 0.065          # annualised, matches greeks_enrichment convention
STALE_TOL_MIN = 45         # a bar older than this at the mark ts => modelled
EOD_BAR_UTC = (9, 45)      # 15:15 IST decision bar
SOURCE_PRIORITY = {"upstox": 0, "upstox_expired": 1, "fyers": 2}

# D4-spot (found while building THIS layer): underlying_spot_candles also
# carries cross-source duplicates — 4 sources coexist per (underlying, time):
# upstox_spot / fyers / source_1minute_aggregate / live_tick. ~65% of rows in
# the older panel_2d3d spot CSVs are duplicate timestamps (those CSVs did not
# SELECT source, so they cannot be deduped by rule — only by max-volume
# proxy). Canonical REST history first, live aggregates last:
SPOT_SOURCE_PRIORITY = {"upstox_spot": 0, "fyers": 1,
                        "source_1minute_aggregate": 2, "live_tick": 3}

# moneyness bands: signed m = (K - S)/S for CE, (S - K)/S for PE  (m<0 = ITM)
BANDS = {
    "ATM":        (-0.0075, 0.0075, 0.0),
    "slight_ITM": (-0.03, -0.0075, -0.018),   # same band as moves_rs study E
}
DTE_MIN, DTE_MAX = 8, 40   # near-month monthly, roll below 8 DTE


# ----------------------------------------------------------------------- util
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, iv: float, cp: str,
             r: float = RISK_FREE) -> float:
    """Plain Black-Scholes (European; NSE stock options are European-settled)."""
    if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
        intr = max(0.0, S - K) if cp == "CE" else max(0.0, K - S)
        return intr
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / sq
    d2 = d1 - sq
    if cp == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


@dataclass(frozen=True)
class Mark:
    price: float
    ts: pd.Timestamp            # the bar actually used (or the model ts)
    bar_exact: bool             # a real deduped bar exists at exactly ts
    stale_minutes: float        # age of the newest real bar at ts (0 if exact)
    modelled_exit: bool         # True => price is model output, not tape
    model_method: str | None    # 'bs_carry_iv' | 'intrinsic_floor' | None
    iv_present: bool            # the bar used (or carried) had broker IV
    oi_present: bool            # the bar used had OI


class OptionReadLayer:
    """Read-only view over a deduped option tape + 30m spot tape."""

    def __init__(self, opt: pd.DataFrame, spot30m: pd.DataFrame,
                 risk_free: float = RISK_FREE):
        """opt: raw extract rows (may contain cross-broker duplicates) with
        columns time underlying expiry strike option_type open high low close
        volume oi iv source. spot30m: time underlying close (30m)."""
        self.r = risk_free
        o = opt.copy()
        o["time"] = pd.to_datetime(o["time"], utc=True)
        o["expiry"] = pd.to_datetime(o["expiry"]).dt.date
        o["strike"] = o["strike"].astype(float)
        # ------------------------- D4: dedup at the CONTRACT level ----------
        if "source" not in o.columns:
            o["source"] = "upstox"
        o["_prio"] = o["source"].map(SOURCE_PRIORITY).fillna(9).astype(int)
        o["_vol"] = pd.to_numeric(o["volume"], errors="coerce").fillna(0)
        o = o.sort_values(["_prio", "_vol"], ascending=[True, False],
                          kind="mergesort")
        o = o.drop_duplicates(
            ["underlying", "expiry", "strike", "option_type", "time"],
            keep="first").drop(columns=["_prio", "_vol"])
        o["contract_id"] = (
            o["underlying"] + "|" + o["expiry"].astype(str) + "|"
            + o["strike"].map(lambda x: f"{x:g}") + "|" + o["option_type"])
        self.opt = o.sort_values(["contract_id", "time"],
                                 kind="mergesort").reset_index(drop=True)
        self._by_contract = dict(tuple(self.opt.groupby("contract_id", sort=False)))

        s = spot30m.copy()
        s["time"] = pd.to_datetime(s["time"], utc=True)
        # D4-spot dedup: by declared source priority when source is present,
        # else max-volume proxy (legacy CSVs without a source column).
        if "source" in s.columns:
            s["_prio"] = s["source"].map(SPOT_SOURCE_PRIORITY).fillna(9)
            s = s.sort_values(["_prio"], kind="mergesort")
        else:
            s["_vol"] = pd.to_numeric(s["volume"], errors="coerce").fillna(0)
            s = s.sort_values(["_vol"], ascending=False, kind="mergesort")
        s = s.drop_duplicates(["underlying", "time"], keep="first")
        s = s.drop(columns=[c for c in ("_prio", "_vol") if c in s.columns])
        s = s.sort_values(["underlying", "time"], kind="mergesort")
        self.spot = s
        self._spot_by_und = dict(tuple(s.groupby("underlying", sort=False)))

    # ------------------------------------------------------------ selection
    def spot_at(self, underlying: str, ts: pd.Timestamp) -> float | None:
        """Last 30m spot close at or before ts (same session preferred)."""
        s = self._spot_by_und.get(underlying)
        if s is None:
            return None
        i = s["time"].searchsorted(ts, side="right") - 1
        if i < 0:
            return None
        row = s.iloc[i]
        if (ts - row["time"]) > timedelta(days=3):
            return None
        return float(row["close"])

    def contracts_for(self, underlying: str, session: date, side: str,
                      bands: tuple[str, ...] = ("ATM", "slight_ITM"),
                      dte_min: int = DTE_MIN, dte_max: int = DTE_MAX,
                      asof: pd.Timestamp | None = None) -> pd.DataFrame:
        """Tradeable contract set for (underlying, session, side).

        side: 'CE' or 'PE'. Moneyness from the SPOT tape at `asof` (default:
        the session's 09:45 UTC / 15:15 IST bar, i.e. selection is causal at
        the decision bar). One contract per band: signed moneyness closest to
        the band centre. Only contracts that have >=1 real bar on `session`
        (a tradeable quote today) are eligible.
        """
        if asof is None:
            asof = pd.Timestamp(datetime(session.year, session.month,
                                         session.day, *EOD_BAR_UTC,
                                         tzinfo=timezone.utc))
        S = self.spot_at(underlying, asof)
        if S is None:
            return pd.DataFrame()
        day0 = pd.Timestamp(session, tz="UTC")
        day1 = day0 + timedelta(days=1)
        o = self.opt
        m = ((o["underlying"] == underlying) & (o["option_type"] == side)
             & (o["time"] >= day0) & (o["time"] < day1))
        today = o.loc[m, ["contract_id", "expiry", "strike", "option_type",
                          "close", "volume", "oi", "iv", "time"]]
        if today.empty:
            return today
        # last bar per contract as the reference quote (causal: <= asof)
        today = today[today["time"] <= asof]
        if today.empty:
            return today
        today = today.sort_values("time").groupby("contract_id").tail(1).copy()
        today["dte"] = today["expiry"].map(lambda e: (e - session).days)
        today = today[(today["dte"] >= dte_min) & (today["dte"] <= dte_max)]
        if today.empty:
            return today
        # nearest eligible monthly expiry only
        exp0 = today["expiry"].min()
        today = today[today["expiry"] == exp0].copy()
        sign = 1.0 if side == "CE" else -1.0
        today["mny"] = sign * (today["strike"] - S) / S
        out = []
        for b in bands:
            lo, hi, mid = BANDS[b]
            cand = today[(today["mny"] >= lo) & (today["mny"] < hi)].copy()
            if cand.empty:
                continue
            cand["band"] = b
            cand["dist"] = (cand["mny"] - mid).abs()
            out.append(cand.sort_values("dist").head(1))
        if not out:
            return pd.DataFrame()
        r = pd.concat(out, ignore_index=True).drop(columns=["dist", "time"])
        r["spot_asof"] = S
        r["iv_present"] = r["iv"].notna()
        r["oi_present"] = r["oi"].notna() & (pd.to_numeric(
            r["oi"], errors="coerce").fillna(0) > 0)
        return r.reset_index(drop=True)

    # ----------------------------------------------------------------- bars
    def bars(self, contract_id: str) -> pd.DataFrame:
        """Full deduped 30m bar tape for one contract."""
        b = self._by_contract.get(contract_id)
        if b is None:
            return pd.DataFrame()
        return b.reset_index(drop=True)

    # ----------------------------------------------------------------- mark
    def mark(self, contract_id: str, ts: pd.Timestamp) -> Mark | None:
        """Price the contract at ts. Real bar if fresh enough, else MODELLED
        (D3). Never silently carries a stale bar past STALE_TOL_MIN."""
        b = self._by_contract.get(contract_id)
        if b is None or b.empty:
            return None
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        und, exp_s, strike_s, cp = contract_id.split("|")
        expiry = date.fromisoformat(exp_s)
        strike = float(strike_s)

        i = b["time"].searchsorted(ts, side="right") - 1
        if i < 0:
            return None                       # marked before the tape starts
        row = b.iloc[i]
        age_min = (ts - row["time"]).total_seconds() / 60.0
        iv_p = pd.notna(row.get("iv"))
        oi_p = pd.notna(row.get("oi")) and float(row.get("oi") or 0) > 0
        if age_min <= 0.5:
            return Mark(float(row["close"]), row["time"], True, 0.0, False,
                        None, iv_p, oi_p)
        if age_min <= STALE_TOL_MIN:
            return Mark(float(row["close"]), row["time"], False, age_min,
                        False, None, iv_p, oi_p)

        # ------- modelled exit (D3): the tape walked away from this contract
        S = self.spot_at(und, ts)
        intrinsic = None
        if S is not None:
            intrinsic = max(0.0, S - strike) if cp == "CE" else max(0.0, strike - S)
        T_now = max(0.0, ((datetime(expiry.year, expiry.month, expiry.day,
                                    10, 0, tzinfo=timezone.utc)
                           - ts.to_pydatetime()).total_seconds()
                          / (365.0 * 86400.0)))
        # carry the last REAL broker IV forward if any bar of this tape had one
        iv_tape = b.loc[b["time"] <= ts, "iv"].dropna()
        if S is not None and not iv_tape.empty and T_now > 0:
            px = bs_price(S, strike, T_now, float(iv_tape.iloc[-1]), cp, self.r)
            return Mark(px, ts, False, age_min, True, "bs_carry_iv",
                        True, oi_p)
        if intrinsic is not None:
            # no usable IV anywhere on the tape: intrinsic floor. Honest but
            # BIASED LOW for the buyer's exit; flagged so the analysis can
            # bound results between floor and last-real-bar carry.
            return Mark(intrinsic, ts, False, age_min, True,
                        "intrinsic_floor", False, oi_p)
        return None


# --------------------------------------------------------------- convenience
def load_spot_csvs(paths: list[str]) -> pd.DataFrame:
    """Concatenate 30m spot CSV extracts into one DEDUPED frame.

    Handles both the new extracts (with a `source` column -> priority dedup)
    and the legacy panel_2d3d CSVs (no source -> max-volume proxy). The
    output has exactly one row per (underlying, time) — the input contract
    for regime_defs/timer_defs.
    """
    fr = [pd.read_csv(p) for p in paths]
    s = pd.concat(fr, ignore_index=True)
    s["time"] = pd.to_datetime(s["time"], utc=True)
    if "source" in s.columns:
        s["_k"] = s["source"].map(SPOT_SOURCE_PRIORITY).fillna(9)
        s = s.sort_values("_k", kind="mergesort")
    else:
        s["_k"] = -pd.to_numeric(s["volume"], errors="coerce").fillna(0)
        s = s.sort_values("_k", kind="mergesort")
    return (s.drop_duplicates(["underlying", "time"], keep="first")
            .drop(columns=["_k"])
            .sort_values(["underlying", "time"], kind="mergesort")
            .reset_index(drop=True))


def load_opt_extracts(paths: list[str]) -> pd.DataFrame:
    fr = [pd.read_csv(p) for p in paths]
    return pd.concat(fr, ignore_index=True)
