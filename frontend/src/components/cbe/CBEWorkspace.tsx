"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Briefcase,
  CheckCircle2,
  Coins,
  History,
  Layers,
  ListChecks,
  Play,
  Radar,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Target,
  TrendingUp,
  Wallet,
  type LucideIcon,
} from "lucide-react";

import {
  describeApiError,
  getCBELatestScan,
  getCBEPaperJournal,
  getCBEPaperPositions,
  getCBEPaperSummary,
  resetCBEPaper,
  runCBEScan,
} from "@/lib/api";

// ── Types ─────────────────────────────────────────────────────────────────
type Bias = "bullish" | "bearish" | "neutral" | string;

type CompositeComponents = {
  asset?: number;
  sector?: number;
  stock?: number;
  macd?: number;
  rsi?: number;
};

type MacdMeta = {
  label?: string;
  line?: number;
  signal?: number;
  cross_today?: boolean;
};

type RsiMeta = {
  label?: string;
  rsi?: number;
};

type AlphaRow = {
  instrument: string;
  composite_score: number;          // legacy 0-10 alias
  composite_alpha_score: number;    // 0-100
  gate_passed: boolean;
  directional_bias: Bias;
  bias_conviction: number;
  sector_code?: string;
  sector_quadrant?: string;
  sector_rs_pct?: number;
  stock_quadrant?: string;
  stock_rs_pct?: number;
  stock_rank_in_sector?: number;
  // v3 indicators (MACD + RSI + RRG + weekly)
  macd_line?: number;
  macd_signal?: number;
  macd_hist?: number;
  macd_bullish?: boolean;
  macd_score?: number;
  macd_meta?: MacdMeta;
  rsi_14?: number;
  rsi_score?: number;
  rsi_meta?: RsiMeta;
  weekly_close_vs_ema20?: number;
  weekly_trend?: "up" | "down" | "flat" | "unknown" | string;
  latest_close?: number;
  recent_closes_30d?: number[];
  composite_components?: CompositeComponents;
};

type SectorWinner = {
  code: string;
  name: string;
  rs_pct: number;
  quadrant: string;
};

type AssetLayer = {
  winner?: string;
  stub?: boolean;
  stub_reason?: string;
  score_for_engine?: number;
  asset_rank?: Array<Record<string, unknown>>;
};

type AlphaPayload = {
  source?: string;
  scan_date?: string;
  asset_winner?: string;
  asset_layer?: AssetLayer;
  sector_layer?: {
    timeframe?: string;
    winners?: SectorWinner[];
    ranked_sectors?: SectorWinner[];
  };
  fno_universe_size?: number;
  scored_count?: number;
  watchlist_count?: number;
  elapsed_seconds?: number;
  results?: AlphaRow[];
  watchlist?: AlphaRow[];
  paper_summary?: PaperSummary;
};

type PaperSummary = {
  open_positions: number;
  closed_positions: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  initial_capital: number;
  available_capital: number;
  reserved_margin: number;
  total_equity: number;
  total_return_pct: number;
  max_drawdown: number;
  sharpe_ratio: number;
  total_trades: number;
  win_rate: number;
};

type PaperPosition = {
  position_id: string;
  instrument: string;
  status: "open" | "closed";
  direction: "long" | "short";
  bias?: string;
  opened_at: string;
  closed_at?: string | null;
  entry_price: number;
  latest_close?: number;
  exit_price?: number | null;
  quantity: number;
  notional: number;
  unrealized_pnl: number;
  realized_pnl: number;
  composite_score?: number;
  latest_composite_score?: number;
  close_reason?: string | null;
};

type PaperPositionsResponse = {
  status: string;
  summary: PaperSummary;
  open_positions: PaperPosition[];
  closed_positions: PaperPosition[];
  last_synced_at?: string | null;
};

type JournalRecord = {
  recorded_at: string;
  scan_date?: string;
  instrument: string;
  event: "open" | "close";
  direction?: string;
  bias?: string;
  composite_score?: number;
  entry_price?: number;
  exit_price?: number;
  quantity?: number;
  notional?: number;
  realized_pnl?: number;
  close_reason?: string;
};

type DeskTab = "ranked" | "open" | "history" | "journal";

// ── Formatting helpers ────────────────────────────────────────────────────
function fmtNum(value: unknown, digits = 2): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return numeric.toFixed(digits);
}
function fmtPct(value: unknown, digits = 2): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric.toFixed(digits)}%`;
}
function fmtRupee(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(numeric)}`;
}
function fmtCompact(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0, notation: "compact" }).format(numeric);
}
function fmtDate(value: unknown): string {
  if (!value) return "—";
  try {
    const d = new Date(String(value));
    if (Number.isNaN(d.getTime())) return String(value).slice(0, 16);
    return d.toISOString().replace("T", " ").slice(0, 16);
  } catch {
    return String(value).slice(0, 16);
  }
}

function toneForBias(bias: Bias): string {
  if (bias === "bullish") return "border-accent-green/35 bg-accent-green/10 text-accent-green";
  if (bias === "bearish") return "border-accent-red/35 bg-accent-red/10 text-accent-red";
  return "border-bg-border bg-bg-secondary/35 text-text-secondary";
}

function toneForQuadrant(q?: string): string {
  switch ((q || "").toLowerCase()) {
    case "leading":
      return "text-accent-green";
    case "improving":
      return "text-accent-blue";
    case "weakening":
      return "text-accent-amber";
    case "lagging":
      return "text-accent-red";
    default:
      return "text-text-muted";
  }
}

function toneForGate(passed: boolean, score: number): string {
  if (passed) return "text-accent-green";
  if (score >= 70) return "text-accent-amber";
  return "text-text-secondary";
}

function tonePnL(value: number | undefined): string {
  const n = Number(value || 0);
  if (n > 0) return "text-accent-green";
  if (n < 0) return "text-accent-red";
  return "text-text-secondary";
}

// ── Reusable bits ─────────────────────────────────────────────────────────
function Metric({
  label,
  value,
  detail,
  icon: Icon,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  icon: LucideIcon;
  tone?: string;
}) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/24 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">{label}</div>
        <Icon size={16} className="text-accent-blue" />
      </div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold", tone || "text-text-primary")}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex h-[180px] items-center justify-center rounded-lg border border-bg-border bg-bg-primary/24 text-sm text-text-muted">
      {text}
    </div>
  );
}

// ── L1 + L2 layer panels ──────────────────────────────────────────────────
function AssetLayerPanel({ layer }: { layer?: AssetLayer }) {
  if (!layer) {
    return (
      <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
        <EmptyState text="Run an alpha scan to populate the asset layer." />
      </section>
    );
  }
  return (
    <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <Coins size={15} className="text-accent-amber" />
        L1 · Asset rotation
        {layer.stub ? (
          <span className="rounded border border-accent-amber/30 bg-accent-amber/10 px-2 py-0.5 text-[10px] uppercase text-accent-amber">
            stub
          </span>
        ) : (
          <span className="rounded border border-accent-green/30 bg-accent-green/10 px-2 py-0.5 text-[10px] uppercase text-accent-green">
            live
          </span>
        )}
      </div>
      <div className="space-y-1 text-xs text-text-secondary">
        <div>
          Winner:{" "}
          <span className="font-mono font-semibold text-text-primary">{layer.winner || "—"}</span>
          {layer.score_for_engine !== undefined ? (
            <span className="ml-3 text-text-muted">
              equities score: <span className="font-mono text-text-primary">{fmtNum(layer.score_for_engine, 1)}</span>
            </span>
          ) : null}
        </div>
        {layer.stub_reason ? (
          <div className="text-[11px] text-text-muted">↳ {layer.stub_reason}</div>
        ) : null}
        {layer.asset_rank?.length ? (
          <div className="mt-2 overflow-x-auto">
            <table className="min-w-full text-[11px]">
              <thead className="text-text-muted">
                <tr>
                  <th className="px-2 py-1 text-left">Asset</th>
                  <th className="px-2 py-1 text-right">3m</th>
                  <th className="px-2 py-1 text-right">6m</th>
                  <th className="px-2 py-1 text-right">12m</th>
                  <th className="px-2 py-1 text-right">Score</th>
                </tr>
              </thead>
              <tbody>
                {layer.asset_rank.map((row, idx) => (
                  <tr key={String(row.asset ?? idx)} className="border-t border-bg-border/40">
                    <td className="px-2 py-1 font-semibold text-text-primary">{String(row.asset ?? "—")}</td>
                    <td className="px-2 py-1 text-right font-mono text-text-secondary">{fmtNum(row.momentum_3m, 1)}</td>
                    <td className="px-2 py-1 text-right font-mono text-text-secondary">{fmtNum(row.momentum_6m, 1)}</td>
                    <td className="px-2 py-1 text-right font-mono text-text-secondary">{fmtNum(row.momentum_12m, 1)}</td>
                    <td className="px-2 py-1 text-right font-mono text-text-primary">{fmtNum(row.score, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function SectorLayerPanel({ winners, ranked }: { winners?: SectorWinner[]; ranked?: SectorWinner[] }) {
  const top = (winners && winners.length ? winners : ranked) || [];
  return (
    <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <Layers size={15} className="text-accent-blue" />
        L2 · Sector winners vs Nifty50
      </div>
      {top.length ? (
        <div className="space-y-1.5">
          {top.map((sector) => (
            <div
              key={sector.code}
              className="flex items-center justify-between rounded-md border border-bg-border bg-bg-primary/24 px-3 py-2"
            >
              <div>
                <div className="text-sm font-semibold text-text-primary">{sector.name}</div>
                <div className={clsx("text-[11px] uppercase tracking-wide", toneForQuadrant(sector.quadrant))}>
                  {sector.quadrant}
                </div>
              </div>
              <div className="font-mono text-sm text-text-primary">
                {sector.rs_pct > 0 ? "+" : ""}
                {fmtNum(sector.rs_pct, 2)}%
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState text="No sector winners yet." />
      )}
    </section>
  );
}

// ── Ranked alpha candidates ───────────────────────────────────────────────
// 30-day EOD sparkline. Pure SVG, no recharts — keeps render cheap when
// the table has 200+ rows. Green line if up over the window, red if down.
function Sparkline({
  data,
  width = 96,
  height = 28,
}: {
  data?: number[];
  width?: number;
  height?: number;
}) {
  if (!data || data.length < 2) {
    return <span className="text-[10px] text-text-muted">—</span>;
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = width / (data.length - 1);
  const points = data
    .map((v, i) => `${(i * step).toFixed(2)},${(height - ((v - min) / range) * height).toFixed(2)}`)
    .join(" ");
  const up = data[data.length - 1] >= data[0];
  const stroke = up ? "#34d399" : "#f87171";
  const fill = up ? "rgba(52,211,153,0.12)" : "rgba(248,113,113,0.12)";
  // Area path so the fill closes at bottom — gives the chart visual weight.
  const areaPath =
    `M 0,${height} L ${points.split(" ").join(" L ")} L ${width},${height} Z`;
  const lastVal = data[data.length - 1];
  return (
    <svg width={width} height={height} className="block" aria-label={`30-day sparkline last=${lastVal}`}>
      <path d={areaPath} fill={fill} stroke="none" />
      <polyline points={points} fill="none" stroke={stroke} strokeWidth={1.4} />
    </svg>
  );
}

function macdCellTone(score: number | undefined, cross_today: boolean | undefined) {
  if (score === undefined) return "text-text-muted";
  if (cross_today && score >= 80) return "text-accent-green font-semibold";
  if (score >= 75) return "text-accent-green";
  if (score >= 55) return "text-text-primary";
  if (score >= 40) return "text-accent-amber";
  return "text-accent-red";
}

function rsiCellTone(rsi: number | undefined) {
  if (rsi === undefined || rsi === null) return "text-text-muted";
  if (rsi >= 70) return "text-accent-red";       // overbought
  if (rsi >= 50) return "text-accent-green";     // healthy uptrend
  if (rsi >= 40) return "text-text-primary";     // neutral
  if (rsi >= 30) return "text-accent-amber";     // weakening
  return "text-accent-red";                       // oversold
}

function weeklyChipTone(trend: string | undefined) {
  switch ((trend || "").toLowerCase()) {
    case "up":
      return "border-accent-green/40 bg-accent-green/10 text-accent-green";
    case "down":
      return "border-accent-red/40 bg-accent-red/10 text-accent-red";
    case "flat":
      return "border-bg-border bg-bg-secondary/40 text-text-secondary";
    default:
      return "border-bg-border bg-bg-primary/30 text-text-muted";
  }
}

function RankedScanTable({ rows }: { rows: AlphaRow[] }) {
  if (!rows.length) {
    return <EmptyState text="Run an alpha scan to populate the ranked candidates." />;
  }
  return (
    <div className="overflow-hidden rounded-lg border border-bg-border">
      <div className="max-h-[620px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-sm">
          <thead className="sticky top-0 z-10 bg-bg-card text-[11px] uppercase tracking-[0.12em] text-text-muted">
            <tr>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">#</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Symbol</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Sector</th>
              <th className="border-b border-bg-border px-3 py-2 text-center font-semibold" title="Stock RRG quadrant">RRG</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Bias</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Alpha</th>
              <th className="border-b border-bg-border px-3 py-2 text-center font-semibold">Gate</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold" title="Sector RS vs Nifty50">Sec RS%</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold" title="Stock RS">Stk RS%</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold" title="Daily MACD signal score">MACD</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold" title="14-period RSI">RSI</th>
              <th className="border-b border-bg-border px-3 py-2 text-center font-semibold" title="20-week EMA trend filter">Weekly</th>
              <th className="border-b border-bg-border px-3 py-2 text-center font-semibold" title="Last 30 EOD closes">30d EOD</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const macd = row.macd_meta || {};
              const rsi = row.rsi_14;
              return (
                <tr key={row.instrument} className="border-b border-bg-border/70 bg-bg-secondary/10 hover:bg-bg-hover/60">
                  <td className="border-b border-bg-border/60 px-3 py-2 font-mono text-xs text-text-muted">{index + 1}</td>
                  <td className="border-b border-bg-border/60 px-3 py-2 font-semibold text-text-primary">{row.instrument}</td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-xs text-text-secondary">{row.sector_code || "—"}</td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-center">
                    <span className={clsx("text-[10px] uppercase", toneForQuadrant(row.stock_quadrant))}>
                      {row.stock_quadrant ? row.stock_quadrant.slice(0, 4) : "—"}
                    </span>
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2">
                    <span
                      className={clsx(
                        "rounded-md border px-2 py-1 text-[10px] uppercase",
                        toneForBias(row.directional_bias),
                      )}
                    >
                      {row.directional_bias}
                    </span>
                  </td>
                  <td
                    className={clsx(
                      "border-b border-bg-border/60 px-3 py-2 text-right font-mono font-semibold",
                      toneForGate(row.gate_passed, row.composite_alpha_score),
                    )}
                  >
                    {fmtNum(row.composite_alpha_score, 1)}
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-center">
                    {row.gate_passed ? (
                      <CheckCircle2 size={14} className="mx-auto text-accent-green" />
                    ) : (
                      <span className="text-[10px] text-text-muted">·</span>
                    )}
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs text-text-secondary">
                    {fmtNum(row.sector_rs_pct, 2)}
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs text-text-secondary">
                    {fmtNum(row.stock_rs_pct, 2)}
                  </td>
                  <td
                    className={clsx(
                      "border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs",
                      macdCellTone(row.macd_score, macd.cross_today),
                    )}
                    title={`${macd.label || ""}${macd.cross_today ? " · CROSS today" : ""} · line=${fmtNum(macd.line, 2)} sig=${fmtNum(macd.signal, 2)}`}
                  >
                    {fmtNum(row.macd_score, 1)}
                    {macd.cross_today ? <span className="ml-1 text-accent-blue">✦</span> : null}
                  </td>
                  <td
                    className={clsx(
                      "border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs",
                      rsiCellTone(rsi),
                    )}
                    title={`${row.rsi_meta?.label || ""} (score ${fmtNum(row.rsi_score, 1)})`}
                  >
                    {rsi === undefined || rsi === null ? "—" : fmtNum(rsi, 1)}
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-center">
                    <span className={clsx("inline-block rounded border px-1.5 py-0.5 text-[10px] uppercase", weeklyChipTone(row.weekly_trend))}>
                      {row.weekly_trend || "—"}
                    </span>
                  </td>
                  <td className="border-b border-bg-border/60 px-2 py-1 text-center">
                    <Sparkline data={row.recent_closes_30d} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Open Portfolio ────────────────────────────────────────────────────────
function OpenPortfolioTable({ positions }: { positions: PaperPosition[] }) {
  if (!positions.length) {
    return <EmptyState text="No open positions. Paper book is flat." />;
  }
  return (
    <div className="overflow-hidden rounded-lg border border-bg-border">
      <div className="max-h-[480px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-sm">
          <thead className="sticky top-0 z-10 bg-bg-card text-[11px] uppercase tracking-[0.12em] text-text-muted">
            <tr>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Opened</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Symbol</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Side</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Qty</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Entry</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Mark</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Notional</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Unrealized P&L</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Alpha</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.position_id} className="border-b border-bg-border/70 bg-bg-secondary/10">
                <td className="border-b border-bg-border/60 px-3 py-2 font-mono text-[11px] text-text-muted">
                  {fmtDate(p.opened_at)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 font-semibold text-text-primary">{p.instrument}</td>
                <td className="border-b border-bg-border/60 px-3 py-2">
                  <span
                    className={clsx(
                      "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] uppercase",
                      p.direction === "long"
                        ? "border-accent-green/35 bg-accent-green/10 text-accent-green"
                        : "border-accent-red/35 bg-accent-red/10 text-accent-red",
                    )}
                  >
                    {p.direction === "long" ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
                    {p.direction}
                  </span>
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">{p.quantity}</td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">
                  {fmtNum(p.entry_price, 2)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">
                  {fmtNum(p.latest_close, 2)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">
                  {fmtRupee(p.notional)}
                </td>
                <td
                  className={clsx(
                    "border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs font-semibold",
                    tonePnL(p.unrealized_pnl),
                  )}
                >
                  {p.unrealized_pnl >= 0 ? "+" : ""}
                  {fmtRupee(p.unrealized_pnl)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs text-text-secondary">
                  {fmtNum(p.latest_composite_score ?? p.composite_score, 1)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Historical Trades ─────────────────────────────────────────────────────
function HistoricalTradesTable({ positions }: { positions: PaperPosition[] }) {
  if (!positions.length) {
    return <EmptyState text="No closed trades yet. History will appear here after the first close." />;
  }
  return (
    <div className="overflow-hidden rounded-lg border border-bg-border">
      <div className="max-h-[480px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-sm">
          <thead className="sticky top-0 z-10 bg-bg-card text-[11px] uppercase tracking-[0.12em] text-text-muted">
            <tr>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Closed</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Symbol</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Side</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Qty</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Entry</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Exit</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Realized P&L</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Reason</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.position_id} className="border-b border-bg-border/70 bg-bg-secondary/10">
                <td className="border-b border-bg-border/60 px-3 py-2 font-mono text-[11px] text-text-muted">
                  {fmtDate(p.closed_at)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 font-semibold text-text-primary">{p.instrument}</td>
                <td className="border-b border-bg-border/60 px-3 py-2">
                  <span
                    className={clsx(
                      "rounded-md border px-2 py-1 text-[10px] uppercase",
                      p.direction === "long"
                        ? "border-accent-green/35 bg-accent-green/10 text-accent-green"
                        : "border-accent-red/35 bg-accent-red/10 text-accent-red",
                    )}
                  >
                    {p.direction}
                  </span>
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">{p.quantity}</td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">
                  {fmtNum(p.entry_price, 2)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs">
                  {fmtNum(p.exit_price, 2)}
                </td>
                <td
                  className={clsx(
                    "border-b border-bg-border/60 px-3 py-2 text-right font-mono text-xs font-semibold",
                    tonePnL(p.realized_pnl),
                  )}
                >
                  {p.realized_pnl >= 0 ? "+" : ""}
                  {fmtRupee(p.realized_pnl)}
                </td>
                <td className="border-b border-bg-border/60 px-3 py-2 text-xs text-text-secondary">
                  {p.close_reason || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Journal ───────────────────────────────────────────────────────────────
function JournalList({ records }: { records: JournalRecord[] }) {
  if (!records.length) {
    return <EmptyState text="Journal is empty. Open/close events will land here as they happen." />;
  }
  return (
    <div className="max-h-[480px] space-y-2 overflow-auto pr-1">
      {records.map((r, idx) => (
        <div
          key={`${r.recorded_at}-${idx}`}
          className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-2 text-xs"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span
                className={clsx(
                  "rounded-md border px-2 py-0.5 text-[10px] uppercase",
                  r.event === "open"
                    ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                    : "border-accent-amber/40 bg-accent-amber/10 text-accent-amber",
                )}
              >
                {r.event}
              </span>
              <span className="font-semibold text-text-primary">{r.instrument}</span>
              {r.direction ? (
                <span className="text-text-muted">{r.direction}</span>
              ) : null}
            </div>
            <span className="font-mono text-[10px] text-text-muted">{fmtDate(r.recorded_at)}</span>
          </div>
          <div className="mt-1 grid grid-cols-2 gap-2 text-text-secondary md:grid-cols-4">
            {r.entry_price !== undefined ? <div>Entry: <span className="font-mono">{fmtNum(r.entry_price, 2)}</span></div> : null}
            {r.exit_price !== undefined ? <div>Exit: <span className="font-mono">{fmtNum(r.exit_price, 2)}</span></div> : null}
            {r.quantity !== undefined ? <div>Qty: <span className="font-mono">{r.quantity}</span></div> : null}
            {r.realized_pnl !== undefined ? (
              <div className={tonePnL(r.realized_pnl)}>
                Realized: <span className="font-mono font-semibold">{fmtRupee(r.realized_pnl)}</span>
              </div>
            ) : null}
            {r.close_reason ? <div className="col-span-2 text-text-muted">↳ {r.close_reason}</div> : null}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main workspace ────────────────────────────────────────────────────────
export default function CBEWorkspace() {
  const [activeTab, setActiveTab] = useState<DeskTab>("ranked");
  const [resetting, setResetting] = useState(false);

  // The latest persisted scan (alpha-engine output)
  const latestQuery = useQuery({
    queryKey: ["cbeAlphaLatest"],
    queryFn: () => getCBELatestScan().then((r) => r.data as AlphaPayload),
    staleTime: 30_000,
  });

  // On-demand scan
  const scanMutation = useMutation({
    mutationFn: () =>
      runCBEScan({ source: "alpha_engine" }).then((r) => r.data as AlphaPayload),
  });

  // Paper book — refresh every 30 s so the portfolio panel stays live
  const summaryQuery = useQuery({
    queryKey: ["cbePaperSummary"],
    queryFn: () => getCBEPaperSummary().then((r) => r.data as PaperSummary),
    refetchInterval: 30_000,
  });

  const positionsQuery = useQuery({
    queryKey: ["cbePaperPositions"],
    queryFn: () => getCBEPaperPositions("all", 200).then((r) => r.data as PaperPositionsResponse),
    refetchInterval: 30_000,
  });

  const journalQuery = useQuery({
    queryKey: ["cbePaperJournal"],
    queryFn: () => getCBEPaperJournal(undefined, 200).then((r) => r.data as { records: JournalRecord[] }),
    refetchInterval: 30_000,
  });

  const payload = scanMutation.data ?? latestQuery.data;
  const summary = summaryQuery.data;
  const positions = positionsQuery.data;
  const journal = journalQuery.data?.records || [];

  const ranked = payload?.results || [];
  const watchlist = payload?.watchlist || [];
  const error = scanMutation.error ? describeApiError(scanMutation.error, "Alpha scan failed") : "";

  const handleReset = async () => {
    if (!window.confirm("Reset CBE paper account to baseline? This archives all positions + journal.")) return;
    try {
      setResetting(true);
      await resetCBEPaper("frontend");
      await Promise.all([
        summaryQuery.refetch(),
        positionsQuery.refetch(),
        journalQuery.refetch(),
      ]);
    } finally {
      setResetting(false);
    }
  };

  const tabCounts = useMemo(() => {
    return {
      ranked: ranked.length,
      open: positions?.open_positions?.length ?? 0,
      history: positions?.closed_positions?.length ?? 0,
      journal: journal.length,
    };
  }, [ranked.length, positions, journal.length]);

  return (
    <div className="mx-auto flex w-full max-w-[1640px] flex-col gap-3">
      <header className="rounded-lg border border-bg-border bg-bg-secondary/28 px-4 py-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <Radar size={18} className="text-accent-blue" />
              <h1 className="text-lg font-semibold text-text-primary">CBE Alpha Engine</h1>
              <span className="rounded border border-accent-blue/30 bg-accent-blue/10 px-2 py-0.5 text-[10px] uppercase text-accent-blue">
                v1.1
              </span>
            </div>
            <div className="mt-1 text-xs text-text-muted">
              Top-down capital rotation · L1 asset → L2 sector → L3 stock → L4 option filter → L7 composite gate
              {payload?.scan_date ? ` · last scan ${payload.scan_date}` : ""}
              {payload?.elapsed_seconds ? ` · ${payload.elapsed_seconds.toFixed(2)}s` : ""}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => scanMutation.mutate()}
              disabled={scanMutation.isPending}
              className="inline-flex items-center gap-2 rounded-lg border border-accent-blue/40 bg-accent-blue/16 px-3 py-2 text-sm font-semibold text-accent-blue transition-colors hover:bg-accent-blue/24 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {scanMutation.isPending ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}
              Run Alpha Scan
            </button>
            <button
              type="button"
              onClick={handleReset}
              disabled={resetting}
              className="inline-flex items-center gap-2 rounded-lg border border-accent-red/35 bg-accent-red/10 px-3 py-2 text-sm font-semibold text-accent-red transition-colors hover:bg-accent-red/20 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {resetting ? <RefreshCw size={15} className="animate-spin" /> : <RotateCcw size={15} />}
              Reset Paper Book
            </button>
          </div>
        </div>
      </header>

      {/* Portfolio summary */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
        <Metric
          label="Initial Capital"
          value={fmtRupee(summary?.initial_capital ?? 0)}
          icon={Wallet}
        />
        <Metric
          label="Available"
          value={fmtRupee(summary?.available_capital ?? 0)}
          detail={`Reserved: ${fmtRupee(summary?.reserved_margin ?? 0)}`}
          icon={ShieldCheck}
        />
        <Metric
          label="Total Equity"
          value={fmtRupee(summary?.total_equity ?? 0)}
          detail={fmtPct(summary?.total_return_pct ?? 0)}
          icon={TrendingUp}
          tone={tonePnL((summary?.total_return_pct ?? 0))}
        />
        <Metric
          label="Realized P&L"
          value={fmtRupee(summary?.realized_pnl ?? 0)}
          detail={`Unrealized: ${fmtRupee(summary?.unrealized_pnl ?? 0)}`}
          icon={CheckCircle2}
          tone={tonePnL(summary?.realized_pnl)}
        />
        <Metric
          label="Win Rate"
          value={fmtPct((summary?.win_rate ?? 0) * 100, 1)}
          detail={`${summary?.total_trades ?? 0} closed trades`}
          icon={Target}
        />
        <Metric
          label="Max Drawdown"
          value={fmtPct((summary?.max_drawdown ?? 0) * 100, 2)}
          detail={`Sharpe: ${fmtNum(summary?.sharpe_ratio ?? 0, 2)}`}
          icon={BarChart3}
        />
      </div>

      {/* L1 + L2 layer panels side-by-side */}
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <AssetLayerPanel layer={payload?.asset_layer} />
        <SectorLayerPanel
          winners={payload?.sector_layer?.winners}
          ranked={payload?.sector_layer?.ranked_sectors}
        />
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-accent-red/35 bg-accent-red/10 px-4 py-3 text-sm text-accent-red">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {/* Tabbed desk */}
      <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {[
            { key: "ranked" as const, label: "Ranked candidates", icon: Radar, count: tabCounts.ranked },
            { key: "open" as const, label: "Open Portfolio", icon: Briefcase, count: tabCounts.open },
            { key: "history" as const, label: "Historical Trades", icon: History, count: tabCounts.history },
            { key: "journal" as const, label: "Journal", icon: ListChecks, count: tabCounts.journal },
          ].map((tab) => {
            const Icon = tab.icon;
            const active = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                className={clsx(
                  "inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors",
                  active
                    ? "border-accent-blue/40 bg-accent-blue/14 text-accent-blue"
                    : "border-bg-border bg-bg-primary/24 text-text-secondary hover:bg-bg-hover/60",
                )}
              >
                <Icon size={13} />
                {tab.label}
                <span
                  className={clsx(
                    "rounded-full px-1.5 py-0.5 text-[10px] font-mono",
                    active ? "bg-accent-blue/25 text-accent-blue" : "bg-bg-secondary/50 text-text-muted",
                  )}
                >
                  {tab.count}
                </span>
              </button>
            );
          })}
          <div className="ml-auto text-[11px] text-text-muted">
            {payload?.fno_universe_size ? `F&O universe: ${payload.fno_universe_size}` : null}
            {payload?.scored_count !== undefined ? ` · scored: ${payload.scored_count}` : null}
            {payload?.watchlist_count !== undefined
              ? ` · gate passed: ${payload.watchlist_count}`
              : null}
          </div>
        </div>

        {activeTab === "ranked" ? (
          <RankedScanTable rows={ranked} />
        ) : activeTab === "open" ? (
          <OpenPortfolioTable positions={positions?.open_positions ?? []} />
        ) : activeTab === "history" ? (
          <HistoricalTradesTable positions={positions?.closed_positions ?? []} />
        ) : (
          <JournalList records={journal} />
        )}
      </section>

      {watchlist.length ? (
        <section className="rounded-lg border border-accent-green/35 bg-accent-green/8 p-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-accent-green">
            <CheckCircle2 size={15} />
            Gate-cleared watchlist ({watchlist.length})
          </div>
          <div className="flex flex-wrap gap-2">
            {watchlist.map((row) => (
              <span
                key={row.instrument}
                className="inline-flex items-center gap-2 rounded-md border border-accent-green/35 bg-accent-green/10 px-2.5 py-1 text-xs text-accent-green"
              >
                <span className="font-semibold text-text-primary">{row.instrument}</span>
                <span className="font-mono">{fmtNum(row.composite_alpha_score, 1)}</span>
                <span className="text-[10px] uppercase">{row.directional_bias}</span>
              </span>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
