"use client";

import { AlertTriangle, ShieldCheck } from "lucide-react";

import { MetricTile, Section, StatusBadge, formatIST, formatSignedMoney, tone } from "@/components/desk-ui";

type Summary = Record<string, number | string | boolean | null | undefined>;
type Position = {
  underlying?: string | null;
  regime?: string | null;
  close_reason?: string | null;
  realized_pnl?: number | null;
  selection_reason?: string | null;
  fill_model_version?: string | null;
};
type Positions = { open_positions?: Position[]; closed_positions?: Position[] };

type Cohort = { label: string; trades: number; wins: number; pnl: number };

function number(value: unknown): number {
  const parsed = typeof value === "string" ? Number(value) : value;
  return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : 0;
}

function cohorts(rows: Position[], key: (row: Position) => string): Cohort[] {
  const grouped = new Map<string, Cohort>();
  rows.forEach((row) => {
    const label = key(row) || "unclassified";
    const current = grouped.get(label) ?? { label, trades: 0, wins: 0, pnl: 0 };
    const pnl = number(row.realized_pnl);
    current.trades += 1;
    current.wins += pnl > 0 ? 1 : 0;
    current.pnl += pnl;
    grouped.set(label, current);
  });
  return Array.from(grouped.values()).sort((a, b) => a.pnl - b.pnl);
}

function CohortTable({ title, rows }: { title: string; rows: Cohort[] }) {
  return (
    <Section title={title}>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/60">
              <th className="px-2 py-2 text-left">Cohort</th>
              <th className="px-2 py-2 text-right">Trades</th>
              <th className="px-2 py-2 text-right">Win rate</th>
              <th className="px-2 py-2 text-right">Net P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label} className="border-b border-bg-border/30">
                <td className="px-2 py-2 text-text-primary">{row.label.replaceAll("_", " ")}</td>
                <td className="px-2 py-2 text-right font-mono">{row.trades}</td>
                <td className="px-2 py-2 text-right font-mono">{row.trades ? `${((row.wins / row.trades) * 100).toFixed(1)}%` : "—"}</td>
                <td className={`px-2 py-2 text-right font-mono ${tone(row.pnl)}`}>{formatSignedMoney(row.pnl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

export default function DirectionalPerformanceDiagnostics({
  summary,
  positions,
}: {
  summary?: Summary;
  positions?: Positions;
}) {
  const closed = positions?.closed_positions ?? [];
  const realized = number(summary?.realized_pnl);
  const totalPnl = number(summary?.total_pnl);
  const totalTrades = number(summary?.total_trades ?? closed.length);
  const winRate = number(summary?.win_rate);
  const losses = closed.filter((row) => number(row.realized_pnl) < 0);
  const grossLoss = Math.abs(losses.reduce((sum, row) => sum + number(row.realized_pnl), 0));
  const largestLoss = losses.length ? Math.min(...losses.map((row) => number(row.realized_pnl))) : 0;
  const concentration = grossLoss > 0 ? Math.abs(largestLoss) / grossLoss : 0;
  const guardViolations = closed.filter((row) => {
    const reason = String(row.selection_reason || "").toLowerCase();
    return reason.includes("no trade") || /and -[\d.]+ net trading edge/.test(reason);
  });
  const enoughForDirectionalRetune = totalTrades >= 30;
  const state = totalTrades >= 10 && (realized < 0 || winRate < 0.35) ? "underperforming" : "building evidence";

  return (
    <div className="space-y-4">
      <Section title={`Session · ${summary?.session_date || "loading"}`} description="Realized P&L belongs to trades closed this IST date. Open-position P&L is since entry, not today's return.">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile label="Realized today" value={summary ? formatSignedMoney(number(summary.realized_today)) : "—"} detail={`${number(summary?.closes_today)} closes · ${number(summary?.opens_today)} opens`} color={tone(number(summary?.realized_today))} />
          <MetricTile label="Open P&L since entry" value={summary ? formatSignedMoney(number(summary.unrealized_pnl)) : "—"} detail="Recorded marks; may be stale" />
          <MetricTile label="Marks older than 2 min" value={summary ? `${number(summary.stale_open_marks)}/${number(summary.open_positions)}` : "—"} detail="Execution freshness, including after hours" color={number(summary?.stale_open_marks) ? "text-accent-amber" : undefined} />
          <MetricTile label="Oldest held mark" value={summary?.oldest_open_mark_at ? formatIST(String(summary.oldest_open_mark_at)) : "—"} detail="Market observation time" />
        </div>
        {number(summary?.stale_open_marks) > 0 && <p role="status" className="mt-3 rounded-lg border border-accent-amber/30 bg-accent-amber/5 p-3 text-xs text-amber-200">Stale marks: {String(summary?.stale_open_symbols || "unknown")}. Total equity is provisional. Held contracts are subscribed independently of ATM selection; protective exits require a fresh observed quote. Closed-market marks are expected to age.</p>}
      </Section>
      <Section
        title="Performance diagnosis"
        icon={<AlertTriangle size={16} />}
        description="Closed-trade evidence from the durable directional paper book. This diagnoses the lane; it does not turn a small sample into a parameter search."
        rightSlot={<StatusBadge label={state} variant={state === "underperforming" ? "error" : "warn"} />}
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile label="Net P&L" value={summary ? formatSignedMoney(totalPnl) : "—"} detail={`${totalTrades} closed trades${number(summary?.stale_open_marks) ? " · provisional marks" : ""}`} color={tone(totalPnl)} />
          <MetricTile label="Win rate" value={totalTrades ? `${(winRate * 100).toFixed(1)}%` : "—"} detail={enoughForDirectionalRetune ? "retune sample reached" : `${Math.max(0, 30 - totalTrades)} trades to 30-trade review`} />
          <MetricTile label="Largest loss share" value={grossLoss ? `${(concentration * 100).toFixed(1)}%` : "—"} detail="share of gross closed losses" color={concentration >= 0.4 ? "text-accent-red" : undefined} />
          <MetricTile label="Invalid-entry history" value={String(guardViolations.length)} detail="own evidence said no trade / negative edge" color={guardViolations.length ? "text-accent-red" : "text-accent-green"} />
        </div>
        <div className="mt-4 rounded-xl border border-accent-blue/25 bg-accent-blue/5 p-3 text-xs text-text-secondary">
          <div className="flex items-center gap-2 font-medium text-text-primary"><ShieldCheck size={14} /> Paper-entry boundary hardened</div>
          <p className="mt-1">A contract carrying joint-research evidence with <span className="font-mono">eligible=false</span> is now rejected again at the durable paper ledger, even if an upstream payload incorrectly says risk approved. Existing positions and historical rows are preserved.</p>
        </div>
        {!enoughForDirectionalRetune ? (
          <p className="mt-3 text-xs text-amber-200">Only {totalTrades} trades are closed. The loss is operationally important, but the sample is too small and policy-mixed for a causal parameter retune. The safe change is to stop known-invalid entries and keep collecting post-fix paper evidence.</p>
        ) : null}
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <CohortTable title="Performance by regime" rows={cohorts(closed, (row) => row.regime || "unclassified")} />
        <CohortTable title="Performance by close reason" rows={cohorts(closed, (row) => row.close_reason || "legacy / unavailable")} />
      </div>
    </div>
  );
}
