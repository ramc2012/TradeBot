"use client";
import { useEffect, useMemo } from "react";
import { getBrokerStatus, getPortfolioSummary } from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createLayoutSocket } from "@/lib/websocket";
import { type BrokerName, useStore } from "@/store";
import { clsx } from "clsx";

type LayoutSnapshot = {
  broker_status: Array<BrokerStatusEntry & {
    broker: BrokerName;
  }>;
  portfolio_summary: {
    total_equity: number;
    available_capital: number;
    unrealized_pnl: number;
    realized_pnl: number;
    day_pnl: number;
    win_rate: number;
    sharpe_ratio: number;
    max_drawdown: number;
  };
};

function asFiniteNumber(value: unknown, fallback = 0): number {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function normalizeBrokerStatus(entry: unknown): LayoutSnapshot["broker_status"][number] | null {
  if (!entry || typeof entry !== "object") return null;
  const status = entry as Partial<BrokerStatusEntry>;
  if (typeof status.broker !== "string" || !status.broker.trim()) return null;

  return {
    broker: status.broker as BrokerName,
    connected: Boolean(status.connected),
    ready: typeof status.ready === "boolean" ? status.ready : undefined,
    session_active: typeof status.session_active === "boolean" ? status.session_active : undefined,
    state: asStringOrNull(status.state),
    detail: asStringOrNull(status.detail),
    source: asStringOrNull(status.source),
    checked_at: asStringOrNull(status.checked_at),
    needs_reconnect: Boolean(status.needs_reconnect),
    user_id: asStringOrNull(status.user_id),
    name: asStringOrNull(status.name),
    connected_at: asStringOrNull(status.connected_at),
  };
}

function normalizeBrokerStatuses(value: unknown): LayoutSnapshot["broker_status"] {
  if (!Array.isArray(value)) return [];
  return value
    .map(normalizeBrokerStatus)
    .filter((entry): entry is LayoutSnapshot["broker_status"][number] => entry !== null);
}

function normalizePortfolioSummary(value: unknown): LayoutSnapshot["portfolio_summary"] | null {
  if (!value || typeof value !== "object") return null;
  const summary = value as Partial<LayoutSnapshot["portfolio_summary"]>;
  return {
    total_equity: asFiniteNumber(summary.total_equity),
    available_capital: asFiniteNumber(summary.available_capital),
    unrealized_pnl: asFiniteNumber(summary.unrealized_pnl),
    realized_pnl: asFiniteNumber(summary.realized_pnl),
    day_pnl: asFiniteNumber(summary.day_pnl),
    win_rate: asFiniteNumber(summary.win_rate),
    sharpe_ratio: asFiniteNumber(summary.sharpe_ratio),
    max_drawdown: asFiniteNumber(summary.max_drawdown),
  };
}

function normalizeLayoutSnapshot(value: LayoutSnapshot | undefined): LayoutSnapshot | undefined {
  if (!value || typeof value !== "object") return undefined;
  return {
    broker_status: normalizeBrokerStatuses(value.broker_status),
    portfolio_summary: normalizePortfolioSummary(value.portfolio_summary) ?? {
      total_equity: 0,
      available_capital: 0,
      unrealized_pnl: 0,
      realized_pnl: 0,
      day_pnl: 0,
      win_rate: 0,
      sharpe_ratio: 0,
      max_drawdown: 0,
    },
  };
}

function brokerStatusesMatch(
  current: LayoutSnapshot["broker_status"],
  next: LayoutSnapshot["broker_status"],
): boolean {
  if (current.length !== next.length) return false;
  return next.every((status, index) => {
    const existing = current[index];
    return (
      existing?.broker === status.broker &&
      existing.connected === status.connected &&
      existing.ready === status.ready &&
      existing.session_active === status.session_active &&
      existing.state === status.state &&
      existing.detail === status.detail &&
      existing.source === status.source &&
      existing.checked_at === status.checked_at &&
      existing.needs_reconnect === status.needs_reconnect &&
      existing.user_id === status.user_id &&
      existing.name === status.name &&
      existing.connected_at === status.connected_at
    );
  });
}

export default function BrokerStatusBar() {
  const { mode, portfolio, brokerStatuses: storeBrokerStatuses, setPortfolio, setBrokerStatuses } = useStore();

  const layoutQuery = useLiveSnapshotQuery<LayoutSnapshot>({
    queryKey: ["layoutSnapshot"],
    queryFn: async () => {
      const [broker_status, portfolio_summary] = await Promise.all([
        getBrokerStatus().then((response) => response.data),
        getPortfolioSummary().then((response) => response.data),
      ]);
      return { broker_status, portfolio_summary };
    },
    streamFactory: (onData, onStatusChange) =>
      createLayoutSocket((data) => onData(data as LayoutSnapshot), onStatusChange),
    storageKey: "layout:snapshot:v4",
  });

  const layoutData = useMemo(() => normalizeLayoutSnapshot(layoutQuery.data), [layoutQuery.data]);
  const sanitizedStoreBrokerStatuses = useMemo(
    () => normalizeBrokerStatuses(storeBrokerStatuses),
    [storeBrokerStatuses],
  );
  const statusData = layoutData?.broker_status.length ? layoutData.broker_status : undefined;
  const portfolioData = layoutData?.portfolio_summary ?? undefined;
  const effectiveStatuses = statusData?.length ? statusData : sanitizedStoreBrokerStatuses.length ? sanitizedStoreBrokerStatuses : undefined;
  const effectivePortfolio = portfolioData ?? portfolio ?? null;
  const totalEquity = Number(effectivePortfolio?.total_equity ?? 0);
  const dayPnl = Number(effectivePortfolio?.day_pnl ?? 0);
  const hasPortfolio = effectivePortfolio != null && Number.isFinite(totalEquity);

  useEffect(() => {
    if (!statusData?.length || brokerStatusesMatch(sanitizedStoreBrokerStatuses, statusData)) return;
    setBrokerStatuses(statusData);
  }, [statusData, sanitizedStoreBrokerStatuses, setBrokerStatuses]);

  useEffect(() => {
    if (!portfolioData || !Number.isFinite(Number(portfolioData.total_equity ?? 0))) return;
    if (
      portfolio &&
      portfolio.total_equity === portfolioData.total_equity &&
      portfolio.available_capital === portfolioData.available_capital &&
      portfolio.unrealized_pnl === portfolioData.unrealized_pnl &&
      portfolio.realized_pnl === portfolioData.realized_pnl &&
      portfolio.day_pnl === portfolioData.day_pnl &&
      portfolio.win_rate === portfolioData.win_rate &&
      portfolio.sharpe_ratio === portfolioData.sharpe_ratio &&
      portfolio.max_drawdown === portfolioData.max_drawdown
    ) {
      return;
    }
    setPortfolio(portfolioData);
  }, [portfolioData, portfolio, setPortfolio]);

  const connectedBroker = effectiveStatuses?.find((status) => isBrokerReady(status));
  const connectedBrokerLabel = connectedBroker?.broker ? String(connectedBroker.broker).toUpperCase() : "BROKER";
  const connectedBrokerName = connectedBroker?.name || connectedBroker?.user_id || "Connected";
  const showingSnapshot = layoutQuery.isShowingSnapshot;
  const statusMessage = showingSnapshot ? "last layout state" : null;
  const isLoadingFreshState = !effectiveStatuses?.length && (layoutQuery.isLoading || layoutQuery.isFetching);
  const hasAnyBrokerState = Boolean(effectiveStatuses?.length);

  return (
    <div className="h-8 bg-bg-secondary border-b border-bg-border flex items-center px-4 gap-6 text-xs font-mono shrink-0">
      {/* Mode badge */}
      <span
        className={clsx(
          "px-2 py-0.5 rounded font-bold uppercase tracking-wide",
          mode === "paper"
            ? "bg-accent-green/20 text-accent-green"
            : "bg-accent-amber/20 text-accent-amber animate-pulse"
        )}
      >
        {mode}
      </span>

      {/* Broker */}
      <span className="text-text-muted">
        {connectedBroker ? (
          <span className="text-text-primary">
            <span className="text-accent-green">●</span> {connectedBrokerLabel} — {connectedBrokerName}
          </span>
        ) : isLoadingFreshState ? (
          <span className="text-accent-amber">◌ Connecting broker state…</span>
        ) : hasAnyBrokerState ? (
          <span className="text-accent-amber">◌ Broker session not ready</span>
        ) : (
          <span className="text-text-muted">◌ Awaiting broker state…</span>
        )}
      </span>

      {showingSnapshot && (
        <span className="text-accent-amber">stale · {statusMessage}</span>
      )}

      <span className="flex-1" />

      {/* Day P&L */}
      {hasPortfolio && (
        <>
          <span className="text-text-muted">Equity:</span>
          <span className="text-text-primary">₹{totalEquity.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
          <span className="text-text-muted">Day P&L:</span>
          <span className={dayPnl >= 0 ? "text-accent-green" : "text-accent-red"}>
            {dayPnl >= 0 ? "+" : ""}₹{dayPnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
          </span>
        </>
      )}
    </div>
  );
}
