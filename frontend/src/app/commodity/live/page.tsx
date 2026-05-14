"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { CirclePlay, RefreshCcw, Save } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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
  underlying?: string | null;
  display_name?: string | null;
  price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
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
 * Bloomberg-style data-rich watchlist for the commodity desk. Single dense
 * table, monospace numerics, every column visible at once, color-coded by
 * direction. Designed for traders scanning multiple instruments at a glance.
 *
 * Layout: futures + options merged into ONE wide row per underlying. Sub-row
 * below each futures row shows the ATM CE/PE quotes. Header sticks while
 * scrolling. Bucket and signal codes are abbreviated to fit (FAV/RDY/DRF/etc).
 */
function InstrumentWatchlist({
  futuresRows,
  optionRows,
}: {
  futuresRows: WatchRow[];
  optionRows: WatchRow[];
}) {
  if (futuresRows.length === 0 && optionRows.length === 0) {
    return (
      <div className="px-2 py-8 text-center text-xs text-text-muted">
        No commodity instruments available.
      </div>
    );
  }
  const optionsBySymbol = new Map(
    optionRows.map((row) => [String(row.symbol || ""), row]),
  );

  const TH = "py-1 px-2 text-left text-[10px] font-semibold uppercase tracking-wider text-text-muted bg-bg-secondary/40 sticky top-0 z-10";
  const THR = `${TH} text-right`;
  const TD = "px-2 py-1 align-middle whitespace-nowrap font-mono";
  const TDR = `${TD} text-right`;

  return (
    <div className="overflow-x-auto rounded-md border border-bg-active/30">
      <table className="w-full min-w-[1480px] border-collapse text-[11.5px]">
        <thead>
          <tr>
            <th className={TH}>Symbol</th>
            <th className={TH}>Bkt</th>
            <th className={THR}>Last</th>
            <th className={THR}>Chg</th>
            <th className={THR}>Δ%</th>
            <th className={THR}>ATR</th>
            <th className={THR}>MACD</th>
            <th className={THR}>Signal</th>
            <th className={THR}>Hist</th>
            <th className={THR}>Prox</th>
            <th className={TH}>Regime</th>
            <th className={TH}>MP</th>
            <th className={TH}>Sig</th>
            <th className={THR}>CE.K</th>
            <th className={THR}>CE.LTP</th>
            <th className={THR}>CE.MACD</th>
            <th className={THR}>CE.OI</th>
            <th className={THR}>CE.ΔOI</th>
            <th className={THR}>PE.K</th>
            <th className={THR}>PE.LTP</th>
            <th className={THR}>PE.MACD</th>
            <th className={THR}>PE.OI</th>
            <th className={THR}>PE.ΔOI</th>
            <th className={THR}>DTE</th>
            <th className={THR}>Bar</th>
          </tr>
        </thead>
        <tbody>
          {futuresRows.map((row) => {
            const optionRow = optionsBySymbol.get(String(row.symbol || ""));
            const ce = (optionRow as Record<string, unknown> | undefined)?.ce as
              | Record<string, unknown>
              | undefined;
            const pe = (optionRow as Record<string, unknown> | undefined)?.pe as
              | Record<string, unknown>
              | undefined;
            const ceLtp = Number(ce?.live_ltp ?? ce?.ltp);
            const peLtp = Number(pe?.live_ltp ?? pe?.ltp);
            const ceMacd = Number(ce?.macd);
            const peMacd = Number(pe?.macd);
            const ceOi = Number(ce?.oi);
            const peOi = Number(pe?.oi);
            const ceOiDelta = ce?.oi_change != null ? Number(ce.oi_change) : null;
            const peOiDelta = pe?.oi_change != null ? Number(pe.oi_change) : null;
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
              <tr
                key={`${row.symbol || row.underlying}-bberg`}
                className="border-t border-bg-active/20 hover:bg-bg-secondary/25"
                title={
                  row.bucket_rationale ||
                  row.signal_validation_detail ||
                  row.signal_validation ||
                  ""
                }
              >
                <td className="px-2 py-1 align-middle whitespace-nowrap">
                  <span className="font-semibold text-text-primary">
                    {row.display_name || row.underlying || row.symbol}
                  </span>
                  <span className="ml-1 font-mono text-[10px] text-text-muted">
                    {(row.symbol || "").replace(/^MCX:/, "").replace(/FUT$/, "")}
                  </span>
                </td>
                <td className={`px-2 py-1 align-middle text-[10px] font-semibold ${bucketCellClass(row.bucket ?? null)}`}>
                  {bucketCode(row.bucket ?? null)}
                </td>
                <td className={`${TDR} text-text-primary`}>{formatNumber(row.price, 2)}</td>
                <td className={`${TDR} ${colorForDelta(chg)}`}>
                  {chg == null ? "—" : (chg >= 0 ? "+" : "") + formatNumber(chg, 2)}
                </td>
                <td className={`${TDR} ${colorForDelta(row.change_pct)}`}>
                  {formatPct(row.change_pct, 2)}
                </td>
                <td className={`${TDR} text-text-muted`}>{formatNumber(row.atr, 1)}</td>
                <td className={`${TDR} ${colorForDelta(row.macd)}`}>{formatNumber(row.macd, 2)}</td>
                <td className={`${TDR} text-text-muted`}>{formatNumber(row.macd_signal, 2)}</td>
                <td className={`${TDR} ${colorForDelta(row.macd_histogram)}`}>
                  {formatNumber(row.macd_histogram, 2)}
                </td>
                <td className={`${TDR} text-text-muted`}>
                  {row.proximity_pct == null ? "—" : `${Math.round(row.proximity_pct)}%`}
                </td>
                <td className="px-2 py-1 align-middle text-[10px] uppercase text-text-secondary">
                  {regimeShort(row.regime)}
                </td>
                <td className="px-2 py-1 align-middle text-[10px] uppercase text-text-secondary">
                  {mpShort(row.mp_day_type || row.mp_status)}
                </td>
                <td className="px-2 py-1 align-middle text-[10px] uppercase text-text-secondary">
                  {sigShort(sigVal)}
                </td>
                <td className={`${TDR} text-text-muted`}>
                  {ce?.strike != null ? String(ce.strike) : "—"}
                </td>
                <td className={`${TDR} text-text-primary`}>
                  {Number.isFinite(ceLtp) ? formatNumber(ceLtp, 2) : "—"}
                </td>
                <td className={`${TDR} ${colorForDelta(ceMacd)}`}>
                  {Number.isFinite(ceMacd) ? formatNumber(ceMacd, 2) : "—"}
                </td>
                <td className={`${TDR} text-text-muted`}>
                  {Number.isFinite(ceOi) ? compactNumber(ceOi) : "—"}
                </td>
                <td className={`${TDR} ${colorForDelta(ceOiDelta)}`}>
                  {ceOiDelta == null
                    ? "—"
                    : (ceOiDelta >= 0 ? "+" : "") + compactNumber(ceOiDelta)}
                </td>
                <td className={`${TDR} text-text-muted`}>
                  {pe?.strike != null ? String(pe.strike) : "—"}
                </td>
                <td className={`${TDR} text-text-primary`}>
                  {Number.isFinite(peLtp) ? formatNumber(peLtp, 2) : "—"}
                </td>
                <td className={`${TDR} ${colorForDelta(peMacd)}`}>
                  {Number.isFinite(peMacd) ? formatNumber(peMacd, 2) : "—"}
                </td>
                <td className={`${TDR} text-text-muted`}>
                  {Number.isFinite(peOi) ? compactNumber(peOi) : "—"}
                </td>
                <td className={`${TDR} ${colorForDelta(peOiDelta)}`}>
                  {peOiDelta == null
                    ? "—"
                    : (peOiDelta >= 0 ? "+" : "") + compactNumber(peOiDelta)}
                </td>
                <td className={`${TDR} text-text-muted`}>{dte == null ? "—" : dte}</td>
                <td className={`${TDR} text-[10px] text-text-muted`}>
                  {formatIST(row.bar_time)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PositionsTable({ positions }: { positions: CommodityPosition[] }) {
  if (positions.length === 0) {
    return <div className="px-2 py-6 text-center text-xs text-text-muted">No open commodity positions.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-xs">
        <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="text-left">Instrument</th>
            <th className="text-left">Side</th>
            <th className="text-right">Qty</th>
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
          {positions.map((p) => (
            <tr key={p.position_key || p.live_symbol} className={QUIET_ROW}>
              <td className="py-1.5 font-medium">
                {p.display_name || p.symbol}
                <div className="text-[10px] text-text-muted">{p.live_symbol}</div>
              </td>
              <td>{p.action}</td>
              <td className="text-right font-mono">{p.qty}</td>
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
          ))}
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

  const overviewQuery = useQuery({
    queryKey: ["commodity-live", "overview"],
    queryFn: async () => (await getCommodityOverview()).data,
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

  const watchlistSnapshotQuery = useQuery({
    queryKey: ["commodity-live", "watchlist-snapshot"],
    queryFn: async () => (await getCommodityWatchlistSnapshot()).data as CommodityWatchlistSnapshot,
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

  const runtimeFuturesWatchlist = useMemo(
    () => (status.futures_watchlist ?? status.watchlist ?? []) as WatchRow[],
    [status.futures_watchlist, status.watchlist],
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

  const positions = useMemo(
    () => (positionsQuery.data as CommodityPosition[] | undefined) ?? status.positions ?? [],
    [positionsQuery.data, status.positions],
  );
  const orders = useMemo(
    () => (ordersQuery.data as Order[] | undefined) ?? status.orders ?? [],
    [ordersQuery.data, status.orders],
  );
  const reports = useMemo(
    () => (reportsQuery.data as ReportRow[] | undefined) ?? status.reports ?? [],
    [reportsQuery.data, status.reports],
  );
  const trades = useMemo(() => (status.trade_history ?? []) as TradeRow[], [status.trade_history]);
  const auditEvents = useMemo(() => (auditQuery.data?.events ?? []) as AuditEvent[], [auditQuery.data]);
  const dataQuality = useMemo(() => (dataQualityQuery.data ?? {}) as DataQualitySnap, [dataQualityQuery.data]);
  const mcxQuality = useMemo(
    () => (dataQuality.symbol_health ?? []).filter((s) => (s.symbol || "").startsWith("MCX:")),
    [dataQuality.symbol_health],
  );
  const contracts = useMemo(
    () => (contractsQuery.data?.contracts ?? watchlistSnapshotQuery.data?.contract_catalog?.contracts ?? []) as CommoditySnapshotContract[],
    [contractsQuery.data?.contracts, watchlistSnapshotQuery.data?.contract_catalog?.contracts],
  );

  const totalEquity = Number(summary.total_equity ?? 0);
  const initialCapital = Number(summary.initial_capital ?? 1_000_000);
  const realizedPnl = Number(summary.realized_pnl ?? 0);
  const dayPnl = Number(summary.day_pnl ?? 0);
  const unrealizedPnl = Number(summary.unrealized_pnl ?? 0);
  const totalTrades = Number(summary.total_trades ?? 0);
  const winRate = Number(summary.win_rate ?? 0);
  const maxDrawdown = Number(summary.max_drawdown ?? 0);
  const equityPct = initialCapital > 0 ? ((totalEquity - initialCapital) / initialCapital) * 100 : 0;
  const running = Boolean(status.running);
  const killActive = Boolean(status.kill_switch_active);
  const loopActive = Boolean(status.loop_active);
  const usingSnapshotFutures = runtimeFuturesWatchlist.length === 0 && snapshotFuturesWatchlist.length > 0;

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

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      <header className="mb-3 pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-baseline gap-3">
              <h1 className="text-lg font-semibold tracking-tight">Commodity Desk</h1>
              <span className="text-xs text-text-muted">
                data-centric MCX futures and options workspace
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
              <span className={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}>
                equity {formatINR(totalEquity)} ({formatPct(equityPct)})
              </span>
              <span>realized {formatINR(realizedPnl)}</span>
              <span>open {formatINR(unrealizedPnl)}</span>
              <span>last scan {formatIST(status.last_run_at)}</span>
              <span>{status.last_message || "--"}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded-md px-2 py-1 text-[11px] ${
                killActive
                  ? "bg-rose-500/10 text-rose-300"
                  : running
                    ? "bg-emerald-500/10 text-emerald-300"
                    : loopActive
                      ? "bg-sky-500/10 text-sky-300"
                      : "bg-amber-500/10 text-amber-200"
              }`}
            >
              {killActive ? "kill switch" : running ? "scanning" : loopActive ? "armed" : status.start_required ? "start required" : "idle"}
            </span>
            <button
              type="button"
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending || killActive || loopActive}
              className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-xs text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
              title="Start commodity agent"
            >
              <CirclePlay className="h-3.5 w-3.5" />
              {loopActive ? "Running" : "Start"}
            </button>
            <button
              type="button"
              onClick={() => runOnceMutation.mutate()}
              disabled={runOnceMutation.isPending}
              className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-xs text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
              title="Run one scan"
            >
              <RefreshCcw className="h-3.5 w-3.5" />
              Scan
            </button>
            <Link href="/settings" className="rounded-md bg-bg-secondary/25 px-2 py-1 text-xs text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary">
              Settings
            </Link>
          </div>
        </div>
        {/* Tab navigation — single source of truth for which subview is visible.
            The Decision Bar above stays always-on so the trader sees P&L /
            risk gauges regardless of which tab is selected. */}
        <nav className="mt-3 flex gap-1 overflow-x-auto rounded-md bg-bg-secondary/15 p-1">
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

      {/* 1) DECISION BAR — equity / P&L / risk / DQ at-a-glance. Always on. */}
      <section className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <StatTile
          label="Equity"
          value={formatINR(totalEquity)}
          detail={`init ${formatINR(initialCapital)}`}
          tone={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Day P&L"
          value={formatINR(dayPnl)}
          detail={`open ${formatINR(unrealizedPnl)}`}
          tone={dayPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Realized"
          value={formatINR(realizedPnl)}
          detail={`${totalTrades} trades · win ${(winRate * 100).toFixed(0)}%`}
          tone={realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <StatTile
          label="Drawdown"
          value={formatPct(maxDrawdown * 100, 1)}
          detail={`cap ${formatPct(Number(config.commodity_max_drawdown_pct ?? 0), 1)}`}
          tone={maxDrawdown > 0.1 ? "text-amber-300" : "text-text-primary"}
        />
        <StatTile
          label="Open"
          value={String(positions.length)}
          detail={`${summary.open_orders ?? 0} working`}
        />
        <StatTile
          label="Ready"
          value={`${summary.ready_futures_signals ?? 0}/${summary.ready_option_signals ?? 0}`}
          detail="fut / opt"
          tone={(summary.ready_futures_signals ?? 0) + (summary.ready_option_signals ?? 0) > 0 ? "text-emerald-300" : undefined}
        />
        <StatTile
          label="Daily Loss Cap"
          value={formatINR(config.commodity_daily_loss_limit)}
          detail={`/und ${formatINR(config.commodity_underlying_daily_loss_limit)}`}
        />
        <StatTile
          label="Data Quality"
          value={dataQuality.overall || "—"}
          detail={`MCX ${mcxQuality.length} · cooldown ${config.commodity_stop_cooldown_minutes ?? "—"}m`}
          tone={
            dataQuality.overall === "healthy"
              ? "text-emerald-400"
              : dataQuality.overall === "critical"
                ? "text-rose-400"
                : "text-amber-300"
          }
        />
      </section>

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

      {/* POSITIONS tab — open exposure cards + risk controls + strategy agents. */}
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
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {positions.map((p) => (
                  <MiniPositionCard key={p.position_key || p.live_symbol} p={p} />
                ))}
              </div>
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

      {/* HISTORY tab — closed trades, orders, audit feed. */}
      {activeTab === "history" ? (
        <div className="mb-3 grid grid-cols-12 gap-3">
          <Section
            title="Portfolio Summary"
            detail="paper commodity book"
            className="col-span-12 xl:col-span-4"
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
            className="col-span-12 xl:col-span-8"
          >
            <div className="max-h-[360px] overflow-y-auto">
              <TradesTable trades={trades.slice(0, 50)} />
            </div>
          </Section>
          <Section
            title="Order Flow"
            detail={`${orders.length} rows`}
            className="col-span-12 xl:col-span-7"
          >
            <div className="max-h-[320px] overflow-y-auto">
              <OrdersTable orders={orders.slice(0, 50)} />
            </div>
          </Section>
          <Section
            title="Audit Feed"
            detail="state transitions"
            className="col-span-12 xl:col-span-5"
          >
            <div className="max-h-[320px] overflow-y-auto">
              <AuditFeed events={auditEvents.slice(0, 40)} />
            </div>
          </Section>
        </div>
      ) : null}

      {/* EXPIRY tab — contract catalog + per-symbol expiry selection. */}
      {activeTab === "expiry" ? (
        <div className="mb-3 grid grid-cols-12 gap-3">
          <Section
            title="Risk Controls"
            detail="commodity limits"
            className="col-span-12 xl:col-span-4"
          >
            <div className="grid grid-cols-2 gap-2 text-xs">
              <StatTile label="Daily Loss" value={formatINR(config.commodity_daily_loss_limit)} />
              <StatTile label="Underlying Loss" value={formatINR(config.commodity_underlying_daily_loss_limit)} />
              <StatTile label="Max Drawdown" value={formatPct(Number(config.commodity_max_drawdown_pct ?? 0), 1)} />
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
            detail={`${status.signal_audit?.length ?? 0} rows`}
            className="col-span-12"
          >
            {(status.signal_audit ?? []).length === 0 ? (
              <div className="px-2 py-6 text-center text-xs text-text-muted">No signal audit rows yet.</div>
            ) : (
              <pre className="max-h-[320px] overflow-auto rounded-md bg-bg-primary/60 p-3 text-[11px] text-text-secondary">
                {JSON.stringify(status.signal_audit, null, 2)}
              </pre>
            )}
          </Section>
        </div>
      ) : null}
    </div>
  );
}
