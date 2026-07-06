"use client";

/**
 * LaneTerminal — a lane-scoped wrapper over TerminalPanel. Give it a desk's
 * watchlist and open-position rows; it derives the live-tape symbols those rows
 * reference (underlyings → index symbols, plus any option/future legs) and
 * renders the live cockpit for just that lane. One line per desk.
 */
import { useMemo } from "react";

import { laneTapeSymbols } from "@/lib/marketSymbols";
import { TerminalPanel } from "./TerminalPanel";

export function LaneTerminal({
  watchlist,
  positions,
  underlyings,
  title,
  subtitle,
}: {
  watchlist?: readonly unknown[] | null;
  positions?: readonly unknown[] | null;
  /** Plain underlying-name universe (e.g. ["NIFTY","BANKNIFTY"]); mapped to
   *  their index tape symbols. Use for desks that expose a symbol list. */
  underlyings?: readonly string[] | null;
  title?: string;
  subtitle?: string;
}) {
  const symbols = useMemo(
    () =>
      laneTapeSymbols(
        watchlist,
        positions,
        (underlyings ?? []).map((u) => ({ underlying: u })),
      ),
    [watchlist, positions, underlyings],
  );
  return <TerminalPanel symbols={symbols} title={title} subtitle={subtitle} />;
}
