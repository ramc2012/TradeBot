"use client";

/**
 * LaneTable — the default, scalable lane view.
 *
 * 32 detailed cards do not scale; this compact sortable table is the default,
 * with the rich LaneCard as an expandable row body (click a row to open it).
 *
 * Columns: Lane · Kind · Status · Run/Armed · Risk · Audit · Cadence · Last scan · Plane.
 * Filter pills narrow the set: Attention · Running · Armed · Parked · Risk breach ·
 * Stale · Audit uncovered · Execution-capable. Default sort is attention-first so
 * problems surface at the top even before a filter is chosen.
 */
import { Fragment, useMemo, useState } from "react";
import { clsx } from "clsx";
import { AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";

import { StatusBadge } from "@/components/desk-ui";
import {
  deriveLaneStats,
  isExecUncovered,
  isLaneArmed,
  isLaneAttention,
  isLaneParked,
  laneDisplayStatus,
  laneDisplayVariant,
  type LaneSnapshot,
} from "@/hooks/useLaneRegistry";

import { LaneCard, ageSeconds, cadenceLabel } from "./LaneCard";

type SortKey = "lane" | "kind" | "status" | "risk" | "audit" | "cadence" | "scan" | "plane";
type SortDir = "asc" | "desc";

type FilterKey =
  | "attention"
  | "running"
  | "armed"
  | "parked"
  | "breach"
  | "stale"
  | "audit-uncovered"
  | "execution-capable";

const FILTERS: { key: FilterKey; label: string; pred: (l: LaneSnapshot) => boolean }[] = [
  { key: "attention", label: "Attention", pred: isLaneAttention },
  { key: "running", label: "Running", pred: (l) => l.running === true },
  { key: "armed", label: "Armed", pred: isLaneArmed },
  { key: "parked", label: "Parked", pred: isLaneParked },
  { key: "breach", label: "Risk breach", pred: (l) => l.risk_breach === true },
  { key: "stale", label: "Stale", pred: (l) => Boolean(l.snapshot_stale) || l.status === "stale" },
  { key: "audit-uncovered", label: "Audit uncovered", pred: (l) => !l.audit_coverage },
  {
    key: "execution-capable",
    label: "Execution-capable",
    pred: (l) => l.execution_mode === "paper" || l.execution_mode === "live",
  },
];

function runArmedLabel(l: LaneSnapshot): { label: string; variant: "success" | "info" | "neutral" } {
  if (l.running === true) return { label: "running", variant: "success" };
  if (isLaneArmed(l)) return { label: "armed", variant: "info" };
  if (isLaneParked(l)) return { label: "parked", variant: "neutral" };
  return { label: "—", variant: "neutral" };
}

function riskCell(l: LaneSnapshot): { label: string; variant: "error" | "success" | "neutral" } {
  if (l.risk_breach === true) return { label: "breach", variant: "error" };
  if (l.risk_breach === false) return { label: "ok", variant: "success" };
  return { label: "unknown", variant: "neutral" };
}

function scanEpoch(l: LaneSnapshot): number {
  const t = l.last_success_at ? new Date(l.last_success_at).getTime() : NaN;
  return Number.isNaN(t) ? 0 : t;
}

function compare(a: LaneSnapshot, b: LaneSnapshot, key: SortKey): number {
  switch (key) {
    case "lane":
      return a.label.localeCompare(b.label);
    case "kind":
      return String(a.kind).localeCompare(String(b.kind));
    case "status":
      return laneDisplayStatus(a).localeCompare(laneDisplayStatus(b));
    case "risk": {
      const rank = (l: LaneSnapshot) => (l.risk_breach === true ? 2 : l.risk_breach === false ? 1 : 0);
      return rank(a) - rank(b);
    }
    case "audit":
      return Number(Boolean(a.audit_coverage)) - Number(Boolean(b.audit_coverage));
    case "cadence":
      return (a.cadence_seconds ?? Infinity) - (b.cadence_seconds ?? Infinity);
    case "scan":
      return scanEpoch(a) - scanEpoch(b);
    case "plane":
      return String(a.plane ?? "").localeCompare(String(b.plane ?? ""));
    default:
      return 0;
  }
}

function Th({
  label,
  sortKey,
  active,
  dir,
  onSort,
  className,
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  className?: string;
}) {
  return (
    <th className={clsx("pb-2 pr-3 font-medium", className)}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={clsx(
          "inline-flex items-center gap-1 uppercase tracking-[0.1em] transition-colors hover:text-text-primary",
          active ? "text-text-primary" : "text-text-muted",
        )}
      >
        {label}
        {active ? <span className="text-[9px]">{dir === "asc" ? "▲" : "▼"}</span> : null}
      </button>
    </th>
  );
}

export function LaneTable({ lanes }: { lanes: LaneSnapshot[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("scan");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [filters, setFilters] = useState<Set<FilterKey>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [attentionFirst, setAttentionFirst] = useState(true);

  const stats = useMemo(() => deriveLaneStats(lanes), [lanes]);

  function onSort(k: SortKey) {
    if (k === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(k);
      setSortDir("asc");
    }
    setAttentionFirst(false);
  }

  function toggleFilter(k: FilterKey) {
    setFilters((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  function toggleRow(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const rows = useMemo(() => {
    const active = FILTERS.filter((f) => filters.has(f.key));
    const filtered = lanes.filter((l) => active.every((f) => f.pred(l)));
    const sorted = [...filtered].sort((a, b) => {
      if (attentionFirst) {
        const attn = Number(isLaneAttention(b)) - Number(isLaneAttention(a));
        if (attn !== 0) return attn;
        return String(a.kind).localeCompare(String(b.kind)) || a.label.localeCompare(b.label);
      }
      const c = compare(a, b, sortKey);
      return sortDir === "asc" ? c : -c;
    });
    return sorted;
  }, [lanes, filters, sortKey, sortDir, attentionFirst]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {FILTERS.map((f) => {
          const on = filters.has(f.key);
          const count = lanes.filter(f.pred).length;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => toggleFilter(f.key)}
              className={clsx(
                "rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] transition-colors",
                on
                  ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                  : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
              )}
            >
              {f.label}
              <span className="ml-1 text-text-muted">{count}</span>
            </button>
          );
        })}
        {filters.size > 0 ? (
          <button
            type="button"
            onClick={() => {
              setFilters(new Set());
              setAttentionFirst(true);
            }}
            className="rounded-full border border-bg-border bg-bg-secondary/25 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] text-text-muted hover:text-text-primary"
          >
            Clear
          </button>
        ) : null}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] text-left text-[12px]">
          <thead>
            <tr className="border-b border-bg-border text-text-muted">
              <th className="pb-2 pr-2" />
              <Th label="Lane" sortKey="lane" active={!attentionFirst && sortKey === "lane"} dir={sortDir} onSort={onSort} />
              <Th label="Kind" sortKey="kind" active={!attentionFirst && sortKey === "kind"} dir={sortDir} onSort={onSort} />
              <Th label="Status" sortKey="status" active={!attentionFirst && sortKey === "status"} dir={sortDir} onSort={onSort} />
              <Th label="Run/Armed" sortKey="status" active={false} dir={sortDir} onSort={onSort} />
              <Th label="Risk" sortKey="risk" active={!attentionFirst && sortKey === "risk"} dir={sortDir} onSort={onSort} />
              <Th label="Audit" sortKey="audit" active={!attentionFirst && sortKey === "audit"} dir={sortDir} onSort={onSort} />
              <Th label="Cadence" sortKey="cadence" active={!attentionFirst && sortKey === "cadence"} dir={sortDir} onSort={onSort} />
              <Th label="Last scan" sortKey="scan" active={!attentionFirst && sortKey === "scan"} dir={sortDir} onSort={onSort} />
              <Th label="Plane" sortKey="plane" active={!attentionFirst && sortKey === "plane"} dir={sortDir} onSort={onSort} />
            </tr>
          </thead>
          <tbody>
            {rows.map((l) => {
              const isOpen = expanded.has(l.key);
              const ra = runArmedLabel(l);
              const risk = riskCell(l);
              const scan = ageSeconds(l.last_success_at);
              const attn = isLaneAttention(l);
              return (
                <Fragment key={l.key}>
                  <tr
                    className={clsx(
                      "cursor-pointer border-b border-bg-border/40 transition-colors hover:bg-bg-secondary/25",
                      attn ? "bg-accent-red/[0.04]" : "",
                    )}
                    onClick={() => toggleRow(l.key)}
                  >
                    <td className="py-2 pr-2 align-middle text-text-muted">
                      {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    </td>
                    <td className="py-2 pr-3 align-middle">
                      <div className="flex items-center gap-1.5 font-medium text-text-primary">
                        {attn ? <AlertTriangle size={11} className="shrink-0 text-accent-red" /> : null}
                        <span className="truncate">{l.label}</span>
                      </div>
                    </td>
                    <td className="py-2 pr-3 align-middle text-text-muted">{l.kind}</td>
                    <td className="py-2 pr-3 align-middle">
                      <StatusBadge label={laneDisplayStatus(l)} variant={laneDisplayVariant(l)} />
                    </td>
                    <td className="py-2 pr-3 align-middle">
                      <StatusBadge label={ra.label} variant={ra.variant} />
                    </td>
                    <td className="py-2 pr-3 align-middle">
                      <StatusBadge label={risk.label} variant={risk.variant} />
                    </td>
                    <td className="py-2 pr-3 align-middle text-text-muted">
                      {l.audit_coverage ? (
                        <span className="text-accent-blue">covered</span>
                      ) : l.execution_mode === "paper" || l.execution_mode === "live" ? (
                        <span className="text-accent-amber">uncovered</span>
                      ) : (
                        <span>n/a</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 align-middle text-text-muted">
                      {cadenceLabel(l.cadence_seconds) ?? "—"}
                    </td>
                    <td className="py-2 pr-3 align-middle text-text-muted">
                      {scan != null ? `${Math.round(scan / 60)}m ago` : "—"}
                    </td>
                    <td className="py-2 pr-3 align-middle text-text-muted">
                      {l.plane ?? "—"}
                      {l.snapshot_stale ? <span className="text-accent-amber"> · stale</span> : null}
                    </td>
                  </tr>
                  {isOpen ? (
                    <tr className="border-b border-bg-border/40">
                      <td />
                      <td colSpan={9} className="py-2 pr-3">
                        <div className="max-w-2xl">
                          <LaneCard lane={l} />
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-8 text-center text-text-muted">
                  No lanes match the current filters.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="text-[11px] text-text-muted">
        Showing {rows.length} of {stats.total} lanes · click a row for full runtime context.
      </div>
    </div>
  );
}
