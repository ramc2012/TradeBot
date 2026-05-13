"use client";

/**
 * Fractal Market Profile · data-focused live view.
 *
 * One row per supported underlying with the FMP signal action, confidence,
 * hourly + daily shape, value-area migration, paper P&L, and bucket.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  api as apiClient,
  getFractalMarketProfileLiveSnapshot,
  getFractalMarketProfileSummary,
} from "@/lib/api";
import {
  AuditFeed,
  BucketBadge,
  ThreeListView,
  formatINR,
  formatIST,
  formatNumber,
  formatPct,
  type AuditEvent,
  type Bucket,
  type BucketedRow,
  type Trajectory,
} from "@/components/strategy/desk-helpers";

const REFRESH_MS = 6_000;
const SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"] as const;

type FMPSnap = {
  symbol_code?: string;
  signal?: {
    underlying?: string;
    signal_time?: string;
    setup_name?: string;
    action?: "LONG" | "SHORT" | "FLAT";
    confidence?: number;
    horizon?: string;
    actionable?: boolean;
    latest_close?: number;
    entry_trigger?: number;
    stop_level?: number;
    target_level?: number;
    hourly_shape?: string;
    daily_shape?: string;
    hourly_number?: number;
    value_migration_score?: number;
    rationale?: string[];
    filters?: string[];
    bucket?: Bucket;
    trajectory?: Trajectory;
    proximity_pct?: number | null;
    bucket_rationale?: string | null;
  };
  paper_positions?: Record<string, unknown>;
  data_status?: Record<string, unknown>;
};

export default function FractalMarketProfileLivePage() {
  const summaryQuery = useQuery({
    queryKey: ["fmp-live", "summary"],
    queryFn: async () => (await getFractalMarketProfileSummary()).data,
    refetchInterval: REFRESH_MS * 5,
    refetchIntervalInBackground: true,
  });

  const symbolQueries = SYMBOLS.map((symbol) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useQuery({
      queryKey: ["fmp-live", "snapshot", symbol],
      queryFn: async () =>
        (await getFractalMarketProfileLiveSnapshot(symbol)).data as FMPSnap,
      refetchInterval: REFRESH_MS,
      refetchIntervalInBackground: true,
    }),
  );

  const auditQuery = useQuery({
    queryKey: ["fmp-live", "audit"],
    queryFn: async () =>
      (
        await apiClient.get(
          "/api/audit/events?market=fractal_market_profile&limit=30",
        )
      ).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const rows: Array<FMPSnap & { _key: string }> = useMemo(
    () =>
      SYMBOLS.map((s, i) => {
        const d = symbolQueries[i].data as FMPSnap | undefined;
        return { _key: s, ...(d ?? { symbol_code: s, signal: undefined }) };
      }),
    [symbolQueries],
  );

  const bucketedRows: BucketedRow[] = useMemo(
    () =>
      rows
        .filter((r) => r.signal)
        .map((r) => ({
          underlying: r.symbol_code,
          bucket: r.signal?.bucket,
          trajectory: r.signal?.trajectory,
          proximity_pct: r.signal?.proximity_pct,
          bucket_rationale: r.signal?.bucket_rationale,
        })),
    [rows],
  );

  const auditEvents: AuditEvent[] = useMemo(
    () => (auditQuery.data?.events ?? []) as AuditEvent[],
    [auditQuery.data],
  );

  const automation = summaryQuery.data?.automation ?? {};

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-bg-active/40 pb-2">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-tight">
            Fractal Market Profile · Live Data View
          </h1>
          <span className="text-xs text-text-muted">
            {SYMBOLS.join(" · ")}
          </span>
        </div>
        <div className="flex items-baseline gap-2 text-[11px]">
          <span
            className={`rounded border px-2 py-0.5 ${
              automation.loop_active
                ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                : "border-amber-500/50 bg-amber-500/10 text-amber-200"
            }`}
          >
            {automation.loop_active ? "armed" : "idle"}
          </span>
          <span className="text-text-muted">
            next run {formatIST(automation.next_run_at)}
          </span>
          <Link
            href="/fractal-market-profile"
            className="rounded border border-bg-active/60 px-2 py-0.5 text-text-muted hover:text-text-primary"
          >
            classic view →
          </Link>
        </div>
      </header>

      <div className="grid grid-cols-12 gap-3">
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Underlyings ({SYMBOLS.length})
          </h2>
          <table className="w-full text-xs">
            <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
              <tr>
                <th className="text-left">Symbol</th>
                <th className="text-left">Setup</th>
                <th className="text-left">Action</th>
                <th className="text-right">Conf</th>
                <th className="text-right">Close</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Stop</th>
                <th className="text-right">Tgt</th>
                <th className="text-left">Hourly</th>
                <th className="text-left">Daily</th>
                <th className="text-right">VA Mig</th>
                <th className="text-left">Bucket</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const s = r.signal;
                if (!s) {
                  return (
                    <tr
                      key={r._key}
                      className="border-t border-bg-active/20 text-text-muted"
                    >
                      <td className="py-1.5 font-medium text-text-primary">
                        {r.symbol_code}
                      </td>
                      <td colSpan={11} className="text-text-muted">
                        loading…
                      </td>
                    </tr>
                  );
                }
                return (
                  <tr
                    key={r._key}
                    className="border-t border-bg-active/20"
                  >
                    <td className="py-1.5 font-medium">{r.symbol_code}</td>
                    <td className="text-text-muted">{s.setup_name || "—"}</td>
                    <td>
                      <span
                        className={
                          s.action === "LONG"
                            ? "text-emerald-300"
                            : s.action === "SHORT"
                              ? "text-rose-300"
                              : "text-text-muted"
                        }
                      >
                        {s.action || "FLAT"}
                      </span>
                    </td>
                    <td className="text-right font-mono">
                      {formatPct((s.confidence ?? 0) * 100, 1)}
                    </td>
                    <td className="text-right font-mono">
                      {formatNumber(s.latest_close, 2)}
                    </td>
                    <td className="text-right font-mono">
                      {formatNumber(s.entry_trigger, 2)}
                    </td>
                    <td className="text-right font-mono text-rose-300">
                      {formatNumber(s.stop_level, 2)}
                    </td>
                    <td className="text-right font-mono text-emerald-300">
                      {formatNumber(s.target_level, 2)}
                    </td>
                    <td className="text-text-muted">{s.hourly_shape || "—"}</td>
                    <td className="text-text-muted">{s.daily_shape || "—"}</td>
                    <td className="text-right font-mono">
                      {formatNumber(s.value_migration_score, 2)}
                    </td>
                    <td>
                      <BucketBadge
                        bucket={s.bucket}
                        trajectory={s.trajectory}
                        proximity={s.proximity_pct}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Three-list view · FMP signals
          </h2>
          <ThreeListView rows={bucketedRows} />
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Recent rationale + filters
          </h2>
          <ul className="space-y-1 text-[11.5px]">
            {rows
              .filter((r) => r.signal)
              .map((r) => (
                <li
                  key={`${r._key}-rationale`}
                  className="border-b border-bg-active/15 pb-1"
                >
                  <div className="font-medium">{r.symbol_code}</div>
                  {(r.signal?.rationale ?? []).slice(0, 3).map((line, i) => (
                    <div key={i} className="text-text-secondary">
                      · {line}
                    </div>
                  ))}
                  {(r.signal?.filters ?? []).map((line, i) => (
                    <div key={`f${i}`} className="text-rose-300">
                      ✗ {line}
                    </div>
                  ))}
                </li>
              ))}
          </ul>
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Audit feed (market=fractal_market_profile) · last {auditEvents.length}
          </h2>
          <AuditFeed events={auditEvents} />
        </section>
      </div>
    </div>
  );
}
