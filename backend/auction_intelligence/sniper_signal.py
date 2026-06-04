"""In-process store for the Sniper excursion-estimator alpha signal.

The Sniper model (a trained LightGBM excursion estimator over ~123 MP / HTF /
auction-state / VWAP / inferred-order-flow / option-chain / context features)
runs in an ISOLATED sidecar container that reads 1-min bars directly from
TimescaleDB and rebuilds its full feature vector with the canonical
``nomad_sniper`` feature builders. We deliberately do NOT load LightGBM or
rebuild that 123-feature vector inside the production backend — that would add a
heavy dependency and risk OOM-recreating the container.

Instead the sidecar POSTs its per-underlying prediction to
``/api/auction-intelligence/sniper-signal`` and we cache it here, in-process.
The Auction Intelligence decision cycle then reads the freshest non-stale signal
for the underlying and applies it as a bounded confidence overlay
(``AuctionIntelligenceService._apply_sniper_overlay``) — exactly the symmetric
counterpart to the sidecar's existing GET of the backend's live order-flow
snapshot. The AI thus gains the benefit of the sniper's full feature set via the
prediction, with graceful degradation: no fresh signal -> no adjustment.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Canonical short codes shared with the Auction Intelligence decision cycle
# (``session.symbol`` is "NIFTY"/"BANKNIFTY"/"SENSEX"...). The sidecar may POST
# either the short code or a DB/broker form (e.g. "NSE:NIFTY50-INDEX") — both
# must resolve to the same key.
_SYMBOL_ALIASES = {
    "NIFTY50": "NIFTY",
    "NIFTY50INDEX": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "BANKNIFTYINDEX": "BANKNIFTY",
    "NIFTYFINSERVICE": "FINNIFTY",
    "FINNIFTYINDEX": "FINNIFTY",
    "MIDCPNIFTYINDEX": "MIDCPNIFTY",
    "BSESENSEX": "SENSEX",
    "SENSEXINDEX": "SENSEX",
}


def normalize_symbol(value: str | None) -> str:
    s = str(value or "").upper().strip()
    if ":" in s:  # drop exchange prefix, e.g. NSE:/BSE:
        s = s.split(":", 1)[1]
    s = (
        s.replace("-INDEX", "")
        .replace(" INDEX", "")
        .replace("-EQ", "")
        .replace("-FUT", "")
        .replace(" FUT", "")
        .strip()
        .replace(" ", "")
    )
    return _SYMBOL_ALIASES.get(s, s)


@dataclass
class SniperSignal:
    """A single per-underlying sniper prediction snapshot."""

    symbol: str
    direction: str  # LONG | SHORT | FLAT
    magnitude_atr: float  # expected favorable excursion in ATR units (conviction proxy)
    confidence: float  # 0..1, derived by the sidecar from the estimator output
    horizon: str  # e.g. "60m" — the horizon this signal represents
    decision_time: Optional[str] = None  # ISO ts of the bar the sniper predicted on
    model: Optional[str] = None  # estimator artifact name
    up_atr: Optional[float] = None  # predicted upside excursion (ATR units)
    down_atr: Optional[float] = None  # predicted downside excursion (ATR units)
    extras: dict[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def age_seconds(self, *, now: Optional[datetime] = None) -> float:
        now = now or _utcnow()
        try:
            recv = datetime.fromisoformat(str(self.received_at))
            if recv.tzinfo is None:
                recv = recv.replace(tzinfo=timezone.utc)
        except Exception:
            return float("inf")
        return max(0.0, (now - recv).total_seconds())


class SniperSignalStore:
    """Thread-safe, process-global cache keyed by normalized underlying symbol."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_symbol: dict[str, SniperSignal] = {}

    def put(self, signal: SniperSignal) -> str:
        key = normalize_symbol(signal.symbol)
        if not key:
            return ""
        with self._lock:
            self._by_symbol[key] = signal
        return key

    def get(
        self,
        symbol: str,
        *,
        max_staleness_seconds: float | None = None,
    ) -> Optional[SniperSignal]:
        key = normalize_symbol(symbol)
        with self._lock:
            signal = self._by_symbol.get(key)
        if signal is None:
            return None
        if (
            max_staleness_seconds is not None
            and signal.age_seconds() > max_staleness_seconds
        ):
            return None
        return signal

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {key: asdict(value) for key, value in self._by_symbol.items()}

    def clear(self) -> None:
        with self._lock:
            self._by_symbol.clear()


# Process-global singleton. Survives across decision cycles (the service is
# re-instantiated per cycle, but this module-level store is not).
sniper_signal_store = SniperSignalStore()
