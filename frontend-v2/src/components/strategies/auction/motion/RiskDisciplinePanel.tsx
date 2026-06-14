"use client";

/**
 * Risk / Discipline panel — the restored risk thesis, made visible.
 *
 * This panel is the point of the In-Motion tab: the operator should SEE the
 * discipline gating trades. It shows, per agent, the conviction vs. the 0.55
 * floor; the governor's size multiplier; the kill-switch state; and — when the
 * gate is closed — exactly WHY (the governor reasons), so a blocked book is
 * never a mystery.
 */
import { ShieldAlert, ShieldCheck, ShieldX, Skull } from "lucide-react";

import { Section, StatusBadge, formatNumber, formatPct } from "@/components/desk-ui";

import type { AgentDecision, Risk } from "../types";
import { CONFIDENCE_FLOOR } from "./DecisionTheater";

function FloorRow({ d }: { d: AgentDecision }) {
  const conf = Math.max(0, Math.min(1, Number(d.confidence ?? 0)));
  const above = conf >= CONFIDENCE_FLOOR && (d.action || "FLAT").toUpperCase() !== "FLAT";
  const isFlat = (d.action || "FLAT").toUpperCase() === "FLAT";
  const floorPct = CONFIDENCE_FLOOR * 100;
  const fill = above ? "rgb(var(--accent-green))" : "rgb(var(--accent-amber))";
  const status = isFlat ? "flat" : above ? "clears" : "below floor";
  const statusColor = isFlat ? "text-text-muted" : above ? "text-accent-green" : "text-accent-amber";

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[11px]">
        <span className="font-semibold capitalize text-text-primary">{d.agent_name || "—"}</span>
        <span className="flex items-center gap-2">
          <span className="font-mono text-text-secondary">{d.confidence == null ? "—" : formatPct(conf, 0)}</span>
          <span className={`uppercase tracking-[0.1em] text-[9.5px] ${statusColor}`}>{status}</span>
        </span>
      </div>
      <div className="relative h-1.5 overflow-hidden rounded-full bg-bg-primary/40">
        <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${conf * 100}%`, background: fill }} />
        <div className="absolute top-0 h-full w-px bg-white/55" style={{ left: `${floorPct}%` }} />
      </div>
    </div>
  );
}

export function RiskDisciplinePanel({ decisions, risk }: { decisions?: AgentDecision[]; risk?: Risk }) {
  const agents = decisions || [];
  const allowed = risk?.allowed === true;
  const killSwitch = risk?.kill_switch === true;
  const mult = Math.max(0, Math.min(1, Number(risk?.max_size_multiplier ?? 0)));
  const reasons = (risk?.reasons || []).filter(Boolean);
  const multColor = mult >= 0.66 ? "rgb(var(--accent-green))" : mult >= 0.33 ? "rgb(var(--accent-amber))" : "rgb(var(--accent-red))";

  const verdictBadge = killSwitch ? (
    <StatusBadge label="kill switch" variant="error" icon={<Skull size={11} />} />
  ) : allowed ? (
    <StatusBadge label="allowed" variant="success" icon={<ShieldCheck size={11} />} />
  ) : (
    <StatusBadge label="blocked" variant="warn" icon={<ShieldX size={11} />} />
  );

  return (
    <Section
      title="Risk · discipline"
      icon={<ShieldAlert size={16} />}
      description="The governor in action — conviction floor, size throttle, kill switch and the gates that fired"
      rightSlot={verdictBadge}
    >
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* Conviction-floor column */}
        <div className="space-y-3">
          <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Conviction vs. {Math.round(CONFIDENCE_FLOOR * 100)}% floor</div>
          {agents.length ? (
            agents.map((d, i) => <FloorRow key={`${d.agent_name}-${i}`} d={d} />)
          ) : (
            <div className="text-[12px] text-text-muted">No sleeves reporting — desk is flat.</div>
          )}
        </div>

        {/* Governor column */}
        <div className="space-y-3">
          <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Governor</div>

          {/* size multiplier */}
          <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3">
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-text-muted">Size multiplier</span>
              <span className="font-mono text-text-primary">{formatNumber(mult, 2)}×</span>
            </div>
            <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-bg-primary/40">
              <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${mult * 100}%`, background: multColor }} />
            </div>
            <div className="mt-1.5 text-[10px] text-text-muted">
              {mult <= 0 ? "throttled to zero — no size permitted" : mult < 1 ? "throttled below full size" : "full size permitted"}
            </div>
          </div>

          {/* kill switch + gate state */}
          <div className="grid grid-cols-2 gap-2">
            <div className={`rounded-xl border px-3 py-2.5 ${killSwitch ? "border-accent-red/40 bg-accent-red/10" : "border-bg-border bg-bg-primary/15"}`}>
              <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Kill switch</div>
              <div className={`mt-0.5 font-mono text-[13px] font-semibold ${killSwitch ? "text-accent-red" : "text-accent-green"}`}>
                {killSwitch ? "ENGAGED" : "clear"}
              </div>
            </div>
            <div className={`rounded-xl border px-3 py-2.5 ${allowed ? "border-accent-green/35 bg-accent-green/10" : "border-accent-amber/35 bg-accent-amber/10"}`}>
              <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Gate</div>
              <div className={`mt-0.5 font-mono text-[13px] font-semibold ${allowed ? "text-accent-green" : "text-accent-amber"}`}>
                {allowed ? "OPEN" : "CLOSED"}
              </div>
            </div>
          </div>

          {/* Why blocked — the discipline reasons */}
          {!allowed || reasons.length ? (
            <div className={`rounded-xl border p-3 ${allowed ? "border-bg-border bg-bg-primary/15" : "border-accent-amber/30 bg-accent-amber/[0.06]"}`}>
              <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">
                {allowed ? "Notes" : "Why blocked"}
              </div>
              {reasons.length ? (
                <ul className="mt-1.5 space-y-1">
                  {reasons.map((r, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-[11.5px] leading-snug text-text-secondary">
                      <span className={`mt-1 h-1 w-1 shrink-0 rounded-full ${allowed ? "bg-accent-blue/70" : "bg-accent-amber/80"}`} />
                      {r}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="mt-1.5 text-[11.5px] text-text-muted">Gate is closed but no reason was reported this snapshot.</div>
              )}
            </div>
          ) : (
            <div className="rounded-xl border border-accent-green/25 bg-accent-green/[0.05] p-3 text-[11.5px] text-accent-green">
              Discipline clear — no gate fired. Sleeves above the floor are free to size.
            </div>
          )}
        </div>
      </div>
    </Section>
  );
}
