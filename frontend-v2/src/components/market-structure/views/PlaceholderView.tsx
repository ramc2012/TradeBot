"use client";

/**
 * Placeholder views — honest scaffolding for the two views that are NOT built.
 *
 * Two views remain: Risk & Execution and Research. They echo the pinned context
 * (so switching views visibly keeps the pin) and deep-link into the existing
 * desk that serves that function today. They render
 * no data, because a half-built panel is indistinguishable from a broken one,
 * and a terminal whose panels might be either is worse than one with a gap.
 */
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";

import { StatusBadge } from "@/components/desk-ui";

import { VIEW_LABEL, deskHref, describeContext, type WorkspaceContext } from "../context/schema";

type Plan = { blurb: string; sources: string; desks: Array<{ label: string; path: string }> };

/**
 * ONLY the views that are genuinely not built. `command`, `structure`, `flow`
 * and `strategies` have real components; keeping a plan entry for a built view
 * would let a future edit route a working view back into this scaffold.
 */
export type PlaceholderViewKey = "risk" | "research";

const PLAN: Record<PlaceholderViewKey, Plan> = {
  risk: {
    blurb:
      "Plan completeness, sizing inputs, exposure and the execution path — including why a plan is not actionable.",
    sources: "/api/trading/risk-status, positions, and each lane's risk block",
    desks: [
      { label: "Execution", path: "/trading" },
      { label: "Positions", path: "/positions" },
    ],
  },
  research: {
    blurb: "Replay, backtests and walk-forwards over the same pinned context and time frontier.",
    sources: "the research lab and backtester endpoints",
    desks: [
      { label: "Research lab", path: "/research" },
      { label: "Portfolio", path: "/analytics" },
    ],
  },
};

export function PlaceholderView({ view, ctx }: { view: PlaceholderViewKey; ctx: WorkspaceContext }) {
  const plan = PLAN[view];
  return (
    <section className="rounded-2xl border border-bg-border bg-bg-secondary/22 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold text-text-primary">{VIEW_LABEL[view]}</h2>
        <StatusBadge label="not built yet" variant="neutral" />
      </div>
      <p className="mt-2 max-w-2xl text-[12.5px] text-text-muted">{plan.blurb}</p>
      <p className="mt-1 max-w-2xl text-[11.5px] text-text-muted">
        Will be composed from {plan.sources}. Nothing is rendered here until it is real.
      </p>

      <div className="mt-3 rounded-xl border border-bg-border/70 bg-bg-primary/20 px-3 py-2">
        <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Pinned context</div>
        <div className="mt-0.5 font-mono text-[11.5px] text-text-secondary">{describeContext(ctx)}</div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {plan.desks.map((d) => (
          <Link
            key={d.path}
            href={deskHref(d.path, ctx)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border px-2.5 py-1 text-[11.5px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary"
          >
            {d.label}
            <ArrowUpRight size={12} />
          </Link>
        ))}
      </div>
    </section>
  );
}
