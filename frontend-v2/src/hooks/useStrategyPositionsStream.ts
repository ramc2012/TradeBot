"use client";

/**
 * Live open-positions stream for a single strategy desk.
 *
 * Reuses the PROVEN /ws/positions-overview channel — the same one the global
 * /positions page and the commodity desk already consume in production. That
 * channel aggregates every strategy's paper book and pushes a fresh snapshot
 * every ~2s with live per-tick P&L marks overlaid server-side. Each desk just
 * selects its own slice instead of polling /paper-positions on a slow cadence.
 *
 * Built on useLiveSnapshotQuery, so a desk gets, for free:
 *   - a localStorage snapshot for instant first paint,
 *   - the WebSocket stream while the socket is up,
 *   - a polling fallback (fetchAppStrategyPortfolioSnapshot) when it isn't.
 *
 * The overlay is intentionally ADDITIVE: callers gate on `isStreamConnected`
 * and a present slice, falling back to their existing polled positions when
 * either is missing — so the worst case is identical to today's behaviour.
 */
import {
  type AppStrategyPortfolioSnapshot,
  fetchAppStrategyPortfolioSnapshot,
} from "@/lib/strategy-position-ledger";
import { createPositionsOverviewSocket } from "@/lib/websocket";

import { useLiveSnapshotQuery } from "./useLiveSnapshotQuery";

/** Strategy slices that expose the canonical {open_positions, closed_positions} shape. */
export type StrategyStreamKey = "directional" | "gann" | "auction" | "fractal" | "cbe";

export type StrategyPositionsSlice = {
  summary?: Record<string, unknown>;
  open_positions?: Array<Record<string, unknown>>;
  closed_positions?: Array<Record<string, unknown>>;
};

type PositionsOverviewPayload = AppStrategyPortfolioSnapshot & {
  // The overview socket aliases the nse slice as `strategy` for legacy reasons.
  strategy?: AppStrategyPortfolioSnapshot["nse"];
};

function normalizePositionsOverview(payload: PositionsOverviewPayload): AppStrategyPortfolioSnapshot {
  return {
    nse: payload.nse ?? payload.strategy ?? null,
    commodity: payload.commodity ?? null,
    directional: payload.directional ?? null,
    gann: payload.gann ?? null,
    auction: payload.auction ?? null,
    fractal: payload.fractal ?? null,
    cbe: payload.cbe ?? null,
    errors: payload.errors ?? {},
    fetchedAt: payload.fetchedAt ?? new Date().toISOString(),
  };
}

/**
 * Subscribe to the aggregate positions-overview stream.
 *
 * Pass `{ enabled: false }` (e.g. when the desk's positions tab is hidden) to
 * release the socket and stop polling entirely.
 */
export function useStrategyPositionsStream(options?: { enabled?: boolean }) {
  const enabled = options?.enabled ?? true;

  return useLiveSnapshotQuery<AppStrategyPortfolioSnapshot>({
    queryKey: ["strategyPositionsStream"],
    queryFn: () => fetchAppStrategyPortfolioSnapshot(),
    storageKey: "strategyPositionsStream",
    streamFactory: (onData, onStatusChange) =>
      createPositionsOverviewSocket((payload) => {
        onData(normalizePositionsOverview(payload as PositionsOverviewPayload));
      }, onStatusChange),
    staleTime: 5_000,
    refetchInterval: 15_000,
    enabled,
  });
}

/** Pull one strategy's open/closed book out of the aggregate snapshot. */
export function selectStrategySlice(
  snapshot: AppStrategyPortfolioSnapshot | undefined,
  key: StrategyStreamKey,
): StrategyPositionsSlice | undefined {
  const slice = snapshot?.[key];
  return (slice as StrategyPositionsSlice | null | undefined) ?? undefined;
}
