from __future__ import annotations

from datetime import date
from pathlib import Path
import asyncio

from api.routers import strategy


def test_strategy1_watchlist_signals_focus_on_index_underlyings() -> None:
    agent_status = {
        "last_run_at": "2026-04-09T15:08:55.896624+05:30",
        "regime_summary": {
            "NIFTY": "bullish",
            "BANKNIFTY": "bearish",
            "TCS": "bullish",
        },
    }

    signals = strategy._build_strategy1_watchlist_signals(agent_status)

    assert [signal["underlying"] for signal in signals] == ["NIFTY", "BANKNIFTY"]
    assert [signal["direction"] for signal in signals] == ["CE", "PE"]


def test_strategy2_signal_marks_research_only_when_snapshot_exists(monkeypatch) -> None:
    snapshot = [
        {
            "date": date.today().isoformat(),
            "buyer_fail_score": "0",
            "seller_fail_score": "5",
            "fa_up": "false",
            "fa_dn": "false",
            "ib_broken_up": "true",
            "ib_broken_dn": "false",
            "session_high": "102",
            "session_low": "98",
            "ibr": "1",
            "close_price": "101.5",
            "daily_move": "1.5",
        }
    ]

    monkeypatch.setattr(strategy, "_safe_read_csv", lambda _: snapshot)

    signal = strategy._build_strategy2_signal("NIFTY")

    assert signal["direction"] == "CE"
    assert signal["status"] == "research-only"
    assert signal["freshness"] == "live"
    assert "no live 5-minute execution loop" in signal["instruction"]


def test_strategy2_signal_marks_missing_when_pipeline_absent(monkeypatch) -> None:
    monkeypatch.setattr(strategy, "_safe_read_csv", lambda _: [])

    signal = strategy._build_strategy2_signal("FINNIFTY")

    assert signal["direction"] is None
    assert signal["status"] == "not-ready"
    assert signal["freshness"] == "missing"


def test_strategy2_live_signals_use_runtime_state() -> None:
    agent_status = {
        "strategies": [
            {
                "key": "index_mp_strategy",
                "positions": [
                    {
                        "underlying": "NIFTY",
                        "option_type": "CE",
                        "entry_price": 120.5,
                        "current_price": 128.0,
                        "return_pct": 6.2,
                    }
                ],
                "signals": [
                    {
                        "underlying": "NIFTY",
                        "direction": "CE",
                        "status": "entry-ready",
                        "reason": "mp_trend_up",
                        "freshness": "live",
                        "instruction": "NIFTY ready",
                        "as_of": "2026-04-09T15:20:00+05:30",
                        "option_last_bar_time": "2026-04-09T15:15:00+05:30",
                        "spot_last_time": "2026-04-09T15:19:00+05:30",
                    }
                ],
            }
        ]
    }

    signals = strategy._build_strategy2_live_signals(agent_status)

    assert signals[0]["direction"] == "CE"
    assert signals[0]["status"] == "active"
    assert "live CE position open" in signals[0]["instruction"]


def test_get_portfolio_stats_returns_empty_payload_when_archive_is_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(strategy, "DATA_ROOT", tmp_path)

    payload = asyncio.run(strategy.get_portfolio_stats(underlying="SENSEX", source="csv"))

    assert payload["source"] == "empty"
    assert payload["total_trades"] == 0
    assert payload["equity_curve"] == [{"trade": 0, "equity": 100_000, "date": ""}]


def test_get_portfolio_stats_returns_empty_payload_when_underlying_has_no_rows(monkeypatch, tmp_path: Path) -> None:
    staggered_exit = tmp_path / "staggered_exit"
    staggered_exit.mkdir(parents=True)
    (staggered_exit / "trade_results.csv").write_text("underlying,strategy,entry_time,blended_return\n", encoding="utf-8")
    monkeypatch.setattr(strategy, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        strategy,
        "_safe_read_csv",
        lambda _path: [
            {
                "underlying": "NIFTY",
                "strategy": "target_50pct",
                "entry_time": "2026-04-17T09:30:00+05:30",
                "blended_return": "1.25",
            }
        ],
    )

    payload = asyncio.run(strategy.get_portfolio_stats(underlying="SENSEX", source="csv"))

    assert payload["source"] == "empty"
    assert payload["underlying"] == "SENSEX"
    assert payload["final_equity"] == 100_000
