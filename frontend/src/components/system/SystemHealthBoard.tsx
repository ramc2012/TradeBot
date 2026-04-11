"use client";

import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  Radio,
  Server,
  Shield,
} from "lucide-react";

export type SystemHealthService = {
  key: string;
  label: string;
  status: "healthy" | "idle" | "degraded" | "critical";
  detail: string;
  meta?: Record<string, unknown>;
};

export type SystemHealthLane = {
  key: string;
  parent: string;
  label: string;
  status: string;
  timeframe?: string | null;
  scope?: string | null;
  open_positions?: number | null;
  last_scan_at?: string | null;
  scan_interval_seconds?: number | null;
  notes?: string | null;
};

export type SystemHealthResponse = {
  generated_at: string;
  summary: {
    status: "healthy" | "idle" | "degraded" | "critical";
    service_counts: Record<string, number>;
    degraded_services: number;
    critical_services: number;
  };
  services: SystemHealthService[];
  strategy_lanes: SystemHealthLane[];
};

function serviceTone(status?: string | null) {
  if (status === "healthy" || status === "active" || status === "ready") {
    return "border-accent-green/25 bg-accent-green/10 text-accent-green";
  }
  if (status === "degraded" || status === "warning" || status === "stale") {
    return "border-accent-amber/25 bg-accent-amber/10 text-accent-amber";
  }
  if (status === "critical" || status === "error") {
    return "border-accent-red/25 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-active bg-bg-secondary/50 text-text-secondary";
}

function serviceRail(status?: string | null) {
  if (status === "healthy" || status === "active" || status === "ready") return "bg-accent-green";
  if (status === "degraded" || status === "warning" || status === "stale") return "bg-accent-amber";
  if (status === "critical" || status === "error") return "bg-accent-red";
  return "bg-text-muted/40";
}

function serviceIcon(key: string) {
  if (key.includes("database") || key === "postgres") return Database;
  if (key.includes("broker") || key.includes("strategy")) return Shield;
  if (key.includes("market")) return Radio;
  return Server;
}

function statusIcon(status?: string | null) {
  if (status === "healthy") return CheckCircle2;
  if (status === "critical") return AlertTriangle;
  return Activity;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function HealthCard({
  service,
  compact = false,
}: {
  service: SystemHealthService;
  compact?: boolean;
}) {
  const Icon = serviceIcon(service.key);
  const StatusIcon = statusIcon(service.status);
  const compactMeta = service.meta?.last_run_at
    ? `Last ${formatTimestamp(String(service.meta.last_run_at))}`
    : service.meta?.next_run_at
      ? `Next ${formatTimestamp(String(service.meta.next_run_at))}`
      : service.meta?.subscribed_symbol_count
        ? `Symbols ${String(service.meta.subscribed_symbol_count)}`
        : service.meta?.scan_interval_seconds
          ? `Cadence ${String(service.meta.scan_interval_seconds)}s`
          : null;

  if (compact) {
    return (
      <div className="relative overflow-hidden rounded-2xl border border-bg-border bg-bg-secondary/24 px-3 py-2.5">
        <div className={clsx("absolute inset-y-0 left-0 w-[3px]", serviceRail(service.status))} />
        <div className="flex items-start gap-3 pl-1">
          <div className="rounded-lg bg-bg-primary/35 p-1.5 text-text-secondary">
            <Icon size={14} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <div className="truncate text-xs font-semibold text-text-primary">{service.label}</div>
              <span className={clsx("h-2 w-2 shrink-0 rounded-full", serviceRail(service.status))} />
            </div>
            <div className="mt-0.5 truncate text-[11px] text-text-muted">{service.detail}</div>
            {compactMeta ? <div className="mt-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">{compactMeta}</div> : null}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-[22px] border border-bg-border bg-bg-secondary/28 p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="rounded-xl border border-bg-active bg-bg-primary/50 p-2 text-text-secondary">
            <Icon size={16} />
          </div>
          <div>
            <div className="text-sm font-semibold text-text-primary">{service.label}</div>
            <div className="mt-1 text-xs leading-5 text-text-muted">{service.detail}</div>
          </div>
        </div>
        <span
          className={clsx(
            "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]",
            serviceTone(service.status),
          )}
        >
          <StatusIcon size={12} />
          {service.status}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-text-muted">
        {service.meta?.last_run_at ? (
          <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
            Last run {formatTimestamp(String(service.meta.last_run_at))}
          </span>
        ) : null}
        {service.meta?.next_run_at ? (
          <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
            Next run {formatTimestamp(String(service.meta.next_run_at))}
          </span>
        ) : null}
        {service.meta?.scan_interval_seconds ? (
          <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
            Cadence {String(service.meta.scan_interval_seconds)}s
          </span>
        ) : null}
        {service.meta?.subscribed_symbol_count ? (
          <span className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-1">
            Symbols {String(service.meta.subscribed_symbol_count)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export function SystemHealthBoard({
  health,
  compact = false,
  includeLanes = true,
}: {
  health: SystemHealthResponse;
  compact?: boolean;
  includeLanes?: boolean;
}) {
  const summary = health.summary;
  const laneCount = health.strategy_lanes?.length || 0;

  return (
    <div className="space-y-4">
      <div className={clsx("grid gap-3", compact ? "sm:grid-cols-2 xl:grid-cols-4" : "md:grid-cols-5")}>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/32 px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Overall</div>
          <div className="mt-1.5 text-base font-semibold text-text-primary xl:text-lg">{summary.status}</div>
          <div className="mt-1 text-[11px] text-text-muted">Generated {formatTimestamp(health.generated_at)}</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/32 px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Healthy</div>
          <div className="mt-1.5 font-mono text-base font-semibold text-accent-green xl:text-lg">
            {summary.service_counts.healthy || 0}
          </div>
          <div className="mt-1 text-[11px] text-text-muted">Services operating normally</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/32 px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Degraded</div>
          <div className="mt-1.5 font-mono text-base font-semibold text-accent-amber xl:text-lg">
            {summary.service_counts.degraded || 0}
          </div>
          <div className="mt-1 text-[11px] text-text-muted">Reduced capacity or stale runtime</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/32 px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Critical</div>
          <div className="mt-1.5 font-mono text-base font-semibold text-accent-red xl:text-lg">
            {summary.service_counts.critical || 0}
          </div>
          <div className="mt-1 text-[11px] text-text-muted">Requires operator action</div>
        </div>
        {!compact ? (
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/32 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Strategy Lanes</div>
            <div className="mt-1.5 font-mono text-base font-semibold text-text-primary xl:text-lg">{laneCount}</div>
            <div className="mt-1 text-[11px] text-text-muted">Per-strategy runtime monitors</div>
          </div>
        ) : null}
      </div>

      <div className={clsx("grid gap-3", compact ? "sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-5" : "xl:grid-cols-3")}>
        {health.services.map((service) => (
          <HealthCard key={service.key} service={service} compact={compact} />
        ))}
      </div>

      {includeLanes ? (
        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/24 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-text-primary">Strategy Lane Health</div>
              <div className="mt-1 text-xs text-text-muted">
                Each strategy lane is monitored independently so you can see cadence, scope, and stale scans without opening every desk page.
              </div>
            </div>
            <div className="text-xs text-text-muted">{laneCount} lanes</div>
          </div>

          <div className="mt-4 overflow-auto">
            <table className="w-full min-w-[880px] text-left text-xs">
              <thead className="border-b border-bg-border text-text-muted">
                <tr>
                  <th className="py-2 pr-3">Lane</th>
                  <th className="py-2 pr-3">Parent</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Timeframe</th>
                  <th className="py-2 pr-3">Scope</th>
                  <th className="py-2 pr-3">Open</th>
                  <th className="py-2 pr-3">Last Scan</th>
                  <th className="py-2">Cadence</th>
                </tr>
              </thead>
              <tbody>
                {health.strategy_lanes.map((lane) => (
                  <tr key={lane.key} className="border-b border-bg-border/40 align-top">
                    <td className="py-2 pr-3 font-semibold text-text-primary">{lane.label}</td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.parent.replaceAll("_", " ")}</td>
                    <td className="py-2 pr-3">
                      <span className={clsx("inline-flex rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]", serviceTone(lane.status))}>
                        {lane.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.timeframe || "--"}</td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.scope || lane.notes || "--"}</td>
                    <td className="py-2 pr-3 font-mono text-text-primary">{lane.open_positions ?? 0}</td>
                    <td className="py-2 pr-3 text-text-secondary">{formatTimestamp(lane.last_scan_at)}</td>
                    <td className="py-2 text-text-secondary">
                      {lane.scan_interval_seconds ? `${lane.scan_interval_seconds}s` : "--"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}
