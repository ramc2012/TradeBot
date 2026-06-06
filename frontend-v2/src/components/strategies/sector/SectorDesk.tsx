"use client";

/**
 * Sector Interaction desk — native v2.
 *
 * A cross-sector rotation + causal-structure desk built on the live NSE
 * F&O / ATM watchlist (overview + signals) and the real sector-index
 * VAR/Granger model. No paper-trading lane — this is an analytical desk,
 * so there is no Performance tab.
 *
 * Tabs:
 *   rotation → RRG scatter (innovative centerpiece) + leadership table.
 *              Click any sector row/bubble to open the drill-down detail.
 *   leaders  → live signal rankings (stance, drivers) + indicators + alerts.
 *   model    → VAR/Granger causal network + correlation heatmap +
 *              leader/follower ranking + significant-edge table.
 *   detail   → per-sector drill-down: drivers/draggers, parameters,
 *              performance cycle, alt-data playbook (lazy-loaded on select).
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Compass,
  GitBranch,
  Grid3x3,
  Layers,
  ListTree,
  Radar,
  Trophy,
} from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedNumber,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

import { RrgChart, type RrgPoint } from "./RrgChart";
import { SectorNetwork, type NetNode, type NetEdge } from "./SectorNetwork";
import { CorrelationHeatmap, type CorrMatrix } from "./CorrelationHeatmap";

const TABS = [
  { key: "rotation", label: "Rotation", icon: Radar },
  { key: "leaders", label: "Leaders", icon: Trophy },
  { key: "model", label: "Causal Model", icon: GitBranch },
  { key: "detail", label: "Sector Detail", icon: ListTree },
];

const QUAD_VARIANT: Record<string, "success" | "info" | "warn" | "error" | "neutral"> = {
  leading: "success",
  improving: "info",
  weakening: "warn",
  lagging: "error",
};

const STANCE_VARIANT: Record<string, "success" | "error" | "neutral"> = {
  overweight: "success",
  underweight: "error",
  neutral: "neutral",
};

// ---- data shapes (from /api/sector-interaction/*) ----
type Constituent = {
  symbol: string;
  kind?: string;
  underlying_price?: number;
  change_pct?: number;
  oi_change_pct?: number;
  oi_signal?: number;
  volume?: number;
  iv?: number;
  rsi?: number;
  macd_histogram?: number;
  leadership_score?: number;
};
type SectorRow = {
  sector_key: string;
  sector: string;
  constituents?: number;
  leadership_score?: number;
  relative_strength?: number;
  momentum?: number;
  avg_change_pct?: number;
  avg_oi_change_pct?: number;
  avg_iv?: number;
  rrg_quadrant?: string;
  rank?: number;
  leaders?: Constituent[];
  laggards?: Constituent[];
};
type Overview = {
  as_of?: string;
  source_mode?: string;
  universe?: { symbols?: number; stocks?: number; indices?: number; mapped?: number; unmapped?: string[] };
  sectors?: SectorRow[];
  rrg?: RrgPoint[];
  notes?: string[];
};

type Driver = { indicator: string; category?: string; contribution?: number; latest_z?: number };
type Ranking = { sector: string; score?: number; rank?: number; change?: number; stance?: string; top_drivers?: Driver[] };
type Indicator = { code: string; label: string; category?: string; latest_z?: number; quality_score?: number; signal_state?: string; cadence?: string };
type Alert = { sector?: string; severity?: string; message?: string };
type Signals = {
  as_of?: string;
  rankings?: Ranking[];
  indicator_latest?: Indicator[];
  alerts?: Alert[];
  runtime_handoff?: { active?: boolean; observed_dates?: number; required_dates?: number; indicator_count?: number; source?: string };
};

type Model = {
  source?: string;
  source_note?: string;
  timeframe?: string;
  periods?: number;
  requested_periods?: number;
  selected_lag?: number;
  alpha?: number;
  sectors?: string[];
  correlation_matrix?: CorrMatrix;
  network?: { nodes?: NetNode[]; edges?: NetEdge[] };
  rankings?: { leaders?: NetNode[]; followers?: NetNode[] };
  real_data_contract?: { synthetic_used?: boolean; minimum_periods?: number; observed_periods?: number; sector_count?: number };
};

type Phase = { name?: string; index?: number; description?: string };
type SectorDetail = {
  sector_key?: string;
  sector?: string;
  rank?: number;
  sector_count?: number;
  summary?: SectorRow;
  constituents?: Constituent[];
  parameters?: Array<{ code: string; label: string; value?: number; unit?: string; state?: string }>;
  performance_cycle?: {
    current_phase?: string;
    next_phase_to_watch?: string;
    cycle_score?: number;
    relative_strength?: number;
    momentum?: number;
    phases?: Phase[];
    interpretation?: string;
  };
  alt_data?: Array<{ name: string; status?: string; value?: number; unit?: string; state?: string; detail?: string }>;
  relative_position?: Array<{ sector_key: string; sector: string; rank?: number; leadership_score?: number; quadrant?: string }>;
};

const quadVariant = (q?: string) => QUAD_VARIANT[String(q || "").toLowerCase()] || "neutral";

export default function SectorDesk() {
  const [activeTab, setActiveTab] = useUrlTab("rotation");
  const [selected, setSelected] = useState<string | null>(null);

  const overviewQuery = useQuery({
    queryKey: ["sector", "overview"],
    queryFn: async () => (await apiClient.get("/api/sector-interaction/india/overview")).data as Overview,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const signalsQuery = useQuery({
    queryKey: ["sector", "signals"],
    queryFn: async () => (await apiClient.get("/api/sector-interaction/signals", { params: { country: "IN" } })).data as Signals,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const modelQuery = useQuery({
    queryKey: ["sector", "model"],
    queryFn: async () => (await apiClient.get("/api/sector-interaction/model", { params: { country: "IN" } })).data as Model,
    enabled: activeTab === "model",
    refetchInterval: REFRESH_MS.slow,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const detailQuery = useQuery({
    queryKey: ["sector", "detail", selected],
    queryFn: async () => (await apiClient.get(`/api/sector-interaction/sectors/${selected}`)).data as SectorDetail,
    enabled: activeTab === "detail" && !!selected,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const ov = overviewQuery.data;
  const sectors = useMemo(() => [...(ov?.sectors || [])].sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99)), [ov]);
  const rrg = ov?.rrg || [];

  // KPI: quadrant census + top leader.
  const census = useMemo(() => {
    const c: Record<string, number> = { leading: 0, improving: 0, weakening: 0, lagging: 0 };
    for (const p of rrg) c[String(p.quadrant || "").toLowerCase()] = (c[String(p.quadrant || "").toLowerCase()] || 0) + 1;
    return c;
  }, [rrg]);
  const topLeader = sectors[0];
  const bottomLagger = sectors[sectors.length - 1];

  const openDetail = (key: string) => {
    setSelected(key);
    setActiveTab("detail");
  };

  return (
    <DeskShell
      title="Sector Interaction"
      description="Live NSE sector rotation, leadership and VAR/Granger causal structure from the F&O / ATM watchlist."
      asOf={ov?.as_of}
      isFetching={overviewQuery.isFetching}
      isLive
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/sector-interaction"
      rightSlot={
        <div className="hidden items-center gap-2 md:flex">
          <StatusBadge label={`${ov?.universe?.mapped ?? 0}/${ov?.universe?.symbols ?? 0} mapped`} variant="info" />
          <StatusBadge label={ov?.source_mode === "live_fno_atm_watchlist" ? "live F&O" : ov?.source_mode || "—"} variant="success" />
        </div>
      }
    >
      {activeTab === "rotation" ? (
        <RotationTab
          ov={ov}
          sectors={sectors}
          rrg={rrg}
          census={census}
          topLeader={topLeader}
          bottomLagger={bottomLagger}
          selected={selected}
          onSelect={setSelected}
          onOpen={openDetail}
        />
      ) : null}

      {activeTab === "leaders" ? <LeadersTab signals={signalsQuery.data} onOpenSector={openDetail} sectors={sectors} /> : null}

      {activeTab === "model" ? <ModelTab data={modelQuery.data} loading={modelQuery.isFetching} /> : null}

      {activeTab === "detail" ? (
        <DetailTab
          selected={selected}
          sectors={sectors}
          detail={detailQuery.data}
          loading={detailQuery.isFetching}
          onSelect={setSelected}
        />
      ) : null}
    </DeskShell>
  );
}

/* ----------------------------- Rotation tab ----------------------------- */

function RotationTab({
  ov,
  sectors,
  rrg,
  census,
  topLeader,
  bottomLagger,
  selected,
  onSelect,
  onOpen,
}: {
  ov?: Overview;
  sectors: SectorRow[];
  rrg: RrgPoint[];
  census: Record<string, number>;
  topLeader?: SectorRow;
  bottomLagger?: SectorRow;
  selected: string | null;
  onSelect: (k: string) => void;
  onOpen: (k: string) => void;
}) {
  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Sectors tracked" value={String(sectors.length)} detail={`${ov?.universe?.stocks ?? 0} stocks`} />
        <MetricTile
          label="Top leader"
          value={shortLabel(topLeader?.sector)}
          detail={`lead ${formatNumber(topLeader?.leadership_score, 1)}`}
          color="text-accent-green"
        />
        <MetricTile
          label="Weakest"
          value={shortLabel(bottomLagger?.sector)}
          detail={`lead ${formatNumber(bottomLagger?.leadership_score, 1)}`}
          color="text-accent-red"
        />
        <MetricTile label="Leading" value={String(census.leading || 0)} detail="strong · accel" color="text-accent-green" />
        <MetricTile label="Improving" value={String(census.improving || 0)} detail="weak · accel" color="text-accent-blue" />
        <MetricTile label="Lagging" value={String(census.lagging || 0)} detail="weak · decel" color="text-accent-red" />
      </section>

      <Section
        title="Relative Rotation Graph"
        icon={<Radar size={16} />}
        description="x = relative strength · y = momentum · bubble size = leadership magnitude. Click a sector to drill in."
      >
        <RrgChart points={rrg} selected={selected} onSelect={onSelect} />
      </Section>

      <Section title="Sector leadership" icon={<Layers size={16} />} description="Ranked by live leadership score. Click a row for the drill-down.">
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-bg-border/60">
                {["#", "Sector", "Quadrant", "Lead", "RS", "Mom", "Avg %chg", "Avg OIΔ%", "Avg IV", "Names"].map((h, i) => (
                  <th
                    key={h}
                    className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${
                      i <= 1 ? "text-left" : "text-right"
                    }`}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sectors.map((s) => {
                const isSel = s.sector_key === selected;
                return (
                  <tr
                    key={s.sector_key}
                    onMouseEnter={() => onSelect(s.sector_key)}
                    onClick={() => onOpen(s.sector_key)}
                    className={`cursor-pointer border-b border-bg-border/25 hover:bg-bg-primary/25 ${isSel ? "bg-bg-primary/20" : ""}`}
                  >
                    <td className="px-2.5 py-1.5 text-left font-mono text-[12px] text-text-muted">{s.rank ?? "—"}</td>
                    <td className="px-2.5 py-1.5 text-left text-[12.5px] font-medium text-text-primary">{shortLabel(s.sector)}</td>
                    <td className="px-2.5 py-1.5 text-right">
                      <StatusBadge label={s.rrg_quadrant || "—"} variant={quadVariant(s.rrg_quadrant)} />
                    </td>
                    <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(s.leadership_score)}`}>
                      {formatSignedNumber(s.leadership_score, 1)}
                    </td>
                    <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{formatNumber(s.relative_strength, 1)}</td>
                    <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(s.momentum)}`}>{formatSignedNumber(s.momentum, 2)}</td>
                    <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(s.avg_change_pct)}`}>
                      {formatPct(s.avg_change_pct, 2, { asPercent: true })}
                    </td>
                    <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(s.avg_oi_change_pct)}`}>
                      {formatPct(s.avg_oi_change_pct, 1, { asPercent: true })}
                    </td>
                    <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{formatNumber(s.avg_iv, 1)}</td>
                    <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-muted">{s.constituents ?? "—"}</td>
                  </tr>
                );
              })}
              {!sectors.length ? (
                <tr>
                  <td colSpan={10} className="px-2.5 py-6 text-center text-sm text-text-muted">
                    Awaiting live sector data…
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </Section>

      {ov?.notes?.length ? (
        <Section title="Method notes" icon={<Activity size={16} />}>
          <ul className="space-y-1.5">
            {ov.notes.map((n, i) => (
              <li key={i} className="flex items-start gap-2 text-[12px] text-text-secondary">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-text-muted/60" />
                {n}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

/* ----------------------------- Leaders tab ----------------------------- */

function LeadersTab({
  signals,
  onOpenSector,
  sectors,
}: {
  signals?: Signals;
  onOpenSector: (k: string) => void;
  sectors: SectorRow[];
}) {
  const rankings = signals?.rankings || [];
  const indicators = signals?.indicator_latest || [];
  const alerts = signals?.alerts || [];
  const handoff = signals?.runtime_handoff;
  // map a sector label back to its key for drill-down
  const keyOf = (label?: string) => sectors.find((s) => s.sector === label)?.sector_key;

  const overweight = rankings.filter((r) => r.stance === "overweight").length;
  const underweight = rankings.filter((r) => r.stance === "underweight").length;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <MetricTile label="Ranked sectors" value={String(rankings.length)} />
        <MetricTile label="Overweight" value={String(overweight)} color="text-accent-green" />
        <MetricTile label="Underweight" value={String(underweight)} color="text-accent-red" />
        <MetricTile label="Indicators" value={String(indicators.length)} detail={handoff?.source ? "live" : ""} />
        <MetricTile label="Alerts" value={String(alerts.length)} color={alerts.length ? "text-accent-amber" : undefined} />
        <MetricTile label="Feed" value={handoff?.active ? "active" : "idle"} detail={`obs ${handoff?.observed_dates ?? 0}/${handoff?.required_dates ?? 0}`} />
      </section>

      <Section title="Live signal rankings" icon={<Trophy size={16} />} description="Stance + the top contributing drivers per sector. Click to drill in.">
        <div className="grid gap-3 md:grid-cols-2">
          {rankings.map((r) => {
            const key = keyOf(r.sector);
            return (
              <button
                key={r.sector}
                type="button"
                onClick={() => key && onOpenSector(key)}
                className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-left transition hover:border-bg-border hover:bg-bg-primary/30"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-text-muted">#{r.rank}</span>
                    <span className="text-[13px] font-semibold text-text-primary">{shortLabel(r.sector)}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`font-mono text-[13px] ${tone(r.score)}`}>{formatSignedNumber(r.score, 2)}</span>
                    <StatusBadge label={r.stance || "neutral"} variant={STANCE_VARIANT[String(r.stance || "neutral")] || "neutral"} />
                  </div>
                </div>
                <div className="mt-2 space-y-1">
                  {(r.top_drivers || []).slice(0, 4).map((d, i) => {
                    const c = d.contribution ?? 0;
                    const mag = Math.min(100, Math.abs(c) * 22);
                    return (
                      <div key={i} className="flex items-center gap-2">
                        <span className="w-[44%] shrink-0 truncate text-[10.5px] text-text-muted">{d.indicator}</span>
                        <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-bg-primary/40">
                          <div
                            className="absolute top-0 h-full rounded-full"
                            style={{
                              width: `${mag}%`,
                              left: c >= 0 ? "50%" : `${50 - mag / 2}%`,
                              background: c >= 0 ? "rgb(var(--accent-green))" : "rgb(var(--accent-red))",
                            }}
                          />
                          <div className="absolute left-1/2 top-0 h-full w-px bg-bg-border" />
                        </div>
                        <span className={`w-10 shrink-0 text-right font-mono text-[10px] ${tone(c)}`}>{formatSignedNumber(c, 2)}</span>
                      </div>
                    );
                  })}
                </div>
              </button>
            );
          })}
          {!rankings.length ? <div className="py-8 text-center text-sm text-text-muted md:col-span-2">No live rankings yet.</div> : null}
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Live indicators" icon={<Activity size={16} />}>
          <MiniTable
            head={["Indicator", "Category", "z", "Qual", "State"]}
            rows={indicators.map((ind) => [
              ind.label,
              <span key="c" className="text-text-muted">{ind.category}</span>,
              <span key="z" className={tone(ind.latest_z)}>{formatSignedNumber(ind.latest_z, 2)}</span>,
              formatPct(ind.quality_score, 0),
              <StatusBadge
                key="s"
                label={ind.signal_state || "—"}
                variant={ind.signal_state === "positive" ? "success" : ind.signal_state === "negative" ? "error" : "neutral"}
              />,
            ])}
          />
        </Section>
        <Section title="Alerts" icon={<AlertTriangle size={16} />}>
          {alerts.length ? (
            <ul className="space-y-1.5">
              {alerts.map((a, i) => (
                <li key={i} className="flex items-center gap-2 text-[12.5px] text-text-secondary">
                  <StatusBadge
                    label={a.severity || "info"}
                    variant={a.severity === "high" ? "error" : a.severity === "medium" ? "warn" : "info"}
                  />
                  {a.message}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-text-muted">No active alerts.</div>
          )}
        </Section>
      </div>
    </div>
  );
}

/* ----------------------------- Model tab ----------------------------- */

function ModelTab({ data, loading }: { data?: Model; loading: boolean }) {
  if (loading && !data) {
    return (
      <Section title="VAR / Granger model">
        <div className="py-10 text-center text-sm text-text-muted">Estimating causal structure…</div>
      </Section>
    );
  }
  const nodes = data?.network?.nodes || [];
  const edges = data?.network?.edges || [];
  const leaders = data?.rankings?.leaders || [];
  const followers = data?.rankings?.followers || [];
  const contract = data?.real_data_contract;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <MetricTile label="Sectors" value={String((data?.sectors || []).length)} detail={data?.timeframe} />
        <MetricTile label="Periods" value={String(data?.periods ?? 0)} detail={`req ${data?.requested_periods ?? 0}`} />
        <MetricTile label="Lag" value={String(data?.selected_lag ?? "—")} detail={`α ${data?.alpha ?? ""}`} />
        <MetricTile label="Sig. edges" value={String(edges.length)} />
        <MetricTile label="Net leaders" value={String(leaders.length)} color="text-accent-green" />
        <MetricTile
          label="Data"
          value={contract?.synthetic_used ? "synthetic" : "real"}
          detail={contract?.synthetic_used ? "fallback" : "broker/NSE"}
          color={contract?.synthetic_used ? "text-accent-amber" : "text-accent-green"}
        />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Granger causal network" icon={<GitBranch size={16} />} description="Directed: source leads target · thickness = weight · opacity = significance.">
          <SectorNetwork nodes={nodes} edges={edges} />
        </Section>
        <Section title="Correlation heatmap" icon={<Grid3x3 size={16} />} description="Pearson ρ across sector-index returns over the model window.">
          <CorrelationHeatmap matrix={data?.correlation_matrix} />
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Leaders (net influence ↑)" icon={<Trophy size={16} />}>
          <MiniTable
            head={["Sector", "Net inf.", "Out w", "Out e"]}
            rows={leaders.map((n) => [
              n.label || (n as { sector?: string }).sector || n.id,
              <span key="n" className={tone(n.net_influence)}>{formatSignedNumber(n.net_influence, 2)}</span>,
              formatNumber((n as { outgoing_weight?: number }).outgoing_weight, 2),
              String(n.outgoing_edges ?? 0),
            ])}
          />
        </Section>
        <Section title="Followers (net influence ↓)" icon={<ListTree size={16} />}>
          <MiniTable
            head={["Sector", "Net inf.", "In w", "In e"]}
            rows={followers.map((n) => [
              n.label || (n as { sector?: string }).sector || n.id,
              <span key="n" className={tone(n.net_influence)}>{formatSignedNumber(n.net_influence, 2)}</span>,
              formatNumber((n as { incoming_weight?: number }).incoming_weight, 2),
              String(n.incoming_edges ?? 0),
            ])}
          />
        </Section>
      </div>

      <Section title="Significant causal edges" icon={<GitBranch size={16} />} description={data?.source_note}>
        <MiniTable
          head={["Relationship", "Lag", "Weight", "p-value"]}
          rows={[...edges]
            .sort((a, b) => (a.p_value ?? 1) - (b.p_value ?? 1))
            .map((e) => [
              e.relationship || `${e.source} → ${e.target}`,
              String(e.lag ?? "—"),
              formatNumber(e.weight, 2),
              <span key="p" className={(e.p_value ?? 1) < 0.05 ? "text-accent-green" : "text-text-muted"}>
                {e.p_value != null ? e.p_value.toFixed(4) : "—"}
              </span>,
            ])}
        />
      </Section>
    </div>
  );
}

/* ----------------------------- Detail tab ----------------------------- */

function DetailTab({
  selected,
  sectors,
  detail,
  loading,
  onSelect,
}: {
  selected: string | null;
  sectors: SectorRow[];
  detail?: SectorDetail;
  loading: boolean;
  onSelect: (k: string) => void;
}) {
  if (!selected) {
    return (
      <Section title="Sector drill-down" icon={<ListTree size={16} />} description="Pick a sector to inspect drivers, draggers, the rotation cycle and the alt-data playbook.">
        <div className="flex flex-wrap gap-2">
          {sectors.map((s) => (
            <button
              key={s.sector_key}
              type="button"
              onClick={() => onSelect(s.sector_key)}
              className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-1.5 text-[12px] text-text-secondary transition hover:border-bg-border hover:bg-bg-primary/30"
            >
              {shortLabel(s.sector)}
            </button>
          ))}
        </div>
      </Section>
    );
  }
  if (loading && !detail) {
    return (
      <Section title="Sector drill-down">
        <div className="py-10 text-center text-sm text-text-muted">Loading {selected}…</div>
      </Section>
    );
  }

  const sum = detail?.summary;
  const cons = [...(detail?.constituents || [])].sort((a, b) => (b.leadership_score ?? 0) - (a.leadership_score ?? 0));
  const drivers = cons.filter((c) => (c.leadership_score ?? 0) >= 0).slice(0, 8);
  const draggers = cons.filter((c) => (c.leadership_score ?? 0) < 0).slice(-8).reverse();
  const cycle = detail?.performance_cycle;
  const phases = cycle?.phases || [];
  const curIdx = phases.findIndex((p) => p.name === cycle?.current_phase);

  return (
    <div className="space-y-4">
      <Section
        title={detail?.sector || selected}
        icon={<ListTree size={16} />}
        rightSlot={
          <div className="flex items-center gap-2">
            <StatusBadge label={`rank ${detail?.rank ?? "—"}/${detail?.sector_count ?? "—"}`} variant="info" />
            <StatusBadge label={sum?.rrg_quadrant || "—"} variant={quadVariant(sum?.rrg_quadrant)} />
            <SectorSwitcher sectors={sectors} selected={selected} onSelect={onSelect} />
          </div>
        }
      >
        <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile label="Leadership" value={formatSignedNumber(sum?.leadership_score, 2)} color={tone(sum?.leadership_score)} />
          <MetricTile label="Rel. strength" value={formatNumber(sum?.relative_strength, 1)} />
          <MetricTile label="Momentum" value={formatSignedNumber(sum?.momentum, 2)} color={tone(sum?.momentum)} />
          <MetricTile label="Avg %chg" value={formatPct(sum?.avg_change_pct, 2, { asPercent: true })} color={tone(sum?.avg_change_pct)} />
          <MetricTile label="Avg OIΔ%" value={formatPct(sum?.avg_oi_change_pct, 1, { asPercent: true })} color={tone(sum?.avg_oi_change_pct)} />
          <MetricTile label="Avg IV" value={formatNumber(sum?.avg_iv, 1)} detail={`${sum?.constituents ?? 0} names`} />
        </section>
      </Section>

      {detail?.parameters?.length ? (
        <Section title="Sector parameters" icon={<Activity size={16} />}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
            {detail.parameters.map((p) => (
              <div key={p.code} className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2">
                <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{p.label}</div>
                <div className="mt-0.5 font-mono text-[15px] text-text-primary">
                  {formatNumber(p.value, p.unit === "%" ? 1 : 2)}
                  {p.unit ? <span className="ml-0.5 text-[10px] text-text-muted">{p.unit}</span> : null}
                </div>
                <div className="mt-1">
                  <StatusBadge
                    label={p.state || "—"}
                    variant={p.state === "constructive" ? "success" : p.state === "weak" || p.state === "stressed" ? "error" : "neutral"}
                  />
                </div>
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Drivers" icon={<Trophy size={16} />} description="Top positive-leadership constituents.">
          <ConstituentTable rows={drivers} />
        </Section>
        <Section title="Draggers" icon={<AlertTriangle size={16} />} description="Constituents weighing on the sector.">
          <ConstituentTable rows={draggers} />
        </Section>
      </div>

      {cycle ? (
        <Section title="Rotation cycle" icon={<Compass size={16} />} rightSlot={<StatusBadge label={cycle.current_phase || "—"} variant="info" />}>
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <div className="flex flex-wrap gap-2">
                {phases.map((p, i) => {
                  const isCur = i === curIdx;
                  const isNext = p.name === cycle.next_phase_to_watch;
                  return (
                    <div
                      key={p.name || i}
                      className={`flex-1 rounded-lg border px-3 py-2 text-center text-[11.5px] ${
                        isCur
                          ? "border-accent-blue/60 bg-accent-blue/10 text-text-primary"
                          : isNext
                            ? "border-accent-amber/50 bg-accent-amber/10 text-text-secondary"
                            : "border-bg-border bg-bg-primary/10 text-text-muted"
                      }`}
                      title={p.description}
                    >
                      <div className="font-medium">{p.name}</div>
                      {isCur ? <div className="text-[9px] uppercase tracking-wide text-accent-blue">current</div> : null}
                      {isNext && !isCur ? <div className="text-[9px] uppercase tracking-wide text-accent-amber">next</div> : null}
                    </div>
                  );
                })}
              </div>
              {cycle.interpretation ? <p className="mt-3 text-[12.5px] leading-relaxed text-text-secondary">{cycle.interpretation}</p> : null}
            </div>
            <div className="grid grid-cols-3 gap-2 lg:grid-cols-1">
              <Tile label="Cycle score" value={formatNumber(cycle.cycle_score, 2)} />
              <Tile label="Rel. strength" value={formatNumber(cycle.relative_strength, 1)} />
              <Tile label="Momentum" value={formatSignedNumber(cycle.momentum, 2)} />
            </div>
          </div>
        </Section>
      ) : null}

      {detail?.alt_data?.length ? (
        <Section title="Alt-data playbook" icon={<Layers size={16} />}>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {detail.alt_data.map((a, i) => (
              <div key={i} className="rounded-xl border border-bg-border bg-bg-primary/15 p-3">
                <div className="flex items-center justify-between">
                  <span className="text-[12px] font-semibold text-text-primary">{a.name}</span>
                  <StatusBadge label={a.state || a.status || "—"} variant="neutral" />
                </div>
                {a.value != null ? (
                  <div className="mt-1 font-mono text-[16px] text-text-primary">
                    {formatNumber(a.value, a.unit === "%" ? 1 : 2)}
                    {a.unit ? <span className="ml-0.5 text-[10px] text-text-muted">{a.unit}</span> : null}
                  </div>
                ) : null}
                {a.detail ? <p className="mt-1 text-[11px] leading-snug text-text-muted">{a.detail}</p> : null}
              </div>
            ))}
          </div>
        </Section>
      ) : null}
    </div>
  );
}

function SectorSwitcher({ sectors, selected, onSelect }: { sectors: SectorRow[]; selected: string; onSelect: (k: string) => void }) {
  return (
    <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
      <select className="bg-transparent outline-none" value={selected} onChange={(e) => onSelect(e.target.value)}>
        {sectors.map((s) => (
          <option key={s.sector_key} value={s.sector_key} className="bg-bg-card text-text-primary">
            {s.sector}
          </option>
        ))}
      </select>
    </label>
  );
}

function ConstituentTable({ rows }: { rows: Constituent[] }) {
  return (
    <MiniTable
      head={["Symbol", "Price", "%chg", "OIΔ%", "RSI", "Lead"]}
      rows={rows.map((c) => [
        <span key="s" className="font-medium text-text-primary">{c.symbol}</span>,
        formatNumber(c.underlying_price, 1),
        <span key="ch" className={tone(c.change_pct)}>{formatPct(c.change_pct, 2, { asPercent: true })}</span>,
        <span key="oi" className={tone(c.oi_change_pct)}>{formatPct(c.oi_change_pct, 1, { asPercent: true })}</span>,
        formatNumber(c.rsi, 0),
        <span key="ld" className={tone(c.leadership_score)}>{formatSignedNumber(c.leadership_score, 1)}</span>,
      ])}
    />
  );
}

/* ----------------------------- shared bits ----------------------------- */

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className="font-mono text-[14px] text-text-primary">{value}</div>
    </div>
  );
}

function MiniTable({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {head.map((h, i) => (
              <th
                key={i}
                className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${
                  i === 0 ? "text-left" : "text-right"
                }`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((r, i) => (
              <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                {r.map((c, j) => (
                  <td
                    key={j}
                    className={`whitespace-nowrap px-2.5 py-1.5 font-mono text-[12px] ${
                      j === 0 ? "text-left text-text-primary" : "text-right text-text-secondary"
                    }`}
                  >
                    {c}
                  </td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={head.length} className="px-2.5 py-6 text-center text-sm text-text-muted">
                No data
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** Trim "Nifty " prefix so labels read cleanly in tables/tiles. */
function shortLabel(s?: string): string {
  if (!s) return "—";
  return s.replace(/^Nifty\s+/i, "").replace(/Financial Services/i, "Fin Svc");
}
