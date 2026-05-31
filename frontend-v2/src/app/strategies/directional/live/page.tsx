"use client";

/**
 * Directional Options · data-focused live view.
 *
 * Per underlying: regime, signal direction, selected contract, risk verdict,
 * bucket classification. Same visual language as the other desks.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useQueries, useQuery } from "@tanstack/react-query";

import {
  api as apiClient,
  getDirectionalOptionsLiveSnapshot,
  getDirectionalOptionsSummary,
} from "@/lib/api";
import {
  AuditFeed,
  BucketBadge,
  ThreeListView,
  formatIST,
  formatNumber,
  formatPct,
  type AuditEvent,
  type Bucket,
  type BucketedRow,
  type Trajectory,
} from "@/components/v1-strategy/desk-helpers";

const REFRESH_MS = 6_000;
// Indices-only after the RL/indices-only refactor — commodity
// underlyings are out of scope for this engine.
const DEFAULT_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"];

type DOSnap = {
  as_of?: string;
  underlying?: string;
  timeframe?: string;
  spot_price?: number;
  feature_snapshot?: Record<string, unknown>;
  regime?: { label?: string; allowed_directions?: string[] };
  signal?: {
    direction?: "CE" | "PE" | null;
    strength?: number;
    confidence?: number;
    rationale?: string[];
  } | null;
  selected_contract?: {
    trading_symbol?: string;
    strike?: number;
    option_type?: string;
    ltp?: number;
    option_price?: number;
    iv_pct?: number;
    iv?: number;
    implied_vol?: number;
    days_to_expiry?: number;
  } | null;
  risk?: {
    approved?: boolean;
    sleeve_fraction?: number;
    contracts?: number;
    reasons?: string[];
  } | null;
  selection_reason?: string;
  bucket?: Bucket;
  trajectory?: Trajectory;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
};

export default function DirectionalOptionsLivePage() {
  const summaryQuery = useQuery({
    queryKey: ["do-live", "summary"],
    queryFn: async () => (await getDirectionalOptionsSummary()).data,
    refetchInterval: REFRESH_MS * 5,
    refetchIntervalInBackground: true,
  });

  const symbols = useMemo(() => {
    const configured = summaryQuery.data?.underlyings;
    return Array.isArray(configured) && configured.length
      ? configured.map((symbol: unknown) => String(symbol)).filter(Boolean)
      : DEFAULT_SYMBOLS;
  }, [summaryQuery.data?.underlyings]);

  const symbolQueries = useQueries({
    queries: symbols.map((symbol) => ({
      queryKey: ["do-live", "snapshot", symbol],
      queryFn: async () => {
        const payload = (await getDirectionalOptionsLiveSnapshot(symbol)).data;
        return (payload?.snapshot ?? payload) as DOSnap;
      },
      refetchInterval: REFRESH_MS,
      refetchIntervalInBackground: true,
    })),
  });

  const auditQuery = useQuery({
    queryKey: ["do-live", "audit"],
    queryFn: async () =>
      (
        await apiClient.get(
          "/api/audit/events?market=directional_options&limit=30",
        )
      ).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const rows: Array<DOSnap & { _key: string }> = useMemo(
    () =>
      symbols.map((s, i) => {
        const d = symbolQueries[i].data as DOSnap | undefined;
        return { _key: s, ...(d ?? { underlying: s }) };
      }),
    [symbolQueries, symbols],
  );

  const bucketedRows: BucketedRow[] = useMemo(
    () =>
      rows
        .filter((r) => r.bucket)
        .map((r) => ({
          underlying: r.underlying || r._key,
          bucket: r.bucket,
          trajectory: r.trajectory,
          proximity_pct: r.proximity_pct,
          bucket_rationale: r.bucket_rationale,
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
            Directional Options · Live Data View
          </h1>
          <span className="text-xs text-text-muted">
            {symbols.join(" · ")} · long-only premium
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
            href="/directional-options"
            className="rounded border border-bg-active/60 px-2 py-0.5 text-text-muted hover:text-text-primary"
          >
            classic view →
          </Link>
        </div>
      </header>

      <div className="grid grid-cols-12 gap-3">
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Underlyings · selected contracts
          </h2>
          <table className="w-full text-xs">
            <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
              <tr>
                <th className="text-left">Symbol</th>
                <th className="text-right">Spot</th>
                <th className="text-left">Regime</th>
                <th className="text-left">Sig</th>
                <th className="text-right">Str</th>
                <th className="text-left">Contract</th>
                <th className="text-right">LTP</th>
                <th className="text-right">IV%</th>
                <th className="text-right">TTE</th>
                <th className="text-left">Risk</th>
                <th className="text-left">Bucket</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const sig = r.signal;
                const c = r.selected_contract;
                const risk = r.risk;
                const contractLtp = c?.ltp ?? c?.option_price;
                const contractIv = c?.iv_pct ?? c?.iv ?? (
                  typeof c?.implied_vol === "number" && c.implied_vol <= 1
                    ? c.implied_vol * 100
                    : c?.implied_vol
                );
                const signalStrength = sig?.strength ?? sig?.confidence;
                return (
                  <tr
                    key={r._key}
                    className="border-t border-bg-active/20"
                  >
                    <td className="py-1.5 font-medium">{r.underlying}</td>
                    <td className="text-right font-mono">
                      {formatNumber(r.spot_price, 2)}
                    </td>
                    <td className="text-text-muted">
                      {r.regime?.label || "—"}
                    </td>
                    <td>
                      <span
                        className={
                          sig?.direction === "CE"
                            ? "text-emerald-300"
                            : sig?.direction === "PE"
                              ? "text-rose-300"
                              : "text-text-muted"
                        }
                      >
                        {sig?.direction || "—"}
                      </span>
                    </td>
                    <td className="text-right font-mono">
                      {formatNumber(signalStrength, 2)}
                    </td>
                    <td className="font-mono text-[10.5px]">
                      {c ? `${c.option_type || ""} ${c.strike || ""}` : "—"}
                    </td>
                    <td className="text-right font-mono">
                      {formatNumber(contractLtp, 2)}
                    </td>
                    <td className="text-right font-mono">
                      {formatNumber(contractIv, 1)}
                    </td>
                    <td className="text-right font-mono">
                      {c?.days_to_expiry ?? "—"}
                    </td>
                    <td>
                      {risk ? (
                        <span
                          className={
                            risk.approved
                              ? "text-emerald-300"
                              : "text-rose-300"
                          }
                        >
                          {risk.approved ? "OK" : "block"}
                        </span>
                      ) : (
                        <span className="text-text-muted">—</span>
                      )}
                    </td>
                    <td>
                      <BucketBadge
                        bucket={r.bucket}
                        trajectory={r.trajectory}
                        proximity={r.proximity_pct}
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
            Three-list view · DO signals
          </h2>
          <ThreeListView rows={bucketedRows} />
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Selection reasons + risk
          </h2>
          <ul className="space-y-1 text-[11.5px]">
            {rows.map((r) => (
              <li
                key={`${r._key}-reason`}
                className="border-b border-bg-active/15 pb-1"
              >
                <div className="font-medium">{r.underlying}</div>
                <div className="text-text-secondary">
                  {r.selection_reason || "—"}
                </div>
                {r.risk?.approved === false ? (
                  <div className="text-rose-300">
                    ✗ {(r.risk?.reasons ?? []).join("; ") || "blocked"}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Audit feed (market=directional_options) · last {auditEvents.length}
          </h2>
          <AuditFeed events={auditEvents} />
        </section>
      </div>
    </div>
  );
}
