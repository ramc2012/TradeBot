"use client";

import { useEffect, useMemo, useState, startTransition } from "react";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Crosshair,
  Gauge,
  Layers3,
  RefreshCw,
  RadioTower,
  ShieldCheck,
  Waves,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { getOrderflowSnapshot } from "@/lib/api";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";

type OrderflowSymbol = "NIFTY" | "BANKNIFTY" | "SENSEX" | "CRUDEOIL";

type FootprintLevel = {
  price: number;
  bid_volume: number;
  ask_volume: number;
  delta: number;
  imbalance: number;
  intensity: number;
};

type FootprintBar = {
  timestamp: string;
  label: string;
  open: number;
  high: number;
  low: number;
  close: number;
  total_volume: number;
  buy_volume: number;
  sell_volume: number;
  delta: number;
  cumulative_delta: number;
  imbalance: number;
  levels: FootprintLevel[];
};

type TimeframeSession = {
  session_date: string;
  interval_minutes: number;
  bar_count: number;
  open: number;
  high: number;
  low: number;
  close: number;
  delta: number;
  volume: number;
  footprint: FootprintBar[];
  whales: WhaleMarker[];
};

type HeatmapLevel = {
  price: number;
  side: "bid" | "ask" | "reference";
  label: string;
  kind: string;
  quantity?: number | null;
  intensity: number;
};

type WhaleMarker = {
  id: string;
  timestamp?: string | null;
  label: string;
  side: string;
  direction: "BULLISH" | "BEARISH" | "NEUTRAL";
  price: number;
  strike?: number | null;
  notional: number;
  volume: number;
  oi_change?: number | null;
  score: number;
  source: string;
};

type DomLevel = {
  price: number;
  quantity: number;
  cumulative_quantity: number;
};

type DomLadder = {
  bids: DomLevel[];
  asks: DomLevel[];
  mid_price?: number | null;
  spread: number;
  level_count: number;
};

type TapeRow = {
  timestamp?: string | null;
  label: string;
  price: number;
  quantity: number;
  side: string;
  tone: "up" | "down" | "neutral";
  is_block: boolean;
};

type OrderflowInstrument = {
  symbol: OrderflowSymbol;
  display: string;
  market: string;
  instrument_proxy?: string;
  price: number;
  change: number;
  change_pct: number;
  timestamp?: string | null;
  age_seconds?: number | null;
  data_quality?: {
    execution_ready?: boolean;
    degraded_reason?: string | null;
    order_flow_source?: string;
    quote_source?: string;
    tick_history_count?: number;
    trade_print_count?: number;
    stale_data_seconds?: number;
  };
  source?: {
    history?: string;
    history_symbol?: string;
    quote?: string;
    order_flow?: string;
    common_fetch?: string;
  };
  session?: {
    date?: string;
    mode?: string;
    lot_size?: number;
    tick_size?: number;
  };
  metrics: {
    spread: number;
    top_imbalance: number;
    depth_imbalance: number;
    delta: number;
    cumulative_delta: number;
    vwap: number;
    vwap_drift: number;
    queue_pressure: number;
    trade_imbalance: number;
    order_flow_imbalance: number;
    book_pressure: number;
    toxicity_score: number;
    timing_confidence: number;
    execution_aggression?: string;
  };
  market_profile: {
    poc: number;
    vah: number;
    val: number;
    initial_balance_high: number;
    initial_balance_low: number;
    day_type?: string;
    trend?: string;
  };
  footprint: FootprintBar[];
  timeframes?: Record<string, {
    interval_minutes: number;
    session_count: number;
    sessions: TimeframeSession[];
  }>;
  history?: {
    history_source?: string;
    history_symbol?: string;
    session_dates?: string[];
    error?: string;
  };
  heatmap: HeatmapLevel[];
  whales: WhaleMarker[];
  dom?: DomLadder;
  tape?: TapeRow[];
  synthetic_quote?: boolean;
  raw_bar_count: number;
  raw_trade_count: number;
  error?: string;
};

type OrderflowSnapshot = {
  as_of: string;
  symbols: OrderflowSymbol[];
  instruments: OrderflowInstrument[];
  reference_model: Record<string, string>;
};

const SYMBOLS: OrderflowSymbol[] = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"];
const TIMEFRAMES = ["3", "5", "15", "30"] as const;
const CHART_WIDTH = 1680;
const CHART_HEIGHT = 760;
const TOP_PAD = 34;
const RIGHT_PAD = 118;
const BOTTOM_PAD = 76;
const LEFT_PAD = 92;

function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatPrice(value: unknown, digits = 2): string {
  const parsed = finiteNumber(value);
  if (!parsed) return "--";
  return parsed.toLocaleString("en-IN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatSigned(value: unknown, digits = 2): string {
  const parsed = finiteNumber(value);
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${parsed.toLocaleString("en-IN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  })}`;
}

function formatPct(value: unknown, digits = 1): string {
  return `${formatSigned(finiteNumber(value) * 100, digits)}%`;
}

function formatCompact(value: unknown): string {
  const parsed = Math.abs(finiteNumber(value));
  const sign = finiteNumber(value) < 0 ? "-" : "";
  if (parsed >= 10_000_000) return `${sign}${(parsed / 10_000_000).toFixed(1)}Cr`;
  if (parsed >= 100_000) return `${sign}${(parsed / 100_000).toFixed(1)}L`;
  if (parsed >= 1_000) return `${sign}${(parsed / 1_000).toFixed(1)}k`;
  return `${sign}${parsed.toFixed(0)}`;
}

function formatAge(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "age --";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function formatTime(raw?: string | null): string {
  if (!raw) return "--";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });
}

function toneForNumber(value: number) {
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function colorForDelta(delta: number, alpha = 1) {
  return delta >= 0 ? `rgba(0, 212, 163, ${alpha})` : `rgba(255, 71, 87, ${alpha})`;
}

function priceDomain(instrument: OrderflowInstrument): [number, number] {
  const values = [
    ...instrument.footprint.flatMap((bar) => [bar.high, bar.low, bar.close, bar.open]),
    ...instrument.heatmap.map((level) => level.price),
    instrument.market_profile.poc,
    instrument.market_profile.vah,
    instrument.market_profile.val,
    instrument.market_profile.initial_balance_high,
    instrument.market_profile.initial_balance_low,
    instrument.price,
  ].filter((value) => Number.isFinite(value) && value > 0);
  const whalePrices = instrument.whales
    .map((item) => item.price)
    .filter((value) => Number.isFinite(value) && value > 0);
  const min = Math.min(...values, ...whalePrices);
  const max = Math.max(...values, ...whalePrices);
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
    const anchor = instrument.price || 1;
    return [anchor * 0.99, anchor * 1.01];
  }
  const padding = Math.max((max - min) * 0.12, instrument.session?.tick_size || 1);
  return [min - padding, max + padding];
}

function buildPath(points: Array<{ x: number; y: number }>): string {
  return points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
}

function findNearestBarIndex(footprint: FootprintBar[], timestamp?: string | null): number {
  if (!footprint.length) return 0;
  if (!timestamp) return footprint.length - 1;
  const target = new Date(timestamp).getTime();
  if (!Number.isFinite(target)) return footprint.length - 1;
  let bestIndex = footprint.length - 1;
  let bestDistance = Number.POSITIVE_INFINITY;
  footprint.forEach((bar, index) => {
    const current = new Date(bar.timestamp).getTime();
    const distance = Math.abs(current - target);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  });
  return bestIndex;
}

function MetricTile({
  icon: Icon,
  label,
  value,
  detail,
  hot,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  detail: string;
  hot?: boolean;
}) {
  return (
    <div className="min-w-0 border border-bg-border bg-black/24 px-3 py-2">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-text-muted">
        <Icon size={13} className={hot ? "text-accent-amber" : "text-text-secondary"} />
        <span className="truncate">{label}</span>
      </div>
      <div className={clsx("mt-1 font-mono text-lg font-semibold", hot ? "text-accent-amber" : "text-text-primary")}>
        {value}
      </div>
      <div className="mt-0.5 truncate text-[11px] text-text-muted">{detail}</div>
    </div>
  );
}

function InstrumentTabs({
  selected,
  activeInstrument,
  onSelect,
}: {
  selected: OrderflowSymbol;
  activeInstrument?: OrderflowInstrument;
  onSelect: (symbol: OrderflowSymbol) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      {SYMBOLS.map((symbol) => {
        const item = activeInstrument?.symbol === symbol ? activeInstrument : undefined;
        const active = selected === symbol;
        const healthy = item?.data_quality?.execution_ready !== false && !item?.error;
        return (
          <button
            key={symbol}
            type="button"
            onClick={() => startTransition(() => onSelect(symbol))}
            className={clsx(
              "border px-3 py-2 text-left transition-colors",
              active
                ? "border-accent-cyan/60 bg-accent-cyan/10"
                : "border-bg-border bg-black/20 hover:border-bg-active hover:bg-bg-hover/50",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm font-semibold tracking-[0.12em] text-text-primary">{symbol}</span>
              <span className={clsx("h-2 w-2 rounded-full", healthy ? "bg-accent-green" : "bg-accent-amber")} />
            </div>
            <div className="mt-1 flex items-baseline gap-2 font-mono">
              <span className="text-lg text-text-primary">{item ? formatPrice(item.price, symbol === "CRUDEOIL" ? 0 : 2) : "select"}</span>
              {item ? (
                <span className={clsx("text-xs", toneForNumber(item.change_pct ?? 0))}>
                  {formatSigned(item.change_pct, 2)}%
                </span>
              ) : null}
            </div>
            <div className="mt-1 truncate text-[10px] uppercase tracking-[0.14em] text-text-muted">
              {item?.synthetic_quote ? (
                <span className="text-accent-amber">proxy · bar-derived</span>
              ) : (
                <>{item?.source?.order_flow ?? "broker history"} · {item ? formatAge(item.age_seconds) : "load on select"}</>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}

function TimeframeTabs({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (timeframe: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {TIMEFRAMES.map((timeframe) => {
        const active = selected === timeframe;
        return (
          <button
            key={timeframe}
            type="button"
            onClick={() => startTransition(() => onSelect(timeframe))}
            className={clsx(
              "min-w-[76px] border px-3 py-2 font-mono text-xs font-semibold uppercase tracking-[0.14em] transition-colors",
              active
                ? "border-accent-amber/70 bg-accent-amber/12 text-accent-amber"
                : "border-bg-border bg-black/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
            )}
          >
            {timeframe}m
          </button>
        );
      })}
    </div>
  );
}

function SessionStrip({
  sessions,
  selected,
  onSelect,
}: {
  sessions: TimeframeSession[];
  selected: string;
  onSelect: (sessionDate: string) => void;
}) {
  const ordered = sessions.slice().reverse();
  return (
    <div className="flex gap-2 overflow-x-auto pb-1">
      <button
        type="button"
        onClick={() => startTransition(() => onSelect("latest"))}
        className={clsx(
          "shrink-0 border px-3 py-2 font-mono text-[11px] uppercase tracking-[0.14em]",
          selected === "latest"
            ? "border-accent-cyan/70 bg-accent-cyan/10 text-accent-cyan"
            : "border-bg-border bg-black/25 text-text-secondary",
        )}
      >
        latest
      </button>
      {ordered.map((session) => {
        const active = selected === session.session_date;
        return (
          <button
            key={session.session_date}
            type="button"
            onClick={() => startTransition(() => onSelect(session.session_date))}
            className={clsx(
              "shrink-0 border px-3 py-2 text-left font-mono transition-colors",
              active
                ? "border-accent-cyan/70 bg-accent-cyan/10"
                : "border-bg-border bg-black/25 hover:border-bg-active",
            )}
          >
            <div className="text-xs font-semibold text-text-primary">{session.session_date}</div>
            <div className={clsx("mt-0.5 text-[11px]", toneForNumber(session.delta))}>
              {formatSigned(session.delta, 0)} · {session.bar_count} bars
            </div>
          </button>
        );
      })}
    </div>
  );
}

function OrderflowChart({
  instrument,
  timeframe,
  session,
}: {
  instrument: OrderflowInstrument;
  timeframe: string;
  session?: TimeframeSession;
}) {
  const footprint = instrument.footprint;
  const [minPrice, maxPrice] = priceDomain(instrument);
  const plotWidth = CHART_WIDTH - LEFT_PAD - RIGHT_PAD;
  const plotHeight = CHART_HEIGHT - TOP_PAD - BOTTOM_PAD;
  const slot = plotWidth / Math.max(footprint.length, 1);
  const yFor = (price: number) => TOP_PAD + ((maxPrice - price) / Math.max(maxPrice - minPrice, 0.01)) * plotHeight;
  const xFor = (index: number) => LEFT_PAD + slot * index + slot / 2;
  const closePath = buildPath(footprint.map((bar, index) => ({ x: xFor(index), y: yFor(bar.close) })));
  const yTicks = Array.from({ length: 7 }, (_, index) => minPrice + ((maxPrice - minPrice) * index) / 6).reverse();
  const maxAbsDelta = Math.max(...footprint.flatMap((bar) => [Math.abs(bar.delta), ...bar.levels.map((level) => Math.abs(level.delta))]), 1);
  const referenceLevels = [
    { label: "POC", price: instrument.market_profile.poc, color: "#00d4a3" },
    { label: "VAH", price: instrument.market_profile.vah, color: "#3b82f6" },
    { label: "VAL", price: instrument.market_profile.val, color: "#3b82f6" },
    { label: "IBH", price: instrument.market_profile.initial_balance_high, color: "#ffa502" },
    { label: "IBL", price: instrument.market_profile.initial_balance_low, color: "#ffa502" },
  ].filter((level) => level.price > 0);

  return (
    <div className="overflow-hidden border border-bg-border bg-black">
      <div className="flex flex-col gap-2 border-b border-bg-border bg-bg-secondary/70 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="font-mono text-xs font-semibold uppercase tracking-[0.22em] text-accent-amber">
            Liquidity Heatmap · Footprint · Whale Markers
          </div>
          <div className="mt-1 text-sm text-text-secondary">
            {instrument.history?.history_symbol ?? instrument.source?.history_symbol ?? instrument.display} · {timeframe}m · {session?.session_date ?? instrument.session?.date ?? "--"} · {footprint.length} bars
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs uppercase tracking-[0.16em] text-text-muted">
          <span className="inline-flex items-center gap-1 text-accent-green"><span className="h-2 w-2 bg-accent-green" />buy delta</span>
          <span className="inline-flex items-center gap-1 text-accent-red"><span className="h-2 w-2 bg-accent-red" />sell delta</span>
          <span className="inline-flex items-center gap-1 text-accent-amber"><span className="h-2 w-2 rounded-full bg-accent-amber" />whale</span>
        </div>
      </div>

      <div className="relative overflow-x-auto">
        <svg
          className="h-auto w-full min-w-0 sm:min-w-[640px] lg:min-w-[960px]"
          viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={`${instrument.symbol} orderflow chart`}
        >
          <defs>
            <linearGradient id="orderflowBackground" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="#111827" stopOpacity="0.88" />
              <stop offset="100%" stopColor="#030712" stopOpacity="1" />
            </linearGradient>
            <filter id="softGlow" x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="3" result="coloredBlur" />
              <feMerge>
                <feMergeNode in="coloredBlur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          <rect width={CHART_WIDTH} height={CHART_HEIGHT} fill="url(#orderflowBackground)" />
          <rect x={LEFT_PAD} y={TOP_PAD} width={plotWidth} height={plotHeight} fill="#050816" stroke="#1e2d45" />

          {yTicks.map((tick) => (
            <g key={tick}>
              <line x1={LEFT_PAD} x2={CHART_WIDTH - RIGHT_PAD} y1={yFor(tick)} y2={yFor(tick)} stroke="#1e2d45" strokeDasharray="4 7" opacity="0.55" />
              <text x={22} y={yFor(tick) + 5} fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="14">
                {formatPrice(tick, instrument.symbol === "CRUDEOIL" ? 0 : 0)}
              </text>
            </g>
          ))}

          {instrument.heatmap.map((level, index) => {
            const y = yFor(level.price);
            const color = level.side === "bid" ? "#00d4a3" : level.side === "ask" ? "#ff4757" : "#ffa502";
            const height = level.kind === "book_depth" ? 10 : 6;
            const opacity = clamp(0.08 + level.intensity * 0.42, 0.08, 0.5);
            return (
              <g key={`${level.kind}-${level.price}-${index}`}>
                <rect
                  x={LEFT_PAD}
                  y={y - height / 2}
                  width={plotWidth}
                  height={height}
                  fill={color}
                  opacity={opacity}
                />
                {level.side === "reference" ? (
                  <text x={CHART_WIDTH - RIGHT_PAD + 10} y={y + 5} fill={color} fontFamily="JetBrains Mono" fontSize="13">
                    {level.label} {formatPrice(level.price, instrument.symbol === "CRUDEOIL" ? 0 : 0)}
                  </text>
                ) : null}
              </g>
            );
          })}

          {footprint.map((bar, index) => {
            const xCenter = xFor(index);
            const bodyTop = yFor(Math.max(bar.open, bar.close));
            const bodyBottom = yFor(Math.min(bar.open, bar.close));
            const bodyHeight = Math.max(3, bodyBottom - bodyTop);
            const candleColor = bar.close >= bar.open ? "#00d4a3" : "#ff4757";
            return (
              <g key={`${bar.timestamp}-${index}`}>
                <line x1={xCenter} x2={xCenter} y1={yFor(bar.high)} y2={yFor(bar.low)} stroke={candleColor} strokeWidth="1.2" opacity="0.88" />
                <rect x={xCenter - Math.min(slot * 0.13, 7)} y={bodyTop} width={Math.min(slot * 0.26, 14)} height={bodyHeight} fill={candleColor} opacity="0.8" />
                {bar.levels.map((level) => {
                  const levelY = yFor(level.price);
                  const alpha = clamp(0.12 + Math.abs(level.delta) / maxAbsDelta * 0.58, 0.12, 0.72);
                  const rectWidth = clamp(level.intensity * slot * 0.54, 3, slot * 0.58);
                  return (
                    <rect
                      key={`${bar.timestamp}-${level.price}`}
                      x={xCenter - rectWidth / 2}
                      y={levelY - 3}
                      width={rectWidth}
                      height={6}
                      fill={colorForDelta(level.delta, alpha)}
                    />
                  );
                })}
                {index % 2 === 0 || footprint.length <= 8 ? (
                  <text x={xCenter} y={CHART_HEIGHT - 28} textAnchor="middle" fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="12">
                    {bar.label}
                  </text>
                ) : null}
                <rect
                  x={xCenter - slot * 0.28}
                  y={CHART_HEIGHT - 44}
                  width={slot * 0.56}
                  height={clamp(Math.abs(bar.delta) / maxAbsDelta * 32, 2, 32)}
                  fill={colorForDelta(bar.delta, 0.85)}
                  transform={bar.delta >= 0 ? undefined : `translate(0 ${-clamp(Math.abs(bar.delta) / maxAbsDelta * 32, 2, 32)})`}
                  opacity="0.86"
                />
              </g>
            );
          })}

          {closePath ? <path d={closePath} fill="none" stroke="#f8fafc" strokeWidth="2.4" opacity="0.9" /> : null}

          {referenceLevels.map((level) => (
            <g key={level.label}>
              <line x1={LEFT_PAD} x2={CHART_WIDTH - RIGHT_PAD} y1={yFor(level.price)} y2={yFor(level.price)} stroke={level.color} strokeDasharray="5 5" opacity="0.75" />
              <text x={LEFT_PAD + 12} y={yFor(level.price) - 7} fill={level.color} fontFamily="JetBrains Mono" fontSize="13">
                {level.label} {formatPrice(level.price, instrument.symbol === "CRUDEOIL" ? 0 : 0)}
              </text>
            </g>
          ))}

          {instrument.whales.map((whale) => {
            const index = findNearestBarIndex(footprint, whale.timestamp);
            const x = xFor(index);
            const y = yFor(clamp(whale.price, minPrice, maxPrice));
            const radius = clamp(5 + whale.score / 9, 8, 18);
            const color = whale.direction === "BULLISH" ? "#00d4a3" : whale.direction === "BEARISH" ? "#ff4757" : "#ffa502";
            return (
              <g key={whale.id} filter="url(#softGlow)">
                <circle cx={x} cy={y} r={radius} fill={color} opacity="0.22" stroke={color} strokeWidth="1.4" />
                <circle cx={x} cy={y} r={Math.max(radius * 0.42, 4)} fill={color} opacity="0.82" />
                <text x={x + radius + 6} y={y - 5} fill={color} fontFamily="JetBrains Mono" fontSize="13" fontWeight="700">
                  {whale.side}
                </text>
                <text x={x + radius + 6} y={y + 11} fill="#cbd5e1" fontFamily="JetBrains Mono" fontSize="11">
                  {whale.score}
                </text>
              </g>
            );
          })}

          <line x1={LEFT_PAD} x2={CHART_WIDTH - RIGHT_PAD} y1={yFor(instrument.price)} y2={yFor(instrument.price)} stroke="#ffffff" strokeDasharray="2 6" opacity="0.6" />
          <rect x={CHART_WIDTH - RIGHT_PAD + 8} y={yFor(instrument.price) - 15} width="92" height="30" rx="2" fill="#e2e8f0" opacity="0.12" />
          <text x={CHART_WIDTH - RIGHT_PAD + 15} y={yFor(instrument.price) + 5} fill="#e2e8f0" fontFamily="JetBrains Mono" fontSize="14" fontWeight="700">
            {formatPrice(instrument.price, instrument.symbol === "CRUDEOIL" ? 0 : 0)}
          </text>
        </svg>
      </div>
    </div>
  );
}

function WhaleTape({ whales }: { whales: WhaleMarker[] }) {
  return (
    <div className="border border-bg-border bg-black">
      <div className="flex items-center justify-between border-b border-bg-border px-3 py-2">
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-amber">Whale Tape</div>
        <div className="text-[10px] uppercase tracking-[0.16em] text-text-muted">{whales.length} marked</div>
      </div>
      <div className="max-h-[315px] overflow-y-auto">
        {whales.length ? whales.map((whale) => (
          <div key={whale.id} className="grid grid-cols-[52px_minmax(0,1fr)_56px] gap-2 border-b border-bg-border/70 px-3 py-2 last:border-b-0">
            <div className="font-mono text-[11px] text-text-muted">{formatTime(whale.timestamp)}</div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className={clsx("font-mono text-xs font-bold", whale.direction === "BULLISH" ? "text-accent-green" : whale.direction === "BEARISH" ? "text-accent-red" : "text-accent-amber")}>
                  {whale.side}
                </span>
                <span className="truncate text-[11px] uppercase tracking-[0.12em] text-text-secondary">{whale.label}</span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-text-muted">
                {whale.strike ? `${formatPrice(whale.strike, 0)} · ` : ""}{formatCompact(whale.notional)} · vol {formatCompact(whale.volume)}
              </div>
            </div>
            <div className="text-right font-mono text-sm font-semibold text-text-primary">{whale.score}</div>
          </div>
        )) : (
          <div className="px-3 py-8 text-center text-xs text-text-muted">No whale markers in the current broker snapshot.</div>
        )}
      </div>
    </div>
  );
}

function LatestFootprint({ instrument }: { instrument: OrderflowInstrument }) {
  const latest = instrument.footprint[instrument.footprint.length - 1];
  const rows = latest?.levels?.slice().reverse() ?? [];
  return (
    <div className="border border-bg-border bg-black">
      <div className="flex items-center justify-between border-b border-bg-border px-3 py-2">
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-cyan">Latest Footprint</div>
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-text-muted">{latest?.label ?? "--"}</div>
      </div>
      <div className="grid grid-cols-[76px_1fr_1fr_72px] border-b border-bg-border px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-text-muted">
        <span>Price</span>
        <span>Bid</span>
        <span>Ask</span>
        <span className="text-right">Delta</span>
      </div>
      <div className="max-h-[275px] overflow-y-auto">
        {rows.map((row) => (
          <div key={row.price} className="grid grid-cols-[76px_1fr_1fr_72px] items-center gap-2 border-b border-bg-border/55 px-3 py-1.5 font-mono text-[11px] last:border-b-0">
            <span className="text-text-primary">{formatPrice(row.price, instrument.symbol === "CRUDEOIL" ? 0 : 0)}</span>
            <span className="bg-accent-red/10 px-1 text-accent-red">{formatCompact(row.bid_volume)}</span>
            <span className="bg-accent-green/10 px-1 text-accent-green">{formatCompact(row.ask_volume)}</span>
            <span className={clsx("text-right", toneForNumber(row.delta))}>{formatSigned(row.delta, 0)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function DataIntegrityPanel({ instrument }: { instrument: OrderflowInstrument }) {
  const quality = instrument.data_quality ?? {};
  const healthy = quality.execution_ready !== false && !quality.degraded_reason;
  return (
    <div className="border border-bg-border bg-black px-3 py-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-green">Data Integrity</div>
        <span className={clsx("font-mono text-[10px] uppercase tracking-[0.16em]", healthy ? "text-accent-green" : "text-accent-amber")}>
          {healthy ? "ready" : "degraded"}
        </span>
      </div>
      <div className="mt-3 space-y-2 text-xs">
        <InfoRow label="Common fetch" value={instrument.source?.common_fetch ?? "auction live snapshot"} />
        <InfoRow label="History source" value={instrument.source?.history ?? "--"} />
        <InfoRow label="Quote source" value={instrument.source?.quote ?? "--"} />
        <InfoRow label="Orderflow source" value={instrument.source?.order_flow ?? "--"} />
        <InfoRow label="Snapshot age" value={formatAge(instrument.age_seconds)} />
        <InfoRow label="Tick / prints" value={`${quality.tick_history_count ?? 0} / ${quality.trade_print_count ?? instrument.raw_trade_count ?? 0}`} />
        <InfoRow label="Degraded reason" value={quality.degraded_reason || "none"} tone={quality.degraded_reason ? "warn" : "normal"} />
      </div>
      <div className="mt-3 border-t border-bg-border pt-3 text-[11px] leading-5 text-text-muted">
        Footprint and whales reuse broker ticks, 1-minute history, MP levels, and option-chain pressure. Raw exchange market-by-order sweeps are not available in this app, so sweep-style markers are labelled as proxies.
      </div>
    </div>
  );
}

/**
 * Bookmap/Quantower-style DOM ladder. Top-N bid + ask levels side-by-side
 * with depth bars sized by per-level cumulative quantity. When the
 * underlying broker doesn't publish L2 depth (e.g. MCX commodities in
 * this app), we render an explicit empty state with the synthetic-data
 * badge instead of pretending to have a book.
 */
function DomLadder({
  dom,
  synthetic,
  symbol,
}: {
  dom: DomLadder | undefined;
  synthetic: boolean;
  symbol: OrderflowSymbol;
}) {
  const hasData = dom && (dom.bids.length > 0 || dom.asks.length > 0);
  if (!hasData) {
    return (
      <div className="border border-bg-border bg-black px-3 py-3">
        <div className="flex items-baseline justify-between gap-2">
          <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-text-secondary">
            DOM ladder
          </div>
          <span className="font-mono text-[10px] uppercase text-accent-amber">L2 unavailable</span>
        </div>
        <div className="mt-3 text-[11px] leading-5 text-text-muted">
          {synthetic
            ? "Quote source is the commodity 1-minute history bridge — exchange L2 is not exposed via the current broker session. Footprint and metrics fall back to bar-derived inference."
            : "No depth rows received from broker chain. The footprint and queue-pressure metrics still update on every refresh."}
        </div>
      </div>
    );
  }
  const maxBidSize = Math.max(1, ...dom.bids.map((level) => level.cumulative_quantity));
  const maxAskSize = Math.max(1, ...dom.asks.map((level) => level.cumulative_quantity));
  const priceDigits = symbol === "CRUDEOIL" ? 0 : 2;
  return (
    <div className="border border-bg-border bg-black px-3 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-cyan">
          DOM ladder
        </div>
        <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-muted">
          spread {formatPrice(dom.spread, priceDigits)} · {dom.level_count} lvls
        </div>
      </div>
      <div className="mt-2 grid grid-cols-[1fr_auto_1fr] gap-x-3">
        {/* Bid side — descending, left-aligned bar drawn from right edge */}
        <div className="text-right">
          {dom.bids.map((level) => {
            const widthPct = Math.min(100, (level.cumulative_quantity / maxBidSize) * 100);
            return (
              <div key={`bid-${level.price}`} className="relative flex items-center justify-end gap-2 py-0.5">
                <span
                  className="absolute inset-y-0 right-0 bg-accent-green/15"
                  style={{ width: `${widthPct}%` }}
                  aria-hidden
                />
                <span className="relative font-mono text-[11px] text-accent-green/80">{formatCompact(level.cumulative_quantity)}</span>
                <span className="relative font-mono text-[11px] text-text-secondary">{formatCompact(level.quantity)}</span>
              </div>
            );
          })}
        </div>
        {/* Price column — bids descend matching asks ascend; align by row */}
        <div>
          {Array.from({ length: Math.max(dom.bids.length, dom.asks.length) }, (_, idx) => {
            const bid = dom.bids[idx];
            const ask = dom.asks[idx];
            return (
              <div key={idx} className="grid grid-cols-2 gap-1 py-0.5">
                <span className={clsx("text-right font-mono text-[11px]", bid ? "text-accent-green" : "text-text-muted")}>
                  {bid ? formatPrice(bid.price, priceDigits) : "—"}
                </span>
                <span className={clsx("text-left font-mono text-[11px]", ask ? "text-accent-red" : "text-text-muted")}>
                  {ask ? formatPrice(ask.price, priceDigits) : "—"}
                </span>
              </div>
            );
          })}
        </div>
        {/* Ask side — ascending, left-aligned bar drawn from left edge */}
        <div>
          {dom.asks.map((level) => {
            const widthPct = Math.min(100, (level.cumulative_quantity / maxAskSize) * 100);
            return (
              <div key={`ask-${level.price}`} className="relative flex items-center gap-2 py-0.5">
                <span
                  className="absolute inset-y-0 left-0 bg-accent-red/15"
                  style={{ width: `${widthPct}%` }}
                  aria-hidden
                />
                <span className="relative font-mono text-[11px] text-text-secondary">{formatCompact(level.quantity)}</span>
                <span className="relative font-mono text-[11px] text-accent-red/80">{formatCompact(level.cumulative_quantity)}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div className="mt-3 border-t border-bg-border pt-2 text-[10px] uppercase tracking-[0.16em] text-text-muted">
        Bar lengths are cumulative depth · inner numbers are level quantity
      </div>
    </div>
  );
}

/**
 * Sierra Chart-style Time and Sales tape. Newest print first, painted
 * green for up-ticks and red for down-ticks, with bold rows for block
 * trades (≥3× median quantity in the recent window). Side chip shows
 * the aggressor when the broker exposes it.
 */
function TapeFeed({
  tape,
  symbol,
  synthetic,
}: {
  tape: TapeRow[] | undefined;
  symbol: OrderflowSymbol;
  synthetic: boolean;
}) {
  const rows = tape ?? [];
  const priceDigits = symbol === "CRUDEOIL" ? 0 : 2;
  return (
    <div className="border border-bg-border bg-black px-3 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-amber">
          Time & sales
        </div>
        <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-muted">
          {rows.length} prints {synthetic ? "· bar-derived" : ""}
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="mt-3 text-[11px] leading-5 text-text-muted">
          {synthetic
            ? "MCX prints not exposed by broker session — use footprint delta for flow signal."
            : "Tape will populate as the next minute's trade prints land in the broker snapshot."}
        </div>
      ) : (
        <div className="mt-2 max-h-[260px] overflow-y-auto font-mono text-[11px]">
          <div className="grid grid-cols-[auto_1fr_auto_auto] gap-x-3 border-b border-bg-border pb-1 text-[9px] uppercase tracking-[0.14em] text-text-muted">
            <span>Time</span>
            <span className="text-right">Price</span>
            <span className="text-right">Qty</span>
            <span className="text-right">Side</span>
          </div>
          {rows.slice(0, 40).map((row, idx) => {
            const tone =
              row.tone === "up"
                ? "text-accent-green"
                : row.tone === "down"
                  ? "text-accent-red"
                  : "text-text-secondary";
            return (
              <div
                key={`${row.timestamp ?? ""}-${row.price}-${idx}`}
                className={clsx(
                  "grid grid-cols-[auto_1fr_auto_auto] gap-x-3 py-0.5",
                  row.is_block && "bg-accent-amber/10",
                )}
              >
                <span className="text-text-muted">{row.label || "--"}</span>
                <span className={clsx("text-right", tone, row.is_block && "font-semibold")}>
                  {formatPrice(row.price, priceDigits)}
                </span>
                <span className={clsx("text-right", row.is_block ? "text-accent-amber font-semibold" : "text-text-secondary")}>
                  {formatCompact(row.quantity)}
                </span>
                <span
                  className={clsx(
                    "text-right uppercase text-[10px] tracking-[0.14em]",
                    row.side === "buy" ? "text-accent-green/80" : row.side === "sell" ? "text-accent-red/80" : "text-text-muted",
                  )}
                >
                  {row.side === "buy" ? "B" : row.side === "sell" ? "S" : "·"}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function InfoRow({ label, value, tone = "normal" }: { label: string; value: string; tone?: "normal" | "warn" }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-text-muted">{label}</span>
      <span className={clsx("truncate text-right font-mono", tone === "warn" ? "text-accent-amber" : "text-text-secondary")}>{value}</span>
    </div>
  );
}

export default function OrderflowWorkbench() {
  const [selectedSymbol, setSelectedSymbol] = useState<OrderflowSymbol>("NIFTY");
  const [selectedTimeframe, setSelectedTimeframe] = useState<string>("15");
  const [selectedSessionDate, setSelectedSessionDate] = useState<string>("latest");

  useEffect(() => {
    setSelectedSessionDate("latest");
  }, [selectedSymbol, selectedTimeframe]);

  const query = usePersistentSnapshotQuery<OrderflowSnapshot>({
    queryKey: ["orderflow", "snapshot", selectedSymbol, selectedTimeframe],
    storageKey: `orderflow:snapshot:${selectedSymbol}:history-v2`,
    queryFn: async () => {
      const response = await getOrderflowSnapshot(selectedSymbol, "3,5,15,30", 5);
      return response.data as OrderflowSnapshot;
    },
    refetchInterval: 30_000,
    staleTime: 25_000,
    retry: 1,
  });

  const instruments = query.data?.instruments ?? [];
  const selected = instruments.find((item) => item.symbol === selectedSymbol) ?? instruments[0];
  const timeframe = selected?.timeframes?.[selectedTimeframe];
  const sessions = timeframe?.sessions ?? [];
  const activeSession = useMemo(() => {
    if (!sessions.length) return undefined;
    if (selectedSessionDate === "latest") return sessions[sessions.length - 1];
    return sessions.find((session) => session.session_date === selectedSessionDate) ?? sessions[sessions.length - 1];
  }, [selectedSessionDate, sessions]);
  const chartInstrument = useMemo(() => {
    if (!selected || !activeSession) return selected;
    const sessionWhales = activeSession.whales?.length ? activeSession.whales : selected.whales;
    return {
      ...selected,
      price: activeSession.close || selected.price,
      change: activeSession.close - activeSession.open,
      change_pct: activeSession.open ? ((activeSession.close - activeSession.open) / activeSession.open) * 100 : selected.change_pct,
      footprint: activeSession.footprint,
      whales: sessionWhales,
    };
  }, [activeSession, selected]);
  const latestBar = chartInstrument?.footprint?.[chartInstrument.footprint.length - 1];

  return (
    <div className="min-h-full bg-bg-primary text-text-primary">
      <div className="border-b border-bg-border bg-black/40 px-4 py-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="flex items-center gap-2 font-mono text-[10px] font-semibold uppercase tracking-[0.22em] text-accent-cyan">
              <RadioTower size={14} />
              Live institutional orderflow
            </div>
            <h1 className="mt-2 max-w-4xl font-mono text-3xl font-semibold uppercase tracking-[0.12em] text-text-primary md:text-4xl">
              Orderflow chart
            </h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-text-secondary">
              Broker-backed heatmap, footprint delta, CVD, MP reference levels, and whale pressure markers for NIFTY, BANKNIFTY, SENSEX, and Crude Oil.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="border border-bg-border bg-bg-secondary px-3 py-2 font-mono text-[11px] uppercase tracking-[0.16em] text-text-secondary">
              {query.isFetching ? "stream refresh" : `as of ${formatTime(query.data?.as_of)}`}
            </span>
            <button
              type="button"
              onClick={() => query.refetch()}
              className="inline-flex items-center gap-2 border border-accent-cyan/40 bg-accent-cyan/10 px-3 py-2 font-mono text-xs font-semibold uppercase tracking-[0.12em] text-accent-cyan transition-colors hover:bg-accent-cyan/15"
            >
              <RefreshCw size={14} className={query.isFetching ? "animate-spin" : ""} />
              Refresh
            </button>
          </div>
        </div>
      </div>

      <div className="space-y-3 p-3 xl:p-4">
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-end">
          <InstrumentTabs selected={selectedSymbol} activeInstrument={selected} onSelect={setSelectedSymbol} />
          <TimeframeTabs selected={selectedTimeframe} onSelect={setSelectedTimeframe} />
        </div>
        {sessions.length ? (
          <SessionStrip sessions={sessions} selected={selectedSessionDate} onSelect={setSelectedSessionDate} />
        ) : null}

        {query.isError ? (
          <div className="border border-accent-red/50 bg-accent-red/10 px-4 py-3 text-sm text-accent-red">
            Orderflow snapshot request failed. Showing cached data if available.
          </div>
        ) : null}

        {!selected || !chartInstrument ? (
          <div className="border border-bg-border bg-black px-4 py-12 text-center text-sm text-text-muted">
            Loading broker-backed orderflow snapshot.
          </div>
        ) : selected.error ? (
          <div className="border border-accent-amber/60 bg-accent-amber/10 px-4 py-5 text-sm text-accent-amber">
            {selected.symbol}: {selected.error}
          </div>
        ) : (
          <>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-6">
              <MetricTile
                icon={Activity}
                label="Last"
                value={formatPrice(chartInstrument.price, chartInstrument.symbol === "CRUDEOIL" ? 0 : 2)}
                detail={`${formatSigned(chartInstrument.change, 2)} · ${formatSigned(chartInstrument.change_pct, 2)}%`}
                hot={Math.abs(chartInstrument.change_pct) >= 0.5}
              />
              <MetricTile
                icon={Waves}
                label="CVD"
                value={formatSigned(latestBar?.cumulative_delta ?? selected.metrics.cumulative_delta, 0)}
                detail={`bar ${formatSigned(latestBar?.delta, 0)} · ${latestBar?.label ?? "--"}`}
                hot={Math.abs(selected.metrics.cumulative_delta) > 0}
              />
              <MetricTile
                icon={Gauge}
                label="Imbalance"
                value={formatPct(selected.metrics.order_flow_imbalance || selected.metrics.trade_imbalance, 0)}
                detail={`top ${formatPct(selected.metrics.top_imbalance, 0)} · depth ${formatPct(selected.metrics.depth_imbalance, 0)}`}
                hot={Math.abs(selected.metrics.order_flow_imbalance || selected.metrics.trade_imbalance) >= 0.12}
              />
              <MetricTile
                icon={Crosshair}
                label="VWAP drift"
                value={formatSigned(selected.metrics.vwap_drift, 2)}
                detail={`vwap ${formatPrice(selected.metrics.vwap, selected.symbol === "CRUDEOIL" ? 0 : 2)}`}
                hot={Math.abs(selected.metrics.vwap_drift) > 0}
              />
              <MetricTile
                icon={Zap}
                label="Whales"
                value={String(chartInstrument.whales.length)}
                detail={`${chartInstrument.whales[0]?.label ?? "none"} · score ${chartInstrument.whales[0]?.score ?? "--"}`}
                hot={chartInstrument.whales.length > 0}
              />
              <MetricTile
                icon={
                  selected.data_quality?.execution_ready === false
                    ? AlertTriangle
                    : selected.synthetic_quote
                      ? AlertTriangle
                      : ShieldCheck
                }
                label="Quality"
                value={
                  selected.data_quality?.execution_ready === false
                    ? "DEGRADED"
                    : selected.synthetic_quote
                      ? "PROXY"
                      : "READY"
                }
                detail={
                  selected.synthetic_quote
                    ? `bar-derived · ${formatAge(selected.age_seconds)}`
                    : `${selected.source?.order_flow ?? "--"} · ${formatAge(selected.age_seconds)}`
                }
                hot={selected.data_quality?.execution_ready === false || Boolean(selected.synthetic_quote)}
              />
            </div>

            <OrderflowChart instrument={chartInstrument} timeframe={selectedTimeframe} session={activeSession} />

            {/* Side-by-side market microstructure: DOM ladder + Time and
                Sales + latest footprint bar. Mirrors what an order-flow
                trader looks at simultaneously (Bookmap left, ATAS center,
                Sierra tape right). Falls back gracefully when broker
                doesn't expose L2 / raw prints. */}
            <div className="grid gap-4 xl:grid-cols-[1fr_1fr_1fr]">
              <DomLadder dom={selected.dom} synthetic={Boolean(selected.synthetic_quote)} symbol={selected.symbol} />
              <TapeFeed tape={selected.tape} synthetic={Boolean(selected.synthetic_quote)} symbol={selected.symbol} />
              <LatestFootprint instrument={chartInstrument} />
            </div>

            <div className="grid gap-4 xl:grid-cols-[1.15fr_1fr]">
              <WhaleTape whales={chartInstrument.whales} />
              <DataIntegrityPanel instrument={selected} />
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="border border-bg-border bg-black px-3 py-3">
                <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-amber">Market Profile Anchors</div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <InfoRow label="POC" value={formatPrice(selected.market_profile.poc, selected.symbol === "CRUDEOIL" ? 0 : 0)} />
                  <InfoRow label="VAH" value={formatPrice(selected.market_profile.vah, selected.symbol === "CRUDEOIL" ? 0 : 0)} />
                  <InfoRow label="VAL" value={formatPrice(selected.market_profile.val, selected.symbol === "CRUDEOIL" ? 0 : 0)} />
                  <InfoRow label="IB" value={`${formatPrice(selected.market_profile.initial_balance_low, 0)}-${formatPrice(selected.market_profile.initial_balance_high, 0)}`} />
                </div>
              </div>
              <div className="border border-bg-border bg-black px-3 py-3">
                <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-cyan">Execution Read</div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <InfoRow label="Style" value={selected.metrics.execution_aggression ?? "--"} />
                  <InfoRow label="Timing" value={formatPct(selected.metrics.timing_confidence, 0)} />
                  <InfoRow label="Queue" value={formatSigned(selected.metrics.queue_pressure, 2)} />
                  <InfoRow label="Toxicity" value={formatPct(selected.metrics.toxicity_score, 0)} />
                </div>
              </div>
              <div className="border border-bg-border bg-black px-3 py-3">
                <div className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent-green">Source Contract</div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <InfoRow label="Market" value={selected.market} />
                  <InfoRow label="Proxy" value={selected.instrument_proxy ?? "--"} />
                  <InfoRow label="Lot" value={String(selected.session?.lot_size ?? "--")} />
                  <InfoRow label="Mode" value={selected.session?.mode ?? "--"} />
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
