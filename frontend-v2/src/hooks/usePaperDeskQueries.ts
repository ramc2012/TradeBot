"use client";

/**
 * Standardised paper-desk data hook.
 *
 * Every desk's paper-trading tab needs the same three queries:
 *   - summary (capital + P&L)
 *   - positions (open + closed)
 *   - journal (recent decisions)
 *
 * v1 duplicated this triple of useQuery() + manual refetch wiring in at
 * least Auction Intelligence and CBE (and the v1 directional desk does
 * it inline too). This hook is the canonical version — every v2 paper
 * tab consumes it.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { REFRESH_MS } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

export type PaperDeskEndpoints = {
  /** GET endpoint returning the capital + win-rate summary. */
  summary: string;
  /** GET endpoint returning {open_positions, closed_positions}. */
  positions: string;
  /** GET endpoint returning {records: [...]}. */
  journal: string;
  /** Optional POST endpoint for reset; if present the hook exposes resetAll(). */
  reset?: string;
};

export type UsePaperDeskQueriesOptions = {
  deskKey: string;
  endpoints: PaperDeskEndpoints;
  symbol?: string | null;
  limit?: number;
};

export function usePaperDeskQueries({
  deskKey,
  endpoints,
  symbol,
  limit = 50,
}: UsePaperDeskQueriesOptions) {
  const qc = useQueryClient();

  const summaryQuery = useQuery({
    queryKey: ["paper-desk", deskKey, "summary"],
    queryFn: async () => (await apiClient.get(endpoints.summary)).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const positionsQuery = useQuery({
    queryKey: ["paper-desk", deskKey, "positions", symbol || "all"],
    queryFn: async () =>
      (await apiClient.get(endpoints.positions, { params: { symbol, status: "all", limit } })).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const journalQuery = useQuery({
    queryKey: ["paper-desk", deskKey, "journal", symbol || "all"],
    queryFn: async () =>
      (await apiClient.get(endpoints.journal, { params: { symbol, limit } })).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const refreshAll = async () => {
    await Promise.all([
      summaryQuery.refetch(),
      positionsQuery.refetch(),
      journalQuery.refetch(),
    ]);
  };

  const resetAccount = endpoints.reset
    ? async (actor?: string) => {
        await apiClient.post(endpoints.reset!, { confirm: "RESET", actor });
        qc.invalidateQueries({ queryKey: ["paper-desk", deskKey] });
      }
    : null;

  return {
    summary: summaryQuery,
    positions: positionsQuery,
    journal: journalQuery,
    refreshAll,
    resetAccount,
  };
}
