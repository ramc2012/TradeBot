"""Event-driven R-multiple backtest for the regime-gated Gann engine.

The previous version just peeked 3 bars ahead of every confluence flag and
called the sign of the move a win — no stops, no targets, no position concept,
so it couldn't validate or tune anything. This one runs a real sequential
simulation: enter on an `evaluate_gann_signal` setup, then walk the tape bar by
bar applying the SAME underlying break-even / trailing-stop / Gann-target exit
logic the live agent uses, and score every trade in R-multiples. The summary
(win-rate, avg-R, profit-factor, expectancy, max-drawdown-R, per-archetype
breakdown) is what you actually tune thresholds against.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from gann_tp_delta.anchors import select_anchor
from gann_tp_delta.cycles import resolve_price_unit
from gann_tp_delta.geometry import gann_fan, price_time_square, square_of_nine, time_cycles
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.strategy import evaluate_gann_signal


class GannTPDeltaBacktester:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def run(
        self,
        frame: pd.DataFrame,
        *,
        anchor_mode: str,
        h_mode: str,
        entry_conviction: float = 0.0,
        underlying: str | None = None,
    ) -> dict[str, Any]:
        if frame.empty or len(frame.index) < 40:
            return self._empty("Not enough candles for backtest.")
        max_bars = int(self.config.get("backtest", {}).get("max_bars") or 600)
        if max_bars > 40 and len(frame.index) > max_bars:
            frame = frame.tail(max_bars).reset_index(drop=True)
        frame = frame.reset_index(drop=True)

        cfg = self.config
        gcfg = cfg["geometry"]
        risk_cfg = cfg.get("risk", {})
        be_at = float(risk_cfg.get("breakeven_at_r", 1.0))
        trail_start = float(risk_cfg.get("trail_start_r", 1.5))
        time_stop_bars = int(risk_cfg.get("time_stop_bars", 26))
        time_stop_min_r = float(risk_cfg.get("time_stop_min_r", 0.5))

        trades: list[dict[str, Any]] = []
        trade: dict[str, Any] | None = None

        for end in range(30, len(frame.index)):
            bar = frame.iloc[end]
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            # ── manage an open trade against THIS bar's range ───────────────
            if trade is not None:
                side, entry, R = trade["side"], trade["entry"], trade["R"]
                if side == "long":
                    trade["peak"] = max(trade["peak"], high)
                    if not trade["be_done"] and (close - entry) / R >= be_at:
                        trade["stop"] = max(trade["stop"], entry)
                        trade["be_done"] = True
                    peak_r = (trade["peak"] - entry) / R
                    if peak_r >= trail_start:
                        trade["stop"] = max(trade["stop"], entry + (peak_r - 1.0) * R)
                    exit_price, reason = None, None
                    if low <= trade["stop"]:
                        exit_price, reason = trade["stop"], "stop"
                    elif trade["target"] is not None and high >= trade["target"]:
                        exit_price, reason = trade["target"], "target"
                    elif end - trade["entry_index"] >= time_stop_bars and (close - entry) / R < time_stop_min_r:
                        exit_price, reason = close, "time"
                    if exit_price is not None:
                        trades.append(self._close(trade, exit_price, reason, end, frame))
                        trade = None
                else:  # short
                    trade["trough"] = min(trade["trough"], low)
                    if not trade["be_done"] and (entry - close) / R >= be_at:
                        trade["stop"] = min(trade["stop"], entry)
                        trade["be_done"] = True
                    trough_r = (entry - trade["trough"]) / R
                    if trough_r >= trail_start:
                        trade["stop"] = min(trade["stop"], entry - (trough_r - 1.0) * R)
                    exit_price, reason = None, None
                    if high >= trade["stop"]:
                        exit_price, reason = trade["stop"], "stop"
                    elif trade["target"] is not None and low <= trade["target"]:
                        exit_price, reason = trade["target"], "target"
                    elif end - trade["entry_index"] >= time_stop_bars and (entry - close) / R < time_stop_min_r:
                        exit_price, reason = close, "time"
                    if exit_price is not None:
                        trades.append(self._close(trade, exit_price, reason, end, frame))
                        trade = None

            # ── look for a new entry only when flat ─────────────────────────
            if trade is None:
                window = frame.iloc[: end + 1].reset_index(drop=True)
                anchor = select_anchor(window, mode=anchor_mode, config=cfg["anchors"])
                if anchor is None:
                    continue
                h, _ = harmonic_speed(window, mode=h_mode, anchor_config=cfg["anchors"], scaling_config=cfg["scaling"])
                angles = gann_fan(anchor=anchor, h=h.value, current_bar_index=end, current_price=close,
                                  ratios=gcfg["gann_ratios"], projection_bars=int(gcfg["projection_bars"]))
                # `geometry.price_unit` may be the string "auto" (the per
                # instrument Square-of-Nine chart scale). float("auto") raises,
                # so resolve it the same way service._snapshot does — otherwise
                # the backtester, tune_sweep and both validators all die on the
                # shipped config, and the backtest must stay identical to live.
                _unit_cfg = gcfg.get("price_unit", 1.0)
                _price_unit = (
                    resolve_price_unit(close)
                    if str(_unit_cfg).lower() == "auto"
                    else float(_unit_cfg or 1.0)
                )
                sq9 = square_of_nine(anchor_price=anchor.price, current_price=close,
                                     price_unit=_price_unit, degrees=gcfg["sq9_degrees"])
                cycles = time_cycles(anchor=anchor, current_bar_index=end, cycles=gcfg["bar_cycles"],
                                     window_bars=int(gcfg["cycle_window_bars"]))
                square = price_time_square(anchor=anchor, current_bar_index=end, current_price=close,
                                           h=h.value, tolerance=float(gcfg["squaring_tolerance"]))
                sig = evaluate_gann_signal(
                    frame=window,
                    anchor=anchor,
                    angles=angles,
                    sq9_levels=sq9,
                    cycles=cycles,
                    square=square,
                    h=h.value,
                    config=cfg,
                    underlying=underlying,
                )
                if (sig.side in ("long", "short") and sig.archetype
                        and sig.conviction >= entry_conviction
                        and sig.stop_underlying is not None and sig.risk_per_unit and sig.risk_per_unit > 0):
                    trade = {
                        "entry_index": end, "entry": close, "side": sig.side, "archetype": sig.archetype,
                        "stop": float(sig.stop_underlying), "R": float(sig.risk_per_unit),
                        "target": float(sig.targets_underlying[0]) if sig.targets_underlying else None,
                        "entry_time": pd.Timestamp(bar["time"]).isoformat(), "conviction": sig.conviction,
                        "peak": close, "trough": close, "be_done": False,
                    }

        # mark any still-open trade at the last close
        if trade is not None:
            last = frame.iloc[-1]
            trades.append(self._close(trade, float(last["close"]), "open_at_end", len(frame.index) - 1, frame))

        return self._summarize(trades)

    @staticmethod
    def _close(trade: dict[str, Any], exit_price: float, reason: str, end: int, frame: pd.DataFrame) -> dict[str, Any]:
        entry, R, side = trade["entry"], trade["R"], trade["side"]
        points = (exit_price - entry) if side == "long" else (entry - exit_price)
        return {
            "entry_time": trade["entry_time"],
            "exit_time": pd.Timestamp(frame.iloc[end]["time"]).isoformat(),
            "side": side,
            "archetype": trade["archetype"],
            "conviction": trade["conviction"],
            "entry": round(entry, 2),
            "exit": round(exit_price, 2),
            "bars_held": end - trade["entry_index"],
            "exit_reason": reason,
            "r_multiple": round(points / R, 3) if R else 0.0,
            "points": round(points, 2),
        }

    def _summarize(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        max_events = int(self.config.get("backtest", {}).get("max_events") or 200)
        if not trades:
            return self._empty("No setups triggered over the window.")
        rs = [float(t["r_multiple"]) for t in trades]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        # max drawdown on the cumulative R equity curve
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in rs:
            cum += r
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        by_arch: dict[str, dict[str, Any]] = {}
        for t in trades:
            a = by_arch.setdefault(t["archetype"], {"n": 0, "r": 0.0, "wins": 0})
            a["n"] += 1
            a["r"] += float(t["r_multiple"])
            a["wins"] += 1 if float(t["r_multiple"]) > 0 else 0
        for a in by_arch.values():
            a["r"] = round(a["r"], 2)
            a["win_rate_pct"] = round(100.0 * a["wins"] / a["n"], 1) if a["n"] else 0.0

        return {
            "summary": {
                "trades": len(trades),
                "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
                "total_r": round(sum(rs), 2),
                "expectancy_r": round(sum(rs) / len(rs), 3),
                "avg_win_r": round(gross_win / len(wins), 3) if wins else 0.0,
                "avg_loss_r": round(-gross_loss / len(losses), 3) if losses else 0.0,
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
                "max_drawdown_r": round(max_dd, 2),
                "by_archetype": by_arch,
                # Back-compat aliases so the existing research panel stays
                # populated (values are now in R units) until it's upgraded.
                "event_count": len(trades),
                "total_points": round(sum(rs), 2),
                "avg_win": round(gross_win / len(wins), 3) if wins else 0.0,
                "avg_loss": round(-gross_loss / len(losses), 3) if losses else 0.0,
            },
            "events": trades[-max_events:],
        }

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {
            "summary": {
                "trades": 0, "win_rate_pct": 0.0, "total_r": 0.0, "expectancy_r": 0.0,
                "avg_win_r": 0.0, "avg_loss_r": 0.0, "profit_factor": None,
                "max_drawdown_r": 0.0, "by_archetype": {},
                "event_count": 0, "total_points": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            },
            "events": [],
            "reason": reason,
        }
