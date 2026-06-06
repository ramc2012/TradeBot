"use client";

/**
 * Upstox API budget — rolling rate-limit window usage for the research
 * sync. Backed by /api/analysis/research-cache-status (`api_budget`).
 * Native v2 surface; amber warning when window utilisation > 80%.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Gauge, Timer } from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatDuration,
  formatNumber,
  tone,
} from "@/components/desk-ui";
import { api } from "@/lib/api";

type ApiBudget = {
  limits?: {
    per_second?: number | null;
    per_minute?: number | null;
    per_30_minutes?: number | null;
    window_minutes?: number | null;
  } | null;
  configured?: {
    gap_seconds?: number | null;
    calls_per_second?: number | null;
    calls_per_minute?: number | null;
    calls_per_30_minutes?: number | null;
  } | null;
  rolling_30m?: {
    completed_runs?: number | null;
    calls?: number | null;
    utilization_pct_of_doc_limit?: number | null;
    avg_calls_per_minute?: number | null;
    avg_calls_per_second?: number | null;
    active_run_avg_calls_per_second?: number | null;
    by_endpoint?: Record<string, number> | null;
  } | null;
  last_run?: {
    calls?: number | null;
    elapsed_seconds?: number | null;
    avg_calls_per_second?: number | null;
    by_endpoint?: Record<string, number> | null;
  } | null;
  theoretical?: {
    total_calls?: number | null;
    completed_calls?: number | null;
    remaining_calls?: number | null;
    full_seconds_at_configured_rate?: number | null;
    full_seconds_at_documented_cap?: number | null;
    full_seconds_at_observed_rate?: number | null;
    remaining_seconds_at_observed_rate?: number | null;
  } | null;
};

type Scheduler = {
  state?: string | null;
  label?: string | null;
  detail?: string | null;
  poll_minutes?: number | null;
  cooldown_minutes?: number | null;
  next_batch_at?: string | null;
  seconds_until_next_batch?: number | null;
} | null;

type CacheStatus = {
  api_budget?: ApiBudget | null;
  scheduler?: Scheduler;
};

function num(v?: number | null): number | null {
  return v == null || Number.isNaN(v) ? null : v;
}

/** Window utilisation as a 0–100 percentage of the documented 30m cap. */
function windowUsedPct(b?: ApiBudget | null): number | null {
  const used = num(b?.rolling_30m?.calls);
  const cap = num(b?.limits?.per_30_minutes);
  const doc = num(b?.rolling_30m?.utilization_pct_of_doc_limit);
  if (doc != null) return doc;
  if (used != null && cap != null && cap > 0) return (used / cap) * 100;
  return null;
}

export default function UpstoxBudgetCard() {
  const q = useQuery({
    queryKey: ["upstox-api-budget"],
    queryFn: async () => (await api.get("/api/analysis/research-cache-status")).data as CacheStatus,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const budget = q.data?.api_budget ?? null;
  const scheduler = q.data?.scheduler ?? null;
  const usedPct = windowUsedPct(budget);
  const overBudget = usedPct != null && usedPct > 80;

  const rolling = budget?.rolling_30m ?? null;
  const limits = budget?.limits ?? null;
  const configured = budget?.configured ?? null;
  const lastRun = budget?.last_run ?? null;
  const theo = budget?.theoretical ?? null;

  const mix = useMemo(() => {
    const m = rolling?.by_endpoint ?? {};
    return Object.entries(m).sort((a, b) => Number(b[1]) - Number(a[1]));
  }, [rolling]);

  const barColor = overBudget
    ? "bg-accent-red"
    : usedPct != null && usedPct > 50
      ? "bg-accent-amber"
      : "bg-accent-green";

  return (
    <Section
      title="Upstox API budget"
      icon={<Gauge size={16} />}
      description="Rolling rate-limit window for the research sync — calls used against the documented 30-minute cap."
      rightSlot={
        <StatusBadge
          label={q.isError ? "offline" : overBudget ? "throttling risk" : "nominal"}
          variant={q.isError ? "error" : overBudget ? "warn" : "success"}
        />
      }
    >
      {q.isError ? (
        <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
          Could not load API budget telemetry.
        </div>
      ) : !budget ? (
        <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
          <span className="animate-pulse">Loading API budget…</span>
        </div>
      ) : (
        <div className="space-y-4">
          {overBudget ? (
            <div className="flex items-center gap-2 rounded-xl border border-accent-amber/35 bg-accent-amber/10 px-3 py-2 text-[12px] text-accent-amber">
              <AlertTriangle size={14} className="shrink-0" />
              Window usage {formatNumber(usedPct, 1)}% of the documented cap — approaching the rate limit; sync may
              start backing off.
            </div>
          ) : null}

          {/* Window gauge */}
          <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-4 py-3">
            <div className="flex items-end justify-between gap-3">
              <div>
                <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                  Rolling {num(limits?.window_minutes) ?? 30}-minute window
                </div>
                <div className="mt-1 font-mono text-2xl font-semibold text-text-primary">
                  {formatNumber(num(rolling?.calls) ?? 0, 0)}
                  <span className="ml-1 text-sm text-text-muted">
                    / {formatNumber(num(limits?.per_30_minutes), 0)} calls
                  </span>
                </div>
              </div>
              <div className={`text-right font-mono text-lg font-semibold ${overBudget ? "text-accent-red" : tone(1)}`}>
                {usedPct != null ? `${formatNumber(usedPct, 1)}%` : "—"}
                <div className="text-[10px] font-normal uppercase tracking-[0.14em] text-text-muted">of doc cap</div>
              </div>
            </div>
            <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-bg-secondary/60">
              <div
                className={`h-full rounded-full ${barColor} transition-all`}
                style={{ width: `${Math.min(100, Math.max(1.5, usedPct ?? 0))}%` }}
              />
            </div>
            <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-text-muted">
              <span>0</span>
              <span className="text-accent-amber/80">80% warn</span>
              <span>{formatNumber(num(limits?.per_30_minutes), 0)}</span>
            </div>
          </div>

          {/* KPI strip */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile
              label="Avg / min (30m)"
              value={formatNumber(num(rolling?.avg_calls_per_minute), 2)}
              detail={`doc max ${formatNumber(num(limits?.per_minute), 0)}/min`}
            />
            <MetricTile
              label="Configured ceiling"
              value={`${formatNumber(num(configured?.calls_per_30_minutes), 0)}`}
              detail={`gap ${formatNumber(num(configured?.gap_seconds), 1)}s · ${formatNumber(num(configured?.calls_per_second), 2)}/s`}
            />
            <MetricTile
              label="Last run rate"
              value={`${formatNumber(num(lastRun?.avg_calls_per_second), 2)}/s`}
              detail={`${formatNumber(num(lastRun?.calls), 0)} calls in ${formatDuration(num(lastRun?.elapsed_seconds))}`}
            />
            <MetricTile
              label="Window cap"
              value={`${formatNumber(num(limits?.per_30_minutes), 0)}`}
              detail={`${formatNumber(num(limits?.per_second), 0)}/s · ${formatNumber(num(limits?.per_minute), 0)}/min`}
            />
          </div>

          {/* ETA + call mix */}
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="space-y-2 rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">
                <Timer size={13} /> Useful-dataset ETA
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <Cell label="Total est. calls" value={formatNumber(num(theo?.total_calls), 0)} />
                <Cell label="Remaining calls" value={formatNumber(num(theo?.remaining_calls), 0)} />
                <Cell
                  label="Full ETA (obs. rate)"
                  value={formatDuration(num(theo?.full_seconds_at_observed_rate))}
                />
                <Cell
                  label="Remaining ETA"
                  value={formatDuration(num(theo?.remaining_seconds_at_observed_rate))}
                />
              </div>
              <div className="text-[11px] text-text-muted">
                Scheduler:{" "}
                <span className="text-text-secondary">{scheduler?.label || scheduler?.state || "—"}</span>
                {num(scheduler?.poll_minutes) != null ? ` · poll ${scheduler?.poll_minutes}m` : ""}
              </div>
            </div>

            <div className="space-y-2 rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
              <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-secondary">
                Call mix · last 30 minutes
              </div>
              {mix.length === 0 ? (
                <div className="px-1 py-4 text-center text-[12px] text-text-muted">No calls in window.</div>
              ) : (
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {mix.map(([endpoint, count]) => (
                    <Cell
                      key={endpoint}
                      label={endpoint.replaceAll("_", " ")}
                      value={formatNumber(Number(count), 0)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </Section>
  );
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/30 px-2 py-1.5">
      <div className="truncate text-[10px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className="mt-0.5 font-mono text-[13px] text-text-primary">{value}</div>
    </div>
  );
}
