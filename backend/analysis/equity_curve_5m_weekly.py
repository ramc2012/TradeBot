"""
Compounded equity curve — 5m MACD zero-cross, weekly series, target_30pct exit.

Rules:
  - Start: ₹100,000
  - Each trade: deploy 100% of current equity into one option contract
    (return_pct is applied to full equity — single-contract compounding)
  - Trades are sorted by entry_time chronologically
  - Same-day overlapping trades are allowed (sequential within day)
  - Draws max-drawdown, win streaks, and per-underlying split
"""
from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.dates as mdates
import numpy as np

# ── Load data ─────────────────────────────────────────────────────────────────

DATA_CSV = (
    Path(__file__).resolve().parent.parent
    / "runtime/index_analytics_data/timeframe_sweep/trade_results.csv"
)
OUTPUT_DIR = DATA_CSV.parent / "equity_curves"
OUTPUT_DIR.mkdir(exist_ok=True)

def load_trades(exit_strategy: str = "target_30pct") -> list[dict]:
    with DATA_CSV.open() as fh:
        rows = list(csv.DictReader(fh))
    trades = [
        r for r in rows
        if r["timeframe"] == "5m"
        and r["expiry_day_only"] == "False"
        and r["expiry_kind"] == "weekly"
        and r["exit_strategy"] == exit_strategy
    ]
    # Sort chronologically by entry_time
    trades.sort(key=lambda r: r["entry_time"])
    return trades


# ── Compounding engine ─────────────────────────────────────────────────────────

def compound_equity(
    trades: list[dict],
    start: float = 100_000.0,
    alloc_frac: float = 0.20,       # fraction of equity deployed per trade
    stop_loss_pct: float | None = None,  # cap loss at this level (e.g. -50 means -50%)
) -> tuple[list[datetime], list[float], list[dict]]:
    """
    Fixed-fraction compounding with optional stop-loss cap.
    Each trade deploys `alloc_frac` of current equity.
    P&L = deployed × effective_return (capped at stop_loss_pct if provided).
    """
    equity = start
    equity_curve: list[float] = [start]
    timestamps: list[datetime] = [datetime.fromisoformat(trades[0]["entry_time"])]
    annotated: list[dict] = []
    peak = start

    for t in trades:
        ret = float(t["return_pct"])
        # Apply stop loss cap to raw return
        if stop_loss_pct is not None and ret < stop_loss_pct:
            ret = stop_loss_pct
        deployed = equity * alloc_frac
        pnl = deployed * ret / 100.0
        equity = max(equity + pnl, 0.0)

        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0

        ts = datetime.fromisoformat(t["exit_time"])
        equity_curve.append(equity)
        timestamps.append(ts)
        annotated.append({**t, "equity_after": round(equity, 2), "drawdown_pct": round(dd, 2)})

    return timestamps, equity_curve, annotated


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(equity_curve: list[float], trades: list[dict]) -> dict:
    start = equity_curve[0]
    end = equity_curve[-1]
    total_return = (end - start) / start * 100.0

    peak = start
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    returns = [float(t["return_pct"]) for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    # CAGR — data spans ~1 year
    n_days = (
        datetime.fromisoformat(trades[-1]["exit_time"])
        - datetime.fromisoformat(trades[0]["entry_time"])
    ).days
    years = n_days / 365.0
    cagr = ((end / start) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0

    # Calmar
    calmar = cagr / max_dd if max_dd > 0 else float("inf")

    # Consecutive win/loss streaks
    best_streak = worst_streak = cur_streak = 0
    cur_side = None
    for r in returns:
        side = "W" if r > 0 else "L"
        if side == cur_side:
            cur_streak += 1
        else:
            cur_streak = 1
            cur_side = side
        if side == "W":
            best_streak = max(best_streak, cur_streak)
        else:
            worst_streak = max(worst_streak, cur_streak)

    return {
        "start_equity":    start,
        "end_equity":      round(end, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct":        round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "calmar_ratio":    round(calmar, 2),
        "n_trades":        len(trades),
        "win_rate":        round(len(wins) / len(trades) * 100, 1),
        "avg_win":         round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss":        round(sum(losses) / len(losses), 2) if losses else 0,
        "profit_factor":   round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) else 999,
        "best_streak":     best_streak,
        "worst_streak":    worst_streak,
        "data_days":       n_days,
    }


# ── Plot ──────────────────────────────────────────────────────────────────────

def plot(exit_strategy: str = "target_30pct", alloc_frac: float = 0.20) -> Path:
    trades = load_trades(exit_strategy)
    print(f"Loaded {len(trades)} trades for 5m weekly {exit_strategy}  (alloc={alloc_frac*100:.0f}%)")

    # Primary: 20% alloc + -50% stop loss (the realistic tradeable scenario)
    timestamps, equity_curve, annotated = compound_equity(trades, alloc_frac=alloc_frac, stop_loss_pct=-50.0)
    stats = compute_stats(equity_curve, trades)

    # Comparison curves (same alloc, different stop levels)
    _, eq_nostop, _ = compound_equity(trades, alloc_frac=alloc_frac, stop_loss_pct=None)
    _, eq_sl30,   _ = compound_equity(trades, alloc_frac=alloc_frac, stop_loss_pct=-30.0)
    eq_sl50 = equity_curve  # already computed above as primary

    # Per-underlying split (20% alloc, -50% SL)
    nifty_trades  = [t for t in trades if t["underlying"] == "NIFTY"]
    sensex_trades = [t for t in trades if t["underlying"] == "SENSEX"]
    _, nifty_eq,  _ = compound_equity(nifty_trades,  alloc_frac=alloc_frac, stop_loss_pct=-50.0) if nifty_trades  else ([], [100_000], [])
    _, sensex_eq, _ = compound_equity(sensex_trades, alloc_frac=alloc_frac, stop_loss_pct=-50.0) if sensex_trades else ([], [100_000], [])
    nifty_ts  = [datetime.fromisoformat(t["entry_time"]) for t in nifty_trades]
    sensex_ts = [datetime.fromisoformat(t["entry_time"]) for t in sensex_trades]

    # Drawdown series
    peak = equity_curve[0]
    dd_series = []
    for v in equity_curve:
        if v > peak:
            peak = v
        dd_series.append(-(peak - v) / peak * 100.0)

    # ── Figure layout ──────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(22, 16), facecolor="#0d1117")
    gs = fig.add_gridspec(3, 2, height_ratios=[3, 1.4, 1.4],
                          hspace=0.50, wspace=0.38,
                          left=0.07, right=0.97, top=0.90, bottom=0.07)

    ax_main  = fig.add_subplot(gs[0, :])   # full-width equity
    ax_dd    = fig.add_subplot(gs[1, :])   # full-width drawdown
    ax_ul    = fig.add_subplot(gs[2, 0])   # per-underlying
    ax_dist  = fig.add_subplot(gs[2, 1])   # return distribution

    DARK_BG   = "#0d1117"
    PANEL_BG  = "#161b22"
    GREEN     = "#3fb950"
    RED       = "#f85149"
    BLUE      = "#58a6ff"
    ORANGE    = "#e3b341"
    MUTED     = "#8b949e"
    WHITE     = "#e6edf3"

    for ax in (ax_main, ax_dd, ax_ul, ax_dist):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=MUTED, labelsize=10)
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")

    # ── Main equity curve ─────────────────────────────────────────────────────
    eq_arr = np.array(equity_curve)
    ts_arr = timestamps

    # Shade above/below starting equity (20% alloc — primary)
    ax_main.fill_between(ts_arr, equity_curve[0], eq_arr,
                         where=eq_arr >= equity_curve[0],
                         alpha=0.12, color=GREEN, interpolate=True)
    ax_main.fill_between(ts_arr, equity_curve[0], eq_arr,
                         where=eq_arr < equity_curve[0],
                         alpha=0.18, color=RED, interpolate=True)

    # 3 stop-loss scenario comparisons (same 20% alloc)
    ax_main.plot(ts_arr, np.array(eq_nostop), color=RED,    linewidth=1.0, linestyle="--",
                 alpha=0.7, zorder=2, label="No stop loss")
    ax_main.plot(ts_arr, np.array(eq_sl30),   color=ORANGE, linewidth=1.0, linestyle="--",
                 alpha=0.7, zorder=2, label="-30% stop loss")
    ax_main.plot(ts_arr, eq_arr,              color=GREEN,  linewidth=2.0, zorder=4,
                 label="-50% stop loss (primary)")

    # ATH line (20%)
    running_peak = np.maximum.accumulate(eq_arr)
    ax_main.plot(ts_arr, running_peak, color=MUTED, linewidth=0.6,
                 linestyle=":", alpha=0.5, zorder=2, label="ATH (20%)")

    # Mark max drawdown point
    peak_eq = equity_curve[0]
    max_dd_val = max_dd_idx = 0
    for i, v in enumerate(equity_curve):
        if v > peak_eq:
            peak_eq = v
        dd = (peak_eq - v) / peak_eq * 100.0
        if dd > max_dd_val:
            max_dd_val = dd
            max_dd_idx = i
    ax_main.scatter([timestamps[max_dd_idx]], [equity_curve[max_dd_idx]],
                    color=RED, s=60, zorder=5, label=f"Max DD -{max_dd_val:.1f}%")

    ax_main.set_title(f"Compounded Equity Curve — 5m MACD Zero-Cross | Weekly Series | target_30pct  [{alloc_frac*100:.0f}% alloc/trade, -50% SL]",
                       color=WHITE, fontsize=14, fontweight="bold", pad=10)
    ax_main.set_ylabel("Portfolio Value (₹)", color=MUTED, fontsize=8)
    ax_main.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x/1_00_000:.1f}L"))
    ax_main.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_main.xaxis.set_major_locator(mdates.MonthLocator())
    ax_main.tick_params(axis="x", rotation=30)
    ax_main.grid(True, color="#21262d", linewidth=0.5, alpha=0.8)
    ax_main.legend(framealpha=0, labelcolor=MUTED, fontsize=8)

    # Stats box
    s = stats
    stats_text = (
        f"Start ₹{s['start_equity']/1e5:.1f}L → End ₹{s['end_equity']/1e5:.1f}L   "
        f"Total Return: {s['total_return_pct']:+.1f}%   "
        f"CAGR: {s['cagr_pct']:.1f}%   "
        f"Max DD: -{s['max_drawdown_pct']:.1f}%   "
        f"Calmar: {s['calmar_ratio']:.2f}   "
        f"Trades: {s['n_trades']}   Win: {s['win_rate']}%   PF: {s['profit_factor']}"
    )
    ax_main.text(0.01, 0.97, stats_text, transform=ax_main.transAxes,
                 fontsize=10, color=BLUE, va="top",
                 bbox=dict(boxstyle="round,pad=0.3", facecolor="#1c2128", edgecolor="#30363d", alpha=0.9))

    # ── Drawdown ──────────────────────────────────────────────────────────────
    dd_arr = np.array(dd_series)
    ax_dd.fill_between(ts_arr, 0, dd_arr, color=RED, alpha=0.6, linewidth=0)
    ax_dd.plot(ts_arr, dd_arr, color=RED, linewidth=0.8)
    ax_dd.axhline(0, color=MUTED, linewidth=0.5, alpha=0.5)
    ax_dd.set_ylabel("Drawdown %", color=MUTED, fontsize=8)
    ax_dd.set_title("Drawdown from Peak", color=WHITE, fontsize=12, fontweight="bold")
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_dd.xaxis.set_major_locator(mdates.MonthLocator())
    ax_dd.tick_params(axis="x", rotation=30)
    ax_dd.grid(True, color="#21262d", linewidth=0.5, alpha=0.8)
    ax_dd.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))

    # ── Per-underlying equity ─────────────────────────────────────────────────
    if nifty_ts:
        ax_ul.plot([datetime.fromisoformat(t["entry_time"]) for t in nifty_trades] +
                   [datetime.fromisoformat(nifty_trades[-1]["exit_time"])],
                   nifty_eq, color=BLUE, linewidth=1.2, label=f"NIFTY ({len(nifty_trades)})")
    if sensex_ts:
        ax_ul.plot([datetime.fromisoformat(t["entry_time"]) for t in sensex_trades] +
                   [datetime.fromisoformat(sensex_trades[-1]["exit_time"])],
                   sensex_eq, color=ORANGE, linewidth=1.2, label=f"SENSEX ({len(sensex_trades)})")
    ax_ul.set_title("Per-Underlying Equity", color=WHITE, fontsize=12, fontweight="bold")
    ax_ul.set_ylabel("Value (₹)", color=MUTED, fontsize=8)
    ax_ul.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x/1e5:.1f}L"))
    ax_ul.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax_ul.xaxis.set_major_locator(mdates.MonthLocator())
    ax_ul.tick_params(axis="x", rotation=30)
    ax_ul.grid(True, color="#21262d", linewidth=0.5, alpha=0.8)
    ax_ul.legend(framealpha=0, labelcolor=MUTED, fontsize=8)

    # ── Return distribution ───────────────────────────────────────────────────
    returns = [float(t["return_pct"]) for t in trades]
    win_returns  = [r for r in returns if r > 0]
    loss_returns = [r for r in returns if r <= 0]
    bins = np.linspace(min(returns) - 5, max(returns) + 5, 40)
    ax_dist.hist(loss_returns, bins=bins, color=RED,   alpha=0.7, label=f"Loss ({len(loss_returns)})")
    ax_dist.hist(win_returns,  bins=bins, color=GREEN, alpha=0.7, label=f"Win ({len(win_returns)})")
    ax_dist.axvline(0, color=MUTED, linewidth=0.8, linestyle="--")
    ax_dist.set_title("Return Distribution", color=WHITE, fontsize=12, fontweight="bold")
    ax_dist.set_xlabel("Return %", color=MUTED, fontsize=8)
    ax_dist.grid(True, color="#21262d", linewidth=0.5, alpha=0.8)
    ax_dist.legend(framealpha=0, labelcolor=MUTED, fontsize=8)

    # ── Figure title ──────────────────────────────────────────────────────────
    fig.text(0.5, 0.955, "MACD Zero-Cross Strategy  ·  5-min  ·  Weekly ATM Options  ·  Compounded Returns",
             ha="center", fontsize=16, color=WHITE, fontweight="bold")
    fig.text(0.5, 0.935, f"NIFTY + SENSEX  ·  Apr 2025 – Apr 2026  ·  53 series  ·  ₹1L starting capital",
             ha="center", fontsize=12, color=MUTED)

    out_path = OUTPUT_DIR / f"5m_weekly_target30_equity_alloc{int(alloc_frac*100)}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"Saved: {out_path}")

    # Print stats
    print("\n── Stats ─────────────────────────────────────────────────────")
    for k, v in stats.items():
        print(f"  {k:<25} {v}")

    return out_path


if __name__ == "__main__":
    plot()
