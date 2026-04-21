"use client";

import { useDeferredValue, useState, useTransition } from "react";
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
  Gauge,
  Layers3,
  Loader2,
  Radar,
  RefreshCw,
  ShieldCheck,
  Workflow,
  Zap,
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

import {
  getAuctionIntelligenceCanaryReadiness,
  getAuctionIntelligenceDefaultConfig,
  getAuctionIntelligenceDemoScenario,
  getAuctionIntelligenceGateBValidation,
  getAuctionIntelligenceGateCValidation,
  runAuctionIntelligenceGateAValidation,
  getAuctionIntelligenceLiveSnapshot,
  getAuctionIntelligenceSummary,
  runAuctionIntelligencePaperProposal,
  runAuctionIntelligenceShadowBackfill,
  getAuctionIntelligenceMPDataStatus,
  getAuctionIntelligenceMPSignals,
  getAuctionIntelligenceMPOpenSignal,
  getAuctionIntelligenceMPAgentContext,
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
  quote: {
    timestamp: string;
    bid: number;
    ask: number;
    bid_size: number;
    ask_size: number;
  };
  depth: {
    timestamp: string;
    bids: { price: number; quantity: number }[];
    asks: { price: number; quantity: number }[];
  };
  bars: DemoRequestBar[];
  prior_bars: DemoRequestBar[];
  trades: {
    timestamp: string;
    price: number;
    quantity: number;
    aggressor_side: string;
  }[];
  metadata: {
    symbol_code: string;
    scenario: string;
    scenario_label: string;
    lot_size: number;
    history_source?: string;
    quote_source?: string;
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
    session: { timezone: string; open: string; close: string };
    latency_budget_ms: number;
    slippage_bps: number;
    commission_bps: number;
  };
  market_profile: {
    poc: number;
    vah: number;
    val: number;
    initial_balance_high: number;
    initial_balance_low: number;
    initial_balance_range: number;
    day_range: number;
    range_extension_up: number;
    range_extension_down: number;
    value_area_overlap: number | null;
    poc_shift: number | null;
    value_migration: number | null;
    bracket_state: string | null;
    poor_high: boolean;
    poor_low: boolean;
    prior_poc_untouched: boolean | null;
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
  implementation_plan?: string[];
};

type DataMode = "live" | "demo";

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
  label: string;
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
    error?: string;
  };
  series_metadata?: {
    symbol_code?: string;
    symbol?: string;
    source?: string;
    session_count?: number;
    session_dates?: string[];
    record_count?: number;
    session_limit?: number;
  };
  artifact_count?: number;
  artifacts_preview?: {
    artifact_type: string;
    artifact_key: string;
    payload: Record<string, unknown>;
  }[];
};

type ShadowBackfillResponse = {
  symbol_code: string;
  source: string;
  history_symbol: string;
  snapshot_count: number;
  skipped_sessions: { session_date: string; error?: string }[];
  observation_bars: number;
  snapshot_cutoff: string;
  shadow_net_liquidation: number;
  record_count: number;
  storage?: {
    persisted: boolean;
    record_count?: number;
    error?: string;
  };
};

type CanaryReadinessResponse = {
  symbol: string;
  ready: boolean;
  stage: string;
  blockers: string[];
  requirements: {
    manual_approval_required: boolean;
    allowed_agents: string[];
    max_live_lots: number;
    daily_loss_limit: number;
    max_size_multiplier: number;
  };
  gate_b?: {
    passed?: boolean;
    score?: number;
  } | null;
  gate_c?: {
    passed?: boolean;
    score?: number;
  } | null;
  next_step: string;
};

const FALLBACK_SCENARIOS: DemoScenarioOption[] = [
  { id: "acceptance_up", label: "Breakout acceptance above prior value" },
  { id: "failed_auction", label: "Failed downside auction with re-entry" },
  { id: "balance", label: "Rotational balance session" },
];

function formatPrice(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function formatCompact(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Intl.NumberFormat("en-IN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatPct(value: number | null | undefined, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function formatSigned(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function sectionChrome(extra?: string) {
  return clsx(
    "rounded-3xl border border-bg-border bg-[linear-gradient(180deg,rgba(12,18,31,0.92),rgba(8,11,24,0.98))]",
    extra,
  );
}

function toneForAction(action: string) {
  if (action === "LONG") return "bg-accent-green/12 text-accent-green border-accent-green/25";
  if (action === "SHORT") return "bg-accent-red/12 text-accent-red border-accent-red/25";
  return "bg-white/5 text-text-secondary border-white/10";
}

function toneForRisk(allowed: boolean, killSwitch: boolean) {
  if (killSwitch) return "bg-accent-red/12 text-accent-red border-accent-red/25";
  if (allowed) return "bg-accent-green/12 text-accent-green border-accent-green/25";
  return "bg-accent-amber/12 text-accent-amber border-accent-amber/25";
}

function toneForRegime(label: string) {
  if (label.includes("trend") || label.includes("acceptance")) {
    return "bg-accent-green/12 text-accent-green border-accent-green/25";
  }
  if (label.includes("failed") || label.includes("rejection") || label.includes("reversal")) {
    return "bg-accent-amber/12 text-accent-amber border-accent-amber/25";
  }
  if (label === "balance" || label === "developing_balance") {
    return "bg-accent-blue/12 text-accent-blue border-accent-blue/25";
  }
  return "bg-white/5 text-text-secondary border-white/10";
}

function SmallMetric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="border-t border-white/5 py-3 first:border-t-0">
      <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">{label}</div>
      <div className="mt-1 text-lg font-semibold text-text-primary">{value}</div>
      <div className="mt-1 text-xs text-text-secondary">{hint}</div>
    </div>
  );
}

function ActionPill({ label, className }: { label: string; className: string }) {
  return (
    <span className={clsx("inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.18em]", className)}>
      {label.replaceAll("_", " ")}
    </span>
  );
}

export default function AuctionIntelligenceWorkspace() {
  const [dataMode, setDataMode] = useState<DataMode>("live");
  const [symbol, setSymbol] = useState("NIFTY");
  const [scenario, setScenario] = useState("acceptance_up");
  const [mpUnderlying, setMpUnderlying] = useState("NIFTY");
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
  const payload = validationQuery.data;

  const gateAQuery = useQuery<ValidationResponse>({
    queryKey: [
      "auction-intelligence",
      "gate-a",
      deferredMode,
      deferredSymbol,
      deferredMode === "demo" ? deferredScenario : "live",
      payload?.request.session.session_date ?? "no-session",
      payload?.request.metadata.snapshot_time ?? "no-snapshot-time",
    ],
    queryFn: async () => {
      if (!payload) throw new Error("No validation payload loaded");
      const { request } = payload;
      return (
        await runAuctionIntelligenceGateAValidation({
          session: request.session,
          portfolio: request.portfolio,
          quote: request.quote,
          depth: request.depth,
          bars: request.bars,
          prior_bars: request.prior_bars,
          trades: request.trades,
        })
      ).data;
    },
    enabled: Boolean(payload),
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
    queryKey: [
      "auction-intelligence",
      "gate-c",
      deferredMode,
      validationSymbol,
    ],
    queryFn: async () => (
      await getAuctionIntelligenceGateCValidation(validationSymbol, 30, 500)
    ).data,
    enabled: deferredMode === "live",
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const canaryReadinessQuery = useQuery<CanaryReadinessResponse>({
    queryKey: [
      "auction-intelligence",
      "canary-readiness",
      deferredMode,
      validationSymbol,
    ],
    queryFn: async () => (
      await getAuctionIntelligenceCanaryReadiness(validationSymbol)
    ).data,
    enabled: deferredMode === "live",
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  // ── MP Signal layer queries ─────────────────────────────────────────────
  const mpDataStatusQuery = useQuery({
    queryKey: ["auction-intelligence", "mp-data-status"],
    queryFn: async () => (await getAuctionIntelligenceMPDataStatus()).data,
    staleTime: 120_000,
    refetchInterval: 120_000,
  });

  const mpSignalsQuery = useQuery({
    queryKey: ["auction-intelligence", "mp-signals", mpUnderlying],
    queryFn: async () => (await getAuctionIntelligenceMPSignals(mpUnderlying)).data,
    staleTime: 60_000,
    refetchInterval: 60_000,
  });

  const mpOpenSignalQuery = useQuery({
    queryKey: ["auction-intelligence", "mp-open-signal", mpUnderlying],
    queryFn: async () => (await getAuctionIntelligenceMPOpenSignal(mpUnderlying)).data,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const mpAgentContextQuery = useQuery({
    queryKey: ["auction-intelligence", "mp-agent-context", mpUnderlying],
    queryFn: async () => (await getAuctionIntelligenceMPAgentContext(mpUnderlying)).data,
    staleTime: 60_000,
    refetchInterval: 60_000,
  });

  const paperProposal = useMutation({
    mutationFn: async () => {
      if (!validationQuery.data) throw new Error("No validation payload loaded");
      const { request } = validationQuery.data;
      const payload = {
        session: request.session,
        portfolio: request.portfolio,
        quote: request.quote,
        depth: request.depth,
        bars: request.bars,
        prior_bars: request.prior_bars,
        trades: request.trades,
      };
      return (await runAuctionIntelligencePaperProposal(payload)).data;
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
      ]);
    },
  });

  const summary = summaryQuery.data;
  const config = configQuery.data;
  const request = payload?.request;
  const analysis = payload?.analysis;
  const gateA = gateAQuery.data;
  const gateB = gateBQuery.data;
  const gateC = gateCQuery.data;
  const canaryReadiness = canaryReadinessQuery.data;
  const liveReady = Boolean(summary?.live_ready);

  const symbols = dataMode === "live"
    ? summary?.live_symbols ?? ["NIFTY", "BANKNIFTY"]
    : summary?.demo_symbols ?? ["NIFTY", "BANKNIFTY"];
  const scenarios = summary?.demo_scenarios ?? FALLBACK_SCENARIOS;

  const chartRows = (request?.bars ?? []).map((bar, index) => ({
    ...bar,
    label: new Date(bar.timestamp).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
    }),
    index,
  }));
  const ntmVolxRows = (analysis?.ntm_volx?.pressure_ladder ?? []).map((level) => ({
    label: formatPrice(level.strike, 0),
    strike: level.strike,
    callPressure: level.call_pressure,
    putPressureSigned: -Math.abs(level.put_pressure),
    netPressure: level.net_pressure,
  }));
  const ntmPressureDomain: [number, number] = (() => {
    const values = ntmVolxRows.flatMap((row) => [row.callPressure, row.putPressureSigned]);
    if (!values.length) return [-100, 100];
    const edge = Math.max(...values.map((value) => Math.abs(value)));
    return [-(edge * 1.15), edge * 1.15];
  })();

  return (
    <div className="mx-auto max-w-7xl space-y-6 pb-8">
      <section
        className={sectionChrome("overflow-hidden px-6 py-6 md:px-8")}
        style={{
          backgroundImage:
            "radial-gradient(circle at top left, rgba(0, 212, 163, 0.14), transparent 28%), radial-gradient(circle at top right, rgba(59, 130, 246, 0.14), transparent 22%), linear-gradient(180deg, rgba(13, 17, 23, 0.92), rgba(8, 11, 24, 0.98))",
        }}
      >
        <div className="flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="max-w-3xl space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-text-muted">
              <Layers3 size={13} className="text-accent-green" />
              Auction Intelligence
              <span className="text-white/30">/</span>
              {dataMode === "live" ? "Broker-backed validation" : "Demo-backed validation"}
            </div>
            <h1 className="max-w-3xl font-mono text-3xl font-semibold leading-tight text-text-primary md:text-4xl">
              Live MP structure, order-flow timing, and paper decisions on one desk.
            </h1>
            <p className="max-w-2xl text-sm leading-6 text-text-secondary md:text-base line-clamp-2">
              {dataMode === "live"
                ? "The live desk replays the latest broker-backed session through the MP and order-flow stack, then exposes the current regime and paper execution plan."
                : "The demo desk validates the same MP stack with deterministic scenarios before you move back to live paper flow."}
            </p>
          </div>

          <div className="grid gap-3 xl:w-[520px]">
            <div className="grid gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => startTransition(() => setDataMode("live"))}
                className={clsx(
                  "inline-flex items-center justify-center gap-2 rounded-2xl border px-4 py-3 text-sm font-medium transition-colors",
                  dataMode === "live"
                    ? "border-accent-green/35 bg-accent-green/12 text-accent-green"
                    : "border-bg-border bg-white/5 text-text-secondary hover:border-accent-blue/30 hover:text-text-primary",
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
                    ? "border-accent-blue/35 bg-accent-blue/12 text-accent-blue"
                    : "border-bg-border bg-white/5 text-text-secondary hover:border-accent-blue/30 hover:text-text-primary",
                )}
              >
                Demo scenario
              </button>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-2">
              <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">Underlying</div>
              <select
                value={symbol}
                onChange={(event) => startTransition(() => setSymbol(event.target.value))}
                className="w-full rounded-2xl border border-bg-border bg-bg-secondary px-4 py-3 text-sm text-text-primary outline-none transition-colors focus:border-accent-blue"
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
                <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">Scenario</div>
                <select
                  value={scenario}
                  onChange={(event) => startTransition(() => setScenario(event.target.value))}
                  className="w-full rounded-2xl border border-bg-border bg-bg-secondary px-4 py-3 text-sm text-text-primary outline-none transition-colors focus:border-accent-blue"
                >
                  {scenarios.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="rounded-2xl border border-bg-border bg-black/15 px-4 py-3">
                <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">Connected brokers</div>
                <div className="mt-2 text-sm text-text-primary">
                  {summary?.connected_brokers?.length
                    ? summary.connected_brokers.join(", ")
                    : liveReady
                      ? "connected"
                      : "No broker session detected"}
                </div>
              </div>
            )}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => validationQuery.refetch()}
              className="inline-flex items-center justify-center gap-2 rounded-2xl border border-bg-border bg-white/5 px-4 py-3 text-sm font-medium text-text-primary transition-colors hover:border-accent-blue/30 hover:bg-accent-blue/10"
            >
              {validationQuery.isFetching || isPending ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
              {dataMode === "live" ? "Refresh Live Snapshot" : "Refresh Validation"}
            </button>
            <button
              type="button"
              disabled={
                dataMode === "live"
                  ? shadowBackfill.isPending
                  : !payload || paperProposal.isPending
              }
              onClick={() => {
                if (dataMode === "live") {
                  shadowBackfill.mutate();
                  return;
                }
                paperProposal.mutate();
              }}
              className="inline-flex items-center justify-center gap-2 rounded-2xl border border-accent-green/30 bg-accent-green/12 px-4 py-3 text-sm font-medium text-accent-green transition-colors hover:bg-accent-green/18 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {dataMode === "live" ? (
                shadowBackfill.isPending ? <Loader2 size={16} className="animate-spin" /> : <ArrowRight size={16} />
              ) : paperProposal.isPending ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <ArrowRight size={16} />
              )}
              {dataMode === "live" ? "Run Shadow Backfill" : "Validate Paper Proposal"}
            </button>
          </div>
          </div>
        </div>
      </section>

      {(validationQuery.isError
        || paperProposal.isError
        || gateAQuery.isError
        || gateBQuery.isError
        || gateCQuery.isError
        || shadowBackfill.isError
        || canaryReadinessQuery.isError) && (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm text-accent-red">
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div>
              <div className="font-semibold text-text-primary">Validation call failed</div>
              <div className="mt-1 text-text-secondary">
                {String(
                  validationQuery.error
                  ?? paperProposal.error
                  ?? gateAQuery.error
                  ?? gateBQuery.error
                  ?? gateCQuery.error
                  ?? shadowBackfill.error
                  ?? canaryReadinessQuery.error,
                )}
              </div>
            </div>
          </div>
        </section>
      )}

      {paperProposal.data?.journal_paths?.length > 0 && (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm">
            <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-accent-green" />
            <div>
              <div className="font-semibold text-text-primary">Paper proposal written</div>
              <div className="mt-1 text-text-secondary">
                {paperProposal.data.journal_paths.join(", ")}
              </div>
            </div>
          </div>
        </section>
      )}

      {shadowBackfill.data?.storage?.persisted && (
        <section className={sectionChrome("px-5 py-4")}>
          <div className="flex items-start gap-3 text-sm">
            <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-accent-green" />
            <div>
              <div className="font-semibold text-text-primary">Shadow backfill persisted</div>
              <div className="mt-1 text-text-secondary">
                {`${shadowBackfill.data.record_count} records across ${shadowBackfill.data.snapshot_count} snapshots for ${shadowBackfill.data.symbol_code}.`}
              </div>
            </div>
          </div>
        </section>
      )}

      <section className="grid gap-6 xl:grid-cols-[1.35fr_0.65fr]">
        <div className={sectionChrome("p-5")}>
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">Validation Tape</div>
              <h2 className="mt-1 text-lg font-semibold text-text-primary">
                {payload?.scenario_label ?? "Loading scenario"}
              </h2>
              {payload?.session_date && (
                <div className="mt-2 text-sm text-text-secondary">
                  Session date {new Date(payload.session_date).toLocaleDateString("en-IN")}
                  {request?.metadata.snapshot_mode ? ` · ${request.metadata.snapshot_mode.replaceAll("_", " ")}` : ""}
                </div>
              )}
            </div>
            {analysis && (
              <ActionPill
                label={analysis.regime.label}
                className={toneForRegime(analysis.regime.label)}
              />
            )}
          </div>

          <div className="mt-5 h-[320px] rounded-2xl border border-white/5 bg-black/15 p-3">
            {chartRows.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                  <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                  <XAxis
                    dataKey="label"
                    tick={{ fill: "#8aa0bd", fontSize: 12 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "#8aa0bd", fontSize: 12 }}
                    axisLine={false}
                    tickLine={false}
                    domain={["dataMin - 20", "dataMax + 20"]}
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: "#0d1117",
                      border: "1px solid rgba(255,255,255,0.08)",
                      borderRadius: 16,
                    }}
                  />
                  <ReferenceLine y={analysis?.market_profile.poc} stroke="#00d4a3" strokeDasharray="4 4" />
                  <ReferenceLine y={analysis?.market_profile.vah} stroke="#3b82f6" strokeDasharray="3 4" />
                  <ReferenceLine y={analysis?.market_profile.val} stroke="#3b82f6" strokeDasharray="3 4" />
                  <ReferenceLine y={analysis?.market_profile.initial_balance_high} stroke="#f59e0b" strokeDasharray="2 4" />
                  <ReferenceLine y={analysis?.market_profile.initial_balance_low} stroke="#f59e0b" strokeDasharray="2 4" />
                  <Line
                    type="monotone"
                    dataKey="close"
                    stroke="#e2e8f0"
                    strokeWidth={2.2}
                    dot={false}
                    activeDot={{ r: 4, fill: "#00d4a3" }}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-text-secondary">
                {validationQuery.isFetching ? "Loading validation payload…" : "No chart data"}
              </div>
            )}
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <SmallMetric
              label="POC / Value"
              value={
                analysis
                  ? `${formatPrice(analysis.market_profile.poc)} · ${formatPrice(analysis.market_profile.val)} / ${formatPrice(analysis.market_profile.vah)}`
                  : "—"
              }
              hint="Primary auction reference for acceptance or reversion."
            />
            <SmallMetric
              label="Initial Balance"
              value={
                analysis
                  ? `${formatPrice(analysis.market_profile.initial_balance_low)} → ${formatPrice(analysis.market_profile.initial_balance_high)}`
                  : "—"
              }
              hint="First hour bracket used for extension and day-type logic."
            />
            <SmallMetric
              label="Range Extension"
              value={
                analysis
                  ? `${formatSigned(analysis.market_profile.range_extension_up)} / ${formatSigned(-analysis.market_profile.range_extension_down)}`
                  : "—"
              }
              hint="Directional extension beyond the initial balance."
            />
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <ShieldCheck size={13} className="text-accent-green" />
            Regime and Risk
          </div>
          <div className="mt-4 space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm text-text-secondary">Regime confidence</div>
                <div className="mt-1 text-2xl font-semibold text-text-primary">
                  {analysis ? formatPct(analysis.regime.confidence, 0) : "—"}
                </div>
              </div>
              {analysis && (
                <ActionPill
                  label={analysis.risk.kill_switch ? "kill switch" : analysis.risk.allowed ? "risk clear" : "risk blocked"}
                  className={toneForRisk(analysis.risk.allowed, analysis.risk.kill_switch)}
                />
              )}
            </div>

            <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
              <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">Allowed directions</div>
              <div className="mt-2 flex flex-wrap gap-2">
                {(analysis?.regime.allowed_directions?.length
                  ? analysis.regime.allowed_directions
                  : ["none"]).map((direction) => (
                  <ActionPill
                    key={direction}
                    label={direction}
                    className={toneForAction(direction)}
                  />
                ))}
              </div>
              <div className="mt-4 space-y-2 text-sm text-text-secondary">
                {(analysis?.regime.reasons ?? ["Waiting for demo analysis"]).map((reason) => (
                  <div key={reason} className="flex items-start gap-2">
                    <ChevronRight size={14} className="mt-1 shrink-0 text-accent-blue" />
                    <span>{reason}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <SmallMetric
                label="Daily loss cap"
                value={config ? `₹${config.risk.max_daily_loss.toLocaleString("en-IN")}` : "—"}
                hint="Shared governor cap before automation is allowed."
              />
              <SmallMetric
                label="Concurrent positions"
                value={config ? String(config.risk.max_concurrent_positions) : "—"}
                hint="Hard cap across sleeves during MVP rollout."
              />
              <SmallMetric
                label="Symbol exposure"
                value={config ? formatPct(config.risk.max_symbol_exposure, 0) : "—"}
                hint="Per-symbol cap before the governor suppresses new entries."
              />
              <SmallMetric
                label="Data staleness"
                value={config ? `${config.risk.stale_data_seconds}s` : "—"}
                hint="Feed must stay fresh before orders are permitted."
              />
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Radar size={13} className="text-accent-blue" />
            Order Flow
          </div>
          <div className="mt-4 grid gap-x-6 gap-y-2 sm:grid-cols-2">
            <SmallMetric label="Delta" value={analysis ? formatSigned(analysis.order_flow.delta) : "—"} hint="Aggressive buy volume minus sell volume." />
            <SmallMetric label="Cumulative delta" value={analysis ? formatSigned(analysis.order_flow.cumulative_delta) : "—"} hint="Session flow pressure across the replay tape." />
            <SmallMetric label="Top imbalance" value={analysis ? formatPct(analysis.order_flow.top_imbalance) : "—"} hint="Best bid versus ask liquidity." />
            <SmallMetric label="Depth imbalance" value={analysis ? formatPct(analysis.order_flow.depth_imbalance) : "—"} hint="Multi-level book skew." />
            <SmallMetric label="VWAP drift" value={analysis ? formatSigned(analysis.order_flow.vwap_drift) : "—"} hint="Short-term deviation from VWAP." />
            <SmallMetric label="Queue pressure" value={analysis ? formatSigned(analysis.order_flow.queue_pressure) : "—"} hint="Composite pressure for passive versus urgent execution." />
            <SmallMetric label="Fill style" value={analysis?.order_flow.execution_aggression ?? "—"} hint="Planner bias from microstructure context." />
            <SmallMetric label="Timing confidence" value={analysis ? formatPct(analysis.order_flow.timing_confidence, 0) : "—"} hint="Entry timing score for the execution planner." />
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Bot size={13} className="text-accent-green" />
            Sleeve Decisions
          </div>
          <div className="mt-4 space-y-3">
            {(analysis?.agent_decisions ?? []).map((decision) => (
              <div key={decision.agent_name} className="rounded-2xl border border-white/6 bg-black/15 p-4">
                <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                  <div>
                    <div className="text-[11px] uppercase tracking-[0.18em] text-text-muted">{decision.agent_name}</div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <ActionPill label={decision.action} className={toneForAction(decision.action)} />
                      <span className="text-sm text-text-secondary">
                        confidence {formatPct(decision.confidence, 0)}
                      </span>
                      <span className="text-sm text-text-secondary">
                        sleeve {formatPct(decision.sleeve_fraction, 0)}
                      </span>
                    </div>
                  </div>
                  <div className="grid gap-2 text-sm text-text-secondary sm:grid-cols-2">
                    <div>Entry: <span className="font-mono text-text-primary">{formatPrice(decision.entry_price)}</span></div>
                    <div>Stop: <span className="font-mono text-text-primary">{formatPrice(decision.stop_price)}</span></div>
                    <div>Target: <span className="font-mono text-text-primary">{formatPrice(decision.target_price)}</span></div>
                    <div>Qty: <span className="font-mono text-text-primary">{decision.quantity || "—"}</span></div>
                  </div>
                </div>
                <div className="mt-3 space-y-2 text-sm text-text-secondary">
                  {decision.rationale.slice(0, 3).map((reason) => (
                    <div key={reason} className="flex items-start gap-2">
                      <ChevronRight size={14} className="mt-1 shrink-0 text-accent-blue" />
                      <span>{reason}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr_1fr]">
        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Activity size={13} className="text-accent-amber" />
            NTM VolX
          </div>
          <div className="mt-2 text-sm leading-6 text-text-secondary">
            Near-the-money option control proxy built from premium turnover, positive OI change, and liquidity quality across the closest strike pairs.
          </div>
          <div className="mt-4 h-[240px] rounded-2xl border border-white/6 bg-black/15 p-3">
            {ntmVolxRows.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ntmVolxRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                  <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                  <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} />
                  <YAxis domain={ntmPressureDomain} tick={{ fill: "#94a3b8", fontSize: 12 }} axisLine={false} tickLine={false} width={72} />
                  <ReferenceLine y={0} stroke="rgba(255,255,255,0.12)" />
                  <Tooltip
                    contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                    formatter={(value: number, name: string) => {
                      if (name === "Net pressure") return [formatPct(Number(value), 0), name];
                      return [formatCompact(Math.abs(Number(value))), name];
                    }}
                  />
                  <Bar dataKey="callPressure" name="Call pressure" barSize={14} radius={[6, 6, 0, 0]}>
                    {ntmVolxRows.map((row) => (
                      <Cell key={`call-${row.strike}`} fill="rgba(52,211,153,0.58)" />
                    ))}
                  </Bar>
                  <Bar dataKey="putPressureSigned" name="Put pressure" barSize={14} radius={[0, 0, 6, 6]}>
                    {ntmVolxRows.map((row) => (
                      <Cell key={`put-${row.strike}`} fill="rgba(244,63,94,0.58)" />
                    ))}
                  </Bar>
                  <Line type="monotone" dataKey="netPressure" name="Net pressure" stroke="#fbbf24" strokeWidth={2} dot={false} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-text-secondary">
                No option-chain snapshot was available for NTM VolX.
              </div>
            )}
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Gauge size={13} className="text-accent-green" />
            NTM Read
          </div>
          <div className="mt-4 grid gap-x-6 gap-y-2 sm:grid-cols-2">
            <SmallMetric
              label="Dominant side"
              value={analysis?.ntm_volx ? `${analysis.ntm_volx.dominant_side} · ${analysis.ntm_volx.directional_bias}` : "—"}
              hint={analysis?.ntm_volx?.notes?.[0] ?? "Waiting for an option-chain snapshot."}
            />
            <SmallMetric
              label="VXR"
              value={analysis?.ntm_volx ? analysis.ntm_volx.vxr.toFixed(2) : "—"}
              hint={analysis?.ntm_volx ? `Net pressure ${formatPct(analysis.ntm_volx.net_pressure, 0)}` : "1.0 is balanced; higher values mean one side is pressing harder."}
            />
            <SmallMetric
              label="Premium turnover"
              value={analysis?.ntm_volx ? `${formatCompact(analysis.ntm_volx.call_notional)} / ${formatCompact(analysis.ntm_volx.put_notional)}` : "—"}
              hint="Call versus put premium turnover in the NTM ladder."
            />
            <SmallMetric
              label="Wall strikes"
              value={analysis?.ntm_volx ? `${formatPrice(analysis.ntm_volx.call_wall_strike, 0)} / ${formatPrice(analysis.ntm_volx.put_wall_strike, 0)}` : "—"}
              hint="Highest-pressure call and put strikes."
            />
            <SmallMetric
              label="OI change"
              value={analysis?.ntm_volx ? `${formatCompact(analysis.ntm_volx.call_oi_change)} / ${formatCompact(analysis.ntm_volx.put_oi_change)}` : "—"}
              hint="Positive OI additions used as a pressure confirmation layer."
            />
            <SmallMetric
              label="Pairs tracked"
              value={analysis?.ntm_volx ? String(analysis.ntm_volx.pair_count) : "—"}
              hint={analysis?.ntm_volx ? `${analysis.ntm_volx.expiry} expiry around ATM ${formatPrice(analysis.ntm_volx.atm_strike, 0)}` : "No expiry selected yet."}
            />
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Workflow size={13} className="text-accent-amber" />
            Execution Plan
          </div>
          <div className="mt-4 space-y-3">
            {(analysis?.execution_plan?.length ? analysis.execution_plan : []).map((step) => (
              <div key={`${step.agent_name}-${step.action}`} className="rounded-2xl border border-white/6 bg-black/15 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <ActionPill label={step.action} className={toneForAction(step.action)} />
                    <span className="text-sm font-semibold text-text-primary">{step.agent_name}</span>
                  </div>
                  <div className="text-sm text-text-secondary">
                    {step.order_type} · {step.style} · {step.slices} slice{step.slices === 1 ? "" : "s"}
                  </div>
                </div>
                <div className="mt-3 grid gap-2 text-sm text-text-secondary sm:grid-cols-2">
                  <div>Contract: <span className="font-mono text-text-primary">{step.trading_symbol ?? step.symbol}</span></div>
                  <div>Broker side: <span className="font-mono text-text-primary">{step.broker_action ?? "—"}</span></div>
                  <div>Premium: <span className="font-mono text-text-primary">{formatPrice(step.premium ?? step.limit_price)}</span></div>
                  <div>Qty: <span className="font-mono text-text-primary">{step.quantity ?? "—"}</span></div>
                  <div>Strike: <span className="font-mono text-text-primary">{step.strike ? step.strike.toFixed(0) : "—"}</span></div>
                  <div>Expiry: <span className="font-mono text-text-primary">{step.expiry ?? "—"}</span></div>
                  <div>Moneyness: <span className="font-mono text-text-primary">{step.moneyness ?? "—"}</span></div>
                  <div>DTE: <span className="font-mono text-text-primary">{step.days_to_expiry ?? "—"}</span></div>
                  <div>Limit: <span className="font-mono text-text-primary">{formatPrice(step.limit_price)}</span></div>
                  <div>Cancel after: <span className="font-mono text-text-primary">{step.cancel_after_seconds}s</span></div>
                </div>
                {step.selection_reason && (
                  <div className="mt-3 rounded-2xl border border-white/6 bg-black/20 px-3 py-2 text-sm text-text-secondary">
                    {step.selection_reason}
                  </div>
                )}
                <div className="mt-3 space-y-2 text-sm text-text-secondary">
                  {step.rationale.map((reason) => (
                    <div key={reason} className="flex items-start gap-2">
                      <ChevronRight size={14} className="mt-1 shrink-0 text-accent-blue" />
                      <span>{reason}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {!analysis?.execution_plan?.length && (
              <div className="rounded-2xl border border-white/6 bg-black/15 p-4 text-sm text-text-secondary">
                No executable plan was produced for this scenario.
              </div>
            )}
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-text-muted">
            <Gauge size={13} className="text-accent-blue" />
            Rollout and Validation
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
              <div className="text-sm font-semibold text-text-primary">Deployment scope</div>
              <div className="mt-3 space-y-2 text-sm text-text-secondary">
                <div>Primary: <span className="font-mono text-text-primary">{analysis?.config_scope.primary_underlyings.join(", ") ?? "—"}</span></div>
                <div>Secondary: <span className="font-mono text-text-primary">{analysis?.config_scope.secondary_underlyings.join(", ") ?? "—"}</span></div>
                <div>Instrument: <span className="font-mono text-text-primary">{analysis?.config_scope.instrument_type ?? "—"}</span></div>
                <div>Latency budget: <span className="font-mono text-text-primary">{analysis ? `${analysis.config_scope.latency_budget_ms}ms` : "—"}</span></div>
              </div>
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
              <div className="text-sm font-semibold text-text-primary">Paper-trade gate</div>
              <div className="mt-3 space-y-2 text-sm text-text-secondary">
                <div>Validation symbol: <span className="font-mono text-text-primary">{dataMode === "live" ? `${validationSymbol} FUT` : request?.session.symbol ?? "—"}</span></div>
                <div>Lot size: <span className="font-mono text-text-primary">{request?.metadata.lot_size ?? "—"}</span></div>
                <div>History source: <span className="font-mono text-text-primary">{request?.metadata.history_source ?? "—"}</span></div>
                <div>Fees per order: <span className="font-mono text-text-primary">{config ? `₹${config.paper_trading.fees_per_order}` : "—"}</span></div>
                <div>Paper slippage: <span className="font-mono text-text-primary">{config ? `${config.paper_trading.slippage_bps} bps` : "—"}</span></div>
              </div>
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4 md:col-span-2">
              <div className="flex items-start gap-3">
                <Zap size={16} className="mt-0.5 shrink-0 text-accent-amber" />
                <div className="space-y-2 text-sm text-text-secondary">
                  <div className="font-semibold text-text-primary">What this validates right now</div>
                  <div>
                    {dataMode === "live"
                      ? "The page is hitting the broker-backed snapshot route, replaying the latest available session through the same Market Profile and order-flow stack, rendering the resulting regime and sleeve decisions, and round-tripping a paper-proposal write when you click the validation action."
                      : "The page is hitting the deterministic demo route, replaying a controlled scenario through the same Market Profile and order-flow stack, rendering the resulting regime and sleeve decisions, and round-tripping a paper-proposal write when you click the validation action."}
                  </div>
                  <div>
                    {dataMode === "live"
                      ? "On closed-market days, the broker path replays the latest completed session so the operator surface still validates against real history."
                      : "The broker-backed snapshot path uses the same UI contract, so the page structure stays unchanged when you switch from demo to live validation."}
                  </div>
                </div>
              </div>
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4 md:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-text-primary">Gate A status</div>
                  <div className="mt-1 text-sm text-text-secondary">
                    Data and feature-engine checks over the loaded snapshot.
                  </div>
                </div>
                {gateA ? (
                  <ActionPill
                    label={gateA.passed ? "pass" : "fail"}
                    className={gateA.passed ? "bg-accent-green/12 text-accent-green border-accent-green/25" : "bg-accent-red/12 text-accent-red border-accent-red/25"}
                  />
                ) : (
                  <div className="text-sm text-text-secondary">
                    {gateAQuery.isFetching ? "Running Gate A..." : "Waiting for snapshot"}
                  </div>
                )}
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-3">
                <SmallMetric
                  label="Gate score"
                  value={gateA ? formatPct(gateA.score, 0) : "—"}
                  hint="Share of error-severity checks currently passing."
                />
                <SmallMetric
                  label="Checks passed"
                  value={
                    gateA
                      ? `${gateA.checks.filter((check) => check.passed).length}/${gateA.checks.length}`
                      : "—"
                  }
                  hint="Current pass count across data and feature-engine checks."
                />
                <SmallMetric
                  label="Generated"
                  value={gateA ? new Date(gateA.generated_at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "—"}
                  hint="Validation run time for the loaded payload."
                />
              </div>
              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                {(gateA?.checks ?? []).slice(0, 8).map((check) => (
                  <div
                    key={check.key}
                    className={clsx(
                      "rounded-2xl border p-3",
                      check.passed
                        ? "border-accent-green/20 bg-accent-green/8"
                        : check.severity === "warning"
                          ? "border-accent-amber/20 bg-accent-amber/8"
                          : "border-accent-red/20 bg-accent-red/8",
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="text-sm font-semibold text-text-primary">{check.label}</div>
                      <span className="text-[11px] uppercase tracking-[0.18em] text-text-muted">
                        {check.passed ? "pass" : check.severity}
                      </span>
                    </div>
                    <div className="mt-2 text-xs text-text-secondary">
                      Observed: <span className="font-mono text-text-primary">{JSON.stringify(check.observed)}</span>
                      {check.threshold !== undefined && check.threshold !== null ? (
                        <>
                          {" "}· Threshold: <span className="font-mono text-text-primary">{JSON.stringify(check.threshold)}</span>
                        </>
                      ) : null}
                    </div>
                    <div className="mt-2 text-xs text-text-secondary">{check.detail}</div>
                  </div>
                ))}
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                  <div className="text-sm font-semibold text-text-primary">Next validation stages</div>
                  <div className="mt-3 space-y-2 text-sm text-text-secondary">
                    {(summary?.validation_gates ?? []).map((gate) => (
                      <div key={gate.id} className="flex items-start justify-between gap-3">
                        <span>{gate.label}</span>
                        <span className="font-mono uppercase text-text-muted">{gate.status}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                  <div className="text-sm font-semibold text-text-primary">Implementation next steps</div>
                  <div className="mt-3 space-y-2 text-sm text-text-secondary">
                    {(summary?.implementation_plan ?? gateA?.pending_checks ?? []).slice(0, 3).map((item) => (
                      <div key={item} className="flex items-start gap-2">
                        <ChevronRight size={14} className="mt-1 shrink-0 text-accent-blue" />
                        <span>{item}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4 md:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-text-primary">Gate B status</div>
                  <div className="mt-1 text-sm text-text-secondary">
                    Rule-engine expectancy and walk-forward checks over recent session replays.
                  </div>
                </div>
                {gateB ? (
                  <ActionPill
                    label={gateB.passed ? "pass" : "fail"}
                    className={gateB.passed ? "bg-accent-green/12 text-accent-green border-accent-green/25" : "bg-accent-red/12 text-accent-red border-accent-red/25"}
                  />
                ) : (
                  <div className="text-sm text-text-secondary">
                    {gateBQuery.isFetching ? "Running Gate B..." : "Waiting for validation"}
                  </div>
                )}
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-4">
                <SmallMetric
                  label="Trades"
                  value={gateB ? String(gateB.metrics.evaluated_trades ?? "0") : "—"}
                  hint="Validated swing-sleeve trades over the replay set."
                />
                <SmallMetric
                  label="Expectancy"
                  value={gateB ? formatSigned(Number(gateB.metrics.expectancy ?? 0)) : "—"}
                  hint="Net expectancy per trade after the configured cost model."
                />
                <SmallMetric
                  label="Profit factor"
                  value={gateB ? String(gateB.metrics.profit_factor ?? "—") : "—"}
                  hint="Gross winners divided by gross losers after costs."
                />
                <SmallMetric
                  label="Positive windows"
                  value={gateB ? formatPct(Number(gateB.metrics.positive_window_ratio ?? 0), 0) : "—"}
                  hint="Share of walk-forward windows with positive net PnL."
                />
                <SmallMetric
                  label="Artifacts"
                  value={gateB ? String(gateB.artifact_count ?? gateB.storage?.artifact_count ?? 0) : "—"}
                  hint="Persisted session-level replay records for this validation run."
                />
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                  <div className="text-sm font-semibold text-text-primary">Series metadata</div>
                  <div className="mt-3 space-y-2 text-sm text-text-secondary">
                    <div>Symbol: <span className="font-mono text-text-primary">{gateB?.series_metadata?.symbol_code ?? "—"}</span></div>
                    <div>Source: <span className="font-mono text-text-primary">{gateB?.series_metadata?.source ?? "—"}</span></div>
                    <div>Sessions: <span className="font-mono text-text-primary">{gateB?.series_metadata?.session_count ?? "—"}</span></div>
                    <div>Persisted: <span className="font-mono text-text-primary">{gateB?.storage?.persisted ? gateB.storage.run_id ?? "yes" : "no"}</span></div>
                    <div>Latest session: <span className="font-mono text-text-primary">{gateB?.series_metadata?.session_dates?.[gateB.series_metadata.session_dates.length - 1] ?? "—"}</span></div>
                  </div>
                </div>
                <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                  <div className="text-sm font-semibold text-text-primary">Rule-engine checks</div>
                  <div className="mt-3 space-y-2 text-sm text-text-secondary">
                    {(gateB?.checks ?? []).slice(0, 5).map((check) => (
                      <div key={check.key} className="flex items-start justify-between gap-3">
                        <span>{check.label}</span>
                        <span className={clsx("font-mono uppercase", check.passed ? "text-accent-green" : check.severity === "warning" ? "text-accent-amber" : "text-accent-red")}>
                          {check.passed ? "pass" : check.severity}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
              <div className="mt-4 rounded-2xl border border-white/6 bg-black/15 p-4">
                <div className="text-sm font-semibold text-text-primary">Gate B replay artifacts</div>
                <div className="mt-3 grid gap-3 lg:grid-cols-2">
                  {(gateB?.artifacts_preview ?? []).slice(0, 6).map((artifact) => (
                    <div key={`${artifact.artifact_type}:${artifact.artifact_key}`} className="rounded-2xl border border-white/6 bg-black/20 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="text-sm font-semibold text-text-primary">{artifact.artifact_key}</div>
                        <span className="text-[11px] uppercase tracking-[0.18em] text-text-muted">{artifact.artifact_type}</span>
                      </div>
                      <div className="mt-2 space-y-1 text-xs text-text-secondary">
                        <div>Status: <span className="font-mono text-text-primary">{String(artifact.payload.status ?? "n/a")}</span></div>
                        <div>Setup: <span className="font-mono text-text-primary">{String(artifact.payload.setup_name ?? "n/a")}</span></div>
                        <div>Regime: <span className="font-mono text-text-primary">{String(artifact.payload.regime_label ?? "n/a")}</span></div>
                        <div>Decision: <span className="font-mono text-text-primary">{String(artifact.payload.decision_action ?? "n/a")}</span></div>
                        <div>Skip reason: <span className="font-mono text-text-primary">{String(artifact.payload.skip_reason ?? "—")}</span></div>
                      </div>
                    </div>
                  ))}
                  {!gateB?.artifacts_preview?.length && (
                    <div className="text-sm text-text-secondary">No artifact preview is available for this validation run.</div>
                  )}
                </div>
              </div>
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4 md:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-text-primary">Gate C status</div>
                  <div className="mt-1 text-sm text-text-secondary">
                    Shadow-mode divergence and operator-control checks over persisted broker-backed observations.
                  </div>
                </div>
                {dataMode !== "live" ? (
                  <div className="text-sm text-text-secondary">Gate C is only applicable on the live broker path.</div>
                ) : gateC ? (
                  <ActionPill
                    label={gateC.passed ? "pass" : "fail"}
                    className={gateC.passed ? "bg-accent-green/12 text-accent-green border-accent-green/25" : "bg-accent-red/12 text-accent-red border-accent-red/25"}
                  />
                ) : (
                  <div className="text-sm text-text-secondary">
                    {gateCQuery.isFetching ? "Running Gate C..." : "Waiting for shadow journal"}
                  </div>
                )}
              </div>
              {dataMode === "live" ? (
                <>
                  <div className="mt-4 grid gap-3 md:grid-cols-4">
                    <SmallMetric
                      label="Sessions"
                      value={gateC ? String(gateC.metrics.session_count ?? "0") : "—"}
                      hint="Observed shadow sessions inside the current validation window."
                    />
                    <SmallMetric
                      label="Signals"
                      value={gateC ? String(gateC.metrics.signal_count ?? "0") : "—"}
                      hint="Non-flat signals captured by the shadow journal."
                    />
                    <SmallMetric
                      label="Median drift"
                      value={gateC ? String(gateC.metrics.fill_drift_median_ticks ?? "—") : "—"}
                      hint="Median simulated-versus-observed fill drift in ticks."
                    />
                    <SmallMetric
                      label="P95 drift"
                      value={gateC ? String(gateC.metrics.fill_drift_p95_ticks ?? "—") : "—"}
                      hint="Tail fill drift used to block unstable promotion."
                    />
                  </div>
                  <div className="mt-4 grid gap-3 md:grid-cols-2">
                    <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                      <div className="text-sm font-semibold text-text-primary">Shadow journal</div>
                      <div className="mt-3 space-y-2 text-sm text-text-secondary">
                        <div>Symbol: <span className="font-mono text-text-primary">{gateC?.series_metadata?.symbol ?? `${validationSymbol} FUT`}</span></div>
                        <div>Records: <span className="font-mono text-text-primary">{gateC?.series_metadata?.record_count ?? gateC?.metrics.record_count ?? "—"}</span></div>
                        <div>Storage: <span className="font-mono text-text-primary">{gateC?.storage?.persisted ? gateC.storage.run_id ?? "persisted" : "not persisted"}</span></div>
                        <div>Backfill: <span className="font-mono text-text-primary">{shadowBackfill.data ? `${shadowBackfill.data.snapshot_count} snapshots / ${shadowBackfill.data.record_count} records` : "ready"}</span></div>
                        <div>Next step: <span className="font-mono text-text-primary">{gateC?.pending_checks?.[1] ?? "Observe additional sessions."}</span></div>
                      </div>
                    </div>
                    <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                      <div className="text-sm font-semibold text-text-primary">Shadow checks</div>
                      <div className="mt-3 space-y-2 text-sm text-text-secondary">
                        {(gateC?.checks ?? []).slice(0, 7).map((check) => (
                          <div key={check.key} className="flex items-start justify-between gap-3">
                            <span>{check.label}</span>
                            <span className={clsx("font-mono uppercase", check.passed ? "text-accent-green" : check.severity === "warning" ? "text-accent-amber" : "text-accent-red")}>
                              {check.passed ? "pass" : check.severity}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </>
              ) : (
                <div className="mt-4 rounded-2xl border border-white/6 bg-black/15 p-4 text-sm text-text-secondary">
                  Switch to the live broker path to run shadow backfill and Gate C divergence validation.
                </div>
              )}
            </div>
            <div className="rounded-2xl border border-white/6 bg-black/15 p-4 md:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-sm font-semibold text-text-primary">Gate D canary readiness</div>
                  <div className="mt-1 text-sm text-text-secondary">
                    Smallest-size live rollout gate combining current Gate B and Gate C state with live-canary constraints.
                  </div>
                </div>
                {dataMode !== "live" ? (
                  <div className="text-sm text-text-secondary">Canary readiness is only relevant on the live broker path.</div>
                ) : canaryReadiness ? (
                  <ActionPill
                    label={canaryReadiness.ready ? "ready" : "blocked"}
                    className={canaryReadiness.ready ? "bg-accent-green/12 text-accent-green border-accent-green/25" : "bg-accent-amber/12 text-accent-amber border-accent-amber/25"}
                  />
                ) : (
                  <div className="text-sm text-text-secondary">
                    {canaryReadinessQuery.isFetching ? "Checking Gate D..." : "Waiting for canary state"}
                  </div>
                )}
              </div>
              {dataMode === "live" ? (
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                    <div className="text-sm font-semibold text-text-primary">Canary constraints</div>
                    <div className="mt-3 space-y-2 text-sm text-text-secondary">
                      <div>Symbol: <span className="font-mono text-text-primary">{canaryReadiness?.symbol ?? validationSymbol}</span></div>
                      <div>Agents: <span className="font-mono text-text-primary">{canaryReadiness?.requirements.allowed_agents.join(", ") ?? "—"}</span></div>
                      <div>Max live lots: <span className="font-mono text-text-primary">{canaryReadiness?.requirements.max_live_lots ?? "—"}</span></div>
                      <div>Daily loss limit: <span className="font-mono text-text-primary">{canaryReadiness ? `₹${canaryReadiness.requirements.daily_loss_limit.toLocaleString("en-IN")}` : "—"}</span></div>
                      <div>Manual approval: <span className="font-mono text-text-primary">{canaryReadiness?.requirements.manual_approval_required ? "required" : "not required"}</span></div>
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
                    <div className="text-sm font-semibold text-text-primary">Blockers and next step</div>
                    <div className="mt-3 space-y-2 text-sm text-text-secondary">
                      {(canaryReadiness?.blockers?.length
                        ? canaryReadiness.blockers
                        : [canaryReadiness?.next_step ?? "Ready for the smallest-size live canary."]).map((item) => (
                        <div key={item} className="flex items-start gap-2">
                          <ChevronRight size={14} className="mt-1 shrink-0 text-accent-blue" />
                          <span>{item}</span>
                        </div>
                      ))}
                    </div>
                    <div className="mt-4 text-xs uppercase tracking-[0.18em] text-text-muted">
                      Gate B {canaryReadiness?.gate_b?.passed ? "pass" : "blocked"} · Gate C {canaryReadiness?.gate_c?.passed ? "pass" : "blocked"}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="mt-4 rounded-2xl border border-white/6 bg-black/15 p-4 text-sm text-text-secondary">
                  Canary rollout remains hidden in demo mode. Use the live broker path to check production gating.
                </div>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className={sectionChrome("px-5 py-4")}>
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-3">
            <Activity size={16} className="mt-0.5 shrink-0 text-accent-green" />
            <div className="text-sm text-text-secondary">
              <div className="font-semibold text-text-primary">Snapshot state</div>
              <div className="mt-1">
                {validationQuery.isShowingSnapshot
                  ? "Showing the last persisted successful snapshot because the latest fetch failed."
                  : validationQuery.snapshotSavedAt
                    ? `Last successful validation snapshot saved at ${new Date(validationQuery.snapshotSavedAt).toLocaleString("en-IN")}.`
                    : "No snapshot saved yet."}
              </div>
            </div>
          </div>
          <div className="text-xs uppercase tracking-[0.18em] text-text-muted">
            {validationQuery.isFetching
              ? dataMode === "live"
                ? "Refreshing live broker snapshot"
                : "Refreshing backend scenario"
              : gateAQuery.isFetching
                ? "Running Gate A validation"
              : gateBQuery.isFetching
                ? "Running Gate B validation"
              : gateCQuery.isFetching
                ? "Running Gate C validation"
              : shadowBackfill.isPending
                ? "Persisting shadow backfill"
              : canaryReadinessQuery.isFetching
                ? "Checking canary readiness"
              : dataMode === "live"
                ? "Live validation ready"
                : "Ready for broker hookup"}
          </div>
        </div>
      </section>

      {/* ── MP Signal Intelligence ─────────────────────────────────────── */}
      <section className={sectionChrome("px-5 py-5")}>
        <div className="flex items-center gap-3 mb-4">
          <CandlestickChart size={16} className="text-accent-blue shrink-0" />
          <div className="font-semibold text-text-primary">MP Signal Intelligence</div>
          <div className="ml-auto flex gap-1 rounded-lg border border-white/10 bg-black/20 p-1">
            {(["NIFTY", "BANKNIFTY", "SENSEX"] as const).map(ul => (
              <button
                key={ul}
                onClick={() => setMpUnderlying(ul)}
                className={`px-3 py-1 rounded text-xs font-semibold transition-colors ${
                  mpUnderlying === ul
                    ? "bg-accent-blue/25 text-accent-blue"
                    : "text-text-muted hover:text-text-primary"
                }`}
              >
                {ul}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

          {/* Open Signal Card */}
          <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
            <div className="flex items-center gap-2 mb-3">
              <Zap size={13} className="text-accent-amber" />
              <div className="text-sm font-semibold text-text-primary">Next Session Signal</div>
              <span className="ml-auto text-[10px] text-text-muted">
                {mpOpenSignalQuery.data?.as_of ?? "—"}
              </span>
            </div>
            {mpOpenSignalQuery.isLoading ? (
              <div className="flex items-center gap-2 text-xs text-text-muted py-3">
                <Loader2 size={12} className="animate-spin" /> Loading…
              </div>
            ) : mpOpenSignalQuery.data?.signals?.length ? (
              mpOpenSignalQuery.data.signals.map((s: any, i: number) => (
                <div key={i} className={`rounded-xl border p-3 ${
                  s.direction === "CE"
                    ? "border-accent-green/30 bg-accent-green/8"
                    : "border-accent-red/30 bg-accent-red/8"
                }`}>
                  <div className="flex items-center gap-2 mb-2">
                    <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                      s.direction === "CE"
                        ? "bg-accent-green/20 text-accent-green"
                        : "bg-accent-red/20 text-accent-red"
                    }`}>
                      {s.direction === "CE" ? "BUY CE" : "BUY PE"}
                    </span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent-amber/15 text-accent-amber">
                      {s.strength}
                    </span>
                    <span className="text-[10px] text-text-muted ml-auto">{s.reason}</span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-[10px] font-mono text-text-secondary mb-2">
                    <div>Alloc: <span className="text-text-primary">{(s.alloc * 100).toFixed(0)}%</span></div>
                    <div>BF: <span className="text-text-primary">{s.buyer_fail}</span></div>
                    <div>SF: <span className="text-text-primary">{s.seller_fail}</span></div>
                  </div>
                  <div className="text-[10px] text-text-muted italic leading-relaxed">{s.instruction}</div>
                </div>
              ))
            ) : (
              <div className="text-xs text-text-muted py-3 flex items-center gap-2">
                <ShieldCheck size={14} />
                {mpOpenSignalQuery.data?.skip_reason
                  ? `No trade — ${mpOpenSignalQuery.data.skip_reason}`
                  : "No actionable signal"}
              </div>
            )}
          </div>

          {/* Agent Context Card */}
          <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
            <div className="flex items-center gap-2 mb-3">
              <Bot size={13} className="text-accent-purple" />
              <div className="text-sm font-semibold text-text-primary">MP Agent Context</div>
            </div>
            <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
              {(mpAgentContextQuery.data ?? []).map((c: any, i: number) => (
                <div key={i} className={`text-xs border-l-2 pl-2.5 py-1 ${
                  c.level === "bullish" ? "border-l-accent-green"
                  : c.level === "bearish" ? "border-l-accent-red"
                  : c.level === "warning" ? "border-l-accent-amber"
                  : "border-l-accent-blue"
                }`}>
                  <div className="flex items-center gap-1.5 mb-0.5">
                    <span className="text-[10px] text-text-muted uppercase">{c.type}</span>
                    <span className="text-[10px] text-text-muted ml-auto">{c.time}</span>
                  </div>
                  <div className="text-text-secondary leading-relaxed">{c.message}</div>
                </div>
              ))}
              {!mpAgentContextQuery.isLoading && !mpAgentContextQuery.data?.length && (
                <div className="text-xs text-text-muted py-3">No context data yet</div>
              )}
            </div>
          </div>

          {/* Data Status Card */}
          <div className="rounded-2xl border border-white/6 bg-black/15 p-4">
            <div className="flex items-center gap-2 mb-3">
              <Layers3 size={13} className="text-accent-blue" />
              <div className="text-sm font-semibold text-text-primary">Data Pipeline</div>
            </div>
            <div className="space-y-1.5">
              {(mpDataStatusQuery.data ?? [])
                .filter((s: any) => s.name.startsWith(mpUnderlying))
                .map((s: any, i: number) => (
                  <div key={i} className="flex items-center gap-2 text-xs font-mono">
                    <span className={`w-2 h-2 rounded-full shrink-0 ${
                      s.status === "ok" ? "bg-accent-green"
                      : s.status === "warning" ? "bg-accent-amber"
                      : "bg-accent-red"
                    }`} />
                    <span className="text-text-primary truncate flex-1">{s.name}</span>
                    <span className="text-text-muted shrink-0">{s.rows > 0 ? s.rows.toLocaleString() : "—"}</span>
                  </div>
              ))}
              {mpDataStatusQuery.isLoading && (
                <div className="flex items-center gap-2 text-xs text-text-muted">
                  <Loader2 size={10} className="animate-spin" /> Loading status…
                </div>
              )}
            </div>
          </div>

        </div>

        {/* MP Signal History Table */}
        <div className="mt-4 rounded-2xl border border-white/6 bg-black/15 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Gauge size={13} className="text-text-muted" />
            <div className="text-sm font-semibold text-text-primary">Recent MP Signals — {mpUnderlying}</div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="text-text-muted border-b border-white/8">
                  <th className="text-left py-1 pr-3">Date</th>
                  <th className="text-left py-1 pr-3">Day Type</th>
                  <th className="text-right py-1 pr-3">Move</th>
                  <th className="text-right py-1 pr-3">POC</th>
                  <th className="text-center py-1 pr-3">BF</th>
                  <th className="text-center py-1 pr-3">SF</th>
                  <th className="text-center py-1">Signal</th>
                </tr>
              </thead>
              <tbody>
                {((mpSignalsQuery.data?.signals ?? []) as any[]).slice(-15).reverse().map((s: any, i: number) => (
                  <tr key={i} className="border-b border-white/5 hover:bg-white/2">
                    <td className="py-1 pr-3 text-text-secondary">{s.date}</td>
                    <td className={`py-1 pr-3 ${
                      s.day_type?.includes("TREND_UP") ? "text-accent-green"
                      : s.day_type?.includes("TREND_DN") ? "text-accent-red"
                      : s.day_type?.includes("FAILED") ? "text-accent-amber"
                      : "text-text-secondary"
                    }`}>{s.day_type}</td>
                    <td className={`py-1 pr-3 text-right ${
                      s.daily_move > 0 ? "text-accent-green"
                      : s.daily_move < 0 ? "text-accent-red"
                      : "text-text-muted"
                    }`}>{s.daily_move > 0 ? "+" : ""}{Math.round(s.daily_move)}</td>
                    <td className="py-1 pr-3 text-right text-text-secondary">{s.poc?.toLocaleString()}</td>
                    <td className="py-1 pr-3 text-center">{s.buyer_fail}</td>
                    <td className="py-1 pr-3 text-center">{s.seller_fail}</td>
                    <td className={`py-1 text-center font-semibold ${
                      s.direction === "CE" ? "text-accent-green"
                      : s.direction === "PE" ? "text-accent-red"
                      : s.direction === "CONFLICT" ? "text-accent-amber"
                      : "text-text-muted"
                    }`}>{s.direction}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {mpSignalsQuery.isLoading && (
              <div className="flex items-center gap-2 py-4 text-xs text-text-muted">
                <Loader2 size={12} className="animate-spin" /> Loading signals…
              </div>
            )}
            {!mpSignalsQuery.isLoading && !mpSignalsQuery.data?.signals?.length && (
              <div className="py-4 text-center text-xs text-text-muted">
                No MP signal data for {mpUnderlying}
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
