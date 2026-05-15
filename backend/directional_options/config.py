"""Default configuration for the directional long-options engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PACKAGE_ROOT.parent / "runtime" / "directional_options"
DATA_ROOT = PACKAGE_ROOT.parent / "runtime" / "index_analytics_data"


DEFAULT_CONFIG: dict[str, Any] = {
    "label": "Directional Long Options",
    "description": (
        "Long-premium research and execution sandbox focused on buying calls and puts "
        "only when expected convexity clears theta, spread, slippage, and IV drag."
    ),
    "data_root": DATA_ROOT,
    "runtime_root": RUNTIME_ROOT,
    "universe": ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"],
    "timeframes": ["5minute", "15minute"],
    "default_underlying": "NIFTY",
    "default_timeframe": "5minute",
    "feature_engine": {
        "ema_fast": 8,
        "ema_slow": 21,
        "adx_period": 14,
        "atr_period": 14,
        "breakout_lookback": 12,
        "rv_window": 20,
        "range_window": 20,
        "warmup_bars": 32,
    },
    "signal_engine": {
        # Lowered from 0.58 → 0.50 so the new "exploration" regime (whose
        # confidence range is ~0.45-0.65) can produce paper signals. The
        # risk allocator already scales size by confidence — a 0.50-conf
        # signal sizes at the 0.5× floor (₹7.5k risk on ₹30L), small
        # enough for learning bets to land regularly without exceeding the
        # daily loss cap.
        "min_confidence": 0.50,
        # Intraday strategy — 5/15 min bars. Holding 18 bars on a 15-min
        # timeframe (= 4.5 hours) crosses into swing territory, which this
        # engine is not built for. Horizons clamp to ≤45 minutes on 5-min
        # bars and ≤2.25 hours on 15-min bars.
        "breakout_confidence_bonus": 0.06,
        "expected_move_atr_multiplier": 1.25,
        "expected_move_trend_multiplier": 0.85,
        "short_horizon_bars": 3,
        "medium_horizon_bars": 6,
        "long_horizon_bars": 9,
    },
    "selector": {
        "max_candidates": 18,
        "preferred_weekly_days": 8,
        "max_days_to_expiry": 45,
        # Lowered min_volume + min_oi so MCX commodity options pass the
        # liquidity floor — GOLD/SILVERM/NATURALGAS weekly contracts often
        # trade at lower volume/OI than NSE index weeklies but are still
        # tradable. CRUDEOIL was getting "All local watchlist contracts
        # failed liquidity or edge hurdles" with the old 150/2.5k floors.
        "min_volume": 50.0,
        "min_oi": 500.0,
        "max_spread_pct": 0.20,
        "fallback_spread_pct": 0.30,
        "sigma_floor": 0.12,
        # MCX commodities routinely trade 60–90% IV; clamping at 0.62
        # caused future_option_value to be undervalued in the trading-edge
        # calc, making net edge come out negative on real setups. 0.95
        # leaves headroom without enabling pathological vol blow-ups.
        "sigma_ceiling": 0.95,
        "sigma_multiplier": 1.08,
        "risk_free_rate": 0.06,
        "distributional_optimizer": {
            # Permissive hurdles — taking the trade and learning beats
            # waiting for theoretically-perfect edge. Confidence×size
            # scaler keeps low-conviction bets small.
            "min_net_edge_pct": 0.005,
            "min_probability_of_profit": 0.32,
            "min_liquidity_score": 0.20,
            "min_timing_fit": 0.18,
            # The error buffer used to be ~3.5% of premium + uncertainty
            # blow-up (×0.18). For commodities with model_uncertainty ~0.3
            # that's 8.9% — bigger than typical trading edge. Halved so
            # the buffer no longer dominates the trading_edge calc.
            "model_error_base_pct": 0.015,
            "ordinary_delta_min": 0.45,
            "ordinary_delta_max": 0.65,
            "fast_move_delta_min": 0.35,
            "fast_move_delta_max": 0.55,
            "jump_delta_min": 0.20,
            "jump_delta_max": 0.40,
            "otm_jump_threshold": 0.42,
            "otm_timing_threshold": 0.58,
            "index_put_skew_tax": 0.035,
            "max_skew_tax": 0.22,
            "weekly_timing_threshold": 0.42,
            "event_variance_premium": 0.025,
            "drawdown_state_penalty": 0.0,
        },
        "score_weights": {
            "direction": 22.0,
            "expected_pnl": 26.0,
            "liquidity": 20.0,
            "iv_value": 12.0,
            "theta_penalty": 10.0,
            "slippage_penalty": 10.0,
            "tail_edge": 18.0,
            "timing_fit": 14.0,
            "skew_tax": 12.0,
            "model_uncertainty": 10.0,
        },
    },
    "risk": {
        # Matches the paper desk's actual equity (~₹30L). Was 10L which made
        # even a single BANKNIFTY lot un-affordable on near-ATM strikes once
        # premium_cap_pct was applied. If the live paper equity drifts we
        # can wire `paper_portfolio.get_equity()` here later.
        "starting_equity": 3_000_000.0,
        # Base sizing — scaler 0.5×–1.5× of these per signal confidence
        # (see DirectionalOptionsRiskEngine._confidence_multiplier).
        # 0.5% risk → at 0.85 conf the lot risk budget is 0.75% of equity.
        # 2.5% premium → at 0.85 conf the premium cap is 3.75% of equity
        # (~₹1.12 lakh on a ₹30L book), which clears one BANKNIFTY weekly
        # ATM lot even when the option premium runs ₹120–₹250.
        "risk_pct": 0.005,
        "premium_cap_pct": 0.025,
        # Mirror min_confidence so the allocator knows the curve floor.
        "min_confidence": 0.58,
        "planned_stop_pct": 0.35,
        "profit_target_pct": 0.45,
        "trail_giveback_pct": 0.18,
        "expiry_guard_days": 0.8,
        "daily_loss_cap_r": 2.0,
        "weekly_loss_cap_r": 5.0,
        # 4% expected-edge floor (was 8%). Commodity options price with
        # higher model uncertainty than NSE indices — an 8% expected
        # PnL/premium hurdle filtered out every commodity candidate even
        # when the underlying setup was sound. Risk allocator still gates
        # the bet size by confidence.
        "min_expected_edge_pct": 0.04,
        # Intraday strategy — at most one open directional bet per
        # underlying, but several underlyings can coexist if convictions
        # align.
        "max_open_positions": 4,
    },
    "execution": {
        "entry_slippage_pct": 0.0075,
        "exit_slippage_pct": 0.006,
        "fee_per_unit": 0.45,
    },
    "backtest": {
        "lookback_sessions": 16,
        "mark_to_market_every_bar": True,
        "max_trades_per_day": 2,
    },
    "paper_trading": {
        "journal_root": RUNTIME_ROOT / "paper",
        "live_lookback_days": 10,
        "stale_watchlist_seconds": 600,
    },
}


def clone_default_config() -> dict[str, Any]:
    """Return a deep-copy safe configuration dictionary."""
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)
