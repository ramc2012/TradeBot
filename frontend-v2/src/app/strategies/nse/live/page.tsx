"use client";

/**
 * NSE strategy desk · data-focused live view.
 *
 * Renders Strategy 1 (30m ATM MACD) + Strategy 2 (5m Index MACD + MP)
 * in a single dense screen. Same visual language as /commodity.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  api as apiClient,
  getStrategyAgentStatus,
  getPositions,
} from "@/lib/api";
import {
  AuditFeed,
  BUCKET_COLOR,
  BucketBadge,
  ThreeListView,
  formatINR,
  formatIST,
  formatNumber,
  formatPct,
  relativeAge,
  type AuditEvent,
  type BucketedRow,
} from "@/components/v1-strategy/desk-helpers";

const REFRESH_MS = 4_000;

type SignalRow = BucketedRow & {
  strategy?: string;
  direction?: string;
  status?: string | null;
  reason?: string;
  instruction?: string;
  expiry?: string;
  atm_strike?: number;
  ltp?: number | null;
  iv_pct?: number | null;
  priority_score?: number | null;
  spot_price?: number | null;
  mp_day_type?: string | null;
  ce_macd?: number | null;
  pe_macd?: number | null;
  freshness?: string | null;
  signal_date?: string | null;
};

type LanePayload = {
  key: string;
  label: string;
  status?: string;
  open_positions?: number;
  entries?: number;
  exits?: number;
  last_scan_at?: string;
  signal_lane?: SignalRow[];
  portfolio_summary?: {
    total_equity?: number;
    realized_pnl?: number;
    available_capital?: number;
    initial_capital?: number;
  };
};

type AgentStatus = {
  enabled?: boolean;
  loop_active?: boolean;
  running?: boolean;
  kill_switch_active?: boolean;
  last_run_at?: string;
  last_message?: string;
  last_error?: string | null;
  strategies?: LanePayload[];
};

export default function StrategyLivePage() {
  const statusQuery = useQuery({
    queryKey: ["nse-live", "status"],
    queryFn: async () => (await getStrategyAgentStatus()).data as AgentStatus,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const positionsQuery = useQuery({
    queryKey: ["nse-live", "positions"],
    queryFn: async () => (await getPositions()).data,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const auditQuery = useQuery({
    queryKey: ["nse-live", "audit"],
    queryFn: async () =>
      (await apiClient.get("/api/audit/events?market=nse&limit=30")).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const status = statusQuery.data ?? {};
  const strategies: LanePayload[] = useMemo(
    () => status.strategies ?? [],
    [status.strategies],
  );
  const positions = useMemo(
    () => (positionsQuery.data as Record<string, unknown>[] | undefined) ?? [],
    [positionsQuery.data],
  );
  const auditEvents: AuditEvent[] = useMemo(
    () => (auditQuery.data?.events ?? []) as AuditEvent[],
    [auditQuery.data],
  );

  const allSignals: SignalRow[] = useMemo(
    () => strategies.flatMap((s) => s.signal_lane ?? []),
    [strategies],
  );

  const totalEquity = strategies.reduce(
    (sum, s) => sum + Number(s.portfolio_summary?.total_equity ?? 0),
    0,
  );
  const totalRealized = strategies.reduce(
    (sum, s) => sum + Number(s.portfolio_summary?.realized_pnl ?? 0),
    0,
  );
  const totalOpen = strategies.reduce(
    (sum, s) => sum + Number(s.open_positions ?? 0),
    0,
  );

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-bg-active/40 pb-2">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-tight">
            NSE Desk · Live Data View
          </h1>
          <span className="text-xs text-text-muted">
            total equity {formatINR(totalEquity)}{" "}
            <span
              className={totalRealized >= 0 ? "text-emerald-400" : "text-rose-400"}
            >
              ({formatINR(totalRealized)} realized)
            </span>
          </span>
          <span className="text-xs text-text-muted">
            open {totalOpen} · strategies {strategies.length}
          </span>
        </div>
        <div className="flex items-baseline gap-2 text-[11px]">
          <span
            className={`rounded border px-2 py-0.5 ${
              status.kill_switch_active
                ? "border-rose-500/60 bg-rose-500/10 text-rose-300"
                : status.running
                  ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/50 bg-amber-500/10 text-amber-200"
            }`}
          >
            {status.kill_switch_active
              ? "KILL"
              : status.running
                ? "scanning"
                : "idle"}
          </span>
          <span className="text-text-muted">
            last scan {formatIST(status.last_run_at)}
          </span>
          <Link
            href="/strategy"
            className="rounded border border-bg-active/60 px-2 py-0.5 text-text-muted hover:text-text-primary"
          >
            classic view →
          </Link>
        </div>
      </header>

      {(status.last_error || status.last_message) ? (
        <div className="mb-2 rounded border border-bg-active/30 bg-bg-secondary/20 px-3 py-1.5 text-[11.5px] text-text-secondary">
          {status.last_error ? (
            <span className="text-rose-300">{status.last_error}</span>
          ) : (
            status.last_message
          )}
        </div>
      ) : null}

      <div className="grid grid-cols-12 gap-3">
        {strategies.map((lane) => {
          const rows = lane.signal_lane ?? [];
          return (
            <section
              key={lane.key}
              className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2"
            >
              <div className="mb-1 flex items-baseline justify-between">
                <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-primary">
                  {lane.label}
                </h2>
                <span className="text-[10.5px] text-text-muted">
                  status {lane.status || "—"} · open {lane.open_positions ?? 0} ·
                  entries {lane.entries ?? 0} · exits {lane.exits ?? 0} · last{" "}
                  {formatIST(lane.last_scan_at)}
                </span>
              </div>
              {rows.length === 0 ? (
                <div className="px-2 py-3 text-xs text-text-muted">
                  no signal lane rows
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                    <tr>
                      <th className="text-left">Underlying</th>
                      <th className="text-left">Dir</th>
                      <th className="text-right">Spot</th>
                      <th className="text-right">Strike</th>
                      <th className="text-right">LTP</th>
                      <th className="text-right">IV%</th>
                      <th className="text-right">CE MACD</th>
                      <th className="text-right">PE MACD</th>
                      <th className="text-left">MP</th>
                      <th className="text-left">Status</th>
                      <th className="text-left">Bucket</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, idx) => (
                      <tr
                        key={`${r.underlying || r.symbol || "row"}-${idx}`}
                        className="border-t border-bg-active/20"
                      >
                        <td className="py-1.5 font-medium">
                          {r.underlying || r.symbol}
                        </td>
                        <td>{r.direction || "—"}</td>
                        <td className="text-right font-mono">
                          {formatNumber(r.spot_price, 2)}
                        </td>
                        <td className="text-right font-mono">
                          {formatNumber(r.atm_strike, 0)}
                        </td>
                        <td className="text-right font-mono">
                          {formatNumber(r.ltp, 2)}
                        </td>
                        <td className="text-right font-mono">
                          {formatNumber(r.iv_pct, 1)}
                        </td>
                        <td className="text-right font-mono">
                          {formatNumber(r.ce_macd, 2)}
                        </td>
                        <td className="text-right font-mono">
                          {formatNumber(r.pe_macd, 2)}
                        </td>
                        <td className="text-text-muted">
                          {r.mp_day_type || "—"}
                        </td>
                        <td className="text-text-muted">
                          {r.status || "—"}
                        </td>
                        <td>
                          <BucketBadge
                            bucket={r.bucket}
                            trajectory={r.trajectory}
                            proximity={r.proximity_pct}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          );
        })}

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Three-list view · all NSE signals
          </h2>
          <ThreeListView rows={allSignals} />
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Open positions ({positions.length})
          </h2>
          {positions.length === 0 ? (
            <div className="px-2 py-3 text-xs text-text-muted">
              no open positions
            </div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th className="text-left">Symbol</th>
                  <th className="text-right">Qty</th>
                  <th className="text-right">Entry</th>
                  <th className="text-right">Now</th>
                  <th className="text-right">P&L</th>
                  <th className="text-right">Ret%</th>
                </tr>
              </thead>
              <tbody>
                {positions.slice(0, 14).map((p, idx) => {
                  const obj = p as Record<string, unknown>;
                  return (
                    <tr
                      key={`${String(obj.symbol || idx)}`}
                      className="border-t border-bg-active/20"
                    >
                      <td className="py-1 font-medium">
                        {String(obj.symbol || "—")}
                      </td>
                      <td className="text-right font-mono">
                        {String(obj.qty || "—")}
                      </td>
                      <td className="text-right font-mono">
                        {formatNumber(Number(obj.entry_price ?? 0), 2)}
                      </td>
                      <td className="text-right font-mono">
                        {formatNumber(Number(obj.current_price ?? 0), 2)}
                      </td>
                      <td
                        className={`text-right font-mono ${
                          Number(obj.unrealized_pnl ?? 0) >= 0
                            ? "text-emerald-400"
                            : "text-rose-400"
                        }`}
                      >
                        {formatINR(Number(obj.unrealized_pnl ?? 0), 0)}
                      </td>
                      <td
                        className={`text-right font-mono ${
                          Number(obj.return_pct ?? 0) >= 0
                            ? "text-emerald-400"
                            : "text-rose-400"
                        }`}
                      >
                        {formatPct(Number(obj.return_pct ?? 0), 1)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>

        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Audit feed · last {auditEvents.length} (market=nse)
          </h2>
          <AuditFeed events={auditEvents} />
        </section>
      </div>
    </div>
  );
}
