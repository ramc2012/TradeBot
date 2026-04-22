"use client";

import { memo, useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  getOrders,
  getRiskStatus,
  getStrategyAgentStatus,
  getStrategyEquityHistory,
  getTradingKillSwitchStatus,
  runStrategyAgentOnce,
  updateTradingKillSwitch,
} from "@/lib/api";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createStrategyDashboardSocket } from "@/lib/websocket";

type StrategyPosition = {
  symbol: string;
  underlying: string;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
  qty: number;
  entry_price: number;
  current_price: number;
  latest_rsi?: number | null;
  phase?: string | null;
  signal_reason: string;
  entered_at?: string | null;
  price_updated_at?: string | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
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
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
};

type StrategySummary = {
  initial_capital: number;
  available_capital: number;
  total_equity: number;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  day_pnl?: number | null;
  total_trades: number;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  sharpe_ratio?: number | null;
  open_positions: number;
  entries: number;
  exits: number;
};

type StrategyLane = {
  key: string;
  label: string;
  agent?: {
    timeframe?: string | null;
    scope?: string | null;
    scan_interval_seconds?: number | null;
    position_cap?: number | null;
  } | null;
  summary: StrategySummary;
  positions: StrategyPosition[];
  trade_history: TradeRecord[];
  last_scan_at?: string | null;
  last_message?: string | null;
};

type AgentStatus = {
  running: boolean;
  loop_active?: boolean;
  auto_run_enabled: boolean;
  kill_switch_active: boolean;
  scan_interval_seconds: number;
  last_run_at?: string | null;
  next_scan_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  target_expiry?: string | null;
  candidate_expiries?: string[];
  active_windows?: number;
  regime_summary?: Record<string, string>;
  commentary?: Array<{ time: string; scope: string; tone: string; message: string }>;
  strategies: StrategyLane[];
};

type KillSwitchState = {
  kill_switch_active: boolean;
  auto_run_enabled?: boolean;
  loop_active?: boolean;
};

type OrderRow = {
  order_id: string;
  symbol: string;
  action: string;
  qty: number;
  price?: number | null;
  status: string;
};

type RiskStatus = {
  trading_allowed?: boolean;
  daily_loss?: number | null;
  max_daily_loss?: number | null;
  open_positions?: number | null;
  max_positions?: number | null;
  sizing_mode?: string | null;
  circuit_breakers?: {
    consecutive_stops?: number | null;
    paused_until?: string | null;
    drawdown_pct?: number | null;
  };
};

function fmt(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function fmtSigned(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function fmtMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function fmtTime(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function pnlTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function statusTone(status?: string | null) {
  if (status === "COMPLETE" || status === "FILLED") {
    return "border-accent-green/25 bg-accent-green/10 text-accent-green";
  }
  if (status === "OPEN" || status === "PENDING") {
    return "border-accent-blue/25 bg-accent-blue/10 text-accent-blue";
  }
  if (status === "CANCELLED" || status === "REJECTED") {
    return "border-accent-red/25 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-active bg-bg-secondary/50 text-text-secondary";
}

const MetricTile = memo(function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/30 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-1.5 font-mono text-base font-semibold text-text-primary", tone)}>{value}</div>
      <div className="mt-1 text-[10px] text-text-muted">{detail}</div>
    </div>
  );
});

function LaneSelector({
  lane,
  selected,
  onSelect,
}: {
  lane: StrategyLane;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        "rounded-xl border px-3 py-3 text-left transition-colors",
        selected
          ? "border-accent-blue/40 bg-accent-blue/10"
          : "border-bg-border bg-bg-secondary/20 hover:border-bg-active hover:bg-bg-secondary/30",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text-primary">{lane.label}</div>
          <div className="mt-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">
            {lane.agent?.timeframe || "--"} · {lane.agent?.scope || "Strategy lane"}
          </div>
        </div>
        <div className={clsx("font-mono text-sm font-semibold", pnlTone(lane.summary.unrealized_pnl))}>
          {fmtSigned(lane.summary.unrealized_pnl, 0)}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-[11px] text-text-secondary">
        <div>
          <div className="text-text-muted">Equity</div>
          <div className="mt-1 font-mono text-text-primary">{fmtMoney(lane.summary.total_equity)}</div>
        </div>
        <div>
          <div className="text-text-muted">Open</div>
          <div className="mt-1 font-mono text-text-primary">{lane.summary.open_positions}</div>
        </div>
        <div>
          <div className="text-text-muted">Trades</div>
          <div className="mt-1 font-mono text-text-primary">{lane.summary.total_trades}</div>
        </div>
      </div>
    </button>
  );
}

const PositionsTable = memo(function PositionsTable({ positions }: { positions: StrategyPosition[] }) {
  if (!positions.length) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
        No open positions in this lane.
      </div>
    );
  }

  return (
    <div className="overflow-auto">
      <table className="w-full min-w-[880px] text-left text-xs">
        <thead className="border-b border-bg-border text-text-muted">
          <tr>
            <th className="py-2 pr-3">Contract</th>
            <th className="py-2 pr-3">Qty</th>
            <th className="py-2 pr-3">Entry</th>
            <th className="py-2 pr-3">LTP</th>
            <th className="py-2 pr-3">Open P&amp;L</th>
            <th className="py-2 pr-3">Ret%</th>
            <th className="py-2 pr-3">Phase</th>
            <th className="py-2 pr-3">Signal</th>
            <th className="py-2">Updated</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={`${position.symbol}-${position.entered_at || position.price_updated_at || "na"}`} className="border-b border-bg-border/40">
              <td className="py-2 pr-3">
                <div className="font-semibold text-text-primary">{position.underlying}</div>
                <div className="text-text-muted">
                  {position.option_type || "--"} {position.strike ?? "--"} · {position.expiry || "--"}
                </div>
              </td>
              <td className="py-2 pr-3 font-mono text-text-primary">{position.qty}</td>
              <td className="py-2 pr-3 font-mono text-text-primary">{fmt(position.entry_price)}</td>
              <td className="py-2 pr-3 font-mono text-text-primary">{fmt(position.current_price)}</td>
              <td className={clsx("py-2 pr-3 font-mono font-semibold", pnlTone(position.unrealized_pnl))}>
                {fmtSigned(position.unrealized_pnl, 0)}
              </td>
              <td className={clsx("py-2 pr-3 font-mono", pnlTone(position.return_pct))}>
                {position.return_pct != null ? `${fmtSigned(position.return_pct, 1)}%` : "--"}
              </td>
              <td className="py-2 pr-3 text-text-secondary">{position.phase || "--"}</td>
              <td className="py-2 pr-3 text-text-secondary">{position.signal_reason}</td>
              <td className="py-2 text-text-secondary">{fmtTime(position.price_updated_at || position.entered_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

const TradeHistoryTable = memo(function TradeHistoryTable({ trades }: { trades: TradeRecord[] }) {
  if (!trades.length) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
        No closed trades in this lane yet.
      </div>
    );
  }

  return (
    <div className="overflow-auto">
      <table className="w-full min-w-[880px] text-left text-xs">
        <thead className="border-b border-bg-border text-text-muted">
          <tr>
            <th className="py-2 pr-3">Contract</th>
            <th className="py-2 pr-3">Qty</th>
            <th className="py-2 pr-3">Entry</th>
            <th className="py-2 pr-3">Exit</th>
            <th className="py-2 pr-3">P&amp;L</th>
            <th className="py-2 pr-3">Ret%</th>
            <th className="py-2">Exited</th>
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((trade, index) => {
            const retPct = trade.entry_price && trade.exit_price
              ? ((trade.exit_price - trade.entry_price) / trade.entry_price) * 100
              : null;
            return (
              <tr key={`${trade.symbol}-${trade.exit_time || trade.entry_time || index}`} className="border-b border-bg-border/40">
                <td className="py-2 pr-3">
                  <div className="font-semibold text-text-primary">{trade.symbol?.split(":")[1] ?? trade.symbol}</div>
                  <div className="text-text-muted">
                    {trade.option_type || "--"} {trade.strike ?? "--"} · {trade.expiry || "--"}
                  </div>
                </td>
                <td className="py-2 pr-3 font-mono text-text-primary">{trade.qty}</td>
                <td className="py-2 pr-3 font-mono text-text-primary">{fmt(trade.entry_price)}</td>
                <td className="py-2 pr-3 font-mono text-text-primary">{fmt(trade.exit_price)}</td>
                <td className={clsx("py-2 pr-3 font-mono font-semibold", pnlTone(trade.pnl))}>
                  {fmtSigned(trade.pnl, 0)}
                </td>
                <td className={clsx("py-2 pr-3 font-mono", pnlTone(retPct))}>
                  {retPct != null ? `${fmtSigned(retPct, 1)}%` : "--"}
                </td>
                <td className="py-2 text-text-secondary">{fmtTime(trade.exit_time)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
});

const CommentaryFeed = memo(function CommentaryFeed({
  items,
}: {
  items: Array<{ time: string; scope: string; tone: string; message: string }>;
}) {
  if (!items.length) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
        No commentary in this runtime yet.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {items.slice().reverse().slice(0, 10).map((item, index) => (
        <div key={`${item.time}-${index}`} className="rounded-xl border border-bg-border bg-bg-secondary/20 px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted">{item.scope}</div>
            <div className="text-[10px] text-text-muted">{fmtTime(item.time)}</div>
          </div>
          <div className="mt-1 text-xs leading-5 text-text-secondary">{item.message}</div>
        </div>
      ))}
    </div>
  );
});

const RegimeTable = memo(function RegimeTable({ regimes }: { regimes: Record<string, string> }) {
  const entries = Object.entries(regimes || {});
  if (!entries.length) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
        No regime states reported yet.
      </div>
    );
  }

  return (
    <div className="overflow-auto">
      <table className="w-full text-left text-xs">
        <thead className="border-b border-bg-border text-text-muted">
          <tr>
            <th className="py-2 pr-3">Underlying</th>
            <th className="py-2">Regime</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([underlying, regime]) => (
            <tr key={underlying} className="border-b border-bg-border/40">
              <td className="py-2 pr-3 font-semibold text-text-primary">{underlying}</td>
              <td className="py-2 text-text-secondary">{regime.replaceAll("_", " ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

const EquityCurve = memo(function EquityCurve({
  curves,
  laneKey,
  initialCapital,
}: {
  curves: Array<{ key: string; label: string; equity_curve: Array<{ time: string; equity: number }> }>;
  laneKey: string;
  initialCapital: number;
}) {
  const curve = curves.find((entry) => entry.key === laneKey)?.equity_curve ?? [];
  if (curve.length < 2) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
        Equity curve will appear after a few scans.
      </div>
    );
  }

  const chartData = curve.map((point, index) => ({
    index,
    equity: point.equity,
    time: fmtTime(point.time),
  }));
  const latest = chartData[chartData.length - 1];
  const isProfit = latest.equity >= initialCapital;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2433" vertical={false} />
        <XAxis dataKey="index" hide />
        <YAxis hide />
        <Tooltip
          contentStyle={{ background: "#0d1117", border: "1px solid #1e2433", borderRadius: 6, fontSize: 11 }}
          formatter={(value: number) => [fmtMoney(value), "Equity"]}
          labelFormatter={(_, payload) => payload?.[0]?.payload?.time ?? ""}
        />
        <ReferenceLine y={initialCapital} stroke="#4b5563" strokeDasharray="4 4" />
        <Line
          type="monotone"
          dataKey="equity"
          stroke={isProfit ? "#22c55e" : "#ef4444"}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
});

export default function StrategyDashboard() {
  const queryClient = useQueryClient();
  const [selectedKey, setSelectedKey] = useState<string>("");
  const [activeTab, setActiveTab] = useState<"positions" | "trades" | "commentary" | "regimes">("positions");

  const dashboardQuery = useLiveSnapshotQuery<{
    agent_status: AgentStatus;
    kill_switch_state: KillSwitchState;
    orders: OrderRow[];
    risk_status: RiskStatus;
    equity_curves: Array<{ key: string; label: string; equity_curve: Array<{ time: string; equity: number }> }>;
  }>({
    queryKey: ["strategyDashboardSnapshot"],
    queryFn: async () => {
      const [agent_status, kill_switch_state, orders, risk_status, equity_curves] = await Promise.all([
        getStrategyAgentStatus().then((response) => response.data as AgentStatus),
        getTradingKillSwitchStatus().then((response) => response.data as KillSwitchState),
        getOrders().then((response) => response.data as OrderRow[]),
        getRiskStatus().then((response) => response.data as RiskStatus),
        getStrategyEquityHistory().then((response) =>
          response.data as Array<{ key: string; label: string; equity_curve: Array<{ time: string; equity: number }> }>,
        ),
      ]);
      return {
        agent_status,
        kill_switch_state,
        orders,
        risk_status,
        equity_curves,
      };
    },
    streamFactory: (onData, onStatusChange) =>
      createStrategyDashboardSocket(
        (data) =>
          onData(data as {
            agent_status: AgentStatus;
            kill_switch_state: KillSwitchState;
            orders: OrderRow[];
            risk_status: RiskStatus;
            equity_curves: Array<{ key: string; label: string; equity_curve: Array<{ time: string; equity: number }> }>;
          }),
        onStatusChange,
      ),
    staleTime: 10_000,
  });

  const agentStatus = dashboardQuery.data?.agent_status;
  const killSwitchState = dashboardQuery.data?.kill_switch_state;
  const orders = dashboardQuery.data?.orders;
  const riskStatus = dashboardQuery.data?.risk_status;
  const equityCurves = dashboardQuery.data?.equity_curves;
  const isLoading = dashboardQuery.isLoading;
  const dataUpdatedAt = dashboardQuery.dataUpdatedAt;

  const runScan = useMutation({
    mutationFn: () => runStrategyAgentOnce(true),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategyAgentStatus"] });
      queryClient.invalidateQueries({ queryKey: ["strategyEquityHistory"] });
    },
  });
  const toggleKillSwitch = useMutation({
    mutationFn: (active: boolean) => updateTradingKillSwitch(active),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["nseKillSwitch"] });
      queryClient.invalidateQueries({ queryKey: ["strategyAgentStatus"] });
    },
  });

  const strategies = agentStatus?.strategies || [];

  useEffect(() => {
    if (!strategies.length) {
      setSelectedKey("");
      return;
    }
    if (!selectedKey || !strategies.some((strategy) => strategy.key === selectedKey)) {
      setSelectedKey(strategies[0].key);
    }
  }, [selectedKey, strategies]);

  const selectedLane = strategies.find((strategy) => strategy.key === selectedKey) || strategies[0];
  const deskSummary = useMemo(() => {
    return strategies.reduce(
      (accumulator, strategy) => {
        accumulator.equity += strategy.summary.total_equity || 0;
        accumulator.realized += strategy.summary.realized_pnl || 0;
        accumulator.open += strategy.summary.unrealized_pnl || 0;
        accumulator.openPositions += strategy.summary.open_positions || 0;
        accumulator.trades += strategy.summary.total_trades || 0;
        return accumulator;
      },
      { equity: 0, realized: 0, open: 0, openPositions: 0, trades: 0 },
    );
  }, [strategies]);
  const latestUpdateAgo = dataUpdatedAt ? Math.round((Date.now() - dataUpdatedAt) / 1000) : null;
  const laneOrderCount = orders?.length || 0;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-sm text-text-muted">
        <RefreshCw size={16} className="mr-2 animate-spin" />
        Loading NSE strategy desk.
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1760px] space-y-4 pb-8">
      <section className="rounded-[24px] border border-bg-active/60 bg-bg-secondary/28 px-4 py-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-[280px]">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Activity size={16} className="text-accent-blue" />
              NSE Strategy Desk
            </div>
            <div className="mt-1 text-xs text-text-muted">Multi-lane execution for Strategy 1 and Strategy 2.</div>
            <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-text-muted">
              <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
                {agentStatus?.loop_active ? "Loop active" : agentStatus?.auto_run_enabled ? "Waiting for next cycle" : "Manual mode"}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
                Scan every {agentStatus?.scan_interval_seconds || 60}s
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
                Windows {agentStatus?.active_windows || 0}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
                Expiry {agentStatus?.target_expiry || "--"}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => runScan.mutate()}
              disabled={runScan.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-3 py-1.5 text-xs font-semibold text-accent-blue transition-colors hover:bg-accent-blue/20 disabled:opacity-50"
            >
              {runScan.isPending ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
              Run Scan
            </button>
            <button
              type="button"
              onClick={() => toggleKillSwitch.mutate(!killSwitchState?.kill_switch_active)}
              disabled={toggleKillSwitch.isPending}
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors",
                killSwitchState?.kill_switch_active
                  ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                  : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
              )}
            >
              {killSwitchState?.kill_switch_active ? <ShieldCheck size={11} /> : <ShieldAlert size={11} />}
              {killSwitchState?.kill_switch_active ? "Release Kill Switch" : "Kill Switch"}
            </button>
          </div>
        </div>
      </section>

      {agentStatus?.last_error ? (
        <div className="flex items-center gap-2 rounded-xl border border-accent-red/30 bg-accent-red/10 px-3 py-2 text-xs text-accent-red">
          <AlertTriangle size={12} />
          {agentStatus.last_error}
        </div>
      ) : agentStatus?.last_message ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-secondary/20 px-3 py-2 text-xs text-text-muted">
          <span>{agentStatus.last_message}</span>
          <span>{latestUpdateAgo != null ? `${latestUpdateAgo}s ago` : "--"}</span>
        </div>
      ) : null}

      <section className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Desk Equity" value={fmtMoney(deskSummary.equity)} detail="Combined NSE strategy capital" tone={pnlTone(deskSummary.open + deskSummary.realized)} />
        <MetricTile label="Realized" value={fmtMoney(deskSummary.realized)} detail={`${deskSummary.trades} closed trades`} tone={pnlTone(deskSummary.realized)} />
        <MetricTile label="Open P&L" value={fmtMoney(deskSummary.open)} detail={`${deskSummary.openPositions} open positions`} tone={pnlTone(deskSummary.open)} />
        <MetricTile label="Open Orders" value={String(laneOrderCount)} detail="Live paper order blotter" />
        <MetricTile label="Risk Mode" value={String(riskStatus?.sizing_mode || "--")} detail={`Loss ${fmtMoney(riskStatus?.daily_loss)} / ${fmtMoney(riskStatus?.max_daily_loss)}`} />
        <MetricTile
          label="Trading State"
          value={riskStatus?.trading_allowed ? "allowed" : "blocked"}
          detail={`${riskStatus?.open_positions || 0} / ${riskStatus?.max_positions || 0} positions`}
          tone={riskStatus?.trading_allowed ? "text-accent-green" : "text-accent-red"}
        />
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
        {strategies.map((lane) => (
          <LaneSelector
            key={lane.key}
            lane={lane}
            selected={lane.key === selectedLane?.key}
            onSelect={() => setSelectedKey(lane.key)}
          />
        ))}
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.35fr,0.65fr]">
        <div className="rounded-[22px] border border-bg-border bg-bg-secondary/22 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-text-primary">{selectedLane?.label || "Strategy lane"}</div>
              <div className="mt-1 text-xs text-text-muted">
                {selectedLane?.agent?.timeframe || "--"} · {selectedLane?.agent?.scope || "Lane scope"} · Last scan {fmtTime(selectedLane?.last_scan_at)}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {(["positions", "trades", "commentary", "regimes"] as const).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setActiveTab(tab)}
                  className={clsx(
                    "rounded-full border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] transition-colors",
                    activeTab === tab
                      ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                      : "border-bg-border bg-bg-primary/30 text-text-secondary hover:border-bg-active hover:text-text-primary",
                  )}
                >
                  {tab}
                </button>
              ))}
            </div>
          </div>

          <div className="mt-4">
            {activeTab === "positions" ? <PositionsTable positions={selectedLane?.positions || []} /> : null}
            {activeTab === "trades" ? <TradeHistoryTable trades={selectedLane?.trade_history || []} /> : null}
            {activeTab === "commentary" ? <CommentaryFeed items={agentStatus?.commentary || []} /> : null}
            {activeTab === "regimes" ? <RegimeTable regimes={agentStatus?.regime_summary || {}} /> : null}
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/22 p-4">
            <div className="text-sm font-semibold text-text-primary">Selected Lane Summary</div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile label="Equity" value={fmtMoney(selectedLane?.summary.total_equity)} detail="Current lane equity" tone={pnlTone((selectedLane?.summary.realized_pnl || 0) + (selectedLane?.summary.unrealized_pnl || 0))} />
              <MetricTile label="Win Rate" value={selectedLane?.summary.win_rate != null ? `${(selectedLane.summary.win_rate * 100).toFixed(1)}%` : "--"} detail={`Profit factor ${fmt(selectedLane?.summary.profit_factor)}`} />
              <MetricTile label="Realized" value={fmtMoney(selectedLane?.summary.realized_pnl)} detail={`Avg win ${fmtMoney(selectedLane?.summary.avg_win)}`} tone={pnlTone(selectedLane?.summary.realized_pnl)} />
              <MetricTile label="Open P&L" value={fmtMoney(selectedLane?.summary.unrealized_pnl)} detail={`Drawdown ${selectedLane?.summary.max_drawdown != null ? `${selectedLane.summary.max_drawdown.toFixed(2)}%` : "--"}`} tone={pnlTone(selectedLane?.summary.unrealized_pnl)} />
            </div>
            {selectedLane ? (
              <div className="mt-4">
                <EquityCurve
                  curves={equityCurves || []}
                  laneKey={selectedLane.key}
                  initialCapital={selectedLane.summary.initial_capital}
                />
              </div>
            ) : null}
          </div>

          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/22 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-text-primary">Open Order Blotter</div>
                <div className="mt-1 text-xs text-text-muted">Current paper order queue for the NSE desk.</div>
              </div>
              <div className="text-xs text-text-muted">{laneOrderCount} orders</div>
            </div>
            {orders?.length ? (
              <div className="mt-4 overflow-auto">
                <table className="w-full min-w-[520px] text-left text-xs">
                  <thead className="border-b border-bg-border text-text-muted">
                    <tr>
                      <th className="py-2 pr-3">Order</th>
                      <th className="py-2 pr-3">Action</th>
                      <th className="py-2 pr-3">Qty</th>
                      <th className="py-2 pr-3">Price</th>
                      <th className="py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders.map((order) => (
                      <tr key={order.order_id} className="border-b border-bg-border/40">
                        <td className="py-2 pr-3">
                          <div className="font-semibold text-text-primary">{order.symbol}</div>
                          <div className="text-text-muted">{order.order_id}</div>
                        </td>
                        <td className={clsx("py-2 pr-3 font-semibold", order.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                          {order.action}
                        </td>
                        <td className="py-2 pr-3 font-mono text-text-primary">{order.qty}</td>
                        <td className="py-2 pr-3 font-mono text-text-primary">{order.price != null ? fmt(order.price) : "--"}</td>
                        <td className="py-2">
                          <span className={clsx("inline-flex rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]", statusTone(order.status))}>
                            {order.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="mt-4 flex min-h-[140px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
                No open orders in the blotter.
              </div>
            )}
          </div>

          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/22 p-4">
            <div className="text-sm font-semibold text-text-primary">Risk Controls</div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile
                label="Trading Allowed"
                value={riskStatus?.trading_allowed ? "yes" : "no"}
                detail={`Sizing ${riskStatus?.sizing_mode || "--"}`}
                tone={riskStatus?.trading_allowed ? "text-accent-green" : "text-accent-red"}
              />
              <MetricTile
                label="Kill Switch"
                value={killSwitchState?.kill_switch_active ? "active" : "released"}
                detail={killSwitchState?.auto_run_enabled ? "Auto-run enabled" : "Manual mode"}
                tone={killSwitchState?.kill_switch_active ? "text-accent-red" : "text-accent-green"}
              />
              <MetricTile
                label="Daily Loss"
                value={fmtMoney(riskStatus?.daily_loss)}
                detail={`Limit ${fmtMoney(riskStatus?.max_daily_loss)}`}
                tone={pnlTone(-(riskStatus?.daily_loss || 0))}
              />
              <MetricTile
                label="Circuit Breakers"
                value={String(riskStatus?.circuit_breakers?.consecutive_stops || 0)}
                detail={`Paused until ${riskStatus?.circuit_breakers?.paused_until || "--"}`}
              />
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
