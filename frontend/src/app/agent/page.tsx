"use client";

import { useQuery } from "@tanstack/react-query";
import { getStrategyAgentStatus } from "@/lib/api";
import {
  AgentCommentaryFeed,
  StrategyAgentStatus,
  StrategyMonitorSection,
  StrategyStatusPanel,
} from "@/components/trading/StrategyAgentMonitor";
import { Activity, Bot, BrainCircuit, Send } from "lucide-react";

export default function AgentPage() {
  const { data: strategyStatus } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((r) => r.data as StrategyAgentStatus),
    refetchInterval: 15000,
    staleTime: 10000,
  });

  const strategyCount = strategyStatus?.strategies?.length || 0;
  const openPositions = (strategyStatus?.strategies || []).reduce(
    (sum, strategy) => sum + (strategy.summary.open_positions || 0),
    0,
  );
  const totalTrades = (strategyStatus?.strategies || []).reduce(
    (sum, strategy) => sum + (strategy.summary.total_trades || 0),
    0,
  );

  return (
    <div className="max-w-screen-xl space-y-4">
      <div>
        <h1 className="text-lg font-bold font-mono text-text-primary flex items-center gap-2">
          <Bot size={18} className="text-accent-purple" />
          Autonomous Agent
        </h1>
        <div className="mt-1 text-xs text-text-muted">
          This agent runs automatically during market hours. It scans the market intelligence watchlist, chooses trades from the active strategies, and sends trade logic to Telegram when configured.
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <BrainCircuit size={14} /> Strategy Books
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">{strategyCount}</div>
          <div className="mt-1 text-xs text-text-muted">MACD zero cross and Greeks Sync are evaluated independently.</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Activity size={14} /> Open Positions
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">{openPositions}</div>
          <div className="mt-1 text-xs text-text-muted">Positions are managed automatically with RSI exits and trailing stops.</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Send size={14} /> Telegram Trade Notes
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">
            {strategyStatus?.telegram?.configured ? "Ready" : "Not Set"}
          </div>
          <div className="mt-1 text-xs text-text-muted">
            {strategyStatus?.telegram?.configured
              ? "Entries, exits, and periodic summaries can be pushed with strategy reasoning."
              : "Configure bot token and chat ID in Settings to receive trade commentary."}
          </div>
        </div>
      </div>

      <StrategyStatusPanel agentStatus={strategyStatus} />
      <AgentCommentaryFeed agentStatus={strategyStatus} />
      <StrategyMonitorSection agentStatus={strategyStatus} />

      <div className="card p-4 text-xs text-text-muted">
        Closed trades across both strategies: <span className="font-mono text-text-primary">{totalTrades}</span>. The Trading page still holds manual order entry and broker positions, while this page is now limited to autonomous agent supervision only.
      </div>
    </div>
  );
}
