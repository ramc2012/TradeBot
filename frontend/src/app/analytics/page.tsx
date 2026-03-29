"use client";
import { useQuery } from "@tanstack/react-query";
import {
  getPerformance, getEquityCurve, getCalendarHeatmap,
  getPortfolioGreeks, getTrades,
} from "@/lib/api";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { clsx } from "clsx";

function MetricCard({ label, value, color = "text-text-primary" }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="card p-4 text-center">
      <div className="text-text-muted text-xs mb-1">{label}</div>
      <div className={`font-mono text-lg font-bold ${color}`}>{value}</div>
    </div>
  );
}

function GreekBadge({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className={`px-3 py-2 rounded bg-bg-secondary border border-bg-border flex flex-col items-center`}>
      <span className="text-text-muted text-xs">{label}</span>
      <span className={`font-mono font-bold text-sm ${color}`}>{value.toFixed(4)}</span>
    </div>
  );
}

export default function AnalyticsPage() {
  const { data: perf } = useQuery({
    queryKey: ["performance", "all"],
    queryFn: () => getPerformance("all").then((r) => r.data),
    refetchInterval: 10000,
  });

  const { data: curve } = useQuery({
    queryKey: ["equityCurve"],
    queryFn: () => getEquityCurve().then((r) => r.data),
    refetchInterval: 10000,
  });

  const { data: heatmap } = useQuery({
    queryKey: ["heatmap"],
    queryFn: () => getCalendarHeatmap().then((r) => r.data),
  });

  const { data: greeks } = useQuery({
    queryKey: ["portfolioGreeks"],
    queryFn: () => getPortfolioGreeks().then((r) => r.data),
    refetchInterval: 5000,
  });

  const { data: trades } = useQuery({
    queryKey: ["trades"],
    queryFn: () => getTrades().then((r) => r.data),
    refetchInterval: 10000,
  });

  const curveData = curve || [];
  const tradeList = trades || [];

  return (
    <div className="max-w-screen-xl space-y-4">
      <h1 className="text-lg font-bold font-mono text-text-primary">Analytics</h1>

      {/* Metrics Row */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <MetricCard label="Total P&L" value={perf ? `₹${perf.total_pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : "--"} color={perf?.total_pnl >= 0 ? "text-accent-green" : "text-accent-red"} />
        <MetricCard label="Win Rate" value={perf ? `${(perf.win_rate * 100).toFixed(1)}%` : "--"} />
        <MetricCard label="Profit Factor" value={perf ? perf.profit_factor.toFixed(2) : "--"} />
        <MetricCard label="Sharpe" value={perf ? perf.sharpe_ratio.toFixed(2) : "--"} />
        <MetricCard label="Max DD" value={perf ? `${(perf.max_drawdown * 100).toFixed(2)}%` : "--"} color="text-accent-red" />
      </div>

      {/* Equity Curve */}
      <div className="card p-4">
        <h2 className="text-sm text-text-secondary mb-4">Equity Curve</h2>
        {curveData.length > 0 ? (
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={curveData}>
              <defs>
                <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#00d4a3" stopOpacity={0.2} />
                  <stop offset="95%" stopColor="#00d4a3" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
              <XAxis dataKey="timestamp" tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(v) => v.slice(0, 10)} />
              <YAxis tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`} />
              <Tooltip
                contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: "4px" }}
                labelStyle={{ color: "#94a3b8" }}
                itemStyle={{ color: "#00d4a3" }}
              />
              <Area type="monotone" dataKey="equity" stroke="#00d4a3" fill="url(#eq)" strokeWidth={2} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-48 flex items-center justify-center text-text-muted text-sm">
            No trade history yet
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Portfolio Greeks */}
        <div className="card p-4">
          <h2 className="text-sm text-text-secondary mb-4">Portfolio Greeks</h2>
          {greeks ? (
            <div className="grid grid-cols-4 gap-2">
              <GreekBadge label="Δ Delta" value={greeks.delta} color="text-accent-blue" />
              <GreekBadge label="Γ Gamma" value={greeks.gamma} color="text-accent-purple" />
              <GreekBadge label="Θ Theta" value={greeks.theta} color="text-accent-red" />
              <GreekBadge label="V Vega" value={greeks.vega} color="text-accent-amber" />
            </div>
          ) : (
            <div className="text-text-muted text-sm text-center py-4">No option positions</div>
          )}
        </div>

        {/* Trade Stats */}
        <div className="card p-4">
          <h2 className="text-sm text-text-secondary mb-4">Performance Summary</h2>
          {perf ? (
            <div className="grid grid-cols-2 gap-3 text-xs font-mono">
              <div className="flex justify-between"><span className="text-text-muted">Total Trades</span><span>{perf.total_trades}</span></div>
              <div className="flex justify-between"><span className="text-text-muted">Avg Win</span><span className="text-accent-green">₹{perf.avg_win.toFixed(0)}</span></div>
              <div className="flex justify-between"><span className="text-text-muted">Day P&L</span><span className={perf.day_pnl >= 0 ? "text-accent-green" : "text-accent-red"}>₹{perf.day_pnl.toFixed(0)}</span></div>
              <div className="flex justify-between"><span className="text-text-muted">Avg Loss</span><span className="text-accent-red">₹{perf.avg_loss.toFixed(0)}</span></div>
            </div>
          ) : (
            <div className="text-text-muted text-sm text-center py-4">No data</div>
          )}
        </div>
      </div>

      {/* Trade History */}
      <div className="card p-4">
        <h2 className="text-sm text-text-secondary mb-3">Trade History</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead>
              <tr className="text-text-muted border-b border-bg-border">
                <th className="text-left pb-2">Symbol</th>
                <th className="text-left pb-2">Side</th>
                <th className="text-right pb-2">Qty</th>
                <th className="text-right pb-2">Entry</th>
                <th className="text-right pb-2">Exit</th>
                <th className="text-right pb-2">P&L</th>
                <th className="text-right pb-2">Time</th>
              </tr>
            </thead>
            <tbody>
              {tradeList.length > 0 ? tradeList.map((t: any, i: number) => (
                <tr key={i} className="border-b border-bg-border/50 hover:bg-bg-hover/30">
                  <td className="py-1.5 text-accent-blue">{t.symbol?.split(":")[1] || t.symbol}</td>
                  <td className={clsx("py-1.5", t.action === "BUY" ? "text-accent-green" : "text-accent-red")}>{t.action}</td>
                  <td className="py-1.5 text-right">{t.qty}</td>
                  <td className="py-1.5 text-right">{t.entry_price?.toFixed(2)}</td>
                  <td className="py-1.5 text-right">{t.exit_price?.toFixed(2)}</td>
                  <td className={clsx("py-1.5 text-right font-bold", (t.pnl ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                    {(t.pnl ?? 0) >= 0 ? "+" : ""}₹{Math.abs(t.pnl ?? 0).toFixed(0)}
                  </td>
                  <td className="py-1.5 text-right text-text-muted">{t.exit_time?.slice(11, 16)}</td>
                </tr>
              )) : (
                <tr><td colSpan={7} className="py-6 text-center text-text-muted">No trades</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
