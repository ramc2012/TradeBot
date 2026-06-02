"use client";

/**
 * NSE Strategy 1 desk — canonical merged page (2026-06-02).
 *
 * The former dense "live view" and the classic NSE strategy workspace were
 * merged into a single tabbed NSE-S1 desk. /strategies/nse redirects here.
 * All rendering lives in the NseStrategyDesk component.
 */
import NseStrategyDesk from "@/components/v1-strategy/NseStrategyDesk";

export default function NseStrategyLivePage() {
  return <NseStrategyDesk />;
}
