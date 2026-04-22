"use client";
import { useEffect } from "react";
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
    storageKey: "layout:snapshot",
  });

  const statusData = Array.isArray(layoutQuery.data?.broker_status) ? layoutQuery.data.broker_status : undefined;
  const portfolioData = layoutQuery.data?.portfolio_summary ?? undefined;
  const effectiveStatuses = statusData?.length ? statusData : storeBrokerStatuses.length ? storeBrokerStatuses : undefined;
  const effectivePortfolio = portfolioData ?? portfolio ?? null;
  const totalEquity = Number(effectivePortfolio?.total_equity ?? 0);
  const dayPnl = Number(effectivePortfolio?.day_pnl ?? 0);
  const hasPortfolio = effectivePortfolio != null && Number.isFinite(totalEquity);

  useEffect(() => {
    if (statusData?.length) setBrokerStatuses(statusData);
  }, [statusData, setBrokerStatuses]);

  useEffect(() => {
    if (portfolioData && Number.isFinite(Number(portfolioData.total_equity ?? 0))) {
      setPortfolio(portfolioData);
    }
  }, [portfolioData, setPortfolio]);

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
