"use client";

import type { ReactNode } from "react";
import { startTransition, useDeferredValue, useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Boxes,
  CandlestickChart,
  Play,
  RefreshCw,
  Save,
  ShieldAlert,
  ShieldCheck,
  Waves,
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

import { StreamStatus } from "@/components/live/StreamStatus";
import {
  getCommodityOverview,
  getCommodityWatchlistSnapshot,
  startCommodityStrategyAgent,
  updateCommodityKillSwitch,
  updateCommodityStrategyConfig,
  updateCommodityStrategyContracts,
} from "@/lib/api";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createCommodityOverviewSocket, createCommodityWatchlistSocket } from "@/lib/websocket";

type KillSwitchState = {
  market: string;
  auto_run_enabled: boolean;
  kill_switch_active: boolean;
  loop_active?: boolean;
  start_required?: boolean;
  cancelled_orders?: number;
};

type CommodityStrategyLane = {
  key: string;
  title: string;
  status: string;
  instrument: string;
  tracked_symbols: number;
  configured_contracts?: number;
  open_positions?: number;
  lots_per_trade?: number;
  broker: string;
  notes: string;
};

type CommodityPosition = {
  position_key: string;
  symbol: string;
  live_symbol: string;
  underlying: string;
  strategy_key: string;
  strategy_title: string;
  instrument_type: string;
  action: "BUY" | "SELL";
  qty: number;
  lots: number;
  lot_size: number;
  entry_price: number;
  current_price: number;
  stop_price: number;
  target_price?: number | null;
  regime: string;
  signal_reason: string;
  atr?: number | null;
  macd_value?: number | null;
  mp_poc?: number | null;
  mp_vah?: number | null;
  mp_val?: number | null;
  entered_at: string;
  contract_unit_label: string;
  quote_unit_label: string;
  display_name: string;
  notional_value?: number | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: "CE" | "PE" | null;
  target_reached?: boolean;
};

type CommodityOrder = {
  time: string;
  order_id: string;
  symbol: string;
  action: "BUY" | "SELL";
  qty: number;
  lots?: number | null;
  lot_size?: number | null;
  order_type: string;
  status: string;
  fill_price?: number | null;
  reason: string;
  flow?: string | null;
  strategy_key?: string | null;
  strategy_title?: string | null;
};

type CommodityTrade = {
  symbol: string;
  action: string;
  qty: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  entry_time: string;
  exit_time: string;
  instrument_type?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: "CE" | "PE" | null;
};

type CommodityReport = {
  time: string;
  total_equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  open_positions: number;
  tracked_symbols: number;
  last_message: string;
};

type CommodityWatchRow = {
  symbol: string;
  underlying?: string | null;
  display_name?: string | null;
  price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  signal?: string | null;
  reason: string;
  regime: string;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  atr?: number | null;
  raw_signal?: string | null;
  mp_direction?: string | null;
  mp_day_type?: string | null;
  mp_reason?: string | null;
  mp_status?: string | null;
  mp_poc?: number | null;
  mp_vah?: number | null;
  mp_val?: number | null;
  mp_ib_high?: number | null;
  mp_ib_low?: number | null;
  mp_periods?: number | null;
  bar_time?: string | null;
  lot_size?: number | null;
  lots_per_trade?: number | null;
  default_qty?: number | null;
  contract_unit_label?: string | null;
  quote_unit_label?: string | null;
  strategy_title?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  execution_lane?: string | null;
  required_margin?: number | null;
  bias_side?: "CE" | "PE" | null;
};

type ATMWatchlistOptionSide = {
  strike: number;
  option_type: "CE" | "PE";
  instrument_key?: string | null;
  trading_symbol?: string | null;
  ltp: number;
  prev_close?: number | null;
  change?: number | null;
  change_pct?: number | null;
  oi: number;
  prev_oi?: number | null;
  oi_change?: number | null;
  oi_change_pct?: number | null;
  volume: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
  selection_mode?: string | null;
  liquidity_score?: number | null;
  distance_steps?: number | null;
  distance_from_atm?: number | null;
  is_liquid?: boolean;
  bar_time?: string | null;
  zero_cross?: string | null;
};

type CommodityOptionRow = {
  underlying: string;
  symbol: string;
  display_name?: string | null;
  lookup_symbol?: string | null;
  kind: string;
  spot_price: number;
  expiry: string;
  selected_expiry?: string | null;
  suggested_expiry?: string | null;
  active_expiry?: string | null;
  available_expiries?: string[];
  expiry_mappings?: Array<{
    expiry: string;
    lookup_symbol: string;
  }>;
  selected_lookup_symbol?: string | null;
  atm_strike: number;
  live_source: string;
  fyers_symbol?: string | null;
  lot_size?: number | null;
  contract_unit_label?: string | null;
  quote_unit_label?: string | null;
  strategy_title?: string | null;
  contract_notes?: string | null;
  selection_policy?: string | null;
  selection_locked?: boolean;
  regime?: string | null;
  signal_side?: "CE" | "PE" | null;
  signal_reason?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  trade_symbol?: string | null;
  trade_strike?: number | null;
  trade_price?: number | null;
  trade_bar_time?: string | null;
  ce_symbol?: string | null;
  pe_symbol?: string | null;
  ce_trade_price?: number | null;
  pe_trade_price?: number | null;
  capital_per_trade?: number | null;
  lots_affordable?: number | null;
  is_trade_contract_liquid?: boolean;
  ce?: ATMWatchlistOptionSide | null;
  pe?: ATMWatchlistOptionSide | null;
};

type CommodityContractCatalogPayload = {
  contracts: Array<{
    symbol: string;
    underlying: string;
    lookup_symbol?: string | null;
    active_lookup_symbol?: string | null;
    default_lookup_symbol?: string | null;
    expiries: string[];
    expiry_mappings?: Array<{
      expiry: string;
      lookup_symbol: string;
    }>;
    selected_lookup_symbol?: string | null;
    selected_expiry?: string | null;
    suggested_expiry?: string | null;
    active_expiry?: string | null;
    selection_policy?: string | null;
    selection_locked?: boolean;
    has_options: boolean;
    lot_size?: number | null;
    contract_unit_label?: string | null;
    quote_unit_label?: string | null;
    strategy_title?: string | null;
    detail?: string | null;
  }>;
  summary: {
    total_symbols: number;
    contracts_ready: number;
    active_selections: number;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
};

type CommodityATMWatchlistPayload = {
  expiry: string | null;
  rows: CommodityOptionRow[];
  summary: {
    total_rows: number;
    ce_ready: number;
    pe_ready: number;
    tracked_symbols?: number;
    configured_contracts?: number;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
};

type CommodityWatchlistSnapshot = {
  contract_catalog: CommodityContractCatalogPayload;
  atm_watchlist: CommodityATMWatchlistPayload;
};

type CommodityStatus = {
  enabled: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  loop_active?: boolean;
  start_required?: boolean;
  running: boolean;
  scan_interval_seconds: number;
  last_run_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  strategies?: CommodityStrategyLane[];
  config: {
    symbols: string[];
    selected_option_expiries?: Record<string, string>;
    futures_timeframe: string;
    options_timeframe: string;
    futures_macd_fast: number;
    futures_macd_slow: number;
    futures_macd_signal: number;
    options_macd_fast: number;
    options_macd_slow: number;
    options_macd_signal: number;
    mp_period_minutes: number;
    lots_per_trade: number;
    option_capital_fraction: number;
    option_hard_stop_pct: number;
  };
  summary: {
    total_equity?: number | null;
    realized_pnl?: number | null;
    unrealized_pnl?: number | null;
    total_trades?: number | null;
    open_positions?: number | null;
    tracked_symbols?: number | null;
    open_orders?: number | null;
    ready_futures_signals?: number | null;
    ready_option_signals?: number | null;
  };
  watchlist: CommodityWatchRow[];
  futures_watchlist?: CommodityWatchRow[];
  option_watchlist?: CommodityOptionRow[];
  positions: CommodityPosition[];
  trade_history?: CommodityTrade[];
  orders: CommodityOrder[];
  reports: CommodityReport[];
  commentary: Array<{
    time: string;
    tone: string;
    message: string;
  }>;
  data_health?: {
    fyers_token_health?: {
      connected?: boolean;
      valid?: boolean;
      status?: string | null;
      message?: string | null;
    };
    option_history?: {
      failure_count?: number;
      success_count?: number;
      brokers?: Record<
        string,
        {
          success?: number;
          failure?: number;
          last_status?: string | null;
          last_detail?: string | null;
        }
      >;
    };
  };
};

type CommodityTab = "execution" | "signals" | "setup";

type CommodityPortfolioRow = {
  id: string;
  sleeve: "Strategy 1" | "Strategy 2";
  underlying: string;
  contract: string;
  side: string;
  qty: number;
  entryTime?: string | null;
  entryPrice?: number | null;
  lastTime?: string | null;
  lastPrice?: number | null;
  pnl?: number | null;
  returnPct?: number | null;
  status: "open" | "closed";
  statusLabel: string;
  signalReason?: string | null;
};

const EXAMPLE_SYMBOLS = [
  "MCX:GOLD26JUNFUT",
  "MCX:SILVERM26JUNFUT",
  "MCX:CRUDEOIL26MAYFUT",
  "MCX:NATURALGAS26MAYFUT",
];

function safeArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((entry) => entry != null) as T[]) : [];
}

function safeStringArray(value: unknown): string[] {
  return safeArray<unknown>(value).filter((entry): entry is string => typeof entry === "string");
}

function safeExpiryMappings(
  value: unknown,
): Array<{ expiry: string; lookup_symbol: string }> {
  return safeArray<unknown>(value).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const mapping = entry as { expiry?: unknown; lookup_symbol?: unknown };
    if (typeof mapping.expiry !== "string" || typeof mapping.lookup_symbol !== "string") {
      return [];
    }
    return [{ expiry: mapping.expiry, lookup_symbol: mapping.lookup_symbol }];
  });
}

function normalizeCommodityContract(
  contract: CommodityContractCatalogPayload["contracts"][number],
): CommodityContractCatalogPayload["contracts"][number] {
  return {
    ...contract,
    expiries: safeStringArray(contract?.expiries),
    expiry_mappings: safeExpiryMappings(contract?.expiry_mappings),
  };
}

function normalizeCommodityOptionRow(row: CommodityOptionRow): CommodityOptionRow {
  return {
    ...row,
    available_expiries: safeStringArray(row?.available_expiries),
    expiry_mappings: safeExpiryMappings(row?.expiry_mappings),
  };
}

function normalizeCommodityOverviewData(
  value:
    | {
        status?: CommodityStatus;
        kill_switch_state?: KillSwitchState;
        orders?: CommodityOrder[];
        positions?: CommodityPosition[];
        reports?: CommodityReport[];
      }
    | undefined,
) {
  return {
    status: value?.status,
    kill_switch_state: value?.kill_switch_state,
    orders: safeArray<CommodityOrder>(value?.orders),
    positions: safeArray<CommodityPosition>(value?.positions),
    reports: safeArray<CommodityReport>(value?.reports),
  };
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${numeric.toFixed(digits)}${suffix}`;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function formatCompact(value?: number | null) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  if (Math.abs(numeric) >= 1_000_000) return `${(numeric / 1_000_000).toFixed(1)}M`;
  if (Math.abs(numeric) >= 1_000) return `${(numeric / 1_000).toFixed(1)}k`;
  return `${Math.round(numeric)}`;
}

function formatIv(value?: number | null) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const normalized = numeric > 5 ? numeric : numeric * 100;
  return `${normalized.toFixed(1)}%`;
}

function formatIndicator(value?: number | null, digits = 2) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${numeric.toFixed(digits)}`;
}

function valueTone(value?: number | null) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "text-text-muted";
  if (numeric > 0) return "text-accent-green";
  if (numeric < 0) return "text-accent-red";
  return "text-text-secondary";
}

function macdTone(value?: number | null) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "text-text-muted";
  return numeric >= 0 ? "text-accent-green" : "text-accent-red";
}

function rsiTone(value?: number | null) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "text-text-muted";
  if (numeric >= 70) return "text-accent-red";
  if (numeric <= 30) return "text-accent-green";
  return "text-text-secondary";
}

function formatFixed(value?: number | string | null, digits = 2) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "--";
}

function toneBadge(tone: string) {
  if (tone === "ready" || tone === "success" || tone === "paper_execution") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (
    tone === "blocked_kill_switch"
    || tone === "warning"
    || tone === "monitoring"
    || tone === "trend_aligned"
    || tone === "mp_pending"
    || tone === "mp_warming_up"
  ) {
    return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  }
  if (
    tone === "error"
    || tone === "price_unavailable"
    || tone === "insufficient_margin"
    || tone === "insufficient_capital"
    || tone === "dead_zone"
    || tone === "illiquid_contract"
    || tone === "mp_conflict"
  ) {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  if (tone === "BUY" || tone === "bullish") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (tone === "SELL" || tone === "bearish") {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-active bg-bg-secondary/60 text-text-secondary";
}

function prettifyToken(value?: string | null) {
  if (!value) return "--";
  return value.replaceAll("_", " ");
}

function runtimeHealthTone(value?: string | null, valid?: boolean) {
  if (valid) return "ready";
  if (value === "missing" || value === "expired_reconnect_required") return "error";
  return "warning";
}

function toEpoch(value?: string | null) {
  if (!value) return 0;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}

function commodityContractLabel(position: {
  display_name?: string | null;
  symbol?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
}) {
  if (position.option_type) {
    return `${position.display_name || position.symbol || "--"} · ${position.option_type} ${position.strike ?? "--"} · ${position.expiry || "--"}`;
  }
  return position.display_name || position.symbol || "--";
}

function commodityUnderlyingFromSymbol(symbol?: string | null) {
  const raw = String(symbol || "");
  const parts = raw.split(":");
  if (parts.length > 1) {
    const contract = parts[1] || raw;
    return contract.replace(/\d.*$/, "");
  }
  return raw || "--";
}

function commodityOptionRowKey(row: {
  symbol?: string | null;
  active_expiry?: string | null;
  expiry?: string | null;
}) {
  return `${row.symbol || "--"}:${row.active_expiry || row.expiry || "--"}`;
}

function commodityExpiryLookupLabel(mapping?: { expiry: string; lookup_symbol: string } | null) {
  if (!mapping) return "--";
  return `${mapping.expiry} -> ${mapping.lookup_symbol}`;
}

function mergeCommodityOptionRow(
  snapshotRow: CommodityOptionRow,
  liveRow?: CommodityOptionRow | null,
): CommodityOptionRow {
  if (!liveRow) {
    return snapshotRow;
  }

  const liveTradeSide =
    snapshotRow.signal_side === "CE"
      ? liveRow.ce
      : snapshotRow.signal_side === "PE"
        ? liveRow.pe
        : null;
  const lotSize = liveRow.lot_size ?? snapshotRow.lot_size ?? 0;
  const capitalPerTrade = snapshotRow.capital_per_trade ?? 0;
  const liveTradePrice = liveTradeSide?.ltp ?? snapshotRow.trade_price ?? null;
  const lotsAffordable =
    capitalPerTrade > 0 && lotSize > 0 && liveTradePrice && liveTradePrice > 0
      ? Math.floor(capitalPerTrade / (liveTradePrice * lotSize))
      : snapshotRow.lots_affordable;

  return {
    ...snapshotRow,
    spot_price: liveRow.spot_price,
    expiry: liveRow.expiry,
    selected_expiry: liveRow.selected_expiry ?? snapshotRow.selected_expiry,
    suggested_expiry: liveRow.suggested_expiry ?? snapshotRow.suggested_expiry,
    active_expiry: liveRow.active_expiry ?? snapshotRow.active_expiry,
    available_expiries: liveRow.available_expiries ?? snapshotRow.available_expiries,
    expiry_mappings: liveRow.expiry_mappings ?? snapshotRow.expiry_mappings,
    selected_lookup_symbol: liveRow.selected_lookup_symbol ?? snapshotRow.selected_lookup_symbol,
    lookup_symbol: liveRow.lookup_symbol ?? snapshotRow.lookup_symbol,
    live_source: liveRow.live_source || snapshotRow.live_source,
    fyers_symbol: liveRow.fyers_symbol ?? snapshotRow.fyers_symbol,
    lot_size: liveRow.lot_size ?? snapshotRow.lot_size,
    contract_unit_label: liveRow.contract_unit_label ?? snapshotRow.contract_unit_label,
    quote_unit_label: liveRow.quote_unit_label ?? snapshotRow.quote_unit_label,
    contract_notes: liveRow.contract_notes ?? snapshotRow.contract_notes,
    selection_policy: liveRow.selection_policy ?? snapshotRow.selection_policy,
    selection_locked: liveRow.selection_locked ?? snapshotRow.selection_locked,
    ce: liveRow.ce ?? snapshotRow.ce,
    pe: liveRow.pe ?? snapshotRow.pe,
    ce_symbol: liveRow.ce?.instrument_key || liveRow.ce?.trading_symbol || snapshotRow.ce_symbol,
    pe_symbol: liveRow.pe?.instrument_key || liveRow.pe?.trading_symbol || snapshotRow.pe_symbol,
    ce_trade_price: liveRow.ce?.ltp ?? snapshotRow.ce_trade_price,
    pe_trade_price: liveRow.pe?.ltp ?? snapshotRow.pe_trade_price,
    trade_symbol: liveTradeSide?.instrument_key || liveTradeSide?.trading_symbol || snapshotRow.trade_symbol,
    trade_strike: liveTradeSide?.strike ?? snapshotRow.trade_strike,
    trade_price: liveTradePrice,
    lots_affordable: lotsAffordable,
    is_trade_contract_liquid: liveTradeSide?.is_liquid ?? snapshotRow.is_trade_contract_liquid,
  };
}

function buildCommodityPortfolioRows(
  positions: CommodityPosition[],
  trades: CommodityTrade[],
  lastRunAt?: string | null,
): CommodityPortfolioRow[] {
  const rows: CommodityPortfolioRow[] = [];

  for (const position of positions || []) {
    rows.push({
      id: `open:${position.position_key || position.live_symbol || position.symbol}`,
      sleeve: position.strategy_key === "commodity_options" ? "Strategy 1" : "Strategy 2",
      underlying: position.underlying,
      contract: commodityContractLabel(position),
      side: position.option_type ? `BUY ${position.option_type}` : position.action,
      qty: position.qty,
      entryTime: position.entered_at,
      entryPrice: position.entry_price,
      lastTime: lastRunAt || position.entered_at,
      lastPrice: position.current_price,
      pnl: position.unrealized_pnl,
      returnPct: position.return_pct,
      status: "open",
      statusLabel: "open",
      signalReason: position.signal_reason,
    });
  }

  for (const trade of trades || []) {
    const grossCost = (trade.entry_price || 0) * Math.max(trade.qty || 0, 1);
    rows.push({
      id: `closed:${trade.symbol}:${trade.exit_time || trade.entry_time || "na"}`,
      sleeve: trade.instrument_type === "FUT" ? "Strategy 2" : "Strategy 1",
      underlying: commodityUnderlyingFromSymbol(trade.symbol),
      contract: commodityContractLabel({
        symbol: trade.symbol,
        option_type: trade.option_type,
        strike: trade.strike,
        expiry: trade.expiry,
      }),
      side: trade.option_type ? `BUY ${trade.option_type}` : trade.action,
      qty: trade.qty,
      entryTime: trade.entry_time,
      entryPrice: trade.entry_price,
      lastTime: trade.exit_time,
      lastPrice: trade.exit_price,
      pnl: trade.pnl,
      returnPct: grossCost > 0 ? (trade.pnl / grossCost) * 100 : null,
      status: "closed",
      statusLabel: "closed",
      signalReason: trade.instrument_type || trade.action,
    });
  }

  rows.sort((left, right) => {
    const rightTime = Math.max(toEpoch(right.lastTime), toEpoch(right.entryTime));
    const leftTime = Math.max(toEpoch(left.lastTime), toEpoch(left.entryTime));
    return rightTime - leftTime;
  });

  return rows;
}

function StatusBadge({
  label,
  tone,
}: {
  label: string;
  tone: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
        toneBadge(tone),
      )}
    >
      {label}
    </span>
  );
}

function MetricTile({
  label,
  value,
  tone,
  detail,
}: {
  label: string;
  value: string;
  tone?: string;
  detail?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/35 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold text-text-primary", tone)}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function PanelHeader({
  icon,
  title,
  detail,
  meta,
}: {
  icon: ReactNode;
  title: string;
  detail: string;
  meta?: string;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          {icon}
          {title}
        </div>
        <div className="mt-1 text-xs text-text-muted">{detail}</div>
      </div>
      {meta ? <div className="text-xs text-text-muted">{meta}</div> : null}
    </div>
  );
}

function StrategyLaneCard({
  lane,
}: {
  lane: CommodityStrategyLane;
}) {
  return (
    <div className="rounded-[22px] border border-bg-border bg-bg-secondary/35 p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-semibold text-text-primary">{lane.title}</div>
          <div className="mt-1 text-xs text-text-muted">{lane.instrument}</div>
        </div>
        <StatusBadge label={prettifyToken(lane.status)} tone={lane.status} />
      </div>
      <div className="mt-4 grid gap-3 text-xs text-text-secondary sm:grid-cols-2">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Tracked</div>
          <div className="mt-1 font-mono text-text-primary">{lane.tracked_symbols}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Broker</div>
          <div className="mt-1 font-mono text-text-primary">{lane.broker}</div>
        </div>
        {lane.configured_contracts != null ? (
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Configured</div>
            <div className="mt-1 font-mono text-text-primary">{lane.configured_contracts}</div>
          </div>
        ) : null}
        {lane.open_positions != null ? (
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Open Positions</div>
            <div className="mt-1 font-mono text-text-primary">{lane.open_positions}</div>
          </div>
        ) : null}
        {lane.lots_per_trade != null ? (
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Trade Size</div>
            <div className="mt-1 font-mono text-text-primary">{lane.lots_per_trade} lot</div>
          </div>
        ) : null}
      </div>
      <div className="mt-4 text-xs leading-5 text-text-secondary">{lane.notes}</div>
    </div>
  );
}

function CommodityTabButton({
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
        "rounded-2xl border px-4 py-3 text-left transition-colors",
        active
          ? "border-accent-blue/40 bg-accent-blue/10 text-text-primary"
          : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.16em]">{label}</div>
      <div className="mt-1 text-[11px] leading-5 text-text-muted">{detail}</div>
    </button>
  );
}

function ATMOptionCell({
  option,
  accent,
}: {
  option?: ATMWatchlistOptionSide | null;
  accent: "ce" | "pe";
}) {
  const ltpTone = accent === "ce" ? "text-accent-green" : "text-accent-red";
  const modeLabel = option?.selection_mode ? prettifyToken(option.selection_mode) : null;

  if (!option) {
    return (
      <div className="rounded-2xl border border-dashed border-bg-border px-3 py-4 text-[11px] text-text-muted">
        No ATM contract
      </div>
    );
  }

  return (
    <div className="grid grid-cols-5 gap-x-3 gap-y-2 rounded-2xl border border-bg-border bg-bg-secondary/20 px-3 py-3 text-[11px]">
      <div>
        <div className="text-text-muted">LTP</div>
        <div className={clsx("font-mono font-semibold", ltpTone)}>{formatFixed(option.ltp, 2)}</div>
      </div>
      <div>
        <div className="text-text-muted">Chg</div>
        <div className={valueTone(option.change_pct)}>{formatSigned(option.change_pct, 2, "%")}</div>
      </div>
      <div>
        <div className="text-text-muted">Vol</div>
        <div className="text-text-secondary">{formatCompact(option.volume)}</div>
      </div>
      <div>
        <div className="text-text-muted">OI</div>
        <div className="text-text-primary">{formatCompact(option.oi)}</div>
      </div>
      <div>
        <div className="text-text-muted">dOI</div>
        <div className={valueTone(option.oi_change)}>{formatCompact(option.oi_change)}</div>
      </div>
      <div>
        <div className="text-text-muted">IV</div>
        <div className="text-text-secondary">{formatIv(option.iv)}</div>
      </div>
      <div>
        <div className="text-text-muted">MACD</div>
        <div className={macdTone(option.macd)}>{formatIndicator(option.macd, 3)}</div>
      </div>
      <div>
        <div className="text-text-muted">RSI</div>
        <div className={rsiTone(option.rsi)}>{formatIndicator(option.rsi, 1)}</div>
      </div>
      <div>
        <div className="text-text-muted">Delta</div>
        <div className="text-text-secondary">{formatSigned(option.delta, 3)}</div>
      </div>
      <div>
        <div className="text-text-muted">Symbol</div>
        <div className="truncate text-text-muted" title={option.trading_symbol || option.instrument_key || undefined}>
          {option.trading_symbol || option.instrument_key || "--"}
        </div>
      </div>
      <div className="col-span-5 flex flex-wrap items-center gap-2 pt-1">
        {modeLabel ? <StatusBadge label={modeLabel} tone={option.is_liquid ? "ready" : "warning"} /> : null}
        {option.zero_cross ? <StatusBadge label={prettifyToken(option.zero_cross)} tone={option.zero_cross.includes("fresh") ? "ready" : "idle"} /> : null}
        <div className="text-text-muted">
          strike {option.strike} · {option.distance_steps != null ? `${formatFixed(option.distance_steps, 1)} step` : "--"} from ATM
        </div>
      </div>
    </div>
  );
}

export default function CommodityPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<CommodityTab>("execution");
  const [draftSymbols, setDraftSymbols] = useState("");
  const [hasEditedSymbols, setHasEditedSymbols] = useState(false);
  const [contractExpiryDrafts, setContractExpiryDrafts] = useState<Record<string, string>>({});
  const [hasEditedContractExpiries, setHasEditedContractExpiries] = useState(false);
  const deferredDraftSymbols = useDeferredValue(draftSymbols);

  const commodityOverviewQuery = useLiveSnapshotQuery<{
    status: CommodityStatus;
    kill_switch_state: KillSwitchState;
    orders: CommodityOrder[];
    positions: CommodityPosition[];
    reports: CommodityReport[];
  }>({
    queryKey: ["commodityOverview"],
    queryFn: async () => (await getCommodityOverview()).data as {
      status: CommodityStatus;
      kill_switch_state: KillSwitchState;
      orders: CommodityOrder[];
      positions: CommodityPosition[];
      reports: CommodityReport[];
    },
    streamFactory: (onData, onStatusChange) =>
      createCommodityOverviewSocket(
        (data) =>
          onData(data as {
            status: CommodityStatus;
            kill_switch_state: KillSwitchState;
            orders: CommodityOrder[];
            positions: CommodityPosition[];
            reports: CommodityReport[];
          }),
        onStatusChange,
      ),
    storageKey: "commodityOverviewSnapshot:v4",
    staleTime: 10_000,
  });

  const normalizedOverview = useMemo(
    () => normalizeCommodityOverviewData(commodityOverviewQuery.data),
    [commodityOverviewQuery.data],
  );
  const status = normalizedOverview.status;
  const killSwitchState = normalizedOverview.kill_switch_state;
  const orders = normalizedOverview.orders;
  const positions = normalizedOverview.positions;
  const reports = normalizedOverview.reports;

  const commodityWatchlistQuery = useLiveSnapshotQuery<CommodityWatchlistSnapshot>({
    queryKey: ["commodityWatchlistSnapshot"],
    queryFn: async () => (await getCommodityWatchlistSnapshot()).data as CommodityWatchlistSnapshot,
    streamFactory: (onData, onStatusChange) =>
      createCommodityWatchlistSocket(
        (data) => onData(data as CommodityWatchlistSnapshot),
        onStatusChange,
      ),
    storageKey: "commodityWatchlistSnapshot:v4",
    staleTime: 15_000,
    retry: 1,
  });

  const contractCatalogData = commodityWatchlistQuery.data?.contract_catalog;
  const commodityATMWatchlistData = commodityWatchlistQuery.data?.atm_watchlist;
  const contractCatalogSummary = contractCatalogData?.summary ?? {
    total_symbols: 0,
    contracts_ready: 0,
    active_selections: 0,
  };
  const config = status?.config;
  const summary = status?.summary;
  const configSymbols = Array.isArray(config?.symbols) ? config.symbols : [];
  const selectedOptionExpiries = config?.selected_option_expiries ?? {};
  const lotsPerTrade = config?.lots_per_trade ?? 1;
  const optionCapitalFraction = config?.option_capital_fraction ?? 0.2;
  const optionHardStopPct = config?.option_hard_stop_pct ?? 25;
  const trackedSymbolsCount = summary?.tracked_symbols ?? 0;
  const openPositionsCount = summary?.open_positions ?? 0;
  const readyFuturesCount = summary?.ready_futures_signals ?? 0;
  const readyOptionsCount = summary?.ready_option_signals ?? 0;
  const totalEquity = summary?.total_equity;
  const realizedPnl = summary?.realized_pnl;
  const unrealizedPnl = summary?.unrealized_pnl;

  useEffect(() => {
    if (hasEditedSymbols) {
      return;
    }
    const nextSymbols = configSymbols.length
      ? configSymbols.join("\n")
      : EXAMPLE_SYMBOLS.join("\n");
    startTransition(() => {
      setDraftSymbols(nextSymbols);
    });
  }, [configSymbols, hasEditedSymbols]);

  useEffect(() => {
    if (hasEditedContractExpiries) {
      return;
    }
    const nextDrafts: Record<string, string> = {};
    for (const contract of contractCatalogData?.contracts ?? []) {
      const persistedExpiry = contract.selected_expiry || contract.active_expiry;
      if (persistedExpiry) {
        nextDrafts[contract.symbol] = persistedExpiry;
      }
    }
    setContractExpiryDrafts(nextDrafts);
  }, [contractCatalogData, hasEditedContractExpiries]);

  const parsedSymbols = useMemo(
    () =>
      deferredDraftSymbols
        .split("\n")
        .map((value) => value.trim().toUpperCase())
        .filter(Boolean),
    [deferredDraftSymbols],
  );

  const invalidateCommodityQueries = () => {
    queryClient.invalidateQueries({ queryKey: ["commodityOverview"] });
    queryClient.invalidateQueries({ queryKey: ["commodityStrategyStatus"] });
    queryClient.invalidateQueries({ queryKey: ["commodityKillSwitch"] });
    queryClient.invalidateQueries({ queryKey: ["commodityOrders"] });
    queryClient.invalidateQueries({ queryKey: ["commodityPositions"] });
    queryClient.invalidateQueries({ queryKey: ["commodityReports"] });
    queryClient.invalidateQueries({ queryKey: ["commodityWatchlistSnapshot"] });
  };

  const saveConfigMutation = useMutation({
    mutationFn: (symbols: string[]) => updateCommodityStrategyConfig(symbols),
    onSuccess: () => {
      invalidateCommodityQueries();
      setHasEditedSymbols(false);
      setHasEditedContractExpiries(false);
    },
  });

  const startAgentMutation = useMutation({
    mutationFn: () => startCommodityStrategyAgent(),
    onSuccess: invalidateCommodityQueries,
  });

  const killSwitchMutation = useMutation({
    mutationFn: (active: boolean) => updateCommodityKillSwitch(active),
    onSuccess: invalidateCommodityQueries,
  });

  const saveContractSelectionsMutation = useMutation({
    mutationFn: (selectedOptionExpiries: Record<string, string>) =>
      updateCommodityStrategyContracts(selectedOptionExpiries),
    onSuccess: () => {
      invalidateCommodityQueries();
      setHasEditedContractExpiries(false);
    },
  });

  const commentary = useMemo(
    () => safeArray<CommodityStatus["commentary"][number]>(status?.commentary),
    [status?.commentary],
  );
  const positionRows = useMemo(
    () => (positions.length ? positions : safeArray<CommodityPosition>(status?.positions)),
    [positions, status?.positions],
  );
  const tradeHistoryRows = useMemo(
    () => safeArray<CommodityTrade>(status?.trade_history),
    [status?.trade_history],
  );
  const orderRows = useMemo(
    () => (orders.length ? orders : safeArray<CommodityOrder>(status?.orders)),
    [orders, status?.orders],
  );
  const reportRows = useMemo(
    () => (reports.length ? reports : safeArray<CommodityReport>(status?.reports)),
    [reports, status?.reports],
  );
  const strategies = useMemo(() => safeArray<CommodityStrategyLane>(status?.strategies), [status?.strategies]);
  const contractCatalog = useMemo(
    () => safeArray<CommodityContractCatalogPayload["contracts"][number]>(contractCatalogData?.contracts).map(normalizeCommodityContract),
    [contractCatalogData?.contracts],
  );
  const futuresRows = useMemo(
    () => {
      const directRows = safeArray<CommodityWatchRow>(status?.futures_watchlist);
      return directRows.length ? directRows : safeArray<CommodityWatchRow>(status?.watchlist);
    },
    [status?.futures_watchlist, status?.watchlist],
  );
  const strategyOptionRows = useMemo(
    () => safeArray<CommodityOptionRow>(status?.option_watchlist).map(normalizeCommodityOptionRow),
    [status?.option_watchlist],
  );
  const liveOptionRows = useMemo(
    () => safeArray<CommodityOptionRow>(commodityATMWatchlistData?.rows).map(normalizeCommodityOptionRow),
    [commodityATMWatchlistData?.rows],
  );
  const optionRows = useMemo(() => {
    if (!liveOptionRows.length) {
      return strategyOptionRows;
    }
    if (!strategyOptionRows.length) {
      return liveOptionRows;
    }

    const liveRowsByKey = new Map(
      liveOptionRows.map((row) => [commodityOptionRowKey(row), row] as const),
    );
    const liveRowsBySymbol = new Map(
      liveOptionRows.map((row) => [row.symbol, row] as const),
    );
    const consumedLiveKeys = new Set<string>();
    const mergedRows = strategyOptionRows.map((row) => {
      const liveRow = liveRowsByKey.get(commodityOptionRowKey(row)) || liveRowsBySymbol.get(row.symbol);
      if (liveRow) {
        consumedLiveKeys.add(commodityOptionRowKey(liveRow));
      }
      return mergeCommodityOptionRow(row, liveRow);
    });

    for (const liveRow of liveOptionRows) {
      const liveKey = commodityOptionRowKey(liveRow);
      if (!consumedLiveKeys.has(liveKey)) {
        mergedRows.push(liveRow);
      }
    }

    return mergedRows;
  }, [liveOptionRows, strategyOptionRows]);
  const killSwitchActive = killSwitchState?.kill_switch_active ?? status?.kill_switch_active ?? false;
  const loopActive = status?.loop_active ?? killSwitchState?.loop_active ?? false;
  const startRequired = status?.start_required ?? killSwitchState?.start_required ?? false;
  const selectedExpiryCount = Object.keys(selectedOptionExpiries).length;
  const saveError = saveConfigMutation.error as { response?: { data?: { detail?: string } } } | null;
  const fyersHealth = status?.data_health?.fyers_token_health;
  const optionHistoryHealth = status?.data_health?.option_history;
  const optionHistoryFailures = Number(optionHistoryHealth?.failure_count || 0);
  const optionHistorySuccesses = Number(optionHistoryHealth?.success_count || 0);
  const optionHistoryLatestFailure = useMemo(
    () =>
      Object.entries(optionHistoryHealth?.brokers || {})
        .map(([broker, brokerState]) => {
          const failures = Number(brokerState.failure || 0);
          if (!failures) return null;
          return `${broker.toUpperCase()}: ${brokerState.last_detail || "fetch failed"}`;
        })
        .filter(Boolean)
        .join(" | "),
    [optionHistoryHealth?.brokers],
  );

  const futuresLane = useMemo(() => strategies.find((lane) => lane.key === "commodity_futures"), [strategies]);
  const optionsLane = useMemo(() => strategies.find((lane) => lane.key === "commodity_options"), [strategies]);
  const actionableFutures = useMemo(() => futuresRows.filter((row) => row.signal_validation === "ready"), [futuresRows]);
  const actionableOptions = useMemo(() => optionRows.filter((row) => row.signal_validation === "ready"), [optionRows]);
  const portfolioRows = useMemo(
    () => buildCommodityPortfolioRows(positionRows, tradeHistoryRows, status?.last_run_at),
    [positionRows, status?.last_run_at, tradeHistoryRows],
  );

  return (
    <div className="max-w-[1880px] space-y-6 pb-10">
      <section className="rounded-[28px] border border-bg-active/60 bg-bg-secondary/30 px-5 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-4xl">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-text-muted">
              <Boxes size={18} className="text-accent-amber" />
              Commodity
            </div>
            <h1 className="mt-2 max-w-3xl text-2xl font-semibold text-text-primary">
              MCX futures, expiry locks, and ATM options on one paper desk.
            </h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-text-secondary line-clamp-2">
              The commodity surface now keeps runtime state, watchlists, signals, and setup tighter so the page can stay live without the old request burst.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {!killSwitchActive && (!loopActive || startRequired) ? (
              <button
                type="button"
                onClick={() => startAgentMutation.mutate()}
                disabled={startAgentMutation.isPending}
                className={clsx(
                  "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-xs font-semibold transition-colors",
                  "border-accent-blue/40 bg-accent-blue/10 text-accent-blue hover:bg-accent-blue/20",
                  startAgentMutation.isPending && "cursor-not-allowed opacity-60",
                )}
              >
                {startAgentMutation.isPending ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
                Start Commodity Engine
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => killSwitchMutation.mutate(!killSwitchActive)}
              disabled={killSwitchMutation.isPending}
              className={clsx(
                "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-xs font-semibold transition-colors",
                killSwitchActive
                  ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                  : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
                killSwitchMutation.isPending && "cursor-not-allowed opacity-60",
              )}
            >
              {killSwitchActive ? <ShieldCheck size={14} /> : <ShieldAlert size={14} />}
              {killSwitchActive ? "Release Kill Switch" : "Activate Kill Switch"}
            </button>
          </div>
        </div>

        <div className="mt-5 grid gap-4 xl:grid-cols-[1.3fr_1.3fr_1fr]">
          <StrategyLaneCard
            lane={
              futuresLane || {
                key: "commodity_futures",
                title: "Strategy 2 · Futures",
                status: "idle",
                instrument: "MCX futures",
                tracked_symbols: trackedSymbolsCount,
                open_positions: openPositionsCount,
                lots_per_trade: lotsPerTrade,
                broker: "fyers",
                notes: "The futures paper engine is waiting for symbols, a start action, or a live market session.",
              }
            }
          />
          <StrategyLaneCard
            lane={
              optionsLane || {
                key: "commodity_options",
                title: "Strategy 1 · Options",
                status: "monitoring",
                instrument: "MCX ATM CE/PE",
                tracked_symbols: trackedSymbolsCount,
                configured_contracts: selectedExpiryCount,
                broker: "fyers",
                notes: "The options sleeve picks the nearest liquid CE or PE contract and waits for its own 30-minute MACD zero-cross before entering.",
              }
            }
          />
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/35 p-4">
            <div className="text-sm font-semibold text-text-primary">Runtime Rail</div>
            <div className="mt-4 flex flex-wrap gap-2">
              <StatusBadge label={loopActive ? "Loop Active" : startRequired ? "Start Required" : "Paused"} tone={loopActive ? "success" : "warning"} />
              <StatusBadge label={killSwitchActive ? "Kill Switch On" : "Kill Switch Off"} tone={killSwitchActive ? "warning" : "ready"} />
              <StatusBadge label={status?.running ? "Scanning" : "Idle"} tone={status?.running ? "success" : "idle"} />
              {fyersHealth ? (
                <StatusBadge
                  label={`Fyers ${prettifyToken(fyersHealth.status)}`}
                  tone={runtimeHealthTone(fyersHealth.status, fyersHealth.valid)}
                />
              ) : null}
              <StatusBadge
                label={
                  optionHistoryFailures > 0
                    ? `History Warnings ${optionHistoryFailures}`
                    : optionHistorySuccesses > 0
                      ? `History Healthy ${optionHistorySuccesses}`
                      : "History Idle"
                }
                tone={optionHistoryFailures > 0 ? "warning" : optionHistorySuccesses > 0 ? "ready" : "idle"}
              />
            </div>
            <div className="mt-4 space-y-2 text-xs text-text-secondary">
              <div>Last scan: <span className="font-mono text-text-primary">{formatTimestamp(status?.last_run_at)}</span></div>
              <div>Cadence: <span className="font-mono text-text-primary">{status?.scan_interval_seconds ?? 30}s</span></div>
              <div>Futures size: <span className="font-mono text-text-primary">{lotsPerTrade} lot</span></div>
              <div>Options budget: <span className="font-mono text-text-primary">{Math.round(optionCapitalFraction * 100)}%</span></div>
            </div>
            {fyersHealth?.message || optionHistoryLatestFailure ? (
              <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/40 px-3 py-3 text-xs text-text-secondary">
                {fyersHealth?.message ? <div>{fyersHealth.message}</div> : null}
                {optionHistoryLatestFailure ? (
                  <div className={clsx(fyersHealth?.message && "mt-2", optionHistoryFailures > 0 ? "text-accent-amber" : "text-text-muted")}>
                    {optionHistoryFailures > 0
                      ? `Latest history failure: ${optionHistoryLatestFailure}`
                      : optionHistoryLatestFailure}
                  </div>
                ) : null}
              </div>
            ) : null}
            <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/40 px-3 py-3 text-sm text-text-secondary">
              {status?.last_error || status?.last_message || "Waiting for commodity state…"}
            </div>
          </div>
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-sm font-semibold text-text-primary">Desk Views</div>
            <div className="mt-1 text-xs text-text-muted">
              Execution, signal validation, and expiry setup now stay on one desk without repeating the runtime rail.
            </div>
          </div>
          <div className="text-xs text-text-muted">
            {activeTab === "execution"
              ? `${positionRows.length} open positions`
              : activeTab === "signals"
                ? `${futuresRows.length + optionRows.length} live rows`
                : `${contractCatalog.length} configured contracts`}
          </div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <CommodityTabButton
            active={activeTab === "execution"}
            label="Portfolio"
            detail="Combined positions, runtime state, orders, and commentary."
            onClick={() => setActiveTab("execution")}
          />
          <CommodityTabButton
            active={activeTab === "signals"}
            label="Signals"
            detail="15-minute futures MACD+MP validation and 30-minute options triggers."
            onClick={() => setActiveTab("signals")}
          />
          <CommodityTabButton
            active={activeTab === "setup"}
            label="Setup"
            detail="Saved MCX universe and per-symbol expiry map for the option lane."
            onClick={() => setActiveTab("setup")}
          />
        </div>

        <div className="mt-4 grid gap-2 xl:grid-cols-2">
          <StreamStatus
            title="Portfolio"
            isStreamConnected={commodityOverviewQuery.isStreamConnected}
            isShowingSnapshot={commodityOverviewQuery.isShowingSnapshot}
            snapshotSavedAt={commodityOverviewQuery.snapshotSavedAt}
            liveText="positions, orders, and runtime rail update automatically"
            bootstrapText="loading execution state before the live socket takes over"
          />
          <StreamStatus
            title="Watchlists"
            isStreamConnected={commodityWatchlistQuery.isStreamConnected}
            isShowingSnapshot={commodityWatchlistQuery.isShowingSnapshot}
            snapshotSavedAt={commodityWatchlistQuery.snapshotSavedAt}
            liveText="contract map and ATM option board are streaming"
            bootstrapText="loading contract map and ATM option board"
          />
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "execution" && "hidden")}>
        <PanelHeader
          icon={<Activity size={16} className="text-accent-blue" />}
          title="Combined Portfolio"
          detail="Both commodity sleeves share one paper portfolio. Futures and options positions sit in a single ledger, and the execution tape stays below them as a scrollable operational box."
          meta={`${positionRows.length} open | ${actionableFutures.length + actionableOptions.length} actionable`}
        />

        <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile label="Tracked Symbols" value={`${trackedSymbolsCount}`} />
          <MetricTile label="Futures Ready" value={`${readyFuturesCount || actionableFutures.length}`} tone={actionableFutures.length ? "text-accent-green" : undefined} />
          <MetricTile label="Options Ready" value={`${readyOptionsCount || actionableOptions.length}`} tone={actionableOptions.length ? "text-accent-green" : undefined} />
          <MetricTile label="Open Positions" value={`${openPositionsCount}`} />
          <MetricTile label="Equity" value={formatFixed(totalEquity, 2)} />
          <MetricTile label="Realized P&L" value={formatSigned(realizedPnl, 2)} tone={(realizedPnl || 0) >= 0 ? "text-accent-green" : "text-accent-red"} />
          <MetricTile label="Unrealized P&L" value={formatSigned(unrealizedPnl, 2)} tone={(unrealizedPnl || 0) >= 0 ? "text-accent-green" : "text-accent-red"} detail={`Futures ${actionableFutures.length} · Options ${actionableOptions.length}`} />
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<CandlestickChart size={16} className="text-accent-green" />}
            title="Open Positions"
            detail="Futures and options share one table so the portfolio heat is visible in one place instead of split across strategy sections."
            meta={`${positionRows.length} positions`}
          />
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1480px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Sleeve</th>
                  <th className="pb-2 pr-3">Contract</th>
                  <th className="pb-2 pr-3">Side</th>
                  <th className="pb-2 pr-3">Lots</th>
                  <th className="pb-2 pr-3">Qty</th>
                  <th className="pb-2 pr-3">Entry</th>
                  <th className="pb-2 pr-3">Last</th>
                  <th className="pb-2 pr-3">Stop / Target</th>
                  <th className="pb-2 pr-3">Signal</th>
                  <th className="pb-2 pr-3">Regime</th>
                  <th className="pb-2">Open P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positionRows.length ? (
                  positionRows.map((position) => (
                    <tr key={position.position_key || position.live_symbol || position.symbol} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <StatusBadge
                          label={position.strategy_key === "commodity_options" ? "Strategy 1" : "Strategy 2"}
                          tone={position.strategy_key === "commodity_options" ? "warning" : "ready"}
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{position.display_name || position.symbol}</div>
                        <div className="mt-1 font-mono text-[11px] text-text-muted">
                          {position.option_type ? `${position.option_type} ${position.strike ?? "--"} · ${position.expiry || "--"}` : position.symbol}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">
                          {position.contract_unit_label} · {position.quote_unit_label}
                        </div>
                      </td>
                      <td className={clsx("py-3 pr-3 font-semibold", position.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                        {position.option_type ? `BUY ${position.option_type}` : position.action}
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{position.lots}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{position.qty}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{formatFixed(position.entry_price, 2)}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{formatFixed(position.current_price, 2)}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        {formatFixed(position.stop_price, 2)} / {position.target_price != null ? formatFixed(position.target_price, 2) : "--"}
                        {position.target_reached ? <div className="mt-1 text-[11px] text-accent-green">runner active</div> : null}
                      </td>
                      <td className="py-3 pr-3 text-text-muted">
                        {prettifyToken(position.signal_reason)}
                        {position.macd_value != null ? <div className={clsx("mt-1 font-mono text-[11px]", macdTone(position.macd_value))}>MACD {formatIndicator(position.macd_value, 3)}</div> : null}
                      </td>
                      <td className="py-3 pr-3 text-text-secondary">
                        {prettifyToken(position.regime)}
                        {position.mp_poc != null ? (
                          <div className="mt-1 font-mono text-[11px] text-text-muted">
                            POC {position.mp_poc} · VA {position.mp_val ?? "--"} / {position.mp_vah ?? "--"}
                          </div>
                        ) : null}
                      </td>
                      <td className={clsx("py-3 font-mono font-semibold", (position.unrealized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                        {formatSigned(position.unrealized_pnl, 2)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatSigned(position.return_pct, 2, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={11} className="py-10 text-center text-sm text-text-muted">
                      No commodity positions are open right now.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<ArrowDownRight size={16} className="text-accent-blue" />}
            title="Portfolio Ledger"
            detail="Each contract row shows the buy fill, latest exit or mark, timestamps, and incurred P&L so trade outcomes are obvious without scanning the order tape."
            meta={`${portfolioRows.length} rows`}
          />
          <div className="mt-4 max-h-[360px] overflow-auto">
            <table className="w-full min-w-[1380px] text-left text-xs">
              <thead className="sticky top-0 bg-bg-card">
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="py-2 pr-3">Sleeve</th>
                  <th className="py-2 pr-3">Underlying</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Side</th>
                  <th className="py-2 pr-3">Qty</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit / Mark</th>
                  <th className="py-2 pr-3">Signal</th>
                  <th className="py-2">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {portfolioRows.length ? (
                  portfolioRows.map((row) => (
                    <tr key={row.id} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <StatusBadge label={row.sleeve} tone={row.sleeve === "Strategy 1" ? "warning" : "ready"} />
                      </td>
                      <td className="py-3 pr-3 font-medium text-text-primary">{row.underlying}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{row.contract}</td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={row.statusLabel} tone={row.status === "open" ? "ready" : "idle"} />
                      </td>
                      <td className={clsx("py-3 pr-3 font-semibold", row.side.includes("BUY") ? "text-accent-green" : "text-accent-red")}>
                        {row.side}
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{row.qty}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{formatFixed(row.entryPrice, 2)}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.entryTime)}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{formatFixed(row.lastPrice, 2)}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.lastTime)}</div>
                      </td>
                      <td className="py-3 pr-3 text-text-muted">{prettifyToken(row.signalReason)}</td>
                      <td className={clsx("py-3 font-mono font-semibold", valueTone(row.pnl))}>
                        {formatSigned(row.pnl, 2)}
                        <div className="mt-1 text-[11px] text-text-muted">{formatSigned(row.returnPct, 2, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={10} className="py-10 text-center text-sm text-text-muted">
                      No portfolio rows are available yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[1.25fr_0.9fr]">
          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<Activity size={16} className="text-accent-blue" />}
              title="Execution Console"
              detail="Positions stay above. This box scrolls through the agent reasoning, order flow, and safety messages underneath."
              meta={`${commentary.length} notes`}
            />
            <div className="mt-4 max-h-[520px] space-y-3 overflow-y-auto pr-1">
              {commentary.length ? (
                commentary.map((entry, index) => (
                  <div key={`${entry.time}-${index}`} className="rounded-2xl border border-bg-border bg-bg-primary/35 px-3 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <StatusBadge label={entry.tone} tone={entry.tone} />
                      <div className="text-[11px] text-text-muted">{formatTimestamp(entry.time)}</div>
                    </div>
                    <div className="mt-2 text-sm leading-6 text-text-secondary">{entry.message}</div>
                  </div>
                ))
              ) : (
                <div className="rounded-2xl border border-dashed border-bg-border px-3 py-12 text-center text-xs text-text-muted">
                  No execution commentary yet.
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<ArrowUpRight size={16} className="text-accent-green" />}
              title="Recent Orders"
              detail="Entry and exit flow are separated so reversals are easier to read than two adjacent fills with the same side."
              meta={`${orderRows.length} rows`}
            />
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-xs">
                <thead>
                  <tr className="border-b border-bg-border text-text-muted">
                    <th className="pb-2 pr-3">Time</th>
                    <th className="pb-2 pr-3">Sleeve</th>
                    <th className="pb-2 pr-3">Contract</th>
                    <th className="pb-2 pr-3">Flow</th>
                    <th className="pb-2 pr-3">Side</th>
                    <th className="pb-2 pr-3">Lots / Qty</th>
                    <th className="pb-2 pr-3">Fill</th>
                    <th className="pb-2">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {orderRows.length ? (
                    orderRows.map((order) => (
                      <tr key={order.order_id} className="border-b border-bg-border/40 align-top">
                        <td className="py-3 pr-3 text-text-muted">{formatTimestamp(order.time)}</td>
                        <td className="py-3 pr-3">
                          {order.strategy_title ? (
                            <StatusBadge
                              label={order.strategy_key === "commodity_options" ? "Strategy 1" : "Strategy 2"}
                              tone={order.strategy_key === "commodity_options" ? "warning" : "ready"}
                            />
                          ) : (
                            <span className="text-text-muted">--</span>
                          )}
                        </td>
                        <td className="py-3 pr-3 font-mono text-text-primary">{order.symbol}</td>
                        <td className="py-3 pr-3">
                          <StatusBadge label={order.flow || "trade"} tone={order.flow === "exit" ? "warning" : "success"} />
                        </td>
                        <td className={clsx("py-3 pr-3 font-semibold", order.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                          {order.action}
                        </td>
                        <td className="py-3 pr-3 font-mono text-text-primary">
                          {order.lots || "--"} / {order.qty}
                        </td>
                        <td className="py-3 pr-3 font-mono text-text-primary">{formatFixed(order.fill_price, 2)}</td>
                        <td className="py-3 text-text-muted">{prettifyToken(order.reason)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={8} className="py-10 text-center text-sm text-text-muted">
                        No commodity orders yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<ArrowDownRight size={16} className="text-accent-amber" />}
              title="Equity History"
              detail="The old snapshots now render as a line chart so portfolio drift stays readable, while the contract ledger above shows which trade created the move."
              meta={`${reportRows.length} points`}
            />
            {reportRows.length > 1 ? (
              <div className="mt-4">
                <ResponsiveContainer width="100%" height={240}>
                  <LineChart data={reportRows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                    <XAxis dataKey="time" tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(value: string) => formatTimestamp(value)} />
                    <YAxis tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(value: number) => formatCompact(value)} />
                    <Tooltip
                      contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: "6px" }}
                      formatter={(value: number, key: string) => [formatFixed(value, 2), key === "total_equity" ? "Equity" : key === "realized_pnl" ? "Realized" : "Unrealized"]}
                      labelFormatter={(value: string) => formatTimestamp(value)}
                    />
                    <ReferenceLine y={reportRows[0]?.total_equity || 0} stroke="#4a5568" strokeDasharray="3 3" />
                    <Line type="monotone" dataKey="total_equity" stroke="#00d4a3" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="realized_pnl" stroke="#3b82f6" strokeWidth={1.5} dot={false} />
                    <Line type="monotone" dataKey="unrealized_pnl" stroke="#f59e0b" strokeWidth={1.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div className="mt-4 flex h-[240px] items-center justify-center rounded-2xl border border-dashed border-bg-border text-sm text-text-muted">
                No equity history points yet.
              </div>
            )}
          </div>
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "signals" && "hidden")}>
        <PanelHeader
          icon={<Waves size={16} className="text-accent-amber" />}
          title="Signal Validation"
          detail="Futures entries require a 15-minute MACD zero-cross confirmed by Market Profile. Options entries require a 30-minute CE or PE zero-cross on the nearest liquid contract."
          meta={`${futuresRows.length} futures rows · ${optionRows.length} option rows`}
        />

        <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-8">
          <MetricTile label="Futures Rows" value={`${futuresRows.length}`} />
          <MetricTile label="MP Ready" value={`${futuresRows.filter((row) => row.mp_status === "ready").length}`} tone="text-accent-blue" />
          <MetricTile label="Futures Ready" value={`${actionableFutures.length}`} tone={actionableFutures.length ? "text-accent-green" : undefined} />
          <MetricTile label="Futures Open" value={`${positionRows.filter((position) => position.strategy_key === "commodity_futures").length}`} />
          <MetricTile label="Options Rows" value={`${optionRows.length}`} />
          <MetricTile label="Liquid Contracts" value={`${optionRows.filter((row) => row.is_trade_contract_liquid).length}`} tone="text-accent-blue" />
          <MetricTile label="Options Ready" value={`${actionableOptions.length}`} tone={actionableOptions.length ? "text-accent-green" : undefined} />
          <MetricTile
            label="Options Open"
            value={`${positionRows.filter((position) => position.strategy_key === "commodity_options").length}`}
            detail={`${Math.round(optionCapitalFraction * 100)}% budget · ${optionHardStopPct}% stop`}
          />
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<CandlestickChart size={16} className="text-accent-green" />}
            title="Strategy 2 · Futures"
            detail="Entries are only valid when a fresh 15-minute MACD zero-cross matches the live Market Profile gate. MP parameters are surfaced here so the confirmation path is visible."
            meta={`${actionableFutures.length} ready`}
          />
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1760px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Underlying</th>
                  <th className="pb-2 pr-3">Spot</th>
                  <th className="pb-2 pr-3">MACD</th>
                  <th className="pb-2 pr-3">Raw Cross</th>
                  <th className="pb-2 pr-3">MP Gate</th>
                  <th className="pb-2 pr-3">POC / VA</th>
                  <th className="pb-2 pr-3">IB</th>
                  <th className="pb-2 pr-3">Sizing</th>
                  <th className="pb-2 pr-3">Validation</th>
                  <th className="pb-2">Bar Time</th>
                </tr>
              </thead>
              <tbody>
                {futuresRows.length ? (
                  futuresRows.map((row) => (
                    <tr key={`${row.symbol}:${row.bar_time || "na"}`} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{row.display_name || row.underlying || row.symbol}</div>
                        <div className="mt-1 font-mono text-[11px] text-text-muted">{row.symbol}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.strategy_title || "Strategy 2 · Futures"}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-mono text-text-primary">{formatFixed(row.price, 2)}</div>
                        <div className={clsx("mt-1 text-[11px]", valueTone(row.change_pct))}>{formatSigned(row.change_pct, 2, "%")}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className={clsx("font-mono", macdTone(row.macd))}>{formatIndicator(row.macd, 3)}</div>
                        <div className="mt-1 font-mono text-[11px] text-text-muted">
                          signal {formatIndicator(row.macd_signal, 3)} · hist {formatIndicator(row.macd_histogram, 3)}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">ATR {formatIndicator(row.atr, 2)}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="flex flex-wrap gap-2">
                          <StatusBadge label={row.raw_signal || "No cross"} tone={row.raw_signal || "idle"} />
                          {row.signal ? <StatusBadge label={`${row.signal} confirmed`} tone={row.signal} /> : null}
                        </div>
                        <div className="mt-2 text-[11px] text-text-muted">{prettifyToken(row.reason)}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="flex flex-wrap gap-2">
                          <StatusBadge label={row.mp_status || "waiting"} tone={row.mp_status || "idle"} />
                          <StatusBadge label={row.mp_direction || "neutral"} tone={row.mp_direction || "idle"} />
                        </div>
                        <div className="mt-2 text-[11px] text-text-secondary">
                          {prettifyToken(row.mp_day_type)} · {prettifyToken(row.mp_reason)}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.signal_validation_detail || "No MP commentary."}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>POC {row.mp_poc ?? "--"}</div>
                        <div className="mt-1">VA {row.mp_val ?? "--"} / {row.mp_vah ?? "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.mp_periods ?? 0} periods</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>IBH {row.mp_ib_high ?? "--"}</div>
                        <div className="mt-1">IBL {row.mp_ib_low ?? "--"}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.lots_per_trade ?? "--"} lot · {row.default_qty ?? "--"} qty</div>
                        <div className="mt-1 text-[11px] text-text-muted">margin {formatFixed(row.required_margin, 2)}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={prettifyToken(row.signal_validation)} tone={row.signal_validation || "idle"} />
                        <div className="mt-2 max-w-[220px] text-[11px] leading-5 text-text-muted">
                          {row.signal_validation_detail || "No validation detail available."}
                        </div>
                      </td>
                      <td className="py-3 text-text-muted">{formatTimestamp(row.bar_time)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={10} className="py-10 text-center text-sm text-text-muted">
                      No futures watch rows are available yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
          <PanelHeader
            icon={<Boxes size={16} className="text-accent-amber" />}
            title="Strategy 1 · Options"
            detail="The options sleeve waits for its own 30-minute CE or PE zero-cross. Each row shows the expiry-specific MCX future that backs the current option chain, so the board stays aligned with the exchange ladder."
            meta={`${actionableOptions.length} ready · ${commodityATMWatchlistData?.source || "strategy"} live board`}
          />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1960px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Underlying</th>
                  <th className="pb-2 pr-3">Expiry</th>
                  <th className="pb-2 pr-3">Regime</th>
                  <th className="pb-2 pr-3">Trigger</th>
                  <th className="pb-2 pr-3">Trade Contract</th>
                  <th className="pb-2 pr-3">Sizing</th>
                  <th className="pb-2 pr-3">Validation</th>
                  <th className="pb-2 pr-3">CE Snapshot</th>
                  <th className="pb-2">PE Snapshot</th>
                </tr>
              </thead>
              <tbody>
                {optionRows.length ? (
                  optionRows.map((row) => (
                    <tr key={`${row.symbol}:${row.active_expiry || row.expiry}`} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{row.display_name || row.underlying}</div>
                        <div className="mt-1 font-mono text-[11px] text-text-muted">{row.symbol}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.selection_policy ? prettifyToken(row.selection_policy) : "nearest liquid contract"}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.active_expiry || row.expiry}</div>
                        <div className="mt-1 text-[11px] text-text-muted">ATM {row.atm_strike}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.lookup_symbol || row.fyers_symbol || "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.live_source}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="flex flex-wrap gap-2">
                          <StatusBadge label={prettifyToken(row.regime)} tone={row.regime || "idle"} />
                          {row.signal_side ? <StatusBadge label={`${row.signal_side} trigger`} tone={row.signal_side === "CE" ? "BUY" : "SELL"} /> : null}
                        </div>
                        <div className="mt-2 text-[11px] text-text-muted">{prettifyToken(row.signal_reason)}</div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.trade_bar_time ? formatTimestamp(row.trade_bar_time) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">
                          spot {formatFixed(row.spot_price, 2)} · lot {row.lot_size ?? "--"}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">{row.contract_unit_label || "--"} · {row.quote_unit_label || "--"}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-mono text-text-primary">{row.trade_symbol || "--"}</div>
                        <div className="mt-1 font-mono text-[11px] text-text-muted">
                          {row.signal_side || "--"} {row.trade_strike ?? "--"} @ {formatFixed(row.trade_price, 2)}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">
                          {row.is_trade_contract_liquid ? "liquidity pass" : "liquidity pending"}
                        </div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">
                        <div>{row.lots_affordable ?? 0} lots affordable</div>
                        <div className="mt-1 text-[11px] text-text-muted">
                          capital {formatFixed(row.capital_per_trade, 2)}
                        </div>
                        <div className="mt-1 text-[11px] text-text-muted">
                          stop {optionHardStopPct}% · budget {Math.round(optionCapitalFraction * 100)}%
                        </div>
                      </td>
                      <td className="py-3 pr-3">
                        <StatusBadge label={prettifyToken(row.signal_validation)} tone={row.signal_validation || "idle"} />
                        <div className="mt-2 max-w-[220px] text-[11px] leading-5 text-text-muted">
                          {row.signal_validation_detail || "No validation detail available."}
                        </div>
                      </td>
                      <td className="py-3 pr-3"><ATMOptionCell option={row.ce} accent="ce" /></td>
                      <td className="py-3"><ATMOptionCell option={row.pe} accent="pe" /></td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={9} className="py-10 text-center text-sm text-text-muted">
                      No option watch rows are available for the saved expiry map.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className={clsx("space-y-4", activeTab !== "setup" && "hidden")}>
        <PanelHeader
          icon={<Boxes size={16} className="text-accent-amber" />}
          title="Setup"
          detail="Save the MCX futures universe first, then choose the MCX expiry-to-future pair that Strategy 1 should follow for each root."
          meta={`${selectedExpiryCount} expiry selections`}
        />

        <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<Save size={16} className="text-accent-blue" />}
              title="Symbol Universe"
              detail="One primary MCX future per line. The options lane resolves each expiry into the correct MCX future contract from the exchange-style ladder instead of assuming the same symbol for every expiry."
              meta={`${parsedSymbols.length} symbols`}
            />

            <textarea
              value={draftSymbols}
              onChange={(event) => {
                setHasEditedSymbols(true);
                setDraftSymbols(event.target.value);
              }}
              className="terminal-input mt-4 min-h-[240px] w-full resize-y text-sm"
              spellCheck={false}
            />

            <div className="mt-3 text-xs text-text-muted">Example Fyers codes: {EXAMPLE_SYMBOLS.join(", ")}</div>

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => saveConfigMutation.mutate(parsedSymbols)}
                disabled={saveConfigMutation.isPending}
                className={clsx(
                  "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-xs font-semibold transition-colors",
                  "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20",
                  saveConfigMutation.isPending && "cursor-not-allowed opacity-60",
                )}
              >
                <Save size={14} />
                Save Symbols
              </button>
              <button
                type="button"
                onClick={() => {
                  setHasEditedSymbols(false);
                  setDraftSymbols((configSymbols.length ? configSymbols : EXAMPLE_SYMBOLS).join("\n"));
                }}
                className="inline-flex items-center gap-2 rounded-full border border-bg-border px-4 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-accent-blue hover:text-accent-blue"
              >
                <RefreshCw size={14} />
                Reset Draft
              </button>
            </div>

            {(saveConfigMutation.isError || saveConfigMutation.isSuccess) ? (
              <div className={clsx("mt-3 text-xs", saveConfigMutation.isError ? "text-accent-red" : "text-accent-green")}>
                {saveConfigMutation.isError
                  ? (saveError?.response?.data?.detail || "Unable to save symbol list.")
                  : "Commodity symbol universe saved."}
              </div>
            ) : null}

            <StreamStatus
              className="mt-4"
              title="Setup Feed"
              isStreamConnected={commodityWatchlistQuery.isStreamConnected}
              isShowingSnapshot={commodityWatchlistQuery.isShowingSnapshot}
              snapshotSavedAt={commodityWatchlistQuery.snapshotSavedAt}
              liveText="saved universe and expiry map refresh from the live setup feed"
            />

            <div className="mt-6 grid gap-3 sm:grid-cols-3">
              <MetricTile label="Tracked" value={`${trackedSymbolsCount}`} />
              <MetricTile label="Contracts Ready" value={`${contractCatalogSummary.contracts_ready}`} />
              <MetricTile label="Expiry Map" value={`${selectedExpiryCount}`} />
            </div>
          </div>

          <div className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
            <PanelHeader
              icon={<RefreshCw size={16} className="text-accent-green" />}
              title="Per-Symbol Expiry Map"
              detail="MCX option expiries can resolve into different underlying futures. This map shows the exchange-style expiry ladder and which future contract each expiry will query."
              meta={`${contractCatalog.length} contracts`}
            />

            {contractCatalogData?.detail ? (
              <div className="mt-3 text-xs text-accent-amber">{contractCatalogData.detail}</div>
            ) : null}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[980px] text-left text-xs">
                <thead>
                  <tr className="border-b border-bg-border text-text-muted">
                    <th className="pb-2 pr-3">Underlying</th>
                    <th className="pb-2 pr-3">Saved Future</th>
                    <th className="pb-2 pr-3">Active Option Future</th>
                    <th className="pb-2 pr-3">Lot</th>
                    <th className="pb-2 pr-3">MCX Expiry Ladder</th>
                    <th className="pb-2 pr-3">Trade Expiry</th>
                    <th className="pb-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {contractCatalog.length ? (
                    contractCatalog.map((contract) => (
                      <tr key={contract.symbol} className="border-b border-bg-border/40 align-top">
                        <td className="py-3 pr-3 font-medium text-text-primary">{contract.underlying}</td>
                        <td className="py-3 pr-3 font-mono text-text-muted">{contract.symbol}</td>
                        <td className="py-3 pr-3 font-mono text-text-muted">
                          {contract.active_lookup_symbol || contract.lookup_symbol || contract.symbol}
                        </td>
                        <td className="py-3 pr-3 text-text-secondary">
                          {contract.lot_size ? `${contract.lot_size} · ${contract.contract_unit_label}` : "--"}
                        </td>
                        <td className="py-3 pr-3 text-text-secondary">
                          {contract.expiry_mappings?.length ? (
                            <div className="space-y-1">
                              {contract.expiry_mappings.map((mapping) => (
                                <div key={`${contract.symbol}:${mapping.expiry}`} className="font-mono text-[11px] text-text-muted">
                                  {commodityExpiryLookupLabel(mapping)}
                                </div>
                              ))}
                            </div>
                          ) : contract.expiries.length ? (
                            contract.expiries.join(", ")
                          ) : "--"}
                        </td>
                        <td className="py-3 pr-3">
                          <select
                            value={contractExpiryDrafts[contract.symbol] || ""}
                            onChange={(event) => {
                              setHasEditedContractExpiries(true);
                              setContractExpiryDrafts((current) => ({
                                ...current,
                                [contract.symbol]: event.target.value,
                              }));
                            }}
                            disabled={!contract.expiries.length}
                            className="terminal-input min-w-[176px] py-1.5 text-xs"
                          >
                            <option value="">No selection</option>
                            {(contract.expiry_mappings?.length ? contract.expiry_mappings : contract.expiries.map((item) => ({ expiry: item, lookup_symbol: contract.active_lookup_symbol || contract.lookup_symbol || contract.symbol }))).map((item) => (
                              <option key={`${contract.symbol}:${item.expiry}`} value={item.expiry}>
                                {commodityExpiryLookupLabel(item)}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td className="py-3 text-text-secondary">
                          {!contract.has_options ? (
                            <div className="text-accent-amber">{contract.detail || "No option contracts"}</div>
                          ) : contract.selected_expiry ? (
                            <div className="space-y-1">
                              <div className="text-accent-green">
                                Saved {contract.selected_expiry}
                                {contract.selection_locked ? " · locked till expiry" : ""}
                              </div>
                              <div className="font-mono text-[11px] text-text-muted">
                                {contract.selected_lookup_symbol || contract.active_lookup_symbol || contract.lookup_symbol || contract.symbol}
                              </div>
                              <div className="text-[11px] text-text-muted">
                                {contract.selection_policy ? prettifyToken(contract.selection_policy) : "exchange ladder"}
                                {" · "}
                                {contract.quote_unit_label}
                              </div>
                            </div>
                          ) : contract.suggested_expiry ? (
                            <div className="space-y-1">
                              <div className="text-text-secondary">Suggested {contract.suggested_expiry}</div>
                              <div className="font-mono text-[11px] text-text-muted">
                                {contract.active_lookup_symbol || contract.lookup_symbol || contract.symbol}
                              </div>
                              <div className="text-[11px] text-text-muted">{contract.quote_unit_label}</div>
                            </div>
                          ) : (
                            <div className="text-text-muted">Waiting for selection</div>
                          )}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={7} className="py-10 text-center text-sm text-text-muted">
                        Save MCX futures symbols to list commodity option expiries.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  const payload = Object.fromEntries(Object.entries(contractExpiryDrafts).filter(([, value]) => value));
                  saveContractSelectionsMutation.mutate(payload);
                }}
                disabled={saveContractSelectionsMutation.isPending}
                className={clsx(
                  "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-xs font-semibold transition-colors",
                  "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20",
                  saveContractSelectionsMutation.isPending && "cursor-not-allowed opacity-60",
                )}
              >
                <Save size={14} />
                Save Expiry Map
              </button>
              <button
                type="button"
                onClick={() => {
                  setHasEditedContractExpiries(false);
                  const nextDrafts: Record<string, string> = {};
                  for (const contract of contractCatalog) {
                    const persistedExpiry = contract.selected_expiry || contract.active_expiry;
                    if (persistedExpiry) {
                      nextDrafts[contract.symbol] = persistedExpiry;
                    }
                  }
                  setContractExpiryDrafts(nextDrafts);
                }}
                className="inline-flex items-center gap-2 rounded-full border border-bg-border px-4 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-accent-blue hover:text-accent-blue"
              >
                <RefreshCw size={14} />
                Reset Expiries
              </button>
            </div>

            {(saveContractSelectionsMutation.isError || saveContractSelectionsMutation.isSuccess) ? (
              <div className={clsx("mt-3 text-xs", saveContractSelectionsMutation.isError ? "text-accent-red" : "text-accent-green")}>
                {saveContractSelectionsMutation.isError
                  ? "Unable to save expiry selections."
                  : "Commodity expiry map saved."}
              </div>
            ) : null}
          </div>
        </div>
      </section>
    </div>
  );
}
