"use client";

/**
 * Honesty layer for the terminal: shows whether the live tape is connected and,
 * per-symbol, how fresh the last value is — so a coalesced/stale frame is never
 * silently presented as a raw real-time print. green=live · amber=stale · red=offline.
 */
import { useEffect, useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import { useQuote, useQuotesConnection } from "@/hooks/useQuoteStore";

/** Global connection state of the shared /ws/quotes socket. */
export function QuoteConnectionBadge() {
  const connected = useQuotesConnection();
  return (
    <StatusBadge
      label={connected ? "● live tape" : "tape offline"}
      variant={connected ? "success" : "warn"}
    />
  );
}

/** Per-symbol freshness: stale after `staleMs` since the last received tick. */
export function LiveMarkBadge({ symbol, staleMs = 4000 }: { symbol: string; staleMs?: number }) {
  const quote = useQuote(symbol);
  const connected = useQuotesConnection();
  // Re-tick once a second so the age verdict updates even without new ticks.
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  if (!connected) return <StatusBadge label="offline" variant="warn" />;
  if (!quote || quote.rxAt == null) return <StatusBadge label="—" variant="info" />;

  const ageS = Math.max(0, (Date.now() - quote.rxAt) / 1000);
  const stale = ageS * 1000 > staleMs;
  const label = `${stale ? "stale " : ""}${ageS < 1 ? "<1" : Math.round(ageS)}s${quote.coalesced ? " ·c" : ""}`;
  return <StatusBadge label={label} variant={stale ? "warn" : "success"} />;
}
