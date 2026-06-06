"use client";

/**
 * F&O Data Ingest Console — native v2 surface (embedded as the "Data ingest"
 * tab of /research). Two job workflows backed by /api/fo-data/*:
 *
 *   1. General F&O download — pulls historical option OHLCV candles for NSE
 *      F&O underlyings into the backtester TimescaleDB (needs an Upstox token).
 *   2. Index minute-analytics dataset — builds a separate file-based 1-minute
 *      option + spot dataset for NIFTY / SENSEX research.
 *
 * Each workflow shows a submit form, a live progress monitor (polled at 2s
 * while running), a task-history table, and a stored-data stats panel.
 * Mutations go through api.post / api.delete then invalidate react-query.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  BarChart3,
  Calendar,
  CheckCircle2,
  Database,
  Download,
  Info,
  Loader2,
  Play,
  RefreshCw,
  Trash2,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { clsx } from "clsx";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatDuration,
  formatIST,
  formatNumber,
} from "@/components/desk-ui";
import { api } from "@/lib/api";

// ── Types ──────────────────────────────────────────────────────────────────

type JobStatus = "pending" | "running" | "done" | "error";

interface DownloadTask {
  task_id: string;
  status: JobStatus;
  total_instruments: number;
  processed: number;
  skipped: number;
  stored_candles: number;
  pct: number;
  current_symbol: string;
  error: string;
  elapsed_secs: number;
  started_at: string | null;
  finished_at: string | null;
}

interface StatsRow {
  underlying: string;
  option_type: string;
  candles: number;
  earliest: string | null;
  latest: string | null;
  expiries: number;
  strikes: number;
}

interface IndexAnalyticsTask {
  task_id: string;
  status: JobStatus;
  current_stage: string;
  underlyings: string[];
  interval: string;
  total_spot_series: number;
  processed_spot_series: number;
  total_expiries: number;
  processed_expiries: number;
  total_contracts: number;
  processed_contracts: number;
  total_request_units: number;
  processed_request_units: number;
  skipped_contracts: number;
  stored_files: number;
  stored_spot_files: number;
  stored_candles: number;
  stored_spot_candles: number;
  incomplete_contracts?: number;
  pct: number;
  current_underlying: string;
  current_expiry: string;
  current_symbol: string;
  data_root: string;
  latest_file: string;
  error: string;
  elapsed_secs: number;
  started_at: string | null;
  finished_at: string | null;
}

interface IndexAnalyticsStatsRow {
  underlying: string;
  expiry_kind: string;
  dataset_type?: string;
  contracts: number;
  expiries: number;
  files: number;
  candles: number;
  earliest: string | null;
  latest: string | null;
}

interface IndexAnalyticsStatsResponse {
  rows: IndexAnalyticsStatsRow[];
  summary?: { contracts: number; files: number; candles: number };
  data_root?: string;
}

interface InstrumentsResponse {
  underlying: string;
  total_instruments: number;
  sample: unknown[];
}

// ── API surface ─────────────────────────────────────────────────────────────

const foApi = {
  startDownload: (body: object) => api.post("/api/fo-data/start", body).then((r) => r.data),
  getStatus: (id: string) => api.get(`/api/fo-data/status/${id}`).then((r) => r.data as DownloadTask),
  getTasks: () => api.get("/api/fo-data/tasks").then((r) => r.data as DownloadTask[]),
  getStats: () => api.get("/api/fo-data/stats").then((r) => r.data as { rows: StatsRow[] }),
  deleteTask: (id: string) => api.delete(`/api/fo-data/tasks/${id}`).then((r) => r.data),
  getInstruments: (underlying: string) =>
    api.get("/api/fo-data/instruments", { params: { underlying } }).then((r) => r.data as InstrumentsResponse),

  startIndex: (body: object) =>
    api.post("/api/fo-data/index-analytics/start", body).then((r) => r.data),
  getIndexStatus: (id: string) =>
    api.get(`/api/fo-data/index-analytics/status/${id}`).then((r) => r.data as IndexAnalyticsTask),
  getIndexTasks: () =>
    api.get("/api/fo-data/index-analytics/tasks").then((r) => r.data as IndexAnalyticsTask[]),
  getIndexStats: () =>
    api.get("/api/fo-data/index-analytics/stats").then((r) => r.data as IndexAnalyticsStatsResponse),
  deleteIndexTask: (id: string) =>
    api.delete(`/api/fo-data/index-analytics/tasks/${id}`).then((r) => r.data),
};

// ── Static form options ─────────────────────────────────────────────────────

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"];
const INTERVALS: { value: string; label: string }[] = [
  { value: "30minute", label: "30-minute" },
  { value: "1minute", label: "1-minute (very large)" },
  { value: "day", label: "Daily" },
];
const INDEX_DATASET_UNDERLYINGS = ["NIFTY", "SENSEX"];

// ── Small helpers ───────────────────────────────────────────────────────────

const num = (n?: number | null) =>
  n == null || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-IN");

const statusVariant = (s: JobStatus) =>
  s === "done" ? "success" : s === "error" ? "error" : s === "running" ? "info" : "neutral";

function yearAgo(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 1);
  return d.toISOString().slice(0, 10);
}
function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function ProgressBar({ pct, tone = "blue" }: { pct: number; tone?: "blue" | "green" }) {
  const p = Math.max(0, Math.min(pct ?? 0, 100));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-bg-primary/40">
      <div
        className={clsx(
          "h-full rounded-full transition-all duration-500",
          tone === "green" ? "bg-accent-green" : "bg-accent-blue",
        )}
        style={{ width: `${p}%` }}
      />
    </div>
  );
}

function StatusIcon({ status }: { status: JobStatus }) {
  if (status === "running") return <Loader2 size={14} className="animate-spin text-accent-blue" />;
  if (status === "done") return <CheckCircle2 size={14} className="text-accent-green" />;
  if (status === "error") return <XCircle size={14} className="text-accent-red" />;
  return <Loader2 size={14} className="text-text-muted" />;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      {children}
    </label>
  );
}

const inputCls =
  "w-full rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-sm text-text-primary outline-none focus:border-accent-blue/60";

function Chip({
  active,
  onClick,
  children,
  tone = "blue",
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  tone?: "blue" | "green" | "red";
}) {
  const activeCls =
    tone === "green"
      ? "border-accent-green/50 bg-accent-green/15 text-accent-green"
      : tone === "red"
        ? "border-accent-red/50 bg-accent-red/15 text-accent-red"
        : "border-accent-blue/50 bg-accent-blue/15 text-accent-blue";
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-lg border px-3 py-1 text-xs font-semibold transition-colors",
        active ? activeCls : "border-bg-border bg-bg-primary/20 text-text-muted hover:border-bg-active hover:text-text-secondary",
      )}
    >
      {children}
    </button>
  );
}

function ErrorLine({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-accent-red/25 bg-accent-red/5 px-2.5 py-2 text-xs text-accent-red">
      <AlertCircle size={12} className="shrink-0" /> {text}
    </div>
  );
}

// ── Instrument preview (general download) ───────────────────────────────────

function InstrumentPreview({ underlying }: { underlying: string }) {
  const q = useQuery({
    queryKey: ["foInstruments", underlying],
    queryFn: () => foApi.getInstruments(underlying),
    enabled: !!underlying,
    staleTime: 5 * 60_000,
    retry: false,
  });
  if (q.isLoading) return null;
  const total = q.data?.total_instruments ?? 0;
  return (
    <div className="mt-1 text-[11px] text-text-muted">
      <span className="font-semibold text-accent-blue">{num(total)}</span> contracts for {underlying}
      {total > 0 ? ` · ~${Math.round((total * 3250) / 1_000_000)}M candles @ 30m` : " (catalog empty — check Upstox connection)"}
    </div>
  );
}

// ── General F&O download form ───────────────────────────────────────────────

function DownloadForm({ onStarted }: { onStarted: (id: string) => void }) {
  const [uls, setUls] = useState<string[]>(["NIFTY", "BANKNIFTY"]);
  const [fromDate, setFromDate] = useState(yearAgo);
  const [toDate, setToDate] = useState(today);
  const [interval, setInterval] = useState("30minute");
  const [optionTypes, setOptionTypes] = useState<string[]>(["CE", "PE"]);
  const [minStrike, setMinStrike] = useState("");
  const [maxStrike, setMaxStrike] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const toggle = (set: React.Dispatch<React.SetStateAction<string[]>>, v: string) =>
    set((prev) => (prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v]));

  const submit = async () => {
    if (!token) return setError("Upstox Bearer token is required — connect Upstox in Settings, then paste it here.");
    if (!uls.length) return setError("Select at least one underlying.");
    if (!optionTypes.length) return setError("Select at least one option type.");
    setBusy(true);
    setError("");
    try {
      const body: Record<string, unknown> = {
        underlyings: uls,
        from_date: fromDate,
        to_date: toDate,
        interval,
        option_types: optionTypes,
        upstox_token: token,
      };
      if (minStrike) body.min_strike = parseFloat(minStrike);
      if (maxStrike) body.max_strike = parseFloat(maxStrike);
      const res = await foApi.startDownload(body);
      onStarted(res.task_id);
    } catch (e) {
      setError(extractErr(e, "Failed to start download."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="Download F&O historical candles"
      icon={<Download size={15} className="text-accent-blue" />}
      description="Fetch expired & active NSE option OHLCV into the backtester TimescaleDB."
    >
      <div className="space-y-3.5">
        <div className="rounded-xl border border-accent-blue/20 bg-accent-blue/5 px-3 py-2.5 text-[11.5px] text-text-secondary">
          <div className="mb-0.5 flex items-center gap-1.5 font-semibold text-accent-blue">
            <Info size={12} /> Requires an active Upstox connection
          </div>
          Paste your Upstox Bearer token (Settings → Upstox → copy <code className="text-text-primary">access_token</code>). Stored candles feed the backtester directly.
        </div>

        <Field label="Upstox Bearer token *">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Paste access_token"
            className={inputCls}
          />
        </Field>

        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-text-muted">Underlyings</div>
          <div className="flex flex-wrap gap-1.5">
            {UNDERLYINGS.map((u) => (
              <Chip key={u} active={uls.includes(u)} onClick={() => toggle(setUls, u)}>
                {u}
              </Chip>
            ))}
          </div>
          {uls.map((u) => (
            <InstrumentPreview key={u} underlying={u} />
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="From date">
            <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} className={inputCls} />
          </Field>
          <Field label="To date">
            <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} className={inputCls} />
          </Field>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-text-muted">Candle interval</div>
          <div className="flex flex-wrap gap-1.5">
            {INTERVALS.map(({ value, label }) => (
              <Chip key={value} active={interval === value} onClick={() => setInterval(value)} tone="green">
                {label}
              </Chip>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-text-muted">Option types</div>
          <div className="flex gap-1.5">
            {["CE", "PE"].map((ot) => (
              <Chip
                key={ot}
                active={optionTypes.includes(ot)}
                onClick={() => toggle(setOptionTypes, ot)}
                tone={ot === "CE" ? "green" : "red"}
              >
                {ot}
              </Chip>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-text-muted">
            Strike range <span className="normal-case tracking-normal text-text-muted/70">(optional — blank = all strikes)</span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <input
              type="number"
              value={minStrike}
              onChange={(e) => setMinStrike(e.target.value)}
              placeholder="Min e.g. 19000"
              className={inputCls}
            />
            <input
              type="number"
              value={maxStrike}
              onChange={(e) => setMaxStrike(e.target.value)}
              placeholder="Max e.g. 27000"
              className={inputCls}
            />
          </div>
        </div>

        {error ? <ErrorLine text={error} /> : null}

        <button
          type="button"
          onClick={submit}
          disabled={busy || !token || !uls.length}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-accent-blue/40 bg-accent-blue/15 py-2.5 text-sm font-semibold text-accent-blue transition-colors hover:bg-accent-blue/25 disabled:opacity-50"
        >
          {busy ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          {busy ? "Starting download…" : "Start download"}
        </button>
      </div>
    </Section>
  );
}

// ── Index analytics dataset form ────────────────────────────────────────────

function IndexAnalyticsForm({ onStarted }: { onStarted: (id: string) => void }) {
  const [uls, setUls] = useState<string[]>([...INDEX_DATASET_UNDERLYINGS]);
  const [fromDate, setFromDate] = useState(yearAgo);
  const [toDate, setToDate] = useState(today);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const toggle = (v: string) =>
    setUls((prev) => (prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v]));

  const submit = async () => {
    if (!uls.length) return setError("Select at least one underlying.");
    setBusy(true);
    setError("");
    try {
      const res = await foApi.startIndex({
        underlyings: uls,
        from_date: fromDate,
        to_date: toDate,
        interval: "1minute",
      });
      onStarted(res.task_id);
    } catch (e) {
      setError(extractErr(e, "Failed to start index analytics dataset."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="Index minute-analytics dataset"
      icon={<BarChart3 size={15} className="text-accent-green" />}
      description="File-based 1-minute option + spot dataset for NIFTY / SENSEX research."
    >
      <div className="space-y-3.5">
        <div className="rounded-xl border border-accent-green/20 bg-accent-green/5 px-3 py-2.5 text-[11.5px] text-text-secondary">
          <div className="mb-0.5 flex items-center gap-1.5 font-semibold text-accent-green">
            <Info size={12} /> What this collects
          </div>
          Weekly + monthly option contracts (1-min OHLC / volume / OI) plus 1-min index spot history, written as a separate analytics folder. Uses the saved Upstox connection from Settings.
        </div>

        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-[0.12em] text-text-muted">Underlyings</div>
          <div className="flex flex-wrap gap-1.5">
            {INDEX_DATASET_UNDERLYINGS.map((u) => (
              <Chip key={u} active={uls.includes(u)} onClick={() => toggle(u)} tone="green">
                {u}
              </Chip>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="From date">
            <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} className={inputCls} />
          </Field>
          <Field label="To date">
            <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} className={inputCls} />
          </Field>
        </div>

        <div className="rounded-lg border border-bg-border bg-bg-primary/20 px-2.5 py-2 text-[11px] text-text-muted">
          Interval fixed to <span className="font-semibold text-text-primary">1 minute</span>. Writes to a dedicated analytics dataset, not the backtester DB.
        </div>

        {error ? <ErrorLine text={error} /> : null}

        <button
          type="button"
          onClick={submit}
          disabled={busy || !uls.length}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-accent-green/40 bg-accent-green/15 py-2.5 text-sm font-semibold text-accent-green transition-colors hover:bg-accent-green/25 disabled:opacity-50"
        >
          {busy ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          {busy ? "Starting dataset build…" : "Start index dataset"}
        </button>
      </div>
    </Section>
  );
}

// ── Live monitors ───────────────────────────────────────────────────────────

function DownloadMonitor({ taskId, onSettled }: { taskId: string; onSettled: () => void }) {
  const q = useQuery({
    queryKey: ["foStatus", taskId],
    queryFn: () => foApi.getStatus(taskId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "done" || s === "error" ? false : 2000;
    },
    refetchOnWindowFocus: false,
  });
  const t = q.data;
  useEffect(() => {
    if (t && (t.status === "done" || t.status === "error")) onSettled();
  }, [t?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!t) return <div className="text-xs text-text-muted">Starting…</div>;

  return (
    <Section padded className="border-accent-blue/30">
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <StatusIcon status={t.status} />
            <span className="text-sm font-semibold text-text-primary">F&amp;O download in progress</span>
            <StatusBadge label={t.status} variant={statusVariant(t.status)} />
          </div>
          <span className="text-xs text-text-muted">{formatDuration(t.elapsed_secs)} elapsed</span>
        </div>
        <ProgressBar pct={t.pct} tone="blue" />
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <MetricTile size="sm" label="Progress" value={`${formatNumber(t.pct, 1)}%`} color="text-accent-blue" />
          <MetricTile size="sm" label="Instruments" value={`${num(t.processed)} / ${num(t.total_instruments)}`} />
          <MetricTile size="sm" label="Candles" value={num(t.stored_candles)} color="text-accent-green" />
          <MetricTile size="sm" label="Skipped" value={num(t.skipped)} />
        </div>
        {t.current_symbol ? (
          <p className="truncate font-mono text-[11px] text-text-muted">
            Current: <span className="text-accent-blue">{t.current_symbol}</span>
          </p>
        ) : null}
        {t.error ? <ErrorLine text={t.error} /> : null}
      </div>
    </Section>
  );
}

function IndexMonitor({ taskId, onSettled }: { taskId: string; onSettled: () => void }) {
  const q = useQuery({
    queryKey: ["indexStatus", taskId],
    queryFn: () => foApi.getIndexStatus(taskId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "done" || s === "error" ? false : 2000;
    },
    refetchOnWindowFocus: false,
  });
  const t = q.data;
  useEffect(() => {
    if (t && (t.status === "done" || t.status === "error")) onSettled();
  }, [t?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!t) return <div className="text-xs text-text-muted">Starting dataset build…</div>;

  return (
    <Section padded className="border-accent-green/30">
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <StatusIcon status={t.status} />
            <span className="text-sm font-semibold text-text-primary">Index analytics dataset build</span>
            <StatusBadge label={t.status} variant={statusVariant(t.status)} />
          </div>
          <span className="text-xs text-text-muted">{formatDuration(t.elapsed_secs)} elapsed</span>
        </div>
        <ProgressBar pct={t.pct} tone="green" />
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile size="sm" label="Progress" value={`${formatNumber(t.pct, 1)}%`} color="text-accent-green" />
          <MetricTile size="sm" label="Spot series" value={`${num(t.processed_spot_series)} / ${num(t.total_spot_series)}`} />
          <MetricTile size="sm" label="Expiries" value={`${num(t.processed_expiries)} / ${num(t.total_expiries)}`} />
          <MetricTile size="sm" label="Contracts" value={`${num(t.processed_contracts)} / ${num(t.total_contracts)}`} />
          <MetricTile size="sm" label="API windows" value={`${num(t.processed_request_units)} / ${num(t.total_request_units)}`} />
          <MetricTile size="sm" label="Candles" value={num(t.stored_candles)} color="text-accent-green" />
        </div>
        <div className="flex flex-wrap gap-1.5">
          {t.underlyings.map((u) => (
            <StatusBadge key={u} label={u} variant="success" />
          ))}
        </div>
        <div className="grid gap-2 text-[11px] text-text-muted md:grid-cols-2">
          <div className="rounded-lg border border-bg-border bg-bg-primary/15 p-2.5">
            <div className="font-semibold text-text-secondary">Current stage</div>
            <div className="mt-0.5 text-text-primary">{t.current_stage.replaceAll("_", " ") || "—"}</div>
            <div className="mt-0.5">
              {t.current_underlying || "—"} {t.current_expiry ? `· ${t.current_expiry}` : ""}
            </div>
            {t.current_symbol ? <div className="mt-0.5 truncate font-mono">{t.current_symbol}</div> : null}
          </div>
          <div className="rounded-lg border border-bg-border bg-bg-primary/15 p-2.5">
            <div className="font-semibold text-text-secondary">Dataset folder</div>
            <div className="mt-0.5 break-all">{t.data_root || "—"}</div>
            <div className="mt-1">
              Files {num(t.stored_files)} · Spot files {num(t.stored_spot_files)} · Reused {num(t.skipped_contracts)}
            </div>
          </div>
        </div>
        {t.latest_file ? (
          <div className="rounded-lg border border-bg-border bg-bg-primary/15 p-2.5 text-[11px] text-text-muted">
            Latest file: <span className="break-all text-text-primary">{t.latest_file}</span>
          </div>
        ) : null}
        {t.error ? <ErrorLine text={t.error} /> : null}
      </div>
    </Section>
  );
}

// ── Stored stats panels ─────────────────────────────────────────────────────

function BacktesterStats({ rows }: { rows: StatsRow[] }) {
  const byUnderlying = useMemo(() => {
    const m = new Map<string, StatsRow[]>();
    for (const r of rows) {
      const arr = m.get(r.underlying) ?? [];
      arr.push(r);
      m.set(r.underlying, arr);
    }
    return Array.from(m.entries()).sort(
      (a, b) =>
        b[1].reduce((s, r) => s + Number(r.candles), 0) - a[1].reduce((s, r) => s + Number(r.candles), 0),
    );
  }, [rows]);

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
        <Database size={26} className="mx-auto mb-2 opacity-30" />
        No backtester data stored yet. Start a download to populate historical F&amp;O candles.
      </div>
    );
  }

  return (
    <div className="max-h-[560px] space-y-2 overflow-y-auto pr-1">
      {byUnderlying.map(([ul, group]) => {
        const totalCandles = group.reduce((s, r) => s + Number(r.candles), 0);
        const earliest = group.map((r) => r.earliest).filter(Boolean).sort()[0] ?? null;
        const latest = group.map((r) => r.latest).filter(Boolean).sort().reverse()[0] ?? null;
        return (
          <div key={ul} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <TrendingUp size={13} className="text-accent-blue" />
                <span className="text-sm font-semibold text-text-primary">{ul}</span>
              </div>
              <span className="text-[11px] text-text-muted">{num(totalCandles)} candles</span>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              {group
                .sort((a, b) => a.option_type.localeCompare(b.option_type))
                .map((r) => {
                  const ce = r.option_type === "CE";
                  return (
                    <div
                      key={r.option_type}
                      className={clsx(
                        "rounded-lg border p-2 text-center",
                        ce ? "border-accent-green/20 bg-accent-green/5" : "border-accent-red/20 bg-accent-red/5",
                      )}
                    >
                      <div className={clsx("text-xs font-bold", ce ? "text-accent-green" : "text-accent-red")}>
                        {r.option_type}
                      </div>
                      <div className="font-mono text-sm text-text-primary">{num(r.candles)}</div>
                      <div className="text-[10px] text-text-muted">
                        {num(r.expiries)} exp · {num(r.strikes)} strikes
                      </div>
                    </div>
                  );
                })}
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[10.5px] text-text-muted">
              <Calendar size={10} />
              {formatIST(earliest)} → {formatIST(latest)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function IndexStats({ data }: { data?: IndexAnalyticsStatsResponse }) {
  const rows = data?.rows ?? [];
  const byUnderlying = useMemo(() => {
    const m = new Map<string, IndexAnalyticsStatsRow[]>();
    for (const r of rows) {
      const arr = m.get(r.underlying) ?? [];
      arr.push(r);
      m.set(r.underlying, arr);
    }
    return Array.from(m.entries());
  }, [rows]);

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
        <Database size={26} className="mx-auto mb-2 opacity-30" />
        No index analytics dataset stored yet. Start the collector to populate option minute files + spot history.
      </div>
    );
  }

  const order: Record<string, number> = { spot: 0, weekly: 1, monthly: 2 };

  return (
    <div className="space-y-2">
      {data?.data_root ? (
        <div className="rounded-lg border border-bg-border bg-bg-primary/15 p-2.5 text-[11px] text-text-muted">
          Dataset root: <span className="break-all text-text-primary">{data.data_root}</span>
        </div>
      ) : null}
      {byUnderlying.map(([ul, group]) => {
        const totalCandles = group.reduce((s, r) => s + Number(r.candles), 0);
        const ordered = [...group].sort((a, b) => (order[a.expiry_kind] ?? 9) - (order[b.expiry_kind] ?? 9));
        return (
          <div key={ul} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <TrendingUp size={13} className="text-accent-green" />
                <span className="text-sm font-semibold text-text-primary">{ul}</span>
              </div>
              <span className="text-[11px] text-text-muted">{num(totalCandles)} minute candles</span>
            </div>
            <div className="mt-2 grid gap-2 md:grid-cols-3">
              {ordered.map((r) => {
                const isSpot = r.expiry_kind === "spot";
                return (
                  <div key={r.expiry_kind} className="rounded-lg border border-bg-border bg-bg-primary/15 p-2.5">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-bold uppercase tracking-[0.12em] text-text-secondary">
                        {isSpot ? "spot" : r.expiry_kind}
                      </span>
                      <span className="text-[10px] text-text-muted">
                        {isSpot ? `${num(r.files)} file` : `${num(r.expiries)} exp`}
                      </span>
                    </div>
                    <div className="mt-0.5 font-mono text-sm text-text-primary">
                      {num(r.contracts)} {isSpot ? "series" : "contracts"}
                    </div>
                    <div className="text-[10px] text-text-muted">
                      {num(r.files)} files · {num(r.candles)} candles
                    </div>
                    <div className="mt-0.5 text-[10px] text-text-muted">
                      {formatIST(r.earliest)} → {formatIST(r.latest)}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Task history tables ─────────────────────────────────────────────────────

function DownloadHistory({ tasks, onDelete }: { tasks: DownloadTask[]; onDelete: (id: string) => void }) {
  if (!tasks.length) {
    return <div className="px-1 py-6 text-center text-xs text-text-muted">No F&amp;O download jobs yet.</div>;
  }
  return (
    <div className="space-y-2">
      {tasks.map((t) => (
        <div key={t.task_id} className="flex items-center gap-3 rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <StatusBadge label={t.status} variant={statusVariant(t.status)} />
          <div className="min-w-0 flex-1">
            <ProgressBar pct={t.pct} tone="blue" />
            <div className="mt-1 flex items-center justify-between gap-3">
              <span className="truncate text-[11px] text-text-muted">
                {num(t.stored_candles)} candles · {num(t.processed)}/{num(t.total_instruments)} instruments
                {t.skipped ? ` · ${num(t.skipped)} skipped` : ""}
              </span>
              <span className="shrink-0 text-[11px] text-text-muted">{formatIST(t.started_at)}</span>
            </div>
            {t.error ? <p className="mt-1 truncate text-[11px] text-accent-red">{t.error}</p> : null}
          </div>
          <button
            type="button"
            onClick={() => onDelete(t.task_id)}
            className="shrink-0 rounded-md p-1 text-text-muted hover:text-accent-red"
            title="Remove"
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}

function IndexHistory({ tasks, onDelete }: { tasks: IndexAnalyticsTask[]; onDelete: (id: string) => void }) {
  if (!tasks.length) {
    return <div className="px-1 py-6 text-center text-xs text-text-muted">No index dataset jobs yet.</div>;
  }
  return (
    <div className="space-y-2">
      {tasks.map((t) => (
        <div key={t.task_id} className="flex items-center gap-3 rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <StatusBadge label={t.status} variant={statusVariant(t.status)} />
          <div className="min-w-0 flex-1">
            <ProgressBar pct={t.pct} tone="green" />
            <div className="mt-1 flex items-center justify-between gap-3">
              <span className="truncate text-[11px] text-text-muted">
                {t.underlyings.join(", ")} · {num(t.stored_candles)} candles · contracts {num(t.processed_contracts)}/{num(t.total_contracts)} · windows {num(t.processed_request_units)}/{num(t.total_request_units)}
                {t.skipped_contracts ? ` · reused ${num(t.skipped_contracts)}` : ""} · {t.current_stage.replaceAll("_", " ")}
              </span>
              <span className="shrink-0 text-[11px] text-text-muted">{formatIST(t.started_at)}</span>
            </div>
            {t.error ? <p className="mt-1 truncate text-[11px] text-accent-red">{t.error}</p> : null}
          </div>
          <button
            type="button"
            onClick={() => onDelete(t.task_id)}
            className="shrink-0 rounded-md p-1 text-text-muted hover:text-accent-red"
            title="Remove"
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}

// ── Error extraction ────────────────────────────────────────────────────────

function extractErr(e: unknown, fallback: string): string {
  if (typeof e === "object" && e !== null) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string };
    return err.response?.data?.detail || err.message || fallback;
  }
  return fallback;
}

// ── Main console ────────────────────────────────────────────────────────────

export default function DataIngestConsole() {
  const qc = useQueryClient();
  const [activeDownloadId, setActiveDownloadId] = useState<string | null>(null);
  const [activeIndexId, setActiveIndexId] = useState<string | null>(null);

  const statsQ = useQuery({
    queryKey: ["foStats"],
    queryFn: foApi.getStats,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const tasksQ = useQuery({
    queryKey: ["foTasks"],
    queryFn: foApi.getTasks,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const indexStatsQ = useQuery({
    queryKey: ["indexStats"],
    queryFn: foApi.getIndexStats,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const indexTasksQ = useQuery({
    queryKey: ["indexTasks"],
    queryFn: foApi.getIndexTasks,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const downloadTasks = useMemo(
    () => (Array.isArray(tasksQ.data) ? tasksQ.data : []),
    [tasksQ.data],
  );
  const indexTasks = useMemo(
    () => (Array.isArray(indexTasksQ.data) ? indexTasksQ.data : []),
    [indexTasksQ.data],
  );

  // Auto-attach the live monitor to any already-running job (e.g. after reload).
  useEffect(() => {
    if (activeDownloadId) return;
    const running = downloadTasks.find((t) => t.status === "running");
    if (running) setActiveDownloadId(running.task_id);
  }, [downloadTasks, activeDownloadId]);
  useEffect(() => {
    if (activeIndexId) return;
    const running = indexTasks.find((t) => t.status === "running");
    if (running) setActiveIndexId(running.task_id);
  }, [indexTasks, activeIndexId]);

  const statsRows = statsQ.data?.rows ?? [];
  const indexSummary = indexStatsQ.data?.summary;

  // KPI strip totals.
  const totalBacktesterCandles = useMemo(
    () => statsRows.reduce((s, r) => s + Number(r.candles ?? 0), 0),
    [statsRows],
  );
  const underlyingCount = useMemo(
    () => new Set(statsRows.map((r) => r.underlying)).size,
    [statsRows],
  );
  const runningJobs = useMemo(
    () => [...downloadTasks, ...indexTasks].filter((t) => t.status === "running").length,
    [downloadTasks, indexTasks],
  );

  const refreshAll = () => {
    statsQ.refetch();
    tasksQ.refetch();
    indexStatsQ.refetch();
    indexTasksQ.refetch();
  };

  const onDownloadSettled = () => {
    setActiveDownloadId(null);
    qc.invalidateQueries({ queryKey: ["foStats"] });
    qc.invalidateQueries({ queryKey: ["foTasks"] });
  };
  const onIndexSettled = () => {
    setActiveIndexId(null);
    qc.invalidateQueries({ queryKey: ["indexStats"] });
    qc.invalidateQueries({ queryKey: ["indexTasks"] });
  };

  const deleteDownloadTask = async (id: string) => {
    if (activeDownloadId === id) setActiveDownloadId(null);
    try {
      await foApi.deleteTask(id);
    } catch {
      /* refetch surfaces staleness */
    }
    qc.invalidateQueries({ queryKey: ["foTasks"] });
  };
  const deleteIndexTask = async (id: string) => {
    if (activeIndexId === id) setActiveIndexId(null);
    try {
      await foApi.deleteIndexTask(id);
    } catch {
      /* refetch surfaces staleness */
    }
    qc.invalidateQueries({ queryKey: ["indexTasks"] });
  };

  const feedError = statsQ.isError || indexStatsQ.isError || tasksQ.isError || indexTasksQ.isError;

  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Database size={18} className="text-accent-blue" />
            <div>
              <h1 className="text-xl font-semibold text-text-primary">F&amp;O data ingest</h1>
              <p className="mt-0.5 text-sm text-text-muted">
                Download historical option candles and build index minute-analytics datasets.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={refreshAll}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
          >
            <RefreshCw
              size={13}
              className={tasksQ.isFetching || statsQ.isFetching || indexTasksQ.isFetching ? "animate-spin" : ""}
            />
            Refresh
          </button>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile
          label="Backtester candles"
          value={num(totalBacktesterCandles)}
          detail={`${underlyingCount} underlyings`}
        />
        <MetricTile
          label="Index dataset"
          value={indexSummary ? num(indexSummary.candles) : "—"}
          detail={indexSummary ? `${num(indexSummary.contracts)} contracts · ${num(indexSummary.files)} files` : "no dataset"}
          color={indexSummary ? "text-accent-green" : undefined}
        />
        <MetricTile
          label="Running jobs"
          value={String(runningJobs)}
          detail={`${downloadTasks.length + indexTasks.length} total in history`}
          color={runningJobs ? "text-accent-blue" : undefined}
        />
        <MetricTile
          label="Feed"
          value={feedError ? "offline" : "live"}
          detail={statsQ.dataUpdatedAt ? formatIST(statsQ.dataUpdatedAt) : ""}
          color={feedError ? "text-accent-red" : "text-accent-green"}
        />
      </section>

      {activeDownloadId ? (
        <DownloadMonitor taskId={activeDownloadId} onSettled={onDownloadSettled} />
      ) : null}
      {activeIndexId ? <IndexMonitor taskId={activeIndexId} onSettled={onIndexSettled} /> : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <DownloadForm onStarted={setActiveDownloadId} />
        <Section
          title="Stored backtester data"
          icon={<Database size={15} className="text-accent-blue" />}
          description="Per-underlying CE/PE candle coverage in TimescaleDB."
        >
          <BacktesterStats rows={statsRows} />
        </Section>
      </div>

      <Section title="F&O download history" icon={<Download size={15} className="text-accent-blue" />}>
        <DownloadHistory tasks={downloadTasks} onDelete={deleteDownloadTask} />
      </Section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <IndexAnalyticsForm onStarted={setActiveIndexId} />
        <Section
          title="Index dataset snapshot"
          icon={<BarChart3 size={15} className="text-accent-green" />}
          description="File-based NIFTY / SENSEX minute option + spot coverage."
        >
          <IndexStats data={indexStatsQ.data} />
        </Section>
      </div>

      <Section title="Index dataset history" icon={<BarChart3 size={15} className="text-accent-green" />}>
        <IndexHistory tasks={indexTasks} onDelete={deleteIndexTask} />
      </Section>
    </div>
  );
}
