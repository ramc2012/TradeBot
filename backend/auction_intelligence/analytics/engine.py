"""
MP Intelligence Analytics Engine
=================================
Computes multi-timeframe profile aggregates, regime history, setup performance
matrix, value-migration trend, and concept-drift signals from the daily MP CSV
data already available in the system.

All methods accept a list[dict] of rows from enriched_mp_with_failures.csv
(or daily_mp_params.csv for lighter weight calls) so they are fully stateless
and trivially testable.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flt(row: dict, key: str, default: float = 0.0) -> float:
    try:
        val = row.get(key)
        return float(val) if val not in (None, "", "nan", "NaN") else default
    except (ValueError, TypeError):
        return default


def _bool(row: dict, key: str) -> bool:
    return str(row.get(key, "")).lower() in ("true", "1", "yes")


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _iso_week(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------------------
# Composite / Weekly profile aggregation
# ---------------------------------------------------------------------------

class MPAnalyticsEngine:
    """Stateless analytics over MP session rows."""

    # ------------------------------------------------------------------
    # 1. Multi-TF profile snapshots
    # ------------------------------------------------------------------

    def build_composite_profile(
        self,
        rows: list[dict],
        lookback: int = 20,
        label: str | None = None,
    ) -> dict:
        """
        Approximate composite profile from the last `lookback` sessions.

        Uses daily MP params (poc, vah, val, session_high, session_low,
        total_tpos) to construct a weighted value area.

        Returns a profile dict compatible with the FMP tpo_rows shape so the
        frontend can render it with the same component.
        """
        window = [r for r in rows if _parse_date(r.get("date", "")) is not None]
        window = sorted(window, key=lambda r: r["date"])[-lookback:]
        if not window:
            return {}

        # Weighted POC = tpos-weighted mean of session POCs
        total_tpos = sum(_flt(r, "total_tpos", 1.0) for r in window)
        if total_tpos <= 0:
            total_tpos = len(window)

        w_poc = sum(_flt(r, "poc") * _flt(r, "total_tpos", 1.0) for r in window) / total_tpos
        w_vah = sum(_flt(r, "vah") * _flt(r, "total_tpos", 1.0) for r in window) / total_tpos
        w_val = sum(_flt(r, "val") * _flt(r, "total_tpos", 1.0) for r in window) / total_tpos

        composite_high = max(_flt(r, "session_high") for r in window)
        composite_low = min(_flt(r, "session_low") for r in window)

        # Build a synthetic TPO count from daily VA coverage
        # Bucket size = median ibr / 4 (gives ~10-30 price levels)
        ibrs = sorted(_flt(r, "ibr") for r in window if _flt(r, "ibr") > 0)
        median_ibr = ibrs[len(ibrs) // 2] if ibrs else 50.0
        tick = max(median_ibr / 6.0, 1.0)
        tick = round(tick, 2)

        # Each session contributes TPO weight to prices in its value area
        price_weights: dict[float, float] = defaultdict(float)
        for r in window:
            session_tpos = _flt(r, "total_tpos", 1.0)
            vah = _flt(r, "vah")
            val = _flt(r, "val")
            poc = _flt(r, "poc")
            if vah <= val or poc <= 0:
                continue
            # POC gets 2x weight, value area edges get 1x
            p = val
            while p <= vah + tick * 0.5:
                rounded = round(round(p / tick) * tick, 4)
                dist_from_poc = abs(rounded - poc)
                va_range = max(vah - val, tick)
                # Weight decays linearly from POC
                w = session_tpos * max(1.0 - dist_from_poc / va_range, 0.3)
                price_weights[rounded] += w
                p += tick

        tpo_rows = [
            {"price": round(p, 4), "count": round(c, 2), "letters": ""}
            for p, c in sorted(price_weights.items())
        ]

        # Composite value area from weighted distribution
        if tpo_rows:
            total_w = sum(r["count"] for r in tpo_rows)
            target_w = total_w * 0.70
            # Find POC of composite
            comp_poc_row = max(tpo_rows, key=lambda r: r["count"])
            comp_poc = comp_poc_row["price"]
            # Expand from POC to cover 70% of weight
            comp_val, comp_vah = self._value_area_from_rows(tpo_rows, comp_poc, target_w)
        else:
            comp_poc = w_poc
            comp_val = w_val
            comp_vah = w_vah

        return {
            "scope": label or f"composite_{lookback}d",
            "lookback_sessions": len(window),
            "session_start": window[0]["date"],
            "session_end": window[-1]["date"],
            "high_price": round(composite_high, 2),
            "low_price": round(composite_low, 2),
            "poc": round(comp_poc, 2),
            "vah": round(comp_vah, 2),
            "val": round(comp_val, 2),
            "weighted_poc": round(w_poc, 2),
            "weighted_vah": round(w_vah, 2),
            "weighted_val": round(w_val, 2),
            "tick_size": round(tick, 4),
            "tpo_rows": tpo_rows,
            "va_width": round(comp_vah - comp_val, 2),
        }

    def _value_area_from_rows(
        self,
        tpo_rows: list[dict],
        poc_price: float,
        target_weight: float,
    ) -> tuple[float, float]:
        prices = [r["price"] for r in tpo_rows]
        counts = {r["price"]: r["count"] for r in tpo_rows}
        if poc_price not in prices:
            # Find closest
            poc_price = min(prices, key=lambda p: abs(p - poc_price))
        poc_idx = prices.index(poc_price)
        lo = hi = poc_idx
        covered = counts[poc_price]
        while covered < target_weight and (lo > 0 or hi < len(prices) - 1):
            can_go_down = lo > 0
            can_go_up = hi < len(prices) - 1
            if can_go_down and can_go_up:
                if counts[prices[hi + 1]] >= counts[prices[lo - 1]]:
                    hi += 1
                    covered += counts[prices[hi]]
                else:
                    lo -= 1
                    covered += counts[prices[lo]]
            elif can_go_down:
                lo -= 1
                covered += counts[prices[lo]]
            else:
                hi += 1
                covered += counts[prices[hi]]
        return prices[lo], prices[hi]

    def build_weekly_profiles(self, rows: list[dict]) -> list[dict]:
        """Group daily sessions into ISO weeks and compute week-level profile."""
        weeks: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            d = _parse_date(r.get("date", ""))
            if d:
                weeks[_iso_week(d)].append(r)

        result = []
        for week_key in sorted(weeks):
            week_rows = sorted(weeks[week_key], key=lambda r: r["date"])
            if not week_rows:
                continue
            session_high = max(_flt(r, "session_high") for r in week_rows)
            session_low = min(_flt(r, "session_low") for r in week_rows)
            total_tpos = sum(_flt(r, "total_tpos", 1.0) for r in week_rows)
            if total_tpos <= 0:
                continue
            w_poc = sum(_flt(r, "poc") * _flt(r, "total_tpos", 1.0) for r in week_rows) / total_tpos
            w_vah = sum(_flt(r, "vah") * _flt(r, "total_tpos", 1.0) for r in week_rows) / total_tpos
            w_val = sum(_flt(r, "val") * _flt(r, "total_tpos", 1.0) for r in week_rows) / total_tpos

            result.append({
                "scope": "weekly",
                "week": week_key,
                "sessions": len(week_rows),
                "start_date": week_rows[0]["date"],
                "end_date": week_rows[-1]["date"],
                "high_price": round(session_high, 2),
                "low_price": round(session_low, 2),
                "poc": round(w_poc, 2),
                "vah": round(w_vah, 2),
                "val": round(w_val, 2),
                "total_tpos": round(total_tpos, 0),
                "day_types": [_classify_day_type(r) for r in week_rows],
            })
        return result

    # ------------------------------------------------------------------
    # 2. Value migration trend
    # ------------------------------------------------------------------

    def value_migration_trend(
        self,
        rows: list[dict],
        lookback: int = 60,
        ma_period: int = 10,
    ) -> dict:
        """
        Track POC shift, VA center, VA width, and close-vs-POC over time.
        Returns time-series for charting.
        """
        window = sorted(rows, key=lambda r: r.get("date", ""))[-lookback:]
        if len(window) < 2:
            return {"sessions": [], "summary": {}}

        sessions = []
        prev_poc: float | None = None
        prev_va_center: float | None = None

        for r in window:
            poc = _flt(r, "poc")
            vah = _flt(r, "vah")
            val = _flt(r, "val")
            close = _flt(r, "close_price") or _flt(r, "close")
            session_high = _flt(r, "session_high")
            session_low = _flt(r, "session_low")
            va_center = (vah + val) / 2.0
            va_width = vah - val
            day_range = max(session_high - session_low, 0.0)
            close_vs_poc = close - poc
            close_location = (close - session_low) / max(day_range, 1.0)

            poc_shift = round(poc - prev_poc, 2) if prev_poc is not None else 0.0
            va_center_shift = round(va_center - prev_va_center, 2) if prev_va_center is not None else 0.0

            sessions.append({
                "date": r.get("date", ""),
                "poc": round(poc, 2),
                "vah": round(vah, 2),
                "val": round(val, 2),
                "va_center": round(va_center, 2),
                "va_width": round(va_width, 2),
                "close": round(close, 2),
                "close_vs_poc": round(close_vs_poc, 2),
                "close_location": round(close_location, 4),
                "poc_shift": poc_shift,
                "va_center_shift": va_center_shift,
                "day_type": _classify_day_type(r),
                "buyer_fail": _flt(r, "buyer_fail_score"),
                "seller_fail": _flt(r, "seller_fail_score"),
                "net_failure": round(_flt(r, "seller_fail_score") - _flt(r, "buyer_fail_score"), 2),
            })

            prev_poc = poc
            prev_va_center = va_center

        # Add rolling MA for smoothing
        for i, s in enumerate(sessions):
            window_slice = sessions[max(0, i - ma_period + 1): i + 1]
            s["poc_ma"] = round(sum(x["poc"] for x in window_slice) / len(window_slice), 2)
            s["va_center_ma"] = round(sum(x["va_center"] for x in window_slice) / len(window_slice), 2)
            s["va_width_ma"] = round(sum(x["va_width"] for x in window_slice) / len(window_slice), 2)

        # Summary statistics
        poc_shifts = [s["poc_shift"] for s in sessions[1:]]
        net_failures = [s["net_failure"] for s in sessions]
        upward_migrations = sum(1 for x in poc_shifts if x > 0)
        downward_migrations = sum(1 for x in poc_shifts if x < 0)

        summary = {
            "avg_poc_shift": round(sum(poc_shifts) / len(poc_shifts), 2) if poc_shifts else 0.0,
            "upward_migration_pct": round(upward_migrations / len(poc_shifts) * 100, 1) if poc_shifts else 0.0,
            "downward_migration_pct": round(downward_migrations / len(poc_shifts) * 100, 1) if poc_shifts else 0.0,
            "avg_net_failure": round(sum(net_failures) / len(net_failures), 2) if net_failures else 0.0,
            "cumulative_poc_shift": round(sessions[-1]["poc"] - sessions[0]["poc"], 2) if sessions else 0.0,
            "cumulative_va_center_shift": round(sessions[-1]["va_center"] - sessions[0]["va_center"], 2) if sessions else 0.0,
            "avg_va_width": round(sum(s["va_width"] for s in sessions) / len(sessions), 2) if sessions else 0.0,
        }

        return {"sessions": sessions, "summary": summary}

    # ------------------------------------------------------------------
    # 3. Regime history + transition matrix
    # ------------------------------------------------------------------

    def regime_history(self, rows: list[dict], lookback: int = 60) -> dict:
        """
        Day-type sequence with transition probability matrix.
        Also computes consecutive run detection (trend streaks, balance periods).
        """
        window = sorted(rows, key=lambda r: r.get("date", ""))[-lookback:]
        if not window:
            return {"sessions": [], "transition_matrix": {}, "streaks": []}

        day_types_ordered = [
            "TREND_UP", "TREND_DN", "NORMAL_VAR_UP", "NORMAL_VAR_DN",
            "FAILED_AUCTION", "DOUBLE_DIST", "NORMAL", "UNKNOWN",
        ]

        sessions = []
        for r in window:
            dt = _classify_day_type(r)
            buyer_fail = _flt(r, "buyer_fail_score")
            seller_fail = _flt(r, "seller_fail_score")
            next_day = _flt(r, "next_day_move")
            next_3d = _flt(r, "next_3d_move")
            sessions.append({
                "date": r.get("date", ""),
                "day_type": dt,
                "direction": _signal_direction(dt, buyer_fail, seller_fail),
                "buyer_fail": buyer_fail,
                "seller_fail": seller_fail,
                "next_day_move": round(next_day, 2),
                "next_3d_move": round(next_3d, 2),
                "poc": _flt(r, "poc"),
                "vah": _flt(r, "vah"),
                "val": _flt(r, "val"),
                "poor_high": _bool(r, "poor_high"),
                "poor_low": _bool(r, "poor_low"),
            })

        # Transition matrix
        transitions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for prev, curr in zip(sessions, sessions[1:]):
            transitions[prev["day_type"]][curr["day_type"]] += 1

        # Normalize to probabilities
        transition_matrix: dict[str, dict[str, float]] = {}
        for from_type, to_counts in transitions.items():
            total = sum(to_counts.values())
            transition_matrix[from_type] = {
                to_type: round(count / total, 3)
                for to_type, count in sorted(to_counts.items(), key=lambda x: -x[1])
            }

        # Day-type distribution
        type_counts = Counter(s["day_type"] for s in sessions)
        distribution = [
            {
                "day_type": dt,
                "count": type_counts.get(dt, 0),
                "pct": round(type_counts.get(dt, 0) / len(sessions) * 100, 1),
            }
            for dt in day_types_ordered
            if type_counts.get(dt, 0) > 0
        ]

        # Streak detection
        streaks = []
        if sessions:
            current_type = sessions[0]["day_type"]
            streak_start = sessions[0]["date"]
            streak_len = 1
            for s in sessions[1:]:
                if s["day_type"] == current_type:
                    streak_len += 1
                else:
                    if streak_len >= 2:
                        streaks.append({
                            "day_type": current_type,
                            "length": streak_len,
                            "start_date": streak_start,
                            "end_date": s["date"],
                        })
                    current_type = s["day_type"]
                    streak_start = s["date"]
                    streak_len = 1
            if streak_len >= 2:
                streaks.append({
                    "day_type": current_type,
                    "length": streak_len,
                    "start_date": streak_start,
                    "end_date": sessions[-1]["date"],
                })

        return {
            "sessions": sessions,
            "distribution": distribution,
            "transition_matrix": transition_matrix,
            "streaks": sorted(streaks, key=lambda x: -x["length"])[:10],
        }

    # ------------------------------------------------------------------
    # 4. Setup performance matrix
    # ------------------------------------------------------------------

    def setup_performance(self, rows: list[dict]) -> dict:
        """
        Conditional outcome table for each (day_type, direction, signal_strength)
        combination using next_day_move and next_3d_move from the enriched CSV.

        Returns win rates, average returns, expectancy, and calibration data.
        """
        # Only include rows that have forward outcome data
        rows_with_outcome = [
            r for r in rows
            if r.get("next_day_move") not in (None, "", "nan", "NaN")
        ]

        matrix: dict[str, dict] = defaultdict(lambda: {
            "count": 0,
            "next_day_moves": [],
            "next_3d_moves": [],
            "win_days": 0,
            "win_3ds": 0,
        })

        for r in rows_with_outcome:
            dt = _classify_day_type(r)
            buyer_fail = _flt(r, "buyer_fail_score")
            seller_fail = _flt(r, "seller_fail_score")
            direction = _signal_direction(dt, buyer_fail, seller_fail)
            strength = _signal_strength(direction, buyer_fail, seller_fail)

            next_day = _flt(r, "next_day_move")
            next_3d = _flt(r, "next_3d_move")

            # Win = move in correct direction for the signal
            if direction == "CE":
                win_day = next_day > 0
                win_3d = next_3d > 0
            elif direction == "PE":
                win_day = next_day < 0
                win_3d = next_3d < 0
            else:
                win_day = False
                win_3d = False

            key = f"{dt}|{direction}|{strength}"
            cell = matrix[key]
            cell["count"] += 1
            cell["next_day_moves"].append(next_day)
            cell["next_3d_moves"].append(next_3d)
            if win_day:
                cell["win_days"] += 1
            if win_3d:
                cell["win_3ds"] += 1

        # Compute stats per cell
        cells = []
        for key, cell in matrix.items():
            parts = key.split("|")
            dt, direction, strength = parts[0], parts[1], parts[2]
            n = cell["count"]
            if n == 0:
                continue

            nd_moves = cell["next_day_moves"]
            n3d_moves = cell["next_3d_moves"]
            avg_nd = sum(nd_moves) / n
            avg_n3d = sum(n3d_moves) / n
            std_nd = _std(nd_moves)
            win_rate_1d = cell["win_days"] / n
            win_rate_3d = cell["win_3ds"] / n
            expectancy_1d = (win_rate_1d * avg_nd) - ((1 - win_rate_1d) * abs(avg_nd))

            cells.append({
                "day_type": dt,
                "direction": direction,
                "strength": strength,
                "count": n,
                "win_rate_1d": round(win_rate_1d * 100, 1),
                "win_rate_3d": round(win_rate_3d * 100, 1),
                "avg_next_day_move": round(avg_nd, 1),
                "avg_next_3d_move": round(avg_n3d, 1),
                "std_next_day": round(std_nd, 1),
                "expectancy_1d": round(expectancy_1d, 1),
                "sharpe_proxy": round(avg_nd / std_nd, 3) if std_nd > 0 else 0.0,
            })

        # Conviction calibration: group by signal_strength buckets
        # Strong → win_rate, Moderate → win_rate, Neutral → win_rate
        calibration = []
        for strength in ("strong", "moderate", "neutral", "conflict"):
            matching = [c for c in cells if c["strength"] == strength]
            if not matching:
                continue
            total_n = sum(c["count"] for c in matching)
            avg_wr_1d = sum(c["win_rate_1d"] * c["count"] for c in matching) / total_n if total_n else 0
            avg_wr_3d = sum(c["win_rate_3d"] * c["count"] for c in matching) / total_n if total_n else 0
            calibration.append({
                "strength": strength,
                "total_signals": total_n,
                "avg_win_rate_1d": round(avg_wr_1d, 1),
                "avg_win_rate_3d": round(avg_wr_3d, 1),
            })

        # Top performers
        top_cells = sorted(
            [c for c in cells if c["count"] >= 5],
            key=lambda c: c["win_rate_1d"],
            reverse=True,
        )[:10]

        # Overall metrics
        all_nd = [x for r in rows_with_outcome for x in [_flt(r, "next_day_move")]]
        overall_win = sum(1 for x in all_nd if x > 0) / len(all_nd) * 100 if all_nd else 50.0

        return {
            "total_signals": len(rows_with_outcome),
            "overall_next_day_win_rate": round(overall_win, 1),
            "cells": cells,
            "top_performers": top_cells,
            "calibration": calibration,
            "day_type_summary": self._day_type_summary(rows_with_outcome),
        }

    def _day_type_summary(self, rows: list[dict]) -> list[dict]:
        """Aggregate outcome stats per day_type regardless of direction."""
        by_type: dict[str, list] = defaultdict(list)
        for r in rows:
            dt = _classify_day_type(r)
            nd = _flt(r, "next_day_move")
            by_type[dt].append(nd)

        result = []
        for dt, moves in sorted(by_type.items()):
            n = len(moves)
            avg = sum(moves) / n
            std = _std(moves)
            result.append({
                "day_type": dt,
                "count": n,
                "avg_next_day_move": round(avg, 1),
                "std_next_day_move": round(std, 1),
                "positive_pct": round(sum(1 for x in moves if x > 0) / n * 100, 1),
                "sharpe": round(avg / std, 3) if std > 0 else 0.0,
            })
        return sorted(result, key=lambda x: abs(x["avg_next_day_move"]), reverse=True)

    # ------------------------------------------------------------------
    # 5. Concept drift detection (Page-Hinkley)
    # ------------------------------------------------------------------

    def concept_drift(
        self,
        rows: list[dict],
        window: int = 20,
        delta: float = 0.5,
        threshold: float = 8.0,
    ) -> dict:
        """
        Page-Hinkley test over rolling win-rate of MP signals.

        Fires when the cumulative sum of deviations from the mean exceeds
        `threshold`, indicating a regime shift in signal performance.

        Returns drift events, rolling win-rate series, and current drift state.
        """
        rows_with_outcome = [
            r for r in sorted(rows, key=lambda r: r.get("date", ""))
            if r.get("next_day_move") not in (None, "", "nan", "NaN")
        ]
        if len(rows_with_outcome) < window:
            return {
                "drift_detected": False,
                "series": [],
                "drift_events": [],
                "current_state": "insufficient_data",
            }

        # Signal-level binary outcome (1 = win, 0 = loss)
        outcomes = []
        for r in rows_with_outcome:
            dt = _classify_day_type(r)
            bf = _flt(r, "buyer_fail_score")
            sf = _flt(r, "seller_fail_score")
            direction = _signal_direction(dt, bf, sf)
            nd = _flt(r, "next_day_move")
            if direction in ("CE", "PE"):
                win = (1 if nd > 0 else 0) if direction == "CE" else (1 if nd < 0 else 0)
            else:
                win = None  # skip neutral/conflict for drift detection
            outcomes.append({"date": r["date"], "direction": direction, "nd": nd, "win": win})

        directional = [o for o in outcomes if o["win"] is not None]
        if len(directional) < window:
            return {"drift_detected": False, "series": [], "drift_events": [], "current_state": "insufficient_data"}

        # Compute rolling win rate
        series = []
        for i in range(window - 1, len(directional)):
            slice_ = directional[max(0, i - window + 1): i + 1]
            wr = sum(x["win"] for x in slice_) / len(slice_)
            series.append({"date": directional[i]["date"], "rolling_win_rate": round(wr * 100, 1), "n": len(slice_)})

        if not series:
            return {"drift_detected": False, "series": series, "drift_events": [], "current_state": "stable"}

        # Page-Hinkley on rolling win rates
        mean_wr = sum(s["rolling_win_rate"] for s in series) / len(series)
        ph_min = 0.0
        ph_sum = 0.0
        drift_events = []
        for s in series:
            ph_sum += (s["rolling_win_rate"] - mean_wr + delta)
            ph_min = min(ph_min, ph_sum)
            ph_stat = ph_sum - ph_min
            s["ph_stat"] = round(ph_stat, 2)
            s["drift_signal"] = ph_stat > threshold

        drift_events = [s for s in series if s.get("drift_signal")]
        recent_drift = any(s.get("drift_signal") for s in series[-5:])
        current_wr = series[-1]["rolling_win_rate"]

        return {
            "drift_detected": recent_drift,
            "current_rolling_win_rate": current_wr,
            "historical_mean_win_rate": round(mean_wr, 1),
            "drift_magnitude": round(current_wr - mean_wr, 1),
            "series": series,
            "drift_events": drift_events,
            "current_state": "drift" if recent_drift else ("recovering" if current_wr < mean_wr - 5 else "stable"),
            "ph_threshold": threshold,
        }

    # ------------------------------------------------------------------
    # 6. Order-flow proxy from daily bars (CVD approximation)
    # ------------------------------------------------------------------

    def orderflow_proxy(self, rows: list[dict], lookback: int = 60) -> dict:
        """
        Approximate cumulative volume delta from OHLC context.

        Since we don't have true L3 bid/ask aggressor data from NSE, we use
        the daily auction structure as a proxy:
        - Close above VWAP proxy (POC) → bullish delta day
        - Close below → bearish delta day
        - Magnitude scaled by close_location and failure scores

        Returns a CVD series with key MP level annotations.
        """
        window = sorted(rows, key=lambda r: r.get("date", ""))[-lookback:]
        if not window:
            return {"series": [], "summary": {}}

        cvd = 0.0
        series = []
        for r in window:
            poc = _flt(r, "poc")
            vah = _flt(r, "vah")
            val = _flt(r, "val")
            close = _flt(r, "close_price") or _flt(r, "close")
            session_high = _flt(r, "session_high")
            session_low = _flt(r, "session_low")
            day_range = max(session_high - session_low, 1.0)
            close_location = (close - session_low) / day_range

            buyer_fail = _flt(r, "buyer_fail_score")
            seller_fail = _flt(r, "seller_fail_score")

            # Signed delta proxy: close_location 0=full bear, 1=full bull, 0.5=neutral
            signed_delta = (close_location - 0.5) * 2.0  # -1 to +1
            # Modulate by failure scores (seller fail = negative pressure)
            fail_bias = (seller_fail - buyer_fail) / max(seller_fail + buyer_fail, 1.0)
            daily_delta = signed_delta - fail_bias * 0.3
            cvd += daily_delta

            # Relative to MP key levels
            close_vs_poc = "above_poc" if close > poc else ("at_poc" if abs(close - poc) < day_range * 0.02 else "below_poc")
            close_vs_va = "above_va" if close > vah else ("below_va" if close < val else "inside_va")

            series.append({
                "date": r.get("date", ""),
                "cvd": round(cvd, 3),
                "daily_delta": round(daily_delta, 3),
                "close_vs_poc": close_vs_poc,
                "close_vs_va": close_vs_va,
                "poc": round(poc, 2),
                "vah": round(vah, 2),
                "val": round(val, 2),
                "close": round(close, 2),
                "close_location": round(close_location, 4),
                "buyer_fail": buyer_fail,
                "seller_fail": seller_fail,
            })

        # Divergence detection: CVD direction vs price direction
        divergences = []
        for i in range(5, len(series)):
            price_change = series[i]["close"] - series[i - 5]["close"]
            cvd_change = series[i]["cvd"] - series[i - 5]["cvd"]
            if price_change > 0 and cvd_change < -0.5:
                divergences.append({"date": series[i]["date"], "type": "bearish_divergence", "price_change": round(price_change, 1), "cvd_change": round(cvd_change, 3)})
            elif price_change < 0 and cvd_change > 0.5:
                divergences.append({"date": series[i]["date"], "type": "bullish_divergence", "price_change": round(price_change, 1), "cvd_change": round(cvd_change, 3)})

        return {
            "series": series,
            "divergences": divergences[-10:],
            "current_cvd": round(cvd, 3),
            "summary": {
                "total_bull_days": sum(1 for s in series if s["daily_delta"] > 0),
                "total_bear_days": sum(1 for s in series if s["daily_delta"] < 0),
                "net_cvd": round(cvd, 3),
                "divergences_count": len(divergences),
            },
        }

    # ------------------------------------------------------------------
    # Full analytics bundle
    # ------------------------------------------------------------------

    def full_analytics(
        self,
        rows: list[dict],
        lookback: int = 60,
        composite_20d: bool = True,
        composite_60d: bool = True,
    ) -> dict:
        """Return everything in one payload for the frontend dashboard."""
        # Sort rows chronologically
        rows_sorted = sorted(rows, key=lambda r: r.get("date", ""))

        profiles = {}
        if composite_20d:
            profiles["composite_20d"] = self.build_composite_profile(
                rows_sorted, lookback=20, label="Composite 20D"
            )
        if composite_60d:
            profiles["composite_60d"] = self.build_composite_profile(
                rows_sorted, lookback=60, label="Composite 60D"
            )

        weekly = self.build_weekly_profiles(rows_sorted)
        # Return last 12 weeks
        weekly = weekly[-12:]

        return {
            "profiles": profiles,
            "weekly_profiles": weekly,
            "value_migration": self.value_migration_trend(rows_sorted, lookback=lookback),
            "regime_history": self.regime_history(rows_sorted, lookback=lookback),
            "setup_performance": self.setup_performance(rows_sorted),
            "concept_drift": self.concept_drift(rows_sorted),
            "orderflow_proxy": self.orderflow_proxy(rows_sorted, lookback=lookback),
        }


# ---------------------------------------------------------------------------
# Standalone helpers (duplicated from router to keep module self-contained)
# ---------------------------------------------------------------------------

def _classify_day_type(r: dict) -> str:
    dt = r.get("day_type", "")
    if dt and dt not in ("", "UNKNOWN"):
        return dt
    fa_up = _bool(r, "fa_up")
    fa_dn = _bool(r, "fa_dn")
    ib_up = _bool(r, "ib_broken_up")
    ib_dn = _bool(r, "ib_broken_dn")
    sh = _flt(r, "session_high")
    sl = _flt(r, "session_low")
    ibr = _flt(r, "ibr")
    close = _flt(r, "close_price") or _flt(r, "close")
    sr = sh - sl
    if sr <= 0 or ibr <= 0:
        return "UNKNOWN"
    rr = sr / ibr
    cp = (close - sl) / sr if sr > 0 else 0.5
    if ib_up != ib_dn and rr >= 2.0:
        if ib_up and cp >= 0.70:
            return "TREND_UP"
        if ib_dn and cp <= 0.30:
            return "TREND_DN"
    if ib_up and ib_dn and rr >= 1.5:
        return "DOUBLE_DIST"
    if ib_up != ib_dn and rr >= 1.2:
        return "NORMAL_VAR_UP" if ib_up else "NORMAL_VAR_DN"
    if fa_up or fa_dn:
        return "FAILED_AUCTION"
    return "NORMAL"


def _signal_direction(day_type: str, buyer_fail: float, seller_fail: float) -> str:
    if buyer_fail >= 4 and seller_fail < 2:
        return "PE"
    if seller_fail >= 4 and buyer_fail < 2:
        return "CE"
    if day_type == "TREND_UP":
        return "CE"
    if day_type == "TREND_DN":
        return "PE"
    if buyer_fail >= 2 and seller_fail >= 2:
        return "CONFLICT"
    return "NEUTRAL"


def _signal_strength(direction: str, buyer_fail: float, seller_fail: float) -> str:
    if direction == "CONFLICT":
        return "conflict"
    if direction in {"CE", "PE"} and max(buyer_fail, seller_fail) >= 4:
        return "strong"
    if direction in {"CE", "PE"}:
        return "moderate"
    return "neutral"


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)
