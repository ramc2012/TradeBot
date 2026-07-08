"use client";

/**
 * LiveMarkCell — a terminal-grade live price for a single table cell.
 *
 * Subscribes to ONE tape symbol via `useQuote` and renders its live LTP with the
 * green/red flash + a live dot when a tick is present; otherwise it falls back to
 * the row's static snapshot value (so nothing regresses for symbols the backend
 * isn't streaming yet). This is the primitive that makes the watchlist/positions
 * tables themselves "terminal-like" — no separate Terminal tab needed.
 *
 * Memoized leaf: one symbol's tick re-renders only its own cell, not the table.
 */
import { memo } from "react";

import { formatNumber } from "@/components/desk-ui";
import { useQuote } from "@/hooks/useQuoteStore";
import { usePriceFlash } from "@/hooks/usePriceFlash";

export const LiveMarkCell = memo(function LiveMarkCell({
  symbol,
  fallback,
  decimals = 2,
  prefix = "",
}: {
  /** Broker/tape symbol (namespaced) to stream; null/undefined → fallback only. */
  symbol?: string | null;
  /** Static snapshot value shown when there's no live tick. */
  fallback?: number | null;
  decimals?: number;
  prefix?: string;
}) {
  const q = useQuote(symbol || undefined);
  const live = q?.ltp != null;
  const flash = usePriceFlash(live ? q?.ltp : undefined);
  const value = live ? (q?.ltp as number) : fallback;
  const flashBg = flash === "up" ? "bg-accent-green/20" : flash === "down" ? "bg-accent-red/20" : "";
  return (
    <span
      className={`inline-flex items-center justify-end gap-1 rounded px-1 font-mono tabular-nums transition-colors ${flashBg}`}
      title={live ? "live tape" : undefined}
    >
      {value != null ? `${prefix}${formatNumber(value, decimals)}` : "—"}
      {live ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent-green" /> : null}
    </span>
  );
});
