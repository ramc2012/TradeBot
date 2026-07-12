"use client";

import { useTransition } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Database, Layers3, Play, Radar, ShieldCheck } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  useUrlTab,
} from "@/components/desk-ui";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { getInstitutionalConvergenceStatus, runInstitutionalConvergence } from "@/lib/api";

type GateMap = Record<string, boolean>;
type ResultRow = {
  kind?: string;
  symbol?: string;
  sector?: string;
  status?: string;
  action?: string;
  score?: number;
  alpha_score?: number;
  gates?: GateMap;
  blocked_reasons?: string[];
  order_flow?: { source?: string; book_symbol?: string };
};
type StatusPayload = {
  enabled?: boolean;
  mode?: string;
  market_open?: boolean;
  universe?: { indices?: string[]; stocks?: ResultRow[]; stock_count?: number; sector_count?: number; cbe_scan_date?: string };
  latest?: { generated_at?: string; actionable_count?: number; result_count?: number; results?: ResultRow[]; gate_breakdown?: Record<string, number> };
};

const TABS = [
  { key: "lane", label: "Lane", icon: Radar },
  { key: "gates", label: "Gate audit", icon: ShieldCheck },
];

export default function InstitutionalConvergenceDesk() {
  const [activeTab, setActiveTab] = useUrlTab("lane");
  const queryClient = useQueryClient();
  const [, startTransition] = useTransition();
  const query = useQuery({
    queryKey: ["institutional-convergence", "status"],
    queryFn: async () => (await getInstitutionalConvergenceStatus()).data as StatusPayload,
    refetchInterval: REFRESH_MS.snapshot,
  });
  const run = useMutation({
    mutationFn: runInstitutionalConvergence,
    onSuccess: () => startTransition(() => void queryClient.invalidateQueries({ queryKey: ["institutional-convergence"] })),
  });
  const data = query.data;
  const latest = data?.latest;
  const rows = latest?.results ?? [];
  const indices = rows.filter((row) => row.kind === "index");
  const stocks = rows.filter((row) => row.kind === "stock");

  return (
    <DeskShell
      title="Institutional Convergence"
      description="NSE auction, options-positioning and order-flow convergence · NIFTY/BANKNIFTY + sector-diversified CBE stock sleeve"
      asOf={latest?.generated_at}
      isFetching={query.isFetching || run.isPending}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      rightSlot={
        <div className="flex items-center gap-2">
          <StatusBadge label={data?.market_open ? "NSE open" : "NSE closed"} variant={data?.market_open ? "success" : "neutral"} />
          <StatusBadge label="shadow only" variant="warn" />
          <button className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-surface-hover disabled:opacity-50" disabled={!data?.market_open || run.isPending} onClick={() => run.mutate()}>
            <Play size={12} /> Run cycle
          </button>
        </div>
      }
    >
      {activeTab === "lane" ? <div className="space-y-4">
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile label="Indices" value={String(data?.universe?.indices?.length ?? 0)} detail={(data?.universe?.indices ?? []).join(" · ")} />
          <MetricTile label="Stocks" value={String(data?.universe?.stock_count ?? 0)} detail="one per sector" />
          <MetricTile label="Sectors" value={String(data?.universe?.sector_count ?? 0)} detail={`CBE ${data?.universe?.cbe_scan_date ?? "—"}`} />
          <MetricTile label="Evaluated" value={String(latest?.result_count ?? 0)} detail="latest cycle" />
          <MetricTile label="Actionable" value={String(latest?.actionable_count ?? 0)} detail="shadow signals" />
          <MetricTile label="Mode" value="SHADOW" detail="execution disabled" />
        </section>

        <Section title="Index convergence" icon={<Layers3 size={16} />} description="Full market-profile, options-pressure and genuine-book gates. All gates must pass.">
          <ResultTable rows={indices} empty="Run the first in-session cycle to evaluate NIFTY and BANKNIFTY." />
        </Section>
        <Section title="Diversified stock sleeve" icon={<Activity size={16} />} description="Top CBE names, restricted to one stock per sector. Intraday trigger remains blocked until its own profile and tape are ready.">
          <ResultTable rows={stocks.length ? stocks : (data?.universe?.stocks ?? [])} empty="No sector-diversified CBE candidates are available." />
        </Section>
        <Section title="Blocked-gate census" icon={<Database size={16} />}>
          <div className="flex flex-wrap gap-2">
            {Object.entries(latest?.gate_breakdown ?? {}).map(([gate, count]) => <StatusBadge key={gate} label={`${gate} · ${count}`} variant="warn" />)}
            {!Object.keys(latest?.gate_breakdown ?? {}).length ? <span className="text-sm text-text-muted">No completed cycle yet.</span> : null}
          </div>
        </Section>
      </div> : null}
      {activeTab === "gates" ? <SignalQualityTab laneKeys={["institutional_convergence"]} title="Institutional Convergence signal validation" /> : null}
    </DeskShell>
  );
}

function ResultTable({ rows, empty }: { rows: ResultRow[]; empty: string }) {
  if (!rows.length) return <div className="py-8 text-center text-sm text-text-muted">{empty}</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[780px] text-left text-xs">
        <thead className="text-text-muted"><tr><th className="pb-2">Instrument</th><th>Sector</th><th>Status</th><th>Score</th><th>Order flow</th><th>Gates</th></tr></thead>
        <tbody className="divide-y divide-border">
          {rows.map((row) => (
            <tr key={`${row.kind}-${row.symbol}`}>
              <td className="py-3 font-semibold text-text-primary">{row.symbol}</td>
              <td>{row.sector ?? "INDEX"}</td>
              <td><StatusBadge label={row.status ?? "selected"} variant={row.status === "actionable_shadow" ? "success" : row.status === "error" ? "error" : "warn"} /></td>
              <td>{formatNumber(row.score ?? row.alpha_score, 1)}</td>
              <td>{row.order_flow?.source ?? "collecting"}<div className="text-[10px] text-text-muted">{row.order_flow?.book_symbol}</div></td>
              <td><div className="flex max-w-[360px] flex-wrap gap-1">{Object.entries(row.gates ?? {}).map(([gate, pass]) => <StatusBadge key={gate} label={gate} variant={pass ? "success" : "neutral"} />)}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
