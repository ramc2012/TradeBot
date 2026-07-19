"use client";

/**
 * "In Motion" tab — watch the auction lane work in real time.
 *
 * Three stacked sections, all driven by the same 8s live snapshot so they
 * update on every WS push:
 *   1. Decision theater  — agents propose → governor disposes
 *   2. Risk / discipline — the conviction floor + size throttle gating trades
 *   3. Microstructure    — signed flow + book imbalance + the regime strip
 *
 * Additive: this renders alongside (never replaces) the existing tabs.
 */
import { Radio } from "lucide-react";

import { StatusBadge, formatNumber } from "@/components/desk-ui";
import type { OrderFlow } from "@/components/strategies/shared";

import type { Snapshot } from "../types";
import { DecisionTheater } from "./DecisionTheater";
import { RiskDisciplinePanel } from "./RiskDisciplinePanel";
import { MicrostructurePanel } from "./MicrostructurePanel";

export function MotionTab({ snap }: { snap?: Snapshot }) {
  const analysis = snap?.analysis;
  const decisions = analysis?.agent_decisions;
  const risk = analysis?.risk;
  const regime = analysis?.regime;
  const of = analysis?.order_flow as OrderFlow | undefined;
  const mp = analysis?.market_profile;
  const spot = snap?.request?.session?.last_price ?? mp?.close_price ?? null;

  const hasLive = Boolean(analysis);

  return (
    <div className="space-y-4">
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Radio size={16} className={hasLive ? "animate-pulse text-accent-blue" : "text-text-muted"} />
          In Motion
          <span className="ml-1 text-[11.5px] font-normal text-text-muted">
            the auction lane working live · refreshes every push
          </span>
        </div>
        <div className="flex items-center gap-2">
          {spot != null ? (
            <span className="font-mono text-[12px] text-text-secondary">
              spot <span className="text-text-primary">{formatNumber(spot, 1)}</span>
            </span>
          ) : null}
          <StatusBadge label={hasLive ? "snapshot present" : "no snapshot"} variant={hasLive ? "info" : "neutral"} />
        </div>
      </section>

      <DecisionTheater decisions={decisions} risk={risk} />

      <RiskDisciplinePanel decisions={decisions} risk={risk} />

      <MicrostructurePanel of={of} regime={regime} />
    </div>
  );
}
