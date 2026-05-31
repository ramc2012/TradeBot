"use client";

/**
 * /research — consolidates v1's /analysis (validation reports +
 * backtests), /backtester (manual backtest runner), and /data (F&O
 * data ingest console) into a single tabbed surface.
 *
 * The v1 routes redirect into this page with ?tab=… preselected.
 */
import { useState } from "react";
import { clsx } from "clsx";
import { Database, FlaskConical, GitCompare } from "lucide-react";

import { Section, useUrlTab } from "@/components/desk-ui";
import PageStub from "@/components/PageStub";

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

      {tab === "backtests" ? (
        <PageStub
          title="Backtests"
          description="Manual backtest runner with walk-forward windows."
          v1Href="http://localhost:3000/backtester"
          v1Label="Open backtester in v1"
        />
      ) : null}

      {tab === "data" ? (
        <PageStub
          title="F&O data ingest"
          description="Start / monitor option-candle and index-analytics downloads."
          v1Href="http://localhost:3000/data"
          v1Label="Open data console in v1"
        />
      ) : null}

      {tab === "validation" ? (
        <PageStub
          title="Validation reports"
          description="Walk-forward + Greeks-sync validation against the MACD-on-premium thesis."
          v1Href="http://localhost:3000/analysis"
          v1Label="Open validation in v1"
        />
      ) : null}
    </div>
  );
}
