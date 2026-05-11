"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  Brain,
  CheckCircle2,
  Database,
  RefreshCw,
  Shield,
  WifiOff,
} from "lucide-react";

import {
  getATMWatchlist,
  getBrokerStatus,
  getOptionChain,
  getOptionExpiries,
  getSectorRotation,
  getStrategyAgentStatus,
} from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";
import {
  MARKET_INDEX_SYMBOLS,
  type MarketIndexSymbol,
  getMarketIndexLabel,
} from "@/lib/marketSymbols";
import { useStore, useTickSymbol } from "@/store";

type StrategySummary = {
  available_capital?: number | null;
  total_equity?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  day_pnl?: number | null;
  total_trades?: number | null;
  win_rate?: number | null;
  open_positions?: number | null;
  entries?: number | null;
  exits?: number | null;
};

type StrategyPosition = {
  symbol?: string | null;
  underlying?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  entered_at?: string | null;
  price_updated_at?: string | null;
  phase?: string | null;
  signal_reason?: string | null;
  latest_rsi?: number | null;
  entry_iv_pct?: number | null;
  regime?: string | null;
  spot_setup?: string | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
};

type StrategySignal = {
  underlying?: string | null;
  direction?: string | null;
  status?: string | null;
  freshness?: string | null;
  reason?: string | null;
  instruction?: string | null;
  source?: string | null;
  as_of?: string | null;
  signal_date?: string | null;
  trade_date?: string | null;
  spot_price?: number | null;
  atm_strike?: number | null;
  ltp?: number | null;
  iv_pct?: number | null;
  priority_score?: number | null;
  mp_day_type?: string | null;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  option_last_bar_time?: string | null;
  spot_last_time?: string | null;
};

type StrategyTrade = {
  symbol?: string | null;
  action?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
  option_type?: string | null;
};

type StrategyPayload = {
  key?: string | null;
  label?: string | null;
  last_scan_at?: string | null;
  last_message?: string | null;
  summary?: StrategySummary;
  positions?: StrategyPosition[];
  trade_history?: StrategyTrade[];
  signals?: StrategySignal[];
  meta?: Record<string, any>;
};

type StrategyAgentStatus = {
  running?: boolean;
  loop_active?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  last_run_at?: string | null;
  next_scan_at?: string | null;
  last_message?: string | null;
  data_health?: Record<string, any>;
  target_expiry?: string | null;
  strategies?: StrategyPayload[];
};

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
  trail?: Array<{ ratio: number; momentum: number }>;
};

type SectorRotationPayload = {
  benchmark?: {
    name?: string | null;
    tracked_change_pct?: number | null;
  } | null;
  watchlist?: SectorWatchlistRow[];
  rrg?: {
    points?: SectorWatchlistRow[];
  };
  detail?: string | null;
};

type LiveMarketTab = "chain" | "watchlist" | "sectors" | "rrg";

const TRADING_BROKERS = ["fyers", "upstox"];
const BROKER_LABEL: Record<string, string> = {
  fyers: "Fyers",
  upstox: "Upstox",
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatMoney(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 10_00_000) return `${sign}Rs ${(abs / 10_00_000).toFixed(2)}L`;
  if (abs >= 1_000) return `${sign}Rs ${(abs / 1_000).toFixed(1)}K`;
  return `${sign}Rs ${abs.toFixed(0)}`;
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

function BucketBar({ rows }: { rows: StrategySignal[] }) {
  const counts = rows.reduce(
    (acc, row) => {
      const key = String(row.direction || row.status || row.freshness || "other").toUpperCase();
      if (key.includes("CE")) acc.ce += 1;
      else if (key.includes("PE")) acc.pe += 1;
      else if (key.includes("MISSING")) acc.missing += 1;
      else acc.watch += 1;
      return acc;
    },
    { ce: 0, pe: 0, watch: 0, missing: 0 },
  );
  const total = Math.max(1, counts.ce + counts.pe + counts.watch + counts.missing);
  const segments = [
    { label: "CE", value: counts.ce, className: "bg-accent-green" },
    { label: "PE", value: counts.pe, className: "bg-accent-red" },
    { label: "Watch", value: counts.watch, className: "bg-accent-amber" },
    { label: "Missing", value: counts.missing, className: "bg-text-muted/50" },
  ];

  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/25 px-3 py-2" title="Saved signal split by CE, PE, watch and missing lanes">
      <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-[0.08em] text-text-muted">
        <span>Signal Split</span>
        <span className="font-mono">{counts.ce}/{counts.pe}/{counts.watch}/{counts.missing}</span>
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-bg-primary">
        {segments.map((segment) => (
          <div
            key={segment.label}
            className={segment.className}
            style={{ width: `${(segment.value / total) * 100}%` }}
            title={`${segment.label}: ${segment.value}`}
          />
        ))}
      </div>
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

function RrgMap({ points }: { points: SectorWatchlistRow[] }) {
  const xValues = points.map((point) => point.rrg_ratio ?? 100);
  const yValues = points.map((point) => point.rrg_momentum ?? 100);
  const xMin = Math.min(95, ...xValues) - 1;
  const xMax = Math.max(105, ...xValues) + 1;
  const yMin = Math.min(95, ...yValues) - 1;
  const yMax = Math.max(105, ...yValues) + 1;

  return (
    <div className="relative min-h-[340px] overflow-hidden rounded-lg border border-bg-border bg-bg-primary/50">
      <div className="absolute inset-0 grid grid-cols-2 grid-rows-2 text-[10px] uppercase tracking-[0.14em] text-text-muted">
        <div className="border-b border-r border-bg-border/60 p-3">Lagging</div>
        <div className="border-b border-bg-border/60 p-3 text-right">Improving</div>
        <div className="border-r border-bg-border/60 p-3">Weakening</div>
        <div className="p-3 text-right">Leading</div>
      </div>
      <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
        <line x1="50" y1="0" x2="50" y2="100" stroke="#26344d" strokeWidth="0.35" />
        <line x1="0" y1="50" x2="100" y2="50" stroke="#26344d" strokeWidth="0.35" />
      </svg>
      {points.slice(0, 28).map((point) => {
        const left = positionPct(point.rrg_ratio, xMin, xMax);
        const top = 100 - positionPct(point.rrg_momentum, yMin, yMax);
        return (
          <div
            key={point.code}
            title={`${point.name} · ${point.quadrant || "unknown"}`}
            className={clsx(
              "absolute -translate-x-1/2 -translate-y-1/2 rounded-md border px-1.5 py-1 text-[10px] font-semibold uppercase shadow-lg",
              quadrantTone(point.quadrant),
            )}
            style={{ left: `${left}%`, top: `${top}%` }}
          >
            {point.code.replaceAll("_", " ").split(" ").map((part) => part[0]).join("").slice(0, 4)}
          </div>
        );
      })}
      {!points.length ? (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-text-muted">No RRG points available.</div>
      ) : null}
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
    queryKey: ["marketAtmWatchlist", expiry],
    queryFn: () => getATMWatchlist(expiry || undefined).then((response) => response.data),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const sectorQuery = useQuery<SectorRotationPayload>({
    queryKey: ["marketSectorRotation", "daily"],
    queryFn: () => getSectorRotation("daily").then((response) => response.data),
    staleTime: 30_000,
    refetchInterval: 60_000,
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
  const rrgPoints = sectorQuery.data?.rrg?.points || sectorRows;
  const spot = selectedTick?.ltp || chain?.spot_price || 0;

  return (
    <section className="rounded-xl border border-bg-active/60 bg-bg-secondary/25 p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-mono text-lg font-semibold text-text-primary">
            <BarChart3 size={17} className="text-accent-green" />
            Live Market Intelligence
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
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

      <div className="mt-3 grid gap-2 md:grid-cols-4">
        <LiveTabButton
          active={activeTab === "chain"}
          label="Option Chain"
          detail="Strike ladder, PCR, ATM IV, max pain, CE/PE OI."
          onClick={() => setActiveTab("chain")}
        />
        <LiveTabButton
          active={activeTab === "watchlist"}
          label="CE/PE Watchlist"
          detail="Full ATM board across index and stock underlyings."
          onClick={() => setActiveTab("watchlist")}
        />
        <LiveTabButton
          active={activeTab === "sectors"}
          label="Sector Rotation"
          detail="Relative strength, momentum, and quadrant ranking."
          onClick={() => setActiveTab("sectors")}
        />
        <LiveTabButton
          active={activeTab === "rrg"}
          label="RRG"
          detail="Full-width relative rotation map with ranked context."
          onClick={() => setActiveTab("rrg")}
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
          <PanelHeader
            icon={<BarChart3 size={16} className="text-accent-blue" />}
            title="Sector Rotation"
            detail="Sector relative strength and momentum versus NIFTY 50."
            meta={sectorQuery.data?.benchmark?.name || "NIFTY 50"}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Benchmark" value={sectorQuery.data?.benchmark?.name || "NIFTY 50"} detail={formatPercent(sectorQuery.data?.benchmark?.tracked_change_pct)} />
            <MetricTile label="Sectors" value={String(sectorRows.length)} />
            <MetricTile label="Leading" value={String(sectorRows.filter((sector) => sector.quadrant === "leading").length)} />
            <MetricTile label="Improving" value={String(sectorRows.filter((sector) => sector.quadrant === "improving").length)} />
          </div>
          <div className="mt-3 max-h-[64vh] overflow-auto rounded-lg border border-bg-border">
            <table className="w-full min-w-[860px] text-xs">
              <thead className="sticky top-0 bg-bg-primary/95 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Sector</th>
                  <th className="px-3 py-2 text-left">Symbol</th>
                  <th className="px-3 py-2 text-right">Price</th>
                  <th className="px-3 py-2 text-right">Tracked</th>
                  <th className="px-3 py-2 text-right">RS</th>
                  <th className="px-3 py-2 text-right">Ratio</th>
                  <th className="px-3 py-2 text-right">Momentum</th>
                  <th className="px-3 py-2 text-left">Quadrant</th>
                  <th className="px-3 py-2 text-left">Trend</th>
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
                  </tr>
                ))}
                {!sectorRows.length ? (
                  <tr><td colSpan={9} className="px-3 py-8 text-center text-text-muted">{sectorQuery.data?.detail || "No sector rotation rows available."}</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {activeTab === "rrg" ? (
        <div className="mt-3 grid gap-3 xl:grid-cols-[1.45fr_0.75fr]">
          <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
            <PanelHeader
              icon={<Shield size={16} className="text-accent-green" />}
              title="RRG"
              detail="Relative Rotation Graph: ratio on X-axis, momentum on Y-axis."
              meta={`${rrgPoints.length} points`}
            />
            <div className="mt-3 [&>div]:min-h-[560px]">
              <RrgMap points={rrgPoints} />
            </div>
          </div>
          <div className="rounded-xl border border-bg-border bg-bg-primary/20 p-3">
            <PanelHeader
              icon={<BarChart3 size={16} className="text-accent-blue" />}
              title="RRG Ranking"
              detail="Sorted by quadrant, relative strength, and momentum."
              meta={sectorQuery.data?.benchmark?.name || "NIFTY 50"}
            />
            <div className="mt-3 max-h-[560px] space-y-2 overflow-auto">
              {sectorRows.map((sector) => (
                <div key={sector.code} className="rounded-lg border border-bg-border bg-bg-secondary/25 px-3 py-2">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-text-primary">{sector.name}</div>
                      <div className="mt-1 text-[11px] text-text-muted">{prettify(sector.trend)} · RS {formatPercent(sector.relative_strength_pct)}</div>
                    </div>
                    <StatusBadge label={prettify(sector.quadrant)} tone={sector.quadrant} />
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[11px] text-text-secondary">
                    <span>Ratio {formatNumber(sector.rrg_ratio)}</span>
                    <span>Momentum {formatNumber(sector.rrg_momentum)}</span>
                  </div>
                </div>
              ))}
              {!sectorRows.length ? <div className="text-sm text-text-muted">{sectorQuery.data?.detail || "No RRG ranking available."}</div> : null}
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function MarketBrokerState() {
  const layoutBrokerStatuses = useStore((state) => state.brokerStatuses);
  const statusQuery = useQuery<BrokerStatusEntry[]>({
    queryKey: ["brokerHealthBanner"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    refetchInterval: 30_000,
    staleTime: 20_000,
  });

  const entries = statusQuery.data?.length ? statusQuery.data : layoutBrokerStatuses;
  const trading = entries.filter((entry) => TRADING_BROKERS.includes(entry.broker));
  const disconnected = trading.filter((entry) => !isBrokerReady(entry));
  const connected = trading.filter((entry) => isBrokerReady(entry));

  if (disconnected.length === 0 && connected.length > 0) {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-accent-green/20 bg-accent-green/8 px-3 py-2 text-xs text-accent-green">
        <CheckCircle2 size={13} className="shrink-0" />
        <span className="font-medium">Brokers</span>
        <span>{connected.map((entry) => BROKER_LABEL[entry.broker] ?? entry.broker).join(" · ")}</span>
      </div>
    );
  }

  const names = disconnected.map((entry) => BROKER_LABEL[entry.broker] ?? entry.broker).join(", ") || "Fyers, Upstox";
  return (
    <a
      href="/settings"
      className="inline-flex items-center gap-2 rounded-full border border-accent-red/25 bg-accent-red/8 px-3 py-2 text-xs text-accent-red transition-colors hover:opacity-90"
      title="Broker sessions can stay offline on holidays; this page reads saved NSE strategy state."
    >
      <WifiOff size={13} className="shrink-0" />
      <span className="font-semibold">{names} offline</span>
    </a>
  );
}

function findStrategy(status: StrategyAgentStatus | undefined, key: string) {
  return (status?.strategies || []).find((strategy) => strategy.key === key) || null;
}

export default function MarketPage() {
  const statusQuery = useQuery<StrategyAgentStatus>({
    queryKey: ["nseStrategyMarketIntelligence"],
    queryFn: () => getStrategyAgentStatus().then((response) => response.data),
    refetchInterval: 60_000,
    staleTime: 15_000,
    refetchOnWindowFocus: false,
  });

  const status = statusQuery.data;
  const strategy1 = findStrategy(status, "macd_strategy");
  const strategy2 = findStrategy(status, "index_mp_strategy");
  const strategy1Prepared = useMemo<StrategySignal[]>(
    () => (strategy1?.meta?.prepared_watchlist || strategy1?.signals || []) as StrategySignal[],
    [strategy1?.meta?.prepared_watchlist, strategy1?.signals],
  );
  const strategy2Signals = useMemo<StrategySignal[]>(
    () => (strategy2?.signals || []) as StrategySignal[],
    [strategy2?.signals],
  );
  const allPositions = [...(strategy1?.positions || []), ...(strategy2?.positions || [])];
  const marketHealth = status?.data_health?.market_intelligence || strategy1?.meta?.market_intelligence || strategy2?.meta?.market_intelligence || {};
  const brokerSnapshot = status?.data_health?.broker_snapshot || strategy1?.meta?.broker_snapshot || strategy2?.meta?.broker_snapshot || {};
  const strategy2Pipeline = (strategy2?.meta?.pipeline || []) as Array<Record<string, any>>;
  const okPipeline = strategy2Pipeline.filter((row) => row.status === "ok").length;
  const totalUnrealized = (strategy1?.summary?.unrealized_pnl || 0) + (strategy2?.summary?.unrealized_pnl || 0);
  const totalRealized = (strategy1?.summary?.realized_pnl || 0) + (strategy2?.summary?.realized_pnl || 0);

  return (
    <div className="mx-auto max-w-[1680px] space-y-3 pb-6">
      <section className="rounded-xl border border-bg-active/60 bg-bg-secondary/30 px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 font-mono text-xl font-semibold text-text-primary" title="NSE Strategy 1 and Strategy 2 saved intelligence only">
              <Brain size={18} className="text-accent-blue" />
              Market Intelligence
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => void statusQuery.refetch()}
              className="inline-flex items-center gap-2 rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs text-text-secondary transition-colors hover:text-text-primary"
              title="Refresh saved NSE strategy state"
            >
              <RefreshCw size={14} className={statusQuery.isFetching ? "animate-spin" : ""} />
              Refresh
            </button>
            <MarketBrokerState />
          </div>
        </div>

        <div className="mt-3 grid gap-2 md:grid-cols-3 xl:grid-cols-9">
          <MetricTile label="Last Saved" value={formatTimestamp(status?.last_run_at)} detail={status?.last_message || undefined} />
          <MetricTile label="Runtime" value={status?.running ? "Running" : status?.loop_active ? "Loop" : "Idle"} detail={status?.last_message || undefined} />
          <MetricTile label="Broker Gate" value={brokerSnapshot?.broker_ready ? "Ready" : "Offline"} detail={`Fyers ${brokerSnapshot?.fyers_ready ? "ready" : "off"} / Upstox ${brokerSnapshot?.upstox_ready ? "ready" : "off"}`} tone={brokerSnapshot?.broker_ready ? "text-accent-green" : "text-accent-red"} />
          <MetricTile label="S1 Saved" value={String(strategy1Prepared.length)} detail={`${strategy1?.positions?.length || 0} open positions`} />
          <MetricTile label="S2 Lanes" value={String(strategy2Signals.length)} detail={`${okPipeline}/${strategy2Pipeline.length || 0} replay feeds OK`} />
          <MetricTile label="Latest Rows" value={String(marketHealth?.watchlist_rows_latest || strategy1?.meta?.watchlist_rows || 0)} detail={formatTimestamp(marketHealth?.latest_watchlist_time || strategy1?.meta?.latest_watchlist_time)} />
          <MetricTile label="Open Positions" value={String(allPositions.length)} />
          <MetricTile label="Open P&L" value={formatMoney(totalUnrealized)} tone={pnlTone(totalUnrealized)} />
          <MetricTile label="Realized" value={formatMoney(totalRealized)} tone={pnlTone(totalRealized)} />
        </div>
      </section>

      <LiveMarketTools />

      <section className="grid gap-3 xl:grid-cols-2">
        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<Activity size={16} className="text-accent-green" />}
            title="Strategy 1 · 30m ATM MACD"
            detail="Saved Strategy 1 positions and prepared CE/PE candidates from the last usable NSE data."
            meta={prettify(strategy1?.meta?.mode || "waiting")}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Equity" value={formatMoney(strategy1?.summary?.total_equity)} />
            <MetricTile label="Available" value={formatMoney(strategy1?.summary?.available_capital)} />
            <MetricTile label="Open P&L" value={formatMoney(strategy1?.summary?.unrealized_pnl)} tone={pnlTone(strategy1?.summary?.unrealized_pnl)} />
            <BucketBar rows={strategy1Prepared} />
          </div>
          <div className="mt-3 overflow-hidden rounded-lg border border-bg-border">
            <table className="w-full min-w-[980px] text-xs">
              <thead className="bg-bg-primary/60 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Open Position</th>
                  <th className="px-3 py-2 text-right">Qty</th>
                  <th className="px-3 py-2 text-right">Entry</th>
                  <th className="px-3 py-2 text-right">Last</th>
                  <th className="px-3 py-2 text-right">P&L</th>
                  <th className="px-3 py-2 text-right">Return</th>
                  <th className="px-3 py-2 text-left">State</th>
                  <th className="px-3 py-2 text-left">Updated</th>
                </tr>
              </thead>
              <tbody>
                {(strategy1?.positions || []).map((position, index) => (
                  <tr key={position.symbol || `${position.underlying || "position"}-${index}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2">
                      <div className="font-semibold text-text-primary">{position.underlying || position.symbol}</div>
                      <div className="text-[11px] text-text-muted">{position.expiry} {formatNumber(position.strike, 0)} {position.option_type}</div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">{position.qty || "--"}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(position.entry_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(position.current_price)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono font-semibold", pnlTone(position.unrealized_pnl))}>{formatMoney(position.unrealized_pnl)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(position.return_pct))}>{formatSigned(position.return_pct, 2, "%")}</td>
                    <td className="px-3 py-2"><StatusBadge label={prettify(position.phase)} tone={position.phase} /></td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(position.price_updated_at || position.entered_at)}</td>
                  </tr>
                ))}
                {!strategy1?.positions?.length ? (
                  <tr><td colSpan={8} className="px-3 py-6 text-center text-text-muted">No saved open Strategy 1 positions.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<BarChart3 size={16} className="text-accent-blue" />}
            title="Strategy 2 · 5m Index MACD + MP"
            detail="Saved Strategy 2 index lanes, Market Profile replay state and recent ledger."
            meta={prettify(strategy2?.meta?.mode || "waiting")}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Equity" value={formatMoney(strategy2?.summary?.total_equity)} />
            <MetricTile label="Trades" value={String(strategy2?.summary?.total_trades || 0)} />
            <MetricTile label="Realized" value={formatMoney(strategy2?.summary?.realized_pnl)} tone={pnlTone(strategy2?.summary?.realized_pnl)} />
            <BucketBar rows={strategy2Signals} />
          </div>
          <div className="mt-3 overflow-hidden rounded-lg border border-bg-border">
            <table className="w-full min-w-[920px] text-xs">
              <thead className="bg-bg-primary/60 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Lane</th>
                  <th className="px-3 py-2 text-left">Signal</th>
                  <th className="px-3 py-2 text-right">Spot</th>
                  <th className="px-3 py-2 text-right">POC</th>
                  <th className="px-3 py-2 text-right">VAH / VAL</th>
                  <th className="px-3 py-2 text-left">Freshness</th>
                  <th className="px-3 py-2 text-left">Last Bar</th>
                </tr>
              </thead>
              <tbody>
                {strategy2Signals.map((row) => (
                  <tr key={`${row.underlying}-${row.status}-${row.option_last_bar_time}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 font-semibold text-text-primary">{row.underlying || "--"}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {row.direction ? <StatusBadge label={row.direction} tone={row.direction} /> : null}
                        <StatusBadge label={prettify(row.status)} tone={row.status} />
                      </div>
                      <div className="mt-1 truncate text-[11px] text-text-muted" title={row.reason || row.instruction || undefined}>{row.reason || row.instruction || "--"}</div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.spot_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.poc)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.vah)} / {formatNumber(row.val)}</td>
                    <td className="px-3 py-2"><StatusBadge label={prettify(row.freshness)} tone={row.freshness} /></td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(row.option_last_bar_time || row.spot_last_time || row.as_of)}</td>
                  </tr>
                ))}
                {!strategy2Signals.length ? (
                  <tr><td colSpan={7} className="px-3 py-6 text-center text-text-muted">No saved Strategy 2 lanes available.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="grid gap-3 xl:grid-cols-[1.4fr_0.8fr]">
        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<Database size={16} className="text-accent-amber" />}
            title="Strategy 1 Prepared CE/PE List"
            detail="Saved candidate list. It is not regenerated from broker feeds while brokers are disconnected."
            meta={`${strategy1Prepared.length} rows`}
          />
          <div className="mt-3 max-h-[52vh] overflow-auto rounded-lg border border-bg-border">
            <table className="w-full min-w-[1180px] text-xs">
              <thead className="sticky top-0 bg-bg-primary/95 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Underlying</th>
                  <th className="px-3 py-2 text-left">Side</th>
                  <th className="px-3 py-2 text-right">Score</th>
                  <th className="px-3 py-2 text-right">Spot</th>
                  <th className="px-3 py-2 text-right">ATM</th>
                  <th className="px-3 py-2 text-right">LTP</th>
                  <th className="px-3 py-2 text-right">IV</th>
                  <th className="px-3 py-2 text-left">Reason</th>
                  <th className="px-3 py-2 text-left">Saved</th>
                </tr>
              </thead>
              <tbody>
                {strategy1Prepared.map((row) => (
                  <tr key={`${row.underlying}-${row.direction}-${row.priority_score}-${row.as_of}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 font-semibold text-text-primary">{row.underlying || "--"}</td>
                    <td className="px-3 py-2"><StatusBadge label={row.direction || "--"} tone={row.direction} /></td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.priority_score, 2)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.spot_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.atm_strike, 0)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.ltp)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.iv_pct, 1)}%</td>
                    <td className="px-3 py-2 text-text-secondary" title={row.instruction || undefined}>{row.reason || "--"}</td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(row.option_last_bar_time || row.as_of)}</td>
                  </tr>
                ))}
                {!strategy1Prepared.length ? (
                  <tr><td colSpan={9} className="px-3 py-8 text-center text-text-muted">No saved Strategy 1 prepared list returned by the API.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-3">
          <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
            <PanelHeader
              icon={<Shield size={16} className="text-accent-green" />}
              title="Persistence Check"
              detail="Confirms the page is reading saved state and not depending on holiday broker sessions."
            />
            <div className="mt-3 grid gap-2">
              <MetricTile label="Market Mode" value={prettify(strategy1?.meta?.market_state || strategy2?.meta?.market_state || "unknown")} />
              <MetricTile label="Readiness" value={marketHealth?.ready ? "Saved Ready" : "Not Ready"} detail={prettify(marketHealth?.readiness_mode || marketHealth?.execution_mode)} tone={marketHealth?.ready ? "text-accent-green" : "text-accent-red"} />
              <MetricTile label="Latest Session" value={String(marketHealth?.watchlist_rows_latest || 0)} detail={formatTimestamp(marketHealth?.latest_watchlist_time)} />
              <MetricTile label="Today Rows" value={String(marketHealth?.watchlist_rows_today || 0)} detail="Holiday/offline rows can be zero" />
            </div>
          </div>

          <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
            <PanelHeader
              icon={<Database size={16} className="text-accent-blue" />}
              title="Recent Strategy 2 Ledger"
              detail="Recent saved Strategy 2 closed trades."
              meta={`${strategy2?.trade_history?.length || 0} trades`}
            />
            <div className="mt-3 space-y-2">
              {(strategy2?.trade_history || []).slice(0, 5).map((trade) => (
                <div key={`${trade.symbol}-${trade.exit_time}`} className="rounded-lg border border-bg-border bg-bg-primary/25 px-3 py-2 text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-semibold text-text-primary">{trade.symbol || "--"}</div>
                      <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(trade.exit_time || trade.entry_time)} · {trade.qty || "--"} qty</div>
                    </div>
                    <div className={clsx("font-mono font-semibold", pnlTone(trade.pnl))}>{formatMoney(trade.pnl)}</div>
                  </div>
                </div>
              ))}
              {!strategy2?.trade_history?.length ? <div className="text-xs text-text-muted">No saved Strategy 2 closed trades.</div> : null}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
