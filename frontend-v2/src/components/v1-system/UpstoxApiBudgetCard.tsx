"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Loader2, RefreshCw } from "lucide-react";

import { getResearchCacheStatus } from "@/lib/api";

function formatInteger(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(value);
}

function formatDecimal(value?: number | null, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return "-";
  const total = Math.round(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${total}s`;
}

export default function UpstoxApiBudgetCard() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["researchCacheStatus"],
    queryFn: () => getResearchCacheStatus().then((response) => response.data),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const budget = data?.api_budget;
  const scheduler = data?.scheduler;

  return (
    <div className="card space-y-4 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">Upstox API Budget</div>
          <p className="mt-1 text-xs text-text-muted">
            Rolling call usage, configured pacing, and theoretical useful-dataset ETA for the research sync.
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="rounded p-1 text-text-muted hover:text-text-primary"
          title="Refresh"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <Loader2 size={12} className="animate-spin" /> Loading API budget...
        </div>
      ) : null}

      {isError ? (
        <div className="flex items-center gap-2 text-xs text-accent-red">
          <AlertCircle size={12} /> Could not load API budget telemetry.
        </div>
      ) : null}

      {!isLoading && !isError && budget ? (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Rolling 30m</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatInteger(budget.rolling_30m?.calls)} / {formatInteger(budget.limits?.per_30_minutes)}
              </div>
              <div className="text-xs text-text-muted">
                {formatDecimal(budget.rolling_30m?.utilization_pct_of_doc_limit, 1)}% of documented cap
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Configured Ceiling</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatInteger(budget.configured?.calls_per_30_minutes)} / {formatInteger(budget.limits?.per_30_minutes)}
              </div>
              <div className="text-xs text-text-muted">
                gap {formatDecimal(budget.configured?.gap_seconds, 1)}s between calls
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Last Run Rate</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatDecimal(budget.last_run?.avg_calls_per_second, 2)}/s
              </div>
              <div className="text-xs text-text-muted">
                {formatInteger(budget.last_run?.calls)} calls in {formatDuration(budget.last_run?.elapsed_seconds)}
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Full Useful Target</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatDuration(budget.theoretical?.full_seconds_at_configured_rate)}
              </div>
              <div className="text-xs text-text-muted">configured-rate estimate from empty cache</div>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <div className="space-y-2 rounded border border-bg-border bg-bg-secondary/30 p-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">
                Current Rates vs Limits
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Observed avg / min</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.rolling_30m?.avg_calls_per_minute, 1)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Doc max / min</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.limits?.per_minute)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Observed avg / sec</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.rolling_30m?.avg_calls_per_second, 3)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Configured / sec</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.configured?.calls_per_second, 3)}</div>
                </div>
              </div>
              <div className="text-xs text-text-muted">
                Scheduler: <span className="text-text-primary">{scheduler?.label || "-"}</span>
              </div>
            </div>

            <div className="space-y-2 rounded border border-bg-border bg-bg-secondary/30 p-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Useful-Dataset ETA</div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Total estimated calls</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.theoretical?.total_calls)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Remaining estimated calls</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.theoretical?.remaining_calls)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Full ETA at observed rate</div>
                  <div className="font-semibold text-text-primary">{formatDuration(budget.theoretical?.full_seconds_at_observed_rate)}</div>
                </div>
                <div className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">Remaining ETA at observed rate</div>
                  <div className="font-semibold text-text-primary">{formatDuration(budget.theoretical?.remaining_seconds_at_observed_rate)}</div>
                </div>
              </div>
              <div className="text-[11px] text-text-muted">
                Model: 1 expiry fetch per underlying, 1 spot-history fetch per underlying, 1 contract-discovery fetch per expiry, and 1 historical-candle fetch per required CE/PE research contract.
              </div>
            </div>
          </div>

          <div className="space-y-2 rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">
              Call Mix In Last 30 Minutes
            </div>
            <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
              {Object.entries(budget.rolling_30m?.by_endpoint || {}).map(([endpoint, count]) => (
                <div key={endpoint} className="rounded border border-bg-border bg-bg-primary/40 px-2 py-2">
                  <div className="text-text-muted">{endpoint.replaceAll("_", " ")}</div>
                  <div className="font-semibold text-text-primary">{formatInteger(Number(count))}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
