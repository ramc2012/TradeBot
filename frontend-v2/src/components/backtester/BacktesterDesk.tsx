"use client";

/**
 * Backtester desk — native v2 replacement for the v1 /backtester embed.
 *
 * The UI to RUN backtests for the Options-MACD strategy and tune its
 * parameters. A config form (MACD params + risk legs), three data-source
 * tabs (ICICI Breeze NSE historical, CSV upload, walk-forward), a Run
 * button with running state, and a rich results panel: KPI strip, an
 * equity-style cumulative-P&L curve, per-instrument breakdown, an
 * exit-reason distribution, and — for walk-forward — a per-window table
 * plus a stability summary.
 *
 * Backed by /api/backtester/{run-breeze,run-csv,walk-forward,default-config}.
 * The report shape (summary/aggregate/by_instrument) and walk-forward shape
 * (accepted/window_results) match the live backtester router exactly.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  BarChart3,
  CheckCircle2,
  FlaskConical,
  GitCompare,
  Layers,
  Loader2,
  Play,
  ServerCog,
  Sliders,
  Upload,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedMoney,
  tone,
} from "@/components/desk-ui";
import { CHART, pnlColor } from "@/components/strategies/shared";
import {
  getBacktesterDefaultConfig,
  getBrokerStatus,
  runBacktestBreeze,
  runWalkForward,
  uploadBacktestCsv,
} from "@/lib/api";

// ── Backend contract types ────────────────────────────────────────────────────

type DataSource = "breeze" | "csv" | "walkforward";

type StrategyConfig = {
  macd_fast: number;
  macd_slow: number;
  macd_signal: number;
  timeframe: string;
  strike_selection: string;
  option_types: string[];
  sl_pct: number;
  target_1_pct: number;
  target_2_pct: number;
  target_3_pct: number;
  time_exit_bars: number;
  capital_per_trade: number;
  max_concurrent: number;
  use_signal_cross: boolean;
  use_histogram_accel: boolean;
};

type ExitBreakdown = {
  target_1?: number;
  target_2?: number;
  target_3?: number;
  stop_loss?: number;
  time_exit?: number;
  expiry?: number;
};

type InstrumentResult = {
  option_type: string;
  total_signals: number;
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  avg_holding_bars: number;
  reward_risk_ratio: number;
  exit_breakdown: ExitBreakdown;
};

type BacktestReport = {
  error?: string;
  summary?: {
    underlying?: string;
    market?: string;
    instruments?: number;
    config?: Record<string, number>;
  };
  aggregate?: {
    total_trades?: number;
    avg_win_rate?: number;
    avg_profit_factor?: number;
    avg_sharpe?: number;
    avg_max_drawdown_pct?: number;
    total_pnl_rupees?: number;
  };
  by_instrument?: InstrumentResult[];
};

type RunEnvelope = {
  status?: string;
  report?: BacktestReport;
  result_count?: number;
  bars_loaded?: number;
};

type WindowResult = {
  window: number;
  best_params: { macd_fast: number; macd_slow: number; macd_signal: number };
  oos_win_rate: number;
  oos_profit_factor: number;
  oos_sharpe: number;
  accepted: boolean;
};

type WalkForwardResult = {
  status?: string;
  accepted?: boolean;
  accepted_windows?: number;
  total_windows?: number;
  best_params?: { macd_fast: number; macd_slow: number; macd_signal: number };
  window_results?: WindowResult[];
};

type BrokerStatus = { broker: string; connected?: boolean; state?: string };

// ── Defaults ──────────────────────────────────────────────────────────────────

const FALLBACK_CONFIG: StrategyConfig = {
  macd_fast: 12,
  macd_slow: 26,
  macd_signal: 9,
  timeframe: "5min",
  strike_selection: "ATM",
  option_types: ["CE", "PE"],
  sl_pct: 0.35,
  target_1_pct: 0.5,
  target_2_pct: 1.0,
  target_3_pct: 1.8,
  time_exit_bars: 78,
  capital_per_trade: 150_000,
  max_concurrent: 3,
  use_signal_cross: true,
  use_histogram_accel: false,
};

const TIMEFRAMES = ["3min", "5min", "15min", "30min"] as const;
const EXIT_LABELS: Record<keyof ExitBreakdown, string> = {
  target_1: "Target 1",
  target_2: "Target 2",
  target_3: "Target 3",
  stop_loss: "Stop loss",
  time_exit: "Time exit",
  expiry: "Expiry",
};
const EXIT_COLORS: Record<keyof ExitBreakdown, string> = {
  target_1: CHART.green,
  target_2: CHART.green,
  target_3: CHART.green,
  stop_loss: CHART.red,
  time_exit: CHART.amber,
  expiry: CHART.violet,
};

const AXIS = { stroke: CHART.axis, fontSize: 10, tickLine: false } as const;

// ── Small utils ───────────────────────────────────────────────────────────────

const numOr = (v: string, fallback: number): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};

function compactRs(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(1)}L`;
  if (a >= 1e3) return `${sign}₹${(a / 1e3).toFixed(0)}k`;
  return `${sign}₹${a.toFixed(0)}`;
}

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  if (detail) return detail;
  const msg = (e as { message?: string })?.message;
  return msg || fallback;
}

function TipBox({ rows }: { rows: Array<{ k: string; v: string; c?: string }> }) {
  return (
    <div
      className="rounded-lg border px-3 py-2 text-[11px] shadow-lg"
      style={{ background: CHART.surface, borderColor: CHART.border }}
    >
      {rows.map((r) => (
        <div key={r.k} className="flex justify-between gap-4">
          <span className="text-text-muted">{r.k}</span>
          <span className="font-mono" style={{ color: r.c ?? "#e6edf3" }}>
            {r.v}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Field primitives ──────────────────────────────────────────────────────────

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 font-mono text-[12.5px] text-text-primary outline-none focus:border-accent-blue/60"
      />
    </label>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[12.5px] text-text-primary outline-none focus:border-accent-blue/60"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

// ── Results: KPI strip ────────────────────────────────────────────────────────

function ResultKpis({ report }: { report: BacktestReport }) {
  const agg = report.aggregate ?? {};
  const wr = agg.avg_win_rate ?? 0;
  const pf = agg.avg_profit_factor ?? 0;
  const sharpe = agg.avg_sharpe ?? 0;
  const dd = agg.avg_max_drawdown_pct ?? 0;
  const pnl = agg.total_pnl_rupees ?? 0;
  const trades = agg.total_trades ?? 0;
  // Expectancy per trade in rupees (derived — report has no per-trade list).
  const expectancy = trades > 0 ? pnl / trades : null;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
      <MetricTile size="sm" label="Trades" value={String(trades)} detail={`${report.by_instrument?.length ?? 0} instrument(s)`} />
      <MetricTile
        size="sm"
        label="Win rate"
        value={formatPct(wr)}
        detail={wr >= 0.52 ? "above bar" : "below 52%"}
        color={wr >= 0.52 ? "text-accent-green" : "text-accent-red"}
      />
      <MetricTile
        size="sm"
        label="Profit factor"
        value={pf === Infinity ? "∞" : formatNumber(pf, 2)}
        detail={pf >= 1.3 ? "above bar" : "below 1.3"}
        color={tone(pf - 1)}
      />
      <MetricTile
        size="sm"
        label="Sharpe"
        value={formatNumber(sharpe, 2)}
        detail={sharpe >= 1 ? "above bar" : "below 1.0"}
        color={sharpe >= 1 ? "text-accent-green" : "text-accent-amber"}
      />
      <MetricTile
        size="sm"
        label="Expectancy"
        value={expectancy == null ? "—" : formatSignedMoney(expectancy)}
        detail="per trade"
        color={tone(expectancy)}
      />
      <MetricTile
        size="sm"
        label="Max DD"
        value={formatPct(dd)}
        detail={dd < 0.2 ? "contained" : "elevated"}
        color={dd < 0.2 ? "text-accent-green" : "text-accent-red"}
      />
      <MetricTile size="sm" label="Total P&L" value={formatSignedMoney(pnl)} detail={pnl >= 0 ? "net profit" : "net loss"} color={tone(pnl)} />
    </div>
  );
}

// ── Results: verdict banner ───────────────────────────────────────────────────

function Verdict({ report }: { report: BacktestReport }) {
  const agg = report.aggregate ?? {};
  const criteria = [
    { label: "Win rate > 52%", met: (agg.avg_win_rate ?? 0) > 0.52 },
    { label: "Profit factor > 1.3", met: (agg.avg_profit_factor ?? 0) > 1.3 },
    { label: "Sharpe > 1.0", met: (agg.avg_sharpe ?? 0) > 1.0 },
    { label: "Max DD < 30%", met: (agg.avg_max_drawdown_pct ?? 1) < 0.3 },
    { label: "Has trades", met: (agg.total_trades ?? 0) > 0 },
  ];
  const passed = criteria.filter((c) => c.met).length;
  const ok = passed >= 4;

  return (
    <div
      className={`flex flex-wrap items-center gap-3 rounded-2xl border px-4 py-3 ${
        ok ? "border-accent-green/40 bg-accent-green/5" : "border-accent-amber/40 bg-accent-amber/5"
      }`}
    >
      {ok ? (
        <CheckCircle2 size={20} className="shrink-0 text-accent-green" />
      ) : (
        <AlertCircle size={20} className="shrink-0 text-accent-amber" />
      )}
      <div className="min-w-0">
        <div className={`text-sm font-semibold ${ok ? "text-accent-green" : "text-accent-amber"}`}>
          {ok ? "Strategy looks viable" : "Strategy needs improvement"}
        </div>
        <div className="text-[11.5px] text-text-muted">{passed}/{criteria.length} success criteria met</div>
      </div>
      <div className="ml-auto flex flex-wrap gap-1.5">
        {criteria.map((c) => (
          <StatusBadge key={c.label} label={`${c.met ? "✓" : "✗"} ${c.label}`} variant={c.met ? "success" : "error"} />
        ))}
      </div>
    </div>
  );
}

// ── Results: equity-style cumulative-P&L curve ────────────────────────────────
// The report has no per-trade list, so we build a cumulative-P&L walk across
// the per-instrument results (CE → PE). Faithful to the data we have.

function EquityCurve({ report }: { report: BacktestReport }) {
  const series = useMemo(() => {
    // by_instrument carries no per-leg P&L, so distribute the aggregate total
    // across legs weighted by trade count, then walk the cumulative sum.
    const insts = report.by_instrument ?? [];
    const total = report.aggregate?.total_pnl_rupees ?? 0;
    const totTrades = insts.reduce((s, r) => s + (r.total_trades || 0), 0) || 1;
    let cum = 0;
    return insts.map((r, i) => {
      const share = total * ((r.total_trades || 0) / totTrades);
      cum += share;
      return { i, label: r.option_type, trades: r.total_trades, pnl: share, cumPnl: cum };
    });
  }, [report]);

  const gradOffset = useMemo(() => {
    if (!series.length) return 1;
    const max = Math.max(...series.map((d) => d.cumPnl), 0);
    const min = Math.min(...series.map((d) => d.cumPnl), 0);
    if (max <= 0) return 0;
    if (min >= 0) return 1;
    return max / (max - min);
  }, [series]);

  if (series.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-text-muted">No P&L series to plot.</div>
    );
  }

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={series} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
          <defs>
            <linearGradient id="btEqFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset={gradOffset} stopColor={CHART.green} stopOpacity={0.45} />
              <stop offset={gradOffset} stopColor={CHART.red} stopOpacity={0.32} />
            </linearGradient>
            <linearGradient id="btEqLine" x1="0" y1="0" x2="0" y2="1">
              <stop offset={gradOffset} stopColor={CHART.green} stopOpacity={1} />
              <stop offset={gradOffset} stopColor={CHART.red} stopOpacity={1} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={CHART.grid} vertical={false} />
          <XAxis dataKey="label" {...AXIS} />
          <YAxis {...AXIS} width={54} tickFormatter={(v) => compactRs(v)} />
          <ReferenceLine y={0} stroke={CHART.axis} strokeDasharray="3 3" />
          <Tooltip
            cursor={{ stroke: CHART.axis, strokeWidth: 1 }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const d = payload[0].payload as (typeof series)[number];
              return (
                <TipBox
                  rows={[
                    { k: "Leg", v: `${d.label} · ${d.trades} trades` },
                    { k: "Leg P&L", v: formatSignedMoney(d.pnl), c: pnlColor(d.pnl) },
                    { k: "Cumulative", v: formatSignedMoney(d.cumPnl), c: pnlColor(d.cumPnl) },
                  ]}
                />
              );
            }}
          />
          <Area
            type="monotone"
            dataKey="cumPnl"
            stroke="url(#btEqLine)"
            strokeWidth={2}
            fill="url(#btEqFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Results: exit-reason distribution ─────────────────────────────────────────

function ExitDistribution({ report }: { report: BacktestReport }) {
  const data = useMemo(() => {
    const acc = new Map<keyof ExitBreakdown, number>();
    for (const r of report.by_instrument ?? []) {
      for (const k of Object.keys(EXIT_LABELS) as Array<keyof ExitBreakdown>) {
        acc.set(k, (acc.get(k) ?? 0) + (r.exit_breakdown?.[k] ?? 0));
      }
    }
    return (Object.keys(EXIT_LABELS) as Array<keyof ExitBreakdown>)
      .map((k) => ({ key: k, label: EXIT_LABELS[k], count: acc.get(k) ?? 0, color: EXIT_COLORS[k] }))
      .filter((d) => d.count > 0);
  }, [report]);

  if (data.length === 0) {
    return <div className="flex h-56 items-center justify-center text-sm text-text-muted">No exits recorded.</div>;
  }

  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
          <CartesianGrid stroke={CHART.grid} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval={0} angle={-18} textAnchor="end" height={48} />
          <YAxis {...AXIS} width={28} allowDecimals={false} />
          <Tooltip
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const d = payload[0].payload as (typeof data)[number];
              return <TipBox rows={[{ k: d.label, v: `${d.count} exits`, c: d.color }]} />;
            }}
          />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.key} fill={d.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Results: per-instrument table ─────────────────────────────────────────────

const TH = "px-2.5 py-2 text-left text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold";
const TD = "px-2.5 py-2 text-[12px] text-text-secondary whitespace-nowrap";

function InstrumentTable({ report }: { report: BacktestReport }) {
  const insts = report.by_instrument ?? [];
  if (insts.length === 0) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-sm text-text-muted">
        <Layers size={15} /> No instruments in result
      </div>
    );
  }
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            <th className={TH}>Leg</th>
            <th className={`${TH} text-right`}>Signals</th>
            <th className={`${TH} text-right`}>Trades</th>
            <th className={`${TH} text-right`}>Win rate</th>
            <th className={`${TH} text-right`}>Profit factor</th>
            <th className={`${TH} text-right`}>Sharpe</th>
            <th className={`${TH} text-right`}>Max DD</th>
            <th className={`${TH} text-right`}>R:R</th>
            <th className={`${TH} text-right`}>Avg hold</th>
          </tr>
        </thead>
        <tbody>
          {insts.map((r) => (
            <tr key={r.option_type} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              <td className={TD}>
                <StatusBadge label={r.option_type} variant={r.option_type === "CE" ? "success" : "error"} />
              </td>
              <td className={`${TD} text-right font-mono`}>{r.total_signals}</td>
              <td className={`${TD} text-right font-mono`}>{r.total_trades}</td>
              <td className={`${TD} text-right font-mono ${r.win_rate >= 0.52 ? "text-accent-green" : "text-accent-red"}`}>
                {formatPct(r.win_rate)}
              </td>
              <td className={`${TD} text-right font-mono ${tone(r.profit_factor - 1)}`}>{formatNumber(r.profit_factor, 2)}</td>
              <td className={`${TD} text-right font-mono ${r.sharpe_ratio >= 1 ? "text-accent-green" : "text-accent-amber"}`}>
                {formatNumber(r.sharpe_ratio, 2)}
              </td>
              <td className={`${TD} text-right font-mono`}>{formatPct(r.max_drawdown_pct)}</td>
              <td className={`${TD} text-right font-mono`}>{formatNumber(r.reward_risk_ratio, 2)}</td>
              <td className={`${TD} text-right font-mono text-text-muted`}>{formatNumber(r.avg_holding_bars, 0)} bars</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Results panel ─────────────────────────────────────────────────────────────

function ResultsPanel({ report, source }: { report: BacktestReport; source: DataSource }) {
  if (report.error) {
    return (
      <Section title="Backtest result" icon={<BarChart3 size={16} />}>
        <div className="rounded-xl border border-accent-amber/40 bg-accent-amber/5 px-4 py-6 text-center text-sm text-accent-amber">
          {report.error}
        </div>
      </Section>
    );
  }
  const cfg = report.summary?.config ?? {};
  return (
    <div className="space-y-4">
      <Verdict report={report} />
      <Section
        title={`Results — ${report.summary?.underlying ?? "—"}`}
        icon={<BarChart3 size={16} />}
        description={`${report.summary?.market ?? "—"} · MACD ${cfg.macd_fast ?? "?"}/${cfg.macd_slow ?? "?"}/${cfg.macd_signal ?? "?"} · ${source === "csv" ? "CSV" : "Breeze NSE"} source`}
      >
        <ResultKpis report={report} />
      </Section>

      <Section title="Cumulative P&L" icon={<Activity size={16} />} description="Stepped across strategy legs (CE → PE), weighted by trade count">
        <EquityCurve report={report} />
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Per-leg breakdown" icon={<Layers size={16} />}>
          <InstrumentTable report={report} />
        </Section>
        <Section title="Exit reasons" icon={<BarChart3 size={16} />} description="Where trades closed, summed across legs">
          <ExitDistribution report={report} />
        </Section>
      </div>
    </div>
  );
}

// ── Walk-forward panel ────────────────────────────────────────────────────────

function WalkForwardPanel({ result }: { result: WalkForwardResult }) {
  const windows = result.window_results ?? [];
  const accepted = result.accepted ?? false;
  const accWin = result.accepted_windows ?? windows.filter((w) => w.accepted).length;
  const totWin = result.total_windows ?? windows.length;

  // Stability: dispersion of best params + OOS metrics across windows.
  const stability = useMemo(() => {
    if (windows.length === 0) return null;
    const mean = (xs: number[]) => xs.reduce((s, x) => s + x, 0) / xs.length;
    const std = (xs: number[]) => {
      const m = mean(xs);
      return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
    };
    const wr = windows.map((w) => w.oos_win_rate);
    const pf = windows.map((w) => w.oos_profit_factor);
    const sh = windows.map((w) => w.oos_sharpe);
    const fast = windows.map((w) => w.best_params.macd_fast);
    const slow = windows.map((w) => w.best_params.macd_slow);
    const sig = windows.map((w) => w.best_params.macd_signal);
    return {
      wrMean: mean(wr),
      wrStd: std(wr),
      pfMean: mean(pf),
      shMean: mean(sh),
      paramSpread: `${Math.min(...fast)}–${Math.max(...fast)} / ${Math.min(...slow)}–${Math.max(...slow)} / ${Math.min(...sig)}–${Math.max(...sig)}`,
      consistent: std(wr) < 0.1,
    };
  }, [windows]);

  const chartData = useMemo(
    () => windows.map((w) => ({ label: `W${w.window}`, pf: w.oos_profit_factor, accepted: w.accepted })),
    [windows],
  );

  return (
    <div className="space-y-4">
      <div
        className={`flex flex-wrap items-center gap-3 rounded-2xl border px-4 py-3 ${
          accepted ? "border-accent-green/40 bg-accent-green/5" : "border-accent-red/40 bg-accent-red/5"
        }`}
      >
        {accepted ? (
          <CheckCircle2 size={20} className="shrink-0 text-accent-green" />
        ) : (
          <AlertCircle size={20} className="shrink-0 text-accent-red" />
        )}
        <div>
          <div className={`text-sm font-semibold ${accepted ? "text-accent-green" : "text-accent-red"}`}>
            {accepted ? "Walk-forward PASSED" : "Walk-forward FAILED"}
          </div>
          <div className="text-[11.5px] text-text-muted">
            {accWin}/{totWin} windows accepted · final params{" "}
            {result.best_params
              ? `${result.best_params.macd_fast}/${result.best_params.macd_slow}/${result.best_params.macd_signal}`
              : "—"}
          </div>
        </div>
        <div className="ml-auto">
          <StatusBadge label={`${accWin}/${totWin} OOS`} variant={accepted ? "success" : "error"} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricTile size="sm" label="Windows passed" value={`${accWin}/${totWin}`} detail="OOS acceptance" color={accepted ? "text-accent-green" : "text-accent-red"} />
        <MetricTile size="sm" label="Mean OOS win" value={stability ? formatPct(stability.wrMean) : "—"} detail={stability ? `σ ${formatPct(stability.wrStd)}` : ""} />
        <MetricTile size="sm" label="Mean OOS PF" value={stability ? formatNumber(stability.pfMean, 2) : "—"} detail="profit factor" color={stability ? tone(stability.pfMean - 1) : undefined} />
        <MetricTile
          size="sm"
          label="Stability"
          value={stability ? (stability.consistent ? "Consistent" : "Variable") : "—"}
          detail={stability ? `params ${stability.paramSpread}` : ""}
          color={stability?.consistent ? "text-accent-green" : "text-accent-amber"}
        />
      </div>

      {chartData.length > 0 ? (
        <Section title="OOS profit factor by window" icon={<Activity size={16} />} description="Out-of-sample PF per rolling window — green bars clear the 1.3 bar">
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="label" {...AXIS} />
                <YAxis {...AXIS} width={36} />
                <ReferenceLine y={1.3} stroke={CHART.amber} strokeDasharray="4 4" />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0].payload as (typeof chartData)[number];
                    return (
                      <TipBox
                        rows={[
                          { k: d.label, v: `PF ${formatNumber(d.pf, 2)}`, c: d.accepted ? CHART.green : CHART.red },
                          { k: "OOS", v: d.accepted ? "accepted" : "rejected" },
                        ]}
                      />
                    );
                  }}
                />
                <Bar dataKey="pf" radius={[3, 3, 0, 0]} isAnimationActive={false}>
                  {chartData.map((d) => (
                    <Cell key={d.label} fill={d.accepted ? CHART.green : CHART.red} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      ) : null}

      <Section title="Per-window detail" icon={<GitCompare size={16} />}>
        {windows.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-sm text-text-muted">
            No windows produced — data span may be too short for {totWin || 5} rolling splits.
          </div>
        ) : (
          <div className="-mx-2 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-bg-border/60">
                  <th className={TH}>Window</th>
                  <th className={`${TH} text-right`}>Best params (F/S/Sig)</th>
                  <th className={`${TH} text-right`}>OOS win</th>
                  <th className={`${TH} text-right`}>OOS PF</th>
                  <th className={`${TH} text-right`}>OOS Sharpe</th>
                  <th className={`${TH} text-right`}>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {windows.map((w) => (
                  <tr key={w.window} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                    <td className={`${TD} font-medium text-text-primary`}>Window {w.window}</td>
                    <td className={`${TD} text-right font-mono`}>
                      {w.best_params.macd_fast}/{w.best_params.macd_slow}/{w.best_params.macd_signal}
                    </td>
                    <td className={`${TD} text-right font-mono ${w.oos_win_rate > 0.5 ? "text-accent-green" : "text-accent-red"}`}>
                      {formatPct(w.oos_win_rate)}
                    </td>
                    <td className={`${TD} text-right font-mono ${tone(w.oos_profit_factor - 1)}`}>{formatNumber(w.oos_profit_factor, 2)}</td>
                    <td className={`${TD} text-right font-mono ${w.oos_sharpe > 1 ? "text-accent-green" : "text-accent-amber"}`}>
                      {formatNumber(w.oos_sharpe, 2)}
                    </td>
                    <td className={`${TD} text-right`}>
                      <StatusBadge label={w.accepted ? "PASS" : "FAIL"} variant={w.accepted ? "success" : "error"} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

// ── Main desk ─────────────────────────────────────────────────────────────────

export default function BacktesterDesk() {
  const [source, setSource] = useState<DataSource>("breeze");

  // Strategy / MACD config (string-backed inputs).
  const [macdFast, setMacdFast] = useState("12");
  const [macdSlow, setMacdSlow] = useState("26");
  const [macdSignal, setMacdSignal] = useState("9");
  const [timeframe, setTimeframe] = useState("5min");
  const [slPct, setSlPct] = useState("35");
  const [t1Pct, setT1Pct] = useState("50");
  const [t3Pct, setT3Pct] = useState("180");
  const [useSignalCross, setUseSignalCross] = useState(true);

  // Breeze form.
  const [stockCode, setStockCode] = useState("NIFTY");
  const [strikePrice, setStrikePrice] = useState("24000");
  const [right, setRight] = useState("call");
  const [expiry, setExpiry] = useState("2024-12-26T07:00:00.000Z");
  const [fromDate, setFromDate] = useState("2024-11-01T07:00:00.000Z");
  const [toDate, setToDate] = useState("2024-12-26T07:00:00.000Z");

  // CSV form.
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvUnderlying, setCsvUnderlying] = useState("SPY");
  const [csvMarket, setCsvMarket] = useState("US");

  // Walk-forward form.
  const [wfWindows, setWfWindows] = useState("5");
  const [wfTrainPct, setWfTrainPct] = useState("70");

  // Default config (informational baseline).
  useQuery({
    queryKey: ["backtester", "default-config"],
    queryFn: () => getBacktesterDefaultConfig().then((r) => r.data as StrategyConfig),
    staleTime: REFRESH_MS.slow,
  });

  const brokerQ = useQuery({
    queryKey: ["broker", "status"],
    queryFn: () => getBrokerStatus().then((r) => r.data as BrokerStatus[]),
    staleTime: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });
  const breezeConnected = (brokerQ.data ?? []).find((b) => b.broker === "icici_breeze")?.connected ?? false;

  const buildConfig = (): StrategyConfig => ({
    ...FALLBACK_CONFIG,
    macd_fast: numOr(macdFast, 12),
    macd_slow: numOr(macdSlow, 26),
    macd_signal: numOr(macdSignal, 9),
    timeframe,
    sl_pct: numOr(slPct, 35) / 100,
    target_1_pct: numOr(t1Pct, 50) / 100,
    target_2_pct: 1.0,
    target_3_pct: numOr(t3Pct, 180) / 100,
    use_signal_cross: useSignalCross,
  });

  const [report, setReport] = useState<BacktestReport | null>(null);
  const [wfResult, setWfResult] = useState<WalkForwardResult | null>(null);
  const [resultSource, setResultSource] = useState<DataSource>("breeze");

  const breezeMut = useMutation({
    mutationFn: async () => {
      const res = await runBacktestBreeze({
        stock_code: stockCode,
        expiry_date: expiry,
        right,
        strike_price: strikePrice,
        from_date: fromDate,
        to_date: toDate,
        interval: timeframe.replace("min", "minute"),
        config: buildConfig(),
      });
      return res.data as RunEnvelope;
    },
    onSuccess: (data) => {
      setReport(data.report ?? null);
      setWfResult(null);
      setResultSource("breeze");
    },
  });

  const csvMut = useMutation({
    mutationFn: async () => {
      if (!csvFile) throw new Error("Select a CSV file first.");
      const fd = new FormData();
      fd.append("file", csvFile);
      fd.append("underlying", csvUnderlying);
      fd.append("market", csvMarket);
      fd.append("config_json", JSON.stringify(buildConfig()));
      const res = await uploadBacktestCsv(fd);
      return res.data as RunEnvelope;
    },
    onSuccess: (data) => {
      setReport(data.report ?? null);
      setWfResult(null);
      setResultSource("csv");
    },
  });

  const wfMut = useMutation({
    mutationFn: async () => {
      const res = await runWalkForward({
        config: buildConfig(),
        train_pct: numOr(wfTrainPct, 70) / 100,
        n_windows: numOr(wfWindows, 5),
        data: null,
        underlying: stockCode,
        market: "NSE",
      });
      return res.data as WalkForwardResult;
    },
    onSuccess: (data) => {
      setWfResult(data);
      setReport(null);
      setResultSource("walkforward");
    },
  });

  const running = breezeMut.isPending || csvMut.isPending || wfMut.isPending;
  const activeError =
    source === "breeze"
      ? breezeMut.isError
        ? errText(breezeMut.error, "Breeze backtest failed.")
        : null
      : source === "csv"
        ? csvMut.isError
          ? errText(csvMut.error, "CSV backtest failed.")
          : null
        : wfMut.isError
          ? errText(wfMut.error, "Walk-forward failed.")
          : null;

  const sourceTabs: Array<{ key: DataSource; label: string; icon: typeof Play }> = [
    { key: "breeze", label: "ICICI Breeze (NSE)", icon: ServerCog },
    { key: "csv", label: "CSV upload", icon: Upload },
    { key: "walkforward", label: "Walk-forward", icon: GitCompare },
  ];

  const runActive = () => {
    if (source === "breeze") breezeMut.mutate();
    else if (source === "csv") csvMut.mutate();
    else wfMut.mutate();
  };
  const runDisabled =
    running || (source === "breeze" && !breezeConnected) || (source === "csv" && !csvFile);

  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <FlaskConical size={18} className="text-accent-blue" />
              <h1 className="text-xl font-semibold text-text-primary">Options MACD Backtester</h1>
            </div>
            <p className="mt-1 text-sm text-text-muted">
              MACD on ATM option premium · zero/signal-line cross. Tune parameters, run against NSE
              history, US CSV data, or walk-forward validation.
            </p>
          </div>
          <StatusBadge
            label={breezeConnected ? "Breeze live" : "Breeze offline"}
            variant={breezeConnected ? "success" : "neutral"}
          />
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* ── Config column ── */}
        <div className="space-y-4">
          <Section title="MACD parameters" icon={<Sliders size={16} />}>
            <div className="grid grid-cols-3 gap-2">
              <Field label="Fast" value={macdFast} onChange={setMacdFast} type="number" />
              <Field label="Slow" value={macdSlow} onChange={setMacdSlow} type="number" />
              <Field label="Signal" value={macdSignal} onChange={setMacdSignal} type="number" />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <SelectField
                label="Timeframe"
                value={timeframe}
                onChange={setTimeframe}
                options={TIMEFRAMES.map((t) => ({ value: t, label: t }))}
              />
              <SelectField
                label="Signal mode"
                value={useSignalCross ? "signal" : "zero"}
                onChange={(v) => setUseSignalCross(v === "signal")}
                options={[
                  { value: "signal", label: "Signal cross" },
                  { value: "zero", label: "Zero cross" },
                ]}
              />
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2">
              <Field label="SL %" value={slPct} onChange={setSlPct} type="number" />
              <Field label="T1 %" value={t1Pct} onChange={setT1Pct} type="number" />
              <Field label="T3 %" value={t3Pct} onChange={setT3Pct} type="number" />
            </div>
          </Section>

          <Section title="Strategy logic" icon={<FlaskConical size={16} />}>
            <ul className="space-y-1.5 text-[12px] text-text-muted">
              <li>
                MACD computed on the <span className="text-text-primary">option premium</span>, not the underlying.
              </li>
              <li>Buy CE on cross-up, buy PE on cross-down; ATM strike selection.</li>
              <li>
                Risk legs: SL <span className="text-text-primary">{slPct}%</span>, targets{" "}
                <span className="text-text-primary">{t1Pct}% / 100% / {t3Pct}%</span>.
              </li>
              <li>Time exit ≈ 2 sessions (78 × 5min bars).</li>
            </ul>
          </Section>
        </div>

        {/* ── Run column ── */}
        <div className="space-y-4 lg:col-span-2">
          <Section title="Data source" icon={<ServerCog size={16} />} padded={false}>
            <div className="border-b border-bg-border/50 px-5 pt-4">
              <div className="flex flex-wrap gap-1">
                {sourceTabs.map(({ key, label, icon: Icon }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setSource(key)}
                    className={`inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12px] font-semibold transition-colors ${
                      source === key
                        ? "border-accent-blue text-text-primary"
                        : "border-transparent text-text-muted hover:text-text-secondary"
                    }`}
                  >
                    <Icon size={14} />
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-3 p-5">
              {source === "breeze" ? (
                <>
                  {!breezeConnected ? (
                    <div className="flex items-center gap-2 rounded-lg border border-accent-amber/30 bg-accent-amber/5 px-3 py-2 text-[12px] text-accent-amber">
                      <AlertCircle size={14} className="shrink-0" />
                      <span>
                        ICICI Breeze not connected. Connect in{" "}
                        <a href="/settings" className="underline">
                          Settings
                        </a>{" "}
                        for 3 years of NSE historical F&O data.
                      </span>
                    </div>
                  ) : null}
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Underlying" value={stockCode} onChange={setStockCode} placeholder="NIFTY" />
                    <Field label="Strike" value={strikePrice} onChange={setStrikePrice} placeholder="24000" />
                    <SelectField
                      label="Option type"
                      value={right}
                      onChange={setRight}
                      options={[
                        { value: "call", label: "Call (CE)" },
                        { value: "put", label: "Put (PE)" },
                      ]}
                    />
                    <Field label="Expiry (ISO)" value={expiry} onChange={setExpiry} />
                    <Field label="From (ISO)" value={fromDate} onChange={setFromDate} />
                    <Field label="To (ISO)" value={toDate} onChange={setToDate} />
                  </div>
                </>
              ) : null}

              {source === "csv" ? (
                <>
                  <p className="text-[12px] text-text-muted">
                    Upload an{" "}
                    <a
                      href="https://www.optionsdx.com"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-accent-blue hover:underline"
                    >
                      OptionsDX
                    </a>{" "}
                    or custom CSV with columns: timestamp, expiry, strike, option_type, open, high,
                    low, close, volume.
                  </p>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Underlying name" value={csvUnderlying} onChange={setCsvUnderlying} placeholder="SPY" />
                    <SelectField
                      label="Market"
                      value={csvMarket}
                      onChange={setCsvMarket}
                      options={[
                        { value: "US", label: "US" },
                        { value: "NSE", label: "NSE" },
                      ]}
                    />
                  </div>
                  <label
                    className={`flex cursor-pointer flex-col items-center gap-2 rounded-xl border-2 border-dashed px-4 py-6 transition-colors ${
                      csvFile
                        ? "border-accent-green/40 bg-accent-green/5"
                        : "border-bg-border hover:border-accent-blue/40"
                    }`}
                  >
                    <Upload size={20} className={csvFile ? "text-accent-green" : "text-text-muted"} />
                    <span className="text-[12.5px] text-text-secondary">
                      {csvFile ? csvFile.name : "Drop CSV here or click to upload"}
                    </span>
                    <input
                      type="file"
                      accept=".csv"
                      className="hidden"
                      onChange={(e) => setCsvFile(e.target.files?.[0] ?? null)}
                    />
                  </label>
                </>
              ) : null}

              {source === "walkforward" ? (
                <>
                  <p className="text-[12px] text-text-muted">
                    Rolling train/test optimisation across N windows — grid-searches MACD params on
                    each in-sample slice, validates out-of-sample. Guards against curve-fitting. Runs
                    on the loaded NSE underlying.
                  </p>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Windows" value={wfWindows} onChange={setWfWindows} type="number" />
                    <Field label="Train %" value={wfTrainPct} onChange={setWfTrainPct} type="number" />
                  </div>
                  <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-[11.5px] text-text-muted">
                    <div className="mb-1 font-semibold text-text-secondary">Acceptance criteria (per window)</div>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono">
                      <span>OOS win rate &gt; 50%</span>
                      <span>OOS profit factor &gt; 1.3</span>
                      <span>OOS Sharpe &gt; 1.0</span>
                      <span>Pass in {Math.max(1, numOr(wfWindows, 5) - 1)} of {numOr(wfWindows, 5)}</span>
                    </div>
                  </div>
                </>
              ) : null}

              <button
                type="button"
                onClick={runActive}
                disabled={runDisabled}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-accent-blue/40 bg-accent-blue/15 py-2.5 text-[13px] font-semibold text-accent-blue transition-colors hover:bg-accent-blue/25 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {running ? (
                  <>
                    <Loader2 size={15} className="animate-spin" />
                    {source === "walkforward" ? "Optimising…" : source === "csv" ? "Processing CSV…" : "Fetching & running…"}
                  </>
                ) : (
                  <>
                    <Play size={15} />
                    {source === "walkforward" ? "Run walk-forward" : "Run backtest"}
                  </>
                )}
              </button>

              {activeError ? (
                <div className="flex items-start gap-2 rounded-lg border border-accent-red/30 bg-accent-red/5 px-3 py-2 text-[12px] text-accent-red">
                  <AlertCircle size={14} className="mt-0.5 shrink-0" />
                  <span>{activeError}</span>
                </div>
              ) : null}
            </div>
          </Section>

          {/* Results inline on wide layouts */}
          {wfResult && resultSource === "walkforward" ? <WalkForwardPanel result={wfResult} /> : null}
          {report && resultSource !== "walkforward" ? <ResultsPanel report={report} source={resultSource} /> : null}

          {!report && !wfResult ? (
            <div className="rounded-2xl border border-dashed border-bg-border/60 px-4 py-12 text-center text-sm text-text-muted">
              Configure the strategy, pick a data source, and run a backtest. Results — KPI strip,
              cumulative P&L, per-leg breakdown and exit distribution — render here.
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
