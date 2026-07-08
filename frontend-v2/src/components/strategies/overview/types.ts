/**
 * Shared shapes + lane registry for the Strategies Overview desk.
 *
 * This page is a read-only aggregator: it stitches together each lane's
 * existing summary / live-snapshot endpoint plus the shared
 * /ws/positions-overview book. Every field every lane emits is optional in
 * practice (a lane may be idle, an endpoint may 404, a snapshot may be null),
 * so EVERYTHING here is nullable and consumers must guard each read.
 */
import type { StrategyStreamKey } from "@/hooks/useStrategyPositionsStream";

/** The canonical lane keys this overview understands. */
export type LaneKey =
  | "nse"
  | "directional"
  | "auction"
  | "fractal"
  | "gann"
  | "cbe"
  | "commodity";

/** A normalised latest-signal row, derived from whatever shape a lane emits. */
export type LaneSignal = {
  /** "CE" | "PE" | "bullish" | "bearish" | "LONG" | … — raw, lane-specific. */
  direction?: string | null;
  /** 0..1 ratio when known. */
  confidence?: number | null;
  symbol?: string | null;
  reason?: string | null;
  time?: string | null;
  /** A short status / state word ("entry-ready", "armed", "waiting", …). */
  state?: string | null;
};

/** A normalised per-lane view assembled from the lane's status + book. */
export type LaneView = {
  key: LaneKey;
  label: string;
  /** Existing v2 route this card links to. */
  href: string;
  /** Whether the lane's loop / scanner reports as active. */
  running?: boolean | null;
  /** ISO time of the lane's last scan / snapshot, when known. */
  lastScanAt?: string | null;
  /** Live open-position count (from the positions-overview book). */
  openCount?: number | null;
  /** Live day P&L (unrealized + day realized) when the lane reports it. */
  dayPnl?: number | null;
  /** Live unrealized P&L of the open book. */
  unrealizedPnl?: number | null;
  /** Regime label when the lane exposes one. */
  regime?: string | null;
  /** Latest signal, normalised. */
  signal?: LaneSignal | null;
  /** True when the lane's status endpoint errored / returned nothing. */
  degraded?: boolean;
};

/** Lanes whose open book lives on the positions-overview stream. */
export const STREAM_KEY_BY_LANE: Partial<Record<LaneKey, StrategyStreamKey>> = {
  directional: "directional",
  gann: "gann",
  auction: "auction",
  fractal: "fractal",
  cbe: "cbe",
};

export type LaneStatic = {
  key: LaneKey;
  label: string;
  href: string;
};

/** Static registry — names + routes. Wiring/parsing happens in the desk. */
export const LANES: LaneStatic[] = [
  { key: "nse", label: "NSE Index · MACD", href: "/strategies/nse/live" },
  { key: "directional", label: "Long Premium", href: "/strategies/directional" },
  { key: "auction", label: "Auction IQ", href: "/strategies/auction" },
  // Fractal MP (FMP) parked out of production 2026-07-07 — revisit later. The
  // LaneKey member, STREAM_KEY_BY_LANE.fractal and the desk's `case "fractal"`
  // block stay so this file + StrategiesOverviewDesk still type-check.
  // { key: "fractal", label: "Fractal MP", href: "/strategies/fractal" },
  { key: "gann", label: "Gann TP Delta", href: "/strategies/gann" },
  { key: "cbe", label: "CBE Scanner", href: "/strategies/cbe" },
  { key: "commodity", label: "Commodity", href: "/strategies/commodity" },
];

/**
 * Map a raw direction/bias word into a coarse bullish/bearish/neutral bucket
 * so the regime-consensus summary can count across lanes that speak different
 * dialects (CE/PE, bullish/bearish, LONG/SHORT, BUY/SELL).
 */
export function biasBucket(raw?: string | null): "bullish" | "bearish" | "neutral" {
  const s = String(raw ?? "").toLowerCase();
  if (!s) return "neutral";
  if (
    s.includes("ce") ||
    s.includes("bull") ||
    s.includes("long") ||
    s.includes("buy") ||
    s.includes("up") ||
    s.includes("breakout")
  ) {
    return "bullish";
  }
  if (
    s.includes("pe") ||
    s.includes("bear") ||
    s.includes("short") ||
    s.includes("sell") ||
    s.includes("down") ||
    s.includes("risk_off")
  ) {
    return "bearish";
  }
  return "neutral";
}
