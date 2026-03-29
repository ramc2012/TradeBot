"use client";
import { useEffect } from "react";
import { getBrokerStatus, getPortfolioSummary } from "@/lib/api";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";
import { useStore } from "@/store";
import { clsx } from "clsx";

export default function BrokerStatusBar() {
  const { mode, activeBroker, portfolio, setPortfolio, setBrokerStatuses } = useStore();

  const statusQuery = usePersistentSnapshotQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    refetchInterval: 30000,
    storageKey: "layout:broker-status",
  });

  const portfolioQuery = usePersistentSnapshotQuery({
    queryKey: ["portfolioSummary"],
    queryFn: () => getPortfolioSummary().then((r) => r.data),
    refetchInterval: 5000,
    storageKey: "layout:portfolio-summary",
  });

  const statusData = statusQuery.data;
  const portfolioData = portfolioQuery.data;

  useEffect(() => {
    if (statusData) setBrokerStatuses(statusData);
  }, [statusData, setBrokerStatuses]);

  useEffect(() => {
    if (portfolioData) setPortfolio(portfolioData);
  }, [portfolioData, setPortfolio]);

  const connectedBroker = statusData?.find((s: { broker: string; connected: boolean }) => s.connected);
  const dayPnl = portfolio?.day_pnl ?? 0;
  const showingSnapshot = statusQuery.isShowingSnapshot || portfolioQuery.isShowingSnapshot;
  const statusMessage = statusQuery.isShowingSnapshot
    ? "last broker state"
    : portfolioQuery.isShowingSnapshot
      ? "last portfolio state"
      : null;

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
            <span className="text-accent-green">●</span> {connectedBroker.broker.toUpperCase()} — {connectedBroker.name}
          </span>
        ) : (
          <span className="text-text-muted">⚪ No broker connected</span>
        )}
      </span>

      {showingSnapshot && (
        <span className="text-accent-amber">stale · {statusMessage}</span>
      )}

      <span className="flex-1" />

      {/* Day P&L */}
      {portfolio && (
        <>
          <span className="text-text-muted">Equity:</span>
          <span className="text-text-primary">₹{portfolio.total_equity.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
          <span className="text-text-muted">Day P&L:</span>
          <span className={dayPnl >= 0 ? "text-accent-green" : "text-accent-red"}>
            {dayPnl >= 0 ? "+" : ""}₹{dayPnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
          </span>
        </>
      )}
    </div>
  );
}
