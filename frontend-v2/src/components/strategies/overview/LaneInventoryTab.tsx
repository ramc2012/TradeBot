"use client";

/**
 * Lane Inventory — the registry-driven single lane list.
 *
 * Renders EVERY lane from GET /api/system/lanes (the ONE backend source of
 * truth) with operational honesty as the whole point:
 *
 *   - Headline tallies SEPARATE Running / Armed / Parked / Attention — a green
 *     "ready" lane is ARMED for the next session, never counted as running.
 *   - A risk-coverage line shows evaluated N/total + unknown M, so "3 breaches"
 *     can't be misread as terminal-wide all-clear when ~28 lanes are unevaluated.
 *   - An exceptions-first strip lifts breaches/stale/errors/unaudited to the top.
 *   - A compact sortable/filterable table is the default (32 cards don't scale);
 *     the rich card is an expandable row. A grouped-card view stays behind a toggle.
 *
 * This is the backbone of "one UI over the split": it consumes core's
 * aggregated endpoint only and never needs to know if the backend is one
 * process or two.
 */
import { useState } from "react";
import { clsx } from "clsx";

import { MetricTile } from "@/components/desk-ui";
import { deriveLaneStats, useLaneRegistry } from "@/hooks/useLaneRegistry";

import { LaneCard } from "./LaneCard";
import { LaneExceptionsStrip } from "./LaneExceptionsStrip";
import { LaneTable } from "./LaneTable";

const KIND_ORDER: string[] = [
  "strategy-engine",
  "scheduler-runner",
  "product-lane",
  "monitor",
];

const KIND_LABEL: Record<string, string> = {
  "strategy-engine": "Strategy engines",
  "scheduler-runner": "Scheduled runners",
  "product-lane": "Product lanes",
  monitor: "Monitors & data plane",
};

export function LaneInventoryTab() {
  const { data, isError, isLoading } = useLaneRegistry();
  const lanes = data?.lanes ?? [];
  const [view, setView] = useState<"table" | "cards">("table");

  if (isError && !lanes.length) {
    return (
      <div className="rounded-2xl border border-bg-border bg-bg-secondary/20 px-4 py-6 text-sm text-text-muted">
        Could not reach the lane registry (`/api/system/lanes`).
      </div>
    );
  }

  const stats = deriveLaneStats(lanes);

  const grouped = KIND_ORDER.map((kind) => ({
    kind,
    label: KIND_LABEL[kind] ?? kind,
    rows: lanes.filter((l) => l.kind === kind),
  })).filter((g) => g.rows.length);
  const known = new Set(KIND_ORDER);
  const other = lanes.filter((l) => !known.has(l.kind));
  if (other.length) grouped.push({ kind: "other", label: "Other", rows: other });

  return (
    <div className="flex flex-col gap-4">
      {/* Honest headline: running vs armed vs parked vs attention — never merged. */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile
          label="Running"
          value={String(stats.running)}
          detail="active loops right now"
        />
        <MetricTile
          label="Armed"
          value={String(stats.armed)}
          detail="ready for next session"
        />
        <MetricTile
          label="Parked"
          value={String(stats.parked)}
          detail="disabled / parked"
        />
        <MetricTile
          label="Attention"
          value={String(stats.attention)}
          detail="breach · stale · error"
          color={stats.attention > 0 ? "text-accent-red" : undefined}
        />
      </section>

      {/* Risk coverage — so "3 breaches" is read as "3 of 4 evaluated; 28 unknown". */}
      <div
        className={clsx(
          "flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border px-3.5 py-2.5 text-[12px]",
          stats.riskUnknown > 0
            ? "border-accent-amber/30 bg-accent-amber/5"
            : "border-bg-border bg-bg-secondary/20",
        )}
      >
        <span className="font-semibold uppercase tracking-[0.14em] text-text-muted">
          Risk coverage
        </span>
        <span className="text-text-secondary">
          Evaluated <span className="font-mono text-text-primary">{stats.riskEvaluated}</span>/
          {stats.total}
        </span>
        <span className={stats.riskBreached > 0 ? "text-accent-red" : "text-text-secondary"}>
          <span className="font-mono">{stats.riskBreached}</span> breach
        </span>
        <span className={stats.riskUnknown > 0 ? "text-accent-amber" : "text-text-secondary"}>
          <span className="font-mono">{stats.riskUnknown}</span> unknown
        </span>
        <span className="text-text-muted">
          {stats.execUncovered} execution-capable lane{stats.execUncovered === 1 ? "" : "s"} unaudited
        </span>
      </div>

      {/* Exceptions first — problems before the trader scrolls. */}
      <LaneExceptionsStrip lanes={lanes} />

      {isLoading && !lanes.length ? (
        <div className="text-sm text-text-muted">Loading lane registry…</div>
      ) : null}

      <div className="flex items-center justify-end gap-1.5">
        <span className="text-[11px] uppercase tracking-[0.14em] text-text-muted">View</span>
        {(["table", "cards"] as const).map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => setView(v)}
            className={clsx(
              "rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] transition-colors",
              view === v
                ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:text-text-primary",
            )}
          >
            {v}
          </button>
        ))}
      </div>

      {view === "table" ? (
        <LaneTable lanes={lanes} />
      ) : (
        grouped.map((group) => (
          <section key={group.kind} className="flex flex-col gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
              {group.label}
              <span className="ml-2 font-normal text-text-muted/70">{group.rows.length}</span>
            </h3>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
              {group.rows.map((lane) => (
                <LaneCard key={lane.key} lane={lane} />
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
