"""mp_core — the ONE Market-Profile computation for every lane and surface.

WHY THIS PACKAGE EXISTS (2026-08-29). The system grew FOUR Market-Profile
surfaces: auction_intelligence/market_profile (the TPO engine), institutional
convergence (which imports that engine but rebuilds profiles from scratch on
every evaluation), the auction commodity sleeve (same engine, own call sites),
and fractal_market_profile (its own service). Each recomputes TPO ladders over
the same bars, and none of them carries the intelligence that the 2026-08-28
research pass paid for: which profile metrics predict anything, and which are
decoration.

WHAT IT IS:
    engine        = auction_intelligence.market_profile.MarketProfileEngine,
                    re-exported. The TPO math has ONE implementation; this
                    package deliberately does not fork it.
    intelligence  = the research layer: day-type classification, the two
                    out-of-sample-validated signals, and VERDICTS — machine-
                    readable statements of what each metric is entitled to mean.
    service       = the compute-once layer: content-addressed memoisation so a
                    profile built for one consumer is served to every other
                    consumer of the same (symbol, bars) for free.

CONSUMERS: institutional_convergence routes its _profile() through
build_cached_profile(); the unified API (/api/mp/unified) serves the same
snapshots to the UI, replacing per-widget recomputation.
"""
from mp_core.intelligence import VERDICTS, classify_day_type, unified_signals
from mp_core.service import build_cached_profile, cache_stats, unified_snapshot

__all__ = [
    "VERDICTS",
    "classify_day_type",
    "unified_signals",
    "build_cached_profile",
    "cache_stats",
    "unified_snapshot",
]
