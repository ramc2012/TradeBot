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
  getCommodityATMWatchlist,
  getCommodityStrategyStatus,
  getDirectionalOptionsLiveSnapshot,
  getDirectionalOptionsPaperJournal,
  getDirectionalOptionsPaperPositions,
  getDirectionalOptionsSummary,
  getFractalMarketProfileLiveSnapshot,
  getFractalMarketProfilePaperJournal,
  getFractalMarketProfilePaperPositions,
  getFractalMarketProfileSummary,
  getStrategyAgentComments,
  getStrategyAgentStatus,
  getStrategyDataStatus,
  getStrategyOpenSignals,
  getStrategyPortfolio,
} from "@/lib/api";
import { isBrokerReady } from "@/lib/broker-status";
import { createStrategyOverviewSocket } from "@/lib/websocket";

type StrategyTab = "portfolio" | "signals" | "operations";
type StrategyDetailTab = "instruments" | "positions" | "history" | "portfolio" | "performance" | "flow";
type InstrumentCategory = "conditions_met" | "watch" | "avoid";

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
  expiry?: string | null;
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

type StrategyInstrumentCandidate = {
  symbol: string;
  category: InstrumentCategory;
  priorityScore: number;
  statusLabel: string;
  reason: string;
  direction?: string | null;
  source?: string | null;
  historyTrades: number;
  historyPnl: number;
  winRate: number | null;
  lastSeen?: string | null;
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

function computeDTE(expiry?: string | null): number | null {
  if (!expiry) return null;
  const parsed = new Date(expiry);
  if (Number.isNaN(parsed.getTime())) return null;
  return Math.ceil((parsed.getTime() - Date.now()) / 86_400_000);
}

function dteTone(dte?: number | null) {
  if (dte == null) return "text-text-muted";
  if (dte <= 2) return "text-accent-red";
  if (dte <= 7) return "text-accent-amber";
  return "text-text-muted";
}

function formatHeldFor(enteredAt?: string | null): string {
  if (!enteredAt) return "--";
  const parsed = new Date(enteredAt);
  if (Number.isNaN(parsed.getTime())) return "--";
  const diffMs = Date.now() - parsed.getTime();
  if (diffMs < 0) return "--";
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  if (hours < 24) return remMin ? `${hours}h ${remMin}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days}d ${remHours}h` : `${days}d`;
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
        "inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]",
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
  // `detail` is intentionally rendered only as a hover tooltip to keep the
  // tile compact. The full text remains accessible via the wrapper's title.
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2" title={detail || label}>
      <div className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-base font-semibold text-text-primary", tone)}>{value}</div>
    </div>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div title={label}>
      <div className="text-[9px] uppercase tracking-[0.08em] text-text-muted">{label}</div>
      <div className="font-mono text-sm font-semibold text-text-primary">{value}</div>
    </div>
  );
}

function CandidateDistributionStrip({
  met,
  watch,
  avoid,
}: {
  met: number;
  watch: number;
  avoid: number;
}) {
  const total = Math.max(met + watch + avoid, 1);
  const segments = [
    { label: "Met", value: met, className: "bg-accent-green" },
    { label: "Watch", value: watch, className: "bg-accent-amber" },
    { label: "Avoid", value: avoid, className: "bg-accent-red" },
  ];
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/25 px-3 py-2" title="Instrument bucket split: met, watch, avoid">
      <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-[0.08em] text-text-muted">
        <span>Signal Split</span>
        <span className="font-mono">{met}/{watch}/{avoid}</span>
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-bg-primary">
        {segments.map((segment) => (
          <div
            key={segment.label}
            className={segment.className}
            style={{ width: `${(segment.value / total) * 100}%` }}
            title={`${segment.label}: ${segment.value}`}
          />
        ))}
      </div>
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
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary" title={detail}>
          {icon}
          {title}
        </div>
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
  // `detail` survives as a hover-only help tip on the whole tab to keep
  // the tab strip dense and scannable.
  return (
    <button
      type="button"
      onClick={onClick}
      title={detail}
      className={clsx(
        "rounded-lg border px-3 py-2 text-center transition-colors",
        active
          ? "border-accent-blue/40 bg-accent-blue/10 text-text-primary"
          : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.1em]">{label}</div>
    </button>
  );
}

function DetailTabButton({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition-colors",
        active
          ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
          : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <span>{label}</span>
      {count != null ? <span className="font-mono text-[11px] text-text-muted">{count}</span> : null}
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

function signalUnderlying(row: any) {
  const explicit = String(row?.symbol || "").trim();
  if (explicit && explicit !== "--") return explicit;
  const underlying = String(row?.underlying || "--").trim();
  const side = String(row?.direction || row?.option_type || "").trim().toUpperCase();
  if (row?.strategy === "Strategy 1" && underlying !== "--" && (side === "CE" || side === "PE")) {
    return `${underlying} ${side}`;
  }
  return underlying;
}

function tradeUnderlying(row: any) {
  return strategyUnderlyingFromSymbol(row?.symbol || row?.underlying);
}

function categoryLabel(category: InstrumentCategory) {
  if (category === "conditions_met") return "Already Conditions Met";
  if (category === "watch") return "Interesting To Watch";
  return "To Be Avoided";
}

function categoryTone(category: InstrumentCategory) {
  if (category === "conditions_met") return "text-accent-green";
  if (category === "watch") return "text-accent-amber";
  return "text-accent-red";
}

function defaultStrategyUniverse(strategyKey?: string) {
  if (strategyKey === "index_mp_strategy") return ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"];
  if (strategyKey === "directional_long_options") return ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"];
  if (strategyKey === "commodity_futures" || strategyKey === "commodity_options" || strategyKey === "commodity_strategy") {
    return ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"];
  }
  if (strategyKey === "market_profile" || strategyKey === "fractal_market_profile") return ["NIFTY", "SENSEX", "CRUDEOIL"];
  return ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"];
}

function buildEmptySummary(overrides: Record<string, number> = {}) {
  return {
    open_positions: 0,
    total_trades: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    total_equity: 0,
    win_rate: null,
    entries: 0,
    exits: 0,
    ...overrides,
  };
}

function normalizeExternalPosition(row: any, fallbackUnderlying = "--") {
  const underlying = String(row?.underlying || row?.symbol || fallbackUnderlying || "--").replace(/^MCX:/, "");
  return {
    symbol: row?.symbol || row?.trading_symbol || underlying,
    underlying,
    option_type: row?.option_type || row?.instrument_type || row?.direction || "--",
    strike: Number(row?.strike || row?.strike_price || 0) || null,
    expiry: row?.expiry || row?.expiry_date || null,
    qty: Number(row?.qty || row?.quantity || row?.lots || 0),
    entry_price: Number(row?.entry_price || row?.avg_price || row?.premium || 0) || null,
    current_price: Number(row?.current_price || row?.mark_price || row?.latest_premium || row?.ltp || 0) || null,
    unrealized_pnl: Number(row?.unrealized_pnl || row?.pnl || 0),
    return_pct: Number(row?.return_pct || 0) || null,
    phase: row?.phase || row?.status || "paper",
    signal_reason: row?.selection_reason || row?.reason || row?.thesis || null,
    entered_at: row?.entered_at || row?.entry_time || row?.recorded_at || null,
    price_updated_at: row?.price_updated_at || row?.mark_time || row?.recorded_at || null,
  };
}

function normalizeExternalTrade(row: any, fallbackUnderlying = "--") {
  const underlying = String(row?.underlying || row?.symbol || fallbackUnderlying || "--").replace(/^MCX:/, "");
  return {
    symbol: row?.symbol || row?.trading_symbol || underlying,
    underlying,
    option_type: row?.option_type || row?.instrument_type || row?.direction || "--",
    strike: Number(row?.strike || row?.strike_price || 0) || null,
    expiry: row?.expiry || row?.expiry_date || null,
    qty: Number(row?.qty || row?.quantity || row?.lots || 0),
    entry_price: Number(row?.entry_price || row?.premium || 0) || null,
    exit_price: Number(row?.exit_price || row?.mark_price || row?.latest_premium || 0) || null,
    pnl: Number(row?.realized_pnl || row?.pnl || row?.result_R || 0),
    action: row?.selection_reason || row?.reason || row?.lesson || row?.status || "paper record",
    entry_time: row?.entry_time || row?.recorded_at || null,
    exit_time: row?.exit_time || row?.closed_at || row?.recorded_at || null,
  };
}

function buildExternalStrategies(extra: any) {
  const commodityStatus = extra?.commodityStatus || {};
  const directionalSummary = extra?.directionalSummary || {};
  const directionalLive = extra?.directionalLive?.snapshot || {};
  const directionalPositions = extra?.directionalPositions?.open_positions || extra?.directionalPositions?.positions || [];
  const directionalJournal = extra?.directionalJournal?.journal || extra?.directionalJournal?.records || [];
  const fmpSummary = extra?.fmpSummary || {};
  const fmpLive = extra?.fmpLive || {};
  const fmpPositions = extra?.fmpPositions?.open_positions || extra?.fmpPositions?.positions || [];
  const fmpJournal = extra?.fmpJournal?.journal || extra?.fmpJournal?.records || [];

  const commodityAgents = (commodityStatus?.strategy_agents || []).map((agent: any) => ({
    key: agent.key || "commodity_strategy",
    label: agent.label || agent.title || prettify(agent.key || "Commodity Strategy"),
    summary: buildEmptySummary({ open_positions: Number(agent.open_positions || 0) }),
    positions: (agent.positions || []).map((row: any) => normalizeExternalPosition(row, row?.underlying || "CRUDEOIL")),
    trade_history: (agent.trade_history || []).map((row: any) => normalizeExternalTrade(row, row?.underlying || "CRUDEOIL")),
    signals: agent.signals || [],
    recent_events: agent.recent_events || [],
    last_scan_at: agent.last_scan_at || commodityStatus?.last_run_at,
    last_message: agent.last_message || commodityStatus?.last_message || "Waiting for MCX market hours.",
    meta: { mode: agent.mode || commodityStatus?.mode || "market_closed", scope: "MCX futures/options" },
    instrument_universe: ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"],
  }));

  const directionalSignal = directionalLive?.signal
    ? [{
        underlying: directionalLive.underlying || "NIFTY",
        direction: directionalLive.signal.direction,
        status: directionalLive.selected_contract ? "entry-ready" : "monitoring",
        reason: directionalLive.selection_reason,
        instruction: directionalLive.signal.thesis,
        as_of: directionalLive.as_of,
        source: "distributional_optimizer",
      }]
    : [];

  const directional = {
    key: "directional_long_options",
    label: directionalSummary?.label || "Directional Long Options",
    summary: buildEmptySummary({
      open_positions: Number(directionalPositions.length || 0),
      total_trades: Number(directionalJournal.length || 0),
    }),
    positions: directionalPositions.map((row: any) => normalizeExternalPosition(row, directionalLive?.underlying || "NIFTY")),
    trade_history: directionalJournal.map((row: any) => normalizeExternalTrade(row, directionalLive?.underlying || "NIFTY")),
    signals: directionalSignal,
    recent_events: directionalJournal,
    last_scan_at: directionalLive?.as_of || directionalSummary?.automation?.last_success_at,
    last_message: directionalLive?.selection_reason || directionalSummary?.automation?.last_message || "Armed for next session.",
    meta: {
      mode: directionalLive?.data_status?.execution_ready ? "prepared" : directionalSummary?.automation?.last_message || "armed",
      data_status: directionalLive?.data_status,
      scope: "CE/PE distributional optimizer",
    },
    instrument_universe: directionalSummary?.underlyings?.length ? directionalSummary.underlyings : ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"],
  };

  const fmpSnapshot = fmpLive?.snapshot || fmpLive;
  const fmpSignal = fmpSnapshot?.proposal || fmpSnapshot?.signal;
  const fmpSignals = fmpSignal
    ? [{
        underlying: fmpSnapshot?.symbol || fmpSignal?.symbol || "NIFTY",
        direction: fmpSignal?.direction || fmpSignal?.option_type,
        status: fmpSignal?.approved ? "entry-ready" : "monitoring",
        reason: fmpSignal?.reason || fmpSignal?.setup,
        instruction: fmpSignal?.thesis || fmpSignal?.reason,
        as_of: fmpSnapshot?.as_of,
        source: "fractal_market_profile",
      }]
    : [];
  const fmp = {
    key: "fractal_market_profile",
    label: "Fractal Market Profile",
    summary: buildEmptySummary({
      open_positions: Number(fmpSummary?.paper_summary?.open_positions || fmpPositions.length || 0),
      total_trades: Number(fmpSummary?.paper_summary?.closed_positions || fmpJournal.length || 0),
      realized_pnl: Number(fmpSummary?.paper_summary?.realized_pnl || 0),
      unrealized_pnl: Number(fmpSummary?.paper_summary?.unrealized_pnl || 0),
    }),
    positions: fmpPositions.map((row: any) => normalizeExternalPosition(row, "NIFTY")),
    trade_history: fmpJournal.map((row: any) => normalizeExternalTrade(row, "NIFTY")),
    signals: fmpSignals,
    recent_events: fmpJournal,
    last_scan_at: fmpSummary?.automation?.last_success_at || fmpSnapshot?.as_of,
    last_message: fmpSummary?.automation?.last_message || "Armed for next market session.",
    meta: { mode: fmpSummary?.auto_started ? "armed" : "idle", scope: "MP/FMP" },
    instrument_universe: fmpSummary?.supported_symbols || ["NIFTY", "SENSEX", "CRUDEOIL"],
  };

  return [directional, ...commodityAgents, fmp];
}

async function safeApi<T>(request: Promise<{ data: T }>, fallback: T, timeoutMs = 5_000): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | null = null;
  const requestResult = request
    .then((response) => response.data)
    .catch(() => fallback);

  if (timeoutMs <= 0) {
    return requestResult;
  }

  try {
    return await Promise.race([
      requestResult,
      new Promise<T>((resolve) => {
        timeout = setTimeout(() => resolve(fallback), timeoutMs);
      }),
    ]);
  } finally {
    if (timeout) {
      clearTimeout(timeout);
    }
  }
}

function buildStrategyInstrumentCandidates(strategy: any, signalRows: any[]): StrategyInstrumentCandidate[] {
  const candidates = new Map<string, StrategyInstrumentCandidate>();
  const history = new Map<string, { trades: number; wins: number; pnl: number; latest?: string | null }>();

  function ensure(symbol: string): StrategyInstrumentCandidate {
    const normalized = String(symbol || "--").trim() || "--";
    const existing = candidates.get(normalized);
    if (existing) return existing;
    const candidate: StrategyInstrumentCandidate = {
      symbol: normalized,
      category: "avoid",
      priorityScore: 0,
      statusLabel: "no current setup",
      reason: "No fresh qualifying condition is available for this strategy.",
      historyTrades: 0,
      historyPnl: 0,
      winRate: null,
      lastSeen: null,
    };
    candidates.set(normalized, candidate);
    return candidate;
  }

  for (const trade of strategy?.trade_history || []) {
    const symbol = tradeUnderlying(trade);
    if (!symbol || symbol === "--") continue;
    const pnl = Number(trade.pnl || 0);
    const item = history.get(symbol) || { trades: 0, wins: 0, pnl: 0, latest: null };
    item.trades += 1;
    item.wins += pnl > 0 ? 1 : 0;
    item.pnl += pnl;
    item.latest = trade.exit_time || trade.entry_time || item.latest;
    history.set(symbol, item);
  }

  for (const event of strategy?.recent_events || []) {
    if (String(event.event || "").toLowerCase() !== "exit") continue;
    const symbol = signalUnderlying(event);
    if (!symbol || symbol === "--" || history.has(symbol)) continue;
    const pnl = Number(event.pnl || 0);
    history.set(symbol, {
      trades: 1,
      wins: pnl > 0 ? 1 : 0,
      pnl,
      latest: event.time,
    });
  }

  for (const [symbol, item] of Array.from(history.entries())) {
    const candidate = ensure(symbol);
    candidate.historyTrades = item.trades;
    candidate.historyPnl = item.pnl;
    candidate.winRate = item.trades ? item.wins / item.trades : null;
    candidate.lastSeen = item.latest || candidate.lastSeen;
    candidate.priorityScore += item.pnl > 0 ? 30 + Math.min(item.pnl / 1000, 20) : -25;
    if (item.pnl > 0) {
      candidate.category = "watch";
      candidate.statusLabel = "historically favourable";
      candidate.reason = "Past strategy history is positive; wait for the live trigger before entry.";
    } else if (item.trades > 0) {
      candidate.category = "avoid";
      candidate.statusLabel = "negative history";
      candidate.reason = "Historical strategy result is negative until a stronger fresh condition overrides it.";
    }
  }

  for (const position of strategy?.positions || []) {
    const candidate = ensure(position.underlying);
    candidate.category = "conditions_met";
    candidate.statusLabel = "open position";
    candidate.reason = position.signal_reason || "Strategy already has a live/open condition.";
    candidate.direction = position.option_type;
    candidate.source = "position";
    candidate.lastSeen = position.price_updated_at || position.entered_at;
    candidate.priorityScore += 100;
  }

  for (const row of signalRows || []) {
    const symbol = signalUnderlying(row);
    if (!symbol || symbol === "--") continue;
    const candidate = ensure(symbol);
    const status = String(row.status || "").toLowerCase();
    const hasDirection = Boolean(row.direction);
    const learningScore = Number(row.learning_score || 0);
    const learningEntries = Number(row.learning_entries || 0);
    const learningNote = learningEntries
      ? ` Learning score ${formatSigned(learningScore, 1)} from ${learningEntries} stored observation${learningEntries === 1 ? "" : "s"}.`
      : "";
    candidate.direction = row.direction || candidate.direction;
    candidate.source = row.source || candidate.source;
    candidate.lastSeen = row.option_last_bar_time || row.spot_last_time || row.as_of || candidate.lastSeen;
    candidate.priorityScore += learningScore;

    if (row.learning_blocked) {
      candidate.category = "avoid";
      candidate.statusLabel = "learning risk gate";
      candidate.reason = `${row.instruction || row.reason || "Learning score is blocking new entries for this setup."}${learningNote}`;
      candidate.priorityScore -= 80;
    } else if (status.includes("entry-ready") || status.includes("active") || status.includes("open")) {
      candidate.category = "conditions_met";
      candidate.statusLabel = prettify(row.status);
      candidate.reason = `${row.instruction || row.reason || "Entry condition is already satisfied."}${learningNote}`;
      candidate.priorityScore += 90;
    } else if (hasDirection && candidate.historyPnl >= 0) {
      candidate.category = candidate.category === "conditions_met" ? candidate.category : "watch";
      candidate.statusLabel = prettify(row.status || row.strength || "watching");
      candidate.reason = `${row.instruction || row.reason || "Directional context exists, but final entry condition is pending."}${learningNote}`;
      candidate.priorityScore += 45;
    } else if (status.includes("missing") || status.includes("not-ready") || status.includes("stale")) {
      candidate.category = "avoid";
      candidate.statusLabel = prettify(row.status);
      candidate.reason = `${row.instruction || "Input data is missing or stale."}${learningNote}`;
      candidate.priorityScore -= 40;
    }
  }

  for (const symbol of strategy?.instrument_universe || defaultStrategyUniverse(strategy?.key)) {
    ensure(symbol);
  }

  return Array.from(candidates.values()).sort((left, right) => {
    const categoryRank = { conditions_met: 0, watch: 1, avoid: 2 };
    const rankDiff = categoryRank[left.category] - categoryRank[right.category];
    if (rankDiff) return rankDiff;
    return right.priorityScore - left.priorityScore;
  });
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
        expiry: position.expiry,
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
        expiry: trade.expiry,
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
  const [selectedStrategyKey, setSelectedStrategyKey] = useState("macd_strategy");
  const [activeStrategyTab, setActiveStrategyTab] = useState<StrategyDetailTab>("instruments");

  const strategyOverviewQuery = useLiveSnapshotQuery<{
    agent_status: StrategyAgentStatus;
    open_signals: any;
	    comments: StrategyComment[];
	    brokers: any[];
	    pipeline: any;
	    live_portfolio: any;
	    strategy_desk?: any;
	  }>({
    queryKey: ["strategyOverview"],
    queryFn: async () => {
      const [
        agent_status,
        open_signals,
        comments,
        brokers,
        pipeline,
        live_portfolio,
      ] = await Promise.all([
        getStrategyAgentStatus().then((response) => response.data as StrategyAgentStatus),
        getStrategyOpenSignals().then((response) => response.data as any),
        getStrategyAgentComments().then((response) => response.data as StrategyComment[]),
        getBrokerStatus().then((response) => response.data as any[]),
        getStrategyDataStatus().then((response) => response.data as any),
        getStrategyPortfolio().then((response) => response.data as any),
      ]);

      const [
        commodity_status,
        commodity_watchlist,
        directional_summary,
        directional_live,
        directional_positions,
        directional_journal,
        fmp_summary,
        fmp_live,
        fmp_positions,
        fmp_journal,
      ] = await Promise.all([
        safeApi(getCommodityStrategyStatus(), {}, 2_500),
        safeApi(getCommodityATMWatchlist(), {}, 2_500),
        safeApi(getDirectionalOptionsSummary(), {}, 2_500),
        safeApi(getDirectionalOptionsLiveSnapshot("NIFTY", "5minute", 16), {}, 2_500),
        safeApi(getDirectionalOptionsPaperPositions(undefined, "all", 50), {}, 2_500),
        safeApi(getDirectionalOptionsPaperJournal(undefined, 50), {}, 2_500),
        safeApi(getFractalMarketProfileSummary(), {}, 2_500),
        safeApi(getFractalMarketProfileLiveSnapshot("NIFTY"), {}, 2_500),
        safeApi(getFractalMarketProfilePaperPositions(undefined, "all", 50), {}, 2_500),
        safeApi(getFractalMarketProfilePaperJournal(undefined, 50), {}, 2_500),
      ]);

      return {
        agent_status,
        open_signals,
        comments,
        brokers,
        pipeline,
        live_portfolio,
        strategy_desk: {
          commodityStatus: commodity_status,
          commodityWatchlist: commodity_watchlist,
          directionalSummary: directional_summary,
          directionalLive: directional_live,
          directionalPositions: directional_positions,
          directionalJournal: directional_journal,
          fmpSummary: fmp_summary,
          fmpLive: fmp_live,
          fmpPositions: fmp_positions,
          fmpJournal: fmp_journal,
        },
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
	            strategy_desk?: any;
	          }),
        onStatusChange,
      ),
    preferStream: false,
    staleTime: 10_000,
  });

  const agentStatus = strategyOverviewQuery.data?.agent_status;
  const openSignals = strategyOverviewQuery.data?.open_signals;
  const comments = strategyOverviewQuery.data?.comments;
  const brokers = strategyOverviewQuery.data?.brokers;
  const pipeline = strategyOverviewQuery.data?.pipeline;
  const livePortfolio = strategyOverviewQuery.data?.live_portfolio;
  const strategyDesk = strategyOverviewQuery.data?.strategy_desk;

  const strategies = useMemo(() => agentStatus?.strategies || [], [agentStatus?.strategies]);
  const externalStrategies = useMemo(() => buildExternalStrategies(strategyDesk), [strategyDesk]);
  const deskStrategies = useMemo(() => [...strategies, ...externalStrategies], [externalStrategies, strategies]);
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
  const selectedStrategy = useMemo(
    () => deskStrategies.find((strategy) => strategy.key === selectedStrategyKey) || deskStrategies[0],
    [selectedStrategyKey, deskStrategies],
  );
  const selectedSignalRows = selectedStrategy?.key === "index_mp_strategy"
    ? strategy2Rows
    : selectedStrategy?.key === "macd_strategy"
      ? strategy1Rows
      : normalizeSignalRows(selectedStrategy?.signals || []);
  const selectedPortfolioRows = useMemo(
    () => {
      const existingRows = portfolioRows.filter((row) => row.strategyKey === selectedStrategy?.key);
      if (existingRows.length || !selectedStrategy || strategies.some((strategy) => strategy.key === selectedStrategy.key)) {
        return existingRows;
      }
      return buildStrategyPortfolioRows([selectedStrategy], selectedStrategy.last_scan_at || agentStatus?.last_run_at);
    },
    [agentStatus?.last_run_at, portfolioRows, selectedStrategy, strategies],
  );
  const selectedCandidates = useMemo(
    () => buildStrategyInstrumentCandidates(selectedStrategy, selectedSignalRows),
    [selectedSignalRows, selectedStrategy],
  );
  const candidatesByCategory = useMemo(
    () => ({
      conditions_met: selectedCandidates.filter((candidate) => candidate.category === "conditions_met"),
      watch: selectedCandidates.filter((candidate) => candidate.category === "watch"),
      avoid: selectedCandidates.filter((candidate) => candidate.category === "avoid"),
    }),
    [selectedCandidates],
  );
  const strategyRuntimeRows = agentStatus?.strategy_agents || [];
  const liveStrategyCount = deskStrategies.length || strategyRuntimeRows.length || strategies.length;
  const activeScanCount = deskStrategies.filter((strategy: any) => strategy?.meta?.mode !== "disabled").length || strategyRuntimeRows.filter((strategy: any) => strategy.mode !== "disabled").length || strategies.length;
  const commodityWatchRows = Number(
    strategyDesk?.commodityWatchlist?.summary?.total_rows
    || strategyDesk?.commodityStatus?.option_watchlist?.length
    || strategyDesk?.commodityStatus?.futures_watchlist?.length
    || strategyDesk?.commodityStatus?.watchlist?.length
    || 0,
  );
  const marketIntelligenceHealth = ((agentStatus?.data_health || {}) as any).market_intelligence || {};
  const directionalDataStatus = strategyDesk?.directionalLive?.snapshot?.data_status || {};
  const fmpAutomation = strategyDesk?.fmpSummary?.automation || {};
  const pipelineRows = [...(pipeline?.live_pipeline || []), ...(pipeline?.strategy2_pipeline || [])];
  const okPipelineRows = pipelineRows.filter((item: any) => item.status === "ok").length;
  const currentLiveRows = pipelineRows.filter((item: any) => item.freshness === "live").length;
  const coreStatusLoading = !agentStatus && strategyOverviewQuery.isLoading;
  const marketDataLabel = coreStatusLoading
    ? "Loading"
    : currentLiveRows > 0
      ? "Live"
      : okPipelineRows > 0
        ? "Replay / session-close"
        : "Stale / blocked";
  const marketDataTone = coreStatusLoading
    ? "text-text-secondary"
    : currentLiveRows > 0
      ? "text-accent-green"
      : okPipelineRows > 0
        ? "text-accent-amber"
        : "text-accent-red";

  return (
    <div className="mx-auto max-w-[1680px] space-y-3 pb-6">
      <section className="rounded-xl border border-bg-active/60 bg-bg-secondary/30 px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="max-w-4xl">
            <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
              <Bot size={18} className="text-accent-blue" />
              NSE Options Strategy Desk
            </div>
          </div>
          <div className="rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2" title="Loop state, broker gate, scan timestamps, target expiry, and cadence">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-text-primary">Runtime</span>
              <StatusBadge label={!agentStatus ? "Loading" : agentStatus.running ? "Loop Active" : "Idle"} tone={!agentStatus ? "idle" : agentStatus.running ? "ready" : "idle"} />
              {agentStatus ? (
                <>
                  <StatusBadge
                    label={agentStatus.kill_switch_active ? "Kill Switch On" : "Kill Switch Off"}
                    tone={agentStatus.kill_switch_active ? "warning" : "ready"}
                  />
                  <StatusBadge
                    label={agentStatus.auto_run_enabled ? "Automatic" : "Manual"}
                    tone={agentStatus.auto_run_enabled ? "ready" : "warning"}
                  />
                </>
              ) : null}
              {brokerSnapshot ? (
                <StatusBadge
                  label={brokerSnapshot.broker_ready ? "Broker Ready" : "Broker Blocked"}
                  tone={brokerSnapshot.broker_ready ? "ready" : "error"}
                />
              ) : null}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-text-secondary">
              <div>Last <span className="font-mono text-text-primary">{formatTimestamp(agentStatus?.last_run_at)}</span></div>
              <div>Next <span className="font-mono text-text-primary">{formatTimestamp(agentStatus?.next_scan_at)}</span></div>
              <div>Expiry <span className="font-mono text-text-primary">{agentStatus?.target_expiry || "--"}</span></div>
              <div>{agentStatus?.scan_interval_seconds || 60}s</div>
            </div>
          </div>
        </div>

        <div className="mt-3 grid gap-2 md:grid-cols-3 xl:grid-cols-9">
          <MetricTile label="Live Strategies" value={coreStatusLoading ? "--" : String(liveStrategyCount)} detail={coreStatusLoading ? "loading" : `${activeScanCount} scan-capable`} tone="text-accent-blue" />
          <MetricTile label="Market Data" value={marketDataLabel} detail={`${okPipelineRows}/${pipelineRows.length || 0} feeds OK`} tone={marketDataTone} />
          <MetricTile label="Open Positions" value={String(allPositions.length)} />
          <MetricTile label="Strategy 1 Open" value={String(strategies.find((strategy) => strategy.key === "macd_strategy")?.summary.open_positions || 0)} />
          <MetricTile label="Strategy 2 Open" value={String(strategies.find((strategy) => strategy.key === "index_mp_strategy")?.summary.open_positions || 0)} />
          <MetricTile label="Open P&L" value={formatSigned(totalOpenPnl, 0)} tone={pnlTone(totalOpenPnl)} />
          <MetricTile label="Realized" value={formatSigned(totalRealized, 0)} tone={pnlTone(totalRealized)} />
          <MetricTile label="Win Rate" value={totalTrades ? `${combinedWinRate.toFixed(1)}%` : "--"} detail={`${totalTrades} closed trades`} />
          <CandidateDistributionStrip
            met={candidatesByCategory.conditions_met.length}
            watch={candidatesByCategory.watch.length}
            avoid={candidatesByCategory.avoid.length}
          />
        </div>
      </section>

      <section className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
        <PanelHeader
          icon={<Bot size={16} className="text-accent-green" />}
          title="Strategy Agent Readiness"
          detail="Every strategy agent is shown with its visible instrument scope and tomorrow-open preparation state. Upstox analytics history is treated as valid paper-trading data; live broker reconnect is still shown separately."
          meta={`${deskStrategies.length} strategy lanes`}
        />
        <div className="mt-3 grid gap-2 lg:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-text-primary">NSE CE/PE MACD + MP</div>
              <StatusBadge label={!agentStatus ? "loading" : marketIntelligenceHealth?.ready ? "prepared" : "checking"} tone={!agentStatus ? "idle" : marketIntelligenceHealth?.ready ? "ready" : "warning"} />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
              <MiniMetric label="F&O Rows" value={String(marketIntelligenceHealth?.watchlist_rows_latest || 0)} />
              <MiniMetric label="CE Ready" value={String(marketIntelligenceHealth?.latest_ce_ready || 0)} />
              <MiniMetric label="PE Ready" value={String(marketIntelligenceHealth?.latest_pe_ready || 0)} />
            </div>
            <div className="mt-2 truncate text-xs text-text-muted" title={`${marketIntelligenceHealth?.latest_watchlist_session || "session pending"} · ${prettify(marketIntelligenceHealth?.readiness_mode)}`}>{marketIntelligenceHealth?.latest_watchlist_session || "session pending"} · {prettify(marketIntelligenceHealth?.readiness_mode)}</div>
          </div>

          <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-text-primary">Directional Options</div>
              <StatusBadge label={directionalDataStatus.execution_ready ? "prepared" : "monitoring"} tone={directionalDataStatus.execution_ready ? "ready" : "warning"} />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
              <MiniMetric label="Universe" value={String(strategyDesk?.directionalSummary?.underlyings?.length || 0)} />
              <MiniMetric label="Watch Rows" value={String(directionalDataStatus.watchlist_rows_latest || directionalDataStatus.watchlist_rows_today || 0)} />
              <MiniMetric label="Mode" value={prettify(directionalDataStatus.readiness_mode || "armed")} />
            </div>
            <div className="mt-2 truncate text-xs text-text-muted" title={strategyDesk?.directionalLive?.snapshot?.selection_reason || strategyDesk?.directionalSummary?.automation?.last_message || "Awaiting next scan."}>{strategyDesk?.directionalLive?.snapshot?.selection_reason || strategyDesk?.directionalSummary?.automation?.last_message || "Awaiting next scan."}</div>
          </div>

          <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-text-primary">Commodity</div>
              <StatusBadge label={commodityWatchRows ? "prepared" : "needs feed"} tone={commodityWatchRows ? "ready" : "warning"} />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
              <MiniMetric label="Agents" value={String(strategyDesk?.commodityStatus?.strategy_agents?.length || 0)} />
              <MiniMetric label="Watch Rows" value={String(commodityWatchRows)} />
              <MiniMetric label="Open" value={String(strategyDesk?.commodityStatus?.strategy_agents?.reduce?.((sum: number, item: any) => sum + Number(item.open_positions || 0), 0) || 0)} />
            </div>
            <div className="mt-2 truncate text-xs text-text-muted" title={strategyDesk?.commodityStatus?.last_message || strategyDesk?.commodityWatchlist?.detail || "Waiting for MCX market hours."}>{strategyDesk?.commodityStatus?.last_message || strategyDesk?.commodityWatchlist?.detail || "Waiting for MCX market hours."}</div>
          </div>

          <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-text-primary">MP / FMP</div>
              <StatusBadge label={fmpAutomation.loop_active ? "armed" : "idle"} tone={fmpAutomation.loop_active ? "ready" : "warning"} />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
              <MiniMetric label="Symbols" value={String(strategyDesk?.fmpSummary?.supported_symbols?.length || 0)} />
              <MiniMetric label="Open" value={String(strategyDesk?.fmpSummary?.paper_summary?.open_positions || 0)} />
              <MiniMetric label="Replay" value={String(strategyDesk?.fmpSummary?.replay_reports?.length || 0)} />
            </div>
            <div className="mt-2 truncate text-xs text-text-muted" title={fmpAutomation.last_message || "Armed for next session."}>{fmpAutomation.last_message || "Armed for next session."}</div>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <PanelHeader
            icon={<Shield size={16} className="text-accent-blue" />}
            title="Individual Strategy Workbench"
            detail="Each strategy has its own priority instruments and operational tabs. Instruments are ranked first by live condition state, then by realized strategy history."
            meta={selectedStrategy?.label || "No strategy selected"}
          />
          <div className="flex flex-wrap gap-2">
            {deskStrategies.map((strategy) => (
              <button
                key={strategy.key}
                type="button"
                onClick={() => {
                  setSelectedStrategyKey(strategy.key);
                  setActiveStrategyTab("instruments");
                }}
                className={clsx(
                  "rounded-xl border px-3 py-2 text-left text-xs transition-colors",
                  selectedStrategy?.key === strategy.key
                    ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                    : "border-bg-border bg-bg-primary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
                )}
              >
                <div className="font-semibold">{strategy.key === "macd_strategy" ? "Strategy 1" : strategy.key === "index_mp_strategy" ? "Strategy 2" : strategy.label}</div>
                <div className="mt-1 text-[11px] text-text-muted">{strategy.summary.open_positions || 0} open · {strategy.summary.total_trades || 0} trades</div>
              </button>
            ))}
          </div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-4">
          <MetricTile label="Mode" value={prettify(selectedStrategy?.meta?.mode || "unknown")} detail={formatTimestamp(selectedStrategy?.last_scan_at)} />
          <MetricTile label="Priority Instruments" value={String(candidatesByCategory.conditions_met.length + candidatesByCategory.watch.length)} detail="met + watch" tone="text-accent-green" />
          <MetricTile label="Avoid List" value={String(candidatesByCategory.avoid.length)} detail="blocked, stale or weak history" tone="text-accent-red" />
          <MetricTile label="Strategy P&L" value={formatSigned(selectedStrategy?.summary.realized_pnl, 0)} detail={`${selectedStrategy?.summary.total_trades || 0} closed trades`} tone={pnlTone(selectedStrategy?.summary.realized_pnl)} />
        </div>

        {(() => {
          const isCommodity = (selectedStrategy?.key || "").startsWith("commodity");
          const commodityFlowRows: any[] = isCommodity
            ? (strategyDesk?.commodityStatus?.futures_watchlist || [])
            : [];
          return (
            <div className="mt-4 flex flex-wrap gap-2">
              <DetailTabButton active={activeStrategyTab === "instruments"} label="Priority Instruments" count={selectedCandidates.length} onClick={() => setActiveStrategyTab("instruments")} />
              <DetailTabButton active={activeStrategyTab === "positions"} label="Trade Positions" count={selectedStrategy?.positions?.length || 0} onClick={() => setActiveStrategyTab("positions")} />
              <DetailTabButton active={activeStrategyTab === "history"} label="Trade History" count={selectedStrategy?.trade_history?.length || 0} onClick={() => setActiveStrategyTab("history")} />
              <DetailTabButton active={activeStrategyTab === "portfolio"} label="Strategy Portfolio" count={selectedPortfolioRows.length} onClick={() => setActiveStrategyTab("portfolio")} />
              <DetailTabButton active={activeStrategyTab === "performance"} label="Performance Metrics" onClick={() => setActiveStrategyTab("performance")} />
              {isCommodity ? (
                <DetailTabButton active={activeStrategyTab === "flow"} label="MCX Futures Flow" count={commodityFlowRows.length} onClick={() => setActiveStrategyTab("flow")} />
              ) : null}
            </div>
          );
        })()}

        {activeStrategyTab === "instruments" ? (
          <div className="mt-4 grid gap-4 xl:grid-cols-3">
            {(["conditions_met", "watch", "avoid"] as InstrumentCategory[]).map((category) => (
              <div
                key={category}
                className="rounded-lg border border-bg-border bg-bg-primary/25 p-2"
                title={
                  category === "conditions_met"
                    ? "Can be acted on only if risk engine and broker state approve."
                    : category === "watch"
                      ? "Historically favourable or aligned, but final condition is pending."
                      : "No signal, stale inputs, or negative strategy history."
                }
              >
                <div className={clsx("text-sm font-semibold", categoryTone(category))}>{categoryLabel(category)} · {candidatesByCategory[category].length}</div>
                <div className="mt-2 max-h-[420px] space-y-1.5 overflow-y-auto pr-1">
                  {candidatesByCategory[category].length ? (
                    candidatesByCategory[category].map((candidate) => (
                      <div key={`${category}-${candidate.symbol}`} className="rounded-lg border border-bg-border bg-bg-secondary/25 px-2 py-2">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <div className="font-semibold text-text-primary">{candidate.symbol}</div>
                            <div className="mt-0.5 truncate text-[11px] text-text-muted" title={candidate.reason}>{candidate.reason}</div>
                          </div>
                          <div className="text-right">
                            <StatusBadge label={candidate.direction || candidate.statusLabel} tone={candidate.category === "avoid" ? "error" : candidate.direction || candidate.statusLabel} />
                            <div className="mt-1 font-mono text-[11px] text-text-muted">score {formatNumber(candidate.priorityScore, 1)}</div>
                          </div>
                        </div>
                        <div className="mt-2 grid grid-cols-3 gap-1.5 text-[11px]">
                          <div className="rounded-md border border-bg-border bg-bg-primary/30 px-2 py-1.5">
                            <div className="text-text-muted">Hist P&L</div>
                            <div className={clsx("mt-1 font-mono", pnlTone(candidate.historyPnl))}>{formatSigned(candidate.historyPnl, 0)}</div>
                          </div>
                          <div className="rounded-md border border-bg-border bg-bg-primary/30 px-2 py-1.5">
                            <div className="text-text-muted">Win Rate</div>
                            <div className="mt-1 font-mono text-text-primary">{candidate.winRate != null ? `${(candidate.winRate * 100).toFixed(0)}%` : "--"}</div>
                          </div>
                          <div className="rounded-md border border-bg-border bg-bg-primary/30 px-2 py-1.5">
                            <div className="text-text-muted">Last Seen</div>
                            <div className="mt-1 font-mono text-text-primary">{formatTimestamp(candidate.lastSeen)}</div>
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="rounded-xl border border-dashed border-bg-border px-3 py-8 text-center text-xs text-text-muted">
                      No instruments in this bucket.
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : null}

        {activeStrategyTab === "positions" ? (
          <div className="mt-4 overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/25 p-3">
            <table className="w-full min-w-[1180px] text-left text-xs">
              <thead className="text-text-muted">
                <tr className="border-b border-bg-border">
                  <th className="pb-2 pr-3">Underlying</th>
                  <th className="pb-2 pr-3">Contract</th>
                  <th className="pb-2 pr-3">Qty</th>
                  <th className="pb-2 pr-3">Entry</th>
                  <th className="pb-2 pr-3">Mark</th>
                  <th className="pb-2 pr-3">Phase</th>
                  <th className="pb-2">P&L</th>
                </tr>
              </thead>
              <tbody>
                {(selectedStrategy?.positions || []).length ? (
                  (selectedStrategy?.positions || []).map((position: any) => (
                    <tr key={`${position.symbol}-${position.entered_at}`} className="border-b border-bg-border/40">
                      <td className="py-3 pr-3 font-semibold text-text-primary">{position.underlying}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{position.option_type} {position.strike} · {position.expiry}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{position.qty}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{formatNumber(position.entry_price)}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{formatNumber(position.current_price)}</td>
                      <td className="py-3 pr-3"><StatusBadge label={prettify(position.phase)} tone={position.phase} /></td>
                      <td className={clsx("py-3 font-mono font-semibold", pnlTone(position.unrealized_pnl))}>{formatSigned(position.unrealized_pnl, 0)}</td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan={7} className="py-10 text-center text-sm text-text-muted">No open positions for this strategy.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        ) : null}

        {activeStrategyTab === "history" ? (() => {
          // Backend now ships `today_trades` and `historical_trades` (recent-first).
          // Fall back to `trade_history` if a legacy payload arrives.
          const allRows: any[] = selectedStrategy?.trade_history || [];
          const todayRows: any[] =
            (selectedStrategy as any)?.today_trades?.length
              ? (selectedStrategy as any).today_trades
              : allRows.filter((t) => {
                  const ts = t.exit_time || t.entry_time || "";
                  return ts.slice(0, 10) === new Date().toISOString().slice(0, 10);
                });
          const historyRows: any[] =
            (selectedStrategy as any)?.historical_trades?.length
              ? (selectedStrategy as any).historical_trades
              : allRows.filter((t) => !todayRows.includes(t));

          const renderRow = (trade: any) => (
            <tr key={`${trade.symbol}-${trade.exit_time || trade.entry_time}`} className="border-b border-bg-border/40">
              <td className="py-3 pr-3 font-semibold text-text-primary">{tradeUnderlying(trade)}</td>
              <td className="py-3 pr-3 font-mono text-text-secondary">{strategyContractLabel(trade.option_type, trade.strike, trade.expiry)}</td>
              <td className="py-3 pr-3 font-mono text-text-secondary">{formatNumber(trade.entry_price)} · {formatTimestamp(trade.entry_time)}</td>
              <td className="py-3 pr-3 font-mono text-text-secondary">{formatNumber(trade.exit_price)} · {formatTimestamp(trade.exit_time)}</td>
              <td className="py-3 pr-3 text-text-muted">{prettify(trade.action || trade.instrument_type)}</td>
              <td className={clsx("py-3 font-mono font-semibold", pnlTone(trade.pnl))}>{formatSigned(trade.pnl, 0)}</td>
            </tr>
          );

          const renderTable = (rows: any[], emptyText: string) => (
            <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/25 p-3">
              <table className="w-full min-w-[1180px] text-left text-xs">
                <thead className="text-text-muted">
                  <tr className="border-b border-bg-border">
                    <th className="pb-2 pr-3">Underlying</th>
                    <th className="pb-2 pr-3">Contract</th>
                    <th className="pb-2 pr-3">Entry</th>
                    <th className="pb-2 pr-3">Exit</th>
                    <th className="pb-2 pr-3">Reason</th>
                    <th className="pb-2">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length ? rows.map(renderRow) : (
                    <tr><td colSpan={6} className="py-10 text-center text-sm text-text-muted">{emptyText}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          );

          return (
            <div className="mt-4 space-y-4">
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                  Today · {todayRows.length}
                </div>
                {renderTable(todayRows, "No trades closed today for this strategy.")}
              </div>
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                  History · {historyRows.length}
                </div>
                {renderTable(historyRows, "No prior trade history for this strategy.")}
              </div>
            </div>
          );
        })() : null}

        {activeStrategyTab === "portfolio" ? (
          <div className="mt-4 grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
            <div className="rounded-2xl border border-bg-border bg-bg-primary/25 p-4">
              <PanelHeader icon={<Shield size={16} className="text-accent-green" />} title="Strategy Portfolio" detail="Capital and exposure specific to the selected strategy." />
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <MetricTile label="Equity" value={formatCompact(selectedStrategy?.summary.total_equity)} />
                <MetricTile label="Open P&L" value={formatSigned(selectedStrategy?.summary.unrealized_pnl, 0)} tone={pnlTone(selectedStrategy?.summary.unrealized_pnl)} />
                <MetricTile label="Realized" value={formatSigned(selectedStrategy?.summary.realized_pnl, 0)} tone={pnlTone(selectedStrategy?.summary.realized_pnl)} />
                <MetricTile label="Entries / Exits" value={`${selectedStrategy?.summary.entries || 0} / ${selectedStrategy?.summary.exits || 0}`} />
              </div>
            </div>
            <div className="max-h-[360px] overflow-auto rounded-2xl border border-bg-border bg-bg-primary/25 p-3">
              {selectedPortfolioRows.length ? (
                selectedPortfolioRows.map((row) => (
                  <div key={row.id} className="mb-2 rounded-xl border border-bg-border bg-bg-secondary/25 px-3 py-3 text-xs">
                    <div className="flex justify-between gap-3">
                      <span className="font-semibold text-text-primary">{row.underlying}</span>
                      <span className={clsx("font-mono", pnlTone(row.pnl))}>{formatSigned(row.pnl, 0)}</span>
                    </div>
                    <div className="mt-1 text-text-muted">{row.contract} · {row.statusLabel}</div>
                  </div>
                ))
              ) : (
                <div className="py-12 text-center text-sm text-text-muted">No portfolio rows for this strategy.</div>
              )}
            </div>
          </div>
        ) : null}

        {activeStrategyTab === "performance" ? (
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <div className="rounded-2xl border border-bg-border bg-bg-primary/25 p-4">
              <PanelHeader icon={<BarChart3 size={16} className="text-accent-blue" />} title="Performance Metrics" detail="Live paper metrics reported by the selected strategy runtime." />
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <MetricTile label="Trades" value={String(selectedStrategy?.summary.total_trades || 0)} />
                <MetricTile label="Win Rate" value={selectedStrategy?.summary.win_rate != null ? `${((selectedStrategy.summary.win_rate || 0) * 100).toFixed(1)}%` : "--"} />
                <MetricTile label="Open Positions" value={String(selectedStrategy?.summary.open_positions || 0)} />
                <MetricTile label="Signals" value={String(selectedStrategy?.signals?.length || 0)} />
              </div>
            </div>
            <div className="rounded-2xl border border-bg-border bg-bg-primary/25 p-4">
              <PanelHeader icon={<Database size={16} className="text-accent-amber" />} title="Market Data Visibility" detail="Shows whether this strategy is seeing current data, session-close replay, or stale inputs." />
              <div className="mt-4 space-y-2 text-xs">
                {(selectedStrategy?.key === "index_mp_strategy" ? pipeline?.strategy2_pipeline : pipeline?.live_pipeline || []).map((item: any) => (
                  <div key={item.name} className="rounded-xl border border-bg-border bg-bg-secondary/25 px-3 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-semibold text-text-primary">{item.name}</span>
                      <StatusBadge label={item.freshness || item.status} tone={item.status || item.freshness} />
                    </div>
                    <div className="mt-1 text-text-muted">{item.detail}</div>
                    <div className="mt-1 font-mono text-[11px] text-text-muted">{item.rows || 0} rows · {item.last_date}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {activeStrategyTab === "flow" && (selectedStrategy?.key || "").startsWith("commodity") ? (() => {
          const commodityRows: any[] = strategyDesk?.commodityStatus?.futures_watchlist || [];
          if (!commodityRows.length) {
            return (
              <div className="mt-4 rounded-2xl border border-dashed border-bg-border px-3 py-12 text-center text-xs text-text-muted">
                No MCX futures rows yet. The agent will populate this once the market opens or a session replay loads.
              </div>
            );
          }
          return (
            <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/25 p-3">
              <PanelHeader
                icon={<CandlestickChart size={16} className="text-accent-amber" />}
                title="MCX Futures Flow"
                detail="Per-symbol MACD + MP gate state plus bar-CVD, anchored VWAP, and IB extension. CVD-agreement filters fresh MACD crosses before entry."
                meta={`${commodityRows.length} rows`}
              />
              <div className="mt-4 overflow-x-auto">
                <table className="w-full min-w-[1700px] text-left text-xs">
                  <thead>
                    <tr className="border-b border-bg-border text-text-muted">
                      <th className="pb-2 pr-3">Symbol</th>
                      <th className="pb-2 pr-3">Price · vs VWAP</th>
                      <th className="pb-2 pr-3">MACD</th>
                      <th className="pb-2 pr-3">Regime / MP</th>
                      <th className="pb-2 pr-3" title="Bar-CVD: session-anchored CVD + 6-bar trend">CVD</th>
                      <th className="pb-2 pr-3" title="Initial Balance: 1-hour opening range. Extension > 50% = directional day.">IB Ext</th>
                      <th className="pb-2 pr-3" title="Volume-by-price clusters">VbP</th>
                      <th className="pb-2">Validation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {commodityRows.map((row: any) => {
                      const price = row.price;
                      const vwap = row.vwap;
                      const vwapDelta = price != null && vwap != null ? price - vwap : null;
                      const vwapPct = vwapDelta != null && vwap ? (vwapDelta / vwap) * 100 : null;
                      const ibPct = row.ib_extension_pct;
                      const ibDir = row.ib_extended_above ? "up" : row.ib_extended_below ? "down" : "inside";
                      return (
                        <tr key={row.symbol} className="border-b border-bg-border/40 align-top">
                          <td className="py-3 pr-3">
                            <div className="font-medium text-text-primary">{row.underlying}</div>
                            <div className="mt-1 text-[11px] text-text-muted">{row.symbol}</div>
                          </td>
                          <td className="py-3 pr-3 font-mono text-text-secondary">
                            <div>{price != null ? formatNumber(price) : "--"}</div>
                            {vwap != null ? (
                              <div className={clsx("mt-1 text-[11px]", vwapDelta != null && vwapDelta > 0 ? "text-accent-green" : vwapDelta != null && vwapDelta < 0 ? "text-accent-red" : "text-text-muted")}>
                                VWAP {formatNumber(vwap)}{vwapPct != null ? ` (${vwapPct >= 0 ? "+" : ""}${vwapPct.toFixed(2)}%)` : ""}
                              </div>
                            ) : null}
                          </td>
                          <td className="py-3 pr-3 font-mono text-text-secondary">
                            <div>{row.macd != null ? formatNumber(row.macd, 2) : "--"}</div>
                            <div className="mt-1 text-[11px] text-text-muted">
                              hist {row.macd_histogram != null ? formatNumber(row.macd_histogram, 2) : "--"}
                            </div>
                          </td>
                          <td className="py-3 pr-3 text-text-secondary">
                            <StatusBadge label={prettify(row.regime)} tone={row.regime} />
                            {row.mp_day_type ? (
                              <div className="mt-1 text-[11px] text-text-muted">MP: {prettify(row.mp_day_type)}{row.mp_direction ? ` (${row.mp_direction})` : ""}</div>
                            ) : null}
                          </td>
                          <td className="py-3 pr-3 font-mono text-text-secondary">
                            <div className={clsx(row.cvd_session != null && row.cvd_session > 0 ? "text-accent-green" : row.cvd_session != null && row.cvd_session < 0 ? "text-accent-red" : "text-text-muted")}>
                              sess {row.cvd_session != null ? formatSigned(row.cvd_session, 0) : "--"}
                            </div>
                            <div className="mt-1 text-[11px] text-text-muted">
                              Δ6 {row.cvd_window_delta != null ? formatSigned(row.cvd_window_delta, 0) : "--"}
                            </div>
                            {row.cvd_agrees != null ? (
                              <div className="mt-1">
                                <StatusBadge label={row.cvd_agrees ? "✓ aligned" : "✗ disagree"} tone={row.cvd_agrees ? "ready" : "error"} />
                              </div>
                            ) : null}
                          </td>
                          <td className="py-3 pr-3 font-mono text-text-secondary">
                            <StatusBadge label={ibDir} tone={ibDir === "up" ? "ready" : ibDir === "down" ? "error" : "idle"} />
                            {ibPct != null ? (
                              <div className="mt-1 text-[11px] text-text-muted">{(ibPct * 100).toFixed(0)}% of IB</div>
                            ) : null}
                          </td>
                          <td className="py-3 pr-3 font-mono text-[11px] text-text-muted">
                            HVN {row.hvn_count ?? "--"} · LVN {row.lvn_count ?? "--"}
                            {row.cvd_divergence ? (
                              <div className="mt-1 text-accent-amber">div: {row.cvd_divergence.kind}</div>
                            ) : null}
                          </td>
                          <td className="py-3 text-text-muted text-[11px]" title={row.signal_validation_detail}>
                            <StatusBadge label={prettify(row.signal_validation)} tone={row.signal_validation === "ready" ? "ready" : "idle"} />
                            <div className="mt-1 truncate max-w-[260px]">{row.signal_validation_detail || ""}</div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })() : null}
      </section>

      <section className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div title="Portfolio stays first. Signal lanes stay separate from operations, and performance lives under operations so the live options book is not buried under research panels.">
            <div className="text-sm font-semibold text-text-primary">Workspace Tabs</div>
          </div>
          <div className="text-xs text-text-muted">
            {activeTab === "portfolio"
              ? `${allPositions.length} live positions`
              : activeTab === "signals"
                ? `${strategy1Rows.length + strategy2Rows.length} signal rows`
                : `${portfolioRows.length} portfolio rows`}
          </div>
        </div>

        <div className="mt-3 grid gap-2 md:grid-cols-3">
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
                        <div className="mt-1 flex items-center gap-2 text-[11px] text-text-muted">
                          <span>{position.expiry || "--"}</span>
                          {(() => {
                            const dte = computeDTE(position.expiry);
                            return dte != null ? (
                              <span className={clsx("font-mono font-semibold", dteTone(dte))}>· {dte}d</span>
                            ) : null;
                          })()}
                        </div>
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
                        <div className="mt-1 flex items-center gap-2 text-[11px] text-text-muted">
                          <span className="font-mono text-text-secondary">{formatHeldFor(position.entered_at)}</span>
                          <span>·</span>
                          <span>{formatTimestamp(position.price_updated_at || position.entered_at)}</span>
                        </div>
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
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.contract}</div>
                        {(() => {
                          const dte = computeDTE(row.expiry);
                          return row.status === "open" && dte != null ? (
                            <div className={clsx("mt-1 text-[11px] font-semibold", dteTone(dte))}>{dte}d to expiry</div>
                          ) : null;
                        })()}
                      </td>
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
            title="Strategy 2 · 15m Index MACD + MP"
            detail="This lane is the live MP-confirmed options workflow. Market Profile context is surfaced with the option trigger so each index row reads like an actionable trading lane rather than a CSV monitor."
            meta={`${strategy2Rows.length} rows`}
          />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1700px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Index</th>
                  <th className="pb-2 pr-3">Direction</th>
                  <th className="pb-2 pr-3">Status</th>
                  <th className="pb-2 pr-3">MP Context</th>
                  <th className="pb-2 pr-3">Spot / Value Area</th>
                  <th className="pb-2 pr-3" title="Bar-CVD agreement + window delta on the chosen side">Flow</th>
                  <th className="pb-2 pr-3">Freshness</th>
                  <th className="pb-2">Instruction</th>
                </tr>
              </thead>
              <tbody>
                {strategy2Rows.length ? (
                  strategy2Rows.map((rowTyped) => {
                    // The orderflow fields are added by the backend (see
                    // commodity_strategy_agent + strategy_agent S2 path) but
                    // don't appear in the front-end's SignalRow type because
                    // that's narrower. Cast to any for new fields.
                    const row = rowTyped as any;
                    const sideVwap = row.direction === "CE" ? row.ce_vwap : row.direction === "PE" ? row.pe_vwap : null;
                    const sideCvd = row.direction === "CE" ? row.ce_cvd_session : row.direction === "PE" ? row.pe_cvd_session : null;
                    return (
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
                        <td className="py-3 pr-3 font-mono text-text-secondary" title="CVD = cumulative volume delta (Lee-Ready bar approximation)">
                          {row.cvd_agrees == null ? (
                            <span className="text-text-muted">—</span>
                          ) : (
                            <StatusBadge label={row.cvd_agrees ? "✓ aligned" : "✗ disagree"} tone={row.cvd_agrees ? "ready" : "error"} />
                          )}
                          <div className="mt-1 text-[11px] text-text-muted">
                            Δ{row.cvd_window_delta != null ? formatSigned(row.cvd_window_delta, 0) : "--"} · sess {sideCvd != null ? formatSigned(sideCvd, 0) : "--"}
                          </div>
                          {sideVwap != null ? (
                            <div className="mt-0.5 text-[11px] text-text-muted">VWAP {formatNumber(sideVwap)}</div>
                          ) : null}
                        </td>
                        <td className="py-3 pr-3 text-text-secondary">
                          <StatusBadge label={row.freshness || "unknown"} tone={freshnessTone(row.freshness)} />
                          <div className="mt-2 text-[11px] text-text-muted">
                            {row.spot_source ? `${row.spot_source} spot` : "--"} · option {row.option_last_bar_time || "--"}
                          </div>
                        </td>
                        <td className="py-3 text-text-muted">{row.instruction || "--"}</td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={8} className="py-10 text-center text-sm text-text-muted">
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
                meta={`${brokerRows.filter((broker) => isBrokerReady(broker)).length} connected`}
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
                    <StatusBadge label={isBrokerReady(broker) ? "connected" : "offline"} tone={isBrokerReady(broker) ? "ready" : "error"} />
                    <span className="font-semibold uppercase text-text-primary">{broker.broker}</span>
                    <span className="text-text-muted">{isBrokerReady(broker) ? broker.name || broker.user_id || "connected" : broker.detail || "disconnected"}</span>
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
                      <td className="py-2 pr-3 text-text-secondary">
                        <div>{row.contract}</div>
                        {(() => {
                          const dte = computeDTE(row.expiry);
                          return row.status === "open" && dte != null ? (
                            <div className={clsx("mt-1 text-[11px] font-semibold", dteTone(dte))}>{dte}d to expiry</div>
                          ) : null;
                        })()}
                      </td>
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
