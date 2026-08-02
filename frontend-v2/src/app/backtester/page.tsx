import { redirect } from "next/navigation";

// The backtest runner lives on /research as the "Backtests" tab
// (components/backtester/BacktesterDesk). This route survives for old links.
export default function Page() {
  redirect("/research?tab=backtests");
}
