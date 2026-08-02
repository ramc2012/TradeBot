"use client";

/**
 * /research — the canonical home of the backtest runner, the F&O data
 * ingest console, and the validation/research monitor. The old
 * /backtester, /data and /analysis routes redirect here with ?tab=…
 * preselected; the tab bodies import the components directly.
 */
import { clsx } from "clsx";
import { Database, FlaskConical, GitCompare } from "lucide-react";

import { useUrlTab } from "@/components/desk-ui";
import BacktesterDesk from "@/components/backtester/BacktesterDesk";
import DataIngestConsole from "@/components/data/DataIngestConsole";
import ResearchMonitorBoard from "@/components/research-monitor/ResearchMonitorBoard";

const TABS = [
  { key: "backtests",  label: "Backtests",  icon: GitCompare },
  { key: "data",       label: "Data ingest", icon: Database },
  { key: "validation", label: "Validation", icon: FlaskConical },
];

export default function ResearchPage() {
  const [tab, setTab] = useUrlTab("backtests");

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">Research</h1>
        <p className="mt-1 text-sm text-text-muted">
          Non-live surfaces. Backtests, F&O data ingestion, and
          validation reports — one page instead of three routes.
        </p>
      </header>

      <nav className="flex flex-wrap items-center gap-1 border-b border-bg-border/40">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12.5px] font-semibold transition-colors",
              tab === key ? "border-accent-blue text-text-primary" : "border-transparent text-text-muted hover:text-text-secondary",
            )}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </nav>

      {tab === "backtests" ? <BacktesterDesk /> : null}
      {tab === "data" ? <DataIngestConsole /> : null}
      {tab === "validation" ? <ResearchMonitorBoard /> : null}
    </div>
  );
}
