"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Database,
  FileText,
  GitBranch,
  Gauge,
  Loader2,
  Network,
  RefreshCw,
  ShieldCheck,
  Signal,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  describeApiError,
  getSectorInteractionAcquisitionPlan,
  getSectorInteractionExtendedNetwork,
  getSectorInteractionIndiaOverview,
  getSectorInteractionIndiaRealModel,
  getSectorInteractionIngestionStatus,
  getSectorInteractionModel,
  getSectorInteractionNSEConstituentStatus,
  getSectorInteractionOverview,
  getSectorInteractionPipelineStatus,
  getSectorInteractionReport,
  getSectorInteractionSignals,
  getSectorInteractionSourceMap,
  getSectorInteractionValidationBacktest,
  runSectorInteractionIngestion,
  runSectorInteractionIndiaLiveMarketIngestion,
  seedSectorInteractionRAG,
  syncSectorInteractionNSEConstituents,
} from "@/lib/api";

type CountryCode = "US" | "IN";

type OverviewPayload = {
  module: string;
  description: string;
  countries: Array<{
    code: CountryCode;
    label: string;
    sector_count: number;
    sectors: string[];
    source_note: string;
  }>;
  methodology: {
    model: string;
    lag_selection: string;
    edge_weight: string;
    network_interpretation: string;
  };
};

type MatrixPayload = {
  labels: string[];
  values: number[][];
};

type NetworkEdge = {
  source: string;
  target: string;
  p_value: number;
  weight: number;
  lag: number;
  relationship: string;
};

type NetworkNode = {
  id: string;
  label: string;
  sector: string;
  outgoing_weight: number;
  incoming_weight: number;
  outgoing_edges: number;
  incoming_edges: number;
  net_influence: number;
};

type ModelPayload = {
  country: CountryCode;
  label: string;
  source_mode: string;
  source_note: string;
  periods: number;
  selected_lag: number;
  alpha: number;
  sectors: string[];
  correlation_matrix: MatrixPayload;
  network: {
    nodes: NetworkNode[];
    edges: NetworkEdge[];
  };
  rankings: {
    leaders: NetworkNode[];
    followers: NetworkNode[];
  };
  dashboard_panels: string[];
  real_data_contract?: {
    synthetic_used: boolean;
    minimum_periods: number;
    observed_periods: number;
    sector_count: number;
    required_action?: string;
  };
  close_counts?: Record<string, number>;
  timeframe?: string;
};

type PlanPayload = {
  architecture: string[];
  source_categories: Array<{
    name: string;
    examples: string[];
    metrics: string[];
  }>;
  processing: string[];
  legal_controls: string[];
  roadmap: Array<{ phase: string; work: string }>;
};

type SourceMapPayload = {
  country: CountryCode;
  label: string;
  sector_mapping_standard: string;
  indicators: Array<{
    code: string;
    label: string;
    category: string;
    cadence: string;
    source_status: string;
    quality_score: number;
    lead_months: number;
    production_source: string;
    metric_definition: string;
    sector_weights: Record<string, number>;
  }>;
  data_contract: {
    required_columns: string[];
    date_alignment: string;
    normalization: string;
    audit_fields: string[];
  };
};

type SignalsPayload = {
  country: CountryCode;
  label: string;
  as_of: string;
  source_mode: string;
  runtime_handoff: {
    active: boolean;
    observed_dates: number;
    required_dates: number;
    indicator_count: number;
    source: string;
    reason: string;
  };
  rankings: Array<{
    sector: string;
    score: number;
    rank: number;
    change: number;
    stance: "overweight" | "underweight" | "neutral";
    top_drivers: Array<{
      indicator: string;
      category: string;
      contribution: number;
      latest_z: number;
    }>;
  }>;
  indicator_latest: Array<{
    code: string;
    label: string;
    category: string;
    latest_z: number;
    quality_score: number;
    source_status: string;
    cadence: string;
    signal_state: "positive" | "negative" | "neutral";
    mapped_sectors: Record<string, number>;
  }>;
  alerts: Array<{ sector: string; severity: string; message: string }>;
  method: string;
};

type ExtendedNetworkPayload = {
  country: CountryCode;
  label: string;
  selected_lag: number;
  alpha: number;
  nodes: Array<{
    id: string;
    label: string;
    node_type: "sector" | "indicator";
    category: string;
    quality_score?: number;
    source_status?: string;
  }>;
  indicator_edges: Array<NetworkEdge & {
    source_type: string;
    target_type: string;
    category: string;
    configured_exposure: number;
  }>;
  summary: {
    sector_edge_count: number;
    indicator_edge_count: number;
    indicator_count: number;
    sector_count: number;
  };
};

type BacktestPayload = {
  country: CountryCode;
  label: string;
  source_mode: string;
  summary: {
    observations: number;
    cumulative_return_pct: number;
    average_monthly_return_pct: number;
    hit_rate_pct: number;
    information_ratio: number;
    max_drawdown_pct: number;
  };
  equity_curve: Array<{ date: string; value: number }>;
  recent_windows: Array<{
    date: string;
    leaders: string[];
    laggards: string[];
    leader_return: number;
    laggard_return: number;
    long_short_return: number;
    cumulative_return: number;
  }>;
  method: string;
};

type PipelineStatusPayload = {
  country: CountryCode;
  label: string;
  as_of: string;
  summary: {
    connector_count: number;
    open_data_count: number;
    prototype_count: number;
    licensed_required_count: number;
    tos_review_required_count: number;
    readiness_score: number;
    critical_blockers: number;
  };
  connectors: Array<{
    indicator_code: string;
    label: string;
    category: string;
    cadence: string;
    source_status: string;
    production_source: string;
    compliance_state: string;
    run_status: string;
    readiness_score: number;
    quality_score: number;
    schedule: string;
    freshness_sla_hours: number;
    freshness_lag_hours: number;
    rows_loaded_30d: number;
    mapped_sector_count: number;
    blockers: string[];
    next_action: string;
  }>;
  data_layers: Array<{
    layer: string;
    purpose: string;
    primary_keys: string[];
    retention: string;
  }>;
  execution_controls: string[];
  blockers: Array<{
    indicator_code: string;
    label: string;
    blocker: string;
    next_action: string;
  }>;
};

type SectorReportPayload = {
  country: CountryCode;
  label: string;
  as_of: string;
  source_mode: string;
  headline: string;
  summary_bullets: string[];
  top_overweights: SignalsPayload["rankings"];
  top_underweights: SignalsPayload["rankings"];
  strongest_indicator_edges: ExtendedNetworkPayload["indicator_edges"];
  risk_flags: string[];
  next_actions: string[];
  disclaimer: string;
};

type IngestionStatusPayload = {
  country: CountryCode;
  label: string;
  runtime_root: string;
  storage_status: {
    local_root: string;
    local_observation_count: number;
    local_run_count: number;
    durable_enabled: boolean;
    durable_state_key: string;
    durable_updated_at?: string | null;
    durable_observation_count: number;
    durable_run_count: number;
    effective_observation_count: number;
    effective_run_count: number;
    backend: string;
  };
  runtime_summary: {
    observation_count: number;
    indicator_count: number;
    sector_count: number;
    latest_observation_date?: string | null;
    latest_created_at?: string | null;
    run_count: number;
    last_run?: {
      run_id: string;
      mode: string;
      status: string;
      stored_observations: number;
      finished_at: string;
    } | null;
  };
  connectors: Array<{
    indicator_code: string;
    label: string;
    source_status: string;
    run_status: string;
    readiness_score: number;
    latest_observation_date?: string | null;
    latest_value?: number | null;
    has_runtime_data: boolean;
  }>;
  recent_runs: Array<{
    run_id: string;
    mode: string;
    status: string;
    stored_observations: number;
    blocked_connectors: Array<{ indicator_code: string; label: string; reason: string }>;
    finished_at: string;
  }>;
  recent_observations: Array<{
    date: string;
    indicator_code: string;
    sector: string;
    value: number;
    quality_score: number;
    source_status: string;
  }>;
  promotion_rules: string[];
};

type IngestionRunPayload = {
  country: CountryCode;
  dry_run: boolean;
  generated_observations: number;
  stored_observations: number;
  blocked_connectors: Array<{ indicator_code: string; label: string; reason: string }>;
  message: string;
};

type LiveConstituent = {
  symbol: string;
  kind: string;
  sector_key: string;
  sector: string;
  latest_time?: string | null;
  underlying_price: number;
  change_pct: number;
  oi_change_pct: number;
  volume: number;
  iv: number;
  rsi: number;
  leadership_score: number;
};

type LiveSectorSummary = {
  sector_key: string;
  sector: string;
  rank: number;
  constituents: number;
  leadership_score: number;
  relative_strength: number;
  momentum: number;
  avg_change_pct: number;
  avg_oi_change_pct: number;
  avg_iv: number;
  rrg_quadrant: "leading" | "weakening" | "improving" | "lagging";
  leaders: LiveConstituent[];
  laggards: LiveConstituent[];
};

type IndiaLiveOverviewPayload = {
  country: "IN";
  default_country: "IN";
  source_mode: string;
  as_of: string;
  nse_constituent_status?: NSEConstituentStatusPayload;
  universe: {
    symbols: number;
    stocks: number;
    indices: number;
    mapped: number;
    unmapped: string[];
  };
  sectors: LiveSectorSummary[];
  rrg: Array<{
    sector_key: string;
    sector: string;
    x: number;
    y: number;
    quadrant: string;
    leadership_score: number;
  }>;
  notes: string[];
};

type NSEConstituentStatusPayload = {
  state_key: string;
  updated_at?: string | null;
  source: string;
  sector_count: number;
  symbol_count: number;
  successful_sources: Array<{ sector_key: string; label: string; symbols?: number }>;
  failed_sources: Array<{ sector_key: string; label: string; error?: string }>;
  sectors: Array<{
    sector_key: string;
    label: string;
    constituents: number;
    source_url: string;
  }>;
  runtime_overlay_active: boolean;
};

type NSEConstituentSyncPayload = NSEConstituentStatusPayload & {
  stored?: boolean;
  synced_at?: string;
  message?: string;
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function shortLabel(label: string) {
  return label
    .replace("Consumer ", "Cons. ")
    .replace("Communication Services", "Comms")
    .replace("Nifty Financial Services", "Fin Serv")
    .replace("Nifty ", "");
}

function heatColor(value: number) {
  const clamped = Math.max(-1, Math.min(1, value));
  if (clamped >= 0) {
    const opacity = 0.14 + Math.abs(clamped) * 0.58;
    return `rgba(0, 212, 163, ${opacity})`;
  }
  const opacity = 0.14 + Math.abs(clamped) * 0.58;
  return `rgba(255, 71, 87, ${opacity})`;
}

function influenceTone(value: number) {
  if (value > 0.2) return "text-accent-green";
  if (value < -0.2) return "text-accent-red";
  return "text-text-secondary";
}

function StatTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/18 p-4">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-xl font-semibold text-text-primary", tone)}>{value}</div>
      <div className="mt-1 text-xs leading-5 text-text-muted">{detail}</div>
    </div>
  );
}

function quadrantTone(quadrant: string) {
  if (quadrant === "leading") return "text-accent-green";
  if (quadrant === "improving") return "text-accent-blue";
  if (quadrant === "weakening") return "text-accent-amber";
  return "text-accent-red";
}

function LiveRRGChart({ points }: { points: IndiaLiveOverviewPayload["rrg"] }) {
  const extent = useMemo(() => {
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const maxAbs = Math.max(1, ...xs.map(Math.abs), ...ys.map(Math.abs));
    return maxAbs * 1.18;
  }, [points]);

  const scale = (value: number) => 190 + (value / extent) * 150;
  const topPoints = points.slice(0, 18);

  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
      <svg viewBox="0 0 380 380" role="img" aria-label="India sector RRG" className="h-[380px] w-full">
        <line x1="190" y1="28" x2="190" y2="352" stroke="#1e2d45" strokeWidth="1.2" />
        <line x1="28" y1="190" x2="352" y2="190" stroke="#1e2d45" strokeWidth="1.2" />
        <text x="294" y="42" className="fill-accent-green text-[11px] font-semibold">Leading</text>
        <text x="44" y="42" className="fill-accent-blue text-[11px] font-semibold">Improving</text>
        <text x="286" y="342" className="fill-accent-amber text-[11px] font-semibold">Weakening</text>
        <text x="46" y="342" className="fill-accent-red text-[11px] font-semibold">Lagging</text>
        {topPoints.map((point) => {
          const x = scale(point.x);
          const y = 380 - scale(point.y);
          const radius = Math.max(5, Math.min(14, 6 + Math.abs(point.leadership_score) * 0.45));
          const fill = point.quadrant === "leading" ? "#00d4a3" : point.quadrant === "improving" ? "#3b82f6" : point.quadrant === "weakening" ? "#f59e0b" : "#ff4757";
          return (
            <g key={point.sector_key}>
              <circle cx={x} cy={y} r={radius} fill={fill} fillOpacity="0.78" stroke="#e5edf7" strokeOpacity="0.38" />
              <text x={x + radius + 4} y={y + 4} className="fill-text-secondary text-[10px] font-medium">
                {shortLabel(point.sector)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function LiveIndiaOverview({ overview }: { overview?: IndiaLiveOverviewPayload }) {
  if (!overview) return null;
  const topSectors = overview.sectors.slice(0, 8);
  const laggingSectors = overview.sectors.slice(-5).reverse();

  return (
    <>
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Default Market" value="India" detail={overview.source_mode.replaceAll("_", " ")} tone="text-accent-green" />
        <StatTile label="F&O Universe" value={`${overview.universe.stocks}`} detail={`${overview.universe.symbols} total symbols, ${overview.universe.indices} indices`} />
        <StatTile label="Mapped Stocks" value={`${overview.universe.mapped}`} detail={overview.universe.unmapped.length ? `${overview.universe.unmapped.length} unmapped` : "all stocks mapped"} tone={overview.universe.unmapped.length ? "text-accent-amber" : "text-accent-green"} />
        <StatTile label="Top Sector" value={shortLabel(overview.sectors[0]?.sector || "--")} detail={`score ${formatNumber(overview.sectors[0]?.leadership_score)}`} tone="text-accent-green" />
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.12fr_0.88fr]">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
                <Activity size={17} className="text-accent-green" />
                Live India Sector Leadership
              </div>
              <div className="mt-1 text-xs text-text-muted">F&O and ATM watchlist constituents ranked by live change, option-flow breadth, IV, RSI, volume and momentum proxies.</div>
            </div>
            <div className="text-right text-[11px] text-text-muted">As of {overview.as_of}</div>
          </div>
          <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
            <table className="w-full min-w-[860px] text-sm">
              <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
                <tr>
                  <th className="px-4 py-3">Rank</th>
                  <th className="px-4 py-3">Sector</th>
                  <th className="px-4 py-3">Constituents</th>
                  <th className="px-4 py-3">Score</th>
                  <th className="px-4 py-3">RRG</th>
                  <th className="px-4 py-3">Avg Change</th>
                  <th className="px-4 py-3">Avg IV</th>
                  <th className="px-4 py-3">Leaders</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border/70 text-text-secondary">
                {topSectors.map((sector) => (
                  <tr key={sector.sector_key}>
                    <td className="px-4 py-3 font-mono">{sector.rank}</td>
                    <td className="px-4 py-3">
                      <Link href={`/sector-interaction/${sector.sector_key}`} className="font-medium text-text-primary transition-colors hover:text-accent-blue">
                        {sector.sector}
                      </Link>
                    </td>
                    <td className="px-4 py-3 font-mono">{sector.constituents}</td>
                    <td className="px-4 py-3 font-mono text-accent-green">{formatNumber(sector.leadership_score, 2)}</td>
                    <td className={clsx("px-4 py-3 font-semibold", quadrantTone(sector.rrg_quadrant))}>{sector.rrg_quadrant}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(sector.avg_change_pct, 2)}%</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(sector.avg_iv, 3)}</td>
                    <td className="px-4 py-3 text-xs text-text-muted">{sector.leaders.slice(0, 3).map((item) => item.symbol).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Network size={17} className="text-accent-blue" />
            Relative Rotation Graph
          </div>
          <LiveRRGChart points={overview.rrg} />
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 text-sm font-semibold text-text-primary">Lagging / Weak Sectors</div>
          <div className="space-y-3">
            {laggingSectors.map((sector) => (
              <Link
                key={sector.sector_key}
                href={`/sector-interaction/${sector.sector_key}`}
                className="flex items-center justify-between gap-3 rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 transition-colors hover:border-accent-blue/35"
              >
                <div>
                  <div className="text-sm font-semibold text-text-primary">{sector.sector}</div>
                  <div className="mt-1 text-xs text-text-muted">{sector.constituents} constituents / {sector.rrg_quadrant}</div>
                </div>
                <div className="font-mono text-sm text-accent-red">{formatNumber(sector.leadership_score, 2)}</div>
              </Link>
            ))}
          </div>
        </div>
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 text-sm font-semibold text-text-primary">Real-Data Guardrails</div>
          <div className="grid gap-3 md:grid-cols-2">
            {overview.notes.map((note) => (
              <div key={note} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-xs leading-5 text-text-secondary">
                {note}
              </div>
            ))}
            {overview.universe.unmapped.length ? (
              <div className="rounded-2xl border border-accent-amber/25 bg-accent-amber/10 px-4 py-3 text-xs leading-5 text-accent-amber">
                Unmapped stocks: {overview.universe.unmapped.join(", ")}
              </div>
            ) : null}
          </div>
        </div>
      </section>
    </>
  );
}

function NSEConstituentOverlayPanel({
  status,
  running,
  onSync,
  syncResult,
}: {
  status?: NSEConstituentStatusPayload;
  running: boolean;
  onSync: () => void;
  syncResult?: NSEConstituentSyncPayload;
}) {
  const active = Boolean(status?.runtime_overlay_active);
  const failures = status?.failed_sources?.length || 0;
  return (
    <section className="grid gap-4 xl:grid-cols-[0.86fr_1.14fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Database size={17} className={active ? "text-accent-green" : "text-accent-amber"} />
            Official NSE Constituent Overlay
          </div>
          <button
            type="button"
            onClick={onSync}
            disabled={running}
            className="inline-flex items-center gap-2 rounded-2xl border border-accent-blue/30 bg-accent-blue/10 px-3 py-2 text-xs font-semibold text-accent-blue transition-colors hover:border-accent-blue/50 disabled:opacity-50"
          >
            <RefreshCw size={14} className={running ? "animate-spin" : ""} />
            Sync NSE
          </button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile
            label="Overlay"
            value={active ? "Active" : "Static"}
            detail={status?.source?.replaceAll("_", " ") || "fallback taxonomy"}
            tone={active ? "text-accent-green" : "text-accent-amber"}
          />
          <StatTile label="Official Sectors" value={`${status?.sector_count || 0}`} detail={`${status?.symbol_count || 0} unique symbols`} />
          <StatTile label="Loaded Sources" value={`${status?.successful_sources?.length || 0}`} detail={`${failures} failed/blocked`} tone={failures ? "text-accent-amber" : "text-accent-green"} />
          <StatTile label="Updated" value={status?.updated_at ? status.updated_at.slice(0, 10) : "--"} detail="runtime DB state" />
        </div>
        {syncResult?.message ? (
          <div className="mt-4 rounded-2xl border border-accent-amber/25 bg-accent-amber/10 px-4 py-3 text-xs leading-5 text-accent-amber">
            {syncResult.message}
          </div>
        ) : null}
      </div>

      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">Official Sector Coverage</div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {(status?.sectors || []).slice(0, 12).map((sector) => (
            <div key={sector.sector_key} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3">
              <div className="text-sm font-semibold text-text-primary">{sector.label}</div>
              <div className="mt-1 font-mono text-xs text-accent-blue">{sector.constituents} constituents</div>
            </div>
          ))}
          {!(status?.sectors || []).length ? (
            <div className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-sm leading-6 text-text-muted">
              No official NSE overlay is stored yet. Sync once to let the India live page classify F&O/ATM symbols from Nifty sector CSVs before using static fallbacks.
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function RealSectorModelPanel({ model }: { model?: ModelPayload }) {
  if (!model) return null;
  const edges = model.network?.edges || [];
  const leaders = model.rankings?.leaders || [];
  const contract = model.real_data_contract;
  const insufficient = model.source_mode === "insufficient_real_sector_history";

  return (
    <section className="grid gap-4 xl:grid-cols-[0.92fr_1.08fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Network size={17} className={insufficient ? "text-accent-amber" : "text-accent-green"} />
          Real Sector Interaction VAR
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile label="Mode" value={model.source_mode.replaceAll("_", " ")} detail={model.timeframe || "daily"} tone={insufficient ? "text-accent-amber" : "text-accent-green"} />
          <StatTile label="Periods" value={`${contract?.observed_periods ?? model.periods ?? 0}`} detail={`required ${contract?.minimum_periods ?? 48}`} />
          <StatTile label="Sectors" value={`${contract?.sector_count ?? model.sectors.length}`} detail="real aligned sector histories" />
          <StatTile label="Edges" value={`${edges.length}`} detail={model.selected_lag ? `VAR lag ${model.selected_lag}` : "not estimated"} tone="text-accent-blue" />
        </div>
        <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/16 p-4 text-xs leading-5 text-text-muted">
          {insufficient ? contract?.required_action || model.source_note : model.source_note}
        </div>
      </div>

      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">
          {insufficient ? "Available Real Histories" : "Directed Edges"}
        </div>
        {insufficient ? (
          <div className="grid gap-2 md:grid-cols-2">
            {Object.entries(model.close_counts || {}).slice(0, 12).map(([sector, count]) => (
              <div key={sector} className="flex items-center justify-between rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-sm">
                <span className="text-text-secondary">{sector}</span>
                <span className="font-mono text-text-primary">{count}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
                <tr>
                  <th className="px-4 py-3">Relationship</th>
                  <th className="px-4 py-3">Lag</th>
                  <th className="px-4 py-3">p-value</th>
                  <th className="px-4 py-3">Weight</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border/70 text-text-secondary">
                {edges.slice(0, 10).map((edge) => (
                  <tr key={`${edge.source}-${edge.target}`}>
                    <td className="px-4 py-3 font-medium text-text-primary">{edge.relationship}</td>
                    <td className="px-4 py-3 font-mono">{edge.lag}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(edge.p_value, 4)}</td>
                    <td className="px-4 py-3 font-mono text-accent-blue">{formatNumber(edge.weight, 3)}</td>
                  </tr>
                ))}
                {!edges.length ? (
                  <tr>
                    <td className="px-4 py-6 text-text-muted" colSpan={4}>No significant real-data Granger edges at the selected alpha.</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
        {!insufficient && leaders.length ? (
          <div className="mt-4 text-xs text-text-muted">
            Top real-data leader: <span className="text-text-primary">{leaders[0].sector}</span> net {formatNumber(leaders[0].net_influence)}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function CountryButton({
  active,
  label,
  detail,
  onClick,
}: {
  active: boolean;
  label: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-2xl border px-4 py-3 text-left transition-colors",
        active
          ? "border-accent-blue/45 bg-accent-blue/12 text-text-primary"
          : "border-bg-border bg-bg-primary/18 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-sm font-semibold">{label}</div>
      <div className="mt-1 text-[11px] text-text-muted">{detail}</div>
    </button>
  );
}

function CorrelationHeatmap({ matrix }: { matrix?: MatrixPayload }) {
  if (!matrix) return null;
  return (
    <div className="overflow-auto rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
      <div
        className="grid min-w-[760px] gap-1"
        style={{ gridTemplateColumns: `118px repeat(${matrix.labels.length}, minmax(42px, 1fr))` }}
      >
        <div />
        {matrix.labels.map((label) => (
          <div key={label} className="truncate text-center text-[10px] font-semibold text-text-muted" title={label}>
            {shortLabel(label)}
          </div>
        ))}
        {matrix.labels.map((rowLabel, rowIndex) => (
          <div key={rowLabel} className="contents">
            <div className="truncate py-2 pr-2 text-[11px] font-medium text-text-secondary" title={rowLabel}>
              {shortLabel(rowLabel)}
            </div>
            {matrix.values[rowIndex].map((value, colIndex) => (
              <div
                key={`${rowLabel}-${matrix.labels[colIndex]}`}
                className="flex h-9 items-center justify-center rounded-md border border-bg-border/35 font-mono text-[10px] text-text-primary"
                style={{ backgroundColor: heatColor(value) }}
                title={`${rowLabel} / ${matrix.labels[colIndex]}: ${formatNumber(value, 3)}`}
              >
                {formatNumber(value, 2)}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function NetworkGraph({ model }: { model?: ModelPayload }) {
  const layout = useMemo(() => {
    const nodes = model?.network.nodes || [];
    const centerX = 260;
    const centerY = 190;
    const radius = 138;
    return new Map(
      nodes.map((node, index) => {
        const angle = (Math.PI * 2 * index) / Math.max(nodes.length, 1) - Math.PI / 2;
        return [
          node.id,
          {
            ...node,
            x: centerX + Math.cos(angle) * radius,
            y: centerY + Math.sin(angle) * radius,
          },
        ];
      }),
    );
  }, [model?.network.nodes]);
  const positionedNodes = useMemo(() => Array.from(layout.values()), [layout]);

  if (!model) return null;
  const topEdges = model.network.edges.slice(0, 18);
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
      <svg viewBox="0 0 520 380" role="img" aria-label={`${model.label} sector Granger network`} className="h-[380px] w-full">
        <defs>
          <marker id="edge-arrow" markerHeight="8" markerWidth="8" orient="auto" refX="7" refY="4">
            <path d="M0,0 L8,4 L0,8 Z" fill="#3b82f6" opacity="0.82" />
          </marker>
        </defs>
        {topEdges.map((edge) => {
          const source = layout.get(edge.source);
          const target = layout.get(edge.target);
          if (!source || !target) return null;
          return (
            <line
              key={`${edge.source}-${edge.target}`}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke="#3b82f6"
              strokeOpacity={Math.min(0.85, 0.22 + edge.weight / 4)}
              strokeWidth={Math.max(1.2, Math.min(4, edge.weight))}
              markerEnd="url(#edge-arrow)"
            />
          );
        })}
        {positionedNodes.map((node) => {
          const positive = node.net_influence > 0;
          const nodeRadius = Math.max(17, Math.min(30, 18 + Math.abs(node.net_influence) * 2.6));
          return (
            <g key={node.id}>
              <circle
                cx={node.x}
                cy={node.y}
                r={nodeRadius}
                fill={positive ? "#08382f" : "#351925"}
                stroke={positive ? "#00d4a3" : "#ff4757"}
                strokeOpacity="0.78"
              />
              <text x={node.x} y={node.y + 4} textAnchor="middle" className="fill-text-primary text-[10px] font-semibold">
                {shortLabel(node.label).slice(0, 10)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function RankingTable({ rows }: { rows: NetworkNode[] }) {
  return (
    <div className="overflow-hidden rounded-2xl border border-bg-border bg-bg-primary/12">
      <table className="w-full min-w-[520px] text-sm">
        <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
          <tr>
            <th className="px-4 py-3">Sector</th>
            <th className="px-4 py-3">Out</th>
            <th className="px-4 py-3">In</th>
            <th className="px-4 py-3">Net</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-bg-border/70 text-text-secondary">
          {rows.slice(0, 8).map((row) => (
            <tr key={row.sector}>
              <td className="px-4 py-3 font-medium text-text-primary">{row.sector}</td>
              <td className="px-4 py-3 font-mono">{formatNumber(row.outgoing_weight)}</td>
              <td className="px-4 py-3 font-mono">{formatNumber(row.incoming_weight)}</td>
              <td className={clsx("px-4 py-3 font-mono font-semibold", influenceTone(row.net_influence))}>
                {formatNumber(row.net_influence)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function statusTone(status: string) {
  if (status === "open_data") return "border-accent-green/25 bg-accent-green/10 text-accent-green";
  if (status === "prototype") return "border-accent-blue/25 bg-accent-blue/10 text-accent-blue";
  if (status === "licensed_required") return "border-accent-amber/25 bg-accent-amber/10 text-accent-amber";
  return "border-bg-border bg-bg-secondary/40 text-text-secondary";
}

function stanceTone(stance: string) {
  if (stance === "overweight") return "text-accent-green";
  if (stance === "underweight") return "text-accent-red";
  return "text-text-secondary";
}

function SignalsPanel({ signals }: { signals?: SignalsPayload }) {
  if (!signals) return null;
  return (
    <section className="grid gap-4 xl:grid-cols-[0.95fr_1.05fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Signal size={17} className="text-accent-green" />
              Sector Signal Ranking
            </div>
            <div className="mt-1 text-xs text-text-muted">Composite alternative-data scores as of {signals.as_of}.</div>
            <div className="mt-1 text-xs text-text-muted">
              Runtime handoff: {signals.runtime_handoff.observed_dates}/{signals.runtime_handoff.required_dates} dates,
              {" "}{signals.runtime_handoff.active ? "active" : "fallback"}
            </div>
          </div>
        </div>
        <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
          <table className="w-full min-w-[680px] text-sm">
            <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <tr>
                <th className="px-4 py-3">Rank</th>
                <th className="px-4 py-3">Sector</th>
                <th className="px-4 py-3">Score</th>
                <th className="px-4 py-3">Stance</th>
                <th className="px-4 py-3">Top Driver</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/70 text-text-secondary">
              {signals.rankings.slice(0, 10).map((row) => (
                <tr key={row.sector}>
                  <td className="px-4 py-3 font-mono">{row.rank}</td>
                  <td className="px-4 py-3 font-medium text-text-primary">{row.sector}</td>
                  <td className="px-4 py-3 font-mono">{formatNumber(row.score, 3)}</td>
                  <td className={clsx("px-4 py-3 font-semibold", stanceTone(row.stance))}>{row.stance}</td>
                  <td className="px-4 py-3 text-xs text-text-muted">{row.top_drivers[0]?.indicator || "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Gauge size={17} className="text-accent-cyan" />
          Indicator State
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {signals.indicator_latest.slice(0, 8).map((indicator) => (
            <div key={indicator.code} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-text-primary">{indicator.label}</div>
                  <div className="mt-1 text-[11px] text-text-muted">{indicator.category} / {indicator.cadence}</div>
                </div>
                <div className={clsx("rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]", statusTone(indicator.source_status))}>
                  {indicator.source_status.replaceAll("_", " ")}
                </div>
              </div>
              <div className="mt-3 flex items-center justify-between text-sm">
                <span className="text-text-muted">latest z</span>
                <span className={clsx("font-mono font-semibold", indicator.latest_z >= 0 ? "text-accent-green" : "text-accent-red")}>
                  {formatNumber(indicator.latest_z, 3)}
                </span>
              </div>
              <div className="mt-2 text-xs leading-5 text-text-secondary">
                {Object.keys(indicator.mapped_sectors).slice(0, 3).join(", ")}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function IndicatorNetworkPanel({ network }: { network?: ExtendedNetworkPayload }) {
  if (!network) return null;
  const edgeRows = network.indicator_edges.slice(0, 12);
  return (
    <section className="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Network size={17} className="text-accent-purple" />
          Indicator Network
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile label="Indicators" value={`${network.summary.indicator_count}`} detail="alternative-data nodes" tone="text-accent-purple" />
          <StatTile label="Indicator Edges" value={`${network.summary.indicator_edge_count}`} detail="validated lead-lag links" tone="text-accent-blue" />
          <StatTile label="Sectors" value={`${network.summary.sector_count}`} detail="return nodes" />
          <StatTile label="Lag" value={`${network.selected_lag}`} detail={`alpha ${network.alpha}`} />
        </div>
        <p className="mt-4 text-xs leading-5 text-text-muted">
          Indicator edges test whether source metrics improve sector-return forecasts beyond the sector&apos;s own lag.
        </p>
      </div>
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">Alternative Indicator Edges</div>
        <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
          <table className="w-full min-w-[760px] text-sm">
            <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <tr>
                <th className="px-4 py-3">Relationship</th>
                <th className="px-4 py-3">Category</th>
                <th className="px-4 py-3">Exposure</th>
                <th className="px-4 py-3">p-value</th>
                <th className="px-4 py-3">Weight</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/70 text-text-secondary">
              {edgeRows.map((edge) => (
                <tr key={`${edge.source}-${edge.target}`}>
                  <td className="px-4 py-3 font-medium text-text-primary">{edge.relationship}</td>
                  <td className="px-4 py-3 text-xs">{edge.category}</td>
                  <td className="px-4 py-3 font-mono">{formatNumber(edge.configured_exposure, 2)}</td>
                  <td className="px-4 py-3 font-mono">{formatNumber(edge.p_value, 4)}</td>
                  <td className="px-4 py-3 font-mono text-accent-blue">{formatNumber(edge.weight, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function ValidationPanel({ backtest }: { backtest?: BacktestPayload }) {
  if (!backtest) return null;
  const validationDetail = backtest.source_mode.includes("pending")
    ? "validation pending"
    : backtest.source_mode.includes("synthetic")
      ? "synthetic long-short"
      : "real/live long-short";
  return (
    <section className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Activity size={17} className="text-accent-green" />
          Validation Backtest
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile label="Cumulative" value={`${formatNumber(backtest.summary.cumulative_return_pct, 1)}%`} detail={validationDetail} tone="text-accent-green" />
          <StatTile label="Hit Rate" value={`${formatNumber(backtest.summary.hit_rate_pct, 1)}%`} detail={`${backtest.summary.observations} windows`} />
          <StatTile label="Info Ratio" value={formatNumber(backtest.summary.information_ratio, 2)} detail="monthly annualized" tone="text-accent-blue" />
          <StatTile label="Max DD" value={`${formatNumber(backtest.summary.max_drawdown_pct, 1)}%`} detail="peak-to-trough" tone="text-accent-amber" />
        </div>
        <p className="mt-4 text-xs leading-5 text-text-muted">{backtest.method}</p>
      </div>
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">Equity Curve</div>
        <div className="h-72 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={backtest.equity_curve} margin={{ top: 8, right: 12, left: 0, bottom: 8 }}>
              <CartesianGrid stroke="#1e2d45" strokeDasharray="3 3" />
              <XAxis dataKey="date" stroke="#4a5568" tick={{ fill: "#94a3b8", fontSize: 10 }} minTickGap={24} />
              <YAxis stroke="#4a5568" tick={{ fill: "#94a3b8", fontSize: 11 }} width={44} />
              <Tooltip contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 12 }} />
              <Line type="monotone" dataKey="value" stroke="#00d4a3" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  );
}

function PipelineStatusPanel({ pipeline }: { pipeline?: PipelineStatusPayload }) {
  if (!pipeline) return null;
  const topConnectors = pipeline.connectors
    .slice()
    .sort((left, right) => right.readiness_score - left.readiness_score)
    .slice(0, 8);
  return (
    <section className="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Database size={17} className="text-accent-cyan" />
          Production Pipeline Readiness
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile label="Readiness" value={formatNumber(pipeline.summary.readiness_score, 2)} detail={`${pipeline.summary.connector_count} connectors`} tone="text-accent-cyan" />
          <StatTile label="Open Data" value={`${pipeline.summary.open_data_count}`} detail="can move first" tone="text-accent-green" />
          <StatTile label="Blocked" value={`${pipeline.summary.critical_blockers}`} detail="license/TOS/source work" tone="text-accent-amber" />
          <StatTile label="Prototype" value={`${pipeline.summary.prototype_count}`} detail="needs approved feed" />
        </div>
        <div className="mt-4 space-y-2">
          {pipeline.execution_controls.slice(0, 3).map((control) => (
            <div key={control} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-xs leading-5 text-text-secondary">
              {control}
            </div>
          ))}
        </div>
      </div>
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">Connector Queue</div>
        <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
          <table className="w-full min-w-[820px] text-sm">
            <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <tr>
                <th className="px-4 py-3">Connector</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Ready</th>
                <th className="px-4 py-3">Freshness</th>
                <th className="px-4 py-3">Next Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/70 text-text-secondary">
              {topConnectors.map((connector) => (
                <tr key={connector.indicator_code}>
                  <td className="px-4 py-3">
                    <div className="font-medium text-text-primary">{connector.label}</div>
                    <div className="mt-1 text-[11px] text-text-muted">{connector.production_source}</div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={clsx("rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]", statusTone(connector.source_status))}>
                      {connector.run_status}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono">{formatNumber(connector.readiness_score, 2)}</td>
                  <td className="px-4 py-3 text-xs">{connector.freshness_lag_hours}h / {connector.freshness_sla_hours}h</td>
                  <td className="px-4 py-3 text-xs leading-5 text-text-muted">{connector.next_action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function SectorReportPanel({ report }: { report?: SectorReportPayload }) {
  if (!report) return null;
  return (
    <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <FileText size={17} className="text-accent-blue" />
          Sector Research Brief
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
          <div className="text-base font-semibold leading-7 text-text-primary">{report.headline}</div>
          <div className="mt-1 text-xs text-text-muted">As of {report.as_of} / {report.source_mode.replaceAll("_", " ")}</div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {report.summary_bullets.map((bullet) => (
            <div key={bullet} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-sm leading-6 text-text-secondary">
              {bullet}
            </div>
          ))}
        </div>
      </div>
      <div className="space-y-4">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-3 text-sm font-semibold text-text-primary">Risk Flags</div>
          <div className="space-y-2">
            {report.risk_flags.map((flag) => (
              <div key={flag} className="rounded-2xl border border-accent-amber/20 bg-accent-amber/10 px-4 py-3 text-xs leading-5 text-text-secondary">
                {flag}
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-3 text-sm font-semibold text-text-primary">Next Actions</div>
          <div className="space-y-2">
            {report.next_actions.map((action) => (
              <div key={action} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-xs leading-5 text-text-secondary">
                {action}
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function IngestionRuntimePanel({
  ingestion,
  running,
  onDryRun,
  onAppend,
  onAppendIndiaLive,
  runResult,
}: {
  ingestion?: IngestionStatusPayload;
  running: boolean;
  onDryRun: () => void;
  onAppend: () => void;
  onAppendIndiaLive?: () => void;
  runResult?: IngestionRunPayload;
}) {
  if (!ingestion) return null;
  const activeConnectors = ingestion.connectors.filter((connector) => connector.has_runtime_data).length;
  return (
    <section className="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Database size={17} className="text-accent-green" />
            Runtime Ingestion Store
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onDryRun}
              disabled={running}
              className="rounded-2xl border border-bg-border bg-bg-primary/20 px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-accent-blue/35 hover:text-text-primary disabled:opacity-50"
            >
              Dry Run
            </button>
            <button
              type="button"
              onClick={onAppend}
              disabled={running}
              className="rounded-2xl border border-accent-green/30 bg-accent-green/10 px-3 py-2 text-xs font-semibold text-accent-green transition-colors hover:border-accent-green/50 disabled:opacity-50"
            >
              Append Open Data
            </button>
            {onAppendIndiaLive ? (
              <button
                type="button"
                onClick={onAppendIndiaLive}
                disabled={running}
                className="rounded-2xl border border-accent-blue/30 bg-accent-blue/10 px-3 py-2 text-xs font-semibold text-accent-blue transition-colors hover:border-accent-blue/50 disabled:opacity-50"
              >
                Append India Live
              </button>
            ) : null}
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <StatTile label="Observations" value={`${ingestion.runtime_summary.observation_count}`} detail={`${activeConnectors} connectors active`} tone="text-accent-green" />
          <StatTile label="Indicators" value={`${ingestion.runtime_summary.indicator_count}`} detail={`${ingestion.runtime_summary.sector_count} mapped sectors`} />
          <StatTile label="Runs" value={`${ingestion.runtime_summary.run_count}`} detail={ingestion.runtime_summary.latest_observation_date || "no runtime date"} />
          <StatTile
            label="Storage"
            value={ingestion.storage_status.durable_enabled ? "Durable" : "Local"}
            detail={ingestion.storage_status.backend.replaceAll("_", " ")}
            tone={ingestion.storage_status.durable_enabled ? "text-accent-green" : "text-accent-amber"}
          />
        </div>
        {runResult ? (
          <div className="mt-4 rounded-2xl border border-accent-blue/25 bg-accent-blue/10 px-4 py-3 text-sm text-accent-blue">
            {runResult.message} Generated {runResult.generated_observations}; blocked {runResult.blocked_connectors.length}.
          </div>
        ) : null}
        <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/16 p-4 text-xs leading-5 text-text-muted">
          Runtime root: <span className="text-text-secondary">{ingestion.runtime_root}</span>
          <br />
          Durable mirror: <span className="text-text-secondary">
            {ingestion.storage_status.durable_updated_at || "not enabled"}
          </span>
        </div>
      </div>
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 text-sm font-semibold text-text-primary">Recent Observations</div>
        <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <tr>
                <th className="px-4 py-3">Date</th>
                <th className="px-4 py-3">Indicator</th>
                <th className="px-4 py-3">Sector</th>
                <th className="px-4 py-3">Value</th>
                <th className="px-4 py-3">Quality</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/70 text-text-secondary">
              {ingestion.recent_observations.length ? ingestion.recent_observations.slice(0, 10).map((row) => (
                <tr key={`${row.indicator_code}-${row.sector}-${row.date}`}>
                  <td className="px-4 py-3 font-mono text-xs">{row.date}</td>
                  <td className="px-4 py-3 font-medium text-text-primary">{row.indicator_code}</td>
                  <td className="px-4 py-3">{row.sector}</td>
                  <td className="px-4 py-3 font-mono">{formatNumber(row.value, 4)}</td>
                  <td className="px-4 py-3 font-mono">{formatNumber(row.quality_score, 2)}</td>
                </tr>
              )) : (
                <tr>
                  <td className="px-4 py-6 text-sm text-text-muted" colSpan={5}>
                    No runtime observations stored yet. Use dry run first, then append approved open-data observations.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function SourceMapPanel({ sourceMap }: { sourceMap?: SourceMapPayload }) {
  if (!sourceMap) return null;
  return (
    <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <Database size={17} className="text-accent-cyan" />
        Source Map and Data Contract
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {sourceMap.indicators.map((indicator) => (
          <div key={indicator.code} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="text-sm font-semibold text-text-primary">{indicator.label}</div>
                <div className="mt-1 text-[11px] text-text-muted">{indicator.production_source}</div>
              </div>
              <div className={clsx("rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]", statusTone(indicator.source_status))}>
                {indicator.source_status.replaceAll("_", " ")}
              </div>
            </div>
            <div className="mt-3 text-xs leading-5 text-text-secondary">{indicator.metric_definition}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/16 p-4 text-xs leading-6 text-text-muted">
        Required columns: <span className="text-text-secondary">{sourceMap.data_contract.required_columns.join(", ")}</span>
      </div>
    </section>
  );
}

function AcquisitionPlan({ plan }: { plan?: PlanPayload }) {
  if (!plan) return null;
  return (
    <section className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
      <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Database size={17} className="text-accent-cyan" />
          Alternative Data Pipeline
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {plan.source_categories.map((category) => (
            <div key={category.name} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
              <div className="text-sm font-semibold text-text-primary">{category.name}</div>
              <div className="mt-2 text-xs leading-5 text-text-muted">{category.examples.join(" / ")}</div>
              <div className="mt-3 text-[11px] uppercase tracking-[0.14em] text-text-muted">Metrics</div>
              <div className="mt-1 text-xs leading-5 text-text-secondary">{category.metrics.join(", ")}</div>
            </div>
          ))}
        </div>
      </div>
      <div className="space-y-4">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
            <ShieldCheck size={17} className="text-accent-green" />
            Controls
          </div>
          <div className="space-y-3">
            {plan.legal_controls.map((control) => (
              <div key={control} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 text-sm leading-6 text-text-secondary">
                {control}
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-4 text-sm font-semibold text-text-primary">Roadmap</div>
          <div className="space-y-3">
            {plan.roadmap.map((item) => (
              <div key={item.phase} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
                <div className="font-mono text-xs font-semibold text-accent-blue">{item.phase}</div>
                <div className="mt-2 text-sm leading-6 text-text-secondary">{item.work}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

export default function SectorInteractionWorkspace() {
  const [country, setCountry] = useState<CountryCode>("IN");
  const [periods, setPeriods] = useState(160);

  const overviewQuery = useQuery({
    queryKey: ["sector-interaction", "overview"],
    queryFn: async () => (await getSectorInteractionOverview()).data as OverviewPayload,
    staleTime: 5 * 60_000,
  });
  const indiaLiveQuery = useQuery({
    queryKey: ["sector-interaction", "india-live-overview"],
    queryFn: async () => (await getSectorInteractionIndiaOverview()).data as IndiaLiveOverviewPayload,
    staleTime: 60_000,
    enabled: country === "IN",
  });
  const nseConstituentQuery = useQuery({
    queryKey: ["sector-interaction", "nse-constituents-status"],
    queryFn: async () => (await getSectorInteractionNSEConstituentStatus()).data as NSEConstituentStatusPayload,
    staleTime: 60_000,
    enabled: country === "IN",
  });
  const indiaRealModelQuery = useQuery({
    queryKey: ["sector-interaction", "india-real-model", periods],
    queryFn: async () => (await getSectorInteractionIndiaRealModel(periods, 2, 0.05, "daily")).data as ModelPayload,
    staleTime: 5 * 60_000,
    enabled: country === "IN",
  });
  const modelQuery = useQuery({
    queryKey: ["sector-interaction", "model", country, periods],
    queryFn: async () => (await getSectorInteractionModel(country, periods, 2, 0.05)).data as ModelPayload,
    staleTime: 60_000,
    enabled: country !== "IN",
  });
  const signalsQuery = useQuery({
    queryKey: ["sector-interaction", "signals", country, periods],
    queryFn: async () => (await getSectorInteractionSignals(country, periods)).data as SignalsPayload,
    staleTime: 60_000,
    enabled: country !== "IN",
  });
  const extendedNetworkQuery = useQuery({
    queryKey: ["sector-interaction", "extended-network", country, periods],
    queryFn: async () => (await getSectorInteractionExtendedNetwork(country, periods, 2, 0.05)).data as ExtendedNetworkPayload,
    staleTime: 60_000,
    enabled: country !== "IN",
  });
  const sourceMapQuery = useQuery({
    queryKey: ["sector-interaction", "source-map", country],
    queryFn: async () => (await getSectorInteractionSourceMap(country)).data as SourceMapPayload,
    staleTime: 5 * 60_000,
  });
  const backtestQuery = useQuery({
    queryKey: ["sector-interaction", "validation-backtest", country, periods],
    queryFn: async () => (await getSectorInteractionValidationBacktest(country, periods)).data as BacktestPayload,
    staleTime: 60_000,
    enabled: country !== "IN",
  });
  const pipelineQuery = useQuery({
    queryKey: ["sector-interaction", "pipeline-status", country],
    queryFn: async () => (await getSectorInteractionPipelineStatus(country)).data as PipelineStatusPayload,
    staleTime: 60_000,
  });
  const ingestionQuery = useQuery({
    queryKey: ["sector-interaction", "ingestion-status", country],
    queryFn: async () => (await getSectorInteractionIngestionStatus(country)).data as IngestionStatusPayload,
    staleTime: 60_000,
  });
  const reportQuery = useQuery({
    queryKey: ["sector-interaction", "report", country, periods],
    queryFn: async () => (await getSectorInteractionReport(country, periods)).data as SectorReportPayload,
    staleTime: 60_000,
    enabled: country !== "IN",
  });
  const planQuery = useQuery({
    queryKey: ["sector-interaction", "plan"],
    queryFn: async () => (await getSectorInteractionAcquisitionPlan()).data as PlanPayload,
    staleTime: 5 * 60_000,
  });
  const seedMutation = useMutation({
    mutationFn: async () => (await seedSectorInteractionRAG()).data as { stored: number; document_ids: string[]; already_present: string[] },
  });
  const ingestionMutation = useMutation({
    mutationFn: async ({ dryRun }: { dryRun: boolean }) =>
      (await runSectorInteractionIngestion(country, dryRun, false)).data as IngestionRunPayload,
    onSuccess: () => {
      ingestionQuery.refetch();
      pipelineQuery.refetch();
    },
  });
  const indiaLiveIngestionMutation = useMutation({
    mutationFn: async ({ dryRun }: { dryRun: boolean }) =>
      (await runSectorInteractionIndiaLiveMarketIngestion(dryRun)).data as IngestionRunPayload,
    onSuccess: () => {
      ingestionQuery.refetch();
      pipelineQuery.refetch();
      signalsQuery.refetch();
      extendedNetworkQuery.refetch();
    },
  });
  const nseConstituentMutation = useMutation({
    mutationFn: async () => (await syncSectorInteractionNSEConstituents(8)).data as NSEConstituentSyncPayload,
    onSuccess: () => {
      nseConstituentQuery.refetch();
      indiaLiveQuery.refetch();
    },
  });

  const overview = overviewQuery.data;
  const model = modelQuery.data;
  const signals = signalsQuery.data;
  const extendedNetwork = extendedNetworkQuery.data;
  const sourceMap = sourceMapQuery.data;
  const backtest = backtestQuery.data;
  const pipeline = pipelineQuery.data;
  const ingestion = ingestionQuery.data;
  const report = reportQuery.data;
  const plan = planQuery.data;
  const nseConstituentStatus = nseConstituentQuery.data || indiaLiveQuery.data?.nse_constituent_status;
  const countryRows = overview?.countries || [];
  const leaderRows = model?.rankings.leaders || [];
  const edgeChartData = useMemo(
    () =>
      (model?.network.edges || []).slice(0, 10).map((edge) => ({
        name: `${shortLabel(edge.source)} -> ${shortLabel(edge.target)}`,
        weight: edge.weight,
      })),
    [model?.network.edges],
  );
  const isIndiaLive = country === "IN";
  const error = overviewQuery.error || indiaLiveQuery.error || nseConstituentQuery.error || indiaRealModelQuery.error || modelQuery.error || signalsQuery.error || extendedNetworkQuery.error || sourceMapQuery.error || backtestQuery.error || pipelineQuery.error || ingestionQuery.error || reportQuery.error || planQuery.error;

  return (
    <div className="mx-auto max-w-[1600px] space-y-4">
      <section className="rounded-[28px] border border-bg-border bg-bg-secondary/22 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="flex items-center gap-3">
              <div className="rounded-2xl border border-accent-cyan/25 bg-accent-cyan/10 p-2 text-accent-cyan">
                <Network size={22} />
              </div>
              <div>
                <h1 className="text-2xl font-semibold text-text-primary">Sector Interaction Model</h1>
                <p className="mt-1 max-w-3xl text-sm leading-6 text-text-muted">
                  India-first live sector leadership, F&O/ATM watchlist taxonomy, RRG positioning, and real-data pipeline controls.
                </p>
              </div>
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              {countryRows.map((item) => (
                <CountryButton
                  key={item.code}
                  active={country === item.code}
                  label={item.label}
                  detail={`${item.sector_count} sectors`}
                  onClick={() => setCountry(item.code)}
                />
              ))}
              {!countryRows.length ? (
                <>
                  <CountryButton active={country === "US"} label="United States" detail="11 sectors" onClick={() => setCountry("US")} />
                  <CountryButton active={country === "IN"} label="India" detail="9 sectors" onClick={() => setCountry("IN")} />
                </>
              ) : null}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="rounded-2xl border border-bg-border bg-bg-primary/20 px-3 py-2 text-sm text-text-secondary">
              <span className="mr-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">Periods</span>
              <select
                value={periods}
                onChange={(event) => setPeriods(Number(event.target.value))}
                className="bg-transparent font-mono text-text-primary outline-none"
              >
                {[96, 120, 160, 240].map((value) => (
                  <option key={value} value={value} className="bg-bg-card text-text-primary">
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => {
                indiaLiveQuery.refetch();
                nseConstituentQuery.refetch();
                indiaRealModelQuery.refetch();
                modelQuery.refetch();
                signalsQuery.refetch();
                extendedNetworkQuery.refetch();
                backtestQuery.refetch();
                pipelineQuery.refetch();
                ingestionQuery.refetch();
                reportQuery.refetch();
              }}
              className="inline-flex items-center gap-2 rounded-2xl border border-bg-border bg-bg-primary/20 px-4 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-accent-blue/35 hover:text-text-primary"
            >
              <RefreshCw size={15} className={indiaLiveQuery.isFetching || nseConstituentQuery.isFetching || indiaRealModelQuery.isFetching || modelQuery.isFetching || signalsQuery.isFetching || backtestQuery.isFetching ? "animate-spin" : ""} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => seedMutation.mutate()}
              className="inline-flex items-center gap-2 rounded-2xl border border-accent-blue/30 bg-accent-blue/10 px-4 py-2 text-sm font-semibold text-accent-blue transition-colors hover:border-accent-blue/50"
            >
              <GitBranch size={15} />
              Seed RAG
            </button>
          </div>
        </div>
        {seedMutation.data ? (
          <div className="mt-4 rounded-2xl border border-accent-green/25 bg-accent-green/10 px-4 py-3 text-sm text-accent-green">
            RAG seed complete. Stored {seedMutation.data.stored}; already present {seedMutation.data.already_present.length}.
          </div>
        ) : null}
      </section>

      {error ? (
        <div className="rounded-2xl border border-accent-red/35 bg-accent-red/10 p-4 text-sm text-accent-red">
          {describeApiError(error)}
        </div>
      ) : null}

      {isIndiaLive && indiaLiveQuery.isLoading ? (
        <div className="flex min-h-[420px] items-center justify-center rounded-[26px] border border-bg-border bg-bg-secondary/24 text-text-secondary">
          <Loader2 className="mr-3 animate-spin" size={18} />
          Loading live India sector overview...
        </div>
      ) : isIndiaLive ? (
        <>
          <LiveIndiaOverview overview={indiaLiveQuery.data} />

          <NSEConstituentOverlayPanel
            status={nseConstituentStatus}
            running={nseConstituentMutation.isPending}
            onSync={() => nseConstituentMutation.mutate()}
            syncResult={nseConstituentMutation.data}
          />

          <RealSectorModelPanel model={indiaRealModelQuery.data} />

          <PipelineStatusPanel pipeline={pipeline} />

          <IngestionRuntimePanel
            ingestion={ingestion}
            running={ingestionMutation.isPending || indiaLiveIngestionMutation.isPending}
            onDryRun={() => ingestionMutation.mutate({ dryRun: true })}
            onAppend={() => ingestionMutation.mutate({ dryRun: false })}
            onAppendIndiaLive={isIndiaLive ? () => indiaLiveIngestionMutation.mutate({ dryRun: false }) : undefined}
            runResult={indiaLiveIngestionMutation.data || ingestionMutation.data}
          />
        </>
      ) : modelQuery.isLoading ? (
        <div className="flex min-h-[420px] items-center justify-center rounded-[26px] border border-bg-border bg-bg-secondary/24 text-text-secondary">
          <Loader2 className="mr-3 animate-spin" size={18} />
          Loading sector network...
        </div>
      ) : (
        <>
          <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatTile label="Country" value={model?.label || "--"} detail={model?.source_mode || "synthetic"} />
            <StatTile label="Sectors" value={`${model?.sectors.length || 0}`} detail={`VAR lag ${model?.selected_lag || "--"}`} />
            <StatTile label="Edges" value={`${model?.network.edges.length || 0}`} detail={`alpha ${model?.alpha || "--"}`} tone="text-accent-blue" />
            <StatTile
              label="Top Leader"
              value={signals?.rankings[0]?.sector ? shortLabel(signals.rankings[0].sector) : leaderRows[0]?.sector ? shortLabel(leaderRows[0].sector) : "--"}
              detail={signals?.rankings[0] ? `${signals.rankings[0].stance} ${formatNumber(signals.rankings[0].score)}` : `net ${formatNumber(leaderRows[0]?.net_influence)}`}
              tone="text-accent-green"
            />
          </section>

          <SignalsPanel signals={signals} />

          <IndicatorNetworkPanel network={extendedNetwork} />

          <ValidationPanel backtest={backtest} />

          <PipelineStatusPanel pipeline={pipeline} />

          <IngestionRuntimePanel
            ingestion={ingestion}
            running={ingestionMutation.isPending}
            onDryRun={() => ingestionMutation.mutate({ dryRun: true })}
            onAppend={() => ingestionMutation.mutate({ dryRun: false })}
            runResult={ingestionMutation.data}
          />

          <SectorReportPanel report={report} />

          <section className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
            <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
                    <Activity size={17} className="text-accent-green" />
                    Correlation Heatmap
                  </div>
                  <div className="mt-1 text-xs text-text-muted">Contemporaneous comovement among sector return series.</div>
                </div>
              </div>
              <CorrelationHeatmap matrix={model?.correlation_matrix} />
            </div>

            <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
              <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
                <Network size={17} className="text-accent-blue" />
                Granger Network
              </div>
              <NetworkGraph model={model} />
            </div>
          </section>

          <section className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
            <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
              <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
                <ArrowUpRight size={17} className="text-accent-green" />
                Leader/Follower Ranking
              </div>
              <div className="overflow-x-auto">
                <RankingTable rows={leaderRows} />
              </div>
            </div>
            <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
              <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
                <ArrowDownRight size={17} className="text-accent-amber" />
                Strongest Directed Edges
              </div>
              <div className="h-80 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={edgeChartData} layout="vertical" margin={{ top: 8, right: 16, left: 90, bottom: 8 }}>
                    <CartesianGrid stroke="#1e2d45" strokeDasharray="3 3" horizontal={false} />
                    <XAxis type="number" stroke="#4a5568" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                    <YAxis dataKey="name" type="category" width={90} stroke="#4a5568" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                    <Tooltip
                      cursor={{ fill: "rgba(59,130,246,0.08)" }}
                      contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 12 }}
                    />
                    <Bar dataKey="weight" fill="#3b82f6" radius={[0, 6, 6, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </section>

          <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
            <div className="mb-4 text-sm font-semibold text-text-primary">Edge Table</div>
            <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
              <table className="w-full min-w-[760px] text-sm">
                <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
                  <tr>
                    <th className="px-4 py-3">Relationship</th>
                    <th className="px-4 py-3">Lag</th>
                    <th className="px-4 py-3">p-value</th>
                    <th className="px-4 py-3">Weight</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bg-border/70 text-text-secondary">
                  {(model?.network.edges || []).slice(0, 14).map((edge) => (
                    <tr key={`${edge.source}-${edge.target}`}>
                      <td className="px-4 py-3 font-medium text-text-primary">{edge.relationship}</td>
                      <td className="px-4 py-3 font-mono">{edge.lag}</td>
                      <td className="px-4 py-3 font-mono">{formatNumber(edge.p_value, 4)}</td>
                      <td className="px-4 py-3 font-mono text-accent-blue">{formatNumber(edge.weight, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs leading-5 text-text-muted">{model?.source_note}</p>
          </section>
        </>
      )}

      <SourceMapPanel sourceMap={sourceMap} />

      <AcquisitionPlan plan={plan} />
    </div>
  );
}
