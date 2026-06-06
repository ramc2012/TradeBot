"use client";

/**
 * /backtester — the Options-MACD backtest runner.
 *
 * Native v2 surface (replaces the v1 embed). Also rendered as the
 * "Backtests" tab inside the /research hub, which imports this page's
 * default export. Keeping the default export signature stable keeps that
 * embed working.
 */
import BacktesterDesk from "@/components/backtester/BacktesterDesk";

export default function BacktesterPage() {
  return <BacktesterDesk />;
}
