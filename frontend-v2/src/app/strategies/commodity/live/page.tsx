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
  getCommodityProfileHistory,
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
type BottomTabKey =
  | "positions"
  | "queue"
  | "orders"
  | "trades"
  | "expiry"
  | "stats"
  | "audit";
// Order matters — leftmost is the default when nothing is selected.
// Positions sits first so the open book is the headline view when the
// desk has exposure; Queue is the default when flat.
const BOTTOM_TABS: { key: BottomTabKey; label: string }[] = [
  { key: "positions", label: "Positions" },
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
  // Full TPO surface — added in v5 so the detail modal can render the
  // classic letter histogram instead of just POC/VAH/VAL summary.
  mp_tpo_letters?: Record<string, string> | null;
  mp_tpo_counts?: Record<string, number> | null;
  mp_tick_size?: number | null;
  mp_high?: number | null;
  mp_low?: number | null;
  mp_single_prints?: number[] | null;
  mp_poor_high?: boolean | null;
  mp_poor_low?: boolean | null;
  mp_buying_tail?: number[] | null;
  mp_selling_tail?: number[] | null;
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
  // Full TPO of the prior session — surfaced by the agent so the detail
  // modal's "Last day" tile renders the completed auction immediately,
  // without waiting for tomorrow's roll into the persisted history.
  prior_session_profile?: {
    session_date?: string | null;
    poc?: number | null;
    vah?: number | null;
    val?: number | null;
    ib_high?: number | null;
    ib_low?: number | null;
    high?: number | null;
    low?: number | null;
    tick_size?: number | null;
    tpo_letters?: Record<string, string> | null;
    tpo_counts?: Record<string, number> | null;
    single_prints?: number[] | null;
    poor_high?: boolean | null;
    poor_low?: boolean | null;
    period_count?: number | null;
  } | null;
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
  status?: string;
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
  today_trades?: TradeRow[];
  historical_trades?: TradeRow[];
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

function istDateKey(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  } catch {
    return "";
  }
}

function isClosedTrade(trade: TradeRow): boolean {
  return Boolean(trade.exit_time) && String(trade.status || "").toLowerCase() !== "open";
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
/**
 * Regime-colored border for the MP visual so each row's auction state
 * registers from the row outline alone.
 */
function regimeBorderClass(dayType: string | null | undefined): string {
  const t = String(dayType || "").toLowerCase();
  if (t === "trend_up") return "ring-emerald-500/60";
  if (t === "trend_down") return "ring-rose-500/60";
  if (t === "balance_above_poc") return "ring-emerald-400/30";
  if (t === "balance_below_poc") return "ring-rose-400/30";
  if (t === "balance") return "ring-slate-500/40";
  return "ring-bg-secondary/40";
}

function MPProfileBar({
  row,
  className = "",
  height = 22,
  showLegend = false,
}: {
  row: WatchRow;
  className?: string;
  height?: number;
  showLegend?: boolean;
}) {
  const price = Number(row.price ?? 0);
  const poc = Number(row.mp_poc ?? 0);
  const vah = Number(row.mp_vah ?? 0);
  const val = Number(row.mp_val ?? 0);
  const ibh = Number(row.mp_ib_high ?? 0);
  const ibl = Number(row.mp_ib_low ?? 0);
  const vwap = Number(row.vwap ?? 0);
  const vwapU = Number(row.vwap_upper ?? 0);
  const vwapL = Number(row.vwap_lower ?? 0);
  const borderRing = regimeBorderClass(row.mp_day_type);

  if (!poc || !vah || !val || vah <= val) {
    return (
      <div
        className={`flex items-center justify-center rounded bg-bg-secondary/30 px-2 text-[9.5px] uppercase tracking-wider text-text-muted ring-1 ring-bg-secondary/40 ${className}`}
        style={{ height }}
      >
        mp warming
      </div>
    );
  }

  // Stretch the domain past VAL/VAH so VWAP bands and the live marker
  // don't clip on extension days.
  const candidates = [val, vah, ibl, ibh, price, vwap, vwapU, vwapL].filter((n) => n > 0);
  const lowEdge = Math.min(...candidates);
  const highEdge = Math.max(...candidates);
  const padBase = (highEdge - lowEdge) * 0.06 || (vah - val) * 0.1;
  const minDomain = lowEdge - padBase;
  const maxDomain = highEdge + padBase;
  const span = maxDomain - minDomain || 1;
  const toX = (v: number) => ((v - minDomain) / span) * 100;

  const valX = toX(val);
  const vahX = toX(vah);
  const pocX = toX(poc);
  const ibLowX = ibl ? toX(Math.max(ibl, lowEdge)) : null;
  const ibHighX = ibh ? toX(Math.min(ibh, highEdge)) : null;
  const priceX = price ? toX(price) : null;
  const vwapX = vwap ? toX(vwap) : null;
  const vwapUX = vwapU ? toX(vwapU) : null;
  const vwapLX = vwapL ? toX(vwapL) : null;

  const direction = String(row.mp_direction || "").toLowerCase();
  const markerColor =
    direction === "buy" ? "fill-emerald-400" : direction === "sell" ? "fill-rose-400" : "fill-sky-300";

  const tooltip = [
    `POC ${formatNumber(poc, 2)}`,
    `VA [${formatNumber(val, 2)}–${formatNumber(vah, 2)}]`,
    ibl && ibh ? `IB [${formatNumber(ibl, 2)}–${formatNumber(ibh, 2)}]` : null,
    vwap ? `VWAP ${formatNumber(vwap, 2)}` : null,
    price ? `LTP ${formatNumber(price, 2)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      className={`relative overflow-hidden rounded-md bg-gradient-to-b from-bg-secondary/30 to-bg-secondary/50 ring-1 ${borderRing} ${className}`}
      style={{ height }}
      title={tooltip}
    >
      <svg
        className="absolute inset-0 h-full w-full"
        preserveAspectRatio="none"
        viewBox={`0 0 100 ${height}`}
        role="img"
        aria-label="market profile"
      >
        <defs>
          <linearGradient id="vaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(148 163 184 / 0.35)" />
            <stop offset="100%" stopColor="rgb(148 163 184 / 0.2)" />
          </linearGradient>
        </defs>

        {/* Value-area band (VAL → VAH) — primary auction zone */}
        <rect
          x={valX}
          y={height * 0.18}
          width={Math.max(vahX - valX, 0.5)}
          height={height * 0.64}
          fill="url(#vaGrad)"
          rx={1.2}
        />
        {/* VAL/VAH edges as subtle vertical guides */}
        <line x1={valX} x2={valX} y1={height * 0.12} y2={height * 0.88} className="stroke-text-muted/50" strokeWidth={0.35} strokeDasharray="0.5 0.7" />
        <line x1={vahX} x2={vahX} y1={height * 0.12} y2={height * 0.88} className="stroke-text-muted/50" strokeWidth={0.35} strokeDasharray="0.5 0.7" />

        {/* IB band — inner amber pill */}
        {ibLowX !== null && ibHighX !== null ? (
          <rect
            x={ibLowX}
            y={height * 0.36}
            width={Math.max(ibHighX - ibLowX, 0.4)}
            height={height * 0.28}
            className="fill-amber-300/20"
            rx={1}
          />
        ) : null}

        {/* VWAP ±σ band as a soft full-height tint */}
        {vwapLX !== null && vwapUX !== null ? (
          <rect
            x={Math.min(vwapLX, vwapUX)}
            y={0}
            width={Math.abs(vwapUX - vwapLX) || 0.3}
            height={height}
            className="fill-sky-400/8"
          />
        ) : null}

        {/* POC tick — the single highest-volume price */}
        <rect x={pocX - 0.6} y={height * 0.06} width={1.2} height={height * 0.88} className="fill-amber-300/95" rx={0.5} />

        {/* VWAP dashed sky-blue line */}
        {vwapX !== null ? (
          <line
            x1={vwapX}
            x2={vwapX}
            y1={height * 0.08}
            y2={height * 0.92}
            className="stroke-sky-400"
            strokeWidth={0.9}
            strokeDasharray="1.8 1.2"
          />
        ) : null}

        {/* Horizontal mid-line — quiet baseline */}
        <line
          x1={0}
          x2={100}
          y1={height / 2}
          y2={height / 2}
          className="stroke-text-muted/20"
          strokeWidth={0.3}
        />

        {/* Live price marker — top and bottom arrows + faint vertical line */}
        {priceX !== null ? (
          <g>
            <line
              x1={priceX}
              x2={priceX}
              y1={0}
              y2={height}
              className="stroke-text-primary/70"
              strokeWidth={0.55}
            />
            <polygon
              points={`${priceX - 1.6},0 ${priceX + 1.6},0 ${priceX},${height * 0.2}`}
              className={markerColor}
            />
            <polygon
              points={`${priceX - 1.6},${height} ${priceX + 1.6},${height} ${priceX},${height - height * 0.2}`}
              className={markerColor}
            />
          </g>
        ) : null}
      </svg>

      {/* Tiny in-bar labels for the major levels — only when there's room */}
      {showLegend ? (
        <div className="pointer-events-none absolute inset-0 flex items-end justify-between px-1 pb-[1.5px] text-[8.5px] font-mono text-text-muted/90">
          <span>{formatNumber(val, 0)}</span>
          <span className="text-amber-300/90">{formatNumber(poc, 0)}</span>
          <span>{formatNumber(vah, 0)}</span>
        </div>
      ) : null}
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
  // Confidence ladder: tiny 5-segment bar under the badge. Segments fill
  // proportionally to confidence (so 0.6 fills 3 segments brightly + 1 dim).
  // Borrowed from poker / sports-betting UIs which need to telegraph
  // certainty without taking up real estate.
  const filled = Math.round(conf * 5);
  return (
    <span className="inline-flex flex-col gap-[2px]">
      <span
        className={`inline-flex items-center gap-1 rounded px-1.5 py-[1.5px] text-[10px] font-medium uppercase tracking-wide ring-1 ${colorClass}`}
      >
        <span>{triggerLabel(style)}</span>
        {sig ? <span className="rounded bg-black/30 px-1 text-[9px]">{sig}</span> : null}
        {conf > 0 ? <span className="font-mono">{conf.toFixed(2)}</span> : null}
      </span>
      {conf > 0 ? (
        <span className="flex gap-[2px]">
          {[0, 1, 2, 3, 4].map((i) => (
            <span
              key={i}
              className={`h-[3px] w-[10px] rounded-sm ${
                i < filled ? "bg-current opacity-70" : "bg-current opacity-15"
              }`}
            />
          ))}
        </span>
      ) : null}
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
  const cur = Number(position.current_price ?? 0);
  const stop = Number(position.stop_price ?? 0);
  // Distance to stop in absolute price points (signed: positive = comfortable,
  // negative = stop already breached on the wrong side).
  const stopDist =
    cur > 0 && stop > 0
      ? side === "BUY"
        ? cur - stop
        : stop - cur
      : null;
  return (
    <span className="inline-flex items-baseline gap-1 font-mono leading-none">
      <span className={`text-[10px] uppercase ${sideColor}`}>{side}</span>
      <span className="text-[9.5px] text-text-secondary">{position.lots}</span>
      <span className={`text-[11px] ${pnlColor}`}>{formatSigned(pnl)}</span>
      <span className="text-[9.5px] text-text-muted">({formatPct(ret, 1)})</span>
      {stopDist !== null ? (
        <span
          className={`text-[9.5px] ${stopDist >= 0 ? "text-text-muted" : "text-rose-400"}`}
          title={`Stop ${formatNumber(stop, 2)} · distance ${formatSigned(stopDist, 2)}`}
        >
          ⊥{formatSigned(stopDist, 2)}
        </span>
      ) : null}
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
  zebra,
  onClick,
}: {
  row: WatchRow;
  position?: CommodityPosition;
  zebra: boolean;
  onClick: () => void;
}) {
  const change = Number(row.change ?? 0);
  const changePct = Number(row.change_pct ?? 0);
  const price = Number(row.price ?? 0);
  const vwap = Number(row.vwap ?? 0);
  const live = Boolean(row.live_tick_source);
  // Single-symbol-only naming (no display-name duplication). MCX:GOLD26JUNFUT
  // → GOLD; for "ALUMINI" / "ZINCMINI" keep mini suffix because users read it
  // as the contract size.
  const symbol = String(row.underlying || row.symbol || "")
    .replace(/^MCX:/, "")
    .replace(/\d{2}[A-Z]{3}FUT$/, "")
    .toUpperCase();
  // VWAP differential: helps trader instantly read "price vs auction mean".
  const vwapDelta = vwap > 0 && price > 0 ? price - vwap : null;

  return (
    <tr
      onClick={onClick}
      className={`cursor-pointer transition-colors ${
        zebra ? "bg-bg-secondary/[0.07]" : "bg-transparent"
      } hover:bg-bg-secondary/25`}
    >
      <td className="py-2 pl-3 pr-2 align-middle">
        <div className="flex items-center gap-2">
          {live ? (
            <span className="relative h-2 w-2 flex-none rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(74,222,128,0.6)]" title="streaming" />
          ) : (
            <span className="h-2 w-2 flex-none rounded-full bg-text-muted/40" title="not streaming" />
          )}
          <span className="font-mono text-[13px] font-semibold tracking-wide text-text-primary">
            {symbol}
          </span>
        </div>
      </td>
      <td className="px-2 text-right align-middle font-mono text-[13px] font-medium text-text-primary">
        {formatNumber(price, 2)}
      </td>
      <td className={`px-2 text-right align-middle font-mono text-[11px] ${colorForDelta(change)}`}>
        <div>{formatSigned(change, 2)}</div>
        <div className="text-[10px] opacity-80">{formatPct(changePct, 2)}</div>
      </td>
      <td className="px-2 text-right align-middle font-mono text-[11px] text-text-secondary">
        <div>{vwap > 0 ? formatNumber(vwap, 2) : "—"}</div>
        {vwapDelta !== null ? (
          <div className={`text-[10px] ${colorForDelta(vwapDelta)}`}>{formatSigned(vwapDelta, 2)}</div>
        ) : null}
      </td>
      <td className="px-3 align-middle">
        <MPProfileBar row={row} className="w-full min-w-[180px]" height={30} showLegend />
      </td>
      <td className="px-2 text-right align-middle text-[12px]">
        <CVDChip row={row} />
      </td>
      <td className="px-2 align-middle">
        <TriggerBadge row={row} />
      </td>
      <td className="px-2 text-right align-middle font-mono text-[11px] text-text-secondary">
        {row.stop_hint != null ? formatNumber(Number(row.stop_hint), 2) : "—"}
      </td>
      <td className="pl-2 pr-3 text-right align-middle text-[11px]">
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
  const [triggerFilter, setTriggerFilter] = useState<string>("all");
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
  const filtered = ranked.filter(
    (item) => triggerFilter === "all" || item.row.entry_style === triggerFilter,
  );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterSelect
          value={triggerFilter}
          onChange={setTriggerFilter}
          options={["all", "open_drive", "ib_break", "failed_auction", "va_migration", "lvn_fade"]}
        />
        <FilterCount n={filtered.length} total={ranked.length} />
      </FilterBar>
      {filtered.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11px] text-text-muted">
          {ranked.length === 0
            ? "No armed triggers this cycle."
            : "No triggers match the filter."}
        </div>
      ) : (
        <ul className="flex-1 space-y-0.5 overflow-y-auto text-[11px]">
          {filtered.slice(0, 20).map(({ row }) => {
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
      )}
    </div>
  );
}

// ─── Positions tab ─────────────────────────────────────────────────────────
// Detailed view of every open position with a Bloomberg-style aggregate
// footer (total P&L summed across the book). Each row also carries a
// risk-gauge mini-bar showing where current price sits between the stop
// and the target — at-a-glance "how close to stop am I?" without doing math.

function hoursBetween(now: Date, isoStart: string | undefined): string {
  if (!isoStart) return "—";
  const start = new Date(isoStart).getTime();
  if (!Number.isFinite(start)) return "—";
  const mins = Math.max(0, Math.floor((now.getTime() - start) / 60000));
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const r = mins % 60;
  if (hrs < 24) return r ? `${hrs}h ${r}m` : `${hrs}h`;
  const days = Math.floor(hrs / 24);
  const rh = hrs % 24;
  return rh ? `${days}d ${rh}h` : `${days}d`;
}

/** Horizontal risk gauge: |stop| ←──●──→ |target|, with the marker positioned
 * by price's progress between the two. Color goes red→amber→green based on
 * which side of the entry it's on, so a glance at the cell reads "risk on,
 * holding, in profit, near target". Modeled on car fuel gauges.
 */
function RiskGauge({
  side,
  entry,
  current,
  stop,
  target,
}: {
  side: string;
  entry: number;
  current: number;
  stop: number;
  target: number;
}) {
  if (!entry || !current || !stop) {
    return <span className="text-text-muted">—</span>;
  }
  // Normalise so 0 = stop, 1 = entry, 2 = target (for BUY); flip for SELL.
  const toPct = (p: number) => {
    if (side === "BUY") {
      const span = (target || entry * 1.01) - stop;
      return Math.max(0, Math.min(1, (p - stop) / (span || 1)));
    } else {
      const span = stop - (target || entry * 0.99);
      return Math.max(0, Math.min(1, (stop - p) / (span || 1)));
    }
  };
  const entryPct = toPct(entry);
  const currentPct = toPct(current);
  // Tone the marker by where current sits relative to entry.
  const tone =
    currentPct >= entryPct + 0.15
      ? "fill-emerald-400"
      : currentPct >= entryPct - 0.05
        ? "fill-amber-300"
        : "fill-rose-400";
  return (
    <div
      className="relative h-3 w-full rounded bg-gradient-to-r from-rose-500/25 via-bg-secondary/40 to-emerald-500/25 ring-1 ring-bg-secondary/40"
      title={`Stop @ ${formatNumber(stop, 2)} → entry @ ${formatNumber(entry, 2)} → target @ ${target ? formatNumber(target, 2) : "—"} · now @ ${formatNumber(current, 2)}`}
    >
      <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 12" preserveAspectRatio="none">
        {/* entry tick */}
        <line
          x1={entryPct * 100}
          x2={entryPct * 100}
          y1={1}
          y2={11}
          className="stroke-text-muted/60"
          strokeWidth={0.6}
          strokeDasharray="1 1"
        />
        {/* current marker */}
        <circle cx={currentPct * 100} cy={6} r={2.4} className={tone} />
      </svg>
    </div>
  );
}

function PositionsTab({
  positions,
  onSelect,
}: {
  positions: CommodityPosition[];
  onSelect: (sym: string) => void;
}) {
  const [sideFilter, setSideFilter] = useState<"all" | "BUY" | "SELL">("all");
  const [pnlFilter, setPnlFilter] = useState<"all" | "winning" | "losing">("all");
  const filtered = positions.filter((p) => {
    if (sideFilter !== "all" && p.action !== sideFilter) return false;
    if (pnlFilter === "winning" && Number(p.unrealized_pnl ?? 0) <= 0) return false;
    if (pnlFilter === "losing" && Number(p.unrealized_pnl ?? 0) >= 0) return false;
    return true;
  });
  const now = new Date();
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterSelect
          value={sideFilter}
          onChange={(v) => setSideFilter(v as "all" | "BUY" | "SELL")}
          options={["all", "BUY", "SELL"]}
        />
        <FilterSelect
          value={pnlFilter}
          onChange={(v) => setPnlFilter(v as "all" | "winning" | "losing")}
          options={["all", "winning", "losing"]}
        />
        <FilterCount n={filtered.length} total={positions.length} />
      </FilterBar>
      {filtered.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11.5px] text-text-muted">
          {positions.length === 0
            ? "Desk is flat — no open positions."
            : "No positions match the filter."}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <table className="w-full text-[11px]">
            <thead className="sticky top-0 bg-bg-primary text-[9.5px] uppercase tracking-[0.14em] text-text-muted">
              <tr>
                <th className="py-1.5 pl-2 pr-2 text-left">Symbol</th>
                <th className="px-2 text-left">Side · Lots</th>
                <th className="px-2 text-right">Entry</th>
                <th className="px-2 text-right">Current</th>
                <th className="px-2 text-right">Stop · Δ</th>
                <th className="px-2 text-right">Target · Δ</th>
                <th className="px-3 text-left">Risk gauge</th>
                <th className="px-2 text-right">Unrealized</th>
                <th className="px-2 text-right">Return</th>
                <th className="px-2 text-left">Trigger</th>
                <th className="px-2 text-right">Held</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((p, idx) => {
                const cur = Number(p.current_price ?? 0);
                const entry = Number(p.entry_price ?? 0);
                const stop = Number(p.stop_price ?? 0);
                const target = Number(p.target_price ?? 0);
                const side = (p.action || "").toUpperCase();
                const pnl = Number(p.unrealized_pnl ?? 0);
                const ret = Number(p.return_pct ?? 0);
                const stopDist =
                  cur > 0 && stop > 0 ? (side === "BUY" ? cur - stop : stop - cur) : null;
                const tgtDist =
                  cur > 0 && target > 0 ? (side === "BUY" ? target - cur : cur - target) : null;
                const sym = String(p.symbol || p.live_symbol || "")
                  .replace(/^MCX:/, "")
                  .replace(/\d{2}[A-Z]{3}FUT$/, "");
                return (
                  <tr
                    key={p.position_key || `${p.symbol}-${idx}`}
                    onClick={() => p.symbol && onSelect(p.symbol)}
                    className={`cursor-pointer border-t border-bg-secondary/15 ${idx % 2 ? "bg-bg-secondary/[0.06]" : ""} hover:bg-bg-secondary/20`}
                  >
                    <td className="py-1.5 pl-2 pr-2 align-middle">
                      <span className="font-mono text-[12px] font-semibold text-text-primary">{sym}</span>
                    </td>
                    <td className="px-2 align-middle">
                      <span className={`text-[10.5px] uppercase ${side === "BUY" ? "text-emerald-300" : "text-rose-300"}`}>
                        {side}
                      </span>
                      <span className="ml-1 font-mono text-[10.5px] text-text-secondary">{p.lots}</span>
                    </td>
                    <td className="px-2 text-right align-middle font-mono">{formatNumber(entry, 2)}</td>
                    <td className="px-2 text-right align-middle font-mono">{formatNumber(cur, 2)}</td>
                    <td className="px-2 text-right align-middle">
                      <div className="font-mono">{formatNumber(stop, 2)}</div>
                      {stopDist !== null ? (
                        <div className={`text-[10px] ${stopDist >= 0 ? "text-text-muted" : "text-rose-400"}`}>
                          {formatSigned(stopDist, 2)}
                        </div>
                      ) : null}
                    </td>
                    <td className="px-2 text-right align-middle">
                      <div className="font-mono">{target > 0 ? formatNumber(target, 2) : "—"}</div>
                      {tgtDist !== null ? (
                        <div className={`text-[10px] ${tgtDist >= 0 ? "text-text-muted" : "text-emerald-300"}`}>
                          {formatSigned(tgtDist, 2)}
                        </div>
                      ) : null}
                    </td>
                    <td className="px-3 align-middle">
                      <RiskGauge
                        side={side}
                        entry={entry}
                        current={cur}
                        stop={stop}
                        target={target}
                      />
                    </td>
                    <td className={`px-2 text-right align-middle font-mono ${pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {formatSigned(pnl)}
                    </td>
                    <td className={`px-2 text-right align-middle font-mono ${ret >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                      {formatPct(ret, 1)}
                    </td>
                    <td className="px-2 align-middle text-[10.5px] text-text-muted">
                      {triggerLabel(p.entry_style)}
                    </td>
                    <td className="px-2 text-right align-middle font-mono text-text-muted">
                      {hoursBetween(now, p.entered_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            {/* Bloomberg/IB-style aggregate footer — always visible at the
                bottom of the table so the trader's reading flow ends at
                the grand total instead of an ambiguous last row. */}
            {filtered.length > 0 ? (
              <PositionsFooter positions={filtered} />
            ) : null}
          </table>
        </div>
      )}
    </div>
  );
}

/** Total-P&L footer row. Aggregates the *filtered* book so when the user
 * filters "losing only" the footer shows just the drag from losers; same
 * for "winning only". Glanceable: large signed P&L + return-on-capital + a
 * compact W/L count. The colour follows the sign of the aggregate.
 */
function PositionsFooter({ positions }: { positions: CommodityPosition[] }) {
  const total = positions.reduce((acc, p) => acc + Number(p.unrealized_pnl ?? 0), 0);
  const wins = positions.filter((p) => Number(p.unrealized_pnl ?? 0) > 0).length;
  const losses = positions.filter((p) => Number(p.unrealized_pnl ?? 0) < 0).length;
  const grossEntry = positions.reduce(
    (acc, p) => acc + Number(p.entry_price ?? 0) * Number(p.qty ?? 0),
    0,
  );
  const grossPct = grossEntry > 0 ? (total / grossEntry) * 100 : 0;
  const arrow = total >= 0 ? "▲" : "▼";
  const tone =
    total > 0 ? "text-emerald-300" : total < 0 ? "text-rose-300" : "text-text-secondary";
  const cellTone =
    total > 0
      ? "bg-emerald-500/[0.07]"
      : total < 0
        ? "bg-rose-500/[0.07]"
        : "bg-bg-secondary/30";
  return (
    <tfoot className={`sticky bottom-0 ${cellTone} backdrop-blur-sm`}>
      <tr className="border-t-2 border-bg-secondary/40 text-[11.5px]">
        <td className="py-2 pl-2 pr-2 align-middle font-semibold uppercase tracking-[0.14em] text-text-muted">
          Total ({positions.length})
        </td>
        <td className="px-2 align-middle text-[10.5px] text-text-muted" colSpan={2}>
          W {wins} · L {losses}
        </td>
        <td className="px-2 align-middle text-right text-[10.5px] text-text-muted" colSpan={3}>
          gross entry {formatINR(grossEntry, 0)}
        </td>
        <td className={`px-2 text-right align-middle font-mono text-[14px] font-semibold ${tone}`}>
          {arrow} {formatSigned(total, 0)}
        </td>
        <td className={`px-2 text-right align-middle font-mono text-[12px] ${tone}`}>
          {formatPct(grossPct, 2)}
        </td>
        <td className="px-2 align-middle text-[10.5px] text-text-muted" colSpan={2}>
          unrealised P&L
        </td>
      </tr>
    </tfoot>
  );
}

// ─── Orders tab ────────────────────────────────────────────────────────────

function OrdersTab({ orders, onSelect }: { orders: Order[]; onSelect: (sym: string) => void }) {
  const [symFilter, setSymFilter] = useState("");
  const [sideFilter, setSideFilter] = useState<"all" | "BUY" | "SELL">("all");
  const [flowFilter, setFlowFilter] = useState<"all" | "entry" | "exit">("all");
  const filtered = orders.filter((o) => {
    if (symFilter && !String(o.symbol || "").toLowerCase().includes(symFilter.toLowerCase())) return false;
    if (sideFilter !== "all" && o.action !== sideFilter) return false;
    if (flowFilter !== "all" && o.flow !== flowFilter) return false;
    return true;
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterText placeholder="symbol contains…" value={symFilter} onChange={setSymFilter} />
        <FilterSelect value={sideFilter} onChange={(v) => setSideFilter(v as "all" | "BUY" | "SELL")} options={["all", "BUY", "SELL"]} />
        <FilterSelect value={flowFilter} onChange={(v) => setFlowFilter(v as "all" | "entry" | "exit")} options={["all", "entry", "exit"]} />
        <FilterCount n={filtered.length} total={orders.length} />
      </FilterBar>
      {filtered.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11px] text-text-muted">
          {orders.length === 0 ? "No orders today. (Full history → Reports)" : "No orders match the filter."}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
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
              {filtered.slice(0, 80).map((o, idx) => (
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
      )}
    </div>
  );
}

// ─── Filter bar primitives ────────────────────────────────────────────────

function FilterBar({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1 flex flex-wrap items-center gap-1.5 border-b border-bg-secondary/20 pb-1">
      {children}
    </div>
  );
}
function FilterText({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="h-5 w-32 rounded bg-bg-secondary/30 px-1.5 text-[10.5px] text-text-primary placeholder:text-text-muted/60 focus:outline-none focus:ring-1 focus:ring-bg-active/60"
    />
  );
}
function FilterSelect({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-5 rounded bg-bg-secondary/30 px-1 text-[10.5px] text-text-primary focus:outline-none focus:ring-1 focus:ring-bg-active/60"
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}
function FilterCount({ n, total }: { n: number; total: number }) {
  return (
    <span className="ml-auto text-[10px] text-text-muted">
      {n === total ? `${n}` : `${n} of ${total}`}
    </span>
  );
}

// ─── Trades tab ────────────────────────────────────────────────────────────

function TradesTab({ trades, onSelect }: { trades: TradeRow[]; onSelect: (sym: string) => void }) {
  const [symFilter, setSymFilter] = useState("");
  const [sideFilter, setSideFilter] = useState<"all" | "BUY" | "SELL">("all");
  const [outcomeFilter, setOutcomeFilter] = useState<"all" | "wins" | "losses">("all");
  const sorted = [...trades].sort(
    (a, b) => new Date(b.exit_time || "").getTime() - new Date(a.exit_time || "").getTime(),
  );
  const filtered = sorted.filter((t) => {
    if (symFilter && !String(t.symbol || "").toLowerCase().includes(symFilter.toLowerCase())) return false;
    if (sideFilter !== "all" && t.action !== sideFilter) return false;
    if (outcomeFilter === "wins" && Number(t.pnl ?? 0) <= 0) return false;
    if (outcomeFilter === "losses" && Number(t.pnl ?? 0) >= 0) return false;
    return true;
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterText placeholder="symbol contains…" value={symFilter} onChange={setSymFilter} />
        <FilterSelect value={sideFilter} onChange={(v) => setSideFilter(v as "all" | "BUY" | "SELL")} options={["all", "BUY", "SELL"]} />
        <FilterSelect value={outcomeFilter} onChange={(v) => setOutcomeFilter(v as "all" | "wins" | "losses")} options={["all", "wins", "losses"]} />
        <FilterCount n={filtered.length} total={trades.length} />
      </FilterBar>
      {filtered.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11px] text-text-muted">
          {trades.length === 0 ? "No closed trades today. (Full history → Reports)" : "No trades match the filter."}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
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
              {filtered.slice(0, 100).map((t, idx) => (
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
      )}
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
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "roll" | "expired">("all");
  if (rows.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-text-muted">
        No instruments yet.
      </div>
    );
  }
  const filtered = rows.filter((row) => {
    if (statusFilter === "all") return true;
    const dte = daysTo(parseMCXExpiry(row.symbol));
    if (statusFilter === "expired") return dte !== null && dte < 0;
    if (statusFilter === "roll") return dte !== null && dte >= 0 && dte <= 10;
    if (statusFilter === "active") return dte !== null && dte > 10;
    return true;
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterSelect
          value={statusFilter}
          onChange={(v) => setStatusFilter(v as "all" | "active" | "roll" | "expired")}
          options={["all", "active", "roll", "expired"]}
        />
        <FilterCount n={filtered.length} total={rows.length} />
      </FilterBar>
      <div className="flex-1 overflow-y-auto">
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
            {filtered.map((row, idx) => {
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
                <tr
                  key={String(row.symbol || row.underlying)}
                  className={`border-t border-bg-secondary/15 ${idx % 2 ? "bg-bg-secondary/[0.06]" : ""}`}
                >
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

  // Equity / Day P&L are already in the page header — Stats deliberately
  // surfaces the *aggregate* performance numbers that the header has no room
  // for (win rate, profit factor, drawdown, distribution).
  return (
    <div className="overflow-y-auto" style={{ maxHeight: 160 }}>
      <div className="grid grid-cols-12 gap-2 text-[11px]">
        <StatTile k="Trades" v={String(trades.length)} cols={2} />
        <StatTile k="Win rate" v={`${(winRate * 100).toFixed(0)}%`} cols={2} />
        <StatTile
          k="Profit factor"
          v={Number.isFinite(profitFactor) ? profitFactor.toFixed(2) : "∞"}
          cols={2}
        />
        <StatTile k="Max drawdown" v={`${(maxDD * 100).toFixed(1)}%`} cols={2} tone={maxDD > 0.1 ? "text-amber-300" : ""} />
        <StatTile k="W / L" v={`${wins.length} / ${losses.length}`} cols={2} />
        <StatTile k="Open" v={String(positions.length)} cols={2} />
        <StatTile k="Avg win" v={formatINR(avgWin)} cols={3} tone="text-emerald-300" />
        <StatTile k="Avg loss" v={formatINR(-avgLoss)} cols={3} tone="text-rose-300" />
        <StatTile k="Gross win" v={formatINR(grossProfit)} cols={3} tone="text-emerald-300" />
        <StatTile k="Gross loss" v={formatINR(-grossLoss)} cols={3} tone="text-rose-300" />
        <StatTile k="Realized" v={formatINR(realized)} cols={6} tone={realized >= 0 ? "text-emerald-300" : "text-rose-300"} />
        <StatTile k="Unrealized" v={formatINR(openPnl)} cols={6} tone={openPnl >= 0 ? "text-emerald-300" : "text-rose-300"} />
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
  const [textFilter, setTextFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | "fired" | "skipped" | "entry" | "exit">(
    "all",
  );
  const filtered = events.filter((e) => {
    const t = String(e.event_type || "");
    const m = `${e.symbol || e.underlying || ""} ${e.message || ""}`.toLowerCase();
    if (textFilter && !m.includes(textFilter.toLowerCase())) return false;
    if (typeFilter === "fired" && !t.includes("fired")) return false;
    if (typeFilter === "skipped" && !t.includes("skipped")) return false;
    if (typeFilter === "entry" && t !== "position_entry") return false;
    if (typeFilter === "exit" && t !== "position_exit") return false;
    return true;
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <FilterBar>
        <FilterText placeholder="symbol or message…" value={textFilter} onChange={setTextFilter} />
        <FilterSelect
          value={typeFilter}
          onChange={(v) => setTypeFilter(v as "all" | "fired" | "skipped" | "entry" | "exit")}
          options={["all", "fired", "skipped", "entry", "exit"]}
        />
        <FilterCount n={filtered.length} total={events.length} />
      </FilterBar>
      {filtered.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-[11px] text-text-muted">
          {events.length === 0 ? "No audit events yet." : "No events match the filter."}
        </div>
      ) : (
        <ul className="flex-1 overflow-y-auto space-y-0.5 text-[11px]">
          {filtered.slice(0, 60).map((event, idx) => (
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
                <span className="text-text-muted">{(event.message || "").slice(0, 100)}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ─── Instrument detail modal ──────────────────────────────────────────────

// ─── TPO Chart ──────────────────────────────────────────────────────────────
//
// Classic Steidlmayer vertical Market Profile: every 30-min period in the
// session is a letter (A, B, C, …), and each letter is drawn at every price
// it traded at. Reading the histogram horizontally tells you "where price
// spent the most time" (POC = the longest row). Vertically, gaps in the
// letters between two prices are *single prints* — the auction moved
// through them quickly.
//
// We also overlay POC / VAH / VAL guide lines, IB band as a faint amber
// background column on the left, and dotted reference lines at the POC
// from prior periods (Y / W / M) so the trader sees in one glance whether
// today's auction overlaps history.

type ReferenceLine = {
  label: string; // "Y" | "W" | "M"
  price: number;
  color: string;
};

function TPOChart({
  letters,
  poc,
  vah,
  val,
  ibh,
  ibl,
  high,
  low,
  tickSize,
  price,
  references = [],
  pocBaseColor = "rgba(252, 211, 77, 0.18)",
  height = 420,
}: {
  letters: Record<string, string>;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  ibh?: number | null;
  ibl?: number | null;
  high?: number | null;
  low?: number | null;
  tickSize?: number | null;
  price?: number | null;
  references?: ReferenceLine[];
  pocBaseColor?: string;
  height?: number;
}) {
  const entries = useMemo(() => {
    const out: { price: number; letters: string }[] = [];
    for (const [k, v] of Object.entries(letters || {})) {
      const p = Number(k);
      if (!Number.isFinite(p)) continue;
      out.push({ price: p, letters: String(v || "") });
    }
    out.sort((a, b) => b.price - a.price); // high → low (top → bottom)
    return out;
  }, [letters]);

  if (entries.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-md bg-bg-secondary/25 text-[11px] uppercase tracking-wide text-text-muted ring-1 ring-bg-secondary/40"
        style={{ height }}
      >
        TPO data not yet available — waiting for first IB
      </div>
    );
  }

  // ── Layout: fit the entire profile to the available box ───────────────
  //
  // The profile is always rendered to its full vertical extent — no scroll.
  // Two consequences make this match the way Sierra Chart / IRT auto-scale:
  //
  //   • A long-range session (e.g. NICKEL on a directional day) produces
  //     many tick rows → rows compress to short bars instead of clipping.
  //   • A tight, balanced session produces few rows → the same chart box
  //     gives each level a tall, readable row.
  //
  // The per-row pixel height is `chartHeight / numRows`, so the box height
  // itself scales the boxes by the instrument's actual move — which is the
  // ATR-driven sizing the desk asked for.

  const PRICE_AXIS_WIDTH = 50;
  const RIGHT_PAD = 26; // room for reference badges (Y / W / M)
  const chartHeight = height;
  const rowHeight = chartHeight / entries.length;
  const tick =
    Number(tickSize ?? 0) ||
    (entries.length > 1 ? entries[0].price - entries[1].price : 0.5);

  // Cell width sized to the densest row so every letter fits on one line;
  // bounded so a very thin row doesn't shrink letters below readability.
  const maxLetters = entries.reduce((m, e) => Math.max(m, e.letters.length), 0);
  const fontPx = Math.max(5, Math.min(11, rowHeight - 1));
  const cellWidth = Math.max(4, Math.min(11, fontPx + 1));
  const lettersFit = rowHeight >= 7 && fontPx >= 6;

  const valNum = Number(val ?? 0);
  const vahNum = Number(vah ?? 0);
  const pocNum = Number(poc ?? 0);
  const priceNum = Number(price ?? 0);
  const ibhNum = ibh != null ? Number(ibh) : null;
  const iblNum = ibl != null ? Number(ibl) : null;

  // Sparse price labels — every Nth row so labels stay legible regardless
  // of how thin individual rows have become.
  const targetLabelGap = 14;
  const labelEvery = Math.max(1, Math.round(targetLabelGap / Math.max(rowHeight, 1)));

  return (
    <div
      className="relative overflow-hidden rounded-md bg-bg-primary ring-1 ring-bg-secondary/40"
      style={{ height: chartHeight }}
    >
      {/* Axis separator */}
      <div
        className="absolute top-0 bottom-0 bg-bg-secondary/20"
        style={{ left: PRICE_AXIS_WIDTH, width: 1 }}
      />
      {entries.map((row, i) => {
        const top = i * rowHeight;
        const inVA =
          valNum && vahNum && row.price >= valNum && row.price <= vahNum;
        const inIB =
          iblNum != null &&
          ibhNum != null &&
          row.price >= iblNum &&
          row.price <= ibhNum;
        const isPOC = pocNum && Math.abs(row.price - pocNum) < tick * 0.5;
        const atVAH = vahNum && Math.abs(row.price - vahNum) < tick * 0.5;
        const atVAL = valNum && Math.abs(row.price - valNum) < tick * 0.5;
        const atPrice =
          priceNum && Math.abs(row.price - priceNum) < tick * 0.5;
        const ref = references.find(
          (r) => Math.abs(r.price - row.price) < tick * 0.5,
        );
        const bandFill = isPOC
          ? pocBaseColor
          : inIB
            ? "rgba(252, 211, 77, 0.09)"
            : inVA
              ? "rgba(148, 163, 184, 0.07)"
              : "transparent";
        const letterColor = isPOC
          ? "#fcd34d"
          : inIB
            ? "#fde68a"
            : inVA
              ? "#cbd5e1"
              : "#94a3b8";
        const showLabel =
          i === 0 || i === entries.length - 1 || i % labelEvery === 0;
        const labelFontPx = Math.max(7, Math.min(9, rowHeight - 1));

        return (
          <div
            key={row.price}
            className="absolute left-0 right-0 flex items-center"
            style={{
              top,
              height: rowHeight,
              background: bandFill,
              borderTop: atVAH ? "1px dashed rgba(148,163,184,0.45)" : undefined,
              borderBottom: atVAL ? "1px dashed rgba(148,163,184,0.45)" : undefined,
            }}
            title={`${formatNumber(row.price, 2)} · ${row.letters.length} TPO (${row.letters.split("").join(" ")})`}
          >
            {/* Price axis label, drawn sparsely */}
            {showLabel ? (
              <div
                className="absolute font-mono whitespace-nowrap text-right"
                style={{
                  right: `calc(100% - ${PRICE_AXIS_WIDTH - 4}px)`,
                  fontSize: labelFontPx,
                  color: isPOC ? "#fcd34d" : "#64748b",
                  lineHeight: 1,
                }}
              >
                {atPrice ? "▶ " : ""}
                {formatNumber(row.price, 2)}
              </div>
            ) : null}
            {/* Letters (when row is tall enough) OR proportional bar */}
            <div
              className="absolute flex items-center"
              style={{
                left: PRICE_AXIS_WIDTH + 4,
                right: RIGHT_PAD,
                top: 0,
                bottom: 0,
              }}
            >
              {lettersFit ? (
                <div className="flex font-mono leading-none">
                  {row.letters.split("").map((ch, j) => (
                    <span
                      key={j}
                      style={{
                        width: cellWidth,
                        fontSize: fontPx,
                        color: letterColor,
                        fontWeight: isPOC ? 600 : 400,
                      }}
                    >
                      {ch}
                    </span>
                  ))}
                </div>
              ) : (
                // Too thin to render legible letters → tinted proportional
                // bar. Width = TPO count / max count, so the auction shape
                // is still legible at a glance.
                <div
                  style={{
                    width: `${(row.letters.length / Math.max(maxLetters, 1)) * 100}%`,
                    height: Math.max(1.5, rowHeight * 0.7),
                    background: letterColor,
                    opacity: isPOC ? 0.95 : inIB ? 0.7 : inVA ? 0.55 : 0.4,
                    borderRadius: 1,
                  }}
                />
              )}
            </div>
            {/* Reference badge */}
            {ref ? (
              <div
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-1 text-[9px] font-semibold font-mono"
                style={{
                  background: ref.color + "33",
                  color: ref.color,
                  lineHeight: 1.2,
                }}
              >
                {ref.label}
              </div>
            ) : null}
          </div>
        );
      })}
      {/* Corner axis legend */}
      <div className="pointer-events-none absolute right-1 top-1 flex flex-col items-end gap-0.5 text-[9px] font-mono">
        {high ? <span className="text-text-muted">H {formatNumber(Number(high), 2)}</span> : null}
        {vah ? <span className="text-text-secondary">VAH {formatNumber(Number(vah), 2)}</span> : null}
        {poc ? <span className="font-semibold text-amber-300">POC {formatNumber(Number(poc), 2)}</span> : null}
        {val ? <span className="text-text-secondary">VAL {formatNumber(Number(val), 2)}</span> : null}
        {low ? <span className="text-text-muted">L {formatNumber(Number(low), 2)}</span> : null}
      </div>
    </div>
  );
}

/** Per-period profile data shape — POC / VAH / VAL plus optional IB band.
 *  Used for the timeline rows below the headline 'Today' visual. */
type PeriodProfile = {
  label: string;        // "Yesterday", "This week", etc.
  status: "live" | "available" | "pending";
  date?: string;
  poc?: number;
  vah?: number;
  val?: number;
  ibh?: number;
  ibl?: number;
};

/**
 * Big visual profile for the detail modal — same grammar as the inline row
 * MPProfileBar but with:
 *   • prior-period reference markers (POC ticks for Y / W / M),
 *   • OF chips on the side (VWAP, CVD, divergence, IB extension),
 *   • a wider domain so labels never clip.
 */
function TodayProfileHero({
  row,
  references,
}: {
  row: WatchRow;
  references: PeriodProfile[];
}) {
  const price = Number(row.price ?? 0);
  const poc = Number(row.mp_poc ?? 0);
  const vah = Number(row.mp_vah ?? 0);
  const val = Number(row.mp_val ?? 0);
  const ibh = Number(row.mp_ib_high ?? 0);
  const ibl = Number(row.mp_ib_low ?? 0);
  const vwap = Number(row.vwap ?? 0);
  const vwapU = Number(row.vwap_upper ?? 0);
  const vwapL = Number(row.vwap_lower ?? 0);
  if (!poc || !vah || !val || vah <= val) {
    return (
      <div className="flex h-[120px] items-center justify-center rounded-md bg-bg-secondary/25 text-[11px] uppercase tracking-wide text-text-muted ring-1 ring-bg-secondary/40">
        market profile warming up
      </div>
    );
  }
  // Domain includes all reference levels so they remain visible.
  const refValues = references.flatMap((r) => [r.poc, r.vah, r.val, r.ibh, r.ibl]).filter(
    (n): n is number => typeof n === "number" && n > 0,
  );
  const all = [val, vah, ibl, ibh, price, vwap, vwapU, vwapL, ...refValues].filter((n) => n > 0);
  const minDomain = Math.min(...all) - (Math.max(...all) - Math.min(...all)) * 0.04;
  const maxDomain = Math.max(...all) + (Math.max(...all) - Math.min(...all)) * 0.04;
  const span = maxDomain - minDomain || 1;
  const x = (v: number) => ((v - minDomain) / span) * 100;
  const H = 120;
  const direction = String(row.mp_direction || "").toLowerCase();
  const markerColor =
    direction === "buy" ? "fill-emerald-400" : direction === "sell" ? "fill-rose-400" : "fill-sky-300";
  const REF_COLORS: Record<string, string> = {
    Y: "rgb(244 114 182 / 0.85)", // yesterday — pink
    W: "rgb(125 211 252 / 0.85)", // week     — light sky
    M: "rgb(192 132 252 / 0.85)", // month    — violet
  };

  return (
    <div
      className={`relative overflow-hidden rounded-md bg-gradient-to-b from-bg-secondary/30 to-bg-secondary/50 ring-1 ${regimeBorderClass(row.mp_day_type)}`}
      style={{ height: H }}
    >
      <svg
        className="absolute inset-0 h-full w-full"
        preserveAspectRatio="none"
        viewBox={`0 0 100 ${H}`}
        role="img"
        aria-label="today market profile"
      >
        <defs>
          <linearGradient id="heroVa" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(148 163 184 / 0.45)" />
            <stop offset="100%" stopColor="rgb(148 163 184 / 0.18)" />
          </linearGradient>
        </defs>
        {/* Value area */}
        <rect x={x(val)} y={H * 0.18} width={Math.max(x(vah) - x(val), 0.5)} height={H * 0.64} fill="url(#heroVa)" rx={2} />
        {/* VAH/VAL guides */}
        <line x1={x(val)} x2={x(val)} y1={H * 0.1} y2={H * 0.9} className="stroke-text-muted/60" strokeWidth={0.4} strokeDasharray="1 1" />
        <line x1={x(vah)} x2={x(vah)} y1={H * 0.1} y2={H * 0.9} className="stroke-text-muted/60" strokeWidth={0.4} strokeDasharray="1 1" />
        {/* IB pill */}
        {ibl && ibh ? (
          <rect x={x(ibl)} y={H * 0.4} width={Math.max(x(ibh) - x(ibl), 0.4)} height={H * 0.2} className="fill-amber-300/22" rx={2} />
        ) : null}
        {/* VWAP band */}
        {vwapL && vwapU ? (
          <rect x={Math.min(x(vwapL), x(vwapU))} y={0} width={Math.abs(x(vwapU) - x(vwapL))} height={H} className="fill-sky-400/8" />
        ) : null}
        {/* VWAP line */}
        {vwap ? (
          <line x1={x(vwap)} x2={x(vwap)} y1={H * 0.08} y2={H * 0.92} className="stroke-sky-400" strokeWidth={1} strokeDasharray="3 2" />
        ) : null}
        {/* POC */}
        <rect x={x(poc) - 0.8} y={H * 0.05} width={1.6} height={H * 0.9} className="fill-amber-300/95" rx={0.7} />
        {/* Reference markers from prior periods */}
        {references.map((ref) => {
          if (!ref.poc) return null;
          const key = ref.label[0];
          const color = REF_COLORS[key] || "rgb(148 163 184 / 0.9)";
          return (
            <g key={ref.label}>
              {/* POC dotted line */}
              <line
                x1={x(ref.poc)}
                x2={x(ref.poc)}
                y1={H * 0.1}
                y2={H * 0.9}
                stroke={color}
                strokeWidth={0.7}
                strokeDasharray="0.5 1.5"
              />
              {/* Marker label */}
              <text
                x={x(ref.poc)}
                y={H * 0.07}
                fontSize="3.5"
                textAnchor="middle"
                fill={color}
                style={{ fontFamily: "ui-monospace,monospace" }}
              >
                {key}
              </text>
              {/* VAH/VAL ticks (thinner) */}
              {ref.vah ? (
                <line x1={x(ref.vah)} x2={x(ref.vah)} y1={H * 0.4} y2={H * 0.6} stroke={color} strokeWidth={0.4} strokeDasharray="0.4 0.8" />
              ) : null}
              {ref.val ? (
                <line x1={x(ref.val)} x2={x(ref.val)} y1={H * 0.4} y2={H * 0.6} stroke={color} strokeWidth={0.4} strokeDasharray="0.4 0.8" />
              ) : null}
            </g>
          );
        })}
        {/* Live price marker */}
        {price > 0 ? (
          <g>
            <line x1={x(price)} x2={x(price)} y1={0} y2={H} className="stroke-text-primary/85" strokeWidth={0.7} />
            <polygon points={`${x(price) - 1.8},0 ${x(price) + 1.8},0 ${x(price)},${H * 0.16}`} className={markerColor} />
            <polygon points={`${x(price) - 1.8},${H} ${x(price) + 1.8},${H} ${x(price)},${H - H * 0.16}`} className={markerColor} />
          </g>
        ) : null}
      </svg>

      {/* In-bar level labels */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between px-2 pb-1 text-[10px] font-mono">
        <span className="text-text-muted">VAL {formatNumber(val, 2)}</span>
        <span className="text-amber-300">POC {formatNumber(poc, 2)}</span>
        <span className="text-text-muted">VAH {formatNumber(vah, 2)}</span>
      </div>

      {/* Order-flow chip strip — overlays today's profile so the live OF
          context (VWAP, CVD, divergence, IB extension) sits next to the auction. */}
      <div className="absolute right-2 top-2 flex flex-wrap items-center justify-end gap-1 text-[9.5px] font-mono">
        {vwap ? (
          <span className="rounded bg-sky-500/20 px-1.5 py-[1px] text-sky-200">VWAP {formatNumber(vwap, 2)}</span>
        ) : null}
        <span
          className={`rounded px-1.5 py-[1px] ${
            Number(row.cvd_session ?? 0) > 0
              ? "bg-emerald-500/20 text-emerald-200"
              : Number(row.cvd_session ?? 0) < 0
                ? "bg-rose-500/20 text-rose-200"
                : "bg-bg-secondary/40 text-text-muted"
          }`}
        >
          CVD {Number(row.cvd_session ?? 0) > 0 ? "↑" : Number(row.cvd_session ?? 0) < 0 ? "↓" : "·"}{" "}
          {compactNumber(Number(row.cvd_session ?? row.cvd_latest))}
        </span>
        {row.cvd_divergence?.kind ? (
          <span className="rounded bg-fuchsia-500/20 px-1.5 py-[1px] text-fuchsia-200">
            DIV {row.cvd_divergence.kind}
          </span>
        ) : null}
        {row.ib_extended_above || row.ib_extended_below ? (
          <span className="rounded bg-amber-500/20 px-1.5 py-[1px] text-amber-200">
            IB {row.ib_extended_above ? "↑" : "↓"} {row.ib_extension_pct != null ? `${(Number(row.ib_extension_pct) * 100).toFixed(0)}%` : ""}
          </span>
        ) : null}
      </div>

      {/* Reference legend (key for the dotted markers) */}
      {references.some((r) => r.poc) ? (
        <div className="absolute left-2 top-2 flex items-center gap-2 text-[9.5px] font-mono">
          {references.map((r) =>
            r.poc ? (
              <span key={r.label} style={{ color: REF_COLORS[r.label[0]] || "rgb(148 163 184)" }}>
                {r.label[0]}={formatNumber(r.poc, 2)}
              </span>
            ) : null,
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Compact horizontal profile row for a prior period (Y / W / M). Either
 * shows a small visual when the data is available, or a labelled
 * 'pending' placeholder so the timeline structure is visible.
 */
function PeriodProfileRow({ profile }: { profile: PeriodProfile }) {
  if (profile.status === "pending") {
    return (
      <div className="flex h-[36px] items-center justify-between rounded bg-bg-secondary/15 px-3 ring-1 ring-bg-secondary/30">
        <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">
          {profile.label}
        </span>
        <span className="text-[10px] text-text-muted">
          Backend not yet wired — coming soon
        </span>
      </div>
    );
  }
  const poc = Number(profile.poc ?? 0);
  const vah = Number(profile.vah ?? 0);
  const val = Number(profile.val ?? 0);
  if (!poc || !vah || !val || vah <= val) {
    return (
      <div className="flex h-[36px] items-center justify-between rounded bg-bg-secondary/15 px-3 ring-1 ring-bg-secondary/30">
        <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">
          {profile.label}
        </span>
        <span className="text-[10px] text-text-muted">data unavailable</span>
      </div>
    );
  }
  const lowEdge = Math.min(val, profile.ibl || val);
  const highEdge = Math.max(vah, profile.ibh || vah);
  const pad = (highEdge - lowEdge) * 0.06;
  const min = lowEdge - pad;
  const max = highEdge + pad;
  const span = max - min || 1;
  const x = (v: number) => ((v - min) / span) * 100;
  return (
    <div className="flex items-center gap-3">
      <div className="w-[78px] shrink-0 text-[10.5px] uppercase tracking-[0.14em] text-text-secondary">
        {profile.label}
        {profile.date ? <div className="text-[9.5px] text-text-muted normal-case">{profile.date}</div> : null}
      </div>
      <div className="relative h-[36px] flex-1 overflow-hidden rounded bg-bg-secondary/25 ring-1 ring-bg-secondary/30">
        <svg viewBox="0 0 100 36" preserveAspectRatio="none" className="absolute inset-0 h-full w-full">
          <rect x={x(val)} y={6} width={Math.max(x(vah) - x(val), 0.5)} height={24} className="fill-slate-500/30" rx={1.5} />
          {profile.ibl && profile.ibh ? (
            <rect x={x(profile.ibl)} y={12} width={Math.max(x(profile.ibh) - x(profile.ibl), 0.4)} height={12} className="fill-amber-300/20" rx={1} />
          ) : null}
          <rect x={x(poc) - 0.6} y={3} width={1.2} height={30} className="fill-amber-300/85" rx={0.4} />
        </svg>
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-between px-2 pb-[1px] text-[9px] font-mono text-text-muted/85">
          <span>{formatNumber(val, 2)}</span>
          <span className="text-amber-300">{formatNumber(poc, 2)}</span>
          <span>{formatNumber(vah, 2)}</span>
        </div>
      </div>
    </div>
  );
}

function InstrumentDetailModal({
  row,
  position,
  onClose,
}: {
  row: WatchRow;
  position?: CommodityPosition;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Pull yesterday's profile out of trigger_evidence when the engine
  // surfaced it (open_drive / va_migration emit prior_pvah / prior_pval).
  const evidence = (row.trigger_evidence || {}) as Record<string, unknown>;
  const evPoc = typeof evidence.today_poc === "number" ? evidence.today_poc : null;
  const yPoc = typeof evidence.prior_poc === "number" ? evidence.prior_poc : null;
  const yVah = typeof evidence.prior_pvah === "number" ? evidence.prior_pvah : null;
  const yVal = typeof evidence.prior_pval === "number" ? evidence.prior_pval : null;
  const yesterdayProfile: PeriodProfile = {
    label: "Yesterday",
    date: row.prior_session_date || undefined,
    status: yPoc || yVah || yVal ? "available" : "pending",
    poc: yPoc ?? undefined,
    vah: yVah ?? undefined,
    val: yVal ?? undefined,
  };
  // Week / month profiles are not yet wired in the backend — show placeholders
  // so the timeline structure is in place. Will populate when the agent
  // exposes them.
  // Fetch persisted prior profiles (yesterday + week + month aggregates) the
  // moment the modal opens. The endpoint serves daily snapshots saved by the
  // agent under backend/runtime/commodity_profiles, so the timeline grows
  // organically every session.
  const histRoot = String(row.underlying || row.symbol || "");
  const historyQuery = useQuery({
    queryKey: ["commodity", "profile-history", histRoot],
    queryFn: () => getCommodityProfileHistory(histRoot).then((r) => r.data),
    enabled: !!histRoot,
    staleTime: 60_000,
  });
  const history = (historyQuery.data || {}) as Record<string, unknown>;

  type HistPeriod = {
    label: string;
    date?: string;
    poc?: number | null;
    vah?: number | null;
    val?: number | null;
    high?: number | null;
    low?: number | null;
    tpo_letters?: Record<string, string> | null;
    tick_size?: number | null;
  };
  const histPeriod = (key: string, label: string): HistPeriod | null => {
    const raw = history[key];
    if (!raw || typeof raw !== "object") return null;
    const obj = raw as Record<string, unknown>;
    return {
      label,
      date: typeof obj.session_date === "string" ? obj.session_date : undefined,
      poc: typeof obj.poc === "number" ? obj.poc : null,
      vah: typeof obj.vah === "number" ? obj.vah : null,
      val: typeof obj.val === "number" ? obj.val : null,
      high: typeof obj.high === "number" ? obj.high : null,
      low: typeof obj.low === "number" ? obj.low : null,
      tpo_letters: (obj.tpo_letters && typeof obj.tpo_letters === "object")
        ? (obj.tpo_letters as Record<string, string>)
        : null,
      tick_size: typeof obj.tick_size === "number" ? obj.tick_size : null,
    };
  };

  // Last-day TPO. Source priority:
  //   1. row.prior_session_profile — the live agent payload; carries full
  //      letters and is always fresh, so we prefer it when present.
  //   2. persisted history endpoint (`previous_day` aggregate).
  //   3. trigger_evidence (prior_poc / prior_pvah / prior_pval) as a thin
  //      fallback when the agent only surfaced reference levels.
  const yesterdayLive = row.prior_session_profile;
  const yesterday: HistPeriod | null = yesterdayLive
    ? {
        label: "Yesterday",
        date: yesterdayLive.session_date || undefined,
        poc: yesterdayLive.poc ?? null,
        vah: yesterdayLive.vah ?? null,
        val: yesterdayLive.val ?? null,
        high: yesterdayLive.high ?? null,
        low: yesterdayLive.low ?? null,
        tpo_letters: yesterdayLive.tpo_letters ?? null,
        tick_size: yesterdayLive.tick_size ?? null,
      }
    : histPeriod("previous_day", "Yesterday")
      || (yPoc || yVah || yVal
        ? {
            label: "Yesterday",
            date: row.prior_session_date || undefined,
            poc: yPoc,
            vah: yVah,
            val: yVal,
            high: null,
            low: null,
            tpo_letters: null,
            tick_size: null,
          }
        : null);
  const thisWeek = histPeriod("this_week", "This week");
  const lastWeek = histPeriod("last_week", "Last week");
  const thisMonth = histPeriod("this_month", "This month");
  const lastMonth = histPeriod("last_month", "Last month");
  const historicalPeriods: HistPeriod[] = [yesterday, thisWeek, lastWeek, thisMonth, lastMonth].filter(
    (p): p is HistPeriod => p !== null && (p.poc != null || p.vah != null || p.val != null),
  );

  // Reference lines drawn on today's chart — colour-coded by period.
  const refColors: Record<string, string> = {
    Yesterday: "#fbbf24",
    "This week": "#a78bfa",
    "Last week": "#818cf8",
    "This month": "#34d399",
    "Last month": "#10b981",
  };
  const todayReferences: ReferenceLine[] = historicalPeriods
    .filter((p) => p.poc != null)
    .map((p) => ({
      label: p.label === "Yesterday" ? "Y" : p.label.startsWith("This week") ? "W" : p.label.startsWith("Last week") ? "lW" : p.label.startsWith("This month") ? "M" : "lM",
      price: Number(p.poc),
      color: refColors[p.label] || "#94a3b8",
    }));

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative max-h-[94vh] w-full max-w-[1500px] rounded-lg bg-bg-primary ring-1 ring-bg-secondary/40 flex flex-col"
      >
        {/* Close button lives OUTSIDE the scroll container so it stays
            visible even after scrolling the long detailed-chart body. */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close detailed chart"
          className="absolute right-3 top-3 z-10 rounded p-1 text-text-muted bg-bg-primary/80 backdrop-blur-sm hover:bg-bg-secondary/60 hover:text-text-primary"
        >
          <X className="h-4 w-4" />
        </button>

        <div className="overflow-y-auto p-5">

        {/* Title */}
        <div className="mb-4 flex items-baseline justify-between">
          <div>
            <div className="text-base font-semibold text-text-primary">
              {row.display_name}{" "}
              <span className="font-mono text-[12px] text-text-muted">{row.symbol}</span>
            </div>
            <div className="text-[11px] text-text-muted">
              {row.contract_unit_label} · {row.quote_unit_label} · bar {formatIST(row.bar_time)}
            </div>
          </div>
          <div className="text-right">
            <div
              className={`font-mono text-2xl font-semibold ${colorForDelta(Number(row.change ?? 0))}`}
            >
              {formatNumber(Number(row.price ?? 0), 2)}
            </div>
            <div className={`font-mono text-[11px] ${colorForDelta(Number(row.change ?? 0))}`}>
              {formatSigned(Number(row.change ?? 0), 2)} ({formatPct(Number(row.change_pct ?? 0), 2)})
            </div>
          </div>
        </div>

        {/* ── Hero charts: Yesterday LEFT of Today, full width ──────────── */}
        {/*
         * Classic split-profile layout (Sierra Chart / IRT / Bookmap):
         * the completed prior session sits on the LEFT, today's developing
         * profile on the RIGHT. The eye reads left-to-right in time and the
         * chart area gets the full modal width — every other panel (refs,
         * OF, validation, week/month) drops to the bottom strip.
         *
         * Width split is 40 / 60: yesterday's full session vs today's
         * partial session, biased toward today since it's the active read.
         */}
        <div className="mb-4 grid grid-cols-12 gap-3">
          {/* Yesterday (completed prior session) */}
          <div className="col-span-5">
            <div className="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-[0.14em] text-text-muted">
              <span className="flex items-center gap-1.5">
                <span style={{ color: refColors.Yesterday }}>Yesterday</span>
                <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-semibold tracking-wider text-emerald-300">
                  FINAL
                </span>
              </span>
              <span>
                {yesterday?.date ? <span className="font-mono">{yesterday.date}</span> : "—"}
                {yesterday?.tick_size
                  ? ` · tick ${formatNumber(Number(yesterday.tick_size), 2)}`
                  : ""}
              </span>
            </div>
            {yesterday && yesterday.tpo_letters && Object.keys(yesterday.tpo_letters).length > 0 ? (
              <TPOChart
                letters={yesterday.tpo_letters}
                poc={yesterday.poc}
                vah={yesterday.vah}
                val={yesterday.val}
                high={yesterday.high}
                low={yesterday.low}
                tickSize={yesterday.tick_size}
                pocBaseColor={refColors.Yesterday + "33"}
                height={520}
              />
            ) : (
              <div
                className="flex flex-col items-center justify-center gap-2 rounded-md bg-bg-secondary/25 text-[11px] uppercase tracking-wide text-text-muted ring-1 ring-bg-secondary/40"
                style={{ height: 520 }}
              >
                <span>Last-day TPO not yet on file</span>
                {yesterday && (yesterday.poc != null || yesterday.vah != null || yesterday.val != null) ? (
                  <div className="space-y-1 font-mono text-[10px] normal-case tracking-normal">
                    <div className="flex gap-3">
                      <span className="text-text-muted">VAH</span>
                      <span>{yesterday.vah != null ? formatNumber(Number(yesterday.vah), 2) : "—"}</span>
                    </div>
                    <div className="flex gap-3">
                      <span className="text-text-muted">POC</span>
                      <span className="text-amber-300">{yesterday.poc != null ? formatNumber(Number(yesterday.poc), 2) : "—"}</span>
                    </div>
                    <div className="flex gap-3">
                      <span className="text-text-muted">VAL</span>
                      <span>{yesterday.val != null ? formatNumber(Number(yesterday.val), 2) : "—"}</span>
                    </div>
                  </div>
                ) : (
                  <span className="normal-case tracking-normal">Builds on first session that closes after deploy.</span>
                )}
              </div>
            )}
          </div>

          {/* Today (developing) */}
          <div className="col-span-7">
            <div className="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-[0.14em] text-text-muted">
              <span className="flex items-center gap-1.5">
                <span>Today · {row.mp_day_type || "—"}</span>
                {(() => {
                  const periods = Number(row.mp_periods ?? 0);
                  const isDeveloping = periods < 13; // ~6.5h MCX session at 30m
                  if (!isDeveloping) {
                    return (
                      <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-semibold tracking-wider text-emerald-300">
                        FINAL
                      </span>
                    );
                  }
                  const ibComplete = periods >= 2;
                  return (
                    <span
                      className={`rounded px-1.5 py-0.5 text-[9px] font-semibold tracking-wider ${
                        ibComplete
                          ? "bg-amber-500/20 text-amber-300"
                          : "bg-sky-500/20 text-sky-300"
                      }`}
                    >
                      {ibComplete ? "DEVELOPING" : "DEVELOPING IB"}
                    </span>
                  );
                })()}
              </span>
              <span>
                {row.mp_periods ?? "—"} periods · tick {formatNumber(Number(row.mp_tick_size ?? 0), 2)}
              </span>
            </div>
            <TPOChart
              letters={(row.mp_tpo_letters as Record<string, string>) || {}}
              poc={row.mp_poc as number | undefined}
              vah={row.mp_vah as number | undefined}
              val={row.mp_val as number | undefined}
              ibh={row.mp_ib_high as number | undefined}
              ibl={row.mp_ib_low as number | undefined}
              high={row.mp_high as number | undefined}
              low={row.mp_low as number | undefined}
              tickSize={row.mp_tick_size as number | undefined}
              price={Number(row.price ?? 0)}
              references={todayReferences}
              height={520}
            />
          </div>
        </div>

        {/* ── Bottom strip: references + OF + validation ────────────────── */}
        <div className="mb-4 grid grid-cols-12 gap-3">
          <div className="col-span-3 rounded bg-bg-secondary/15 p-2 ring-1 ring-bg-secondary/30">
            <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">
              References on today's chart
            </div>
            {todayReferences.length === 0 ? (
              <div className="text-[11px] text-text-muted">
                Building history — references appear as snapshots persist.
              </div>
            ) : (
              <ul className="space-y-0.5 text-[11px] font-mono">
                {todayReferences.map((r) => (
                  <li key={r.label + r.price} className="flex items-center gap-2">
                    <span
                      className="inline-flex h-4 w-6 items-center justify-center rounded text-[9px] font-semibold"
                      style={{ background: r.color + "33", color: r.color }}
                    >
                      {r.label}
                    </span>
                    <span className="text-text-secondary">POC</span>
                    <span className="ml-auto">{formatNumber(r.price, 2)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="col-span-4 rounded bg-bg-secondary/15 p-2 ring-1 ring-bg-secondary/30">
            <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">
              Order-flow now
            </div>
            <div className="grid grid-cols-3 gap-y-1 text-[11px] font-mono">
              <KV label="VWAP" v={formatNumber(Number(row.vwap ?? 0), 2)} />
              <KV
                label="CVD"
                v={formatSigned(Number(row.cvd_session ?? row.cvd_latest ?? 0))}
                tone={Number(row.cvd_session ?? row.cvd_latest ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}
              />
              <KV label="ATR(1m)" v={formatNumber(Number(row.atr ?? 0), 2)} />
              <KV label="Regime" v={String(row.regime || "—")} />
              <KV label="Confidence" v={`${Math.round(Number(row.confidence ?? 0) * 100)}%`} />
              <KV label="Trigger" v={triggerLabel(row.entry_style)} />
            </div>
          </div>
          <div className="col-span-5 rounded bg-bg-secondary/15 p-2 ring-1 ring-bg-secondary/30">
            <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">
              Trigger evidence
            </div>
            <div className="text-[11px] text-text-secondary">
              {row.signal_validation_detail || (
                <span className="text-text-muted">No active trigger this bar.</span>
              )}
            </div>
          </div>
        </div>

        {/* ── Week / month profiles (only on demand, below the hero) ────── */}
        {historicalPeriods.filter((p) => p.label !== "Yesterday").length > 0 ? (
          <div className="mb-4">
            <div className="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-[0.14em] text-text-muted">
              <span>Week / month profiles</span>
              <span>
                {historyQuery.isLoading
                  ? "loading…"
                  : `${historicalPeriods.filter((p) => p.label !== "Yesterday").length} on file`}
              </span>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {historicalPeriods
                .filter((p) => p.label !== "Yesterday")
                .map((p) => (
                  <div
                    key={p.label}
                    className="rounded bg-bg-secondary/15 p-1.5 ring-1 ring-bg-secondary/30"
                  >
                    <div className="mb-1 flex items-baseline justify-between text-[9.5px] uppercase tracking-[0.14em] text-text-muted">
                      <span style={{ color: refColors[p.label] }}>{p.label}</span>
                      {p.date ? <span className="font-mono">{p.date}</span> : null}
                    </div>
                    {p.tpo_letters && Object.keys(p.tpo_letters).length > 0 ? (
                      <TPOChart
                        letters={p.tpo_letters}
                        poc={p.poc}
                        vah={p.vah}
                        val={p.val}
                        high={p.high}
                        low={p.low}
                        tickSize={p.tick_size}
                        pocBaseColor={(refColors[p.label] || "#fbbf24") + "33"}
                        height={220}
                      />
                    ) : (
                      <div className="space-y-1 px-1 py-2 text-[10px] font-mono">
                        <div className="flex justify-between">
                          <span className="text-text-muted">VAH</span>
                          <span>{p.vah != null ? formatNumber(Number(p.vah), 2) : "—"}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-text-muted">POC</span>
                          <span className="text-amber-300">{p.poc != null ? formatNumber(Number(p.poc), 2) : "—"}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-text-muted">VAL</span>
                          <span>{p.val != null ? formatNumber(Number(p.val), 2) : "—"}</span>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
            </div>
          </div>
        ) : null}

        {/* Position card (visual gauge, not a table) */}
        {position ? (
          <div className="mb-4 rounded bg-bg-secondary/15 p-3 ring-1 ring-bg-secondary/30">
            <div className="mb-2 flex items-baseline justify-between text-[10.5px] uppercase tracking-[0.14em] text-text-muted">
              <span>Open position · {position.action}</span>
              <span>
                entered {formatIST(position.entered_at)} · {position.lots} {position.lots && Number(position.lots) > 1 ? "lots" : "lot"}
              </span>
            </div>
            <RiskGauge
              side={String(position.action || "")}
              entry={Number(position.entry_price ?? 0)}
              current={Number(position.current_price ?? 0)}
              stop={Number(position.stop_price ?? 0)}
              target={Number(position.target_price ?? 0)}
            />
            <div className="mt-2 grid grid-cols-4 gap-3 text-[11px] font-mono">
              <KV label="Entry" v={formatNumber(Number(position.entry_price ?? 0), 2)} />
              <KV label="Current" v={formatNumber(Number(position.current_price ?? 0), 2)} />
              <KV label="Stop" v={formatNumber(Number(position.stop_price ?? 0), 2)} />
              <KV
                label="Target"
                v={position.target_price ? formatNumber(Number(position.target_price), 2) : "—"}
              />
              <KV
                label="P&L"
                v={formatSigned(Number(position.unrealized_pnl ?? 0))}
                tone={Number(position.unrealized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}
              />
              <KV
                label="Return"
                v={formatPct(Number(position.return_pct ?? 0), 2)}
                tone={Number(position.return_pct ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}
              />
              <KV label="Style" v={triggerLabel(position.entry_style)} />
              <KV label="Regime" v={String(position.regime || "—")} />
            </div>
          </div>
        ) : null}

        </div>{/* /scrollable body */}
      </div>
    </div>
  );
}

/** Tight inline key-value cell used inside the visual cards. */
function KV({ label, v, tone }: { label: string; v: string; tone?: string }) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={`font-mono text-[12px] ${tone || "text-text-primary"}`}>{v}</div>
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
  // Orders + Trades tabs show TODAY only (IST). Full lifetime history lives
  // in the Reports module — same scoping as the NSE desk. Orders carry an
  // IST ISO `time`; trades carry `exit_time`. Prefer the backend's pre-split
  // `today_trades` when present, else filter trade_history by exit date.
  // Undated orders are kept (defensive — never hide a live order).
  const todayStrIST = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const orders = ((status.orders ?? []) as Order[]).filter(
    (o) => !o.time || String(o.time).startsWith(todayStrIST),
  );
  const trades = (
    status.today_trades !== undefined
      ? status.today_trades.filter(isClosedTrade)
      : ((status.trade_history ?? []) as TradeRow[]).filter((t) =>
          isClosedTrade(t) && istDateKey(t.exit_time) === todayStrIST,
        )
  ) as TradeRow[];
  // Stats tab keeps the FULL trade history — win rate / profit factor / W-L /
  // per-underlying are lifetime aggregates and would be meaningless scoped to
  // a single day. Only the Trades + Orders LISTS are today-only.
  const allTrades = (status.trade_history ?? []) as TradeRow[];
  const closedTrades = allTrades.filter(isClosedTrade);
  const closedTodayTrades = closedTrades.filter((t) => istDateKey(t.exit_time) === todayStrIST);

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
  // Expiry selector populates the *next 3 monthly contracts* from today,
  // not just the months we already happen to track. Pattern borrowed from
  // Zerodha Kite / Sensibull where the contract picker shows the immediate
  // tradable months even if the user has nothing in those expiries yet.
  // Each option carries the days-to-expiry so the trader sees at a glance
  // "JUN — 18d" / "JUL — 49d".
  const expiryOptions = useMemo(() => {
    const MONTHS = [
      "JAN",
      "FEB",
      "MAR",
      "APR",
      "MAY",
      "JUN",
      "JUL",
      "AUG",
      "SEP",
      "OCT",
      "NOV",
      "DEC",
    ];
    const now = new Date();
    const opts: { value: string; label: string }[] = [{ value: "active", label: "Active month" }];
    for (let i = 0; i < 3; i++) {
      const m = now.getMonth() + i;
      const year = now.getFullYear() + Math.floor(m / 12);
      const month = m % 12;
      // Approximate expiry: last calendar day of the month (close enough for
      // a DTE display; backend has the precise SEBI calendar when needed).
      const lastDay = new Date(year, month + 1, 0);
      const dte = Math.ceil((lastDay.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));
      const yy = String(year).slice(2);
      const mmm = MONTHS[month];
      const value = `${yy}${mmm}`;
      // Skip if already past expiry (negative DTE) — happens on the very
      // last trading day of a month.
      if (dte < 0) continue;
      const labelDte = dte === 0 ? "today" : `${dte}d`;
      opts.push({ value, label: `${mmm} ${year} · ${labelDte}` });
    }
    // Also include any months from the live universe that aren't already in
    // the next-3 (e.g. when the configured contract is further out).
    const knownMonths = new Set(opts.map((o) => o.value));
    for (const r of rows) {
      const m = String(r.symbol || "")
        .toUpperCase()
        .match(/^MCX:[A-Z0-9]+?(\d{2})([A-Z]{3})FUT$/);
      if (m) {
        const key = `${m[1]}${m[2]}`;
        if (!knownMonths.has(key)) {
          opts.push({ value: key, label: `${m[2]} 20${m[1]} · tracked` });
          knownMonths.add(key);
        }
      }
    }
    return opts;
  }, [rows]);

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
  const [bottomTab, setBottomTab] = useState<BottomTabKey>("positions");
  // Auto-flip to a sensible default only on first load: positions when the
  // desk has exposure, queue when flat. User clicks override and stick.
  const [hasInteractedWithTabs, setHasInteractedWithTabs] = useState(false);
  const [strategyModalOpen, setStrategyModalOpen] = useState(false);
  const [selectedExpiry, setSelectedExpiry] = useState<string>("active");
  // Wrap setBottomTab so any user click locks the user's choice (we stop
  // auto-flipping after that).
  const setBottomTabSticky = (k: BottomTabKey) => {
    setHasInteractedWithTabs(true);
    setBottomTab(k);
  };
  // First-load auto-flip: positions when there's open exposure, queue when flat.
  const positionCountForEffect = useMemo(
    () => ((status.positions as CommodityPosition[] | undefined) ?? []).length,
    [status.positions],
  );
  useEffect(() => {
    if (hasInteractedWithTabs) return;
    setBottomTab(positionCountForEffect > 0 ? "positions" : "queue");
  }, [hasInteractedWithTabs, positionCountForEffect]);

  // Keyboard nav: 1-7 jumps tabs, Esc closes any open modal. Power-user
  // affordance modelled on Bloomberg / TWS hotkeys. Ignored when the focus
  // is inside an input, select, or textarea so it doesn't fight typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement | null;
      const inField =
        tgt &&
        (tgt.tagName === "INPUT" || tgt.tagName === "TEXTAREA" || tgt.tagName === "SELECT");
      if (inField) return;
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      const idx = parseInt(e.key, 10);
      if (!Number.isNaN(idx) && idx >= 1 && idx <= BOTTOM_TABS.length) {
        e.preventDefault();
        setBottomTabSticky(BOTTOM_TABS[idx - 1].key);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const selectedRow = useMemo(
    () => rows.find((r) => r.symbol === selectedSymbol) || null,
    [rows, selectedSymbol],
  );

  // ── Header / status tiles ──────────────────────────────────────────
  const totalEquity = finiteNumber(summary.total_equity);
  const initialCapital = finiteNumber(summary.initial_capital, 1_000_000);
  const unrealized = positions.reduce(
    (acc, p) => acc + finiteNumber(p.unrealized_pnl),
    0,
  );
  // Day P&L = closed trades booked today + current open MTM. Backend
  // `today_trades` can include open rows, so rebuild the closed bucket here.
  const dayClosedRealized = closedTodayTrades.reduce((acc, t) => acc + finiteNumber(t.pnl), 0);
  const dayPnl = dayClosedRealized + unrealized;
  const realizedLifetime = finiteNumber(
    summary.realized_pnl_lifetime ?? summary.realized_pnl,
  );
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
  const liveDotClass = isStreaming
    ? "bg-emerald-400 shadow-[0_0_6px_rgba(74,222,128,0.7)]"
    : overviewQuery.hasSnapshot
      ? "bg-amber-400"
      : "bg-text-muted/40";
  const liveLabel = isStreaming
    ? `LIVE · ${Object.keys(ticks).length}/${tickSymbols.length}`
    : overviewQuery.hasSnapshot
      ? "SNAPSHOT"
      : "OFFLINE";

  // Market session pill (MCX open / closed). Re-uses trading_calendar from
  // the overview payload — no extra request.
  const cal = (status as unknown as { trading_calendar?: Record<string, unknown> })
    .trading_calendar || {};
  const marketOpen = Boolean(cal.is_open);
  const marketLabel = marketOpen
    ? "MCX OPEN"
    : `MCX ${String(cal.status || "closed").toUpperCase()}`;
  const marketTone = marketOpen
    ? "text-emerald-300"
    : String(cal.status || "").toLowerCase() === "break"
      ? "text-amber-300"
      : "text-text-muted";

  // Broker badges from data_health.{fyers,upstox}_token_health
  const dataHealth = (status as unknown as { data_health?: Record<string, Record<string, unknown>> })
    .data_health || {};
  const fyersValid = Boolean(dataHealth.fyers_token_health?.valid);
  const upstoxValid = Boolean(dataHealth.upstox_token_health?.valid);

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-bg-primary text-text-primary">
      {/* ── Header (single dense line) ──────────────────────────────
          Left: liveness dot · market status · title
          Middle: account values (equity, day P&L, open/armed)
          Right: PAPER badge · broker dots · action buttons
          The previous header had duplicate descriptions and tiles that
          consumed two rows on most screens — this version stays single
          line on a 1366-wide viewport. */}
      <header className="flex items-center gap-3 border-b border-bg-secondary/30 px-3 py-1.5 text-[11px]">
        {/* Left cluster: status dot + market + title */}
        <div className="flex items-center gap-2">
          <span className={`h-2.5 w-2.5 flex-none rounded-full ${liveDotClass}`} title={liveLabel} />
          <span className={`hidden text-[9.5px] font-semibold uppercase tracking-[0.14em] sm:inline ${marketTone}`}>
            {marketLabel}
          </span>
          <h1 className="text-[14px] font-semibold tracking-tight">Commodities</h1>
          <span
            className={`rounded px-1.5 py-[1.5px] text-[9.5px] font-medium uppercase tracking-[0.14em] ${
              killActive
                ? "bg-rose-500/15 text-rose-300"
                : running
                  ? "bg-emerald-500/15 text-emerald-300"
                  : loopActive
                    ? "bg-sky-500/15 text-sky-300"
                    : startRequired
                      ? "bg-amber-500/15 text-amber-200"
                      : "bg-bg-secondary/40 text-text-muted"
            }`}
            title={status.last_message || ""}
          >
            {statusLabel}
          </span>
        </div>

        {/* Middle: portfolio values */}
        <div className="ml-auto flex items-baseline gap-4 font-mono">
          <HeaderStat
            k="EQUITY"
            v={formatINR(totalEquity)}
            sub={formatPct(equityPct, 2)}
            tone={equityPct >= 0 ? "text-emerald-300" : "text-rose-300"}
          />
          <HeaderStat
            k="DAY P&L"
            v={formatINR(dayPnl)}
            sub={`closed ${formatINR(dayClosedRealized)} · open ${formatINR(unrealized)}`}
            tone={dayPnl >= 0 ? "text-emerald-300" : "text-rose-300"}
          />
          <HeaderStat
            k="REALIZED (LIFE)"
            v={formatINR(realizedLifetime)}
            sub="since account start"
            tone={realizedLifetime >= 0 ? "text-emerald-300" : "text-rose-300"}
          />
          <HeaderStat
            k="OPEN / ARMED"
            v={`${positions.length} / ${armedCount}`}
            sub={`${rows.length} tracked`}
          />
        </div>

        {/* Right: PAPER · broker status · actions */}
        <div className="flex items-center gap-1.5">
          <span
            className="rounded bg-amber-500/15 px-1.5 py-[1.5px] text-[9.5px] font-medium uppercase tracking-[0.14em] text-amber-200"
            title="paper trading book"
          >
            PAPER
          </span>
          <BrokerDot label="FY" ok={fyersValid} title="Fyers broker session" />
          <BrokerDot label="UP" ok={upstoxValid} title="Upstox broker session" />
          <div className="mx-1 h-4 w-px bg-bg-secondary/40" aria-hidden />
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending || killActive || loopActive}
            className="inline-flex items-center gap-1 rounded bg-bg-secondary/25 px-2 py-1 text-[10.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            title="Start commodity agent"
          >
            <CirclePlay className="h-3 w-3" />
            {loopActive ? "Running" : "Start"}
          </button>
          <button
            type="button"
            onClick={() => runOnceMutation.mutate()}
            disabled={runOnceMutation.isPending}
            className="inline-flex items-center gap-1 rounded bg-bg-secondary/25 px-2 py-1 text-[10.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            title="Run one scan"
          >
            <RefreshCcw className="h-3 w-3" />
            Scan
          </button>
          <button
            type="button"
            onClick={() => setStrategyModalOpen(true)}
            className="inline-flex items-center gap-1 rounded bg-bg-secondary/25 px-2 py-1 text-[10.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary"
            title="MP+OF rules · risk caps · universe"
          >
            <Settings className="h-3 w-3" />
            Strategy
          </button>
          <Link
            href="/settings"
            className="rounded bg-bg-secondary/25 px-2 py-1 text-[10.5px] text-text-secondary hover:bg-bg-secondary/40 hover:text-text-primary"
          >
            ⚙
          </Link>
        </div>
      </header>

      {/* ── 50/50 split: top half watchlist · bottom half tabs ─────────── */}
      <main className="grid flex-1 min-h-0 grid-rows-2 gap-2 px-3 py-2">
        {/* TOP HALF — watchlist with CE/PE-style spacing */}
        <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-bg-secondary/30">
          {/* Watchlist title bar with expiry chip */}
          <div className="flex items-center gap-3 border-b border-bg-secondary/30 bg-bg-secondary/15 px-3 py-1.5">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-text-secondary">
              MCX Futures · live
            </h2>
            <span className="text-[10.5px] text-text-muted">
              {rows.length} symbols
              {armedCount > 0 ? ` · ${armedCount} armed` : ""}
              {positions.length > 0 ? ` · ${positions.length} open` : ""}
            </span>
            {/* Expiry selector — surfaces what month the live contracts roll
                into. Currently informational; the backend always tracks the
                active month per symbol. Bound state lets us switch to a
                further-out month for analysis later. */}
            <div className="ml-auto flex items-center gap-2">
              <span className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">Expiry</span>
              <select
                value={selectedExpiry}
                onChange={(e) => setSelectedExpiry(e.target.value)}
                className="h-6 rounded bg-bg-secondary/40 px-1.5 text-[10.5px] text-text-primary focus:outline-none focus:ring-1 focus:ring-bg-active/60"
              >
                {expiryOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {/* Watchlist table */}
          <div className="flex-1 overflow-y-auto">
            <table className="w-full table-fixed">
              <thead className="sticky top-0 z-10 bg-bg-secondary/25 text-[9.5px] uppercase tracking-[0.14em] text-text-muted">
                <tr>
                  <th className="w-[11%] py-2 pl-3 pr-2 text-left">Symbol</th>
                  <th className="w-[8%] px-2 text-right">LTP</th>
                  <th className="w-[8%] px-2 text-right">Change</th>
                  <th className="w-[8%] px-2 text-right">VWAP</th>
                  <th className="w-[25%] px-3 text-left">Market Profile · live</th>
                  <th className="w-[8%] px-2 text-right">CVD</th>
                  <th className="w-[14%] px-2 text-left">Trigger</th>
                  <th className="w-[8%] px-2 text-right">Stop</th>
                  <th className="w-[10%] pl-2 pr-3 text-right">Position</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-10 text-center text-[11.5px] text-text-muted">
                      No instruments yet — waiting for the first scan.
                    </td>
                  </tr>
                ) : (
                  rows.map((row, idx) => (
                    <InstrumentRow
                      key={String(row.symbol || row.underlying)}
                      row={row}
                      position={positionBySymbol[String(row.symbol || "")]}
                      zebra={idx % 2 === 1}
                      onClick={() => setSelectedSymbol(String(row.symbol || ""))}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        {/* BOTTOM HALF — browser-style tabs */}
        <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-bg-secondary/30">
          <nav className="flex items-end gap-1 border-b-2 border-sky-500/70 bg-bg-secondary/30 px-1.5 pt-1.5">
            {BOTTOM_TABS.map((t, idx) => {
              const isActive = bottomTab === t.key;
              const count =
                t.key === "positions"
                  ? positions.length
                  : t.key === "queue"
                    ? armedCount
                    : t.key === "orders"
                      ? orders.length
                      : t.key === "trades"
                        ? trades.length
                        : t.key === "audit"
                          ? auditEvents.length
                          : t.key === "expiry"
                            ? rows.length
                            : null;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setBottomTabSticky(t.key)}
                  title={`Switch to ${t.label} — press ${idx + 1}`}
                  // Chrome-style tab look: each tab is a SOLID filled chip
                  // with a clearly different background tint depending on
                  // active state. Active = sky-blue solid tint that visually
                  // joins the content area below; inactive = darker grey
                  // solid that recedes. This replaces the "active is
                  // transparent + sky strip" pattern which was too subtle.
                  className={`group relative -mb-[2px] rounded-t-md px-3 py-1.5 text-[11px] uppercase tracking-[0.14em] transition-colors ${
                    isActive
                      ? "bg-sky-500/85 text-white shadow-[inset_0_-2px_0_0_rgba(56,189,248,0.95)]"
                      : "bg-bg-secondary/55 text-text-muted hover:bg-bg-secondary/75 hover:text-text-primary"
                  }`}
                >
                  {/* keyboard shortcut hint chip */}
                  <span
                    className={`mr-1.5 inline-flex h-3.5 w-3.5 items-center justify-center rounded text-[9px] font-mono ${
                      isActive ? "bg-white/25 text-white" : "bg-bg-primary/60 text-text-muted"
                    }`}
                  >
                    {idx + 1}
                  </span>
                  {t.label}
                  {count !== null ? (
                    <span
                      className={`ml-1.5 rounded px-1 py-[1px] text-[9.5px] font-mono ${
                        isActive ? "bg-white/25 text-white" : "bg-bg-primary/60 text-text-muted"
                      }`}
                    >
                      {count}
                    </span>
                  ) : null}
                </button>
              );
            })}
            <span className="ml-auto pb-1 text-[10px] text-text-muted">
              {bottomTab === "positions"
                ? "open book · click row → instrument modal"
                : bottomTab === "queue"
                  ? "priority × confidence"
                  : bottomTab === "orders"
                    ? "newest first · click row → instrument"
                    : bottomTab === "trades"
                      ? "closed trades · newest first"
                      : bottomTab === "expiry"
                        ? "MCX futures · roll window 10d"
                        : bottomTab === "stats"
                          ? "portfolio statistics"
                          : "mp_signal.* events"}
            </span>
          </nav>
          <div className="flex-1 min-h-0 overflow-hidden bg-bg-primary px-3 py-2">
            {bottomTab === "positions" ? (
              <PositionsTab
                positions={positions}
                onSelect={(sym) => setSelectedSymbol(sym)}
              />
            ) : null}
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
              <StatsTab summary={summary} trades={allTrades} positions={positions} />
            ) : null}
            {bottomTab === "audit" ? <AuditFeed events={auditEvents} /> : null}
          </div>
        </section>
      </main>

      {/* ── Sticky live-status footer ─────────────────────────────────
          Always-visible single-line strip showing open P&L, ready triggers,
          and last-tick freshness. Pattern from Robinhood / Webull where the
          headline P&L stays in view as the user scrolls the trade ledger.
          Only renders when meaningful (positions open or signals armed). */}
      {positions.length > 0 || armedCount > 0 ? (
        <footer className="flex items-center gap-4 border-t border-bg-secondary/30 bg-bg-secondary/10 px-3 py-1 text-[10.5px]">
          <span className="font-mono uppercase tracking-[0.14em] text-text-muted">
            Live book
          </span>
          {positions.length > 0 ? (
            <>
              <span
                className={`font-mono text-[12px] font-semibold ${unrealized >= 0 ? "text-emerald-300" : "text-rose-300"}`}
              >
                {unrealized >= 0 ? "▲" : "▼"} {formatSigned(unrealized, 0)} unrealised
              </span>
              <span className="font-mono text-text-muted">
                · {positions.length} open · closed today {formatINR(dayClosedRealized)}
              </span>
            </>
          ) : null}
          {armedCount > 0 ? (
            <span className="font-mono text-sky-300">
              · {armedCount} armed
            </span>
          ) : null}
          <span className="ml-auto font-mono text-text-muted">
            {formatIST(status.last_run_at)}
          </span>
        </footer>
      ) : null}

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
          onClose={() => setSelectedSymbol(null)}
        />
      ) : null}
    </div>
  );
}

// ─── Header tile ───────────────────────────────────────────────────────────

function HeaderStat({
  k,
  v,
  sub,
  tone,
}: {
  k: string;
  v: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5 leading-none">
      <span className="text-[9px] uppercase tracking-[0.14em] text-text-muted">{k}</span>
      <span className={`font-mono text-[12.5px] font-semibold ${tone || "text-text-primary"}`}>{v}</span>
      {sub ? <span className="font-mono text-[9px] text-text-muted">{sub}</span> : null}
    </div>
  );
}

function BrokerDot({
  label,
  ok,
  title,
}: {
  label: string;
  ok: boolean;
  title?: string;
}) {
  return (
    <span
      title={`${title || label} · ${ok ? "connected" : "disconnected"}`}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-[1.5px] text-[9.5px] font-medium uppercase tracking-[0.14em] ${
        ok ? "bg-emerald-500/15 text-emerald-300" : "bg-rose-500/15 text-rose-300"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-400" : "bg-rose-400"}`} />
      {label}
    </span>
  );
}
