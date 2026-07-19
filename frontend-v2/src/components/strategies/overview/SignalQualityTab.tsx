"use client";

import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { FreshnessBadge, MetricTile, REFRESH_MS, Section, StatusBadge } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

type LaneQuality = {
  key: string;
  label: string;
  status: string;
  evaluated_count: number;
  actionable_count: number;
  failure_count: number;
  coverage_pct: number | null;
  rejection_counts: Record<string, number>;
  stale_or_missing_count: number;
  median_latency_seconds: number | null;
  frequency_drift_pct: number | null;
  replay_mismatch_count: number | null;
  replay_parity_pass: boolean | null;
  last_error?: string | null;
};

type FeedQuality = {
  key: string;
  kind: string;
  symbol: string;
  source: string;
  age_seconds: number | null;
  accepted_count: number;
  rejected_count: number;
  execution_ready: boolean;
  validation_status: string;
  rejection_counts: Record<string, number>;
};

type Payload = {
  generated_at: string;
  summary: {
    evaluated_count: number;
    actionable_count: number;
    failure_count: number;
    stale_or_missing_count: number;
    degraded_feed_count: number;
  };
  lanes: LaneQuality[];
  validated_snapshots: { feeds: FeedQuality[]; feed_count: number };
};

const number = (value: number | null | undefined, suffix = "") =>
  value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(value % 1 ? 1 : 0)}${suffix}`;

function topRejections(counts: Record<string, number>): string {
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 2);
  return rows.length ? rows.map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(" · ") : "—";
}

export function SignalQualityTab({
  laneKeys,
  title = "Signal validation by lane",
}: {
  laneKeys?: string[];
  title?: string;
}) {
  const query = useQuery({
    queryKey: ["overview", "signal-validation"],
    queryFn: async () => (await apiClient.get("/api/system/signal-validation")).data as Payload,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const data = query.data;
  const lanes = laneKeys?.length
    ? (data?.lanes ?? []).filter((lane) => laneKeys.includes(lane.key))
    : (data?.lanes ?? []);
  const summary = laneKeys?.length
    ? {
        evaluated_count: lanes.reduce((total, lane) => total + lane.evaluated_count, 0),
        actionable_count: lanes.reduce((total, lane) => total + lane.actionable_count, 0),
        failure_count: lanes.reduce((total, lane) => total + lane.failure_count, 0),
        stale_or_missing_count: lanes.reduce((total, lane) => total + lane.stale_or_missing_count, 0),
        degraded_feed_count: data?.summary.degraded_feed_count ?? 0,
      }
    : data?.summary;
  const feeds = data?.validated_snapshots?.feeds ?? [];

  return (
    <div className="space-y-3">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <MetricTile label="Evaluated" value={String(summary?.evaluated_count ?? 0)} detail="latest lane cycles" />
        <MetricTile label="Actionable" value={String(summary?.actionable_count ?? 0)} detail="signals through gates" />
        <MetricTile label="Rejected inputs" value={String(summary?.stale_or_missing_count ?? 0)} detail="stale or missing" />
        <MetricTile label="Cycle failures" value={String(summary?.failure_count ?? 0)} detail="latest cycles" />
        <MetricTile label="Degraded feeds" value={String(summary?.degraded_feed_count ?? 0)} detail={`${feeds.length} validated feeds`} />
      </section>

      <Section
        title={title}
        icon={<ShieldCheck size={16} className="text-accent-blue" />}
        description="Coverage, gate rejections, scan latency, cadence drift and replay parity. P/L is intentionally excluded."
        /* A query returning 200 is NOT liveness — it is "loaded". Data age is
           stated separately by the freshness badge. */
        rightSlot={<div className="flex items-center gap-2"><StatusBadge label={query.isError ? "unavailable" : query.isFetching ? "refreshing" : "loaded"} variant={query.isError ? "error" : query.isFetching ? "info" : "neutral"} /><FreshnessBadge asOf={data?.generated_at ?? null} label="scan" /></div>}
      >
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full min-w-[1120px] border-collapse text-left">
            <thead><tr className="border-b border-bg-border/60">
              {['Lane', 'Coverage', 'Evaluated → actionable', 'Top rejections', 'Stale / missing', 'Median latency', 'Cadence drift', 'Replay mismatches'].map((label) => (
                <th key={label} className="px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-text-muted">{label}</th>
              ))}
            </tr></thead>
            <tbody>
              {lanes.map((lane) => (
                <tr key={lane.key} className="border-b border-bg-border/25 align-top hover:bg-bg-primary/20">
                  <td className="px-2.5 py-2"><div className="font-semibold text-text-primary">{lane.label}</div><StatusBadge label={lane.status} variant={lane.status === "failed" ? "error" : lane.status === "running" ? "info" : "success"} />{lane.last_error ? <div className="mt-1 max-w-[210px] text-[10px] text-accent-red">{lane.last_error}</div> : null}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{number(lane.coverage_pct, "%")}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{lane.evaluated_count} → {lane.actionable_count}</td>
                  <td className="max-w-[290px] px-2.5 py-2 text-[11px] text-text-muted">{topRejections(lane.rejection_counts)}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{lane.stale_or_missing_count}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{number(lane.median_latency_seconds, "s")}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{number(lane.frequency_drift_pct, "%")}</td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{lane.replay_mismatch_count == null ? "not audited" : lane.replay_mismatch_count}</td>
                </tr>
              ))}
              {!lanes.length ? <tr><td colSpan={8} className="px-2.5 py-6 text-center text-sm text-text-muted">No scheduler telemetry has been recorded for this lane yet.</td></tr> : null}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Validated input snapshots" description="Canonical freshness and provenance for candle and option-chain inputs used by signal lanes.">
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full min-w-[840px] border-collapse text-left">
            <thead><tr className="border-b border-bg-border/60">
              {['Feed', 'Source', 'Age', 'Accepted', 'Rejected', 'Execution ready', 'Reasons'].map((label) => <th key={label} className="px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-text-muted">{label}</th>)}
            </tr></thead>
            <tbody>
              {feeds.map((feed) => <tr key={feed.key} className="border-b border-bg-border/25">
                <td className="px-2.5 py-2"><div className="font-semibold text-text-primary">{feed.symbol}</div><div className="text-[10px] text-text-muted">{feed.kind}</div></td>
                <td className="px-2.5 py-2 text-text-secondary">{feed.source}</td>
                <td className="px-2.5 py-2 font-mono text-text-secondary">{number(feed.age_seconds, "s")}</td>
                <td className="px-2.5 py-2 font-mono text-text-secondary">{feed.accepted_count}</td>
                <td className="px-2.5 py-2 font-mono text-text-secondary">{feed.rejected_count}</td>
                <td className="px-2.5 py-2"><StatusBadge label={feed.validation_status} variant={feed.execution_ready ? "success" : "error"} /></td>
                <td className="px-2.5 py-2 text-[11px] text-text-muted">{topRejections(feed.rejection_counts)}</td>
              </tr>)}
              {!feeds.length ? <tr><td colSpan={7} className="px-2.5 py-6 text-center text-sm text-text-muted">No validated feed has been observed since this backend started.</td></tr> : null}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
