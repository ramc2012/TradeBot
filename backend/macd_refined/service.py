"""Service orchestrator for the MACD Refined lane."""
from __future__ import annotations

from time import monotonic
from typing import Any, Optional

from macd_refined.backtest import MacdRefinedBacktester
from macd_refined.config import clone_default_config
from macd_refined.data import MacdRefinedDataStore
from macd_refined.live import MacdRefinedLiveEngine
from macd_refined.paper import MacdRefinedPaperStore


class MacdRefinedService:
    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.config = config or clone_default_config()
        self.store = MacdRefinedDataStore(self.config["data_root"])
        self.paper = MacdRefinedPaperStore(self.config["paper_trading"]["journal_root"], config=self.config)
        self.backtester = MacdRefinedBacktester(self.store, self.config)
        self.live = MacdRefinedLiveEngine(self.store, self.paper, self.config)
        self._summary_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
        self._backtest_cache: dict[tuple[str, str, int], dict[str, Any]] = {}

    # ── Summary ───────────────────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        if self._summary_cache["payload"] is not None and self._summary_cache["expires_at"] > monotonic():
            return self._summary_cache["payload"]
        start, end = self.store.dataset_date_range()
        try:
            available = self.store.available_underlyings()
        except Exception:
            available = []
        try:
            automation = self._automation_status()
        except Exception:
            automation = {"enabled": False}
        payload = {
            "key": self.config["key"],
            "label": self.config["label"],
            "description": self.config["description"],
            "timeframe": self.config["timeframe"],
            "live_universe_mode": self.config.get("live_universe_mode", "list"),
            "live_universe": list(self.config.get("live_universe") or []),
            "backtest_universe_size": len(available),
            "dataset": {
                "root": str(self.config["data_root"]),
                "expiry_start": start.isoformat() if start else None,
                "expiry_end": end.isoformat() if end else None,
                "expiry_files": len(self.store.list_expiry_files()),
            },
            "params": self.backtester._config_summary(),
            "automation": automation,
            "paper_summary": self.paper.capital_status(),
        }
        self._summary_cache = {"payload": payload, "expires_at": monotonic() + 60.0}
        return payload

    def _automation_status(self) -> dict[str, Any]:
        from core.market_hours_paper_supervisor import market_hours_paper_supervisor
        return market_hours_paper_supervisor.get_runner_status(self.config.get("key") or "macd_refined")

    # ── Backtest (research replay or causal engine) ───────────────────────
    def backtest(
        self, *, source: str = "research", underlyings: Optional[list[str]] = None, expiry_count: int = 8
    ) -> dict[str, Any]:
        # The historical research dataset is India-only; US is live/paper-only.
        if str(self.config.get("market") or "india").lower() == "us":
            return {"source": source, "note": "No US historical dataset — US is live/paper only.",
                    "signals": {"signal_level_metrics": {}}, "portfolio": {}}
        key = (source, ",".join(sorted(underlyings)) if underlyings else "", int(expiry_count))
        cached = self._backtest_cache.get(key)
        if cached is not None:
            return cached
        result = self.backtester.run(source=source, underlyings=list(underlyings) if underlyings else None, expiry_count=int(expiry_count))
        self._backtest_cache[key] = result
        return result

    def backtest_compare(self, *, underlyings: Optional[list[str]] = None, expiry_count: int = 8) -> dict[str, Any]:
        """Both views side by side — the documented (research-validated) edge
        and the honest causal forward engine (the walk-forward gap, spec §11)."""
        return {
            "research": self.backtest(source="research", underlyings=underlyings, expiry_count=expiry_count),
            "engine": self.backtest(source="engine", underlyings=underlyings, expiry_count=expiry_count),
            "caveats": [
                "research = replay of data/signals/macd_signals.parquet (validated; pure hold-to-window, gross).",
                "engine = causal forward generator (no hindsight leg selection) with -50% stop + slippage.",
                "Per spec §10 the research win-rates/medians are optimistic UPPER BOUNDS; trust the engine + live paper book for deployability.",
            ],
        }

    # ── Positioning (current + next expiry) ───────────────────────────────
    def positioning(self) -> dict[str, Any]:
        return self.live.positioning_snapshot()

    async def run_live_cycle(self, *, allow_entries: bool = True) -> dict[str, Any]:
        result = await self.live.run_cycle(allow_entries=allow_entries)
        # Per-session data-audit re-run (audit 2026-07-18): the audit used to be
        # manual-only, so data_audit_latest.json went stale for days. Piggyback
        # on the supervisor-driven live cycle (no new scheduling infrastructure)
        # and re-run at most ONCE per session.
        self._maybe_schedule_session_data_audit()
        return result

    def _maybe_schedule_session_data_audit(self, *, now_ist=None) -> bool:
        """Kick a background data_audit once per session, late-session only.

        Guards (all must pass): india lane (the US lane is parked — no Alpaca
        source); MACD_REFINED_DATA_AUDIT_PER_SESSION enabled; IST >= 14:45 so
        the full-universe chain sweep never competes with the open-window broker
        budget (the starvation class the 2026-07-17 incidents traced to); the
        last report's ran_at is from a prior date; no audit task already
        in-flight. Fire-and-forget — the audit writes data_audit_latest.json
        itself. Returns True when a run was scheduled (for tests)."""
        try:
            if str(self.config.get("market") or "india").lower() == "us":
                return False
            from core.config import settings as _settings

            if not bool(getattr(_settings, "MACD_REFINED_DATA_AUDIT_PER_SESSION", True)):
                return False
            if now_ist is None:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZoneInfo

                now_ist = _dt.now(_ZoneInfo("Asia/Kolkata"))
            if (now_ist.hour, now_ist.minute) < (14, 45):
                return False
            import json as _json
            from pathlib import Path as _Path

            report_path = _Path(self.live.tracking_root).parent / "data_audit_latest.json"
            if report_path.exists():
                ran_at = str(_json.loads(report_path.read_text()).get("ran_at") or "")
                # ran_at is UTC; at a >=14:45 IST trigger the UTC and IST dates
                # coincide, so a plain date-prefix compare is session-accurate.
                if ran_at[:10] == now_ist.date().isoformat():
                    return False
            task = getattr(self, "_session_audit_task", None)
            if task is not None and not task.done():
                return False
            import asyncio as _asyncio

            self._session_audit_task = _asyncio.create_task(self.data_audit())
            return True
        except Exception:  # noqa: BLE001 — never let the audit kick break the cycle
            return False

    async def refresh_paper_marks(self) -> dict[str, Any]:
        """Seconds-cadence protective-exit heartbeat for open positions.

        Reads the real-time plane (Fyers-WS tick / shared oc: chain cache) and
        fires only the existing stop/target/trail/window exits — never opens a
        position, never runs a broker REST decision fetch. See
        ``MacdRefinedLiveEngine.refresh_paper_marks``."""
        return await self.live.refresh_paper_marks()

    async def data_audit(
        self,
        *,
        max_names: int | None = None,
        underlyings: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.live.data_audit(max_names=max_names, underlyings=underlyings)

    def signals(self, *, limit: int = 100, underlying: str | None = None) -> dict[str, Any]:
        """Recent generated premium-MACD signals (recorded with gate verdicts)."""
        return self.live.recent_signals(limit=limit, underlying=underlying)

    def data_audit_report(self) -> dict[str, Any]:
        import json
        from pathlib import Path
        path = Path(self.live.tracking_root).parent / "data_audit_latest.json"
        if not path.exists():
            return {"available": False, "note": "No audit has run yet. POST /api/macd-refined/data-audit to start one."}
        try:
            return {"available": True, **json.loads(path.read_text())}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)}

    # ── Paper surfaces ────────────────────────────────────────────────────
    def paper_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        return self.paper.list_positions(symbol=symbol, status=status, limit=limit)

    def paper_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        return self.paper.list_journal(symbol=symbol, limit=limit)

    def paper_summary(self) -> dict[str, Any]:
        return self.paper.capital_status()

    def reset_paper(self, *, actor: str | None = None) -> dict[str, Any]:
        result = self.paper.reset_account(actor=actor)
        self._summary_cache = {"payload": None, "expires_at": 0.0}
        return result


macd_refined_service = MacdRefinedService()

# The US clone (us_macd_refined_service) was RETIRED 2026-07-20 on the owner's
# instruction that only MACD and MACD-refined survive. It was exec=parked and
# brokers.alpaca does not exist on this deployment, so it could never run.
# Historical data under backend/runtime/us_macd_refined/ is KEPT.
