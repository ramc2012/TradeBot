"use client";

import { useQuery } from "@tanstack/react-query";
import { getBrokerStatus, getStrategyAgentStatus } from "@/lib/api";
import { isBrokerReady } from "@/lib/broker-status";
import {
  AgentCommentaryFeed,
  StrategyAgentStatus,
  StrategyMonitorSection,
  StrategyStatusPanel,
} from "@/components/v1-trading/StrategyAgentMonitor";
import { Activity, Bot, Radio, Shield } from "lucide-react";

export default function AgentPage() {
  const { data: strategyStatus } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((r) => r.data as StrategyAgentStatus),
    refetchInterval: 15000,
    staleTime: 10000,
  });
  const { data: brokers } = useQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then((r) => r.data as any[]),
    refetchInterval: 30000,
    staleTime: 10000,
  });

  const strategyCount = strategyStatus?.strategies?.length || 0;
  const openPositions = (strategyStatus?.strategies || []).reduce(
    (sum, strategy) => sum + (strategy.summary.open_positions || 0),
    0,
  );
  const connectedBrokers = (brokers || []).filter((broker: any) => isBrokerReady(broker));

  return (
    <div className="max-w-screen-xl space-y-4">
      <div>
        <h1 className="text-lg font-bold font-mono text-text-primary flex items-center gap-2">
          <Bot size={18} className="text-accent-purple" />
          Strategy Agent
        </h1>
        <div className="mt-1 text-xs text-text-muted">
          Live monitor for both paper runtimes: Strategy 1 on 30-minute ATM options and Strategy 2 on 15-minute index options with Market Profile gating.
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Shield size={14} /> Live Runtime
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">{strategyCount}</div>
          <div className="mt-1 text-xs text-text-muted">Both strategy runtimes report here, with positions, signal lanes, and recent activity.</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Activity size={14} /> Open Positions
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">{openPositions}</div>
          <div className="mt-1 text-xs text-text-muted">Positions stay under strategy management after every automatic scan.</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Radio size={14} /> Broker Links
          </div>
          <div className="mt-3 text-2xl font-mono font-semibold text-text-primary">
            {connectedBrokers.length}
          </div>
          <div className="mt-1 text-xs text-text-muted">
            {connectedBrokers.length
              ? connectedBrokers.map((broker: any) => broker.broker).join(" + ").toUpperCase()
              : "No connected brokers"}
          </div>
        </div>
      </div>

      <StrategyMonitorSection agentStatus={strategyStatus} />

      <div className="grid grid-cols-1 xl:grid-cols-[0.85fr,1.15fr] gap-4">
        <StrategyStatusPanel agentStatus={strategyStatus} />
        <AgentCommentaryFeed agentStatus={strategyStatus} />
      </div>
    </div>
  );
}
