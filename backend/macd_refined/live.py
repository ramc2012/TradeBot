"""Live current + next monthly expiry fetch, volume/turnover persistence, and
causal signal evaluation for MACD Refined.

The auto-runner (market-hours supervisor) calls :meth:`run_cycle` on a cadence.
Each cycle it:

  1. Resolves the current + next monthly expiry for every symbol in the live
     universe (so the book can position into next month — spec request).
  2. Fetches those expiries' option chains via the active broker adapter and
     PERSISTS a per-contract snapshot (LTP, volume, OI, IV, turnover) under
     ``runtime/macd_refined/volume_tracking`` — this is the "volume tracking of
     option contracts" the spec asks to add. The accumulated LTP series is what
     the live premium-MACD and IV-rank are computed from (no look-ahead).
  3. Generates causal proposals (ATM premium-MACD zero-cross + low-IV + liquidity
     gates) and syncs the paper book.

Everything degrades gracefully when no broker is authenticated (the dev case):
the cycle returns a status payload with ``broker_ready: false`` and books are
left untouched.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from analysis.instruments import ALL_FO_INDICES
from core.config import settings
from macd_refined.indicators import compute_macd, iv_rank, turnover_rupees, zero_cross_up
from macd_refined.risk import size_position


def _forced_close_flag(underlying: str, expiry: str, today: date) -> bool:
    """Owner rule (2026-07-21): a held position may ride its expiry only until
    <= N TRADING days remain, then it is compulsorily closed.

    Returns False whenever the policy flags are down, so the mark payload is
    byte-identical to today with the feature off.  Computed here (next to
    ``window_end_passed``) rather than inside ``paper.PaperBook`` so the paper
    book stays a pure state machine over the marks it is handed.
    """
    try:
        from core.expiry_policy import forced_close_check

        decision = forced_close_check(
            underlying, date.fromisoformat(str(expiry)[:10]), today=today
        )
    except Exception:  # noqa: BLE001 - never let the policy break the mark pass
        return False
    return bool(decision is not None and decision.must_close)


_INDEX_FYERS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "BANKEX": "BSE:BANKEX-INDEX",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_key(ts) -> str:
    """Canonical, chronologically-sortable UTC ISO key for a bar timestamp.

    macd_refined's 30m series come from two sources with different tz
    conventions: the parquet-resample path indexes on ``captured_at`` (UTC,
    +00:00) while the broker-history fallback can surface UTC or tz-naive
    timestamps. Comparing ``str(ts)`` lexically then mis-orders '+00:00' vs
    '+05:30' (or naive) strings — a genuinely newer bar can read as LESS THAN
    a stored one, silently dropping a fresh zero-cross for the rest of the
    session (or re-firing an already-signalled one on the reverse flip). All
    dedup comparisons and persisted state go through this so both sides are
    UTC and lexical order == chronological order. tz-naive is assumed UTC to
    match the dominant (parquet + DB) convention."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None or t.tz is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat()


def fyers_symbol(underlying: str) -> str:
    sym = str(underlying or "").upper().strip()
    if sym in _INDEX_FYERS:
        return _INDEX_FYERS[sym]
    return f"NSE:{sym}-EQ"


def us_monthly_expiries(today: date, ahead: int = 2) -> list[date]:
    """The next `ahead` US standard monthly option expiries (3rd Friday of the
    month) on or after `today`."""
    import calendar
    out: list[date] = []
    year, month = today.year, today.month
    while len(out) < max(ahead, 1):
        weeks = calendar.monthcalendar(year, month)
        fridays = [w[calendar.FRIDAY] for w in weeks if w[calendar.FRIDAY] != 0]
        third = date(year, month, fridays[2])
        if third >= today:
            out.append(third)
        month = 1 if month == 12 else month + 1
        year = year + 1 if month == 1 else year
        if year > today.year + 2:
            break
    return out


class MacdRefinedLiveEngine:
    def __init__(self, store, paper, config: dict[str, Any]):
        self.store = store
        self.paper = paper
        self.config = config
        self.tracking_root = Path(config["live"]["volume_store_root"])
        if not self.tracking_root.is_absolute():
            self.tracking_root = Path(__file__).resolve().parent.parent / self.tracking_root
        self.tracking_root.mkdir(parents=True, exist_ok=True)
        # Cross-cycle cache of broker 30-min premium history per option symbol:
        # {instrument_key: (fetched_at, close_series)}. New 30-min bars only form
        # every 30 min, so we refetch at most every ~25 min — this lets the
        # premium-MACD evaluate from day one (before enough live snapshots have
        # accumulated) without hammering the broker every 60s cycle.
        self._hist_cache: dict[str, tuple] = {}
        self._universe_cache: list[str] | None = None
        # Signal journal (every generated premium-MACD cross + gate verdicts) and
        # per-contract last-signalled-bar dedup state, so a cross is recorded once
        # and never missed across cycles.
        self._signals_path = self.tracking_root.parent / "signals.jsonl"
        self._signal_state_path = self.tracking_root.parent / "signal_state.json"
        self._signal_state: dict[str, str] = self._load_signal_state()
        self._fyers_fb: tuple | None = None  # (token, adapter) fallback cache
        # Rotating start offset into the universe. A full 216-name sweep cannot
        # fit in one cycle (see run_cycle), so the cycle always covered the same
        # leading names and starved the tail forever. The cursor makes each
        # cycle resume where the last one stopped. Persisted so a restart does
        # not reset every lane back to the same first names.
        self._universe_cursor_path = self.tracking_root.parent / "universe_cursor.json"
        self._universe_cursor: int = self._load_universe_cursor()

    def _load_signal_state(self) -> dict[str, str]:
        try:
            if self._signal_state_path.exists():
                raw = dict(json.loads(self._signal_state_path.read_text()))
                # Migrate any legacy mixed-tz string values to canonical UTC ISO
                # so the dedup comparison (now UTC-vs-UTC) is chronologically
                # correct on the first cycle after this fix.
                migrated: dict[str, str] = {}
                for key, value in raw.items():
                    try:
                        migrated[key] = _utc_key(value)
                    except Exception:
                        migrated[key] = str(value)
                return migrated
        except Exception:
            pass
        return {}

    def _save_signal_state(self) -> None:
        try:
            tmp = self._signal_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._signal_state, default=str))
            tmp.replace(self._signal_state_path)
        except Exception:
            pass

    def _load_universe_cursor(self) -> int:
        try:
            if self._universe_cursor_path.exists():
                return max(0, int(json.loads(self._universe_cursor_path.read_text()).get("cursor", 0)))
        except Exception:
            pass
        return 0

    def _save_universe_cursor(self) -> None:
        try:
            tmp = self._universe_cursor_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"cursor": int(self._universe_cursor)}))
            tmp.replace(self._universe_cursor_path)
        except Exception:
            pass

    def _journal_signal(self, row: dict[str, Any]) -> None:
        try:
            with self._signals_path.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    def recent_signals(self, limit: int = 100, underlying: str | None = None) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        if self._signals_path.exists():
            try:
                for line in self._signals_path.read_text().splitlines()[-5000:]:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            except Exception:
                pass
        if underlying:
            u = str(underlying).upper()
            rows = [r for r in rows if str(r.get("underlying", "")).upper() == u]
        rows.sort(key=lambda r: str(r.get("signal_time") or r.get("recorded_at") or ""), reverse=True)
        accepted = sum(1 for r in rows if r.get("accepted"))
        return {"count": len(rows), "accepted": accepted, "signals": rows[: int(limit)]}

    @staticmethod
    def _normalize_iv(iv: float) -> float:
        """Broker IV → decimal. Some feeds report IV in percent (e.g. 11.2),
        others as a decimal (0.112). >3.0 ⇒ percent (300% vol is implausible)."""
        try:
            v = float(iv or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if v <= 0:
            return 0.0
        return v / 100.0 if v > 3.0 else v

    async def _resolve_universe(self) -> list[str]:
        """The live universe. mode='full' → every F&O underlying in
        fo_underlying_catalog (indices first); otherwise the curated list."""
        if str(self.config.get("live_universe_mode") or "list").lower() != "full":
            return list(self.config.get("live_universe") or [])
        if self._universe_cache is not None:
            return self._universe_cache
        syms: list[str] = []
        try:
            from sqlalchemy import text
            from db.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    text(
                        """
                        SELECT DISTINCT u.symbol
                        FROM fo_underlying_catalog AS u
                        WHERE u.symbol IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM fo_contract_catalog AS c
                              WHERE c.underlying = u.symbol
                                AND c.expiry >= CURRENT_DATE
                          )
                        """
                    )
                )
                syms = [str(r[0]).upper() for r in res.fetchall() if r[0]]
        except Exception:
            syms = []
        if not syms:
            syms = list(self.config.get("live_universe") or [])
        idx_set = set(ALL_FO_INDICES)
        ordered = [s for s in ALL_FO_INDICES if s in syms] + sorted(s for s in syms if s not in idx_set)
        self._universe_cache = list(dict.fromkeys(ordered))
        return self._universe_cache

    # ── Market profile helpers ────────────────────────────────────────────
    @property
    def _market(self) -> str:
        return str(self.config.get("market") or "india").lower()

    def _underlying_symbol(self, underlying: str) -> str:
        """Broker symbol for the underlying. US = bare ticker (Alpaca);
        India = fyers index/equity symbol."""
        if self._market == "us":
            return str(underlying).upper()
        return fyers_symbol(underlying)

    def _lot_for(self, underlying: str, chain_lot: Any = None) -> int:
        ov = self.config.get("lot_size_override")
        if ov:
            return int(ov)
        return int(chain_lot or self.store.lot_size_for(underlying) or 1)

    # ── Broker adapter ────────────────────────────────────────────────────
    async def _adapter(self):
        """US → Alpaca; India → FYERS (specifically).
        Resolve the FYERS adapter SPECIFICALLY for India — the lane's symbols
        (NSE:NIFTY50-INDEX, NSE:RELIANCE-EQ, NSE:…CE/PE) are fyers-format, so we
        must not fall back to another broker. Order: (1) the active fyers
        session, (2) ensure_fyers_session() restore, (3) build a fyers adapter
        directly from the valid SAVED token. (3) exists because
        ensure_fyers_session/broker-status validate via /user/profile, which
        401s for fyers while market-data endpoints (chain/history/quotes) return
        200 — so a 'disconnected' status must NOT block data access while the
        token is still valid."""
        if self._market == "us":
            try:
                from brokers.alpaca import alpaca_adapter
                return alpaca_adapter if alpaca_adapter.has_credentials else None
            except Exception:
                return None
        try:
            from api.routers.auth import get_active_adapter, ensure_fyers_session, get_broker_token
            ad = get_active_adapter("fyers")
            if ad is not None:
                return ad
            try:
                if await ensure_fyers_session():
                    ad = get_active_adapter("fyers")
                    if ad is not None:
                        return ad
            except Exception:
                pass
            token = get_broker_token("fyers")
            if token:
                if self._fyers_fb and self._fyers_fb[0] == token:
                    return self._fyers_fb[1]
                from brokers.fyers import FyersAdapter
                a = FyersAdapter()
                await a.authenticate({"access_token": token})  # no profile probe
                self._fyers_fb = (token, a)
                return a
        except Exception:
            return None
        return None

    # ── Expiry resolution ─────────────────────────────────────────────────
    def resolve_expiries(self, underlying: str, today: Optional[date] = None) -> list[date]:
        today = today or datetime.now(timezone.utc).date()
        ahead = int(self.config["live"].get("expiries_ahead", 2))
        if self._market == "us":
            return us_monthly_expiries(today, ahead)
        return self.store.resolve_monthly_expiries(underlying, today, ahead=ahead)

    # ── Volume / turnover persistence ─────────────────────────────────────
    def _tracking_path(self, underlying: str) -> Path:
        return self.tracking_root / f"{str(underlying).upper()}.parquet"

    @staticmethod
    def _parquet_storage_error() -> str | None:
        """Return a fatal configuration error when pandas cannot use Parquet.

        The live cycle depends on durable snapshots for MACD history, turnover
        gates, and open-position marks.  Failing once up front is safer than
        reporting one identical failure for every F&O symbol.
        """
        errors: list[str] = []
        for module_name in ("pyarrow", "fastparquet"):
            try:
                import_module(module_name)
                return None
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{module_name}: {exc}")
        return (
            "MACD Refined Parquet storage is unavailable; install pyarrow or "
            f"fastparquet. Tried {', '.join(errors)}"
        )

    def _persist_snapshots(self, underlying: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        path = self._tracking_path(underlying)
        new = pd.DataFrame(rows)
        if path.exists():
            # Never replace unreadable history with a fresh one-row file.  A
            # read error is operationally significant and must reach the
            # cycle's failure report while the original remains untouched.
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, new], ignore_index=True)
        else:
            combined = new
        # Bound growth — keep the most recent ~80k rows per name.
        combined = combined.tail(80_000)
        # Atomic write (tmp + replace) so a concurrent reader (load_tracking /
        # positioning) never sees a half-written parquet.
        tmp = path.with_suffix(".parquet.tmp")
        try:
            combined.to_parquet(tmp, index=False)
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return len(rows)

    def load_tracking(self, underlying: str) -> pd.DataFrame:
        path = self._tracking_path(underlying)
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_parquet(path)
            df["captured_at"] = pd.to_datetime(df["captured_at"], errors="coerce")
            return df.dropna(subset=["captured_at"])
        except Exception:
            return pd.DataFrame()

    # ── One cycle ─────────────────────────────────────────────────────────
    async def run_cycle(self, *, allow_entries: bool = True, today: Optional[date] = None) -> dict[str, Any]:
        adapter = await self._adapter()
        universe = await self._resolve_universe()
        status: dict[str, Any] = {
            "ran_at": _utc_now(),
            "broker_ready": adapter is not None,
            "universe": universe,
            "fetched": {},
            "snapshots_persisted": 0,
            "proposals": 0,
            "failures": {},
            "storage_ready": False,
        }
        if adapter is None:
            status["note"] = "No authenticated broker adapter; persisted nothing. Volume tracking + signals resume once FYERS is connected."
            return status
        storage_error = self._parquet_storage_error()
        if storage_error:
            status["failures"]["storage"] = storage_error
            status["note"] = storage_error
            return status
        status["storage_ready"] = True

        strikes_side = int(self.config["live"].get("strikes_each_side", 3))
        # Stage funnel for observability — how many ATM contracts reach each gate.
        diag = {"legs_evaluated": 0, "macd_series_ready": 0, "fresh_cross": 0,
                "iv_pass": 0, "liquidity_pass": 0, "sized_ok": 0}

        # Each name is independent until the final paper-book sync.  Bound the
        # concurrency so a 217-name universe finishes inside the supervisor
        # timeout while the process-global Fyers limiter still governs the
        # aggregate request rate.
        concurrency = max(1, int(self.config["live"].get("max_concurrent_names", 6)))
        semaphore = asyncio.Semaphore(concurrency)

        # Open-position names go first.  The cycle is allowed to be interrupted
        # by the supervisor, so risk management must not sit behind 200+ names
        # of discovery work.
        open_positions = self.paper.list_positions(status="open", limit=500).get("open_positions", [])
        open_underlyings = {
            str(position.get("underlying") or "").upper()
            for position in open_positions
            if position.get("underlying")
        }

        # ROTATE the discovery order across cycles.
        #
        # A full sweep cannot fit in one cycle. Fyers allows 200 REST req/min
        # shared across the whole app and CLASS_BULK (this sweep) is capped at
        # 25% of every window, so this lane gets ~50 req/min. A name costs ~4
        # calls (current+next chain, then CE/PE premium history), so 216 names
        # is ~864 calls ~= 17 minutes of budget — longer than the per-name
        # timeout and pressing against the supervisor's cycle budget.
        #
        # The order used to be identical every cycle, so the same leading names
        # consumed the whole budget and names past them were NEVER scanned —
        # failure_count sat at 203-215 of 216 indefinitely and only 5-8 distinct
        # underlyings were evaluated per day. Resuming from a persisted cursor
        # spreads coverage: each cycle picks up where the last stopped, so the
        # entire universe is covered over consecutive cycles instead of never.
        #
        # Names holding an OPEN POSITION are still pinned to the front,
        # unrotated — risk management must never wait for its turn. `sorted` is
        # stable, so the rotated order is preserved inside each group.
        if universe:
            start = self._universe_cursor % len(universe)
            universe = universe[start:] + universe[:start]
        universe = sorted(universe, key=lambda name: str(name).upper() not in open_underlyings)

        async def _process_underlying(underlying: str) -> dict[str, Any]:
            # NOTE: the concurrency semaphore is acquired by `_safe_process`
            # BEFORE the per-name timeout starts — see the comment there.
            expiries = self.resolve_expiries(underlying, today)
            fy = self._underlying_symbol(underlying)
            persisted_here = 0
            for kind, exp in zip(("current", "next"), expiries):
                chain = await asyncio.wait_for(
                    adapter.get_option_chain(fy, exp.isoformat()),
                    timeout=float(self.config["live"].get("broker_timeout_seconds", 12.0)),
                )
                rows, _snaps = self._chain_to_rows(underlying, exp, kind, chain, strikes_side)
                persisted_here += self._persist_snapshots(underlying, rows)
            # Generate causal proposals (premium-MACD seeded from broker
            # history when live snapshots are still thin). Per-name diag is
            # returned to the caller and folded into the aggregate funnel.
            name_diag: dict[str, int] = {}
            signal_updates: dict[str, str] = {}
            props = await self._evaluate(
                adapter, underlying, expiries, name_diag, signal_updates
            )
            return {
                "underlying": underlying,
                "expiries": [e.isoformat() for e in expiries],
                "persisted_rows": persisted_here,
                "proposals": props,
                "diag": name_diag,
                "signal_updates": signal_updates,
            }

        async def _safe_process(underlying: str) -> dict[str, Any]:
            # The semaphore MUST be acquired outside `wait_for`, so the
            # per-name timeout measures actual work and not time spent queued.
            #
            # Every task is created up-front (see `tasks = [...]` below), so all
            # ~216 names start running immediately and then block on this
            # semaphore. When the timeout wrapped the semaphore acquisition, a
            # queued name's 75s clock ran while it held no slot: with
            # max_concurrent_names=6 and ~12-15s of real work per name, only the
            # ~30 names that got a slot within 75s ever completed, and names
            # 31..216 were reported as "timed out" having done no work at all.
            # Because the universe order is deterministic, the SAME first ~30
            # names won every cycle and the tail was starved permanently —
            # failure_count 203-215/216 every cycle since 2026-07-21.
            #
            # Acquiring first makes the cycle a proper bounded queue: total
            # runtime ~= ceil(216/6) * per_name  (~470s), inside the 1140s
            # supervisor budget. Trading behaviour is unchanged — this only
            # governs how many names get scanned, not what any signal decides.
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        _process_underlying(underlying),
                        timeout=float(self.config["live"].get("name_timeout_seconds", 75.0)),
                    )
                except asyncio.TimeoutError:
                    timeout_s = float(self.config["live"].get("name_timeout_seconds", 75.0))
                    return {
                        "underlying": underlying,
                        "error": f"name scan timed out after {timeout_s:g}s",
                    }
                except Exception as exc:  # noqa: BLE001
                    return {"underlying": underlying, "error": str(exc) or type(exc).__name__}

        # CLASS_BULK: the full-universe chain sweep is the single biggest bulk
        # consumer of the shared broker budget. Hard-capped at 25% of every
        # limiter window and yields instantly while CRITICAL work (watchlist
        # rows, index chain refresh, held-position marks) is queued.
        # Contextvars copy into the created tasks, so every broker call under
        # _process_underlying inherits the class.
        from brokers.rate_limiter import CLASS_BULK, broker_class

        with broker_class(CLASS_BULK):
            tasks = [asyncio.create_task(_safe_process(underlying)) for underlying in universe]
        paper_syncs = 0
        try:
            # Commit every completed name independently.  Previously an outer
            # timeout discarded every proposal and skipped all open-position
            # marks because paper.sync_cycle lived after asyncio.gather.
            for completed in asyncio.as_completed(tasks):
                item = await completed
                underlying = str(item["underlying"])
                if item.get("error") is not None:
                    status["failures"][underlying] = str(item["error"])
                    continue
                persisted_here = int(item.get("persisted_rows") or 0)
                name_diag = dict(item.get("diag") or {})
                proposals = list(item.get("proposals") or [])
                signal_updates = dict(item.get("signal_updates") or {})
                status["fetched"][underlying] = {
                    "expiries": list(item.get("expiries") or []),
                    "persisted_rows": persisted_here,
                    "legs": name_diag.get("legs_evaluated", 0),
                    "macd_ready": name_diag.get("macd_series_ready", 0),
                }
                status["snapshots_persisted"] += persisted_here
                status["proposals"] += len(proposals)
                for k, v in name_diag.items():
                    diag[k] = diag.get(k, 0) + v

                marks = self._marks_for_open(today, underlyings={underlying})
                try:
                    if marks or proposals:
                        status["paper_summary"] = self.paper.sync_cycle(
                            proposals=proposals,
                            marks=marks,
                            now=_utc_now(),
                            allow_entries=allow_entries,
                        )
                        paper_syncs += 1
                    # A signal becomes deduplicated only after the related book
                    # update has succeeded (or no book mutation was required).
                    self._signal_state.update(signal_updates)
                    if signal_updates:
                        self._save_signal_state()
                except Exception as exc:  # noqa: BLE001
                    status["failures"][f"paper_sync:{underlying}"] = str(exc)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Advance the rotation by how many names this cycle actually COMPLETED,
        # so the next cycle resumes at the first name this one could not reach.
        # Advancing by completions (not by the whole universe) means no name is
        # skipped: the tail this cycle starved becomes the head of the next one.
        # Guard with max(...,1) so a fully-failing cycle still moves and cannot
        # wedge the rotation on one bad leading name.
        if universe:
            covered = max(len(status["fetched"]), 1)
            self._universe_cursor = (self._universe_cursor + covered) % len(universe)
            self._save_universe_cursor()
            status["universe_cursor"] = self._universe_cursor
            status["universe_covered"] = len(status["fetched"])
            status["universe_size"] = len(universe)

        status["paper_syncs"] = paper_syncs
        status["paper_summary"] = status.get("paper_summary") or self.paper.capital_status()
        status["funnel"] = diag
        status["signals_recorded"] = self.recent_signals(limit=1)["count"]
        return status

    # ── Chain → rows ──────────────────────────────────────────────────────
    def _chain_to_rows(self, underlying: str, expiry: date, kind: str, chain, strikes_side: int):
        rows: list[dict[str, Any]] = []
        snaps: list[dict[str, Any]] = []
        spot = float(getattr(chain, "spot_price", 0.0) or 0.0)
        entries = list(getattr(chain, "entries", []) or [])
        if not entries:
            return rows, snaps
        strikes = sorted({float(e.strike) for e in entries if getattr(e, "strike", 0)})
        atm = min(strikes, key=lambda k: abs(k - spot)) if (strikes and spot > 0) else (strikes[len(strikes) // 2] if strikes else 0.0)
        keep = set(self._near_atm_strikes(strikes, atm, strikes_side))
        lot = self._lot_for(underlying)
        captured = _utc_now()
        for e in entries:
            strike = float(getattr(e, "strike", 0) or 0)
            if strike not in keep:
                continue
            ltp = float(getattr(e, "ltp", 0) or 0)
            vol = float(getattr(e, "volume", 0) or 0)
            oi = float(getattr(e, "oi", 0) or 0)
            iv = float(getattr(e, "iv", 0) or 0) if getattr(e, "iv", None) is not None else 0.0
            moneyness = "ATM" if strike == atm else ("ITM" if (getattr(e, "option_type", "") == "CE") == (strike < spot) else "OTM")
            row = {
                "captured_at": captured,
                "underlying": str(underlying).upper(),
                "expiry": expiry.isoformat(),
                "expiry_kind": kind,
                "option_type": str(getattr(e, "option_type", "")),
                "strike": strike,
                "moneyness": moneyness,
                "ltp": ltp,
                "volume": vol,
                "oi": oi,
                "iv": iv,
                "turnover_rupees": turnover_rupees(vol, ltp),
                "delta": getattr(e, "delta", None),
                "lot_size": lot,
                "instrument_key": str(getattr(e, "instrument_key", "") or ""),
                "spot_price": spot,
            }
            rows.append(row)
            snaps.append(row)
        return rows, snaps

    @staticmethod
    def _near_atm_strikes(strikes: list[float], atm: float, n: int) -> list[float]:
        if not strikes:
            return []
        ordered = sorted(strikes, key=lambda k: abs(k - atm))
        return ordered[: (2 * n + 1)]

    # ── Causal evaluation from accumulated tracking ───────────────────────
    @staticmethod
    def _close_series_from_rows(rows):
        """Time-indexed 30-min close Series from candle dicts (time/close),
        dropping the still-forming last bar. None when unusable."""
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        if "time" not in frame.columns or "close" not in frame.columns:
            return None
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        ser = frame.dropna(subset=["time"]).sort_values("time").set_index("time")["close"].astype(float)
        ser = ser[ser > 0]
        if len(ser) >= 1:
            ser = ser.iloc[:-1]  # drop the still-forming last bar
        return ser if len(ser) else None

    async def _hist_close_series(
        self,
        adapter,
        instrument_key,
        *,
        underlying=None,
        expiry=None,
        strike=None,
        option_type=None,
    ):
        """30-min premium close series for an option, cached ~25 min.

        Resilient by design — a watchlist contract must NEVER be silently
        dropped on a transient broker error. When the contract identity is
        supplied, read through OptionHistoryService.load_candles (persisted DB
        first + throttled broker top-up + persist + gap-backfill), so even if
        the live broker call fails we still return the stored history. A raw
        broker call with one retry is kept as the secondary path. A failed
        fetch is NOT cached, so the next cycle retries instead of going blind.
        """
        if not instrument_key:
            return None
        now = datetime.now(timezone.utc)
        cached = self._hist_cache.get(instrument_key)
        if cached and (now - cached[0]).total_seconds() < 1500:
            return cached[1]

        ser = None
        # Preferred: resilient shared service (DB fallback + persist + gap-fill).
        if underlying and expiry is not None and strike is not None and option_type:
            try:
                from market_data.option_history import option_history_service

                candles = await asyncio.wait_for(
                    option_history_service.load_candles(
                        underlying=str(underlying),
                        expiry=expiry,
                        strike=float(strike),
                        option_type=str(option_type),
                        instrument_key=instrument_key,
                        interval="30minute",
                        limit=120,
                        allow_broker_refresh=True,
                    ),
                    timeout=float(self.config["live"].get("broker_timeout_seconds", 12.0)),
                )
                ser = self._close_series_from_rows(candles)
            except Exception:
                ser = None

        # Secondary: raw broker history with one retry (never fail on a hiccup).
        if ser is None or len(ser) < 1:
            rf = (now.date() - timedelta(days=30)).isoformat()
            rt = now.date().isoformat()
            for _attempt in range(2):
                try:
                    rows = await asyncio.wait_for(
                        adapter.get_historical_candles(instrument_key, "30", rf, rt),
                        timeout=float(self.config["live"].get("broker_timeout_seconds", 12.0)),
                    )
                except Exception:
                    rows = None
                if rows:
                    ser = self._close_series_from_rows(rows)
                    break

        # Cache only a usable series; a miss is left uncached so the next cycle
        # re-attempts (watchlist contract must not be skipped for ~25 min).
        if ser is not None and len(ser) >= 1:
            self._hist_cache[instrument_key] = (now, ser)
        return ser

    async def _evaluate(
        self,
        adapter,
        underlying: str,
        expiries: list[date],
        diag: dict[str, int] | None = None,
        signal_updates: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        diag = diag if diag is not None else {}
        signal_updates = signal_updates if signal_updates is not None else {}

        def _bump(k: str) -> None:
            diag[k] = diag.get(k, 0) + 1

        df = self.load_tracking(underlying)
        if df.empty:
            return []
        sig_cfg = self.config["signal"]
        flt_cfg = self.config["filters"]
        min_bars = int(sig_cfg["macd_slow"]) + int(sig_cfg["macd_signal"])
        proposals: list[dict[str, Any]] = []
        capital = self.paper.capital_status()
        total_equity = float(
            capital.get("total_equity_net")
            if capital.get("total_equity_net") is not None
            else capital.get("total_equity") or self.config["risk"]["starting_equity"]
        )
        available = float(
            capital.get("available_capital_net")
            if capital.get("available_capital_net") is not None
            else capital.get("available_capital", total_equity)
        )
        if settings.SIGNAL_VALIDATION_UNCAPPED:
            # OWNER DIRECTIVE 2026-07-17 (signal validation, paper-only): pin
            # the sizing base to max(starting_equity, total_equity_net) —
            # mirrors S1's MACD_STRATEGY_UNCAPPED_CAPITAL — so the drawn-down
            # book (−33.7%, available ₹23.5k) can never shrink the equity cap
            # below the ₹50k min-ticket floor and brick the lane. min_ticket,
            # turnover floors and all exits stay active.
            equity = max(float(self.config["risk"]["starting_equity"]), total_equity)
        else:
            equity = max(0.0, min(total_equity, available))
        today = datetime.now(timezone.utc).date()
        iv_window = int(flt_cfg["iv_rank_window_sessions"])
        baseline_sessions = int(sig_cfg["volume_baseline_sessions"])
        min_turnover = float(flt_cfg["min_daily_turnover_rupees"])

        # PURE MACD — both legs eligible on their OWN premium zero-cross. No
        # directional leg-selection. IV is mapping-only (recorded, not gated).
        for exp in expiries:
            exp_iso = exp.isoformat()
            sub = df[df["expiry"] == exp_iso]
            if sub.empty:
                continue
            dte = (exp - today).days
            window_open = dte >= int(flt_cfg["entry_window_days_before_expiry"])
            for option_type in ("CE", "PE"):
                leg = sub[sub["option_type"] == option_type]
                if leg.empty:
                    continue
                _bump("legs_evaluated")
                latest_ts = leg["captured_at"].max()
                latest_rows = leg[leg["captured_at"] == latest_ts]
                spot = float(latest_rows["spot_price"].iloc[0]) if "spot_price" in latest_rows else 0.0
                strikes = sorted(latest_rows["strike"].unique())
                if not strikes:
                    continue
                atm = min(strikes, key=lambda k: abs(k - spot)) if spot > 0 else strikes[len(strikes) // 2]
                atm_rows = leg[leg["strike"] == atm].sort_values("captured_at")
                atm_ik = str(atm_rows.iloc[-1].get("instrument_key") or "") if len(atm_rows) else ""
                series = atm_rows.set_index("captured_at")["ltp"].resample("30min").last().dropna()
                if len(series) >= 1:
                    series = series.iloc[:-1]  # drop still-forming bar
                if len(series) < min_bars + 2:
                    series = await self._hist_close_series(
                        adapter, atm_ik,
                        underlying=underlying, expiry=exp, strike=atm, option_type=option_type,
                    )
                    if series is None or len(series) < min_bars + 2:
                        continue
                _bump("macd_series_ready")
                macd, _sig, _hist = compute_macd(
                    series, int(sig_cfg["macd_fast"]), int(sig_cfg["macd_slow"]), int(sig_cfg["macd_signal"])
                )
                crosses = zero_cross_up(macd)
                # FRESH cross: on the last completed bar OR the one before (seam
                # tolerance vs the cache/cycle interaction), AND newer than the
                # last bar we already signalled for this contract (dedup, no miss).
                ckey = f"{underlying}|{exp_iso}|{atm}|{option_type}"
                last_sig = signal_updates.get(ckey) or self._signal_state.get(ckey)
                recent_idx = list(series.index[-2:])
                cross_bars = [t for t, c in zip(series.index, crosses.to_numpy()) if c and t in recent_idx]
                # Compare in canonical UTC so a +05:30 (or naive) fallback bar is
                # ordered correctly against a +00:00 parquet bar.
                fresh = [t for t in cross_bars if last_sig is None or _utc_key(t) > last_sig]
                if not fresh:
                    continue
                signal_bar = max(fresh, key=_utc_key)
                signal_updates[ckey] = _utc_key(signal_bar)
                _bump("fresh_cross")

                last = latest_rows[latest_rows["strike"] == atm]
                if last.empty:
                    continue
                last = last.iloc[0]
                iv = self._normalize_iv(float(last.get("iv") or 0.0))
                ltp = float(last.get("ltp") or 0.0)
                lot = self._lot_for(underlying, last.get("lot_size"))
                # IV-rank — MAPPING LABEL ONLY (never gates). Rank against ONE IV
                # observation PER SESSION (last capture of each day), so
                # iv_rank_window_sessions=252 really spans 252 sessions — not 252
                # per-cycle capture snapshots (~13/session ≈ 19 sessions), which
                # mislabelled every journaled iv_zone for the mapping study.
                iv_by_session = (
                    atm_rows.assign(_d=atm_rows["captured_at"].dt.date)
                    .groupby("_d")["iv"].last().astype(float).map(self._normalize_iv)
                )
                if len(iv_by_session):
                    iv_by_session = iv_by_session.iloc[:-1]  # drop today's forming session
                ivr = iv_rank(iv, iv_by_session.tail(iv_window))
                # Liquidity baseline (real tradeability gate).
                daily_turn = atm_rows.assign(_d=atm_rows["captured_at"].dt.date).groupby("_d")["turnover_rupees"].max()
                prior_days = daily_turn[[d < today for d in daily_turn.index]]
                turn = float(prior_days.tail(baseline_sessions).median()) if not prior_days.empty else (
                    float(daily_turn.iloc[-1]) if len(daily_turn) else 0.0
                )
                passed_liq = turn >= min_turnover
                if passed_liq:
                    _bump("liquidity_pass")

                sized = None
                accepted = False
                skip_reason = ""
                if ltp <= 0:
                    skip_reason = "no premium"
                elif not window_open:
                    skip_reason = f"inside last {flt_cfg['entry_window_days_before_expiry']}d to expiry (dte={dte})"
                elif not passed_liq:
                    skip_reason = f"turnover ₹{turn:,.0f} < floor ₹{min_turnover:,.0f}"
                else:
                    sized = size_position(
                        premium=ltp, lot_size=lot, daily_turnover_rupees=turn or float("inf"),
                        equity=equity, sizing_cfg=self.config["sizing"],
                    )
                    if sized.accepted:
                        accepted = True
                        _bump("sized_ok")
                    else:
                        skip_reason = sized.reason

                # RECORD every generated signal (accepted or not) — IV-rank is a
                # mapping label, the volume/turnover bias is context.
                iv_zone = "cheap" if (ivr is not None and ivr < float(flt_cfg["iv_rank_max"])) else (
                    "rich" if ivr is not None else "unknown")
                self._journal_signal({
                    "recorded_at": _utc_now(), "signal_time": str(signal_bar),
                    "underlying": underlying, "option_type": option_type, "strike": atm,
                    "expiry": exp_iso, "dte": dte, "premium": round(ltp, 2),
                    "macd": round(float(macd.iloc[-1]), 4), "spot": spot,
                    "iv": round(iv, 4), "iv_rank": (round(ivr, 4) if ivr is not None else None), "iv_zone": iv_zone,
                    "turnover_rupees": round(turn, 0), "passed_liquidity": passed_liq, "window_open": window_open,
                    "accepted": accepted, "skip_reason": skip_reason,
                    "qty_units": (sized.qty_units if sized else 0), "qty_lots": (sized.qty_lots if sized else 0),
                    "instrument_key": atm_ik,
                })

                if accepted and sized:
                    window_end = exp - timedelta(days=int(flt_cfg["entry_window_days_before_expiry"]))
                    proposals.append({
                        "underlying": underlying, "option_type": option_type, "strike": atm,
                        "expiry": exp_iso, "expiry_window_end": window_end.isoformat(),
                        "instrument_key": atm_ik, "trading_symbol": f"{underlying} {atm:.0f} {option_type}",
                        "entry_premium": ltp,
                        "quantity_lots": sized.qty_lots, "quantity_units": sized.qty_units,
                        "lot_size": lot, "spot": spot, "iv": iv, "iv_rank": ivr,
                        "direction_bias": ("up" if option_type == "CE" else "down"),
                        "signal_kind": "macd_zero_cross", "daily_turnover_rupees": turn,
                        "selection_reason": f"premium-MACD zero-cross {option_type} @ATM {atm:.0f}; IV-rank {ivr if ivr is None else round(ivr,2)} ({iv_zone}); turnover ₹{turn:,.0f}",
                    })
        return proposals

    def _marks_for_open(
        self,
        today: Optional[date],
        *,
        underlyings: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Mark open positions off the latest persisted LTP; flag window-end."""
        from datetime import timedelta
        today = today or datetime.now(timezone.utc).date()
        window_days = int(self.config["filters"]["entry_window_days_before_expiry"])
        marks: dict[str, dict[str, Any]] = {}
        positions = self.paper.list_positions(status="open", limit=500).get("open_positions", [])
        cache: dict[str, pd.DataFrame] = {}
        for p in positions:
            und = str(p.get("underlying") or "")
            if underlyings is not None and und.upper() not in underlyings:
                continue
            if und not in cache:
                cache[und] = self.load_tracking(und)
            df = cache[und]
            premium = float(p.get("latest_premium") or p.get("entry_premium") or 0.0)
            spot = float(p.get("latest_spot") or 0.0)
            fresh = False
            if not df.empty:
                m = df[(df["expiry"] == str(p.get("expiry"))) & (df["strike"] == float(p.get("strike") or 0)) & (df["option_type"] == str(p.get("option_type")))]
                if not m.empty:
                    last = m.sort_values("captured_at").iloc[-1]
                    premium = float(last.get("ltp") or premium)
                    spot = float(last.get("spot_price") or spot)
                    # Only treat as a fresh mark if the latest capture is from
                    # today's session (this cycle just persisted current chains).
                    fresh = pd.Timestamp(last["captured_at"]).date() >= today
            expiry = str(p.get("expiry") or "")
            window_passed = False
            try:
                window_passed = today >= (date.fromisoformat(expiry) - timedelta(days=window_days))
            except Exception:
                pass
            marks[str(p.get("position_id") or "")] = {
                "premium": premium, "spot": spot, "window_end_passed": window_passed, "fresh": fresh,
            }
            # Added ONLY when it is True, so with the policy flags down the mark
            # payload is byte-identical to before this change.
            if _forced_close_flag(und, expiry, today):
                marks[str(p.get("position_id") or "")]["forced_close"] = True
        return marks

    # ── Seconds-cadence protective-exit heartbeat ─────────────────────────
    async def _live_option_mark(
        self,
        position: dict[str, Any],
        max_age_seconds: float,
        chain_cache: dict[tuple[str, str], dict | None],
    ) -> tuple[Optional[float], Optional[float]]:
        """Freshest (premium, spot) for one held leg off the REAL-TIME plane.

        Order, all read-only and NEVER a broker REST / route_order fetch:
          1) ``data_router.get_live_mark`` — the in-process Fyers-WS tick
             buffer, then the cross-process Redis ``tick:{symbol}`` last-value
             (returns None past its own freshness budget).
          2) the shared ``oc:`` option-chain cache the desks already maintain
             (also a real-time snapshot, no fetch here).
        Returns (None, spot_or_None) when no fresh premium exists so the caller
        marks the position ``fresh=False`` (price exits skipped)."""
        from market_data import live_marks
        from market_data.data_router import data_router

        candidates: list[str] = []
        for fld in ("instrument_key", "trading_symbol"):
            val = str(position.get(fld) or "").strip()
            if not val:
                continue
            mapped = live_marks.registered_app_symbol(val)
            if mapped:
                candidates.append(mapped)
            candidates.append(val)

        premium: Optional[float] = None
        for sym in dict.fromkeys(candidates):
            try:
                live = await data_router.get_live_mark(sym, max_age_seconds=max_age_seconds)
            except Exception:  # noqa: BLE001 — a dead feed simply yields no mark
                live = None
            if live and live > 0:
                premium = float(live)
                break

        spot: Optional[float] = None
        try:
            und_sym = self._underlying_symbol(str(position.get("underlying") or ""))
            s = await data_router.get_live_mark(und_sym, max_age_seconds=max_age_seconds)
            if s and s > 0:
                spot = float(s)
        except Exception:  # noqa: BLE001
            spot = None

        if premium is None:
            entry_ltp, chain_spot = await self._chain_cache_mark(position, chain_cache)
            if entry_ltp is not None and entry_ltp > 0:
                premium = float(entry_ltp)
            if spot is None and chain_spot:
                spot = float(chain_spot)
        return premium, spot

    async def _chain_cache_mark(
        self,
        position: dict[str, Any],
        chain_cache: dict[tuple[str, str], dict | None],
    ) -> tuple[Optional[float], Optional[float]]:
        """(ltp, spot) for the held strike from the shared ``oc:`` chain cache.

        Read-only Redis lookup (no broker call). The cache is only populated
        for tracked chains (mostly indices), so a miss is expected and simply
        yields None — the position then stays on its 30m-cycle backstop."""
        from market_data.option_chain import option_chain_service

        underlying = str(position.get("underlying") or "").upper()
        expiry = str(position.get("expiry") or "").strip()
        otype = str(position.get("option_type") or "").upper()
        try:
            strike = float(position.get("strike") or 0.0)
        except (TypeError, ValueError):
            strike = 0.0
        if not (underlying and expiry and otype in ("CE", "PE") and strike > 0):
            return None, None
        key = (underlying, expiry)
        if key not in chain_cache:
            try:
                chain_cache[key] = await option_chain_service.get_cached(underlying, expiry)
            except Exception:  # noqa: BLE001
                chain_cache[key] = None
        payload = chain_cache.get(key)
        if not payload:
            return None, None
        spot_raw = payload.get("spot_price")
        spot = float(spot_raw) if spot_raw else None
        for entry in payload.get("entries") or []:
            try:
                if (
                    float(entry.get("strike")) == strike
                    and str(entry.get("option_type") or "").upper() == otype
                ):
                    ltp = entry.get("ltp")
                    return (float(ltp) if ltp else None), spot
            except (TypeError, ValueError):
                continue
        return None, spot

    async def refresh_paper_marks(
        self,
        today: Optional[date] = None,
        *,
        max_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Seconds-cadence protective-exit pass over OPEN macd_refined positions.

        Owner directive 2026-07-17: the 30-minute ``run_cycle`` sets the entry
        SIDE, but held-position updates must be in SECONDS. This lightweight
        pass re-marks every open position off the REAL-TIME plane (Fyers-WS
        tick buffer → Redis ``tick:{symbol}`` → shared ``oc:`` chain cache —
        never a broker REST / route_order decision fetch) and fires ONLY the
        existing protective exits (hard stop / partial book / trailing / window
        end) via ``paper.sync_cycle(allow_entries=False)``. Entry cadence and
        entry logic are untouched, so a breached stop/target is caught in
        seconds instead of waiting up to 30 minutes for the next decision cycle.

        A leg with no fresh real-time mark is passed ``fresh=False`` so its
        PRICE exits are skipped (matching ``_manage``'s stale-mark guard) — the
        30m cycle stays its backstop and only the time-based window_end fires.
        """
        today = today or datetime.now(timezone.utc).date()
        if max_age_seconds is None:
            max_age_seconds = float(self.config["live"].get("marks_max_age_seconds", 60.0))
        window_days = int(self.config["filters"]["entry_window_days_before_expiry"])
        positions = self.paper.list_positions(status="open", limit=500).get("open_positions", [])
        if not positions:
            return {
                "status": "ok", "refreshed": 0, "positions": 0, "exits": 0,
                "paper_summary": self.paper.capital_status(),
            }

        marks: dict[str, dict[str, Any]] = {}
        fresh_count = 0
        chain_cache: dict[tuple[str, str], dict | None] = {}
        for p in positions:
            pid = str(p.get("position_id") or "")
            if not pid:
                continue
            ref_premium = float(p.get("latest_premium") or p.get("entry_premium") or 0.0)
            spot = float(p.get("latest_spot") or 0.0)
            premium, live_spot = await self._live_option_mark(p, max_age_seconds, chain_cache)
            fresh = premium is not None and premium > 0
            if fresh:
                # Ratio guard against a cross-wired broker tick (an index-magnitude
                # value mis-attributed to an option symbol) — reject and fall back
                # to the last displayed premium with fresh=False so no wrong exit
                # fires. Mirrors market_data.live_marks.MAX_LIVE_DIVERGENCE_RATIO.
                if ref_premium > 0 and (
                    premium > ref_premium * 4.0 or premium < ref_premium / 4.0
                ):
                    fresh = False
                else:
                    ref_premium = float(premium)
                    fresh_count += 1
            if live_spot and live_spot > 0:
                spot = float(live_spot)
            expiry = str(p.get("expiry") or "")
            window_passed = False
            try:
                window_passed = today >= (date.fromisoformat(expiry) - timedelta(days=window_days))
            except Exception:  # noqa: BLE001
                pass
            marks[pid] = {
                "premium": ref_premium, "spot": spot,
                "window_end_passed": window_passed, "fresh": fresh,
            }
            # Added ONLY when it is True — see the note in _marks_for_open.
            if _forced_close_flag(str(p.get("underlying") or ""), expiry, today):
                marks[pid]["forced_close"] = True

        open_before = len(positions)
        summary = self.paper.sync_cycle(
            proposals=[], marks=marks, now=_utc_now(), allow_entries=False
        )
        open_after = int(summary.get("open_positions") or 0)
        return {
            "status": "ok",
            "refreshed": fresh_count,
            "positions": open_before,
            "exits": max(0, open_before - open_after),
            "paper_summary": summary,
        }

    async def data_audit(
        self,
        *,
        max_names: int | None = None,
        underlyings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Data-sufficiency sweep across the FULL F&O universe.

        For every underlying, resolve current + next monthly expiry, fetch the
        chain, persist per-contract volume/turnover (backfill), and check that
        BOTH the ATM CE and ATM PE have ≥ MACD-warmup 30-min history (broker).
        Returns a per-name report classifying each as sufficient / insufficient
        / no_data / error. Read-only w.r.t. the paper book.
        """
        report_path = self.tracking_root.parent / "data_audit_latest.json"

        def _write(payload: dict[str, Any]) -> None:
            try:
                tmp = report_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, default=str))
                tmp.replace(report_path)
            except Exception:
                pass

        adapter = await self._adapter()
        if adapter is None:
            out = {"broker_ready": False, "note": "No authenticated broker adapter.", "ran_at": _utc_now()}
            _write(out)
            return out
        storage_error = self._parquet_storage_error()
        if storage_error:
            out = {
                "broker_ready": True,
                "storage_ready": False,
                "fatal_error": storage_error,
                "ran_at": _utc_now(),
                "summary": {"sufficient": 0, "insufficient": 0, "no_data": 0, "error": 1, "total": 0},
                "insufficient": [{"underlying": "_storage", "status": "error", "error": storage_error}],
                "names": [],
            }
            _write(out)
            return out
        sig_cfg = self.config["signal"]
        min_bars = int(sig_cfg["macd_slow"]) + int(sig_cfg["macd_signal"])
        strikes_side = int(self.config["live"].get("strikes_each_side", 3))
        universe = await self._resolve_universe()
        if underlyings:
            requested = {str(name).strip().upper() for name in underlyings if str(name).strip()}
            universe = [name for name in universe if str(name).upper() in requested]
            # Preserve explicit names temporarily missing from the catalog so
            # open-position backfills still attempt their contracts.
            present = {str(name).upper() for name in universe}
            universe.extend(sorted(requested - present))
        if max_names:
            universe = universe[: int(max_names)]
        today = datetime.now(timezone.utc).date()
        names: list[dict[str, Any]] = []
        suff = insuff = nodata = err = 0

        for u in universe:
            rec: dict[str, Any] = {"underlying": u}
            try:
                expiries = self.resolve_expiries(u, today)
                rec["current_expiry"] = expiries[0].isoformat() if expiries else None
                rec["next_expiry"] = expiries[1].isoformat() if len(expiries) > 1 else None
                kready = {"current": [0, 0], "next": [0, 0]}  # [ready, total]
                persisted = 0
                for kind, exp in zip(("current", "next"), expiries):
                    chain = await adapter.get_option_chain(self._underlying_symbol(u), exp.isoformat())
                    rows, _snaps = self._chain_to_rows(u, exp, kind, chain, strikes_side)
                    persisted += self._persist_snapshots(u, rows)
                    spot = float(getattr(chain, "spot_price", 0.0) or 0.0)
                    entries = list(getattr(chain, "entries", []) or [])
                    strikes = sorted({float(e.strike) for e in entries if getattr(e, "strike", 0)})
                    if not strikes:
                        continue
                    atm = min(strikes, key=lambda k: abs(k - spot)) if spot > 0 else strikes[len(strikes) // 2]
                    for ot in ("CE", "PE"):
                        kready[kind][1] += 1
                        ik = next(
                            (str(getattr(e, "instrument_key", "") or "")
                             for e in entries
                             if float(getattr(e, "strike", 0) or 0) == atm and str(getattr(e, "option_type", "")) == ot
                             and getattr(e, "instrument_key", None)),
                            "",
                        )
                        ser = await self._hist_close_series(
                            adapter, ik,
                            underlying=u, expiry=exp, strike=atm, option_type=ot,
                        )
                        if ser is not None and len(ser) >= min_bars + 2:
                            kready[kind][0] += 1
                cur_ready, cur_total = kready["current"]
                nxt_ready, nxt_total = kready["next"]
                rec["current"] = f"{cur_ready}/{cur_total}"   # ATM CE+PE history ready (tradeable now)
                rec["next"] = f"{nxt_ready}/{nxt_total}"       # next-month positioning readiness
                rec["persisted_rows"] = persisted
                # Classify on CURRENT-expiry tradeability — next-month legs are
                # often new (thin history) and mature over time, which is not a
                # backfillable gap.
                if persisted == 0 or cur_total == 0:
                    rec["status"] = "no_data"; nodata += 1
                elif cur_ready >= cur_total:
                    rec["status"] = "sufficient"; suff += 1
                else:
                    rec["status"] = "insufficient"; insuff += 1
            except Exception as exc:  # noqa: BLE001
                rec["status"] = "error"; rec["error"] = str(exc)[:160]; err += 1
            names.append(rec)

        out = {
            "ran_at": _utc_now(),
            "broker_ready": True,
            "storage_ready": True,
            "universe_size": len(universe),
            "min_macd_bars": min_bars + 2,
            "summary": {"sufficient": suff, "insufficient": insuff, "no_data": nodata, "error": err, "total": len(universe)},
            "insufficient": [r for r in names if r.get("status") in ("insufficient", "no_data", "error")],
            "names": names,
        }
        _write(out)
        return out

    def positioning_snapshot(self, today: Optional[date] = None) -> dict[str, Any]:
        """Read-only view: current + next expiry resolution and what volume
        tracking exists per symbol (for the UI / API)."""
        today = today or datetime.now(timezone.utc).date()
        out: list[dict[str, Any]] = []
        universe = self._universe_cache or list(self.config.get("live_universe") or [])
        for underlying in universe:
            df = self.load_tracking(underlying)
            expiries = self.resolve_expiries(underlying, today)
            latest = None
            rows_tracked = 0
            if not df.empty:
                rows_tracked = int(len(df))
                latest = pd.Timestamp(df["captured_at"].max()).isoformat()
            out.append({
                "underlying": underlying,
                "is_index": underlying in set(ALL_FO_INDICES),
                "current_expiry": expiries[0].isoformat() if expiries else None,
                "next_expiry": expiries[1].isoformat() if len(expiries) > 1 else None,
                "tracked_rows": rows_tracked,
                "latest_capture": latest,
            })
        return {"as_of": today.isoformat(), "symbols": out}
