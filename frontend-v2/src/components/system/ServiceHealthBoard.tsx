"use client";

/**
 * Native v2 system-health board. Polls /api/system/health at the snapshot
 * cadence (replaces v1's createSystemHealthSocket WS) and renders a KPI
 * strip + per-service cards (DB / redis / broker / market-data / paper
 * engines) coloured via serviceStateTone, plus a strategy-lane table.
 *
 * Default export takes no required props so the /system hub can embed it
 * as <HealthEmbed />.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  Layers,
  Radio,
  RefreshCw,
  Server,
  Shield,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { clsx } from "clsx";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatNumber,
  serviceStateTone,
} from "@/components/desk-ui";
import { api } from "@/lib/api";

import UpstoxBudgetCard from "./UpstoxBudgetCard";

export type SystemHealthService = {
  key: string;
  label: string;
  status: string;
  detail: string;
  meta?: Record<string, unknown> | null;
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
    status: string;
    service_counts: Record<string, number>;
    degraded_services: number;
    critical_services: number;
  };
  services: SystemHealthService[];
  strategy_lanes: SystemHealthLane[];
};

const STATE_VARIANT = (status?: string | null) => {
  const s = String(status || "").toLowerCase();
  if (s === "healthy" || s === "active" || s === "ready") return "success" as const;
  if (s === "degraded" || s === "warning" || s === "stale") return "warn" as const;
  if (s === "critical" || s === "error") return "error" as const;
  if (s === "idle") return "neutral" as const;
  return "neutral" as const;
};

function serviceRail(status?: string | null): string {
  const s = String(status || "").toLowerCase();
  if (s === "healthy" || s === "active" || s === "ready") return "bg-accent-green";
  if (s === "degraded" || s === "warning" || s === "stale") return "bg-accent-amber";
  if (s === "critical" || s === "error") return "bg-accent-red";
  if (s === "idle") return "bg-accent-blue/70";
  return "bg-text-muted/40";
}

function ServiceIcon({ keyName }: { keyName: string }) {
  const k = keyName.toLowerCase();
  const cls = "text-text-secondary";
  if (k.includes("postgres") || k.includes("database")) return <Database size={16} className={cls} />;
  if (k.includes("redis")) return <Zap size={16} className={cls} />;
  if (k.includes("broker")) return <ShieldCheck size={16} className={cls} />;
  if (k.includes("market")) return <Radio size={16} className={cls} />;
  if (k.includes("strategy") || k.includes("auction") || k.includes("fractal"))
    return <Shield size={16} className={cls} />;
  if (k.includes("research")) return <Activity size={16} className={cls} />;
  return <Server size={16} className={cls} />;
}

/** Pull the most operator-relevant one-liner out of a service's meta blob. */
function metaChips(svc: SystemHealthService): string[] {
  const m = svc.meta ?? {};
  const chips: string[] = [];
  const push = (label: string, v: unknown) => {
    if (v == null || v === "") return;
    chips.push(`${label} ${String(v)}`);
  };
  if (typeof m.state === "string") push("state", m.state);
  if (typeof m.mode === "string") push("mode", m.mode);
  if (typeof m.subscribed_symbol_count === "number") push("symbols", m.subscribed_symbol_count);
  if (typeof m.open_positions === "number") push("open", m.open_positions);
  if (typeof m.scan_interval_seconds === "number") push("cadence", `${m.scan_interval_seconds}s`);
  if (typeof m.last_run_at === "string") push("last", formatIST(m.last_run_at));
  if (typeof m.next_run_at === "string") push("next", formatIST(m.next_run_at));
  if (typeof m.next_scan_at === "string") push("next", formatIST(m.next_scan_at));
  if (typeof m.last_tick_age_seconds === "number") push("tick age", `${Math.round(m.last_tick_age_seconds)}s`);
  if (Array.isArray(m.connected_brokers) && m.connected_brokers.length)
    push("brokers", (m.connected_brokers as unknown[]).join(", "));
  if (typeof m.kill_switch_active === "boolean" && m.kill_switch_active) chips.push("kill-switch ON");
  return chips.slice(0, 4);
}

function ServiceCard({ svc }: { svc: SystemHealthService }) {
  const chips = metaChips(svc);
  return (
    <div className="relative overflow-hidden rounded-2xl border border-bg-border bg-bg-secondary/28 p-3.5">
      <div className={clsx("absolute inset-y-0 left-0 w-[3px]", serviceRail(svc.status))} />
      <div className="flex items-start justify-between gap-3 pl-1.5">
        <div className="flex min-w-0 items-start gap-3">
          <div className="rounded-xl border border-bg-active bg-bg-primary/50 p-2">
            <ServiceIcon keyName={svc.key} />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-text-primary">{svc.label}</div>
            <div className="mt-1 text-xs leading-5 text-text-muted">{svc.detail}</div>
          </div>
        </div>
        <StatusBadge label={svc.status} tone={serviceStateTone(svc.status)} />
      </div>
      {chips.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5 pl-1.5">
          {chips.map((c) => (
            <span
              key={c}
              className="rounded-full border border-bg-border bg-bg-primary/40 px-2 py-0.5 text-[10.5px] text-text-muted"
            >
              {c}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function ServiceHealthBoard({ showBudget = true }: { showBudget?: boolean } = {}) {
  const q = useQuery({
    queryKey: ["system-health"],
    queryFn: async () => (await api.get("/api/system/health")).data as SystemHealthResponse,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const health = q.data;
  const summary = health?.summary;
  const counts = summary?.service_counts ?? {};
  const services = useMemo(() => health?.services ?? [], [health]);
  const lanes = useMemo(() => health?.strategy_lanes ?? [], [health]);

  const healthy = counts.healthy ?? 0;
  const idle = counts.idle ?? 0;
  const degraded = summary?.degraded_services ?? counts.degraded ?? 0;
  const critical = summary?.critical_services ?? counts.critical ?? 0;
  const overall = summary?.status ?? (q.isError ? "critical" : "loading");

  const overallVariant = STATE_VARIANT(overall);
  const OverallIcon = critical > 0 ? AlertTriangle : degraded > 0 ? Activity : CheckCircle2;

  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <Shield size={18} className="text-accent-blue" />
              Service health
            </h1>
            <p className="mt-1 text-sm text-text-muted">
              Runtime health for core services, market data, strategy supervisors, and the research sync — grouped by
              operator impact, not raw process count.
            </p>
          </div>
          <button
            type="button"
            onClick={() => q.refetch()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
          >
            <RefreshCw size={13} className={q.isFetching ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      {/* KPI strip */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 px-4 py-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Overall</div>
          <div className="mt-1.5 flex items-center gap-2">
            <OverallIcon
              size={16}
              className={
                overallVariant === "success"
                  ? "text-accent-green"
                  : overallVariant === "warn"
                    ? "text-accent-amber"
                    : overallVariant === "error"
                      ? "text-accent-red"
                      : "text-text-muted"
              }
            />
            <StatusBadge label={String(overall)} variant={overallVariant} />
          </div>
          <div className="mt-1 text-[11px] text-text-muted">
            {health?.generated_at ? formatIST(health.generated_at) : q.isError ? "feed offline" : "—"}
          </div>
        </div>
        <MetricTile label="Healthy" value={String(healthy)} detail="operating normally" color="text-accent-green" />
        <MetricTile label="Idle" value={String(idle)} detail="enabled, not scanning" color={idle ? "text-accent-blue" : undefined} />
        <MetricTile
          label="Degraded"
          value={String(degraded)}
          detail="reduced / stale"
          color={degraded ? "text-accent-amber" : undefined}
        />
        <MetricTile
          label="Critical"
          value={String(critical)}
          detail="needs operator action"
          color={critical ? "text-accent-red" : undefined}
        />
        <MetricTile
          label="Strategy lanes"
          value={String(lanes.length)}
          detail="per-lane monitors"
        />
      </section>

      {/* Service cards */}
      <Section title="Services" icon={<Server size={16} />} description="Core infrastructure, market data, and strategy supervisors.">
        {q.isError ? (
          <div className="rounded-xl border border-dashed border-accent-red/30 bg-accent-red/5 px-4 py-10 text-center text-sm text-accent-red">
            Could not reach /api/system/health.
          </div>
        ) : services.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-12 text-center text-sm text-text-muted">
            <span className="animate-pulse">Loading deployed service health…</span>
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {services.map((svc) => (
              <ServiceCard key={svc.key} svc={svc} />
            ))}
          </div>
        )}
      </Section>

      {/* Upstox API budget — suppressed when /system renders it on its own tab */}
      {showBudget ? <UpstoxBudgetCard /> : null}

      {/* Strategy lanes */}
      <Section
        title="Strategy lane health"
        icon={<Layers size={16} />}
        description="Each lane is monitored independently — cadence, scope, and stale scans without opening every desk."
        rightSlot={<span className="text-xs text-text-muted">{lanes.length} lanes</span>}
      >
        {lanes.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
            No active strategy lanes reported.
          </div>
        ) : (
          <div className="overflow-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="border-b border-bg-border text-text-muted">
                <tr>
                  <th className="py-2 pr-3 font-medium">Lane</th>
                  <th className="py-2 pr-3 font-medium">Parent</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Timeframe</th>
                  <th className="py-2 pr-3 font-medium">Scope</th>
                  <th className="py-2 pr-3 text-right font-medium">Open</th>
                  <th className="py-2 pr-3 font-medium">Last scan</th>
                  <th className="py-2 font-medium">Cadence</th>
                </tr>
              </thead>
              <tbody>
                {lanes.map((lane) => (
                  <tr key={lane.key} className="border-b border-bg-border/40 align-top">
                    <td className="py-2 pr-3 font-semibold text-text-primary">{lane.label}</td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.parent.replaceAll("_", " ")}</td>
                    <td className="py-2 pr-3">
                      <StatusBadge label={lane.status} tone={serviceStateTone(lane.status)} />
                    </td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.timeframe || "—"}</td>
                    <td className="py-2 pr-3 text-text-secondary">{lane.scope || lane.notes || "—"}</td>
                    <td className="py-2 pr-3 text-right font-mono text-text-primary">{lane.open_positions ?? 0}</td>
                    <td className="py-2 pr-3 text-text-secondary">
                      {lane.last_scan_at ? formatIST(lane.last_scan_at) : "—"}
                    </td>
                    <td className="py-2 font-mono text-text-secondary">
                      {lane.scan_interval_seconds ? `${formatNumber(lane.scan_interval_seconds, 0)}s` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
