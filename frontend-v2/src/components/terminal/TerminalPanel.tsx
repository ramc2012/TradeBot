"use client";

/**
 * TerminalPanel — the live cockpit. A self-populating quote grid (driven by the
 * low-latency /ws/quotes tape) on the left, a 5-level depth ladder for the
 * focused symbol on the right. Drop this into any desk as a "Terminal" tab.
 *
 * The grid auto-populates from whatever symbols the backend is actually
 * streaming (useKnownSymbols), so there is no hardcoded broker symbol list.
 */
import { useMemo, useState } from "react";

import { useKnownSymbols } from "@/hooks/useQuoteStore";
import { QuoteGrid } from "./QuoteGrid";
import { DepthLadder } from "./DepthLadder";
import { QuoteConnectionBadge } from "./LiveMarkBadge";

function shortLabel(sym: string): string {
  // "NSE:NIFTY50-INDEX" → "NIFTY50-INDEX"
  return sym.includes(":") ? sym.split(":").slice(1).join(":") : sym;
}

export function TerminalPanel() {
  const symbols = useKnownSymbols();
  const [selected, setSelected] = useState<string | null>(null);
  const labels = useMemo(
    () => Object.fromEntries(symbols.map((s) => [s, shortLabel(s)])),
    [symbols],
  );
  const focus = selected ?? symbols[0] ?? null;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[13px] font-semibold text-text-primary">Live Terminal</div>
          <div className="text-[11px] text-text-muted">
            Per-tick quote tape · click a row for its depth ladder
          </div>
        </div>
        <QuoteConnectionBadge />
      </div>

      {symbols.length === 0 ? (
        <div className="rounded-xl border border-bg-border bg-bg-card/40 p-8 text-center text-[12px] text-text-muted">
          Waiting for the live tape… this grid auto-populates from whatever symbols
          the backend is streaming (indices intraday; ATM + held legs once subscribed).
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
