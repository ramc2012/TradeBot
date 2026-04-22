"use client";

import { Activity, MonitorSmartphone, Shield } from "lucide-react";

import {
  SystemHealthBoard,
  type SystemHealthResponse,
} from "@/components/system/SystemHealthBoard";
import PageTabs from "@/components/layout/PageTabs";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { getSystemHealth } from "@/lib/api";
import { createSystemHealthSocket } from "@/lib/websocket";

const SETTINGS_TABS = [
  { href: "/settings", label: "Settings" },
  { href: "/health", label: "Health" },
];

export default function HealthPage() {
  const healthQuery = useLiveSnapshotQuery<SystemHealthResponse>({
    queryKey: ["systemHealth"],
    queryFn: () => getSystemHealth().then((response) => response.data as SystemHealthResponse),
    streamFactory: (onData, onStatusChange) =>
      createSystemHealthSocket((data) => onData(data as SystemHealthResponse), onStatusChange),
    staleTime: 10_000,
  });

  const health = healthQuery.data;
  const frontendReady = healthQuery.isSuccess;

  return (
    <div className="mx-auto max-w-[1680px] space-y-6 pb-10">
      <section className="rounded-[28px] border border-bg-active/60 bg-bg-secondary/30 px-5 py-4">
        <div className="max-w-4xl space-y-3">
          <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
            <Shield size={18} className="text-accent-blue" />
            System Health
          </div>
          <p className="mt-1.5 text-sm leading-6 text-text-secondary">
            Runtime health for core services, market data, strategy supervisors, and validation.
          </p>
          <PageTabs tabs={SETTINGS_TABS} />
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[0.9fr,1.1fr]">
        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/25 p-4">
          <div className="flex items-start gap-3">
            <div className="rounded-xl border border-bg-active bg-bg-primary/50 p-2 text-accent-blue">
              <MonitorSmartphone size={16} />
            </div>
            <div>
              <div className="text-sm font-semibold text-text-primary">Frontend UI</div>
              <div className="mt-1 text-xs leading-5 text-text-muted">UI shell plus backend health query.</div>
            </div>
          </div>
          <div className="mt-4 inline-flex rounded-full border border-accent-green/25 bg-accent-green/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-accent-green">
            {frontendReady ? "healthy" : "loading"}
          </div>
        </div>

        <div className="rounded-[24px] border border-bg-border bg-bg-secondary/25 p-4">
          <div className="flex items-start gap-3">
            <div className="rounded-xl border border-bg-active bg-bg-primary/50 p-2 text-accent-green">
              <Activity size={16} />
            </div>
            <div>
              <div className="text-sm font-semibold text-text-primary">Monitoring Model</div>
              <div className="mt-1 text-xs leading-5 text-text-muted">Grouped by operator impact, not raw process count.</div>
            </div>
          </div>
        </div>
      </section>

      {health ? (
        <SystemHealthBoard health={health} />
      ) : (
        <div className="rounded-[24px] border border-dashed border-bg-active bg-bg-secondary/15 p-8 text-sm text-text-muted">
          Loading deployed service health.
        </div>
      )}
    </div>
  );
}
