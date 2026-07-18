"use client";

/**
 * Lane Inventory — the registry-driven single lane list.
 *
 * Renders EVERY lane from GET /api/system/lanes (the ONE backend source of
 * truth), grouped by kind, each an honest status row: real status, execution
 * mode, broker profile / cadence, a plane + staleness indicator so a split
 * plane going stale is VISIBLE in the one UI, and a risk-breach badge
 * (surface-only). Lanes with an existing desk route deep-link to it; lanes
 * without one (monitors, daemons, data-plane runners) render as status rows.
 *
 * This is the backbone of "one UI over the split": it consumes core's
 * aggregated endpoint only and never needs to know if the backend is one
 * process or two.
 */
import Link from "next/link";
import { clsx } from "clsx";
import { ArrowUpRight, AlertTriangle } from "lucide-react";

import { MetricTile, StatusBadge, formatDuration } from "@/components/desk-ui";
import {
  isPlaneStale,
  laneRoute,
  laneStatusVariant,
  useLaneRegistry,
  type LaneSnapshot,
} from "@/hooks/useLaneRegistry";

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

function cadenceLabel(seconds?: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function ageSeconds(iso?: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

function LaneRow({ lane }: { lane: LaneSnapshot }) {
  const route = laneRoute(lane.key);
  const planeStale = isPlaneStale(lane);
  const age = ageSeconds(lane.last_success_at);

  const inner = (
    <div
      className={clsx(
        "flex flex-col gap-2 rounded-xl border px-3.5 py-2.5 transition-colors",
        route
          ? "border-bg-border bg-bg-secondary/22 hover:border-bg-active hover:bg-bg-secondary/35"
          : "border-bg-border/60 bg-bg-secondary/12",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-primary">
            <span className="truncate">{lane.label}</span>
            {route ? (
              <ArrowUpRight size={12} className="shrink-0 text-text-muted" />
            ) : null}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] uppercase tracking-[0.1em] text-text-muted">
            <span>{lane.execution_mode}</span>
            {lane.broker_profile ? <span>· {lane.broker_profile} REST</span> : null}
            {cadenceLabel(lane.cadence_seconds) ? (
              <span>· every {cadenceLabel(lane.cadence_seconds)}</span>
            ) : null}
            {age != null ? <span>· ok {formatDuration(age)} ago</span> : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusBadge label={lane.status} variant={laneStatusVariant(lane.status)} />
          {lane.risk_breach === true ? (
            <StatusBadge
              label="risk breach"
              variant="error"
              icon={<AlertTriangle size={11} />}
            />
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        {lane.plane ? (
          <span
            className={clsx(
              "inline-flex rounded-full border px-2 py-0.5 font-medium tracking-wide",
              planeStale
                ? "border-accent-amber/40 bg-accent-amber/10 text-accent-amber"
                : "border-bg-border bg-bg-primary/20 text-text-muted",
            )}
            title={
              planeStale
                ? "This lane's plane stopped publishing its status snapshot — shown stale, not falsely green."
                : "Owning process plane"
            }
          >
            {lane.foreign_plane ? "plane " : ""}
            {lane.plane}
            {planeStale ? " · snapshot stale" : ""}
          </span>
        ) : null}
        {lane.audit_coverage ? (
          <span className="inline-flex rounded-full border border-accent-blue/30 bg-accent-blue/10 px-2 py-0.5 font-medium tracking-wide text-accent-blue">
            audit-covered
          </span>
        ) : null}
        {lane.enabled_flag_name && lane.enabled === false ? (
          <span className="text-text-muted">flag off</span>
        ) : null}
        {lane.risk_breach_reason ? (
          <span className="text-accent-red">{lane.risk_breach_reason}</span>
        ) : null}
        {lane.last_error ? (
          <span className="truncate text-accent-red" title={lane.last_error}>
            {lane.last_error}
          </span>
        ) : null}
      </div>
    </div>
  );

  return route ? (
    <Link href={route} className="group block">
      {inner}
    </Link>
  ) : (
    <div>{inner}</div>
  );
}

export function LaneInventoryTab() {
  const { data, isError, isLoading } = useLaneRegistry();
  const lanes = data?.lanes ?? [];
  const summary = data?.summary;

  if (isError && !lanes.length) {
    return (
      <div className="rounded-2xl border border-bg-border bg-bg-secondary/20 px-4 py-6 text-sm text-text-muted">
        Could not reach the lane registry (`/api/system/lanes`).
      </div>
    );
  }

  const grouped = KIND_ORDER.map((kind) => ({
    kind,
    label: KIND_LABEL[kind] ?? kind,
    rows: lanes.filter((l) => l.kind === kind),
  })).filter((g) => g.rows.length);

  // Any lane the registry classifies under an unexpected kind still shows.
  const known = new Set(KIND_ORDER);
  const other = lanes.filter((l) => !known.has(l.kind));
  if (other.length) grouped.push({ kind: "other", label: "Other", rows: other });

  return (
    <div className="flex flex-col gap-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile
          label="Lanes"
          value={String(summary?.total ?? lanes.length)}
          detail="one registry, all planes"
        />
        <MetricTile
          label="Running / ready"
          value={String(
            (summary?.by_status?.running ?? 0) + (summary?.by_status?.ready ?? 0),
          )}
          detail="probed live"
        />
        <MetricTile
          label="Stale"
          value={String(summary?.by_status?.stale ?? 0)}
          detail="incl. foreign-plane"
        />
        <MetricTile
          label="Risk breaches"
          value={String(summary?.risk_breached ?? 0)}
          detail="surfaced, not enforced"
        />
      </section>

      {isLoading && !lanes.length ? (
        <div className="text-sm text-text-muted">Loading lane registry…</div>
      ) : null}

      {grouped.map((group) => (
        <section key={group.kind} className="flex flex-col gap-2">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
            {group.label}
            <span className="ml-2 font-normal text-text-muted/70">{group.rows.length}</span>
          </h3>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
            {group.rows.map((lane) => (
              <LaneRow key={lane.key} lane={lane} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
