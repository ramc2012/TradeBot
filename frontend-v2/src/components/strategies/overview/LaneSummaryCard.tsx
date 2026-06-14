"use client";

/**
 * One lane's at-a-glance card on the Strategies Overview.
 *
 * Whole card is a Link to the lane's existing v2 route. Renders running/idle
 * status, last-scan age, live open count + day/open P&L, latest signal
 * (direction + confidence + symbol) and regime when available. Every field is
 * optional — missing values degrade to "—" / "idle" / "no signal".
 */
import Link from "next/link";
import { clsx } from "clsx";
import { ArrowUpRight } from "lucide-react";

import {
  StatusBadge,
  formatDuration,
  formatPct,
  formatSignedMoney,
  regimeTone,
  tone,
} from "@/components/desk-ui";
import type { LaneSignal, LaneView } from "./types";

function ageSeconds(iso?: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

function dirTone(direction?: string | null): string {
  const s = String(direction ?? "").toLowerCase();
  if (s.includes("ce") || s.includes("bull") || s.includes("long") || s.includes("buy") || s.includes("up"))
    return "text-accent-green";
  if (s.includes("pe") || s.includes("bear") || s.includes("short") || s.includes("sell") || s.includes("down"))
    return "text-accent-red";
  return "text-text-secondary";
}

function SignalLine({ signal }: { signal?: LaneSignal | null }) {
  const hasRead = signal && (signal.direction || signal.state);
  if (!hasRead) {
    return <span className="text-[11.5px] text-text-muted">no signal</span>;
  }
  const dir = signal?.direction;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px]">
      {dir ? (
        <span className={clsx("font-semibold uppercase tracking-wide", dirTone(dir))}>{dir}</span>
      ) : null}
      {signal?.state ? <span className="text-text-secondary">{signal.state.replace(/_/g, " ")}</span> : null}
      {signal?.symbol ? <span className="text-text-muted">· {signal.symbol}</span> : null}
      {signal?.confidence != null ? (
        <span className="text-text-muted">· conf {formatPct(signal.confidence, 0)}</span>
      ) : null}
    </div>
  );
}

export function LaneSummaryCard({ lane }: { lane: LaneView }) {
  const age = ageSeconds(lane.lastScanAt);
  const running = lane.running === true;
  const idle = lane.running === false;
  const pnl = lane.dayPnl ?? lane.unrealizedPnl ?? null;

  const statusLabel = lane.degraded
    ? "unavailable"
    : running
      ? "running"
      : idle
        ? "idle"
        : "—";
  const statusVariant = lane.degraded
    ? "error"
    : running
      ? "success"
      : idle
        ? "warn"
        : "neutral";

  return (
    <Link
      href={lane.href}
      className="group flex flex-col gap-3 rounded-2xl border border-bg-border bg-bg-secondary/22 px-4 py-3.5 transition-colors hover:border-bg-active hover:bg-bg-secondary/35"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-semibold text-text-primary">
            <span className="truncate">{lane.label}</span>
            <ArrowUpRight
              size={13}
              className="shrink-0 text-text-muted opacity-0 transition-opacity group-hover:opacity-100"
            />
          </div>
          <div className="mt-0.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">
            {age != null ? `scanned ${formatDuration(age)} ago` : "no recent scan"}
          </div>
        </div>
        <StatusBadge label={statusLabel} variant={statusVariant} />
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
          <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Open</div>
          <div className="mt-0.5 font-mono text-sm font-semibold text-text-primary">
            {lane.openCount != null ? lane.openCount : "—"}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
          <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">
            {lane.dayPnl != null ? "Day P&L" : "Open P&L"}
          </div>
          <div className={clsx("mt-0.5 font-mono text-sm font-semibold", tone(pnl))}>
            {pnl != null ? formatSignedMoney(pnl) : "—"}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-bg-border/40 pt-2.5">
        <SignalLine signal={lane.signal} />
        {lane.regime ? (
          <span
            className={clsx(
              "inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em]",
              regimeTone(lane.regime),
            )}
          >
            {lane.regime.replace(/_/g, " ")}
          </span>
        ) : null}
      </div>
    </Link>
  );
}
