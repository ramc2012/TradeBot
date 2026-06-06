"use client";

/**
 * Validation-gates surface for the Auction desk.
 *
 * Gate A (data + feature engine) is a POST that re-validates the current
 * live-snapshot request body. Gates B/C are GET (rule-engine / walk-forward
 * and shadow-mode divergence). Canary readiness rolls B+C per symbol into a
 * Gate-D go/no-go with the live trading requirements.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ShieldCheck, XCircle, Rocket } from "lucide-react";

import { MetricTile, Section, StatusBadge, formatNumber, formatPct } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

import type { CanaryReadiness, GateCheck, GateResult, Snapshot } from "./types";

function fmtVal(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : formatNumber(v, 4);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, val]) => `${k}=${val == null ? "—" : typeof val === "number" ? formatNumber(val, 2) : String(val)}`)
      .join(", ");
  }
  return String(v);
}

function GateChecksTable({ checks }: { checks: GateCheck[] }) {
  if (!checks.length) return <div className="text-sm text-text-muted">No checks reported.</div>;
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {["", "Check", "Observed", "Threshold"].map((h, i) => (
              <th key={i} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i <= 1 ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {checks.map((c, i) => (
            <tr key={c.key ?? i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              <td className="px-2.5 py-1.5">
                {c.passed ? <CheckCircle2 size={14} className="text-accent-green" /> : <XCircle size={14} className={c.severity === "warning" ? "text-accent-amber" : "text-accent-red"} />}
              </td>
              <td className="px-2.5 py-1.5 text-[12px] text-text-primary">
                {c.label || c.key}
                {c.detail ? <div className="text-[10.5px] text-text-muted">{c.detail}</div> : null}
              </td>
              <td className={`px-2.5 py-1.5 text-right text-[12px] font-mono ${c.passed ? "text-text-secondary" : "text-accent-red"}`}>{fmtVal(c.observed)}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-muted">{fmtVal(c.threshold)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GateCard({ gate, result, loading }: { gate: string; result?: GateResult; loading?: boolean }) {
  const checks = result?.checks || [];
  const passed = result?.passed;
  const failing = checks.filter((c) => c.passed === false);
  return (
    <Section
      title={`${gate} · ${result?.label || ""}`.trim()}
      icon={<ShieldCheck size={16} />}
      rightSlot={
        <StatusBadge
          label={loading && !result ? "running" : passed ? "pass" : "fail"}
          variant={loading && !result ? "info" : passed ? "success" : "error"}
        />
      }
    >
      {loading && !result ? (
        <div className="py-6 text-center text-sm text-text-muted">Validating…</div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-bg-primary/40">
              <div className="h-full rounded-full" style={{ width: `${Math.round((result?.score ?? 0) * 100)}%`, background: passed ? "var(--accent-green)" : "var(--accent-amber)" }} />
            </div>
            <span className="font-mono text-[12px] text-text-secondary">{formatPct(result?.score, 0)}</span>
          </div>
          {failing.length ? (
            <div className="text-[11.5px] text-accent-red">
              {failing.length} blocking check{failing.length > 1 ? "s" : ""}: {failing.map((c) => c.label || c.key).join(", ")}
            </div>
          ) : (
            <div className="text-[11.5px] text-accent-green">All checks passing.</div>
          )}
          <GateChecksTable checks={checks} />
        </div>
      )}
    </Section>
  );
}

export function GatesPanel({ symbol, snapshot }: { symbol: string; snapshot?: Snapshot }) {
  // Gate A re-validates the data/feature engine against the *current* snapshot
  // request body (session/quote/bars). It only runs once a snapshot exists.
  const gateABody = useMemo(() => {
    const r = snapshot?.request;
    if (!r?.session || !r?.quote || !r?.bars) return null;
    return { session: r.session, quote: r.quote, bars: r.bars, depth: r.depth, prior_bars: r.prior_bars, trades: r.trades };
  }, [snapshot]);

  const gateAQuery = useQuery({
    queryKey: ["auction", "gate-a", symbol, snapshot?.session_date, (snapshot?.request?.bars || []).length],
    queryFn: async () => (await apiClient.post("/api/auction-intelligence/validate-gate-a", gateABody)).data as GateResult,
    enabled: !!gateABody,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const gateBQuery = useQuery({
    queryKey: ["auction", "gate-b", symbol],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/validate-gate-b", { params: { symbol } })).data as GateResult,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const gateCQuery = useQuery({
    queryKey: ["auction", "gate-c", symbol],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/validate-gate-c", { params: { symbol } })).data as GateResult,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const canaryQuery = useQuery({
    queryKey: ["auction", "canary", symbol],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/canary-readiness", { params: { symbol } })).data as CanaryReadiness,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const canary = canaryQuery.data;
  const req = canary?.requirements;
  const passCount = [gateAQuery.data?.passed, gateBQuery.data?.passed, gateCQuery.data?.passed].filter(Boolean).length;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
        <MetricTile label="Gates passing" value={`${passCount}/3`} color={passCount === 3 ? "text-accent-green" : passCount === 0 ? "text-accent-red" : "text-accent-amber"} />
        <MetricTile label="Gate A score" value={formatPct(gateAQuery.data?.score, 0)} detail="data + features" />
        <MetricTile label="Gate B score" value={formatPct(gateBQuery.data?.score, 0)} detail="walk-forward" />
        <MetricTile label="Gate C score" value={formatPct(gateCQuery.data?.score, 0)} detail="shadow / drift" />
        <MetricTile label="Canary" value={canary?.ready ? "READY" : "BLOCKED"} detail={canary?.stage} color={canary?.ready ? "text-accent-green" : "text-accent-amber"} />
      </section>

      <GateCard gate="Gate A" result={gateAQuery.data} loading={gateAQuery.isFetching} />
      <GateCard gate="Gate B" result={gateBQuery.data} loading={gateBQuery.isFetching} />
      <GateCard gate="Gate C" result={gateCQuery.data} loading={gateCQuery.isFetching} />

      <Section title={`Canary readiness · ${canary?.symbol || symbol}`} icon={<Rocket size={16} />} rightSlot={<StatusBadge label={canary?.ready ? "ready" : "blocked"} variant={canary?.ready ? "success" : "warn"} />}>
        {canaryQuery.isFetching && !canary ? (
          <div className="py-6 text-center text-sm text-text-muted">Checking readiness…</div>
        ) : (
          <div className="space-y-3">
            {canary?.blockers?.length ? (
              <ul className="space-y-1.5">
                {canary.blockers.map((b, i) => (
                  <li key={i} className="flex items-center gap-2 text-[12.5px] text-text-secondary">
                    <XCircle size={13} className="text-accent-amber" /> {b}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-[12.5px] text-accent-green">No blockers — symbol is canary-ready.</div>
            )}
            {req ? (
              <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-5">
                <MetricTile size="sm" label="Manual approval" value={req.manual_approval_required ? "required" : "no"} />
                <MetricTile size="sm" label="Max live lots" value={String(req.max_live_lots ?? "—")} />
                <MetricTile size="sm" label="Daily loss cap" value={formatNumber(req.daily_loss_limit, 0)} />
                <MetricTile size="sm" label="Size mult." value={formatNumber(req.max_size_multiplier, 2)} />
                <MetricTile size="sm" label="Agents" value={(req.allowed_agents || []).join(", ") || "—"} />
              </div>
            ) : null}
          </div>
        )}
      </Section>
    </div>
  );
}
