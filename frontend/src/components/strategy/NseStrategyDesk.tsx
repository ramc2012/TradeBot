"use client";

/**
 * NSE Strategy 1 desk — single unified, tabbed page.
 *
 * Merges the former /strategy (classic) and /strategy/live (dense) pages
 * into one NSE-S1-focused workspace (2026-06-02). Strategy 2 was deleted;
 * cross-desk (commodity / directional / FMP) context lives on its own
 * dedicated pages, so this desk is purely Strategy 1: 30m ATM option
 * premium MACD zero-cross.
 *
 * Tabs:
 *   Watchlist   — sortable, zebra-striped MACD watchlist (trend + value)
 *   Positions   — live open positions
 *   Trades      — today's closed trades + day total
 *   Statistics  — win rate / profit factor / P&L split / equity curve
 *   Signals     — bucketed Met/Favourable/Drifting + raw signal lane
 *   Activity    — agent commentary + NSE audit feed
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BarChart3,
  CandlestickChart,
  ListChecks,
  Radio,
  Shield,
  TrendingUp,
  Wallet,
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
  api as apiClient,
  getStrategyAgentComments,
  getStrategyAgentStatus,
  getStrategyOpenSignals,
} from "@/lib/api";
import {
  AuditFeed,
  ThreeListView,
  formatINR,
  formatIST,
  type AuditEvent,
  type BucketedRow,
} from "@/components/strategy/desk-helpers";

const REFRESH_MS = 4_000;
const NSE_STRATEGY_KEY = "macd_strategy";

// ── Types ────────────────────────────────────────────────────────────────

type WatchlistRow = {
  underlying?: string | null;
  symbol?: string | null;
  direction?: string | null;
  status?: string | null;
  reason?: string | null;
  strength?: string | null;
  instruction?: string | null;
  expiry?: string | null;
  atm_strike?: number | null;
  strike?: number | null;
  ltp?: number | null;
  iv_pct?: number | null;
  macd?: number | null;
  previous_macd?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
  priority_score?: number | null;
  as_of?: string | null;
};

type PositionRow = {
  underlying?: string | null;
  symbol?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  phase?: string | null;
  trailing_stop?: number | null;
  latest_rsi?: number | null;
  signal_reason?: string | null;
  entered_at?: string | null;
  price_updated_at?: string | null;
};

type TradeRow = {
  symbol?: string | null;
  action?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
  instrument_type?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
};

type StrategySummary = {
  total_equity?: number | null;
  initial_capital?: number | null;
  available_capital?: number | null;
  realized_pnl?: number | null;
  realized_pnl_lifetime?: number | null;
  unrealized_pnl?: number | null;
  day_pnl?: number | null;
  day_realized_pnl?: number | null;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  sharpe_ratio?: number | null;
  total_trades?: number | null;
  open_positions?: number | null;
  entries?: number | null;
  exits?: number | null;
};

type StrategyLane = {
  key: string;
  label: string;
  summary?: StrategySummary;
  positions?: PositionRow[];
  signals?: BucketedRow[];
  signal_lane?: BucketedRow[];
  today_trades?: TradeRow[];
  historical_trades?: TradeRow[];
  trade_history?: TradeRow[];
  last_scan_at?: string | null;
  last_message?: string | null;
};

type AgentStatus = {
  running?: boolean;
  loop_active?: boolean;
  kill_switch_active?: boolean;
  last_run_at?: string | null;
  last_message?: string | null;
  commentary?: Array<{ time?: string; scope?: string; tone?: string; level?: string; message?: string }>;
  strategies?: StrategyLane[];
};

type Tab = "watchlist" | "positions" | "trades" | "statistics" | "signals" | "activity";

type SortDir = "asc" | "desc";

// ── Formatting helpers ─────────────────────────────────────────────────────

function fmt(n?: number | null, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtSigned(n?: number | null, digits = 0, suffix = ""): string {
  if (n == null || Number.isNaN(n)) return "—";
  const prefix = n > 0 ? "+" : "";
  return `${prefix}${Number(n).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${suffix}`;
}

function pnlTone(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "text-text-muted";
  if (n > 0) return "text-accent-green";
  if (n < 0) return "text-accent-red";
  return "text-text-secondary";
}

function prettify(value?: string | null): string {
  if (!value) return "—";
  return value.replaceAll("_", " ");
}

function heldFor(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const mins = Math.floor((Date.now() - t) / 60_000);
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const rem = mins % 60;
  if (h < 24) return rem ? `${h}h ${rem}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

const todayIST = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });

// ── Small UI atoms ─────────────────────────────────────────────────────────

function StatChip({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/30 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-sm font-semibold", tone || "text-text-primary")}>{value}</div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  const s = (status || "").toLowerCase();
  const tone = s.includes("entry-ready") || s.includes("ready") || s.includes("active")
    ? "border-accent-green/40 bg-accent-green/10 text-accent-green"
    : s.includes("trend-aligned") || s.includes("monitor")
      ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
      : s.includes("waiting") || s.includes("standby")
        ? "border-bg-border bg-bg-secondary/40 text-text-secondary"
        : s.includes("missing") || s.includes("avoid") || s.includes("stale")
          ? "border-accent-red/40 bg-accent-red/10 text-accent-red"
          : "border-bg-border bg-bg-secondary/40 text-text-secondary";
  return (
    <span className={clsx("inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em]", tone)}>
      {prettify(status) || "—"}
    </span>
  );
}

function DirBadge({ direction }: { direction?: string | null }) {
  if (!direction) return <span className="text-text-muted">—</span>;
  const isCE = direction.toUpperCase() === "CE";
  return (
    <span className={clsx(
      "inline-flex rounded-md border px-1.5 py-0.5 text-[10px] font-bold",
      isCE ? "border-accent-green/40 bg-accent-green/10 text-accent-green" : "border-accent-red/40 bg-accent-red/10 text-accent-red",
    )}>
      {direction.toUpperCase()}
    </span>
  );
}

/** MACD trend cell — current value + rising/falling arrow vs previous bucket. */
function MacdTrend({ macd, prev, hist }: { macd?: number | null; prev?: number | null; hist?: number | null }) {
  if (macd == null) return <span className="text-text-muted">—</span>;
  const delta = prev == null ? null : macd - prev;
  const rising = delta != null && delta > 0;
  const falling = delta != null && delta < 0;
  // Above/below the zero line drives the base color (the strategy's signal axis).
  const aboveZero = macd >= 0;
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx("font-mono text-xs font-semibold", aboveZero ? "text-accent-green" : "text-accent-red")}>
        {macd >= 0 ? "+" : ""}{fmt(macd, 4)}
      </span>
      {rising ? <ArrowUp size={12} className="text-accent-green" /> : falling ? <ArrowDown size={12} className="text-accent-red" /> : <span className="text-text-muted">·</span>}
      {delta != null ? (
        <span className="text-[10px] text-text-muted" title="Change vs previous 30m bucket">
          {delta >= 0 ? "+" : ""}{fmt(delta, 4)}
        </span>
      ) : null}
      {hist != null ? (
        <span className={clsx("text-[10px]", hist >= 0 ? "text-accent-green/70" : "text-accent-red/70")} title="MACD histogram (momentum)">
          h{hist >= 0 ? "+" : ""}{fmt(hist, 3)}
        </span>
      ) : null}
    </div>
  );
}

function SortHeader({
  label,
  sortKey,
  active,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: string;
  active: boolean;
  dir: SortDir;
  onSort: (key: string) => void;
  align?: "left" | "right";
}) {
  return (
    <th className={clsx("pb-2 pr-3 select-none", align === "right" && "text-right")}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={clsx(
          "inline-flex items-center gap-1 transition-colors hover:text-text-primary",
          align === "right" && "flex-row-reverse",
          active ? "text-text-primary" : "text-text-muted",
        )}
      >
        {label}
        {active ? (
          dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />
        ) : (
          <ArrowUpDown size={11} className="opacity-40" />
        )}
      </button>
    </th>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex min-h-[180px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
      {message}
    </div>
  );
}

function zebra(idx: number): string {
  return idx % 2 === 0 ? "bg-transparent" : "bg-bg-secondary/20";
}

// ── Main component ─────────────────────────────────────────────────────────

export default function NseStrategyDesk() {
  const [tab, setTab] = useState<Tab>("watchlist");

  const statusQuery = useQuery({
    queryKey: ["nse-desk", "status"],
    queryFn: async () => (await getStrategyAgentStatus()).data as AgentStatus,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });
  const signalsQuery = useQuery({
    queryKey: ["nse-desk", "open-signals"],
    queryFn: async () => (await getStrategyOpenSignals()).data as { strategy1_watchlist?: WatchlistRow[] },
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });
  const commentsQuery = useQuery({
    queryKey: ["nse-desk", "comments"],
    queryFn: async () => (await getStrategyAgentComments(40)).data,
    refetchInterval: REFRESH_MS * 2,
  });
  const auditQuery = useQuery({
    queryKey: ["nse-desk", "audit"],
    queryFn: async () => (await apiClient.get("/api/audit/events?market=nse&limit=40")).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const status = statusQuery.data ?? {};
  const lane = useMemo<StrategyLane | undefined>(
    () => (status.strategies || []).find((s) => s.key === NSE_STRATEGY_KEY) || (status.strategies || [])[0],
    [status.strategies],
  );
  const summary = lane?.summary ?? {};
  const watchlist = useMemo(() => signalsQuery.data?.strategy1_watchlist ?? [], [signalsQuery.data]);
  const positions = useMemo(() => lane?.positions ?? [], [lane?.positions]);

  const todaysTrades = useMemo(() => {
    const today = todayIST();
    const fromSplit = lane?.today_trades;
    if (fromSplit && fromSplit.length) return fromSplit;
    return (lane?.trade_history ?? []).filter((t) => (t.exit_time || "").startsWith(today));
  }, [lane?.today_trades, lane?.trade_history]);

  const signalRows = useMemo<BucketedRow[]>(
    () => (lane?.signals && lane.signals.length ? lane.signals : lane?.signal_lane) ?? [],
    [lane?.signals, lane?.signal_lane],
  );

  const auditEvents: AuditEvent[] = auditQuery.data?.events ?? [];
  const commentary = useMemo(() => {
    const fromComments = Array.isArray(commentsQuery.data) ? commentsQuery.data : commentsQuery.data?.comments;
    return (fromComments || status.commentary || []) as Array<{ time?: string; scope?: string; tone?: string; level?: string; message?: string }>;
  }, [commentsQuery.data, status.commentary]);

  const running = status.running && !status.kill_switch_active;
  const statusLabel = status.kill_switch_active ? "Kill switch" : running ? "Running" : "Idle";
  const statusTone = status.kill_switch_active
    ? "border-accent-red/40 bg-accent-red/10 text-accent-red"
    : running
      ? "border-accent-green/40 bg-accent-green/10 text-accent-green"
      : "border-accent-amber/40 bg-accent-amber/10 text-accent-amber";

  const tabs: Array<{ key: Tab; label: string; icon: typeof CandlestickChart; count?: number }> = [
    { key: "watchlist", label: "Watchlist", icon: ListChecks, count: watchlist.length },
    { key: "positions", label: "Positions", icon: Wallet, count: positions.length },
    { key: "trades", label: "Trades", icon: CandlestickChart, count: todaysTrades.length },
    { key: "statistics", label: "Statistics", icon: BarChart3 },
    { key: "signals", label: "Signals", icon: Radio, count: signalRows.length },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="max-w-screen-2xl space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 font-mono text-lg font-bold text-text-primary">
            <TrendingUp size={18} className="text-accent-green" />
            NSE Strategy 1 · 30m ATM MACD
          </h1>
          <div className="mt-1 text-xs text-text-muted">
            Option-premium MACD zero-cross. Hard stop −25%, exit on opposite 30m cross. Flip CE↔PE allowed.
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className={clsx("rounded-full border px-2.5 py-1 font-semibold uppercase tracking-[0.14em]", statusTone)}>
            {statusLabel}
          </span>
          <span className="rounded-full border border-bg-border bg-bg-secondary/30 px-2.5 py-1 text-text-muted">
            Last scan {formatIST(status.last_run_at)}
          </span>
        </div>
      </div>

      {/* Summary chips */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        <StatChip label="Equity" value={formatINR(summary.total_equity)} />
        <StatChip label="Day P&L" value={fmtSigned(summary.day_pnl, 0)} tone={pnlTone(summary.day_pnl)} />
        <StatChip label="Realized (life)" value={fmtSigned(summary.realized_pnl_lifetime ?? summary.realized_pnl, 0)} tone={pnlTone(summary.realized_pnl_lifetime ?? summary.realized_pnl)} />
        <StatChip label="Open P&L" value={fmtSigned(summary.unrealized_pnl, 0)} tone={pnlTone(summary.unrealized_pnl)} />
        <StatChip label="Open / Trades" value={`${summary.open_positions ?? 0} / ${summary.total_trades ?? 0}`} />
        <StatChip label="Win rate" value={summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(1)}%` : "—"} />
      </div>

      {/* Tabs */}
      <div className="flex flex-wrap gap-1 border-b border-bg-border">
        {tabs.map((t) => {
          const Icon = t.icon;
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={clsx(
                "flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-semibold transition-colors",
                active
                  ? "border-accent-green text-text-primary"
                  : "border-transparent text-text-muted hover:text-text-secondary",
              )}
            >
              <Icon size={13} />
              {t.label}
              {t.count != null ? (
                <span className={clsx("rounded-full px-1.5 py-0.5 text-[10px]", active ? "bg-accent-green/15 text-accent-green" : "bg-bg-secondary/40 text-text-muted")}>
                  {t.count}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      {tab === "watchlist" ? <WatchlistTab rows={watchlist} /> : null}
      {tab === "positions" ? <PositionsTab rows={positions} /> : null}
      {tab === "trades" ? <TradesTab rows={todaysTrades} /> : null}
      {tab === "statistics" ? <StatisticsTab summary={summary} trades={lane?.trade_history ?? []} /> : null}
      {tab === "signals" ? <SignalsTab rows={signalRows} /> : null}
      {tab === "activity" ? <ActivityTab commentary={commentary} audit={auditEvents} /> : null}
    </div>
  );
}

// ── Watchlist tab — sortable + zebra ───────────────────────────────────────

function WatchlistTab({ rows }: { rows: WatchlistRow[] }) {
  const [sortKey, setSortKey] = useState<string>("priority_score");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const onSort = (key: string) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const sorted = useMemo(() => {
    const getVal = (r: WatchlistRow): number | string => {
      switch (sortKey) {
        case "underlying": return r.underlying || "";
        case "direction": return r.direction || "";
        case "spot": return r.atm_strike ?? 0; // strike proxy; spot not always present
        case "strike": return r.strike ?? r.atm_strike ?? 0;
        case "ltp": return r.ltp ?? -Infinity;
        case "iv": return r.iv_pct ?? -Infinity;
        case "rsi": return r.rsi ?? -Infinity;
        case "macd": return r.macd ?? -Infinity;
        case "status": return r.status || "";
        case "priority_score":
        default: return r.priority_score ?? -Infinity;
      }
    };
    const copy = [...rows];
    copy.sort((a, b) => {
      const va = getVal(a);
      const vb = getVal(b);
      let cmp: number;
      if (typeof va === "string" || typeof vb === "string") {
        cmp = String(va).localeCompare(String(vb));
      } else {
        cmp = (va as number) - (vb as number);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  if (!rows.length) {
    return <EmptyState message="No watchlist rows. The desk publishes one row per index+side once 30m ATM premium history is available." />;
  }

  return (
    <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold text-text-primary">ATM MACD watchlist</div>
        <div className="text-[11px] text-text-muted">Click a column to sort · {sorted.length} rows</div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1100px] text-left text-xs">
          <thead className="border-b border-bg-border">
            <tr>
              <SortHeader label="Underlying" sortKey="underlying" active={sortKey === "underlying"} dir={sortDir} onSort={onSort} />
              <SortHeader label="Dir" sortKey="direction" active={sortKey === "direction"} dir={sortDir} onSort={onSort} />
              <SortHeader label="Strike" sortKey="strike" active={sortKey === "strike"} dir={sortDir} onSort={onSort} align="right" />
              <SortHeader label="LTP" sortKey="ltp" active={sortKey === "ltp"} dir={sortDir} onSort={onSort} align="right" />
              <SortHeader label="IV%" sortKey="iv" active={sortKey === "iv"} dir={sortDir} onSort={onSort} align="right" />
              <SortHeader label="RSI" sortKey="rsi" active={sortKey === "rsi"} dir={sortDir} onSort={onSort} align="right" />
              <th className="pb-2 pr-3">MACD · trend</th>
              <SortHeader label="Status" sortKey="status" active={sortKey === "status"} dir={sortDir} onSort={onSort} />
              <SortHeader label="Score" sortKey="priority_score" active={sortKey === "priority_score"} dir={sortDir} onSort={onSort} align="right" />
              <th className="pb-2">Instruction</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, idx) => (
              <tr key={`${r.underlying}-${r.direction}-${idx}`} className={clsx("border-b border-bg-border/30 align-top", zebra(idx))}>
                <td className="py-2.5 pr-3">
                  <div className="font-semibold text-text-primary">{r.underlying}</div>
                  <div className="mt-0.5 text-[10px] text-text-muted">{r.expiry || "—"}</div>
                </td>
                <td className="py-2.5 pr-3"><DirBadge direction={r.direction} /></td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{fmt(r.strike ?? r.atm_strike, 0)}</td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(r.ltp)}</td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{r.iv_pct != null ? fmt(r.iv_pct, 1) : "—"}</td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{r.rsi != null ? fmt(r.rsi, 1) : "—"}</td>
                <td className="py-2.5 pr-3"><MacdTrend macd={r.macd} prev={r.previous_macd} hist={r.macd_histogram} /></td>
                <td className="py-2.5 pr-3"><StatusBadge status={r.status} /></td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{r.priority_score != null ? fmt(r.priority_score, 2) : "—"}</td>
                <td className="max-w-[320px] py-2.5 text-[11px] text-text-secondary" title={r.instruction || ""}>{r.instruction || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Positions tab ──────────────────────────────────────────────────────────

function PositionsTab({ rows }: { rows: PositionRow[] }) {
  if (!rows.length) return <EmptyState message="No open positions right now." />;
  return (
    <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1180px] text-left text-xs">
          <thead className="border-b border-bg-border text-text-muted">
            <tr>
              <th className="pb-2 pr-3">Underlying</th>
              <th className="pb-2 pr-3">Contract</th>
              <th className="pb-2 pr-3">Side</th>
              <th className="pb-2 pr-3 text-right">Qty</th>
              <th className="pb-2 pr-3 text-right">Entry</th>
              <th className="pb-2 pr-3 text-right">Mark</th>
              <th className="pb-2 pr-3 text-right">Open P&amp;L</th>
              <th className="pb-2 pr-3">Phase</th>
              <th className="pb-2 pr-3 text-right">Trail / RSI</th>
              <th className="pb-2 pr-3">Signal</th>
              <th className="pb-2">Age</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p, idx) => (
              <tr key={`${p.underlying}-${p.option_type}-${p.strike}-${idx}`} className={clsx("border-b border-bg-border/30 align-top", zebra(idx))}>
                <td className="py-2.5 pr-3 font-semibold text-text-primary">{p.underlying}</td>
                <td className="py-2.5 pr-3 font-mono text-text-secondary">
                  {p.option_type} {p.strike}
                  <div className="text-[10px] text-text-muted">{p.expiry || "—"}</div>
                </td>
                <td className="py-2.5 pr-3"><DirBadge direction={p.option_type} /></td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{p.qty}</td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(p.entry_price)}</td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(p.current_price)}</td>
                <td className={clsx("py-2.5 pr-3 text-right font-mono font-semibold", pnlTone(p.unrealized_pnl))}>
                  {fmtSigned(p.unrealized_pnl, 0)}
                  {p.return_pct != null ? <div className="text-[10px] font-normal text-text-muted">{fmtSigned(p.return_pct, 1, "%")}</div> : null}
                </td>
                <td className="py-2.5 pr-3"><StatusBadge status={p.phase} /></td>
                <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">
                  {p.trailing_stop != null ? fmt(p.trailing_stop) : "—"}
                  <div className="text-[10px] text-text-muted">RSI {p.latest_rsi != null ? fmt(p.latest_rsi, 1) : "—"}</div>
                </td>
                <td className="max-w-[200px] py-2.5 pr-3 text-[11px] text-text-secondary">{prettify(p.signal_reason)}</td>
                <td className="py-2.5 text-[11px] text-text-muted">{heldFor(p.entered_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Trades tab (today only) ────────────────────────────────────────────────

function TradesTab({ rows }: { rows: TradeRow[] }) {
  const dayTotal = useMemo(() => rows.reduce((s, t) => s + (t.pnl || 0), 0), [rows]);
  const wins = rows.filter((t) => (t.pnl || 0) > 0).length;

  if (!rows.length) return <EmptyState message="No closed trades today." />;

  return (
    <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3 text-xs">
        <span className="text-text-muted">{rows.length} trades today · {wins} win / {rows.length - wins} loss</span>
        <span className={clsx("font-mono font-semibold", pnlTone(dayTotal))}>Day realized {fmtSigned(dayTotal, 0)}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1000px] text-left text-xs">
          <thead className="border-b border-bg-border text-text-muted">
            <tr>
              <th className="pb-2 pr-3">Contract</th>
              <th className="pb-2 pr-3">Side</th>
              <th className="pb-2 pr-3 text-right">Qty</th>
              <th className="pb-2 pr-3 text-right">Entry</th>
              <th className="pb-2 pr-3 text-right">Exit</th>
              <th className="pb-2 pr-3 text-right">P&amp;L</th>
              <th className="pb-2 pr-3 text-right">Ret%</th>
              <th className="pb-2 pr-3">Entered</th>
              <th className="pb-2">Exited</th>
            </tr>
          </thead>
          <tbody>
            {[...rows].reverse().map((t, idx) => {
              const notional = (t.entry_price || 0) * (t.qty || 0);
              const ret = notional > 0 && t.pnl != null ? (t.pnl / notional) * 100 : null;
              return (
                <tr key={`${t.symbol}-${t.exit_time}-${idx}`} className={clsx("border-b border-bg-border/30", zebra(idx))}>
                  <td className="py-2.5 pr-3">
                    <div className="font-semibold text-text-primary">{t.symbol?.split(":")[1] ?? t.symbol}</div>
                    <div className="text-[10px] text-text-muted">{t.option_type || "—"} {t.strike ?? "—"} · {t.expiry || "—"}</div>
                  </td>
                  <td className="py-2.5 pr-3"><DirBadge direction={t.option_type} /></td>
                  <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{t.qty}</td>
                  <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(t.entry_price)}</td>
                  <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(t.exit_price)}</td>
                  <td className={clsx("py-2.5 pr-3 text-right font-mono font-semibold", pnlTone(t.pnl))}>{fmtSigned(t.pnl, 0)}</td>
                  <td className={clsx("py-2.5 pr-3 text-right font-mono", pnlTone(ret))}>{ret != null ? fmtSigned(ret, 1, "%") : "—"}</td>
                  <td className="py-2.5 pr-3 text-[11px] text-text-muted">{formatIST(t.entry_time)}</td>
                  <td className="py-2.5 text-[11px] text-text-muted">{formatIST(t.exit_time)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Statistics tab ─────────────────────────────────────────────────────────

function StatisticsTab({ summary, trades }: { summary: StrategySummary; trades: TradeRow[] }) {
  // Build a cumulative realized-P&L curve from the (chronological) trade list.
  const curve = useMemo(() => {
    const chrono = [...trades].sort((a, b) => (a.exit_time || "").localeCompare(b.exit_time || ""));
    let cum = 0;
    return chrono.map((t, i) => {
      cum += t.pnl || 0;
      return { n: i + 1, equity: cum, label: formatIST(t.exit_time) };
    });
  }, [trades]);

  const cards: Array<{ label: string; value: string; tone?: string; detail?: string }> = [
    { label: "Total trades", value: String(summary.total_trades ?? 0) },
    { label: "Win rate", value: summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(1)}%` : "—" },
    { label: "Profit factor", value: summary.profit_factor != null ? fmt(summary.profit_factor, 2) : "—" },
    { label: "Avg win", value: fmtSigned(summary.avg_win, 0), tone: pnlTone(summary.avg_win) },
    { label: "Avg loss", value: fmtSigned(summary.avg_loss, 0), tone: pnlTone(summary.avg_loss) },
    { label: "Sharpe", value: summary.sharpe_ratio != null ? fmt(summary.sharpe_ratio, 2) : "—" },
    { label: "Max drawdown", value: summary.max_drawdown != null ? `${(summary.max_drawdown * 100).toFixed(1)}%` : "—", tone: "text-accent-red" },
    { label: "Day P&L", value: fmtSigned(summary.day_pnl, 0), tone: pnlTone(summary.day_pnl), detail: `Realized today ${fmtSigned(summary.day_realized_pnl, 0)}` },
    { label: "Realized (lifetime)", value: fmtSigned(summary.realized_pnl_lifetime ?? summary.realized_pnl, 0), tone: pnlTone(summary.realized_pnl_lifetime ?? summary.realized_pnl) },
    { label: "Open P&L", value: fmtSigned(summary.unrealized_pnl, 0), tone: pnlTone(summary.unrealized_pnl) },
    { label: "Equity", value: formatINR(summary.total_equity) },
    { label: "Available cash", value: formatINR(summary.available_capital) },
  ];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
        {cards.map((c) => (
          <div key={c.label} className="rounded-xl border border-bg-border bg-bg-secondary/25 p-3">
            <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{c.label}</div>
            <div className={clsx("mt-1.5 font-mono text-base font-semibold", c.tone || "text-text-primary")}>{c.value}</div>
            {c.detail ? <div className="mt-0.5 text-[10px] text-text-muted">{c.detail}</div> : null}
          </div>
        ))}
      </div>

      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <BarChart3 size={15} className="text-accent-blue" /> Cumulative realized P&amp;L
        </div>
        {curve.length >= 2 ? (
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={curve} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.12)" />
              <XAxis dataKey="n" tick={{ fontSize: 10, fill: "#64748b" }} />
              <YAxis tick={{ fontSize: 10, fill: "#64748b" }} tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`} />
              <Tooltip
                contentStyle={{ background: "#0f1724", border: "1px solid #1e293b", borderRadius: 8, fontSize: 11 }}
                formatter={(v: number) => [formatINR(v), "Cum P&L"]}
              />
              <ReferenceLine y={0} stroke="#475569" strokeDasharray="2 2" />
              <Line type="monotone" dataKey="equity" stroke="#22c55e" strokeWidth={1.5} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <EmptyState message="Need at least two closed trades to draw the realized P&L curve." />
        )}
      </div>
    </div>
  );
}

// ── Signals tab ────────────────────────────────────────────────────────────

function SignalsTab({ rows }: { rows: BucketedRow[] }) {
  if (!rows.length) return <EmptyState message="No live signal-lane rows yet." />;
  return (
    <div className="space-y-4">
      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Shield size={15} className="text-accent-green" /> Signal buckets
        </div>
        <ThreeListView rows={rows} />
      </div>
    </div>
  );
}

// ── Activity tab — commentary + audit ──────────────────────────────────────

function ActivityTab({
  commentary,
  audit,
}: {
  commentary: Array<{ time?: string; scope?: string; tone?: string; level?: string; message?: string }>;
  audit: AuditEvent[];
}) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Activity size={15} className="text-accent-blue" /> Agent commentary
        </div>
        {commentary.length ? (
          <div className="max-h-[560px] space-y-2 overflow-y-auto">
            {commentary.slice().reverse().slice(0, 40).map((c, idx) => (
              <div key={`${c.time}-${idx}`} className="rounded-xl border border-bg-border bg-bg-secondary/20 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">{c.scope || c.tone || c.level || "info"}</span>
                  <span className="text-[10px] text-text-muted">{formatIST(c.time)}</span>
                </div>
                <div className="mt-1 text-xs leading-5 text-text-secondary">{c.message}</div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState message="No commentary in this runtime yet." />
        )}
      </div>

      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Radio size={15} className="text-accent-green" /> NSE audit feed
        </div>
        {audit.length ? <AuditFeed events={audit} /> : <EmptyState message="No audit events." />}
      </div>
    </div>
  );
}
