"""Data-readiness validator — scans `data/raw/` and reports exactly what the pipeline can run.

Checks the two file families the pipeline consumes:
  - Underlying futures: ``upstox_<underlying>_fut_<YYYYMMDD>.parquet``  (families A/B/E)
  - ATM options:        ``upstox_<underlying>_<expiryYYYYMMDD>_<strike>_<CE|PE>.parquet`` (C/D)

Reports per-underlying coverage + schema, and the strongest achievable gate mode:
  - ``actual_option`` : ATM CE+PE present across ≥1 expiry
  - ``bs_proxy``      : an IV column exists (on options or underlying) — price via Black-Scholes
  - ``atr_proxy``     : underlying futures only (always available; the graceful-degrade floor)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from nomad_sniper.data.bars import UNDERLYINGS
from nomad_sniper.utils.settings import settings

REQUIRED_BAR_COLS = {"open", "high", "low", "close", "volume"}
_TS_CANDIDATES = ("timestamp", "datetime", "date", "time")

_FUT_RE = re.compile(r"^upstox_(?P<u>[a-z0-9]+)_fut_(?P<expiry>\d{8})$", re.IGNORECASE)
_OPT_RE = re.compile(
    r"^upstox_(?P<u>[a-z0-9]+)_(?P<expiry>\d{8})_(?P<strike>\d+)_(?P<opt>CE|PE)$",
    re.IGNORECASE,
)


@dataclass
class FileReport:
    path: str
    columns: list[str]
    missing_required: list[str]
    has_oi: bool
    has_iv: bool
    rows: int | None
    min_ts: str | None
    max_ts: str | None
    error: str | None = None


@dataclass
class UnderlyingReport:
    underlying: str
    futures_files: list[FileReport] = field(default_factory=list)
    option_files: int = 0
    option_expiries: list[str] = field(default_factory=list)
    option_has_iv: bool = False
    option_ce_pe_complete_expiries: int = 0
    coverage_start: str | None = None
    coverage_end: str | None = None

    @property
    def has_futures(self) -> bool:
        return any(f.error is None and not f.missing_required for f in self.futures_files)


@dataclass
class ReadinessReport:
    raw_dir: str
    underlyings: list[UnderlyingReport]
    recommended_gate: str
    blocking: list[str]
    warnings: list[str]

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _schema_and_coverage(path: Path) -> FileReport:
    try:
        schema = pq.read_schema(path)
        cols = [c.lower() for c in schema.names]
    except Exception as e:  # noqa: BLE001
        return FileReport(str(path), [], sorted(REQUIRED_BAR_COLS), False, False, None, None, None, error=str(e))

    missing = sorted(REQUIRED_BAR_COLS - set(cols))
    ts_col = next((c for c in _TS_CANDIDATES if c in cols), None)
    rows = min_ts = max_ts = None
    try:
        if ts_col is not None:
            actual = next(n for n in schema.names if n.lower() == ts_col)
            s = pd.read_parquet(path, columns=[actual])[actual]
            ts = pd.to_datetime(s)
            rows = int(len(ts))
            min_ts = str(ts.min())
            max_ts = str(ts.max())
        else:
            rows = pq.read_metadata(path).num_rows
    except Exception as e:  # noqa: BLE001
        return FileReport(str(path), cols, missing, "oi" in cols, "iv" in cols, None, None, None, error=str(e))

    return FileReport(str(path), cols, missing, "oi" in cols, "iv" in cols, rows, min_ts, max_ts)


def check_data_readiness(raw_dir: Path | str | None = None) -> ReadinessReport:
    raw_dir = Path(raw_dir) if raw_dir else settings.raw_dir
    reports: dict[str, UnderlyingReport] = {u: UnderlyingReport(underlying=u) for u in UNDERLYINGS}
    warnings: list[str] = []

    if not raw_dir.exists():
        return ReadinessReport(
            raw_dir=str(raw_dir), underlyings=list(reports.values()),
            recommended_gate="none",
            blocking=[f"raw dir does not exist: {raw_dir}"], warnings=[],
        )

    # Futures
    for f in sorted(raw_dir.glob("upstox_*_fut_*.parquet")):
        m = _FUT_RE.match(f.stem)
        if not m:
            continue
        u = m.group("u").lower()
        if u not in reports:
            warnings.append(f"futures file for unknown underlying {u!r}: {f.name}")
            continue
        reports[u].futures_files.append(_schema_and_coverage(f))

    # Options
    opt_acc: dict[str, dict] = {u: {"files": 0, "expiries": set(), "iv": False, "ce": set(), "pe": set()} for u in UNDERLYINGS}
    for f in sorted(raw_dir.glob("upstox_*_*_*_*.parquet")):
        m = _OPT_RE.match(f.stem)
        if not m:
            continue
        u = m.group("u").lower()
        if u not in opt_acc:
            continue
        acc = opt_acc[u]
        acc["files"] += 1
        exp = m.group("expiry")
        acc["expiries"].add(exp)
        try:
            cols = [c.lower() for c in pq.read_schema(f).names]
            if "iv" in cols:
                acc["iv"] = True
        except Exception:  # noqa: BLE001
            pass
        (acc["ce"] if m.group("opt").upper() == "CE" else acc["pe"]).add((exp, m.group("strike")))

    for u, acc in opt_acc.items():
        r = reports[u]
        r.option_files = acc["files"]
        r.option_expiries = sorted(acc["expiries"])
        r.option_has_iv = acc["iv"]
        # an expiry is "complete" if it has at least one matching CE and PE strike
        ce_exp = {e for (e, _) in acc["ce"]}
        pe_exp = {e for (e, _) in acc["pe"]}
        r.option_ce_pe_complete_expiries = len(ce_exp & pe_exp)
        # coverage from futures files
        starts = [f.min_ts for f in r.futures_files if f.min_ts]
        ends = [f.max_ts for f in r.futures_files if f.max_ts]
        r.coverage_start = min(starts) if starts else None
        r.coverage_end = max(ends) if ends else None

    # Gate recommendation + blocking issues
    any_futures = any(r.has_futures for r in reports.values())
    any_actual_option = any(r.option_ce_pe_complete_expiries > 0 for r in reports.values())
    any_iv = any(r.option_has_iv for r in reports.values()) or any(
        any(f.has_iv for f in r.futures_files) for r in reports.values()
    )

    blocking: list[str] = []
    if not any_futures:
        blocking.append(
            "No valid underlying futures files. Add upstox_<underlying>_fut_<YYYYMMDD>.parquet "
            "with columns open/high/low/close/volume (oi optional) to data/raw/."
        )

    for r in reports.values():
        for fr in r.futures_files:
            if fr.error:
                warnings.append(f"{Path(fr.path).name}: read error — {fr.error}")
            elif fr.missing_required:
                warnings.append(f"{Path(fr.path).name}: missing columns {fr.missing_required}")
            elif fr.min_ts is None:
                warnings.append(f"{Path(fr.path).name}: no recognizable timestamp column")

    if any_actual_option:
        recommended = "actual_option"
    elif any_iv:
        recommended = "bs_proxy"
    elif any_futures:
        recommended = "atr_proxy"
    else:
        recommended = "none"

    if recommended == "atr_proxy":
        warnings.append(
            "No ATM option data found → families C/D will be null and the gate runs in "
            "`atr_proxy` mode (underlying-only). This is the intended graceful-degrade path; "
            "add option files later to enable bs_proxy/actual_option."
        )

    return ReadinessReport(
        raw_dir=str(raw_dir),
        underlyings=list(reports.values()),
        recommended_gate=recommended,
        blocking=blocking,
        warnings=warnings,
    )
