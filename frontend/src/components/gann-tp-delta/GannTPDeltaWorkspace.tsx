"use client";

import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { clsx } from "clsx";
import { AlertTriangle, Bot, Compass, FilePlus2, Play, RefreshCw, Target } from "lucide-react";

import {
  describeApiError,
  getGannTPDeltaBacktest,
  getGannTPDeltaLiveSnapshot,
  getGannTPDeltaPaperAgentStatus,
  getGannTPDeltaPaperJournal,
  getGannTPDeltaSummary,
  runGannTPDeltaPaperAgentOnce,
  runGannTPDeltaPaperProposal,
} from "@/lib/api";

type BarRow = { index: number; time: string; open: number; high: number; low: number; close: number };
type Angle = { name: string; direction: string; current_price: number; projected_price: number; slope: number; distance_pct: number };
type Sq9Level = { degree: number; direction: string; price: number; level_type: string; distance_pct: number };
type Cycle = { cycle: number; start_bar_index: number; center_bar_index: number; end_bar_index: number; active: boolean };
type Signal = { score: number; threshold: number; bias: string; state: string; reasons: string[]; trigger?: number; stop?: number; targets: number[] };
type Workspace = {
  module: { underlyings: string[]; timeframes: string[] };
  selection: { underlying: string; timeframe: string; lookback_sessions: number; anchor_mode: string; h_mode: string };
  snapshot: {
    status: string;
    reason?: string;
    as_of?: string;
    spot_price?: number;
    bars?: BarRow[];
    anchor?: { kind: string; time: string; price: number; bar_index: number } | null;
    h?: { mode: string; value: number; sample_count: number; source: string };
    gann_angles?: Angle[];
    sq9_levels?: Sq9Level[];
    time_cycles?: Cycle[];
    nearest_angle?: Angle | null;
    nearest_sq9_level?: Sq9Level | null;
    active_time_cycle?: Cycle | null;
    price_time_square?: { active: boolean; ratio: number; scaled_price_move: number; time_bars: number };
    signal?: Signal | null;
    alerts?: Array<{ key: string; severity: string; message: string }>;
  };
  backtest: { summary: { event_count: number; total_points: number; win_rate_pct: number; avg_win: number; avg_loss: number }; events: Array<{ time: string; bias: string; score: number; pnl_points: number }> };
};
type AgentPosition = {
  position_id: string;
  underlying: string;
  direction: string;
  option_type: string;
  trading_symbol?: string;
  expiry?: string;
  strike?: number;
  qty_units?: number;
  entry_price?: number;
  current_price?: number;
  stop_price?: number;
  target_price?: number;
  signal_score?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  close_reason?: string;
};
type AgentDecision = {
  underlying: string;
  signal_state?: string;
  signal_score?: number;
  decision?: string;
  reason?: string;
  option_type?: string;
};
type AgentStatus = {
  mode: string;
  last_scan_at?: string;
  last_message?: string;
  last_run?: Record<string, string | number | boolean>;
  summary: {
    open_positions: number;
    closed_positions?: number;
    realized_pnl?: number;
    unrealized_pnl?: number;
    total_pnl?: number;
  };
  open_positions: AgentPosition[];
  closed_positions?: AgentPosition[];
  recent_signals?: AgentDecision[];
};

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"];
const TIMEFRAMES = ["5minute", "15minute", "1hour", "1day"];
const ANCHORS = ["auto_pivot", "session", "manual"];
const H_MODES = ["median_tpd", "average_tpd", "atr", "manual"];

const numberFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

export default function GannTPDeltaWorkspace() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [timeframe, setTimeframe] = useState("15minute");
  const [lookback, setLookback] = useState(60);
  const [anchorMode, setAnchorMode] = useState("auto_pivot");
  const [hMode, setHMode] = useState("median_tpd");
  const [manualH, setManualH] = useState(47);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [journalCount, setJournalCount] = useState(0);
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [agentRunning, setAgentRunning] = useState(false);

  const load = () => {
    setError(null);
    setLoading(true);
    Promise.all([
      getGannTPDeltaSummary(),
      getGannTPDeltaLiveSnapshot(underlying, timeframe, lookback, anchorMode, hMode, hMode === "manual" ? manualH : undefined),
      getGannTPDeltaPaperJournal(underlying, 20),
      getGannTPDeltaPaperAgentStatus(50),
    ])
      .then(([summaryResponse, snapshotResponse, journalResponse, agentResponse]) => {
        setWorkspace({
          module: summaryResponse.data,
          selection: { underlying, timeframe, lookback_sessions: lookback, anchor_mode: anchorMode, h_mode: hMode },
          snapshot: snapshotResponse.data,
          backtest: { summary: { event_count: 0, total_points: 0, win_rate_pct: 0, avg_win: 0, avg_loss: 0 }, events: [] },
        });
        setJournalCount(journalResponse.data?.summary?.count || 0);
        setAgentStatus(agentResponse.data);
        getGannTPDeltaBacktest(underlying, timeframe, lookback, anchorMode, hMode)
          .then((backtestResponse) => {
            setWorkspace((current) => (current ? { ...current, backtest: backtestResponse.data } : current));
          })
          .catch(() => {
            setWorkspace((current) => current);
          });
      })
      .catch((err) => setError(describeApiError(err, "Failed to load Gann TP Delta workspace.")))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, [underlying, timeframe, lookback, anchorMode, hMode, manualH]);

  const snapshot = workspace?.snapshot;
  const signal = snapshot?.signal;

  const recordProposal = () => {
    setLoading(true);
    runGannTPDeltaPaperProposal(underlying, timeframe, lookback, anchorMode, hMode)
      .then(() => getGannTPDeltaPaperJournal(underlying, 20))
      .then((journalResponse) => setJournalCount(journalResponse.data?.summary?.count || 0))
      .catch((err) => setError(describeApiError(err, "Failed to record paper proposal.")))
      .finally(() => setLoading(false));
  };

  const runAgent = () => {
    setAgentRunning(true);
    setError(null);
    runGannTPDeltaPaperAgentOnce(timeframe, lookback, anchorMode, hMode, false)
      .then((response) => setAgentStatus(response.data))
      .catch((err) => setError(describeApiError(err, "Failed to run Gann paper agent.")))
      .finally(() => setAgentRunning(false));
  };

  return (
    <main className="min-h-screen bg-bg-primary px-4 py-4 text-text-primary">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm text-accent-blue">
            <Compass size={16} />
            Gann TP Delta Harmonic
          </div>
          <h1 className="font-mono text-2xl font-semibold tracking-normal">Price-Time Geometry Research</h1>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={load} className="inline-flex items-center gap-2 rounded-md border border-bg-border px-3 py-2 text-sm text-text-secondary hover:border-accent-blue hover:text-text-primary">
            <RefreshCw size={15} className={clsx(loading && "animate-spin")} />
            Refresh
          </button>
          <button type="button" onClick={recordProposal} className="inline-flex items-center gap-2 rounded-md border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-sm text-emerald-300 hover:border-emerald-300">
            <FilePlus2 size={15} />
            Paper proposal
          </button>
          <button type="button" onClick={runAgent} disabled={agentRunning} className="inline-flex items-center gap-2 rounded-md border border-accent-blue/40 bg-accent-blue/10 px-3 py-2 text-sm text-accent-blue hover:border-accent-blue disabled:cursor-not-allowed disabled:opacity-60">
            <Play size={15} className={clsx(agentRunning && "animate-pulse")} />
            Run paper agent
          </button>
        </div>
      </div>

      <div className="mb-4 grid gap-2 lg:grid-cols-6">
        <Select label="Underlying" value={underlying} options={workspace?.module.underlyings || UNDERLYINGS} onChange={setUnderlying} />
        <Select label="Timeframe" value={timeframe} options={workspace?.module.timeframes || TIMEFRAMES} onChange={setTimeframe} />
        <Select label="Anchor" value={anchorMode} options={ANCHORS} onChange={setAnchorMode} />
        <Select label="h mode" value={hMode} options={H_MODES} onChange={setHMode} />
        <label className="rounded-md border border-bg-border bg-bg-secondary/70 px-3 py-2 text-xs text-text-muted">
          Lookback
          <input className="mt-1 w-full bg-transparent font-mono text-sm text-text-primary outline-none" type="number" min={4} max={180} value={lookback} onChange={(event) => setLookback(Number(event.target.value))} />
        </label>
        <label className="rounded-md border border-bg-border bg-bg-secondary/70 px-3 py-2 text-xs text-text-muted">
          Manual h
          <input className="mt-1 w-full bg-transparent font-mono text-sm text-text-primary outline-none" type="number" value={manualH} onChange={(event) => setManualH(Number(event.target.value))} />
        </label>
      </div>

      {error ? (
        <div className="mb-4 flex items-center gap-2 rounded-md border border-red-400/30 bg-red-400/10 px-3 py-2 text-sm text-red-200">
          <AlertTriangle size={16} />
          {error}
        </div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="rounded-lg border border-bg-border bg-bg-secondary/60 p-3">
          <GannChart snapshot={snapshot} />
        </div>
        <aside className="space-y-3">
          <MetricGrid snapshot={snapshot} journalCount={journalCount} />
          <Panel title="Confluence">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs uppercase tracking-[0.16em] text-text-muted">State</div>
                <div className={clsx("mt-1 font-mono text-lg", signal?.state?.includes("setup") ? "text-emerald-300" : "text-text-primary")}>{signal?.state || "no signal"}</div>
              </div>
              <div className="rounded-md border border-accent-blue/30 bg-accent-blue/10 px-3 py-2 font-mono text-xl text-accent-blue">{signal?.score ?? 0}/{signal?.threshold ?? 3}</div>
            </div>
            <div className="mt-3 space-y-1 text-sm text-text-secondary">
              {(signal?.reasons || ["No active confluence."]).map((reason) => <div key={reason}>- {reason}</div>)}
            </div>
          </Panel>
          <Panel title="Alerts">
            <div className="space-y-2">
              {(snapshot?.alerts?.length ? snapshot.alerts : [{ key: "none", severity: "info", message: "No alert events active." }]).map((alert) => (
                <div key={`${alert.key}-${alert.message}`} className="rounded-md border border-bg-border bg-bg-primary/40 px-3 py-2 text-sm text-text-secondary">{alert.message}</div>
              ))}
            </div>
          </Panel>
          <AgentPanel status={agentStatus} running={agentRunning} onRun={runAgent} />
        </aside>
      </section>

      <section className="mt-4 grid gap-4 xl:grid-cols-2">
        <Panel title="Backtest">
          <div className="grid grid-cols-5 gap-2 text-sm">
            <Stat label="Events" value={workspace?.backtest.summary.event_count ?? 0} />
            <Stat label="Points" value={workspace?.backtest.summary.total_points ?? 0} />
            <Stat label="Win %" value={workspace?.backtest.summary.win_rate_pct ?? 0} />
            <Stat label="Avg win" value={workspace?.backtest.summary.avg_win ?? 0} />
            <Stat label="Avg loss" value={workspace?.backtest.summary.avg_loss ?? 0} />
          </div>
        </Panel>
        <Panel title="Levels">
          <div className="grid gap-2 md:grid-cols-2">
            {(snapshot?.sq9_levels || []).slice(0, 8).map((level) => (
              <div key={`${level.direction}-${level.degree}-${level.price}`} className="flex items-center justify-between rounded-md border border-bg-border bg-bg-primary/40 px-3 py-2 text-sm">
                <span className={level.level_type === "cardinal" ? "text-amber-300" : "text-text-secondary"}>{level.degree} deg {level.direction}</span>
                <span className="font-mono">{numberFmt.format(level.price)}</span>
              </div>
            ))}
          </div>
        </Panel>
      </section>
    </main>
  );
}

function Select({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (value: string) => void }) {
  return (
    <label className="rounded-md border border-bg-border bg-bg-secondary/70 px-3 py-2 text-xs text-text-muted">
      {label}
      <select className="mt-1 w-full bg-bg-secondary font-mono text-sm text-text-primary outline-none" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-bg-border bg-bg-secondary/60 p-3">
      <h2 className="mb-3 flex items-center gap-2 font-mono text-sm font-semibold text-text-primary"><Target size={15} />{title}</h2>
      {children}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-bg-border bg-bg-primary/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className="mt-1 font-mono text-base text-text-primary">{typeof value === "number" ? numberFmt.format(value) : value}</div>
    </div>
  );
}

function MetricGrid({ snapshot, journalCount }: { snapshot?: Workspace["snapshot"]; journalCount: number }) {
  return (
    <Panel title="Dashboard">
      <div className="grid grid-cols-2 gap-2">
        <Stat label="Spot" value={snapshot?.spot_price ?? "-"} />
        <Stat label="h" value={snapshot?.h?.value ? snapshot.h.value.toFixed(2) : "-"} />
        <Stat label="Anchor" value={snapshot?.anchor?.kind || "-"} />
        <Stat label="Bias" value={snapshot?.signal?.bias || "-"} />
        <Stat label="Nearest angle" value={snapshot?.nearest_angle?.name || "-"} />
        <Stat label="SQ9" value={snapshot?.nearest_sq9_level?.price ? snapshot.nearest_sq9_level.price.toFixed(2) : "-"} />
        <Stat label="Cycle" value={snapshot?.active_time_cycle?.cycle || "-"} />
        <Stat label="Journal" value={journalCount} />
      </div>
    </Panel>
  );
}

function AgentPanel({ status, running, onRun }: { status: AgentStatus | null; running: boolean; onRun: () => void }) {
  const openPositions = status?.open_positions || [];
  const recentSignals = status?.recent_signals || [];
  return (
    <Panel title="Paper Agent">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 text-sm text-text-secondary">
          <Bot size={15} className="shrink-0 text-accent-blue" />
          <span className="truncate">{status?.last_message || "Paper agent has not run yet."}</span>
        </div>
        <button type="button" onClick={onRun} disabled={running} className="inline-flex shrink-0 items-center gap-2 rounded-md border border-accent-blue/40 px-2 py-1 text-xs text-accent-blue hover:border-accent-blue disabled:cursor-not-allowed disabled:opacity-60">
          <Play size={13} />
          Run
        </button>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Stat label="Open" value={status?.summary.open_positions ?? 0} />
        <Stat label="Unrl P&L" value={status?.summary.unrealized_pnl ?? 0} />
        <Stat label="Total P&L" value={status?.summary.total_pnl ?? 0} />
      </div>
      <div className="mt-3 space-y-2">
        {openPositions.slice(0, 4).map((position) => (
          <div key={position.position_id} className="rounded-md border border-bg-border bg-bg-primary/40 px-3 py-2 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate font-semibold text-text-primary">{position.underlying} {position.option_type} {position.strike}</span>
              <span className={clsx("font-mono", (position.unrealized_pnl || 0) >= 0 ? "text-emerald-300" : "text-red-300")}>{numberFmt.format(position.unrealized_pnl || 0)}</span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-text-muted">
              <span className="truncate">{position.expiry} | score {position.signal_score ?? "-"}</span>
              <span className="shrink-0 font-mono">{numberFmt.format(position.entry_price || 0)} -&gt; {numberFmt.format(position.current_price || 0)}</span>
            </div>
          </div>
        ))}
        {!openPositions.length ? <div className="rounded-md border border-bg-border bg-bg-primary/40 px-3 py-2 text-sm text-text-muted">No open paper positions.</div> : null}
      </div>
      <div className="mt-3 max-h-40 space-y-1 overflow-auto pr-1">
        {recentSignals.slice(0, 8).map((item) => (
          <div key={`${item.underlying}-${item.signal_state}-${item.reason}`} className="flex items-center justify-between gap-2 rounded-md bg-bg-primary/30 px-2 py-1 text-xs text-text-secondary">
            <span className="truncate">{item.underlying} {item.signal_state || "none"}</span>
            <span className="shrink-0 font-mono text-text-muted">{item.decision || "skip"} {item.signal_score ?? 0}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function GannChart({ snapshot }: { snapshot?: Workspace["snapshot"] }) {
  const bars = snapshot?.bars || [];
  const geometry = useMemo(() => buildChartGeometry(snapshot), [snapshot]);
  if (!snapshot || snapshot.status !== "ready" || bars.length < 2 || !geometry) {
    return <div className="flex h-[520px] items-center justify-center rounded-md border border-dashed border-bg-border text-sm text-text-muted">{snapshot?.reason || "No chart data."}</div>;
  }
  const { width, height, xFor, yFor, closePath } = geometry;
  return (
    <div className="h-[560px] w-full overflow-auto">
      <svg viewBox={`0 0 ${width} ${height}`} className="h-full min-w-[1120px]">
        <rect width={width} height={height} fill="transparent" />
        {bars.map((bar) => {
          const x = xFor(bar.index);
          return (
            <g key={`${bar.time}-${bar.index}`}>
              <line x1={x} x2={x} y1={yFor(bar.high)} y2={yFor(bar.low)} stroke={bar.close >= bar.open ? "#34d399" : "#fb7185"} strokeWidth={1} opacity={0.65} />
              <rect x={x - 2} y={Math.min(yFor(bar.open), yFor(bar.close))} width={4} height={Math.max(Math.abs(yFor(bar.open) - yFor(bar.close)), 1)} fill={bar.close >= bar.open ? "#34d399" : "#fb7185"} opacity={0.55} />
            </g>
          );
        })}
        {(snapshot.time_cycles || []).filter((cycle) => cycle.active).map((cycle) => (
          <rect key={cycle.cycle} x={xFor(cycle.start_bar_index)} y={0} width={Math.max(xFor(cycle.end_bar_index) - xFor(cycle.start_bar_index), 4)} height={height} fill="#f59e0b" opacity={0.08} />
        ))}
        {(snapshot.sq9_levels || []).slice(0, 16).map((level) => (
          <line key={`${level.direction}-${level.degree}`} x1={0} x2={width} y1={yFor(level.price)} y2={yFor(level.price)} stroke={level.level_type === "cardinal" ? "#fbbf24" : "#94a3b8"} strokeDasharray={level.level_type === "cardinal" ? "0" : "5 5"} opacity={0.35} />
        ))}
        {(snapshot.gann_angles || []).map((angle) => (
          <line key={angle.name} x1={xFor(snapshot.anchor?.bar_index || 0)} y1={yFor(snapshot.anchor?.price || angle.current_price)} x2={width} y2={yFor(angle.projected_price)} stroke={angle.name === "1x1" ? "#60a5fa" : "#64748b"} strokeWidth={angle.name === "1x1" ? 2 : 1} opacity={angle.name === "1x1" ? 0.9 : 0.45} />
        ))}
        <path d={closePath} fill="none" stroke="#e5e7eb" strokeWidth={1.6} opacity={0.9} />
        <text x={14} y={24} fill="#94a3b8" fontSize={12}>{snapshot.anchor?.kind} | h {snapshot.h?.value.toFixed(2)} | score {snapshot.signal?.score}/{snapshot.signal?.threshold}</text>
      </svg>
    </div>
  );
}

function buildChartGeometry(snapshot?: Workspace["snapshot"]) {
  const bars = snapshot?.bars || [];
  if (bars.length < 2) return null;
  const width = 1040;
  const height = 500;
  const barValues = bars.flatMap((bar) => [bar.high, bar.low]);
  const barMin = Math.min(...barValues);
  const barMax = Math.max(...barValues);
  const current = snapshot?.spot_price ?? bars[bars.length - 1]?.close;
  const focusPad = Math.max((barMax - barMin) * 0.35, Math.abs(current) * 0.008, 1);
  const focusMin = Math.min(barMin, current) - focusPad;
  const focusMax = Math.max(barMax, current) + focusPad;
  const values = [...barValues, current];
  if (snapshot?.anchor?.price && snapshot.anchor.price >= focusMin && snapshot.anchor.price <= focusMax) {
    values.push(snapshot.anchor.price);
  }
  values.push(
    ...(snapshot?.gann_angles || [])
      .map((angle) => angle.current_price)
      .filter((price) => price >= focusMin && price <= focusMax),
  );
  values.push(
    ...(snapshot?.sq9_levels || [])
      .map((level) => level.price)
      .filter((price) => price >= focusMin && price <= focusMax),
  );
  values.push(...(snapshot?.signal?.targets || []).filter((price) => price >= focusMin && price <= focusMax));
  if (snapshot?.signal?.stop && snapshot.signal.stop >= focusMin && snapshot.signal.stop <= focusMax) {
    values.push(snapshot.signal.stop);
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = Math.max((max - min) * 0.12, Math.abs(current) * 0.001, 1);
  const minIndex = Math.min(...bars.map((bar) => bar.index), snapshot?.anchor?.bar_index ?? bars[0].index);
  const maxIndex = Math.max(...bars.map((bar) => bar.index), minIndex + 1);
  const xFor = (index: number) => ((index - minIndex) / Math.max(maxIndex - minIndex, 1)) * width;
  const yFor = (price: number) => height - ((price - min + pad) / Math.max(max - min + pad * 2, 1)) * height;
  const closePath = bars.map((bar, index) => `${index === 0 ? "M" : "L"} ${xFor(bar.index).toFixed(2)} ${yFor(bar.close).toFixed(2)}`).join(" ");
  return { width, height, xFor, yFor, closePath };
}
