"use client";

/**
 * RAG case-memory panel. The auction lane gates each candidate trade against a
 * retrieval of historically similar cases — win-rate, expectancy, and the
 * matched cases themselves drive an ALLOW / BLOCK decision shown here.
 */
import { Brain } from "lucide-react";

import { MetricTile, Section, StatusBadge, formatMoney, formatPct, tone } from "@/components/desk-ui";

import type { RagContext } from "./types";

export function RagMemory({ rag }: { rag?: RagContext }) {
  if (!rag) {
    return (
      <Section title="Case memory (RAG)" icon={<Brain size={16} />}>
        <div className="py-6 text-center text-sm text-text-muted">No retrieval context attached to this snapshot.</div>
      </Section>
    );
  }
  const cs = rag.case_stats || {};
  const blocked = (rag.decision || "").toLowerCase() === "block";
  const retrievals = rag.retrievals || [];

  return (
    <div className="space-y-4">
      <Section
        title="Case memory (RAG)"
        icon={<Brain size={16} />}
        description="Retrieval gate over historically similar auction trades"
        rightSlot={<StatusBadge label={rag.decision || "—"} variant={blocked ? "error" : "success"} />}
      >
        <div className="space-y-3">
          {rag.summary ? <p className="text-[12.5px] text-text-secondary">{rag.summary}</p> : null}
          <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <MetricTile label="Matched" value={String(cs.matched_cases ?? 0)} detail={`${cs.resolved_cases ?? 0} resolved`} />
            <MetricTile label="Win rate" value={formatPct(cs.win_rate, 0)} color={(cs.win_rate ?? 0) >= 0.5 ? "text-accent-green" : "text-accent-red"} />
            <MetricTile label="Wins / Losses" value={`${cs.wins ?? 0} / ${cs.losses ?? 0}`} />
            <MetricTile label="Expectancy" value={formatMoney(cs.expectancy, 0)} color={tone(cs.expectancy)} />
            <MetricTile label="Best P&L" value={formatMoney(cs.best_pnl, 0)} color={tone(cs.best_pnl)} />
            <MetricTile label="Worst P&L" value={formatMoney(cs.worst_pnl, 0)} color={tone(cs.worst_pnl)} />
          </section>
          {rag.reason_codes?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {rag.reason_codes.map((c, i) => <StatusBadge key={i} label={c.replace(/_/g, " ")} variant={blocked ? "error" : "info"} />)}
            </div>
          ) : null}
        </div>
      </Section>

      <Section title="Matched cases" icon={<Brain size={16} />}>
        {retrievals.length ? (
          <div className="-mx-2 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-bg-border/60">
                  {["Symbol", "Dir", "Result", "P&L", "Score"].map((h, i) => (
                    <th key={i} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i === 0 ? "text-left" : "text-right"}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {retrievals.slice(0, 12).map((r, i) => {
                  const m = r.metadata || {};
                  const result = String(m.result ?? "");
                  return (
                    <tr key={r.id ?? i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                      <td className="px-2.5 py-1.5 text-left text-[12px] text-text-primary font-mono">{m.symbol ?? m.underlying ?? "—"}</td>
                      <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{m.direction ?? "—"}</td>
                      <td className="px-2.5 py-1.5 text-right">
                        <StatusBadge label={result || "—"} variant={result === "win" ? "success" : result === "loss" ? "error" : "neutral"} />
                      </td>
                      <td className={`px-2.5 py-1.5 text-right text-[12px] font-mono ${tone(Number(m.pnl))}`}>{formatMoney(Number(m.pnl), 0)}</td>
                      <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-muted">{formatPct(r.score, 0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="py-4 text-center text-sm text-text-muted">No matched cases.</div>
        )}
      </Section>
    </div>
  );
}
