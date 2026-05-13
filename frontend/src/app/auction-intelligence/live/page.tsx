"use client";

/**
 * Auction Intelligence · data-focused live view.
 *
 * Renders the three sub-agents (positional / swing / scalp) decision rows
 * with bucket classification, plus the live MP coordinates, order-flow
 * snapshot, and Gate B/C readiness inline. No tabs.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  api as apiClient,
  getAuctionIntelligenceLiveSnapshot,
  getAuctionIntelligenceSummary,
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
} from "@/components/strategy/desk-helpers";

const REFRESH_MS = 5_000;
const SYMBOLS = ["NIFTY", "BANKNIFTY"] as const;

type AgentDecision = {
  agent_name?: string;
  action?: string;
  confidence?: number;
  entry_price?: number | null;
  stop_price?: number | null;
  target_price?: number | null;
  quantity?: number | null;
  sleeve_fraction?: number | null;
  rationale?: string[];
  metadata?: Record<string, unknown>;
  bucket?: Bucket;
  trajectory?: Trajectory;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
};

type AnalysisBundle = {
  market_profile?: {
    poc?: number;
    vah?: number;
    val?: number;
    initial_balance_high?: number;
    initial_balance_low?: number;
    day_type?: string;
  };
  order_flow?: {
    timing_confidence?: number;
    cumulative_delta?: number;
    queue_pressure?: number;
    spread?: number;
    toxicity_score?: number;
  };
  regime?: { label?: string; allowed_directions?: string[] };
  agent_decisions?: AgentDecision[];
  risk?: { allowed?: boolean; reasons?: string[]; kill_switch?: boolean };
  execution_plan?: Array<Record<string, unknown>>;
};

type LiveSnapshot = {
  symbol_code?: string;
  session_date?: string;
  analysis?: AnalysisBundle;
  data_status?: Record<string, unknown>;
};

export default function AuctionIntelligenceLivePage() {
  const summaryQuery = useQuery({
    queryKey: ["ai-live", "summary"],
    queryFn: async () => (await getAuctionIntelligenceSummary()).data,
    refetchInterval: REFRESH_MS * 4,
    refetchIntervalInBackground: true,
  });

  const niftyQuery = useQuery({
    queryKey: ["ai-live", "snapshot", "NIFTY"],
    queryFn: async () =>
      (await getAuctionIntelligenceLiveSnapshot("NIFTY")).data as LiveSnapshot,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const bankNiftyQuery = useQuery({
    queryKey: ["ai-live", "snapshot", "BANKNIFTY"],
    queryFn: async () =>
      (await getAuctionIntelligenceLiveSnapshot("BANKNIFTY"))
        .data as LiveSnapshot,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const auditQuery = useQuery({
    queryKey: ["ai-live", "audit"],
    queryFn: async () =>
      (
        await apiClient.get(
          "/api/audit/events?market=auction_intelligence&limit=30",
        )
      ).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const snapshots = useMemo(
    () => [niftyQuery.data, bankNiftyQuery.data].filter(Boolean) as LiveSnapshot[],
    [niftyQuery.data, bankNiftyQuery.data],
  );

  const auditEvents: AuditEvent[] = useMemo(
    () => (auditQuery.data?.events ?? []) as AuditEvent[],
    [auditQuery.data],
  );

  const allDecisions: BucketedRow[] = useMemo(
    () =>
      snapshots.flatMap((snap) =>
        (snap.analysis?.agent_decisions ?? []).map((d) => ({
          symbol: snap.symbol_code,
          underlying: `${snap.symbol_code}·${d.agent_name}`,
          bucket: d.bucket,
          trajectory: d.trajectory,
          proximity_pct: d.proximity_pct,
          bucket_rationale: d.bucket_rationale,
        })),
      ),
    [snapshots],
  );

  const automation = summaryQuery.data?.automation ?? {};

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-bg-active/40 pb-2">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-tight">
            Auction Intelligence · Live Data View
          </h1>
          <span className="text-xs text-text-muted">
            symbols {SYMBOLS.join(", ")} · agents positional / swing / scalp
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
            next run {formatIST(automation.next_run_at)} · last finished{" "}
            {formatIST(automation.last_finished_at)}
          </span>
          <Link
            href="/auction-intelligence"
            className="rounded border border-bg-active/60 px-2 py-0.5 text-text-muted hover:text-text-primary"
          >
            classic view →
          </Link>
        </div>
      </header>

      <div className="grid grid-cols-12 gap-3">
        {snapshots.map((snap) => {
          const mp = snap.analysis?.market_profile ?? {};
          const flow = snap.analysis?.order_flow ?? {};
          const regime = snap.analysis?.regime ?? {};
          const decisions = snap.analysis?.agent_decisions ?? [];
          const risk = snap.analysis?.risk ?? {};
          return (
            <section
              key={snap.symbol_code}
              className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6"
            >
              <div className="mb-1 flex items-baseline justify-between">
                <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-primary">
                  {snap.symbol_code}
                </h2>
                <span className="text-[10.5px] text-text-muted">
                  session {snap.session_date} · regime{" "}
                  <span className="text-text-primary">{regime.label || "—"}</span>
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-[11px]">
                <div className="rounded border border-bg-active/30 px-2 py-1.5">
                  <div className="text-[10px] uppercase tracking-wide text-text-muted">
                    Market Profile
                  </div>
                  <div className="grid grid-cols-2 gap-1 font-mono">
                    <span>POC</span>
                    <span className="text-right">{formatNumber(mp.poc, 2)}</span>
                    <span>VAH</span>
                    <span className="text-right">{formatNumber(mp.vah, 2)}</span>
                    <span>VAL</span>
                    <span className="text-right">{formatNumber(mp.val, 2)}</span>
                    <span>IB hi</span>
                    <span className="text-right">
                      {formatNumber(mp.initial_balance_high, 2)}
                    </span>
                    <span>IB lo</span>
                    <span className="text-right">
                      {formatNumber(mp.initial_balance_low, 2)}
                    </span>
                    <span>Day</span>
                    <span className="text-right text-text-secondary">
                      {mp.day_type || "—"}
                    </span>
                  </div>
                </div>
                <div className="rounded border border-bg-active/30 px-2 py-1.5">
                  <div className="text-[10px] uppercase tracking-wide text-text-muted">
                    Order Flow
                  </div>
                  <div className="grid grid-cols-2 gap-1 font-mono">
                    <span>Timing</span>
                    <span className="text-right">
                      {formatPct((flow.timing_confidence ?? 0) * 100, 1)}
                    </span>
                    <span>Cum Δ</span>
                    <span className="text-right">
                      {formatNumber(flow.cumulative_delta, 0)}
                    </span>
                    <span>Queue</span>
                    <span className="text-right">
                      {formatNumber(flow.queue_pressure, 2)}
                    </span>
                    <span>Spread</span>
                    <span className="text-right">
                      {formatNumber(flow.spread, 3)}
                    </span>
                    <span>Toxicity</span>
                    <span className="text-right">
                      {formatNumber(flow.toxicity_score, 2)}
                    </span>
                  </div>
                </div>
              </div>
              <div className="mt-2">
                <div className="mb-1 text-[10.5px] uppercase tracking-wide text-text-muted">
                  Agent decisions ({decisions.length})
                </div>
                <table className="w-full text-xs">
                  <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                    <tr>
                      <th className="text-left">Agent</th>
                      <th className="text-left">Action</th>
                      <th className="text-right">Conf</th>
                      <th className="text-right">Entry</th>
                      <th className="text-right">Stop</th>
                      <th className="text-right">Tgt</th>
                      <th className="text-left">Bucket</th>
                    </tr>
                  </thead>
                  <tbody>
                    {decisions.length === 0 ? (
                      <tr>
                        <td
                          colSpan={7}
                          className="py-2 text-center text-text-muted"
                        >
                          no decisions
                        </td>
                      </tr>
                    ) : (
                      decisions.map((d, idx) => (
                        <tr
                          key={`${snap.symbol_code}-${d.agent_name}-${idx}`}
                          className="border-t border-bg-active/20"
                        >
                          <td className="py-1 font-medium">{d.agent_name}</td>
                          <td>
                            <span
                              className={
                                d.action === "LONG"
                                  ? "text-emerald-300"
                                  : d.action === "SHORT"
                                    ? "text-rose-300"
                                    : "text-text-muted"
                              }
                            >
                              {d.action}
                            </span>
                          </td>
                          <td className="text-right font-mono">
                            {formatPct((d.confidence ?? 0) * 100, 1)}
                          </td>
                          <td className="text-right font-mono">
                            {formatNumber(d.entry_price, 2)}
                          </td>
                          <td className="text-right font-mono text-rose-300">
                            {formatNumber(d.stop_price, 2)}
                          </td>
                          <td className="text-right font-mono text-emerald-300">
                            {formatNumber(d.target_price, 2)}
                          </td>
                          <td>
                            <BucketBadge
                              bucket={d.bucket}
                              trajectory={d.trajectory}
                              proximity={d.proximity_pct}
                            />
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
              {risk?.allowed === false ? (
                <div className="mt-1 rounded border border-rose-500/40 bg-rose-500/5 px-2 py-1 text-[10.5px] text-rose-200">
                  Risk blocked: {(risk.reasons ?? []).join("; ") || "—"}
                </div>
              ) : null}
            </section>
          );
        })}

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Three-list view · all agents
          </h2>
          <ThreeListView rows={allDecisions} />
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Audit feed (market=auction_intelligence) · last {auditEvents.length}
          </h2>
          <AuditFeed events={auditEvents} />
        </section>
      </div>
    </div>
  );
}
