"use client";

import type { ReactNode } from "react";
import { startTransition, useMemo, useState } from "react";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  Bot,
  CandlestickChart,
  Database,
  FileText,
  Radio,
  Shield,
  Waves,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { StrategyAgentStatus } from "@/components/trading/StrategyAgentMonitor";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import {
  getBrokerStatus,
  getStrategyAgentComments,
  getStrategyAgentStatus,
  getStrategyDataStatus,
  getStrategyOpenSignals,
  getStrategyPortfolio,
} from "@/lib/api";
import { createStrategyOverviewSocket } from "@/lib/websocket";

type StrategyTab = "portfolio" | "signals" | "operations";

type StrategyComment = {
  time: string;
  type: string;
  level: string;
  message: string;
};

type StrategySignalRow = {
  strategy: string;
  source: string;
  underlying: string;
  signal_date?: string;
  trade_date?: string;
  as_of?: string;
  direction?: string | null;
  reason?: string | null;
  strength?: string | null;
  status?: string | null;
  freshness?: string | null;
  instruction?: string | null;
  mp_day_type?: string | null;
  spot_price?: number | null;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  spot_source?: string | null;
  spot_session_date?: string | null;
  spot_last_time?: string | null;
  option_last_bar_time?: string | null;
};

type StrategyPortfolioRow = {
  id: string;
  strategyKey: string;
  strategyLabel: string;
  underlying: string;
  contract: string;
  qty: number;
  entryTime?: string | null;
  entryPrice?: number | null;
  lastTime?: string | null;
  lastPrice?: number | null;
  pnl?: number | null;
  returnPct?: number | null;
  status: "open" | "closed";
  statusLabel: string;
  signalReason?: string | null;
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  if (Math.abs(value) >= 10_00_000) return `₹${(value / 10_00_000).toFixed(2)}L`;
  if (Math.abs(value) >= 1_000) return `₹${(value / 1_000).toFixed(1)}K`;
  return `₹${value.toFixed(0)}`;
}

function formatTimestamp(value?: string | null) {
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

function freshnessTone(value?: string | null) {
  if (value === "live") return "ready";
  if (value === "stale") return "warning";
  if (value === "missing") return "error";
  return "idle";
}

function badgeTone(value?: string | null) {
  if (value === "ready" || value === "active" || value === "entry-ready" || value === "open") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (
    value === "trend-aligned"
    || value === "watching"
    || value === "monitoring"
    || value === "warning"
    || value === "stale"
  ) {
    return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  }
  if (
    value === "not-ready"
    || value === "pipeline_missing"
    || value === "error"
    || value === "missing"
  ) {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  if (value === "CE" || value === "bullish") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (value === "PE" || value === "bearish") {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-active bg-bg-secondary/60 text-text-secondary";
}

function prettify(value?: string | null) {
  if (!value) return "--";
  return value.replaceAll("_", " ");
}

function tokenStatusTone(value?: string | null, valid?: boolean) {
  if (valid) return "ready";
  if (value === "missing" || value === "expired_reconnect_required") return "error";
  return "warning";
}

function summarizeOptionHistoryHealth(agentStatus?: StrategyAgentStatus) {
  const health = agentStatus?.data_health?.option_history;
  const failures = Number(health?.failure_count || 0);
  const successes = Number(health?.success_count || 0);
  const latestFailure = Object.entries(health?.brokers || {})
    .map(([broker, brokerState]) => {
      const brokerFailures = Number(brokerState.failure || 0);
      if (!brokerFailures) return null;
      return `${broker.toUpperCase()}: ${brokerState.last_detail || "fetch failed"}`;
    })
    .filter(Boolean)
    .join(" | ");

  return {
    failures,
    successes,
    latestFailure,
  };
}

function StatusBadge({
  label,
  tone,
}: {
  label: string;
  tone?: string | null;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
        badgeTone(tone || label),
      )}
    >
      {label}
    </span>
  );
}

function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/35 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold text-text-primary", tone)}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function PanelHeader({
  icon,
  title,
  detail,
  meta,
}: {
  icon: ReactNode;
  title: string;
  detail: string;
  meta?: string;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          {icon}
          {title}
        </div>
        <div className="mt-1 text-xs text-text-muted">{detail}</div>
      </div>
      {meta ? <div className="text-xs text-text-muted">{meta}</div> : null}
    </div>
  );
}

function TabButton({
  active,
  label,
  detail,
  onClick,
}: {
  active: boolean;
  label: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-2xl border px-4 py-3 text-left transition-colors",
        active
          ? "border-accent-blue/40 bg-accent-blue/10 text-text-primary"
          : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.16em]">{label}</div>
      <div className="mt-1 text-[11px] leading-5 text-text-muted">{detail}</div>
    </button>
  );
}

function normalizeSignalRows(rows: StrategySignalRow[] | undefined) {
  return (rows || []).map((row, index) => ({ ...row, _id: `${row.underlying}-${row.reason || row.status || index}` }));
}

function toEpoch(value?: string | null) {
  if (!value) return 0;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}

function strategyContractLabel(optionType?: string | null, strike?: number | null, expiry?: string | null) {
  return `${optionType || "--"} ${strike != null ? String(strike) : "--"} · ${expiry || "--"}`;
}

function strategyUnderlyingFromSymbol(symbol?: string | null) {
  const parts = String(symbol || "").split(":");
  return parts.length > 1 ? parts[1] : String(symbol || "--");
}

function buildStrategyPortfolioRows(
  strategies: StrategyAgentStatus["strategies"],
  lastRunAt?: string | null,
): StrategyPortfolioRow[] {
  const rows: StrategyPortfolioRow[] = [];

  for (const strategy of strategies || []) {
    for (const position of strategy.positions || []) {
      rows.push({
        id: `${strategy.key}:open:${position.symbol}:${position.entered_at || position.price_updated_at || "na"}`,
        strategyKey: strategy.key,
        strategyLabel: strategy.label,
        underlying: position.underlying,
        contract: strategyContractLabel(position.option_type, position.strike, position.expiry),
        qty: position.qty,
        entryTime: position.entered_at,
        entryPrice: position.entry_price,
        lastTime: position.price_updated_at || lastRunAt || position.entered_at,
        lastPrice: position.current_price,
        pnl: position.unrealized_pnl,
        returnPct: position.return_pct,
        status: "open",
        statusLabel: position.phase ? `open · ${prettify(position.phase)}` : "open",
        signalReason: position.signal_reason,
      });
    }

    for (const trade of strategy.trade_history || []) {
      const grossCost = (trade.entry_price || 0) * Math.max(trade.qty || 0, 1);
      rows.push({
        id: `${strategy.key}:closed:${trade.symbol}:${trade.exit_time || trade.entry_time || "na"}`,
        strategyKey: strategy.key,
        strategyLabel: strategy.label,
        underlying: strategyUnderlyingFromSymbol(trade.symbol),
        contract: strategyContractLabel(trade.option_type, trade.strike, trade.expiry),
        qty: trade.qty,
        entryTime: trade.entry_time,
        entryPrice: trade.entry_price,
        lastTime: trade.exit_time,
        lastPrice: trade.exit_price,
        pnl: trade.pnl,
        returnPct: grossCost > 0 ? (trade.pnl / grossCost) * 100 : null,
        status: "closed",
        statusLabel: "closed",
        signalReason: trade.option_type ? `${trade.option_type} exit` : trade.action,
      });
    }
  }

  rows.sort((left, right) => {
    const rightTime = Math.max(toEpoch(right.lastTime), toEpoch(right.entryTime));
    const leftTime = Math.max(toEpoch(left.lastTime), toEpoch(left.entryTime));
    return rightTime - leftTime;
  });
  return rows;
}

export default function StrategyPage() {
  const [activeTab, setActiveTab] = useState<StrategyTab>("portfolio");

  const strategyOverviewQuery = useLiveSnapshotQuery<{
    agent_status: StrategyAgentStatus;
    open_signals: any;
    comments: StrategyComment[];
    brokers: any[];
    pipeline: any;
    live_portfolio: any;
  }>({
    queryKey: ["strategyOverview"],
    queryFn: async () => {
      const [agent_status, open_signals, comments, brokers, pipeline, live_portfolio] = await Promise.all([
        getStrategyAgentStatus().then((response) => response.data as StrategyAgentStatus),
        getStrategyOpenSignals().then((response) => response.data as any),
        getStrategyAgentComments().then((response) => response.data as StrategyComment[]),
        getBrokerStatus().then((response) => response.data as any[]),
        getStrategyDataStatus().then((response) => response.data as any),
        getStrategyPortfolio().then((response) => response.data as any),
      ]);
      return {
        agent_status,
        open_signals,
        comments,
        brokers,
        pipeline,
        live_portfolio,
      };
    },
    streamFactory: (onData, onStatusChange) =>
      createStrategyOverviewSocket(
        (data) =>
          onData(data as {
            agent_status: StrategyAgentStatus;
            open_signals: any;
            comments: StrategyComment[];
            brokers: any[];
            pipeline: any;
            live_portfolio: any;
          }),
        onStatusChange,
      ),
    staleTime: 10_000,
  });

  const agentStatus = strategyOverviewQuery.data?.agent_status;
  const openSignals = strategyOverviewQuery.data?.open_signals;
  const comments = strategyOverviewQuery.data?.comments;
  const brokers = strategyOverviewQuery.data?.brokers;
  const pipeline = strategyOverviewQuery.data?.pipeline;
  const livePortfolio = strategyOverviewQuery.data?.live_portfolio;

  const strategies = useMemo(() => agentStatus?.strategies || [], [agentStatus?.strategies]);
  const {
    allPositions,
    totalOpenPnl,
    totalRealized,
    totalTrades,
    combinedWinRate,
    strategy1Rows,
    strategy2Rows,
    portfolioRows,
  } = useMemo(() => {
    const nextAllPositions = strategies.flatMap((strategy) =>
      (strategy.positions || []).map((position) => ({
        ...position,
        strategyKey: strategy.key,
        strategyLabel: strategy.label,
      })),
    );
    const nextTotalOpenPnl = strategies.reduce((sum, strategy) => sum + (strategy.summary.unrealized_pnl || 0), 0);
    const nextTotalRealized = strategies.reduce((sum, strategy) => sum + (strategy.summary.realized_pnl || 0), 0);
    const nextTotalTrades = strategies.reduce((sum, strategy) => sum + (strategy.summary.total_trades || 0), 0);
    const weightedWins = strategies.reduce(
      (sum, strategy) => sum + Math.round((strategy.summary.win_rate || 0) * (strategy.summary.total_trades || 0)),
      0,
    );

    return {
      allPositions: nextAllPositions,
      totalOpenPnl: nextTotalOpenPnl,
      totalRealized: nextTotalRealized,
      totalTrades: nextTotalTrades,
      combinedWinRate: nextTotalTrades ? (weightedWins / nextTotalTrades) * 100 : 0,
      strategy1Rows: normalizeSignalRows([
        ...(openSignals?.live_positions || []),
        ...(openSignals?.strategy1_watchlist || []),
      ]),
      strategy2Rows: normalizeSignalRows(openSignals?.strategy2_signals || []),
      portfolioRows: buildStrategyPortfolioRows(strategies, agentStatus?.last_run_at),
    };
  }, [agentStatus?.last_run_at, openSignals?.live_positions, openSignals?.strategy1_watchlist, openSignals?.strategy2_signals, strategies]);
  const brokerRows = useMemo(() => brokers || [], [brokers]);
  const commentRows = useMemo(() => comments || [], [comments]);
  const brokerSnapshot = agentStatus?.data_health?.broker_snapshot;
  const upstoxHealth = brokerSnapshot?.upstox_token_health;
  const fyersHealth = brokerSnapshot?.fyers_token_health;
  const optionHistoryHealth = summarizeOptionHistoryHealth(agentStatus);
  const equityCurve = useMemo(() => livePortfolio?.equity_curve || [], [livePortfolio?.equity_curve]);
  const monthly = useMemo(() => livePortfolio?.monthly || [], [livePortfolio?.monthly]);

  return (
    <div className="mx-auto max-w-[1680px] space-y-6 pb-10">
      <section className="rounded-[28px] border border-bg-active/60 bg-bg-secondary/30 px-5 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-4xl">
            <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
              <Bot size={18} className="text-accent-blue" />
              NSE Options Strategy Desk
            </div>
            <p className="mt-2 text-sm leading-6 text-text-secondary">
              Strategy 1 trades 30-minute ATM option MACD zero-cross setups, while Strategy 2 trades 5-minute index options only when MACD and Market Profile confirmation align. This desk is organized around live option exposure first, then signals, then runtime operations.
            </p>
          </div>
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/35 px-4 py-4">
            <div className="text-sm font-semibold text-text-primary">Runtime Rail</div>
            <div className="mt-4 flex flex-wrap gap-2">
              <StatusBadge label={agentStatus?.running ? "Loop Active" : "Idle"} tone={agentStatus?.running ? "ready" : "idle"} />
              <StatusBadge
                label={agentStatus?.kill_switch_active ? "Kill Switch On" : "Kill Switch Off"}
                tone={agentStatus?.kill_switch_active ? "warning" : "ready"}
              />
              <StatusBadge
                label={agentStatus?.auto_run_enabled ? "Automatic" : "Manual"}
                tone={agentStatus?.auto_run_enabled ? "ready" : "warning"}
              />
              {brokerSnapshot ? (
                <StatusBadge
                  label={brokerSnapshot.broker_ready ? "Broker Ready" : "Broker Blocked"}
                  tone={brokerSnapshot.broker_ready ? "ready" : "error"}
                />
              ) : null}
            </div>
            <div className="mt-4 space-y-2 text-xs text-text-secondary">
              <div>Last scan: <span className="font-mono text-text-primary">{formatTimestamp(agentStatus?.last_run_at)}</span></div>
              <div>Next scan: <span className="font-mono text-text-primary">{formatTimestamp(agentStatus?.next_scan_at)}</span></div>
              <div>Expiry: <span className="font-mono text-text-primary">{agentStatus?.target_expiry || "--"}</span></div>
              <div>Cadence: <span className="font-mono text-text-primary">{agentStatus?.scan_interval_seconds || 60}s</span></div>
            </div>
          </div>
        </div>

        <div className="mt-5 grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile label="Open Positions" value={String(allPositions.length)} />
          <MetricTile label="Strategy 1 Open" value={String(strategies.find((strategy) => strategy.key === "macd_strategy")?.summary.open_positions || 0)} />
          <MetricTile label="Strategy 2 Open" value={String(strategies.find((strategy) => strategy.key === "index_mp_strategy")?.summary.open_positions || 0)} />
          <MetricTile label="Open P&L" value={formatSigned(totalOpenPnl, 0)} tone={pnlTone(totalOpenPnl)} />
          <MetricTile label="Realized" value={formatSigned(totalRealized, 0)} tone={pnlTone(totalRealized)} />
          <MetricTile label="Win Rate" value={totalTrades ? `${combinedWinRate.toFixed(1)}%` : "--"} detail={`${totalTrades} closed trades`} />
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-sm font-semibold text-text-primary">Workspace Tabs</div>
            <div className="mt-1 text-xs text-text-muted">
              Portfolio stays first. Signal lanes stay separate from operations, and performance lives under operations so the live options book is not buried under research panels.
            </div>
          </div>
          <div className="text-xs text-text-muted">
            {activeTab === "portfolio"
              ? `${allPositions.length} live positions`
              : activeTab === "signals"
                ? `${strategy1Rows.length + strategy2Rows.length} signal rows`
                : `${portfolioRows.length} portfolio rows`}
          </div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <TabButton
            active={activeTab === "portfolio"}
            label="Portfolio"
            detail="Combined live positions, strategy summary, and commentary."
            onClick={() => startTransition(() => setActiveTab("portfolio"))}
          />
          <TabButton
            active={activeTab === "signals"}
            label="Signals"
            detail="Strategy 1 monitoring lane and Strategy 2 MP-confirmed option lane."
            onClick={() => startTransition(() => setActiveTab("signals"))}
          />
          <TabButton
            active={activeTab === "operations"}
            label="Operations"
            detail="Performance, trade book, broker links, and live data pipeline."
            onClick={() => startTransition(() => setActiveTab("operations"))}
          />
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "portfolio" && "hidden")}>
        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<CandlestickChart size={16} className="text-accent-green" />}
            title="Live Option Positions"
            detail="Strategy 1 and Strategy 2 option positions are shown in one table so live exposure is visible before commentary or research status."
            meta={`${allPositions.length} positions`}
          />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1540px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Strategy</th>
                  <th className="pb-2 pr-3">Underlying</th>
                  <th className="pb-2 pr-3">Contract</th>
                  <th className="pb-2 pr-3">Phase</th>
                  <th className="pb-2 pr-3">Qty</th>
                  <th className="pb-2 pr-3">Entry</th>
                  <th className="pb-2 pr-3">Last</th>
                  <th className="pb-2 pr-3">Trail / RSI</th>
                  <th className="pb-2 pr-3">Signal</th>
                  <th className="pb-2">Open P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {allPositions.length ? (
                  allPositions.map((position) => (
                    <tr key={`${position.symbol}-${position.entered_at}`} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <StatusBadge
                          label={position.strategyKey === "macd_strategy" ? "Strategy 1" : "Strategy 2"}
                          tone={position.strategyKey === "macd_strategy" ? "ready" : "warning"}
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{position.underlying}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{position.strategyLabel}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-mono text-text-primary">
                          {position.option_type} {position.strike}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">{position.expiry || "--"}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={prettify(position.phase)} tone={position.phase || "idle"} />
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{position.qty}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{formatNumber(position.entry_price)}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{formatNumber(position.current_price)}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>trail {position.trailing_stop != null ? formatNumber(position.trailing_stop) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">RSI {position.latest_rsi != null ? formatNumber(position.latest_rsi, 1) : "--"}</div>
                      </td>
                      <td className="py-3 pr-3 text-text-muted">
                        {prettify(position.signal_reason)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(position.price_updated_at || position.entered_at)}</div>
                      </td>
                      <td className={clsx("py-3 font-mono font-semibold", pnlTone(position.unrealized_pnl))}>
                        {formatSigned(position.unrealized_pnl, 0)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatSigned(position.return_pct, 1, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={10} className="py-10 text-center text-sm text-text-muted">
                      No live option positions are open right now.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<FileText size={16} className="text-accent-blue" />}
            title="Portfolio Ledger"
            detail="Each row shows the contract, entry fill, latest exit or mark, timestamps, and the exact P&L so trade outcomes are visible without reading the commentary feed."
            meta={`${portfolioRows.length} rows`}
          />

          <div className="mt-4 max-h-[360px] overflow-auto">
            <table className="w-full min-w-[1280px] text-left text-xs">
              <thead className="sticky top-0 bg-bg-card">
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="py-2 pr-3">Strategy</th>
                  <th className="py-2 pr-3">Underlying</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Qty</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit / Mark</th>
                  <th className="py-2 pr-3">Signal</th>
                  <th className="py-2">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {portfolioRows.length ? (
                  portfolioRows.map((row) => (
                    <tr key={row.id} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <StatusBadge
                          label={row.strategyKey === "macd_strategy" ? "Strategy 1" : "Strategy 2"}
                          tone={row.strategyKey === "macd_strategy" ? "ready" : "warning"}
                        />
                      </td>
                      <td className="py-3 pr-3 font-medium text-text-primary">{row.underlying}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{row.contract}</td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={row.statusLabel} tone={row.status === "open" ? "ready" : "idle"} />
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{row.qty}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.entryPrice != null ? formatNumber(row.entryPrice) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.entryTime)}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.lastPrice != null ? formatNumber(row.lastPrice) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.lastTime)}</div>
                      </td>
                      <td className="py-3 pr-3 text-text-muted">{prettify(row.signalReason)}</td>
                      <td className={clsx("py-3 font-mono font-semibold", pnlTone(row.pnl))}>
                        {formatSigned(row.pnl, 0)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatSigned(row.returnPct, 1, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={9} className="py-10 text-center text-sm text-text-muted">
                      No portfolio rows are available yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[0.95fr,1.05fr]">
          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<Shield size={16} className="text-accent-blue" />}
              title="Strategy Summary"
              detail="Both runtimes report into one summary block under the portfolio so the page does not lead with infrastructure."
            />

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {strategies.map((strategy) => (
                <div key={strategy.key} className="rounded-2xl border border-bg-border bg-bg-secondary/20 px-4 py-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-text-primary">{strategy.label}</div>
                      <div className="mt-1 text-[11px] text-text-muted">
                        {strategy.summary.open_positions || 0} open · {strategy.summary.total_trades || 0} closed
                      </div>
                    </div>
                    <div className={clsx("font-mono text-sm font-semibold", pnlTone(strategy.summary.unrealized_pnl))}>
                      {formatSigned(strategy.summary.unrealized_pnl, 0)}
                    </div>
                  </div>
                  <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2">
                    <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
                      <div className="text-text-muted">Equity</div>
                      <div className="mt-1 font-mono text-text-primary">{formatCompact(strategy.summary.total_equity)}</div>
                    </div>
                    <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
                      <div className="text-text-muted">Win Rate</div>
                      <div className="mt-1 font-mono text-text-primary">
                        {strategy.summary.win_rate != null ? `${((strategy.summary.win_rate || 0) * 100).toFixed(1)}%` : "--"}
                      </div>
                    </div>
                    <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
                      <div className="text-text-muted">Entries / Exits</div>
                      <div className="mt-1 font-mono text-text-primary">
                        {strategy.summary.entries || 0} / {strategy.summary.exits || 0}
                      </div>
                    </div>
                    <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
                      <div className="text-text-muted">Last Scan</div>
                      <div className="mt-1 font-mono text-text-primary">{formatTimestamp(strategy.last_scan_at)}</div>
                    </div>
                  </div>
                  <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/35 px-3 py-3 text-xs text-text-secondary">
                    {strategy.last_message || "Waiting for the next scan."}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<Activity size={16} className="text-accent-blue" />}
              title="Agent Commentary"
              detail="Scrollable runtime commentary sits below positions so it stays operational instead of becoming the first thing on the page."
              meta={`${commentRows.length} notes`}
            />

            <div className="mt-4 max-h-[520px] space-y-3 overflow-y-auto pr-1">
              {commentRows.length ? (
                commentRows.map((comment, index) => (
                  <div key={`${comment.time}-${index}`} className="rounded-2xl border border-bg-border bg-bg-primary/35 px-3 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <StatusBadge label={comment.type} tone={comment.level} />
                      <div className="text-[11px] text-text-muted">{comment.time}</div>
                    </div>
                    <div className="mt-2 text-sm leading-6 text-text-secondary">{comment.message}</div>
                  </div>
                ))
              ) : (
                <div className="rounded-2xl border border-dashed border-bg-border px-3 py-12 text-center text-xs text-text-muted">
                  No agent commentary yet.
                </div>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "signals" && "hidden")}>
        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<Waves size={16} className="text-accent-amber" />}
            title="Strategy 1 · 30m ATM MACD"
            detail="This lane watches the 30-minute option regime and the live option positions opened from that regime. It stays separate from Strategy 2 so the MP-confirmed index workflow does not obscure the simpler monthly-window book."
            meta={`${strategy1Rows.length} rows`}
          />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1320px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Underlying</th>
                  <th className="pb-2 pr-3">Direction</th>
                  <th className="pb-2 pr-3">Status</th>
                  <th className="pb-2 pr-3">Source</th>
                  <th className="pb-2 pr-3">Reason</th>
                  <th className="pb-2">Instruction</th>
                </tr>
              </thead>
              <tbody>
                {strategy1Rows.length ? (
                  strategy1Rows.map((row) => (
                    <tr key={row._id} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3 font-medium text-text-primary">{row.underlying}</td>
                      <td className="py-3 pr-3">
                        {row.direction ? <StatusBadge label={row.direction} tone={row.direction} /> : <span className="text-text-muted">--</span>}
                      </td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={prettify(row.status)} tone={row.status || row.freshness} />
                      </td>
                      <td className="py-3 pr-3">
                        <div className="text-text-secondary">{prettify(row.source)}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.as_of || "--"}</div>
                      </td>
                      <td className="py-3 pr-3 text-text-secondary">{prettify(row.reason)}</td>
                      <td className="py-3 text-text-muted">{row.instruction || "--"}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="py-10 text-center text-sm text-text-muted">
                      No Strategy 1 live rows are available.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<CandlestickChart size={16} className="text-accent-green" />}
            title="Strategy 2 · 5m Index MACD + MP"
            detail="This lane is the live MP-confirmed options workflow. Market Profile context is surfaced with the option trigger so each index row reads like an actionable trading lane rather than a CSV monitor."
            meta={`${strategy2Rows.length} rows`}
          />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1600px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Index</th>
                  <th className="pb-2 pr-3">Direction</th>
                  <th className="pb-2 pr-3">Status</th>
                  <th className="pb-2 pr-3">MP Context</th>
                  <th className="pb-2 pr-3">Spot / Value Area</th>
                  <th className="pb-2 pr-3">Freshness</th>
                  <th className="pb-2">Instruction</th>
                </tr>
              </thead>
              <tbody>
                {strategy2Rows.length ? (
                  strategy2Rows.map((row) => (
                    <tr key={row._id} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{row.underlying}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.as_of || "--"}</div>
                      </td>
                      <td className="py-3 pr-3">
                        {row.direction ? <StatusBadge label={row.direction} tone={row.direction} /> : <span className="text-text-muted">--</span>}
                      </td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={prettify(row.status)} tone={row.status || row.freshness} />
                      </td>
                      <td className="py-3 pr-3 text-text-secondary">
                        <div>{prettify(row.reason)}</div>
                        {row.mp_day_type ? <div className="mt-1 text-[11px] text-text-muted">{prettify(row.mp_day_type)}</div> : null}
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>spot {row.spot_price != null ? formatNumber(row.spot_price) : "--"}</div>
                        <div className="mt-1">POC {row.poc != null ? formatNumber(row.poc) : "--"} · VA {row.val != null ? formatNumber(row.val) : "--"} / {row.vah != null ? formatNumber(row.vah) : "--"}</div>
                      </td>
                      <td className="py-3 pr-3 text-text-secondary">
                        <StatusBadge label={row.freshness || "unknown"} tone={freshnessTone(row.freshness)} />
                        <div className="mt-2 text-[11px] text-text-muted">
                          {row.spot_source ? `${row.spot_source} spot` : "--"} · option {row.option_last_bar_time || "--"}
                        </div>
                      </td>
                      <td className="py-3 text-text-muted">{row.instruction || "--"}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={7} className="py-10 text-center text-sm text-text-muted">
                      No Strategy 2 signal rows are available.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "operations" && "hidden")}>
        <div className="grid gap-4 xl:grid-cols-[1.1fr,0.9fr]">
          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<BarChart3 size={16} className="text-accent-blue" />}
              title="Live Strategy Performance"
              detail="These metrics come from the live paper runtimes, not the archive research files."
              meta={livePortfolio?.source || "live runtime"}
            />

            <div className="mt-4 grid gap-3 md:grid-cols-3">
              {strategies.map((strategy) => (
                <div key={strategy.key} className="rounded-2xl border border-bg-border bg-bg-secondary/20 px-4 py-4">
                  <div className="text-sm font-semibold text-text-primary">{strategy.label}</div>
                  <div className="mt-3 grid gap-2 text-xs">
                    <div className="flex justify-between gap-3"><span className="text-text-muted">Equity</span><span className="font-mono text-text-primary">{formatCompact(strategy.summary.total_equity)}</span></div>
                    <div className="flex justify-between gap-3"><span className="text-text-muted">Realized</span><span className={clsx("font-mono", pnlTone(strategy.summary.realized_pnl))}>{formatSigned(strategy.summary.realized_pnl, 0)}</span></div>
                    <div className="flex justify-between gap-3"><span className="text-text-muted">Open P&amp;L</span><span className={clsx("font-mono", pnlTone(strategy.summary.unrealized_pnl))}>{formatSigned(strategy.summary.unrealized_pnl, 0)}</span></div>
                    <div className="flex justify-between gap-3"><span className="text-text-muted">Trades</span><span className="font-mono text-text-primary">{strategy.summary.total_trades || 0}</span></div>
                    <div className="flex justify-between gap-3"><span className="text-text-muted">Win Rate</span><span className="font-mono text-text-primary">{strategy.summary.win_rate != null ? `${((strategy.summary.win_rate || 0) * 100).toFixed(1)}%` : "--"}</span></div>
                  </div>
                </div>
              ))}
            </div>

            {equityCurve.length > 1 ? (
              <div className="mt-5">
                <div className="mb-2 text-[11px] uppercase tracking-[0.16em] text-text-muted">Combined Equity Curve</div>
                <ResponsiveContainer width="100%" height={190}>
                  <LineChart data={equityCurve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                    <XAxis dataKey="trade" tick={{ fontSize: 9, fill: "#4a5568" }} />
                    <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                    <Tooltip
                      contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                      formatter={(value: number) => [`${(value / 1e5).toFixed(2)}L`, "Equity"]}
                    />
                    <ReferenceLine y={livePortfolio?.start_capital || 100000} stroke="#4a5568" strokeDasharray="3 3" />
                    <Line type="monotone" dataKey="equity" stroke="#00d4a3" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : null}

            {monthly.length ? (
              <div className="mt-5">
                <div className="mb-2 text-[11px] uppercase tracking-[0.16em] text-text-muted">Monthly Equity Change</div>
                <ResponsiveContainer width="100%" height={160}>
                  <BarChart data={monthly}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                    <XAxis dataKey="month" tick={{ fontSize: 8, fill: "#4a5568" }} />
                    <YAxis tick={{ fontSize: 8, fill: "#4a5568" }} tickFormatter={(value: number) => `${value > 0 ? "+" : ""}${value.toFixed(0)}%`} />
                    <Tooltip
                      contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                      formatter={(value: number) => [`${value > 0 ? "+" : ""}${value.toFixed(1)}%`, "Equity Δ"]}
                    />
                    <ReferenceLine y={0} stroke="#4a5568" />
                    <Bar dataKey="eq_change_pct">
                      {monthly.map((row: any, index: number) => (
                        <Cell key={`${row.month}-${index}`} fill={row.eq_change_pct >= 0 ? "#22c55e" : "#ef4444"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : null}
          </div>

          <div className="space-y-4">
            <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
              <PanelHeader
                icon={<Radio size={16} className="text-accent-green" />}
                title="Broker Links"
                detail="Connection health for the adapters used by the live option desks."
                meta={`${brokerRows.filter((broker) => broker.connected).length} connected`}
              />
              <div className="mt-4 flex flex-wrap gap-2">
                {upstoxHealth ? (
                  <StatusBadge
                    label={`Upstox ${prettify(upstoxHealth.status)}`}
                    tone={tokenStatusTone(upstoxHealth.status, upstoxHealth.valid)}
                  />
                ) : null}
                {fyersHealth ? (
                  <StatusBadge
                    label={`Fyers ${prettify(fyersHealth.status)}`}
                    tone={tokenStatusTone(fyersHealth.status, fyersHealth.valid)}
                  />
                ) : null}
                <StatusBadge
                  label={
                    optionHistoryHealth.failures > 0
                      ? `History warnings ${optionHistoryHealth.failures}`
                      : optionHistoryHealth.successes > 0
                        ? `History healthy ${optionHistoryHealth.successes}`
                        : "History idle"
                  }
                  tone={
                    optionHistoryHealth.failures > 0
                      ? "warning"
                      : optionHistoryHealth.successes > 0
                        ? "ready"
                        : "idle"
                  }
                />
              </div>
              {(upstoxHealth?.message || fyersHealth?.message || optionHistoryHealth.latestFailure) ? (
                <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/40 px-3 py-3 text-xs text-text-secondary">
                  {upstoxHealth?.message ? <div>{upstoxHealth.message}</div> : null}
                  {fyersHealth?.message ? <div className={clsx(upstoxHealth?.message && "mt-2")}>{fyersHealth.message}</div> : null}
                  {optionHistoryHealth.latestFailure ? (
                    <div className={clsx((upstoxHealth?.message || fyersHealth?.message) && "mt-2", "text-accent-amber")}>
                      Latest history failure: {optionHistoryHealth.latestFailure}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <div className="mt-4 space-y-2">
                {brokerRows.map((broker) => (
                  <div key={broker.broker} className="flex items-center gap-2 rounded-2xl border border-bg-border bg-bg-secondary/20 px-3 py-3 text-xs">
                    <StatusBadge label={broker.connected ? "connected" : "offline"} tone={broker.connected ? "ready" : "error"} />
                    <span className="font-semibold uppercase text-text-primary">{broker.broker}</span>
                    <span className="text-text-muted">{broker.connected ? broker.name || broker.user_id || "connected" : "disconnected"}</span>
                    <span className="ml-auto text-[11px] text-text-muted">{broker.connected_at ? formatTimestamp(broker.connected_at) : "--"}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
              <PanelHeader
                icon={<Database size={16} className="text-accent-amber" />}
                title="Live Data Pipeline"
                detail="Live execution inputs and archive references are separated so stale research cannot read like live broker data."
                meta={pipeline?.as_of || "--"}
              />
              <div className="mt-4 space-y-3">
                {[...(pipeline?.live_pipeline || []), ...(pipeline?.strategy2_pipeline || [])].map((item: any) => (
                  <div key={item.name} className="rounded-2xl border border-bg-border bg-bg-secondary/20 px-3 py-3 text-xs">
                    <div className="flex items-center gap-2">
                      <StatusBadge label={item.freshness || item.status} tone={item.status || item.freshness} />
                      <span className="font-semibold text-text-primary">{item.name}</span>
                    </div>
                    <div className="mt-2 text-text-secondary">{item.detail}</div>
                    <div className="mt-1 text-[11px] text-text-muted">{item.rows?.toLocaleString() || 0} rows · {item.last_date}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<FileText size={16} className="text-accent-blue" />}
            title="Portfolio Ledger"
            detail="The same contract-level ledger is kept alongside performance so fills, marks, and P&L can be reconciled against the curve."
            meta={`${portfolioRows.length} rows`}
          />

          <div className="mt-4 max-h-[420px] overflow-auto">
            <table className="w-full min-w-[1240px] text-left text-xs">
              <thead className="sticky top-0 bg-bg-card">
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="py-2 pr-3">Strategy</th>
                  <th className="py-2 pr-3">Underlying</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Qty</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit / Mark</th>
                  <th className="py-2 pr-3">Signal</th>
                  <th className="py-2">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {portfolioRows.length ? (
                  portfolioRows.map((row) => (
                    <tr key={row.id} className="border-b border-bg-border/40">
                      <td className="py-2 pr-3 text-text-muted">{row.strategyLabel}</td>
                      <td className="py-2 pr-3 text-text-primary">{row.underlying}</td>
                      <td className="py-2 pr-3 text-text-secondary">{row.contract}</td>
                      <td className="py-2 pr-3">
                        <StatusBadge label={row.statusLabel} tone={row.status === "open" ? "ready" : "idle"} />
                      </td>
                      <td className="py-2 pr-3 font-mono text-text-primary">{row.qty}</td>
                      <td className="py-2 pr-3 font-mono text-text-secondary">
                        <div>{row.entryPrice != null ? formatNumber(row.entryPrice) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.entryTime)}</div>
                      </td>
                      <td className="py-2 pr-3 font-mono text-text-secondary">
                        <div>{row.lastPrice != null ? formatNumber(row.lastPrice) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.lastTime)}</div>
                      </td>
                      <td className="py-2 pr-3 text-text-muted">{prettify(row.signalReason)}</td>
                      <td className={clsx("py-2 font-semibold", pnlTone(row.pnl))}>
                        {formatSigned(row.pnl, 0)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatSigned(row.returnPct, 1, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={9} className="py-10 text-center text-sm text-text-muted">
                      No portfolio rows are available yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
