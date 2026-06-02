"use client";

/**
 * NSE Strategy 1 desk — canonical merged page (2026-06-02).
 *
 * The former dense "live view" and the classic /strategy workspace were
 * merged into a single tabbed NSE-S1 desk. /strategy now redirects here.
 * All rendering lives in the NseStrategyDesk component.
 */
import NseStrategyDesk from "@/components/strategy/NseStrategyDesk";

export default function StrategyLivePage() {
  return <NseStrategyDesk />;
}
