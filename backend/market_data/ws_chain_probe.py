"""WS-first chain design — phase-P0 empirical probe (inert unless enabled).

Answers the three market-open questions the design left open, by observing the
LIVE tick stream once option legs are subscribed (no separate subscribe harness):

  1. Upstox greeks on the wire — does an option tick carry non-null iv/delta/…?
  2. Fyers oi/pdoi cadence — is `oi` (and `pdoi`) present on EVERY option frame,
     or only some? (the picker liquidity-lift assumes near-per-frame oi.)
  3. Upstox `iv` unit — fraction (~0.10–0.50) or percent (~10–50)?

Enable with WS_CHAIN_PROBE_ENABLED=true for one live session, read the summary
via GET /api/diagnostics/ws-chain-probe (or the periodic log line), then disable.
The tick builders call `ws_chain_probe.observe(...)` behind the settings flag, so
this file is completely inert in normal operation.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from loguru import logger


class _SrcStat:
    __slots__ = (
        "frames", "option_frames", "with_oi", "with_prev_oi", "with_greeks",
        "with_iv", "iv_min", "iv_max", "iv_sum", "iv_n",
    )

    def __init__(self) -> None:
        self.frames = 0
        self.option_frames = 0
        self.with_oi = 0
        self.with_prev_oi = 0
        self.with_greeks = 0
        self.with_iv = 0
        self.iv_min: Optional[float] = None
        self.iv_max: Optional[float] = None
        self.iv_sum = 0.0
        self.iv_n = 0

    def as_dict(self) -> dict[str, Any]:
        of = self.option_frames or 1
        iv_mean = (self.iv_sum / self.iv_n) if self.iv_n else None
        # Heuristic unit inference from the mean IV magnitude.
        iv_unit = None
        if iv_mean is not None:
            iv_unit = "fraction" if iv_mean < 3.0 else "percent"
        return {
            "frames": self.frames,
            "option_frames": self.option_frames,
            "oi_present_pct": round(100.0 * self.with_oi / of, 1),
            "prev_oi_present_pct": round(100.0 * self.with_prev_oi / of, 1),
            "greeks_present_pct": round(100.0 * self.with_greeks / of, 1),
            "iv_present_pct": round(100.0 * self.with_iv / of, 1),
            "iv_min": self.iv_min,
            "iv_max": self.iv_max,
            "iv_mean": round(iv_mean, 4) if iv_mean is not None else None,
            "iv_unit_inferred": iv_unit,
        }


class WSChainProbe:
    """Process-global observation sink. Cheap (a few counters per source)."""

    def __init__(self) -> None:
        self._by_source: dict[str, _SrcStat] = defaultdict(_SrcStat)
        self._log_every = 200

    def observe(
        self,
        source: str,
        *,
        is_option: bool,
        oi: Optional[int] = None,
        prev_oi: Optional[int] = None,
        iv: Optional[float] = None,
        delta: Optional[float] = None,
    ) -> None:
        try:
            st = self._by_source[source]
            st.frames += 1
            if not is_option:
                return
            st.option_frames += 1
            if oi is not None and oi > 0:
                st.with_oi += 1
            if prev_oi is not None and prev_oi > 0:
                st.with_prev_oi += 1
            if delta is not None:
                st.with_greeks += 1
            if iv is not None and iv > 0:
                st.with_iv += 1
                st.iv_n += 1
                st.iv_sum += float(iv)
                st.iv_min = iv if st.iv_min is None else min(st.iv_min, iv)
                st.iv_max = iv if st.iv_max is None else max(st.iv_max, iv)
            if st.option_frames % self._log_every == 0:
                logger.info(f"[ws-chain-probe] {source}: {st.as_dict()}")
        except Exception:  # noqa: BLE001 — a probe must never break the tick path
            pass

    def snapshot(self) -> dict[str, Any]:
        return {src: st.as_dict() for src, st in self._by_source.items()}

    def reset(self) -> None:
        self._by_source.clear()


ws_chain_probe = WSChainProbe()
