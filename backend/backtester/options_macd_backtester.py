"""Options MACD Backtester — Nomad Curie

Strategy hypothesis:
  MACD on ATM option PREMIUM (not the underlying price) crossing the zero line
  captures simultaneous delta + vega alignment that signals high-probability
  directional moves.

Supports:
  - Data loading from CSV (OptionsDX format), pandas DataFrame, or Breeze API
  - MACD on option premium close series
  - Zero-line cross, signal-line cross, histogram acceleration signals
  - Trade simulation: SL, Target 1/2/3, time-based exit
  - Per-instrument metrics: win rate, profit factor, Sharpe, max drawdown
  - Walk-forward optimization (70/30 rolling windows)
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    # MACD params
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    timeframe: str = "5min"

    # Strike selection
    strike_selection: str = "ATM"      # ATM | ITM1 | OTM1
    option_types: List[str] = field(default_factory=lambda: ["CE", "PE"])

    # Risk
    sl_pct: float = 0.35               # 35% SL on premium
    target_1_pct: float = 0.50         # 50% gain
    target_2_pct: float = 1.00         # 100% gain
    target_3_pct: float = 1.80         # 180% gain
    time_exit_bars: int = 78           # ~2 sessions of 5-min bars

    # Capital
    capital_per_trade: float = 150_000  # ₹1.5L per trade
    max_concurrent: int = 3

    # Signal filters
    min_histogram_accel: float = 0.0   # minimum delta in histogram
    use_signal_cross: bool = True      # also trade signal-line crosses
    use_histogram_accel: bool = False  # trade histogram acceleration (more noise)


@dataclass
class SignalRecord:
    timestamp: datetime
    underlying: str
    market: str
    expiry: str
    strike: float
    option_type: str
    signal_type: str    # ZERO_CROSS_UP | ZERO_CROSS_DOWN | SIGNAL_CROSS_UP | SIGNAL_CROSS_DOWN
    macd_value: float
    signal_value: float
    histogram: float
    premium_at_signal: float
    bar_index: int


@dataclass
class TradeRecord:
    signal: SignalRecord
    entry_time: datetime
    entry_premium: float
    exit_time: Optional[datetime] = None
    exit_premium: Optional[float] = None
    exit_reason: str = ""
    lots: int = 1
    lot_size: int = 1
    pnl_points: float = 0.0
    pnl_pct: float = 0.0
    pnl_rupees: float = 0.0
    holding_bars: int = 0

    @property
    def is_winner(self) -> bool:
        return self.pnl_points > 0


@dataclass
class BacktestResult:
    config: BacktestConfig
    underlying: str
    market: str
    option_type: str

    # Summary
    total_signals: int = 0
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0

    # P&L
    total_pnl_points: float = 0.0
    total_pnl_rupees: float = 0.0
    avg_winner_pct: float = 0.0
    avg_loser_pct: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_bars: float = 0.0

    # Risk-adjusted
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_rupees: float = 0.0
    reward_risk_ratio: float = 0.0

    # Exit breakdown
    target_1_exits: int = 0
    target_2_exits: int = 0
    target_3_exits: int = 0
    sl_exits: int = 0
    time_exits: int = 0
    expiry_exits: int = 0

    # Trades list
    trades: List[TradeRecord] = field(default_factory=list)


# ── Core Engine ───────────────────────────────────────────────────────────────

class OptionsMACDBacktester:
    """
    Core backtesting engine for the Options MACD zero-line cross strategy.

    Usage:
        bt = OptionsMACDBacktester(config)
        bt.load_from_dataframe(df, underlying="NIFTY", market="NSE")
        results = bt.run_backtest()
        print(results)
    """

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()
        self._data: Optional[pd.DataFrame] = None
        self._underlying = ""
        self._market = ""

    # ── Data Loading ──────────────────────────────────────────────────────────

    def load_from_dataframe(
        self,
        df: pd.DataFrame,
        underlying: str = "",
        market: str = "NSE",
    ) -> "OptionsMACDBacktester":
        """
        Load a DataFrame with columns:
          timestamp, expiry, strike, option_type, open, high, low, close, volume
          (optional: iv, delta, underlying_price)

        timestamp should be parseable datetime strings or datetime objects.
        """
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        self._data = df
        self._underlying = underlying
        self._market = market
        logger.info(
            f"Loaded {len(df)} rows for {underlying} ({market}) "
            f"across {df['option_type'].unique().tolist()} option types"
        )
        return self

    def load_from_csv(
        self,
        path_or_buffer,
        underlying: str = "",
        market: str = "US",
        column_map: Optional[Dict[str, str]] = None,
    ) -> "OptionsMACDBacktester":
        """
        Load from CSV file. Supports OptionsDX format or custom.
        column_map: {'source_col': 'target_col'} to rename columns.

        OptionsDX format columns:
          date, expiration, strike, type, open, high, low, close, volume,
          openInterest, underlyingPrice, IV, delta, gamma, theta, vega
        """
        df = pd.read_csv(path_or_buffer)

        # Auto-detect OptionsDX format
        if "date" in df.columns and "type" in df.columns:
            rename = {
                "date": "timestamp",
                "expiration": "expiry",
                "type": "option_type",
                "openInterest": "oi",
                "underlyingPrice": "underlying_price",
                "IV": "iv",
            }
            df = df.rename(columns=rename)
            # Normalize option_type
            df["option_type"] = df["option_type"].str.upper().map(
                {"CALL": "CE", "PUT": "PE", "CE": "CE", "PE": "PE"}
            )

        if column_map:
            df = df.rename(columns=column_map)

        return self.load_from_dataframe(df, underlying=underlying, market=market)

    def load_from_breeze(
        self,
        breeze_data: List[Dict],
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        market: str = "NSE",
    ) -> "OptionsMACDBacktester":
        """Load from ICICI Breeze API response format."""
        rows = []
        for r in breeze_data:
            rows.append({
                "timestamp": r.get("datetime", ""),
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type.upper(),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
                "oi": int(r.get("open_interest", 0)),
            })
        df = pd.DataFrame(rows)
        return self.load_from_dataframe(df, underlying=underlying, market=market)

    # ── MACD Computation ─────────────────────────────────────────────────────

    @staticmethod
    def compute_macd(
        series: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Compute MACD on the given price series.
        Returns: (macd_line, signal_line, histogram)
        """
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        histogram = macd - signal_line
        return macd, signal_line, histogram

    def _add_macd_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add MACD columns to a per-contract DataFrame."""
        macd, sig, hist = self.compute_macd(
            df["close"],
            self.config.macd_fast,
            self.config.macd_slow,
            self.config.macd_signal,
        )
        df = df.copy()
        df["macd"] = macd
        df["macd_signal"] = sig
        df["histogram"] = hist
        # Previous bar values for cross detection
        df["prev_macd"] = df["macd"].shift(1)
        df["prev_signal"] = df["macd_signal"].shift(1)
        df["prev_hist"] = df["histogram"].shift(1)
        df["hist_delta"] = df["histogram"] - df["prev_hist"]
        return df

    # ── Signal Detection ─────────────────────────────────────────────────────

    def _detect_signals(self, df: pd.DataFrame) -> List[SignalRecord]:
        """Detect MACD signals on a single-contract OHLCV DataFrame."""
        signals: List[SignalRecord] = []
        cfg = self.config
        min_bars = cfg.macd_slow + cfg.macd_signal

        for i in range(min_bars, len(df)):
            row = df.iloc[i]
            if any(pd.isna([row["macd"], row["prev_macd"], row["macd_signal"], row["prev_signal"]])):
                continue

            signal_type = None

            # Zero-line cross UP (macd crosses above 0)
            if row["prev_macd"] < 0 and row["macd"] > 0:
                signal_type = "ZERO_CROSS_UP"

            # Zero-line cross DOWN (macd crosses below 0)
            elif row["prev_macd"] > 0 and row["macd"] < 0:
                signal_type = "ZERO_CROSS_DOWN"

            # Signal-line cross UP
            elif cfg.use_signal_cross and row["prev_macd"] < row["prev_signal"] and row["macd"] > row["macd_signal"]:
                signal_type = "SIGNAL_CROSS_UP"

            # Signal-line cross DOWN
            elif cfg.use_signal_cross and row["prev_macd"] > row["prev_signal"] and row["macd"] < row["macd_signal"]:
                signal_type = "SIGNAL_CROSS_DOWN"

            # Histogram acceleration (momentum confirmation)
            elif cfg.use_histogram_accel:
                if row["histogram"] > 0 and row["hist_delta"] > cfg.min_histogram_accel:
                    signal_type = "HIST_ACCEL_UP"
                elif row["histogram"] < 0 and row["hist_delta"] < -cfg.min_histogram_accel:
                    signal_type = "HIST_ACCEL_DOWN"

            if signal_type:
                signals.append(SignalRecord(
                    timestamp=row["timestamp"],
                    underlying=self._underlying,
                    market=self._market,
                    expiry=str(row.get("expiry", "")),
                    strike=float(row.get("strike", 0)),
                    option_type=str(row.get("option_type", "")),
                    signal_type=signal_type,
                    macd_value=float(row["macd"]),
                    signal_value=float(row["macd_signal"]),
                    histogram=float(row["histogram"]),
                    premium_at_signal=float(row["close"]),
                    bar_index=i,
                ))

        return signals

    # ── Trade Simulation ─────────────────────────────────────────────────────

    def _simulate_trade(
        self,
        signal: SignalRecord,
        df: pd.DataFrame,
        action: str,  # BUY or SELL (for CE/PE direction)
    ) -> Optional[TradeRecord]:
        """Simulate a single trade from a signal."""
        cfg = self.config
        i = signal.bar_index

        if i >= len(df) - 1:
            return None  # No room to trade

        entry_row = df.iloc[i + 1]  # Enter on next bar open
        entry_premium = float(entry_row["open"])

        if entry_premium <= 0:
            return None

        # Risk levels based on premium
        sl_price = entry_premium * (1 - cfg.sl_pct)
        target_1 = entry_premium * (1 + cfg.target_1_pct)
        target_2 = entry_premium * (1 + cfg.target_2_pct)
        target_3 = entry_premium * (1 + cfg.target_3_pct)

        # Lot calculation
        lot_size = 1  # Simplified; real: NSE lot size per instrument
        lot_value = entry_premium * lot_size
        lots = max(1, int(cfg.capital_per_trade / (lot_value * 100)))  # approx

        exit_premium = None
        exit_reason = "EXPIRY"
        exit_bar = len(df) - 1

        for j in range(i + 2, min(i + cfg.time_exit_bars + 2, len(df))):
            bar = df.iloc[j]
            lo = float(bar["low"])
            hi = float(bar["high"])
            cl = float(bar["close"])

            # Check SL (hit low for long)
            if lo <= sl_price:
                exit_premium = sl_price
                exit_reason = "STOP_LOSS"
                exit_bar = j
                break

            # Check Target 3
            if hi >= target_3:
                exit_premium = target_3
                exit_reason = "TARGET_3"
                exit_bar = j
                break

            # Check Target 2
            if hi >= target_2:
                exit_premium = target_2
                exit_reason = "TARGET_2"
                exit_bar = j
                break

            # Check Target 1
            if hi >= target_1:
                exit_premium = target_1
                exit_reason = "TARGET_1"
                exit_bar = j
                break

        else:
            # Time exit
            exit_bar_idx = min(i + 1 + cfg.time_exit_bars, len(df) - 1)
            exit_premium = float(df.iloc[exit_bar_idx]["close"])
            exit_reason = "TIME_EXIT"
            exit_bar = exit_bar_idx

        if exit_premium is None:
            exit_premium = float(df.iloc[exit_bar]["close"])

        pnl_points = exit_premium - entry_premium
        pnl_pct = pnl_points / entry_premium if entry_premium > 0 else 0
        pnl_rupees = pnl_points * lots * lot_size * 100  # approx

        return TradeRecord(
            signal=signal,
            entry_time=df.iloc[i + 1]["timestamp"],
            entry_premium=entry_premium,
            exit_time=df.iloc[exit_bar]["timestamp"],
            exit_premium=exit_premium,
            exit_reason=exit_reason,
            lots=lots,
            lot_size=lot_size,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_rupees=pnl_rupees,
            holding_bars=exit_bar - (i + 1),
        )

    # ── Main Backtest ─────────────────────────────────────────────────────────

    def run_backtest(self) -> List[BacktestResult]:
        """
        Run backtest on loaded data.
        Returns a list of BacktestResult, one per (underlying, expiry, option_type) combination.
        """
        if self._data is None:
            raise ValueError("No data loaded. Call load_from_dataframe() or load_from_csv() first.")

        cfg = self.config
        results = []

        # Group by option_type (and optionally expiry+strike)
        for opt_type in self._data["option_type"].unique():
            if opt_type not in cfg.option_types:
                continue

            subset = self._data[self._data["option_type"] == opt_type].copy()

            # Further group by expiry + strike if multiple contracts
            groupby_cols = [c for c in ["expiry", "strike"] if c in subset.columns]
            if groupby_cols:
                groups = subset.groupby(groupby_cols)
            else:
                groups = [("single", subset)]

            for group_key, group_df in groups:
                group_df = group_df.sort_values("timestamp").reset_index(drop=True)
                if len(group_df) < cfg.macd_slow + cfg.macd_signal + 10:
                    continue  # Too short for MACD

                # Add MACD
                group_df = self._add_macd_columns(group_df)

                # Detect signals
                signals = self._detect_signals(group_df)

                # Simulate trades
                trades: List[TradeRecord] = []
                open_trades = 0

                for signal in signals:
                    if open_trades >= cfg.max_concurrent:
                        continue

                    # Determine trade direction
                    if signal.signal_type in ("ZERO_CROSS_UP", "SIGNAL_CROSS_UP", "HIST_ACCEL_UP"):
                        action = "BUY"
                    else:
                        action = "BUY"  # We BUY PEs when bearish (buying premium)

                    trade = self._simulate_trade(signal, group_df, action)
                    if trade:
                        trades.append(trade)

                if not trades:
                    continue

                result = self._calculate_metrics(
                    trades=trades,
                    signals=signals,
                    underlying=self._underlying,
                    market=self._market,
                    option_type=opt_type,
                )
                results.append(result)
                logger.info(
                    f"{self._underlying} {opt_type} — "
                    f"Trades: {result.total_trades}, "
                    f"Win: {result.win_rate:.1%}, "
                    f"PF: {result.profit_factor:.2f}, "
                    f"Sharpe: {result.sharpe_ratio:.2f}"
                )

        return results

    # ── Metrics ─────────────────────────────────────────────────────────────

    def _calculate_metrics(
        self,
        trades: List[TradeRecord],
        signals: List[SignalRecord],
        underlying: str,
        market: str,
        option_type: str,
    ) -> BacktestResult:
        """Calculate comprehensive performance metrics."""
        cfg = self.config
        result = BacktestResult(
            config=cfg,
            underlying=underlying,
            market=market,
            option_type=option_type,
            total_signals=len(signals),
            total_trades=len(trades),
            trades=trades,
        )

        if not trades:
            return result

        winners = [t for t in trades if t.pnl_points > 0]
        losers = [t for t in trades if t.pnl_points <= 0]

        result.winners = len(winners)
        result.losers = len(losers)
        result.win_rate = len(winners) / len(trades)
        result.total_pnl_points = sum(t.pnl_points for t in trades)
        result.total_pnl_rupees = sum(t.pnl_rupees for t in trades)
        result.avg_holding_bars = np.mean([t.holding_bars for t in trades])

        gross_profit = sum(t.pnl_points for t in winners) if winners else 0
        gross_loss = abs(sum(t.pnl_points for t in losers)) if losers else 1e-9
        result.profit_factor = gross_profit / gross_loss

        result.avg_winner_pct = np.mean([t.pnl_pct for t in winners]) if winners else 0
        result.avg_loser_pct = np.mean([t.pnl_pct for t in losers]) if losers else 0

        if result.avg_loser_pct < 0:
            result.reward_risk_ratio = abs(result.avg_winner_pct / result.avg_loser_pct)

        # Consecutive runs
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for t in trades:
            if t.pnl_points > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_loss_streak = max(max_loss_streak, cur_loss)
        result.max_consecutive_wins = max_win_streak
        result.max_consecutive_losses = max_loss_streak

        # Exit breakdown
        for t in trades:
            if t.exit_reason == "TARGET_1":
                result.target_1_exits += 1
            elif t.exit_reason == "TARGET_2":
                result.target_2_exits += 1
            elif t.exit_reason == "TARGET_3":
                result.target_3_exits += 1
            elif t.exit_reason == "STOP_LOSS":
                result.sl_exits += 1
            elif t.exit_reason == "TIME_EXIT":
                result.time_exits += 1
            elif t.exit_reason == "EXPIRY":
                result.expiry_exits += 1

        # Sharpe ratio (annualized, assuming 252 trading days, 75 bars/day for 5-min)
        returns = np.array([t.pnl_pct for t in trades])
        if len(returns) > 1 and returns.std() > 0:
            result.sharpe_ratio = (returns.mean() / returns.std()) * math.sqrt(252)

        # Max drawdown
        equity = np.cumsum([t.pnl_rupees for t in trades])
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        if len(drawdown) > 0 and peak.max() > 0:
            result.max_drawdown_rupees = float(drawdown.max())
            result.max_drawdown_pct = float(drawdown.max() / max(peak.max(), 1e-9))

        return result

    # ── Walk-Forward Optimization ─────────────────────────────────────────────

    def walk_forward_optimize(
        self,
        train_pct: float = 0.70,
        n_windows: int = 5,
        param_grid: Optional[Dict[str, List]] = None,
    ) -> Dict[str, Any]:
        """
        Walk-forward validation to prevent curve fitting.

        1. Split data into n_windows rolling train/test splits
        2. On each train window: optimize MACD params
        3. On each test window: run with best params, record OOS metrics
        4. Accept if OOS win_rate > 50% and profit_factor > 1.3 in 4/5 windows
        5. Final params = median of best params across windows

        Returns dict with 'accepted', 'best_params', 'window_results'
        """
        if self._data is None:
            raise ValueError("No data loaded")

        if param_grid is None:
            param_grid = {
                "macd_fast": list(range(8, 16)),
                "macd_slow": list(range(20, 36)),
                "macd_signal": list(range(5, 13)),
            }

        df = self._data.copy()
        total_rows = len(df)
        window_size = total_rows // n_windows

        window_results = []
        best_params_list = []

        for w in range(n_windows):
            window_start = w * window_size
            window_end = window_start + window_size
            window_df = df.iloc[window_start:window_end]

            train_end = int(len(window_df) * train_pct)
            train_df = window_df.iloc[:train_end]
            test_df = window_df.iloc[train_end:]

            if len(train_df) < 100 or len(test_df) < 20:
                continue

            # Grid search on train
            best_pf = -1
            best_params = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9}

            for fast in param_grid["macd_fast"]:
                for slow in param_grid["macd_slow"]:
                    if slow <= fast + 3:
                        continue
                    for sig in param_grid["macd_signal"]:
                        try:
                            bt = OptionsMACDBacktester(
                                BacktestConfig(
                                    macd_fast=fast, macd_slow=slow, macd_signal=sig,
                                    max_concurrent=self.config.max_concurrent,
                                    sl_pct=self.config.sl_pct,
                                    target_1_pct=self.config.target_1_pct,
                                )
                            )
                            bt._data = train_df.copy()
                            bt._underlying = self._underlying
                            bt._market = self._market
                            train_results = bt.run_backtest()

                            if train_results:
                                pf = np.mean([r.profit_factor for r in train_results])
                                if pf > best_pf:
                                    best_pf = pf
                                    best_params = {"macd_fast": fast, "macd_slow": slow, "macd_signal": sig}
                        except Exception:
                            continue

            # Test on OOS window with best params
            bt_test = OptionsMACDBacktester(
                BacktestConfig(
                    macd_fast=best_params["macd_fast"],
                    macd_slow=best_params["macd_slow"],
                    macd_signal=best_params["macd_signal"],
                    max_concurrent=self.config.max_concurrent,
                    sl_pct=self.config.sl_pct,
                    target_1_pct=self.config.target_1_pct,
                )
            )
            bt_test._data = test_df.copy()
            bt_test._underlying = self._underlying
            bt_test._market = self._market
            oos_results = bt_test.run_backtest()

            if oos_results:
                oos_win_rate = np.mean([r.win_rate for r in oos_results])
                oos_pf = np.mean([r.profit_factor for r in oos_results])
                oos_sharpe = np.mean([r.sharpe_ratio for r in oos_results])
                accepted_window = (
                    oos_win_rate > 0.50
                    and oos_pf > 1.3
                    and oos_sharpe > 1.0
                )
            else:
                oos_win_rate = 0
                oos_pf = 0
                oos_sharpe = 0
                accepted_window = False

            window_results.append({
                "window": w + 1,
                "best_params": best_params,
                "oos_win_rate": oos_win_rate,
                "oos_profit_factor": oos_pf,
                "oos_sharpe": oos_sharpe,
                "accepted": accepted_window,
            })
            best_params_list.append(best_params)

        accepted_windows = sum(1 for w in window_results if w["accepted"])
        overall_accepted = accepted_windows >= max(1, n_windows - 1)  # 4 of 5

        if best_params_list:
            final_params = {
                "macd_fast": int(np.median([p["macd_fast"] for p in best_params_list])),
                "macd_slow": int(np.median([p["macd_slow"] for p in best_params_list])),
                "macd_signal": int(np.median([p["macd_signal"] for p in best_params_list])),
            }
        else:
            final_params = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9}

        logger.info(
            f"Walk-forward: {accepted_windows}/{len(window_results)} windows accepted. "
            f"Final params: {final_params}. Overall: {'ACCEPTED' if overall_accepted else 'REJECTED'}"
        )

        return {
            "accepted": overall_accepted,
            "accepted_windows": accepted_windows,
            "total_windows": len(window_results),
            "best_params": final_params,
            "window_results": window_results,
        }

    # ── Parameter Sensitivity ─────────────────────────────────────────────────

    def parameter_sensitivity(
        self,
        param: str,
        values: List,
    ) -> List[Dict]:
        """
        Test a single parameter across a range of values, return metrics for each.
        Useful for visualizing the sensitivity of the strategy to MACD parameters.
        """
        if self._data is None:
            raise ValueError("No data loaded")

        sensitivity = []
        for val in values:
            cfg = BacktestConfig(
                macd_fast=self.config.macd_fast,
                macd_slow=self.config.macd_slow,
                macd_signal=self.config.macd_signal,
                sl_pct=self.config.sl_pct,
                target_1_pct=self.config.target_1_pct,
            )
            setattr(cfg, param, val)

            bt = OptionsMACDBacktester(cfg)
            bt._data = self._data.copy()
            bt._underlying = self._underlying
            bt._market = self._market

            try:
                results = bt.run_backtest()
                if results:
                    r = results[0]
                    sensitivity.append({
                        "param_value": val,
                        "win_rate": r.win_rate,
                        "profit_factor": r.profit_factor,
                        "sharpe_ratio": r.sharpe_ratio,
                        "max_drawdown_pct": r.max_drawdown_pct,
                        "total_trades": r.total_trades,
                    })
            except Exception as e:
                logger.warning(f"Sensitivity test failed for {param}={val}: {e}")

        return sensitivity

    # ── Report Generation ─────────────────────────────────────────────────────

    def generate_report(self, results: List[BacktestResult]) -> Dict:
        """Generate a comprehensive summary report."""
        if not results:
            return {"error": "No results to report"}

        report = {
            "summary": {
                "underlying": self._underlying,
                "market": self._market,
                "instruments": len(results),
                "config": {
                    "macd_fast": self.config.macd_fast,
                    "macd_slow": self.config.macd_slow,
                    "macd_signal": self.config.macd_signal,
                    "sl_pct": self.config.sl_pct,
                    "target_1_pct": self.config.target_1_pct,
                    "target_2_pct": self.config.target_2_pct,
                    "target_3_pct": self.config.target_3_pct,
                },
            },
            "aggregate": {
                "total_trades": sum(r.total_trades for r in results),
                "avg_win_rate": float(np.mean([r.win_rate for r in results])),
                "avg_profit_factor": float(np.mean([r.profit_factor for r in results])),
                "avg_sharpe": float(np.mean([r.sharpe_ratio for r in results])),
                "avg_max_drawdown_pct": float(np.mean([r.max_drawdown_pct for r in results])),
                "total_pnl_rupees": float(sum(r.total_pnl_rupees for r in results)),
            },
            "by_instrument": [
                {
                    "option_type": r.option_type,
                    "total_signals": r.total_signals,
                    "total_trades": r.total_trades,
                    "win_rate": round(r.win_rate, 4),
                    "profit_factor": round(r.profit_factor, 2),
                    "sharpe_ratio": round(r.sharpe_ratio, 2),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 4),
                    "avg_holding_bars": round(r.avg_holding_bars, 1),
                    "reward_risk_ratio": round(r.reward_risk_ratio, 2),
                    "exit_breakdown": {
                        "target_1": r.target_1_exits,
                        "target_2": r.target_2_exits,
                        "target_3": r.target_3_exits,
                        "stop_loss": r.sl_exits,
                        "time_exit": r.time_exits,
                        "expiry": r.expiry_exits,
                    },
                }
                for r in results
            ],
        }
        return report
