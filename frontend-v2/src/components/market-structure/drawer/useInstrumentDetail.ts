"use client";

/**
 * useInstrumentDetail — detail ON DEMAND, for exactly one instrument.
 *
 * The matrix is a summary; everything expensive lives here and is fetched only
 * for the pinned symbol, only while the drawer is open. Two rules keep this
 * from becoming the poll storm the backend was just cured of:
 *
 *   1. `enabled` is gated on (drawer open && symbol) — arrow-keying through 216
 *      rows fetches NOTHING, because focus does not pin.
 *   2. The heavy auction snapshot (59 KB/symbol) is not fetched automatically
 *      at all; it loads on an explicit request, and the result is handed back to
 *      the workspace so the matrix's Auction column can fill in honestly for the
 *      symbols the trader actually looked at.
 */
import { useQuery } from "@tanstack/react-query";

import { REFRESH_MS } from "@/components/desk-ui";
import {
  getAuctionIntelligenceLiveSnapshot,
  getInstitutionalConvergenceDetail,
} from "@/lib/api";

import type { MarketKey } from "../context/schema";

export type ConvergenceDetail = {
  symbol?: string;
  result?: Record<string, unknown> | null;
};

export type AuctionDetail = {
  analysis?: {
    regime?: string | null;
    confidence?: number | null;
    risk?: { allowed?: boolean | null; reasons?: string[] | null; max_size_multiplier?: number | null } | null;
  } | null;
  data_status?: Record<string, unknown> | null;
  generated_at?: string | null;
};

export function useConvergenceDetail(symbol: string, market: MarketKey, enabled: boolean) {
  return useQuery({
    queryKey: ["ms-detail", "convergence", market, symbol],
    queryFn: async () =>
      (await getInstitutionalConvergenceDetail(symbol, market)).data as ConvergenceDetail,
    enabled: enabled && !!symbol,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useAuctionDetail(symbol: string, requested: boolean) {
  return useQuery({
    queryKey: ["ms-detail", "auction", symbol],
    queryFn: async () => (await getAuctionIntelligenceLiveSnapshot(symbol)).data as AuctionDetail,
    // 59 KB per symbol — never automatic, never on focus, only on request.
    enabled: requested && !!symbol,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
}
