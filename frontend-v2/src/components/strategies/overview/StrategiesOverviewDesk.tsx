"use client";

/**
 * Strategies Overview desk — native v2, fully additive.
 *
 * One read-only board that shows every strategy lane working live at a glance.
 * It does NOT own any new backend surface: it aggregates each lane's existing
 * summary / live-snapshot endpoint plus the shared /ws/positions-overview book
 * (via useStrategyPositionsStream), then normalises everything into a single
 * LaneView[] the tabs render.
 *
 * Tabs:
 *   overview         → per-lane summary cards (status / last-scan / open / P&L / signal)
 *   signals          → sortable + filterable table of the latest signal per lane
 *   signal-quality   → evaluated coverage, rejection funnel, latency, drift and input quality
 *   market-structure → NIFTY market profile (TPO) + order flow + regime consensus
 *
 * Defensiveness is the whole point: any lane field may be missing, any endpoint
 * may 404, any snapshot may be null. Every read is guarded; nothing throws.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { LayoutGrid, ListChecks, Map as MapIcon, Network, ShieldCheck } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  StatusBadge,
  formatSignedMoney,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { useLaneRegistry } from "@/hooks/useLaneRegistry";
import {
  selectStrategySlice,
  useStrategyPositionsStream,
} from "@/hooks/useStrategyPositionsStream";
import { createStrategySnapshotSocket } from "@/lib/websocket";
import { api as apiClient } from "@/lib/api";
import { buildStrategyBookSummaries } from "@/lib/strategy-position-ledger";
import type { Snapshot as AuctionSnapshot } from "@/components/strategies/auction/types";

import { LaneSummaryCard } from "./LaneSummaryCard";
import { LaneInventoryTab } from "./LaneInventoryTab";
import { PortfolioReconciliation } from "./PortfolioReconciliation";
import { SignalBoardTab } from "./SignalBoardTab";
import { MarketStructureTab } from "./MarketStructureTab";
import { SignalQualityTab } from "./SignalQualityTab";
import {
  LANES,
  STREAM_KEY_BY_LANE,
  type LaneKey,
  type LaneSignal,
  type LaneView,
} from "./types";

const TABS = [
  { key: "overview", label: "Overview", icon: LayoutGrid },
  { key: "inventory", label: "Lane inventory", icon: Network },
  { key: "signals", label: "Signals", icon: ListChecks },
  { key: "signal-quality", label: "Signal quality", icon: ShieldCheck },
  { key: "market-structure", label: "Market structure", icon: MapIcon },
];

// ── Loose payload shapes (each lane's summary / status endpoint) ─────────────
// All optional — these are best-effort reads, every consumer guards.

type AnyRecord = Record<string, unknown>;

type NseLane = {
  key?: string;
  running?: boolean | null;
  last_scan_at?: string | null;
  open_positions?: number | null;
  summary?: {
    day_pnl?: number | null;
    unrealized_pnl?: number | null;
    open_positions?: number | null;
  } | null;
  signals?: Array<{
    underlying?: string | null;
    direction?: string | null;
    status?: string | null;
    reason?: string | null;
    priority_score?: number | null;
  }> | null;
  positions?: unknown[] | null;
};

type NseStatus = {
  running?: boolean | null;
  loop_active?: boolean | null;
  kill_switch_active?: boolean | null;
  last_run_at?: string | null;
  strategies?: NseLane[] | null;
};

type DirectionalSummary = {
  automation?: { loop_active?: boolean | null } | null;
};

type DirectionalSnapshot = {
  snapshot?: {
    as_of?: string | null;
    underlying?: string | null;
    regime?: { label?: string | null } | null;
    signal?: { direction?: string | null; confidence?: number | null } | null;
    selection_reason?: string | null;
  } | null;
};

type AuctionSummary = AnyRecord & {
  paper?: { running?: boolean | null } | null;
  running?: boolean | null;
  loop_active?: boolean | null;
};

type FractalSummary = AnyRecord & {
  running?: boolean | null;
  loop_active?: boolean | null;
};

type FractalSnapshot = {
  session?: { last_price?: number | null } | null;
  generated_at?: string | null;
  daily_profile?: { direction_bias?: string | null; shape?: string | null } | null;
  current_signal?: {
    underlying?: string | null;
    action?: string | null;
    confidence?: number | null;
    signal_time?: string | null;
    setup_name?: string | null;
    daily_context?: string | null;
  } | null;
};

type GannSnapshot = {
  as_of?: string | null;
  underlying?: string | null;
  signal?: {
    bias?: string | null;
    state?: string | null;
    score?: number | null;
    threshold?: number | null;
    reasons?: string[] | null;
  } | null;
};

type CbeScan = {
  source?: string | null;
  scan_date?: string | null;
  created_at?: string | null;
  watchlist_count?: number | null;
  asset_winner?: string | null;
  results?: Array<{
    instrument?: string | null;
    directional_bias?: string | null;
    bias_conviction?: number | null;
    composite_score?: number | null;
    is_watchlist?: boolean | null;
    weekly_trend?: string | null;
  }> | null;
};

type CommodityStatus = {
  last_run_at?: string | null;
  running?: boolean | null;
  loop_active?: boolean | null;
  summary?: {
    day_pnl?: number | null;
    unrealized_pnl?: number | null;
    open_positions?: number | null;
  } | null;
  positions?: unknown[] | null;
};

// Helpers ────────────────────────────────────────────────────────────────────

function num(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Sum a numeric field across an open-position book. */
function sumField(rows: unknown[] | null | undefined, field: string): number | null {
  if (!Array.isArray(rows) || !rows.length) return null;
  let total = 0;
  let seen = false;
  for (const row of rows) {
    const v = num((row as AnyRecord | null | undefined)?.[field]);
    if (v != null) {
      total += v;
      seen = true;
    }
  }
  return seen ? total : null;
}

export default function StrategiesOverviewDesk() {
  const [activeTab, setActiveTab] = useUrlTab("overview");

  // Live open book — the shared positions-overview stream. Always on so the
  // open/closed/P&L marks stay fresh on every tab.
  const posStream = useStrategyPositionsStream();
  const streamLive = posStream.isStreamConnected;

  // Lane registry — shares LaneInventoryTab's single react-query fetch (same
  // key), so reading it here for the inventory-tab timestamp costs no extra
  // request. Its summary.generated_at is the honest "as of" for that tab.
  const laneReg = useLaneRegistry();

  // ── Per-lane status / summary queries (slow-ish, summary cadence) ──────────
  // Each is independent + tolerant: a 404 / network error just yields no data
  // and the lane card degrades to "idle / —".

  const nseQuery = useQuery({
    queryKey: ["overview", "nse-status"],
    queryFn: async () =>
      (await apiClient.get("/api/trading/strategy-agent/status")).data as NseStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const directionalSummaryQuery = useQuery({
    queryKey: ["overview", "directional-summary"],
    queryFn: async () =>
      (await apiClient.get("/api/directional-options/summary")).data as DirectionalSummary,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const directionalSnapQuery = useLiveSnapshotQuery<DirectionalSnapshot>({
    queryKey: ["overview", "directional-snap"],
    queryFn: async () =>
      (
        await apiClient.get("/api/directional-options/live-snapshot", {
          params: { underlying: "NIFTY" },
        })
      ).data as DirectionalSnapshot,
    storageKey: "overview-directional-snap",
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket(
        "directional",
        "NIFTY",
        "3minute",
        (d) => onData(d as DirectionalSnapshot),
        onStatusChange,
      ),
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const auctionSummaryQuery = useQuery({
    queryKey: ["overview", "auction-summary"],
    queryFn: async () =>
      (await apiClient.get("/api/auction-intelligence/summary")).data as AuctionSummary,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // Auction live snapshot doubles as the market-structure feed (NIFTY).
  const auctionSnapQuery = useLiveSnapshotQuery<AuctionSnapshot>({
    queryKey: ["overview", "auction-snap", "NIFTY"],
    queryFn: async () =>
      (
        await apiClient.get("/api/auction-intelligence/live-snapshot", {
          params: { symbol: "NIFTY" },
        })
      ).data as AuctionSnapshot,
    storageKey: "overview-auction-snap-NIFTY",
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket(
        "auction",
        "NIFTY",
        null,
        (d) => onData(d as AuctionSnapshot),
        onStatusChange,
      ),
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const fractalSummaryQuery = useQuery({
    queryKey: ["overview", "fractal-summary"],
    queryFn: async () =>
      (await apiClient.get("/api/fractal-market-profile/summary")).data as FractalSummary,
    // FMP lane parked out of production 2026-07-07 (tile removed from LANES) —
    // keep the code but stop the polling.
    enabled: false,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const fractalSnapQuery = useLiveSnapshotQuery<FractalSnapshot>({
    queryKey: ["overview", "fractal-snap", "NIFTY"],
    queryFn: async () =>
      (
        await apiClient.get("/api/fractal-market-profile/live-snapshot", {
          params: { symbol: "NIFTY" },
        })
      ).data as FractalSnapshot,
    storageKey: "overview-fractal-snap-NIFTY",
    enabled: false, // FMP parked out of production 2026-07-07 — keep code, stop polling
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket(
        "fractal",
        "NIFTY",
        null,
        (d) => onData(d as FractalSnapshot),
        onStatusChange,
      ),
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const gannSnapQuery = useLiveSnapshotQuery<GannSnapshot>({
    queryKey: ["overview", "gann-snap", "NIFTY"],
    queryFn: async () =>
      (
        await apiClient.get("/api/gann-tp-delta/live-snapshot", {
          params: { underlying: "NIFTY", timeframe: "15minute" },
        })
      ).data as GannSnapshot,
    storageKey: "overview-gann-snap-NIFTY",
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket(
        "gann",
        "NIFTY",
        "15minute",
        (d) => onData(d as GannSnapshot),
        onStatusChange,
      ),
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const cbeQuery = useQuery({
    queryKey: ["overview", "cbe-latest"],
    queryFn: async () =>
      (await apiClient.get("/api/cbe/latest")).data as CbeScan,
    refetchInterval: REFRESH_MS.slow,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const commodityQuery = useQuery({
    queryKey: ["overview", "commodity-status"],
    queryFn: async () =>
      (await apiClient.get("/api/commodity/strategy-agent/status")).data as CommodityStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // ── Assemble normalised LaneView[] ─────────────────────────────────────────

  const lanes = useMemo<LaneView[]>(() => {
    const out: LaneView[] = [];

    for (const lane of LANES) {
      const base = { key: lane.key, label: lane.label, href: lane.href };

      // Live open book off the shared positions stream (when this lane uses it).
      const streamKey = STREAM_KEY_BY_LANE[lane.key];
      const slice = streamKey ? selectStrategySlice(posStream.data, streamKey) : undefined;
      const streamOpen = slice?.open_positions;
      const streamOpenCount = Array.isArray(streamOpen) ? streamOpen.length : null;
      const streamUnreal = sumField(streamOpen, "unrealized_pnl");

      switch (lane.key) {
        case "nse": {
          out.push(buildNse(base, nseQuery.data, nseQuery.isError, streamOpenCount, streamUnreal));
          break;
        }
        case "directional": {
          out.push(
            buildDirectional(
              base,
              directionalSummaryQuery.data,
              directionalSnapQuery.data,
              directionalSummaryQuery.isError && directionalSnapQuery.isError,
              streamOpenCount,
              streamUnreal,
            ),
          );
          break;
        }
        case "auction": {
          out.push(
            buildAuction(
              base,
              auctionSummaryQuery.data,
              auctionSnapQuery.data,
              auctionSummaryQuery.isError && auctionSnapQuery.isError,
              streamOpenCount,
              streamUnreal,
            ),
          );
          break;
        }
        case "fractal": {
          out.push(
            buildFractal(
              base,
              fractalSummaryQuery.data,
              fractalSnapQuery.data,
              fractalSummaryQuery.isError && fractalSnapQuery.isError,
              streamOpenCount,
              streamUnreal,
            ),
          );
          break;
        }
        case "gann": {
          out.push(
            buildGann(base, gannSnapQuery.data, gannSnapQuery.isError, streamOpenCount, streamUnreal),
          );
          break;
        }
        case "cbe": {
          out.push(buildCbe(base, cbeQuery.data, cbeQuery.isError, streamOpenCount, streamUnreal));
          break;
        }
        case "commodity": {
          out.push(buildCommodity(base, commodityQuery.data, commodityQuery.isError));
          break;
        }
        default:
          out.push({ ...base, degraded: true });
      }
    }
    return out;
  }, [
    posStream.data,
    nseQuery.data,
    nseQuery.isError,
    directionalSummaryQuery.data,
    directionalSummaryQuery.isError,
    directionalSnapQuery.data,
    directionalSnapQuery.isError,
    auctionSummaryQuery.data,
    auctionSummaryQuery.isError,
    auctionSnapQuery.data,
    auctionSnapQuery.isError,
    fractalSummaryQuery.data,
    fractalSummaryQuery.isError,
    fractalSnapQuery.data,
    fractalSnapQuery.isError,
    gannSnapQuery.data,
    gannSnapQuery.isError,
    cbeQuery.data,
    cbeQuery.isError,
    commodityQuery.data,
    commodityQuery.isError,
  ]);

  // ── Portfolio-level roll-ups for the KPI strip ─────────────────────────────
  const runningCount = lanes.filter((l) => l.running).length;
  // The old 6-lane reduce (stream/summary mix) — kept ONLY to show the Overview
  // surface's own number inside the reconciliation panel, not as the headline.
  const overviewScopeOpen = lanes.reduce((acc, l) => acc + (l.openCount ?? 0), 0);
  // Canonical open count — one source of truth across all 9 strategy books.
  const bookSummaries = useMemo(
    () => buildStrategyBookSummaries(posStream.data),
    [posStream.data],
  );
  const totalOpen = bookSummaries.reduce((acc, b) => acc + b.openPositions, 0);
  const totalUnreal = lanes.reduce(
    (acc, l) => acc + (l.unrealizedPnl ?? 0),
    0,
  );
  const signalsLive = lanes.filter((l) => l.signal && (l.signal.direction || l.signal.state)).length;

  const isFetching =
    nseQuery.isFetching ||
    directionalSummaryQuery.isFetching ||
    auctionSummaryQuery.isFetching ||
    fractalSummaryQuery.isFetching ||
    gannSnapQuery.isFetching ||
    cbeQuery.isFetching ||
    commodityQuery.isFetching;

  // Tab-aware freshness: the inventory tab renders the 32-lane registry, whose
  // real "as of" is the registry generated_at — NOT the auction snapshot time
  // (which always read "no data" here). Other tabs keep the auction quote time.
  const auctionAsOf = auctionSnapQuery.data?.request?.quote?.timestamp as string | undefined;
  const inventoryAsOf = laneReg.data?.summary?.generated_at ?? null;

  return (
    <DeskShell
      title="Strategies Overview"
      description="Every strategy lane at a glance — running state, last scan, live open book, latest signal and regime, aggregated read-only across the desk."
      asOf={activeTab === "inventory" ? inventoryAsOf : auctionAsOf}
      asOfLabel="Updated"
      asOfStaleSeconds={activeTab === "inventory" ? 120 : undefined}
      isFetching={isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      rightSlot={
        <StatusBadge
          label={streamLive ? "● live book" : "polling"}
          variant={streamLive ? "success" : "info"}
        />
      }
    >
      {activeTab !== "signal-quality" && activeTab !== "inventory" ? (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile
            label="Lanes running"
            value={`${runningCount} / ${lanes.length}`}
            detail="active loops / scanners"
          />
          <MetricTile
            label="Open positions"
            value={String(totalOpen)}
            detail="canonical · all 9 books"
          />
          <MetricTile
            label="Open P&L"
            value={formatSignedMoney(totalUnreal)}
            color={tone(totalUnreal)}
            detail="unrealized, streamed"
          />
          <MetricTile label="Live signals" value={String(signalsLive)} detail="lanes with a read" />
        </section>
      ) : null}

      {activeTab === "overview" ? (
        <>
          <section className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {lanes.map((lane) => (
              <LaneSummaryCard key={lane.key} lane={lane} />
            ))}
          </section>
          <PortfolioReconciliation
            snapshot={posStream.data}
            overviewDisplayedOpen={overviewScopeOpen}
          />
        </>
      ) : null}

      {activeTab === "inventory" ? <LaneInventoryTab /> : null}

      {activeTab === "signals" ? <SignalBoardTab lanes={lanes} /> : null}

      {activeTab === "signal-quality" ? <SignalQualityTab /> : null}

      {activeTab === "market-structure" ? (
        <MarketStructureTab snapshot={auctionSnapQuery.data} lanes={lanes} />
      ) : null}
    </DeskShell>
  );
}

// ── Per-lane builders ─────────────────────────────────────────────────────────

type Base = { key: LaneKey; label: string; href: string };

function buildNse(
  base: Base,
  data: NseStatus | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!data && isError) return { ...base, degraded: true };
  const lanes = data?.strategies ?? [];
  const lane = lanes.find((s) => s?.key === "macd_strategy") ?? lanes[0];
  const running = (data?.running || data?.loop_active) && !data?.kill_switch_active;
  const summary = lane?.summary ?? undefined;
  const topSignal = (lane?.signals ?? [])[0];
  const signal: LaneSignal | null = topSignal
    ? {
        direction: topSignal.direction ?? null,
        symbol: topSignal.underlying ?? null,
        reason: topSignal.reason ?? null,
        confidence: null,
        state: topSignal.status ?? null,
      }
    : null;
  return {
    ...base,
    running: Boolean(running),
    lastScanAt: lane?.last_scan_at ?? data?.last_run_at ?? null,
    openCount: streamOpen ?? num(summary?.open_positions) ?? (lane?.positions?.length ?? null),
    dayPnl: num(summary?.day_pnl),
    unrealizedPnl: streamUnreal ?? num(summary?.unrealized_pnl),
    signal,
  };
}

function buildDirectional(
  base: Base,
  summary: DirectionalSummary | undefined,
  snap: DirectionalSnapshot | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!summary && !snap && isError) return { ...base, degraded: true };
  const s = snap?.snapshot ?? undefined;
  const sig = s?.signal ?? undefined;
  const dir = sig?.direction && sig.direction !== "flat" ? sig.direction : null;
  return {
    ...base,
    running: Boolean(summary?.automation?.loop_active),
    lastScanAt: s?.as_of ?? null,
    openCount: streamOpen,
    unrealizedPnl: streamUnreal,
    regime: s?.regime?.label ?? null,
    signal:
      dir || s?.regime?.label
        ? {
            direction: dir,
            confidence: num(sig?.confidence),
            symbol: s?.underlying ?? "NIFTY",
            reason: s?.selection_reason ?? null,
            time: s?.as_of ?? null,
            state: dir ? "signal" : null,
          }
        : null,
  };
}

function buildAuction(
  base: Base,
  summary: AuctionSummary | undefined,
  snap: AuctionSnapshot | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!summary && !snap && isError) return { ...base, degraded: true };
  const analysis = snap?.analysis;
  const regime = analysis?.regime;
  const running = Boolean(summary?.paper?.running ?? summary?.running ?? summary?.loop_active);
  const allowed = (regime?.allowed_directions ?? []).filter(Boolean);
  const dir = allowed.length ? allowed.join(" / ") : null;
  return {
    ...base,
    running,
    lastScanAt: (snap?.request?.quote?.timestamp as string | undefined) ?? null,
    openCount: streamOpen,
    unrealizedPnl: streamUnreal,
    regime: regime?.label ?? null,
    signal:
      regime?.label || dir
        ? {
            direction: dir,
            confidence: num(regime?.confidence),
            symbol: snap?.symbol_code ?? "NIFTY",
            reason: (regime?.reasons ?? [])[0] ?? null,
            state: regime?.label ?? null,
          }
        : null,
  };
}

function buildFractal(
  base: Base,
  summary: FractalSummary | undefined,
  snap: FractalSnapshot | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!summary && !snap && isError) return { ...base, degraded: true };
  const sig = snap?.current_signal ?? undefined;
  const daily = snap?.daily_profile ?? undefined;
  return {
    ...base,
    running: Boolean(summary?.running ?? summary?.loop_active),
    lastScanAt: snap?.generated_at ?? null,
    openCount: streamOpen,
    unrealizedPnl: streamUnreal,
    regime: daily?.direction_bias ?? daily?.shape ?? null,
    signal: sig
      ? {
          direction: sig.action ?? daily?.direction_bias ?? null,
          confidence: num(sig.confidence),
          symbol: sig.underlying ?? "NIFTY",
          reason: sig.setup_name ?? sig.daily_context ?? null,
          time: sig.signal_time ?? null,
          state: sig.setup_name ?? null,
        }
      : daily?.direction_bias
        ? { direction: daily.direction_bias, symbol: "NIFTY", state: daily.shape ?? null }
        : null,
  };
}

function buildGann(
  base: Base,
  snap: GannSnapshot | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!snap && isError) return { ...base, degraded: true };
  const sig = snap?.signal ?? undefined;
  return {
    ...base,
    // Gann has no loop flag in the snapshot; presence of a fresh snapshot is
    // our best running proxy. Leave null when there's nothing.
    running: snap ? true : null,
    lastScanAt: snap?.as_of ?? null,
    openCount: streamOpen,
    unrealizedPnl: streamUnreal,
    regime: sig?.bias ?? null,
    signal: sig
      ? {
          direction: sig.bias ?? null,
          confidence:
            sig.score != null && sig.threshold
              ? Math.min(1, Number(sig.score) / Number(sig.threshold))
              : null,
          symbol: snap?.underlying ?? "NIFTY",
          reason: (sig.reasons ?? [])[0] ?? null,
          time: snap?.as_of ?? null,
          state: sig.state ?? null,
        }
      : null,
  };
}

function buildCbe(
  base: Base,
  scan: CbeScan | undefined,
  isError: boolean,
  streamOpen: number | null,
  streamUnreal: number | null,
): LaneView {
  if (!scan && isError) return { ...base, degraded: true };
  const results = scan?.results ?? [];
  const top =
    results.find((r) => r?.is_watchlist) ??
    [...results].sort((a, b) => (num(b?.composite_score) ?? 0) - (num(a?.composite_score) ?? 0))[0];
  return {
    ...base,
    // CBE is a weekly scanner (no live loop); a recent scan ⇒ "running".
    running: scan ? Boolean(scan.watchlist_count) : null,
    lastScanAt: scan?.created_at ?? scan?.scan_date ?? null,
    openCount: streamOpen,
    unrealizedPnl: streamUnreal,
    signal: top
      ? {
          direction: top.directional_bias ?? top.weekly_trend ?? null,
          confidence: num(top.bias_conviction),
          symbol: top.instrument ?? scan?.asset_winner ?? null,
          reason: `composite ${num(top.composite_score)?.toFixed(1) ?? "—"}`,
          time: scan?.created_at ?? scan?.scan_date ?? null,
          state: top.is_watchlist ? "watchlist" : null,
        }
      : null,
  };
}

function buildCommodity(
  base: Base,
  data: CommodityStatus | undefined,
  isError: boolean,
): LaneView {
  if (!data && isError) return { ...base, degraded: true };
  const summary = data?.summary ?? undefined;
  return {
    ...base,
    running: Boolean(data?.running ?? data?.loop_active),
    lastScanAt: data?.last_run_at ?? null,
    openCount: num(summary?.open_positions) ?? (data?.positions?.length ?? null),
    dayPnl: num(summary?.day_pnl),
    unrealizedPnl: num(summary?.unrealized_pnl) ?? sumField(data?.positions, "unrealized_pnl"),
    // Commodity is a futures/MP engine; no single per-snapshot signal here.
    signal: null,
  };
}
