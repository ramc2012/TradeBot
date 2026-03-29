"use client";
import { useQuery } from "@tanstack/react-query";
import {
  getPerformance, getEquityCurve, getCalendarHeatmap,
  getPortfolioGreeks, getTrades, getSectorRotation,
} from "@/lib/api";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, ScatterChart, Scatter, ReferenceLine, LabelList,
} from "recharts";
import { clsx } from "clsx";

interface SectorWatchlistRow {
  code: string;
  name: string;
  symbol: string;
  price: number;
  tracked_change_pct: number;
  relative_strength_pct: number;
  rrg_ratio: number;
  rrg_momentum: number;
  quadrant: string;
  trend: string;
  samples: number;
}

interface SectorRotationPayload {
  benchmark?: {
    symbol: string;
    name: string;
    price: number;
    tracked_change_pct: number;
    samples: number;
  } | null;
  watchlist: SectorWatchlistRow[];
  rrg: {
    points: Array<SectorWatchlistRow & {
      trail: Array<{ ratio: number; momentum: number }>;
    }>;
    quadrant_counts: Record<string, number>;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
}

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

function formatSignedPct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(2)}%`;
}

function quadrantTone(quadrant?: string) {
  switch (quadrant) {
    case "leading":
      return "text-accent-green bg-accent-green/10 border-accent-green/20";
    case "improving":
      return "text-accent-blue bg-accent-blue/10 border-accent-blue/20";
    case "weakening":
      return "text-accent-amber bg-accent-amber/10 border-accent-amber/20";
    default:
      return "text-accent-red bg-accent-red/10 border-accent-red/20";
  }
}

function directionMeta(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return { icon: <Minus size={12} />, tone: "text-text-muted", badge: "bg-bg-primary text-text-muted" };
  }
  if (value > 0) {
    return { icon: <ArrowUpRight size={12} />, tone: "text-accent-green", badge: "bg-accent-green/10 text-accent-green" };
  }
  if (value < 0) {
    return { icon: <ArrowDownRight size={12} />, tone: "text-accent-red", badge: "bg-accent-red/10 text-accent-red" };
  }
  return { icon: <Minus size={12} />, tone: "text-text-secondary", badge: "bg-bg-primary text-text-secondary" };
}

function SignedPill({ value }: { value?: number | null }) {
  const direction = directionMeta(value);
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded-full px-2 py-1 text-[11px] font-semibold", direction.badge)}>
      {direction.icon}
      {formatSignedPct(value)}
    </span>
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

  const { data: sectorRotation } = useQuery<SectorRotationPayload>({
    queryKey: ["sectorRotation"],
    queryFn: () => getSectorRotation().then((r) => r.data),
    refetchInterval: 15000,
    staleTime: 5000,
  });

  const curveData = curve || [];
  const tradeList = trades || [];
  const rrgPoints = sectorRotation?.rrg?.points ?? [];
  const watchlist = sectorRotation?.watchlist ?? [];
  const xValues = rrgPoints.map((point) => point.rrg_ratio);
  const yValues = rrgPoints.map((point) => point.rrg_momentum);
  const topLeader = watchlist.find((sector) => sector.quadrant === "leading") ?? watchlist[0];
  const topImproving = [...watchlist]
    .filter((sector) => sector.quadrant === "improving")
    .sort((left, right) => right.rrg_momentum - left.rrg_momentum)[0];
  const quadrantPalette: Record<string, string> = {
    leading: "#00d4a3",
    improving: "#38bdf8",
    weakening: "#f59e0b",
    lagging: "#ef4444",
  };
  const quadrantSeries = Object.entries(quadrantPalette).map(([quadrant, color]) => ({
    quadrant,
    color,
    data: rrgPoints.filter((point) => point.quadrant === quadrant),
  }));
  const xDomain: [number, number] = [
    Math.min(95, ...(xValues.length ? xValues : [100])) - 1,
    Math.max(105, ...(xValues.length ? xValues : [100])) + 1,
  ];
  const yDomain: [number, number] = [
    Math.min(95, ...(yValues.length ? yValues : [100])) - 1,
    Math.max(105, ...(yValues.length ? yValues : [100])) + 1,
  ];

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

      <div className="grid grid-cols-1 xl:grid-cols-[1.05fr_0.95fr] gap-4">
        <div className="card p-4 space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div>
              <h2 className="text-sm text-text-secondary">Sector Watchlist</h2>
              <div className="text-xs text-text-muted">
                Relative strength and RRG state against NIFTY 50
              </div>
            </div>
            <div className="text-right text-xs text-text-muted">
              <div>{sectorRotation?.source?.toUpperCase() || "LIVE"}</div>
              {sectorRotation?.benchmark && (
                <div>
                  {sectorRotation.benchmark.name} {sectorRotation.benchmark.price.toFixed(2)} · {formatSignedPct(sectorRotation.benchmark.tracked_change_pct)}
                </div>
              )}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Benchmark</div>
              <div className="mt-2 text-sm font-semibold text-text-primary">{sectorRotation?.benchmark?.name || "NIFTY 50"}</div>
              <div className="mt-2">
                <SignedPill value={sectorRotation?.benchmark?.tracked_change_pct} />
              </div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Top Leader</div>
              <div className="mt-2 text-sm font-semibold text-text-primary">{topLeader?.name || "--"}</div>
              <div className="mt-2">
                <SignedPill value={topLeader?.relative_strength_pct} />
              </div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Top Improving</div>
              <div className="mt-2 text-sm font-semibold text-text-primary">{topImproving?.name || "--"}</div>
              <div className="mt-2">
                <SignedPill value={topImproving?.rrg_momentum != null ? topImproving.rrg_momentum - 100 : null} />
              </div>
            </div>
          </div>

          {sectorRotation?.watchlist?.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-text-muted border-b border-bg-border">
                    <th className="text-left pb-2">Sector</th>
                    <th className="text-right pb-2">Price</th>
                    <th className="text-right pb-2">Tracked</th>
                    <th className="text-right pb-2">RS vs NIFTY</th>
                    <th className="text-right pb-2">RRG Ratio</th>
                    <th className="text-right pb-2">Momentum</th>
                    <th className="text-right pb-2">Quadrant</th>
                  </tr>
                </thead>
                <tbody>
                  {sectorRotation.watchlist.map((sector) => (
                    <tr key={sector.code} className="border-b border-bg-border/40 hover:bg-bg-hover/30">
                      <td className="py-2">
                        <div className="font-semibold text-text-primary">{sector.name}</div>
                        <div className={clsx("mt-1 inline-flex rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide", quadrantTone(sector.quadrant))}>
                          {sector.trend}
                        </div>
                      </td>
                      <td className="py-2 text-right">{sector.price.toFixed(2)}</td>
                      <td className="py-2 text-right">
                        <SignedPill value={sector.tracked_change_pct} />
                      </td>
                      <td className="py-2 text-right">
                        <SignedPill value={sector.relative_strength_pct} />
                      </td>
                      <td className="py-2 text-right text-accent-blue">{sector.rrg_ratio.toFixed(2)}</td>
                      <td className={clsx("py-2 text-right font-semibold", sector.rrg_momentum >= 100 ? "text-accent-green" : "text-accent-red")}>
                        {sector.rrg_momentum.toFixed(2)}
                      </td>
                      <td className="py-2 text-right">
                        <span className={clsx("inline-flex rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide", quadrantTone(sector.quadrant))}>
                          {sector.quadrant}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded border border-dashed border-bg-border px-3 py-8 text-center text-sm text-text-muted">
              {sectorRotation?.detail || "Waiting for sector quotes to build the watchlist."}
            </div>
          )}
        </div>

        <div className="card p-4 space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div>
              <h2 className="text-sm text-text-secondary">Sector RRG</h2>
              <div className="text-xs text-text-muted">
                Ratio on X-axis, momentum on Y-axis, both centered at 100
              </div>
            </div>
            {!!sectorRotation?.rrg?.quadrant_counts && (
              <div className="text-xs text-text-muted text-right">
                <div>Leading {sectorRotation.rrg.quadrant_counts.leading || 0}</div>
                <div>Improving {sectorRotation.rrg.quadrant_counts.improving || 0}</div>
              </div>
            )}
          </div>

          {rrgPoints.length ? (
            <ResponsiveContainer width="100%" height={320}>
              <ScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                <XAxis
                  type="number"
                  dataKey="rrg_ratio"
                  domain={xDomain}
                  tick={{ fill: "#4a5568", fontSize: 10 }}
                  tickFormatter={(value) => Number(value).toFixed(0)}
                />
                <YAxis
                  type="number"
                  dataKey="rrg_momentum"
                  domain={yDomain}
                  tick={{ fill: "#4a5568", fontSize: 10 }}
                  tickFormatter={(value) => Number(value).toFixed(0)}
                />
                <Tooltip
                  cursor={{ strokeDasharray: "3 3" }}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: "4px" }}
                  formatter={(value: number, name: string, item: any) => [
                    `${Number(value).toFixed(2)}`,
                    name === "rrg_ratio" ? `${item?.payload?.name} ratio` : `${item?.payload?.name} momentum`,
                  ]}
                  labelFormatter={() => "Sector"}
                />
                <ReferenceLine x={100} stroke="#334155" strokeDasharray="3 3" />
                <ReferenceLine y={100} stroke="#334155" strokeDasharray="3 3" />
                {quadrantSeries.map((series) => (
                  <Scatter key={series.quadrant} data={series.data} fill={series.color}>
                    <LabelList dataKey="name" position="top" fontSize={10} fill={series.color} />
                  </Scatter>
                ))}
              </ScatterChart>
            </ResponsiveContainer>
          ) : (
            <div className="rounded border border-dashed border-bg-border px-3 py-8 text-center text-sm text-text-muted">
              {sectorRotation?.detail || "Waiting for enough sector history to position the RRG points."}
            </div>
          )}

          <div className="flex flex-wrap gap-2 text-[11px] text-text-muted">
            {Object.entries(quadrantPalette).map(([quadrant, color]) => (
              <span key={quadrant} className="inline-flex items-center gap-1 rounded-full border border-bg-border px-2 py-1">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
                {quadrant}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
