"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  Database,
  RefreshCw,
  Shield,
} from "lucide-react";

import {
  getATMWatchlist,
  getFnoAnalytics,
  getOptionChain,
  getOptionExpiries,
  getSectorRotation,
  getSectorRotationComponents,
} from "@/lib/api";
import {
  MARKET_INDEX_SYMBOLS,
  type MarketIndexSymbol,
  getMarketIndexLabel,
} from "@/lib/marketSymbols";
import { useTickSymbol } from "@/store";

type ChainEntry = {
  strike: number;
  option_type: "CE" | "PE";
  ltp?: number | null;
  oi?: number | null;
  volume?: number | null;
  iv?: number | null;
  delta?: number | null;
  theta?: number | null;
  oi_change?: number | null;
  ltp_change_pct?: number | null;
};

type OptionChainPayload = {
  symbol?: string | null;
  expiry?: string | null;
  spot_price?: number | null;
  entries?: ChainEntry[];
  pcr_oi?: number | null;
  pcr_volume?: number | null;
  max_pain?: number | null;
  atm_strike?: number | null;
  atm_iv?: number | null;
  total_ce_oi?: number | null;
  total_pe_oi?: number | null;
  error?: string | null;
};

type ExpiryPayload = {
  expiries?: string[];
  default_expiry?: string | null;
};

type AtmOptionLeg = {
  ltp?: number | null;
  change_pct?: number | null;
  oi?: number | null;
  oi_change?: number | null;
  iv?: number | null;
  rsi?: number | null;
  macd_histogram?: number | null;
};

type AtmWatchlistRow = {
  underlying?: string | null;
  kind?: string | null;
  spot_price?: number | null;
  atm_strike?: number | null;
  expiry?: string | null;
  live_source?: string | null;
  ce?: AtmOptionLeg | null;
  pe?: AtmOptionLeg | null;
};

type AtmWatchlistPayload = {
  expiry?: string | null;
  rows?: AtmWatchlistRow[];
  detail?: string | null;
};

type SectorWatchlistRow = {
  code: string;
  name: string;
  symbol?: string;
  price?: number | null;
  tracked_change_pct?: number | null;
  relative_strength_pct?: number | null;
  rrg_ratio?: number | null;
  rrg_momentum?: number | null;
  quadrant?: string | null;
  trend?: string | null;
  samples?: number | null;
  series_source?: string | null;
  member_count?: number | null;
  trail?: Array<{ ratio: number; momentum: number }>;
};

type SectorRotationPayload = {
  timeframe?: string | null;
  period_label?: string | null;
  benchmark?: {
    name?: string | null;
    tracked_change_pct?: number | null;
    samples?: number | null;
  } | null;
  watchlist?: SectorWatchlistRow[];
  rrg?: {
    points?: SectorWatchlistRow[];
  };
  stocks_by_sector?: Record<string, {
    sector?: SectorWatchlistRow;
    stocks?: SectorWatchlistRow[];
    rrg?: {
      points?: SectorWatchlistRow[];
      quadrant_counts?: Record<string, number>;
    };
    source?: string | null;
    configured_members?: number | null;
    available_members?: number | null;
    detail?: string | null;
  }>;
  source?: string | null;
  detail?: string | null;
  timestamp?: string | null;
};

type SectorComponentsPayload = {
  timeframe?: string | null;
  period_label?: string | null;
  sector?: SectorWatchlistRow | null;
  stocks?: SectorWatchlistRow[];
  rrg?: {
    points?: SectorWatchlistRow[];
    quadrant_counts?: Record<string, number>;
  };
  source?: string | null;
  configured_members?: number | null;
  available_members?: number | null;
  detail?: string | null;
  timestamp?: string | null;
};

type LiveMarketTab = "chain" | "watchlist" | "sectors" | "rrg" | "research";
type ResearchViewTab =
  | "overview"
  | "option_chain"
  | "open_interest"
  | "scalper"
  | "idea"
  | "fo_stats"
  | "strategy_chart"
  | "strategy_builder"
  | "company_info";

type GreekRow = {
  underlying?: string;
  option_type?: string;
  expiry?: string;
  strike?: number;
  ltp?: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  intrinsic_value?: number | null;
  time_value?: number | null;
  probability_itm?: number | null;
  break_even?: number | null;
  days_to_expiry?: number | null;
};

type OiPriceSignal = {
  underlying?: string;
  option_type?: string;
  label?: string;
  direction?: string;
  conviction?: string;
  price_change_pct?: number | null;
  oi_change_pct?: number | null;
  notes?: string[];
};

type CalendarSpread = {
  underlying?: string;
  near_contract_id?: string;
  far_contract_id?: string;
  near_expiry?: string;
  far_expiry?: string;
  near_price?: number;
  far_price?: number;
  spread?: number;
  spread_pct?: number;
  annualized_basis_pct?: number | null;
};

type FuturesCurve = {
  underlying?: string;
  spot_price?: number | null;
  points?: Array<Record<string, any>>;
  curve_shape?: string;
  calendar_spreads?: Array<Record<string, any>>;
  basis?: number | null;
  basis_pct?: number | null;
  annualized_basis_pct?: number | null;
  rollover_pct?: number | null;
  rollover_quality?: string | null;
  notes?: string[];
};

type StrikePositioning = {
  underlying?: string;
  expiry?: string;
  strike?: number;
  bias?: string;
  note?: string;
};

type StraddleRow = {
  underlying?: string;
  kind?: string;
  expiry?: string;
  days_to_expiry?: number | null;
  spot_price?: number | null;
  atm_strike?: number | null;
  ce_ltp?: number | null;
  pe_ltp?: number | null;
  atm_straddle?: number | null;
  expected_move?: number | null;
  expected_move_pct?: number | null;
  avg_iv?: number | null;
  ce_oi?: number | null;
  pe_oi?: number | null;
  pcr_oi?: number | null;
};

type MaxPainRow = {
  underlying?: string;
  expiry?: string;
  strikes_count?: number;
  max_pain_strike?: number | null;
  max_call_oi_strike?: number | null;
  max_put_oi_strike?: number | null;
  total_call_oi?: number | null;
  total_put_oi?: number | null;
  chain_pcr_oi?: number | null;
};

type FoRiskPayload = {
  snapshot_date?: string | null;
  mwpl?: {
    row_count?: number;
    high_utilisation?: Array<{
      symbol?: string;
      market_wide_position_limit?: number | null;
      open_interest?: number | null;
      utilisation_pct?: number | null;
    }>;
    above_80_pct_count?: number;
    above_95_pct_count?: number;
    snapshot_date?: string | null;
  };
  ban_list?: {
    count?: number;
    symbols?: string[];
    reasons?: Record<string, string>;
    snapshot_date?: string | null;
  };
};

type FnoAnalyticsPayload = {
  status?: string;
  as_of?: string;
  nse?: {
    status?: string;
    contract_master?: {
      summary?: Record<string, any>;
      sample?: Array<Record<string, any>>;
    };
    option_chain?: Record<string, any>;
    risk?: Record<string, any>;
    greeks?: { rows?: GreekRow[]; count?: number; mode?: string };
    oi_price_signals?: {
      count?: number;
      by_label?: Record<string, OiPriceSignal[]>;
      top?: OiPriceSignal[];
    };
    straddle_summary?: StraddleRow[];
    max_pain?: MaxPainRow[];
  };
  mcx?: {
    status?: string;
    contract_master?: {
      summary?: Record<string, any>;
      sample?: Array<Record<string, any>>;
    };
    option_chain?: Record<string, any>;
    risk?: Record<string, any>;
    source?: Record<string, any>;
    greeks?: { rows?: GreekRow[]; count?: number; mode?: string };
    futures_curve?: {
      curves?: FuturesCurve[];
      calendar_spreads?: CalendarSpread[];
      count?: number;
    };
    positioning?: { strikes?: StrikePositioning[] };
    straddle_summary?: StraddleRow[];
    max_pain?: MaxPainRow[];
  };
  fo_risk?: FoRiskPayload;
  quality_checks?: Array<{ key?: string; label?: string; status?: string; detail?: string }>;
  stage_status?: Array<{ stage?: number; name?: string; status?: string; detail?: string }>;
  research?: {
    modules?: Array<{ key?: string; label?: string; status?: string; metric?: string; detail?: string }>;
    answer_cards?: Array<{ key?: string; label?: string; status?: string; value?: string; detail?: string }>;
    sources?: Array<{ key?: string; label?: string; status?: string; detail?: string }>;
  };
  signals?: {
    nse?: Record<string, any>;
    mcx?: Record<string, any>;
  };
};

const SECTOR_PERIOD_OPTIONS = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "yearly", label: "Yearly" },
] as const;
type SectorPeriod = (typeof SECTOR_PERIOD_OPTIONS)[number]["value"];

const RESEARCH_VIEW_TABS: Array<{ value: ResearchViewTab; label: string; detail: string }> = [
  { value: "overview", label: "Overview", detail: "Index, contract and quality breadth" },
  { value: "option_chain", label: "Option Chain", detail: "ATM straddle, expected move and max pain" },
  { value: "open_interest", label: "Open Interest", detail: "OI-price matrix and build-up labels" },
  { value: "scalper", label: "Scalper", detail: "Fast OI, IV and MCX risk flags" },
  { value: "idea", label: "Idea", detail: "Ranked live research signals" },
  { value: "fo_stats", label: "F&O Stats", detail: "Greeks, IV and participation stats" },
  { value: "strategy_chart", label: "Strategy Chart", detail: "Futures curve and positioning" },
  { value: "strategy_builder", label: "Strategy Builder", detail: "Trade inputs and risk gates" },
  { value: "company_info", label: "Company Info", detail: "Contract master and source coverage" },
];

function SectorPeriodSelect({
  value,
  onChange,
}: {
  value: SectorPeriod;
  onChange: (value: SectorPeriod) => void;
}) {
  return (
    <label className="inline-flex items-center gap-2 rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs text-text-secondary">
      <span className="uppercase tracking-[0.1em] text-text-muted">Period</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as SectorPeriod)}
        className="bg-transparent font-mono text-text-primary outline-none"
        title="Sector lead period"
      >
        {SECTOR_PERIOD_OPTIONS.map((option) => (
          <option key={option.value} value={option.value} className="bg-bg-card text-text-primary">
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 10_00_000) return `${sign}${(abs / 10_00_000).toFixed(1)}L`;
  if (abs >= 1_000) return `${sign}${(abs / 1_000).toFixed(1)}K`;
  return `${value.toFixed(0)}`;
}

function formatPercent(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}%`;
}

function formatIv(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const normalized = value > 5 ? value : value * 100;
  return `${normalized.toFixed(1)}%`;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function prettify(value?: string | null) {
  if (!value) return "--";
  return value
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .split(" ")
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function pnlTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function quadrantTone(value?: string | null) {
  const quadrant = String(value || "").toLowerCase();
  if (quadrant === "leading") return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  if (quadrant === "improving") return "border-accent-blue/30 bg-accent-blue/10 text-accent-blue";
  if (quadrant === "weakening") return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  return "border-accent-red/30 bg-accent-red/10 text-accent-red";
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function positionPct(value: number | null | undefined, min: number, max: number) {
  if (value == null || !Number.isFinite(value) || max <= min) return 50;
  return clamp(((value - min) / (max - min)) * 100, 7, 93);
}

function badgeTone(value?: string | null) {
  const text = String(value || "").toLowerCase();
  if (["ready", "prepared", "ok", "active", "connected", "ce", "trend-aligned", "entry-ready"].some((key) => text.includes(key))) {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (["pe", "stale", "waiting", "monitoring", "market closed", "session-close"].some((key) => text.includes(key))) {
    return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  }
  if (["blocked", "missing", "error", "expired", "offline", "kill"].some((key) => text.includes(key))) {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-border bg-bg-secondary/40 text-text-secondary";
}

function StatusBadge({ label, tone }: { label: string; tone?: string | null }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]",
        badgeTone(tone || label),
      )}
    >
      {label}
    </span>
  );
}

function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2" title={detail || label}>
      <div className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-base font-semibold text-text-primary", tone)}>{value}</div>
      {detail ? <div className="truncate text-[10px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function PanelHeader({
  icon,
  title,
  detail,
  meta,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  meta?: string;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex items-center gap-2 text-sm font-semibold text-text-primary" title={detail}>
        {icon}
        {title}
      </div>
      {meta ? <div className="text-[11px] text-text-muted">{meta}</div> : null}
    </div>
  );
}

function LiveIndexButton({
  symbol,
  active,
  onSelect,
}: {
  symbol: MarketIndexSymbol;
  active: boolean;
  onSelect: (symbol: MarketIndexSymbol) => void;
}) {
  const tick = useTickSymbol(symbol);
  const positive = tick && tick.close > 0 ? tick.ltp >= tick.close : undefined;
  return (
    <button
      type="button"
      onClick={() => onSelect(symbol)}
      className={clsx(
        "rounded-lg border px-3 py-2 text-left transition-colors",
        active
          ? "border-accent-blue bg-accent-blue/10"
          : "border-bg-border bg-bg-secondary/35 hover:border-bg-active",
      )}
    >
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{getMarketIndexLabel(symbol)}</div>
      <div className="mt-1 flex items-end justify-between gap-3">
        <span className="font-mono text-base font-semibold text-text-primary">{tick ? tick.ltp.toFixed(2) : "--"}</span>
        <span
          className={clsx(
            "rounded-md px-1.5 py-0.5 text-[10px] font-semibold",
            positive === undefined
              ? "bg-bg-primary text-text-muted"
              : positive
                ? "bg-accent-green/12 text-accent-green"
                : "bg-accent-red/12 text-accent-red",
          )}
        >
          {tick && tick.close ? formatPercent(((tick.ltp - tick.close) / tick.close) * 100) : "Waiting"}
        </span>
      </div>
    </button>
  );
}

function OptionLegCell({ leg }: { leg?: AtmOptionLeg | null }) {
  return (
    <div className="grid grid-cols-4 gap-2 text-right font-mono text-[11px]">
      <span className="font-semibold text-text-primary">{formatNumber(leg?.ltp)}</span>
      <span className={pnlTone(leg?.change_pct)}>{formatPercent(leg?.change_pct)}</span>
      <span>{formatCompact(leg?.oi)}</span>
      <span className={pnlTone(leg?.oi_change)}>{formatCompact(leg?.oi_change)}</span>
    </div>
  );
}

function AtmWatchlistTable({ rows }: { rows: AtmWatchlistRow[] }) {
  return (
    <div className="overflow-auto rounded-lg border border-bg-border">
      <table className="w-full min-w-[980px] text-xs">
        <thead className="bg-bg-primary/70 text-text-muted">
          <tr>
            <th className="px-3 py-2 text-left">Underlying</th>
            <th className="px-3 py-2 text-right">Spot</th>
            <th className="px-3 py-2 text-right">ATM</th>
            <th className="px-3 py-2 text-right">CE LTP / Chg / OI / OI Chg</th>
            <th className="px-3 py-2 text-right">PE LTP / Chg / OI / OI Chg</th>
            <th className="px-3 py-2 text-left">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.underlying}-${row.expiry}-${row.atm_strike}`} className="border-t border-bg-border/60">
              <td className="px-3 py-2 font-semibold text-text-primary">{row.underlying || "--"}</td>
              <td className="px-3 py-2 text-right font-mono">{formatNumber(row.spot_price)}</td>
              <td className="px-3 py-2 text-right font-mono">{formatNumber(row.atm_strike, 0)}</td>
              <td className="px-3 py-2"><OptionLegCell leg={row.ce} /></td>
              <td className="px-3 py-2"><OptionLegCell leg={row.pe} /></td>
              <td className="px-3 py-2 text-text-muted">{row.live_source || row.kind || "--"}</td>
            </tr>
          ))}
          {!rows.length ? (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-text-muted">No CE/PE ATM watchlist rows returned.</td></tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

function getQuadrantCounts(rows: SectorWatchlistRow[]) {
  return {
    leading: rows.filter((row) => row.quadrant === "leading").length,
    improving: rows.filter((row) => row.quadrant === "improving").length,
    deteriorating: rows.filter((row) => row.quadrant === "weakening").length,
    lagging: rows.filter((row) => row.quadrant === "lagging").length,
  };
}

function rrgPointLabel(point: SectorWatchlistRow) {
  if (!point.code.includes("_")) return point.code.slice(0, 7);
  return point.code.replaceAll("_", " ").split(" ").map((part) => part[0]).join("").slice(0, 4);
}

function RrgMap({
  points,
  activeCode,
  onPointClick,
}: {
  points: SectorWatchlistRow[];
  activeCode?: string | null;
  onPointClick?: (point: SectorWatchlistRow) => void;
}) {
  const xValues = points.map((point) => point.rrg_ratio ?? 100);
  const yValues = points.map((point) => point.rrg_momentum ?? 100);
  const xMin = Math.min(95, ...xValues) - 1;
  const xMax = Math.max(105, ...xValues) + 1;
  const yMin = Math.min(95, ...yValues) - 1;
  const yMax = Math.max(105, ...yValues) + 1;

  return (
    <div className="relative min-h-[340px] overflow-hidden rounded-lg border border-bg-border bg-bg-primary/50">
      <div className="absolute inset-0 grid grid-cols-2 grid-rows-2 text-[10px] uppercase tracking-[0.14em] text-text-muted">
        <div className="border-b border-r border-bg-border/60 p-3">Improving</div>
        <div className="border-b border-bg-border/60 p-3 text-right">Leading</div>
        <div className="border-r border-bg-border/60 p-3">Lagging</div>
        <div className="p-3 text-right">Weakening</div>
      </div>
      <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
        <line x1="50" y1="0" x2="50" y2="100" stroke="#26344d" strokeWidth="0.35" />
        <line x1="0" y1="50" x2="100" y2="50" stroke="#26344d" strokeWidth="0.35" />
      </svg>
      {points.slice(0, 28).map((point) => {
        const left = positionPct(point.rrg_ratio, xMin, xMax);
        const top = 100 - positionPct(point.rrg_momentum, yMin, yMax);
        const clickable = Boolean(onPointClick);
        return (
          <button
            key={point.code}
            type="button"
            onClick={() => onPointClick?.(point)}
            title={`${point.name} · ${point.quadrant || "unknown"}`}
            className={clsx(
              "absolute -translate-x-1/2 -translate-y-1/2 rounded-md border px-1.5 py-1 text-[10px] font-semibold uppercase shadow-lg transition-transform",
              clickable && "cursor-pointer hover:z-10 hover:scale-110",
              activeCode === point.code && "ring-1 ring-accent-blue",
              quadrantTone(point.quadrant),
            )}
            style={{ left: `${left}%`, top: `${top}%` }}
          >
            {rrgPointLabel(point)}
          </button>
        );
      })}
      {!points.length ? (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-text-muted">No RRG points available.</div>
      ) : null}
    </div>
  );
}

function SignalList({ title, rows }: { title: string; rows: Array<Record<string, any>> }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">{title}</div>
        <div className="font-mono text-[11px] text-text-muted">{rows.length}</div>
      </div>
      <div className="mt-2 max-h-[150px] space-y-1.5 overflow-auto">
        {rows.slice(0, 6).map((row, index) => (
          <div key={`${title}-${row.symbol || row.underlying || row.contract_id || index}`} className="rounded-md bg-bg-secondary/35 px-2.5 py-2">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-[11px] font-semibold text-text-primary">
                {row.symbol || row.underlying || row.contract_id || "--"}
              </span>
              <span className="font-mono text-[10px] text-text-muted">
                {row.side || row.option_type || row.buildup || row.risk || ""}
              </span>
            </div>
            <div className="mt-1 truncate font-mono text-[10px] text-text-muted">
              OI {formatCompact(row.oi ?? row.total_oi ?? row.oi_change)} · Vol {formatCompact(row.volume ?? row.total_volume)} · IV {formatNumber(row.iv ?? row.avg_iv)}
            </div>
          </div>
        ))}
        {!rows.length ? <div className="text-[11px] text-text-muted">No rows currently flagged.</div> : null}
      </div>
    </div>
  );
}

function qualityTone(value?: string | null) {
  const status = String(value || "").toLowerCase();
  if (["ok", "ready", "existing"].includes(status)) return "text-accent-green";
  if (["partial", "attention", "watch"].includes(status)) return "text-accent-amber";
  if (["missing", "unavailable", "error"].includes(status)) return "text-accent-red";
  return "text-text-secondary";
}

// ─── Options Analytics (Phase A) ────────────────────────────────────────────
// Surfaces the Black-Scholes Greeks, OI–price participant matrix, futures
// curve shape + calendar spreads, and per-strike positioning bias that the
// backend's /api/market/fno-analytics endpoint now computes.

const OI_PRICE_LABELS = [
  { key: "long_buildup", label: "Long Buildup", tone: "text-accent-green", glyph: "↑↑" },
  { key: "short_covering", label: "Short Covering", tone: "text-accent-green", glyph: "↑↓" },
  { key: "short_buildup", label: "Short Buildup", tone: "text-accent-red", glyph: "↓↑" },
  { key: "long_unwinding", label: "Long Unwinding", tone: "text-accent-red", glyph: "↓↓" },
] as const;

function curveShapeTone(shape?: string | null) {
  const value = String(shape || "").toLowerCase();
  if (value === "contango") return "text-accent-amber";
  if (value === "backwardation") return "text-accent-red";
  if (value === "flat") return "text-accent-blue";
  if (value === "mixed") return "text-text-secondary";
  return "text-text-muted";
}

function rolloverTone(quality?: string | null) {
  const value = String(quality || "").toLowerCase();
  if (value === "strong") return "text-accent-green";
  if (value === "weak") return "text-accent-red";
  if (value === "neutral") return "text-accent-amber";
  return "text-text-muted";
}

function positioningTone(bias?: string | null) {
  const value = String(bias || "").toLowerCase();
  if (value === "call_writing") return "text-accent-red";
  if (value === "put_writing") return "text-accent-green";
  if (value === "call_buying") return "text-accent-green";
  if (value === "put_buying") return "text-accent-red";
  return "text-text-muted";
}

function convictionTone(conv?: string | null) {
  const value = String(conv || "").toLowerCase();
  if (value === "high") return "text-accent-green";
  if (value === "medium") return "text-accent-amber";
  return "text-text-muted";
}

function GreeksTable({ rows, title, mode }: { rows: GreekRow[]; title: string; mode?: string }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">{title}</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">
          {mode || "BS"} · {rows.length}
        </div>
      </div>
      <div className="mt-2 overflow-auto">
        <table className="w-full min-w-[520px] text-[11px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="py-1.5 pr-2 text-left">Contract</th>
              <th className="py-1.5 pr-2 text-right">LTP</th>
              <th className="py-1.5 pr-2 text-right">IV</th>
              <th className="py-1.5 pr-2 text-right">Δ</th>
              <th className="py-1.5 pr-2 text-right">Γ</th>
              <th className="py-1.5 pr-2 text-right">Θ/day</th>
              <th className="py-1.5 pr-2 text-right">Vega</th>
              <th className="py-1.5 text-right">BE</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.slice(0, 8).map((row, i) => (
              <tr key={`${row.underlying}-${row.option_type}-${row.strike}-${i}`} className="border-b border-bg-border/20">
                <td className="py-1.5 pr-2 text-text-primary">
                  <span className="font-semibold">{row.underlying || "--"}</span>
                  <span className="ml-1 text-text-muted">{row.strike ? formatNumber(row.strike, 0) : "--"}{row.option_type || ""}</span>
                </td>
                <td className="py-1.5 pr-2 text-right">{formatNumber(row.ltp)}</td>
                <td className="py-1.5 pr-2 text-right text-accent-blue">
                  {row.iv != null ? formatPercent(row.iv * 100, 1) : "--"}
                </td>
                <td className="py-1.5 pr-2 text-right">{formatNumber(row.delta, 3)}</td>
                <td className="py-1.5 pr-2 text-right">{row.gamma != null ? row.gamma.toExponential(2) : "--"}</td>
                <td className="py-1.5 pr-2 text-right text-accent-red">{formatNumber(row.theta, 2)}</td>
                <td className="py-1.5 pr-2 text-right">{formatNumber(row.vega, 2)}</td>
                <td className="py-1.5 text-right text-text-secondary">{formatNumber(row.break_even, 0)}</td>
              </tr>
            ))}
            {!rows.length ? (
              <tr><td colSpan={8} className="py-3 text-center text-text-muted">No Greeks computed yet (waiting on spot + premium snapshots).</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function OiPriceMatrixCard({ payload }: { payload?: { count?: number; by_label?: Record<string, OiPriceSignal[]> } }) {
  const byLabel = payload?.by_label || {};
  const total = payload?.count ?? 0;
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">OI–Price Participant Matrix</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{total} classified</div>
      </div>
      <div className="mt-2 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {OI_PRICE_LABELS.map((bucket) => {
          const items = byLabel[bucket.key] || [];
          return (
            <div key={bucket.key} className="rounded-md border border-bg-border/50 bg-bg-secondary/30 p-2">
              <div className="flex items-center justify-between">
                <span className={clsx("text-[11px] font-semibold uppercase tracking-wide", bucket.tone)}>
                  <span className="mr-1 font-mono">{bucket.glyph}</span>{bucket.label}
                </span>
                <span className="font-mono text-[10px] text-text-muted">{items.length}</span>
              </div>
              <div className="mt-1 max-h-[140px] space-y-1 overflow-auto">
                {items.slice(0, 6).map((item, i) => (
                  <div key={`${bucket.key}-${item.underlying}-${item.option_type}-${i}`} className="rounded-sm bg-bg-primary/40 px-2 py-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-[11px] font-semibold text-text-primary">
                        {item.underlying} <span className="text-text-muted">{item.option_type}</span>
                      </span>
                      <span className={clsx("font-mono text-[9px] uppercase", convictionTone(item.conviction))}>
                        {item.conviction || "--"}
                      </span>
                    </div>
                    <div className="mt-0.5 flex justify-between font-mono text-[10px] text-text-muted">
                      <span>P {formatSigned(item.price_change_pct, 2, "%")}</span>
                      <span>OI {formatSigned(item.oi_change_pct, 2, "%")}</span>
                    </div>
                  </div>
                ))}
                {!items.length ? <div className="text-[10px] text-text-muted">No items.</div> : null}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FuturesCurveCard({ curves, spreads }: { curves: FuturesCurve[]; spreads: CalendarSpread[] }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">MCX Futures Curve</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{curves.length} underlyings · {spreads.length} spreads</div>
      </div>
      <div className="mt-2 grid gap-2 lg:grid-cols-2">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-text-muted">Curve Shape per Underlying</div>
          <div className="mt-1 space-y-1.5">
            {curves.map((curve, i) => (
              <div key={`${curve.underlying}-${i}`} className="rounded-md bg-bg-secondary/30 px-2.5 py-2">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-text-primary">{curve.underlying}</span>
                  <span className={clsx("font-mono text-[10px] uppercase", curveShapeTone(curve.curve_shape))}>
                    {curve.curve_shape || "--"}
                  </span>
                </div>
                <div className="mt-1 flex justify-between font-mono text-[10px] text-text-muted">
                  <span>Basis {formatSigned(curve.basis_pct, 2, "%")}</span>
                  <span>Ann {formatSigned(curve.annualized_basis_pct, 1, "%")}</span>
                  <span>
                    Rollover{" "}
                    <span className={rolloverTone(curve.rollover_quality)}>
                      {curve.rollover_pct != null ? `${curve.rollover_pct.toFixed(1)}%` : "--"}
                      {curve.rollover_quality ? ` · ${curve.rollover_quality}` : ""}
                    </span>
                  </span>
                </div>
                {curve.notes?.length ? (
                  <div className="mt-1 text-[10px] leading-4 text-text-muted">{curve.notes[0]}</div>
                ) : null}
              </div>
            ))}
            {!curves.length ? (
              <div className="rounded-md bg-bg-secondary/20 px-2.5 py-3 text-[11px] text-text-muted">
                No curves yet. Curves materialise once two or more futures contracts per root have live prices.
              </div>
            ) : null}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-text-muted">Calendar Spreads (top by annualised basis)</div>
          <div className="mt-1 space-y-1">
            {spreads.slice(0, 8).map((s, i) => (
              <div key={`${s.near_contract_id}-${s.far_contract_id}-${i}`} className="rounded-md bg-bg-secondary/30 px-2.5 py-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-text-primary">{s.underlying}</span>
                  <span className="font-mono text-[10px] text-text-muted">
                    {s.near_expiry} → {s.far_expiry}
                  </span>
                </div>
                <div className="mt-0.5 flex justify-between font-mono text-[10px]">
                  <span className="text-text-muted">{formatNumber(s.near_price, 1)} → {formatNumber(s.far_price, 1)}</span>
                  <span className={clsx(((s.spread ?? 0) >= 0) ? "text-accent-amber" : "text-accent-red")}>
                    {formatSigned(s.spread, 1)} ({formatSigned(s.spread_pct, 2, "%")})
                  </span>
                  <span className="text-text-secondary">
                    Ann {formatSigned(s.annualized_basis_pct, 1, "%")}
                  </span>
                </div>
              </div>
            ))}
            {!spreads.length ? <div className="text-[11px] text-text-muted">No calendar spreads available.</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

function StrikePositioningCard({ rows }: { rows: StrikePositioning[] }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">Strike Positioning Bias</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{rows.length} strikes</div>
      </div>
      <div className="mt-2 space-y-1">
        {rows.slice(0, 8).map((row, i) => (
          <div key={`${row.underlying}-${row.strike}-${i}`} className="rounded-md bg-bg-secondary/30 px-2.5 py-1.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-semibold text-text-primary">
                {row.underlying} <span className="font-mono text-text-muted">{row.strike ? formatNumber(row.strike, 0) : "--"}</span>
              </span>
              <span className={clsx("font-mono text-[10px] uppercase", positioningTone(row.bias))}>
                {(row.bias || "--").replaceAll("_", " ")}
              </span>
            </div>
            {row.note ? <div className="mt-0.5 text-[10px] leading-4 text-text-muted">{row.note}</div> : null}
          </div>
        ))}
        {!rows.length ? (
          <div className="text-[11px] text-text-muted">No strikes flagged. Bias surfaces when OI builds with a directional bias.</div>
        ) : null}
      </div>
    </div>
  );
}

function StraddleTable({ rows, title }: { rows: StraddleRow[]; title: string }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">{title}</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{rows.length}</div>
      </div>
      <div className="mt-2 overflow-auto">
        <table className="w-full min-w-[560px] text-[11px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="py-1.5 pr-2 text-left">Underlying</th>
              <th className="py-1.5 pr-2 text-right">Spot</th>
              <th className="py-1.5 pr-2 text-right">ATM</th>
              <th className="py-1.5 pr-2 text-right">CE</th>
              <th className="py-1.5 pr-2 text-right">PE</th>
              <th className="py-1.5 pr-2 text-right">Straddle</th>
              <th className="py-1.5 pr-2 text-right">Exp Move</th>
              <th className="py-1.5 pr-2 text-right">PCR-OI</th>
              <th className="py-1.5 text-right">Avg IV</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.slice(0, 10).map((row, i) => {
              const em = row.expected_move_pct ?? 0;
              const emTone = em > 5 ? "text-accent-red" : em > 1.5 ? "text-accent-amber" : "text-text-secondary";
              const pcrTone = row.pcr_oi == null
                ? "text-text-muted"
                : row.pcr_oi > 1.25 ? "text-accent-green"
                : row.pcr_oi < 0.8 ? "text-accent-red"
                : "text-text-secondary";
              return (
                <tr key={`${row.underlying}-${i}`} className="border-b border-bg-border/20">
                  <td className="py-1.5 pr-2 text-text-primary">
                    <span className="font-semibold">{row.underlying || "--"}</span>
                  </td>
                  <td className="py-1.5 pr-2 text-right">{formatNumber(row.spot_price, 2)}</td>
                  <td className="py-1.5 pr-2 text-right text-text-secondary">{formatNumber(row.atm_strike, 0)}</td>
                  <td className="py-1.5 pr-2 text-right">{formatNumber(row.ce_ltp, 2)}</td>
                  <td className="py-1.5 pr-2 text-right">{formatNumber(row.pe_ltp, 2)}</td>
                  <td className="py-1.5 pr-2 text-right text-text-primary">{formatNumber(row.atm_straddle, 2)}</td>
                  <td className={clsx("py-1.5 pr-2 text-right font-semibold", emTone)}>
                    {row.expected_move_pct != null ? `±${row.expected_move_pct.toFixed(2)}%` : "--"}
                  </td>
                  <td className={clsx("py-1.5 pr-2 text-right", pcrTone)}>{formatNumber(row.pcr_oi, 2)}</td>
                  <td className="py-1.5 text-right text-accent-blue">
                    {row.avg_iv != null ? formatPercent(row.avg_iv * 100, 1) : "--"}
                  </td>
                </tr>
              );
            })}
            {!rows.length ? (
              <tr><td colSpan={9} className="py-3 text-center text-text-muted">No ATM rows available yet.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 text-[10px] leading-4 text-text-muted">
        Straddle premium ≈ 1-σ implied move by expiry. PCR-OI &gt; 1.25 is put-heavy (bullish skew), &lt; 0.8 is call-heavy (bearish skew).
      </div>
    </div>
  );
}

function MaxPainTable({ rows, title }: { rows: MaxPainRow[]; title: string }) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">{title}</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{rows.length}</div>
      </div>
      <div className="mt-2 overflow-auto">
        <table className="w-full min-w-[560px] text-[11px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="py-1.5 pr-2 text-left">Underlying</th>
              <th className="py-1.5 pr-2 text-left">Expiry</th>
              <th className="py-1.5 pr-2 text-right">Strikes</th>
              <th className="py-1.5 pr-2 text-right">Max Pain</th>
              <th className="py-1.5 pr-2 text-right">Resistance (CE OI)</th>
              <th className="py-1.5 pr-2 text-right">Support (PE OI)</th>
              <th className="py-1.5 pr-2 text-right">Total CE OI</th>
              <th className="py-1.5 pr-2 text-right">Total PE OI</th>
              <th className="py-1.5 text-right">Chain PCR</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.slice(0, 10).map((row, i) => {
              const pcr = row.chain_pcr_oi;
              const pcrTone = pcr == null
                ? "text-text-muted"
                : pcr > 1.25 ? "text-accent-green"
                : pcr < 0.8 ? "text-accent-red"
                : "text-text-secondary";
              return (
                <tr key={`${row.underlying}-${row.expiry}-${i}`} className="border-b border-bg-border/20">
                  <td className="py-1.5 pr-2 text-text-primary font-semibold">{row.underlying || "--"}</td>
                  <td className="py-1.5 pr-2 text-text-muted text-[10px]">{row.expiry || "--"}</td>
                  <td className="py-1.5 pr-2 text-right text-text-secondary">{row.strikes_count || "--"}</td>
                  <td className="py-1.5 pr-2 text-right text-accent-blue font-semibold">{formatNumber(row.max_pain_strike, 0)}</td>
                  <td className="py-1.5 pr-2 text-right text-accent-red">{formatNumber(row.max_call_oi_strike, 0)}</td>
                  <td className="py-1.5 pr-2 text-right text-accent-green">{formatNumber(row.max_put_oi_strike, 0)}</td>
                  <td className="py-1.5 pr-2 text-right text-text-secondary">{formatCompact(row.total_call_oi)}</td>
                  <td className="py-1.5 pr-2 text-right text-text-secondary">{formatCompact(row.total_put_oi)}</td>
                  <td className={clsx("py-1.5 text-right", pcrTone)}>{formatNumber(row.chain_pcr_oi, 2)}</td>
                </tr>
              );
            })}
            {!rows.length ? (
              <tr><td colSpan={9} className="py-3 text-center text-text-muted">No multi-strike chains stored yet. Max-pain needs ≥2 strikes per (underlying, expiry).</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 text-[10px] leading-4 text-text-muted">
        Max-pain = strike where writer payout is minimised at expiry. Resistance = strike with biggest CE OI build-up.
        Support = strike with biggest PE OI build-up. Chain PCR-OI is computed across all loaded strikes (not just ATM).
      </div>
    </div>
  );
}

function FoRiskCard({ payload }: { payload?: FoRiskPayload }) {
  const mwpl = payload?.mwpl;
  const ban = payload?.ban_list;
  const banSymbols = ban?.symbols || [];
  const highUtil = mwpl?.high_utilisation || [];
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">F&O Risk · MWPL Utilisation · Ban List</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">
          {payload?.snapshot_date || "no snapshot"}
        </div>
      </div>
      <div className="mt-2 grid gap-2 md:grid-cols-3">
        <div className="rounded-md border border-bg-border/40 bg-bg-secondary/30 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wide text-text-muted">Tracked</div>
          <div className="font-mono text-lg text-text-primary">{mwpl?.row_count ?? 0}</div>
          <div className="text-[10px] text-text-muted">symbols on MWPL list</div>
        </div>
        <div className="rounded-md border border-accent-amber/30 bg-accent-amber/8 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wide text-accent-amber">≥ 80% utilisation</div>
          <div className="font-mono text-lg text-accent-amber">{mwpl?.above_80_pct_count ?? 0}</div>
          <div className="text-[10px] text-text-muted">at risk of ban next print</div>
        </div>
        <div className="rounded-md border border-accent-red/30 bg-accent-red/8 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wide text-accent-red">≥ 95% — banned</div>
          <div className="font-mono text-lg text-accent-red">{ban?.count ?? 0}</div>
          <div className="text-[10px] text-text-muted">fresh F&O positions blocked</div>
        </div>
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-text-muted">Top utilisation %</div>
          <div className="mt-1 overflow-auto">
            <table className="w-full min-w-[420px] text-[11px]">
              <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="py-1.5 pr-2 text-left">Symbol</th>
                  <th className="py-1.5 pr-2 text-right">Utilisation</th>
                  <th className="py-1.5 pr-2 text-right">Open Interest</th>
                  <th className="py-1.5 text-right">MWPL Limit</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {highUtil.slice(0, 12).map((row, i) => {
                  const u = row.utilisation_pct ?? 0;
                  const tone = u >= 95
                    ? "text-accent-red"
                    : u >= 80 ? "text-accent-amber"
                    : "text-text-secondary";
                  return (
                    <tr key={`${row.symbol}-${i}`} className="border-b border-bg-border/20">
                      <td className="py-1.5 pr-2 text-text-primary font-semibold">{row.symbol || "--"}</td>
                      <td className={clsx("py-1.5 pr-2 text-right", tone)}>{u.toFixed(1)}%</td>
                      <td className="py-1.5 pr-2 text-right text-text-secondary">{formatCompact(row.open_interest)}</td>
                      <td className="py-1.5 text-right text-text-muted">{formatCompact(row.market_wide_position_limit)}</td>
                    </tr>
                  );
                })}
                {!highUtil.length ? (
                  <tr><td colSpan={4} className="py-3 text-center text-text-muted">No MWPL snapshot yet — POST /api/market/fo-risk/refresh to seed.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-text-muted">F&O Ban List ({banSymbols.length})</div>
          <div className="mt-1 max-h-[260px] overflow-auto rounded-md border border-bg-border/40 bg-bg-secondary/25 p-2">
            {banSymbols.length ? (
              <div className="flex flex-wrap gap-1">
                {banSymbols.map((sym) => (
                  <span
                    key={sym}
                    className="rounded-md border border-accent-red/30 bg-accent-red/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-accent-red"
                  >
                    {sym}
                  </span>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-text-muted">No banned symbols today.</div>
            )}
          </div>
          <div className="mt-1.5 text-[10px] leading-4 text-text-muted">
            Banned symbols only allow position-reducing trades. Pre-trade gate must check this before any F&O entry.
          </div>
        </div>
      </div>
    </div>
  );
}

function OptionsAnalyticsSection({
  payload,
  isLoading,
}: {
  payload?: FnoAnalyticsPayload;
  isLoading: boolean;
}) {
  const nseGreeks = payload?.nse?.greeks?.rows || [];
  const mcxGreeks = payload?.mcx?.greeks?.rows || [];
  const oiMatrix = payload?.nse?.oi_price_signals || {};
  const curves = payload?.mcx?.futures_curve?.curves || [];
  const spreads = payload?.mcx?.futures_curve?.calendar_spreads || [];
  const positioning = payload?.mcx?.positioning?.strikes || [];
  const nseStraddles = payload?.nse?.straddle_summary || [];
  const mcxStraddles = payload?.mcx?.straddle_summary || [];
  const nseMaxPain = payload?.nse?.max_pain || [];
  const mcxMaxPain = payload?.mcx?.max_pain || [];
  const foRisk = payload?.fo_risk;

  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
      <PanelHeader
        icon={<Activity size={16} className="text-accent-blue" />}
        title="Options Analytics"
        detail="ATM straddle / expected move, Greeks (Black-Scholes), OI–price participant matrix, futures curve shape and per-strike positioning bias."
        meta={isLoading ? "loading" : `${(oiMatrix.count || 0)} OI signals · ${curves.length} curves · ${nseStraddles.length + mcxStraddles.length} ATM rows`}
      />
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <StraddleTable rows={nseStraddles} title="NSE ATM Straddle & Expected Move" />
        <StraddleTable rows={mcxStraddles} title="MCX ATM Straddle & Expected Move" />
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <MaxPainTable rows={nseMaxPain} title="NSE Max Pain · Support/Resistance · Chain PCR" />
        <MaxPainTable rows={mcxMaxPain} title="MCX Max Pain · Support/Resistance · Chain PCR" />
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <GreeksTable rows={nseGreeks} title="NSE Option Greeks (top ATM)" mode={payload?.nse?.greeks?.mode} />
        <GreeksTable rows={mcxGreeks} title="MCX Option Greeks (top ATM)" mode={payload?.mcx?.greeks?.mode} />
      </div>
      <div className="mt-3">
        <OiPriceMatrixCard payload={oiMatrix} />
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-[1.4fr_1fr]">
        <FuturesCurveCard curves={curves} spreads={spreads} />
        <StrikePositioningCard rows={positioning} />
      </div>
      <div className="mt-3">
        <FoRiskCard payload={foRisk} />
      </div>
    </div>
  );
}

function ResearchModuleGrid({ payload }: { payload?: FnoAnalyticsPayload }) {
  const modules = payload?.research?.modules || [];
  const answers = payload?.research?.answer_cards || [];
  const sources = payload?.research?.sources || [];
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
      <PanelHeader
        icon={<Database size={16} className="text-accent-blue" />}
        title="Live Research Control"
        detail="Data-backed NSE/MCX research modules, source coverage and decision checks generated by the analytics endpoint."
        meta={`${modules.length} modules · ${sources.length} sources`}
      />
      <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {answers.map((item) => (
          <div key={item.key || item.label} className="rounded-lg border border-bg-border/70 bg-bg-secondary/25 px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-[11px] font-semibold text-text-primary">{item.label}</div>
              <span className={clsx("font-mono text-[10px] uppercase", qualityTone(item.status))}>{item.status || "--"}</span>
            </div>
            <div className="mt-2 font-mono text-lg text-text-primary">{item.value || "--"}</div>
            <div className="mt-1 text-[11px] leading-5 text-text-muted">{item.detail || "--"}</div>
          </div>
        ))}
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {modules.map((module) => (
            <div key={module.key || module.label} className="rounded-lg border border-bg-border/70 bg-bg-secondary/25 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-semibold text-text-primary">{module.label}</div>
                <span className={clsx("font-mono text-[10px] uppercase", qualityTone(module.status))}>{module.status || "--"}</span>
              </div>
              <div className="mt-2 font-mono text-sm text-accent-blue">{module.metric || "--"}</div>
              <div className="mt-2 text-[11px] leading-5 text-text-muted">{module.detail || "--"}</div>
            </div>
          ))}
          {!modules.length ? (
            <div className="rounded-lg border border-bg-border/70 bg-bg-secondary/25 p-3 text-xs text-text-muted">
              Research modules will appear once `/api/market/fno-analytics` returns live source coverage.
            </div>
          ) : null}
        </div>
        <div className="rounded-lg border border-bg-border/70 bg-bg-secondary/25 p-3">
          <div className="text-sm font-semibold text-text-primary">Source Coverage</div>
          <div className="mt-3 space-y-2">
            {sources.map((source) => (
              <div key={source.key || source.label} className="rounded-md border border-bg-border/60 bg-bg-primary/30 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs font-semibold text-text-primary">{source.label}</span>
                  <span className={clsx("font-mono text-[10px] uppercase", qualityTone(source.status))}>{source.status || "--"}</span>
                </div>
                <div className="mt-1 font-mono text-[10px] leading-4 text-text-muted">{source.detail || "--"}</div>
              </div>
            ))}
            {!sources.length ? <div className="text-xs text-text-muted">No source coverage returned.</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

function ContractSampleTable({
  title,
  rows,
}: {
  title: string;
  rows: Array<Record<string, any>>;
}) {
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">{title}</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{rows.length} sampled</div>
      </div>
      <div className="mt-2 overflow-auto">
        <table className="w-full min-w-[760px] text-[11px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="py-1.5 pr-2 text-left">Contract</th>
              <th className="py-1.5 pr-2 text-left">Underlying</th>
              <th className="py-1.5 pr-2 text-left">Type</th>
              <th className="py-1.5 pr-2 text-right">Expiry</th>
              <th className="py-1.5 pr-2 text-right">Strike</th>
              <th className="py-1.5 pr-2 text-right">Lot</th>
              <th className="py-1.5 text-left">Risk</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.slice(0, 10).map((row, index) => (
              <tr key={`${row.contract_id || row.instrument_key}-${index}`} className="border-b border-bg-border/20">
                <td className="max-w-[260px] truncate py-1.5 pr-2 text-text-primary" title={row.contract_id || "--"}>{row.contract_id || "--"}</td>
                <td className="py-1.5 pr-2 text-text-secondary">{row.underlying || "--"}</td>
                <td className="py-1.5 pr-2 text-text-secondary">{row.instrument_type || "--"}{row.option_type ? `/${row.option_type}` : ""}</td>
                <td className="py-1.5 pr-2 text-right text-text-muted">{row.expiry_date || "--"}</td>
                <td className="py-1.5 pr-2 text-right text-text-secondary">{formatNumber(row.strike_price, 0)}</td>
                <td className="py-1.5 pr-2 text-right text-text-secondary">{row.lot_size ?? "--"}</td>
                <td className="py-1.5 text-left">
                  <span className={clsx("rounded-md border px-1.5 py-0.5 text-[10px] uppercase", row.is_devolvement_applicable || row.is_physical_settlement ? "border-accent-amber/30 bg-accent-amber/8 text-accent-amber" : "border-bg-border bg-bg-secondary/30 text-text-muted")}>
                    {row.is_devolvement_applicable ? "devolves" : row.is_physical_settlement ? "physical" : "cash"}
                  </span>
                </td>
              </tr>
            ))}
            {!rows.length ? <tr><td colSpan={7} className="py-3 text-center text-text-muted">No contract samples available.</td></tr> : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ResearchStageTable({ rows }: { rows?: Array<{ stage?: number; name?: string; status?: string; detail?: string }> }) {
  const items = rows || [];
  return (
    <div className="rounded-lg border border-bg-border/70 bg-bg-primary/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-text-primary">Build And Test Gates</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-text-muted">{items.length} live statuses</div>
      </div>
      <div className="mt-2 overflow-auto">
        <table className="w-full min-w-[720px] text-[11px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="py-1.5 pr-2 text-left">Stage</th>
              <th className="py-1.5 pr-2 text-left">Module</th>
              <th className="py-1.5 pr-2 text-left">Status</th>
              <th className="py-1.5 text-left">Live Detail</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={`${item.stage}-${item.name}`} className="border-b border-bg-border/20 align-top">
                <td className="py-1.5 pr-2 font-mono text-accent-blue">{item.stage ?? "--"}</td>
                <td className="py-1.5 pr-2 font-semibold text-text-primary">{item.name || "--"}</td>
                <td className="py-1.5 pr-2">
                  <span className={clsx("font-mono text-[10px] uppercase", qualityTone(item.status))}>{item.status || "--"}</span>
                </td>
                <td className="py-1.5 text-text-muted">{item.detail || "Status computed from current data coverage."}</td>
              </tr>
            ))}
            {!items.length ? <tr><td colSpan={4} className="py-3 text-center text-text-muted">No stage statuses returned.</td></tr> : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ResearchViewTabButton({
  tab,
  active,
  onClick,
}: {
  tab: { value: ResearchViewTab; label: string; detail: string };
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "min-h-[58px] rounded-lg border px-3 py-2 text-left transition-colors",
        active
          ? "border-accent-blue bg-accent-blue/10 text-text-primary shadow-[0_0_0_1px_rgba(74,144,226,0.25)]"
          : "border-bg-border bg-bg-secondary/25 text-text-muted hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.12em]">{tab.label}</div>
      <div className="mt-1 text-[10px] leading-snug">{tab.detail}</div>
    </button>
  );
}

function ResearchMetricGrid({
  payload,
  isLoading,
  nseSummary,
  mcxSummary,
  nseOptionSummary,
  mcxOptionSummary,
}: {
  payload?: FnoAnalyticsPayload;
  isLoading: boolean;
  nseSummary: Record<string, any>;
  mcxSummary: Record<string, any>;
  nseOptionSummary: Record<string, any>;
  mcxOptionSummary: Record<string, any>;
}) {
  return (
    <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-8">
      <MetricTile label="NSE Contracts" value={String(nseSummary.total_contracts ?? "--")} detail={`${nseSummary.underlyings ?? 0} underlyings`} />
      <MetricTile label="NSE Options" value={String(nseSummary.option_contracts ?? "--")} detail={`${nseSummary.ce_contracts ?? 0}/${nseSummary.pe_contracts ?? 0} CE/PE`} />
      <MetricTile label="NSE PCR OI" value={formatNumber(nseOptionSummary.pcr_oi)} detail={`${nseOptionSummary.total_underlyings ?? 0} underlyings`} />
      <MetricTile label="NSE Avg IV" value={formatNumber(nseOptionSummary.average_iv)} detail={String(payload?.nse?.option_chain?.status || "snapshot")} />
      <MetricTile label="MCX Contracts" value={String(mcxSummary.total_contracts ?? "--")} detail={`${mcxSummary.underlyings ?? 0} underlyings`} />
      <MetricTile label="MCX Options" value={String(mcxSummary.option_contracts ?? "--")} detail={`${mcxSummary.ce_contracts ?? 0}/${mcxSummary.pe_contracts ?? 0} CE/PE`} />
      <MetricTile label="MCX ATM Rows" value={String(mcxOptionSummary.rows ?? "--")} detail={`CE ${mcxOptionSummary.ce_ready ?? 0} · PE ${mcxOptionSummary.pe_ready ?? 0}`} />
      <MetricTile label="Quality" value={payload?.status || (isLoading ? "loading" : "--")} tone={qualityTone(payload?.status)} />
    </div>
  );
}

function QualityChecksPanel({ payload, isLoading }: { payload?: FnoAnalyticsPayload; isLoading: boolean }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/25 p-3">
      <div className="text-sm font-semibold text-text-primary">Data Quality Checks</div>
      <div className="mt-3 space-y-2">
        {(payload?.quality_checks || []).map((check) => (
          <div key={check.key || check.label} className="rounded-lg border border-bg-border/70 bg-bg-primary/30 px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs font-semibold text-text-primary">{check.label}</span>
              <span className={clsx("font-mono text-[11px] uppercase", qualityTone(check.status))}>{check.status || "--"}</span>
            </div>
            <div className="mt-1 text-[11px] leading-5 text-text-muted">{check.detail}</div>
          </div>
        ))}
        {!payload?.quality_checks?.length ? (
          <div className="text-xs text-text-muted">{isLoading ? "Loading local F&O analytics..." : "No quality checks returned."}</div>
        ) : null}
      </div>
    </div>
  );
}

function SignalWatchPanel({
  nseSignals,
  mcxSignals,
  title = "Risk And Signal Watch",
}: {
  nseSignals: Record<string, any>;
  mcxSignals: Record<string, any>;
  title?: string;
}) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/25 p-3">
      <div className="text-sm font-semibold text-text-primary">{title}</div>
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <SignalList title="NSE OI Change" rows={nseSignals.oi_change_contracts || []} />
        <SignalList title="NSE Volatility" rows={nseSignals.volatility_watch || []} />
        <SignalList title="MCX Devolvement" rows={mcxSignals.devolvement_watch || []} />
        <SignalList title="MCX Spread" rows={mcxSignals.spread_watch || []} />
      </div>
    </div>
  );
}

function ResearchAnalyticsPanel({
  payload,
  isLoading,
  error,
}: {
  payload?: FnoAnalyticsPayload;
  isLoading: boolean;
  error?: unknown;
}) {
  const nseSummary = payload?.nse?.contract_master?.summary || {};
  const mcxSummary = payload?.mcx?.contract_master?.summary || {};
  const nseOptionSummary = payload?.nse?.option_chain?.summary || {};
  const mcxOptionSummary = payload?.mcx?.option_chain || {};
  const nseSignals = payload?.signals?.nse || {};
  const mcxSignals = payload?.signals?.mcx || {};
  const [activeResearchTab, setActiveResearchTab] = useState<ResearchViewTab>("overview");
  const nseGreeks = payload?.nse?.greeks?.rows || [];
  const mcxGreeks = payload?.mcx?.greeks?.rows || [];
  const oiMatrix = payload?.nse?.oi_price_signals || {};
  const curves = payload?.mcx?.futures_curve?.curves || [];
  const spreads = payload?.mcx?.futures_curve?.calendar_spreads || [];
  const positioning = payload?.mcx?.positioning?.strikes || [];
  const nseStraddles = payload?.nse?.straddle_summary || [];
  const mcxStraddles = payload?.mcx?.straddle_summary || [];
  const nseMaxPain = payload?.nse?.max_pain || [];
  const mcxMaxPain = payload?.mcx?.max_pain || [];

  const renderActiveResearchTab = () => {
    switch (activeResearchTab) {
      case "option_chain":
        return (
          <div className="space-y-3">
            <div className="grid gap-3 xl:grid-cols-2">
              <StraddleTable rows={nseStraddles} title="NSE ATM Straddle & Expected Move" />
              <StraddleTable rows={mcxStraddles} title="MCX ATM Straddle & Expected Move" />
            </div>
            <div className="grid gap-3 xl:grid-cols-2">
              <MaxPainTable rows={nseMaxPain} title="NSE Max Pain · Support/Resistance · Chain PCR" />
              <MaxPainTable rows={mcxMaxPain} title="MCX Max Pain · Support/Resistance · Chain PCR" />
            </div>
          </div>
        );
      case "open_interest":
        return (
          <div className="space-y-3">
            <OiPriceMatrixCard payload={oiMatrix} />
            <div className="grid gap-3 xl:grid-cols-2">
              <MaxPainTable rows={nseMaxPain} title="NSE OI Concentration" />
              <MaxPainTable rows={mcxMaxPain} title="MCX OI Concentration" />
            </div>
          </div>
        );
      case "scalper":
        return (
          <div className="space-y-3">
            <SignalWatchPanel title="Scalper Watch" nseSignals={nseSignals} mcxSignals={mcxSignals} />
            <div className="grid gap-3 xl:grid-cols-[1.4fr_1fr]">
              <OiPriceMatrixCard payload={oiMatrix} />
              <StrikePositioningCard rows={positioning} />
            </div>
          </div>
        );
      case "idea":
        return (
          <div className="space-y-3">
            <ResearchModuleGrid payload={payload} />
            <SignalWatchPanel title="Idea Feed" nseSignals={nseSignals} mcxSignals={mcxSignals} />
          </div>
        );
      case "fo_stats":
        return (
          <div className="space-y-3">
            <div className="grid gap-3 xl:grid-cols-2">
              <GreeksTable rows={nseGreeks} title="NSE Option Greeks (top ATM)" mode={payload?.nse?.greeks?.mode} />
              <GreeksTable rows={mcxGreeks} title="MCX Option Greeks (top ATM)" mode={payload?.mcx?.greeks?.mode} />
            </div>
            <div className="grid gap-3 xl:grid-cols-2">
              <StraddleTable rows={nseStraddles} title="NSE Volatility Stats" />
              <StraddleTable rows={mcxStraddles} title="MCX Volatility Stats" />
            </div>
          </div>
        );
      case "strategy_chart":
        return (
          <div className="grid gap-3 xl:grid-cols-[1.4fr_1fr]">
            <FuturesCurveCard curves={curves} spreads={spreads} />
            <StrikePositioningCard rows={positioning} />
          </div>
        );
      case "strategy_builder":
        return (
          <div className="space-y-3">
            <FoRiskCard payload={payload?.fo_risk} />
            <div className="grid gap-3 xl:grid-cols-2">
              <MaxPainTable rows={nseMaxPain} title="NSE Structure Inputs" />
              <MaxPainTable rows={mcxMaxPain} title="MCX Structure Inputs" />
            </div>
            <ResearchStageTable rows={payload?.stage_status} />
          </div>
        );
      case "company_info":
        return (
          <div className="space-y-3">
            <ResearchModuleGrid payload={payload} />
            <div className="grid gap-3 xl:grid-cols-2">
              <ContractSampleTable title="NSE Contract Master Sample" rows={payload?.nse?.contract_master?.sample || []} />
              <ContractSampleTable title="MCX Contract Master Sample" rows={payload?.mcx?.contract_master?.sample || []} />
            </div>
            <ResearchStageTable rows={payload?.stage_status} />
          </div>
        );
      case "overview":
      default:
        return (
          <div className="space-y-3">
            <ResearchMetricGrid
              payload={payload}
              isLoading={isLoading}
              nseSummary={nseSummary}
              mcxSummary={mcxSummary}
              nseOptionSummary={nseOptionSummary}
              mcxOptionSummary={mcxOptionSummary}
            />
            <div className="grid gap-3 xl:grid-cols-[0.85fr_1.15fr]">
              <QualityChecksPanel payload={payload} isLoading={isLoading} />
              <SignalWatchPanel nseSignals={nseSignals} mcxSignals={mcxSignals} />
            </div>
            <ResearchModuleGrid payload={payload} />
          </div>
        );
    }
  };

  return (
    <div className="mt-3 space-y-3">
      <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
        <PanelHeader
          icon={<Activity size={16} className="text-accent-green" />}
          title="NSE + MCX Research"
          detail="FNO 360-style research workspace using local NSE and MCX contract, option, OI, volatility and risk analytics."
          meta={
            payload?.as_of
              ? `as of ${formatTimestamp(payload.as_of)}${error ? " · stale, refresh pending" : ""}`
              : isLoading
              ? "loading"
              : "local"
          }
        />
        {error && !payload ? (
          <div className="mt-3 rounded-lg border border-accent-red/25 bg-accent-red/8 px-3 py-2 text-xs text-accent-red">
            Unable to load `/api/market/fno-analytics`. The next refresh tick will retry automatically.
          </div>
        ) : null}
        {error && payload ? (
          <div className="mt-3 rounded-lg border border-accent-amber/25 bg-accent-amber/8 px-3 py-2 text-xs text-accent-amber">
            Latest refresh failed; showing cached payload from {formatTimestamp(payload.as_of)}. Auto-retry on next tick.
          </div>
        ) : null}
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-9">
          {RESEARCH_VIEW_TABS.map((tab) => (
            <ResearchViewTabButton
              key={tab.value}
              tab={tab}
              active={activeResearchTab === tab.value}
              onClick={() => setActiveResearchTab(tab.value)}
            />
          ))}
        </div>
        <div className="mt-3">{renderActiveResearchTab()}</div>
      </div>

      <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
        <PanelHeader
          icon={<Activity size={16} className="text-accent-green" />}
          title="Live F&O Analytics"
          detail="Contract master, option-chain, data-quality and risk analytics generated from local NSE/MCX data sources."
          meta={
            payload?.as_of
              ? `as of ${formatTimestamp(payload.as_of)}${error ? " · stale, refresh pending" : ""}`
              : isLoading
              ? "loading"
              : "local"
          }
        />
        <div className="mt-3">
          <ResearchMetricGrid
            payload={payload}
            isLoading={isLoading}
            nseSummary={nseSummary}
            mcxSummary={mcxSummary}
            nseOptionSummary={nseOptionSummary}
            mcxOptionSummary={mcxOptionSummary}
          />
        </div>
        <div className="mt-3 grid gap-3 xl:grid-cols-[0.85fr_1.15fr]">
          <QualityChecksPanel payload={payload} isLoading={isLoading} />
          <SignalWatchPanel nseSignals={nseSignals} mcxSignals={mcxSignals} />
        </div>
      </div>

      <OptionsAnalyticsSection payload={payload} isLoading={isLoading} />

      <ResearchModuleGrid payload={payload} />

      <div className="grid gap-3 xl:grid-cols-2">
        <ContractSampleTable title="NSE Contract Master Sample" rows={payload?.nse?.contract_master?.sample || []} />
        <ContractSampleTable title="MCX Contract Master Sample" rows={payload?.mcx?.contract_master?.sample || []} />
      </div>

      <ResearchStageTable rows={payload?.stage_status} />
    </div>
  );
}

function LiveTabButton({
  active,
  label,
  detail,
  onClick,
}: {
  active: boolean;
  label: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "min-h-[58px] rounded-lg border px-3 py-2 text-left transition-colors",
        active
          ? "border-accent-blue bg-accent-blue/10 text-text-primary"
          : "border-bg-border bg-bg-primary/25 text-text-muted hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.12em]">{label}</div>
      <div className="mt-1 text-[11px] leading-snug">{detail}</div>
    </button>
  );
}

function LiveMarketTools() {
  const [symbol, setSymbol] = useState<MarketIndexSymbol>("NSE:NIFTY50-INDEX");
  const [expiry, setExpiry] = useState("");
  const [activeTab, setActiveTab] = useState<LiveMarketTab>("chain");
  const [sectorPeriod, setSectorPeriod] = useState<SectorPeriod>("daily");
  const [selectedRrgSectorCode, setSelectedRrgSectorCode] = useState<string | null>(null);
  const selectedTick = useTickSymbol(symbol);

  const expiriesQuery = useQuery<ExpiryPayload>({
    queryKey: ["marketOptionExpiries", symbol],
    queryFn: () => getOptionExpiries(symbol).then((response) => response.data),
    staleTime: 60_000,
    refetchInterval: 60_000,
  });

  const chainQuery = useQuery<OptionChainPayload>({
    queryKey: ["marketOptionChain", symbol, expiry],
    queryFn: () => getOptionChain(symbol, expiry || undefined).then((response) => response.data),
    staleTime: 10_000,
    refetchInterval: 30_000,
  });

  const watchlistQuery = useQuery<AtmWatchlistPayload>({
    queryKey: ["marketAtmWatchlist", expiry, "live-refresh"],
    queryFn: () => getATMWatchlist(expiry || undefined, true).then((response) => response.data),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const sectorQuery = useQuery<SectorRotationPayload>({
    queryKey: ["marketSectorRotation", sectorPeriod],
    queryFn: () => getSectorRotation(sectorPeriod).then((response) => response.data),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const componentQuery = useQuery<SectorComponentsPayload>({
    queryKey: ["marketSectorRotationComponents", selectedRrgSectorCode, sectorPeriod],
    queryFn: () => getSectorRotationComponents(selectedRrgSectorCode || "", sectorPeriod).then((response) => response.data),
    staleTime: 30_000,
    refetchInterval: selectedRrgSectorCode ? 60_000 : false,
    enabled: Boolean(selectedRrgSectorCode),
  });

  const fnoAnalyticsQuery = useQuery<FnoAnalyticsPayload>({
    queryKey: ["marketFnoAnalytics"],
    queryFn: () => getFnoAnalytics(20).then((response) => response.data),
    staleTime: 30_000,
    // Faster refetch (30s) when on the research tab so a transient
    // backend restart no longer leaves the dashboard stuck on
    // "Unable to load…" for two minutes.
    refetchInterval: activeTab === "research" ? 30_000 : false,
    refetchOnWindowFocus: true,
    retry: 2,
    retryDelay: 2_000,
    enabled: activeTab === "research",
  });

  useEffect(() => {
    const available = expiriesQuery.data?.expiries || [];
    const nextExpiry = expiriesQuery.data?.default_expiry || available[0] || watchlistQuery.data?.expiry || "";
    if (nextExpiry && (!expiry || !available.includes(expiry))) {
      setExpiry(nextExpiry);
    }
  }, [expiry, expiriesQuery.data, watchlistQuery.data?.expiry]);

  const chain = chainQuery.data;
  const entries = chain?.entries || [];
  const ceEntries = entries.filter((entry) => entry.option_type === "CE");
  const peEntries = entries.filter((entry) => entry.option_type === "PE");
  const strikes = Array.from(new Set(entries.map((entry) => entry.strike))).sort((left, right) => left - right);
  const atmIndex = Math.max(0, strikes.findIndex((strike) => strike === chain?.atm_strike));
  const visibleStrikes = strikes.length
    ? strikes.slice(Math.max(0, atmIndex - 6), Math.min(strikes.length, atmIndex + 7))
    : [];
  const watchlistRows = watchlistQuery.data?.rows || [];
  const sectorRows = sectorQuery.data?.watchlist || [];
  const embeddedRrgSector = selectedRrgSectorCode ? sectorQuery.data?.stocks_by_sector?.[selectedRrgSectorCode] : null;
  const selectedRrgSector = componentQuery.data
    ? {
        sector: componentQuery.data.sector || undefined,
        stocks: componentQuery.data.stocks || [],
        rrg: componentQuery.data.rrg || { points: [] },
        source: componentQuery.data.source,
        configured_members: componentQuery.data.configured_members,
        available_members: componentQuery.data.available_members,
        detail: componentQuery.data.detail,
      }
    : embeddedRrgSector;
  const selectedRrgSectorMeta = selectedRrgSector?.sector || sectorRows.find((sector) => sector.code === selectedRrgSectorCode);
  const componentRows = selectedRrgSector?.stocks || [];
  const componentPoints = selectedRrgSector?.rrg?.points || componentRows;
  const isComponentRrg = Boolean(selectedRrgSectorCode);
  const rrgRows = isComponentRrg ? componentRows : sectorRows;
  const rrgPoints = isComponentRrg ? componentPoints : sectorQuery.data?.rrg?.points || sectorRows;
  const sectorPeriodLabel = sectorQuery.data?.period_label || SECTOR_PERIOD_OPTIONS.find((option) => option.value === sectorPeriod)?.label || "Daily";
  const sectorQuadrants = getQuadrantCounts(sectorRows);
  const rrgQuadrants = getQuadrantCounts(rrgRows);
  const spot = selectedTick?.ltp || chain?.spot_price || 0;

  return (
    <section className="rounded-xl border border-bg-active/60 bg-bg-secondary/25 p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-mono text-lg font-semibold text-text-primary">
            <BarChart3 size={17} className="text-accent-green" />
            Live Market Intelligence
          </div>
          <div className="mt-1 text-xs text-text-muted">
            Option chain, CE/PE ATM watchlist, sector rotation, RRG and NSE + MCX research architecture are separate detailed modules.
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={sectorPeriod}
            onChange={(event) => setSectorPeriod(event.target.value as SectorPeriod)}
            className="terminal-input min-w-[132px] py-1.5 text-xs"
            title="Sector lead period"
          >
            {SECTOR_PERIOD_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label} leads</option>
            ))}
          </select>
          <select
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            className="terminal-input min-w-[150px] py-1.5 text-xs"
          >
            {(expiriesQuery.data?.expiries || []).map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
            {!expiriesQuery.data?.expiries?.length ? <option value={watchlistQuery.data?.expiry || ""}>{watchlistQuery.data?.expiry || "Expiry loading"}</option> : null}
          </select>
          <button
            type="button"
            onClick={() => {
              void expiriesQuery.refetch();
              void chainQuery.refetch();
              void watchlistQuery.refetch();
              void sectorQuery.refetch();
              if (activeTab === "research") {
                void fnoAnalyticsQuery.refetch();
              }
            }}
            className="inline-flex items-center gap-2 rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs text-text-secondary transition-colors hover:text-text-primary"
          >
            <RefreshCw size={14} className={chainQuery.isFetching || sectorQuery.isFetching ? "animate-spin" : ""} />
            Refresh live tools
          </button>
        </div>
      </div>

      <div className="mt-3 grid gap-2 xl:grid-cols-5">
        {MARKET_INDEX_SYMBOLS.map((item) => (
          <LiveIndexButton key={item} symbol={item} active={symbol === item} onSelect={setSymbol} />
        ))}
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-5">
        <LiveTabButton
          active={activeTab === "chain"}
          label="Option Chain"
          detail={`${visibleStrikes.length || 0} strikes · PCR ${formatNumber(chain?.pcr_oi)} · ATM ${formatNumber(chain?.atm_strike, 0)}`}
          onClick={() => setActiveTab("chain")}
        />
        <LiveTabButton
          active={activeTab === "watchlist"}
          label="CE/PE Watchlist"
          detail={`${watchlistRows.length} rows · live refresh · expiry ${watchlistQuery.data?.expiry || expiry || "--"}`}
          onClick={() => setActiveTab("watchlist")}
        />
        <LiveTabButton
          active={activeTab === "sectors"}
          label="Sector Rotation"
          detail={`${sectorRows.length} sectors · L ${sectorQuadrants.leading} · I ${sectorQuadrants.improving} · D ${sectorQuadrants.deteriorating} · Lag ${sectorQuadrants.lagging}`}
          onClick={() => setActiveTab("sectors")}
        />
        <LiveTabButton
          active={activeTab === "rrg"}
          label="RRG"
          detail={isComponentRrg && selectedRrgSectorMeta
            ? `${selectedRrgSectorMeta.name} · ${componentQuery.isFetching && !rrgPoints.length ? "loading" : `${rrgPoints.length} components`}`
            : `${rrgPoints.length} points · ${sectorPeriodLabel.toLowerCase()} lead map`}
          onClick={() => setActiveTab("rrg")}
        />
        <LiveTabButton
          active={activeTab === "research"}
          label="NSE + MCX Research"
          detail="Contract master, data quality, risk, analytics, signals and staged test gates"
          onClick={() => setActiveTab("research")}
        />
      </div>

      {activeTab === "chain" ? (
        <div className="mt-3 rounded-xl border border-bg-border bg-bg-primary/20 p-3">
          <PanelHeader
            icon={<Activity size={16} className="text-accent-green" />}
            title="Option Chain"
            detail="Live/cached chain with CE and PE ladders around ATM."
            meta={`${getMarketIndexLabel(symbol)} · ${expiry || chain?.expiry || "--"}`}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-6 xl:grid-cols-9">
            <MetricTile label="Spot" value={spot > 0 ? formatNumber(spot) : "--"} />
            <MetricTile label="PCR OI" value={formatNumber(chain?.pcr_oi)} />
            <MetricTile label="PCR Vol" value={formatNumber(chain?.pcr_volume)} />
            <MetricTile label="ATM" value={formatNumber(chain?.atm_strike, 0)} />
            <MetricTile label="ATM IV" value={formatIv(chain?.atm_iv)} />
            <MetricTile label="Max Pain" value={formatNumber(chain?.max_pain, 0)} />
            <MetricTile label="CE OI" value={formatCompact(chain?.total_ce_oi)} />
            <MetricTile label="PE OI" value={formatCompact(chain?.total_pe_oi)} />
            <MetricTile label="ATM Rows" value={String(watchlistRows.length)} detail={watchlistQuery.data?.detail || undefined} />
          </div>
          <div className="mt-3 max-h-[62vh] overflow-auto rounded-lg border border-bg-border">
            <table className="w-full min-w-[1220px] text-xs">
              <thead className="sticky top-0 bg-bg-primary/95 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-right">CE OI</th>
                  <th className="px-3 py-2 text-right">CE Chg OI</th>
                  <th className="px-3 py-2 text-right">CE Vol</th>
                  <th className="px-3 py-2 text-right">CE IV</th>
                  <th className="px-3 py-2 text-right">CE Delta</th>
                  <th className="px-3 py-2 text-right">CE Theta</th>
                  <th className="px-3 py-2 text-right">CE LTP</th>
                  <th className="px-3 py-2 text-center text-accent-amber">Strike</th>
                  <th className="px-3 py-2 text-left">PE LTP</th>
                  <th className="px-3 py-2 text-left">PE Theta</th>
                  <th className="px-3 py-2 text-left">PE Delta</th>
                  <th className="px-3 py-2 text-left">PE IV</th>
                  <th className="px-3 py-2 text-left">PE Vol</th>
                  <th className="px-3 py-2 text-left">PE Chg OI</th>
                  <th className="px-3 py-2 text-left">PE OI</th>
                </tr>
              </thead>
              <tbody>
                {visibleStrikes.map((strike) => {
                  const ce = ceEntries.find((entry) => entry.strike === strike);
                  const pe = peEntries.find((entry) => entry.strike === strike);
                  const isAtm = chain?.atm_strike === strike;
                  return (
                    <tr key={strike} className={clsx("border-t border-bg-border/60", isAtm && "bg-accent-amber/8")}>
                      <td className="px-3 py-2 text-right font-mono text-accent-green">{formatCompact(ce?.oi)}</td>
                      <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(ce?.oi_change))}>{formatCompact(ce?.oi_change)}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatCompact(ce?.volume)}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatIv(ce?.iv)}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatNumber(ce?.delta, 3)}</td>
                      <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(ce?.theta))}>{formatNumber(ce?.theta)}</td>
                      <td className="px-3 py-2 text-right font-mono font-semibold text-accent-green">{formatNumber(ce?.ltp)}</td>
                      <td className={clsx("px-3 py-2 text-center font-mono font-semibold", isAtm ? "text-accent-amber" : "text-text-primary")}>{formatNumber(strike, 0)}</td>
                      <td className="px-3 py-2 text-left font-mono font-semibold text-accent-red">{formatNumber(pe?.ltp)}</td>
                      <td className={clsx("px-3 py-2 text-left font-mono", pnlTone(pe?.theta))}>{formatNumber(pe?.theta)}</td>
                      <td className="px-3 py-2 text-left font-mono">{formatNumber(pe?.delta, 3)}</td>
                      <td className="px-3 py-2 text-left font-mono">{formatIv(pe?.iv)}</td>
                      <td className="px-3 py-2 text-left font-mono">{formatCompact(pe?.volume)}</td>
                      <td className={clsx("px-3 py-2 text-left font-mono", pnlTone(pe?.oi_change))}>{formatCompact(pe?.oi_change)}</td>
                      <td className="px-3 py-2 text-left font-mono text-accent-red">{formatCompact(pe?.oi)}</td>
                    </tr>
                  );
                })}
                {!visibleStrikes.length ? (
                  <tr><td colSpan={15} className="px-3 py-8 text-center text-text-muted">{chain?.error || "No option-chain ladder available for this index/expiry."}</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {activeTab === "watchlist" ? (
        <div className="mt-3 rounded-xl border border-bg-border bg-bg-primary/20 p-3">
          <PanelHeader
            icon={<Database size={16} className="text-accent-blue" />}
            title="CE/PE ATM Watchlist"
            detail="Prepared ATM CE and PE rows from the market watchlist service."
            meta={`${watchlistRows.length} rows · ${watchlistQuery.data?.expiry || expiry || "--"}`}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Expiry" value={watchlistQuery.data?.expiry || expiry || "--"} />
            <MetricTile label="Rows" value={String(watchlistRows.length)} detail={watchlistQuery.data?.detail || undefined} />
            <MetricTile label="Index Rows" value={String(watchlistRows.filter((row) => String(row.kind || "").toLowerCase() === "index").length)} />
            <MetricTile label="Stock Rows" value={String(watchlistRows.filter((row) => String(row.kind || "").toLowerCase() !== "index").length)} />
          </div>
          <div className="mt-3 max-h-[64vh] overflow-auto">
            <AtmWatchlistTable rows={watchlistRows} />
          </div>
        </div>
      ) : null}

      {activeTab === "sectors" ? (
        <div className="mt-3 rounded-xl border border-bg-border bg-bg-primary/20 p-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <PanelHeader
              icon={<BarChart3 size={16} className="text-accent-blue" />}
              title="Sector Rotation"
              detail="Sector relative strength and momentum versus NIFTY 50."
              meta={`${sectorPeriodLabel} · ${sectorQuery.data?.source || "source pending"}`}
            />
            <SectorPeriodSelect value={sectorPeriod} onChange={setSectorPeriod} />
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-3 xl:grid-cols-6">
            <MetricTile label="Benchmark" value={sectorQuery.data?.benchmark?.name || "NIFTY 50"} detail={`${sectorPeriodLabel} ${formatPercent(sectorQuery.data?.benchmark?.tracked_change_pct)} · ${sectorQuery.data?.benchmark?.samples || 0} samples`} />
            <MetricTile label="Sectors" value={String(sectorRows.length)} />
            <MetricTile label="Leading" value={String(sectorQuadrants.leading)} />
            <MetricTile label="Improving" value={String(sectorQuadrants.improving)} detail={sectorQuery.data?.timestamp ? `Updated ${formatTimestamp(sectorQuery.data.timestamp)}` : undefined} />
            <MetricTile label="Deteriorating" value={String(sectorQuadrants.deteriorating)} detail="Weakening quadrant" />
            <MetricTile label="Lagging" value={String(sectorQuadrants.lagging)} />
          </div>
          <div className="mt-3 max-h-[64vh] overflow-auto rounded-lg border border-bg-border">
            <table className="w-full min-w-[980px] text-xs">
              <thead className="sticky top-0 bg-bg-primary/95 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Sector</th>
                  <th className="px-3 py-2 text-left">Symbol</th>
                  <th className="px-3 py-2 text-right">Price</th>
                  <th className="px-3 py-2 text-right">{sectorPeriodLabel}</th>
                  <th className="px-3 py-2 text-right">Lead</th>
                  <th className="px-3 py-2 text-right">Ratio</th>
                  <th className="px-3 py-2 text-right">Momentum</th>
                  <th className="px-3 py-2 text-left">Quadrant</th>
                  <th className="px-3 py-2 text-left">Trend</th>
                  <th className="px-3 py-2 text-left">Source</th>
                </tr>
              </thead>
              <tbody>
                {sectorRows.map((sector) => (
                  <tr key={sector.code} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 font-semibold text-text-primary">{sector.name}</td>
                    <td className="px-3 py-2 text-text-muted">{sector.symbol || sector.code}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(sector.price)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(sector.tracked_change_pct))}>{formatPercent(sector.tracked_change_pct)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(sector.relative_strength_pct))}>{formatPercent(sector.relative_strength_pct)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(sector.rrg_ratio)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(sector.rrg_momentum)}</td>
                    <td className="px-3 py-2"><StatusBadge label={prettify(sector.quadrant)} tone={sector.quadrant} /></td>
                    <td className="px-3 py-2 text-text-secondary">{prettify(sector.trend)}</td>
                    <td className="px-3 py-2 text-text-muted">{prettify(sector.series_source)}{sector.samples ? ` · ${sector.samples}` : ""}</td>
                  </tr>
                ))}
                {!sectorRows.length ? (
                  <tr><td colSpan={10} className="px-3 py-8 text-center text-text-muted">{sectorQuery.data?.detail || "No sector rotation rows available."}</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {activeTab === "rrg" ? (
        <div className="mt-3 grid gap-3 xl:grid-cols-[1.45fr_0.75fr]">
          <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <PanelHeader
                icon={<Shield size={16} className="text-accent-green" />}
                title={isComponentRrg && selectedRrgSectorMeta ? `${selectedRrgSectorMeta.name} Component RRG` : "RRG"}
                detail={isComponentRrg && selectedRrgSectorMeta
                  ? `Components plotted against ${selectedRrgSectorMeta.name} for the selected period.`
                  : "Relative Rotation Graph: ratio on X-axis, momentum on Y-axis."}
                meta={isComponentRrg
                  ? `${sectorPeriodLabel} · ${componentQuery.isFetching && !rrgPoints.length ? "loading" : `${rrgPoints.length}/${selectedRrgSector?.configured_members || selectedRrgSector?.available_members || rrgPoints.length} components`}`
                  : `${sectorPeriodLabel} · ${rrgPoints.length} points`}
              />
              <div className="flex flex-wrap items-center gap-2">
                {isComponentRrg ? (
                  <button
                    type="button"
                    onClick={() => setSelectedRrgSectorCode(null)}
                    className="rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs text-text-secondary transition-colors hover:text-text-primary"
                  >
                    Sector RRG
                  </button>
                ) : null}
                <SectorPeriodSelect value={sectorPeriod} onChange={setSectorPeriod} />
              </div>
            </div>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <MetricTile label="Leading" value={String(rrgQuadrants.leading)} />
              <MetricTile label="Improving" value={String(rrgQuadrants.improving)} />
              <MetricTile label="Deteriorating" value={String(rrgQuadrants.deteriorating)} detail="Weakening quadrant" />
              <MetricTile label="Lagging" value={String(rrgQuadrants.lagging)} />
            </div>
            <div className="mt-3 [&>div]:min-h-[560px]">
              <RrgMap
                points={rrgPoints}
                activeCode={selectedRrgSectorCode}
                onPointClick={isComponentRrg ? undefined : (point) => setSelectedRrgSectorCode(point.code)}
              />
            </div>
          </div>
          <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
            <PanelHeader
              icon={<BarChart3 size={16} className="text-accent-blue" />}
              title={isComponentRrg && selectedRrgSectorMeta ? `${selectedRrgSectorMeta.name} Components` : "RRG Ranking"}
              detail={isComponentRrg
                ? "Component stocks sorted by quadrant, relative strength, and momentum."
                : "Sorted by quadrant, relative strength, and momentum. Click a sector to open component RRG."}
              meta={isComponentRrg && selectedRrgSectorMeta
                ? `${selectedRrgSectorMeta.name} · ${selectedRrgSector?.source || "source pending"}`
                : `${sectorQuery.data?.benchmark?.name || "NIFTY 50"} · ${sectorQuery.data?.source || "source pending"}`}
            />
            <div className="mt-3 max-h-[560px] space-y-2 overflow-auto">
              {rrgRows.map((sector) => (
                <button
                  key={sector.code}
                  type="button"
                  onClick={() => {
                    if (!isComponentRrg) setSelectedRrgSectorCode(sector.code);
                  }}
                  className={clsx(
                    "w-full rounded-lg border border-bg-border bg-bg-secondary/25 px-3 py-2 text-left transition-colors",
                    !isComponentRrg && "hover:border-accent-blue/45 hover:bg-accent-blue/8",
                  )}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-text-primary">{sector.name}</div>
                      <div className="mt-1 text-[11px] text-text-muted">{prettify(sector.trend)} · {sectorPeriodLabel} lead {formatPercent(sector.relative_strength_pct)}</div>
                    </div>
                    <StatusBadge label={prettify(sector.quadrant)} tone={sector.quadrant} />
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[11px] text-text-secondary">
                    <span>Ratio {formatNumber(sector.rrg_ratio)}</span>
                    <span>Momentum {formatNumber(sector.rrg_momentum)}</span>
                  </div>
                </button>
              ))}
              {!rrgRows.length ? (
                <div className="text-sm text-text-muted">
                  {isComponentRrg && componentQuery.isFetching
                    ? "Loading component RRG points..."
                    : isComponentRrg && selectedRrgSectorMeta
                      ? selectedRrgSector?.detail || `No component RRG points available for ${selectedRrgSectorMeta.name}.`
                    : sectorQuery.data?.detail || "No RRG ranking available."}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      {activeTab === "research" ? (
        <ResearchAnalyticsPanel
          payload={fnoAnalyticsQuery.data}
          isLoading={fnoAnalyticsQuery.isLoading || fnoAnalyticsQuery.isFetching}
          error={fnoAnalyticsQuery.error}
        />
      ) : null}
    </section>
  );
}

export default function MarketPage() {
  return (
    <div className="mx-auto max-w-[1680px] space-y-3 pb-6">
      <LiveMarketTools />
    </div>
  );
}
