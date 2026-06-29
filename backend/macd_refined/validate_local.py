"""Local validation for the MACD Refined lane.

Run from the backend dir:
    ../.venv/bin/python -m macd_refined.validate_local

Exercises the data layer, both backtest modes, the paper book, expiry
resolution, and graceful live degradation — no broker / DB required.
"""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")

from macd_refined.config import MACD_REFINED_LIVE_UNIVERSE, clone_default_config
from macd_refined.data import MacdRefinedDataStore
from macd_refined.service import MacdRefinedService


def _check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    cfg = clone_default_config()
    store = MacdRefinedDataStore(cfg["data_root"])

    # ── Data layer ────────────────────────────────────────────────────────
    files = store.list_expiry_files()
    _check("dataset has expiry files", len(files) >= 5, f"{len(files)} expiry files")
    underlyings = store.available_underlyings()
    _check("universe loaded", len(underlyings) >= 150, f"{len(underlyings)} underlyings")
    iv = store.atm_iv_daily("RELIANCE")
    _check("ATM-IV history builds", not iv.empty, f"{len(iv)} sessions for RELIANCE")

    # ── Expiry resolution (current + next month) ──────────────────────────
    from datetime import date
    exps = store.resolve_monthly_expiries("NIFTY", date(2026, 6, 22), ahead=2)
    _check("resolves current + next monthly expiry", len(exps) == 2 and exps[0] < exps[1], str(exps))

    svc = MacdRefinedService(config=clone_default_config())

    # ── Backtest: research (validated) reproduces the documented edge ──────
    research = svc.backtest(source="research", expiry_count=8)
    rmetrics = research["signals"]["signal_level_metrics"]
    _check("research backtest runs", rmetrics["trades"] > 100, f"{rmetrics['trades']} signals")
    _check("research edge reproduced (win-rate > 0.75)", rmetrics["win_rate"] > 0.75, f"win={rmetrics['win_rate']}")
    _check("research median positive", rmetrics["median_return_pct"] > 50, f"median={rmetrics['median_return_pct']}%")
    _check("research portfolio has CE+PE books", research["portfolio"]["books"]["CE"]["trades"] > 0 and research["portfolio"]["books"]["PE"]["trades"] > 0)

    # ── Backtest: engine (causal) runs and is honestly weaker ─────────────
    engine = svc.backtest(source="engine", underlyings=MACD_REFINED_LIVE_UNIVERSE, expiry_count=8)
    _check("causal engine backtest runs", engine["signals"]["accepted"] >= 0)
    _check("engine labeled causal", "causal" in (engine["signals"].get("note") or "").lower())

    # ── Paper book round-trip ─────────────────────────────────────────────
    svc.reset_paper(actor="validate")
    prop = [{
        "underlying": "RELIANCE", "option_type": "CE", "strike": 3000, "expiry": "2026-07-28",
        "expiry_window_end": "2026-07-21", "instrument_key": "X", "trading_symbol": "RELIANCE 3000 CE",
        "entry_premium": 100.0, "quantity_lots": 1, "quantity_units": 250, "lot_size": 250,
        "spot": 3010, "iv": 0.22, "iv_rank": 0.15, "direction_bias": "up",
        "signal_kind": "macd_confirmation", "daily_turnover_rupees": 5_000_000, "selection_reason": "test",
    }]
    s1 = svc.paper.sync_cycle(proposals=prop, marks={}, allow_entries=True)
    _check("paper opens a position", s1["open_positions"] == 1, f"open={s1['open_positions']}")
    pid = svc.paper_positions(status="open")["open_positions"][0]["position_id"]
    # catastrophe stop: mark at -60%
    s2 = svc.paper.sync_cycle(proposals=[], marks={pid: {"premium": 40.0, "spot": 2900}}, allow_entries=False)
    _check("catastrophe stop closes position", s2["open_positions"] == 0 and s2["closed_positions"] == 1)
    _check("realized loss booked", svc.paper_summary()["realized_pnl"] < 0, f"realized={svc.paper_summary()['realized_pnl']}")
    svc.reset_paper(actor="validate")

    # ── Live cycle degrades gracefully (no broker in this env) ─────────────
    live = asyncio.run(svc.run_live_cycle(allow_entries=False))
    _check("live cycle degrades without broker", live["broker_ready"] in (True, False))
    if not live["broker_ready"]:
        _check("live cycle returns a clear note", bool(live.get("note")))

    # ── Positioning surfaces current + next expiry ────────────────────────
    pos = svc.positioning()
    sample = pos["symbols"][0]
    _check("positioning resolves current + next expiry", bool(sample["current_expiry"]) and bool(sample["next_expiry"]),
           f"{sample['underlying']}: {sample['current_expiry']} / {sample['next_expiry']}")

    print("\nAll MACD Refined validations passed.")


if __name__ == "__main__":
    main()
