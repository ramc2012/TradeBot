"use client";

/**
 * Agent-decision board for the Auction desk. Each sleeve (swing / positional)
 * emits an action (LONG/SHORT/FLAT), a confidence, an entry/stop/target, the
 * fraction of capital it would deploy, and a rationale trail. FLAT decisions
 * still carry rich rationale (why the entry filter failed) so we show them.
 */
import { useState } from "react";
import { Bot, ChevronDown, ChevronRight } from "lucide-react";

import { Section, StatusBadge, formatNumber, formatPct } from "@/components/desk-ui";

import type { AgentDecision } from "./types";

function actionVariant(a?: string): "success" | "error" | "neutral" | "warn" {
  const s = (a || "").toUpperCase();
  if (s === "LONG" || s === "BUY") return "success";
  if (s === "SHORT" || s === "SELL") return "error";
  if (s === "FLAT" || s === "WAIT") return "warn";
  return "neutral";
}

function DecisionRow({ d }: { d: AgentDecision }) {
  const [open, setOpen] = useState(false);
  const rationale = d.rationale || [];
  return (
    <>
      <tr className="border-b border-bg-border/25 hover:bg-bg-primary/20">
        <td className="px-2.5 py-2">
          <button type="button" onClick={() => setOpen((v) => !v)} className="flex items-center gap-1.5 text-text-primary">
            {rationale.length ? (open ? <ChevronDown size={13} /> : <ChevronRight size={13} />) : <span className="w-[13px]" />}
            <span className="font-semibold capitalize">{d.agent_name}</span>
          </button>
        </td>
        <td className="px-2.5 py-2"><StatusBadge label={d.action || "—"} variant={actionVariant(d.action)} /></td>
        <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatPct(d.confidence, 0)}</td>
        <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(d.entry_price, 1)}</td>
        <td className="px-2.5 py-2 text-right font-mono text-accent-red">{formatNumber(d.stop_price, 1)}</td>
        <td className="px-2.5 py-2 text-right font-mono text-accent-green">{formatNumber(d.target_price, 1)}</td>
        <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(d.quantity, 0)}</td>
        <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatPct(d.sleeve_fraction, 0)}</td>
      </tr>
      {open && rationale.length ? (
        <tr className="border-b border-bg-border/25 bg-bg-primary/10">
          <td colSpan={8} className="px-2.5 pb-2.5 pt-1">
            <ul className="space-y-1">
              {rationale.map((r, i) => (
                <li key={i} className="flex items-start gap-2 text-[12px] text-text-secondary">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />
                  {r}
                </li>
              ))}
              {d.metadata?.flat_reason ? (
                <li className="text-[11px] text-text-muted">flat_reason: {String(d.metadata.flat_reason)}</li>
              ) : null}
            </ul>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function AgentDecisions({ decisions }: { decisions?: AgentDecision[] }) {
  const rows = decisions || [];
  return (
    <Section title="Agent decisions" icon={<Bot size={16} />} description="Per-sleeve action, conviction, levels and rationale — expand a row for the reasoning trail">
      {rows.length ? (
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-bg-border/60">
                {["Agent", "Action", "Conf", "Entry", "Stop", "Target", "Qty", "Sleeve"].map((h, i) => (
                  <th key={i} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i <= 1 ? "text-left" : "text-right"}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((d, i) => <DecisionRow key={`${d.agent_name}-${i}`} d={d} />)}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="py-6 text-center text-sm text-text-muted">No agent decisions in this snapshot — the sleeves are waiting for a qualifying setup.</div>
      )}
    </Section>
  );
}
