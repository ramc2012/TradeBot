"use client";

/**
 * Consolidated agent-proposals approval board. The strategy desks post
 * paper/live proposals; this is the single place an operator reviews and
 * approves/rejects them. Backed by /api/agent/proposals (+ approve/reject).
 */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Inbox, RefreshCw, X } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, StatusBadge, formatIST, formatNumber, tone } from "@/components/desk-ui";
import { api } from "@/lib/api";

type Proposal = {
  id: string;
  symbol: string;
  strategy: string;
  entry: number;
  sl: number;
  target: number;
  qty: number;
  rationale: string;
  confidence: "HIGH" | "MED" | "LOW" | string;
  status: string;
  created_at: string;
};

const confVariant = (c: string) => (c === "HIGH" ? "success" : c === "MED" ? "warn" : "neutral");

function riskReward(p: Proposal): number | null {
  const risk = Math.abs(p.entry - p.sl);
  const reward = Math.abs(p.target - p.entry);
  return risk > 0 ? reward / risk : null;
}

export default function ProposalsBoard() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);

  const proposalsQ = useQuery({
    queryKey: ["agent", "proposals"],
    queryFn: async () => (await api.get("/api/agent/proposals")).data as Proposal[],
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const list = useMemo(
    () => (Array.isArray(proposalsQ.data) ? proposalsQ.data : []).filter((p) => (p.status || "pending") === "pending"),
    [proposalsQ.data],
  );

  const act = async (id: string, action: "approve" | "reject") => {
    setBusy(`${id}:${action}`);
    try {
      await api.post(`/api/agent/proposals/${encodeURIComponent(id)}/${action}`, {});
      await qc.invalidateQueries({ queryKey: ["agent", "proposals"] });
    } catch {
      /* surfaced via list staleness */
    } finally {
      setBusy(null);
    }
  };

  const byStrategy = useMemo(() => {
    const m = new Map<string, number>();
    list.forEach((p) => m.set(p.strategy, (m.get(p.strategy) ?? 0) + 1));
    return Array.from(m.entries()).sort((a, b) => b[1] - a[1]);
  }, [list]);
  const highConf = list.filter((p) => p.confidence === "HIGH").length;

  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-text-primary">Proposals</h1>
            <p className="mt-1 text-sm text-text-muted">
              Review and approve/reject pending strategy proposals across every desk.
            </p>
          </div>
          <button
            type="button"
            onClick={() => proposalsQ.refetch()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
          >
            <RefreshCw size={13} className={proposalsQ.isFetching ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="Pending" value={String(list.length)} detail="awaiting decision" />
        <MetricTile label="High conviction" value={String(highConf)} detail="HIGH confidence" color={highConf ? "text-accent-green" : undefined} />
        <MetricTile label="Desks" value={String(byStrategy.length)} detail={byStrategy.map(([s, n]) => `${s} ${n}`).slice(0, 2).join(" · ")} />
        <MetricTile label="Feed" value={proposalsQ.isError ? "offline" : "live"} detail={proposalsQ.dataUpdatedAt ? formatIST(proposalsQ.dataUpdatedAt) : ""} color={proposalsQ.isError ? "text-accent-red" : "text-accent-green"} />
      </section>

      <Section title="Pending proposals" icon={<Inbox size={16} />}>
        {list.length === 0 ? (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-12 text-center text-sm text-text-muted">
            No pending proposals. Desks auto-execute approved setups during market hours; anything needing a manual
            decision lands here.
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {list.map((p) => {
              const rr = riskReward(p);
              const long = p.target >= p.entry;
              return (
                <div key={p.id} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-text-primary">{p.symbol}</span>
                        <StatusBadge label={long ? "LONG" : "SHORT"} variant={long ? "success" : "error"} />
                        <StatusBadge label={p.confidence} variant={confVariant(p.confidence)} />
                      </div>
                      <div className="mt-0.5 text-[11px] text-text-muted">
                        {p.strategy} · {formatIST(p.created_at)}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-1.5">
                      <button
                        type="button"
                        disabled={busy != null}
                        onClick={() => act(p.id, "approve")}
                        className="inline-flex items-center gap-1 rounded-lg border border-accent-green/40 bg-accent-green/10 px-2 py-1 text-[11.5px] font-semibold text-accent-green hover:bg-accent-green/20 disabled:opacity-50"
                      >
                        <Check size={13} />
                        {busy === `${p.id}:approve` ? "…" : "Approve"}
                      </button>
                      <button
                        type="button"
                        disabled={busy != null}
                        onClick={() => act(p.id, "reject")}
                        className="inline-flex items-center gap-1 rounded-lg border border-accent-red/40 bg-accent-red/10 px-2 py-1 text-[11.5px] font-semibold text-accent-red hover:bg-accent-red/20 disabled:opacity-50"
                      >
                        <X size={13} />
                        Reject
                      </button>
                    </div>
                  </div>

                  <div className="mt-2.5 grid grid-cols-4 gap-2 text-center">
                    <Cell label="Entry" value={formatNumber(p.entry, 1)} />
                    <Cell label="Stop" value={formatNumber(p.sl, 1)} valueClass="text-accent-red" />
                    <Cell label="Target" value={formatNumber(p.target, 1)} valueClass="text-accent-green" />
                    <Cell label="R:R" value={rr != null ? `${rr.toFixed(2)}` : "—"} valueClass={tone(rr != null ? rr - 1 : 0)} />
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px] text-text-muted">
                    <span>Qty {p.qty}</span>
                  </div>
                  {p.rationale ? <div className="mt-2 text-[12px] text-text-secondary">{p.rationale}</div> : null}
                </div>
              );
            })}
          </div>
        )}
      </Section>
    </div>
  );
}

function Cell({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-1.5 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={`font-mono text-[13px] ${valueClass ?? "text-text-primary"}`}>{value}</div>
    </div>
  );
}
