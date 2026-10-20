"""Frozen completed-close hypotheses with next-quote, one-lot shadow fills.

Auction v1 is a failed RANGE auction proxy, not archived aggressor order flow
or a reproduction of the production auction agent. Never infer flow from OHLC.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math

from edge_research.archive import Archive, encode
from paper_engine.costs import round_trip_charges

IST = timezone(timedelta(hours=5, minutes=30))


def ts(raw) -> datetime:
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Research timestamps must explicitly identify a timezone")
    return value.astimezone(timezone.utc)


def number(raw) -> float | None:
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def hypothesis(bars: list[dict], lane: str, variant: str, cfg: dict) -> dict:
    """Prior range excludes both acceptance bars. No whole-frame extrema."""
    n = cfg["range_bars"]
    if len(bars) < max(n + 2, cfg["atr_bars"] + 1):
        return {"reason": "warmup"}
    base, prev, last = bars[-n - 2 : -2], bars[-2], bars[-1]
    hi, lo = max(x["high"] for x in base), min(x["low"] for x in base)
    tail = bars[-cfg["atr_bars"] - 1 :]
    tr = [
        max(
            b["high"] - b["low"],
            abs(b["high"] - a["close"]),
            abs(b["low"] - a["close"]),
        )
        for a, b in zip(tail, tail[1:])
    ]
    atr = sum(tr) / len(tr)
    travel = sum(abs(b["close"] - a["close"]) for a, b in zip(bars[-7:], bars[-6:]))
    efficiency = abs(last["close"] - bars[-7]["close"]) / travel if travel else 0.0
    side = 0
    if lane == "auction":
        # Control: first completed close back inside after a range excursion.
        # Challenger also needs rejection from the preceding completed bar.
        if lo < last["close"] < hi:
            if prev["high"] > hi and (variant == "control" or prev["close"] > hi):
                side = -1
            elif prev["low"] < lo and (variant == "control" or prev["close"] < lo):
                side = 1
        stop = (
            max(prev["high"], last["high"])
            if side < 0
            else min(prev["low"], last["low"])
        )
        target = (hi + lo) / 2
    else:
        if last["close"] > hi and (variant == "control" or prev["close"] > hi):
            side = 1
        elif last["close"] < lo and (variant == "control" or prev["close"] < lo):
            side = -1
        stop = (
            min(prev["low"], last["low"])
            if side > 0
            else max(prev["high"], last["high"])
        )
        target = (
            last["close"]
            + side * abs(last["close"] - stop) * cfg["lanes"][lane]["target_r"]
        )
    features = {
        "range_high": hi,
        "range_low": lo,
        "atr": atr,
        "efficiency_30m": efficiency,
    }
    if not side:
        return {"reason": "no_setup", "features": features}
    if atr <= 0 or hi - lo > cfg["max_range_atr"] * atr:
        return {"reason": "range_too_wide", "features": features}
    if (
        lane != "auction"
        and variant == "challenger"
        and efficiency < cfg["min_efficiency"]
    ):
        return {"reason": "weak_30m_displacement", "features": features}
    if side * (last["close"] - stop) <= 0 or side * (target - last["close"]) <= 0:
        return {"reason": "invalid_structure", "features": features}
    return {
        "reason": "candidate",
        "side": side,
        "stop": stop,
        "target": target,
        "features": features,
    }


def quote_problem(
    q: dict | None, contract: dict, now: datetime, cfg: dict, *, entry: bool = True
) -> str | None:
    if not q or q.get("instrument_key") != contract.get("instrument_key"):
        return "missing_exact_quote"
    try:
        age = (now - ts(q["time"])).total_seconds()
        receipt_age = (now - ts(q["observed_at"])).total_seconds()
        if (
            not 0 <= age <= cfg["max_quote_age_seconds"]
            or not 0 <= receipt_age <= cfg["max_quote_age_seconds"]
        ):
            return "stale_quote"
    except (ValueError, KeyError):
        return "missing_quote_time"
    bid, ask = number(q.get("bid")), number(q.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return "invalid_spread"
    cap = (
        cfg["max_option_spread_fraction"]
        if contract["instrument_type"] in {"CE", "PE"}
        else cfg["max_future_spread_fraction"]
    )
    if (ask - bid) / ((bid + ask) / 2) > cap:
        return "wide_spread"
    qty = number(contract.get("pnl_units_per_lot", contract.get("lot_size")))
    depth_required = number(contract.get("depth_required", qty))
    if (
        qty is None
        or qty <= 0
        or depth_required is None
        or depth_required <= 0
        or min(number(q.get("bid_size")) or 0, number(q.get("ask_size")) or 0)
        < depth_required
    ):
        return "insufficient_displayed_size"
    try:
        if (
            entry
            and contract.get("expiry")
            and ts(str(contract["expiry"]) + "T00:00:00+05:30").astimezone(IST).date()
            <= now.astimezone(IST).date()
        ):
            return "expiry_day_excluded"
    except ValueError:
        return "invalid_expiry"
    return None


class ShadowEngine:
    def __init__(self, archive: Archive, config: dict):
        self.archive, self.cfg = archive, config
        self._last_frame = {}
        self._last_greeks = {}

    def observe(self, frame: dict, quotes: list[dict]) -> None:
        now = ts(frame["observed_at"])
        symbol, market = frame["symbol"], frame["market"]
        if symbol not in self.cfg["symbols"] or self.cfg["symbols"][symbol] != market:
            raise ValueError("Symbol/market outside frozen universe")
        # Inputs are archived even if unusable. No reconstructed old decisions.
        with self.archive.db:
            fingerprint = encode(
                {
                    "bars": [
                        {k: v for k, v in b.items() if k != "available_at"}
                        for b in frame.get("bars", [])
                    ],
                    "market_open": frame.get("market_open"),
                    "session_exit_due": frame.get("session_exit_due"),
                    "coverage": frame.get("coverage"),
                    "source": frame.get("candle_source"),
                }
            )
            if self._last_frame.get(symbol) != fingerprint:
                self.archive.event("frame", frame["observed_at"], frame)
            for q in quotes:
                self.archive.event("quote", frame["observed_at"], q)
            for greek in frame.get("greeks", []):
                key = greek.get("instrument_key")
                if key and self._last_greeks.get(key) != greek.get("available_at"):
                    self.archive.event("greeks", frame["observed_at"], greek)
            bars = frame.get("bars", [])
            problem = None
            try:
                if not bars or any(
                    ts(b["end"]) > now or ts(b["available_at"]) > now for b in bars
                ):
                    problem = "incomplete_or_unavailable_bars"
                elif any(
                    ts(b["end"]) - ts(b["start"])
                    != timedelta(minutes=self.cfg["bar_minutes"])
                    for b in bars
                ):
                    problem = "invalid_bar_duration"
                elif any(
                    ts(b["end"]) - ts(a["end"])
                    != timedelta(minutes=self.cfg["bar_minutes"])
                    for a, b in zip(bars, bars[1:])
                ):
                    problem = "gapped_bars"
                elif (now - ts(bars[-1]["end"])).total_seconds() > self.cfg[
                    "max_bar_age_seconds"
                ]:
                    problem = "stale_bars"
                elif any(
                    any(
                        number(b.get(k)) is None
                        for k in ("open", "high", "low", "close")
                    )
                    or b["low"] <= 0
                    or b["high"] < max(b["open"], b["close"], b["low"])
                    or b["low"] > min(b["open"], b["close"])
                    for b in bars
                ):
                    problem = "invalid_bars"
            except (KeyError, ValueError, TypeError):
                problem = "invalid_bars"
            if not frame.get("market_open"):
                problem = "session_closed"
            elif frame.get("session_exit_due"):
                problem = "session_exit_window"
            for lane, spec in self.cfg["lanes"].items():
                if market not in spec["markets"]:
                    continue
                for variant in ("control", "challenger"):
                    key = f"{lane}:{variant}:{symbol}"
                    state = self.archive.state(key)
                    if state:
                        self._advance(key, state, frame, quotes, now, problem)
                    transitioned_flat = bool(state and not self.archive.state(key))
                    bar_end = (
                        bars[-1].get("end")
                        if bars
                        else now.astimezone(IST)
                        .replace(hour=0, minute=0, second=0, microsecond=0)
                        .isoformat()
                    )
                    decision_key = f"{key}:{bar_end}"
                    if self.archive.db.execute(
                        "SELECT 1 FROM decisions WHERE key=?", (decision_key,)
                    ).fetchone():
                        continue
                    result = (
                        {"reason": problem}
                        if problem
                        else hypothesis(bars, lane, variant, self.cfg)
                    )
                    if transitioned_flat:
                        result = {**result, "reason": "exit_or_expiry_this_bar"}
                    elif self.archive.state(key):
                        result = {**result, "reason": "position_or_pending_exists"}
                    if result["reason"] == "candidate":
                        kind = (
                            "FUT"
                            if market == "MCX"
                            else "CE" if result["side"] > 0 else "PE"
                        )
                        contract = frame.get("contracts", {}).get(kind)
                        if not contract:
                            result["reason"] = "missing_contract"
                        else:
                            try:
                                valid_contract = ts(contract["available_at"]) <= now
                            except (KeyError, ValueError):
                                valid_contract = False
                            if (
                                not valid_contract
                                or contract.get("instrument_type") != kind
                            ):
                                result["reason"] = "unavailable_contract"
                            else:
                                state = {
                                    **result,
                                    "lane": lane,
                                    "variant": variant,
                                    "symbol": symbol,
                                    "market": market,
                                    "contract": contract,
                                    "status": "pending",
                                    "decision_at": frame["observed_at"],
                                    "bar_end": bar_end,
                                }
                                self.archive.save(key, state)
                    self.archive.db.execute(
                        "INSERT INTO decisions VALUES(?,?)",
                        (
                            decision_key,
                            encode(
                                {
                                    "key": key,
                                    "available_at": frame["observed_at"],
                                    "bar_end": bar_end,
                                    **result,
                                }
                            ),
                        ),
                    )
        # Cache only committed observations; a failed transaction must be
        # eligible for capture again on the following poll.
        self._last_frame[symbol] = fingerprint
        for greek in frame.get("greeks", []):
            if greek.get("instrument_key"):
                self._last_greeks[greek["instrument_key"]] = greek.get("available_at")

    def _advance(self, key, state, frame, quotes, now, problem):
        c = state["contract"]
        q = next(
            (q for q in quotes if q.get("instrument_key") == c["instrument_key"]), None
        )
        pending = state["status"] == "pending"
        issue = quote_problem(q, c, now, self.cfg, entry=pending)
        if (
            pending
            and (now - ts(state["decision_at"])).total_seconds()
            > self.cfg["entry_timeout_seconds"]
        ):
            self.archive.event(
                "entry_expired",
                frame["observed_at"],
                {"key": key, "reason": issue or problem or "no_next_quote"},
            )
            self.archive.save(key, None)
            return
        # Close/mark requires a valid market quote, even if candle inputs fail.
        # A time/session exit becomes pending until that quote is available.
        hold = self.cfg["lanes"][state["lane"]]["hold_minutes"]
        if not pending and (now - ts(state["entry_at"])).total_seconds() >= hold * 60:
            state["exit_reason"] = state.get("exit_reason") or "time_stop"
        if not pending and (
            not frame.get("market_open") or frame.get("session_exit_due")
        ):
            state["exit_reason"] = state.get("exit_reason") or "session_close"
        if not pending and not problem:
            close = frame["bars"][-1]["close"]
            if state["side"] * (close - state["stop"]) <= 0:
                state["exit_reason"] = (
                    state.get("exit_reason") or "structural_stop_close"
                )
            elif state["side"] * (close - state["target"]) >= 0:
                state["exit_reason"] = state.get("exit_reason") or "target_close"
        if issue:
            state["mark_status"] = issue
            self.archive.save(key, state)
            return
        if pending:
            if (
                problem
                or ts(q["time"]) <= ts(state["decision_at"])
                or ts(q["observed_at"]) <= ts(state["decision_at"])
            ):
                return
            action = "SELL" if state["market"] == "MCX" and state["side"] < 0 else "BUY"
            fill = q["ask"] if action == "BUY" else q["bid"]
            state.update(
                status="open",
                entry_at=q["observed_at"],
                entry_quote_time=q["time"],
                entry_price=fill,
                entry_action=action,
                entry_spread=q["ask"] - q["bid"],
                qty=int(c.get("pnl_units_per_lot", c["lot_size"])),
            )
            self.archive.event(
                "shadow_entry", frame["observed_at"], {"key": key, **state}
            )
        else:
            # No out-of-order or replayed quote may mark/close a held contract.
            if ts(q["time"]) <= ts(state["entry_quote_time"]) or ts(
                q["observed_at"]
            ) <= ts(state["entry_at"]):
                return
            exit_price = q["bid"] if state["entry_action"] == "BUY" else q["ask"]
            sign = 1 if state["entry_action"] == "BUY" else -1
            gross = sign * (exit_price - state["entry_price"]) * state["qty"]
            state.update(
                mark_status="fresh", mark_at=q["observed_at"], unrealized_gross=gross
            )
            if state.get("exit_reason"):
                charges = round_trip_charges(
                    symbol=c["trading_symbol"],
                    instrument_type=c["instrument_type"],
                    entry_price=state["entry_price"],
                    exit_price=exit_price,
                    qty=state["qty"],
                    entry_action=state["entry_action"],
                )
                # Stress adds another full displayed spread per leg and doubles
                # charges. This is a sensitivity, not a measured slippage model.
                extra_spread = (state["entry_spread"] + q["ask"] - q["bid"]) * state[
                    "qty"
                ]
                trade = {
                    **state,
                    "status": "closed",
                    "exit_at": q["observed_at"],
                    "exit_quote_time": q["time"],
                    "exit_price": exit_price,
                    "gross_pnl": round(gross, 2),
                    "charges": charges,
                    "net_pnl": round(gross - charges, 2),
                    "stressed_net_pnl": round(gross - 2 * charges - extra_spread, 2),
                    "session": ts(state["entry_at"]).astimezone(IST).date().isoformat(),
                    "underlying_exit_close": (
                        frame["bars"][-1]["close"] if frame.get("bars") else None
                    ),
                }
                self.archive.db.execute(
                    "INSERT INTO trades(payload) VALUES(?)", (encode(trade),)
                )
                self.archive.save(key, None)
                return
        self.archive.save(key, state)
