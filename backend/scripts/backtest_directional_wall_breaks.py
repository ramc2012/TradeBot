"""Backtest directional option breaks using auditable wall/ATM strike selection."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
DEFAULT_ALLOC_RATIOS = [0.01, 0.02, 0.05, 0.10, 0.20]
DEFAULT_HORIZONS = [24, 48, 72]


@dataclass
class PortfolioSummary:
    horizon_bars: int
    alloc_ratio: float
    trades: int
    final_equity: float
    return_pct: float
    win_rate: float
    total_pnl: float
    max_drawdown_pct: float
    avg_option_return_pct: float
    median_option_return_pct: float
    call_trades: int
    put_trades: int


def _parse_float_list(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default.copy()
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_int_list(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return default.copy()
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _kind(symbol: str) -> str:
    return "INDEX" if symbol.upper() in INDEX_SYMBOLS else "STOCK"


def _finite(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _load_history(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_parquet(path)
    required = {"time", "underlying", "expiry", "strike", "option_type", "close", "oi"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"Input missing columns: {sorted(missing)}")
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df = df[df["time"].notna()]
    df["underlying"] = df["underlying"].astype(str).str.upper()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date.astype(str)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["option_type"] = df["option_type"].astype(str).str.upper()
    for col in ["close", "oi", "volume", "gamma", "underlying_price", "lot_size"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "instrument_key" not in df.columns:
        df["instrument_key"] = (
            df["underlying"]
            + "|"
            + df["expiry"].astype(str)
            + "|"
            + df["strike"].astype(str)
            + "|"
            + df["option_type"]
        )
    if "trading_symbol" not in df.columns:
        df["trading_symbol"] = ""
    df = df[(df["strike"] > 0) & (df["close"] > 0)]
    df = df.sort_values(["underlying", "expiry", "strike", "option_type", "time"])
    return df


def _add_contract_features(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    group_cols = ["instrument_key", "option_type"]
    df = df.copy()
    prev_close = df.groupby(group_cols)["close"].shift(lookback)
    prev_oi = df.groupby(group_cols)["oi"].shift(lookback)
    prev_gex = df.groupby(group_cols)["contract_abs_gex"].shift(lookback)
    df["opt_ret_lb"] = df["close"] / prev_close - 1.0
    df["oi_chg_lb"] = (df["oi"] - prev_oi) / prev_oi.abs().clip(lower=1.0)
    df["wall_gex_chg_lb"] = df["contract_abs_gex"] / prev_gex.replace(0, pd.NA) - 1.0
    df[["opt_ret_lb", "oi_chg_lb", "wall_gex_chg_lb"]] = df[
        ["opt_ret_lb", "oi_chg_lb", "wall_gex_chg_lb"]
    ].replace([math.inf, -math.inf], pd.NA)
    return df


def _build_states(df: pd.DataFrame, min_strikes: int) -> pd.DataFrame:
    rows: list[dict] = []
    grouped = df.groupby(["underlying", "expiry", "time"], sort=True)
    for (underlying, expiry, ts), group in grouped:
        spot = _finite(group["underlying_price"].dropna().median(), 0.0)
        if spot <= 0:
            continue
        strikes = sorted(group["strike"].dropna().unique())
        if len(strikes) < min_strikes:
            continue
        atm_strike = min(strikes, key=lambda strike: (abs(strike - spot), strike))
        ce = group[group["option_type"] == "CE"]
        pe = group[group["option_type"] == "PE"]
        if ce.empty or pe.empty:
            continue
        ce_wall_row = ce.loc[ce["contract_abs_gex"].idxmax()]
        pe_wall_row = pe.loc[pe["contract_abs_gex"].idxmax()]
        if float(ce_wall_row["contract_abs_gex"]) <= 0 or float(pe_wall_row["contract_abs_gex"]) <= 0:
            continue
        rows.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "time": ts,
                "kind": _kind(underlying),
                "spot": spot,
                "strike_count": len(strikes),
                "atm_strike": atm_strike,
                "call_wall": float(ce_wall_row["strike"]),
                "put_wall": float(pe_wall_row["strike"]),
                "call_wall_gex": float(ce_wall_row["contract_abs_gex"]),
                "put_wall_gex": float(pe_wall_row["contract_abs_gex"]),
                "net_gex_ratio": (
                    float(ce["contract_abs_gex"].sum() - pe["contract_abs_gex"].sum())
                    / max(float(ce["contract_abs_gex"].sum() + pe["contract_abs_gex"].sum()), 1.0)
                ),
            }
        )
    return pd.DataFrame(rows)


def _pick_trade_row(
    group: pd.DataFrame,
    *,
    option_type: str,
    target_strike: float,
    atm_strike: float,
    selection: str,
) -> tuple[pd.Series | None, str]:
    side = group[group["option_type"] == option_type]
    if side.empty:
        return None, "missing_side"
    if selection == "atm":
        selected = atm_strike
        mode = "atm"
    else:
        selected = target_strike
        mode = "wall_exact"
    exact = side[side["strike"] == selected]
    if not exact.empty:
        return exact.iloc[0], mode
    nearest_idx = (side["strike"] - selected).abs().idxmin()
    row = side.loc[nearest_idx]
    fallback_mode = "nearest_wall" if selection == "wall" else "nearest_atm"
    return row, fallback_mode


def _make_candidates(
    df: pd.DataFrame,
    states: pd.DataFrame,
    *,
    horizons: list[int],
    index_threshold: float,
    stock_threshold: float,
    selection: str,
    min_break_pressure: float,
    require_flow_confirm: bool,
) -> pd.DataFrame:
    by_snapshot = {
        key: group
        for key, group in df.groupby(["underlying", "expiry", "time"], sort=False)
    }
    by_contract = {
        key: group.sort_values("time").reset_index(drop=True)
        for key, group in df.groupby(["instrument_key", "option_type"], sort=False)
    }
    contract_index: dict[tuple[str, str], dict[pd.Timestamp, int]] = {}
    for key, group in by_contract.items():
        contract_index[key] = {ts: idx for idx, ts in enumerate(group["time"])}

    rows: list[dict] = []
    for state in states.itertuples(index=False):
        threshold = index_threshold if state.kind == "INDEX" else stock_threshold
        snapshot = by_snapshot.get((state.underlying, state.expiry, state.time))
        if snapshot is None or snapshot.empty:
            continue
        configs = [
            ("CALL_BREAK", "CE", float(state.call_wall), (float(state.call_wall) - float(state.spot)) / float(state.spot)),
            ("PUT_BREAK", "PE", float(state.put_wall), (float(state.spot) - float(state.put_wall)) / float(state.spot)),
        ]
        for side, option_type, wall, distance in configs:
            if distance < 0 or distance > threshold:
                continue
            trade_row, selection_mode = _pick_trade_row(
                snapshot,
                option_type=option_type,
                target_strike=wall,
                atm_strike=float(state.atm_strike),
                selection=selection,
            )
            if trade_row is None:
                continue
            opt_ret_lb = _finite(trade_row.get("opt_ret_lb"), 0.0)
            oi_chg_lb = _finite(trade_row.get("oi_chg_lb"), 0.0)
            gex_chg_lb = _finite(trade_row.get("wall_gex_chg_lb"), 0.0)
            break_pressure = opt_ret_lb + oi_chg_lb - max(gex_chg_lb, 0.0)
            flow_confirm = opt_ret_lb > 0 and oi_chg_lb > 0
            if break_pressure <= min_break_pressure:
                continue
            if require_flow_confirm and not flow_confirm:
                continue

            key = (str(trade_row["instrument_key"]), option_type)
            series = by_contract.get(key)
            index_map = contract_index.get(key)
            if series is None or index_map is None:
                continue
            entry_idx = index_map.get(state.time)
            if entry_idx is None:
                continue
            for horizon in horizons:
                exit_idx = entry_idx + horizon
                if exit_idx >= len(series):
                    continue
                exit_row = series.iloc[exit_idx]
                entry_price = _finite(trade_row["close"])
                exit_price = _finite(exit_row["close"])
                if entry_price <= 0 or exit_price <= 0:
                    continue
                rows.append(
                    {
                        "entry_time": state.time,
                        "exit_time": exit_row["time"],
                        "underlying": state.underlying,
                        "kind": state.kind,
                        "expiry": state.expiry,
                        "side": side,
                        "option_type": option_type,
                        "horizon_bars": horizon,
                        "selection": selection,
                        "selection_mode": selection_mode,
                        "wall_strike": wall,
                        "atm_strike": float(state.atm_strike),
                        "trade_strike": float(trade_row["strike"]),
                        "spot": float(state.spot),
                        "distance_to_wall_pct": distance,
                        "strike_count": int(state.strike_count),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "return_pct": exit_price / entry_price - 1.0,
                        "break_pressure": break_pressure,
                        "opt_ret_lb": opt_ret_lb,
                        "oi_chg_lb": oi_chg_lb,
                        "wall_gex_chg_lb": gex_chg_lb,
                        "flow_confirm": flow_confirm,
                        "net_gex_ratio": float(state.net_gex_ratio),
                        "instrument_key": str(trade_row["instrument_key"]),
                        "trading_symbol": str(trade_row.get("trading_symbol") or ""),
                    }
                )
    return pd.DataFrame(rows)


def _simulate_portfolio(
    candidates: pd.DataFrame,
    *,
    initial_capital: float,
    alloc_ratios: list[float],
    horizons: list[int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summaries: list[PortfolioSummary] = []
    executed_rows: list[dict] = []
    curve_rows: list[dict] = []
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    for horizon in horizons:
        horizon_df = candidates[candidates["horizon_bars"] == horizon].sort_values("entry_time")
        for alloc in alloc_ratios:
            cash = initial_capital
            equity_peak = initial_capital
            max_dd = 0.0
            active: list[dict] = []
            closed: list[dict] = []

            def close_matured(now: pd.Timestamp) -> None:
                nonlocal cash, equity_peak, max_dd, active
                still_active: list[dict] = []
                for trade in active:
                    if trade["exit_time"] <= now:
                        cash += trade["exit_value"]
                        closed.append(trade)
                        curve_rows.append(
                            {
                                "time": trade["exit_time"],
                                "horizon_bars": horizon,
                                "alloc_ratio": alloc,
                                "equity": cash,
                            }
                        )
                        equity_peak = max(equity_peak, cash)
                        if equity_peak > 0:
                            max_dd = max(max_dd, (equity_peak - cash) / equity_peak)
                    else:
                        still_active.append(trade)
                active = still_active

            for row in horizon_df.itertuples(index=False):
                close_matured(row.entry_time)
                key = (row.underlying, row.side)
                if any(trade["key"] == key for trade in active):
                    continue
                allocation = cash * alloc
                if allocation <= 0 or allocation > cash:
                    continue
                cash -= allocation
                realized_pnl = allocation * float(row.return_pct)
                trade = row._asdict()
                trade.update(
                    {
                        "alloc_ratio": alloc,
                        "cost": allocation,
                        "realized_pnl": realized_pnl,
                        "exit_value": allocation + realized_pnl,
                        "key": key,
                    }
                )
                active.append(trade)
                executed_rows.append(trade.copy())
            close_matured(pd.Timestamp.max.tz_localize("UTC"))

            trades = pd.DataFrame(closed)
            if trades.empty:
                summaries.append(
                    PortfolioSummary(horizon, alloc, 0, cash, (cash / initial_capital - 1) * 100, 0, 0, max_dd * 100, 0, 0, 0, 0)
                )
                continue
            summaries.append(
                PortfolioSummary(
                    horizon_bars=horizon,
                    alloc_ratio=alloc,
                    trades=len(trades),
                    final_equity=cash,
                    return_pct=(cash / initial_capital - 1.0) * 100.0,
                    win_rate=float((trades["realized_pnl"] > 0).mean()),
                    total_pnl=float(trades["realized_pnl"].sum()),
                    max_drawdown_pct=max_dd * 100.0,
                    avg_option_return_pct=float(trades["return_pct"].mean() * 100.0),
                    median_option_return_pct=float(trades["return_pct"].median() * 100.0),
                    call_trades=int((trades["side"] == "CALL_BREAK").sum()),
                    put_trades=int((trades["side"] == "PUT_BREAK").sum()),
                )
            )

    return (
        pd.DataFrame([asdict(item) for item in summaries]),
        pd.DataFrame(executed_rows),
        pd.DataFrame(curve_rows),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Prepared option_candles parquet/csv.")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--selection", choices=["wall", "atm"], default="wall")
    parser.add_argument("--horizons", default="24,48,72")
    parser.add_argument("--alloc-ratios", default="0.01,0.02,0.05,0.10,0.20")
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--lookback", type=int, default=3)
    parser.add_argument("--index-threshold", type=float, default=0.005)
    parser.add_argument("--stock-threshold", type=float, default=0.01)
    parser.add_argument("--min-index-strikes", type=int, default=30)
    parser.add_argument("--min-stock-strikes", type=int, default=10)
    parser.add_argument("--min-break-pressure", type=float, default=0.0)
    parser.add_argument("--no-flow-confirm", action="store_true")
    args = parser.parse_args()

    horizons = _parse_int_list(args.horizons, DEFAULT_HORIZONS)
    alloc_ratios = _parse_float_list(args.alloc_ratios, DEFAULT_ALLOC_RATIOS)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _load_history(args.input)
    df["kind"] = df["underlying"].map(_kind)
    df["lot_size"] = df["lot_size"].fillna(1).replace(0, 1)
    df["contract_abs_gex"] = (
        df["gamma"].abs().fillna(0)
        * df["oi"].clip(lower=0).fillna(0)
        * df["lot_size"].fillna(1)
        * df["underlying_price"].fillna(0).pow(2)
    )
    df = _add_contract_features(df, args.lookback)

    index_states = _build_states(df[df["kind"] == "INDEX"], args.min_index_strikes)
    stock_states = _build_states(df[df["kind"] == "STOCK"], args.min_stock_strikes)
    states = pd.concat([index_states, stock_states], ignore_index=True) if not index_states.empty or not stock_states.empty else pd.DataFrame()
    candidates = _make_candidates(
        df,
        states,
        horizons=horizons,
        index_threshold=args.index_threshold,
        stock_threshold=args.stock_threshold,
        selection=args.selection,
        min_break_pressure=args.min_break_pressure,
        require_flow_confirm=not args.no_flow_confirm,
    )
    portfolio, executed, curve = _simulate_portfolio(
        candidates,
        initial_capital=args.initial_capital,
        alloc_ratios=alloc_ratios,
        horizons=horizons,
    )

    states.to_csv(out_dir / "wall_states.csv", index=False)
    candidates.to_csv(out_dir / "trade_candidates.csv", index=False)
    portfolio.to_csv(out_dir / "portfolio_summary.csv", index=False)
    executed.to_csv(out_dir / "executed_trades.csv", index=False)
    curve.to_csv(out_dir / "equity_curves.csv", index=False)

    if not executed.empty:
        breakdown = (
            executed.groupby(["horizon_bars", "alloc_ratio", "side"])
            .agg(
                trades=("return_pct", "count"),
                win_rate=("realized_pnl", lambda s: float((s > 0).mean())),
                pnl=("realized_pnl", "sum"),
                avg_option_return_pct=("return_pct", lambda s: float(s.mean() * 100.0)),
                median_option_return_pct=("return_pct", lambda s: float(s.median() * 100.0)),
            )
            .reset_index()
        )
        breakdown.to_csv(out_dir / "call_put_breakdown.csv", index=False)
        instrument = (
            executed.groupby(["horizon_bars", "alloc_ratio", "underlying", "side"])
            .agg(
                trades=("return_pct", "count"),
                pnl=("realized_pnl", "sum"),
                avg_option_return_pct=("return_pct", lambda s: float(s.mean() * 100.0)),
            )
            .reset_index()
        )
        instrument.to_csv(out_dir / "instrument_breakdown.csv", index=False)

    summary = {
        "input": str(args.input),
        "selection": args.selection,
        "rows": int(len(df)),
        "state_rows": int(len(states)),
        "candidate_rows": int(len(candidates)),
        "executed_rows": int(len(executed)),
        "horizons": horizons,
        "alloc_ratios": alloc_ratios,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
