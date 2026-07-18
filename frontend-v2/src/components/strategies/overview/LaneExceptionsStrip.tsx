"use client";

/**
 * LaneExceptionsStrip — problems first.
 *
 * Two MACD Refined risk breaches used to sit below the fold, buried among 17
 * runner cards. This strip lifts every actionable exception to the very top so
 * the trader sees trouble before scrolling:
 *
 *   breaches                    risk_breach === true
 *   stale planes                snapshot_stale || status === "stale"
 *   errors                      status === "error" || last_error
 *   unknown risk                risk_breach == null (the ~28 never-evaluated)
 *   unaudited execution-capable execution_mode ∈ {paper,live} && !audit_coverage
 *
 * Each group renders only when non-empty; each chip deep-links to the lane's
 * desk (laneRoute) where one exists. Unknown-risk is surfaced as its own group,
 * NOT folded into "attention", so the count reads as coverage-gap, not alarm.
 */
import Link from "next/link";
import { clsx } from "clsx";
import { AlertTriangle, Clock, HelpCircle, ShieldQuestion, XCircle } from "lucide-react";

import { isExecUncovered, laneRoute, type LaneSnapshot } from "@/hooks/useLaneRegistry";

type Group = {
  key: string;
  label: string;
  tone: string;
  icon: React.ReactNode;
  lanes: LaneSnapshot[];
  title: string;
};

function Chip({ lane, tone }: { lane: LaneSnapshot; tone: string }) {
  const route = laneRoute(lane.key);
  const title = lane.risk_breach_reason || lane.last_error || lane.last_message || lane.label;
  const body = (
    <span
      className={clsx(
        "inline-flex max-w-[220px] items-center gap-1 truncate rounded-full border px-2 py-0.5 text-[10.5px] font-medium tracking-wide",
        tone,
        route ? "hover:brightness-125" : "",
      )}
      title={title}
    >
      <span className="truncate">{lane.label}</span>
    </span>
  );
  return route ? (
    <Link href={route} className="block">
      {body}
    </Link>
  ) : (
    body
  );
}

export function LaneExceptionsStrip({ lanes }: { lanes: LaneSnapshot[] }) {
  const groups: Group[] = [
    {
      key: "breach",
      label: "Risk breaches",
      tone: "border-accent-red/40 bg-accent-red/10 text-accent-red",
      icon: <AlertTriangle size={12} className="text-accent-red" />,
      lanes: lanes.filter((l) => l.risk_breach === true),
      title: "Lanes whose risk snapshot is in breach (surface-only, not enforced here).",
    },
    {
      key: "stale",
      label: "Stale planes",
      tone: "border-accent-amber/40 bg-accent-amber/10 text-accent-amber",
      icon: <Clock size={12} className="text-accent-amber" />,
      lanes: lanes.filter((l) => Boolean(l.snapshot_stale) || l.status === "stale"),
      title: "Lanes whose owning plane stopped publishing a fresh status snapshot.",
    },
    {
      key: "error",
      label: "Errors",
      tone: "border-accent-red/40 bg-accent-red/10 text-accent-red",
      icon: <XCircle size={12} className="text-accent-red" />,
      lanes: lanes.filter((l) => l.status === "error" || Boolean(l.last_error)),
      title: "Lanes reporting an error status or a last_error.",
    },
    {
      key: "exec-uncovered",
      label: "Unaudited & execution-capable",
      tone: "border-accent-amber/40 bg-accent-amber/10 text-accent-amber",
      icon: <ShieldQuestion size={12} className="text-accent-amber" />,
      lanes: lanes.filter(isExecUncovered),
      title: "Lanes that can place (paper/live) orders but have NO audit coverage.",
    },
    {
      key: "unknown-risk",
      label: "Risk unknown",
      tone: "border-bg-border bg-bg-secondary/40 text-text-secondary",
      icon: <HelpCircle size={12} className="text-text-muted" />,
      lanes: lanes.filter((l) => l.risk_breach == null),
      title:
        "Lanes whose risk was never evaluated — most are monitors/daemons where risk is N/A, but the gap is shown, not hidden as all-clear.",
    },
  ].filter((g) => g.lanes.length > 0);

  if (!groups.length) {
    return (
      <div className="rounded-xl border border-accent-green/25 bg-accent-green/5 px-3.5 py-2 text-[12px] text-accent-green">
        No exceptions — no breaches, stale planes, errors, or unaudited execution lanes.
      </div>
    );
  }

  return (
    <section className="flex flex-col gap-2 rounded-xl border border-bg-border bg-bg-secondary/20 px-3.5 py-3">
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
        <AlertTriangle size={12} className="text-accent-amber" />
        Exceptions first
      </div>
      <div className="flex flex-col gap-2">
        {groups.map((g) => (
          <div key={g.key} className="flex flex-wrap items-center gap-1.5">
            <span
              className="inline-flex items-center gap-1 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-text-secondary"
              title={g.title}
            >
              {g.icon}
              {g.label}
              <span className="text-text-muted">{g.lanes.length}</span>
            </span>
            {g.lanes.map((l) => (
              <Chip key={`${g.key}-${l.key}`} lane={l} tone={g.tone} />
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}
