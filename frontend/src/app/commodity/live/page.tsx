"use client";

import { Fragment, useMemo, useState } from "react";
import Link from "next/link";
import { ChevronDown, ChevronRight, CirclePlay, RefreshCcw, Save } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";

import {
  api as apiClient,
  getCommodityOverview,
  getCommodityOrders,
  getCommodityPositions,
  getCommodityReports,
  getCommodityStrategyContracts,
  getCommodityWatchlistSnapshot,
  runCommodityStrategyOnce,
  startCommodityStrategyAgent,
  updateCommodityStrategyContracts,
} from "@/lib/api";
import {
  createCommodityOverviewSocket,
  createCommodityWatchlistSocket,
} from "@/lib/websocket";

const REFRESH_MS = 4_000;

type TabKey = "watchlist" | "positions" | "history" | "research" | "expiry";
type Bucket = "active" | "ready" | "favourable" | "drifting" | "neutral" | null;

const TABS: { key: TabKey; label: string }[] = [
  { key: "watchlist", label: "Watchlist" },
  { key: "positions", label: "Open Positions" },
  { key: "history", label: "Trade History" },
  { key: "research", label: "Research" },
  { key: "expiry", label: "Expiry Setup" },
];
type Trajectory = "improving" | "stalled" | "deteriorating" | null;

type WatchRow = {
  symbol?: string | null;
  configured_symbol?: string | null;
  active_lookup_symbol?: string | null;
  selected_lookup_symbol?: string | null;
  rollover_detail?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
  indicator_timeframe?: string | null;
  atr?: number | null;
  regime?: string | null;
  mp_day_type?: string | null;
  mp_status?: string | null;
  bar_time?: string | null;
  reason?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  bucket?: Bucket;
  trajectory?: Trajectory;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
  expiry?: string | null;
  ce_symbol?: string | null;
  pe_symbol?: string | null;
  signal_side?: string | null;
  trade_bar_time?: string | null;
  trade_symbol?: string | null;
  ce?: Record<string, unknown> | null;
  pe?: Record<string, unknown> | null;
};

type CommoditySnapshotContract = {
  symbol?: string | null;
  underlying?: string | null;
  lookup_symbol?: string | null;
  active_lookup_symbol?: string | null;
  selected_lookup_symbol?: string | null;
  default_lookup_symbol?: string | null;
  active_expiry?: string | null;
  selected_expiry?: string | null;
  suggested_expiry?: string | null;
  expiries?: string[];
  expiry_mappings?: { expiry?: string; lookup_symbol?: string }[];
  lot_size?: number | null;
  has_options?: boolean | null;
  quote_unit_label?: string | null;
  contract_unit_label?: string | null;
  strategy_title?: string | null;
  selection_policy?: string | null;
  selection_locked?: boolean | null;
  detail?: string | null;
};

type CommodityWatchlistSnapshot = {
  contract_catalog?: {
    contracts?: CommoditySnapshotContract[];
    source?: string | null;
    detail?: string | null;
    timestamp?: string | null;
    summary?: Record<string, unknown>;
  };
  atm_watchlist?: {
    rows?: WatchRow[];
    source?: string | null;
    detail?: string | null;
    timestamp?: string | null;
    summary?: Record<string, unknown>;
  };
};

type CommodityPosition = {
  position_key?: string;
  symbol?: string;
  live_symbol?: string;
  display_name?: string;
  action?: string;
  qty?: number;
  lots?: number;
  entry_price?: number;
  current_price?: number;
  stop_price?: number;
  target_price?: number;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  entered_at?: string;
  regime?: string | null;
  strategy_title?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
};

type AuditEvent = {
  created_at?: string;
  event_type?: string;
  severity?: string;
  message?: string;
  symbol?: string;
  underlying?: string;
  payload?: Record<string, unknown>;
};

type Order = {
  time?: string;
  flow?: string;
  symbol?: string;
  action?: string;
  qty?: number;
  reason?: string;
  fill_price?: number;
};

type TradeRow = {
  symbol?: string;
  action?: string;
  qty?: number;
  entry_price?: number;
  exit_price?: number;
  pnl?: number;
  entry_time?: string;
  exit_time?: string;
  reason?: string;
};

type ReportRow = {
  timestamp?: string;
  total_equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  day_pnl?: number;
  open_positions?: number;
  total_trades?: number;
  win_rate?: number;
  max_drawdown?: number;
};

type DataQualitySymbol = {
  symbol: string;
  stale: boolean;
  flagged: boolean;
  freshest_age_seconds: number;
  freshest_source: string;
};

type DataQualitySnap = {
  overall?: string;
  market_state?: string;
  symbol_count?: number;
  stale_count?: number;
  flagged_count?: number;
  symbol_health?: DataQualitySymbol[];
};

type StatusPayload = {
  enabled?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  loop_active?: boolean;
  start_required?: boolean;
  running?: boolean;
  scan_interval_seconds?: number;
  last_run_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  config?: Record<string, any>;
  strategy_agents?: Record<string, any>[];
  strategies?: Record<string, any>[];
  summary?: Record<string, any>;
  futures_watchlist?: WatchRow[];
  watchlist?: WatchRow[];
  option_watchlist?: WatchRow[];
  positions?: CommodityPosition[];
  trade_history?: TradeRow[];
  orders?: Order[];
  reports?: ReportRow[];
  commentary?: { time?: string; tone?: string; message?: string }[];
  signal_audit?: Record<string, any>[];
  data_health?: Record<string, any>;
};

const BUCKET_COLOR: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300",
  ready: "bg-emerald-500/20 text-emerald-200",
  favourable: "bg-amber-500/15 text-amber-200",
  drifting: "bg-rose-500/15 text-rose-200",
  neutral: "bg-slate-500/15 text-slate-300",
};

const QUIET_ROW = "border-t border-transparent hover:bg-bg-secondary/20";
const QUIET_SURFACE = "rounded-md bg-bg-secondary/15";
const QUIET_TILE = "rounded-md bg-bg-secondary/20";

function formatINR(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return `₹${Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function formatNumber(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatPct(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(decimals)}%`;
}

function timeframeLabel(value: unknown, fallback: string): string {
  const raw = String(value || fallback);
  if (raw === "15minute") return "15m";
  if (raw === "30minute") return "30m";
  return raw.replace("minute", "m");
}

function finiteNumber(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function formatIST(iso: string | null | undefined): string {
  if (!iso) return "--";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function trajectoryGlyph(t: Trajectory): string {
  if (t === "improving") return "▲";
  if (t === "deteriorating") return "▼";
  if (t === "stalled") return "▬";
  return "·";
}

function trajectoryColor(t: Trajectory): string {
  if (t === "improving") return "text-emerald-400";
  if (t === "deteriorating") return "text-rose-400";
  return "text-slate-400";
}

function severityColor(sev: string | undefined): string {
  switch ((sev || "").toLowerCase()) {
    case "error":
      return "text-rose-400";
    case "warning":
      return "text-amber-300";
    case "success":
      return "text-emerald-300";
    case "trade":
      return "text-sky-300";
    default:
      return "text-slate-400";
  }
}

function snapshotContractToWatchRow(contract: CommoditySnapshotContract): WatchRow {
  const symbol =
    contract.selected_lookup_symbol ||
    contract.active_lookup_symbol ||
    contract.lookup_symbol ||
    contract.default_lookup_symbol ||
    contract.symbol ||
    null;
  const expiry =
    contract.selected_expiry ||
    contract.active_expiry ||
    contract.suggested_expiry ||
    null;
  const detail = contract.detail || "Runtime scan rows are empty; showing the saved MCX contract catalog.";
  const unitParts = [contract.contract_unit_label, contract.quote_unit_label].filter(Boolean);
  return {
    symbol,
    configured_symbol: contract.symbol || null,
    active_lookup_symbol: contract.active_lookup_symbol || null,
    selected_lookup_symbol: contract.selected_lookup_symbol || null,
    underlying: contract.underlying || contract.symbol || null,
    display_name: [contract.underlying || contract.symbol, expiry].filter(Boolean).join(" · "),
    regime: contract.selection_policy || "catalog",
    mp_day_type: unitParts.join(" · ") || null,
    mp_status: contract.has_options ? "options mapped" : "futures only",
    signal_validation: contract.has_options ? "catalog_ready" : "catalog_only",
    signal_validation_detail: detail,
    bucket: "neutral",
    trajectory: "stalled",
    bucket_rationale: detail,
  };
}

function enrichFuturesRowsWithActiveContracts(
  rows: WatchRow[],
  contracts: CommoditySnapshotContract[],
): WatchRow[] {
  if (!rows.length || !contracts.length) return rows;

  const contractsByConfiguredSymbol = new Map(
    contracts
      .filter((contract) => contract.symbol)
      .map((contract) => [String(contract.symbol), contract]),
  );
  const contractsByUnderlying = new Map(
    contracts
      .filter((contract) => contract.underlying)
      .map((contract) => [String(contract.underlying), contract]),
  );

  return rows.map((row) => {
    const configuredSymbol = String(row.configured_symbol || row.symbol || "");
    const contract =
      contractsByConfiguredSymbol.get(configuredSymbol) ||
      contractsByUnderlying.get(String(row.underlying || ""));
    const activeSymbol =
      contract?.selected_lookup_symbol ||
      contract?.active_lookup_symbol ||
      row.active_lookup_symbol ||
      row.selected_lookup_symbol ||
      row.symbol ||
      null;
    if (!contract || !activeSymbol || activeSymbol === row.symbol) return row;

    return {
      ...row,
      symbol: activeSymbol,
      configured_symbol: contract.symbol || row.configured_symbol || row.symbol || null,
      active_lookup_symbol: contract.active_lookup_symbol || row.active_lookup_symbol || null,
      selected_lookup_symbol: contract.selected_lookup_symbol || row.selected_lookup_symbol || null,
      rollover_detail:
        row.rollover_detail ||
        `Showing active futures ${activeSymbol} for configured ${contract.symbol || row.symbol}.`,
    };
  });
}

function Section({
  title,
  detail,
  children,
  className = "",
}: {
  title: string;
  detail?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`${QUIET_SURFACE} p-3 ${className}`}>
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">{title}</h2>
        {detail ? <span className="text-[10.5px] text-text-muted">{detail}</span> : null}
      </div>
      {children}
    </section>
  );
}

function StatTile({
  label,
  value,
  detail,
  tone = "text-text-primary",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className={`${QUIET_TILE} px-3 py-2`}>
      <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={`mt-1 font-mono text-base font-semibold ${tone}`}>{value}</div>
      {detail ? <div className="mt-1 truncate text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

/**
 * Pill-style secondary stat. Used in the chip strip below the main Decision
 * Bar so caps / cooldown / DQ status stay visible without stealing the
 * vertical real estate of full StatTiles.
 */
function Chip({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1 rounded-md bg-bg-secondary/20 px-2 py-1">
      <span className="uppercase tracking-[0.14em] text-[10px] text-text-muted">{label}</span>
      <span className={`font-mono text-[11px] ${tone || "text-text-secondary"}`}>{value}</span>
    </span>
  );
}

function BucketPill({ bucket }: { bucket?: Bucket }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] ${BUCKET_COLOR[bucket || "neutral"] || BUCKET_COLOR.neutral}`}>
      {bucket || "--"}
    </span>
  );
}

function OptionLegSummary({ leg }: { leg?: Record<string, unknown> }) {
  if (!leg) {
    return <span className="text-text-muted">--</span>;
  }
  const liquid = Boolean(leg.is_liquid);
  const oi = Number(leg.oi ?? 0);
  const oiChange = leg.oi_change === null || leg.oi_change === undefined ? null : Number(leg.oi_change);
  const volume = Number(leg.volume ?? 0);
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-end gap-2">
        <span className="font-mono text-text-primary">{String((leg.strike ?? "--") as string | number)}</span>
        <span className={liquid ? "text-emerald-300" : "text-amber-300"}>{liquid ? "liquid" : "thin"}</span>
      </div>
      <div className="font-mono text-[12px] text-text-primary">{formatNumber(Number(leg.live_ltp ?? leg.ltp ?? 0), 2)}</div>
      <div className="text-[10.5px] text-text-muted">
        OI {formatNumber(oi, 0)}
        {oiChange !== null ? (
          <span className={oiChange >= 0 ? "text-emerald-400" : "text-rose-400"}> {oiChange >= 0 ? "+" : ""}{formatNumber(oiChange, 0)}</span>
        ) : null}
        <span> · Vol {formatNumber(volume, 0)}</span>
      </div>
    </div>
  );
}

// Tighter helpers used by the Bloomberg-style table.
const POS = "text-emerald-400";
const NEG = "text-rose-400";
const NEU = "text-text-muted";

function colorForDelta(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return NEU;
  if (n > 0) return POS;
  if (n < 0) return NEG;
  return NEU;
}

function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_00_00_000) return `${(n / 1_00_00_000).toFixed(2)}Cr`;
  if (abs >= 1_00_000) return `${(n / 1_00_000).toFixed(2)}L`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(2)}k`;
  return n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function bucketCode(b?: Bucket): string {
  if (b === "active") return "ACT";
  if (b === "ready") return "RDY";
  if (b === "favourable") return "FAV";
  if (b === "drifting") return "DRF";
  if (b === "neutral") return "NEU";
  return "—";
}

function bucketCellClass(b?: Bucket): string {
  if (b === "active" || b === "ready") return "text-emerald-300";
  if (b === "favourable") return "text-amber-300";
  if (b === "drifting") return "text-rose-300";
  return "text-slate-400";
}

function regimeShort(r?: string | null): string {
  if (!r) return "—";
  const t = String(r).toLowerCase();
  if (t.startsWith("bull")) return "BULL";
  if (t.startsWith("bear")) return "BEAR";
  if (t.startsWith("neutral")) return "NTRL";
  if (t === "dead_zone") return "DEAD";
  if (t === "vol_spike") return "VLSP";
  if (t === "warmup") return "WARM";
  return t.slice(0, 4).toUpperCase();
}

function mpShort(s?: string | null): string {
  if (!s) return "—";
  const t = String(s).toLowerCase();
  // Common MP day types: trend_up, trend_down, balance, balance_above_poc,
  // balance_below_poc, failed_auction_high, failed_auction_low
  if (t === "trend_up") return "↑TRND";
  if (t === "trend_down") return "↓TRND";
  if (t === "balance") return "BAL";
  if (t === "balance_above_poc") return "BAL↑";
  if (t === "balance_below_poc") return "BAL↓";
  if (t === "failed_auction_high") return "FA-H";
  if (t === "failed_auction_low") return "FA-L";
  return t.slice(0, 5).toUpperCase();
}

function sigShort(v?: string | null): string {
  if (!v) return "—";
  const t = String(v).toLowerCase();
  if (t === "ready") return "READY";
  if (t === "waiting_cross") return "WAIT";
  if (t === "mp_conflict") return "MP-X";
  if (t === "mp_pending") return "MP-P";
  if (t === "mp_warming_up") return "MP-W";
  if (t === "warming_up") return "WARM";
  if (t === "position_open") return "OPEN";
  if (t === "data_stale") return "STALE";
  if (t === "blocked_kill_switch") return "KILL";
  if (t === "iv_reject") return "IV✗";
  if (t === "iv_unavailable") return "IV?";
  if (t === "tte_filter") return "TTE✗";
  if (t === "event_window") return "EVT";
  return t.slice(0, 5).toUpperCase();
}

function daysToExpiry(expiry?: string | null): number | null {
  if (!expiry) return null;
  try {
    const d = new Date(`${expiry}T00:00:00+05:30`);
    const today = new Date();
    return Math.max(0, Math.floor((d.getTime() - today.getTime()) / 86_400_000));
  } catch {
    return null;
  }
}

/**
 * Watchlist with summary rows + click-to-expand detail panel. Inspired by
 * TradingView's screener and Zerodha Kite's positions view:
 *   - Primary row shows the 8 columns a trader scans for fast triage
 *     (symbol, bucket, last, Δ%, MACD-hist, regime, MP, signal)
 *   - Click the row (or the chevron) to expand a detail panel below with
 *     CE/PE strikes, ATM Greeks, DTE, proximity, bar time, and rationale
 *   - No horizontal scroll on a standard 1440px laptop screen
 */
function InstrumentWatchlist({
  futuresRows,
  optionRows,
}: {
  futuresRows: WatchRow[];
  optionRows: WatchRow[];
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (futuresRows.length === 0 && optionRows.length === 0) {
    return (
      <div className="px-2 py-8 text-center text-xs text-text-muted">
        No commodity instruments available.
      </div>
    );
  }
  const optionsBySymbol = new Map<string, WatchRow>();
  const addOptionKey = (key: unknown, row: WatchRow) => {
    const normalized = String(key || "").trim();
    if (normalized && !optionsBySymbol.has(normalized)) {
      optionsBySymbol.set(normalized, row);
    }
  };
  for (const row of optionRows) {
    addOptionKey(row.symbol, row);
    addOptionKey(row.configured_symbol, row);
    addOptionKey(row.active_lookup_symbol, row);
    addOptionKey(row.selected_lookup_symbol, row);
    addOptionKey(row.underlying, row);
  }

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const TH =
    "px-2 py-1.5 text-left text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted bg-bg-secondary/40 sticky top-0 z-10";
  const THR = `${TH} text-right`;
  const TD = "px-2 py-1.5 align-middle whitespace-nowrap font-mono text-[12px]";
  const TDR = `${TD} text-right`;

  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-md border border-bg-active/30">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className={`${TH} w-6 px-1`} aria-label="expand" />
              <th className={TH}>Symbol</th>
              <th className={`${TH} w-14`}>Bkt</th>
              <th className={`${THR} w-24`}>Last</th>
              <th className={`${THR} w-20`}>Δ%</th>
              <th className={`${THR} w-20`}>MACD 15m</th>
              <th className={`${THR} w-20`}>Hist</th>
              <th className={`${THR} w-16`}>RSI 15m</th>
              <th className={`${TH} w-16`}>Regime</th>
              <th className={`${TH} w-16`}>MP</th>
              <th className={`${TH} w-16`}>Sig</th>
              <th className={`${THR} w-16`}>DTE</th>
              <th className={`${THR} w-20`}>Bar</th>
            </tr>
          </thead>
          <tbody>
            {futuresRows.map((row) => {
            const key = String(row.symbol || row.underlying || "");
            const isOpen = expanded.has(key);
            const optionRow =
              optionsBySymbol.get(String(row.symbol || "")) ||
              optionsBySymbol.get(String(row.configured_symbol || "")) ||
              optionsBySymbol.get(String(row.active_lookup_symbol || "")) ||
              optionsBySymbol.get(String(row.selected_lookup_symbol || "")) ||
              optionsBySymbol.get(String(row.underlying || ""));
            const ce = (optionRow as Record<string, unknown> | undefined)?.ce as
              | Record<string, unknown>
              | undefined;
            const pe = (optionRow as Record<string, unknown> | undefined)?.pe as
              | Record<string, unknown>
              | undefined;
            const optionExpiry = (optionRow as Record<string, unknown> | undefined)?.expiry as
              | string
              | undefined;
            const dte = daysToExpiry(optionExpiry);
            const chg =
              row.price != null && row.previous_close != null
                ? Number(row.price) - Number(row.previous_close)
                : null;
            const sigVal = row.signal_validation || row.reason;
            return (
              <Fragment key={key}>
                <tr
                  className="cursor-pointer border-t border-bg-active/20 hover:bg-bg-secondary/25"
                  onClick={() => toggle(key)}
                  title={
                    row.bucket_rationale ||
                    row.signal_validation_detail ||
                    row.signal_validation ||
                    ""
                  }
                >
                  <td className="px-1 py-1.5 align-middle text-text-muted">
                    {isOpen ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                  </td>
                  <td className="px-2 py-1.5 align-middle whitespace-nowrap">
                    <span className="font-semibold text-text-primary">
                      {row.display_name || row.underlying || row.symbol}
                    </span>
                    <span className="ml-2 font-mono text-[10px] text-text-muted">
                      {(row.symbol || "").replace(/^MCX:/, "").replace(/FUT$/, "")}
                    </span>
                  </td>
                  <td
                    className={`px-2 py-1.5 align-middle text-[11px] font-semibold ${bucketCellClass(
                      row.bucket ?? null,
                    )}`}
                  >
                    {bucketCode(row.bucket ?? null)}
                  </td>
                  <td className={`${TDR} text-text-primary`}>
                    {formatNumber(row.price, 2)}
                    {chg != null ? (
                      <div className={`text-[10px] font-normal ${colorForDelta(chg)}`}>
                        {(chg >= 0 ? "+" : "") + formatNumber(chg, 2)}
                      </div>
                    ) : null}
                  </td>
                  <td className={`${TDR} ${colorForDelta(row.change_pct)}`}>
                    {formatPct(row.change_pct, 2)}
                  </td>
                  <td className={`${TDR} ${colorForDelta(row.macd)}`}>
                    {formatNumber(row.macd, 2)}
                  </td>
                  <td className={`${TDR} ${colorForDelta(row.macd_histogram)}`}>
                    {formatNumber(row.macd_histogram, 2)}
                  </td>
                  <td className={`${TDR} text-text-secondary`}>
                    {formatNumber(row.rsi, 1)}
                  </td>
                  <td className="px-2 py-1.5 align-middle text-[10.5px] uppercase text-text-secondary">
                    {regimeShort(row.regime)}
                  </td>
                  <td className="px-2 py-1.5 align-middle text-[10.5px] uppercase text-text-secondary">
                    {mpShort(row.mp_day_type || row.mp_status)}
                  </td>
                  <td className="px-2 py-1.5 align-middle text-[10.5px] uppercase text-text-secondary">
                    {sigShort(sigVal)}
                  </td>
                  <td className={`${TDR} text-text-muted`}>
                    {dte == null ? "—" : dte}
                  </td>
                  <td className={`${TDR} text-[10px] text-text-muted`}>
                    {formatIST(row.bar_time)}
                  </td>
                </tr>
                {isOpen ? (
                  <tr className="border-t border-bg-active/15 bg-bg-secondary/15">
                    <td className="px-1" />
                    <td colSpan={12} className="px-2 py-2">
                      <InstrumentDetail row={row} ce={ce} pe={pe} dte={dte} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
            })}
          </tbody>
        </table>
      </div>
      <OptionPairWatchlist rows={optionRows} />
    </div>
  );
}

function OptionPairWatchlist({ rows }: { rows: WatchRow[] }) {
  if (!rows.length) {
    return (
      <div className="rounded-md border border-bg-active/20 px-3 py-4 text-center text-xs text-text-muted">
        No CE/PE option pairs are available from the current commodity setup.
      </div>
    );
  }

  const TH =
    "px-2 py-1.5 text-left text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted bg-bg-secondary/40 sticky top-0 z-10";
  const THR = `${TH} text-right`;
  const TD = "px-2 py-1.5 align-middle whitespace-nowrap font-mono text-[12px]";
  const TDR = `${TD} text-right`;

  return (
    <div className="overflow-hidden rounded-md border border-bg-active/25">
      <div className="border-b border-bg-active/20 px-2 py-1.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-text-muted">
        CE / PE Option Pairs · 30m MACD + RSI
      </div>
      <table className="w-full border-collapse">
        <thead>
          <tr>
            <th className={TH}>Underlying</th>
            <th className={TH}>Signal</th>
            <th className={`${THR} w-20`}>CE Strike</th>
            <th className={`${THR} w-20`}>CE LTP</th>
            <th className={`${THR} w-20`}>CE MACD</th>
            <th className={`${THR} w-16`}>CE RSI</th>
            <th className={`${THR} w-20`}>PE Strike</th>
            <th className={`${THR} w-20`}>PE LTP</th>
            <th className={`${THR} w-20`}>PE MACD</th>
            <th className={`${THR} w-16`}>PE RSI</th>
            <th className={`${THR} w-20`}>Bar</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const ce = (row.ce || {}) as Record<string, unknown>;
            const pe = (row.pe || {}) as Record<string, unknown>;
            const ceTimeframe = timeframeLabel(ce.indicator_timeframe, "30minute");
            const peTimeframe = timeframeLabel(pe.indicator_timeframe, "30minute");
            const key = String(row.symbol || row.underlying || row.ce_symbol || row.pe_symbol);
            return (
              <tr key={key} className="border-t border-bg-active/20 hover:bg-bg-secondary/20">
                <td className="px-2 py-1.5 align-middle">
                  <span className="font-semibold text-text-primary">{row.underlying || row.display_name || row.symbol}</span>
                  <span className="ml-2 font-mono text-[10px] text-text-muted">{row.expiry || "--"}</span>
                </td>
                <td className="px-2 py-1.5 align-middle text-[10.5px] uppercase text-text-secondary">
                  {row.signal_side || sigShort(row.signal_validation || row.signal_validation_detail)}
                </td>
                <td className={`${TDR} text-text-secondary`}>{ce.strike != null ? String(ce.strike) : "--"}</td>
                <td className={`${TDR} text-text-primary`}>{formatNumber(Number(ce.live_ltp ?? ce.ltp), 2)}</td>
                <td className={`${TDR} ${colorForDelta(Number(ce.macd))}`} title={`CE MACD ${ceTimeframe}`}>
                  {formatNumber(Number(ce.macd), 2)}
                </td>
                <td className={`${TDR} text-text-secondary`} title={`CE RSI ${ceTimeframe}`}>
                  {formatNumber(Number(ce.rsi), 1)}
                </td>
                <td className={`${TDR} text-text-secondary`}>{pe.strike != null ? String(pe.strike) : "--"}</td>
                <td className={`${TDR} text-text-primary`}>{formatNumber(Number(pe.live_ltp ?? pe.ltp), 2)}</td>
                <td className={`${TDR} ${colorForDelta(Number(pe.macd))}`} title={`PE MACD ${peTimeframe}`}>
                  {formatNumber(Number(pe.macd), 2)}
                </td>
                <td className={`${TDR} text-text-secondary`} title={`PE RSI ${peTimeframe}`}>
                  {formatNumber(Number(pe.rsi), 1)}
                </td>
                <td className={`${TDR} text-[10px] text-text-muted`}>
                  {formatIST(row.trade_bar_time || String(ce.bar_time || pe.bar_time || ""))}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function InstrumentDetail({
  row,
  ce,
  pe,
  dte,
}: {
  row: WatchRow;
  ce?: Record<string, unknown>;
  pe?: Record<string, unknown>;
  dte: number | null;
}) {
  const ceLtp = Number(ce?.live_ltp ?? ce?.ltp);
  const peLtp = Number(pe?.live_ltp ?? pe?.ltp);
  const ceMacd = Number(ce?.macd);
  const peMacd = Number(pe?.macd);
  const ceRsi = Number(ce?.rsi);
  const peRsi = Number(pe?.rsi);
  const futuresTimeframe = timeframeLabel(row.indicator_timeframe, "15minute");
  const ceTimeframe = timeframeLabel(ce?.indicator_timeframe, "30minute");
  const peTimeframe = timeframeLabel(pe?.indicator_timeframe, "30minute");
  const ceOi = Number(ce?.oi);
  const peOi = Number(pe?.oi);
  const ceOiDelta = ce?.oi_change != null ? Number(ce.oi_change) : null;
  const peOiDelta = pe?.oi_change != null ? Number(pe.oi_change) : null;
  const ceVol = Number(ce?.volume ?? 0);
  const peVol = Number(pe?.volume ?? 0);

  return (
    <div className="grid grid-cols-12 gap-3">
      {/* Futures stats column */}
      <div className="col-span-12 lg:col-span-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted">
          Futures · {futuresTimeframe}
        </div>
        <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
          <dt className="text-text-muted">ATR</dt>
          <dd className="text-right font-mono">{formatNumber(row.atr, 2)}</dd>
          <dt className="text-text-muted">MACD signal</dt>
          <dd className="text-right font-mono">{formatNumber(row.macd_signal, 2)}</dd>
          <dt className="text-text-muted">RSI</dt>
          <dd className="text-right font-mono">{formatNumber(row.rsi, 1)}</dd>
          <dt className="text-text-muted">Proximity</dt>
          <dd className="text-right font-mono">
            {row.proximity_pct == null ? "—" : `${Math.round(row.proximity_pct)}%`}
          </dd>
          <dt className="text-text-muted">Prev close</dt>
          <dd className="text-right font-mono">{formatNumber(row.previous_close, 2)}</dd>
          {row.configured_symbol && row.configured_symbol !== row.symbol ? (
            <>
              <dt className="text-text-muted">Configured</dt>
              <dd className="text-right font-mono text-[10px]">{row.configured_symbol}</dd>
            </>
          ) : null}
          <dt className="text-text-muted">DTE</dt>
          <dd className="text-right font-mono">{dte == null ? "—" : dte}</dd>
          <dt className="text-text-muted">Bar time</dt>
          <dd className="text-right font-mono text-[10px]">{formatIST(row.bar_time)}</dd>
        </dl>
      </div>
      {/* CE leg */}
      <div className="col-span-12 sm:col-span-6 lg:col-span-4">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-300/80">
          CE leg · {ceTimeframe}
        </div>
        {ce ? (
          <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
            <dt className="text-text-muted">Strike</dt>
            <dd className="text-right font-mono">{ce.strike != null ? String(ce.strike) : "—"}</dd>
            <dt className="text-text-muted">LTP</dt>
            <dd className="text-right font-mono text-text-primary">
              {Number.isFinite(ceLtp) ? formatNumber(ceLtp, 2) : "—"}
            </dd>
            <dt className="text-text-muted">MACD</dt>
            <dd className={`text-right font-mono ${colorForDelta(ceMacd)}`}>
              {Number.isFinite(ceMacd) ? formatNumber(ceMacd, 2) : "—"}
            </dd>
            <dt className="text-text-muted">RSI</dt>
            <dd className="text-right font-mono">
              {Number.isFinite(ceRsi) ? formatNumber(ceRsi, 1) : "—"}
            </dd>
            <dt className="text-text-muted">OI</dt>
            <dd className="text-right font-mono">
              {Number.isFinite(ceOi) ? compactNumber(ceOi) : "—"}
              {ceOiDelta != null ? (
                <span className={`ml-1 text-[10px] ${colorForDelta(ceOiDelta)}`}>
                  {(ceOiDelta >= 0 ? "+" : "") + compactNumber(ceOiDelta)}
                </span>
              ) : null}
            </dd>
            <dt className="text-text-muted">Volume</dt>
            <dd className="text-right font-mono">{compactNumber(ceVol)}</dd>
          </dl>
        ) : (
          <div className="mt-1.5 text-[11px] italic text-text-muted">No CE leg.</div>
        )}
      </div>
      {/* PE leg */}
      <div className="col-span-12 sm:col-span-6 lg:col-span-4">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-rose-300/80">
          PE leg · {peTimeframe}
        </div>
        {pe ? (
          <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
            <dt className="text-text-muted">Strike</dt>
            <dd className="text-right font-mono">{pe.strike != null ? String(pe.strike) : "—"}</dd>
            <dt className="text-text-muted">LTP</dt>
            <dd className="text-right font-mono text-text-primary">
              {Number.isFinite(peLtp) ? formatNumber(peLtp, 2) : "—"}
            </dd>
            <dt className="text-text-muted">MACD</dt>
            <dd className={`text-right font-mono ${colorForDelta(peMacd)}`}>
              {Number.isFinite(peMacd) ? formatNumber(peMacd, 2) : "—"}
            </dd>
            <dt className="text-text-muted">RSI</dt>
            <dd className="text-right font-mono">
              {Number.isFinite(peRsi) ? formatNumber(peRsi, 1) : "—"}
            </dd>
            <dt className="text-text-muted">OI</dt>
            <dd className="text-right font-mono">
              {Number.isFinite(peOi) ? compactNumber(peOi) : "—"}
              {peOiDelta != null ? (
                <span className={`ml-1 text-[10px] ${colorForDelta(peOiDelta)}`}>
                  {(peOiDelta >= 0 ? "+" : "") + compactNumber(peOiDelta)}
                </span>
              ) : null}
            </dd>
            <dt className="text-text-muted">Volume</dt>
            <dd className="text-right font-mono">{compactNumber(peVol)}</dd>
          </dl>
        ) : (
          <div className="mt-1.5 text-[11px] italic text-text-muted">No PE leg.</div>
        )}
      </div>
      {/* Rationale column */}
      <div className="col-span-12 lg:col-span-1">
        {row.bucket_rationale || row.signal_validation_detail ? (
          <>
            <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Why
            </div>
            <div className="mt-1.5 text-[10.5px] leading-snug text-text-muted">
              {row.bucket_rationale || row.signal_validation_detail}
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

function PositionsTable({ positions }: { positions: CommodityPosition[] }) {
  if (positions.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No open commodity positions.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1000px] text-xs">
        <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="text-left">Instrument</th>
            <th className="text-left">Side</th>
            <th className="text-right" title="Lots × lot size">Lots</th>
            <th className="text-right" title="Total contracts (lots × lot size)">Qty</th>
            <th className="text-right">Entry</th>
            <th className="text-right">Last</th>
            <th className="text-right">Stop</th>
            <th className="text-right">Target</th>
            <th className="text-right">Unrl P&L</th>
            <th className="text-right">Ret%</th>
            <th className="text-left">Context</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            // Show lots × lot_size so the trader can audit position size at a
            // glance — critical after the SENSEX lot-size bug surfaced. When
            // backend doesn't supply lots explicitly, derive from qty (we
            // know SENSEX=20, NIFTY=65, BANKNIFTY=30, MIDCPNIFTY=120, etc.).
            const qty = Number(p.qty ?? 0);
            const lots = Number(p.lots ?? 0);
            const lotSize = lots > 0 ? Math.round(qty / lots) : null;
            const lotLabel = lots > 0 && lotSize
              ? `${lots} × ${lotSize}`
              : lots > 0
                ? `${lots}`
                : "—";
            return (
              <tr key={p.position_key || p.live_symbol} className={QUIET_ROW}>
                <td className="py-1.5 font-medium">
                  {p.display_name || p.symbol}
                  <div className="text-[10px] text-text-muted">{p.live_symbol}</div>
                </td>
                <td>{p.action}</td>
                <td className="text-right font-mono text-[11px] text-text-muted" title="lots × contract size">
                  {lotLabel}
                </td>
                <td className="text-right font-mono">{qty || "—"}</td>
                <td className="text-right font-mono">{formatNumber(p.entry_price, 2)}</td>
                <td className="text-right font-mono">{formatNumber(p.current_price, 2)}</td>
                <td className="text-right font-mono text-rose-300">{formatNumber(p.stop_price, 2)}</td>
                <td className="text-right font-mono text-emerald-300">{formatNumber(p.target_price, 2)}</td>
                <td className={`text-right font-mono ${(p.unrealized_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {formatINR(p.unrealized_pnl)}
                </td>
                <td className={`text-right font-mono ${(p.return_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {formatPct(p.return_pct, 1)}
                </td>
                <td className="text-[10.5px] text-text-muted">
                  {[p.strategy_title, p.regime, p.expiry].filter(Boolean).join(" · ") || "--"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTable({ orders }: { orders: Order[] }) {
  if (orders.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No commodity orders yet.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] text-xs">
        <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="text-left">Time</th>
            <th className="text-left">Flow</th>
            <th className="text-left">Symbol</th>
            <th className="text-left">Side</th>
            <th className="text-right">Qty</th>
            <th className="text-right">Fill</th>
            <th className="text-left">Reason</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o, idx) => (
            <tr key={`${o.time}-${idx}`} className={QUIET_ROW}>
              <td className="py-1 font-mono text-[10.5px] text-text-muted">{formatIST(o.time)}</td>
              <td className={o.flow === "entry" ? "text-emerald-300" : o.flow === "exit" ? "text-rose-300" : "text-text-muted"}>
                {o.flow || "--"}
              </td>
              <td className="font-mono text-[10.5px]">{o.symbol}</td>
              <td>{o.action}</td>
              <td className="text-right font-mono">{o.qty}</td>
              <td className="text-right font-mono">{formatNumber(o.fill_price, 2)}</td>
              <td className="truncate text-[10.5px] text-text-muted">{o.reason || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradesTable({ trades }: { trades: TradeRow[] }) {
  if (trades.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No closed commodity trades yet.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[860px] text-xs">
        <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="text-left">Exit</th>
            <th className="text-left">Symbol</th>
            <th className="text-left">Side</th>
            <th className="text-right">Qty</th>
            <th className="text-right">Entry</th>
            <th className="text-right">Exit Price</th>
            <th className="text-right">P&L</th>
            <th className="text-left">Reason</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, idx) => (
            <tr key={`${t.symbol}-${t.exit_time}-${idx}`} className={QUIET_ROW}>
              <td className="py-1 font-mono text-[10.5px] text-text-muted">{formatIST(t.exit_time)}</td>
              <td className="font-mono text-[10.5px]">{t.symbol}</td>
              <td>{t.action}</td>
              <td className="text-right font-mono">{t.qty}</td>
              <td className="text-right font-mono">{formatNumber(t.entry_price, 2)}</td>
              <td className="text-right font-mono">{formatNumber(t.exit_price, 2)}</td>
              <td className={`text-right font-mono ${(t.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {formatINR(t.pnl)}
              </td>
              <td className="truncate text-text-muted">{t.reason || "--"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActionQueue({ rows }: { rows: WatchRow[] }) {
  const order: Bucket[] = ["ready", "active", "favourable", "drifting", "neutral"];
  const grouped = order.map((bucket) => ({
    bucket,
    rows: rows.filter((r) => r.bucket === bucket),
  }));
  const titles: Record<NonNullable<Bucket>, string> = {
    ready: "Ready · execute",
    active: "Active · in position",
    favourable: "Favourable · tracking",
    drifting: "Drifting · risk",
    neutral: "Neutral · idle",
  };
  return (
    <div className="space-y-2">
      {grouped.map(({ bucket, rows: bucketRows }) => {
        if (!bucket) return null;
        const label = titles[bucket];
        const tone =
          bucket === "ready" || bucket === "active"
            ? "border-emerald-500/40 bg-emerald-500/5"
            : bucket === "favourable"
              ? "border-amber-500/40 bg-amber-500/5"
              : bucket === "drifting"
                ? "border-rose-500/40 bg-rose-500/5"
                : "border-slate-500/30 bg-slate-500/5";
        return (
          <div key={bucket} className={`rounded-md border ${tone} px-2 py-1.5`}>
            <div className="flex items-baseline justify-between text-[10.5px] uppercase tracking-[0.18em] text-text-muted">
              <span>{label}</span>
              <span>{bucketRows.length}</span>
            </div>
            {bucketRows.length === 0 ? (
              <div className="mt-1 text-[11px] italic text-text-muted">—</div>
            ) : (
              <ul className="mt-1.5 space-y-1 text-xs">
                {bucketRows.map((r) => (
                  <li
                    key={`${bucket}-${r.symbol || r.underlying}`}
                    className="flex items-baseline justify-between gap-2 leading-tight"
                  >
                    <span className="min-w-0 truncate">
                      <span className="font-medium text-text-primary">
                        {r.display_name || r.underlying || r.symbol}
                      </span>
                      <span className="ml-1 text-[10px] text-text-muted">
                        {r.signal_validation || r.reason || "--"}
                      </span>
                    </span>
                    <span className="shrink-0 font-mono text-[10.5px] text-text-muted">
                      <span className={trajectoryColor(r.trajectory ?? null)}>
                        {trajectoryGlyph(r.trajectory ?? null)}
                      </span>{" "}
                      {r.proximity_pct != null ? `${Math.round(r.proximity_pct)}%` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function MiniPositionCard({ p }: { p: CommodityPosition }) {
  const pnl = Number(p.unrealized_pnl ?? 0);
  const ret = Number(p.return_pct ?? 0);
  const ageSec = p.entered_at
    ? Math.max(0, (Date.now() - new Date(p.entered_at).getTime()) / 1000)
    : null;
  const ageLabel = (() => {
    if (ageSec === null) return "—";
    const m = Math.floor(ageSec / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
  })();
  return (
    <div className={`${QUIET_TILE} rounded-md px-3 py-2`}>
      <div className="flex items-baseline justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-medium text-text-primary">
            {p.display_name || p.symbol}
          </div>
          <div className="truncate text-[10px] text-text-muted">{p.live_symbol}</div>
        </div>
        <div className="shrink-0 text-right">
          <div
            className={`font-mono text-sm font-semibold ${pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}
          >
            {formatINR(pnl)}
          </div>
          <div className={`font-mono text-[10.5px] ${ret >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
            {formatPct(ret, 2)}
          </div>
        </div>
      </div>
      <div className="mt-1.5 grid grid-cols-3 gap-1 text-[10.5px] text-text-muted">
        <div>
          <div className="uppercase tracking-wider">side</div>
          <div className="font-mono text-text-primary">
            {p.action} {p.qty}
          </div>
        </div>
        <div>
          <div className="uppercase tracking-wider">entry → now</div>
          <div className="font-mono text-text-primary">
            {formatNumber(p.entry_price, 2)} → {formatNumber(p.current_price, 2)}
          </div>
        </div>
        <div className="text-right">
          <div className="uppercase tracking-wider">age</div>
          <div className="font-mono text-text-primary">{ageLabel}</div>
        </div>
      </div>
      <div className="mt-1 flex items-baseline justify-between text-[10.5px]">
        <span className="text-rose-300 font-mono">stop {formatNumber(p.stop_price, 2)}</span>
        <span className="text-emerald-300 font-mono">tgt {formatNumber(p.target_price, 2)}</span>
      </div>
      {p.regime || p.expiry || p.strategy_title ? (
        <div className="mt-1 truncate text-[10px] text-text-muted">
          {[p.strategy_title, p.regime, p.expiry].filter(Boolean).join(" · ")}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Structured signal-audit table. Replaces the previous raw JSON dump on
 * the Research tab so traders can scan signal-by-signal disposition at a
 * glance. Renders the most relevant audit fields and exposes the full
 * payload behind a click-to-expand row for the rare case it's needed.
 */
function SignalAuditTable({ rows }: { rows: Record<string, any>[] }) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  if (!rows || rows.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No signal audit rows yet.</div>;
  }
  const toggle = (idx: number) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  };
  return (
    <div className="max-h-[420px] overflow-auto rounded-md border border-bg-active/30">
      <table className="w-full border-collapse text-[11.5px]">
        <thead className="sticky top-0 z-10 bg-bg-secondary/40">
          <tr className="text-[10px] uppercase tracking-wider text-text-muted">
            <th className="w-6 px-1 py-1.5" aria-label="expand" />
            <th className="px-2 py-1.5 text-left">Time</th>
            <th className="px-2 py-1.5 text-left">Symbol</th>
            <th className="px-2 py-1.5 text-left">Strategy</th>
            <th className="px-2 py-1.5 text-left">Decision</th>
            <th className="px-2 py-1.5 text-left">Reason</th>
            <th className="px-2 py-1.5 text-right">Score</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => {
            const isOpen = open.has(idx);
            const decision = String(
              row.decision || row.status || row.action || "—",
            ).toLowerCase();
            const decisionTone =
              decision === "ready" || decision === "enter" || decision === "long" || decision === "trade"
                ? "text-emerald-300"
                : decision === "block" || decision === "blocked" || decision === "skip"
                  ? "text-rose-300"
                  : decision === "watch" || decision === "watching"
                    ? "text-amber-300"
                    : "text-text-muted";
            const score = row.priority_score ?? row.score ?? row.confidence ?? null;
            return (
              <Fragment key={`${row.time || row.timestamp || idx}-${idx}`}>
                <tr
                  className="cursor-pointer border-t border-bg-active/15 hover:bg-bg-secondary/20"
                  onClick={() => toggle(idx)}
                >
                  <td className="px-1 py-1 align-middle text-text-muted">
                    {isOpen ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                  </td>
                  <td className="px-2 py-1 font-mono text-[10.5px] text-text-muted">
                    {formatIST(row.time || row.timestamp || row.as_of)}
                  </td>
                  <td className="px-2 py-1 font-mono text-[11px]">
                    {String(row.symbol || row.underlying || "—")}
                  </td>
                  <td className="px-2 py-1 text-text-secondary">
                    {String(row.strategy || row.strategy_key || row.lane || "—")}
                  </td>
                  <td className={`px-2 py-1 font-semibold uppercase ${decisionTone}`}>
                    {decision}
                  </td>
                  <td className="max-w-[28ch] truncate px-2 py-1 text-text-secondary" title={String(row.reason || row.notes || "")}>
                    {String(row.reason || row.notes || "—")}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {score == null || score === "" ? "—" : Number(score).toFixed(2)}
                  </td>
                </tr>
                {isOpen ? (
                  <tr className="border-t border-bg-active/10 bg-bg-secondary/15">
                    <td />
                    <td colSpan={6} className="px-3 py-2">
                      <pre className="overflow-auto rounded-md bg-bg-primary/60 p-2 text-[10.5px] text-text-secondary">
                        {JSON.stringify(row, null, 2)}
                      </pre>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AuditFeed({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No audit events yet.</div>;
  }
  return (
    <ul className="space-y-0.5 text-[11.5px]">
      {events.map((e, idx) => (
        <li key={`${e.created_at}-${idx}`} className="flex items-baseline gap-2 border-b border-transparent py-1 hover:bg-bg-secondary/15">
          <span className="w-[86px] shrink-0 font-mono text-[10.5px] text-text-muted">{formatIST(e.created_at)}</span>
          <span className={`w-[92px] shrink-0 text-[10.5px] uppercase ${severityColor(e.severity)}`}>{e.event_type || "--"}</span>
          <span className="w-[150px] shrink-0 truncate font-mono text-[10.5px] text-text-muted">{e.symbol || e.underlying || "--"}</span>
          <span className="min-w-0 flex-1 truncate text-text-secondary">{e.message || JSON.stringify(e.payload || {})}</span>
        </li>
      ))}
    </ul>
  );
}

export default function CommodityLivePage() {
  const [activeTab, setActiveTab] = useState<TabKey>("watchlist");
  const [expiryDraft, setExpiryDraft] = useState<Record<string, string>>({});
  const queryClient = useQueryClient();

  const overviewQuery = useLiveSnapshotQuery<Record<string, unknown>>({
    queryKey: ["commodity-live", "overview"],
    queryFn: async () => (await getCommodityOverview()).data,
    streamFactory: (onData, onStatusChange) =>
      createCommodityOverviewSocket((payload) => onData(payload as Record<string, unknown>), onStatusChange),
    storageKey: "commodity-live:overview",
    streamWhenHidden: true,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const positionsQuery = useQuery({
    queryKey: ["commodity-live", "positions"],
    queryFn: async () => (await getCommodityPositions()).data,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const ordersQuery = useQuery({
    queryKey: ["commodity-live", "orders"],
    queryFn: async () => (await getCommodityOrders(60)).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const reportsQuery = useQuery({
    queryKey: ["commodity-live", "reports"],
    queryFn: async () => (await getCommodityReports(40)).data,
    refetchInterval: REFRESH_MS * 3,
    refetchIntervalInBackground: true,
  });

  const watchlistSnapshotQuery = useLiveSnapshotQuery<CommodityWatchlistSnapshot>({
    queryKey: ["commodity-live", "watchlist-snapshot"],
    queryFn: async () => (await getCommodityWatchlistSnapshot()).data as CommodityWatchlistSnapshot,
    streamFactory: (onData, onStatusChange) =>
      createCommodityWatchlistSocket((payload) => onData(payload as CommodityWatchlistSnapshot), onStatusChange),
    storageKey: "commodity-live:watchlist-snapshot",
    streamWhenHidden: true,
    refetchInterval: REFRESH_MS * 3,
    refetchIntervalInBackground: true,
  });

  const contractsQuery = useQuery({
    queryKey: ["commodity-live", "contracts"],
    queryFn: async () => (await getCommodityStrategyContracts()).data,
    refetchInterval: REFRESH_MS * 6,
    refetchIntervalInBackground: true,
  });

  const auditQuery = useQuery({
    queryKey: ["commodity-live", "audit"],
    queryFn: async () => (await apiClient.get("/api/audit/events?market=commodity&limit=40")).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const dataQualityQuery = useQuery({
    queryKey: ["commodity-live", "data-quality"],
    queryFn: async () => (await apiClient.get("/api/data-quality/snapshot")).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const saveExpiriesMutation = useMutation({
    mutationFn: async (selected: Record<string, string>) => (await updateCommodityStrategyContracts(selected)).data,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["commodity-live", "contracts"] }),
        queryClient.invalidateQueries({ queryKey: ["commodity-live", "overview"] }),
        queryClient.invalidateQueries({ queryKey: ["commodity-live", "watchlist-snapshot"] }),
      ]);
    },
  });

  const runOnceMutation = useMutation({
    mutationFn: async () => (await runCommodityStrategyOnce(true)).data,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["commodity-live"] });
    },
  });

  const startMutation = useMutation({
    mutationFn: async () => (await startCommodityStrategyAgent()).data,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["commodity-live"] });
    },
  });

  const status = (overviewQuery.data?.status ?? {}) as StatusPayload;
  const summary = status.summary ?? {};
  const config = status.config ?? {};
  const contracts = useMemo(
    () => (contractsQuery.data?.contracts ?? watchlistSnapshotQuery.data?.contract_catalog?.contracts ?? []) as CommoditySnapshotContract[],
    [contractsQuery.data?.contracts, watchlistSnapshotQuery.data?.contract_catalog?.contracts],
  );

  const runtimeFuturesWatchlist = useMemo(
    () => enrichFuturesRowsWithActiveContracts((status.futures_watchlist ?? status.watchlist ?? []) as WatchRow[], contracts),
    [contracts, status.futures_watchlist, status.watchlist],
  );
  const snapshotFuturesWatchlist = useMemo(
    () => (watchlistSnapshotQuery.data?.contract_catalog?.contracts ?? []).map(snapshotContractToWatchRow),
    [watchlistSnapshotQuery.data?.contract_catalog?.contracts],
  );
  const watchlist = useMemo(
    () => (runtimeFuturesWatchlist.length > 0 ? runtimeFuturesWatchlist : snapshotFuturesWatchlist),
    [runtimeFuturesWatchlist, snapshotFuturesWatchlist],
  );
  const runtimeOptionWatchlist = useMemo(
    () => (status.option_watchlist ?? []) as WatchRow[],
    [status.option_watchlist],
  );
  const snapshotOptionWatchlist = useMemo(
    () => (watchlistSnapshotQuery.data?.atm_watchlist?.rows ?? []) as WatchRow[],
    [watchlistSnapshotQuery.data?.atm_watchlist?.rows],
  );
  const optionWatchlist = useMemo(
    () => (runtimeOptionWatchlist.length > 0 ? runtimeOptionWatchlist : snapshotOptionWatchlist),
    [runtimeOptionWatchlist, snapshotOptionWatchlist],
  );

  const streamedPositions = (overviewQuery.data?.positions as CommodityPosition[] | undefined) ?? status.positions;
  const streamedOrders = (overviewQuery.data?.orders as Order[] | undefined) ?? status.orders;
  const streamedReports = (overviewQuery.data?.reports as ReportRow[] | undefined) ?? status.reports;
  const positions = useMemo(
    () => streamedPositions ?? (positionsQuery.data as CommodityPosition[] | undefined) ?? [],
    [positionsQuery.data, streamedPositions],
  );
  const orders = useMemo(
    () => streamedOrders ?? (ordersQuery.data as Order[] | undefined) ?? [],
    [ordersQuery.data, streamedOrders],
  );
  const reports = useMemo(
    () => streamedReports ?? (reportsQuery.data as ReportRow[] | undefined) ?? [],
    [reportsQuery.data, streamedReports],
  );
  const trades = useMemo(() => (status.trade_history ?? []) as TradeRow[], [status.trade_history]);
  const auditEvents = useMemo(() => (auditQuery.data?.events ?? []) as AuditEvent[], [auditQuery.data]);
  const globalDataQuality = useMemo(() => (dataQualityQuery.data ?? {}) as DataQualitySnap, [dataQualityQuery.data]);
  const dataQuality = useMemo(
    () => ((status.data_health?.commodity_data_quality ?? globalDataQuality) as DataQualitySnap),
    [globalDataQuality, status.data_health?.commodity_data_quality],
  );
  const mcxQuality = useMemo(
    () => (dataQuality.symbol_health ?? []).filter((s) => (s.symbol || "").startsWith("MCX:")),
    [dataQuality.symbol_health],
  );
  const totalEquity = Number(summary.total_equity ?? 0);
  const initialCapital = Number(summary.initial_capital ?? 1_000_000);
  const realizedPnl = Number(summary.realized_pnl ?? 0);
  const positionOpenPnl = positions.reduce((total, position) => total + finiteNumber(position.unrealized_pnl), 0);
  const dayRealizedPnl = finiteNumber(summary.day_pnl);
  const unrealizedPnl = finiteNumber(summary.unrealized_pnl, positionOpenPnl);
  const dayPnl = dayRealizedPnl + unrealizedPnl;
  const totalTrades = Number(summary.total_trades ?? 0);
  const winRate = Number(summary.win_rate ?? 0);
  const maxDrawdown = Number(summary.max_drawdown ?? 0);
  const equityPct = initialCapital > 0 ? ((totalEquity - initialCapital) / initialCapital) * 100 : 0;
  const running = Boolean(status.running);
  const killActive = Boolean(status.kill_switch_active);
  const loopActive = Boolean(status.loop_active);
  const usingSnapshotFutures = runtimeFuturesWatchlist.length === 0 && snapshotFuturesWatchlist.length > 0;
  const streamState =
    overviewQuery.isStreamConnected && watchlistSnapshotQuery.isStreamConnected
      ? "streaming"
      : overviewQuery.hasSnapshot || watchlistSnapshotQuery.hasSnapshot
        ? "syncing"
        : "loading";

  const saveExpiries = () => {
    const selected: Record<string, string> = {};
    contracts.forEach((contract) => {
      const symbol = String(contract.symbol || "");
      const fallback = contract.selected_expiry || contract.active_expiry || contract.suggested_expiry || "";
      if (symbol && (expiryDraft[symbol] || fallback)) {
        selected[symbol] = expiryDraft[symbol] || fallback;
      }
    });
    saveExpiriesMutation.mutate(selected);
  };

  const statusBadgeText = killActive
    ? "kill switch"
    : running
      ? "scanning"
      : loopActive
        ? "armed"
        : status.start_required
          ? "start required"
          : "idle";
  const statusBadgeTone = killActive
    ? "bg-rose-500/10 text-rose-300"
    : running
      ? "bg-emerald-500/10 text-emerald-300"
      : loopActive
        ? "bg-sky-500/10 text-sky-300"
        : "bg-amber-500/10 text-amber-200";

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      {/* ── Compact header: title + status + actions all on one row. The
          last-scan / last-message chip rides along at the right to save
          the second header row for the tab nav. */}
      <header className="mb-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-base font-semibold tracking-tight">Commodity Desk</h1>
          <span className={`rounded-md px-2 py-0.5 text-[10.5px] font-medium ${statusBadgeTone}`}>
            {statusBadgeText}
          </span>
          <span
            className={`rounded-md px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.14em] ${
              streamState === "streaming"
                ? "bg-emerald-500/10 text-emerald-300"
                : streamState === "syncing"
                  ? "bg-amber-500/10 text-amber-200"
                  : "bg-bg-secondary/50 text-text-muted"
            }`}
          >
            {streamState}
          </span>
          <span className="hidden text-[11px] text-text-muted sm:inline">
            {formatIST(status.last_run_at)} · {status.last_message || "—"}
          </span>
          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending || killActive || loopActive}
              className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-[11.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
              title="Start commodity agent"
            >
              <CirclePlay className="h-3.5 w-3.5" />
              {loopActive ? "Running" : "Start"}
            </button>
            <button
              type="button"
              onClick={() => runOnceMutation.mutate()}
              disabled={runOnceMutation.isPending}
              className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-[11.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
              title="Run one scan"
            >
              <RefreshCcw className="h-3.5 w-3.5" />
              Scan
            </button>
            <Link
              href="/settings"
              className="rounded-md bg-bg-secondary/25 px-2 py-1 text-[11.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary"
            >
              Settings
            </Link>
          </div>
        </div>
        {/* Tab nav sits directly under the header — kept as the single
            source of truth for which subview is visible. */}
        <nav className="mt-2 flex gap-1 overflow-x-auto rounded-md bg-bg-secondary/15 p-1">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setActiveTab(tab.key)}
              className={`whitespace-nowrap rounded px-3 py-1.5 text-xs font-medium transition-colors ${
                activeTab === tab.key
                  ? "bg-bg-secondary/55 text-text-primary"
                  : "text-text-muted hover:bg-bg-secondary/30 hover:text-text-primary"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      {/* ── Decision Bar — 4 headline tiles + a chip strip for the rest.
          Robinhood pattern: hero P&L gets generous typography, secondary
          stats live in muted text below. Saves ~40px of vertical space
          and stops the bar from drowning the tab content. */}
      <section className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <StatTile
          label="Equity"
          value={formatINR(totalEquity)}
          detail={`init ${formatINR(initialCapital)} · ${formatPct(equityPct, 2)}`}
          tone={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Day P&L"
          value={formatINR(dayPnl)}
          detail={`realized ${formatINR(dayRealizedPnl)} · open ${formatINR(unrealizedPnl)}`}
          tone={dayPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Realized"
          value={formatINR(realizedPnl)}
          detail={`${totalTrades} trades · win ${(winRate * 100).toFixed(0)}%`}
          tone={realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Open / Ready"
          value={`${positions.length} / ${(summary.ready_futures_signals ?? 0) + (summary.ready_option_signals ?? 0)}`}
          detail={`fut ${summary.ready_futures_signals ?? 0} · opt ${summary.ready_option_signals ?? 0} · ${summary.open_orders ?? 0} working`}
          tone={(summary.ready_futures_signals ?? 0) + (summary.ready_option_signals ?? 0) > 0 ? "text-emerald-300" : undefined}
        />
      </section>
      {/* Secondary stats — muted chip strip. Daily-loss / drawdown caps /
          data-quality / cooldown live here so the tiles above stay focused
          on the live P&L story. */}
      <div className="mb-3 flex flex-wrap gap-2 text-[11px] text-text-muted">
        <Chip label="Drawdown" value={formatPct(maxDrawdown * 100, 1)} tone={maxDrawdown > 0.1 ? "text-amber-300" : ""} />
        <Chip label="Cap" value={formatPct(Number(config.commodity_max_drawdown_pct ?? 0), 1)} />
        <Chip label="Daily loss" value={formatINR(config.commodity_daily_loss_limit)} />
        <Chip label="Per-underlying loss" value={formatINR(config.commodity_underlying_daily_loss_limit)} />
        <Chip label="Cooldown" value={`${config.commodity_stop_cooldown_minutes ?? "—"}m`} />
        <Chip
          label="Data quality"
          value={dataQuality.overall || "—"}
          tone={
            dataQuality.overall === "healthy"
              ? "text-emerald-300"
              : dataQuality.overall === "critical"
                ? "text-rose-300"
                : "text-amber-300"
          }
        />
        <Chip label="MCX symbols" value={String(mcxQuality.length)} />
      </div>

      {/* WATCHLIST tab — Bloomberg-style dense table + action queue side panel. */}
      {activeTab === "watchlist" ? (
        <>
          <div className="mb-3 grid grid-cols-12 gap-3">
            <Section
              title="Action Queue"
              detail={`${watchlist.length} symbols · bucketed by signal proximity`}
              className="col-span-12 xl:col-span-4"
            >
              <ActionQueue rows={watchlist} />
            </Section>
            <Section
              title="Live Instruments"
              detail={
                usingSnapshotFutures
                  ? "catalog fallback · scanner offline"
                  : `${watchlist.length} futures · ${optionWatchlist.length} option pairs · runtime rows`
              }
              className="col-span-12 xl:col-span-8"
            >
              <InstrumentWatchlist futuresRows={watchlist} optionRows={optionWatchlist} />
            </Section>
          </div>
        </>
      ) : null}

      {/* POSITIONS tab — wide table is faster to scan than a grid of cards;
          risk-controls and strategy-agent sleeves sit beside it in a 4-8
          split so the trader sees everything without scrolling. */}
      {activeTab === "positions" ? (
        <div className="mb-3 grid grid-cols-12 gap-3">
          <Section
            title="Open Positions"
            detail={
              positions.length === 0
                ? "no exposure"
                : `${positions.length} live · stop/target tracked`
            }
            className="col-span-12"
          >
            {positions.length === 0 ? (
              <div className="px-2 py-8 text-center text-xs text-text-muted">
                Desk is flat. Scan output will populate the action queue when signals fire.
              </div>
            ) : (
              <PositionsTable positions={positions} />
            )}
          </Section>
          <Section
            title="Risk Controls"
            detail="commodity limits"
            className="col-span-12 xl:col-span-4"
          >
            <div className="grid grid-cols-2 gap-2 text-xs">
              <StatTile label="Daily Loss" value={formatINR(config.commodity_daily_loss_limit)} />
              <StatTile
                label="Underlying Loss"
                value={formatINR(config.commodity_underlying_daily_loss_limit)}
              />
              <StatTile
                label="Max Drawdown"
                value={formatPct(Number(config.commodity_max_drawdown_pct ?? 0), 1)}
              />
              <StatTile label="Cooldown" value={`${config.commodity_stop_cooldown_minutes ?? "--"}m`} />
              <StatTile label="Lots / Trade" value={String(config.lots_per_trade ?? "--")} />
              <StatTile
                label="Option Budget"
                value={formatPct(Number(config.option_capital_fraction ?? 0) * 100, 1)}
              />
            </div>
          </Section>
          <Section
            title="Strategy Agents"
            detail="commodity sleeves"
            className="col-span-12 xl:col-span-8"
          >
            <div className="grid gap-2 sm:grid-cols-2">
              {(status.strategy_agents ?? []).map((agent) => (
                <div key={String(agent.key)} className={`${QUIET_TILE} px-3 py-2 text-xs`}>
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="font-semibold text-text-primary">
                      {String(agent.title || agent.key)}
                    </div>
                    <div className="text-text-muted">{String(agent.execution_mode || "--")}</div>
                  </div>
                  <div className="mt-1 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
                    <span>{String(agent.instrument_scope || "--")}</span>
                    <span>{String(agent.timeframe || "--")}</span>
                    <span>tracked {String(agent.tracked_symbols ?? "--")}</span>
                    <span>ready {String(agent.ready_signals ?? 0)}</span>
                  </div>
                </div>
              ))}
            </div>
          </Section>
        </div>
      ) : null}

      {/* HISTORY tab — Portfolio Summary as a tight side panel, Closed
          Trades takes the dominant real estate (it's what traders open
          this tab to read), Order Flow and Audit Feed share the row
          below at equal weight. */}
      {activeTab === "history" ? (
        <div className="mb-3 grid grid-cols-12 gap-3">
          <Section
            title="Portfolio Summary"
            detail="paper commodity book"
            className="col-span-12 xl:col-span-3"
          >
            <div className="grid grid-cols-2 gap-2">
              <StatTile label="Initial" value={formatINR(initialCapital)} />
              <StatTile label="Available" value={formatINR(summary.available_capital)} />
              <StatTile
                label="Equity"
                value={formatINR(totalEquity)}
                tone={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}
              />
              <StatTile
                label="Realized"
                value={formatINR(realizedPnl)}
                tone={realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
              />
              <StatTile
                label="Unrealized"
                value={formatINR(unrealizedPnl)}
                tone={unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
              />
              <StatTile
                label="Profit Factor"
                value={summary.profit_factor ? Number(summary.profit_factor).toFixed(2) : "--"}
              />
            </div>
          </Section>
          <Section
            title="Closed Trades"
            detail={`${trades.length} rows · last ${formatIST(trades[0]?.exit_time)}`}
            className="col-span-12 xl:col-span-9"
          >
            <div className="max-h-[480px] overflow-y-auto">
              <TradesTable trades={trades.slice(0, 50)} />
            </div>
          </Section>
          <Section
            title="Order Flow"
            detail={`${orders.length} rows`}
            className="col-span-12 xl:col-span-6"
          >
            <div className="max-h-[320px] overflow-y-auto">
              <OrdersTable orders={orders.slice(0, 50)} />
            </div>
          </Section>
          <Section
            title="Audit Feed"
            detail="state transitions"
            className="col-span-12 xl:col-span-6"
          >
            <div className="max-h-[320px] overflow-y-auto">
              <AuditFeed events={auditEvents.slice(0, 40)} />
            </div>
          </Section>
        </div>
      ) : null}

      {/* EXPIRY tab — focuses purely on the contract catalog and per-symbol
          expiry selection. Risk Controls / Strategy Agents previously lived
          here too (duplicated from the Positions tab); removed to give the
          selection table the full canvas it needs. */}
      {activeTab === "expiry" ? (
        <div className="mb-3 grid grid-cols-12 gap-3">
          <Section
            title="Expiry Selection"
            detail={`${contracts.length} contracts · ${contractsQuery.data?.source || watchlistSnapshotQuery.data?.contract_catalog?.source || "--"}`}
            className="col-span-12"
          >
            <div className="overflow-x-auto">
              <table className="w-full min-w-[840px] text-xs">
                <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                  <tr>
                    <th className="text-left">Symbol</th>
                    <th className="text-left">Lookup</th>
                    <th className="text-left">Expiry</th>
                    <th className="text-left">Suggested</th>
                    <th className="text-left">Mapped</th>
                    <th className="text-right">Lot</th>
                    <th className="text-left">Policy</th>
                    <th className="text-left">Unit</th>
                  </tr>
                </thead>
                <tbody>
                  {contracts.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="py-4 text-center text-text-muted">
                        No contracts loaded.
                      </td>
                    </tr>
                  ) : (
                    contracts.map((contract) => {
                      const symbol = String(contract.symbol || "");
                      const fallback =
                        contract.selected_expiry ||
                        contract.active_expiry ||
                        contract.suggested_expiry ||
                        "";
                      const value = expiryDraft[symbol] ?? fallback;
                      const mapped =
                        contract.selected_lookup_symbol ||
                        contract.active_lookup_symbol ||
                        contract.lookup_symbol ||
                        "—";
                      return (
                        <tr key={symbol} className={QUIET_ROW}>
                          <td className="py-2 font-medium">
                            {contract.underlying || symbol}
                            <div className="text-[10px] text-text-muted">{symbol}</div>
                          </td>
                          <td className="font-mono text-[10.5px] text-text-muted">
                            {contract.active_lookup_symbol || contract.lookup_symbol || "--"}
                          </td>
                          <td>
                            <select
                              value={value}
                              onChange={(event) =>
                                setExpiryDraft((draft) => ({ ...draft, [symbol]: event.target.value }))
                              }
                              className="w-full min-w-[150px] rounded-md bg-bg-primary/70 px-2 py-1 text-xs text-text-primary outline-none ring-1 ring-transparent focus:ring-accent-blue/40"
                            >
                              {(contract.expiries ?? []).map((expiry) => (
                                <option key={expiry} value={expiry}>
                                  {expiry}
                                </option>
                              ))}
                              {value && !(contract.expiries ?? []).includes(value) ? (
                                <option value={value}>{value}</option>
                              ) : null}
                            </select>
                          </td>
                          <td className="font-mono text-[10.5px] text-text-muted">
                            {contract.suggested_expiry || "--"}
                          </td>
                          <td className="font-mono text-[10.5px] text-text-muted">{mapped}</td>
                          <td className="text-right font-mono">{contract.lot_size ?? "--"}</td>
                          <td>
                            <span
                              className={
                                contract.selection_locked ? "text-amber-300" : "text-emerald-300"
                              }
                            >
                              {contract.selection_policy || "--"}
                            </span>
                          </td>
                          <td className="text-text-muted">
                            {[contract.contract_unit_label, contract.quote_unit_label]
                              .filter(Boolean)
                              .join(" · ") || "--"}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
            <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-[10.5px] text-text-muted">
                {contractsQuery.data?.detail ||
                  watchlistSnapshotQuery.data?.contract_catalog?.detail ||
                  "Contract catalog loaded."}
              </span>
              <button
                type="button"
                onClick={saveExpiries}
                disabled={saveExpiriesMutation.isPending}
                className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-xs text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                Save expiry selection
              </button>
            </div>
          </Section>
        </div>
      ) : null}

      {/* RESEARCH tab — Strategy notes + commentary + signal audit JSON. */}
      {activeTab === "research" ? (
        <div className="grid grid-cols-12 gap-3">
          <Section
            title="Commodity Research Context"
            detail="strategy assumptions"
            className="col-span-12 xl:col-span-5"
          >
            <div className="space-y-3 text-xs text-text-secondary">
              {(status.strategies ?? []).map((strategy) => (
                <div key={String(strategy.key)} className={`${QUIET_TILE} px-3 py-2`}>
                  <div className="font-semibold text-text-primary">
                    {String(strategy.title || strategy.key)}
                  </div>
                  <div className="mt-1 text-[11px] text-text-muted">
                    {[strategy.instrument, strategy.timeframe, strategy.broker]
                      .filter(Boolean)
                      .map(String)
                      .join(" · ")}
                  </div>
                  <p className="mt-2 leading-relaxed">{String(strategy.notes || "")}</p>
                </div>
              ))}
            </div>
          </Section>
          <Section
            title="Commentary"
            detail={`${status.commentary?.length ?? 0} messages`}
            className="col-span-12 xl:col-span-7"
          >
            {(status.commentary ?? []).length === 0 ? (
              <div className="px-2 py-6 text-center text-xs text-text-muted">No commentary yet.</div>
            ) : (
              <ul className="max-h-[320px] space-y-0.5 overflow-y-auto text-xs">
                {(status.commentary ?? []).map((entry, idx) => (
                  <li
                    key={`${entry.time}-${idx}`}
                    className="flex gap-3 border-b border-transparent py-1 hover:bg-bg-secondary/15"
                  >
                    <span className="w-[92px] shrink-0 font-mono text-[10.5px] text-text-muted">
                      {formatIST(entry.time)}
                    </span>
                    <span className={`w-[70px] shrink-0 text-[10.5px] uppercase ${severityColor(entry.tone)}`}>
                      {entry.tone || "--"}
                    </span>
                    <span className="text-text-secondary">{entry.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section
            title="Signal Audit"
            detail={`${status.signal_audit?.length ?? 0} rows · most recent first`}
            className="col-span-12"
          >
            <SignalAuditTable rows={status.signal_audit ?? []} />
          </Section>
        </div>
      ) : null}
    </div>
  );
}
