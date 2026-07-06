"use client";

/**
 * TerminalPanel — the live cockpit. A quote grid (driven by the low-latency
 * /ws/quotes tape) on the left, a 5-level depth ladder for the focused symbol
 * on the right. Drop this into any desk as a "Terminal" tab.
 *
 * Two modes:
 *  - GLOBAL (no `symbols` prop): auto-populates from every symbol the backend
 *    is streaming (useKnownSymbols).
 *  - LANE-SCOPED (`symbols` provided): shows only that lane's symbols — its
 *    watchlist underlyings + open-position legs — so each desk gets a live
 *    cockpit for exactly what it trades. Falls back to the global tape when the
 *    lane has nothing scoped yet, so the panel is never blank.
 */
import { useMemo, useState } from "react";

import { useKnownSymbols } from "@/hooks/useQuoteStore";
import { getMarketIndexLabel } from "@/lib/marketSymbols";
import { QuoteGrid } from "./QuoteGrid";
import { DepthLadder } from "./DepthLadder";
import { QuoteConnectionBadge } from "./LiveMarkBadge";

function shortLabel(sym: string): string {
  // Prefer the friendly index label; else "NSE:NIFTY50-INDEX" → "NIFTY50-INDEX".
  const friendly = getMarketIndexLabel(sym);
  if (friendly !== sym) return friendly;
  return sym.includes(":") ? sym.split(":").slice(1).join(":") : sym;
}

export function TerminalPanel({
  symbols: scopedSymbols,
  title = "Live Terminal",
  subtitle,
}: {
  /** Lane-scoped symbol list; omit for the global all-symbols tape. */
  symbols?: string[];
  title?: string;
  subtitle?: string;
} = {}) {
  const knownSymbols = useKnownSymbols();
  const scopedActive = Array.isArray(scopedSymbols) && scopedSymbols.length > 0;
  const symbols = scopedActive ? (scopedSymbols as string[]) : knownSymbols;
  const [selected, setSelected] = useState<string | null>(null);
  const labels = useMemo(
    () => Object.fromEntries(symbols.map((s) => [s, shortLabel(s)])),
    [symbols],
  );
  const focus = selected && symbols.includes(selected) ? selected : symbols[0] ?? null;
  const resolvedSubtitle =
    subtitle ??
    (scopedActive
      ? "This lane's watchlist + positions · click a row for its depth ladder"
      : "Per-tick quote tape · click a row for its depth ladder");

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[13px] font-semibold text-text-primary">{title}</div>
          <div className="text-[11px] text-text-muted">{resolvedSubtitle}</div>
        </div>
        <QuoteConnectionBadge />
      </div>

      {symbols.length === 0 ? (
        <div className="rounded-xl border border-bg-border bg-bg-card/40 p-8 text-center text-[12px] text-text-muted">
          {scopedActive
            ? "No watchlist or open positions for this lane yet — the terminal will populate as the lane picks up symbols."
            : "Waiting for the live tape… this grid auto-populates from whatever symbols the backend is streaming (indices intraday; ATM + held legs once subscribed)."}
        </div>
      ) : (
        <div className="grid gap-3 lg:grid-cols-[1fr_340px]">
          <QuoteGrid symbols={symbols} labels={labels} onSelect={setSelected} selected={focus} />
          <DepthLadder symbol={focus} />
        </div>
      )}
    </div>
  );
}
