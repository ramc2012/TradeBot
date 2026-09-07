"""The job that builds and persists the index vol substrate.

One pass per index per bar: fit the surface, pull the fixed coordinates, match
each tenor against realised vol, and write everything including what was
refused.  Safe to re-run on a bar — every write is an upsert keyed on the bar.

Run it directly for a backfill:

    python -m directional_options.vol.builder --bars 40
    python -m directional_options.vol.builder --underlying NIFTY --bars 200
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.vol.realized import (
    DEFAULT_CONE_WINDOWS,
    DailySeries,
    VolCone,
    build_cone,
    realized_context,
    variance_risk_premium,
)
from directional_options.vol.series import (
    DEFAULT_TENORS_DAYS,
    CMTenor,
    extract_cm_series,
    front_coordinates,
)
from directional_options.vol.store import ensure_tables, persist_snapshot
from directional_options.vol.surface import (
    INDEX_UNIVERSE,
    MIN_BAR_STRIKES,
    SOURCE_INTERVAL,
    build_snapshot_from_rows,
    load_chain_bar,
)

DEFAULT_RV_ESTIMATOR = "garman_klass"

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class BuildResult:
    underlying: str
    bar_ts: datetime | None
    status: str
    reason: str = ""
    written: dict[str, int] = field(default_factory=dict)
    front_tenor_days: int | None = None
    atm_iv: float | None = None
    vrp_spread: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "bar_ts": self.bar_ts.isoformat() if self.bar_ts else None,
            "status": self.status,
            "reason": self.reason,
            "written": self.written,
            "front_tenor_days": self.front_tenor_days,
            "atm_iv": self.atm_iv,
            "vrp_spread": self.vrp_spread,
        }


# Bars are enumerated with the same breadth floor the live path uses, so a
# backfill never spends time on the ATM-tracker-only tail bars.
_BARS_SQL = text(
    """
    SELECT time AS ts
    FROM option_premium_candles
    WHERE underlying = :underlying
      AND interval = :interval
      AND time >= :lower
      AND time <= :upper
    GROUP BY time
    HAVING count(DISTINCT strike) >= :min_strikes
    ORDER BY time DESC
    LIMIT :limit
    """
)


async def usable_bars(
    underlying: str,
    *,
    limit: int = 1,
    as_of: datetime | None = None,
    lookback_days: int = 30,
    interval: str = SOURCE_INTERVAL,
    min_strikes: int = MIN_BAR_STRIKES,
) -> list[datetime]:
    upper = as_of or datetime.now(timezone.utc)
    lower = upper - timedelta(days=lookback_days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _BARS_SQL,
                {
                    "underlying": underlying,
                    "interval": interval,
                    "lower": lower,
                    "upper": upper,
                    "min_strikes": int(min_strikes),
                    "limit": int(limit),
                },
            )
        ).all()
    return [row.ts for row in rows]


@dataclass
class _AsOfCache:
    """Realised context sliced to what was knowable before each session.

    The first version of this builder loaded the realised context ONCE per
    underlying and reused it for every bar of a backfill.  `realized_vol` then
    took `bars[-window:]` — the most recent bars of the WHOLE series — so every
    historical bar was stamped with future realised vol.  It showed in the data:
    index_vol_tenor_metrics.realized_vol for NIFTY was 0.05942 identically on
    2026-08-31, 09-02, 09-03 and 09-04, and that number fed vrp_risk_multiplier,
    so every replayed position was sized on information from after its own
    entry.

    Slicing is by SESSION DATE and strictly less-than: a bar inside session D
    may use realised vol through D-1 and no further.
    """

    series: DailySeries
    estimator: str
    windows: Sequence[int] = DEFAULT_CONE_WINDOWS
    _by_session: dict[date, tuple[DailySeries, VolCone]] = field(default_factory=dict)

    def for_session(self, session_date: date) -> tuple[DailySeries, VolCone]:
        cached = self._by_session.get(session_date)
        if cached is not None:
            return cached
        bars = [b for b in self.series.bars if b.day < session_date]
        sliced = DailySeries(underlying=self.series.underlying, bars=bars, rejected=self.series.rejected)
        cone = build_cone(self.series.underlying, bars, windows=self.windows, estimator=self.estimator)
        self._by_session[session_date] = (sliced, cone)
        return sliced, cone


async def build_bar(
    underlying: str,
    bar_ts: datetime,
    *,
    series: DailySeries,
    cone: VolCone,
    tenors_days: Sequence[int] = DEFAULT_TENORS_DAYS,
    rv_estimator: str = DEFAULT_RV_ESTIMATOR,
    persist: bool = True,
    max_age_minutes: float | None = 240.0,
) -> BuildResult:
    rows = await load_chain_bar(underlying, bar_ts)
    snapshot = build_snapshot_from_rows(
        underlying, bar_ts, rows, max_age_minutes=max_age_minutes
    )

    result = BuildResult(underlying=underlying, bar_ts=bar_ts, status=snapshot.status)
    if not snapshot.ok:
        result.reason = snapshot.reason
        if persist:
            result.written = await persist_snapshot(snapshot)
        return result

    grid = extract_cm_series(snapshot, tenors_days=tenors_days)
    front = front_coordinates(snapshot)

    tenors: list[CMTenor] = list(grid)
    front_days: int | None = None
    if front is not None:
        front_days = front.tenor_days
        # The front coordinate can collide with a grid tenor; keep both rows
        # but let the front one own the "front" kind so a reader can always
        # find the natively observed maturity.
        tenors = [t for t in grid if t.tenor_days != front.tenor_days] + [front]

    premiums: dict[tuple[int, str], Any] = {}
    for tenor in tenors:
        iv = tenor.atm_iv()
        if iv is None:
            continue
        kind = "front" if front_days is not None and tenor.tenor_days == front_days else "grid"
        premiums[(tenor.tenor_days, kind)] = variance_risk_premium(
            underlying,
            bar_ts,
            iv,
            tenor.tenor_days,
            series.bars,
            estimator=rv_estimator,
            cone=cone,
        )

    if persist:
        result.written = await persist_snapshot(
            snapshot, tenors, premiums, front_tenor_days=front_days
        )

    result.front_tenor_days = front_days
    if front is not None:
        result.atm_iv = front.atm_iv()
        vp = premiums.get((front.tenor_days, "front"))
        result.vrp_spread = vp.spread if vp else None
    return result


async def build_underlying(
    underlying: str,
    *,
    bars: int = 1,
    as_of: datetime | None = None,
    lookback_days: int = 30,
    tenors_days: Sequence[int] = DEFAULT_TENORS_DAYS,
    rv_estimator: str = DEFAULT_RV_ESTIMATOR,
    persist: bool = True,
) -> list[BuildResult]:
    symbol = underlying.strip().upper()
    if symbol not in INDEX_UNIVERSE:
        return [
            BuildResult(
                underlying=symbol,
                bar_ts=None,
                status="out_of_scope",
                reason=f"{symbol} is not in {INDEX_UNIVERSE}",
            )
        ]

    timestamps = await usable_bars(
        symbol, limit=bars, as_of=as_of, lookback_days=lookback_days
    )
    if not timestamps:
        return [
            BuildResult(
                underlying=symbol,
                bar_ts=None,
                status="no_data",
                reason=(
                    f"no {SOURCE_INTERVAL} bar in the last {lookback_days}d carries "
                    f"{MIN_BAR_STRIKES}+ strikes"
                ),
            )
        ]

    # The daily series is loaded once, but each bar sees only the sessions that
    # closed BEFORE it — see _AsOfCache for why that distinction is not academic.
    series, _ = await realized_context(symbol, as_of=as_of, estimator=rv_estimator)
    as_of_cache = _AsOfCache(series=series, estimator=rv_estimator)

    results: list[BuildResult] = []
    for ts in sorted(timestamps):
        try:
            session_date = ts.astimezone(IST).date()
            bar_series, bar_cone = as_of_cache.for_session(session_date)
            results.append(
                await build_bar(
                    symbol,
                    ts,
                    series=bar_series,
                    cone=bar_cone,
                    tenors_days=tenors_days,
                    rv_estimator=rv_estimator,
                    persist=persist,
                )
            )
        except Exception as exc:  # pragma: no cover - one bad bar must not kill the pass
            logger.exception(f"[vol.builder] {symbol} {ts.isoformat()} failed: {exc}")
            results.append(
                BuildResult(underlying=symbol, bar_ts=ts, status="error", reason=str(exc))
            )
    return results


async def run(
    *,
    underlyings: Sequence[str] = INDEX_UNIVERSE,
    bars: int = 1,
    as_of: datetime | None = None,
    lookback_days: int = 30,
    persist: bool = True,
) -> dict[str, Any]:
    if persist:
        await ensure_tables()

    out: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": {}}
    for symbol in underlyings:
        results = await build_underlying(
            symbol, bars=bars, as_of=as_of, lookback_days=lookback_days, persist=persist
        )
        ok = [r for r in results if r.status == "ok"]
        out["results"][symbol] = {
            "bars_attempted": len(results),
            "bars_ok": len(ok),
            "latest": results[-1].as_dict() if results else None,
            "statuses": _tally(r.status for r in results),
        }
    return out


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the index volatility substrate.")
    parser.add_argument("--underlying", action="append", help="Index symbol; repeatable.")
    parser.add_argument("--bars", type=int, default=1, help="How many recent usable bars to build.")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not write.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    symbols = tuple(s.upper() for s in (args.underlying or INDEX_UNIVERSE))
    summary = asyncio.run(
        run(
            underlyings=symbols,
            bars=args.bars,
            lookback_days=args.lookback_days,
            persist=not args.dry_run,
        )
    )
    import json

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
