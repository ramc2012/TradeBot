"use client";
import { useState, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { clsx } from "clsx";
import {
  Database, Download, RefreshCw, CheckCircle2, XCircle, Loader2,
  AlertCircle, BarChart3, Calendar, TrendingUp, Play, Trash2, Info,
} from "lucide-react";

// ── API helpers ───────────────────────────────────────────────────────────────

const foApi = {
  startDownload: (body: object) => api.post("/api/fo-data/start", body).then(r => r.data),
  getStatus: (taskId: string) => api.get(`/api/fo-data/status/${taskId}`).then(r => r.data),
  getTasks: () => api.get("/api/fo-data/tasks").then(r => r.data),
  getStats: () => api.get("/api/fo-data/stats").then(r => r.data),
  startIndexAnalytics: (body: object) => api.post("/api/fo-data/index-analytics/start", body).then(r => r.data),
  getIndexAnalyticsStatus: (taskId: string) => api.get(`/api/fo-data/index-analytics/status/${taskId}`).then(r => r.data),
  getIndexAnalyticsTasks: () => api.get("/api/fo-data/index-analytics/tasks").then(r => r.data),
  getIndexAnalyticsStats: () => api.get("/api/fo-data/index-analytics/stats").then(r => r.data),
  getInstruments: (underlying: string) =>
    api.get("/api/fo-data/instruments", { params: { underlying } }).then(r => r.data),
  deleteTask: (taskId: string) => api.delete(`/api/fo-data/tasks/${taskId}`).then(r => r.data),
  deleteIndexAnalyticsTask: (taskId: string) => api.delete(`/api/fo-data/index-analytics/tasks/${taskId}`).then(r => r.data),
};

// ── Types ─────────────────────────────────────────────────────────────────────

interface DownloadTask {
  task_id: string;
  status: "pending" | "running" | "done" | "error";
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
  earliest: string;
  latest: string;
  expiries: number;
  strikes: number;
}

interface IndexAnalyticsTask {
  task_id: string;
  status: "pending" | "running" | "done" | "error";
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

// ── Helpers ───────────────────────────────────────────────────────────────────

function StatusChip({ status }: { status: DownloadTask["status"] }) {
  const map = {
    pending: { cls: "bg-text-muted/15 text-text-muted", label: "PENDING" },
    running: { cls: "bg-accent-blue/15 text-accent-blue", label: "RUNNING" },
    done:    { cls: "bg-accent-green/15 text-accent-green", label: "DONE" },
    error:   { cls: "bg-accent-red/15 text-accent-red", label: "ERROR" },
  };
  const s = map[status] ?? map.pending;
  return (
    <span className={clsx("text-xs font-bold px-2 py-0.5 rounded", s.cls)}>
      {s.label}
    </span>
  );
}

function ProgressBar({ pct }: { pct: number }) {
  return (
    <div className="w-full bg-bg-secondary rounded-full h-1.5 overflow-hidden">
      <div
        className="h-full bg-accent-blue rounded-full transition-all duration-500"
        style={{ width: `${Math.min(pct, 100)}%` }}
      />
    </div>
  );
}

function fmtNum(n: number) {
  return n?.toLocaleString("en-IN") ?? "—";
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  return new Date(s).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" });
}

// ── Active task monitor ───────────────────────────────────────────────────────

function TaskMonitor({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const [task, setTask] = useState<DownloadTask | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    const poll = async () => {
      try {
        const data = await foApi.getStatus(taskId);
        setTask(data);
        if (data.status === "done" || data.status === "error") {
          clearInterval(intervalRef.current);
          onDone();
        }
      } catch {}
    };
    poll();
    intervalRef.current = setInterval(poll, 2000);
    return () => clearInterval(intervalRef.current);
  }, [taskId, onDone]);

  if (!task) return <div className="text-xs text-text-muted">Starting…</div>;

  return (
    <div className="card p-4 space-y-3 border-accent-blue/30">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {task.status === "running" && <Loader2 size={14} className="animate-spin text-accent-blue" />}
          {task.status === "done" && <CheckCircle2 size={14} className="text-accent-green" />}
          {task.status === "error" && <XCircle size={14} className="text-accent-red" />}
          <span className="text-sm font-semibold">Download in progress</span>
          <StatusChip status={task.status} />
        </div>
        <span className="text-xs text-text-muted">{task.elapsed_secs}s elapsed</span>
      </div>
      <ProgressBar pct={task.pct} />
      <div className="grid grid-cols-4 gap-3 text-center">
        <div>
          <div className="text-lg font-mono font-bold text-accent-blue">{task.pct}%</div>
          <div className="text-xs text-text-muted">Progress</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-text-primary">{fmtNum(task.processed)}</div>
          <div className="text-xs text-text-muted">Instruments</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-accent-green">{fmtNum(task.stored_candles)}</div>
          <div className="text-xs text-text-muted">Candles stored</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-text-muted">{fmtNum(task.skipped)}</div>
          <div className="text-xs text-text-muted">Skipped</div>
        </div>
      </div>
      {task.current_symbol && (
        <p className="text-xs text-text-muted font-mono truncate">
          Current: <span className="text-accent-blue">{task.current_symbol}</span>
          {" "}({task.processed}/{task.total_instruments})
        </p>
      )}
      {task.error && (
        <p className="text-xs text-accent-red bg-accent-red/5 rounded p-2">{task.error}</p>
      )}
    </div>
  );
}

function IndexAnalyticsTaskMonitor({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const [task, setTask] = useState<IndexAnalyticsTask | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    const poll = async () => {
      try {
        const data = await foApi.getIndexAnalyticsStatus(taskId);
        setTask(data);
        if (data.status === "done" || data.status === "error") {
          clearInterval(intervalRef.current);
          onDone();
        }
      } catch {}
    };
    poll();
    intervalRef.current = setInterval(poll, 2000);
    return () => clearInterval(intervalRef.current);
  }, [taskId, onDone]);

  if (!task) return <div className="text-xs text-text-muted">Starting dataset build…</div>;

  return (
    <div className="card p-4 space-y-3 border-accent-green/30">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {task.status === "running" && <Loader2 size={14} className="animate-spin text-accent-blue" />}
          {task.status === "done" && <CheckCircle2 size={14} className="text-accent-green" />}
          {task.status === "error" && <XCircle size={14} className="text-accent-red" />}
          <span className="text-sm font-semibold">Index analytics dataset</span>
          <StatusChip status={task.status} />
        </div>
        <span className="text-xs text-text-muted">{task.elapsed_secs}s elapsed</span>
      </div>
      <ProgressBar pct={task.pct} />
      <div className="grid grid-cols-2 gap-3 text-center md:grid-cols-6">
        <div>
          <div className="text-lg font-mono font-bold text-accent-blue">{task.pct}%</div>
          <div className="text-xs text-text-muted">Progress</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-accent-green">
            {fmtNum(task.processed_spot_series)}/{fmtNum(task.total_spot_series)}
          </div>
          <div className="text-xs text-text-muted">Spot series</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-text-primary">
            {fmtNum(task.processed_expiries)}/{fmtNum(task.total_expiries)}
          </div>
          <div className="text-xs text-text-muted">Expiries</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-text-primary">
            {fmtNum(task.processed_contracts)}/{fmtNum(task.total_contracts)}
          </div>
          <div className="text-xs text-text-muted">Contracts</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-text-primary">
            {fmtNum(task.processed_request_units)}/{fmtNum(task.total_request_units)}
          </div>
          <div className="text-xs text-text-muted">API windows</div>
        </div>
        <div>
          <div className="text-lg font-mono font-bold text-accent-green">{fmtNum(task.stored_candles)}</div>
          <div className="text-xs text-text-muted">Candles written</div>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {task.underlyings.map((underlying) => (
          <span
            key={underlying}
            className="rounded border border-accent-green/25 bg-accent-green/10 px-2 py-1 text-[11px] font-semibold text-accent-green"
          >
            {underlying}
          </span>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-3 text-xs text-text-muted md:grid-cols-2">
        <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
          <div className="font-semibold text-text-secondary">Current stage</div>
          <div className="mt-1 text-text-primary">{task.current_stage.replaceAll("_", " ")}</div>
          <div className="mt-1">{task.current_underlying || "--"} {task.current_expiry ? `• ${task.current_expiry}` : ""}</div>
          {task.current_symbol && <div className="mt-1 font-mono truncate">{task.current_symbol}</div>}
        </div>
        <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
          <div className="font-semibold text-text-secondary">Dataset folder</div>
          <div className="mt-1 break-all">{task.data_root || "--"}</div>
          <div className="mt-2">
            Files {fmtNum(task.stored_files)} · Spot files {fmtNum(task.stored_spot_files)} · Reused {fmtNum(task.skipped_contracts)}
          </div>
        </div>
      </div>
      {task.latest_file && (
        <div className="rounded border border-bg-border bg-bg-secondary/20 p-3 text-xs text-text-muted">
          Latest file: <span className="text-text-primary break-all">{task.latest_file}</span>
        </div>
      )}
      {task.error && (
        <p className="text-xs text-accent-red bg-accent-red/5 rounded p-2">{task.error}</p>
      )}
    </div>
  );
}

// ── Stored stats table ────────────────────────────────────────────────────────

function StatsTable({ rows }: { rows: StatsRow[] }) {
  if (!rows.length) {
    return (
      <div className="text-center py-8 text-text-muted text-sm">
        <Database size={32} className="mx-auto mb-2 opacity-30" />
        No data stored yet. Start a download to populate historical F&O data.
      </div>
    );
  }

  // Group by underlying
  const byUnderlying: Record<string, StatsRow[]> = {};
  for (const r of rows) {
    if (!byUnderlying[r.underlying]) byUnderlying[r.underlying] = [];
    byUnderlying[r.underlying].push(r);
  }

  return (
    <div className="space-y-3">
      {Object.entries(byUnderlying).map(([ul, rows]) => {
        const totalCandles = rows.reduce((s, r) => s + Number(r.candles), 0);
        const earliest = rows.map(r => r.earliest).filter(Boolean).sort()[0];
        const latest = rows.map(r => r.latest).filter(Boolean).sort().reverse()[0];
        return (
          <div key={ul} className="card p-3 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <TrendingUp size={14} className="text-accent-blue" />
                <span className="font-semibold text-sm">{ul}</span>
              </div>
              <span className="text-xs text-text-muted">{fmtNum(totalCandles)} total candles</span>
            </div>
            <div className="grid grid-cols-4 gap-2 text-center text-xs">
              {rows.map(r => (
                <div key={r.option_type} className={clsx(
                  "rounded p-2 space-y-0.5",
                  r.option_type === "CE" ? "bg-accent-green/5 border border-accent-green/20" : "bg-accent-red/5 border border-accent-red/20"
                )}>
                  <div className={clsx("font-bold", r.option_type === "CE" ? "text-accent-green" : "text-accent-red")}>
                    {r.option_type}
                  </div>
                  <div className="text-text-primary font-mono">{fmtNum(Number(r.candles))}</div>
                  <div className="text-text-muted">{r.expiries} expiries</div>
                  <div className="text-text-muted">{r.strikes} strikes</div>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-4 text-xs text-text-muted">
              <span><Calendar size={10} className="inline mr-1" />{fmtDate(earliest)} → {fmtDate(latest)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function IndexAnalyticsStatsTable({
  rows,
  dataRoot,
}: {
  rows: IndexAnalyticsStatsRow[];
  dataRoot?: string;
}) {
  if (!rows.length) {
    return (
      <div className="text-center py-8 text-text-muted text-sm">
        <Database size={32} className="mx-auto mb-2 opacity-30" />
        No index analytics dataset stored yet. Start the collector to populate NIFTY and SENSEX option minute files plus spot history.
      </div>
    );
  }

  const byUnderlying: Record<string, IndexAnalyticsStatsRow[]> = {};
  for (const row of rows) {
    if (!byUnderlying[row.underlying]) byUnderlying[row.underlying] = [];
    byUnderlying[row.underlying].push(row);
  }

  return (
    <div className="space-y-3">
      {dataRoot && (
        <div className="rounded border border-bg-border bg-bg-secondary/30 p-3 text-xs text-text-muted">
          Dataset root: <span className="text-text-primary break-all">{dataRoot}</span>
        </div>
      )}
      {Object.entries(byUnderlying).map(([underlying, group]) => {
        const totalCandles = group.reduce((sum, row) => sum + Number(row.candles), 0);
        const orderedGroup = [...group].sort((a, b) => {
          const order = { spot: 0, weekly: 1, monthly: 2 } as Record<string, number>;
          return (order[a.expiry_kind] ?? 9) - (order[b.expiry_kind] ?? 9);
        });
        return (
          <div key={underlying} className="card p-3 space-y-2">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <TrendingUp size={14} className="text-accent-green" />
                <span className="font-semibold text-sm">{underlying}</span>
              </div>
              <span className="text-xs text-text-muted">{fmtNum(totalCandles)} minute candles</span>
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              {orderedGroup.map((row) => (
                <div key={`${row.underlying}-${row.expiry_kind}`} className="rounded border border-bg-border bg-bg-secondary/30 p-3 space-y-1">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-xs font-bold uppercase tracking-wide text-text-secondary">
                      {row.expiry_kind === "spot" ? "spot history" : row.expiry_kind}
                    </span>
                    <span className="text-xs text-text-muted">
                      {row.expiry_kind === "spot" ? `${fmtNum(row.files)} file` : `${fmtNum(row.expiries)} expiries`}
                    </span>
                  </div>
                  <div className="text-sm font-mono text-text-primary">
                    {row.expiry_kind === "spot" ? `${fmtNum(row.contracts)} series` : `${fmtNum(row.contracts)} contracts`}
                  </div>
                  <div className="text-xs text-text-muted">
                    {fmtNum(row.files)} files · {fmtNum(row.candles)} candles
                  </div>
                  <div className="text-xs text-text-muted">
                    {fmtDate(row.earliest)} → {fmtDate(row.latest)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Instrument preview ────────────────────────────────────────────────────────

function InstrumentPreview({ underlying }: { underlying: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["foInstruments", underlying],
    queryFn: () => foApi.getInstruments(underlying),
    enabled: !!underlying,
    staleTime: 5 * 60_000,
  });

  if (isLoading) return <div className="text-xs text-text-muted">Loading instrument count…</div>;
  if (!data) return null;
  return (
    <div className="text-xs text-text-muted bg-bg-secondary rounded p-2">
      <span className="text-accent-blue font-semibold">{fmtNum(data.total_instruments)}</span> total option contracts found for {underlying}.
      Estimate: ~{Math.round(data.total_instruments * 3250 / 1_000_000)}M candles at 30-min interval.
    </div>
  );
}

// ── Download form ─────────────────────────────────────────────────────────────

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"];
const INTERVALS = [
  { value: "30minute", label: "30-minute (recommended)" },
  { value: "1minute", label: "1-minute (very large)" },
  { value: "day", label: "Daily" },
];
const INDEX_DATASET_UNDERLYINGS = ["NIFTY", "SENSEX"];

function IndexAnalyticsForm({ onStarted }: { onStarted: (taskId: string) => void }) {
  const [selectedULs, setSelectedULs] = useState<string[]>([...INDEX_DATASET_UNDERLYINGS]);
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().slice(0, 10);
  });
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const toggleUL = (ul: string) =>
    setSelectedULs(prev => prev.includes(ul) ? prev.filter(x => x !== ul) : [...prev, ul]);

  const handleStart = async () => {
    if (!selectedULs.length) {
      setError("Select at least one underlying.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await foApi.startIndexAnalytics({
        underlyings: selectedULs,
        from_date: fromDate,
        to_date: toDate,
        interval: "1minute",
      });
      onStarted(res.task_id);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed to start index analytics dataset");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-center gap-2">
        <Database size={16} className="text-accent-green" />
        <h3 className="font-semibold text-sm">Index Minute Analytics Dataset</h3>
      </div>

      <div className="bg-bg-secondary rounded p-3 text-xs text-text-muted space-y-1">
        <p className="flex items-center gap-1 text-accent-green font-semibold">
          <Info size={11} /> What this collects
        </p>
        <p>Builds a separate file-based dataset for NIFTY and SENSEX option contracts using Upstox 1-minute OHLC, volume, and OI candles.</p>
        <p>Includes weekly and monthly expiries across the selected 1-year window, and also writes 1-minute NIFTY and SENSEX spot history into the same analytics folder for future research use.</p>
      </div>

      <div>
        <label className="text-xs text-text-muted block mb-2">Underlyings</label>
        <div className="flex flex-wrap gap-2">
          {INDEX_DATASET_UNDERLYINGS.map(ul => (
            <button key={ul} type="button" onClick={() => toggleUL(ul)}
              className={clsx(
                "px-3 py-1 rounded text-xs border font-semibold transition-colors",
                selectedULs.includes(ul)
                  ? "bg-accent-green/20 border-accent-green/50 text-accent-green"
                  : "bg-bg-hover border-bg-border text-text-muted hover:border-text-muted"
              )}>
              {ul}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-text-muted block mb-1">From Date</label>
          <input type="date" value={fromDate} onChange={e => setFromDate(e.target.value)}
            className="terminal-input w-full text-sm" />
        </div>
        <div>
          <label className="text-xs text-text-muted block mb-1">To Date</label>
          <input type="date" value={toDate} onChange={e => setToDate(e.target.value)}
            className="terminal-input w-full text-sm" />
        </div>
      </div>

      <div className="rounded border border-bg-border bg-bg-secondary/20 p-3 text-xs text-text-muted">
        Interval is fixed to <span className="text-text-primary font-semibold">1 minute</span>. The collector uses the saved Upstox connection from Settings and writes a separate analytics dataset instead of the backtester database.
      </div>

      {error && (
        <div className="bg-accent-red/5 border border-accent-red/20 rounded p-2 text-xs text-accent-red flex items-center gap-2">
          <AlertCircle size={12} /> {error}
        </div>
      )}

      <button type="button" onClick={handleStart} disabled={loading || !selectedULs.length}
        className="w-full py-3 rounded text-sm bg-accent-green/20 border border-accent-green/30 text-accent-green hover:bg-accent-green/30 disabled:opacity-50 flex items-center justify-center gap-2 font-semibold">
        {loading ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
        {loading ? "Starting dataset build…" : "Start Index Dataset"}
      </button>
    </div>
  );
}

function DownloadForm({ onStarted }: { onStarted: (taskId: string) => void }) {
  const [selectedULs, setSelectedULs] = useState<string[]>(["NIFTY", "BANKNIFTY"]);
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().slice(0, 10);
  });
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [interval, setInterval] = useState("30minute");
  const [optionTypes, setOptionTypes] = useState<string[]>(["CE", "PE"]);
  const [minStrike, setMinStrike] = useState("");
  const [maxStrike, setMaxStrike] = useState("");
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const toggleUL = (ul: string) =>
    setSelectedULs(prev => prev.includes(ul) ? prev.filter(x => x !== ul) : [...prev, ul]);
  const toggleOT = (ot: string) =>
    setOptionTypes(prev => prev.includes(ot) ? prev.filter(x => x !== ot) : [...prev, ot]);

  const handleStart = async () => {
    if (!token) { setError("Upstox access token is required. Connect Upstox first and paste your token."); return; }
    if (!selectedULs.length) { setError("Select at least one underlying"); return; }
    setLoading(true); setError("");
    try {
      const body: any = {
        underlyings: selectedULs,
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
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed to start download");
    } finally { setLoading(false); }
  };

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-center gap-2">
        <Download size={16} className="text-accent-blue" />
        <h3 className="font-semibold text-sm">Download F&O Historical Data</h3>
      </div>

      {/* Info box */}
      <div className="bg-bg-secondary rounded p-3 text-xs text-text-muted space-y-1">
        <p className="flex items-center gap-1 text-accent-blue font-semibold">
          <Info size={11} /> How it works
        </p>
        <p>Fetches historical OHLCV candles for expired and active NSE F&O option contracts from Upstox. Data is stored in the local TimescaleDB and used by the backtester.</p>
        <p className="text-accent-amber">Requires an active Upstox connection. Paste your Upstox Bearer token from Settings → Upstox.</p>
      </div>

      {/* Upstox Token */}
      <div>
        <label className="text-xs text-text-muted block mb-1">
          Upstox Bearer Token <span className="text-accent-red">*</span>
        </label>
        <input
          type="password"
          value={token}
          onChange={e => setToken(e.target.value)}
          placeholder="Paste your Upstox access_token here"
          className="terminal-input w-full text-sm"
        />
        <p className="text-xs text-text-muted mt-1">
          Get from: Settings → Upstox → connect → copy token from API response
        </p>
      </div>

      {/* Underlyings */}
      <div>
        <label className="text-xs text-text-muted block mb-2">Underlyings</label>
        <div className="flex flex-wrap gap-2">
          {UNDERLYINGS.map(ul => (
            <button key={ul} onClick={() => toggleUL(ul)}
              className={clsx(
                "px-3 py-1 rounded text-xs border font-semibold transition-colors",
                selectedULs.includes(ul)
                  ? "bg-accent-blue/20 border-accent-blue/50 text-accent-blue"
                  : "bg-bg-hover border-bg-border text-text-muted hover:border-text-muted"
              )}>
              {ul}
            </button>
          ))}
        </div>
        {selectedULs.length > 0 && selectedULs.map(ul => (
          <InstrumentPreview key={ul} underlying={ul} />
        ))}
      </div>

      {/* Date range */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-text-muted block mb-1">From Date</label>
          <input type="date" value={fromDate} onChange={e => setFromDate(e.target.value)}
            className="terminal-input w-full text-sm" />
        </div>
        <div>
          <label className="text-xs text-text-muted block mb-1">To Date</label>
          <input type="date" value={toDate} onChange={e => setToDate(e.target.value)}
            className="terminal-input w-full text-sm" />
        </div>
      </div>

      {/* Interval */}
      <div>
        <label className="text-xs text-text-muted block mb-2">Candle Interval</label>
        <div className="flex gap-2 flex-wrap">
          {INTERVALS.map(({ value, label }) => (
            <button key={value} onClick={() => setInterval(value)}
              className={clsx(
                "px-3 py-1.5 rounded text-xs border transition-colors",
                interval === value
                  ? "bg-accent-green/15 border-accent-green/40 text-accent-green"
                  : "bg-bg-hover border-bg-border text-text-muted hover:border-text-muted"
              )}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Option types */}
      <div>
        <label className="text-xs text-text-muted block mb-2">Option Types</label>
        <div className="flex gap-2">
          {["CE", "PE"].map(ot => (
            <button key={ot} onClick={() => toggleOT(ot)}
              className={clsx(
                "px-4 py-1.5 rounded text-xs border font-bold transition-colors",
                optionTypes.includes(ot)
                  ? ot === "CE"
                    ? "bg-accent-green/15 border-accent-green/40 text-accent-green"
                    : "bg-accent-red/15 border-accent-red/40 text-accent-red"
                  : "bg-bg-hover border-bg-border text-text-muted"
              )}>
              {ot}
            </button>
          ))}
        </div>
      </div>

      {/* Strike filter (optional) */}
      <div>
        <label className="text-xs text-text-muted block mb-2">Strike Range (optional — leave blank for all strikes)</label>
        <div className="grid grid-cols-2 gap-3">
          <input value={minStrike} onChange={e => setMinStrike(e.target.value)}
            placeholder="Min strike e.g. 19000" type="number"
            className="terminal-input w-full text-sm" />
          <input value={maxStrike} onChange={e => setMaxStrike(e.target.value)}
            placeholder="Max strike e.g. 27000" type="number"
            className="terminal-input w-full text-sm" />
        </div>
        <p className="text-xs text-text-muted mt-1">
          For NIFTY at ~22000: try 19000–27000 to get ±20 strikes from typical ATM range.
        </p>
      </div>

      {error && (
        <div className="bg-accent-red/5 border border-accent-red/20 rounded p-2 text-xs text-accent-red flex items-center gap-2">
          <AlertCircle size={12} /> {error}
        </div>
      )}

      <button onClick={handleStart} disabled={loading || !token || !selectedULs.length}
        className="w-full py-3 rounded text-sm bg-accent-blue/20 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/30 disabled:opacity-50 flex items-center justify-center gap-2 font-semibold">
        {loading ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
        {loading ? "Starting download…" : "Start Download"}
      </button>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DataPage() {
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeIndexTaskId, setActiveIndexTaskId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ["foStats", refreshKey],
    queryFn: () => foApi.getStats(),
    staleTime: 30_000,
  });

  const { data: tasks, refetch: refetchTasks } = useQuery({
    queryKey: ["foTasks"],
    queryFn: () => foApi.getTasks(),
    staleTime: 10_000,
  });

  const { data: indexStats, refetch: refetchIndexStats } = useQuery({
    queryKey: ["indexAnalyticsStats", refreshKey],
    queryFn: () => foApi.getIndexAnalyticsStats(),
    staleTime: 30_000,
  });

  const { data: indexTasks, refetch: refetchIndexTasks } = useQuery({
    queryKey: ["indexAnalyticsTasks"],
    queryFn: () => foApi.getIndexAnalyticsTasks(),
    staleTime: 10_000,
  });

  useEffect(() => {
    if (activeIndexTaskId) return;
    const runningTask = (indexTasks || []).find((task: IndexAnalyticsTask) => task.status === "running");
    if (runningTask) {
      setActiveIndexTaskId(runningTask.task_id);
    }
  }, [indexTasks, activeIndexTaskId]);

  const handleStarted = (taskId: string) => {
    setActiveTaskId(taskId);
  };

  const handleIndexStarted = (taskId: string) => {
    setActiveIndexTaskId(taskId);
  };

  const handleDone = () => {
    setActiveTaskId(null);
    setRefreshKey(k => k + 1);
    refetchStats();
    refetchTasks();
  };

  const handleIndexDone = () => {
    setActiveIndexTaskId(null);
    setRefreshKey(k => k + 1);
    refetchIndexStats();
    refetchIndexTasks();
  };

  return (
    <div className="max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database size={18} className="text-accent-blue" />
          <h1 className="text-lg font-bold font-mono text-text-primary">Data Collection</h1>
        </div>
        <button onClick={() => { refetchStats(); refetchTasks(); refetchIndexStats(); refetchIndexTasks(); }}
          className="text-text-muted hover:text-text-primary p-1 rounded" title="Refresh">
          <RefreshCw size={14} />
        </button>
      </div>

      {activeIndexTaskId && (
        <IndexAnalyticsTaskMonitor taskId={activeIndexTaskId} onDone={handleIndexDone} />
      )}

      {activeTaskId && (
        <TaskMonitor taskId={activeTaskId} onDone={handleDone} />
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <BarChart3 size={16} className="text-accent-green" />
            <h3 className="font-semibold text-sm">Index Analytics Folder</h3>
          </div>
          <IndexAnalyticsForm onStarted={handleIndexStarted} />
        </div>

        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <Database size={16} className="text-accent-green" />
            <h3 className="font-semibold text-sm">Index Dataset Snapshot</h3>
          </div>
          <IndexAnalyticsStatsTable
            rows={indexStats?.rows ?? []}
            dataRoot={indexStats?.data_root}
          />
        </div>
      </div>

      {indexTasks && indexTasks.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">Index Dataset Tasks</h3>
          <div className="space-y-2">
            {indexTasks.map((task: IndexAnalyticsTask) => (
              <div key={task.task_id} className="card p-3 flex items-center gap-3">
                <StatusChip status={task.status} />
                <div className="flex-1 min-w-0">
                  <ProgressBar pct={task.pct} />
                  <div className="flex items-center justify-between mt-1 gap-3">
                    <span className="text-xs text-text-muted truncate">
                      {task.underlyings.join(", ")} · reused {fmtNum(task.skipped_contracts)} · windows {fmtNum(task.processed_request_units)}/{fmtNum(task.total_request_units)} · contracts {fmtNum(task.processed_contracts)}/{fmtNum(task.total_contracts)} · {fmtNum(task.stored_candles)} candles · {task.current_stage.replaceAll("_", " ")}{task.current_underlying ? ` · ${task.current_underlying}` : ""}{task.current_expiry ? ` ${task.current_expiry}` : ""}
                    </span>
                    <span className="text-xs text-text-muted">{fmtDate(task.started_at)}</span>
                  </div>
                  {task.error && <p className="text-xs text-accent-red mt-1 truncate">{task.error}</p>}
                </div>
                <button onClick={() => foApi.deleteIndexAnalyticsTask(task.task_id).then(refetchIndexTasks)}
                  className="text-text-muted hover:text-accent-red p-1" title="Remove">
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <DownloadForm onStarted={handleStarted} />

        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <BarChart3 size={16} className="text-accent-blue" />
            <h3 className="font-semibold text-sm">Stored Backtester Data</h3>
          </div>
          <StatsTable rows={stats?.rows ?? []} />
        </div>
      </div>

      {tasks && tasks.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">F&amp;O Download History</h3>
          <div className="space-y-2">
            {tasks.map((t: DownloadTask) => (
              <div key={t.task_id} className="card p-3 flex items-center gap-3">
                <StatusChip status={t.status} />
                <div className="flex-1 min-w-0">
                  <ProgressBar pct={t.pct} />
                  <div className="flex items-center justify-between mt-1">
                    <span className="text-xs text-text-muted">
                      {fmtNum(t.stored_candles)} candles · {fmtNum(t.processed)}/{fmtNum(t.total_instruments)} instruments
                    </span>
                    <span className="text-xs text-text-muted">{fmtDate(t.started_at)}</span>
                  </div>
                  {t.error && <p className="text-xs text-accent-red mt-1 truncate">{t.error}</p>}
                </div>
                <button onClick={() => foApi.deleteTask(t.task_id).then(refetchTasks)}
                  className="text-text-muted hover:text-accent-red p-1" title="Remove">
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
