"""Event-driven portfolio backtest for MACD Refined (spec §7, §8, §11).

Two backtest sources, sharing one portfolio overlay:

  • ``research`` (default headline) — replays the project's VALIDATED signal set
    `data/signals/macd_signals.parquet`, the research the strategy doc is built
    from (premium-MACD zero-cross, ATM, hold-to-window). It reproduces the
    documented edge (≈86% win, +114% median) and is the honest "existing data"
    backtest. Per spec §10 the rupee LEVEL of a compounding curve is an
    artifact — trust the structure, not the level.

  • ``engine`` — the CAUSAL forward generator (`signals.generate_signals` +
    hold-to-window simulation with the -50% catastrophe stop and round-trip
    slippage). This is what the live/paper book runs. It does NOT use the
    research's hindsight leg selection, so its numbers are materially lower —
    exactly the walk-forward gap the deploy protocol (§11) exists to measure.

The portfolio overlay applies separate CE/PE books, slot limits, one-leg-per-
stock, the daily new-entry cap, liquidity-scaled compounding sizing, and the
kill switch to produce an honest equity curve from either source.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from macd_refined.data import MacdRefinedDataStore
from macd_refined.risk import kill_switch_state, size_position
from macd_refined.schemas import MacdTrade
from macd_refined.signals import generate_signals


def _net_metrics(returns: list[float]) -> dict[str, float]:
    if not returns:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0,
            "median_return_pct": 0.0, "mean_return_pct": 0.0,
            "profit_factor": 0.0, "pct_below_minus_50": 0.0,
        }
    arr = np.asarray(returns, dtype=float)
    wins = int((arr > 0).sum())
    gross_profit = float(arr[arr > 0].sum())
    gross_loss = float(-arr[arr < 0].sum())
    pf = gross_profit / gross_loss if gross_loss > 1e-9 else (999.0 if gross_profit > 0 else 0.0)
    return {
        "trades": int(arr.size),
        "wins": wins,
        "win_rate": round(wins / arr.size, 4),
        "median_return_pct": round(float(np.median(arr)), 4),
        "mean_return_pct": round(float(arr.mean()), 4),
        "profit_factor": round(pf, 4),
        "pct_below_minus_50": round(float((arr <= -50.0).mean()), 4),
    }


class MacdRefinedBacktester:
    def __init__(self, store: MacdRefinedDataStore, config: dict[str, Any]):
        self.store = store
        self.config = config

    # ── Public entry point ────────────────────────────────────────────────
    def run(
        self,
        *,
        source: str = "research",
        underlyings: Optional[list[str]] = None,
        expiry_count: Optional[int] = None,
    ) -> dict[str, Any]:
        source = (source or "research").lower()
        if source == "engine":
            candidates, sig_stats = self._engine_candidates(underlyings, expiry_count)
        else:
            candidates, sig_stats = self._research_candidates(underlyings, expiry_count)
            source = "research"

        signal_returns = [c["return_pct"] for c in candidates]
        portfolio = self._portfolio(candidates)
        return {
            "source": source,
            "config_summary": self._config_summary(),
            "signals": {
                **sig_stats,
                "signal_level_metrics": _net_metrics(signal_returns),
            },
            "portfolio": portfolio,
        }

    # ── Research replay (validated signal set) ────────────────────────────
    def _research_candidates(
        self, underlyings: Optional[list[str]], expiry_count: Optional[int]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = Path(self.config["data_root"]) / "signals" / "macd_signals.parquet"
        if not path.exists():
            return [], {"generated": 0, "accepted": 0, "note": f"missing {path}"}
        df = pd.read_parquet(path)
        df = df[df["has_signal"] == True].copy()  # noqa: E712
        df["expiry"] = df["expiry"].astype(str).str[:10]
        df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
        df = df.dropna(subset=["signal_date"])

        # Restrict to the most recent N expiries for parity with the engine run.
        if expiry_count:
            keep = sorted(df["expiry"].unique())[-int(expiry_count):]
            df = df[df["expiry"].isin(keep)]
        if underlyings:
            req = set(u.upper() for u in underlyings)
            df = df[df["underlying"].str.upper().isin(req)]
        df = df.sort_values("signal_date")

        candidates: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            entry_price = float(r.get("entry_price") or 0.0)
            ret = r.get("exit_window_return")
            if entry_price <= 0 or ret is None or not np.isfinite(ret):
                continue
            underlying = str(r["underlying"])
            try:
                exp_date = date.fromisoformat(str(r["expiry"]))
            except ValueError:
                continue
            entry_ts = pd.Timestamp(r["signal_date"])
            window_days = int(self.config["filters"]["entry_window_days_before_expiry"])
            exit_ts = pd.Timestamp(exp_date - timedelta(days=window_days))
            if exit_ts <= entry_ts:
                exit_ts = pd.Timestamp(exp_date)
            candidates.append(
                {
                    "underlying": underlying,
                    "book": str(r["opt_type"]),
                    "strike": float(r.get("atm_strike") or 0.0),
                    "expiry": exp_date.isoformat(),
                    "entry_time": entry_ts,
                    "exit_time": exit_ts,
                    "entry_gross": entry_price,
                    "entry_fill": entry_price,         # research figures are gross
                    "exit_gross": entry_price * (1 + float(ret) / 100.0),
                    "return_pct": float(ret),
                    "max_favorable_pct": float(r.get("max_return") or ret),
                    "max_adverse_pct": 0.0,
                    "exit_reason": "window_end",
                    "holding_bars": int(r.get("total_window_bars") or 0),
                    "lot": self.store.lot_size_for(underlying),
                    "daily_turnover_rupees": 0.0,    # not tracked in research file
                    "entry_iv": float(r.get("entry_iv_pct") or 0.0) / 100.0,
                    "iv_rank": None,
                    "direction_bias": "up" if str(r["opt_type"]) == "CE" else "down",
                    "signal_kind": "research_validated",
                }
            )
        stats = {
            "generated": int(len(df)),
            "accepted": len(candidates),
            "simulated": len(candidates),
            "note": "replay of data/signals/macd_signals.parquet (validated research; pure hold-to-window, gross)",
        }
        return candidates, stats

    # ── Engine (causal forward) ───────────────────────────────────────────
    def _engine_candidates(
        self, underlyings: Optional[list[str]], expiry_count: Optional[int]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        bt_cfg = self.config["backtest"]
        expiry_count = expiry_count if expiry_count is not None else bt_cfg.get("lookback_expiries")
        files = self.store.recent_expiry_files(expiry_count)
        requested = set(u.upper() for u in underlyings) if underlyings else None
        max_u = bt_cfg.get("max_underlyings")

        candidates: list[dict[str, Any]] = []
        generated = accepted = 0
        underlyings_seen: set[str] = set()

        for expiry_date, path in files:
            frame = self.store.load_expiry_frame(path)
            if frame.empty:
                continue
            file_underlyings = [str(u) for u in frame["underlying"].dropna().unique()]
            if requested is not None:
                file_underlyings = [u for u in file_underlyings if u.upper() in requested]
            if max_u:
                file_underlyings = file_underlyings[: int(max_u)]
            for underlying in file_underlyings:
                u_frame = frame[frame["underlying"] == underlying]
                if u_frame.empty:
                    continue
                spot = self.store.load_spot(underlying)
                iv_hist = self.store.atm_iv_daily(underlying)
                lot = self.store.lot_size_for(underlying)
                sigs = generate_signals(
                    underlying=underlying, expiry=expiry_date, option_frame=u_frame,
                    spot_frame=spot, atm_iv_history=iv_hist, config=self.config,
                    contract_lot_size=lot,
                )
                generated += len(sigs)
                underlyings_seen.add(underlying)
                groups = {(ot, float(stk)): grp for (ot, stk), grp in u_frame.groupby(["option_type", "strike"])}
                for sig in sigs:
                    if not sig.accepted:
                        continue
                    accepted += 1
                    contract = groups.get((sig.option_type, float(sig.strike)))
                    if contract is None:
                        continue
                    sim = self._simulate_trade(contract, sig, expiry_date)
                    if sim is None:
                        continue
                    candidates.append(
                        {
                            "underlying": sig.underlying,
                            "book": sig.option_type,
                            "strike": sig.strike,
                            "expiry": sig.expiry,
                            "entry_time": pd.Timestamp(sim["entry_time"]),
                            "exit_time": pd.Timestamp(sim["exit_time"]),
                            "entry_gross": sim["entry_gross"],
                            "entry_fill": sim["entry_fill"],
                            "exit_gross": sim["exit_gross"],
                            "return_pct": sim["return_pct_per_unit"],
                            "max_favorable_pct": sim["max_favorable_pct"],
                            "max_adverse_pct": sim["max_adverse_pct"],
                            "exit_reason": sim["exit_reason"],
                            "holding_bars": sim["holding_bars"],
                            "lot": lot,
                            "daily_turnover_rupees": sig.daily_turnover_rupees,
                            "entry_iv": sig.iv,
                            "iv_rank": sig.iv_rank,
                            "direction_bias": sig.direction_bias,
                            "signal_kind": sig.signal_kind,
                        }
                    )
        stats = {
            "generated": generated,
            "accepted": accepted,
            "simulated": len(candidates),
            "underlyings_scanned": sorted(underlyings_seen),
            "underlyings_count": len(underlyings_seen),
            "expiry_files": [d.isoformat() for d, _ in files],
            "note": "causal forward engine (no hindsight leg selection); -50% stop + slippage applied",
        }
        return candidates, stats

    # ── Single trade simulation (hold to window end, -50% stop) ───────────
    def _simulate_trade(self, contract: pd.DataFrame, signal, expiry: date) -> Optional[dict[str, Any]]:
        exec_cfg = self.config["execution"]
        exits_cfg = self.config["exits"]
        slip = float(exec_cfg["round_trip_slippage_pct"])
        window_days = int(self.config["filters"]["entry_window_days_before_expiry"])
        stop_pct = float(exits_cfg["catastrophe_stop_pct"])
        stop_basis = str(exits_cfg.get("catastrophe_stop_basis", "bar_close")).lower()

        contract = contract.sort_values("time").reset_index(drop=True)
        sig_ts = pd.Timestamp(signal.signal_time)
        after = contract.index[contract["time"] > sig_ts]
        if len(after) == 0:
            return None
        entry_idx = int(after[0])
        entry_gross = float(contract.iloc[entry_idx]["open"] or contract.iloc[entry_idx]["close"] or 0.0)
        if entry_gross <= 0:
            return None
        entry_fill = entry_gross * (1.0 + slip / 2.0)
        stop_price = entry_gross * (1.0 - stop_pct)
        window_end = expiry - timedelta(days=window_days)

        exit_idx = entry_idx
        exit_gross = float(contract.iloc[entry_idx]["close"] or entry_gross)
        exit_reason = "expiry"
        peak = entry_gross
        trough = entry_gross

        for j in range(entry_idx, len(contract)):
            bar = contract.iloc[j]
            bar_date = pd.Timestamp(bar["time"]).date()
            hi = float(bar["high"] or bar["close"] or 0.0)
            lo = float(bar["low"] or bar["close"] or 0.0)
            cl = float(bar["close"] or 0.0)
            peak = max(peak, hi)
            trough = min(trough, lo if lo > 0 else trough)
            if stop_basis != "off":
                breached = (lo > 0 and lo <= stop_price) if stop_basis == "intrabar_low" else (cl > 0 and cl <= stop_price)
                if breached:
                    exit_idx = j
                    exit_gross = stop_price if stop_basis == "intrabar_low" else cl
                    exit_reason = "catastrophe_stop"
                    break
            if bar_date >= window_end:
                exit_idx = j
                exit_gross = cl if cl > 0 else exit_gross
                exit_reason = "window_end"
                break
            exit_idx = j
            exit_gross = cl if cl > 0 else exit_gross

        exit_fill = exit_gross * (1.0 - slip / 2.0)
        return_pct = (exit_fill / entry_fill - 1.0) * 100.0
        return {
            "entry_time": pd.Timestamp(contract.iloc[entry_idx]["time"]).isoformat(),
            "entry_gross": entry_gross,
            "entry_fill": entry_fill,
            "exit_time": pd.Timestamp(contract.iloc[exit_idx]["time"]).isoformat(),
            "exit_gross": exit_gross,
            "exit_fill": exit_fill,
            "exit_reason": exit_reason,
            "holding_bars": exit_idx - entry_idx,
            "return_pct_per_unit": return_pct,
            "max_adverse_pct": (trough / entry_gross - 1.0) * 100.0,
            "max_favorable_pct": (peak / entry_gross - 1.0) * 100.0,
        }

    # ── Portfolio overlay (shared) ────────────────────────────────────────
    def _portfolio(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        port_cfg = self.config["portfolio"]
        risk_cfg = self.config["risk"]
        sizing_cfg = self.config["sizing"]
        start_equity = float(risk_cfg["starting_equity"])

        events = sorted(candidates, key=lambda c: c["entry_time"])
        ce_slots = int(port_cfg["ce_slots"])
        pe_slots = int(port_cfg["pe_slots"])
        one_leg = bool(port_cfg["one_leg_per_stock"])
        daily_cap = int(port_cfg["daily_new_entry_cap"])

        equity = peak_equity = start_equity
        realized = 0.0
        open_positions: list[dict[str, Any]] = []
        booked: list[MacdTrade] = []
        closed_returns_pct: list[float] = []
        equity_curve: list[dict[str, Any]] = [{"time": None, "equity": round(equity, 2)}]
        daily_count: dict[date, int] = {}
        skips = {"slot_full": 0, "one_leg": 0, "daily_cap": 0, "sizing": 0, "kill_switch": 0}

        def _close_due(now: pd.Timestamp) -> None:
            nonlocal equity, realized, peak_equity
            # Settle in EXIT-TIME order so the equity curve is chronological
            # (max-drawdown is path-dependent) and closed_returns_pct feeds the
            # kill switch in true close order. Positions are appended in
            # entry-time order, which is NOT exit-time order when holds overlap.
            due = sorted((p for p in open_positions if p["exit_time"] <= now), key=lambda p: p["exit_time"])
            open_positions[:] = [p for p in open_positions if p["exit_time"] > now]
            for pos in due:
                realized += pos["pnl_rupees"]
                equity += pos["pnl_rupees"]
                peak_equity = max(peak_equity, equity)
                closed_returns_pct.append(pos["return_pct"])
                equity_curve.append({"time": pos["exit_time"].isoformat(), "equity": round(equity, 2)})

        for ev in events:
            now = ev["entry_time"]
            _close_due(now)
            book = ev["book"]
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            paused, _ = kill_switch_state(closed_returns_pct, risk_cfg=risk_cfg, current_drawdown_pct=dd)
            if paused:
                skips["kill_switch"] += 1
                continue
            if one_leg and any(p["underlying"] == ev["underlying"] for p in open_positions):
                skips["one_leg"] += 1
                continue
            book_open = sum(1 for p in open_positions if p["book"] == book)
            if (book == "CE" and book_open >= ce_slots) or (book == "PE" and book_open >= pe_slots):
                skips["slot_full"] += 1
                continue
            d = now.date()
            if daily_count.get(d, 0) >= daily_cap:
                skips["daily_cap"] += 1
                continue
            # Turnover-scaled sizing when turnover is known (engine); otherwise
            # (research replay) the equity-fraction + per-name cap govern.
            turnover = float(ev.get("daily_turnover_rupees") or 0.0)
            if turnover <= 0:
                turnover = float("inf")  # let equity/cap bind
            sized = size_position(
                premium=ev["entry_fill"],
                lot_size=int(ev["lot"]),
                daily_turnover_rupees=turnover,
                equity=equity if port_cfg.get("reinvest", True) else start_equity,
                sizing_cfg=sizing_cfg,
            )
            if not sized.accepted:
                skips["sizing"] += 1
                continue
            # P&L from the (already slippage-adjusted) per-unit return.
            pnl_rupees = ev["entry_fill"] * (ev["return_pct"] / 100.0) * sized.qty_units
            booked.append(
                MacdTrade(
                    underlying=ev["underlying"],
                    trading_symbol=f"{ev['underlying']} {ev.get('strike', 0):.0f} {book}",
                    instrument_key="",
                    option_type=book,
                    expiry=ev.get("expiry", ""),
                    strike=float(ev.get("strike") or 0.0),
                    lot_size=int(ev["lot"]),
                    signal_time=ev["entry_time"].isoformat(),
                    entry_time=ev["entry_time"].isoformat(),
                    entry_premium=ev["entry_gross"],
                    entry_fill_premium=ev["entry_fill"],
                    qty_lots=sized.qty_lots,
                    qty_units=sized.qty_units,
                    notional_rupees=sized.notional_rupees,
                    book=book,
                    exit_time=ev["exit_time"].isoformat(),
                    exit_premium=ev["exit_gross"],
                    exit_fill_premium=ev["exit_gross"],
                    exit_reason=ev.get("exit_reason", ""),
                    holding_bars=int(ev.get("holding_bars") or 0),
                    pnl_rupees=round(pnl_rupees, 2),
                    return_pct=round(ev["return_pct"], 4),
                    max_adverse_pct=round(ev.get("max_adverse_pct") or 0.0, 4),
                    max_favorable_pct=round(ev.get("max_favorable_pct") or 0.0, 4),
                    entry_iv=ev.get("entry_iv") or 0.0,
                    iv_rank=ev.get("iv_rank"),
                    direction_bias=ev.get("direction_bias", "neutral"),
                    signal_kind=ev.get("signal_kind", ""),
                )
            )
            daily_count[d] = daily_count.get(d, 0) + 1
            open_positions.append(
                {
                    "underlying": ev["underlying"],
                    "book": book,
                    "exit_time": ev["exit_time"],
                    "pnl_rupees": round(pnl_rupees, 2),
                    "return_pct": ev["return_pct"],
                }
            )

        _close_due(pd.Timestamp.max)

        ce_returns = [t.return_pct for t in booked if t.book == "CE"]
        pe_returns = [t.return_pct for t in booked if t.book == "PE"]
        all_returns = [t.return_pct for t in booked]
        return {
            "starting_equity": start_equity,
            "ending_equity": round(equity, 2),
            "total_pnl_rupees": round(sum(t.pnl_rupees for t in booked), 2),
            "total_return_pct": round((equity / start_equity - 1.0) * 100.0, 4) if start_equity else 0.0,
            "max_drawdown_pct": round(self._max_drawdown(equity_curve), 4),
            "booked_trades": len(booked),
            "skips": skips,
            "books": {
                "CE": _net_metrics(ce_returns),
                "PE": _net_metrics(pe_returns),
                "ALL": _net_metrics(all_returns),
            },
            "ce_pnl_rupees": round(sum(t.pnl_rupees for t in booked if t.book == "CE"), 2),
            "pe_pnl_rupees": round(sum(t.pnl_rupees for t in booked if t.book == "PE"), 2),
            "equity_curve": equity_curve[-500:],
            "sample_trades": [self._trade_payload(t) for t in booked[:60]],
            "note": "Rupee LEVEL of the curve is a compounding artifact (spec §10) — trust structure, not level.",
        }

    @staticmethod
    def _max_drawdown(equity_curve: list[dict[str, Any]]) -> float:
        eq = np.asarray([pt["equity"] for pt in equity_curve], dtype=float)
        if eq.size < 2:
            return 0.0
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.where(peak > 0, peak, 1.0)
        return float(dd.max())

    @staticmethod
    def _trade_payload(t: MacdTrade) -> dict[str, Any]:
        return {
            "underlying": t.underlying, "book": t.book, "trading_symbol": t.trading_symbol,
            "option_type": t.option_type, "strike": t.strike, "expiry": t.expiry,
            "entry_time": t.entry_time, "exit_time": t.exit_time,
            "entry_premium": round(t.entry_premium, 2), "exit_premium": round(t.exit_premium or 0.0, 2),
            "exit_reason": t.exit_reason, "qty_lots": t.qty_lots, "qty_units": t.qty_units,
            "pnl_rupees": t.pnl_rupees, "return_pct": t.return_pct,
            "max_adverse_pct": t.max_adverse_pct, "max_favorable_pct": t.max_favorable_pct,
            "entry_iv": round(t.entry_iv, 4), "iv_rank": round(t.iv_rank, 4) if t.iv_rank is not None else None,
            "direction_bias": t.direction_bias,
        }

    def _config_summary(self) -> dict[str, Any]:
        s, f, p, e, r, x = (
            self.config["signal"], self.config["filters"], self.config["portfolio"],
            self.config["execution"], self.config["risk"], self.config["exits"],
        )
        return {
            "macd": [s["macd_fast"], s["macd_slow"], s["macd_signal"]],
            "timeframe": self.config["timeframe"],
            "iv_rank_max": f["iv_rank_max"],
            "min_daily_turnover_rupees": f["min_daily_turnover_rupees"],
            "entry_window_days_before_expiry": f["entry_window_days_before_expiry"],
            "catastrophe_stop_pct": x["catastrophe_stop_pct"],
            "catastrophe_stop_basis": x.get("catastrophe_stop_basis"),
            "ce_slots": p["ce_slots"], "pe_slots": p["pe_slots"],
            "round_trip_slippage_pct": e["round_trip_slippage_pct"],
            "starting_equity": r["starting_equity"],
        }
