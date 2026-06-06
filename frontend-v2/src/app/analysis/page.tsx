"use client";

/**
 * /analysis — Research Monitor.
 *
 * Native v2 surface (replaces the v1 embed). Renders the
 * ResearchMonitorBoard: research-cache population, the live validation
 * report, the MACD option-study runner/monitor, and the Greeks-Sync
 * research track. The default export is consumed both standalone at
 * /analysis and as the "Validation" tab inside /research, so the
 * signature must stay a no-prop default component.
 */
import ResearchMonitorBoard from "@/components/research-monitor/ResearchMonitorBoard";

export default function AnalysisPage() {
  return <ResearchMonitorBoard />;
}
