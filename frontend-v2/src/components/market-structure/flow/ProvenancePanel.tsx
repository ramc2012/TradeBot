"use client";

/**
 * ProvenancePanel — the ONE wrapper every Flow panel is rendered inside.
 *
 * Rule: no panel in this view carries a bespoke honesty badge. The header is
 * built by `provenanceOf()` from the shared semantic contract, so source ·
 * grade · storage mode · aggregation · completeness · age · data mode are
 * derived once and cannot disagree between two panels reading the same field.
 *
 * Three visually DISTINCT states, which is the whole point:
 *   · data present        → the panel body, with its provenance caption
 *   · nothing measured    → "measured zero" is the body's job, not this
 *                           wrapper's; a zero is a value and renders as one
 *   · no source at all    → `unavailable` prose, neutral, no timestamp and no
 *                           refresh affordance, because there is nothing to
 *                           refresh
 *
 * A structurally-absent CAPABILITY is a fourth state and has its own component
 * (`CapabilityAbsentCard`) — it must never look like "stale" or "loading".
 */
import { clsx } from "clsx";

import { ProvenanceChip, StatusBadge } from "@/components/desk-ui";
import { OfSourceBadge } from "@/components/mpof";
import { provenanceOf, type MarketFeature, type DataMode } from "@/lib/market-semantics";

export function ProvenancePanel({
  title,
  icon,
  description,
  source,
  feature = "flow_attribution",
  asOf,
  timeframe,
  dataMode,
  have,
  expect,
  completenessLabel,
  unavailable,
  showOfBadge = true,
  rightSlot,
  className,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  description?: string;
  source?: string | null;
  /** Defaults to flow attribution — the honest grade for this whole view. */
  feature?: MarketFeature;
  asOf?: string | number | Date | null;
  timeframe?: string | null;
  dataMode?: DataMode;
  have?: number | null;
  expect?: number | null;
  completenessLabel?: string | null;
  /** Non-null ⇒ the panel has NO source and renders the statement instead. */
  unavailable?: string | null;
  showOfBadge?: boolean;
  rightSlot?: React.ReactNode;
  className?: string;
  children?: React.ReactNode;
}) {
  const provenance = provenanceOf({
    source,
    feature,
    asOf,
    timeframe,
    dataMode,
    have,
    expect,
    completenessLabel,
  });

  return (
    <section
      className={clsx("rounded-2xl border border-bg-border bg-bg-secondary/24 p-4", className)}
    >
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[13px] font-semibold text-text-primary">
            {icon}
            {title}
          </div>
          {description ? (
            <p className="mt-1 max-w-prose text-[11px] leading-4 text-text-muted">{description}</p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          {rightSlot}
          {unavailable ? (
            <StatusBadge label="no source" variant="neutral" />
          ) : showOfBadge ? (
            <OfSourceBadge source={source} size="sm" />
          ) : null}
        </div>
      </div>

      {unavailable ? (
        <div className="rounded-xl border border-dashed border-bg-border/70 px-3 py-4">
          <p className="max-w-prose text-[11.5px] leading-5 text-text-muted">{unavailable}</p>
        </div>
      ) : (
        <>
          {children}
          <ProvenanceChip provenance={provenance} density="caption" />
        </>
      )}
    </section>
  );
}
