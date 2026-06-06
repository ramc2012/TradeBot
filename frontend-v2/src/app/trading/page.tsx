/**
 * Execution desk — native v2 surface.
 *
 * Replaces the former v1 StrategyDashboard embed with the native
 * ExecutionDesk (DeskShell + desk-ui primitives): KPI strip, order +
 * position blotter, risk-status panel, and the kill-switch / auto-run
 * execution levers. The v1 page remains reachable from the desk header's
 * "v1 view" link (http://localhost:3000/strategy).
 */
import ExecutionDesk from "@/components/trading/ExecutionDesk";

export default function TradingPage() {
  return <ExecutionDesk />;
}
