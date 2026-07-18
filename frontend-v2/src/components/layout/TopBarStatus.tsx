"use client";

/**
 * TopBar truth strip.
 *
 * The trader glances here and reads FIVE separate facts, never one collapsed
 * "LIVE" light:
 *
 *   API up      transport — the backend answered (NOT "market is live")
 *   NSE / MCX   market session open/closed, from the IST clock
 *   feed        data freshness — WS connected + fresh ticks
 *   brokers X/4 execution readiness — real trading sessions only
 *   paper/live  mode
 *   auto-run    strategy loop armed/paused
 *   kill armed  (only when the kill switch is armed)
 *
 * All of this is derived in useSystemState from health + broker-status + mode +
 * kill-switch. Transport ≠ session ≠ freshness ≠ strategy ≠ execution: each is
 * its own chip so a green transport light can never masquerade as "we're live".
 */
import { Power, ShieldAlert, Wifi, WifiOff } from "lucide-react";

import { StatusBadge } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";

export default function TopBarStatus() {
  const sys = useSystemState();

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {sys.flags.map((flag) => {
        const isApi = flag.label.startsWith("API");
        const isAutoRun = flag.label.startsWith("auto-run");
        const isKill = flag.label === "kill armed";
        const icon = isApi ? (
          sys.apiUp ? (
            <Wifi size={11} />
          ) : (
            <WifiOff size={11} />
          )
        ) : isAutoRun ? (
          <Power size={10} />
        ) : isKill ? (
          <ShieldAlert size={11} />
        ) : undefined;
        return (
          <span key={flag.label} title={flag.title}>
            <StatusBadge label={flag.label} variant={flag.variant} icon={icon} />
          </span>
        );
      })}
    </div>
  );
}
