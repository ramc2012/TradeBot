"""ATM option-bar loader (contract §1.2, families C/D).

Loads minute bars for ATM CE and PE per underlying/expiry and exposes the CE, PE and
synthetic straddle (CE+PE) series for the strike nearest spot at the session reference time.

File convention (parquet in `data/raw/`):
    upstox_<underlying>_<expiryYYYYMMDD>_<strike>_<CE|PE>.parquet
e.g. upstox_nifty_20250130_22000_CE.parquet

Required columns: timestamp, open, high, low, close, volume, oi. Optional: iv (or greeks
columns) — if absent, `iv` is left NaN and `bs_proxy`/`atr_proxy` gates estimate it.

**Optional at runtime.** The whole option family degrades gracefully: if no files match,
`resolve_atm_series` returns an empty `AtmSeries` (all None) and feature families C/D emit
nulls. The pipeline must never hard-fail on missing option data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.settings import settings
from nomad_sniper.utils.timeutil import IST

log = get_logger()

# Reference time at which we pick the ATM strike for the session (contract §3 / Step 3).
ATM_REFERENCE_TIME = time(9, 20)

_FNAME_RE = re.compile(
    r"^upstox_(?P<underlying>[a-z0-9]+)_(?P<expiry>\d{8})_(?P<strike>\d+)_(?P<opt>CE|PE)$",
    re.IGNORECASE,
)


@dataclass
class AtmSeries:
    """ATM CE/PE/straddle minute series for one underlying+session.

    All fields are None when option data is unavailable (graceful degradation).
    """

    underlying: str
    session_date: date | None = None
    strike: float | None = None
    expiry: date | None = None
    ce: pd.DataFrame | None = None      # IST-indexed OHLCV(+iv,oi)
    pe: pd.DataFrame | None = None
    straddle: pd.Series | None = None   # CE close + PE close, IST-indexed

    @property
    def available(self) -> bool:
        return self.ce is not None and self.pe is not None and not self.ce.empty and not self.pe.empty


def _normalize_option_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    ts_col = None
    for c in ("timestamp", "datetime", "date", "time"):
        if c in df.columns:
            ts_col = c
            break
    if ts_col is not None:
        df[ts_col] = pd.to_datetime(df[ts_col])
        df = df.set_index(ts_col)
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST, ambiguous="raise", nonexistent="raise")
    else:
        df.index = df.index.tz_convert(IST)
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Option bars missing required columns: {sorted(missing)}")
    if "oi" not in df.columns:
        df["oi"] = pd.NA
    if "iv" not in df.columns:
        df["iv"] = pd.NA
    keep = ["open", "high", "low", "close", "volume", "oi", "iv"]
    from nomad_sniper.data.bars import close_stamp
    return close_stamp(df[keep].sort_index())  # start-stamped → bar-close (no current-bar leak)


def _discover_option_files(underlying: str, raw_dir: Path) -> list[tuple[Path, dict]]:
    """Return (path, parsed-metadata) for every ATM option file for this underlying."""
    out = []
    for f in sorted(raw_dir.glob(f"upstox_{underlying.lower()}_*_*_*.parquet")):
        m = _FNAME_RE.match(f.stem)
        if not m:
            continue
        meta = m.groupdict()
        if meta["underlying"].lower() != underlying.lower():
            continue
        out.append((f, meta))
    return out


def resolve_atm_series(
    underlying: str,
    session_date: date,
    bars_underlying: pd.DataFrame,
    *,
    raw_dir: Path | None = None,
    spot_bars: pd.DataFrame | None = None,
) -> AtmSeries:
    """Resolve the ATM CE/PE/straddle series for `underlying` on `session_date`.

    The ATM strike is the one nearest the underlying at `ATM_REFERENCE_TIME` (09:20). When
    `spot_bars` (index spot) is provided it is used as the ATM reference — options are priced
    off index spot, so futures (which carry a basis premium) would pick a strike ~1–2 ticks off.
    Falls back to `bars_underlying` when spot is unavailable.

    Returns an empty `AtmSeries` (available == False) if option files are missing — callers
    must handle this and emit nulls for families C/D.
    """
    raw_dir = raw_dir or settings.raw_dir
    files = _discover_option_files(underlying, raw_dir)
    if not files:
        log.info(f"No ATM option files for {underlying}; option families degrade to null.")
        return AtmSeries(underlying=underlying, session_date=session_date)

    # Reference spot at 09:20 — prefer index spot (matches option pricing) over futures.
    ref_src = spot_bars if spot_bars is not None else bars_underlying
    ref_ts = IST.localize(datetime.combine(session_date, ATM_REFERENCE_TIME))
    today_u = ref_src[ref_src.index.date == session_date]
    at_or_before = today_u[today_u.index <= ref_ts]
    if at_or_before.empty:
        log.warning(f"No underlying bars ≤ 09:20 on {session_date}; cannot resolve ATM strike.")
        return AtmSeries(underlying=underlying, session_date=session_date)
    ref_spot = float(at_or_before["close"].iloc[-1])

    # Nearest expiry ≥ session_date.
    expiries = sorted({datetime.strptime(meta["expiry"], "%Y%m%d").date() for _, meta in files})
    eligible = [e for e in expiries if e >= session_date]
    if not eligible:
        log.info(f"No option expiry ≥ {session_date} for {underlying}; degrade to null.")
        return AtmSeries(underlying=underlying, session_date=session_date)
    expiry = eligible[0]

    # Candidate strikes for that expiry.
    expiry_str = expiry.strftime("%Y%m%d")
    strikes = sorted(
        {int(meta["strike"]) for _, meta in files if meta["expiry"] == expiry_str}
    )
    if not strikes:
        return AtmSeries(underlying=underlying, session_date=session_date)
    atm_strike = min(strikes, key=lambda s: abs(s - ref_spot))

    def _load(opt: str) -> pd.DataFrame | None:
        for f, meta in files:
            if (
                meta["expiry"] == expiry_str
                and int(meta["strike"]) == atm_strike
                and meta["opt"].upper() == opt
            ):
                df = _normalize_option_bars(pd.read_parquet(f))
                return df[df.index.date == session_date]
        return None

    ce = _load("CE")
    pe = _load("PE")
    if ce is None or pe is None or ce.empty or pe.empty:
        log.info(f"Incomplete CE/PE for {underlying} {atm_strike} {expiry}; degrade to null.")
        return AtmSeries(
            underlying=underlying, session_date=session_date,
            strike=float(atm_strike), expiry=expiry,
        )

    # Straddle = CE close + PE close on the shared (inner-joined) timeline.
    joined = ce[["close"]].join(pe[["close"]], lsuffix="_ce", rsuffix="_pe", how="inner")
    straddle = (joined["close_ce"] + joined["close_pe"]).rename("straddle")

    return AtmSeries(
        underlying=underlying,
        session_date=session_date,
        strike=float(atm_strike),
        expiry=expiry,
        ce=ce,
        pe=pe,
        straddle=straddle,
    )


def load_atm_by_underlying(
    underlyings: list[str],
    session_date: date,
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    raw_dir: Path | None = None,
) -> dict[str, AtmSeries]:
    """Convenience: resolve ATM series for several underlyings on one session."""
    out: dict[str, AtmSeries] = {}
    for u in underlyings:
        bars = bars_by_underlying.get(u)
        if bars is None:
            out[u] = AtmSeries(underlying=u, session_date=session_date)
            continue
        out[u] = resolve_atm_series(u, session_date, bars, raw_dir=raw_dir)
    return out
