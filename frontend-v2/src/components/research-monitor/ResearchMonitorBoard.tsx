"use client";

/**
 * Research Monitor — native v2.
 *
 * The non-live research operations console. Surfaces, in one place:
 *   - research-cache population state (per-symbol ingest stages)
 *   - the live NSE-cache validation report
 *   - the MACD option-study backtest runner + task monitor + results
 *   - the Greeks-Sync research track
 *
 * Backed by /api/analysis/{research-cache-status,validation-report,
 * greeks-sync-report,macd-backtest/*,broker-status,fo-underlyings}.
 *
 * Every panel degrades gracefully via the persistent-snapshot pattern:
 * the last good payload is kept in localStorage and shown (with a stale
 * banner) when a refresh fails, so the desk never blanks mid-session.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Database,
  Download,
  FileText,
  FlaskConical,
  Gauge,
  Link2,
  Loader2,
  Play,
  RefreshCw,
  Sigma,
  XCircle,
} from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatDuration,
  formatIST,
  formatNumber,
  serviceStateTone,
  tone,
} from "@/components/desk-ui";
import {
  API_URL,
  api,
  getAnalysisBrokerStatus,
  getFoUnderlyings,
  getLatestGreeksSyncReport,
  getLatestValidationReport,
  getResearchCacheStatus,
  startMacdBacktest,
} from "@/lib/api";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";

// ── Types ────────────────────────────────────────────────────────────────────

type Stage = "queued" | "metadata" | "spot" | "contracts" | "populating" | "populated";

interface ResearchSymbol {
  symbol: string;
  kind: string;
  stage: Stage;
  progress_pct: number;
  research_ready: boolean;
  research_contract_target: number;
  research_contracts_processed: number;
  active_now: boolean;
  total_expiries: number;
  discovered_expiries: number;
  selection_spots_ready: number;
  spot_candles: number;
  total_contracts: number;
  complete_contracts: number;
  pending_contracts: number;
  empty_contracts: number;
  option_contracts: number;
  option_candles: number;
  expiries_synced_at: string | null;
  spot_synced_at: string | null;
  last_activity_at: string | null;
}

interface ResearchCacheStatus {
  summary: {
    universe_total: number;
    underlyings_with_expiries: number;
    underlyings_with_spot: number;
    selection_spots_ready: number;
    expiry_total: number;
    expiries_discovered: number;
    contracts_total: number;
    contracts_complete: number;
    contracts_pending: number;
    contracts_empty: number;
    research_contract_target: number;
    research_contracts_processed: number;
    option_contracts: number;
    option_candles: number;
    active_symbols: number;
    populated_symbols: number;
    symbols_in_progress: number;
    stage_counts: Record<string, number>;
    recent_activity_at: string | null;
    active_recent_symbols: number;
    last_successful_option_sync_at: string | null;
    last_complete_contract_sync_at: string | null;
    option_candles_added_last_30m: number;
    complete_contracts_touched_last_30m: number;
  };
  scheduler: {
    state: "idle" | "running" | "waiting" | "rate_limit_cooldown" | "stalled";
    label: string;
    detail: string;
    poll_minutes: number;
    rate_limit_window_minutes: number;
    cooldown_minutes: number;
    next_batch_at: string | null;
    seconds_until_next_batch: number;
    estimated_window_available_pct: number | null;
    estimated_window_used_pct: number | null;
    last_batch_activity_at: string | null;
    last_run_started_at?: string | null;
    last_run_completed_at?: string | null;
  };
  symbols: ResearchSymbol[];
}

interface ValidationStrategyRow {
  strategy: string;
  trades: number;
  avg_return_pct: number;
  median_return_pct: number;
  positive_pct: number;
}
type BreakdownRow = Record<string, string | number>;

interface ValidationReportPayload {
  available: boolean;
  live?: boolean;
  detail?: string;
  generated_at?: string | null;
  source_updated_at?: string | null;
  summary?: {
    generated_at: string;
    coverage: {
      underlyings_with_option_data: number;
      atm_monthly_pairs: number;
      complete_cached_contracts: number;
      cached_option_candles: number;
    };
    opportunities: { total_trades: number; months: string[]; underlyings: string[] };
    exit_analysis: {
      best_strategy: string;
      best_strategy_avg_return_pct: number;
      hold_to_expiry_avg_return_pct: number;
      avg_max_return_pct: number;
      positive_pct: number;
      strategy_ranking: ValidationStrategyRow[];
    };
    breakdowns: { by_underlying: BreakdownRow[] } & Record<string, BreakdownRow[]>;
  };
  files?: {
    report_markdown_url?: string;
    summary_json_url?: string;
    trades_csv_url?: string;
    coverage_csv_url?: string;
  };
}

interface GreeksTrackRow {
  track: string;
  trades: number;
  avg_oracle_best_exit_return_pct: number;
  avg_hold_to_expiry_return_pct: number;
  positive_pct: number;
}
interface GreeksStrategyRow {
  strategy: string;
  trades: number;
  avg_return_pct: number;
  median_return_pct: number;
  positive_pct: number;
}
interface GreeksSyncReportPayload {
  available: boolean;
  detail?: string;
  generated_at?: string | null;
  source_updated_at?: string | null;
  summary?: {
    generated_at: string;
    coverage: {
      underlyings_with_option_data: number;
      atm_monthly_pairs: number;
      complete_cached_contracts: number;
      cached_option_candles: number;
    };
    signals: {
      total_signals: number;
      strong_signals: number;
      avg_score: number;
      median_score: number;
      avg_theta_overwhelm_ratio: number;
      macd_confirmed_pct: number;
    };
    comparison: { track_ranking: GreeksTrackRow[] };
    exit_analysis: { best_strategy: string; best_strategy_avg_return_pct: number; strategy_ranking: GreeksStrategyRow[] };
  };
  files?: { report_markdown_url?: string; summary_json_url?: string; trades_csv_url?: string };
}

interface BrokerStatus {
  upstox_connected?: boolean;
  upstox_ready?: boolean;
  upstox_token_preview?: string | null;
  upstox_token_health?: { message?: string; checked_at?: string; expires_at_ist?: string } | null;
  breeze_connected?: boolean;
  ready?: boolean;
  data_sources?: { note?: string } | null;
  connected_brokers?: string[];
}

interface MacdTrade {
  underlying: string;
  expiry: string;
  option_type: string;
  strike: number;
  entry_time: string;
  entry_price: number;
  max_price: number;
  max_return_pct: number;
  held_return_pct: number;
  target_50pct_hit: boolean;
  target_100pct_hit: boolean;
  final_price: number;
}
interface MacdResults {
  total_opportunities: number;
  by_underlying: Record<
    string,
    {
      opportunities: number;
      avg_max_return: number;
      avg_held_return: number;
      target_50_rate: number;
      target_100_rate: number;
      monthly_breakdown?: Record<string, number>;
    }
  >;
  by_month?: Record<string, { count: number; avg_max_return: number; ce_count: number; pe_count: number }>;
  exit_analysis?: {
    best_strategy: string;
    hold_to_expiry_avg?: number;
    target_50_hit_rate?: number;
    target_100_hit_rate?: number;
    strategy_comparison?: Record<string, { avg_return: number; hit_rate: number }>;
  };
  all_trades: MacdTrade[];
}
interface MacdTask {
  task_id: string;
  status: "pending" | "running" | "done" | "error";
  underlyings: string[];
  from_date: string;
  to_date: string;
  created_at: string;
  finished_at?: string;
  elapsed_secs: number;
  error?: string;
  progress?: { pct?: number; current?: string; processed?: number; total?: number };
  results_summary?: {
    total_opportunities: number;
    trade_count: number;
    by_underlying: Record<string, unknown>;
  };
}

// ── Small helpers ─────────────────────────────────────────────────────────────

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function compactNumber(value?: number | null): string {
  const v = num(value);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return `${v}`;
}

function relTime(value?: string | null): string {
  if (!value) return "—";
  const deltaMs = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(deltaMs) || deltaMs < 0) return "just now";
  const m = Math.floor(deltaMs / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function countdown(seconds?: number | null): string {
  if (seconds == null) return "—";
  return formatDuration(Math.max(0, Math.floor(seconds)));
}

function pctReturnTone(pct: number): string {
  if (pct >= 100) return "text-accent-green";
  if (pct >= 50) return "text-accent-amber";
  if (pct >= 0) return "text-text-secondary";
  return "text-accent-red";
}

function errDetail(error: unknown): string {
  return (
    (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
    (error as Error)?.message ||
    "Request failed"
  );
}

const STAGE_TONE: Record<Stage, string> = {
  populated: "border-accent-green/30 bg-accent-green/10 text-accent-green",
  populating: "border-accent-blue/30 bg-accent-blue/10 text-accent-blue",
  contracts: "border-accent-amber/30 bg-accent-amber/10 text-accent-amber",
  spot: "border-accent-blue/30 bg-accent-blue/10 text-accent-blue",
  metadata: "border-bg-border bg-bg-secondary/40 text-text-muted",
  queued: "border-bg-border bg-bg-secondary/40 text-text-muted",
};
const stageTone = (s: string) => STAGE_TONE[s as Stage] ?? "border-bg-border bg-bg-secondary/40 text-text-muted";

const SCHED_VARIANT: Record<string, "info" | "neutral" | "warn" | "error"> = {
  running: "info",
  idle: "neutral",
  waiting: "warn",
  rate_limit_cooldown: "warn",
  stalled: "error",
};

function ReturnPct({ pct }: { pct?: number | null }) {
  const v = num(pct);
  return (
    <span className={`font-mono text-xs font-semibold ${pctReturnTone(v)}`}>
      {v >= 0 ? "+" : ""}
      {v.toFixed(1)}%
    </span>
  );
}

function SnapshotBanner({ message, savedAt }: { message: string; savedAt?: string | null }) {
  return (
    <div className="rounded-xl border border-accent-amber/30 bg-accent-amber/5 px-3 py-2 text-[11px] text-text-muted">
      <div className="flex items-center gap-1.5 font-medium text-accent-amber">
        <AlertCircle size={12} />
        Showing last successful snapshot
      </div>
      <div className="mt-0.5">
        {message}
        {savedAt ? ` · saved ${relTime(savedAt)}` : null}
      </div>
    </div>
  );
}

function ProgressBar({ pct, label, tint = "blue" }: { pct: number; label?: string; tint?: "blue" | "green" | "amber" }) {
  const fill = tint === "green" ? "bg-accent-green" : tint === "amber" ? "bg-accent-amber" : "bg-accent-blue";
  return (
    <div className="space-y-1">
      {label ? (
        <div className="flex items-baseline justify-between gap-2 text-[11px] text-text-muted">
          <span className="truncate">{label}</span>
          <span className="font-mono shrink-0">{num(pct).toFixed(0)}%</span>
        </div>
      ) : null}
      <div className="h-1.5 overflow-hidden rounded-full bg-bg-border">
        <div className={`h-full rounded-full ${fill} transition-all duration-500`} style={{ width: `${Math.min(100, Math.max(0, num(pct)))}%` }} />
      </div>
    </div>
  );
}

function ReportLinks({
  files,
  accent,
}: {
  files?: { report_markdown_url?: string; summary_json_url?: string; trades_csv_url?: string };
  accent: "amber" | "violet";
}) {
  const hover = accent === "violet" ? "hover:border-accent-purple/40 hover:text-accent-purple" : "hover:border-accent-amber/40 hover:text-accent-amber";
  const link = (path?: string) => (path ? `${API_URL}${path}` : "#");
  const items: Array<{ href?: string; label: string; icon: React.ReactNode }> = [
    { href: files?.report_markdown_url, label: "Markdown", icon: <FileText size={11} /> },
    { href: files?.summary_json_url, label: "Summary JSON", icon: <Download size={11} /> },
    { href: files?.trades_csv_url, label: "Trades CSV", icon: <Download size={11} /> },
  ];
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {items
        .filter((i) => i.href)
        .map((i) => (
          <a
            key={i.label}
            href={link(i.href)}
            target="_blank"
            rel="noreferrer"
            className={`inline-flex items-center gap-1 rounded-lg border border-bg-border px-2 py-1 text-[11px] text-text-secondary ${hover}`}
          >
            {i.icon}
            {i.label}
          </a>
        ))}
    </div>
  );
}

// ── Broker status strip ───────────────────────────────────────────────────────

function BrokerStatusStrip() {
  const { data, error, isShowingSnapshot, snapshotSavedAt } = usePersistentSnapshotQuery<BrokerStatus>({
    queryKey: ["research", "broker-status"],
    queryFn: () => getAnalysisBrokerStatus().then((r) => r.data),
    refetchInterval: REFRESH_MS.snapshot,
    staleTime: 5000,
    refetchOnWindowFocus: false,
    storageKey: "research:broker-status",
  });

  if (!data) {
    return (
      <Section title="Data sources" icon={<Link2 size={16} />}>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          {error ? <AlertCircle size={13} className="text-accent-red" /> : <Loader2 size={13} className="animate-spin" />}
          {error ? errDetail(error) : "Checking broker connections…"}
        </div>
      </Section>
    );
  }

  const upstoxOk = Boolean(data.upstox_connected);
  const upstoxReady = data.upstox_ready ?? upstoxOk;
  const health = data.upstox_token_health;
  const breezeOk = Boolean(data.breeze_connected);
  const upstoxState = upstoxReady ? "ready" : upstoxOk ? "warning" : "error";

  return (
    <Section title="Data sources" icon={<Link2 size={16} />} description="Broker sessions backing the research backfills.">
      {isShowingSnapshot ? <div className="mb-3"><SnapshotBanner message={errDetail(error)} savedAt={snapshotSavedAt} /></div> : null}
      <div className="grid gap-2 md:grid-cols-2">
        <div className={`rounded-xl border px-3 py-2.5 text-xs ${serviceStateTone(upstoxState)}`}>
          <div className="flex items-center gap-2">
            {upstoxReady ? <CheckCircle2 size={13} /> : upstoxOk ? <AlertCircle size={13} /> : <XCircle size={13} />}
            <span className="font-semibold">
              {upstoxReady ? "Upstox ready" : upstoxOk ? "Upstox connected · token attention" : "Upstox not connected"}
            </span>
            {data.upstox_token_preview ? <span className="font-mono text-text-muted">{data.upstox_token_preview}</span> : null}
          </div>
          {health?.message ? (
            <div className="mt-1 text-[11px] text-text-muted">
              {health.message}
              {health.checked_at ? ` · checked ${relTime(health.checked_at)}` : null}
            </div>
          ) : null}
        </div>
        <div className={`rounded-xl border px-3 py-2.5 text-xs ${serviceStateTone(breezeOk ? "ready" : "warning")}`}>
          <div className="flex items-center gap-2">
            {breezeOk ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />}
            <span className="font-semibold">ICICI Breeze {breezeOk ? "connected" : "not connected"}</span>
          </div>
          <div className="mt-1 text-[11px] text-text-muted">
            {breezeOk ? "3-year expired-options history available" : "needed for the expired-options backtest track"}
          </div>
        </div>
      </div>
      {data.data_sources?.note ? <div className="mt-2 text-[11px] text-text-muted">{data.data_sources.note}</div> : null}
    </Section>
  );
}

// ── Research cache panel ──────────────────────────────────────────────────────

function ResearchCachePanel() {
  const [showAllActive, setShowAllActive] = useState(false);
  const [showAllReady, setShowAllReady] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const { data, isLoading, error, refetch, isFetching, isShowingSnapshot, snapshotSavedAt } =
    usePersistentSnapshotQuery<ResearchCacheStatus>({
      queryKey: ["research", "cache-status"],
      queryFn: () => getResearchCacheStatus().then((r) => r.data),
      refetchInterval: REFRESH_MS.live,
      staleTime: 2000,
      refetchOnWindowFocus: false,
      storageKey: "research:cache-status",
    });

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const sched = data?.scheduler;
  const secondsUntilNext = useMemo(() => {
    if (!sched?.next_batch_at) return sched?.seconds_until_next_batch ?? null;
    return Math.max(0, Math.ceil((new Date(sched.next_batch_at).getTime() - nowMs) / 1000));
  }, [sched?.next_batch_at, sched?.seconds_until_next_batch, nowMs]);

  if (isLoading && !data) {
    return (
      <Section title="Research cache" icon={<Database size={16} />}>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <Loader2 size={13} className="animate-spin" /> Loading research cache status…
        </div>
      </Section>
    );
  }

  if (!data) {
    return (
      <Section title="Research cache" icon={<Database size={16} />} description="Live Timescale population state for the recurring Upstox sync.">
        <div className="rounded-xl border border-accent-red/30 bg-accent-red/5 px-4 py-6 text-center text-sm text-accent-red">
          {errDetail(error)}
        </div>
      </Section>
    );
  }

  const { summary, symbols } = data;
  const universe = num(summary.universe_total);
  const expiryPct = universe ? (num(summary.underlyings_with_expiries) / universe) * 100 : 0;
  const spotPct = universe ? (num(summary.underlyings_with_spot) / universe) * 100 : 0;
  const discoveryPct = summary.expiry_total ? (num(summary.expiries_discovered) / summary.expiry_total) * 100 : 0;
  const syncPct = summary.research_contract_target
    ? (num(summary.research_contracts_processed) / summary.research_contract_target) * 100
    : 0;

  const activeQueue = symbols
    .filter((s) => !s.research_ready && (s.active_now || ["metadata", "spot", "contracts", "populating"].includes(s.stage)))
    .sort((a, b) => {
      const ta = a.last_activity_at ? new Date(a.last_activity_at).getTime() : 0;
      const tb = b.last_activity_at ? new Date(b.last_activity_at).getTime() : 0;
      return tb - ta || b.progress_pct - a.progress_pct || a.symbol.localeCompare(b.symbol);
    });

  const readySymbols = symbols
    .filter((s) => s.research_ready)
    .sort((a, b) => b.option_candles - a.option_candles || b.complete_contracts - a.complete_contracts || a.symbol.localeCompare(b.symbol));

  const activeVisible = showAllActive ? activeQueue : activeQueue.slice(0, 8);
  const readyVisible = showAllReady ? readySymbols : readySymbols.slice(0, 8);

  const schedVariant = SCHED_VARIANT[sched?.state ?? "idle"] ?? "neutral";

  const rightSlot = (
    <button
      type="button"
      onClick={() => refetch()}
      className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
    >
      <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} />
      Refresh
    </button>
  );

  return (
    <Section
      title="Research cache"
      icon={<Database size={16} className="text-accent-blue" />}
      description="Live Timescale population state for the recurring Upstox sync."
      rightSlot={rightSlot}
    >
      <div className="space-y-4">
        {isShowingSnapshot ? <SnapshotBanner message={`Cache polling unavailable. ${errDetail(error)}`} savedAt={snapshotSavedAt} /> : null}

        {/* scheduler banner */}
        {sched ? (
          <div className={`rounded-xl border px-3.5 py-3 ${serviceStateTone(sched.state === "running" ? "active" : sched.state === "stalled" ? "error" : sched.state === "idle" ? "" : "warning")}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <StatusBadge label={sched.state.replace(/_/g, " ")} variant={schedVariant} />
                  {sched.label}
                </div>
                <div className="mt-1 text-xs text-text-muted">{sched.detail}</div>
              </div>
              <div className="text-right text-xs text-text-muted">
                {sched.next_batch_at ? (
                  <>
                    <div className="text-[10px] uppercase tracking-[0.14em]">Next batch in</div>
                    <div className="font-mono text-base font-semibold text-text-primary">{countdown(secondsUntilNext)}</div>
                    <div>{formatIST(sched.next_batch_at)}</div>
                  </>
                ) : (
                  <div>Last activity {relTime(sched.last_batch_activity_at)}</div>
                )}
              </div>
            </div>
          </div>
        ) : null}

        {/* KPI tiles */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile label="Universe" value={String(universe)} detail={`${summary.underlyings_with_expiries} metadata ready`} />
          <MetricTile
            label="Spot synced"
            value={String(summary.underlyings_with_spot)}
            detail={`${summary.selection_spots_ready} selection bars`}
            color="text-accent-blue"
          />
          <MetricTile
            label="Research ready"
            value={String(summary.populated_symbols)}
            detail={`${summary.research_contracts_processed}/${summary.research_contract_target || 0} contracts synced`}
            color={summary.populated_symbols ? "text-accent-green" : undefined}
          />
          <MetricTile
            label="Option candles"
            value={compactNumber(summary.option_candles)}
            detail={`+${compactNumber(summary.option_candles_added_last_30m)} in 30m · ${summary.option_contracts} contracts`}
            color="text-accent-amber"
          />
        </div>

        {/* progress + stage chips */}
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2.5 rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
            <ProgressBar pct={expiryPct} label={`Expiry metadata · ${summary.underlyings_with_expiries}/${universe}`} tint="blue" />
            <ProgressBar pct={spotPct} label={`Spot history · ${summary.underlyings_with_spot}/${universe}`} tint="blue" />
            <ProgressBar pct={discoveryPct} label={`Contract discovery · ${summary.expiries_discovered}/${summary.expiry_total || 0} buckets`} tint="amber" />
            <ProgressBar pct={syncPct} label={`Research sync target · ${summary.research_contracts_processed}/${summary.research_contract_target || 0}`} tint="green" />
          </div>
          <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
            <div className="mb-2 text-[10.5px] uppercase tracking-[0.14em] text-text-muted">Stage distribution</div>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(summary.stage_counts ?? {}).map(([stage, count]) => (
                <span key={stage} className={`rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.1em] ${stageTone(stage)}`}>
                  {stage}: {count}
                </span>
              ))}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
              <div>
                Last candle sync
                <div className="font-mono text-text-secondary">{relTime(summary.last_successful_option_sync_at)}</div>
              </div>
              <div>
                Recent activity
                <div className="font-mono text-text-secondary">{relTime(summary.recent_activity_at)}</div>
              </div>
            </div>
          </div>
        </div>

        {/* symbol columns */}
        <div className="grid gap-4 xl:grid-cols-2">
          <SymbolColumn
            title="Being populated"
            count={activeQueue.length}
            symbols={activeVisible}
            showAll={showAllActive}
            onToggle={() => setShowAllActive((v) => !v)}
            emptyText="No symbols are actively moving through the cache right now."
            variant="active"
          />
          <SymbolColumn
            title="Research ready"
            count={readySymbols.length}
            symbols={readyVisible}
            showAll={showAllReady}
            onToggle={() => setShowAllReady((v) => !v)}
            emptyText="No symbols are research-ready yet. Coverage is still being built."
            variant="ready"
          />
        </div>
      </div>
    </Section>
  );
}

function SymbolColumn({
  title,
  count,
  symbols,
  showAll,
  onToggle,
  emptyText,
  variant,
}: {
  title: string;
  count: number;
  symbols: ResearchSymbol[];
  showAll: boolean;
  onToggle: () => void;
  emptyText: string;
  variant: "active" | "ready";
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">
          {title} ({count})
        </div>
        {count > 8 ? (
          <button type="button" onClick={onToggle} className="text-[11px] text-accent-blue hover:underline">
            {showAll ? "Show less" : `Show all (${count})`}
          </button>
        ) : null}
      </div>
      <div className="max-h-[26rem] space-y-2 overflow-y-auto pr-1">
        {symbols.map((s) => (
          <div key={`${variant}-${s.symbol}`} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-text-primary">{s.symbol}</span>
                  <span className="text-[10.5px] text-text-muted">{s.kind}</span>
                  <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] ${stageTone(s.stage)}`}>
                    {s.stage}
                  </span>
                  {variant === "active" && s.active_now ? <span className="text-[10px] text-accent-green">active</span> : null}
                </div>
                <div className="text-[11px] text-text-muted">
                  {s.research_contracts_processed}/{s.research_contract_target || 0} required · {compactNumber(s.option_candles)} candles ·{" "}
                  {s.pending_contracts} backlog
                </div>
              </div>
              <div className="shrink-0 text-right text-[11px] text-text-muted">
                <div>{relTime(s.last_activity_at)}</div>
                <div>
                  {s.discovered_expiries}/{s.total_expiries || 0} expiries
                </div>
              </div>
            </div>
            {variant === "active" ? <div className="mt-2"><ProgressBar pct={s.progress_pct} /></div> : null}
          </div>
        ))}
        {symbols.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-3 py-4 text-[11px] text-text-muted">{emptyText}</div>
        ) : null}
      </div>
    </div>
  );
}

// ── Validation report panel ───────────────────────────────────────────────────

function ValidationReportPanel() {
  const { data, isLoading, error, refetch, isFetching, isShowingSnapshot, snapshotSavedAt } =
    usePersistentSnapshotQuery<ValidationReportPayload>({
      queryKey: ["research", "validation-report"],
      queryFn: () => getLatestValidationReport().then((r) => r.data),
      refetchInterval: REFRESH_MS.summary,
      staleTime: 5000,
      refetchOnWindowFocus: false,
      storageKey: "research:validation-report",
    });

  if (isLoading && !data) {
    return (
      <Section title="Validation report" icon={<FlaskConical size={16} />}>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <Loader2 size={13} className="animate-spin" /> Loading validation report…
        </div>
      </Section>
    );
  }

  if (!data?.available || !data.summary) {
    return (
      <Section
        title="Validation report"
        icon={<FlaskConical size={16} className="text-accent-amber" />}
        description="Cached NSE option data only · recomputed from Timescale as new candles land."
      >
        {isShowingSnapshot ? <div className="mb-3"><SnapshotBanner message={`Refresh failed. ${errDetail(error)}`} savedAt={snapshotSavedAt} /></div> : null}
        <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-sm text-text-muted">
          {data?.detail || "Live validation is waiting for enough cached CE/PE pairs to produce a report."}
        </div>
      </Section>
    );
  }

  const s = data.summary;
  const strategyRows = s.exit_analysis.strategy_ranking.slice(0, 5);
  const underlyingRows = (s.breakdowns.by_underlying ?? []).slice(0, 5);

  const rightSlot = (
    <div className="flex items-center gap-2">
      <span className="text-[11px] text-text-muted">recomputed {relTime(s.generated_at)}</span>
      <button type="button" onClick={() => refetch()} className="text-text-muted hover:text-text-primary">
        <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} />
      </button>
    </div>
  );

  return (
    <Section
      title="Validation report"
      icon={<FlaskConical size={16} className="text-accent-amber" />}
      description="Cached NSE option data only · recomputed from Timescale as new candles land."
      rightSlot={rightSlot}
    >
      <div className="space-y-4">
        {isShowingSnapshot ? <SnapshotBanner message={`Refresh failed. ${errDetail(error)}`} savedAt={snapshotSavedAt} /> : null}

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile
            label="Cache coverage"
            value={String(s.coverage.underlyings_with_option_data)}
            detail={`${s.coverage.complete_cached_contracts} contracts · ${compactNumber(s.coverage.cached_option_candles)} candles`}
            color="text-accent-blue"
          />
          <MetricTile
            label="Opportunities"
            value={String(s.opportunities.total_trades)}
            detail={s.opportunities.months.length ? s.opportunities.months.join(", ") : "no months yet"}
            color={s.opportunities.total_trades ? "text-accent-green" : undefined}
          />
          <MetricTile
            label="Best fixed exit"
            value={s.exit_analysis.best_strategy}
            detail={`${formatNumber(s.exit_analysis.best_strategy_avg_return_pct, 2)}% avg`}
            color="text-accent-amber"
          />
          <MetricTile
            label="Hold to expiry"
            value={`${formatNumber(s.exit_analysis.hold_to_expiry_avg_return_pct, 2)}%`}
            detail={`${formatNumber(s.exit_analysis.avg_max_return_pct, 2)}% avg max move`}
            color={tone(s.exit_analysis.hold_to_expiry_avg_return_pct)}
          />
        </div>

        <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2 text-[11px] text-text-muted">
          ATM monthly pairs analyzed: {s.coverage.atm_monthly_pairs}. Fixed-exit ranking is the deployable comparison; the per-underlying rows use
          the oracle best-exit result and are diagnostic only.
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <RankTable
            title="Fixed exit ranking"
            rows={strategyRows.map((r) => ({
              key: r.strategy,
              primary: r.strategy,
              meta: `${r.trades} trades · median ${formatNumber(r.median_return_pct, 2)}%`,
              pct: r.avg_return_pct,
              footer: `${formatNumber(r.positive_pct, 1)}% positive`,
            }))}
            emptyText="No strategy ranking yet."
          />
          <RankTable
            title="Oracle breakdown by underlying"
            rows={underlyingRows.map((r) => ({
              key: String(r.underlying),
              primary: String(r.underlying),
              meta: `${num(r.trades as number)} trades · top exit ${String(r.top_exit_strategy ?? "—")}`,
              pct: num(r.avg_oracle_best_exit_return_pct as number),
              footer: `${formatNumber(num(r.oracle_positive_pct as number), 1)}% positive`,
            }))}
            emptyText="No per-underlying breakdown yet."
          />
        </div>

        <ReportLinks files={data.files} accent="amber" />
      </div>
    </Section>
  );
}

function RankTable({
  title,
  rows,
  emptyText,
}: {
  title: string;
  rows: Array<{ key: string; primary: string; meta: string; pct: number; footer: string }>;
  emptyText: string;
}) {
  return (
    <div className="space-y-2">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">{title}</div>
      <div className="space-y-2">
        {rows.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-3 py-4 text-[11px] text-text-muted">{emptyText}</div>
        ) : (
          rows.map((r) => (
            <div key={r.key} className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2.5 text-xs">
              <div className="min-w-0 space-y-0.5">
                <div className="truncate font-semibold text-text-primary">{r.primary}</div>
                <div className="text-text-muted">{r.meta}</div>
              </div>
              <div className="shrink-0 space-y-0.5 text-right">
                <ReturnPct pct={r.pct} />
                <div className="text-text-muted">{r.footer}</div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ── Greeks-sync panel ─────────────────────────────────────────────────────────

function GreeksSyncPanel() {
  const { data, isLoading, error, refetch, isFetching, isShowingSnapshot, snapshotSavedAt } =
    usePersistentSnapshotQuery<GreeksSyncReportPayload>({
      queryKey: ["research", "greeks-sync-report"],
      queryFn: () => getLatestGreeksSyncReport().then((r) => r.data),
      refetchInterval: REFRESH_MS.summary,
      staleTime: 5000,
      refetchOnWindowFocus: false,
      storageKey: "research:greeks-sync-report",
    });

  if (isLoading && !data) {
    return (
      <Section title="Greeks-Sync research" icon={<Sigma size={16} />}>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <Loader2 size={13} className="animate-spin" /> Loading Greeks-Sync research…
        </div>
      </Section>
    );
  }

  if (!data?.available || !data.summary) {
    return (
      <Section
        title="Greeks-Sync research"
        icon={<Sigma size={16} className="text-accent-purple" />}
        description="A separate research track scored against the MACD baseline."
      >
        {isShowingSnapshot ? <div className="mb-3"><SnapshotBanner message={`Refresh failed. ${errDetail(error)}`} savedAt={snapshotSavedAt} /></div> : null}
        <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-sm text-text-muted">
          {data?.detail || "Greeks-Sync research is waiting for complete cached CE/PE history."}
        </div>
      </Section>
    );
  }

  const s = data.summary;
  const trackRows = s.comparison.track_ranking.slice(0, 4);
  const strategyRows = s.exit_analysis.strategy_ranking.slice(0, 3);

  const rightSlot = (
    <button type="button" onClick={() => refetch()} className="text-text-muted hover:text-text-primary">
      <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} />
    </button>
  );

  return (
    <Section
      title="Greeks-Sync research"
      icon={<Sigma size={16} className="text-accent-purple" />}
      description="A separate research track scored against the MACD baseline."
      rightSlot={rightSlot}
    >
      <div className="space-y-4">
        {isShowingSnapshot ? <SnapshotBanner message={`Refresh failed. ${errDetail(error)}`} savedAt={snapshotSavedAt} /> : null}

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile label="Signals" value={String(s.signals.total_signals)} detail={`${s.signals.strong_signals} strong`} color="text-accent-purple" />
          <MetricTile label="Average score" value={formatNumber(s.signals.avg_score, 1)} detail={`median ${formatNumber(s.signals.median_score, 1)}`} color="text-accent-green" />
          <MetricTile
            label="MACD confirmed"
            value={`${formatNumber(s.signals.macd_confirmed_pct, 1)}%`}
            detail={`${s.coverage.atm_monthly_pairs} ATM monthly pairs`}
            color="text-accent-amber"
          />
          <MetricTile label="Best fixed exit" value={s.exit_analysis.best_strategy} detail={`${formatNumber(s.exit_analysis.best_strategy_avg_return_pct, 2)}% avg`} color="text-accent-blue" />
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <RankTable
            title="Track comparison"
            rows={trackRows.map((r) => ({
              key: r.track,
              primary: r.track,
              meta: `${r.trades} trades · hold ${formatNumber(r.avg_hold_to_expiry_return_pct, 2)}%`,
              pct: r.avg_oracle_best_exit_return_pct,
              footer: `${formatNumber(r.positive_pct, 1)}% positive`,
            }))}
            emptyText="No track comparison yet."
          />
          <RankTable
            title="Fixed exit ranking"
            rows={strategyRows.map((r) => ({
              key: r.strategy,
              primary: r.strategy,
              meta: `${r.trades} trades · median ${formatNumber(r.median_return_pct, 2)}%`,
              pct: r.avg_return_pct,
              footer: `${formatNumber(r.positive_pct, 1)}% positive`,
            }))}
            emptyText="No strategy ranking yet."
          />
        </div>

        <ReportLinks files={data.files} accent="violet" />
      </div>
    </Section>
  );
}

// ── MACD backtest: runner + task monitor + results ───────────────────────────

function MacdBacktestPanel({ brokerReady }: { brokerReady: boolean }) {
  const qc = useQueryClient();
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const tasksQ = useQuery({
    queryKey: ["research", "macd-tasks"],
    queryFn: async () => (await api.get("/api/analysis/macd-backtest/tasks")).data as MacdTask[],
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const foQ = useQuery({
    queryKey: ["research", "fo-underlyings"],
    queryFn: () => getFoUnderlyings().then((r) => r.data as { total: number; indices: string[]; stocks: string[] }),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });

  const tasks = Array.isArray(tasksQ.data) ? tasksQ.data : [];
  const runningCount = tasks.filter((t) => t.status === "running" || t.status === "pending").length;
  const doneCount = tasks.filter((t) => t.status === "done").length;

  return (
    <Section
      title="MACD option study"
      icon={<Activity size={16} className="text-accent-green" />}
      description="ATM CE/PE MACD(12,26,9) zero-line study across monthly expiries, run off the cached Upstox history."
      rightSlot={
        <button type="button" onClick={() => tasksQ.refetch()} className="text-text-muted hover:text-text-primary">
          <RefreshCw size={13} className={tasksQ.isFetching ? "animate-spin" : ""} />
        </button>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-3 gap-3">
          <MetricTile label="Recent runs" value={String(tasks.length)} detail="last tasks tracked" />
          <MetricTile label="Running" value={String(runningCount)} detail="in progress" color={runningCount ? "text-accent-blue" : undefined} />
          <MetricTile label="Completed" value={String(doneCount)} detail="results available" color={doneCount ? "text-accent-green" : undefined} />
        </div>

        <RunForm
          brokerReady={brokerReady}
          fo={foQ.data}
          onStarted={(id) => {
            setActiveTaskId(id);
            qc.invalidateQueries({ queryKey: ["research", "macd-tasks"] });
          }}
        />

        {activeTaskId ? <TaskMonitor taskId={activeTaskId} /> : null}

        <RecentTasks tasks={tasks} activeTaskId={activeTaskId} onView={setActiveTaskId} />
      </div>
    </Section>
  );
}

function RunForm({
  brokerReady,
  fo,
  onStarted,
}: {
  brokerReady: boolean;
  fo?: { total: number; indices: string[]; stocks: string[] };
  onStarted: (taskId: string) => void;
}) {
  const [mode, setMode] = useState<"indices" | "custom" | "all">("indices");
  const [picked, setPicked] = useState<string[]>([]);
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().slice(0, 10);
  });
  const toDate = useMemo(() => new Date().toISOString().slice(0, 10), []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const indices = fo?.indices ?? ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"];
  const stocks = fo?.stocks ?? [];
  const total = fo?.total ?? 0;

  const underlyings = mode === "all" ? [] : mode === "indices" ? indices : [...indices, ...picked];
  const scopeLabel =
    mode === "all"
      ? `all F&O (${total || "auto"})`
      : mode === "indices"
        ? `${indices.length} indices`
        : `${indices.length} indices + ${picked.length} stocks`;

  const togglePick = (s: string) => setPicked((p) => (p.includes(s) ? p.filter((x) => x !== s) : [...p, s]));

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await startMacdBacktest({ underlyings, from_date: fromDate, to_date: toDate });
      onStarted((r.data as { task_id: string }).task_id);
    } catch (e) {
      setError(errDetail(e));
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || !brokerReady || (mode === "custom" && picked.length === 0);

  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 space-y-3">
      <div className="grid grid-cols-3 gap-2">
        {(
          [
            { key: "indices", label: "Indices only", sub: indices.join(", ") },
            { key: "custom", label: "Indices + stocks", sub: "pick stocks below" },
            { key: "all", label: "All F&O", sub: total ? `${total} instruments` : "auto-discover" },
          ] as const
        ).map(({ key, label, sub }) => (
          <button
            key={key}
            type="button"
            onClick={() => setMode(key)}
            className={`rounded-lg border px-3 py-2 text-left transition-colors ${
              mode === key ? "border-accent-blue/50 bg-accent-blue/10 text-accent-blue" : "border-bg-border bg-bg-primary/15 text-text-muted hover:border-bg-active"
            }`}
          >
            <div className="text-xs font-semibold">{label}</div>
            <div className="truncate text-[10.5px] opacity-70">{sub}</div>
          </button>
        ))}
      </div>

      {mode === "custom" && stocks.length > 0 ? (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-[11px] text-text-muted">
            <span>{picked.length} stocks selected</span>
            <button type="button" onClick={() => setPicked(picked.length === stocks.length ? [] : stocks)} className="text-accent-blue hover:underline">
              {picked.length === stocks.length ? "Clear" : "Select all"}
            </button>
          </div>
          <div className="flex max-h-36 flex-wrap gap-1.5 overflow-y-auto rounded-lg border border-bg-border bg-bg-primary/10 p-2">
            {stocks.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => togglePick(s)}
                className={`rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
                  picked.includes(s) ? "border-accent-green/40 bg-accent-green/15 text-accent-green" : "border-bg-border text-text-muted hover:border-accent-green/30"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3">
        <label className="space-y-1 text-[11px] text-text-muted">
          From date
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="w-full rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-sm text-text-primary"
          />
        </label>
        <label className="space-y-1 text-[11px] text-text-muted">
          To date (today)
          <input type="date" value={toDate} readOnly className="w-full rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-sm text-text-muted opacity-70" />
        </label>
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-accent-red/30 bg-accent-red/5 px-2.5 py-2 text-[11px] text-accent-red">
          <AlertCircle size={12} className="mt-0.5 shrink-0" />
          {error}
        </div>
      ) : null}

      <button
        type="button"
        onClick={run}
        disabled={disabled}
        className="flex w-full items-center justify-center gap-2 rounded-lg border border-accent-green/35 bg-accent-green/15 py-2.5 text-sm font-semibold text-accent-green transition-colors hover:bg-accent-green/25 disabled:opacity-50"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
        {busy ? "Starting…" : `Run MACD backtest — ${scopeLabel}`}
      </button>

      {!brokerReady ? (
        <p className="text-center text-[11px] text-accent-red">Connect Upstox in Settings to enable the backtest.</p>
      ) : null}
    </div>
  );
}

function TaskMonitor({ taskId }: { taskId: string }) {
  const statusQ = useQuery({
    queryKey: ["research", "macd-status", taskId],
    queryFn: async () => (await api.get(`/api/analysis/macd-backtest/status/${taskId}`)).data as MacdTask,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "done" || s === "error" ? false : 2000;
    },
    staleTime: 1000,
  });

  const status = statusQ.data;
  const resultsQ = useQuery({
    queryKey: ["research", "macd-results", taskId],
    queryFn: async () => (await api.get(`/api/analysis/macd-backtest/results/${taskId}`)).data as { results: MacdResults },
    enabled: status?.status === "done",
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });

  if (!status) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2.5 text-xs text-text-muted">
        <Loader2 size={13} className="animate-spin" /> Loading task…
      </div>
    );
  }

  const isDone = status.status === "done";
  const isError = status.status === "error";
  const isRunning = status.status === "running" || status.status === "pending";
  const variant = isDone ? "success" : isError ? "error" : "info";
  const pct = num(status.progress?.pct);

  return (
    <div className="space-y-3 rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold">
          {isDone ? <CheckCircle2 size={14} className="text-accent-green" /> : isError ? <AlertCircle size={14} className="text-accent-red" /> : <Loader2 size={14} className="animate-spin text-accent-blue" />}
          <span className={isDone ? "text-accent-green" : isError ? "text-accent-red" : "text-accent-blue"}>
            {isDone ? "Backtest complete" : isError ? "Backtest failed" : status.status === "pending" ? "Queued" : "Running backtest"}
          </span>
          <StatusBadge label={(status.underlyings?.length ? status.underlyings.join(", ") : "all F&O").slice(0, 40)} variant="neutral" />
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-text-muted">
          <Clock size={11} /> {formatDuration(status.elapsed_secs)}
        </div>
      </div>

      {isRunning ? (
        <div className="space-y-1.5">
          <ProgressBar pct={pct} label={status.progress?.current || "Initializing…"} />
          {status.progress?.processed != null ? (
            <div className="text-right font-mono text-[11px] text-text-muted">
              {status.progress.processed} / {status.progress.total} contracts
            </div>
          ) : null}
        </div>
      ) : null}

      {isError ? <div className="rounded-lg border border-accent-red/30 bg-accent-red/5 px-2.5 py-2 text-[11px] text-accent-red">{status.error || "Unknown error"}</div> : null}

      {isDone && resultsQ.data ? <MacdResultsDashboard results={resultsQ.data.results} /> : null}
      {isDone && resultsQ.isLoading ? (
        <div className="flex items-center gap-2 text-[11px] text-text-muted">
          <Loader2 size={12} className="animate-spin" /> Loading results…
        </div>
      ) : null}
    </div>
  );
}

function MacdResultsDashboard({ results }: { results: MacdResults }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const trades = results.all_trades ?? [];

  if (!results.total_opportunities && trades.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-bg-border/60 px-3 py-4 text-center text-[11px] text-text-muted">
        No MACD zero-line crossings found in the selected window for this scope.
        {results.exit_analysis?.best_strategy === "insufficient_data" ? " (insufficient cached CE/PE history)" : null}
      </div>
    );
  }

  const total = trades.length || results.total_opportunities;
  const avgMax = trades.length ? trades.reduce((acc, t) => acc + num(t.max_return_pct), 0) / trades.length : 0;
  const t50 = trades.filter((t) => t.target_50pct_hit).length;
  const t100 = trades.filter((t) => t.target_100pct_hit).length;

  const underlyingRows = Object.entries(results.by_underlying ?? {}).sort(([, a], [, b]) => num(b.avg_max_return) - num(a.avg_max_return));

  return (
    <div className="space-y-3 border-t border-bg-border pt-3">
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        <MetricTile label="Signals" value={String(results.total_opportunities)} size="sm" color="text-accent-green" />
        <MetricTile label="Avg max return" value={`${formatNumber(avgMax, 1)}%`} size="sm" color={tone(avgMax)} />
        <MetricTile label="50% hit" value={`${total ? ((t50 / total) * 100).toFixed(0) : 0}%`} detail={`${t50}/${total}`} size="sm" color="text-accent-amber" />
        <MetricTile label="100% hit" value={`${total ? ((t100 / total) * 100).toFixed(0) : 0}%`} detail={`${t100}/${total}`} size="sm" color="text-accent-amber" />
      </div>

      {underlyingRows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-bg-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-bg-border text-text-muted">
                <th className="px-3 py-1.5 text-left">Underlying</th>
                <th className="px-3 py-1.5 text-right">Signals</th>
                <th className="px-3 py-1.5 text-right">Avg max</th>
                <th className="px-3 py-1.5 text-right">Avg held</th>
                <th className="px-3 py-1.5 text-right">50%</th>
                <th className="px-3 py-1.5 text-right">100%</th>
                <th className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {underlyingRows.map(([u, d]) => {
                const open = expanded === u;
                const monthly = Object.entries(d.monthly_breakdown ?? {}).sort();
                return (
                  <FragmentRow key={u}>
                    <tr
                      onClick={() => setExpanded(open ? null : u)}
                      className="cursor-pointer border-b border-bg-border/40 hover:bg-bg-primary/20"
                    >
                      <td className="px-3 py-1.5 font-semibold text-text-primary">{u}</td>
                      <td className="px-3 py-1.5 text-right font-mono text-accent-blue">{d.opportunities}</td>
                      <td className="px-3 py-1.5 text-right"><ReturnPct pct={d.avg_max_return} /></td>
                      <td className="px-3 py-1.5 text-right"><ReturnPct pct={d.avg_held_return} /></td>
                      <td className="px-3 py-1.5 text-right font-mono text-accent-green">{(num(d.target_50_rate) * 100).toFixed(0)}%</td>
                      <td className="px-3 py-1.5 text-right font-mono text-accent-amber">{(num(d.target_100_rate) * 100).toFixed(0)}%</td>
                      <td className="px-2 py-1.5 text-right text-text-muted">{monthly.length ? open ? <ChevronUp size={12} /> : <ChevronDown size={12} /> : null}</td>
                    </tr>
                    {open && monthly.length ? (
                      <tr className="bg-bg-primary/15">
                        <td colSpan={7} className="px-3 py-2">
                          <div className="flex flex-wrap gap-1.5 text-[11px]">
                            {monthly.map(([m, c]) => (
                              <span key={m} className="rounded-md border border-bg-border px-2 py-0.5 font-mono text-text-muted">
                                {m}: <span className="text-accent-blue">{c}</span>
                              </span>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </FragmentRow>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {trades.length > 0 ? <TradePreview trades={trades} /> : null}
    </div>
  );
}

// table rows need stable fragment keys without an extra DOM node
function FragmentRow({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

function TradePreview({ trades }: { trades: MacdTrade[] }) {
  const [filter, setFilter] = useState<"all" | "CE" | "PE">("all");
  const [limit, setLimit] = useState(25);
  const rows = trades
    .filter((t) => filter === "all" || t.option_type === filter)
    .sort((a, b) => num(b.max_return_pct) - num(a.max_return_pct));

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">Trades ({trades.length})</div>
        <div className="flex items-center gap-1.5">
          {(["all", "CE", "PE"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded-md border px-2 py-0.5 text-[11px] ${filter === f ? "border-accent-blue/40 bg-accent-blue/15 text-accent-blue" : "border-bg-border text-text-muted"}`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto rounded-lg border border-bg-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-bg-border text-text-muted">
              <th className="px-3 py-1.5 text-left">Instrument</th>
              <th className="px-3 py-1.5 text-left">Entry</th>
              <th className="px-3 py-1.5 text-right">Entry ₹</th>
              <th className="px-3 py-1.5 text-right">Max ₹</th>
              <th className="px-3 py-1.5 text-right">Max ret.</th>
              <th className="px-3 py-1.5 text-right">Held ret.</th>
              <th className="px-3 py-1.5 text-right">50%</th>
              <th className="px-3 py-1.5 text-right">100%</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, limit).map((t, i) => (
              <tr key={`${t.underlying}-${t.strike}-${t.entry_time}-${i}`} className="border-b border-bg-border/30 hover:bg-bg-primary/20">
                <td className="px-3 py-1 font-semibold">
                  <span className={t.option_type === "CE" ? "text-accent-green" : "text-accent-red"}>
                    {t.underlying} {t.strike} {t.option_type}
                  </span>
                </td>
                <td className="px-3 py-1 font-mono text-text-muted">{(t.entry_time || "").slice(0, 16).replace("T", " ") || "—"}</td>
                <td className="px-3 py-1 text-right font-mono">{formatNumber(t.entry_price, 2)}</td>
                <td className="px-3 py-1 text-right font-mono">{formatNumber(t.max_price, 2)}</td>
                <td className="px-3 py-1 text-right"><ReturnPct pct={t.max_return_pct} /></td>
                <td className="px-3 py-1 text-right"><ReturnPct pct={t.held_return_pct} /></td>
                <td className="px-3 py-1 text-right">{t.target_50pct_hit ? <CheckCircle2 size={11} className="inline text-accent-green" /> : <span className="text-text-muted">—</span>}</td>
                <td className="px-3 py-1 text-right">{t.target_100pct_hit ? <CheckCircle2 size={11} className="inline text-accent-amber" /> : <span className="text-text-muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > limit ? (
        <button type="button" onClick={() => setLimit((n) => n + 25)} className="w-full py-1 text-center text-[11px] text-accent-blue hover:underline">
          Show more ({rows.length - limit} remaining)
        </button>
      ) : null}
    </div>
  );
}

function RecentTasks({ tasks, activeTaskId, onView }: { tasks: MacdTask[]; activeTaskId: string | null; onView: (id: string) => void }) {
  if (tasks.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-6 text-center text-[11px] text-text-muted">
        No backtest runs yet. Configure a scope above and run one.
      </div>
    );
  }
  const variantFor = (s: MacdTask["status"]) => (s === "done" ? "success" : s === "error" ? "error" : "info");
  return (
    <div className="space-y-2">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">Recent runs</div>
      {tasks.slice(0, 6).map((t) => (
        <div
          key={t.task_id}
          className={`flex items-center justify-between gap-3 rounded-xl border px-3 py-2 text-xs ${
            t.task_id === activeTaskId ? "border-accent-blue/40 bg-accent-blue/5" : "border-bg-border bg-bg-primary/14"
          }`}
        >
          <div className="min-w-0">
            <div className="truncate font-medium text-text-secondary">{t.underlyings?.length ? t.underlyings.join(", ") : "All F&O instruments"}</div>
            <div className="text-text-muted">
              {t.from_date} → {t.to_date} · {formatDuration(t.elapsed_secs)} · {formatIST(t.created_at)}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <StatusBadge label={t.status} variant={variantFor(t.status)} />
            {t.status === "done" || t.status === "running" ? (
              <button type="button" onClick={() => onView(t.task_id)} className="inline-flex items-center gap-1 text-accent-blue hover:underline">
                View <ArrowRight size={11} />
              </button>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Board ─────────────────────────────────────────────────────────────────────

export default function ResearchMonitorBoard() {
  const brokerQ = useQuery({
    queryKey: ["research", "broker-status"],
    queryFn: () => getAnalysisBrokerStatus().then((r) => r.data as BrokerStatus),
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const cacheQ = useQuery({
    queryKey: ["research", "cache-status"],
    queryFn: () => getResearchCacheStatus().then((r) => r.data as ResearchCacheStatus),
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const brokerReady = Boolean(brokerQ.data?.upstox_ready ?? brokerQ.data?.upstox_connected);
  const summary = cacheQ.data?.summary;
  const feedHealthy = !brokerQ.isError && !cacheQ.isError;

  const refreshAll = useCallback(() => {
    brokerQ.refetch();
    cacheQ.refetch();
  }, [brokerQ, cacheQ]);

  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <Gauge size={18} className="text-accent-green" />
              Research Monitor
            </h1>
            <p className="mt-1 text-sm text-text-muted">
              Research-cache population, validation reports, the MACD option study, and the Greeks-Sync track — one console.
            </p>
          </div>
          <button
            type="button"
            onClick={refreshAll}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
          >
            <RefreshCw size={13} className={brokerQ.isFetching || cacheQ.isFetching ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile
          label="Cache universe"
          value={summary ? String(summary.universe_total) : "—"}
          detail={summary ? `${summary.populated_symbols} research-ready` : "loading"}
        />
        <MetricTile
          label="Option candles"
          value={summary ? compactNumber(summary.option_candles) : "—"}
          detail={summary ? `+${compactNumber(summary.option_candles_added_last_30m)} in 30m` : "loading"}
          color="text-accent-amber"
        />
        <MetricTile
          label="Upstox"
          value={brokerReady ? "ready" : brokerQ.isError ? "offline" : "attention"}
          detail={brokerQ.data?.upstox_token_health?.expires_at_ist ? `token to ${formatIST(brokerQ.data.upstox_token_health.expires_at_ist)}` : "data source"}
          color={brokerReady ? "text-accent-green" : "text-accent-amber"}
        />
        <MetricTile
          label="Feed"
          value={feedHealthy ? "live" : "stale"}
          detail={cacheQ.dataUpdatedAt ? formatIST(cacheQ.dataUpdatedAt) : ""}
          color={feedHealthy ? "text-accent-green" : "text-accent-red"}
        />
      </section>

      <BrokerStatusStrip />
      <ResearchCachePanel />
      <ValidationReportPanel />
      <MacdBacktestPanel brokerReady={brokerReady} />
      <GreeksSyncPanel />
    </div>
  );
}
