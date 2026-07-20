"""Gann lane universe — all indices + stock spots + commodity futures.

Closes GAP 2.  The configured universe was seven symbols; the owner asked for
"all indices, stock spots, commodity futures".

Two things had to be true before that was safe, and both are enforced here:

1. **Daily history must actually exist per symbol class.**  The universe is
   resolved FROM the 30-minute spot store, not from a hardcoded list, so a
   symbol can only enter the universe if it has bars.  :func:`resolve_universe`
   issues ONE bounded query over a recent window (literal UTC bounds on
   ``time``, so chunk exclusion holds) and returns what is genuinely there.
2. **Truncation must be loud.**  The lane previously scanned 6 of its 7
   configured symbols in 828 of 868 cycles and nobody noticed — the same class
   of failure as the 25-of-211 spot-writer cap.  :class:`SweepCursor` does an
   explicit round-robin batch and reports ``scanned`` against
   ``universe_size`` every cycle, so a shortfall is a visible number rather
   than a silence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

#: Index symbols carried in the spot store.  Indices are not resolvable via the
#: commodity spec and are not F&O stocks, so they are named explicitly.
INDEX_SYMBOLS: frozenset[str] = frozenset(
    {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
)

CLASS_INDEX = "index"
CLASS_STOCK = "stock"
CLASS_COMMODITY = "commodity"

_UNIVERSE_SQL = """
SELECT underlying, count(*) AS bars, max(time) AS last_bar
FROM underlying_spot_candles
WHERE interval = $1
  AND time >= $2::timestamptz
  AND time <  $3::timestamptz
GROUP BY underlying
ORDER BY underlying
"""


def classify(symbol: str) -> str:
    """index | stock | commodity."""
    token = str(symbol or "").upper()
    if token in INDEX_SYMBOLS:
        return CLASS_INDEX
    try:
        from market_data.commodity_contract_specs import get_commodity_contract_spec

        spec = get_commodity_contract_spec(token)
        if spec.root and spec.root != "UNKNOWN":
            return CLASS_COMMODITY
    except Exception:
        pass
    return CLASS_STOCK


@dataclass(frozen=True)
class UniverseMember:
    underlying: str
    instrument_class: str
    recent_bars: int
    last_bar: datetime | None


async def resolve_universe(
    connection: Any,
    *,
    interval: str = "30minute",
    freshness_days: int = 7,
    min_recent_bars: int = 5,
    include_classes: Sequence[str] = (CLASS_INDEX, CLASS_STOCK, CLASS_COMMODITY),
    as_of: datetime | None = None,
) -> list[UniverseMember]:
    """Symbols that actually have recent spot bars, classified.

    ``min_recent_bars`` keeps a symbol that printed a single stray bar out of
    the universe.  A symbol that has gone quiet drops out on its own — which
    is the correct behaviour for a lane whose geometry needs history.
    """
    end = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=max(int(freshness_days), 1) + 1)
    rows = await connection.fetch(_UNIVERSE_SQL, str(interval), start, end)
    wanted = {str(item) for item in include_classes}
    out: list[UniverseMember] = []
    for row in rows:
        symbol = str(row["underlying"]).upper()
        bars = int(row["bars"] or 0)
        if bars < int(min_recent_bars):
            continue
        klass = classify(symbol)
        if klass not in wanted:
            continue
        out.append(UniverseMember(symbol, klass, bars, row["last_bar"]))
    return sorted(out, key=lambda m: (m.instrument_class, m.underlying))


def class_counts(members: Iterable[UniverseMember]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for member in members:
        counts[member.instrument_class] = counts.get(member.instrument_class, 0) + 1
    return counts


@dataclass
class SweepCursor:
    """Round-robin batching over the universe, with loud accounting.

    A daily Gann lane does not need to re-derive 220 instruments every 60
    seconds — the bar does not change.  But the runner cadence is owned
    elsewhere, so instead of silently truncating to whatever fits, the lane
    takes an explicit BATCH per cycle and advances a persisted cursor.  With
    ``batch_size=12`` over 225 instruments a full sweep completes in ~19
    cycles (~19 minutes at the 60 s cadence), and the emitted
    ``sweep_progress`` says exactly where it is.
    """

    position: int = 0
    batch_size: int = 12

    def take(self, universe: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
        size = len(universe)
        if size == 0:
            return [], {
                "universe_size": 0,
                "scanned": 0,
                "batch_size": int(self.batch_size),
                "cursor_before": int(self.position),
                "cursor_after": 0,
                "sweep_complete": False,
                "truncated": False,
            }
        batch = max(int(self.batch_size), 1)
        start = int(self.position) % size
        indices = [(start + offset) % size for offset in range(min(batch, size))]
        selection = [universe[index] for index in indices]
        after = (start + len(selection)) % size
        stats = {
            "universe_size": size,
            "scanned": len(selection),
            "batch_size": batch,
            "cursor_before": start,
            "cursor_after": after,
            "sweep_complete": len(selection) >= size,
            # Truncation is a fact to report, not a fact to hide: it is TRUE on
            # every batched cycle by design, and a consumer can tell the
            # designed case from a failure by comparing scanned to batch_size.
            "truncated": len(selection) < size,
        }
        self.position = after
        return selection, stats


__all__ = [
    "INDEX_SYMBOLS",
    "CLASS_INDEX",
    "CLASS_STOCK",
    "CLASS_COMMODITY",
    "UniverseMember",
    "SweepCursor",
    "classify",
    "resolve_universe",
    "class_counts",
]
