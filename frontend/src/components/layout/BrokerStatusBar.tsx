"use client";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getBrokerStatus, getPortfolioSummary } from "@/lib/api";
import { useStore } from "@/store";
import { clsx } from "clsx";

export default function BrokerStatusBar() {
  const { mode, activeBroker, portfolio, setPortfolio, setBrokerStatuses } = useStore();

  const { data: statusData } = useQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    refetchInterval: 30000,
  });

  const { data: portfolioData } = useQuery({
    queryKey: ["portfolioSummary"],
    queryFn: () => getPortfolioSummary().then((r) => r.data),
    refetchInterval: 5000,
  });

  useEffect(() => {
    if (statusData) setBrokerStatuses(statusData);
  }, [statusData, setBrokerStatuses]);

  useEffect(() => {
    if (portfolioData) setPortfolio(portfolioData);
  }, [portfolioData, setPortfolio]);

  const connectedBroker = statusData?.find((s: { broker: string; connected: boolean }) => s.connected);
  const dayPnl = portfolio?.day_pnl ?? 0;

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
