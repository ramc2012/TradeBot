"use client";

/**
 * Commodity desk · MP+OF · single-viewport dashboard.
 *
 * Layout fits a 1366×768 laptop without scroll:
 *   ┌ header strip (status + decision tiles + actions)
 *   ├ table header (column labels)
 *   ├ 8 instrument rows (one per configured MCX future)
 *   │   each row: symbol, live price, MP profile bar (SVG), CVD/VWAP,
 *   │             trigger badge + confidence, stop hint, position chip
 *   └ split footer (action queue · audit feed)
 *
 * Click any row → modal with full per-instrument context.
 *
 * Streaming model
 * --------------
 * The page is socket-primary. `useLiveSnapshotQuery` subscribes to the
 * overview WebSocket (which now carries positions / orders / trade history
 * inline) and uses HTTP polling as a 60-second heartbeat fallback only.
 * Per-symbol tick sockets layer sub-second LTP updates on top so the
 * price columns update faster than the bar-close cadence the agent runs at.
 */

import { Fragment, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { CirclePlay, RefreshCcw, Settings, X } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";

import {
  api as apiClient,
  getCommodityOverview,
  getCommodityWatchlistSnapshot,
  runCommodityStrategyOnce,
  startCommodityStrategyAgent,
} from "@/lib/api";
import {
  createCommodityOverviewSocket,
  createCommodityWatchlistSocket,
  createTickSocket,
} from "@/lib/websocket";

// ─── Polling cadence ───────────────────────────────────────────────────────
// The sockets push immediately on bar close / position change. Polls are a
// cold-start primer + a heartbeat so we recover from a transient socket drop.
const PRIMER_POLL_MS = 5_000;       // first ~minute, until socket connects
const HEARTBEAT_POLL_MS = 60_000;   // steady-state, only kicks in if socket dies

// ─── Bottom tabs ──────────────────────────────────────────────────────────
type BottomTabKey = "queue" | "orders" | "trades" | "expiry" | "stats" | "audit";
const BOTTOM_TABS: { key: BottomTabKey; label: string }[] = [
  { key: "queue", label: "Queue" },
  { key: "orders", label: "Orders" },
  { key: "trades", label: "Trades" },
  { key: "expiry", label: "Expiry" },
  { key: "stats", label: "Stats" },
  { key: "audit", label: "Audit" },
];

// ─── Types ─────────────────────────────────────────────────────────────────

type WatchRow = {
  symbol?: string | null;
  configured_symbol?: string | null;
  active_lookup_symbol?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  price?: number | null;
  previous_close?: number | null;
  change?: number | null;
  change_pct?: number | null;
  atr?: number | null;
  regime?: string | null;
  mp_day_type?: string | null;
  mp_status?: string | null;
  mp_periods?: number | null;
  mp_session_date?: string | null;
  mp_poc?: number | null;
  mp_vah?: number | null;
  mp_val?: number | null;
  mp_ib_high?: number | null;
  mp_ib_low?: number | null;
  mp_direction?: string | null;
  bar_time?: string | null;
  signal?: string | null;
  candidate_signal?: string | null;
  reason?: string | null;
  entry_style?: string | null;
  confidence?: number | null;
  stop_hint?: number | null;
  target_hint?: number | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  trigger_evidence?: Record<string, unknown> | null;
  cvd_latest?: number | null;
  cvd_session?: number | null;
  cvd_window_delta?: number | null;
  cvd_agrees?: boolean | null;
  cvd_block_active?: boolean | null;
  cvd_divergence?: { kind?: string; strength?: number } | null;
  vwap?: number | null;
  vwap_upper?: number | null;
  vwap_lower?: number | null;
  hvn_count?: number | null;
  lvn_count?: number | null;
  ib_extended_above?: boolean | null;
  ib_extended_below?: boolean | null;
  ib_extension_pct?: number | null;
  lot_size?: number | null;
  lots_per_trade?: number | null;
  default_qty?: number | null;
  contract_unit_label?: string | null;
  quote_unit_label?: string | null;
  prior_session_date?: string | null;
  indicator_timeframe?: string | null;
  live_tick_source?: string | null;
  live_tick_time?: string | null;
  live_tick_stale_seconds?: number | null;
};

type CommoditySnapshotContract = {
  symbol?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  lookup_symbol?: string | null;
  lot_size?: number | null;
  quote_unit_label?: string | null;
  contract_unit_label?: string | null;
  strategy_title?: string | null;
};

type CommodityWatchlistSnapshot = {
  contract_catalog?: { contracts?: CommoditySnapshotContract[] };
};

type CommodityPosition = {
  position_key?: string;
  symbol?: string;
  live_symbol?: string;
  display_name?: string;
  underlying?: string;
  action?: string;
  qty?: number;
  lots?: number;
  lot_size?: number;
  entry_price?: number;
  current_price?: number;
  stop_price?: number;
  target_price?: number;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  entered_at?: string;
  regime?: string | null;
  strategy_key?: string | null;
  entry_style?: string | null;
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

type LatestTickSnapshot = {
  symbol?: string | null;
  ltp?: number | null;
  close?: number | null;
  stale?: boolean | null;
  stale_seconds?: number | null;
  timestamp?: string | null;
  source?: string | null;
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
  config?: Record<string, unknown>;
  strategy_agents?: Record<string, unknown>[];
  strategies?: Record<string, unknown>[];
  summary?: Record<string, unknown>;
  futures_watchlist?: WatchRow[];
  watchlist?: WatchRow[];
  positions?: CommodityPosition[];
  trade_history?: TradeRow[];
  orders?: Order[];
  reports?: Record<string, unknown>[];
  commentary?: { time?: string; tone?: string; message?: string }[];
  signal_audit?: Record<string, unknown>[];
};

// ─── Format helpers ───────────────────────────────────────────────────────

function formatINR(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `₹${Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function formatNumber(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatPct(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(decimals)}%`;
}

function formatSigned(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_00_00_000) return `${(n / 1_00_00_000).toFixed(2)}Cr`;
  if (abs >= 1_00_000) return `${(n / 1_00_000).toFixed(2)}L`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(2)}k`;
  return Math.round(n).toLocaleString("en-IN");
}

function formatIST(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function finiteNumber(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

// ─── Style helpers ─────────────────────────────────────────────────────────

const TRIGGER_COLOR: Record<string, string> = {
  open_drive: "bg-emerald-500/20 text-emerald-200 ring-emerald-500/30",
  ib_break: "bg-sky-500/20 text-sky-200 ring-sky-500/30",
  failed_auction: "bg-amber-500/20 text-amber-200 ring-amber-500/30",
  va_migration: "bg-fuchsia-500/20 text-fuchsia-200 ring-fuchsia-500/30",
  lvn_fade: "bg-slate-500/20 text-slate-300 ring-slate-500/30",
};

const TRIGGER_PRIORITY: Record<string, number> = {
  open_drive: 5,
  ib_break: 4,
  failed_auction: 3,
  va_migration: 2,
  lvn_fade: 1,
};

function triggerLabel(entryStyle: string | null | undefined): string {
  if (!entryStyle) return "—";
  return entryStyle
    .split("_")
    .map((w) => (w.length > 1 ? w[0].toUpperCase() + w.slice(1) : w.toUpperCase()))
    .join(" ");
}

function colorForDelta(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "text-slate-400";
  if (n > 0) return "text-emerald-400";
  if (n < 0) return "text-rose-400";
  return "text-slate-400";
}

// ─── MP Profile bar (SVG) ─────────────────────────────────────────────────
/**
 * Mini visual sized for an in-row at-a-glance look at session structure.
 * Shows the VAL→VAH band (light), the IB band (slightly darker), the POC
 * (single bright tick), and a marker at the live price.
 *
 * Width is flexible (parent decides via className); height is fixed at 22px
 * so the row stays compact. If MP isn't ready yet, renders a "warming up"
 * placeholder so the row layout doesn't jump.
 */
function MPProfileBar({
  row,
  className = "",
}: {
  row: WatchRow;
  className?: string;
}) {
  const price = Number(row.price ?? 0);
  const poc = Number(row.mp_poc ?? 0);
  const vah = Number(row.mp_vah ?? 0);
  const val = Number(row.mp_val ?? 0);
  const ibh = Number(row.mp_ib_high ?? 0);
  const ibl = Number(row.mp_ib_low ?? 0);

  if (!poc || !vah || !val || vah <= val) {
    return (
      <div
        className={`flex h-[22px] items-center justify-center rounded bg-bg-secondary/30 px-2 text-[9.5px] uppercase tracking-wider text-text-muted ${className}`}
      >
        mp warming
      </div>
    );
  }

  // Domain: stretch a little beyond VAL/VAH so the marker doesn't clip.
  const padBase = (vah - val) * 0.25;
  const minDomain = Math.min(val, ibl || val, price || val) - padBase;
  const maxDomain = Math.max(vah, ibh || vah, price || vah) + padBase;
  const span = maxDomain - minDomain || 1;
  const toPct = (v: number) => `${((v - minDomain) / span) * 100}%`;

  const valX = toPct(val);
  const vahX = toPct(vah);
  const pocX = toPct(poc);
  const ibLowX = ibl ? toPct(Math.max(ibl, val)) : null;
  const ibHighX = ibh ? toPct(Math.min(ibh, vah)) : null;
  const priceX = price ? toPct(price) : null;

  const direction = String(row.mp_direction || "").toLowerCase();
  const markerColor =
    direction === "buy" ? "fill-emerald-400" : direction === "sell" ? "fill-rose-400" : "fill-sky-300";

  return (
    <div
      className={`relative h-[22px] rounded bg-bg-secondary/40 ${className}`}
      title={`POC ${formatNumber(poc, 2)} · VAH ${formatNumber(vah, 2)} · VAL ${formatNumber(val, 2)} · IB [${formatNumber(ibl, 2)}–${formatNumber(ibh, 2)}]`}
    >
      <svg
        className="absolute inset-0 h-full w-full"
        preserveAspectRatio="none"
        viewBox="0 0 100 22"
        role="img"
        aria-label="market profile"
      >
        {/* Value area band (VAL → VAH) */}
        <rect
          x={parseFloat(valX)}
          y={4}
          width={parseFloat(vahX) - parseFloat(valX)}
          height={14}
          className="fill-slate-500/30"
        />
        {/* IB band (subtle inside the value area) */}
        {ibLowX && ibHighX ? (
          <rect
            x={parseFloat(ibLowX)}
            y={7}
            width={Math.max(parseFloat(ibHighX) - parseFloat(ibLowX), 0)}
            height={8}
            className="fill-slate-400/30"
          />
        ) : null}
        {/* POC bright tick */}
        <rect x={parseFloat(pocX) - 0.5} y={2} width={1} height={18} className="fill-amber-300/85" />
        {/* Center mid-line for orientation */}
        <line x1={0} x2={100} y1={11} y2={11} className="stroke-text-muted/30" strokeWidth={0.4} />
        {/* Live price marker */}
        {priceX ? (
          <g>
            <line
              x1={parseFloat(priceX)}
              x2={parseFloat(priceX)}
              y1={0}
              y2={22}
              className="stroke-text-primary/70"
              strokeWidth={0.6}
            />
            <polygon
              points={`${parseFloat(priceX) - 1.2},0 ${parseFloat(priceX) + 1.2},0 ${parseFloat(priceX)},2.2`}
              className={markerColor}
            />
          </g>
        ) : null}
      </svg>
    </div>
  );
}

// ─── Trigger badge ─────────────────────────────────────────────────────────

function TriggerBadge({ row }: { row: WatchRow }) {
  const style = String(row.entry_style || "").toLowerCase();
  const colorClass = TRIGGER_COLOR[style] || "bg-bg-secondary/40 text-text-muted ring-bg-secondary/50";
  const conf = Number(row.confidence ?? 0);
  const sig = String(row.signal || row.candidate_signal || "").toUpperCase();
  if (!style) {
    return <span className="text-[10.5px] text-text-muted">no trigger</span>;
  }
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-[1.5px] text-[10px] font-medium uppercase tracking-wide ring-1 ${colorClass}`}
    >
      <span>{triggerLabel(style)}</span>
      {sig ? <span className="rounded bg-black/30 px-1 text-[9px]">{sig}</span> : null}
      {conf > 0 ? <span className="font-mono">{conf.toFixed(2)}</span> : null}
    </span>
  );
}

// ─── CVD chip ──────────────────────────────────────────────────────────────

function CVDChip({ row }: { row: WatchRow }) {
  const session = Number(row.cvd_session ?? row.cvd_latest ?? NaN);
  const agrees = row.cvd_agrees;
  if (!Number.isFinite(session)) {
    return <span className="text-text-muted">—</span>;
  }
  const arrow = session > 0 ? "↑" : session < 0 ? "↓" : "·";
  const tone =
    agrees === true ? "text-emerald-300" : agrees === false ? "text-rose-300" : colorForDelta(session);
  return (
    <span className={`font-mono ${tone}`} title={`Session CVD ${session.toLocaleString("en-IN")}`}>
      {arrow} {compactNumber(Math.abs(session))}
    </span>
  );
}

// ─── Position chip ─────────────────────────────────────────────────────────

function PositionChip({ position }: { position?: CommodityPosition }) {
  if (!position) return <span className="text-text-muted">—</span>;
  const pnl = Number(position.unrealized_pnl ?? 0);
  const ret = Number(position.return_pct ?? 0);
  const side = (position.action || "").toUpperCase();
  const sideColor = side === "BUY" ? "text-emerald-300" : "text-rose-300";
  const pnlColor = pnl >= 0 ? "text-emerald-400" : "text-rose-400";
  return (
    <span className="inline-flex items-center gap-1.5 font-mono">
      <span className={`text-[10.5px] uppercase ${sideColor}`}>{side}</span>
      <span className="text-text-secondary">{position.lots}lt</span>
      <span className={`text-[11px] ${pnlColor}`}>
        {formatSigned(pnl)} ({formatPct(ret, 1)})
      </span>
    </span>
  );
}

// ─── Tick stream hook ──────────────────────────────────────────────────────
/**
 * Subscribe one socket per configured MCX symbol. The page passes the live
 * tick values down into each row so the price column updates faster than
 * the agent's 30s scan cadence.
 */
function useCommodityTickStreams(symbols: string[]): Record<string, LatestTickSnapshot> {
  const [ticks, setTicks] = useState<Record<string, LatestTickSnapshot>>({});
  const key = symbols.join("|");
  useEffect(() => {
    const list = key.split("|").filter(Boolean);
    if (list.length === 0) {
      setTicks({});
      return;
    }
    let active = true;
    const sockets = list.map((symbol) =>
      createTickSocket(symbol, (raw) => {
        if (!active) return;
        const data = raw as Record<string, unknown>;
        const ltp = Number(data.ltp ?? data.last_price ?? data.price ?? data.close);
        if (!Number.isFinite(ltp) || ltp <= 0) return;
        const snapshot: LatestTickSnapshot = {
          symbol,
          ltp,
          stale: Boolean(data.stale),
          stale_seconds: Number.isFinite(Number(data.stale_seconds))
            ? Number(data.stale_seconds)
            : null,
          timestamp: String(data.timestamp || new Date().toISOString()),
          source: String(data.source || "tick_stream"),
        };
        setTicks((cur) => ({ ...cur, [symbol]: snapshot }));
      }),
    );
    return () => {
      active = false;
      sockets.forEach((s) => s.close());
    };
  }, [key]);
  return ticks;
}

function overlayTicks(rows: WatchRow[], ticks: Record<string, LatestTickSnapshot>): WatchRow[] {
  if (!Object.keys(ticks).length) return rows;
  return rows.map((row) => {
    const sym = String(row.symbol || row.configured_symbol || row.active_lookup_symbol || "").toUpperCase();
    const tick = ticks[sym];
    if (!tick || !Number.isFinite(Number(tick.ltp))) return row;
    const price = Number(tick.ltp);
    const prev = Number(row.previous_close || 0);
    return {
      ...row,
      price,
      change: prev > 0 ? price - prev : row.change,
      change_pct: prev > 0 ? ((price - prev) / prev) * 100 : row.change_pct,
      live_tick_source: tick.source,
      live_tick_time: tick.timestamp,
      live_tick_stale_seconds: tick.stale_seconds,
    };
  });
}

// ─── Instrument row ────────────────────────────────────────────────────────

function InstrumentRow({
  row,
  position,
  onClick,
}: {
  row: WatchRow;
  position?: CommodityPosition;
  onClick: () => void;
}) {
  const change = Number(row.change ?? 0);
  const changePct = Number(row.change_pct ?? 0);
  const price = Number(row.price ?? 0);
  const live = row.live_tick_source ? "•" : "";
  return (
    <tr
      onClick={onClick}
      className="cursor-pointer border-t border-bg-secondary/20 hover:bg-bg-secondary/15"
    >
      <td className="py-1.5 pl-2 pr-2">
        <div className="flex items-baseline gap-1.5">
          <span className="text-[12px] font-semibold text-text-primary">
            {row.display_name || row.underlying || row.symbol}
          </span>
          {live ? <span className="text-emerald-400">{live}</span> : null}
        </div>
        <div className="font-mono text-[9.5px] text-text-muted">{row.underlying || row.symbol}</div>
      </td>
      <td className="px-2 text-right font-mono text-[12px] text-text-primary">
        {formatNumber(price, 2)}
      </td>
      <td className={`px-2 text-right font-mono text-[11px] ${colorForDelta(change)}`}>
        <div>{formatSigned(change, 2)}</div>
        <div className="text-[10px] opacity-80">{formatPct(changePct, 2)}</div>
      </td>
      <td className="px-2 align-middle">
        <MPProfileBar row={row} className="w-full min-w-[120px]" />
      </td>
      <td className="px-2 text-right text-[11px]">
        <CVDChip row={row} />
      </td>
      <td className="px-2 text-right font-mono text-[10.5px] text-text-secondary">
        {row.vwap != null ? formatNumber(Number(row.vwap), 2) : "—"}
      </td>
      <td className="px-2">
        <TriggerBadge row={row} />
      </td>
      <td className="px-2 text-right font-mono text-[10.5px] text-text-secondary">
        {row.stop_hint != null ? formatNumber(Number(row.stop_hint), 2) : "—"}
      </td>
      <td className="pl-2 pr-3 text-right text-[11px]">
        <PositionChip position={position} />
      </td>
    </tr>
  );
}

// ─── Action queue ──────────────────────────────────────────────────────────
/** Sort by trigger priority × confidence so the most actionable surfaces top. */
function ActionQueue({
  rows,
  onSelect,
}: {
  rows: WatchRow[];
  onSelect: (symbol: string) => void;
}) {
  const ranked = useMemo(() => {
    const armed = rows.filter((r) => r.entry_style && r.signal);
    return armed
      .map((r) => {
        const style = String(r.entry_style || "").toLowerCase();
        return {
          row: r,
          score: (TRIGGER_PRIORITY[style] || 0) * 10 + Number(r.confidence ?? 0),
        };
      })
      .sort((a, b) => b.score - a.score);
  }, [rows]);

  if (ranked.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No armed triggers this cycle.
      </div>
    );
  }
  return (
    <ul className="space-y-0.5 text-[11px]">
      {ranked.slice(0, 6).map(({ row }) => {
        const sym = String(row.symbol || "");
        return (
          <li
            key={sym}
            onClick={() => onSelect(sym)}
            className="flex cursor-pointer items-baseline justify-between gap-2 rounded px-1.5 py-1 hover:bg-bg-secondary/20"
          >
            <div className="flex items-baseline gap-2 truncate">
              <span className="text-[11.5px] font-semibold text-text-primary">
                {row.display_name || row.underlying}
              </span>
              <TriggerBadge row={row} />
            </div>
            <span className="shrink-0 font-mono text-[10.5px] text-text-muted">
              stop {row.stop_hint != null ? formatNumber(Number(row.stop_hint), 2) : "—"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

// ─── Orders tab ────────────────────────────────────────────────────────────

function OrdersTab({ orders, onSelect }: { orders: Order[]; onSelect: (sym: string) => void }) {
  if (orders.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No orders this session.
      </div>
    );
  }
  return (
    <div className="overflow-y-auto" style={{ maxHeight: 160 }}>
      <table className="w-full text-[10.5px]">
        <thead className="sticky top-0 bg-bg-primary text-[9.5px] uppercase tracking-wider text-text-muted">
          <tr>
            <th className="px-2 py-1 text-left">Time</th>
            <th className="px-2 text-left">Symbol</th>
            <th className="px-2 text-left">Flow</th>
            <th className="px-2 text-left">Action</th>
            <th className="px-2 text-right">Qty</th>
            <th className="px-2 text-right">Fill</th>
            <th className="px-2 text-left">Reason</th>
          </tr>
        </thead>
        <tbody>
          {orders.slice(0, 60).map((o, idx) => (
            <tr
              key={`${o.time}-${idx}`}
              className="cursor-pointer border-t border-bg-secondary/15 hover:bg-bg-secondary/20"
              onClick={() => o.symbol && onSelect(o.symbol)}
            >
              <td className="px-2 py-0.5 font-mono text-text-muted">{formatTime(o.time)}</td>
              <td className="px-2 font-mono">{o.symbol}</td>
              <td className="px-2">{o.flow}</td>
              <td className={`px-2 ${o.action === "BUY" ? "text-emerald-300" : "text-rose-300"}`}>{o.action}</td>
              <td className="px-2 text-right font-mono">{o.qty}</td>
              <td className="px-2 text-right font-mono">{formatNumber(o.fill_price, 2)}</td>
              <td className="px-2 text-text-muted">{o.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Trades tab ────────────────────────────────────────────────────────────

function TradesTab({ trades, onSelect }: { trades: TradeRow[]; onSelect: (sym: string) => void }) {
  if (trades.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No closed trades yet.
      </div>
    );
  }
  const sorted = [...trades].sort(
    (a, b) => new Date(b.exit_time || "").getTime() - new Date(a.exit_time || "").getTime(),
  );
  return (
    <div className="overflow-y-auto" style={{ maxHeight: 160 }}>
      <table className="w-full text-[10.5px]">
        <thead className="sticky top-0 bg-bg-primary text-[9.5px] uppercase tracking-wider text-text-muted">
          <tr>
            <th className="px-2 py-1 text-left">Exited</th>
            <th className="px-2 text-left">Symbol</th>
            <th className="px-2 text-left">Side</th>
            <th className="px-2 text-right">Entry</th>
            <th className="px-2 text-right">Exit</th>
            <th className="px-2 text-right">P&L</th>
            <th className="px-2 text-left">Reason</th>
          </tr>
        </thead>
        <tbody>
          {sorted.slice(0, 80).map((t, idx) => (
            <tr
              key={`${t.exit_time}-${idx}`}
              className="cursor-pointer border-t border-bg-secondary/15 hover:bg-bg-secondary/20"
              onClick={() => t.symbol && onSelect(t.symbol)}
            >
              <td className="px-2 py-0.5 font-mono text-text-muted">{formatIST(t.exit_time)}</td>
              <td className="px-2 font-mono">{t.symbol}</td>
              <td className={`px-2 ${t.action === "BUY" ? "text-emerald-300" : "text-rose-300"}`}>{t.action}</td>
              <td className="px-2 text-right font-mono">{formatNumber(t.entry_price, 2)}</td>
              <td className="px-2 text-right font-mono">{formatNumber(t.exit_price, 2)}</td>
              <td className={`px-2 text-right font-mono ${Number(t.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {formatSigned(Number(t.pnl ?? 0))}
              </td>
              <td className="px-2 text-text-muted">{t.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Expiry tab ────────────────────────────────────────────────────────────
// MCX symbols encode the contract month: MCX:GOLD26JUNFUT → June 2026.
// We surface days-to-expiry per row plus a heads-up when the next contract
// month is closer than 10 trading days (typical roll window for MCX).

const MONTH_INDEX: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};

function parseMCXExpiry(symbol: string | null | undefined): Date | null {
  if (!symbol) return null;
  const m = String(symbol).toUpperCase().match(/^MCX:[A-Z0-9]+?(\d{2})([A-Z]{3})FUT$/);
  if (!m) return null;
  const yy = parseInt(m[1], 10);
  const mi = MONTH_INDEX[m[2]];
  if (mi === undefined) return null;
  // Last day of the contract month — close enough for the roll-window calc;
  // most MCX futures expire on the last business day of the month.
  return new Date(Date.UTC(2000 + yy, mi + 1, 0));
}

function daysTo(date: Date | null): number | null {
  if (!date) return null;
  const ms = date.getTime() - Date.now();
  return Math.ceil(ms / (1000 * 60 * 60 * 24));
}

function ExpiryTab({ rows }: { rows: WatchRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No instruments yet.
      </div>
    );
  }
  return (
    <div className="overflow-y-auto" style={{ maxHeight: 160 }}>
      <table className="w-full text-[10.5px]">
        <thead className="sticky top-0 bg-bg-primary text-[9.5px] uppercase tracking-wider text-text-muted">
          <tr>
            <th className="px-2 py-1 text-left">Underlying</th>
            <th className="px-2 text-left">Active contract</th>
            <th className="px-2 text-left">Expiry</th>
            <th className="px-2 text-right">DTE</th>
            <th className="px-2 text-left">Roll status</th>
            <th className="px-2 text-right">Lot · Tick</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const exp = parseMCXExpiry(row.symbol);
            const dte = daysTo(exp);
            const rolling = dte !== null && dte <= 10;
            const expired = dte !== null && dte < 0;
            const status = expired
              ? { label: "expired · roll now", tone: "text-rose-400" }
              : rolling
                ? { label: `roll window · ${dte}d`, tone: "text-amber-300" }
                : { label: dte === null ? "—" : "active", tone: "text-emerald-300" };
            return (
              <tr key={String(row.symbol || row.underlying)} className="border-t border-bg-secondary/15">
                <td className="px-2 py-0.5 font-medium">{row.display_name || row.underlying}</td>
                <td className="px-2 font-mono">{row.symbol}</td>
                <td className="px-2 font-mono">
                  {exp ? exp.toISOString().slice(0, 10) : "—"}
                </td>
                <td className="px-2 text-right font-mono">{dte ?? "—"}</td>
                <td className={`px-2 ${status.tone}`}>{status.label}</td>
                <td className="px-2 text-right font-mono text-text-muted">
                  {row.lot_size ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ─── Stats tab ─────────────────────────────────────────────────────────────

function StatsTab({
  summary,
  trades,
  positions,
}: {
  summary: Record<string, unknown>;
  trades: TradeRow[];
  positions: CommodityPosition[];
}) {
  const wins = trades.filter((t) => Number(t.pnl ?? 0) > 0);
  const losses = trades.filter((t) => Number(t.pnl ?? 0) < 0);
  const grossProfit = wins.reduce((acc, t) => acc + Number(t.pnl ?? 0), 0);
  const grossLoss = Math.abs(losses.reduce((acc, t) => acc + Number(t.pnl ?? 0), 0));
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;
  const winRate = trades.length > 0 ? wins.length / trades.length : 0;
  const avgWin = wins.length > 0 ? grossProfit / wins.length : 0;
  const avgLoss = losses.length > 0 ? grossLoss / losses.length : 0;
  const totalPnl = trades.reduce((acc, t) => acc + Number(t.pnl ?? 0), 0);
  const realized = Number(summary.realized_pnl ?? totalPnl);
  const totalEquity = Number(summary.total_equity ?? 0);
  const initialCapital = Number(summary.initial_capital ?? 1_000_000);
  const maxDD = Number(summary.max_drawdown ?? 0);
  const openPnl = positions.reduce((acc, p) => acc + Number(p.unrealized_pnl ?? 0), 0);

  // Per-underlying breakdown
  const byUnderlying = useMemo(() => {
    const map: Record<string, { trades: number; pnl: number }> = {};
    for (const t of trades) {
      const root = String(t.symbol || "")
        .replace(/^MCX:/, "")
        .replace(/\d{2}[A-Z]{3}FUT$/, "");
      if (!map[root]) map[root] = { trades: 0, pnl: 0 };
      map[root].trades += 1;
      map[root].pnl += Number(t.pnl ?? 0);
    }
    return Object.entries(map)
      .map(([k, v]) => ({ underlying: k, ...v }))
      .sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl));
  }, [trades]);

  return (
    <div className="overflow-y-auto" style={{ maxHeight: 160 }}>
      <div className="grid grid-cols-12 gap-2 text-[11px]">
        <StatTile k="Equity" v={formatINR(totalEquity)} cols={2} tone={totalEquity >= initialCapital ? "text-emerald-300" : "text-rose-300"} />
        <StatTile k="Realized" v={formatINR(realized)} cols={2} tone={realized >= 0 ? "text-emerald-300" : "text-rose-300"} />
        <StatTile k="Unrealized" v={formatINR(openPnl)} cols={2} tone={openPnl >= 0 ? "text-emerald-300" : "text-rose-300"} />
        <StatTile k="Trades" v={String(trades.length)} cols={1} />
        <StatTile k="Win rate" v={`${(winRate * 100).toFixed(0)}%`} cols={1} />
        <StatTile k="Profit factor" v={Number.isFinite(profitFactor) ? profitFactor.toFixed(2) : "∞"} cols={2} />
        <StatTile k="Max DD" v={`${(maxDD * 100).toFixed(1)}%`} cols={2} tone={maxDD > 0.1 ? "text-amber-300" : ""} />
        <StatTile k="Avg win" v={formatINR(avgWin)} cols={2} />
        <StatTile k="Avg loss" v={formatINR(-avgLoss)} cols={2} />
        <StatTile k="W / L" v={`${wins.length} / ${losses.length}`} cols={2} />
        <StatTile k="Open positions" v={String(positions.length)} cols={2} />
        <StatTile k="Initial" v={formatINR(initialCapital)} cols={2} />
      </div>
      {byUnderlying.length > 0 ? (
        <div className="mt-2 rounded bg-bg-secondary/15 px-2 py-1.5">
          <div className="mb-1 text-[9.5px] uppercase tracking-wider text-text-muted">
            Per-underlying P&L
          </div>
          <table className="w-full text-[10.5px]">
            <tbody>
              {byUnderlying.map((u) => (
                <tr key={u.underlying}>
                  <td className="py-0.5">{u.underlying}</td>
                  <td className="text-right font-mono text-text-muted">{u.trades} tr</td>
                  <td className={`text-right font-mono ${u.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {formatSigned(u.pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function StatTile({
  k,
  v,
  cols = 2,
  tone,
}: {
  k: string;
  v: string;
  cols?: number;
  tone?: string;
}) {
  const span = `col-span-${cols}`;
  return (
    <div className={`${span} rounded bg-bg-secondary/15 px-2 py-1`}>
      <div className="text-[9px] uppercase tracking-wider text-text-muted">{k}</div>
      <div className={`mt-0.5 font-mono text-[12px] ${tone || "text-text-primary"}`}>{v}</div>
    </div>
  );
}

// ─── Strategy settings modal ──────────────────────────────────────────────
// Read-only surface of the agent's current MP+OF parameters and risk caps.
// PUT endpoints to mutate these can be added later; for now this is a
// transparent "what the agent will do" panel — replaces what the old
// /strategy page used to hold for the commodity sleeve.

function StrategyModal({
  config,
  strategies,
  onClose,
}: {
  config: Record<string, unknown>;
  strategies: Record<string, unknown>[];
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const meta = strategies[0] || {};

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative max-h-[88vh] w-full max-w-3xl overflow-y-auto rounded-lg bg-bg-primary p-5 ring-1 ring-bg-secondary/40"
      >
        <button
          type="button"
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-text-muted hover:bg-bg-secondary/30 hover:text-text-primary"
        >
          <X className="h-4 w-4" />
        </button>

        <div className="mb-3">
          <div className="text-base font-semibold text-text-primary">
            {String(meta.title || "MP+OF Futures")} · settings
          </div>
          <div className="text-[11px] text-text-muted">
            {String(meta.instrument || "")} · {String(meta.broker || "")}
          </div>
        </div>

        <div className="grid grid-cols-12 gap-3 text-[11.5px]">
          <Section title="Signal engine" cols={6}>
            <Row k="Timeframe" v={String(config.futures_timeframe ?? "1minute")} />
            <Row k="MP period (min)" v={String(config.mp_period_minutes ?? 15)} />
            <Row k="MP min periods" v={String(config.mp_min_periods ?? 4)} />
            <Row k="CVD anchor hour IST" v={String(config.cvd_anchor_hour_ist ?? 9)} />
            <Row k="Min stop %" v={`${(Number(config.futures_min_stop_pct ?? 0) * 100).toFixed(2)}%`} />
            <Row k="Trail × ATR" v={String(config.futures_trail_atr_multiplier ?? "—")} />
            <Row k="Target × R" v={String(config.futures_target_arm_r_multiplier ?? "—")} />
            <Row k="Min hold (bars)" v={String(config.futures_min_hold_bars ?? "—")} />
          </Section>

          <Section title="Risk caps" cols={6}>
            <Row k="Lots per trade" v={String(config.lots_per_trade ?? 1)} />
            <Row k="Daily loss cap" v={formatINR(Number(config.commodity_daily_loss_limit ?? 0))} />
            <Row k="Per-underlying loss" v={formatINR(Number(config.commodity_underlying_daily_loss_limit ?? 0))} />
            <Row k="Max drawdown" v={`${Number(config.commodity_max_drawdown_pct ?? 0).toFixed(1)}%`} />
            <Row k="Stop cooldown" v={`${config.commodity_stop_cooldown_minutes ?? "—"} min`} />
          </Section>

          <Section title="Universe" cols={12}>
            <div className="col-span-2 grid grid-cols-4 gap-1 text-[10.5px]">
              {((config.symbols as string[]) || []).map((s) => (
                <span key={s} className="rounded bg-bg-secondary/25 px-1.5 py-0.5 font-mono text-text-secondary">
                  {s.replace(/^MCX:/, "").replace(/\d{2}[A-Z]{3}FUT$/, "")}
                </span>
              ))}
            </div>
          </Section>

          <Section title="Triggers (priority high → low)" cols={12}>
            <div className="col-span-2 grid grid-cols-5 gap-2 text-[10.5px]">
              {["open_drive", "ib_break", "failed_auction", "va_migration", "lvn_fade"].map((t) => (
                <div key={t} className="rounded bg-bg-secondary/20 px-2 py-1">
                  <div className="text-[9.5px] uppercase tracking-wider text-text-muted">{triggerLabel(t)}</div>
                </div>
              ))}
            </div>
          </Section>

          <div className="col-span-12 rounded bg-bg-secondary/15 px-3 py-2 text-[11px] text-text-secondary">
            {String((meta.notes as string) || "MP+OF futures sleeve. Triggers evaluate on closed 1-min bars.")}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Audit feed ────────────────────────────────────────────────────────────

function AuditFeed({ events }: { events: AuditEvent[] }) {
  if (!events.length) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No audit events yet this session.
      </div>
    );
  }
  return (
    <ul className="space-y-0.5 text-[11px]">
      {events.slice(0, 6).map((event, idx) => (
        <li
          key={`${event.created_at}-${idx}`}
          className="flex gap-2 rounded px-1.5 py-1 hover:bg-bg-secondary/15"
        >
          <span className="w-[58px] shrink-0 font-mono text-[10px] text-text-muted">
            {formatTime(event.created_at)}
          </span>
          <span className="w-[68px] shrink-0 text-[10px] uppercase tracking-wider text-text-secondary">
            {event.underlying || event.symbol || ""}
          </span>
          <span className="truncate text-[10.5px] text-text-primary">
            {(event.event_type || "").replace("mp_signal.", "")}{" "}
            <span className="text-text-muted">{(event.message || "").slice(0, 80)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

// ─── Instrument detail modal ──────────────────────────────────────────────

function InstrumentDetailModal({
  row,
  position,
  recentTrades,
  recentOrders,
  recentAudit,
  onClose,
}: {
  row: WatchRow;
  position?: CommodityPosition;
  recentTrades: TradeRow[];
  recentOrders: Order[];
  recentAudit: AuditEvent[];
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const evidence = (row.trigger_evidence || {}) as Record<string, unknown>;
  const evidenceEntries = Object.entries(evidence);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative max-h-[88vh] w-full max-w-5xl overflow-y-auto rounded-lg bg-bg-primary p-5 ring-1 ring-bg-secondary/40"
      >
        <button
          type="button"
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-text-muted hover:bg-bg-secondary/30 hover:text-text-primary"
        >
          <X className="h-4 w-4" />
        </button>

        {/* Title */}
        <div className="mb-3 flex items-baseline justify-between">
          <div>
            <div className="text-base font-semibold text-text-primary">
              {row.display_name} <span className="font-mono text-[12px] text-text-muted">{row.symbol}</span>
            </div>
            <div className="text-[11px] text-text-muted">
              {row.contract_unit_label} · {row.quote_unit_label} · bar {formatIST(row.bar_time)}
            </div>
          </div>
          <div className="text-right">
            <div className={`font-mono text-2xl font-semibold ${colorForDelta(Number(row.change ?? 0))}`}>
              {formatNumber(Number(row.price ?? 0), 2)}
            </div>
            <div className={`font-mono text-[11px] ${colorForDelta(Number(row.change ?? 0))}`}>
              {formatSigned(Number(row.change ?? 0), 2)} ({formatPct(Number(row.change_pct ?? 0), 2)})
            </div>
          </div>
        </div>

        {/* Profile bar (full-width) */}
        <div className="mb-3">
          <MPProfileBar row={row} className="h-[28px]" />
          <div className="mt-1 flex justify-between text-[10px] text-text-muted">
            <span>VAL {formatNumber(Number(row.mp_val ?? 0), 2)}</span>
            <span>POC {formatNumber(Number(row.mp_poc ?? 0), 2)}</span>
            <span>VAH {formatNumber(Number(row.mp_vah ?? 0), 2)}</span>
          </div>
        </div>

        {/* 3-column context grid */}
        <div className="mb-4 grid grid-cols-12 gap-3 text-[11.5px]">
          <Section title="Market Profile" cols={4}>
            <Row k="Day type" v={String(row.mp_day_type ?? "—")} />
            <Row k="Status" v={String(row.mp_status ?? "—")} />
            <Row k="Periods" v={String(row.mp_periods ?? "—")} />
            <Row k="POC" v={formatNumber(Number(row.mp_poc ?? 0), 2)} />
            <Row k="VAH" v={formatNumber(Number(row.mp_vah ?? 0), 2)} />
            <Row k="VAL" v={formatNumber(Number(row.mp_val ?? 0), 2)} />
            <Row k="IB high" v={formatNumber(Number(row.mp_ib_high ?? 0), 2)} />
            <Row k="IB low" v={formatNumber(Number(row.mp_ib_low ?? 0), 2)} />
            <Row k="IB extended" v={row.ib_extended_above ? "above" : row.ib_extended_below ? "below" : "no"} />
            <Row k="IB ext %" v={row.ib_extension_pct != null ? `${(Number(row.ib_extension_pct) * 100).toFixed(1)}%` : "—"} />
            <Row k="Prior session" v={row.prior_session_date || "—"} />
          </Section>

          <Section title="Order Flow" cols={4}>
            <Row k="CVD session" v={compactNumber(Number(row.cvd_session ?? row.cvd_latest))} />
            <Row k="CVD agree?" v={row.cvd_agrees == null ? "—" : row.cvd_agrees ? "yes" : "no"} />
            <Row k="CVD window Δ" v={compactNumber(Number(row.cvd_window_delta ?? 0))} />
            <Row k="Divergence" v={row.cvd_divergence?.kind ? `${row.cvd_divergence.kind} ${(row.cvd_divergence.strength ?? 0).toString().slice(0, 4)}` : "—"} />
            <Row k="VWAP" v={formatNumber(Number(row.vwap ?? 0), 2)} />
            <Row k="VWAP +σ" v={formatNumber(Number(row.vwap_upper ?? 0), 2)} />
            <Row k="VWAP −σ" v={formatNumber(Number(row.vwap_lower ?? 0), 2)} />
            <Row k="HVN / LVN" v={`${row.hvn_count ?? 0} / ${row.lvn_count ?? 0}`} />
          </Section>

          <Section title="Trigger" cols={4}>
            <Row k="Signal" v={String(row.signal || "—")} />
            <Row k="Candidate" v={String(row.candidate_signal || "—")} />
            <Row k="Style" v={triggerLabel(row.entry_style)} />
            <Row k="Confidence" v={row.confidence != null ? Number(row.confidence).toFixed(2) : "—"} />
            <Row k="Stop hint" v={row.stop_hint != null ? formatNumber(Number(row.stop_hint), 2) : "—"} />
            <Row k="Target hint" v={row.target_hint != null ? formatNumber(Number(row.target_hint), 2) : "—"} />
            <Row k="Validation" v={row.signal_validation || "—"} />
            <Row k="ATR (1m)" v={row.atr != null ? formatNumber(Number(row.atr), 4) : "—"} />
          </Section>
        </div>

        {/* Reason + evidence */}
        {row.signal_validation_detail ? (
          <div className="mb-4 rounded bg-bg-secondary/15 px-3 py-2 text-[11.5px] text-text-secondary">
            {row.signal_validation_detail}
          </div>
        ) : null}
        {evidenceEntries.length > 0 ? (
          <div className="mb-4 grid grid-cols-2 gap-3 rounded bg-bg-secondary/10 p-2 text-[10.5px]">
            <div className="col-span-2 text-[10px] uppercase tracking-wider text-text-muted">Trigger evidence</div>
            {evidenceEntries.map(([k, v]) => (
              <Row key={k} k={k} v={typeof v === "number" ? formatNumber(v, 4) : String(v)} />
            ))}
          </div>
        ) : null}

        {/* Position (if any) */}
        {position ? (
          <div className="mb-4 grid grid-cols-12 gap-3 rounded bg-bg-secondary/15 p-3 text-[11.5px]">
            <div className="col-span-12 text-[10px] uppercase tracking-wider text-text-muted">
              Open position
            </div>
            <Row k="Side" v={String(position.action || "—")} cols={3} />
            <Row k="Lots" v={String(position.lots || "—")} cols={3} />
            <Row k="Qty" v={String(position.qty || "—")} cols={3} />
            <Row k="Entered" v={formatIST(position.entered_at)} cols={3} />
            <Row k="Entry" v={formatNumber(Number(position.entry_price ?? 0), 2)} cols={3} />
            <Row k="Current" v={formatNumber(Number(position.current_price ?? 0), 2)} cols={3} />
            <Row k="Stop" v={formatNumber(Number(position.stop_price ?? 0), 2)} cols={3} />
            <Row k="Target" v={formatNumber(Number(position.target_price ?? 0), 2)} cols={3} />
            <Row k="Unrealized" v={formatINR(Number(position.unrealized_pnl ?? 0), 0)} cols={6} />
            <Row k="Return" v={formatPct(Number(position.return_pct ?? 0), 2)} cols={6} />
          </div>
        ) : null}

        {/* Recent activity */}
        <div className="grid grid-cols-12 gap-3">
          <div className="col-span-6">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">
              Recent trades · {recentTrades.length}
            </div>
            <ul className="space-y-0.5 text-[10.5px]">
              {recentTrades.length === 0 ? (
                <li className="text-text-muted">no closed trades</li>
              ) : (
                recentTrades.slice(0, 5).map((t, idx) => (
                  <li key={`t-${idx}`} className="flex justify-between gap-2 font-mono">
                    <span>{formatIST(t.exit_time)}</span>
                    <span>{t.action}</span>
                    <span>{formatNumber(t.entry_price, 2)} → {formatNumber(t.exit_price, 2)}</span>
                    <span className={`${Number(t.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {formatSigned(Number(t.pnl ?? 0))}
                    </span>
                  </li>
                ))
              )}
            </ul>
          </div>
          <div className="col-span-6">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">
              Recent audit · {recentAudit.length}
            </div>
            <ul className="space-y-0.5 text-[10.5px]">
              {recentAudit.length === 0 ? (
                <li className="text-text-muted">no events</li>
              ) : (
                recentAudit.slice(0, 6).map((e, idx) => (
                  <li key={`a-${idx}`} className="flex gap-2">
                    <span className="w-[58px] shrink-0 font-mono text-text-muted">{formatTime(e.created_at)}</span>
                    <span className="truncate">{(e.event_type || "").replace("mp_signal.", "")} {e.message ? `· ${e.message.slice(0, 100)}` : ""}</span>
                  </li>
                ))
              )}
            </ul>
          </div>
        </div>

        {/* Orders for this symbol */}
        {recentOrders.length > 0 ? (
          <div className="mt-3">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">
              Recent orders · {recentOrders.length}
            </div>
            <ul className="space-y-0.5 text-[10.5px]">
              {recentOrders.slice(0, 4).map((o, idx) => (
                <li key={`o-${idx}`} className="flex justify-between gap-2 font-mono">
                  <span>{formatIST(o.time)}</span>
                  <span>{o.flow}</span>
                  <span>{o.action}</span>
                  <span>{o.qty}</span>
                  <span>{formatNumber(o.fill_price, 2)}</span>
                  <span className="text-text-muted">{o.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// Small inline helpers for the modal grid.
function Section({
  title,
  cols = 12,
  children,
}: {
  title: string;
  cols?: number;
  children: React.ReactNode;
}) {
  return (
    <div className={`col-span-12 lg:col-span-${cols} rounded bg-bg-secondary/15 p-2`}>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">{title}</div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">{children}</div>
    </div>
  );
}
function Row({ k, v, cols }: { k: string; v: string; cols?: number }) {
  return (
    <div className={cols ? `col-span-${cols} flex justify-between gap-1` : "flex justify-between gap-1"}>
      <span className="text-text-muted">{k}</span>
      <span className="font-mono text-text-primary">{v}</span>
    </div>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────

export default function CommodityLivePage() {
  const queryClient = useQueryClient();

  // Socket-primary streams. Falls back to the heartbeat poll if the socket
  // ever disconnects.
  const overviewQuery = useLiveSnapshotQuery<Record<string, unknown>>({
    queryKey: ["commodity-live", "overview"],
    queryFn: async () => (await getCommodityOverview()).data,
    streamFactory: (onData, onStatusChange) =>
      createCommodityOverviewSocket(
        (payload) => onData(payload as Record<string, unknown>),
        onStatusChange,
      ),
    storageKey: "commodity-live:overview",
    streamWhenHidden: true,
    refetchInterval: HEARTBEAT_POLL_MS,
    refetchIntervalInBackground: true,
  });

  const watchlistSnapshotQuery = useLiveSnapshotQuery<CommodityWatchlistSnapshot>({
    queryKey: ["commodity-live", "watchlist-snapshot"],
    queryFn: async () =>
      (await getCommodityWatchlistSnapshot()).data as CommodityWatchlistSnapshot,
    streamFactory: (onData, onStatusChange) =>
      createCommodityWatchlistSocket(
        (payload) => onData(payload as CommodityWatchlistSnapshot),
        onStatusChange,
      ),
    storageKey: "commodity-live:watchlist-snapshot",
    streamWhenHidden: true,
    refetchInterval: HEARTBEAT_POLL_MS,
    refetchIntervalInBackground: true,
  });

  const auditQuery = useQuery({
    queryKey: ["commodity-live", "audit"],
    queryFn: async () =>
      (await apiClient.get("/api/audit/events?market=commodity&limit=60")).data,
    refetchInterval: PRIMER_POLL_MS,
    refetchIntervalInBackground: true,
  });

  // Mutations
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

  // ── Derive view models ──────────────────────────────────────────────
  const status = (overviewQuery.data?.status ?? {}) as StatusPayload;
  const summary = (status.summary ?? {}) as Record<string, unknown>;
  const config = (status.config ?? {}) as Record<string, unknown>;
  const strategies = (status.strategies ?? []) as Record<string, unknown>[];

  // Socket-streamed positions / orders / trade history are inside the
  // overview payload. No more separate polls for those.
  const positions = (status.positions ?? []) as CommodityPosition[];
  const orders = (status.orders ?? []) as Order[];
  const trades = (status.trade_history ?? []) as TradeRow[];

  const contracts = useMemo(
    () =>
      (watchlistSnapshotQuery.data?.contract_catalog?.contracts ?? []) as CommoditySnapshotContract[],
    [watchlistSnapshotQuery.data?.contract_catalog?.contracts],
  );

  // Build the row list: prefer the agent's live watchlist; fall back to the
  // contract catalog so the table never empties when the agent is between
  // scans.
  const runtimeRows = (status.futures_watchlist ?? status.watchlist ?? []) as WatchRow[];
  const fallbackRows: WatchRow[] = useMemo(
    () =>
      contracts.map((c) => ({
        symbol: c.symbol,
        underlying: c.underlying,
        display_name: c.display_name || c.underlying || c.symbol,
        lot_size: c.lot_size ?? undefined,
        quote_unit_label: c.quote_unit_label || undefined,
        contract_unit_label: c.contract_unit_label || undefined,
      })),
    [contracts],
  );
  const baseRows = runtimeRows.length > 0 ? runtimeRows : fallbackRows;

  // Subscribe to per-symbol tick streams so prices update faster than the
  // agent scan cadence.
  const tickSymbols = useMemo(
    () =>
      Array.from(
        new Set(
          baseRows
            .map((r) => String(r.symbol || r.active_lookup_symbol || "").toUpperCase())
            .filter((s) => s.startsWith("MCX:")),
        ),
      ),
    [baseRows],
  );
  const ticks = useCommodityTickStreams(tickSymbols);
  const rows = useMemo(() => overlayTicks(baseRows, ticks), [baseRows, ticks]);

  // Audit events.
  const auditEvents = useMemo(
    () => (auditQuery.data?.events ?? []) as AuditEvent[],
    [auditQuery.data?.events],
  );

  // Index positions by symbol for O(1) lookup in the row renderer.
  const positionBySymbol = useMemo(() => {
    const map: Record<string, CommodityPosition> = {};
    for (const p of positions) {
      if (p.symbol) map[p.symbol] = p;
      if (p.live_symbol && !map[p.live_symbol]) map[p.live_symbol] = p;
    }
    return map;
  }, [positions]);

  // ── Selection + UI state ───────────────────────────────────────────
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [bottomTab, setBottomTab] = useState<BottomTabKey>("queue");
  const [strategyModalOpen, setStrategyModalOpen] = useState(false);
  const selectedRow = useMemo(
    () => rows.find((r) => r.symbol === selectedSymbol) || null,
    [rows, selectedSymbol],
  );
  const symbolFilteredTrades = useMemo(
    () => trades.filter((t) => t.symbol === selectedSymbol),
    [trades, selectedSymbol],
  );
  const symbolFilteredOrders = useMemo(
    () => orders.filter((o) => o.symbol === selectedSymbol),
    [orders, selectedSymbol],
  );
  const symbolFilteredAudit = useMemo(
    () =>
      auditEvents.filter(
        (e) => e.symbol === selectedSymbol || e.underlying === selectedRow?.underlying,
      ),
    [auditEvents, selectedRow?.underlying, selectedSymbol],
  );

  // ── Header / status tiles ──────────────────────────────────────────
  const totalEquity = finiteNumber(summary.total_equity);
  const initialCapital = finiteNumber(summary.initial_capital, 1_000_000);
  const dayRealized = finiteNumber(summary.day_pnl);
  const unrealized = positions.reduce(
    (acc, p) => acc + finiteNumber(p.unrealized_pnl),
    0,
  );
  const dayPnl = dayRealized + unrealized;
  const equityPct =
    initialCapital > 0 ? ((totalEquity - initialCapital) / initialCapital) * 100 : 0;
  const armedCount = rows.filter((r) => r.entry_style && r.signal).length;

  const killActive = Boolean(status.kill_switch_active);
  const running = Boolean(status.running);
  const loopActive = Boolean(status.loop_active);
  const startRequired = Boolean(status.start_required);

  const statusTone = killActive
    ? "bg-rose-500/15 text-rose-300"
    : running
      ? "bg-emerald-500/15 text-emerald-300"
      : loopActive
        ? "bg-sky-500/15 text-sky-300"
        : startRequired
          ? "bg-amber-500/15 text-amber-200"
          : "bg-bg-secondary/40 text-text-muted";
  const statusLabel = killActive
    ? "kill switch"
    : running
      ? "scanning"
      : loopActive
        ? "armed"
        : startRequired
          ? "start required"
          : "idle";

  const isStreaming =
    overviewQuery.isStreamConnected || watchlistSnapshotQuery.isStreamConnected;
  const streamTone = isStreaming
    ? "bg-emerald-500/15 text-emerald-300"
    : overviewQuery.hasSnapshot
      ? "bg-amber-500/15 text-amber-200"
      : "bg-bg-secondary/40 text-text-muted";
  const streamLabel = isStreaming
    ? `LIVE · ${Object.keys(ticks).length}/${tickSymbols.length} ticks`
    : overviewQuery.hasSnapshot
      ? "snapshot · awaiting socket"
      : "loading";

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-bg-primary text-text-primary">
      {/* ── Header ─────────────────────────────────────────────────── */}
      <header className="flex flex-wrap items-center gap-2 border-b border-bg-secondary/30 px-3 py-2">
        <h1 className="text-base font-semibold tracking-tight">Commodity · MP+OF</h1>
        <span className={`rounded-md px-2 py-0.5 text-[10.5px] font-medium uppercase tracking-wide ${statusTone}`}>
          {statusLabel}
        </span>
        <span className={`rounded-md px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.14em] ${streamTone}`}>
          {streamLabel}
        </span>
        <span className="hidden text-[10.5px] text-text-muted sm:inline">
          {formatIST(status.last_run_at)} · {(status.last_message || "").slice(0, 120)}
        </span>

        {/* Decision tiles inline */}
        <div className="ml-auto flex flex-wrap items-baseline gap-3 text-[11px]">
          <Tile
            label="Equity"
            value={formatINR(totalEquity)}
            tone={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}
            detail={formatPct(equityPct, 2)}
          />
          <Tile
            label="Day P&L"
            value={formatINR(dayPnl)}
            tone={dayPnl >= 0 ? "text-emerald-400" : "text-rose-400"}
            detail={`r ${formatINR(dayRealized)} · u ${formatINR(unrealized)}`}
          />
          <Tile
            label="Open / Armed"
            value={`${positions.length} / ${armedCount}`}
            detail={`${rows.length} tracked`}
          />
        </div>

        <div className="ml-1 flex items-center gap-1">
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending || killActive || loopActive}
            className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            title="Start commodity agent"
          >
            <CirclePlay className="h-3.5 w-3.5" />
            {loopActive ? "Running" : "Start"}
          </button>
          <button
            type="button"
            onClick={() => runOnceMutation.mutate()}
            disabled={runOnceMutation.isPending}
            className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            title="Run one scan"
          >
            <RefreshCcw className="h-3.5 w-3.5" />
            Scan
          </button>
          <button
            type="button"
            onClick={() => setStrategyModalOpen(true)}
            className="inline-flex items-center gap-1 rounded-md bg-bg-secondary/25 px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary"
            title="MP+OF rules · risk caps · universe"
          >
            <Settings className="h-3.5 w-3.5" />
            Strategy
          </button>
          <Link
            href="/settings"
            className="rounded-md bg-bg-secondary/25 px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary"
          >
            Settings
          </Link>
        </div>
      </header>

      {/* ── Instrument table ───────────────────────────────────────── */}
      <main className="flex flex-1 min-h-0 flex-col px-3 py-2">
        <div className="overflow-hidden rounded-md border border-bg-secondary/30">
          <table className="w-full table-fixed">
            <thead>
              <tr className="bg-bg-secondary/20 text-[10px] uppercase tracking-[0.14em] text-text-muted">
                <th className="w-[12%] py-1.5 pl-2 pr-2 text-left">Instrument</th>
                <th className="w-[9%] px-2 text-right">LTP</th>
                <th className="w-[9%] px-2 text-right">Δ</th>
                <th className="w-[24%] px-2 text-left">MP Profile · live</th>
                <th className="w-[8%] px-2 text-right">CVD</th>
                <th className="w-[8%] px-2 text-right">VWAP</th>
                <th className="w-[14%] px-2 text-left">Trigger</th>
                <th className="w-[8%] px-2 text-right">Stop</th>
                <th className="w-[8%] pl-2 pr-3 text-right">Position</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-6 text-center text-[11px] text-text-muted">
                    No instruments yet — waiting for the first scan.
                  </td>
                </tr>
              ) : (
                rows.map((row) => (
                  <InstrumentRow
                    key={String(row.symbol || row.underlying)}
                    row={row}
                    position={positionBySymbol[String(row.symbol || "")]}
                    onClick={() => setSelectedSymbol(String(row.symbol || ""))}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* ── Tabbed footer: queue · orders · trades · expiry · stats · audit ── */}
        <div className="mt-2 flex flex-1 min-h-0 flex-col rounded-md border border-bg-secondary/30">
          <nav className="flex items-baseline gap-1 border-b border-bg-secondary/30 px-1.5 py-1">
            {BOTTOM_TABS.map((t) => {
              const isActive = bottomTab === t.key;
              const count =
                t.key === "queue"
                  ? armedCount
                  : t.key === "orders"
                    ? orders.length
                    : t.key === "trades"
                      ? trades.length
                      : t.key === "audit"
                        ? auditEvents.length
                        : null;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setBottomTab(t.key)}
                  className={`rounded px-2 py-0.5 text-[10.5px] uppercase tracking-[0.14em] transition-colors ${
                    isActive
                      ? "bg-bg-secondary/50 text-text-primary"
                      : "text-text-muted hover:bg-bg-secondary/20 hover:text-text-primary"
                  }`}
                >
                  {t.label}
                  {count !== null ? (
                    <span className="ml-1 text-[9.5px] text-text-muted">{count}</span>
                  ) : null}
                </button>
              );
            })}
            <span className="ml-auto text-[10px] text-text-muted">
              {bottomTab === "queue"
                ? "priority × confidence"
                : bottomTab === "orders"
                  ? "newest first · click row → instrument modal"
                  : bottomTab === "trades"
                    ? "closed trades · newest first"
                    : bottomTab === "expiry"
                      ? "MCX futures · roll window 10d"
                      : bottomTab === "stats"
                        ? "portfolio statistics"
                        : "mp_signal.* events"}
            </span>
          </nav>
          <div className="flex-1 min-h-0 overflow-hidden px-2 py-1">
            {bottomTab === "queue" ? (
              <ActionQueue rows={rows} onSelect={(sym) => setSelectedSymbol(sym)} />
            ) : null}
            {bottomTab === "orders" ? (
              <OrdersTab orders={orders} onSelect={(sym) => setSelectedSymbol(sym)} />
            ) : null}
            {bottomTab === "trades" ? (
              <TradesTab trades={trades} onSelect={(sym) => setSelectedSymbol(sym)} />
            ) : null}
            {bottomTab === "expiry" ? <ExpiryTab rows={rows} /> : null}
            {bottomTab === "stats" ? (
              <StatsTab summary={summary} trades={trades} positions={positions} />
            ) : null}
            {bottomTab === "audit" ? <AuditFeed events={auditEvents} /> : null}
          </div>
        </div>
      </main>

      {/* ── Strategy modal ─────────────────────────────────────────── */}
      {strategyModalOpen ? (
        <StrategyModal
          config={config}
          strategies={strategies}
          onClose={() => setStrategyModalOpen(false)}
        />
      ) : null}

      {/* ── Detail modal ───────────────────────────────────────────── */}
      {selectedRow ? (
        <InstrumentDetailModal
          row={selectedRow}
          position={positionBySymbol[String(selectedRow.symbol || "")]}
          recentTrades={symbolFilteredTrades}
          recentOrders={symbolFilteredOrders}
          recentAudit={symbolFilteredAudit}
          onClose={() => setSelectedSymbol(null)}
        />
      ) : null}
    </div>
  );
}

// ─── Header tile ───────────────────────────────────────────────────────────

function Tile({
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
    <div className="flex items-baseline gap-2">
      <span className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</span>
      <span className={`font-mono text-[13px] font-semibold ${tone || "text-text-primary"}`}>
        {value}
      </span>
      {detail ? (
        <span className="font-mono text-[10px] text-text-muted">{detail}</span>
      ) : null}
    </div>
  );
}
