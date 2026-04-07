"use client";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity, AlertCircle, ArrowDown, ArrowUp, BarChart2, CheckCircle2,
  Clock, Database, FileText, Loader2, MessageSquare, Minus, Shield,
  Target, TrendingDown, TrendingUp, XCircle, Zap,
} from "lucide-react";
import {
  getStrategyDataStatus,
  getStrategySignals,
  getStrategyAgentComments,
  getStrategyTrades,
  getStrategyPortfolio,
  getStrategyOpenSignals,
  getStrategyAgentStatus,
} from "@/lib/api";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell, CartesianGrid, ReferenceLine,
} from "recharts";

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmt(v: number | null | undefined, d = 2): string {
  if (v == null || !isFinite(v)) return "--";
  return v.toFixed(d);
}
function fmtSigned(v: number | null | undefined, d = 2): string {
  if (v == null || !isFinite(v)) return "--";
  return (v >= 0 ? "+" : "") + v.toFixed(d);
}
function fmtCrore(v: number | null | undefined): string {
  if (v == null) return "--";
  if (Math.abs(v) >= 10_00_000) return "₹" + (v / 10_00_000).toFixed(2) + "L";
  if (Math.abs(v) >= 1_000) return "₹" + (v / 1_000).toFixed(1) + "K";
  return "₹" + v.toFixed(0);
}
function pnlColor(v: number | null | undefined): string {
  if (v == null) return "text-text-muted";
  return v >= 0 ? "text-accent-green" : "text-accent-red";
}

// ── Live Positions Widget ─────────────────────────────────────────────────────

function LivePositionsWidget() {
  const { data: agentStatus, isLoading } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((r) => r.data as any),
    refetchInterval: 15_000,
  });

  if (isLoading) {
    return (
      <div className="card p-4">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-2 flex items-center gap-1.5">
          <Activity size={12} /> Live Paper Trading
        </div>
        <div className="py-4 text-center text-xs text-text-muted">
          <Loader2 size={14} className="inline animate-spin mr-1" /> Loading…
        </div>
      </div>
    );
  }

  const strat = agentStatus?.strategies?.[0];
  const positions: any[] = strat?.positions ?? [];
  const summary = strat?.summary;
  const totalPnl = positions.reduce((s: number, p: any) => s + (p.unrealized_pnl ?? 0), 0);

  return (
    <div className={clsx(
      "card p-4 border",
      positions.length > 0
        ? totalPnl >= 0 ? "border-accent-green/20" : "border-accent-red/20"
        : "border-bg-border"
    )}>
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex items-center gap-1.5">
          <Activity size={12} className={agentStatus?.running ? "text-accent-blue animate-pulse" : "text-text-muted"} />
          Live Paper Trading
        </div>
        {positions.length > 0 && (
          <div className={clsx("font-mono font-bold text-sm", pnlColor(totalPnl))}>
            {fmtSigned(totalPnl, 0)} open P&L
          </div>
        )}
      </div>

      {positions.length === 0 ? (
        <div className="text-xs text-text-muted py-2">
          {agentStatus?.last_message ?? "No open positions"}
        </div>
      ) : (
        <div className="space-y-2">
          {positions.map((pos: any) => (
            <div key={pos.symbol} className={clsx(
              "rounded border px-3 py-2 flex items-center justify-between gap-4",
              (pos.unrealized_pnl ?? 0) >= 0 ? "border-accent-green/20 bg-accent-green/5" : "border-accent-red/20 bg-accent-red/5"
            )}>
              <div>
                <div className="flex items-center gap-1.5">
                  <span className="font-semibold text-text-primary text-xs">{pos.underlying}</span>
                  <span className={clsx("text-[10px] font-bold px-1.5 py-0.5 rounded",
                    pos.option_type === "CE" ? "bg-accent-green/20 text-accent-green" : "bg-accent-red/20 text-accent-red"
                  )}>
                    {pos.option_type} {pos.strike}
                  </span>
                  <span className="text-[10px] text-text-muted">{pos.expiry?.slice(5)}</span>
                </div>
                <div className="text-[10px] text-text-muted mt-0.5 font-mono">
                  Entry {fmt(pos.entry_price)} → LTP {fmt(pos.current_price)}
                </div>
              </div>
              <div className="text-right">
                <div className={clsx("font-mono font-semibold text-sm", pnlColor(pos.unrealized_pnl))}>
                  {fmtSigned(pos.unrealized_pnl, 0)}
                </div>
                <div className={clsx("text-[10px] font-mono", pnlColor(pos.return_pct))}>
                  {fmtSigned(pos.return_pct, 1)}%
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {summary && (
        <div className="mt-3 pt-3 border-t border-bg-border grid grid-cols-4 gap-2 text-[10px] text-text-muted">
          <div>
            <div>Equity</div>
            <div className={clsx("font-mono font-semibold text-xs", pnlColor(summary.total_equity - summary.initial_capital))}>
              {fmtCrore(summary.total_equity)}
            </div>
          </div>
          <div>
            <div>Realized</div>
            <div className={clsx("font-mono font-semibold text-xs", pnlColor(summary.realized_pnl))}>
              {fmtSigned(summary.realized_pnl, 0)}
            </div>
          </div>
          <div>
            <div>Win Rate</div>
            <div className={clsx("font-mono font-semibold text-xs",
              (summary.win_rate ?? 0) >= 0.5 ? "text-accent-green" : "text-text-primary"
            )}>
              {summary.win_rate != null ? (summary.win_rate * 100).toFixed(0) + "%" : "--"}
            </div>
          </div>
          <div>
            <div>Trades</div>
            <div className="font-mono font-semibold text-xs text-text-primary">
              {summary.total_trades}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Stat Card ──────────────────────────────────────────────────────────────────

function Stat({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div className="bg-bg-card border border-bg-border rounded-lg p-3">
      <div className="text-text-muted text-[10px] uppercase tracking-wider">{label}</div>
      <div className={clsx("font-mono text-base font-semibold mt-0.5", color)}>{value}</div>
      {sub && <div className="text-text-muted text-[10px] mt-0.5">{sub}</div>}
    </div>
  );
}

// ── Data Status Panel ──────────────────────────────────────────────────────────

function DataStatusPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-data-status"],
    queryFn: () => getStrategyDataStatus().then(r => r.data),
    refetchInterval: 60_000,
  });

  if (isLoading) return <PanelSkeleton title="Data Status" />;

  const sources: any[] = data || [];
  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 flex items-center gap-1.5">
        <Database size={12} /> Data Pipeline
      </h3>
      <div className="space-y-1.5">
        {sources.map((s: any, i: number) => (
          <div key={i} className="flex items-center gap-2 text-xs font-mono">
            {s.status === "ok" ? (
              <CheckCircle2 size={11} className="text-accent-green shrink-0" />
            ) : s.status === "warning" ? (
              <AlertCircle size={11} className="text-accent-amber shrink-0" />
            ) : (
              <XCircle size={11} className="text-accent-red shrink-0" />
            )}
            <span className="text-text-secondary truncate flex-1">{s.name}</span>
            <span className="text-text-muted text-[10px]">{s.rows > 0 ? `${s.rows.toLocaleString()}` : "—"}</span>
            <span className="text-text-muted text-[10px]">{s.last_date}</span>
          </div>
        ))}
        {!sources.length && <div className="text-xs text-text-muted py-3 text-center">No data status</div>}
      </div>
    </div>
  );
}

// ── Open Signals Panel ─────────────────────────────────────────────────────────

function OpenSignalsPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-open-signals"],
    queryFn: () => getStrategyOpenSignals().then(r => r.data),
    refetchInterval: 30_000,
  });

  if (isLoading) return <PanelSkeleton title="Next Signals" />;

  const signals: any[] = data?.signals || [];
  const skipReason = data?.skip_reason;
  const asOf = data?.as_of || "";

  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 flex items-center gap-1.5">
        <Zap size={12} /> Next Session Signals
        <span className="ml-auto text-text-muted text-[10px] font-normal">{asOf}</span>
      </h3>
      {signals.length === 0 ? (
        <div className="text-center py-4">
          <Shield size={20} className="mx-auto text-text-muted mb-1.5" />
          <div className="text-xs text-text-muted">
            {skipReason ? `No trade — ${skipReason}` : "No actionable signals"}
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {signals.map((s: any, i: number) => (
            <div key={i} className={clsx(
              "rounded-lg p-2.5 border",
              s.direction === "CE" ? "border-accent-green/30 bg-accent-green/5" :
              s.direction === "PE" ? "border-accent-red/30 bg-accent-red/5" :
              "border-bg-border"
            )}>
              <div className="flex items-center gap-2 mb-1.5">
                <span className={clsx(
                  "text-[10px] font-bold px-2 py-0.5 rounded",
                  s.direction === "CE" ? "bg-accent-green/20 text-accent-green" :
                  "bg-accent-red/20 text-accent-red"
                )}>
                  {s.direction === "CE" ? "BUY CE" : "BUY PE"}
                </span>
                <span className={clsx(
                  "text-[10px] px-1.5 py-0.5 rounded",
                  s.strength === "strong" ? "bg-accent-amber/20 text-accent-amber" :
                  "bg-bg-secondary text-text-muted"
                )}>
                  {s.strength}
                </span>
                <span className="text-[10px] text-text-muted ml-auto">{s.reason}</span>
              </div>
              <div className="text-[10px] text-text-muted italic">{s.instruction}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Agent Comments Panel ───────────────────────────────────────────────────────

function AgentCommentsPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-agent-comments"],
    queryFn: () => getStrategyAgentComments().then(r => r.data),
    refetchInterval: 60_000,
  });

  if (isLoading) return <PanelSkeleton title="Commentary" />;

  const comments: any[] = data || [];

  const levelIcon = (level: string) => {
    switch (level) {
      case "bullish": return <TrendingUp size={11} className="text-accent-green" />;
      case "bearish": return <TrendingDown size={11} className="text-accent-red" />;
      case "warning": return <AlertCircle size={11} className="text-accent-amber" />;
      default: return <Activity size={11} className="text-accent-blue" />;
    }
  };

  const levelBorder = (level: string) => {
    switch (level) {
      case "bullish": return "border-l-accent-green";
      case "bearish": return "border-l-accent-red";
      case "warning": return "border-l-accent-amber";
      default: return "border-l-accent-blue";
    }
  };

  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 flex items-center gap-1.5">
        <MessageSquare size={12} /> Agent Commentary
      </h3>
      <div className="space-y-1.5 max-h-[300px] overflow-y-auto pr-1">
        {comments.map((c: any, i: number) => (
          <div key={i} className={clsx(
            "text-xs border-l-2 pl-2.5 py-1",
            levelBorder(c.level)
          )}>
            <div className="flex items-center gap-1.5 mb-0.5">
              {levelIcon(c.level)}
              <span className="text-text-muted text-[10px]">{c.type}</span>
              <span className="text-text-muted text-[10px] ml-auto">{c.time}</span>
            </div>
            <div className="text-text-secondary leading-relaxed">{c.message}</div>
          </div>
        ))}
        {comments.length === 0 && (
          <div className="text-xs text-text-muted text-center py-4">No commentary available</div>
        )}
      </div>
    </div>
  );
}

// ── Signal History ─────────────────────────────────────────────────────────────

function SignalHistoryPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-signals"],
    queryFn: () => getStrategySignals().then(r => r.data),
    refetchInterval: 60_000,
  });

  if (isLoading) return <PanelSkeleton title="Signal History" />;

  const mpSignals: any[] = (data?.mp_signals || []).slice(-20).reverse();

  const dirColor = (dir: string) => {
    switch (dir) {
      case "CE": return "text-accent-green";
      case "PE": return "text-accent-red";
      case "CONFLICT": return "text-accent-amber";
      default: return "text-text-muted";
    }
  };

  const dayTypeColor = (dt: string) => {
    if (dt.includes("TREND_UP")) return "text-accent-green";
    if (dt.includes("TREND_DN")) return "text-accent-red";
    if (dt.includes("DOUBLE_DIST")) return "text-accent-amber";
    if (dt.includes("FAILED")) return "text-accent-purple";
    return "text-text-secondary";
  };

  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 flex items-center gap-1.5">
        <BarChart2 size={12} /> MP Signal History
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-[10px] font-mono">
          <thead>
            <tr className="text-text-muted border-b border-bg-border">
              <th className="text-left py-1 pr-2">Date</th>
              <th className="text-left py-1 pr-2">Day Type</th>
              <th className="text-right py-1 pr-2">Move</th>
              <th className="text-center py-1 pr-2">BF</th>
              <th className="text-center py-1 pr-2">SF</th>
              <th className="text-center py-1">Dir</th>
            </tr>
          </thead>
          <tbody>
            {mpSignals.map((s: any, i: number) => (
              <tr key={i} className="border-b border-bg-border/30 hover:bg-bg-secondary/30">
                <td className="py-1 pr-2 text-text-secondary">{s.date}</td>
                <td className={clsx("py-1 pr-2", dayTypeColor(s.day_type))}>{s.day_type}</td>
                <td className={clsx("py-1 pr-2 text-right",
                  s.daily_move > 0 ? "text-accent-green" : s.daily_move < 0 ? "text-accent-red" : "text-text-muted"
                )}>
                  {s.daily_move > 0 ? "+" : ""}{Math.round(s.daily_move)}
                </td>
                <td className="py-1 pr-2 text-center">{s.buyer_fail}</td>
                <td className="py-1 pr-2 text-center">{s.seller_fail}</td>
                <td className={clsx("py-1 text-center font-semibold", dirColor(s.mp_direction))}>
                  {s.mp_direction}
                </td>
              </tr>
            ))}
            {!mpSignals.length && (
              <tr>
                <td colSpan={6} className="py-6 text-center text-text-muted">No signal history</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Trade Book Panel ───────────────────────────────────────────────────────────

function TradeBookPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-trades"],
    queryFn: () => getStrategyTrades().then(r => r.data),
    refetchInterval: 60_000,
  });

  if (isLoading) return <PanelSkeleton title="Trade Book" />;

  const trades: any[] = data?.trades || [];
  const total = data?.total || 0;

  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 flex items-center gap-1.5">
        <FileText size={12} /> Trade Book
        <span className="ml-auto text-text-muted text-[10px] font-normal">{total} total</span>
      </h3>
      <div className="overflow-x-auto max-h-[350px] overflow-y-auto">
        <table className="w-full text-[10px] font-mono">
          <thead className="sticky top-0 bg-bg-card">
            <tr className="text-text-muted border-b border-bg-border">
              <th className="text-left py-1 pr-2">Type</th>
              <th className="text-left py-1 pr-2">Entry</th>
              <th className="text-right py-1 pr-2">EP</th>
              <th className="text-right py-1 pr-2">XP</th>
              <th className="text-left py-1 pr-2">Reason</th>
              <th className="text-right py-1">Ret%</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t: any, i: number) => (
              <tr key={i} className="border-b border-bg-border/30 hover:bg-bg-secondary/30">
                <td className={clsx("py-1 pr-2 font-semibold",
                  t.option_type === "CE" ? "text-accent-green" : "text-accent-red"
                )}>
                  {t.option_type}
                </td>
                <td className="py-1 pr-2 text-text-secondary">{t.entry_time?.slice(0, 10)}</td>
                <td className="py-1 pr-2 text-right text-text-secondary">{t.entry_price?.toFixed(0)}</td>
                <td className="py-1 pr-2 text-right text-text-secondary">{t.exit_price?.toFixed(0)}</td>
                <td className="py-1 pr-2 text-text-muted">{t.exit_reason?.replace(/_/g, " ")}</td>
                <td className={clsx("py-1 text-right font-semibold",
                  t.blended_return > 0 ? "text-accent-green" : t.blended_return < 0 ? "text-accent-red" : "text-text-muted"
                )}>
                  {t.blended_return > 0 ? "+" : ""}{t.blended_return?.toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Portfolio Stats Panel ──────────────────────────────────────────────────────

function PortfolioPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["strategy-portfolio"],
    queryFn: () => getStrategyPortfolio().then(r => r.data),
    refetchInterval: 120_000,
  });

  if (isLoading) return <PanelSkeleton title="Portfolio" />;
  if (error || !data) return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-2">Backtest Portfolio</h3>
      <div className="text-xs text-text-muted text-center py-6">No portfolio data available</div>
    </div>
  );

  const p = data;
  const equityCurve = p.equity_curve || [];
  const monthly = p.monthly || [];

  return (
    <div className="card p-4 space-y-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex items-center gap-1.5">
        <Target size={12} /> Backtest Portfolio — {p.strategy}
      </h3>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Stat
          label="Final Equity"
          value={`${p.final_equity_lakhs}L`}
          sub={`from 1L (${p.total_return_pct > 0 ? "+" : ""}${p.total_return_pct}%)`}
          color="text-accent-green"
        />
        <Stat
          label="Win Rate"
          value={`${p.win_rate}%`}
          sub={`${p.wins}W / ${p.losses}L of ${p.total_trades}`}
          color={p.win_rate >= 60 ? "text-accent-green" : p.win_rate >= 50 ? "text-accent-amber" : "text-accent-red"}
        />
        <Stat
          label="Avg Return"
          value={`${p.avg_return > 0 ? "+" : ""}${p.avg_return}%`}
          sub={`Median: ${p.median_return > 0 ? "+" : ""}${p.median_return}%`}
          color={p.avg_return > 0 ? "text-accent-green" : "text-accent-red"}
        />
        <Stat
          label="Max Drawdown"
          value={`${p.max_drawdown_pct}%`}
          sub={`${p.catastrophic_trades} catastrophic`}
          color={p.max_drawdown_pct > 30 ? "text-accent-red" : "text-accent-amber"}
        />
      </div>

      {equityCurve.length > 1 && (
        <div>
          <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">Backtest Equity Curve</div>
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={equityCurve}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
              <XAxis dataKey="trade" tick={{ fontSize: 9, fill: "#4a5568" }} />
              <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(v: number) => `${(v / 1e5).toFixed(0)}L`} />
              <Tooltip
                contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                formatter={(v: number) => [`${(v / 1e5).toFixed(2)}L`, "Equity"]}
                labelFormatter={(v: number) => `Trade #${v}`}
              />
              <ReferenceLine y={100000} stroke="#4a5568" strokeDasharray="3 3" />
              <Line type="monotone" dataKey="equity" stroke="#00d4a3" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {monthly.length > 0 && (
        <div>
          <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">Monthly P&L</div>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={monthly}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
              <XAxis dataKey="month" tick={{ fontSize: 8, fill: "#4a5568" }} />
              <YAxis tick={{ fontSize: 8, fill: "#4a5568" }} tickFormatter={(v: number) => `${v > 0 ? "+" : ""}${v.toFixed(0)}%`} />
              <Tooltip
                contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                formatter={(v: number) => [`${v > 0 ? "+" : ""}${v.toFixed(1)}%`, "Equity Δ"]}
              />
              <ReferenceLine y={0} stroke="#4a5568" />
              <Bar dataKey="eq_change_pct">
                {monthly.map((m: any, i: number) => (
                  <Cell key={i} fill={m.eq_change_pct >= 0 ? "#22c55e" : "#ef4444"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ── Panel Skeleton ─────────────────────────────────────────────────────────────

function PanelSkeleton({ title }: { title: string }) {
  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3">{title}</h3>
      <div className="flex items-center justify-center py-6">
        <Loader2 size={14} className="animate-spin text-text-muted" />
        <span className="text-xs text-text-muted ml-2">Loading...</span>
      </div>
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function StrategyPage() {
  return (
    <div className="space-y-4 max-w-[1600px] mx-auto">

      {/* Header */}
      <div>
        <h1 className="text-base font-bold text-text-primary">Strategy Dashboard</h1>
        <p className="text-xs text-text-muted mt-0.5">
          MACD Zero-Cross · Market Profile · NSE F&O
        </p>
      </div>

      {/* Live positions — full width, prominent at top */}
      <LivePositionsWidget />

      {/* Signal + Commentary + Data Status */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <OpenSignalsPanel />
        <AgentCommentsPanel />
        <DataStatusPanel />
      </div>

      {/* Backtest portfolio stats (full width) */}
      <PortfolioPanel />

      {/* Signal history + Trade book */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SignalHistoryPanel />
        <TradeBookPanel />
      </div>

    </div>
  );
}
