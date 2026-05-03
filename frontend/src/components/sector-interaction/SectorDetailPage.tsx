"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BarChart3, Gauge } from "lucide-react";

import { describeApiError, getSectorInteractionIndiaSector } from "@/lib/api";

type LiveConstituent = {
  symbol: string;
  kind: string;
  latest_time?: string | null;
  underlying_price: number;
  change_pct: number;
  oi_change_pct: number;
  volume: number;
  iv: number;
  rsi: number;
  leadership_score: number;
};

type SectorDetailPayload = {
  sector_key: string;
  sector: string;
  source_mode: string;
  rank: number;
  sector_count: number;
  summary: {
    sector_key: string;
    sector: string;
    constituents: number;
    leadership_score: number;
    relative_strength: number;
    momentum: number;
    avg_change_pct: number;
    avg_oi_change_pct: number;
    avg_iv: number;
    rrg_quadrant: string;
  };
  constituents: LiveConstituent[];
  parameters: Array<{
    code: string;
    label: string;
    value: number;
    unit: string;
    state: string;
  }>;
  performance_cycle: {
    method: string;
    current_phase: string;
    current_phase_index: number;
    next_phase_to_watch: string;
    cycle_score: number;
    relative_strength: number;
    momentum: number;
    interpretation: string;
    phases: Array<{ phase: string; label: string; description: string }>;
  };
  alt_data: Array<{
    name: string;
    status: string;
    value?: number | null;
    unit?: string;
    state?: string;
    detail?: string;
  }>;
  relative_position: Array<{
    sector_key: string;
    sector: string;
    rank: number;
    leadership_score: number;
    quadrant: string;
  }>;
};

type SectorTab = "cycle" | "constituents" | "parameters" | "alt-data" | "peers";

const SECTOR_TABS: Array<{ id: SectorTab; label: string }> = [
  { id: "cycle", label: "Performance Cycle" },
  { id: "constituents", label: "Leaders & Laggards" },
  { id: "parameters", label: "Parameters" },
  { id: "alt-data", label: "Alt Data" },
  { id: "peers", label: "Relative Position" },
];

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

export default function SectorDetailPage({ sectorKey }: { sectorKey: string }) {
  const [activeTab, setActiveTab] = useState<SectorTab>("cycle");
  const detailQuery = useQuery({
    queryKey: ["sector-interaction", "sector-detail", sectorKey],
    queryFn: async () => (await getSectorInteractionIndiaSector(sectorKey)).data as SectorDetailPayload,
    staleTime: 60_000,
  });

  const detail = detailQuery.data;
  const summary = detail?.summary;
  const topPeers = detail?.relative_position.slice(0, 8) || [];
  const leaders = (detail?.constituents || []).slice(0, 8);
  const laggards = (detail?.constituents || []).slice().reverse().slice(0, 8);

  return (
    <div className="mx-auto max-w-[1500px] space-y-4">
      <section className="rounded-[28px] border border-bg-border bg-bg-secondary/22 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <Link href="/sector-interaction" className="inline-flex items-center gap-2 text-sm font-semibold text-text-secondary transition-colors hover:text-accent-blue">
              <ArrowLeft size={16} />
              Overview
            </Link>
            <h1 className="mt-4 text-2xl font-semibold text-text-primary">{detail?.sector || "Sector"}</h1>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-text-muted">
              Constituents, leadership ranking, sector-specific alternative-data hooks, and position versus other India sectors.
            </p>
          </div>
          <div className="rounded-2xl border border-accent-green/25 bg-accent-green/10 px-4 py-3 text-sm text-accent-green">
            {detail?.source_mode?.replaceAll("_", " ") || "live F&O watchlist"}
          </div>
        </div>
      </section>

      {detailQuery.error ? (
        <div className="rounded-2xl border border-accent-red/35 bg-accent-red/10 p-4 text-sm text-accent-red">
          {describeApiError(detailQuery.error)}
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Rank</div>
          <div className="mt-2 font-mono text-xl font-semibold text-accent-green">{detail?.rank || "--"} / {detail?.sector_count || "--"}</div>
          <div className="mt-1 text-xs text-text-muted">sector leadership position</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Constituents</div>
          <div className="mt-2 font-mono text-xl font-semibold text-text-primary">{summary?.constituents || "--"}</div>
          <div className="mt-1 text-xs text-text-muted">mapped F&O stocks</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Leadership</div>
          <div className="mt-2 font-mono text-xl font-semibold text-accent-blue">{formatNumber(summary?.leadership_score)}</div>
          <div className="mt-1 text-xs text-text-muted">composite sector score</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">RRG</div>
          <div className="mt-2 text-xl font-semibold text-text-primary">{summary?.rrg_quadrant || "--"}</div>
          <div className="mt-1 text-xs text-text-muted">relative strength and momentum</div>
        </div>
      </section>

      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-5 flex gap-2 overflow-x-auto border-b border-bg-border">
          {SECTOR_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={`shrink-0 border-b-2 px-3 py-2 text-xs font-semibold transition-colors ${
                activeTab === tab.id
                  ? "border-accent-blue text-accent-blue"
                  : "border-transparent text-text-muted hover:text-text-primary"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {activeTab === "cycle" ? (
          <div className="grid gap-4 xl:grid-cols-[0.95fr_1.05fr]">
            <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-5">
              <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
                <Gauge size={17} className="text-accent-amber" />
                Sector Performance Cycle
              </div>
              <div className="rounded-2xl border border-accent-blue/25 bg-accent-blue/10 p-4">
                <div className="text-xs uppercase tracking-[0.14em] text-text-muted">Current phase</div>
                <div className="mt-2 text-2xl font-semibold capitalize text-text-primary">{detail?.performance_cycle?.current_phase || "--"}</div>
                <div className="mt-2 text-sm leading-6 text-text-secondary">{detail?.performance_cycle?.interpretation}</div>
              </div>
              <div className="mt-4 grid gap-3 sm:grid-cols-3">
                <div className="rounded-2xl border border-bg-border bg-bg-primary/20 p-4">
                  <div className="text-xs text-text-muted">Cycle score</div>
                  <div className="mt-2 font-mono text-lg text-text-primary">{formatNumber(detail?.performance_cycle?.cycle_score)}</div>
                </div>
                <div className="rounded-2xl border border-bg-border bg-bg-primary/20 p-4">
                  <div className="text-xs text-text-muted">Relative strength</div>
                  <div className="mt-2 font-mono text-lg text-text-primary">{formatNumber(detail?.performance_cycle?.relative_strength)}</div>
                </div>
                <div className="rounded-2xl border border-bg-border bg-bg-primary/20 p-4">
                  <div className="text-xs text-text-muted">Momentum</div>
                  <div className="mt-2 font-mono text-lg text-text-primary">{formatNumber(detail?.performance_cycle?.momentum)}</div>
                </div>
              </div>
              <div className="mt-4 text-xs leading-5 text-text-muted">{detail?.performance_cycle?.method}</div>
            </div>

            <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-5">
              <div className="mb-4 text-sm font-semibold text-text-primary">Cycle Path</div>
              <div className="grid gap-3 md:grid-cols-4">
                {(detail?.performance_cycle?.phases || []).map((phase, index) => {
                  const active = phase.phase === detail?.performance_cycle?.current_phase;
                  return (
                    <div
                      key={phase.phase}
                      className={`rounded-2xl border p-4 ${
                        active
                          ? "border-accent-blue/45 bg-accent-blue/12"
                          : "border-bg-border bg-bg-primary/20"
                      }`}
                    >
                      <div className="font-mono text-xs text-text-muted">0{index + 1}</div>
                      <div className="mt-2 text-sm font-semibold text-text-primary">{phase.label}</div>
                      <div className="mt-2 text-xs leading-5 text-text-muted">{phase.description}</div>
                    </div>
                  );
                })}
              </div>
              <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/20 p-4 text-xs text-text-secondary">
                Next phase to watch: <span className="font-semibold capitalize text-text-primary">{detail?.performance_cycle?.next_phase_to_watch || "--"}</span>
              </div>
            </div>
          </div>
        ) : null}

        {activeTab === "constituents" ? (
          <div className="grid gap-4 xl:grid-cols-2">
            <ConstituentTable title="Leaders" rows={leaders} tone="text-accent-green" />
            <ConstituentTable title="Laggards" rows={laggards} tone="text-accent-red" />
          </div>
        ) : null}

        {activeTab === "parameters" ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
            {(detail?.parameters || []).map((parameter) => (
              <div key={parameter.code} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
                <div className="text-xs text-text-muted">{parameter.label}</div>
                <div className="mt-2 font-mono text-xl font-semibold text-text-primary">
                  {formatNumber(parameter.value, parameter.unit === "contracts" ? 0 : 2)}{parameter.unit === "%" ? "%" : ""}
                </div>
                <div className="mt-2 text-xs capitalize text-accent-blue">{parameter.state}</div>
              </div>
            ))}
          </div>
        ) : null}

        {activeTab === "alt-data" ? (
          <div className="grid gap-3 md:grid-cols-3">
            {(detail?.alt_data || []).map((item) => (
              <div key={item.name} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-text-primary">{item.name}</div>
                    <div className="mt-1 text-xs text-text-muted">{item.status.replaceAll("_", " ")}</div>
                  </div>
                  {item.value != null ? (
                    <div className="text-right">
                      <div className="font-mono text-sm font-semibold text-accent-blue">
                        {formatNumber(item.value, item.unit === "contracts" || item.unit === "symbols" ? 0 : 2)}
                        {item.unit === "%" ? "%" : ""}
                      </div>
                      <div className="mt-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">{item.state || "--"}</div>
                    </div>
                  ) : null}
                </div>
                {item.detail ? <div className="mt-3 text-xs leading-5 text-text-secondary">{item.detail}</div> : null}
              </div>
            ))}
          </div>
        ) : null}

        {activeTab === "peers" ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {topPeers.map((peer) => (
              <Link key={peer.sector_key} href={`/sector-interaction/${peer.sector_key}`} className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3 transition-colors hover:border-accent-blue/35">
                <div className="text-sm font-semibold text-text-primary">{peer.sector}</div>
                <div className="mt-1 text-xs text-text-muted">rank {peer.rank} / {peer.quadrant}</div>
                <div className="mt-3 font-mono text-sm text-accent-blue">{formatNumber(peer.leadership_score)}</div>
              </Link>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function ConstituentTable({ title, rows, tone }: { title: string; rows: LiveConstituent[]; tone: string }) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
      <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <BarChart3 size={17} className={tone} />
        {title}
      </div>
          <div className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-primary/12">
            <table className="w-full min-w-[820px] text-sm">
              <thead className="bg-bg-secondary/60 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
                <tr>
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-4 py-3">Score</th>
                  <th className="px-4 py-3">Price</th>
                  <th className="px-4 py-3">Change</th>
                  <th className="px-4 py-3">OI Change</th>
                  <th className="px-4 py-3">Volume</th>
                  <th className="px-4 py-3">IV</th>
                  <th className="px-4 py-3">RSI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border/70 text-text-secondary">
                {rows.map((row) => (
                  <tr key={row.symbol}>
                    <td className="px-4 py-3 font-semibold text-text-primary">{row.symbol}</td>
                    <td className={`px-4 py-3 font-mono ${tone}`}>{formatNumber(row.leadership_score, 2)}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(row.underlying_price, 2)}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(row.change_pct, 2)}%</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(row.oi_change_pct, 1)}%</td>
                    <td className="px-4 py-3 font-mono">{row.volume.toLocaleString("en-IN")}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(row.iv, 3)}</td>
                    <td className="px-4 py-3 font-mono">{formatNumber(row.rsi, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
    </div>
  );
}
