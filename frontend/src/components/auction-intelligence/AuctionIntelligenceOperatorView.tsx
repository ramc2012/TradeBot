"use client";

import { memo, type ReactNode, useDeferredValue, useMemo, useState, useTransition } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  Bot,
  CandlestickChart,
  CheckCircle2,
  ChevronRight,
  Database,
  Gauge,
  Layers3,
  Loader2,
  Radar,
  RefreshCw,
  ShieldCheck,
  TimerReset,
  TrendingUp,
  WalletCards,
  Workflow,
} from "lucide-react";
import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  getAuctionIntelligenceCanaryReadiness,
  getAuctionIntelligenceDefaultConfig,
  getAuctionIntelligenceDemoScenario,
  getAuctionIntelligenceGateBValidation,
  getAuctionIntelligenceGateCValidation,
  getAuctionIntelligenceLiveSnapshot,
  getAuctionIntelligenceMPAgentContext,
  getAuctionIntelligenceMPDataStatus,
  getAuctionIntelligenceMPOpenSignal,
  getAuctionIntelligencePaperPositions,
  getAuctionIntelligenceMPSignals,
  getAuctionIntelligencePaperJournal,
  getAuctionIntelligenceShadowRecords,
  getAuctionIntelligenceSummary,
  runAuctionIntelligencePaperProposal,
  runAuctionIntelligenceShadowBackfill,
} from "@/lib/api";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";

type DemoScenarioOption = {
  id: string;
  label: string;
};

type DemoRequestBar = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

type DemoRequestQuote = {
  timestamp: string;
  bid: number;
  ask: number;
  bid_size: number;
  ask_size: number;
};

type DemoRequestTrade = {
  timestamp: string;
  price: number;
  quantity: number;
  aggressor_side: string;
};

type DemoRequest = {
  session: {
    symbol: string;
    session_date: string;
    last_price: number;
    stale_data_seconds: number;
    minutes_to_close: number;
    broker_connected: boolean;
  };
  portfolio: {
    net_liquidation: number;
    daily_realized_pnl: number;
    open_positions: number;
    symbol_exposure: Record<string, number>;
    agent_drawdowns: Record<string, number>;
    correlated_exposure: number;
  };
  quote: DemoRequestQuote;
  depth: {
    timestamp: string;
    bids: { price: number; quantity: number }[];
    asks: { price: number; quantity: number }[];
  };
  quote_history?: DemoRequestQuote[];
  bars: DemoRequestBar[];
  prior_bars: DemoRequestBar[];
  trades: DemoRequestTrade[];
  metadata: {
    symbol_code: string;
    scenario: string;
    scenario_label: string;
    lot_size: number;
    history_source?: string;
    quote_source?: string;
    order_flow_source?: string;
    snapshot_mode?: string;
    snapshot_time?: string;
    instrument_proxy?: string;
  };
};

type AnalysisResponse = {
  config_scope: {
    primary_underlyings: string[];
    secondary_underlyings: string[];
    instrument_type: string;
    latency_budget_ms: number;
    slippage_bps: number;
    commission_bps: number;
  };
  market_profile: {
    symbol?: string;
    poc: number;
    vah: number;
    val: number;
    initial_balance_high: number;
    initial_balance_low: number;
    initial_balance_range: number;
    day_range: number;
    range_extension_up: number;
    range_extension_down: number;
    bracket_state: string | null;
    close_price: number;
    high_price: number;
    low_price: number;
    spike_direction: string;
  };
  order_flow: {
    spread: number;
    micro_price: number;
    top_imbalance: number;
    depth_imbalance: number;
    delta: number;
    cumulative_delta: number;
    vwap: number;
    vwap_drift: number;
    queue_pressure: number;
    volatility_burst: number;
    passive_fill_probability: number;
    aggressive_fill_probability: number;
    adverse_selection_risk: number;
    timing_confidence: number;
    execution_aggression: string;
    micro_stop_distance: number;
    trade_imbalance?: number;
    order_flow_imbalance?: number;
    book_pressure?: number;
    micro_price_offset_bps?: number;
    trade_intensity_per_minute?: number;
    quote_repricing_rate?: number;
    toxicity_score?: number;
  };
  regime: {
    label: string;
    confidence: number;
    allowed_directions: string[];
    reasons: string[];
    scorecard: Record<string, number>;
  };
  agent_decisions: {
    agent_name: string;
    action: string;
    confidence: number;
    entry_price: number | null;
    stop_price: number | null;
    target_price: number | null;
    quantity: number;
    sleeve_fraction: number;
    rationale: string[];
    metadata?: Record<string, unknown>;
  }[];
  risk: {
    allowed: boolean;
    kill_switch: boolean;
    max_size_multiplier: number;
    reasons: string[];
  };
  execution_plan: {
    agent_name: string;
    symbol: string;
    action: string;
    style: string;
    order_type: string;
    limit_price: number | null;
    slices: number;
    cancel_after_seconds: number;
    rationale: string[];
    quantity?: number;
    broker_action?: string | null;
    underlying_symbol?: string | null;
    instrument_type?: string | null;
    expiry?: string | null;
    strike?: number | null;
    option_type?: string | null;
    trading_symbol?: string | null;
    premium?: number | null;
    spot_price?: number | null;
    moneyness?: string | null;
    expiry_kind?: string | null;
    days_to_expiry?: number | null;
    selection_reason?: string | null;
  }[];
  ntm_volx?: {
    underlying: string;
    expiry: string;
    spot_price: number;
    atm_strike: number;
    dominant_side: "CALLS" | "PUTS" | "BALANCED";
    directional_bias: string;
    regime: string;
    vxr: number;
    call_pressure: number;
    put_pressure: number;
    net_pressure: number;
    call_volume: number;
    put_volume: number;
    call_notional: number;
    put_notional: number;
    call_oi_change: number;
    put_oi_change: number;
    call_wall_strike: number | null;
    put_wall_strike: number | null;
    pair_count: number;
    notes: string[];
    pressure_ladder: {
      strike: number;
      distance_from_spot: number;
      distance_from_spot_pct: number;
      call_volume: number;
      put_volume: number;
      call_notional: number;
      put_notional: number;
      call_oi_change: number;
      put_oi_change: number;
      call_pressure: number;
      put_pressure: number;
      net_pressure: number;
    }[];
  } | null;
};

type WorkspacePayload = {
  mode?: string;
  scenario: string;
  scenario_label: string;
  symbol_code: string;
  session_date?: string;
  available_symbols: string[];
  available_scenarios: DemoScenarioOption[];
  request: DemoRequest;
  analysis: AnalysisResponse;
};

type SummaryResponse = {
  demo_symbols: string[];
  live_symbols: string[];
  demo_scenarios: DemoScenarioOption[];
  connected_brokers?: string[];
  live_ready?: boolean;
  validation_gates?: {
    id: string;
    label: string;
    status: string;
  }[];
};

type DefaultConfigResponse = {
  risk: {
    max_daily_loss: number;
    max_concurrent_positions: number;
    max_symbol_exposure: number;
    max_correlated_exposure: number;
    stale_data_seconds: number;
  };
  mvp_scope: {
    primary_underlyings: string[];
    secondary_underlyings: string[];
    instrument_type: string;
    latency_budget_ms: number;
    slippage_bps: number;
    commission_bps: number;
  };
  paper_trading: {
    slippage_bps: number;
    fees_per_order: number;
  };
};

type ValidationResponse = {
  gate: string;
  passed: boolean;
  score: number;
  generated_at: string;
  checks: {
    key: string;
    label: string;
    passed: boolean;
    observed: unknown;
    threshold?: unknown;
    severity: "error" | "warning" | "info";
    detail: string;
  }[];
  metrics: Record<string, string | number | boolean | null>;
  pending_checks: string[];
  storage?: {
    persisted: boolean;
    run_id?: string;
    artifact_count?: number;
  };
  series_metadata?: {
    symbol_code?: string;
    symbol?: string;
    source?: string;
    session_count?: number;
    session_dates?: string[];
    record_count?: number;
  };
};

type ShadowBackfillResponse = {
  symbol_code: string;
  snapshot_count: number;
  record_count: number;
  storage?: {
    persisted: boolean;
  };
};

type CanaryReadinessResponse = {
  symbol: string;
  ready: boolean;
  blockers: string[];
  requirements: {
    manual_approval_required: boolean;
    allowed_agents: string[];
    max_live_lots: number;
    daily_loss_limit: number;
  };
  next_step: string;
};

type PaperJournalRecord = {
  recorded_at: string;
  symbol: string;
  regime: string;
  agent_name: string;
  action: string;
  confidence: number;
  quantity: number;
  premium?: number | null;
  trading_symbol?: string | null;
  moneyness?: string | null;
  expiry?: string | null;
  days_to_expiry?: number | null;
  selection_reason?: string | null;
  execution_style?: string | null;
};

type PaperJournalResponse = {
  symbol_filter?: string | null;
  count: number;
  total_records: number;
  summary: {
    latest_recorded_at?: string | null;
    avg_confidence?: number | null;
    avg_premium?: number | null;
    action_breakdown: Record<string, number>;
    style_breakdown: Record<string, number>;
    agent_breakdown: Record<string, number>;
  };
  records: PaperJournalRecord[];
};

type PaperPosition = {
  position_id: string;
  status: string;
  opened_at: string;
  closed_at?: string | null;
  agent_name: string;
  signal_action: string;
  trading_symbol?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
  quantity: number;
  entry_premium: number;
  latest_premium: number;
  exit_premium?: number | null;
  entry_confidence: number;
  latest_confidence: number;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  moneyness?: string | null;
  days_to_expiry?: number | null;
  close_reason?: string | null;
};

type PaperPositionsResponse = {
  symbol_filter?: string | null;
  status: string;
  summary: {
    open_count: number;
    closed_count: number;
    realized_pnl: number;
    unrealized_pnl: number;
    latest_opened_at?: string | null;
    latest_closed_at?: string | null;
    last_synced_at?: string | null;
  };
  open_positions: PaperPosition[];
  closed_positions: PaperPosition[];
};

type ShadowRecord = {
  record_id?: string;
  signal_id?: string;
  session_date: string;
  symbol: string;
  agent_name: string;
  action: string;
  confidence?: number;
  setup_name?: string | null;
  regime_label?: string | null;
  fill_drift_ticks?: number | null;
  metadata?: {
    decision_metadata?: {
      setup_name?: string;
    };
  };
};

type ShadowRecordsResponse = {
  symbol: string;
  count: number;
  records: ShadowRecord[];
};

type MpDataSource = {
  name: string;
  status: string;
  rows: number;
  last_date: string;
  detail: string;
};

type MpSignalRecord = {
  date: string;
  day_type: string;
  direction: string;
  buyer_fail: number;
  seller_fail: number;
  net_failure: number;
  daily_move: number;
};

type MpSignalsResponse = {
  underlying: string;
  signals: MpSignalRecord[];
  latest?: MpSignalRecord | null;
};

type MpOpenSignalResponse = {
  as_of?: string;
  signals: {
    signal_date: string;
    direction: string;
    reason: string;
    strength: string;
    alloc: number;
    instruction: string;
    day_type: string;
  }[];
  skip_reason?: string | null;
};

type MpAgentContextItem = {
  time: string;
  type: string;
  level: string;
  message: string;
};

type DataMode = "live" | "demo";

type ProfileRow = DemoRequestBar & {
  label: string;
  range: [number, number];
};

type OrderFlowRow = {
  timestamp: string;
  label: string;
  mid: number;
  micro: number;
  spreadBps: number;
  topImbalance: number;
  tradeDelta: number;
  cumulativeDelta: number;
};

type NTMVolXRow = {
  label: string;
  strike: number;
  callPressure: number;
  putPressureSigned: number;
  netPressure: number;
  callVolume: number;
  putVolume: number;
};

const FALLBACK_SCENARIOS: DemoScenarioOption[] = [
  { id: "acceptance_up", label: "Breakout acceptance above prior value" },
  { id: "failed_auction", label: "Failed downside auction with re-entry" },
  { id: "balance", label: "Rotational balance session" },
];

function formatPrice(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-IN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits === 0 ? 0 : 0,
  });
}

function formatCurrency(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function formatPct(value: number | null | undefined, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function formatSigned(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function formatCompact(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Intl.NumberFormat("en-IN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function sectionChrome(extra?: string) {
  return clsx(
    "rounded-[30px] border border-white/10 bg-[linear-gradient(180deg,rgba(11,18,30,0.95),rgba(7,11,21,0.98))] shadow-[0_24px_60px_rgba(3,8,18,0.38)] backdrop-blur",
    extra,
  );
}

function toneForAction(action: string) {
  if (action === "LONG" || action === "CE") return "border-emerald-400/35 bg-emerald-400/12 text-emerald-300";
  if (action === "SHORT" || action === "PE") return "border-rose-400/35 bg-rose-400/12 text-rose-300";
  return "border-white/12 bg-white/6 text-slate-300";
}

function toneForStatus(status: string) {
  if (status === "ok" || status === "ready" || status === "pass") return "text-emerald-300";
  if (status === "warning" || status === "blocked") return "text-amber-300";
  if (status === "missing" || status === "fail") return "text-rose-300";
  return "text-slate-300";
}

function toneForRegime(label: string) {
  if (label.includes("trend") || label.includes("acceptance")) {
    return "border-emerald-400/35 bg-emerald-400/12 text-emerald-300";
  }
  if (label.includes("failed") || label.includes("rejection") || label.includes("reversal")) {
    return "border-amber-400/35 bg-amber-400/12 text-amber-300";
  }
  if (label.includes("balance")) {
    return "border-sky-400/35 bg-sky-400/12 text-sky-300";
  }
  return "border-white/12 bg-white/6 text-slate-300";
}

function minuteKey(timestamp: string) {
  return new Date(timestamp).toISOString().slice(0, 16);
}

function buildProfileRows(bars: DemoRequestBar[]): ProfileRow[] {
  return bars.map((bar) => ({
    ...bar,
    label: new Date(bar.timestamp).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
    }),
    range: [bar.low, bar.high],
  }));
}

function buildOrderFlowRows(
  quotes: DemoRequestQuote[],
  bars: DemoRequestBar[],
  trades: DemoRequestTrade[],
): OrderFlowRow[] {
  const quoteTape = quotes.length > 0
    ? quotes
    : bars.map((bar) => ({
        timestamp: bar.timestamp,
        bid: bar.close - 0.5,
        ask: bar.close + 0.5,
        bid_size: 100,
        ask_size: 100,
      }));

  const tradeBuckets = new Map<string, number>();
  for (const trade of trades) {
    const side = trade.aggressor_side.toLowerCase();
    const signedQty = side === "buy" ? trade.quantity : side === "sell" ? -trade.quantity : 0;
    const key = minuteKey(trade.timestamp);
    tradeBuckets.set(key, (tradeBuckets.get(key) ?? 0) + signedQty);
  }

  let cumulativeDelta = 0;
  return quoteTape.map((quote) => {
    const mid = (Number(quote.bid) + Number(quote.ask)) / 2;
    const totalSize = Number(quote.bid_size) + Number(quote.ask_size);
    const micro = totalSize > 0
      ? ((Number(quote.ask) * Number(quote.bid_size)) + (Number(quote.bid) * Number(quote.ask_size))) / totalSize
      : mid;
    const spread = Math.max(Number(quote.ask) - Number(quote.bid), 0);
    const spreadBps = mid > 0 ? (spread / mid) * 10_000 : 0;
    const imbalance = totalSize > 0
      ? (Number(quote.bid_size) - Number(quote.ask_size)) / totalSize
      : 0;
    const delta = tradeBuckets.get(minuteKey(quote.timestamp)) ?? 0;
    cumulativeDelta += delta;
    return {
      timestamp: quote.timestamp,
      label: new Date(quote.timestamp).toLocaleTimeString("en-IN", {
        hour: "2-digit",
        minute: "2-digit",
      }),
      mid,
      micro,
      spreadBps,
      topImbalance: imbalance,
      tradeDelta: delta,
      cumulativeDelta,
    };
  });
}

function buildNTMVolXRows(
  levels: NonNullable<AnalysisResponse["ntm_volx"]>["pressure_ladder"],
): NTMVolXRow[] {
  return levels.map((level) => ({
    label: formatPrice(level.strike, 0),
    strike: level.strike,
    callPressure: level.call_pressure,
    putPressureSigned: -Math.abs(level.put_pressure),
    netPressure: level.net_pressure,
    callVolume: level.call_volume,
    putVolume: level.put_volume,
  }));
}

const SectionTitle = memo(function SectionTitle({
  icon,
  eyebrow,
  title,
  detail,
  action,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-slate-400">
          {icon}
          {eyebrow}
        </div>
        <div className="text-lg font-semibold text-slate-100">{title}</div>
        {detail ? <div className="max-w-3xl text-sm leading-6 text-slate-400">{detail}</div> : null}
      </div>
      {action}
    </div>
  );
});

const StatusPill = memo(function StatusPill({
  label,
  className,
}: {
  label: string;
  className: string;
}) {
  return (
    <span className={clsx("inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em]", className)}>
      {label.replaceAll("_", " ")}
    </span>
  );
});

const KpiCard = memo(function KpiCard({
  label,
  value,
  detail,
  tone = "text-slate-100",
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className={clsx("mt-3 text-2xl font-semibold", tone)}>{value}</div>
      <div className="mt-2 text-sm leading-6 text-slate-400">{detail}</div>
    </div>
  );
});

const MetricRow = memo(function MetricRow({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="border-t border-white/6 py-3 first:border-t-0">
      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-slate-100">{value}</div>
      <div className="mt-1 text-xs leading-5 text-slate-400">{hint}</div>
    </div>
  );
});

const DecisionCard = memo(function DecisionCard({
  decision,
}: {
  decision: AnalysisResponse["agent_decisions"][number];
}) {
  return (
    <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">{decision.agent_name}</div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusPill label={decision.action} className={toneForAction(decision.action)} />
            <span className="text-sm text-slate-300">confidence {formatPct(decision.confidence, 0)}</span>
            <span className="text-sm text-slate-300">sleeve {formatPct(decision.sleeve_fraction, 0)}</span>
          </div>
        </div>
        <div className="grid gap-2 text-sm text-slate-400 sm:grid-cols-2">
          <div>Entry <span className="font-mono text-slate-100">{formatPrice(decision.entry_price)}</span></div>
          <div>Stop <span className="font-mono text-slate-100">{formatPrice(decision.stop_price)}</span></div>
          <div>Target <span className="font-mono text-slate-100">{formatPrice(decision.target_price)}</span></div>
          <div>Qty <span className="font-mono text-slate-100">{decision.quantity || "—"}</span></div>
        </div>
      </div>
      <div className="mt-4 space-y-2 text-sm text-slate-400">
        {decision.rationale.slice(0, 3).map((reason) => (
          <div key={reason} className="flex items-start gap-2">
            <ChevronRight size={14} className="mt-1 shrink-0 text-sky-300" />
            <span>{reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
});

const ExecutionCard = memo(function ExecutionCard({
  step,
}: {
  step: AnalysisResponse["execution_plan"][number];
}) {
  return (
    <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <StatusPill label={step.action} className={toneForAction(step.action)} />
          <div>
            <div className="text-sm font-semibold text-slate-100">{step.agent_name}</div>
            <div className="text-xs text-slate-400">{step.style} · {step.order_type} · {step.slices} slice{step.slices === 1 ? "" : "s"}</div>
          </div>
        </div>
        <div className="text-sm text-slate-400">{step.broker_action ?? "paper"} · {step.quantity ?? "—"} qty</div>
      </div>
      <div className="mt-4 grid gap-2 text-sm text-slate-400 sm:grid-cols-2">
        <div>Contract <span className="font-mono text-slate-100">{step.trading_symbol ?? step.symbol}</span></div>
        <div>Premium <span className="font-mono text-slate-100">{formatPrice(step.premium ?? step.limit_price)}</span></div>
        <div>Strike <span className="font-mono text-slate-100">{step.strike ? step.strike.toFixed(0) : "—"}</span></div>
        <div>Expiry <span className="font-mono text-slate-100">{step.expiry ?? "—"}</span></div>
        <div>Moneyness <span className="font-mono text-slate-100">{step.moneyness ?? "—"}</span></div>
        <div>DTE <span className="font-mono text-slate-100">{step.days_to_expiry ?? "—"}</span></div>
      </div>
      {step.selection_reason ? (
        <div className="mt-4 rounded-2xl border border-white/8 bg-white/5 px-3 py-2 text-sm leading-6 text-slate-300">
          {step.selection_reason}
        </div>
      ) : null}
    </div>
  );
});

const ExposureBar = memo(function ExposureBar({
  label,
  value,
  scale = 1,
  accent = "bg-sky-400",
}: {
  label: string;
  value: number;
  scale?: number;
  accent?: string;
}) {
  const width = Math.min(Math.abs(value) / Math.max(scale, 1e-6), 1) * 100;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-slate-300">{label}</span>
        <span className="font-mono text-slate-100">{formatSigned(value * 100, 1)}%</span>
      </div>
      <div className="h-2 rounded-full bg-white/6">
        <div
          className={clsx("h-2 rounded-full", accent)}
          style={{ width: `${Math.max(width, 4)}%` }}
        />
      </div>
    </div>
  );
});

const PersistedTable = memo(function PersistedTable({
  headers,
  rows,
  empty,
}: {
  headers: string[];
  rows: ReactNode[][];
  empty: string;
}) {
  return (
    <div className="overflow-hidden rounded-[26px] border border-white/8 bg-black/15">
      <div className="grid grid-cols-4 gap-3 border-b border-white/8 px-4 py-3 text-[11px] uppercase tracking-[0.18em] text-slate-500">
        {headers.map((header) => (
          <div key={header}>{header}</div>
        ))}
      </div>
      {rows.length > 0 ? (
        rows.map((row, index) => (
          <div
            key={index}
            className="grid grid-cols-4 gap-3 border-t border-white/6 px-4 py-3 text-sm text-slate-300 first:border-t-0"
          >
            {row.map((cell, cellIndex) => (
              <div key={cellIndex} className="min-w-0 truncate">{cell}</div>
            ))}
          </div>
        ))
      ) : (
        <div className="px-4 py-6 text-sm text-slate-400">{empty}</div>
      )}
    </div>
  );
});

const GateSummaryCard = memo(function GateSummaryCard({
  gate,
  title,
  statusDetail,
  metrics,
}: {
  gate: ValidationResponse | undefined;
  title: string;
  statusDetail: string;
  metrics: { label: string; value: string; hint: string }[];
}) {
  return (
    <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-100">{title}</div>
          <div className="mt-1 text-sm leading-6 text-slate-400">{statusDetail}</div>
        </div>
        {gate ? (
          <StatusPill
            label={gate.passed ? "pass" : "fail"}
            className={gate.passed ? "border-emerald-400/35 bg-emerald-400/12 text-emerald-300" : "border-rose-400/35 bg-rose-400/12 text-rose-300"}
          />
        ) : null}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {metrics.map((metric) => (
          <div key={metric.label} className="rounded-2xl border border-white/8 bg-white/5 p-3">
            <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">{metric.label}</div>
            <div className="mt-2 text-lg font-semibold text-slate-100">{metric.value}</div>
            <div className="mt-1 text-xs leading-5 text-slate-400">{metric.hint}</div>
          </div>
        ))}
      </div>
    </div>
  );
});

export default function AuctionIntelligenceOperatorView() {
  const [dataMode, setDataMode] = useState<DataMode>("live");
  const [symbol, setSymbol] = useState("NIFTY");
  const [scenario, setScenario] = useState("acceptance_up");
  const [isPending, startTransition] = useTransition();

  const deferredMode = useDeferredValue(dataMode);
  const deferredSymbol = useDeferredValue(symbol);
  const deferredScenario = useDeferredValue(scenario);
  const validationSymbol = deferredMode === "live" && deferredSymbol === "NIFTY" ? "BANKNIFTY" : deferredSymbol;

  const summaryQuery = useQuery<SummaryResponse>({
    queryKey: ["auction-intelligence", "summary"],
    queryFn: async () => (await getAuctionIntelligenceSummary()).data,
    staleTime: 60_000,
  });

  const configQuery = useQuery<DefaultConfigResponse>({
    queryKey: ["auction-intelligence", "config"],
    queryFn: async () => (await getAuctionIntelligenceDefaultConfig()).data,
    staleTime: 60_000,
  });

  const validationQuery = usePersistentSnapshotQuery<WorkspacePayload>({
    queryKey: [
      "auction-intelligence",
      deferredMode,
      deferredSymbol,
      deferredMode === "demo" ? deferredScenario : "live",
    ],
    storageKey: `auction-intelligence:${deferredMode}:${deferredSymbol}:${deferredMode === "demo" ? deferredScenario : "live"}`,
    queryFn: async () => (
      deferredMode === "live"
        ? (await getAuctionIntelligenceLiveSnapshot(deferredSymbol)).data
        : (await getAuctionIntelligenceDemoScenario(deferredSymbol, deferredScenario)).data
    ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const gateBQuery = useQuery<ValidationResponse>({
    queryKey: [
      "auction-intelligence",
      "gate-b",
      deferredMode,
      validationSymbol,
      deferredMode === "demo" ? deferredScenario : "live",
    ],
    queryFn: async () => (
      await getAuctionIntelligenceGateBValidation(
        validationSymbol,
        deferredMode,
        deferredScenario,
        deferredMode === "live" ? 20 : 6,
        deferredMode === "live" ? 45 : 14,
      )
    ).data,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const gateCQuery = useQuery<ValidationResponse>({
    queryKey: ["auction-intelligence", "gate-c", validationSymbol],
    queryFn: async () => (await getAuctionIntelligenceGateCValidation(validationSymbol, 30, 500)).data,
    enabled: deferredMode === "live",
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const canaryReadinessQuery = useQuery<CanaryReadinessResponse>({
    queryKey: ["auction-intelligence", "canary", validationSymbol],
    queryFn: async () => (await getAuctionIntelligenceCanaryReadiness(validationSymbol)).data,
    enabled: deferredMode === "live",
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const paperJournalQuery = useQuery<PaperJournalResponse>({
    queryKey: ["auction-intelligence", "paper-journal", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligencePaperJournal(deferredSymbol, 36)).data,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const paperPositionsQuery = useQuery<PaperPositionsResponse>({
    queryKey: ["auction-intelligence", "paper-positions", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligencePaperPositions(deferredSymbol, "all", 24)).data,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const shadowRecordsQuery = useQuery<ShadowRecordsResponse>({
    queryKey: ["auction-intelligence", "shadow-records", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligenceShadowRecords(deferredSymbol, 36)).data,
    enabled: deferredMode === "live",
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const mpDataStatusQuery = useQuery<MpDataSource[]>({
    queryKey: ["auction-intelligence", "mp-data-status"],
    queryFn: async () => (await getAuctionIntelligenceMPDataStatus()).data,
    staleTime: 120_000,
    refetchOnWindowFocus: false,
  });

  const mpSignalsQuery = useQuery<MpSignalsResponse>({
    queryKey: ["auction-intelligence", "mp-signals", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligenceMPSignals(deferredSymbol)).data,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const mpOpenSignalQuery = useQuery<MpOpenSignalResponse>({
    queryKey: ["auction-intelligence", "mp-open-signal", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligenceMPOpenSignal(deferredSymbol)).data,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const mpAgentContextQuery = useQuery<MpAgentContextItem[]>({
    queryKey: ["auction-intelligence", "mp-agent-context", deferredSymbol],
    queryFn: async () => (await getAuctionIntelligenceMPAgentContext(deferredSymbol)).data,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const paperProposal = useMutation({
    mutationFn: async () => {
      if (!validationQuery.data) throw new Error("No validation payload loaded");
      const { request } = validationQuery.data;
      return (await runAuctionIntelligencePaperProposal({
        session: request.session,
        portfolio: request.portfolio,
        quote: request.quote,
        depth: request.depth,
        quote_history: request.quote_history ?? [],
        bars: request.bars,
        prior_bars: request.prior_bars,
        trades: request.trades,
      })).data;
    },
    onSuccess: async () => {
      await Promise.all([
        paperJournalQuery.refetch(),
        paperPositionsQuery.refetch(),
      ]);
    },
  });

  const shadowBackfill = useMutation<ShadowBackfillResponse>({
    mutationFn: async () => (
      await runAuctionIntelligenceShadowBackfill(
        validationSymbol,
        20,
        45,
        4,
        "11:15",
        1_000_000,
        {
          dashboard_checked: true,
          alerts_checked: true,
          manual_override_tested: true,
          kill_switch_tested: true,
          kill_switch_passed: true,
        },
      )
    ).data,
    onSuccess: async () => {
      await Promise.all([
        gateCQuery.refetch(),
        canaryReadinessQuery.refetch(),
        shadowRecordsQuery.refetch(),
        paperPositionsQuery.refetch(),
      ]);
    },
  });

  const summary = summaryQuery.data;
  const config = configQuery.data;
  const payload = validationQuery.data;
  const request = payload?.request;
  const analysis = payload?.analysis;
  const gateB = gateBQuery.data;
  const gateC = gateCQuery.data;
  const canaryReadiness = canaryReadinessQuery.data;
  const liveReady = Boolean(summary?.live_ready);

  const symbols = deferredMode === "live"
    ? summary?.live_symbols ?? ["NIFTY", "BANKNIFTY", "SENSEX"]
    : summary?.demo_symbols ?? ["NIFTY", "BANKNIFTY"];
  const scenarios = summary?.demo_scenarios ?? FALLBACK_SCENARIOS;

  const profileRows = useMemo(() => buildProfileRows(request?.bars ?? []), [request?.bars]);
  const orderFlowRows = useMemo(
    () => buildOrderFlowRows(request?.quote_history ?? [], request?.bars ?? [], request?.trades ?? []),
    [request?.quote_history, request?.bars, request?.trades],
  );
  const ntmVolxRows = useMemo(
    () => buildNTMVolXRows(analysis?.ntm_volx?.pressure_ladder ?? []),
    [analysis?.ntm_volx?.pressure_ladder],
  );

  const mpDomain = useMemo<[number, number]>(() => {
    const values = [
      ...(request?.bars ?? []).flatMap((bar) => [bar.low, bar.high, bar.close]),
      analysis?.market_profile.poc,
      analysis?.market_profile.vah,
      analysis?.market_profile.val,
      analysis?.market_profile.initial_balance_high,
      analysis?.market_profile.initial_balance_low,
    ].filter((value): value is number => value !== undefined && value !== null && !Number.isNaN(value));

    if (!values.length) return [0, 100];
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, 12);
    return [Math.floor(min - padding), Math.ceil(max + padding)];
  }, [analysis, request?.bars]);

  const flowPriceDomain = useMemo<[number, number]>(() => {
    const values = orderFlowRows.flatMap((row) => [row.mid, row.micro]);
    if (!values.length) return [0, 100];
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.12, 4);
    return [min - padding, max + padding];
  }, [orderFlowRows]);

  const ntmPressureDomain = useMemo<[number, number]>(() => {
    const values = ntmVolxRows.flatMap((row) => [row.callPressure, row.putPressureSigned]);
    if (!values.length) return [-100, 100];
    const edge = Math.max(...values.map((value) => Math.abs(value)));
    return [-(edge * 1.15), edge * 1.15];
  }, [ntmVolxRows]);

  const maxExposure = useMemo(() => {
    const exposures = Object.values(request?.portfolio.symbol_exposure ?? {}).map((value) => Math.abs(value));
    return Math.max(...exposures, config?.risk.max_symbol_exposure ?? 0.01, 0.01);
  }, [config?.risk.max_symbol_exposure, request?.portfolio.symbol_exposure]);

  const dataSources = useMemo(
    () => (mpDataStatusQuery.data ?? []).filter((item) => item.name.startsWith(deferredSymbol)),
    [deferredSymbol, mpDataStatusQuery.data],
  );

  const journalRows = useMemo<ReactNode[][]>(() => (
    (paperJournalQuery.data?.records ?? []).slice(0, 8).map((record) => [
      <div key={`${record.recorded_at}:${record.trading_symbol ?? record.symbol}`} className="space-y-1">
        <div className="font-medium text-slate-100">{record.trading_symbol ?? record.symbol}</div>
        <div className="text-xs text-slate-500">{new Date(record.recorded_at).toLocaleString("en-IN")}</div>
      </div>,
      <div key={`${record.recorded_at}:side`} className="flex flex-col gap-1">
        <span className={clsx("inline-flex w-fit rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em]", toneForAction(record.action))}>
          {record.action}
        </span>
        <span className="text-xs text-slate-500">{record.agent_name} · {record.execution_style ?? "plan"}</span>
      </div>,
      <div key={`${record.recorded_at}:premium`} className="space-y-1">
        <div className="font-mono text-slate-100">{formatPrice(record.premium)}</div>
        <div className="text-xs text-slate-500">{record.moneyness ?? "—"} · DTE {record.days_to_expiry ?? "—"}</div>
      </div>,
      <div key={`${record.recorded_at}:context`} className="space-y-1">
        <div className="font-mono text-slate-100">{formatPct(record.confidence, 0)}</div>
        <div className="text-xs text-slate-500">{record.regime}</div>
      </div>,
    ])
  ), [paperJournalQuery.data?.records]);

  const shadowRows = useMemo<ReactNode[][]>(() => (
    (shadowRecordsQuery.data?.records ?? []).slice(0, 8).map((record) => {
      const setupName = record.setup_name ?? record.metadata?.decision_metadata?.setup_name ?? "setup_pending";
      return [
        <div key={record.signal_id ?? record.record_id ?? record.session_date} className="space-y-1">
          <div className="font-medium text-slate-100">{record.session_date}</div>
          <div className="text-xs text-slate-500">{record.symbol}</div>
        </div>,
        <div key={`${record.signal_id}:side`} className="flex flex-col gap-1">
          <span className={clsx("inline-flex w-fit rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em]", toneForAction(record.action))}>
            {record.action}
          </span>
          <span className="text-xs text-slate-500">{record.agent_name}</span>
        </div>,
        <div key={`${record.signal_id}:setup`} className="space-y-1">
          <div className="font-mono text-slate-100">{setupName}</div>
          <div className="text-xs text-slate-500">{record.regime_label ?? "—"}</div>
        </div>,
        <div key={`${record.signal_id}:drift`} className="space-y-1">
          <div className="font-mono text-slate-100">{record.fill_drift_ticks ?? "—"}</div>
          <div className="text-xs text-slate-500">ticks drift</div>
        </div>,
      ];
    })
  ), [shadowRecordsQuery.data?.records]);

  const openPositionRows = useMemo<ReactNode[][]>(() => (
    (paperPositionsQuery.data?.open_positions ?? []).slice(0, 6).map((position) => [
      <div key={position.position_id} className="space-y-1">
        <div className="font-medium text-slate-100">{position.trading_symbol ?? "—"}</div>
        <div className="text-xs text-slate-500">{new Date(position.opened_at).toLocaleString("en-IN")}</div>
      </div>,
      <div key={`${position.position_id}:signal`} className="flex flex-col gap-1">
        <span className={clsx("inline-flex w-fit rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em]", toneForAction(position.signal_action))}>
          {position.signal_action}
        </span>
        <span className="text-xs text-slate-500">{position.agent_name}</span>
      </div>,
      <div key={`${position.position_id}:premium`} className="space-y-1">
        <div className="font-mono text-slate-100">{formatPrice(position.entry_premium)} → {formatPrice(position.latest_premium)}</div>
        <div className="text-xs text-slate-500">{position.moneyness ?? "—"} · DTE {position.days_to_expiry ?? "—"}</div>
      </div>,
      <div key={`${position.position_id}:pnl`} className="space-y-1">
        <div className={clsx("font-mono", (position.unrealized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300")}>
          {formatCurrency(position.unrealized_pnl)}
        </div>
        <div className="text-xs text-slate-500">{formatPct(position.latest_confidence, 0)} conf</div>
      </div>,
    ])
  ), [paperPositionsQuery.data?.open_positions]);

  const closedPositionRows = useMemo<ReactNode[][]>(() => (
    (paperPositionsQuery.data?.closed_positions ?? []).slice(0, 6).map((position) => [
      <div key={position.position_id} className="space-y-1">
        <div className="font-medium text-slate-100">{position.trading_symbol ?? "—"}</div>
        <div className="text-xs text-slate-500">{position.closed_at ? new Date(position.closed_at).toLocaleString("en-IN") : "—"}</div>
      </div>,
      <div key={`${position.position_id}:signal`} className="flex flex-col gap-1">
        <span className={clsx("inline-flex w-fit rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em]", toneForAction(position.signal_action))}>
          {position.signal_action}
        </span>
        <span className="text-xs text-slate-500">{position.close_reason ?? "closed"}</span>
      </div>,
      <div key={`${position.position_id}:premium`} className="space-y-1">
        <div className="font-mono text-slate-100">{formatPrice(position.entry_premium)} → {formatPrice(position.exit_premium)}</div>
        <div className="text-xs text-slate-500">{position.agent_name} · qty {position.quantity}</div>
      </div>,
      <div key={`${position.position_id}:pnl`} className="space-y-1">
        <div className={clsx("font-mono", (position.realized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300")}>
          {formatCurrency(position.realized_pnl)}
        </div>
        <div className="text-xs text-slate-500">{position.option_type ?? "—"} {position.strike ? position.strike.toFixed(0) : ""}</div>
      </div>,
    ])
  ), [paperPositionsQuery.data?.closed_positions]);

  const error = validationQuery.error
    ?? paperProposal.error
    ?? summaryQuery.error
    ?? configQuery.error
    ?? gateBQuery.error
    ?? gateCQuery.error
    ?? canaryReadinessQuery.error
    ?? paperJournalQuery.error
    ?? paperPositionsQuery.error
    ?? shadowRecordsQuery.error
    ?? mpDataStatusQuery.error
    ?? mpSignalsQuery.error
    ?? mpOpenSignalQuery.error
    ?? mpAgentContextQuery.error
    ?? shadowBackfill.error;

  return (
    <div className="mx-auto max-w-[1600px] space-y-6 pb-10">
      <section
        className={sectionChrome("overflow-hidden px-6 py-6 md:px-8")}
        style={{
          backgroundImage:
            "radial-gradient(circle at top left, rgba(34,197,94,0.18), transparent 28%), radial-gradient(circle at top right, rgba(56,189,248,0.18), transparent 24%), radial-gradient(circle at bottom right, rgba(245,158,11,0.16), transparent 22%), linear-gradient(180deg, rgba(11,18,30,0.96), rgba(6,10,20,0.99))",
        }}
      >
        <div className="flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="max-w-4xl space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-slate-400">
              <Layers3 size={13} className="text-emerald-300" />
              Auction Intelligence
              <span className="text-white/20">/</span>
              {deferredMode === "live" ? "Operator console" : "Scenario lab"}
            </div>
            <h1 className="max-w-4xl font-mono text-3xl font-semibold leading-tight text-slate-100 md:text-4xl">
              Strategy metrics, persisted signals, paper ledger, and session-quality visuals in one AI trading surface.
            </h1>
            <p className="max-w-3xl text-sm leading-7 text-slate-300 md:text-base">
              This view is now organized around operator use: session structure, order-flow timing, live position context, the options execution ladder, and persisted AI artifacts instead of stacked validation debug blocks.
            </p>
          </div>

          <div className="grid gap-3 xl:w-[620px]">
            <div className="grid gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => startTransition(() => setDataMode("live"))}
                className={clsx(
                  "inline-flex items-center justify-center gap-2 rounded-2xl border px-4 py-3 text-sm font-medium transition-colors",
                  dataMode === "live"
                    ? "border-emerald-400/35 bg-emerald-400/12 text-emerald-300"
                    : "border-white/10 bg-white/5 text-slate-300 hover:border-sky-400/30 hover:text-slate-100",
                )}
              >
                Live broker snapshot
              </button>
              <button
                type="button"
                onClick={() => startTransition(() => setDataMode("demo"))}
                className={clsx(
                  "inline-flex items-center justify-center gap-2 rounded-2xl border px-4 py-3 text-sm font-medium transition-colors",
                  dataMode === "demo"
                    ? "border-sky-400/35 bg-sky-400/12 text-sky-300"
                    : "border-white/10 bg-white/5 text-slate-300 hover:border-sky-400/30 hover:text-slate-100",
                )}
              >
                Demo scenario
              </button>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-2">
                <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Underlying</div>
                <select
                  value={symbol}
                  onChange={(event) => startTransition(() => setSymbol(event.target.value))}
                  className="w-full rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-100 outline-none transition-colors focus:border-sky-400"
                >
                  {symbols.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              {dataMode === "demo" ? (
                <label className="space-y-2">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Scenario</div>
                  <select
                    value={scenario}
                    onChange={(event) => startTransition(() => setScenario(event.target.value))}
                    className="w-full rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-100 outline-none transition-colors focus:border-sky-400"
                  >
                    {scenarios.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Broker sessions</div>
                  <div className="mt-2 text-sm text-slate-100">
                    {summary?.connected_brokers?.length
                      ? summary.connected_brokers.join(", ")
                      : liveReady
                        ? "connected"
                        : "No broker session detected"}
                  </div>
                </div>
              )}
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <button
                type="button"
                onClick={() => validationQuery.refetch()}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-slate-100 transition-colors hover:border-sky-400/30 hover:bg-sky-400/10"
              >
                {validationQuery.isFetching || isPending ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
                Refresh
              </button>
              <button
                type="button"
                disabled={!payload || paperProposal.isPending}
                onClick={() => paperProposal.mutate()}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-emerald-400/30 bg-emerald-400/12 px-4 py-3 text-sm font-medium text-emerald-300 transition-colors hover:bg-emerald-400/18 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {paperProposal.isPending ? <Loader2 size={16} className="animate-spin" /> : <ArrowRight size={16} />}
                Write ledger
              </button>
              <button
                type="button"
                disabled={deferredMode !== "live" || shadowBackfill.isPending}
                onClick={() => shadowBackfill.mutate()}
                className="inline-flex items-center justify-center gap-2 rounded-2xl border border-amber-400/30 bg-amber-400/12 px-4 py-3 text-sm font-medium text-amber-300 transition-colors hover:bg-amber-400/18 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {shadowBackfill.isPending ? <Loader2 size={16} className="animate-spin" /> : <Database size={16} />}
                Shadow sync
              </button>
            </div>
          </div>
        </div>
      </section>

      {error ? (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm text-rose-300">
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div>
              <div className="font-semibold text-slate-100">Auction Intelligence request failed</div>
              <div className="mt-1 leading-6 text-slate-400">{String(error)}</div>
            </div>
          </div>
        </section>
      ) : null}

      {paperProposal.data?.journal_paths?.length ? (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm">
            <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-emerald-300" />
            <div>
              <div className="font-semibold text-slate-100">Paper ledger updated</div>
              <div className="mt-1 leading-6 text-slate-400">{paperProposal.data.journal_paths.join(", ")}</div>
            </div>
          </div>
        </section>
      ) : null}

      {shadowBackfill.data?.storage?.persisted ? (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm">
            <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-emerald-300" />
            <div>
              <div className="font-semibold text-slate-100">Shadow records refreshed</div>
              <div className="mt-1 leading-6 text-slate-400">
                {shadowBackfill.data.record_count} records across {shadowBackfill.data.snapshot_count} snapshots for {shadowBackfill.data.symbol_code}.
              </div>
            </div>
          </div>
        </section>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-6">
        <KpiCard
          label="Regime"
          value={analysis?.regime.label?.replaceAll("_", " ") ?? "—"}
          detail={analysis ? `Confidence ${formatPct(analysis.regime.confidence, 0)} · ${analysis.regime.allowed_directions.join(", ") || "no direction"}` : "Waiting for the latest session analysis."}
          tone={analysis ? toneForStatus(analysis.regime.label.includes("trend") ? "ok" : analysis.regime.label.includes("failed") ? "warning" : "info") : "text-slate-100"}
        />
        <KpiCard
          label="Timing"
          value={analysis ? formatPct(analysis.order_flow.timing_confidence, 0) : "—"}
          detail={analysis ? `${analysis.order_flow.execution_aggression} execution bias · toxicity ${formatPct(analysis.order_flow.toxicity_score ?? 0, 0)}` : "Waiting for live or demo tape."}
        />
        <KpiCard
          label="Risk State"
          value={analysis?.risk.kill_switch ? "Kill Switch" : analysis?.risk.allowed ? "Risk Clear" : "Blocked"}
          detail={analysis?.risk.reasons?.[0] ?? "Risk decision unavailable."}
          tone={analysis?.risk.kill_switch ? "text-rose-300" : analysis?.risk.allowed ? "text-emerald-300" : "text-amber-300"}
        />
        <KpiCard
          label="Open Positions"
          value={String(paperPositionsQuery.data?.summary.open_count ?? 0)}
          detail={paperPositionsQuery.data ? `AI book unrealized ${formatCurrency(paperPositionsQuery.data.summary.unrealized_pnl)}` : "Persisted AI paper positions unavailable."}
        />
        <KpiCard
          label="Persisted Signals"
          value={deferredMode === "live" ? String(shadowRecordsQuery.data?.count ?? 0) : "—"}
          detail={deferredMode === "live" ? `${gateC?.metrics.signal_count ?? 0} Gate C signals in window` : "Shadow tape is only tracked on live mode."}
        />
        <KpiCard
          label="Paper Ledger"
          value={String(paperJournalQuery.data?.total_records ?? 0)}
          detail={paperPositionsQuery.data ? `Closed ${paperPositionsQuery.data.summary.closed_count} · realized ${formatCurrency(paperPositionsQuery.data.summary.realized_pnl)}` : (paperJournalQuery.data?.summary.latest_recorded_at ? `Last write ${new Date(paperJournalQuery.data.summary.latest_recorded_at).toLocaleString("en-IN")}` : "No paper entries written yet.")}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<CandlestickChart size={14} className="text-sky-300" />}
            eyebrow="Market Profile Day Structure"
            title={payload?.scenario_label ?? "Loading session"}
            detail={request?.metadata.snapshot_mode ? `Session ${new Date(request.session.session_date).toLocaleDateString("en-IN")} · ${request.metadata.snapshot_mode.replaceAll("_", " ")} · ${request.metadata.history_source ?? "local cache"}` : "Waiting for a validation payload."}
            action={analysis ? <StatusPill label={analysis.regime.label} className={toneForRegime(analysis.regime.label)} /> : null}
          />

          <div className="mt-5 grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
            <div className="h-[420px] rounded-[26px] border border-white/8 bg-black/20 p-3">
              {profileRows.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={profileRows} margin={{ top: 18, right: 18, left: 4, bottom: 10 }}>
                    <defs>
                      <linearGradient id="mpCloseFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgba(56,189,248,0.42)" />
                        <stop offset="100%" stopColor="rgba(56,189,248,0.02)" />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                    {analysis ? (
                      <ReferenceArea y1={analysis.market_profile.val} y2={analysis.market_profile.vah} fill="rgba(59,130,246,0.10)" />
                    ) : null}
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis yAxisId="price" domain={mpDomain} tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} width={72} />
                    <YAxis yAxisId="volume" orientation="right" tick={{ fill: "#64748b", fontSize: 11 }} axisLine={false} tickLine={false} width={56} />
                    <Tooltip
                      contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                      formatter={(value: number, name: string) => {
                        if (name === "close" || name === "volume") {
                          return [name === "volume" ? formatCompact(Number(value)) : formatPrice(Number(value)), name];
                        }
                        return [formatPrice(Number(value)), name];
                      }}
                    />
                    {analysis ? (
                      <>
                        <ReferenceLine y={analysis.market_profile.poc} yAxisId="price" stroke="#34d399" strokeDasharray="5 4" />
                        <ReferenceLine y={analysis.market_profile.vah} yAxisId="price" stroke="#60a5fa" strokeDasharray="3 4" />
                        <ReferenceLine y={analysis.market_profile.val} yAxisId="price" stroke="#60a5fa" strokeDasharray="3 4" />
                        <ReferenceLine y={analysis.market_profile.initial_balance_high} yAxisId="price" stroke="#f59e0b" strokeDasharray="2 4" />
                        <ReferenceLine y={analysis.market_profile.initial_balance_low} yAxisId="price" stroke="#f59e0b" strokeDasharray="2 4" />
                      </>
                    ) : null}
                    <Bar yAxisId="volume" dataKey="volume" barSize={16} fill="rgba(96,165,250,0.20)" radius={[6, 6, 0, 0]} />
                    <Area yAxisId="price" type="monotone" dataKey="close" stroke="#e2e8f0" strokeWidth={2.4} fill="url(#mpCloseFill)" dot={false} activeDot={{ r: 4, fill: "#34d399" }} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-slate-400">
                  {validationQuery.isFetching ? "Loading session structure…" : "No session bars available."}
                </div>
              )}
            </div>

            <div className="grid gap-3">
              <MetricRow
                label="POC / Value"
                value={analysis ? `${formatPrice(analysis.market_profile.poc)} · ${formatPrice(analysis.market_profile.val)} / ${formatPrice(analysis.market_profile.vah)}` : "—"}
                hint="Primary value ladder used for acceptance and rejection reads."
              />
              <MetricRow
                label="Initial Balance"
                value={analysis ? `${formatPrice(analysis.market_profile.initial_balance_low)} → ${formatPrice(analysis.market_profile.initial_balance_high)}` : "—"}
                hint="First-hour auction bracket used for day-type and extension logic."
              />
              <MetricRow
                label="Range Extension"
                value={analysis ? `${formatSigned(analysis.market_profile.range_extension_up)} / ${formatSigned(-analysis.market_profile.range_extension_down)}` : "—"}
                hint="Directional extension beyond the initial balance."
              />
              <MetricRow
                label="Day Range"
                value={analysis ? formatPrice(analysis.market_profile.day_range) : "—"}
                hint={`Spike ${analysis?.market_profile.spike_direction ?? "none"} · bracket ${analysis?.market_profile.bracket_state ?? "—"}`}
              />
              <MetricRow
                label="Session Price"
                value={request ? formatPrice(request.session.last_price) : "—"}
                hint={request ? `${request.session.minutes_to_close} min to close · stale ${request.session.stale_data_seconds.toFixed(1)}s` : "No live session meta."}
              />
            </div>
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Radar size={14} className="text-emerald-300" />}
            eyebrow="Order Flow"
            title="Microstructure and timing"
            detail="The top chart tracks mid and micro price through the snapshot tape. The lower chart shows signed trade pressure, top-book imbalance, and spread expansion on aligned scale."
          />

          <div className="mt-5 space-y-4">
            <div className="h-[220px] rounded-[26px] border border-white/8 bg-black/20 p-3">
              {orderFlowRows.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={orderFlowRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="flowPriceFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgba(52,211,153,0.22)" />
                        <stop offset="100%" stopColor="rgba(52,211,153,0.01)" />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis yAxisId="price" domain={flowPriceDomain} tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} width={72} />
                    <Tooltip
                      contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                      formatter={(value: number) => [formatPrice(Number(value)), "price"]}
                    />
                    <Area yAxisId="price" type="monotone" dataKey="mid" stroke="#e2e8f0" strokeWidth={1.8} fill="url(#flowPriceFill)" dot={false} />
                    <Line yAxisId="price" type="monotone" dataKey="micro" stroke="#34d399" strokeWidth={2.2} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-slate-400">No order-flow tape available.</div>
              )}
            </div>

            <div className="h-[170px] rounded-[26px] border border-white/8 bg-black/20 p-3">
              {orderFlowRows.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={orderFlowRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                    <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis yAxisId="delta" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} width={60} />
                    <YAxis yAxisId="ratio" orientation="right" domain={[-1, 1]} tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={44} />
                    <Tooltip
                      contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                    />
                    <Bar yAxisId="delta" dataKey="tradeDelta" barSize={14} radius={[4, 4, 0, 0]}>
                      {orderFlowRows.map((row) => (
                        <Cell key={row.timestamp} fill={row.tradeDelta >= 0 ? "rgba(52,211,153,0.58)" : "rgba(244,63,94,0.58)"} />
                      ))}
                    </Bar>
                    <Line yAxisId="ratio" type="monotone" dataKey="topImbalance" stroke="#38bdf8" strokeWidth={2.1} dot={false} />
                    <Line yAxisId="delta" type="monotone" dataKey="cumulativeDelta" stroke="#f59e0b" strokeWidth={1.8} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-slate-400">No aligned quote or trade tape available.</div>
              )}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <MetricRow label="Trade imbalance" value={analysis ? formatPct(analysis.order_flow.trade_imbalance ?? 0, 0) : "—"} hint="Signed aggressive flow across the tape." />
              <MetricRow label="Order-flow imbalance" value={analysis ? formatPct(analysis.order_flow.order_flow_imbalance ?? 0, 0) : "—"} hint="Best-book replenishment skew from quote updates." />
              <MetricRow label="Book pressure" value={analysis ? formatSigned(analysis.order_flow.book_pressure ?? 0, 2) : "—"} hint="Composite pressure combining queue and book skew." />
              <MetricRow label="Micro-price offset" value={analysis ? `${formatSigned(analysis.order_flow.micro_price_offset_bps ?? 0, 2)} bps` : "—"} hint="Micro versus mid displacement." />
              <MetricRow label="Quote repricing" value={analysis ? formatPrice(analysis.order_flow.quote_repricing_rate ?? 0, 2) : "—"} hint="Book update pace used in timing confidence." />
              <MetricRow label="Execution bias" value={analysis?.order_flow.execution_aggression ?? "—"} hint={`Passive fill ${formatPct(analysis?.order_flow.passive_fill_probability, 0)} · aggressive fill ${formatPct(analysis?.order_flow.aggressive_fill_probability, 0)}`} />
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Activity size={14} className="text-violet-300" />}
            eyebrow="NTM VolX"
            title="Near-the-money option control"
            detail="Public Vtrender material frames NTM VolX as a near-the-money call-versus-put control lens. This implementation uses a transparent proxy from premium turnover, positive OI change, and liquidity quality across the nearest strike pairs."
            action={analysis?.ntm_volx ? <StatusPill label={analysis.ntm_volx.regime} className={toneForAction(analysis.ntm_volx.directional_bias)} /> : null}
          />

          <div className="mt-5 h-[300px] rounded-[26px] border border-white/8 bg-black/20 p-3">
            {ntmVolxRows.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={ntmVolxRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                  <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                  <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} />
                  <YAxis yAxisId="pressure" domain={ntmPressureDomain} tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} width={72} />
                  <YAxis yAxisId="ratio" orientation="right" domain={[-1, 1]} tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={44} />
                  <ReferenceLine yAxisId="pressure" y={0} stroke="rgba(255,255,255,0.12)" />
                  <ReferenceLine yAxisId="ratio" y={0} stroke="rgba(255,255,255,0.12)" strokeDasharray="4 4" />
                  <Tooltip
                    contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                    formatter={(value: number, name: string) => {
                      if (name === "Net pressure") return [formatPct(Number(value), 0), name];
                      return [formatCompact(Math.abs(Number(value))), name];
                    }}
                  />
                  <Bar yAxisId="pressure" dataKey="callPressure" name="Call pressure" barSize={14} radius={[6, 6, 0, 0]}>
                    {ntmVolxRows.map((row) => (
                      <Cell key={`call-${row.strike}`} fill="rgba(52,211,153,0.58)" />
                    ))}
                  </Bar>
                  <Bar yAxisId="pressure" dataKey="putPressureSigned" name="Put pressure" barSize={14} radius={[0, 0, 6, 6]}>
                    {ntmVolxRows.map((row) => (
                      <Cell key={`put-${row.strike}`} fill="rgba(244,63,94,0.58)" />
                    ))}
                  </Bar>
                  <Line yAxisId="ratio" type="monotone" dataKey="netPressure" name="Net pressure" stroke="#c084fc" strokeWidth={2.2} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-slate-400">
                NTM VolX is unavailable for this snapshot because no option chain was attached.
              </div>
            )}
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Gauge size={14} className="text-violet-300" />}
            eyebrow="VolX Read"
            title="Pressure summary"
            detail="`VXR` is the dominant-side pressure ratio: 1.0 is balanced, higher values mean one side is pressing materially harder."
          />

          <div className="mt-5 grid gap-3">
            <MetricRow
              label="Dominant side"
              value={analysis?.ntm_volx ? `${analysis.ntm_volx.dominant_side} · ${analysis.ntm_volx.directional_bias}` : "—"}
              hint={analysis?.ntm_volx?.notes?.[0] ?? "Waiting for an options snapshot."}
            />
            <MetricRow
              label="VXR"
              value={analysis?.ntm_volx ? analysis.ntm_volx.vxr.toFixed(2) : "—"}
              hint={analysis?.ntm_volx ? `Net pressure ${formatPct(analysis.ntm_volx.net_pressure, 0)} across ${analysis.ntm_volx.pair_count} strike pairs.` : "No near-the-money pressure ratio yet."}
            />
            <MetricRow
              label="Premium turnover"
              value={analysis?.ntm_volx ? `${formatCompact(analysis.ntm_volx.call_notional)} / ${formatCompact(analysis.ntm_volx.put_notional)}` : "—"}
              hint="Call versus put premium turnover across the selected NTM ladder."
            />
            <MetricRow
              label="Walls"
              value={analysis?.ntm_volx ? `${formatPrice(analysis.ntm_volx.call_wall_strike, 0)} / ${formatPrice(analysis.ntm_volx.put_wall_strike, 0)}` : "—"}
              hint="Highest-pressure call and put strikes inside the NTM set."
            />
            <MetricRow
              label="Open interest change"
              value={analysis?.ntm_volx ? `${formatCompact(analysis.ntm_volx.call_oi_change)} / ${formatCompact(analysis.ntm_volx.put_oi_change)}` : "—"}
              hint="Positive OI additions used as a demand confirmation layer."
            />
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Bot size={14} className="text-emerald-300" />}
            eyebrow="Signals"
            title="Current sleeve decisions"
            detail="The AI signal stack shows what each sleeve wants to do right now, while the overnight MP signal shows the next-session directional bias."
          />

          <div className="mt-5 space-y-4">
            <div className="grid gap-4">
              {(analysis?.agent_decisions ?? []).length > 0 ? (
                (analysis?.agent_decisions ?? []).map((decision) => (
                  <DecisionCard key={decision.agent_name} decision={decision} />
                ))
              ) : (
                <div className="rounded-[26px] border border-white/8 bg-black/15 px-4 py-6 text-sm text-slate-400">
                  No sleeve emitted an actionable signal on this snapshot.
                </div>
              )}
            </div>

            <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Next Session MP Signal</div>
                    <div className="mt-1 text-sm font-semibold text-slate-100">{deferredSymbol}</div>
                  </div>
                  {mpOpenSignalQuery.data?.signals?.[0] ? (
                    <StatusPill
                      label={mpOpenSignalQuery.data.signals[0].direction}
                      className={toneForAction(mpOpenSignalQuery.data.signals[0].direction)}
                    />
                  ) : null}
                </div>
                {mpOpenSignalQuery.data?.signals?.length ? (
                  <div className="mt-4 space-y-3 text-sm text-slate-400">
                    <div>
                      <span className="font-semibold text-slate-100">{mpOpenSignalQuery.data.signals[0].day_type}</span>
                      {" · "}
                      {mpOpenSignalQuery.data.signals[0].reason}
                    </div>
                    <div>{mpOpenSignalQuery.data.signals[0].instruction}</div>
                    <div className="text-xs uppercase tracking-[0.18em] text-slate-500">
                      Strength {mpOpenSignalQuery.data.signals[0].strength} · allocation {formatPct(mpOpenSignalQuery.data.signals[0].alloc, 0)}
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 text-sm leading-6 text-slate-400">
                    {mpOpenSignalQuery.data?.skip_reason ?? "No next-session MP signal available."}
                  </div>
                )}
              </div>

              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Agent context</div>
                <div className="mt-4 space-y-3 text-sm text-slate-400">
                  {(mpAgentContextQuery.data ?? []).slice(0, 5).map((item) => (
                    <div key={`${item.time}:${item.type}:${item.message}`} className="flex items-start gap-2">
                      <ChevronRight size={14} className="mt-1 shrink-0 text-sky-300" />
                      <span>{item.message}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Workflow size={14} className="text-amber-300" />}
            eyebrow="Positions And Execution"
            title="Options ladder and live book context"
            detail="This is the execution side of the AI page: current portfolio context on the left, and the mapped CE/PE option instructions on the right."
          />

          <div className="mt-5 grid gap-4 xl:grid-cols-[0.8fr_1.2fr]">
            <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
              <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Portfolio snapshot</div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-white/8 bg-white/5 p-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Net liq</div>
                  <div className="mt-2 text-lg font-semibold text-slate-100">{formatCurrency(request?.portfolio.net_liquidation)}</div>
                </div>
                <div className="rounded-2xl border border-white/8 bg-white/5 p-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Daily realized</div>
                  <div className={clsx("mt-2 text-lg font-semibold", (request?.portfolio.daily_realized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300")}>
                    {request ? formatCurrency(request.portfolio.daily_realized_pnl) : "—"}
                  </div>
                </div>
              </div>
              <div className="mt-4 space-y-4">
                {Object.entries(request?.portfolio.symbol_exposure ?? {}).map(([key, value]) => (
                  <ExposureBar key={key} label={key} value={value} scale={maxExposure} />
                ))}
                {Object.entries(request?.portfolio.agent_drawdowns ?? {}).map(([key, value]) => (
                  <ExposureBar key={key} label={`${key} drawdown`} value={-Math.abs(value)} scale={0.1} accent="bg-amber-400" />
                ))}
              </div>
              <div className="mt-4 rounded-2xl border border-white/8 bg-white/5 p-3 text-sm text-slate-400">
                Max symbol exposure {formatPct(config?.risk.max_symbol_exposure, 0)} · max correlated exposure {formatPct(config?.risk.max_correlated_exposure, 0)}.
              </div>
              <div className="mt-4 rounded-[26px] border border-white/8 bg-white/5 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">AI paper book</div>
                  {paperPositionsQuery.isFetching ? <Loader2 size={14} className="animate-spin text-slate-500" /> : null}
                </div>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                    <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Open</div>
                    <div className="mt-2 text-lg font-semibold text-slate-100">{paperPositionsQuery.data?.summary.open_count ?? 0}</div>
                    <div className="mt-1 text-xs text-slate-500">Unrealized {formatCurrency(paperPositionsQuery.data?.summary.unrealized_pnl)}</div>
                  </div>
                  <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                    <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Closed</div>
                    <div className="mt-2 text-lg font-semibold text-slate-100">{paperPositionsQuery.data?.summary.closed_count ?? 0}</div>
                    <div className="mt-1 text-xs text-slate-500">Realized {formatCurrency(paperPositionsQuery.data?.summary.realized_pnl)}</div>
                  </div>
                </div>
                <div className="mt-3 text-xs leading-5 text-slate-500">
                  Last AI sync {paperPositionsQuery.data?.summary.last_synced_at ? new Date(paperPositionsQuery.data.summary.last_synced_at).toLocaleString("en-IN") : "not available"}.
                </div>
              </div>
            </div>

            <div className="space-y-4">
              {(analysis?.execution_plan ?? []).length ? (
                (analysis?.execution_plan ?? []).map((step) => (
                  <ExecutionCard key={`${step.agent_name}-${step.trading_symbol ?? step.symbol}-${step.action}`} step={step} />
                ))
              ) : (
                <div className="rounded-[26px] border border-white/8 bg-black/15 px-4 py-6 text-sm text-slate-400">
                  No executable option ladder was produced for the current session.
                </div>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.05fr_0.95fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<WalletCards size={14} className="text-emerald-300" />}
            eyebrow="Persisted Ledger"
            title="Paper journal and AI position book"
            detail="The journal keeps every AI paper proposal. The position book keeps actual open and closed AI option positions with lifecycle state."
            action={paperJournalQuery.isFetching ? <Loader2 size={16} className="animate-spin text-slate-500" /> : null}
          />
          <div className="mt-5 space-y-4">
            <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
              <PersistedTable
                headers={["Contract", "Side", "Premium", "Context"]}
                rows={journalRows}
                empty="No AI paper entries have been written for this symbol yet."
              />
              <div className="grid gap-3">
                <MetricRow
                  label="Entries"
                  value={String(paperJournalQuery.data?.total_records ?? 0)}
                  hint="Persisted paper intents available for this underlying."
                />
                <MetricRow
                  label="Average confidence"
                  value={paperJournalQuery.data?.summary.avg_confidence !== null && paperJournalQuery.data?.summary.avg_confidence !== undefined ? formatPct(paperJournalQuery.data.summary.avg_confidence, 0) : "—"}
                  hint="Mean AI confidence over persisted paper entries."
                />
                <MetricRow
                  label="Average premium"
                  value={paperJournalQuery.data?.summary.avg_premium !== null && paperJournalQuery.data?.summary.avg_premium !== undefined ? formatPrice(paperJournalQuery.data.summary.avg_premium) : "—"}
                  hint="Average mapped option premium in the journal."
                />
                <MetricRow
                  label="Action mix"
                  value={Object.entries(paperJournalQuery.data?.summary.action_breakdown ?? {}).map(([key, value]) => `${key}:${value}`).join(" · ") || "—"}
                  hint="Direction distribution across persisted AI paper entries."
                />
              </div>
            </div>
            <div className="grid gap-4 xl:grid-cols-2">
              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Open AI positions</div>
                  <div className="text-xs text-slate-500">{paperPositionsQuery.data?.summary.open_count ?? 0} active</div>
                </div>
                <PersistedTable
                  headers={["Contract", "Signal", "Premium", "Unrealized"]}
                  rows={openPositionRows}
                  empty="No persisted AI positions are currently open."
                />
              </div>
              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Closed AI positions</div>
                  <div className="text-xs text-slate-500">{paperPositionsQuery.data?.summary.closed_count ?? 0} closed</div>
                </div>
                <PersistedTable
                  headers={["Contract", "Reason", "Premium", "Realized"]}
                  rows={closedPositionRows}
                  empty="No persisted AI positions have closed yet."
                />
              </div>
            </div>
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Activity size={14} className="text-sky-300" />}
            eyebrow="Persisted Signal Tape"
            title="Shadow observations and overnight profile context"
            detail="Gate C shadow records and MP day signals live together here so the operator can cross-check signal quality against persisted structure."
          />
          <div className="mt-5 grid gap-4">
            <PersistedTable
              headers={["Session", "Signal", "Setup", "Drift"]}
              rows={shadowRows}
              empty={deferredMode === "live" ? "No persisted shadow observations found for this symbol." : "Shadow observations are only available on the live broker path."}
            />
            <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Recent MP day signals</div>
                <div className="mt-4 space-y-3">
                  {(mpSignalsQuery.data?.signals ?? []).slice(-4).reverse().map((signal) => (
                    <div key={`${signal.date}:${signal.day_type}`} className="rounded-2xl border border-white/8 bg-white/5 p-3">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-slate-100">{signal.date}</div>
                          <div className="text-xs uppercase tracking-[0.18em] text-slate-500">{signal.day_type}</div>
                        </div>
                        <StatusPill label={signal.direction} className={toneForAction(signal.direction)} />
                      </div>
                      <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400">
                        <span>Buyer fail {signal.buyer_fail.toFixed(1)}</span>
                        <span>Seller fail {signal.seller_fail.toFixed(1)}</span>
                        <span>Move {formatSigned(signal.daily_move, 0)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-[26px] border border-white/8 bg-black/15 p-4">
                <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Snapshot and data health</div>
                <div className="mt-4 space-y-3">
                  <div className="rounded-2xl border border-white/8 bg-white/5 p-3 text-sm text-slate-400">
                    {validationQuery.isShowingSnapshot
                      ? "Showing the last persisted client snapshot because the latest fetch failed."
                      : validationQuery.snapshotSavedAt
                        ? `Last successful snapshot saved at ${new Date(validationQuery.snapshotSavedAt).toLocaleString("en-IN")}.`
                        : "No snapshot has been persisted in the browser yet."}
                  </div>
                  {dataSources.map((source) => (
                    <div key={source.name} className="flex items-start justify-between gap-3 rounded-2xl border border-white/8 bg-white/5 px-3 py-3 text-sm">
                      <div>
                        <div className="font-medium text-slate-100">{source.name}</div>
                        <div className="mt-1 text-xs leading-5 text-slate-400">{source.detail}</div>
                      </div>
                      <div className="text-right">
                        <div className={clsx("font-mono uppercase", toneForStatus(source.status))}>{source.status}</div>
                        <div className="mt-1 text-xs text-slate-500">{source.last_date}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-3">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Gauge size={14} className="text-sky-300" />}
            eyebrow="Strategy Metrics"
            title="Gate B replay quality"
            detail="Backtest and forward-style replay quality for the active symbol family."
          />
          <div className="mt-5">
            <GateSummaryCard
              gate={gateB}
              title="Rule-engine expectancy"
              statusDetail={gateB?.series_metadata?.source ? `${gateB.series_metadata.source} · ${gateB.series_metadata.session_count ?? 0} sessions` : "Waiting for Gate B validation."}
              metrics={[
                {
                  label: "Expectancy",
                  value: gateB ? formatSigned(Number(gateB.metrics.expectancy ?? 0)) : "—",
                  hint: "Net expectancy per evaluated trade.",
                },
                {
                  label: "Profit factor",
                  value: gateB ? String(gateB.metrics.profit_factor ?? "—") : "—",
                  hint: "Gross winners divided by gross losers.",
                },
                {
                  label: "Trades",
                  value: gateB ? String(gateB.metrics.evaluated_trades ?? "0") : "—",
                  hint: "Evaluated trades in the validation window.",
                },
                {
                  label: "Positive windows",
                  value: gateB ? formatPct(Number(gateB.metrics.positive_window_ratio ?? 0), 0) : "—",
                  hint: "Share of windows that closed net positive.",
                },
              ]}
            />
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<ShieldCheck size={14} className="text-amber-300" />}
            eyebrow="Signal Metrics"
            title="Gate C shadow quality"
            detail="Promotion-grade signal quality metrics from the persisted shadow tape."
          />
          <div className="mt-5">
            <GateSummaryCard
              gate={gateC}
              title="Observed shadow stability"
              statusDetail={deferredMode === "live" ? (gateC?.series_metadata?.symbol ? `${gateC.series_metadata.symbol} · ${gateC.series_metadata.record_count ?? 0} records` : "Waiting for Gate C validation.") : "Gate C is only available on the live route."}
              metrics={[
                {
                  label: "Signals",
                  value: gateC ? String(gateC.metrics.signal_count ?? "0") : "—",
                  hint: "Non-flat signals captured in shadow mode.",
                },
                {
                  label: "Median drift",
                  value: gateC ? String(gateC.metrics.fill_drift_median_ticks ?? "—") : "—",
                  hint: "Median fill drift in ticks.",
                },
                {
                  label: "P95 drift",
                  value: gateC ? String(gateC.metrics.fill_drift_p95_ticks ?? "—") : "—",
                  hint: "Tail fill drift used to block promotion.",
                },
                {
                  label: "Sessions",
                  value: gateC ? String(gateC.metrics.session_count ?? "0") : "—",
                  hint: "Observed shadow sessions inside the validation window.",
                },
              ]}
            />
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<TrendingUp size={14} className="text-emerald-300" />}
            eyebrow="Readiness"
            title="Canary and operating limits"
            detail="Live rollout constraints stay visible on the same page as signals and ledger."
          />
          <div className="mt-5 grid gap-3">
            <MetricRow
              label="Canary"
              value={deferredMode === "live" ? (canaryReadiness?.ready ? "Ready" : "Blocked") : "Demo"}
              hint={deferredMode === "live" ? (canaryReadiness?.blockers?.[0] ?? canaryReadiness?.next_step ?? "Waiting for canary evaluation.") : "Canary checks are hidden in demo mode."}
            />
            <MetricRow
              label="Max lots"
              value={deferredMode === "live" ? String(canaryReadiness?.requirements.max_live_lots ?? "—") : "—"}
              hint="Maximum live lots permitted for the canary route."
            />
            <MetricRow
              label="Daily loss limit"
              value={deferredMode === "live" ? formatCurrency(canaryReadiness?.requirements.daily_loss_limit) : "—"}
              hint="Live loss governor before all AI execution is blocked."
            />
            <MetricRow
              label="Stale data budget"
              value={config ? `${config.risk.stale_data_seconds}s` : "—"}
              hint={`Latency budget ${config ? `${config.mvp_scope.latency_budget_ms}ms` : "—"} · slippage ${config ? `${config.paper_trading.slippage_bps} bps` : "—"}`}
            />
          </div>
        </div>
      </section>

      <section className={sectionChrome("px-5 py-4")}>
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-3">
            <TimerReset size={16} className="mt-0.5 shrink-0 text-emerald-300" />
            <div className="text-sm text-slate-400">
              <div className="font-semibold text-slate-100">Page state</div>
              <div className="mt-1 leading-6">
                {validationQuery.isFetching
                  ? "Refreshing the current AI session snapshot."
                  : paperProposal.isPending
                    ? "Writing a new paper ledger entry."
                    : shadowBackfill.isPending
                      ? "Refreshing shadow records from live history."
                      : deferredMode === "live"
                        ? "Live AI console is ready."
                        : "Demo AI console is ready."}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-slate-500">
            <span>{request?.metadata.instrument_proxy ?? "snapshot"}</span>
            <span className="text-white/15">/</span>
            <span>{request?.metadata.quote_source ?? "quote feed"}</span>
            <span className="text-white/15">/</span>
            <span>{request?.metadata.order_flow_source ?? "order flow"}</span>
          </div>
        </div>
      </section>
    </div>
  );
}
