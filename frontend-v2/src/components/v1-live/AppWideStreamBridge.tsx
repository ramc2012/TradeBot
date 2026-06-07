"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  createCommodityOverviewSocket,
  createCommodityWatchlistSocket,
  createPositionsOverviewSocket,
  createStrategyDashboardSocket,
  createStrategyOverviewSocket,
  createSystemHealthSocket,
  createSystemOverviewSocket,
} from "@/lib/websocket";

type SocketHandle = { close: () => void };

function normalizePortfolioSnapshot(payload: Record<string, unknown>) {
  return {
    manual: payload.manual ?? null,
    nse: payload.nse ?? payload.strategy ?? null,
    commodity: payload.commodity ?? null,
    directional: payload.directional ?? null,
    gann: payload.gann ?? null,
    auction: payload.auction ?? null,
    fractal: payload.fractal ?? null,
    cbe: payload.cbe ?? null,
    errors: payload.errors ?? {},
    fetchedAt: payload.fetchedAt ?? new Date().toISOString(),
  };
}

export default function AppWideStreamBridge() {
  const queryClient = useQueryClient();

  useEffect(() => {
    const sockets: SocketHandle[] = [
      createSystemOverviewSocket((payload) => {
        queryClient.setQueryData(["systemOverview"], payload);
      }),
      createSystemHealthSocket((payload) => {
        queryClient.setQueryData(["systemHealth"], payload);
      }),
      createStrategyOverviewSocket((payload) => {
        const data = payload as Record<string, unknown>;
        queryClient.setQueryData(["strategyOverview"], payload);
        queryClient.setQueryData(["strategyAgentStatus"], data.agent_status);
        queryClient.setQueryData(["nse-live", "status"], data.agent_status);
      }),
      createStrategyDashboardSocket((payload) => {
        const data = payload as Record<string, unknown>;
        queryClient.setQueryData(["strategyDashboardSnapshot"], payload);
        queryClient.setQueryData(["strategyAgentStatus"], data.agent_status);
        queryClient.setQueryData(["nse-live", "status"], data.agent_status);
        queryClient.setQueryData(["nse-live", "orders"], data.orders);
        queryClient.setQueryData(["strategyEquityHistory"], data.equity_curves);
      }),
      createPositionsOverviewSocket((payload) => {
        const data = normalizePortfolioSnapshot(payload as Record<string, unknown>);
        queryClient.setQueryData(["globalPositionsSnapshot"], { portfolio: data });
        queryClient.setQueryData(["appStrategyPortfolioSnapshot"], data);
        queryClient.setQueryData(["nse-live", "positions"], data.manual);
      }),
      createCommodityOverviewSocket((payload) => {
        const data = payload as Record<string, unknown>;
        queryClient.setQueryData(["commodity-live", "overview"], payload);
        queryClient.setQueryData(["commodity-live", "positions"], data.positions);
        queryClient.setQueryData(["commodity-live", "orders"], data.orders);
        queryClient.setQueryData(["commodity-live", "reports"], data.reports);
      }),
      createCommodityWatchlistSocket((payload) => {
        queryClient.setQueryData(["commodity-live", "watchlist-snapshot"], payload);
      }),
    ];

    return () => {
      sockets.forEach((socket) => socket.close());
    };
  }, [queryClient]);

  return null;
}
