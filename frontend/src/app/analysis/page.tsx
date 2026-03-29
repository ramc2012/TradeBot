"use client";
import { useState, useEffect, useCallback } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity, Play, RefreshCw, AlertCircle, CheckCircle2, Loader2,
  TrendingUp, ChevronDown, ChevronUp, BarChart2,
  Target, Zap, ArrowRight, Clock, Link2, XCircle, Database, FileText, Download,
} from "lucide-react";
import {
  startMacdBacktest, getMacdBacktestStatus, getMacdBacktestResults,
  listMacdBacktestTasks, getAnalysisBrokerStatus, getFoUnderlyings,
  getResearchCacheStatus, getLatestValidationReport, getLatestGreeksSyncReport, API_URL,
} from "@/lib/api";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";

// ── Types ──────────────────────────────────────────────────────────────────────

interface Trade {
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
  bars_to_max: number;
  final_price: number;
}

interface BacktestResults {
  total_opportunities: number;
  by_underlying: Record<string, {
    opportunities: number;
    avg_max_return: number;
    avg_held_return: number;
    target_50_rate: number;
    target_100_rate: number;
    monthly_breakdown: Record<string, number>;
  }>;
  by_month: Record<string, {
    count: number;
    avg_max_return: number;
    ce_count: number;
    pe_count: number;
  }>;
  exit_analysis: {
    hold_to_expiry_avg: number;
    target_50_hit_rate: number;
    target_100_hit_rate: number;
    best_strategy: string;
    strategy_comparison: Record<string, { avg_return: number; hit_rate: number }>;
  };
  all_trades: Trade[];
}

interface ResearchSymbol {
  symbol: string;
  kind: string;
  stage: "queued" | "metadata" | "spot" | "contracts" | "populating" | "populated";
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
    last_empty_contract_touch_at: string | null;
    option_candles_added_last_30m: number;
    complete_contracts_touched_last_30m: number;
    empty_contracts_touched_last_30m: number;
  };
  scheduler: {
    state: "idle" | "running" | "waiting" | "rate_limit_cooldown" | "stalled";
    label: string;
    detail: string;
    pause_assumed: boolean;
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

interface ValidationBreakdownRow {
  [key: string]: string | number;
}

interface ValidationReportSummary {
  generated_at: string;
  coverage: {
    underlyings_with_option_data: number;
    atm_monthly_pairs: number;
    complete_cached_contracts: number;
    cached_option_candles: number;
  };
  opportunities: {
    total_trades: number;
    months: string[];
    underlyings: string[];
  };
  exit_analysis: {
    best_strategy: string;
    best_strategy_avg_return_pct: number;
    hold_to_expiry_avg_return_pct: number;
    avg_max_return_pct: number;
    positive_pct: number;
    strategy_ranking: ValidationStrategyRow[];
  };
  breakdowns: {
    by_underlying: ValidationBreakdownRow[];
    by_option_type: ValidationBreakdownRow[];
    by_month: ValidationBreakdownRow[];
    by_iv_regime: ValidationBreakdownRow[];
    by_oi_change_regime: ValidationBreakdownRow[];
    by_volume_change_regime: ValidationBreakdownRow[];
    by_oi_pcr_regime: ValidationBreakdownRow[];
    by_volume_pcr_regime: ValidationBreakdownRow[];
  };
}

interface ValidationReportPayload {
  available: boolean;
  live?: boolean;
  detail?: string;
  report_key?: string;
  generated_at?: string | null;
  source_updated_at?: string | null;
  summary?: ValidationReportSummary;
  markdown_preview?: string;
  files?: {
    report_markdown_url?: string;
    summary_json_url?: string;
    trades_csv_url?: string;
    coverage_csv_url?: string;
    chain_summary_csv_url?: string;
  };
}

interface GreeksSyncTrackRow {
  track: string;
  trades: number;
  avg_oracle_best_exit_return_pct: number;
  avg_max_return_pct: number;
  avg_hold_to_expiry_return_pct: number;
  positive_pct: number;
}

interface GreeksSyncStrategyRow {
  strategy: string;
  trades: number;
  avg_return_pct: number;
  median_return_pct: number;
  positive_pct: number;
}

interface GreeksSyncReportPayload {
  available: boolean;
  live?: boolean;
  detail?: string;
  report_key?: string;
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
    comparison: {
      track_ranking: GreeksSyncTrackRow[];
    };
    exit_analysis: {
      best_strategy: string;
      best_strategy_avg_return_pct: number;
      strategy_ranking: GreeksSyncStrategyRow[];
    };
  };
  files?: {
    report_markdown_url?: string;
    summary_json_url?: string;
    trades_csv_url?: string;
    coverage_csv_url?: string;
    chain_summary_csv_url?: string;
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function ReturnBadge({ pct }: { pct: number }) {
  const color = pct >= 100 ? "text-accent-green" : pct >= 50 ? "text-accent-amber" : pct >= 0 ? "text-text-secondary" : "text-accent-red";
  return <span className={clsx("font-mono text-xs font-bold", color)}>{pct >= 0 ? "+" : ""}{pct.toFixed(1)}%</span>;
}

function StatCard({ label, value, sub, color = "text-text-primary" }: {
  label: string; value: string | number; sub?: string; color?: string;
}) {
  return (
    <div className="bg-bg-secondary border border-bg-border rounded p-3 space-y-1">
      <div className="text-xs text-text-muted">{label}</div>
      <div className={clsx("text-xl font-bold font-mono", color)}>{value}</div>
      {sub && <div className="text-xs text-text-muted">{sub}</div>}
    </div>
  );
}

function ProgressBar({ pct, label }: { pct: number; label?: string }) {
  return (
    <div className="space-y-1">
      {label && <div className="text-xs text-text-muted truncate">{label}</div>}
      <div className="h-1.5 bg-bg-border rounded-full overflow-hidden">
        <div className="h-full bg-accent-blue rounded-full transition-all duration-500"
          style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
      <div className="text-xs text-text-muted text-right font-mono">{pct.toFixed(0)}%</div>
    </div>
  );
}

function formatCompactNumber(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${value}`;
}

function formatRelativeTime(value?: string | null) {
  if (!value) return "—";
  const deltaMs = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(deltaMs) || deltaMs < 0) return "just now";
  const minutes = Math.floor(deltaMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatLocalTimestamp(value?: string | null) {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return dt.toLocaleString();
}

function formatCountdown(seconds?: number | null) {
  if (seconds == null) return "—";
  const safe = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safe / 60);
  const secs = safe % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    const remMinutes = minutes % 60;
    return `${hours}h ${remMinutes.toString().padStart(2, "0")}m`;
  }
  return `${minutes}m ${secs.toString().padStart(2, "0")}s`;
}

function getErrorDetail(error: unknown) {
  return (error as any)?.response?.data?.detail || (error as Error)?.message || "Could not load cache status";
}

function SnapshotBanner({
  message,
  snapshotSavedAt,
}: {
  message: string;
  snapshotSavedAt?: string | null;
}) {
  return (
    <div className="rounded border border-accent-amber/30 bg-accent-amber/5 p-3 text-xs text-text-muted">
      <div className="flex items-center gap-2 text-accent-amber">
        <AlertCircle size={12} />
        <span className="font-medium">Showing last successful snapshot</span>
      </div>
      <div className="mt-1">
        {message}
        {snapshotSavedAt && ` · saved ${formatRelativeTime(snapshotSavedAt)} (${formatLocalTimestamp(snapshotSavedAt)})`}
      </div>
    </div>
  );
}

function stageTone(stage: ResearchSymbol["stage"]) {
  switch (stage) {
    case "populated":
      return "bg-accent-green/10 text-accent-green border-accent-green/30";
    case "populating":
      return "bg-accent-blue/10 text-accent-blue border-accent-blue/30";
    case "contracts":
      return "bg-accent-amber/10 text-accent-amber border-accent-amber/30";
    case "spot":
      return "bg-accent-blue/10 text-accent-blue border-accent-blue/30";
    case "metadata":
      return "bg-text-muted/10 text-text-muted border-bg-border";
    default:
      return "bg-bg-secondary text-text-muted border-bg-border";
  }
}

function buildFallbackResearchScheduler(nowMs: number, pauseStartedAt: number | null) {
  const pollMinutes = 30;
  const effectiveRefreshStart = pauseStartedAt ?? nowMs;
  const nextBatchAtMs = effectiveRefreshStart + pollMinutes * 60_000;
  const secondsUntilNextBatch = Math.max(0, Math.ceil((nextBatchAtMs - nowMs) / 1000));

  return {
    state: "waiting" as const,
    label: "Live status refresh delayed",
    detail: `Status polling timed out. The worker may still be running, but the latest scheduler state could not be confirmed.`,
    pause_assumed: false,
    poll_minutes: pollMinutes,
    rate_limit_window_minutes: pollMinutes,
    cooldown_minutes: Math.max(1, Math.floor(pollMinutes / 3)),
    next_batch_at: new Date(nextBatchAtMs).toISOString(),
    seconds_until_next_batch: secondsUntilNextBatch,
    estimated_window_available_pct: null,
    estimated_window_used_pct: null,
    last_batch_activity_at: pauseStartedAt ? new Date(pauseStartedAt).toISOString() : null,
    last_run_started_at: null,
    last_run_completed_at: null,
  };
}

function normaliseSnapshotScheduler(scheduler: ResearchCacheStatus["scheduler"], isSnapshot: boolean) {
  if (!isSnapshot || scheduler.state !== "rate_limit_cooldown") {
    return scheduler;
  }

  return {
    ...scheduler,
    state: "waiting" as const,
    label: "Showing cached scheduler snapshot",
    detail: "The last live refresh failed, so the previous cooldown state may be stale. Wait for the next successful poll to confirm current ingestion status.",
    pause_assumed: false,
  };
}

// ── BrokerStatusCard ────────────────────────────────────────────────────────────

function BrokerStatusCard() {
  const {
    data,
    error,
    isError,
    isLoading,
    isShowingSnapshot,
    snapshotSavedAt,
  } = usePersistentSnapshotQuery({
    queryKey: ["analysisBrokerStatus"],
    queryFn: () => getAnalysisBrokerStatus().then(r => r.data),
    refetchInterval: 10000,
    staleTime: 5000,
    storageKey: "analysis:broker-status",
  });

  if (isLoading && !data) return (
    <div className="card p-3 flex items-center gap-2 text-xs text-text-muted">
      <Loader2 size={12} className="animate-spin" /> Checking broker connections…
    </div>
  );

  if (!data) {
    return (
      <div className="card p-3 flex items-center gap-2 text-xs text-accent-red">
        <AlertCircle size={12} />
        {getErrorDetail(error)}
      </div>
    );
  }

  const upstoxOk = data?.upstox_connected;
  const upstoxReady = data?.upstox_ready ?? upstoxOk;
  const upstoxHealth = data?.upstox_token_health;
  const breezeOk = data?.breeze_connected;
  const dataSource = data?.data_sources;
  const UpstoxIcon = upstoxReady ? CheckCircle2 : upstoxOk ? AlertCircle : XCircle;
  const upstoxTone = upstoxReady
    ? "border-accent-green/30 bg-accent-green/5"
    : upstoxOk
      ? "border-accent-amber/30 bg-accent-amber/5"
      : "border-accent-red/30 bg-accent-red/5";
  const upstoxTextTone = upstoxReady
    ? "text-accent-green"
    : upstoxOk
      ? "text-accent-amber"
      : "text-accent-red";
  const upstoxLabel = upstoxReady
    ? "Upstox Ready"
    : upstoxOk
      ? "Upstox Connected · Token Attention"
      : "Upstox Not Connected";

  return (
    <div className="space-y-2">
      {isShowingSnapshot && (
        <SnapshotBanner
          message={getErrorDetail(error)}
          snapshotSavedAt={snapshotSavedAt}
        />
      )}
      {/* Upstox row */}
      <div className={clsx(
        "card p-3 flex items-start justify-between gap-3 text-xs",
        upstoxTone
      )}>
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <UpstoxIcon size={13} className={upstoxTextTone} />
            <span className={clsx("font-medium", upstoxTextTone)}>
              {upstoxLabel}
            </span>
            {upstoxOk && data?.upstox_token_preview && (
              <span className="text-text-muted font-mono">{data.upstox_token_preview}</span>
            )}
            <span className="text-text-muted">· spot prices &amp; active contracts</span>
          </div>
          {upstoxHealth?.message && (
            <div className={clsx("pl-5 text-[11px]", upstoxReady ? "text-text-muted" : "text-accent-amber")}>
              {upstoxHealth.message}
              {upstoxHealth.checked_at && (
                <span className="text-text-muted"> · checked {formatRelativeTime(upstoxHealth.checked_at)}</span>
              )}
            </div>
          )}
        </div>
        {!upstoxReady && (
          <a href="/settings" className="flex items-center gap-1 text-accent-blue hover:underline">
            <Link2 size={11} /> Connect
          </a>
        )}
      </div>
      {/* ICICI Breeze row */}
      <div className={clsx(
        "card p-3 flex items-center justify-between text-xs",
        breezeOk ? "border-accent-green/30 bg-accent-green/5" : "border-accent-amber/30 bg-accent-amber/5"
      )}>
        <div className="flex items-center gap-2">
          {breezeOk
            ? <CheckCircle2 size={13} className="text-accent-green" />
            : <AlertCircle size={13} className="text-accent-amber" />}
          <span className={breezeOk ? "text-accent-green font-medium" : "text-accent-amber font-medium"}>
            ICICI Breeze {breezeOk ? "Connected" : "Not Connected"}
          </span>
          <span className="text-text-muted">
            · {breezeOk ? "3-year expired options history available" : "needed for expired options backtest"}
          </span>
        </div>
        {!breezeOk && (
          <a href="/settings" className="flex items-center gap-1 text-accent-blue hover:underline">
            <Link2 size={11} /> Connect
          </a>
        )}
      </div>
      {/* Data source note */}
      {dataSource?.note && (
        <div className="text-xs text-text-muted px-1">{dataSource.note}</div>
      )}
    </div>
  );
}

// ── RunForm ─────────────────────────────────────────────────────────────────────

function RunForm({ onStarted }: { onStarted: (taskId: string) => void }) {
  const [mode, setMode] = useState<"all" | "indices" | "custom">("indices");
  const [customSelected, setCustomSelected] = useState<string[]>([]);
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date(); d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().split("T")[0];
  });
  const [toDate] = useState(() => new Date().toISOString().split("T")[0]);
  const [error, setError] = useState("");

  const {
    data: brokerStatus,
    error: brokerStatusError,
    isError: brokerStatusUnavailable,
    isShowingSnapshot: showingBrokerSnapshot,
    snapshotSavedAt: brokerSnapshotSavedAt,
  } = usePersistentSnapshotQuery({
    queryKey: ["analysisBrokerStatus"],
    queryFn: () => getAnalysisBrokerStatus().then(r => r.data),
    staleTime: 5000,
    storageKey: "analysis:broker-status",
  });

  const { data: foData, isLoading: loadingFo } = useQuery({
    queryKey: ["foUnderlyings"],
    queryFn: () => getFoUnderlyings().then(r => r.data),
    staleTime: 300000,
  });

  const NSE_INDICES: string[] = foData?.indices ?? ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"];
  const ALL_STOCKS: string[] = foData?.stocks ?? [];
  const totalFo = foData?.total ?? 0;

  const toggleStock = (s: string) =>
    setCustomSelected(p => p.includes(s) ? p.filter(x => x !== s) : [...p, s]);

  const getUnderlyings = () => {
    if (mode === "all") return [];          // empty = auto-discover all
    if (mode === "indices") return NSE_INDICES;
    return [...NSE_INDICES, ...customSelected];
  };

  const getLabel = () => {
    if (mode === "all") return `All F&O instruments (${totalFo > 0 ? `~${totalFo}` : "auto-discover"})`;
    if (mode === "indices") return `4 NSE indices only`;
    return `${NSE_INDICES.length} indices + ${customSelected.length} stocks`;
  };

  const mut = useMutation({
    mutationFn: () => startMacdBacktest({
      underlyings: getUnderlyings(),
      from_date: fromDate,
      to_date: toDate,
    }),
    onSuccess: (r) => { onStarted(r.data.task_id); setError(""); },
    onError: (e: any) => setError(e?.response?.data?.detail || "Failed to start"),
  });

  const lastKnownReady = brokerStatus?.upstox_ready ?? brokerStatus?.upstox_connected;
  const isReady = !brokerStatusUnavailable && lastKnownReady;
  const tokenMessage = brokerStatus?.upstox_token_health?.message;

  return (
    <div className="card p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Activity size={16} className="text-accent-green" />
        <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">
          Configure Backtest
        </h2>
      </div>

      {/* Strategy description */}
      <div className="bg-bg-secondary border border-bg-border rounded p-3 text-xs text-text-muted space-y-1">
        <p><strong className="text-text-primary">Strategy:</strong> For each monthly expiry → get spot price on first trading day of that month → compute ATM strike → fetch 30-min candles for ATM CE & PE → MACD(12,26,9) → buy when MACD crosses zero line → measure max move to expiry.</p>
        <p className="text-accent-amber flex items-center gap-1"><AlertCircle size={10} /> Uses connected Upstox session automatically. No token input needed.</p>
      </div>

      {/* Instrument scope */}
      <div className="space-y-3">
        <div className="text-xs font-semibold text-text-secondary">Instrument Scope</div>
        <div className="grid grid-cols-3 gap-2">
          {([
            { key: "indices", label: "Indices Only", sub: "NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY", color: "accent-blue" },
            { key: "custom", label: "Indices + Stocks", sub: "Select specific stocks below", color: "accent-green" },
            { key: "all", label: "All F&O", sub: loadingFo ? "Loading…" : `${totalFo || "All"} instruments`, color: "accent-amber" },
          ] as const).map(({ key, label, sub, color }) => (
            <button key={key} onClick={() => setMode(key)}
              className={clsx(
                "rounded border p-2.5 text-left transition-colors space-y-0.5",
                mode === key
                  ? `bg-${color}/15 border-${color}/50 text-${color}`
                  : "bg-bg-hover border-bg-border text-text-muted hover:border-bg-active"
              )}>
              <div className="text-xs font-semibold">{label}</div>
              <div className="text-xs opacity-70">{sub}</div>
            </button>
          ))}
        </div>

        {/* Stock picker for custom mode */}
        {mode === "custom" && ALL_STOCKS.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-text-muted">
              <span>Select additional stocks ({customSelected.length} selected)</span>
              <button onClick={() => setCustomSelected(ALL_STOCKS)} className="text-accent-blue hover:underline">Select All</button>
            </div>
            <div className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto p-1">
              {ALL_STOCKS.map(s => (
                <button key={s} onClick={() => toggleStock(s)}
                  className={clsx(
                    "px-2 py-0.5 rounded text-xs border transition-colors",
                    customSelected.includes(s)
                      ? "bg-accent-green/20 border-accent-green/40 text-accent-green"
                      : "border-bg-border text-text-muted hover:border-accent-green/30"
                  )}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Date range */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-xs text-text-muted">From Date</label>
          <input type="date" value={fromDate} onChange={e => setFromDate(e.target.value)}
            className="terminal-input w-full text-sm" />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-text-muted">To Date (today)</label>
          <input type="date" value={toDate} readOnly className="terminal-input w-full text-sm opacity-60" />
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-xs text-accent-red bg-accent-red/5 border border-accent-red/20 rounded p-2">
          <AlertCircle size={12} className="mt-0.5 shrink-0" /> {error}
        </div>
      )}

      {showingBrokerSnapshot && (
        <div className="flex items-start gap-2 text-xs text-accent-amber bg-accent-amber/5 border border-accent-amber/20 rounded p-2">
          <AlertCircle size={12} className="mt-0.5 shrink-0" />
          <span>
            Live broker verification is unavailable. Last known Upstox state was{" "}
            <strong className="text-accent-amber">{lastKnownReady ? "ready" : "not ready"}</strong>.
            {" "}
            {getErrorDetail(brokerStatusError)}
            {brokerSnapshotSavedAt && ` · saved ${formatRelativeTime(brokerSnapshotSavedAt)}`}
          </span>
        </div>
      )}

      <button
        onClick={() => mut.mutate()}
        disabled={mut.isPending || !isReady || (mode === "custom" && customSelected.length === 0)}
        className="w-full py-2.5 rounded text-sm bg-accent-green/20 border border-accent-green/30 text-accent-green hover:bg-accent-green/30 disabled:opacity-50 flex items-center justify-center gap-2 font-semibold transition-colors">
        {mut.isPending ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
        {mut.isPending ? "Starting…" : `Run MACD Backtest — ${getLabel()}`}
      </button>

      {!isReady && (
        <p className="text-xs text-accent-red text-center">
          {brokerStatusUnavailable
            ? "Live Upstox status is unavailable, so starting a new backtest is disabled until the broker check recovers."
            : (tokenMessage || "Connect Upstox in Settings first to enable the backtest.")}
        </p>
      )}
    </div>
  );
}

// ── TaskMonitor ──────────────────────────────────────────────────────────────────

function TaskMonitor({ taskId, onResults }: { taskId: string; onResults: (r: BacktestResults) => void }) {
  const { data: status, refetch } = useQuery({
    queryKey: ["macdStatus", taskId],
    queryFn: () => getMacdBacktestStatus(taskId).then(r => r.data),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return (s === "done" || s === "error") ? false : 2000;
    },
    staleTime: 1000,
  });

  useEffect(() => {
    if (status?.status === "done") {
      getMacdBacktestResults(taskId).then(r => onResults(r.data.results));
    }
  }, [status?.status, taskId, onResults]);

  if (!status) return (
    <div className="card p-4 text-xs text-text-muted flex items-center gap-2">
      <Loader2 size={12} className="animate-spin" /> Loading…
    </div>
  );

  const pct = status.progress?.pct ?? 0;
  const isDone = status.status === "done";
  const isError = status.status === "error";
  const isRunning = status.status === "running";
  const isPending = status.status === "pending";

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold">
          {isDone ? <CheckCircle2 size={14} className="text-accent-green" /> :
           isError ? <AlertCircle size={14} className="text-accent-red" /> :
           <Loader2 size={14} className="animate-spin text-accent-blue" />}
          <span className={isDone ? "text-accent-green" : isError ? "text-accent-red" : "text-accent-blue"}>
            {isDone ? "Backtest Complete" : isError ? "Backtest Failed" : isPending ? "Queued…" : "Running Backtest…"}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <Clock size={11} /> {status.elapsed_secs}s
          {(isRunning || isPending) && (
            <button onClick={() => refetch()} className="ml-1 text-text-muted hover:text-text-primary">
              <RefreshCw size={11} />
            </button>
          )}
        </div>
      </div>

      {(isRunning || isPending) && (
        <div className="space-y-2">
          <ProgressBar pct={pct} label={status.progress?.current || "Initializing…"} />
          {status.progress?.processed != null && (
            <div className="text-xs text-text-muted text-right font-mono">
              {status.progress.processed} / {status.progress.total} contracts processed
            </div>
          )}
        </div>
      )}

      {isError && (
        <div className="text-xs text-accent-red bg-accent-red/5 border border-accent-red/20 rounded p-2">
          {status.error}
        </div>
      )}

      {isDone && status.results_summary && (
        <div className="grid grid-cols-3 gap-2">
          {[
            { label: "Signals Found", value: status.results_summary.total_opportunities, color: "text-accent-green" },
            { label: "Contracts", value: status.results_summary.trade_count, color: "text-text-primary" },
            { label: "Underlyings", value: Object.keys(status.results_summary.by_underlying ?? {}).length, color: "text-accent-blue" },
          ].map(({ label, value, color }) => (
            <div key={label} className="bg-bg-secondary rounded p-2 text-center border border-bg-border">
              <div className={clsx("text-lg font-bold font-mono", color)}>{value}</div>
              <div className="text-xs text-text-muted">{label}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function PopulationMonitor() {
  const [showAllActive, setShowAllActive] = useState(false);
  const [showAllAvailable, setShowAllAvailable] = useState(false);
  const [fallbackPauseStartedAt, setFallbackPauseStartedAt] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const {
    data,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
    isShowingSnapshot,
    snapshotSavedAt,
  } = usePersistentSnapshotQuery<ResearchCacheStatus>({
    queryKey: ["researchCacheStatus"],
    queryFn: () => getResearchCacheStatus().then(r => r.data),
    refetchInterval: 5000,
    staleTime: 2000,
    storageKey: "analysis:research-cache-status",
  });

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (isError && !data) {
      setFallbackPauseStartedAt((prev) => prev ?? Date.now());
      return;
    }
    setFallbackPauseStartedAt(null);
  }, [data, isError]);

  const fallbackScheduler = buildFallbackResearchScheduler(nowMs, fallbackPauseStartedAt);
  const scheduler = !data
    ? fallbackScheduler
    : normaliseSnapshotScheduler(data.scheduler ?? fallbackScheduler, isShowingSnapshot);
  const estimatedAvailablePct = scheduler.estimated_window_available_pct;
  const estimatedUsedPct = scheduler.estimated_window_used_pct;
  const countdownLabel = formatCountdown(scheduler.seconds_until_next_batch);
  const showPauseBanner = !isShowingSnapshot && scheduler.state === "rate_limit_cooldown";
  const errorDetail = getErrorDetail(error);

  if (isLoading && !data) {
    return (
      <div className="card p-4 flex items-center gap-2 text-xs text-text-muted">
        <Loader2 size={12} className="animate-spin" /> Loading research cache status…
      </div>
    );
  }

  if (!data) {
    return (
      <div className="card p-4 space-y-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-accent-amber">
          <Clock size={14} /> {scheduler.label}
        </div>
        <div className="text-xs text-text-muted">{scheduler.detail}</div>
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-bg-secondary border border-bg-border rounded p-3 space-y-1">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Next Batch Starts In</div>
            <div className="text-lg font-mono font-bold text-accent-amber">{countdownLabel}</div>
            <div className="text-xs text-text-muted">{formatLocalTimestamp(scheduler.next_batch_at)}</div>
          </div>
          <div className="bg-bg-secondary border border-bg-border rounded p-3 space-y-1">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Limit Available</div>
            <div className="text-lg font-mono font-bold text-accent-green">
              {estimatedAvailablePct != null ? `${estimatedAvailablePct.toFixed(1)}%` : "—"}
            </div>
            <div className="text-xs text-text-muted">
              {estimatedUsedPct != null ? `${estimatedUsedPct.toFixed(1)}% used in current window` : `Upstox ${scheduler.rate_limit_window_minutes}m window`}
            </div>
          </div>
        </div>
        <div className="text-[11px] text-text-muted">Last error: {errorDetail}</div>
      </div>
    );
  }

  const { summary, symbols } = data;
  const processedContracts = summary.research_contracts_processed;
  const expiryPct = summary.universe_total ? (summary.underlyings_with_expiries / summary.universe_total) * 100 : 0;
  const spotPct = summary.universe_total ? (summary.underlyings_with_spot / summary.universe_total) * 100 : 0;
  const selectionPct = summary.expiry_total ? (summary.selection_spots_ready / summary.expiry_total) * 100 : 0;
  const discoveryPct = summary.expiry_total ? (summary.expiries_discovered / summary.expiry_total) * 100 : 0;
  const syncPct = summary.research_contract_target ? (processedContracts / summary.research_contract_target) * 100 : 0;
  const schedulerTone = scheduler.state === "running"
    ? "border-accent-blue/30 bg-accent-blue/5 text-accent-blue"
    : scheduler.state === "stalled"
      ? "border-accent-red/30 bg-accent-red/5 text-accent-red"
    : scheduler.state === "waiting"
      ? "border-accent-amber/30 bg-accent-amber/5 text-accent-amber"
      : scheduler.state === "rate_limit_cooldown"
        ? "border-accent-amber/30 bg-accent-amber/5 text-accent-amber"
        : "border-bg-border bg-bg-secondary text-text-secondary";
  const schedulerMeta = scheduler.state === "running"
    ? `started ${formatRelativeTime(scheduler.last_run_started_at)}`
    : scheduler.state === "stalled"
      ? scheduler.next_batch_at
        ? `overdue since ${formatLocalTimestamp(scheduler.next_batch_at)}`
        : `last completed ${formatRelativeTime(scheduler.last_run_completed_at)}`
    : scheduler.next_batch_at
      ? `next batch ${formatLocalTimestamp(scheduler.next_batch_at)}`
      : `last completed ${formatRelativeTime(scheduler.last_run_completed_at)}`;

  const activeQueue = symbols
    .filter(s => !s.research_ready && (s.active_now || ["metadata", "spot", "contracts", "populating"].includes(s.stage)))
    .sort((a, b) => {
      const timeA = a.last_activity_at ? new Date(a.last_activity_at).getTime() : 0;
      const timeB = b.last_activity_at ? new Date(b.last_activity_at).getTime() : 0;
      return timeB - timeA || b.progress_pct - a.progress_pct || a.symbol.localeCompare(b.symbol);
    });

  const researchReadySymbols = symbols
    .filter(s => s.research_ready)
    .sort((a, b) => b.option_candles - a.option_candles || b.complete_contracts - a.complete_contracts || a.symbol.localeCompare(b.symbol));

  const activeVisible = showAllActive ? activeQueue : activeQueue.slice(0, 12);
  const availableVisible = showAllAvailable ? researchReadySymbols : researchReadySymbols.slice(0, 12);

  return (
    <div className="card p-5 space-y-4">
      {isShowingSnapshot && (
        <SnapshotBanner
          message={`Research cache polling is unavailable. ${errorDetail}`}
          snapshotSavedAt={snapshotSavedAt}
        />
      )}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <Database size={15} className="text-accent-blue" />
          <div>
            <div className="text-sm font-semibold text-text-secondary">Research Cache Monitor</div>
            <div className="text-xs text-text-muted">
              Live Timescale population state for the recurring Upstox sync
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs text-text-muted">
          <span className="flex items-center gap-1">
            {isFetching ? <Loader2 size={11} className="animate-spin" /> : <Activity size={11} className="text-accent-green" />}
            Last activity {formatRelativeTime(summary.recent_activity_at)}
          </span>
          <span className="flex items-center gap-1">
            <Database size={11} className="text-accent-blue" />
            Last candle sync {formatRelativeTime(summary.last_successful_option_sync_at)}
          </span>
          <button onClick={() => refetch()} className="text-text-muted hover:text-text-primary">
            <RefreshCw size={12} />
          </button>
        </div>
      </div>

      <div className={clsx("rounded border p-3", schedulerTone)}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="text-sm font-semibold">{scheduler.label}</div>
            <div className="mt-1 text-xs text-text-muted">{scheduler.detail}</div>
          </div>
          <div className="text-right text-xs">
            <div className="font-medium">{schedulerMeta}</div>
            {scheduler.state === "rate_limit_cooldown" && (
              <div className="text-text-muted">resume in {countdownLabel}</div>
            )}
          </div>
        </div>
      </div>

      {showPauseBanner && (
        <div className={clsx(
          "rounded border p-3 space-y-3",
          "border-accent-amber/30 bg-accent-amber/5"
        )}>
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-sm font-semibold text-accent-amber">
                <Clock size={14} /> {scheduler.label}
              </div>
              <div className="text-xs text-text-muted">{scheduler.detail}</div>
            </div>
            <div className="text-right">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Next Batch Starts In</div>
              <div className="text-xl font-mono font-bold text-accent-amber">{countdownLabel}</div>
              <div className="text-xs text-text-muted">{formatLocalTimestamp(scheduler.next_batch_at)}</div>
            </div>
          </div>
          <div className="grid md:grid-cols-2 gap-3">
            <div className="bg-bg-secondary border border-bg-border rounded p-3 space-y-1">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Limit Available In Current Window</div>
              <div className="text-lg font-mono font-bold text-accent-green">
                {estimatedAvailablePct != null ? `${estimatedAvailablePct.toFixed(1)}%` : "—"}
              </div>
              <div className="text-xs text-text-muted">
                {estimatedUsedPct != null
                  ? `${estimatedUsedPct.toFixed(1)}% of the current request budget already used`
                  : `Upstox ${scheduler.rate_limit_window_minutes}m request window`}
              </div>
            </div>
            <div className="bg-bg-secondary border border-bg-border rounded p-3 space-y-1">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Window Model</div>
              <div className="text-sm font-semibold text-text-primary">
                {scheduler.cooldown_minutes}m cooldown after a {scheduler.poll_minutes}m sync cycle
              </div>
              <div className="text-xs text-text-muted">
                Last batch activity {formatRelativeTime(scheduler.last_batch_activity_at)}
                {isError && ` · UI kept the last successful snapshot because polling timed out`}
              </div>
            </div>
          </div>
          {isError && (
            <div className="text-[11px] text-text-muted">
              Polling note: {errorDetail}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Universe" value={summary.universe_total} sub={`${summary.underlyings_with_expiries} metadata ready`} color="text-text-primary" />
        <StatCard label="Spot Synced" value={summary.underlyings_with_spot} sub={`${summary.selection_spots_ready} selection bars`} color="text-accent-blue" />
        <StatCard
          label="Research Ready"
          value={summary.populated_symbols}
          sub={`${summary.research_contracts_processed}/${summary.research_contract_target || 0} required contracts synced`}
          color="text-accent-green"
        />
        <StatCard
          label="Option Candles"
          value={formatCompactNumber(summary.option_candles)}
          sub={`+${formatCompactNumber(summary.option_candles_added_last_30m)} in 30m · ${summary.option_contracts} contracts locally`}
          color="text-accent-amber"
        />
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="space-y-2">
          <ProgressBar pct={expiryPct} label={`Expiry metadata · ${summary.underlyings_with_expiries}/${summary.universe_total} underlyings`} />
          <ProgressBar pct={spotPct} label={`Spot history · ${summary.underlyings_with_spot}/${summary.universe_total} underlyings`} />
          <ProgressBar pct={selectionPct} label={`Selection-day spot · ${summary.selection_spots_ready}/${summary.expiry_total || 0} expiry buckets`} />
        </div>
        <div className="space-y-2">
          <ProgressBar pct={discoveryPct} label={`Contract discovery · ${summary.expiries_discovered}/${summary.expiry_total || 0} expiry buckets`} />
          <ProgressBar pct={syncPct} label={`Research sync target · ${processedContracts}/${summary.research_contract_target || 0} required contracts`} />
          <div className="flex flex-wrap gap-1.5 pt-1">
            {Object.entries(summary.stage_counts ?? {}).map(([stage, count]) => (
              <span key={stage} className={clsx("px-2 py-0.5 rounded border text-[11px] uppercase tracking-wide", stageTone(stage as ResearchSymbol["stage"]))}>
                {stage}: {count}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
              Being Populated ({activeQueue.length})
            </div>
            {activeQueue.length > 12 && (
              <button onClick={() => setShowAllActive(v => !v)} className="text-xs text-accent-blue hover:underline">
                {showAllActive ? "Show Less" : `Show All (${activeQueue.length})`}
              </button>
            )}
          </div>
          <div className="space-y-2 max-h-[28rem] overflow-y-auto pr-1">
            {activeVisible.map(symbol => (
              <div key={`active-${symbol.symbol}`} className="bg-bg-secondary border border-bg-border rounded p-3 space-y-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-text-primary">{symbol.symbol}</span>
                      <span className="text-[11px] text-text-muted">{symbol.kind}</span>
                      <span className={clsx("px-1.5 py-0.5 rounded border text-[10px] uppercase tracking-wide", stageTone(symbol.stage))}>
                        {symbol.stage}
                      </span>
                      {symbol.active_now && <span className="text-[10px] text-accent-green">active now</span>}
                    </div>
                    <div className="text-xs text-text-muted">
                      {symbol.research_contracts_processed}/{symbol.research_contract_target || 0} required · {formatCompactNumber(symbol.option_candles)} candles · {symbol.pending_contracts} backlog
                    </div>
                  </div>
                  <div className="text-right text-xs text-text-muted shrink-0">
                    <div>{formatRelativeTime(symbol.last_activity_at)}</div>
                    <div>{symbol.discovered_expiries}/{symbol.total_expiries || 0} expiries discovered</div>
                  </div>
                </div>
                <ProgressBar pct={symbol.progress_pct} label={`Spot ${formatCompactNumber(symbol.spot_candles)} · Selection ${symbol.selection_spots_ready}/${symbol.total_expiries || 0}`} />
              </div>
            ))}
            {!activeQueue.length && (
              <div className="text-xs text-text-muted border border-dashed border-bg-border rounded p-3">
                No symbols are actively moving through the cache right now.
              </div>
            )}
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
              Research Ready ({researchReadySymbols.length})
            </div>
            {researchReadySymbols.length > 12 && (
              <button onClick={() => setShowAllAvailable(v => !v)} className="text-xs text-accent-blue hover:underline">
                {showAllAvailable ? "Show Less" : `Show All (${researchReadySymbols.length})`}
              </button>
            )}
          </div>
          <div className="space-y-2 max-h-[28rem] overflow-y-auto pr-1">
            {availableVisible.map(symbol => (
              <div key={`available-${symbol.symbol}`} className="bg-bg-secondary border border-bg-border rounded p-3 flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-text-primary">{symbol.symbol}</span>
                    <span className="text-[11px] text-text-muted">{symbol.kind}</span>
                    <span className={clsx("px-1.5 py-0.5 rounded border text-[10px] uppercase tracking-wide", stageTone(symbol.stage))}>
                      {symbol.stage}
                    </span>
                  </div>
                  <div className="text-xs text-text-muted">
                    {symbol.option_contracts} local contracts · {symbol.research_contracts_processed}/{symbol.research_contract_target || 0} required · {formatCompactNumber(symbol.option_candles)} candles
                  </div>
                </div>
                <div className="text-right text-xs text-text-muted shrink-0">
                  <div>{symbol.pending_contracts} pending</div>
                  <div>{formatRelativeTime(symbol.last_activity_at)}</div>
                </div>
              </div>
            ))}
            {!researchReadySymbols.length && (
              <div className="text-xs text-text-muted border border-dashed border-bg-border rounded p-3">
                No symbols are research-ready yet. Partial cache coverage is still being built in the left column.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ValidationReportPanel() {
  const {
    data,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
    isShowingSnapshot,
    snapshotSavedAt,
  } = usePersistentSnapshotQuery<ValidationReportPayload>({
    queryKey: ["latestValidationReport"],
    queryFn: () => getLatestValidationReport().then(r => r.data),
    staleTime: 5000,
    refetchInterval: 15000,
    refetchOnWindowFocus: false,
    storageKey: "analysis:validation-report",
  });

  if (isLoading && !data) {
    return (
      <div className="card p-4 flex items-center gap-2 text-xs text-text-muted">
        <Loader2 size={12} className="animate-spin" /> Loading validation report…
      </div>
    );
  }

  if (isError && !data) {
    return (
      <div className="card p-4 space-y-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-accent-red">
          <AlertCircle size={14} /> Validation Report Unavailable
        </div>
        <div className="text-xs text-text-muted">
          {(error as any)?.response?.data?.detail || (error as Error)?.message || "Could not load validation report"}
        </div>
      </div>
    );
  }

  if (!data?.available || !data.summary) {
    return (
      <div className="card p-4 space-y-2">
        {isShowingSnapshot && (
          <SnapshotBanner
            message={`Validation report refresh failed. ${getErrorDetail(error)}`}
            snapshotSavedAt={snapshotSavedAt}
          />
        )}
        <div className="flex items-center gap-2 text-sm font-semibold text-text-secondary">
          <FileText size={14} className="text-accent-blue" /> Live Validation Report
        </div>
        <div className="text-xs text-text-muted">
          {data?.detail || "Live validation is waiting for enough cached CE/PE history to produce a report."}
        </div>
      </div>
    );
  }

  const summary = data.summary;
  const strategyRows = summary.exit_analysis.strategy_ranking.slice(0, 5);
  const underlyingRows = summary.breakdowns.by_underlying.slice(0, 5);
  const linkHref = (path?: string) => (path ? `${API_URL}${path}` : "#");

  return (
    <div className="card p-5 space-y-4">
      {isShowingSnapshot && (
        <SnapshotBanner
          message={`Validation report refresh failed. ${getErrorDetail(error)}`}
          snapshotSavedAt={snapshotSavedAt}
        />
      )}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <FileText size={15} className="text-accent-amber" />
          <div>
            <div className="text-sm font-semibold text-text-secondary">Live Validation Report</div>
            <div className="text-xs text-text-muted">
              Cached NSE option data only · recomputed from Timescale as new candles land
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="text-[11px] text-text-muted mr-1">
            Source updated {formatRelativeTime(data.source_updated_at || summary.generated_at)}
          </div>
          <a
            href={linkHref(data.files?.report_markdown_url)}
            target="_blank"
            rel="noreferrer"
            className="px-2.5 py-1.5 rounded border border-bg-border text-xs text-text-secondary hover:border-accent-blue/40 hover:text-accent-blue flex items-center gap-1"
          >
            <FileText size={11} /> Markdown
          </a>
          <a
            href={linkHref(data.files?.summary_json_url)}
            target="_blank"
            rel="noreferrer"
            className="px-2.5 py-1.5 rounded border border-bg-border text-xs text-text-secondary hover:border-accent-blue/40 hover:text-accent-blue flex items-center gap-1"
          >
            <Download size={11} /> Summary JSON
          </a>
          <a
            href={linkHref(data.files?.trades_csv_url)}
            target="_blank"
            rel="noreferrer"
            className="px-2.5 py-1.5 rounded border border-bg-border text-xs text-text-secondary hover:border-accent-blue/40 hover:text-accent-blue flex items-center gap-1"
          >
            <Download size={11} /> Trades CSV
          </a>
          <button onClick={() => refetch()} className="text-text-muted hover:text-text-primary">
            {isFetching ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          </button>
        </div>
      </div>

      <div className="text-[11px] text-text-muted">
        Report recomputed {formatRelativeTime(summary.generated_at)} · last source write {formatLocalTimestamp(data.source_updated_at || summary.generated_at)}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Cache Coverage"
          value={summary.coverage.underlyings_with_option_data}
          sub={`${summary.coverage.complete_cached_contracts} contracts · ${formatCompactNumber(summary.coverage.cached_option_candles)} candles`}
          color="text-accent-blue"
        />
        <StatCard
          label="Opportunities"
          value={summary.opportunities.total_trades}
          sub={summary.opportunities.months.join(", ")}
          color="text-accent-green"
        />
        <StatCard
          label="Best Fixed Exit"
          value={summary.exit_analysis.best_strategy}
          sub={`${summary.exit_analysis.best_strategy_avg_return_pct.toFixed(2)}% avg`}
          color="text-accent-amber"
        />
        <StatCard
          label="Hold To Expiry"
          value={`${summary.exit_analysis.hold_to_expiry_avg_return_pct.toFixed(2)}%`}
          sub={`${summary.exit_analysis.avg_max_return_pct.toFixed(2)}% avg max move`}
          color={summary.exit_analysis.hold_to_expiry_avg_return_pct >= 0 ? "text-accent-green" : "text-accent-red"}
        />
      </div>

      <div className="text-[11px] text-text-muted">
        ATM monthly pairs analyzed: {summary.coverage.atm_monthly_pairs}
      </div>

      <div className="text-xs text-text-muted bg-bg-secondary border border-bg-border rounded p-3">
        Fixed-exit ranking below is the deployable comparison. The per-bucket underlying and IV/OI/PCR rows use the oracle best-exit result for each trade and are diagnostic only.
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <div className="space-y-2">
          <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
            Fixed Exit Ranking
          </div>
          <div className="space-y-2">
            {strategyRows.map((row) => (
              <div key={row.strategy} className="bg-bg-secondary border border-bg-border rounded p-3 flex items-center justify-between gap-3 text-xs">
                <div className="space-y-1">
                  <div className="font-semibold text-text-primary">{row.strategy}</div>
                  <div className="text-text-muted">{row.trades} trades · median {row.median_return_pct.toFixed(2)}%</div>
                </div>
                <div className="text-right space-y-1">
                  <ReturnBadge pct={row.avg_return_pct} />
                  <div className="text-text-muted">{row.positive_pct.toFixed(2)}% positive</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
            Oracle Breakdown By Underlying
          </div>
          <div className="space-y-2">
            {underlyingRows.map((row) => (
              <div key={String(row.underlying)} className="bg-bg-secondary border border-bg-border rounded p-3 flex items-center justify-between gap-3 text-xs">
                <div className="space-y-1">
                  <div className="font-semibold text-text-primary">{row.underlying}</div>
                  <div className="text-text-muted">
                    {row.trades} trades · top exit {row.top_exit_strategy}
                  </div>
                </div>
                <div className="text-right space-y-1">
                  <ReturnBadge pct={Number(row.avg_oracle_best_exit_return_pct ?? 0)} />
                  <div className="text-text-muted">{Number(row.oracle_positive_pct ?? 0).toFixed(2)}% positive</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function GreeksSyncReportPanel() {
  const {
    data,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
    isShowingSnapshot,
    snapshotSavedAt,
  } = usePersistentSnapshotQuery<GreeksSyncReportPayload>({
    queryKey: ["latestGreeksSyncReport"],
    queryFn: () => getLatestGreeksSyncReport().then(r => r.data),
    staleTime: 5000,
    refetchInterval: 15000,
    refetchOnWindowFocus: false,
    storageKey: "analysis:greeks-sync-report",
  });

  if (isLoading && !data) {
    return (
      <div className="card p-4 flex items-center gap-2 text-xs text-text-muted">
        <Loader2 size={12} className="animate-spin" /> Loading Greeks Sync research…
      </div>
    );
  }

  if (isError && !data) {
    return (
      <div className="card p-4 space-y-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-accent-red">
          <AlertCircle size={14} /> Greeks Sync Report Unavailable
        </div>
        <div className="text-xs text-text-muted">
          {(error as any)?.response?.data?.detail || (error as Error)?.message || "Could not load Greeks Sync report"}
        </div>
      </div>
    );
  }

  if (!data?.available || !data.summary) {
    return (
      <div className="card p-4 space-y-2">
        {isShowingSnapshot && (
          <SnapshotBanner
            message={`Greeks Sync research refresh failed. ${getErrorDetail(error)}`}
            snapshotSavedAt={snapshotSavedAt}
          />
        )}
        <div className="flex items-center gap-2 text-sm font-semibold text-text-secondary">
          <FileText size={14} className="text-accent-purple" /> Greeks Sync Research
        </div>
        <div className="text-xs text-text-muted">
          {data?.detail || "Greeks Sync research is waiting for complete cached CE/PE history."}
        </div>
      </div>
    );
  }

  const summary = data.summary;
  const trackRows = summary.comparison.track_ranking.slice(0, 4);
  const strategyRows = summary.exit_analysis.strategy_ranking.slice(0, 3);
  const linkHref = (path?: string) => (path ? `${API_URL}${path}` : "#");

  return (
    <div className="card p-5 space-y-4">
      {isShowingSnapshot && (
        <SnapshotBanner
          message={`Greeks Sync research refresh failed. ${getErrorDetail(error)}`}
          snapshotSavedAt={snapshotSavedAt}
        />
      )}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <FileText size={15} className="text-accent-purple" />
          <div>
            <div className="text-sm font-semibold text-text-secondary">Greeks Sync Research</div>
            <div className="text-xs text-text-muted">
              Located here in Analysis as a separate research track against the MACD baseline
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="text-[11px] text-text-muted mr-1">
            Source updated {formatRelativeTime(data.source_updated_at || summary.generated_at)}
          </div>
          <a
            href={linkHref(data.files?.report_markdown_url)}
            target="_blank"
            rel="noreferrer"
            className="px-2.5 py-1.5 rounded border border-bg-border text-xs text-text-secondary hover:border-accent-purple/40 hover:text-accent-purple flex items-center gap-1"
          >
            <FileText size={11} /> Markdown
          </a>
          <a
            href={linkHref(data.files?.summary_json_url)}
            target="_blank"
            rel="noreferrer"
            className="px-2.5 py-1.5 rounded border border-bg-border text-xs text-text-secondary hover:border-accent-purple/40 hover:text-accent-purple flex items-center gap-1"
          >
            <Download size={11} /> Summary JSON
          </a>
          <button onClick={() => refetch()} className="text-text-muted hover:text-text-primary">
            {isFetching ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Signals"
          value={summary.signals.total_signals}
          sub={`${summary.signals.strong_signals} strong`}
          color="text-accent-purple"
        />
        <StatCard
          label="Average Score"
          value={summary.signals.avg_score.toFixed(1)}
          sub={`Median ${summary.signals.median_score.toFixed(1)}`}
          color="text-accent-green"
        />
        <StatCard
          label="MACD Confirmed"
          value={`${summary.signals.macd_confirmed_pct.toFixed(1)}%`}
          sub={`${summary.coverage.atm_monthly_pairs} ATM monthly pairs`}
          color="text-accent-amber"
        />
        <StatCard
          label="Best Fixed Exit"
          value={summary.exit_analysis.best_strategy}
          sub={`${summary.exit_analysis.best_strategy_avg_return_pct.toFixed(2)}% avg`}
          color="text-accent-blue"
        />
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <div className="space-y-2">
          <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
            Track Comparison
          </div>
          <div className="space-y-2">
            {trackRows.map((row) => (
              <div key={row.track} className="bg-bg-secondary border border-bg-border rounded p-3 flex items-center justify-between gap-3 text-xs">
                <div className="space-y-1">
                  <div className="font-semibold text-text-primary">{row.track}</div>
                  <div className="text-text-muted">{row.trades} trades · hold {row.avg_hold_to_expiry_return_pct.toFixed(2)}%</div>
                </div>
                <div className="text-right space-y-1">
                  <ReturnBadge pct={row.avg_oracle_best_exit_return_pct} />
                  <div className="text-text-muted">{row.positive_pct.toFixed(2)}% positive</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
            Fixed Exit Ranking
          </div>
          <div className="space-y-2">
            {strategyRows.map((row) => (
              <div key={row.strategy} className="bg-bg-secondary border border-bg-border rounded p-3 flex items-center justify-between gap-3 text-xs">
                <div className="space-y-1">
                  <div className="font-semibold text-text-primary">{row.strategy}</div>
                  <div className="text-text-muted">{row.trades} trades · median {row.median_return_pct.toFixed(2)}%</div>
                </div>
                <div className="text-right space-y-1">
                  <ReturnBadge pct={row.avg_return_pct} />
                  <div className="text-text-muted">{row.positive_pct.toFixed(2)}% positive</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Results Components ─────────────────────────────────────────────────────────

function ExitStrategyPanel({ analysis }: { analysis: BacktestResults["exit_analysis"] }) {
  const strategies = analysis.strategy_comparison ?? {};
  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Target size={14} className="text-accent-amber" />
        <h3 className="text-sm font-semibold text-text-secondary">Exit Strategy Analysis</h3>
      </div>
      <div className="bg-accent-amber/5 border border-accent-amber/30 rounded p-2 text-xs flex items-center gap-2">
        <Zap size={11} className="text-accent-amber shrink-0" />
        <span><strong className="text-accent-amber">Best:</strong> {analysis.best_strategy}</span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="text-center bg-bg-secondary rounded p-2 border border-bg-border">
          <div className="text-text-muted mb-1">Hold to Expiry</div>
          <ReturnBadge pct={analysis.hold_to_expiry_avg} />
        </div>
        <div className="text-center bg-bg-secondary rounded p-2 border border-bg-border">
          <div className="text-text-muted mb-1">50% Target Hit Rate</div>
          <span className="font-mono font-bold text-accent-green">{(analysis.target_50_hit_rate * 100).toFixed(0)}%</span>
        </div>
        <div className="text-center bg-bg-secondary rounded p-2 border border-bg-border">
          <div className="text-text-muted mb-1">100% Target Hit Rate</div>
          <span className="font-mono font-bold text-accent-amber">{(analysis.target_100_hit_rate * 100).toFixed(0)}%</span>
        </div>
      </div>
      {Object.entries(strategies).length > 0 && (
        <div className="space-y-1">
          {Object.entries(strategies).map(([name, data]: [string, any]) => (
            <div key={name} className="flex items-center justify-between text-xs border border-bg-border rounded px-2 py-1.5">
              <span className="text-text-secondary">{name}</span>
              <div className="flex items-center gap-4">
                <span className="text-text-muted">Hit: <span className="text-text-primary font-mono">{(data.hit_rate * 100).toFixed(0)}%</span></span>
                <ReturnBadge pct={data.avg_return} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MonthlyHeatmap({ byMonth }: { byMonth: BacktestResults["by_month"] }) {
  const months = Object.entries(byMonth).sort(([a], [b]) => a.localeCompare(b));
  if (!months.length) return null;
  const maxCount = Math.max(...months.map(([, v]) => v.count), 1);
  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <BarChart2 size={14} className="text-accent-blue" />
        <h3 className="text-sm font-semibold text-text-secondary">Monthly Opportunities</h3>
      </div>
      <div className="space-y-1.5">
        {months.map(([month, data]) => (
          <div key={month} className="flex items-center gap-2 text-xs">
            <span className="text-text-muted font-mono w-16 shrink-0">{month}</span>
            <div className="flex-1 bg-bg-border rounded h-5 relative overflow-hidden">
              <div className="h-full bg-accent-blue/50 rounded transition-all"
                style={{ width: `${(data.count / maxCount) * 100}%` }} />
              <span className="absolute inset-0 flex items-center pl-2 text-xs font-mono text-text-primary">
                {data.count} signals
              </span>
            </div>
            <ReturnBadge pct={data.avg_max_return} />
            <span className="text-text-muted w-24 shrink-0 text-right">CE:{data.ce_count} PE:{data.pe_count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function UnderlyingTable({ byUnderlying }: { byUnderlying: BacktestResults["by_underlying"] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const rows = Object.entries(byUnderlying).sort(([, a], [, b]) => b.avg_max_return - a.avg_max_return);
  return (
    <div className="card p-4 space-y-2">
      <div className="flex items-center gap-2">
        <TrendingUp size={14} className="text-accent-green" />
        <h3 className="text-sm font-semibold text-text-secondary">By Underlying — sorted by avg max return</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-text-muted border-b border-bg-border">
              <th className="text-left py-1.5 pr-3">Underlying</th>
              <th className="text-right pr-3">Signals</th>
              <th className="text-right pr-3">Avg Max Return</th>
              <th className="text-right pr-3">Avg Held Return</th>
              <th className="text-right pr-3">50% Hit</th>
              <th className="text-right pr-3">100% Hit</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([u, d]) => {
              const isExp = expanded === u;
              return (
                <>
                  <tr key={u} onClick={() => setExpanded(isExp ? null : u)}
                    className="border-b border-bg-border/50 hover:bg-bg-hover cursor-pointer">
                    <td className="py-1.5 pr-3 font-semibold text-text-primary">{u}</td>
                    <td className="text-right pr-3 font-mono text-accent-blue">{d.opportunities}</td>
                    <td className="text-right pr-3"><ReturnBadge pct={d.avg_max_return} /></td>
                    <td className="text-right pr-3"><ReturnBadge pct={d.avg_held_return} /></td>
                    <td className="text-right pr-3 font-mono text-accent-green">{(d.target_50_rate * 100).toFixed(0)}%</td>
                    <td className="text-right pr-3 font-mono text-accent-amber">{(d.target_100_rate * 100).toFixed(0)}%</td>
                    <td className="text-right">{isExp ? <ChevronUp size={12} /> : <ChevronDown size={12} />}</td>
                  </tr>
                  {isExp && (
                    <tr key={`${u}-det`} className="bg-bg-secondary/40">
                      <td colSpan={7} className="px-3 py-2">
                        <div className="text-xs space-y-1">
                          <div className="text-text-muted font-semibold">Monthly breakdown</div>
                          <div className="flex flex-wrap gap-2">
                            {Object.entries(d.monthly_breakdown ?? {}).sort().map(([m, c]) => (
                              <span key={m} className="bg-bg-border rounded px-2 py-0.5 font-mono">
                                {m}: <span className="text-accent-blue">{c as number}</span>
                              </span>
                            ))}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TradeList({ trades }: { trades: Trade[] }) {
  const [filter, setFilter] = useState<"all" | "CE" | "PE">("all");
  const [sortBy, setSortBy] = useState<"max_return" | "held_return" | "date">("max_return");
  const [showCount, setShowCount] = useState(50);

  const filtered = trades
    .filter(t => filter === "all" || t.option_type === filter)
    .sort((a, b) =>
      sortBy === "max_return" ? b.max_return_pct - a.max_return_pct :
      sortBy === "held_return" ? b.held_return_pct - a.held_return_pct :
      new Date(b.entry_time).getTime() - new Date(a.entry_time).getTime()
    );

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-accent-green" />
          <h3 className="text-sm font-semibold text-text-secondary">All Trades ({trades.length})</h3>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {(["all", "CE", "PE"] as const).map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={clsx("px-2 py-0.5 rounded border",
                filter === f ? "bg-accent-blue/20 border-accent-blue/40 text-accent-blue" : "border-bg-border text-text-muted")}>
              {f}
            </button>
          ))}
          <select value={sortBy} onChange={e => setSortBy(e.target.value as any)} className="terminal-input text-xs py-0.5 px-2">
            <option value="max_return">Max Return</option>
            <option value="held_return">Held Return</option>
            <option value="date">Date</option>
          </select>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-text-muted border-b border-bg-border">
              <th className="text-left py-1.5 pr-2">Instrument</th>
              <th className="text-left pr-2">Expiry</th>
              <th className="text-left pr-2">Entry</th>
              <th className="text-right pr-2">Entry ₹</th>
              <th className="text-right pr-2">Max ₹</th>
              <th className="text-right pr-2">Max Ret.</th>
              <th className="text-right pr-2">Held Ret.</th>
              <th className="text-right pr-2">50%</th>
              <th className="text-right">100%</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, showCount).map((t, i) => (
              <tr key={i} className="border-b border-bg-border/30 hover:bg-bg-hover">
                <td className="py-1 pr-2 font-semibold">
                  <span className={t.option_type === "CE" ? "text-accent-blue" : "text-accent-red"}>
                    {t.underlying} {t.strike} {t.option_type}
                  </span>
                </td>
                <td className="pr-2 text-text-muted font-mono">{t.expiry}</td>
                <td className="pr-2 text-text-muted font-mono">{t.entry_time?.slice(0, 16).replace("T", " ") || "-"}</td>
                <td className="text-right pr-2 font-mono">₹{t.entry_price?.toFixed(2)}</td>
                <td className="text-right pr-2 font-mono">₹{t.max_price?.toFixed(2)}</td>
                <td className="text-right pr-2"><ReturnBadge pct={t.max_return_pct} /></td>
                <td className="text-right pr-2"><ReturnBadge pct={t.held_return_pct} /></td>
                <td className="text-right pr-2">{t.target_50pct_hit ? <CheckCircle2 size={10} className="text-accent-green inline" /> : <span className="text-text-muted">-</span>}</td>
                <td className="text-right">{t.target_100pct_hit ? <CheckCircle2 size={10} className="text-accent-amber inline" /> : <span className="text-text-muted">-</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {filtered.length > showCount && (
        <button onClick={() => setShowCount(n => n + 50)}
          className="text-xs text-accent-blue hover:underline w-full text-center py-1">
          Show more ({filtered.length - showCount} remaining)
        </button>
      )}
    </div>
  );
}

function ResultsDashboard({ results }: { results: BacktestResults }) {
  const trades = results.all_trades ?? [];
  const total = trades.length;
  const avgMax = total ? trades.reduce((s, t) => s + t.max_return_pct, 0) / total : 0;
  const t50 = trades.filter(t => t.target_50pct_hit).length;
  const t100 = trades.filter(t => t.target_100pct_hit).length;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total Signals" value={results.total_opportunities} color="text-accent-green" />
        <StatCard label="Avg Max Return" value={`${avgMax.toFixed(1)}%`} color={avgMax > 0 ? "text-accent-green" : "text-accent-red"} />
        <StatCard label="50% Target Hit" value={`${total ? ((t50 / total) * 100).toFixed(0) : 0}%`} sub={`${t50}/${total} trades`} color="text-accent-amber" />
        <StatCard label="100% Target Hit" value={`${total ? ((t100 / total) * 100).toFixed(0) : 0}%`} sub={`${t100}/${total} trades`} color="text-accent-amber" />
      </div>
      {results.exit_analysis && <ExitStrategyPanel analysis={results.exit_analysis} />}
      {results.by_month && Object.keys(results.by_month).length > 0 && <MonthlyHeatmap byMonth={results.by_month} />}
      {results.by_underlying && Object.keys(results.by_underlying).length > 0 && <UnderlyingTable byUnderlying={results.by_underlying} />}
      {trades.length > 0 && <TradeList trades={trades} />}
    </div>
  );
}

function PreviousTasks({ onResume }: { onResume: (id: string) => void }) {
  const { data: tasks } = useQuery({
    queryKey: ["macdTasks"],
    queryFn: () => listMacdBacktestTasks().then(r => r.data),
    staleTime: 10000,
  });
  if (!tasks?.length) return null;
  return (
    <div className="card p-4 space-y-2">
      <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Recent Runs</div>
      {tasks.slice(0, 5).map((t: any) => (
        <div key={t.task_id} className="flex items-center justify-between text-xs border border-bg-border rounded px-3 py-2 hover:bg-bg-hover">
          <div>
            <div className="text-text-secondary font-medium">
              {t.underlyings?.length ? t.underlyings.join(", ") : "All F&O instruments"}
            </div>
            <div className="text-text-muted">{t.from_date} → {t.to_date} · {t.elapsed_secs}s</div>
          </div>
          <div className="flex items-center gap-3">
            <span className={clsx("px-2 py-0.5 rounded text-xs",
              t.status === "done" ? "bg-accent-green/10 text-accent-green" :
              t.status === "error" ? "bg-accent-red/10 text-accent-red" :
              "bg-accent-blue/10 text-accent-blue")}>
              {t.status}
            </span>
            {(t.status === "done" || t.status === "running") && (
              <button onClick={() => onResume(t.task_id)} className="text-accent-blue hover:underline flex items-center gap-1">
                View <ArrowRight size={10} />
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────────

export default function AnalysisPage() {
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [results, setResults] = useState<BacktestResults | null>(null);

  const handleStarted = useCallback((taskId: string) => {
    setActiveTaskId(taskId);
    setResults(null);
  }, []);

  return (
    <div className="h-full overflow-y-auto p-6 space-y-5">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold text-text-primary flex items-center gap-2">
          <Activity size={18} className="text-accent-green" />
          MACD Options Backtest
        </h1>
        <p className="text-xs text-text-muted">
          Zero-line crossover on 30-min ATM CE/PE candles · NSE F&O monthly expiries · Uses connected Upstox session
        </p>
      </div>

      <BrokerStatusCard />
      <PopulationMonitor />
      <ValidationReportPanel />
      <GreeksSyncReportPanel />
      <PreviousTasks onResume={(id) => { setActiveTaskId(id); setResults(null); }} />
      <RunForm onStarted={handleStarted} />
      {activeTaskId && <TaskMonitor taskId={activeTaskId} onResults={setResults} />}
      {results && <ResultsDashboard results={results} />}
    </div>
  );
}
