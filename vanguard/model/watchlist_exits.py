"""Versioned, broker-free exit comparison. Not a tuned or promoted strategy.

The initial entry is the first session candle CLOSE. Its high/low is excluded.
Only completed, consecutive candles may influence the runner. A ratchet made
using this candle's high becomes executable at the NEXT candle's open; no
assumption about the order of this candle's high and low is required.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from math import isfinite

from model.session_clock import IST, available_at


@dataclass(frozen=True)
class ExitPolicy:
    version: str = "watchlist_runner_v1"
    initial_stop: float = .15
    breakeven_activation: float | None = .20
    profit_lock_activation: float | None = .30
    locked_gain_fraction: float = .50
    cost_pct: float = .01
    exit_time_ist: str = "15:15"


POLICY = ExitPolicy()
HARD_STOP_POLICY = ExitPolicy(version="watchlist_stop_only_v1", breakeven_activation=None,
                             profit_lock_activation=None)


def policy_card() -> dict:
    return {**asdict(POLICY), "mode": "shadow_only", "profit_cap": None,
            "entry": "first next-session 30-minute close",
            "ratchet": "effective next candle only; gap through stop fills at open",
            "time_exit": "15:15 IST close of last full 30-minute candle; no overnight carry",
            "control": asdict(HARD_STOP_POLICY),
            "cost_provenance": "assumed round-trip premium cost, not measured bid/ask",
            "validation": "predeclared research candidate; not an optimized exit"}


def analyse_path(bars: list[dict], as_of: datetime, *, policy=POLICY) -> dict:
    """Deterministic replay of one exact contract, supplied by the caller.

    Missing/invalid candles never manufacture a stop fill. Closing a run
    requires this contract's own final candle, not another symbol's close.
    """
    usable = []
    for bar in sorted(bars, key=lambda row: row["time"]):
        try:
            if available_at(bar["time"]) > as_of or bar["time"].astimezone(IST).time() > time(14,45):
                continue
        except ValueError:
            continue
        usable.append(bar)
    if not usable:
        return {"status": "missing_contract", "runner": None}
    if len({b["time"] for b in usable}) != len(usable):
        return {"status": "duplicate_candles", "runner": None}
    entry_bar, last = usable[0], usable[-1]
    day = entry_bar["time"].astimezone(IST).date()
    if any(b["time"].astimezone(IST).date() != day for b in usable):
        return {"status": "mixed_sessions", "runner": None}
    for b in usable:
        values = [b.get(k) for k in ("open", "high", "low", "close")]
        if (any(v is None or not isfinite(float(v)) or float(v) <= 0 for v in values)
                or float(b["low"]) > min(float(b["open"]), float(b["close"]))
                or float(b["high"]) < max(float(b["open"]), float(b["close"]))):
            return {"status": "invalid_candle", "runner": None}
    entry = float(entry_bar["close"])
    after = usable[1:]
    high = max([entry] + [float(b["high"]) for b in after])
    low = min([entry] + [float(b["low"]) for b in after])
    high_bar = next((b for b in after if float(b["high"]) == high), entry_bar)
    is_final = (last["time"].astimezone(IST).hour, last["time"].astimezone(IST).minute) == (14, 45)
    gap = any(b["time"] != a["time"] + timedelta(minutes=30)
              for a, b in zip(usable, usable[1:]))
    entry_on_time = (entry_bar["time"].astimezone(IST).hour,
                     entry_bar["time"].astimezone(IST).minute) == (9, 15)
    base = {
        "version": policy.version, "status": "closed" if is_final else "tracking",
        "entry_ts": entry_bar["time"], "entry_available_at": available_at(entry_bar["time"]),
        "entry_mark": entry, "latest_ts": last["time"], "latest_mark": float(last["close"]),
        "latest_available_at": available_at(last["time"]),
        "return_pct": float(last["close"]) / entry - 1,
        "max_return_pct": high / entry - 1, "min_return_pct": low / entry - 1,
        "peak_bar_ts": high_bar["time"], "bars": len(usable),
        "entry_on_time": entry_on_time, "continuous": not gap,
        "mfe_basis": "post-entry high; opportunity, not an executable exit or realized P&L",
    }
    if not entry_on_time:
        base["runner"] = {"status": "insufficient_data", "reason":
                          "missing opening candle"}
        return base
    peak = entry
    stop = entry * (1 - policy.initial_stop)
    protected = False
    exit_bar = None
    exit_price = None
    reason = None
    prior_stamp = entry_bar["time"]
    for b in after:
        if b["time"] != prior_stamp + timedelta(minutes=30):
            base["runner"] = {"status": "insufficient_data", "reason": "missing intermediate candle before exit"}
            return base
        prior_stamp = b["time"]
        # Only the stop known BEFORE this candle can execute inside it.
        if float(b["open"]) <= stop or float(b["low"]) <= stop:
            exit_price = min(float(b["open"]), stop)
            exit_bar = b
            reason = "profit_protection" if protected else "initial_stop"
            break
        peak = max(peak, float(b["high"]))
        gain = peak / entry - 1
        if policy.breakeven_activation is not None and gain >= policy.breakeven_activation - 1e-12:
            stop = max(stop, entry * (1 + policy.cost_pct))
            protected = True
        if policy.profit_lock_activation is not None and gain >= policy.profit_lock_activation - 1e-12:
            stop = max(stop, entry + policy.locked_gain_fraction * (peak - entry))
        if b["time"].astimezone(IST).hour == 14 and b["time"].astimezone(IST).minute == 45:
            exit_price, exit_bar, reason = float(b["close"]), b, "session_close"
    runner = {"status": "exited" if exit_bar else "tracking", "stop_for_next_bar": stop,
              "peak_observed_before_exit": peak, "reason": reason,
              "exit_bar_ts": exit_bar["time"] if exit_bar else None,
              "exit_available_at": available_at(exit_bar["time"]) if exit_bar else None,
              "exit_mark": exit_price, "cost_pct": policy.cost_pct,
              "gross_return_pct": exit_price / entry - 1 if exit_price is not None else None,
              "net_return_pct": exit_price / entry - 1 - policy.cost_pct if exit_price is not None else None}
    # Only baseline mark-to-mark return is available while the runner is open.
    base["runner"] = runner
    base["baseline_net_return_pct"] = base["return_pct"] - policy.cost_pct
    return base
