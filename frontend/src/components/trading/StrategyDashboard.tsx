"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getStrategyAgentStatus,
  getStrategyEquityHistory,
  runStrategyAgentOnce,
  getTradingKillSwitchStatus,
  updateTradingKillSwitch,
} from "@/lib/api";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, ReferenceLine,
} from "recharts";
import {
  Activity, AlertTriangle, Play, RefreshCw,
  ShieldAlert, ShieldCheck,
} from "lucide-react";
import { clsx } from "clsx";

// ── Types ─────────────────────────────────────────────────────────────────────

type StrategyPosition = {
  symbol: string;
  underlying: string;
  expiry: string;
  strike: number;
  option_type: string;
  instrument_key: string | null;
  qty: number;
  initial_qty: number;
  lot_size: number | null;
  entry_price: number;
  current_price: number;
  peak_price: number;
  entry_bar_time: string;
  entered_at: string;
  signal_reason: string;
  signal_strength: number | null;
  latest_rsi: number | null;
  phase: string;
  trailing_stop: number | null;
  entry_iv_pct: number | null;
  spot_setup: string | null;
  window_end: string | null;
  price_updated_at: string | null;
  unrealized_pnl: number | null;
  return_pct: number | null;
};

type TradeRecord = {
  symbol: string;
  action: string;
  qty: number;
  entry_price: number | null;
  exit_price: number | null;
  pnl: number | null;
  entry_time: string | null;
  exit_time: string | null;
  expiry: string | null;
  strike: number | null;
  option_type: string | null;
};

type StrategySummary = {
  initial_capital: number;
  available_capital: number;
  total_equity: number;
  unrealized_pnl: number | null;
  realized_pnl: number | null;
  day_pnl: number | null;
  total_trades: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  max_drawdown: number | null;
  sharpe_ratio: number | null;
  open_positions: number;
  entries: number;
  exits: number;
};

type Strategy = {
  key: string;
  label: string;
  summary: StrategySummary;
  positions: StrategyPosition[];
  recent_events: any[];
  trade_history: TradeRecord[];
  last_scan_at: string | null;
  last_message: string | null;
};

type AgentStatus = {
  running: boolean;
  auto_run_enabled: boolean;
  kill_switch_active: boolean;
  scan_interval_seconds: number;
  last_run_at: string | null;
  last_error: string | null;
  last_message: string | null;
  target_expiry: string | null;
  candidate_expiries: string[];
  active_windows: number;
  regime_summary: Record<string, string>;
  commentary: Array<{ time: string; scope: string; tone: string; message: string }>;
  strategies: Strategy[];
};

// ── Formatters ────────────────────────────────────────────────────────────────

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
function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "--";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch { return iso; }
}
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "--";
  try {
    return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return iso; }
}
function daysTo(dateStr: string | null): number | null {
  if (!dateStr) return null;
  const diff = new Date(dateStr).getTime() - Date.now();
  return Math.ceil(diff / 86_400_000);
}
function pnlColor(v: number | null | undefined): string {
  if (v == null) return "text-text-muted";
  return v >= 0 ? "text-accent-green" : "text-accent-red";
}

// ── Position Card (mobile-friendly) ──────────────────────────────────────────

function PositionCard({ pos }: { pos: StrategyPosition }) {
  const lots = pos.lot_size ? Math.round(pos.qty / pos.lot_size) : null;
  const pnl = pos.unrealized_pnl ?? 0;
  const ret = pos.return_pct ?? 0;
  const tte = daysTo(pos.window_end);
  const move = pos.current_price - pos.entry_price;

  return (
    <div className={clsx(
      "rounded-lg border p-3 space-y-2",
      pnl >= 0 ? "border-accent-green/20 bg-accent-green/5" : "border-accent-red/20 bg-accent-red/5"
    )}>
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5">
            <span className="font-bold text-sm text-text-primary">{pos.underlying}</span>
            <span className={clsx(
              "text-[10px] font-bold px-1.5 py-0.5 rounded",
              pos.option_type === "CE"
                ? "bg-accent-green/20 text-accent-green"
                : "bg-accent-red/20 text-accent-red"
            )}>
              {pos.option_type}
            </span>
            <span className="text-xs text-text-muted font-mono">{pos.strike}</span>
          </div>
          <div className="text-[10px] text-text-muted mt-0.5">
            {pos.expiry?.slice(5)} · {lots != null ? `${lots} lot${lots !== 1 ? "s" : ""} (${pos.qty})` : `${pos.qty} qty`}
          </div>
        </div>
        <div className="text-right">
          <div className={clsx("font-mono font-bold text-base leading-tight", pnlColor(pnl))}>
            {fmtSigned(pnl, 0)}
          </div>
          <div className={clsx("text-xs font-mono font-semibold", pnlColor(ret))}>
            {fmtSigned(ret, 1)}%
          </div>
        </div>
      </div>

      {/* Price row */}
      <div className="grid grid-cols-4 gap-1 text-[11px]">
        <div>
          <div className="text-text-muted">Entry</div>
          <div className="font-mono text-text-secondary">{fmt(pos.entry_price)}</div>
          <div className="text-[9px] text-text-muted">{fmtTime(pos.entered_at)}</div>
        </div>
        <div>
          <div className="text-text-muted">LTP</div>
          <div className={clsx("font-mono font-semibold", pnlColor(move))}>
            {fmt(pos.current_price)}
          </div>
          <div className="text-[9px] text-text-muted">{fmtTime(pos.price_updated_at)}</div>
        </div>
        <div>
          <div className="text-text-muted">Stop</div>
          <div className="font-mono text-text-muted">
            {pos.trailing_stop ? fmt(pos.trailing_stop) : "--"}
          </div>
        </div>
        <div>
          <div className="text-text-muted">RSI</div>
          <div className={clsx(
            "font-mono",
            pos.latest_rsi != null && pos.latest_rsi > 70 ? "text-accent-red" :
            pos.latest_rsi != null && pos.latest_rsi < 30 ? "text-accent-green" : "text-text-muted"
          )}>
            {pos.latest_rsi != null ? fmt(pos.latest_rsi, 1) : "--"}
          </div>
        </div>
      </div>

      {/* Footer tags */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className={clsx(
          "text-[10px] px-1.5 py-0.5 rounded font-semibold",
          pos.phase === "phase1" ? "bg-accent-blue/15 text-accent-blue" :
          pos.phase === "phase2" ? "bg-accent-green/15 text-accent-green" :
          "bg-accent-amber/15 text-accent-amber"
        )}>
          {pos.phase}
        </span>
        {pos.spot_setup && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-secondary text-text-muted font-semibold">
            {pos.spot_setup}
          </span>
        )}
        {tte != null && (
          <span className={clsx(
            "text-[10px] px-1.5 py-0.5 rounded font-semibold",
            tte <= 2 ? "bg-accent-red/15 text-accent-red" :
            tte <= 5 ? "bg-accent-amber/15 text-accent-amber" :
            "bg-bg-secondary text-text-muted"
          )}>
            {tte}d left
          </span>
        )}
        <span className="text-[10px] text-text-muted ml-auto">{fmtTime(pos.entered_at)}</span>
      </div>
    </div>
  );
}

// ── Positions Panel ───────────────────────────────────────────────────────────

function PositionsPanel({ positions, summary }: { positions: StrategyPosition[]; summary?: StrategySummary }) {
  const totalUnrealized = positions.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);
  const totalInvested = positions.reduce((s, p) => s + p.entry_price * p.qty, 0);

  if (!positions.length) {
    return (
      <div className="rounded-lg border border-dashed border-bg-border p-8 text-center text-sm text-text-muted">
        <Activity size={24} className="mx-auto mb-2 opacity-40" />
        No open positions. Agent is scanning for next signal.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Aggregate bar */}
      <div className="flex items-center gap-4 rounded-lg border border-bg-border bg-bg-secondary/30 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-xs text-text-muted">Open P&L</span>
          <span className={clsx("font-mono font-bold text-sm", pnlColor(totalUnrealized))}>
            {fmtSigned(totalUnrealized, 0)}
          </span>
        </div>
        <div className="h-4 w-px bg-bg-border" />
        <div className="flex items-center gap-2">
          <span className="text-xs text-text-muted">Invested</span>
          <span className="font-mono text-sm text-text-secondary">{fmtCrore(totalInvested)}</span>
        </div>
        <div className="h-4 w-px bg-bg-border" />
        <div className="flex items-center gap-2">
          <span className="text-xs text-text-muted">Positions</span>
          <span className="font-mono text-sm text-text-primary font-semibold">{positions.length}</span>
        </div>
        <div className="ml-auto text-[10px] text-text-muted">
          prices update every 15s
        </div>
      </div>
      {/* Cards grid */}
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {positions.map((pos) => <PositionCard key={pos.symbol} pos={pos} />)}
      </div>
    </div>
  );
}

// ── Equity Curve ──────────────────────────────────────────────────────────────

function EquityCurve({ strategyKey, initialCapital }: { strategyKey: string; initialCapital: number }) {
  const { data } = useQuery({
    queryKey: ["equityHistory"],
    queryFn: () => getStrategyEquityHistory().then((r) => r.data as Array<{
      key: string; label: string; equity_curve: Array<{ time: string; equity: number }>;
    }>),
    refetchInterval: 30_000,
  });

  const curve = data?.find((d) => d.key === strategyKey)?.equity_curve ?? [];
  if (curve.length < 2) {
    return (
      <div className="h-28 flex items-center justify-center text-xs text-text-muted border border-dashed border-bg-border rounded-lg">
        Equity curve appears after a few scans
      </div>
    );
  }

  const chartData = curve.map((p, i) => ({
    i, equity: p.equity,
    pnl: p.equity - initialCapital,
    time: fmtTs(p.time),
  }));
  const minEq = Math.min(...chartData.map((d) => d.equity));
  const maxEq = Math.max(...chartData.map((d) => d.equity));
  const latest = chartData[chartData.length - 1];
  const isProfit = latest.equity >= initialCapital;

  return (
    <ResponsiveContainer width="100%" height={112}>
      <LineChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2433" vertical={false} />
        <XAxis dataKey="i" hide />
        <YAxis domain={[minEq * 0.999, maxEq * 1.001]} hide />
        <Tooltip
          contentStyle={{ background: "#0d1117", border: "1px solid #1e2433", borderRadius: 6, fontSize: 11 }}
          formatter={(v: number) => [`₹${v.toLocaleString("en-IN")}`, "Equity"]}
          labelFormatter={(_, payload) => payload?.[0]?.payload?.time ?? ""}
        />
        <ReferenceLine y={initialCapital} stroke="#4b5563" strokeDasharray="4 4" />
        <Line type="monotone" dataKey="equity"
          stroke={isProfit ? "#22c55e" : "#ef4444"}
          strokeWidth={1.5} dot={false} activeDot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── Trade History ─────────────────────────────────────────────────────────────

function TradeHistoryTable({ trades }: { trades: TradeRecord[] }) {
  if (!trades.length) {
    return (
      <div className="rounded border border-dashed border-bg-border p-6 text-center text-xs text-text-muted">
        No closed trades yet.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead className="text-text-muted border-b border-bg-border">
          <tr>
            <th className="pb-2 text-left">Contract</th>
            <th className="pb-2 text-right">Qty</th>
            <th className="pb-2 text-right">Entry ₹</th>
            <th className="pb-2 text-left pl-2">Entered</th>
            <th className="pb-2 text-right">Exit ₹</th>
            <th className="pb-2 text-left pl-2">Exited</th>
            <th className="pb-2 text-right">P&L</th>
            <th className="pb-2 text-right">Ret%</th>
            <th className="pb-2 text-center">Result</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const pnl = t.pnl ?? 0;
            const retPct = t.entry_price && t.exit_price
              ? ((t.exit_price - t.entry_price) / t.entry_price) * 100
              : null;
            return (
              <tr key={i} className="border-b border-bg-border/40 hover:bg-bg-secondary/20">
                <td className="py-2">
                  <span className="text-text-secondary">{t.symbol?.split(":")[1] ?? t.symbol}</span>
                  {t.option_type && (
                    <span className={clsx("ml-1 font-bold", t.option_type === "CE" ? "text-accent-green" : "text-accent-red")}>
                      {t.option_type}
                    </span>
                  )}
                  {t.strike && <span className="ml-1 text-text-muted">{t.strike}</span>}
                </td>
                <td className="py-2 text-right text-text-primary">{t.qty}</td>
                <td className="py-2 text-right text-text-primary font-semibold">{fmt(t.entry_price)}</td>
                <td className="py-2 pl-2 text-text-muted text-[10px]">{fmtTs(t.entry_time)}</td>
                <td className="py-2 text-right text-text-primary font-semibold">{fmt(t.exit_price)}</td>
                <td className="py-2 pl-2 text-text-muted text-[10px]">{fmtTs(t.exit_time)}</td>
                <td className={clsx("py-2 text-right font-semibold", pnlColor(pnl))}>
                  {fmtSigned(pnl, 0)}
                </td>
                <td className={clsx("py-2 text-right", pnlColor(retPct))}>
                  {retPct != null ? fmtSigned(retPct, 1) + "%" : "--"}
                </td>
                <td className="py-2 text-center">
                  <span className={clsx(
                    "px-1.5 py-0.5 rounded text-[10px] font-bold",
                    pnl > 0 ? "bg-accent-green/15 text-accent-green" : "bg-accent-red/15 text-accent-red"
                  )}>
                    {pnl > 0 ? "WIN" : "LOSS"}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Commentary Feed ───────────────────────────────────────────────────────────

function CommentaryFeed({ items }: { items: AgentStatus["commentary"] }) {
  const toneStyle: Record<string, string> = {
    trade: "border-l-accent-blue bg-accent-blue/5",
    success: "border-l-accent-green bg-accent-green/5",
    warning: "border-l-accent-amber bg-accent-amber/5",
    error: "border-l-accent-red bg-accent-red/5",
    idle: "border-l-bg-border bg-bg-secondary/20",
    info: "border-l-text-muted bg-bg-secondary/10",
  };
  const toneLabel: Record<string, string> = {
    trade: "bg-accent-blue/20 text-accent-blue",
    success: "bg-accent-green/20 text-accent-green",
    warning: "bg-accent-amber/20 text-accent-amber",
    error: "bg-accent-red/20 text-accent-red",
  };
  if (!items.length) {
    return (
      <div className="rounded border border-dashed border-bg-border p-5 text-xs text-text-muted text-center">
        No commentary yet.
      </div>
    );
  }
  return (
    <div className="space-y-1.5 max-h-80 overflow-y-auto pr-1">
      {items.map((item, i) => (
        <div
          key={i}
          className={clsx(
            "rounded border-l-2 px-3 py-2 text-xs",
            toneStyle[item.tone] ?? toneStyle.idle
          )}
        >
          <div className="flex items-center justify-between gap-2 mb-0.5">
            <span className={clsx("px-1.5 py-0.5 rounded text-[10px] font-semibold", toneLabel[item.tone] ?? "bg-bg-secondary text-text-muted")}>
              {item.scope}
            </span>
            <span className="text-[10px] text-text-muted">{fmtTs(item.time)}</span>
          </div>
          <div className="text-text-secondary leading-relaxed">{item.message}</div>
        </div>
      ))}
    </div>
  );
}

// ── Regime Map ────────────────────────────────────────────────────────────────

function RegimeMap({ regimes }: { regimes: Record<string, string> }) {
  const colors: Record<string, string> = {
    bullish: "bg-accent-green/15 text-accent-green border-accent-green/20",
    bearish: "bg-accent-red/15 text-accent-red border-accent-red/20",
    dead_zone: "bg-text-muted/10 text-text-muted border-bg-border",
    iv_spike: "bg-accent-amber/15 text-accent-amber border-accent-amber/20",
    neutral: "bg-accent-blue/15 text-accent-blue border-accent-blue/20",
  };
  const counts: Record<string, number> = {};
  Object.values(regimes).forEach((r) => { counts[r] = (counts[r] ?? 0) + 1; });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([regime, count]) => (
          <div key={regime} className={clsx(
            "flex items-center gap-2 px-3 py-1.5 rounded border text-sm font-semibold",
            colors[regime] ?? "bg-bg-secondary text-text-muted border-bg-border"
          )}>
            <span>{regime.replace("_", " ")}</span>
            <span className="font-mono text-xs opacity-70">{count}</span>
          </div>
        ))}
      </div>
      <div className="grid gap-1">
        {Object.entries(regimes).map(([underlying, regime]) => (
          <div key={underlying} className="flex items-center justify-between text-xs px-2 py-1.5 rounded bg-bg-secondary/30">
            <span className="font-semibold text-text-secondary font-mono">{underlying}</span>
            <span className={clsx(
              "px-2 py-0.5 rounded text-[10px] font-bold",
              colors[regime]?.split(" ")[0] + " " + (colors[regime]?.split(" ")[1] ?? "text-text-muted")
            )}>
              {regime}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Stats Grid ────────────────────────────────────────────────────────────────

function StatsGrid({ summary, equityChange, equityChangePct, capitalUsed, capitalPct }: {
  summary: StrategySummary;
  equityChange: number;
  equityChangePct: number;
  capitalUsed: number;
  capitalPct: number;
}) {
  const stats = [
    {
      label: "Total Equity",
      value: fmtCrore(summary.total_equity),
      sub: fmtSigned(equityChangePct, 2) + "% all-time",
      color: pnlColor(equityChange),
    },
    {
      label: "Open P&L",
      value: fmtSigned(summary.unrealized_pnl),
      sub: "unrealized",
      color: pnlColor(summary.unrealized_pnl),
    },
    {
      label: "Realized P&L",
      value: fmtSigned(summary.realized_pnl),
      sub: `${summary.total_trades} closed trades`,
      color: pnlColor(summary.realized_pnl),
    },
    {
      label: "Capital Used",
      value: fmtCrore(capitalUsed),
      sub: capitalPct.toFixed(1) + "% deployed",
      color: "text-text-primary",
    },
    {
      label: "Win Rate",
      value: summary.win_rate != null ? (summary.win_rate * 100).toFixed(1) + "%" : "--",
      sub: `W ${summary.entries - summary.exits} / ${summary.total_trades}`,
      color: summary.win_rate != null && summary.win_rate > 0.5 ? "text-accent-green" : "text-text-primary",
    },
    {
      label: "Profit Factor",
      value: fmt(summary.profit_factor),
      sub: `Avg W ${fmtSigned(summary.avg_win)} / L ${fmtSigned(summary.avg_loss)}`,
      color: (summary.profit_factor ?? 0) >= 1.5 ? "text-accent-green" : (summary.profit_factor ?? 0) >= 1 ? "text-accent-amber" : "text-accent-red",
    },
    {
      label: "Max Drawdown",
      value: summary.max_drawdown != null ? (summary.max_drawdown * 100).toFixed(2) + "%" : "--",
      sub: "peak-to-trough",
      color: (summary.max_drawdown ?? 0) > 0.1 ? "text-accent-red" : "text-text-primary",
    },
    {
      label: "Sharpe",
      value: fmt(summary.sharpe_ratio, 3),
      sub: "risk-adjusted return",
      color: "text-text-primary",
    },
  ];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
      {stats.map((s) => (
        <div key={s.label} className="bg-bg-secondary/40 rounded-lg border border-bg-border p-3">
          <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">{s.label}</div>
          <div className={clsx("font-mono font-bold text-sm leading-tight", s.color)}>{s.value}</div>
          {s.sub && <div className="text-[10px] text-text-muted mt-0.5">{s.sub}</div>}
        </div>
      ))}
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export default function StrategyDashboard() {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<"positions" | "trades" | "commentary" | "stats" | "regimes">("positions");

  const { data: agentStatus, isLoading, dataUpdatedAt } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((r) => r.data as AgentStatus),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const { data: ksState } = useQuery({
    queryKey: ["nseKillSwitch"],
    queryFn: () => getTradingKillSwitchStatus().then((r) => r.data),
    refetchInterval: 15_000,
  });

  const runScan = useMutation({
    mutationFn: () => runStrategyAgentOnce(true),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategyAgentStatus"] }),
  });

  const killSwitch = useMutation({
    mutationFn: (active: boolean) => updateTradingKillSwitch(active),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nseKillSwitch"] });
      qc.invalidateQueries({ queryKey: ["strategyAgentStatus"] });
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-text-muted text-sm">
        <RefreshCw className="animate-spin mr-2" size={16} /> Loading strategy data…
      </div>
    );
  }

  const strat = agentStatus?.strategies?.[0];
  const summary = strat?.summary;
  const positions = strat?.positions ?? [];
  const tradeHistory = strat?.trade_history ?? [];
  const commentary = agentStatus?.commentary ?? [];
  const regimes = agentStatus?.regime_summary ?? {};

  const capitalUsed = summary ? summary.initial_capital - (summary.available_capital ?? 0) : 0;
  const capitalPct = summary ? (capitalUsed / summary.initial_capital) * 100 : 0;
  const equityChange = summary ? summary.total_equity - summary.initial_capital : 0;
  const equityChangePct = summary ? (equityChange / summary.initial_capital) * 100 : 0;
  const totalOpenPnl = positions.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);

  const isKsActive = (ksState as any)?.kill_switch_active;

  const tabs = [
    { key: "positions" as const, label: `Positions`, count: positions.length },
    { key: "trades" as const, label: `History`, count: tradeHistory.length },
    { key: "commentary" as const, label: `Log`, count: commentary.length },
    { key: "stats" as const, label: `Stats`, count: null },
    { key: "regimes" as const, label: `Regimes`, count: Object.keys(regimes).length },
  ];

  const lastUpdatedAgo = dataUpdatedAt
    ? Math.round((Date.now() - dataUpdatedAt) / 1000)
    : null;

  return (
    <div className="space-y-3">

      {/* ── Header Bar ── */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <div>
            <h1 className="text-sm font-bold font-mono text-text-primary">
              MACD Zero-Cross Strategy
            </h1>
            <p className="text-[10px] text-text-muted">
              {agentStatus?.active_windows ?? 0} windows · {agentStatus?.target_expiry ?? "--"} · {agentStatus?.scan_interval_seconds ?? 60}s interval
            </p>
          </div>
          <div className={clsx(
            "flex items-center gap-1.5 px-2 py-1 rounded text-[11px] font-semibold",
            agentStatus?.running ? "bg-accent-blue/15 text-accent-blue" : "bg-bg-secondary text-text-muted"
          )}>
            <Activity size={11} className={agentStatus?.running ? "animate-pulse" : ""} />
            {agentStatus?.running ? "Running" : "Idle"}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => runScan.mutate()}
            disabled={runScan.isPending}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-xs border border-accent-blue/40 bg-accent-blue/10 text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50 transition-colors"
          >
            {runScan.isPending ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
            Run Scan
          </button>
          <button
            onClick={() => killSwitch.mutate(!isKsActive)}
            disabled={killSwitch.isPending}
            className={clsx(
              "inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-xs border transition-colors",
              isKsActive
                ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
            )}
          >
            {isKsActive ? <ShieldCheck size={11} /> : <ShieldAlert size={11} />}
            {isKsActive ? "Release Kill Switch" : "Kill Switch"}
          </button>
        </div>
      </div>

      {/* ── Status / Error Banner ── */}
      {agentStatus?.last_error ? (
        <div className="flex items-center gap-2 rounded border border-accent-red/40 bg-accent-red/10 px-3 py-2 text-xs text-accent-red">
          <AlertTriangle size={12} /> {agentStatus.last_error}
        </div>
      ) : agentStatus?.last_message && (
        <div className="rounded border border-bg-border/60 bg-bg-secondary/20 px-3 py-2 text-xs text-text-muted flex items-center justify-between">
          <span><span className="text-text-secondary font-semibold">Last scan: </span>{agentStatus.last_message}</span>
          {lastUpdatedAgo != null && (
            <span className="text-[10px] shrink-0">{lastUpdatedAgo}s ago</span>
          )}
        </div>
      )}

      {/* ── Live P&L Banner (prominent, only when positions open) ── */}
      {positions.length > 0 && (
        <div className={clsx(
          "rounded-lg border px-4 py-3 flex items-center justify-between gap-4",
          totalOpenPnl >= 0
            ? "border-accent-green/30 bg-accent-green/8"
            : "border-accent-red/30 bg-accent-red/8"
        )}>
          <div>
            <div className="text-[10px] text-text-muted uppercase tracking-wider">Live Open P&L</div>
            <div className={clsx("font-mono font-bold text-xl leading-tight", pnlColor(totalOpenPnl))}>
              {fmtSigned(totalOpenPnl, 0)}
            </div>
          </div>
          <div className="flex gap-4 overflow-x-auto">
            {positions.map((pos) => (
              <div key={pos.symbol} className="text-right shrink-0">
                <div className="text-[10px] text-text-muted font-mono">
                  {pos.underlying} {pos.option_type} {pos.strike}
                </div>
                <div className={clsx("font-mono text-sm font-semibold", pnlColor(pos.unrealized_pnl))}>
                  {fmtSigned(pos.unrealized_pnl, 0)}
                  <span className="text-[10px] ml-1 opacity-70">
                    ({fmtSigned(pos.return_pct, 1)}%)
                  </span>
                </div>
                <div className="text-[9px] text-text-muted">
                  LTP {fmt(pos.current_price)}
                  {pos.price_updated_at && <span className="ml-1 opacity-60">@ {fmtTime(pos.price_updated_at)}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Tab Navigation ── */}
      <div className="flex border-b border-bg-border gap-0">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={clsx(
              "px-4 py-2 text-xs font-semibold border-b-2 transition-colors flex items-center gap-1.5",
              activeTab === tab.key
                ? "border-accent-blue text-accent-blue"
                : "border-transparent text-text-muted hover:text-text-secondary"
            )}
          >
            {tab.label}
            {tab.count != null && (
              <span className={clsx(
                "text-[10px] rounded px-1 py-0.5 font-bold",
                activeTab === tab.key ? "bg-accent-blue/20" : "bg-bg-secondary"
              )}>
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ── Tab Content ── */}
      <div className="card p-4">

        {activeTab === "positions" && (
          <PositionsPanel positions={positions} summary={summary} />
        )}

        {activeTab === "trades" && (
          <TradeHistoryTable trades={[...tradeHistory].reverse()} />
        )}

        {activeTab === "commentary" && (
          <CommentaryFeed items={[...commentary].reverse()} />
        )}

        {activeTab === "stats" && summary && (
          <div className="space-y-4">
            <StatsGrid
              summary={summary}
              equityChange={equityChange}
              equityChangePct={equityChangePct}
              capitalUsed={capitalUsed}
              capitalPct={capitalPct}
            />
            {strat && (
              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="text-xs font-semibold text-text-muted uppercase tracking-wider">Equity Curve</div>
                  <div className={clsx("text-xs font-mono font-semibold", pnlColor(equityChange))}>
                    {fmtSigned(equityChange, 0)} ({fmtSigned(equityChangePct, 2)}%)
                  </div>
                </div>
                <EquityCurve strategyKey={strat.key} initialCapital={summary.initial_capital} />
              </div>
            )}
          </div>
        )}

        {activeTab === "regimes" && (
          <RegimeMap regimes={regimes} />
        )}

      </div>
    </div>
  );
}
