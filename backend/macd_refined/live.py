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

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from analysis.instruments import ALL_FO_INDICES
from macd_refined.indicators import compute_macd, iv_rank, turnover_rupees, zero_cross_up
from macd_refined.risk import size_position

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


def fyers_symbol(underlying: str) -> str:
    sym = str(underlying or "").upper().strip()
    if sym in _INDEX_FYERS:
        return _INDEX_FYERS[sym]
    return f"NSE:{sym}-EQ"


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

    def _load_signal_state(self) -> dict[str, str]:
        try:
            if self._signal_state_path.exists():
                return dict(json.loads(self._signal_state_path.read_text()))
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
                    text("SELECT DISTINCT symbol FROM fo_underlying_catalog WHERE symbol IS NOT NULL")
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

    # ── Broker adapter ────────────────────────────────────────────────────
    async def _adapter(self):
        """Resolve the FYERS adapter SPECIFICALLY — the lane's symbols
        (NSE:NIFTY50-INDEX, NSE:RELIANCE-EQ, NSE:…CE/PE) are fyers-format, so we
        must not fall back to another broker. Order: (1) the active fyers
        session, (2) ensure_fyers_session() restore, (3) build a fyers adapter
        directly from the valid SAVED token. (3) exists because
        ensure_fyers_session/broker-status validate via /user/profile, which
        401s for fyers while market-data endpoints (chain/history/quotes) return
        200 — so a 'disconnected' status must NOT block data access while the
        token is still valid."""
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
        return self.store.resolve_monthly_expiries(underlying, today, ahead=ahead)

    # ── Volume / turnover persistence ─────────────────────────────────────
    def _tracking_path(self, underlying: str) -> Path:
        return self.tracking_root / f"{str(underlying).upper()}.parquet"

    def _persist_snapshots(self, underlying: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        path = self._tracking_path(underlying)
        new = pd.DataFrame(rows)
        if path.exists():
            try:
                existing = pd.read_parquet(path)
                combined = pd.concat([existing, new], ignore_index=True)
            except Exception:
                combined = new
        else:
            combined = new
        # Bound growth — keep the most recent ~80k rows per name.
        combined = combined.tail(80_000)
        # Atomic write (tmp + replace) so a concurrent reader (load_tracking /
        # positioning) never sees a half-written parquet.
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False)
        tmp.replace(path)
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
        }
        if adapter is None:
            status["note"] = "No authenticated broker adapter; persisted nothing. Volume tracking + signals resume once FYERS is connected."
            return status

        all_proposals: list[dict[str, Any]] = []
        marks: dict[str, dict[str, Any]] = {}
        strikes_side = int(self.config["live"].get("strikes_each_side", 3))
        # Stage funnel for observability — how many ATM contracts reach each gate.
        diag = {"legs_evaluated": 0, "macd_series_ready": 0, "fresh_cross": 0,
                "iv_pass": 0, "liquidity_pass": 0, "sized_ok": 0}

        for underlying in universe:
            try:
                expiries = self.resolve_expiries(underlying, today)
                fy = fyers_symbol(underlying)
                persisted_here = 0
                snapshots_by_exp: dict[str, list[dict[str, Any]]] = {}
                for kind, exp in zip(("current", "next"), expiries):
                    chain = await adapter.get_option_chain(fy, exp.isoformat())
                    rows, snaps = self._chain_to_rows(underlying, exp, kind, chain, strikes_side)
                    persisted_here += self._persist_snapshots(underlying, rows)
                    snapshots_by_exp[exp.isoformat()] = snaps
                status["fetched"][underlying] = {
                    "expiries": [e.isoformat() for e in expiries],
                    "persisted_rows": persisted_here,
                }
                status["snapshots_persisted"] += persisted_here
                # Generate causal proposals (premium-MACD seeded from broker
                # history when live snapshots are still thin). Per-name diag is
                # captured for the data-sufficiency audit, then folded into the
                # aggregate funnel.
                name_diag: dict[str, int] = {}
                props = await self._evaluate(adapter, underlying, expiries, name_diag)
                for k, v in name_diag.items():
                    diag[k] = diag.get(k, 0) + v
                status["fetched"][underlying]["legs"] = name_diag.get("legs_evaluated", 0)
                status["fetched"][underlying]["macd_ready"] = name_diag.get("macd_series_ready", 0)
                all_proposals.extend(props)
            except Exception as exc:  # noqa: BLE001
                status["failures"][underlying] = str(exc)

        # Persist the per-contract last-signalled-bar dedup state.
        self._save_signal_state()
        # Build marks for open positions (latest LTP from this cycle's snapshots).
        marks = self._marks_for_open(today)
        status["proposals"] = len(all_proposals)
        status["funnel"] = diag
        status["signals_recorded"] = self.recent_signals(limit=1)["count"]
        try:
            summary = self.paper.sync_cycle(
                proposals=all_proposals, marks=marks, now=_utc_now(), allow_entries=allow_entries
            )
            status["paper_summary"] = summary
        except Exception as exc:  # noqa: BLE001
            status["failures"]["paper_sync"] = str(exc)
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
        lot = self.store.lot_size_for(underlying)
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
    async def _hist_close_series(self, adapter, instrument_key: str):
        """Broker 30-min premium close series for an option symbol, cached
        ~25 min. Returns a time-indexed pd.Series (forming last bar dropped) or
        None. Lets the premium-MACD evaluate before live snapshots accumulate."""
        if not instrument_key:
            return None
        now = datetime.now(timezone.utc)
        cached = self._hist_cache.get(instrument_key)
        if cached and (now - cached[0]).total_seconds() < 1500:
            return cached[1]
        rf = (now.date() - timedelta(days=30)).isoformat()
        rt = now.date().isoformat()
        try:
            rows = await adapter.get_historical_candles(instrument_key, "30", rf, rt)
        except Exception:
            return None
        ser = None
        if rows:
            frame = pd.DataFrame(rows)
            frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
            ser = frame.dropna(subset=["time"]).sort_values("time").set_index("time")["close"].astype(float)
            ser = ser[ser > 0]
            if len(ser) >= 1:
                ser = ser.iloc[:-1]  # drop the still-forming last bar
        self._hist_cache[instrument_key] = (now, ser)
        return ser

    async def _evaluate(self, adapter, underlying: str, expiries: list[date], diag: dict[str, int] | None = None) -> list[dict[str, Any]]:
        diag = diag if diag is not None else {}

        def _bump(k: str) -> None:
            diag[k] = diag.get(k, 0) + 1

        df = self.load_tracking(underlying)
        if df.empty:
            return []
        sig_cfg = self.config["signal"]
        flt_cfg = self.config["filters"]
        min_bars = int(sig_cfg["macd_slow"]) + int(sig_cfg["macd_signal"])
        proposals: list[dict[str, Any]] = []
        equity = float(self.paper.capital_status().get("total_equity") or self.config["risk"]["starting_equity"])
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
                    series = await self._hist_close_series(adapter, atm_ik)
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
                last_sig = self._signal_state.get(ckey)
                recent_idx = list(series.index[-2:])
                cross_bars = [t for t, c in zip(series.index, crosses.to_numpy()) if c and t in recent_idx]
                fresh = [t for t in cross_bars if last_sig is None or str(t) > last_sig]
                if not fresh:
                    continue
                signal_bar = max(fresh)
                self._signal_state[ckey] = str(signal_bar)
                _bump("fresh_cross")

                last = latest_rows[latest_rows["strike"] == atm]
                if last.empty:
                    continue
                last = last.iloc[0]
                iv = self._normalize_iv(float(last.get("iv") or 0.0))
                ltp = float(last.get("ltp") or 0.0)
                lot = int(last.get("lot_size") or self.store.lot_size_for(underlying))
                # IV-rank — MAPPING LABEL ONLY (never gates).
                iv_hist = atm_rows["iv"].astype(float).map(self._normalize_iv).iloc[:-1]
                ivr = iv_rank(iv, iv_hist.tail(iv_window))
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

    def _marks_for_open(self, today: Optional[date]) -> dict[str, dict[str, Any]]:
        """Mark open positions off the latest persisted LTP; flag window-end."""
        from datetime import timedelta
        today = today or datetime.now(timezone.utc).date()
        window_days = int(self.config["filters"]["entry_window_days_before_expiry"])
        marks: dict[str, dict[str, Any]] = {}
        positions = self.paper.list_positions(status="open", limit=500).get("open_positions", [])
        cache: dict[str, pd.DataFrame] = {}
        for p in positions:
            und = str(p.get("underlying") or "")
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
        return marks

    async def data_audit(self, *, max_names: int | None = None) -> dict[str, Any]:
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
        sig_cfg = self.config["signal"]
        min_bars = int(sig_cfg["macd_slow"]) + int(sig_cfg["macd_signal"])
        strikes_side = int(self.config["live"].get("strikes_each_side", 3))
        universe = await self._resolve_universe()
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
                    chain = await adapter.get_option_chain(fyers_symbol(u), exp.isoformat())
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
                        ser = await self._hist_close_series(adapter, ik)
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
