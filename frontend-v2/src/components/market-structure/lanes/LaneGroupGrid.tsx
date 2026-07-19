"use client";

/**
 * LaneGroupGrid — the 32 lanes, grouped on BOTH axes the design calls for.
 *
 *   (i)  KIND — strategy-engine / scheduler-runner / product-lane / monitor.
 *        This axis is SERVED by /api/system/lanes and is never re-derived here.
 *   (ii) HORIZON — scalp / intraday / swing / positional. This axis does not
 *        exist in any payload; it is a declared, cited table in
 *        lib/lane-taxonomy.ts, and the evidence for each classification is on
 *        the row's tooltip. Cadence is a POLL interval and is never used to
 *        infer a holding period.
 *
 * SCALP is rendered as a permanently unavailable group with its reason and the
 * two capability records behind it — not as an empty-but-possible bucket. That
 * distinction is the point: the gap is in the feed, not in the backlog.
 *
 * Lane state uses `laneDisplayStatus` / `laneDisplayVariant` from the shipped
 * registry hook verbatim, so "ready" still reads ARMED in blue here exactly as
 * it does on the lane-health board. No second status ladder is defined.
 */
import { clsx } from "clsx";
import { ArrowUpRight, Lock } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import {
  laneDisplayStatus,
  laneDisplayVariant,
  laneRoute,
  useLaneRegistry,
  type LaneSnapshot,
} from "@/hooks/useLaneRegistry";
import {
  groupLanesByHorizon,
  groupLanesByKind,
  laneHorizon,
  type LaneGroup,
} from "@/lib/lane-taxonomy";
import { missingCapability } from "@/lib/market-semantics";

type Axis = "kind" | "horizon";

export function LaneGroupGrid() {
  const [axis, setAxis] = useState<Axis>("horizon");
  const registry = useLaneRegistry();
  const lanes = useMemo(() => registry.data?.lanes ?? [], [registry.data]);

  const groups: LaneGroup<LaneSnapshot>[] = useMemo(
    () => (axis === "kind" ? groupLanesByKind(lanes) : groupLanesByHorizon(lanes)),
    [axis, lanes],
  );

  return (
    <section className="rounded-2xl border border-bg-border bg-bg-secondary/22 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[13px] font-semibold text-text-primary">Lane grouping</h2>
          <p className="mt-0.5 max-w-2xl text-[11px] leading-4 text-text-muted">
            Kind comes from the registry. Horizon does not exist in any payload — it is a declared
            table with the evidence on each row, because cadence is a poll interval, not a holding
            period.
          </p>
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-bg-border p-0.5">
          {(["kind", "horizon"] as Axis[]).map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => setAxis(a)}
              aria-pressed={axis === a}
              className={clsx(
                "rounded-md px-2.5 py-1 text-[11px] font-semibold transition-colors",
                axis === a ? "bg-bg-secondary/60 text-text-primary" : "text-text-muted hover:text-text-secondary",
              )}
            >
              by {a}
            </button>
          ))}
        </div>
      </div>

      {registry.isLoading ? (
        <p className="mt-3 text-[11.5px] text-text-muted">Loading the lane registry…</p>
      ) : registry.isError ? (
        <p className="mt-3 text-[11.5px] text-accent-red">
          /api/system/lanes did not answer — no lane inventory is shown rather than a stale one.
        </p>
      ) : !lanes.length ? (
        <p className="mt-3 text-[11.5px] text-text-muted">
          The registry answered with no lanes. Nothing is inferred to fill the grid.
        </p>
      ) : (
        <div className="mt-3 space-y-3">
          {groups.map((g) => (
            <GroupBlock key={g.id} group={g} axis={axis} />
          ))}
        </div>
      )}
    </section>
  );
}

function GroupBlock({ group, axis }: { group: LaneGroup<LaneSnapshot>; axis: Axis }) {
  const permanentlyUnavailable = group.unavailable != null;

  return (
    <div
      className={clsx(
        "rounded-xl border p-3",
        permanentlyUnavailable
          ? "border-bg-border/60 bg-bg-primary/10"
          : "border-bg-border/70 bg-bg-primary/15",
      )}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {permanentlyUnavailable ? <Lock size={12} className="text-text-muted" /> : null}
        <h3
          className={clsx(
            "text-[12px] font-semibold",
            permanentlyUnavailable
              ? "text-text-muted line-through decoration-text-muted/50"
              : "text-text-primary",
          )}
        >
          {group.label}
        </h3>
        {permanentlyUnavailable ? (
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted">
            permanently unavailable
          </span>
        ) : (
          <span className="font-mono text-[10.5px] text-text-muted">{group.lanes.length} lanes</span>
        )}
      </div>
      <p className="mt-0.5 max-w-3xl text-[10.5px] leading-4 text-text-muted">{group.blurb}</p>

      {permanentlyUnavailable ? (
        <div className="mt-2">
          <p className="max-w-prose text-[11px] leading-5 text-text-secondary/85">
            {group.unavailable!.reason}
          </p>
          <ul className="mt-1.5 space-y-0.5">
            {group.unavailable!.missingCapabilities.map(missingCapability).map((c) => (
              <li key={c.key} className="text-[10.5px] leading-4 text-text-muted">
                <span className="font-mono">{c.key}</span> — {c.label}
              </li>
            ))}
          </ul>
          <p className="mt-1.5 font-mono text-[10px] leading-4 text-text-muted/80">
            {group.unavailable!.citation}
          </p>
        </div>
      ) : group.lanes.length ? (
        <ul className="mt-2 grid gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
          {group.lanes.map((l) => (
            <LaneRow key={`${group.id}:${l.key}`} lane={l} axis={axis} />
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-[11px] text-text-muted">
          No lane is classified into this group today.
        </p>
      )}
    </div>
  );
}

function LaneRow({ lane, axis }: { lane: LaneSnapshot; axis: Axis }) {
  const route = laneRoute(lane.key);
  const horizon = laneHorizon(lane.key);
  return (
    <li className="rounded-lg border border-bg-border/60 bg-bg-secondary/20 px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11.5px] font-semibold text-text-secondary" title={lane.key}>
          {lane.label || lane.key}
        </span>
        <StatusBadge label={laneDisplayStatus(lane)} variant={laneDisplayVariant(lane)} />
      </div>
      <div
        className="mt-0.5 truncate text-[10px] text-text-muted"
        title={
          axis === "horizon"
            ? `horizon evidence: ${horizon.evidence}`
            : `kind is served by /api/system/lanes: ${lane.kind}`
        }
      >
        {axis === "horizon" ? horizon.evidence : `${lane.kind} · ${lane.execution_mode}`}
      </div>
      {route ? (
        <Link
          href={route}
          className="mt-1 inline-flex items-center gap-1 text-[10px] font-semibold text-text-muted transition-colors hover:text-text-primary"
        >
          open desk
          <ArrowUpRight size={10} />
        </Link>
      ) : null}
    </li>
  );
}
