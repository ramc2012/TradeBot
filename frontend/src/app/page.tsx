"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Layers3,
  Shield,
  X,
} from "lucide-react";

import type { StrategyAgentStatus } from "@/components/trading/StrategyAgentMonitor";
import {
  SystemHealthBoard,
  type SystemHealthResponse,
} from "@/components/system/SystemHealthBoard";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import {
  getSystemOverview,
} from "@/lib/api";
import { createSystemOverviewSocket } from "@/lib/websocket";

type StrategyLaneSummary = StrategyAgentStatus["strategies"][number];

type CommodityLaneSummary = {
  key: string;
  label: string;
  agent?: {
    timeframe?: string | null;
    scope?: string | null;
  } | null;
  summary?: {
    total_equity?: number | null;
    realized_pnl?: number | null;
    unrealized_pnl?: number | null;
    open_positions?: number | null;
    total_trades?: number | null;
  };
};

type SystemOverview = {
  generated_at: string;
  health: SystemHealthResponse;
  books: {
    combined: {
      equity?: number | null;
      realized_pnl?: number | null;
      open_pnl?: number | null;
      open_positions?: number | null;
    };
    manual: {
      total_pnl?: number | null;
      total_trades?: number | null;
      win_rate?: number | null;
      profit_factor?: number | null;
      open_positions?: number | null;
      open_pnl?: number | null;
    };
    nse_strategy: {
      equity?: number | null;
      realized_pnl?: number | null;
      open_pnl?: number | null;
      strategies?: StrategyLaneSummary[];
      status?: { last_run_at?: string | null };
    };
    commodity_strategy: {
      equity?: number | null;
      realized_pnl?: number | null;
      open_pnl?: number | null;
      strategies?: CommodityLaneSummary[];
      status?: { last_run_at?: string | null };
    };
  };
  risk: {
    trading_allowed?: boolean;
    open_positions?: number | null;
    max_positions?: number | null;
    sizing_mode?: string | null;
    daily_loss?: number | null;
    max_daily_loss?: number | null;
  };
  auction_intelligence: {
    live_ready?: boolean;
    deployable_first_sleeve?: string | null;
    connected_brokers?: string[];
  };
  blockers: Array<{ key: string; label: string; status: string; detail: string }>;
};

function formatMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatSigned(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function tone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function serviceStateTone(status?: string | null) {
  if (status === "healthy" || status === "active" || status === "ready") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (status === "degraded" || status === "warning" || status === "stale") {
    return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  }
  if (status === "critical" || status === "error") {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-border bg-bg-secondary/28 text-text-secondary";
}

function isLaneRunning(status?: string | null) {
  const normalized = String(status || "").toLowerCase();
  return normalized !== "" && !["idle", "paused", "stopped", "critical", "error"].includes(normalized);
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

function MetricTile({
  label,
  value,
  detail,
  color,
}: {
  label: string;
  value: string;
  detail: string;
  color?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-1.5 font-mono text-base font-semibold text-text-primary xl:text-lg", color)}>{value}</div>
      <div className="mt-1 line-clamp-2 text-[11px] text-text-muted">{detail}</div>
    </div>
  );
}

function LaneCard({
  label,
  timeframe,
  scope,
  openPositions,
  realized,
  unrealized,
  totalTrades,
  lastRunAt,
}: {
  label: string;
  timeframe?: string | null;
  scope?: string | null;
  openPositions?: number | null;
  realized?: number | null;
  unrealized?: number | null;
  totalTrades?: number | null;
  lastRunAt?: string | null;
}) {
  return (
    <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text-primary">{label}</div>
          <div className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-muted">
            {timeframe || "--"} · {scope || "Strategy lane"}
          </div>
        </div>
        <div className="text-right text-xs text-text-muted">
          <div>Open {openPositions ?? 0}</div>
          <div className="mt-1">Trades {totalTrades ?? 0}</div>
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Realized</div>
          <div className={clsx("mt-1 font-mono", tone(realized))}>{formatSigned(realized, 0)}</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Open P&amp;L</div>
          <div className={clsx("mt-1 font-mono", tone(unrealized))}>{formatSigned(unrealized, 0)}</div>
        </div>
      </div>
      <div className="mt-3 text-[11px] text-text-muted">Last run {formatTimestamp(lastRunAt)}</div>
    </div>
  );
}


export default function HomePage() {
  const [showAttention, setShowAttention] = useState(false);
  const overviewQuery = useLiveSnapshotQuery<SystemOverview>({
    queryKey: ["systemOverview"],
    queryFn: () => getSystemOverview().then((response) => response.data as SystemOverview),
    streamFactory: (onData, onStatusChange) =>
      createSystemOverviewSocket((data) => onData(data as SystemOverview), onStatusChange),
    staleTime: 10_000,
  });

  const overview = overviewQuery.data;
  const health = overview?.health;
  const nseStrategies = overview?.books.nse_strategy.strategies || [];
  const commodityStrategies = overview?.books.commodity_strategy.strategies || [];
  const totalEquity = overview?.books.combined.equity || 0;
  const totalRealized = overview?.books.combined.realized_pnl || 0;
  const totalOpenPnl = overview?.books.combined.open_pnl || 0;
  const totalOpenPositions = overview?.books.combined.open_positions || 0;
  const nseRealized = overview?.books.nse_strategy.realized_pnl || 0;
  const nseOpenPnl = overview?.books.nse_strategy.open_pnl || 0;
  const nseTrades = nseStrategies.reduce((sum, item) => sum + (item.summary.total_trades || 0), 0);
  const nseOpenPositions = nseStrategies.reduce((sum, item) => sum + (item.summary.open_positions || 0), 0);
  const commodityRealized = overview?.books.commodity_strategy.realized_pnl || 0;
  const commodityOpenPnl = overview?.books.commodity_strategy.open_pnl || 0;
  const commodityTrades = commodityStrategies.reduce((sum, item) => sum + (item.summary?.total_trades || 0), 0);
  const commodityOpenPositions = commodityStrategies.reduce((sum, item) => sum + (item.summary?.open_positions || 0), 0);
  const manual = overview?.books.manual;
  const auctionSummary = overview?.auction_intelligence;
  const topBlockers = overview?.blockers || [];
  const blockerCount = topBlockers.length;
  const generatedAt = overview?.generated_at;
  const configuredStrategies = nseStrategies.length + commodityStrategies.length;
  const runningStrategies = (health?.strategy_lanes || []).filter((lane) => isLaneRunning(lane.status)).length;
  const overallServiceState = health?.summary.status || "idle";

  return (
    <div className="mx-auto max-w-[1680px] space-y-6 pb-10">
      <section className="overflow-hidden rounded-[28px] border border-bg-active/60 bg-[radial-gradient(circle_at_top_left,rgba(0,212,163,0.08),transparent_26%),radial-gradient(circle_at_top_right,rgba(59,130,246,0.08),transparent_26%),linear-gradient(180deg,rgba(15,23,36,0.96),rgba(7,10,21,0.98))] px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-[260px]">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-text-muted">
              <Shield size={13} className="text-accent-green" />
              Overview
            </div>
            <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-text-muted">
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1">
                Updated {formatTimestamp(generatedAt)}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1">
                {blockerCount} blocker{blockerCount === 1 ? "" : "s"}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1">
                Risk {overview?.risk?.sizing_mode || "--"}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1">
                Sleeve {auctionSummary?.deployable_first_sleeve || "--"}
              </span>
            </div>
          </div>
          <div className="grid min-w-[300px] flex-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricTile label="App Equity" value={formatMoney(totalEquity)} detail="Combined books" color={tone(totalOpenPnl + totalRealized)} />
            <MetricTile label="Open Positions" value={String(totalOpenPositions)} detail="Manual and strategy lanes" />
            <MetricTile label="Realized P&L" value={formatSigned(totalRealized)} detail={`Manual trades ${manual?.total_trades || 0}`} color={tone(totalRealized)} />
            <button
              type="button"
              onClick={() => setShowAttention(true)}
              className={clsx(
                "rounded-2xl border px-4 py-3 text-left transition-colors hover:border-bg-active",
                serviceStateTone(overallServiceState),
              )}
            >
              <div className="text-[11px] uppercase tracking-[0.16em] text-current/80">Service State</div>
              <div className="mt-1.5 font-mono text-base font-semibold uppercase xl:text-lg">{overallServiceState}</div>
              <div className="mt-1 text-[11px] text-current/80">
                {health?.summary.critical_services || 0} critical · {health?.summary.degraded_services || 0} degraded · click for operator attention
              </div>
            </button>
          </div>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="NSE Strategy P&L" value={formatSigned(nseRealized + nseOpenPnl)} detail={`${nseTrades} closed trades · ${nseOpenPositions} open`} color={tone(nseRealized + nseOpenPnl)} />
        <MetricTile label="Commodity Strategy P&L" value={formatSigned(commodityRealized + commodityOpenPnl)} detail={`${commodityTrades} closed trades · ${commodityOpenPositions} open`} color={tone(commodityRealized + commodityOpenPnl)} />
        <MetricTile
          label="Strategies"
          value={`${runningStrategies}/${configuredStrategies || 0}`}
          detail={`${configuredStrategies || 0} configured · ${(health?.strategy_lanes || []).length} monitored`}
          color={runningStrategies > 0 ? "text-accent-green" : "text-accent-amber"}
        />
        <MetricTile
          label="Auction IQ"
          value={auctionSummary?.live_ready ? "ready" : "validation"}
          detail={`First sleeve ${auctionSummary?.deployable_first_sleeve || "--"} · Brokers ${(auctionSummary?.connected_brokers || []).join(", ") || "--"}`}
          color={auctionSummary?.live_ready ? "text-accent-green" : "text-accent-blue"}
        />
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-text-primary">Strategy Lanes</div>
            <div className="mt-1 text-xs text-text-muted">Attribution, cadence, and open risk by lane.</div>
          </div>
          <div className="text-xs text-text-muted">{(health?.strategy_lanes || []).length} lanes</div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {nseStrategies.map((strategy) => (
            <LaneCard
              key={strategy.key}
              label={strategy.label}
              timeframe={strategy.agent?.timeframe || null}
              scope={strategy.agent?.scope || null}
              openPositions={strategy.summary.open_positions}
              realized={strategy.summary.realized_pnl}
              unrealized={strategy.summary.unrealized_pnl}
              totalTrades={strategy.summary.total_trades}
              lastRunAt={strategy.last_scan_at || overview?.books.nse_strategy.status?.last_run_at}
            />
          ))}
          {commodityStrategies.map((strategy) => (
            <LaneCard
              key={strategy.key}
              label={strategy.label}
              timeframe={strategy.agent?.timeframe || null}
              scope={strategy.agent?.scope || null}
              openPositions={strategy.summary?.open_positions}
              realized={strategy.summary?.realized_pnl}
              unrealized={strategy.summary?.unrealized_pnl}
              totalTrades={strategy.summary?.total_trades}
              lastRunAt={overview?.books.commodity_strategy.status?.last_run_at}
            />
          ))}
        </div>
      </section>

      {health ? (
        <section className="rounded-[24px] border border-bg-border bg-bg-secondary/18 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-text-primary">Service Health Snapshot</div>
              <div className="mt-1 text-xs text-text-muted">Live service summary for the paper runtime and shared data plane.</div>
            </div>
            <button
              type="button"
              onClick={() => setShowAttention(true)}
              className={clsx(
                "rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]",
                serviceStateTone(overallServiceState),
              )}
            >
              {overallServiceState}
            </button>
          </div>
          <div className="mt-4">
          <SystemHealthBoard health={health} compact includeLanes={false} />
          </div>
        </section>
      ) : null}

      {showAttention ? (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-bg-primary/78 px-4 py-8 backdrop-blur-sm">
          <div className="max-h-[86vh] w-full max-w-[1120px] overflow-y-auto rounded-[28px] border border-bg-active bg-[linear-gradient(180deg,rgba(15,23,36,0.98),rgba(7,10,21,0.99))] p-5 shadow-[0_30px_90px_rgba(0,0,0,0.45)]">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-[11px] uppercase tracking-[0.2em] text-text-muted">Operator Attention</div>
                <div className="mt-2 text-2xl font-semibold text-text-primary">Service State</div>
                <div className="mt-1 text-sm text-text-muted">
                  Review blockers and the current runtime state without leaving the dashboard.
                </div>
              </div>
              <button
                type="button"
                onClick={() => setShowAttention(false)}
                className="rounded-xl border border-bg-border bg-bg-secondary/30 p-2 text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
                aria-label="Close operator attention"
              >
                <X size={16} />
              </button>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <span
                className={clsx(
                  "rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]",
                  serviceStateTone(overallServiceState),
                )}
              >
                {overallServiceState}
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1 text-[10px] uppercase tracking-[0.16em] text-text-muted">
                {health?.summary.critical_services || 0} critical
              </span>
              <span className="rounded-full border border-bg-border bg-bg-primary/35 px-2.5 py-1 text-[10px] uppercase tracking-[0.16em] text-text-muted">
                {health?.summary.degraded_services || 0} degraded
              </span>
            </div>

            <div className="mt-5 grid gap-4 xl:grid-cols-[0.88fr,1.12fr]">
              <div className="space-y-3">
                <div className="text-sm font-semibold text-text-primary">Current blockers</div>
                {topBlockers.length ? (
                  topBlockers.map((service) => (
                    <div key={service.key} className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-3.5">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-text-primary">{service.label}</div>
                          <div className="mt-1 text-xs leading-5 text-text-muted">{service.detail}</div>
                        </div>
                        <span
                          className={clsx(
                            "rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]",
                            service.status === "critical"
                              ? "border-accent-red/30 bg-accent-red/10 text-accent-red"
                              : "border-accent-amber/30 bg-accent-amber/10 text-accent-amber",
                          )}
                        >
                          {service.status}
                        </span>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4 text-sm text-text-secondary">
                    No degraded or critical services are currently reported.
                  </div>
                )}
              </div>

              {health ? <SystemHealthBoard health={health} compact includeLanes={false} /> : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
