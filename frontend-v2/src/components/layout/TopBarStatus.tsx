"use client";

/**
 * Live system-status cluster for the TopBar. The trader glances here and
 * knows, without navigating: connection health, broker readiness, whether
 * the auto-run loop is live, paper-vs-live mode, and — loudly — whether the
 * kill switch is armed.
 */
import { useQuery } from "@tanstack/react-query";
import { Power, ShieldAlert, Wifi, WifiOff } from "lucide-react";

import { REFRESH_MS, StatusBadge } from "@/components/desk-ui";
import { api } from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";

export default function TopBarStatus() {
  const brokers = useQuery({
    queryKey: ["topbar", "brokers"],
    queryFn: async () => (await api.get("/api/auth/broker-status")).data as BrokerStatusEntry[],
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const ks = useQuery({
    queryKey: ["topbar", "kill-switch"],
    queryFn: async () =>
      (await api.get("/api/trading/kill-switch")).data as {
        kill_switch_active?: boolean;
        auto_run_enabled?: boolean;
        loop_active?: boolean;
      },
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const mode = useQuery({
    queryKey: ["topbar", "mode"],
    queryFn: async () => (await api.get("/api/trading/mode")).data as { mode?: string; paper_trading?: boolean; live?: boolean },
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const online = !brokers.isError && !ks.isError && (brokers.isFetched || ks.isFetched);
  const list = Array.isArray(brokers.data) ? brokers.data : [];
  const readyCount = list.filter((b) => isBrokerReady(b)).length;
  const anySession = list.some((b) => b.session_active || b.connected);
  const brokerTone =
    readyCount > 0
      ? "success"
      : anySession
        ? "warn"
        : list.length
          ? "error"
          : "neutral";
  const killActive = !!ks.data?.kill_switch_active;
  const autoRun = !!ks.data?.auto_run_enabled && ks.data?.loop_active !== false;
  const isLive = mode.data?.mode ? mode.data.mode === "live" : mode.data?.live ?? mode.data?.paper_trading === false;

  const brokerTitle = list.map((b) => `${b.broker}: ${b.state || (b.ready ? "ready" : "—")}`).join("\n");

  return (
    <div className="flex items-center gap-1.5">
      <StatusBadge
        label={online ? "live" : "offline"}
        variant={online ? "success" : "error"}
        icon={online ? <Wifi size={11} /> : <WifiOff size={11} />}
      />
      <span title={brokerTitle}>
        <StatusBadge
          label={`${readyCount}/${list.length || 0} broker`}
          variant={brokerTone}
        />
      </span>
      <StatusBadge
        label={autoRun ? "auto-run" : "paused"}
        variant={autoRun ? "success" : "warn"}
        icon={<Power size={10} />}
      />
      {mode.isFetched ? (
        <StatusBadge label={isLive ? "live trading" : "paper"} variant={isLive ? "warn" : "info"} />
      ) : null}
      {killActive ? (
        <StatusBadge label="kill armed" variant="error" icon={<ShieldAlert size={11} />} />
      ) : null}
    </div>
  );
}
