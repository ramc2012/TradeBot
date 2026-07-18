"use client";

/**
 * LaneCard — the detailed, honest per-lane card.
 *
 * Extracted from LaneInventoryTab so the sortable LaneTable can reuse it
 * verbatim as an expandable row body (one rich card, not a 32-tile wall).
 *
 * Renders the runtime context the old card dropped: the HONEST status label
 * ("ready" → "Armed" when not running), exchange_session, last_message,
 * next_run_at, last scan age, and — for a foreign plane — the snapshot age. A
 * green "ready" card now says, in words, that it means "armed for the next
 * session", never a false "live".
 */
import Link from "next/link";
import { clsx } from "clsx";
import { ArrowUpRight, AlertTriangle } from "lucide-react";

import { StatusBadge, formatDuration } from "@/components/desk-ui";
import {
  isPlaneStale,
  laneDisplayStatus,
  laneDisplayVariant,
  laneRoute,
  type LaneSnapshot,
} from "@/hooks/useLaneRegistry";

export function cadenceLabel(seconds?: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function ageSeconds(iso?: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

/** "next 14h" style label for a future run time. */
export function untilLabel(iso?: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const secs = (t - Date.now()) / 1000;
  if (secs <= 0) return "due";
  return formatDuration(secs);
}

export function LaneCard({ lane, linkless = false }: { lane: LaneSnapshot; linkless?: boolean }) {
  const route = laneRoute(lane.key);
  const planeStale = isPlaneStale(lane);
  const scanAge = ageSeconds(lane.last_success_at);
  const nextIn = untilLabel(lane.next_run_at);
  const displayStatus = laneDisplayStatus(lane);
  const foreignAge =
    lane.foreign_plane && lane.snapshot_age_seconds != null
      ? lane.snapshot_age_seconds
      : null;

  const inner = (
    <div
      className={clsx(
        "flex flex-col gap-2 rounded-xl border px-3.5 py-2.5 transition-colors",
        route && !linkless
          ? "border-bg-border bg-bg-secondary/22 hover:border-bg-active hover:bg-bg-secondary/35"
          : "border-bg-border/60 bg-bg-secondary/12",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-primary">
            <span className="truncate">{lane.label}</span>
            {route ? <ArrowUpRight size={12} className="shrink-0 text-text-muted" /> : null}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] uppercase tracking-[0.1em] text-text-muted">
            <span>{lane.execution_mode}</span>
            {lane.broker_profile ? <span>· {lane.broker_profile} REST</span> : null}
            {cadenceLabel(lane.cadence_seconds) ? (
              <span>· every {cadenceLabel(lane.cadence_seconds)}</span>
            ) : null}
            {scanAge != null ? <span>· scan {formatDuration(scanAge)} ago</span> : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusBadge label={displayStatus} variant={laneDisplayVariant(lane)} />
          {lane.risk_breach === true ? (
            <StatusBadge label="risk breach" variant="error" icon={<AlertTriangle size={11} />} />
          ) : null}
        </div>
      </div>

      {/* Runtime context — armed-for-next-session, session window, last message. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-text-secondary">
        {lane.running === true ? (
          <span className="text-accent-green">running now</span>
        ) : displayStatus === "Armed" || displayStatus === "Enabled" ? (
          <span className="text-text-muted">
            armed for next session
            {nextIn ? ` · next ${nextIn}` : ""}
          </span>
        ) : nextIn ? (
          <span className="text-text-muted">next {nextIn}</span>
        ) : null}
        {lane.exchange_session ? (
          <span className="text-text-muted">· {lane.exchange_session}</span>
        ) : null}
        {foreignAge != null ? (
          <span className="text-accent-amber">
            · plane snapshot {formatDuration(foreignAge)} old
          </span>
        ) : null}
      </div>

      {lane.last_message ? (
        <div className="truncate text-[10.5px] text-text-muted" title={lane.last_message}>
          {lane.last_message}
        </div>
      ) : null}

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
        ) : (lane.execution_mode === "paper" || lane.execution_mode === "live") ? (
          <span
            className="inline-flex rounded-full border border-accent-amber/30 bg-accent-amber/10 px-2 py-0.5 font-medium tracking-wide text-accent-amber"
            title="Execution-capable lane with no audit coverage."
          >
            unaudited
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

  if (linkless || !route) return <div>{inner}</div>;
  return (
    <Link href={route} className="group block">
      {inner}
    </Link>
  );
}
