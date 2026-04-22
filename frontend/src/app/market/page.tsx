"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";
import { clsx } from "clsx";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  Minus,
  Radar,
  RefreshCw,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";

import { StreamStatus } from "@/components/live/StreamStatus";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import {
  getATMWatchlist,
  getATMWatchlistExpiries,
  getBrokerStatus,
  getMarketProfile,
  getOptionChain,
  getOptionExpiries,
  getSectorRotation,
} from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";
import { createMarketWatchlistSocket } from "@/lib/websocket";
import {
  MARKET_INDEX_SYMBOLS,
  type MarketIndexSymbol,
  getMarketIndexLabel,
} from "@/lib/marketSymbols";
import { useStore, useTickSymbol } from "@/store";

type ChainEntry = {
  strike: number;
  option_type: "CE" | "PE";
  ltp: number;
  oi: number;
  volume: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  oi_change?: number | null;
  oi_change_pct?: number | null;
  ltp_change?: number | null;
  ltp_change_pct?: number | null;
};

type OptionChainPayload = {
  symbol: string;
  expiry: string;
  spot_price: number;
  entries: ChainEntry[];
  pcr_oi: number;
  pcr_volume: number;
  pcr_prev_oi?: number | null;
  pcr_oi_change?: number | null;
  max_pain: number;
  atm_strike: number;
  atm_iv: number;
  total_ce_oi: number;
  total_pe_oi: number;
  total_ce_oi_change?: number | null;
  total_pe_oi_change?: number | null;
  total_ce_volume?: number | null;
  total_pe_volume?: number | null;
  atm_call_ltp_change?: number | null;
  atm_call_ltp_change_pct?: number | null;
  atm_put_ltp_change?: number | null;
  atm_put_ltp_change_pct?: number | null;
  atm_call_oi_change?: number | null;
  atm_put_oi_change?: number | null;
  timestamp?: string;
  error?: string;
};

type ExpiryPayload = {
  symbol: string;
  expiries: string[];
  default_expiry?: string | null;
};

type MarketProfilePayload = {
  symbol: string;
  timeframe: "day" | "week" | "month" | "hourly";
  date: string;
  poc: number;
  vah: number;
  val: number;
  ib_high: number;
  ib_low: number;
  source_interval?: string;
  sample_count?: number;
  coverage_start?: string | null;
  coverage_end?: string | null;
  error?: string;
};

type SectorWatchlistRow = {
  code: string;
  name: string;
  symbol: string;
  price: number;
  tracked_change_pct: number;
  relative_strength_pct: number;
  rrg_ratio: number;
  rrg_momentum: number;
  quadrant: string;
  trend: string;
  samples: number;
};

type SectorRotationPayload = {
  timeframe?: string;
  benchmark?: {
    symbol: string;
    name: string;
    price: number;
    tracked_change_pct: number;
    samples: number;
  } | null;
  watchlist: SectorWatchlistRow[];
  rrg: {
    points: Array<
      SectorWatchlistRow & {
        trail: Array<{ ratio: number; momentum: number }>;
      }
    >;
    quadrant_counts: Record<string, number>;
  };
  stocks_by_sector?: Record<
    string,
    {
      sector: SectorWatchlistRow;
      stocks: SectorWatchlistRow[];
      rrg: {
        points: Array<SectorWatchlistRow & { trail: Array<{ ratio: number; momentum: number }> }>;
        quadrant_counts: Record<string, number>;
      };
    }
  >;
  unassigned_symbols?: string[];
  source?: string;
  detail?: string | null;
  timestamp?: string;
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
};

type ATMWatchlistPayload = {
  expiry: string | null;
  build_status?: "building" | "ready" | string;
  rows: Array<{
    underlying: string;
    kind: string;
    spot_price: number;
    expiry: string;
    atm_strike: number;
    live_source: string;
    lot_size?: number | null;
    fyers_symbol?: string | null;
    ce?: ATMWatchlistOptionSide | null;
    pe?: ATMWatchlistOptionSide | null;
  }>;
  summary: {
    total_rows: number;
    ce_ready: number;
    pe_ready: number;
    fyers_rows?: number;
    upstox_rows?: number;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
};

type ATMWatchlistExpiryPayload = {
  expiries: string[];
  default_expiry?: string | null;
  monthly_expiry?: string | null;
  expiry_scope_note?: string | null;
  source?: string;
  detail?: string | null;
};

type MarketWatchlistSnapshot = {
  expiry_catalog: ATMWatchlistExpiryPayload;
  watchlist: ATMWatchlistPayload;
};

type MarketWorkspace = "options" | "sectors" | "watchlist";

function formatChangePct(ltp?: number, close?: number) {
  if (!ltp || !close) return "--";
  const pct = ((ltp - close) / close) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

function formatSigned(value?: number | null, decimals = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(decimals)}${suffix}`;
}

function formatSignedPct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(2)}%`;
}

function formatIv(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const normalized = value > 5 ? value : value * 100;
  return `${normalized.toFixed(1)}%`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

function formatIndicator(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function valueTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function macdTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  return value >= 0 ? "text-accent-green" : "text-accent-red";
}

function rsiTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value >= 70) return "text-accent-red";
  if (value <= 30) return "text-accent-green";
  return "text-text-secondary";
}

function directionMeta(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return {
      badge: "bg-bg-primary text-text-muted",
      icon: <Minus size={12} />,
      label: "Flat",
      tone: "text-text-muted",
    };
  }
  if (value > 0) {
    return {
      badge: "bg-accent-green/12 text-accent-green border-accent-green/20",
      icon: <ArrowUpRight size={12} />,
      label: "Up",
      tone: "text-accent-green",
    };
  }
  if (value < 0) {
    return {
      badge: "bg-accent-red/12 text-accent-red border-accent-red/20",
      icon: <ArrowDownRight size={12} />,
      label: "Down",
      tone: "text-accent-red",
    };
  }
  return {
    badge: "bg-bg-primary text-text-secondary border-bg-border",
    icon: <Minus size={12} />,
    label: "Flat",
    tone: "text-text-secondary",
  };
}

function quadrantTone(quadrant?: string) {
  switch (quadrant) {
    case "leading":
      return "border-accent-green/30 bg-accent-green/10 text-accent-green";
    case "improving":
      return "border-accent-blue/30 bg-accent-blue/10 text-accent-blue";
    case "weakening":
      return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
    default:
      return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
}

function sectorAcronym(name: string) {
  const cleaned = name.replace(/[^A-Za-z0-9 ]/g, " ").trim();
  if (!cleaned) return "--";
  const words = cleaned.split(/\s+/).filter(Boolean);
  if (words.length === 1) {
    return words[0].slice(0, Math.min(4, words[0].length)).toUpperCase();
  }
  return words.map((word) => word[0]).join("").slice(0, 4).toUpperCase();
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function positionPct(value: number, min: number, max: number) {
  if (!Number.isFinite(value) || max <= min) return 50;
  return clamp(((value - min) / (max - min)) * 100, 6, 94);
}

function SignedPill({ value }: { value?: number | null }) {
  const direction = directionMeta(value);
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full px-2 py-1 text-[11px] font-semibold",
        direction.badge,
      )}
    >
      {direction.icon}
      {formatSignedPct(value)}
    </span>
  );
}

function MarketTabButton({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "relative flex min-w-[180px] items-center justify-between rounded-t-[18px] rounded-b-xl border px-4 py-2.5 text-left transition-all",
        active
          ? "border-accent-blue/45 bg-[#111a2c] text-text-primary shadow-[0_10px_24px_rgba(6,12,24,0.24)]"
          : "border-bg-border bg-bg-secondary/35 text-text-muted hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.18em]">{label}</div>
      <div
        className={clsx(
          "h-2 w-2 rounded-full border transition-colors",
          active ? "border-accent-blue bg-accent-blue" : "border-bg-border bg-bg-primary/30",
        )}
      />
    </button>
  );
}

function PulseRow({
  label,
  value,
  delta,
  tone = "text-text-primary",
}: {
  label: string;
  value: string;
  delta?: string;
  tone?: string;
}) {
  return (
    <div className="border-b border-bg-border/50 py-2 text-sm last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <span className="text-text-muted">{label}</span>
        <span className={clsx("font-mono font-semibold", tone)}>{value}</span>
      </div>
      {delta && <div className="mt-1 text-right text-[11px] text-text-muted">{delta}</div>}
    </div>
  );
}

function PulseIndicatorCard({
  label,
  value,
  detail,
  tone = "text-text-primary",
  directionValue,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
  directionValue?: number | null;
}) {
  const direction = directionMeta(directionValue);
  const hasDirection = directionValue != null && !Number.isNaN(directionValue);

  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/45 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
        {hasDirection && (
          <div
            className={clsx(
              "inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]",
              direction.badge,
            )}
          >
            {direction.icon}
            {direction.label}
          </div>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2">
        {hasDirection && <span className={direction.tone}>{direction.icon}</span>}
        <span className={clsx("font-mono text-lg font-semibold", tone)}>{value}</span>
      </div>
      {detail && (
        <div className={clsx("mt-2 text-xs font-medium", hasDirection ? direction.tone : "text-text-muted")}>
          {detail}
        </div>
      )}
    </div>
  );
}

function SectorDetailCard({
  sector,
  benchmark,
}: {
  sector?: SectorWatchlistRow | null;
  benchmark?: SectorRotationPayload["benchmark"];
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/40 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Selected Sector</div>
          <div className="mt-2 text-xl font-semibold text-text-primary">{sector?.name || "Waiting"}</div>
          <div className="mt-1 text-sm text-text-muted">
            {benchmark?.name || "NIFTY 50"} benchmark {benchmark?.tracked_change_pct != null ? formatSignedPct(benchmark.tracked_change_pct) : "--"}
          </div>
        </div>
        {sector?.quadrant && (
          <span
            className={clsx(
              "inline-flex rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.12em]",
              quadrantTone(sector.quadrant),
            )}
          >
            {sector.quadrant}
          </span>
        )}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div className="rounded-xl border border-bg-border bg-bg-primary/50 p-3">
          <div className="text-[11px] uppercase tracking-[0.12em] text-text-muted">Tracked</div>
          <div className={clsx("mt-1 font-mono text-lg font-semibold", valueTone(sector?.tracked_change_pct))}>
            {formatSignedPct(sector?.tracked_change_pct)}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/50 p-3">
          <div className="text-[11px] uppercase tracking-[0.12em] text-text-muted">RS vs NIFTY</div>
          <div className={clsx("mt-1 font-mono text-lg font-semibold", valueTone(sector?.relative_strength_pct))}>
            {formatSignedPct(sector?.relative_strength_pct)}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/50 p-3">
          <div className="text-[11px] uppercase tracking-[0.12em] text-text-muted">RRG Ratio</div>
          <div className="mt-1 font-mono text-lg font-semibold text-accent-blue">
            {sector?.rrg_ratio?.toFixed(2) || "--"}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/50 p-3">
          <div className="text-[11px] uppercase tracking-[0.12em] text-text-muted">Momentum</div>
          <div className={clsx("mt-1 font-mono text-lg font-semibold", sector && sector.rrg_momentum >= 100 ? "text-accent-green" : "text-accent-red")}>
            {sector?.rrg_momentum?.toFixed(2) || "--"}
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <SignedPill value={sector?.tracked_change_pct} />
        <SignedPill value={sector?.relative_strength_pct} />
      </div>
    </div>
  );
}

function SectorCluster({
  title,
  sectors,
  selectedCode,
  onSelect,
}: {
  title: string;
  sectors: SectorWatchlistRow[];
  selectedCode: string;
  onSelect: (code: string) => void;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">{title}</div>
        <div className="text-[11px] text-text-muted">{sectors.length}</div>
      </div>
      <div className="mt-3 space-y-2">
        {sectors.length ? (
          sectors.map((sector) => (
            <button
              key={sector.code}
              onClick={() => onSelect(sector.code)}
              className={clsx(
                "flex w-full items-center justify-between gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors",
                selectedCode === sector.code
                  ? "border-accent-blue bg-accent-blue/10"
                  : "border-bg-border bg-bg-primary/45 hover:border-bg-active",
              )}
            >
              <div className="min-w-0">
                <div className="font-medium text-text-primary">{sector.name}</div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-text-muted">
                  <span className={clsx("font-mono", valueTone(sector.tracked_change_pct))}>
                    {formatSignedPct(sector.tracked_change_pct)}
                  </span>
                  <span>RS {formatSignedPct(sector.relative_strength_pct)}</span>
                  <span
                    className={clsx(
                      "inline-flex rounded-full border px-1.5 py-0.5 uppercase tracking-[0.12em]",
                      quadrantTone(sector.quadrant),
                    )}
                  >
                    {sector.trend}
                  </span>
                </div>
              </div>
              <div className="text-right">
                <div className="font-mono text-sm font-semibold text-accent-blue">
                  {sector.rrg_ratio.toFixed(2)}
                </div>
                <div className={clsx("mt-1 font-mono text-[11px]", sector.rrg_momentum >= 100 ? "text-accent-green" : "text-accent-red")}>
                  {sector.rrg_momentum.toFixed(1)}
                </div>
              </div>
            </button>
          ))
        ) : (
          <div className="rounded-xl border border-dashed border-bg-border px-3 py-4 text-sm text-text-muted">
            No sectors in this quadrant yet.
          </div>
        )}
      </div>
    </div>
  );
}

function SectorQuadrantBoard({
  points,
  selectedCode,
  onSelect,
}: {
  points: Array<SectorWatchlistRow & { trail: Array<{ ratio: number; momentum: number }> }>;
  selectedCode: string;
  onSelect: (code: string) => void;
}) {
  const xValues = points.map((point) => point.rrg_ratio);
  const yValues = points.map((point) => point.rrg_momentum);
  const xMin = Math.min(95, ...(xValues.length ? xValues : [100])) - 1;
  const xMax = Math.max(105, ...(xValues.length ? xValues : [100])) + 1;
  const yMin = Math.min(95, ...(yValues.length ? yValues : [100])) - 1;
  const yMax = Math.max(105, ...(yValues.length ? yValues : [100])) + 1;
  const selectedPoint = points.find((point) => point.code === selectedCode) ?? points[0];
  const selectedTrail = selectedPoint
    ? [...selectedPoint.trail, { ratio: selectedPoint.rrg_ratio, momentum: selectedPoint.rrg_momentum }]
    : [];
  const polylinePoints = selectedTrail
    .map((point) => {
      const x = positionPct(point.ratio, xMin, xMax);
      const y = 100 - positionPct(point.momentum, yMin, yMax);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <div className="rounded-[28px] border border-bg-border bg-[#09111c] p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Sector Rotation Map</div>
        </div>
        <div className="text-right text-[11px] text-text-muted">
          <div>Ratio {xMin.toFixed(0)}-{xMax.toFixed(0)}</div>
          <div>Momentum {yMin.toFixed(0)}-{yMax.toFixed(0)}</div>
        </div>
      </div>

      <div className="relative aspect-[1.2/1] min-h-[380px] overflow-hidden rounded-[24px] border border-bg-border bg-[#0c1522]">
        <div className="absolute inset-0 grid grid-cols-2 grid-rows-2">
          <div className="border-b border-r border-bg-border/60 bg-accent-red/4 p-4 text-[11px] uppercase tracking-[0.16em] text-text-muted">Lagging</div>
          <div className="border-b border-bg-border/60 bg-accent-blue/4 p-4 text-right text-[11px] uppercase tracking-[0.16em] text-text-muted">Improving</div>
          <div className="border-r border-bg-border/60 bg-accent-amber/4 p-4 text-[11px] uppercase tracking-[0.16em] text-text-muted">Weakening</div>
          <div className="bg-accent-green/4 p-4 text-right text-[11px] uppercase tracking-[0.16em] text-text-muted">Leading</div>
        </div>

        <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
          <line x1="50" y1="0" x2="50" y2="100" stroke="#223049" strokeWidth="0.35" />
          <line x1="0" y1="50" x2="100" y2="50" stroke="#223049" strokeWidth="0.35" />
          {selectedTrail.length > 1 && (
            <polyline
              fill="none"
              stroke="#7dd3fc"
              strokeWidth="0.8"
              strokeOpacity="0.8"
              strokeLinejoin="round"
              strokeLinecap="round"
              points={polylinePoints}
            />
          )}
        </svg>

        {points.map((point) => {
          const left = positionPct(point.rrg_ratio, xMin, xMax);
          const top = 100 - positionPct(point.rrg_momentum, yMin, yMax);
          const selected = point.code === selectedCode;
          return (
            <button
              key={point.code}
              onClick={() => onSelect(point.code)}
              title={`${point.name} · ${point.quadrant}`}
              className={clsx(
                "absolute -translate-x-1/2 -translate-y-1/2 rounded-full border px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] shadow-[0_8px_24px_rgba(2,6,23,0.35)] transition-transform hover:scale-105",
                quadrantTone(point.quadrant),
                selected && "ring-2 ring-white/40",
              )}
              style={{ left: `${left}%`, top: `${top}%` }}
            >
              {sectorAcronym(point.name)}
            </button>
          );
        })}

        <div className="absolute bottom-3 left-3 right-3 flex items-center justify-between text-[11px] text-text-muted">
          <span>Underperforming</span>
          <span>Outperforming</span>
        </div>
      </div>
    </div>
  );
}

function SectorSpotlightDialog({
  open,
  onClose,
  timeframe,
  benchmark,
  sectorPoints,
  selectedSectorCode,
  onSelectSector,
  selectedSector,
  stockPoints,
  selectedStockCode,
  onSelectStock,
  stockLeading,
  stockImproving,
  stockWeakening,
  stockLagging,
}: {
  open: boolean;
  onClose: () => void;
  timeframe: string;
  benchmark?: SectorRotationPayload["benchmark"];
  sectorPoints: Array<SectorWatchlistRow & { trail: Array<{ ratio: number; momentum: number }> }>;
  selectedSectorCode: string;
  onSelectSector: (code: string) => void;
  selectedSector?: SectorWatchlistRow | null;
  stockPoints: Array<SectorWatchlistRow & { trail: Array<{ ratio: number; momentum: number }> }>;
  selectedStockCode: string;
  onSelectStock: (code: string) => void;
  stockLeading: SectorWatchlistRow[];
  stockImproving: SectorWatchlistRow[];
  stockWeakening: SectorWatchlistRow[];
  stockLagging: SectorWatchlistRow[];
}) {
  if (!open || !selectedSector) return null;

  return (
    <div className="fixed inset-0 z-40 flex items-stretch justify-center bg-[#040912]/84 backdrop-blur-sm">
      <div className="flex h-full w-full max-w-[1680px] flex-col overflow-hidden border-x border-bg-border bg-bg-primary">
        <div className="flex items-center justify-between gap-4 border-b border-bg-border px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">
              Sector Spotlight · {timeframe}
            </div>
            <div className="mt-1 flex items-center gap-3">
              <h2 className="truncate font-mono text-2xl font-semibold text-text-primary">{selectedSector.name}</h2>
              <SignedPill value={selectedSector.tracked_change_pct} />
              <SignedPill value={selectedSector.relative_strength_pct} />
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-bg-border bg-bg-secondary/35 p-2 text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
            aria-label="Close sector spotlight"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-5">
          <div className="grid gap-4 xl:grid-cols-[1.05fr,0.95fr]">
            <div className="space-y-4">
              <SectorDetailCard sector={selectedSector} benchmark={benchmark} />
              <SectorQuadrantBoard
                points={sectorPoints}
                selectedCode={selectedSectorCode}
                onSelect={onSelectSector}
              />
            </div>

            <div className="space-y-4">
              <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Stocks vs Sector</div>
                  <div className="text-[11px] text-text-muted">{stockPoints.length} mapped F&O names</div>
                </div>
                {stockPoints.length ? (
                  <div className="mt-3">
                    <SectorQuadrantBoard
                      points={stockPoints}
                      selectedCode={selectedStockCode}
                      onSelect={onSelectStock}
                    />
                  </div>
                ) : (
                  <div className="mt-3 rounded-xl border border-dashed border-bg-border px-4 py-6 text-sm text-text-muted">
                    No mapped stock-level RRG is available for this sector yet.
                  </div>
                )}
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <SectorCluster title="Leading Stocks" sectors={stockLeading} selectedCode={selectedStockCode} onSelect={onSelectStock} />
                <SectorCluster title="Improving Stocks" sectors={stockImproving} selectedCode={selectedStockCode} onSelect={onSelectStock} />
                <SectorCluster title="Weakening Stocks" sectors={stockWeakening} selectedCode={selectedStockCode} onSelect={onSelectStock} />
                <SectorCluster title="Lagging Stocks" sectors={stockLagging} selectedCode={selectedStockCode} onSelect={onSelectStock} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
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

  if (!option) {
    return (
      <div className="grid grid-cols-5 gap-2 text-[11px] text-text-muted">
        <span className="col-span-5">No ATM contract</span>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-5 gap-x-3 gap-y-1 text-[11px]">
      <div>
        <div className="text-text-muted">LTP</div>
        <div className={clsx("font-mono font-semibold", ltpTone)}>{option.ltp.toFixed(2)}</div>
      </div>
      <div>
        <div className="text-text-muted">Chg</div>
        <div className={valueTone(option.change_pct)}>{formatSignedPct(option.change_pct)}</div>
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
        <div className={macdTone(option.macd_histogram)}>{formatIndicator(option.macd_histogram, 3)}</div>
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
    </div>
  );
}

const BROKER_LABEL: Record<string, string> = {
  fyers: "Fyers",
  upstox: "Upstox",
  icici_breeze: "ICICI Breeze",
  fivepaisa: "5paisa",
};

const TRADING_BROKERS = ["fyers", "upstox"];

function MarketBrokerState() {
  const layoutBrokerStatuses = useStore((state) => state.brokerStatuses);
  const statusQuery = useQuery<BrokerStatusEntry[]>({
    queryKey: ["brokerHealthBanner"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    refetchInterval: 30_000,
    staleTime: 20_000,
  });

  const entries = statusQuery.data?.length ? statusQuery.data : layoutBrokerStatuses;
  const trading = entries.filter((e) => TRADING_BROKERS.includes(e.broker));
  const disconnected = trading.filter((e) => !isBrokerReady(e));
  const connected = trading.filter((e) => isBrokerReady(e));
  const isResolving = !entries.length && (statusQuery.isLoading || statusQuery.isFetching);

  if (isResolving) {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-accent-amber/20 bg-accent-amber/8 px-3 py-2 text-xs text-accent-amber">
        <WifiOff size={13} className="shrink-0" />
        <span className="font-medium">Checking broker sessions</span>
      </div>
    );
  }

  if (disconnected.length === 0 && connected.length > 0) {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-accent-green/20 bg-accent-green/8 px-3 py-2 text-xs text-accent-green">
        <CheckCircle2 size={13} className="shrink-0" />
        <span className="font-medium">Brokers</span>
        <span className="text-accent-green/80">
          {connected.map((e) => BROKER_LABEL[e.broker] ?? e.broker).join(" · ")}
        </span>
      </div>
    );
  }

  if (disconnected.length > 0) {
    const allDisconnected = connected.length === 0;
    const disconnectedNames = disconnected.map((e) => BROKER_LABEL[e.broker] ?? e.broker).join(", ");
    const connectedNames = connected.map((e) => BROKER_LABEL[e.broker] ?? e.broker).join(", ");
    return (
      <a
        href="/settings"
        className={clsx(
          "inline-flex items-center gap-2 rounded-full border px-3 py-2 text-xs transition-colors hover:opacity-90",
          allDisconnected
            ? "border-accent-red/25 bg-accent-red/8 text-accent-red"
            : "border-accent-amber/25 bg-accent-amber/8 text-accent-amber",
        )}
      >
        <WifiOff size={13} className="shrink-0" />
        <span className="font-semibold">{allDisconnected ? disconnectedNames : `${disconnectedNames} expired`}</span>
        {connected.length > 0 ? <span className="text-accent-green/80">Live via {connectedNames}</span> : null}
      </a>
    );
  }

  return null;
}

export default function MarketPage() {
  const [symbol, setSymbol] = useState<MarketIndexSymbol>("NSE:NIFTY50-INDEX");
  const [expiry, setExpiry] = useState("");
  const [profileTimeframe, setProfileTimeframe] = useState<"day" | "week" | "month">("day");
  const [workspace, setWorkspace] = useState<MarketWorkspace>("options");
  const [sectorTimeframe, setSectorTimeframe] = useState<"hourly" | "daily" | "weekly" | "monthly">("daily");
  const [selectedSectorCode, setSelectedSectorCode] = useState("");
  const [selectedStockCode, setSelectedStockCode] = useState("");
  const [watchlistExpiry, setWatchlistExpiry] = useState("");
  const [sectorDialogOpen, setSectorDialogOpen] = useState(false);
  const selectedTick = useTickSymbol(symbol);

  const expiriesQuery = usePersistentSnapshotQuery<ExpiryPayload>({
    storageKey: `market:expiries:${symbol}`,
    queryKey: ["optionExpiries", symbol],
    queryFn: () => getOptionExpiries(symbol).then((response) => response.data),
    enabled: workspace === "options",
    staleTime: 60000,
    refetchInterval: 60000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    const available = expiriesQuery.data?.expiries ?? [];
    const defaultExpiry = expiriesQuery.data?.default_expiry || available[0] || "";
    if (!defaultExpiry) return;
    if (!expiry || !available.includes(expiry)) {
      setExpiry(defaultExpiry);
    }
  }, [expiry, expiriesQuery.data]);

  const chainQuery = usePersistentSnapshotQuery<OptionChainPayload>({
    storageKey: `market:option-chain:${symbol}:${expiry || "auto"}`,
    queryKey: ["optionChain", symbol, expiry || "auto"],
    queryFn: () => getOptionChain(symbol, expiry || undefined).then((response) => response.data),
    enabled: workspace === "options",
    refetchInterval: 15000,
    staleTime: 5000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    const resolvedExpiry = chainQuery.data?.expiry;
    if (!resolvedExpiry) return;
    setExpiry((current) => current || resolvedExpiry);
  }, [chainQuery.data?.expiry]);

  const profileQuery = usePersistentSnapshotQuery<MarketProfilePayload>({
    storageKey: `market:profile:${symbol}:${profileTimeframe}`,
    queryKey: ["marketProfile", symbol, profileTimeframe],
    queryFn: () => getMarketProfile(symbol, profileTimeframe).then((response) => response.data),
    enabled: workspace === "options",
    refetchInterval: 30000,
    staleTime: 5000,
    refetchOnWindowFocus: false,
  });

  const sectorQuery = useQuery<SectorRotationPayload>({
    queryKey: ["marketSectorRotation", sectorTimeframe],
    queryFn: () => getSectorRotation(sectorTimeframe).then((response) => response.data),
    enabled: workspace === "sectors",
    refetchInterval: 60000,
    staleTime: 5000,
  });

  const watchlistLiveQuery = useLiveSnapshotQuery<MarketWatchlistSnapshot>({
    queryKey: ["marketWatchlistSnapshot", watchlistExpiry || "default"],
    queryFn: async () => {
      const expiry_catalog = await getATMWatchlistExpiries(watchlistExpiry || undefined).then(
        (response) => response.data as ATMWatchlistExpiryPayload,
      );
      const effectiveExpiry = watchlistExpiry || expiry_catalog.default_expiry || expiry_catalog.expiries[0] || undefined;
      const watchlist = await getATMWatchlist(effectiveExpiry).then((response) => response.data as ATMWatchlistPayload);
      return {
        expiry_catalog,
        watchlist,
      };
    },
    enabled: workspace === "watchlist",
    streamFactory: (onData, onStatusChange) =>
      createMarketWatchlistSocket(
        watchlistExpiry || "",
        (data) => onData(data as MarketWatchlistSnapshot),
        onStatusChange,
      ),
    storageKey: `marketWatchlistSnapshot:${watchlistExpiry || "default"}`,
    staleTime: 10_000,
    retry: 2,
    retryDelay: 1500,
  });

  const watchlistExpiryData = watchlistLiveQuery.data?.expiry_catalog;
  const watchlistData = watchlistLiveQuery.data?.watchlist;

  useEffect(() => {
    const available = watchlistExpiryData?.expiries ?? [];
    const defaultExpiry = watchlistExpiryData?.default_expiry || available[0] || "";
    if (!defaultExpiry) return;
    if (!watchlistExpiry || !available.includes(watchlistExpiry)) {
      setWatchlistExpiry(defaultExpiry);
    }
  }, [watchlistExpiry, watchlistExpiryData]);

  useEffect(() => {
    const available = sectorQuery.data?.watchlist ?? [];
    if (!available.length) {
      setSelectedSectorCode("");
      return;
    }
    if (!selectedSectorCode || !available.some((sector) => sector.code === selectedSectorCode)) {
      setSelectedSectorCode(available[0].code);
    }
  }, [sectorQuery.data, selectedSectorCode]);

  const selectedSectorStocks = sectorQuery.data?.stocks_by_sector?.[selectedSectorCode]?.rrg?.points ?? [];

  useEffect(() => {
    if (!selectedSectorStocks.length) {
      setSelectedStockCode("");
      return;
    }
    if (!selectedStockCode || !selectedSectorStocks.some((stock) => stock.code === selectedStockCode)) {
      setSelectedStockCode(selectedSectorStocks[0].code);
    }
  }, [selectedSectorStocks, selectedStockCode]);

  const chain = chainQuery.data;
  const profile = profileQuery.data;
  const sectorRotation = sectorQuery.data;
  const entries = chain?.entries ?? [];
  const ceEntries = entries.filter((entry) => entry.option_type === "CE");
  const peEntries = entries.filter((entry) => entry.option_type === "PE");
  const strikes = Array.from(new Set(entries.map((entry) => entry.strike))).sort((a, b) => a - b);
  const atmIndex = Math.max(0, strikes.findIndex((strike) => strike === chain?.atm_strike));
  const visibleStrikes = strikes.length > 0
    ? strikes.slice(Math.max(0, atmIndex - 8), Math.min(strikes.length, atmIndex + 9))
    : [];
  const liveSpot = selectedTick?.ltp || chain?.spot_price || 0;
  const spotPositive = selectedTick && selectedTick.close > 0 ? selectedTick.ltp >= selectedTick.close : undefined;

  const sectorPoints = sectorRotation?.rrg?.points ?? [];
  const sectorWatchlist = sectorRotation?.watchlist ?? [];
  const selectedSector = sectorPoints.find((sector) => sector.code === selectedSectorCode)
    ?? sectorWatchlist.find((sector) => sector.code === selectedSectorCode)
    ?? sectorWatchlist[0]
    ?? null;
  const leadingSectors = sectorWatchlist.filter((sector) => sector.quadrant === "leading");
  const improvingSectors = sectorWatchlist.filter((sector) => sector.quadrant === "improving");
  const weakeningSectors = sectorWatchlist.filter((sector) => sector.quadrant === "weakening");
  const laggingSectors = sectorWatchlist.filter((sector) => sector.quadrant === "lagging");
  const selectedSectorStockRows = sectorRotation?.stocks_by_sector?.[selectedSectorCode]?.stocks ?? [];
  const stockLeading = selectedSectorStockRows.filter((stock) => stock.quadrant === "leading");
  const stockImproving = selectedSectorStockRows.filter((stock) => stock.quadrant === "improving");
  const stockWeakening = selectedSectorStockRows.filter((stock) => stock.quadrant === "weakening");
  const stockLagging = selectedSectorStockRows.filter((stock) => stock.quadrant === "lagging");
  const isWatchlistBuilding = watchlistData?.build_status === "building";
  const showWatchlistLoading = watchlistLiveQuery.isLoading || (isWatchlistBuilding && !(watchlistData?.rows?.length));
  const selectSectorAndOpen = (code: string) => {
    setSelectedSectorCode(code);
    setSectorDialogOpen(true);
  };

  return (
    <div className="mx-auto max-w-[1800px] space-y-4 pb-8">
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h1 className="font-mono text-2xl font-semibold text-text-primary">Market Intelligence</h1>
          <MarketBrokerState />
        </div>

        <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
          <MarketTabButton active={workspace === "options"} label="Options Desk" onClick={() => setWorkspace("options")} />
          <MarketTabButton active={workspace === "sectors"} label="Sector Rotation" onClick={() => setWorkspace("sectors")} />
          <MarketTabButton active={workspace === "watchlist"} label="ATM Watchlist" onClick={() => setWorkspace("watchlist")} />
        </div>
      </section>

      {workspace === "options" ? (
        <div className="grid gap-4 xl:grid-cols-[1.6fr_0.82fr]">
          <section className="card rounded-[24px] p-4">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-bg-border/70 pb-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">Option Chain</div>
                <select
                  value={symbol}
                  onChange={(event) => setSymbol(event.target.value as MarketIndexSymbol)}
                  className="terminal-input min-w-[176px] py-1.5 text-xs"
                >
                  {MARKET_INDEX_SYMBOLS.map((item) => (
                    <option key={item} value={item}>
                      {getMarketIndexLabel(item)}
                    </option>
                  ))}
                </select>
                <select
                  value={expiry}
                  onChange={(event) => setExpiry(event.target.value)}
                  className="terminal-input min-w-[164px] py-1.5 text-xs"
                >
                  {(expiriesQuery.data?.expiries ?? []).map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                  {!expiriesQuery.data?.expiries?.length && <option value="">Expiry loading...</option>}
                </select>
              </div>

              <div className="flex items-center gap-2">
                <SignedPill value={selectedTick && selectedTick.close > 0 ? ((selectedTick.ltp - selectedTick.close) / selectedTick.close) * 100 : null} />
                <button
                  onClick={() => {
                    void expiriesQuery.refetch();
                    void chainQuery.refetch();
                    void profileQuery.refetch();
                  }}
                  className="rounded-lg border border-bg-border bg-bg-secondary/45 p-2 text-text-muted transition-colors hover:text-text-primary"
                  aria-label="Refresh market data"
                >
                  <RefreshCw size={14} />
                </button>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap gap-2">
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                Spot <span className="ml-2 font-mono font-semibold text-text-primary">{liveSpot > 0 ? liveSpot.toFixed(2) : "--"}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                Expiry <span className="ml-2 font-mono font-semibold text-text-primary">{chain?.expiry || "--"}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                ATM <span className="ml-2 font-mono font-semibold text-accent-amber">{chain?.atm_strike || "--"}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                High <span className="ml-2 font-mono font-semibold text-text-primary">{selectedTick?.high != null ? selectedTick.high.toFixed(2) : "--"}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                Low <span className="ml-2 font-mono font-semibold text-text-primary">{selectedTick?.low != null ? selectedTick.low.toFixed(2) : "--"}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                Max Pain <span className="ml-2 font-mono font-semibold text-text-primary">{chain?.max_pain || "--"}</span>
              </div>
            </div>

            <div className="mt-4 overflow-hidden rounded-[22px] border border-bg-border bg-[#08101b]">
              <div className="max-h-[68vh] overflow-auto">
                <table className="w-full min-w-[1580px] text-xs font-mono">
                  <thead className="sticky top-0 z-10 bg-[#0d1625]">
                    <tr className="border-b border-bg-border text-text-muted">
                      <th className="px-2 py-3 text-right">CE OI</th>
                      <th className="px-2 py-3 text-right">CE Chg OI</th>
                      <th className="px-2 py-3 text-right">CE Vol</th>
                      <th className="px-2 py-3 text-right">CE IV</th>
                      <th className="px-2 py-3 text-right">CE D</th>
                      <th className="px-2 py-3 text-right">CE G</th>
                      <th className="px-2 py-3 text-right">CE T</th>
                      <th className="px-2 py-3 text-right">CE V</th>
                      <th className="px-2 py-3 text-right">CE LTP</th>
                      <th className="px-2 py-3 text-right">CE Chg%</th>
                      <th className="px-3 py-3 text-center text-accent-amber">STRIKE</th>
                      <th className="px-2 py-3 text-left">PE LTP</th>
                      <th className="px-2 py-3 text-left">PE Chg%</th>
                      <th className="px-2 py-3 text-left">PE IV</th>
                      <th className="px-2 py-3 text-left">PE D</th>
                      <th className="px-2 py-3 text-left">PE G</th>
                      <th className="px-2 py-3 text-left">PE T</th>
                      <th className="px-2 py-3 text-left">PE V</th>
                      <th className="px-2 py-3 text-left">PE Vol</th>
                      <th className="px-2 py-3 text-left">PE Chg OI</th>
                      <th className="px-2 py-3 text-left">PE OI</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleStrikes.map((strike) => {
                      const ce = ceEntries.find((entry) => entry.strike === strike);
                      const pe = peEntries.find((entry) => entry.strike === strike);
                      const isAtm = chain?.atm_strike === strike;
                      return (
                        <tr
                          key={strike}
                          className={clsx(
                            "border-b border-bg-border/30 transition-colors hover:bg-bg-secondary/20",
                            isAtm && "bg-accent-amber/8",
                          )}
                        >
                          <td className="px-2 py-2.5 text-right text-accent-green">{formatCompact(ce?.oi)}</td>
                          <td className={clsx("px-2 py-2.5 text-right", valueTone(ce?.oi_change))}>{formatCompact(ce?.oi_change)}</td>
                          <td className="px-2 py-2.5 text-right text-text-secondary">{formatCompact(ce?.volume)}</td>
                          <td className="px-2 py-2.5 text-right text-text-secondary">{formatIv(ce?.iv)}</td>
                          <td className="px-2 py-2.5 text-right text-text-primary">{formatSigned(ce?.delta, 3)}</td>
                          <td className="px-2 py-2.5 text-right text-text-primary">{formatSigned(ce?.gamma, 4)}</td>
                          <td className={clsx("px-2 py-2.5 text-right", valueTone(ce?.theta))}>{formatSigned(ce?.theta, 2)}</td>
                          <td className="px-2 py-2.5 text-right text-text-primary">{formatSigned(ce?.vega, 2)}</td>
                          <td className="px-2 py-2.5 text-right font-semibold text-accent-green">{ce?.ltp?.toFixed(2) || "--"}</td>
                          <td className={clsx("px-2 py-2.5 text-right", valueTone(ce?.ltp_change_pct))}>{formatSigned(ce?.ltp_change_pct, 2, "%")}</td>
                          <td className={clsx("px-3 py-2.5 text-center font-semibold", isAtm ? "text-accent-amber" : "text-text-primary")}>
                            {strike}
                          </td>
                          <td className="px-2 py-2.5 text-left font-semibold text-accent-red">{pe?.ltp?.toFixed(2) || "--"}</td>
                          <td className={clsx("px-2 py-2.5 text-left", valueTone(pe?.ltp_change_pct))}>{formatSigned(pe?.ltp_change_pct, 2, "%")}</td>
                          <td className="px-2 py-2.5 text-left text-text-secondary">{formatIv(pe?.iv)}</td>
                          <td className="px-2 py-2.5 text-left text-text-primary">{formatSigned(pe?.delta, 3)}</td>
                          <td className="px-2 py-2.5 text-left text-text-primary">{formatSigned(pe?.gamma, 4)}</td>
                          <td className={clsx("px-2 py-2.5 text-left", valueTone(pe?.theta))}>{formatSigned(pe?.theta, 2)}</td>
                          <td className="px-2 py-2.5 text-left text-text-primary">{formatSigned(pe?.vega, 2)}</td>
                          <td className="px-2 py-2.5 text-left text-text-secondary">{formatCompact(pe?.volume)}</td>
                          <td className={clsx("px-2 py-2.5 text-left", valueTone(pe?.oi_change))}>{formatCompact(pe?.oi_change)}</td>
                          <td className="px-2 py-2.5 text-left text-accent-red">{formatCompact(pe?.oi)}</td>
                        </tr>
                      );
                    })}
                    {!visibleStrikes.length && (
                      <tr>
                        <td colSpan={21} className="px-3 py-10 text-center text-sm text-text-muted">
                          {chain?.error || "No live option chain data available for the selected index."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </section>

          <div className="space-y-4">
            <section className="card rounded-[24px] p-4">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-text-muted">
                <Radar size={14} className="text-accent-blue" />
                Chain Pulse
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                <PulseIndicatorCard
                  label="PCR OI"
                  value={chain?.pcr_oi?.toFixed(2) || "--"}
                  detail={chain?.pcr_oi_change != null ? `vs prev ${formatSigned(chain.pcr_oi_change, 2)}` : undefined}
                  tone="text-accent-amber"
                  directionValue={chain?.pcr_oi_change}
                />
                <PulseIndicatorCard label="PCR Volume" value={chain?.pcr_volume?.toFixed(2) || "--"} tone="text-accent-blue" />
                <PulseIndicatorCard label="ATM IV" value={formatIv(chain?.atm_iv)} tone="text-accent-green" />
                <PulseIndicatorCard label="Max Pain" value={chain?.max_pain ? `${chain.max_pain}` : "--"} />
                <PulseIndicatorCard
                  label="CE OI"
                  value={formatCompact(chain?.total_ce_oi)}
                  detail={chain?.total_ce_oi_change != null ? formatCompact(chain.total_ce_oi_change) : undefined}
                  directionValue={chain?.total_ce_oi_change}
                />
                <PulseIndicatorCard
                  label="PE OI"
                  value={formatCompact(chain?.total_pe_oi)}
                  detail={chain?.total_pe_oi_change != null ? formatCompact(chain.total_pe_oi_change) : undefined}
                  directionValue={chain?.total_pe_oi_change}
                />
                <PulseIndicatorCard label="CE Volume" value={formatCompact(chain?.total_ce_volume)} />
                <PulseIndicatorCard label="PE Volume" value={formatCompact(chain?.total_pe_volume)} />
                <PulseIndicatorCard
                  label="ATM CE"
                  value={formatSigned(chain?.atm_call_ltp_change, 2)}
                  detail={chain?.atm_call_ltp_change_pct != null ? formatSigned(chain.atm_call_ltp_change_pct, 2, "%") : undefined}
                  tone={valueTone(chain?.atm_call_ltp_change)}
                  directionValue={chain?.atm_call_ltp_change}
                />
                <PulseIndicatorCard
                  label="ATM PE"
                  value={formatSigned(chain?.atm_put_ltp_change, 2)}
                  detail={chain?.atm_put_ltp_change_pct != null ? formatSigned(chain.atm_put_ltp_change_pct, 2, "%") : undefined}
                  tone={valueTone(chain?.atm_put_ltp_change)}
                  directionValue={chain?.atm_put_ltp_change}
                />
              </div>
            </section>

            <section className="card rounded-[24px] p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-text-muted">
                  <BarChart3 size={14} className="text-accent-green" />
                  Market Profile
                </div>
                <div className="flex items-center gap-2">
                  {(["day", "week", "month"] as const).map((item) => (
                    <button
                      key={item}
                      onClick={() => setProfileTimeframe(item)}
                      className={clsx(
                        "rounded-lg border px-2.5 py-1 text-[11px] uppercase tracking-[0.08em]",
                        profileTimeframe === item
                          ? "border-accent-green bg-accent-green/12 text-accent-green"
                          : "border-bg-border bg-bg-secondary/45 text-text-muted",
                      )}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </div>
              {profile && !profile.error ? (
                <div className="mt-3">
                  <PulseRow label="POC" value={profile.poc?.toFixed(2) || "--"} tone="text-accent-amber" />
                  <PulseRow label="VAH" value={profile.vah?.toFixed(2) || "--"} tone="text-accent-green" />
                  <PulseRow label="VAL" value={profile.val?.toFixed(2) || "--"} tone="text-accent-red" />
                  <PulseRow label="IB High" value={profile.ib_high?.toFixed(2) || "--"} />
                  <PulseRow label="IB Low" value={profile.ib_low?.toFixed(2) || "--"} />
                  <PulseRow label="Samples" value={profile.sample_count ? `${profile.sample_count}` : "--"} />
                  <PulseRow label="Source" value={profile.source_interval?.toUpperCase() || "--"} />
                </div>
              ) : (
                <div className="mt-4 rounded-xl border border-dashed border-bg-border px-3 py-6 text-sm text-text-muted">
                  {profile?.error || "Waiting for enough live ticks to build market profile."}
                </div>
              )}
            </section>
          </div>
        </div>
      ) : workspace === "sectors" ? (
        <>
          <div className="grid gap-4 xl:grid-cols-[1.52fr_0.88fr]">
            <section className="card rounded-[24px] p-4">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-bg-border/70 pb-3">
                <div className="flex flex-wrap gap-2">
                  <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                    Benchmark
                    <span className="ml-2 font-mono font-semibold text-text-primary">
                      {sectorRotation?.benchmark?.name || "NIFTY 50"}
                    </span>
                  </div>
                  <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                    Sectors
                    <span className="ml-2 font-mono font-semibold text-text-primary">{sectorWatchlist.length}</span>
                  </div>
                  {sectorRotation?.source ? (
                    <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                      Source
                      <span className="ml-2 font-mono font-semibold text-accent-blue">{sectorRotation.source}</span>
                    </div>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  {(["hourly", "daily", "weekly", "monthly"] as const).map((item) => (
                    <button
                      key={item}
                      onClick={() => setSectorTimeframe(item)}
                      className={clsx(
                        "rounded-lg border px-2.5 py-1 text-[11px] uppercase tracking-[0.08em]",
                        sectorTimeframe === item
                          ? "border-accent-blue bg-accent-blue/12 text-accent-blue"
                          : "border-bg-border bg-bg-secondary/45 text-text-muted",
                      )}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </div>

              {sectorPoints.length ? (
                <div className="mt-4">
                  <SectorQuadrantBoard
                    points={sectorPoints}
                    selectedCode={selectedSectorCode}
                    onSelect={selectSectorAndOpen}
                  />
                </div>
              ) : (
                <div className="mt-4 rounded-2xl border border-dashed border-bg-border px-4 py-8 text-sm text-text-muted">
                  {sectorRotation?.detail || "Waiting for enough sector history to build the rotation map."}
                </div>
              )}
            </section>

            <section className="space-y-4">
              <div className="card rounded-[24px] p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Selected Sector</div>
                    <div className="mt-2 font-mono text-2xl font-semibold text-text-primary">
                      {selectedSector?.name || "Awaiting sector"}
                    </div>
                  </div>
                  {selectedSector ? (
                    <button
                      type="button"
                      onClick={() => setSectorDialogOpen(true)}
                      className="rounded-lg border border-accent-blue/35 bg-accent-blue/10 px-3 py-1.5 text-xs font-semibold text-accent-blue transition-colors hover:border-accent-blue hover:bg-accent-blue/15"
                    >
                      Open Spotlight
                    </button>
                  ) : null}
                </div>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-bg-border bg-bg-secondary/35 p-3">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Tracked</div>
                    <div className={clsx("mt-2 font-mono text-xl font-semibold", valueTone(selectedSector?.tracked_change_pct))}>
                      {formatSignedPct(selectedSector?.tracked_change_pct)}
                    </div>
                  </div>
                  <div className="rounded-xl border border-bg-border bg-bg-secondary/35 p-3">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">RS vs NIFTY</div>
                    <div className={clsx("mt-2 font-mono text-xl font-semibold", valueTone(selectedSector?.relative_strength_pct))}>
                      {formatSignedPct(selectedSector?.relative_strength_pct)}
                    </div>
                  </div>
                  <div className="rounded-xl border border-bg-border bg-bg-secondary/35 p-3">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">RRG Ratio</div>
                    <div className="mt-2 font-mono text-xl font-semibold text-accent-blue">
                      {selectedSector?.rrg_ratio?.toFixed(2) || "--"}
                    </div>
                  </div>
                  <div className="rounded-xl border border-bg-border bg-bg-secondary/35 p-3">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Momentum</div>
                    <div className="mt-2 font-mono text-xl font-semibold text-accent-green">
                      {selectedSector?.rrg_momentum?.toFixed(2) || "--"}
                    </div>
                  </div>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <SectorCluster
                  title="Leading"
                  sectors={leadingSectors}
                  selectedCode={selectedSectorCode}
                  onSelect={selectSectorAndOpen}
                />
                <SectorCluster
                  title="Improving"
                  sectors={improvingSectors}
                  selectedCode={selectedSectorCode}
                  onSelect={selectSectorAndOpen}
                />
                <SectorCluster
                  title="Weakening"
                  sectors={weakeningSectors}
                  selectedCode={selectedSectorCode}
                  onSelect={selectSectorAndOpen}
                />
                <SectorCluster
                  title="Lagging"
                  sectors={laggingSectors}
                  selectedCode={selectedSectorCode}
                  onSelect={selectSectorAndOpen}
                />
              </div>
            </section>
          </div>

          <SectorSpotlightDialog
            open={sectorDialogOpen}
            onClose={() => setSectorDialogOpen(false)}
            timeframe={sectorTimeframe}
            benchmark={sectorRotation?.benchmark}
            sectorPoints={sectorPoints}
            selectedSectorCode={selectedSectorCode}
            onSelectSector={setSelectedSectorCode}
            selectedSector={selectedSector}
            stockPoints={selectedSectorStocks}
            selectedStockCode={selectedStockCode}
            onSelectStock={setSelectedStockCode}
            stockLeading={stockLeading}
            stockImproving={stockImproving}
            stockWeakening={stockWeakening}
            stockLagging={stockLagging}
          />
        </>
      ) : (
        <section className="card rounded-[24px] p-4">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-bg-border/70 pb-3">
            <div className="flex flex-wrap gap-2">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">ATM Watchlist</div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                Rows <span className="ml-2 font-mono font-semibold text-text-primary">{watchlistData?.summary?.total_rows ?? 0}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                CE Ready <span className="ml-2 font-mono font-semibold text-accent-green">{watchlistData?.summary?.ce_ready ?? 0}</span>
              </div>
              <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                PE Ready <span className="ml-2 font-mono font-semibold text-accent-red">{watchlistData?.summary?.pe_ready ?? 0}</span>
              </div>
              {watchlistData?.source ? (
                <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 text-xs text-text-secondary">
                  Source <span className="ml-2 font-mono font-semibold text-accent-blue">{watchlistData.source.toUpperCase()}</span>
                </div>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <StreamStatus
                title="Watchlist"
                isStreamConnected={watchlistLiveQuery.isStreamConnected}
                isShowingSnapshot={watchlistLiveQuery.isShowingSnapshot}
                snapshotSavedAt={watchlistLiveQuery.snapshotSavedAt}
                liveText="ATM CE and PE rows are streaming"
                bootstrapText="loading expiry catalog and ATM rows"
              />
              <select
                value={watchlistExpiry}
                onChange={(event) => setWatchlistExpiry(event.target.value)}
                className="terminal-input min-w-[176px] py-1.5 text-xs"
              >
                {(watchlistExpiryData?.expiries ?? []).map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
                {!watchlistExpiryData?.expiries?.length && watchlistExpiry && (
                  <option value={watchlistExpiry}>{watchlistExpiry}</option>
                )}
                {!watchlistExpiryData?.expiries?.length && <option value="">Expiry loading...</option>}
              </select>
              <button
                onClick={() => {
                  void watchlistLiveQuery.refetch();
                }}
                className="rounded-lg border border-bg-border bg-bg-secondary/45 p-2 text-text-muted transition-colors hover:text-text-primary"
                aria-label="Refresh watchlist"
              >
                <RefreshCw size={14} />
              </button>
            </div>
          </div>

          {(watchlistExpiryData?.expiry_scope_note || watchlistExpiryData?.detail || watchlistData?.detail) ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {watchlistExpiryData?.expiry_scope_note ? (
                <div className="rounded-full border border-bg-border bg-bg-secondary/35 px-3 py-1.5 font-mono text-[11px] text-text-muted">
                  {watchlistExpiryData.expiry_scope_note}
                </div>
              ) : null}
              {watchlistExpiryData?.detail ? (
                <div className="rounded-full border border-accent-amber/20 bg-accent-amber/10 px-3 py-1.5 text-[11px] text-accent-amber">
                  {watchlistExpiryData.detail}
                </div>
              ) : null}
              {watchlistData?.detail ? (
                <div className="rounded-full border border-accent-amber/20 bg-accent-amber/10 px-3 py-1.5 text-[11px] text-accent-amber">
                  {watchlistData.detail}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="mt-4 overflow-hidden rounded-[22px] border border-bg-border bg-[#08101b]">
            <div className="max-h-[72vh] overflow-auto">
              <table className="w-full min-w-[1800px] text-left text-xs font-mono">
                <thead className="sticky top-0 z-10 bg-[#0d1625]">
                  <tr className="border-b border-bg-border text-text-muted">
                    <th className="px-3 py-3 font-normal">Underlying</th>
                    <th className="px-3 py-3 font-normal text-right">Spot</th>
                    <th className="px-3 py-3 font-normal">Expiry</th>
                    <th className="px-3 py-3 font-normal text-right">ATM Strike</th>
                    <th className="px-3 py-3 font-normal text-right">Lot</th>
                    <th className="px-3 py-3 font-normal">CE</th>
                    <th className="px-3 py-3 font-normal">PE</th>
                  </tr>
                </thead>
                <tbody>
                  {(watchlistData?.rows ?? []).map((row) => (
                    <tr key={`${row.underlying}:${row.expiry}`} className="border-b border-bg-border/30 align-top hover:bg-bg-secondary/20">
                      <td className="px-3 py-2.5">
                        <div className="font-semibold text-text-primary not-italic">{row.underlying}</div>
                        <div className="mt-1 flex items-center gap-1.5">
                          <span
                            className={clsx(
                              "rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider not-italic",
                              row.kind === "INDEX"
                                ? "bg-accent-blue/15 text-accent-blue"
                                : "bg-accent-amber/15 text-accent-amber",
                            )}
                          >
                            {row.kind === "INDEX" ? "IDX" : "STK"}
                          </span>
                          <span className="text-[9px] text-text-muted not-italic">{row.live_source}</span>
                        </div>
                      </td>
                      <td className="px-3 py-2.5 text-right text-text-primary">{row.spot_price.toFixed(2)}</td>
                      <td className="px-3 py-2.5 text-text-secondary">{row.expiry}</td>
                      <td className="px-3 py-2.5 text-right font-semibold text-accent-amber">{row.atm_strike}</td>
                      <td className="px-3 py-2.5 text-right text-text-muted">{row.lot_size ?? "--"}</td>
                      <td className="px-3 py-2.5"><ATMOptionCell option={row.ce} accent="ce" /></td>
                      <td className="px-3 py-2.5"><ATMOptionCell option={row.pe} accent="pe" /></td>
                    </tr>
                  ))}
                  {showWatchlistLoading && (
                    <tr>
                      <td colSpan={7} className="px-3 py-8 text-center text-sm text-text-muted">
                        <RefreshCw size={14} className="mr-2 inline animate-spin" />
                        {isWatchlistBuilding ? "Building ATM watchlist…" : "Loading ATM watchlist…"}
                      </td>
                    </tr>
                  )}
                  {!showWatchlistLoading && !(watchlistData?.rows?.length) && (
                    <tr>
                      <td colSpan={7} className="px-3 py-8 text-center text-sm text-text-muted">
                        {watchlistData?.detail || "No ATM watchlist rows are available for the selected expiry."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
