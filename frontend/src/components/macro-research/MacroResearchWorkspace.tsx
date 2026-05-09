"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  BookOpen,
  Database,
  Gauge,
  Globe2,
  Loader2,
  Radar,
  RefreshCw,
  Search,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
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
  getMacroResearchOverview,
  getMacroResearchSector,
  getMacroResearchSources,
  searchMacroResearch,
} from "@/lib/api";

type MacroIndicator = {
  id: string;
  label: string;
  latest_value: number;
  latest_year: string;
  unit: string;
  change: number;
  signal: "tailwind" | "headwind";
  influences: string[];
  history: Array<{ date: string; value: number }>;
  source: string;
};

type Commodity = {
  code: string;
  label: string;
  unit: string;
  price: number;
  change_pct: number;
  pressure: "rising" | "falling" | "flat";
  beneficiaries: string[];
  hurt_by_rise: string[];
  source: string;
  why: string;
};

type SectorSnapshot = {
  code: string;
  label: string;
  health_score: number;
  risk_score: number;
  stage: string;
  trend_score: number;
  macro_tailwinds: number;
  macro_headwinds: number;
  drivers: string[];
  draggers: string[];
  leaders: string[];
  agent_uses: string[];
  commodity_notes: string[];
  research_points: Array<{ metric: string; cadence: string; signal: string }>;
};

type BuddingTheme = {
  code: string;
  label: string;
  sector_code: string;
  budding_score: number;
  stage: string;
  why_now: string;
  watchlist: string[];
  news_social_proxy_score: number;
  frontier_research_score: number;
  agent_action: string;
};

type MacroOverview = {
  macro_indicators: MacroIndicator[];
  commodities: Commodity[];
  sectors: SectorSnapshot[];
  sector_leaders: SectorSnapshot[];
  sector_risks: SectorSnapshot[];
  budding_themes: BuddingTheme[];
  market_read: {
    headline: string;
    tailwind_count: number;
    headwind_count: number;
    cost_pressure_count: number;
    leading_sectors: string[];
    risk_sectors: string[];
    agent_instruction: string;
  };
  timestamp: string;
};

type SectorDetail = {
  sector: SectorSnapshot;
  research_matrix: Array<{ metric: string; cadence: string; signal: string }>;
  drivers: string[];
  draggers: string[];
  leaders: string[];
  agent_uses: string[];
  source_queries: {
    news_social_proxy: string[];
    frontier_research: string[];
  };
  agent_prompt: string;
};

type SearchPayload = {
  query: string;
  results: Array<{
    score: number;
    scope: string;
    sector_code: string;
    title: string;
    summary: string;
    tags: string[];
  }>;
};

type SourcesPayload = {
  sources: Array<{
    id: string;
    label: string;
    kind: string;
    url: string;
    docs: string;
    cadence: string;
    requires_key: boolean;
    status: string;
  }>;
  routing: Array<{ question: string; route: string }>;
};

const TABS = [
  { id: "macro", label: "Macro Radar", icon: Globe2 },
  { id: "sectors", label: "Sector Health", icon: BarChart3 },
  { id: "budding", label: "Budding Search", icon: Sparkles },
  { id: "playbook", label: "Sector Playbook", icon: BookOpen },
  { id: "sources", label: "Source Map", icon: Database },
] as const;

type TabId = (typeof TABS)[number]["id"];

function fmt(value?: number | null, digits = 1) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function sourceLabel(source?: string) {
  if (!source) return "unknown";
  return source.replaceAll("_", " ");
}

function scoreTone(score?: number) {
  if (score == null) return "text-text-muted";
  if (score >= 72) return "text-accent-green";
  if (score >= 56) return "text-accent-amber";
  return "text-accent-red";
}

function pressureTone(value?: string) {
  if (value === "falling") return "text-accent-green";
  if (value === "rising") return "text-accent-red";
  return "text-text-secondary";
}

function TabButton({
  active,
  label,
  Icon,
  onClick,
}: {
  active: boolean;
  label: string;
  Icon: LucideIcon;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "flex min-w-[170px] items-center gap-3 rounded-xl border px-4 py-3 text-left transition-colors",
        active
          ? "border-accent-blue/50 bg-accent-blue/14 text-text-primary"
          : "border-bg-border bg-bg-secondary/35 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      <Icon size={16} />
      <span className="text-[11px] font-semibold uppercase tracking-[0.16em]">{label}</span>
    </button>
  );
}

function StatTile({
  label,
  value,
  detail,
  tone = "text-text-primary",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/35 p-4">
      <div className="text-[10px] uppercase tracking-[0.18em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-xl font-semibold", tone)}>{value}</div>
      {detail ? <div className="mt-1 text-xs text-text-muted">{detail}</div> : null}
    </div>
  );
}

function LoadingBlock() {
  return (
    <div className="flex min-h-[360px] items-center justify-center rounded-2xl border border-bg-border bg-bg-secondary/25 text-text-secondary">
      <Loader2 className="mr-3 animate-spin" size={18} />
      Loading macro research...
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-accent-red/35 bg-accent-red/10 p-4 text-sm text-accent-red">
      {message}
    </div>
  );
}

export default function MacroResearchWorkspace() {
  const [tab, setTab] = useState<TabId>("macro");
  const [selectedSector, setSelectedSector] = useState("AUTO");
  const [searchText, setSearchText] = useState("EV battery auto sector margin pressure");
  const [refreshKey, setRefreshKey] = useState(0);

  const overviewQuery = useQuery({
    queryKey: ["macro-research-overview", refreshKey],
    queryFn: () => getMacroResearchOverview(refreshKey > 0).then((res) => res.data as MacroOverview),
    staleTime: 5 * 60 * 1000,
  });

  const sectorQuery = useQuery({
    queryKey: ["macro-research-sector", selectedSector, refreshKey],
    queryFn: () => getMacroResearchSector(selectedSector, refreshKey > 0).then((res) => res.data as SectorDetail),
    staleTime: 5 * 60 * 1000,
  });

  const searchQuery = useQuery({
    queryKey: ["macro-research-search", searchText, selectedSector, refreshKey],
    queryFn: () => searchMacroResearch(searchText, selectedSector, 14, refreshKey > 0).then((res) => res.data as SearchPayload),
    enabled: tab === "budding" || tab === "playbook",
    staleTime: 2 * 60 * 1000,
  });

  const sourcesQuery = useQuery({
    queryKey: ["macro-research-sources"],
    queryFn: () => getMacroResearchSources().then((res) => res.data as SourcesPayload),
    staleTime: 30 * 60 * 1000,
  });

  const overview = overviewQuery.data;
  const allSectors = useMemo(() => {
    if (!overview) return [];
    return [...(overview.sectors || [])].sort((a, b) => b.health_score - a.health_score);
  }, [overview]);

  const chartRows = useMemo(() => {
    if (!overview) return [];
    return (overview.sectors || [])
      .map((row) => ({ sector: row.code, health: row.health_score, risk: row.risk_score }));
  }, [overview]);

  if (overviewQuery.isLoading) {
    return (
      <main className="min-h-screen bg-bg-primary p-6">
        <LoadingBlock />
      </main>
    );
  }

  if (overviewQuery.isError || !overview) {
    return (
      <main className="min-h-screen bg-bg-primary p-6">
        <ErrorBlock message={describeApiError(overviewQuery.error, "Macro research failed")} />
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-bg-primary text-text-primary">
      <div className="space-y-5 p-5 lg:p-6">
        <header className="rounded-2xl border border-bg-border bg-bg-secondary/45 p-5">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-accent-amber">
                <Radar size={15} />
                Macro Research Engine
              </div>
              <h1 className="mt-3 text-2xl font-semibold tracking-tight text-text-primary">
                World, India, commodities, sector drivers and budding-theme discovery
              </h1>
              <p className="mt-2 max-w-5xl text-sm leading-6 text-text-secondary">
                {overview.market_read.headline}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="rounded-xl border border-bg-border bg-bg-primary/45 px-3 py-2 font-mono text-xs text-text-secondary">
                Updated {new Date(overview.timestamp).toLocaleString("en-IN", { hour12: false })}
              </div>
              <button
                type="button"
                onClick={() => setRefreshKey((value) => value + 1)}
                className="inline-flex items-center gap-2 rounded-xl border border-accent-blue/35 bg-accent-blue/10 px-4 py-2 text-sm font-semibold text-accent-blue transition-colors hover:bg-accent-blue/16"
              >
                <RefreshCw size={15} />
                Refresh
              </button>
            </div>
          </div>

          <div className="mt-5 grid gap-3 md:grid-cols-4">
            <StatTile label="Macro tailwinds" value={String(overview.market_read.tailwind_count)} detail="World Bank indicator read" tone="text-accent-green" />
            <StatTile label="Macro headwinds" value={String(overview.market_read.headwind_count)} detail="Rates, inflation, global growth" tone="text-accent-red" />
            <StatTile label="Cost pressures" value={String(overview.market_read.cost_pressure_count)} detail="Commodity basket rising" tone="text-accent-amber" />
            <StatTile label="Budding themes" value={String(overview.budding_themes.length)} detail="Discovery watchlist" tone="text-accent-blue" />
          </div>
        </header>

        <nav className="flex gap-2 overflow-x-auto pb-1">
          {TABS.map(({ id, label, icon: Icon }) => (
            <TabButton key={id} active={tab === id} label={label} Icon={Icon} onClick={() => setTab(id)} />
          ))}
        </nav>

        {tab === "macro" ? (
          <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Macro indicator tape</h2>
                  <p className="mt-1 text-xs text-text-muted">Annual public indicators with local fallback snapshots.</p>
                </div>
                <Gauge className="text-accent-blue" size={18} />
              </div>
              <div className="grid gap-3 lg:grid-cols-2">
                {overview.macro_indicators.map((item) => (
                  <div key={item.id} className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-text-primary">{item.label}</div>
                        <div className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-muted">
                          {item.latest_year} · {sourceLabel(item.source)}
                        </div>
                      </div>
                      <div className={clsx("font-mono text-lg font-semibold", item.signal === "tailwind" ? "text-accent-green" : "text-accent-red")}>
                        {fmt(item.latest_value, 1)}{item.unit}
                      </div>
                    </div>
                    <div className="mt-3 h-16">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={item.history}>
                          <Line type="monotone" dataKey="value" stroke={item.signal === "tailwind" ? "#00d4a3" : "#ff4757"} strokeWidth={2} dot={false} />
                          <XAxis dataKey="date" hide />
                          <YAxis hide domain={["dataMin", "dataMax"]} />
                          <Tooltip contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 8 }} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {item.influences.slice(0, 4).map((tag) => (
                        <span key={tag} className="rounded-lg border border-bg-border bg-bg-secondary/40 px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">
                          {tag}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Commodity pressure board</h2>
              <div className="mt-4 space-y-3">
                {overview.commodities.map((item) => (
                  <div key={item.code} className="rounded-xl border border-bg-border bg-bg-primary/35 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-text-primary">{item.label}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{item.why}</div>
                      </div>
                      <div className="text-right">
                        <div className="font-mono text-sm text-text-primary">{fmt(item.price, item.price > 999 ? 0 : 2)}</div>
                        <div className={clsx("font-mono text-xs", pressureTone(item.pressure))}>
                          {item.change_pct > 0 ? "+" : ""}{fmt(item.change_pct, 2)}%
                        </div>
                      </div>
                    </div>
                    <div className="mt-2 text-[11px] uppercase tracking-[0.12em] text-text-muted">
                      Pressure: <span className={pressureTone(item.pressure)}>{item.pressure}</span> · {sourceLabel(item.source)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>
        ) : null}

        {tab === "sectors" ? (
          <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Sector health vs risk</h2>
                  <p className="mt-1 text-xs text-text-muted">Ranked by driver evidence, macro tailwinds, commodity pressure and trend proxy.</p>
                </div>
                <Activity className="text-accent-green" size={18} />
              </div>
              <div className="h-[310px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartRows}>
                    <CartesianGrid stroke="#1e2d45" strokeDasharray="3 3" />
                    <XAxis dataKey="sector" stroke="#4a5568" fontSize={10} />
                    <YAxis stroke="#4a5568" fontSize={10} domain={[0, 100]} />
                    <Tooltip contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 8 }} />
                    <Bar dataKey="health" fill="#00d4a3" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="risk" fill="#ff4757" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-4 overflow-x-auto">
                <table className="min-w-full text-left text-sm">
                  <thead className="text-[10px] uppercase tracking-[0.16em] text-text-muted">
                    <tr className="border-b border-bg-border">
                      <th className="py-2 pr-3">Sector</th>
                      <th className="py-2 pr-3">Health</th>
                      <th className="py-2 pr-3">Risk</th>
                      <th className="py-2 pr-3">Stage</th>
                      <th className="py-2 pr-3">Leaders</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allSectors.map((full) => {
                      if (!full) return null;
                      return (
                        <tr
                          key={full.code}
                          className="cursor-pointer border-b border-bg-border/70 transition-colors hover:bg-bg-hover/35"
                          onClick={() => {
                            setSelectedSector(full.code);
                            setTab("playbook");
                          }}
                        >
                          <td className="py-3 pr-3 font-semibold text-text-primary">{full.label}</td>
                          <td className={clsx("py-3 pr-3 font-mono font-semibold", scoreTone(full.health_score))}>{fmt(full.health_score, 1)}</td>
                          <td className={clsx("py-3 pr-3 font-mono", full.risk_score > 65 ? "text-accent-red" : "text-text-secondary")}>{fmt(full.risk_score, 1)}</td>
                          <td className="py-3 pr-3 uppercase tracking-[0.08em] text-text-secondary">{full.stage}</td>
                          <td className="py-3 pr-3 font-mono text-xs text-text-muted">{full.leaders.slice(0, 5).join(" / ")}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="space-y-4">
              {overview.sector_leaders.slice(0, 4).map((sector) => (
                <button
                  key={sector.code}
                  type="button"
                  onClick={() => {
                    setSelectedSector(sector.code);
                    setTab("playbook");
                  }}
                  className="w-full rounded-2xl border border-bg-border bg-bg-secondary/35 p-4 text-left transition-colors hover:border-accent-green/45"
                >
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-text-primary">{sector.label}</div>
                      <div className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-muted">{sector.stage}</div>
                    </div>
                    <TrendingUp className="text-accent-green" size={18} />
                  </div>
                  <div className={clsx("mt-3 font-mono text-2xl font-semibold", scoreTone(sector.health_score))}>{fmt(sector.health_score, 1)}</div>
                  <p className="mt-2 text-xs leading-5 text-text-secondary">{sector.drivers[0]}</p>
                </button>
              ))}
            </div>
          </section>
        ) : null}

        {tab === "budding" ? (
          <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Budding sector search engine</h2>
                  <p className="mt-1 text-xs text-text-muted">Combines catalog evidence, public trend proxies, frontier reports and strategy fit.</p>
                </div>
                <div className="flex min-w-0 items-center gap-2 rounded-xl border border-bg-border bg-bg-primary/45 px-3 py-2 lg:w-[420px]">
                  <Search size={15} className="text-text-muted" />
                  <input
                    value={searchText}
                    onChange={(event) => setSearchText(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent text-sm text-text-primary outline-none placeholder:text-text-muted"
                    placeholder="Search EV, defence, AI, rural, rate pressure..."
                  />
                </div>
              </div>

              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                {overview.budding_themes.map((theme) => (
                  <div key={theme.code} className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-text-primary">{theme.label}</div>
                        <div className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-muted">{theme.sector_code} · {theme.stage}</div>
                      </div>
                      <div className={clsx("font-mono text-xl font-semibold", scoreTone(theme.budding_score))}>{fmt(theme.budding_score, 1)}</div>
                    </div>
                    <p className="mt-3 text-xs leading-5 text-text-secondary">{theme.why_now}</p>
                    <div className="mt-3 grid grid-cols-2 gap-2">
                      <div className="rounded-lg border border-bg-border bg-bg-secondary/35 p-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">News/social</div>
                        <div className="mt-1 font-mono text-sm text-text-primary">{fmt(theme.news_social_proxy_score, 1)}</div>
                      </div>
                      <div className="rounded-lg border border-bg-border bg-bg-secondary/35 p-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Frontier</div>
                        <div className="mt-1 font-mono text-sm text-text-primary">{fmt(theme.frontier_research_score, 1)}</div>
                      </div>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-1">
                      {theme.watchlist.slice(0, 6).map((symbol) => (
                        <span key={symbol} className="rounded-lg bg-accent-blue/10 px-2 py-1 font-mono text-[10px] text-accent-blue">
                          {symbol}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Search results</h2>
              <div className="mt-3 space-y-3">
                {searchQuery.isLoading ? (
                  <div className="flex items-center gap-2 text-sm text-text-secondary"><Loader2 size={15} className="animate-spin" /> Searching...</div>
                ) : searchQuery.data?.results?.length ? (
                  searchQuery.data.results.map((result) => (
                    <div key={`${result.scope}:${result.title}`} className="rounded-xl border border-bg-border bg-bg-primary/35 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-text-primary">{result.title}</div>
                          <div className="mt-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">{result.scope} · {result.sector_code}</div>
                        </div>
                        <div className="font-mono text-xs text-accent-amber">{fmt(result.score, 0)}</div>
                      </div>
                      <p className="mt-2 text-xs leading-5 text-text-secondary">{result.summary}</p>
                    </div>
                  ))
                ) : (
                  <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4 text-sm text-text-muted">
                    No matching research points yet.
                  </div>
                )}
              </div>
            </div>
          </section>
        ) : null}

        {tab === "playbook" ? (
          <section className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
            <aside className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-3">
              <div className="px-1 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">Sector selector</div>
              <div className="mt-2 max-h-[620px] space-y-1 overflow-y-auto pr-1">
                {allSectors.map((sector) => (
                    <button
                      key={sector.code}
                      type="button"
                      onClick={() => setSelectedSector(sector.code)}
                      className={clsx(
                        "w-full rounded-xl border px-3 py-3 text-left transition-colors",
                        selectedSector === sector.code
                          ? "border-accent-blue/45 bg-accent-blue/12"
                          : "border-bg-border bg-bg-primary/30 hover:border-bg-active",
                      )}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm font-semibold text-text-primary">{sector.code}</span>
                        <span className={clsx("font-mono text-xs", scoreTone(sector.health_score))}>{fmt(sector.health_score, 0)}</span>
                      </div>
                      <div className="mt-1 truncate text-xs text-text-muted">{sector.label}</div>
                    </button>
                  ))}
              </div>
            </aside>

            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              {sectorQuery.isLoading ? (
                <LoadingBlock />
              ) : sectorQuery.isError || !sectorQuery.data ? (
                <ErrorBlock message={describeApiError(sectorQuery.error, "Sector detail failed")} />
              ) : (
                <SectorPlaybook detail={sectorQuery.data} />
              )}
            </div>
          </section>
        ) : null}

        {tab === "sources" ? (
          <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Public resource connectors</h2>
              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                {sourcesQuery.data?.sources.map((source) => (
                  <a
                    key={source.id}
                    href={source.url}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-xl border border-bg-border bg-bg-primary/35 p-4 transition-colors hover:border-accent-blue/45"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-text-primary">{source.label}</div>
                        <div className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-muted">{source.kind}</div>
                      </div>
                      <span className={clsx("rounded-lg px-2 py-1 text-[10px] uppercase tracking-[0.12em]", source.requires_key ? "bg-accent-amber/12 text-accent-amber" : "bg-accent-green/12 text-accent-green")}>
                        {source.requires_key ? "key" : "public"}
                      </span>
                    </div>
                    <div className="mt-3 text-xs text-text-secondary">{source.status.replaceAll("_", " ")}</div>
                    <div className="mt-2 font-mono text-[11px] text-text-muted">{source.cadence}</div>
                  </a>
                ))}
              </div>
            </div>
            <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
              <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Agent routing</h2>
              <div className="mt-4 space-y-3">
                {sourcesQuery.data?.routing.map((item) => (
                  <div key={item.question} className="rounded-xl border border-bg-border bg-bg-primary/35 p-3">
                    <div className="text-xs font-semibold text-text-primary">{item.question}</div>
                    <div className="mt-2 text-xs leading-5 text-text-secondary">{item.route}</div>
                  </div>
                ))}
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </main>
  );
}

function SectorPlaybook({ detail }: { detail: SectorDetail }) {
  const sector = detail.sector;
  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-blue">{sector.code} playbook</div>
          <h2 className="mt-2 text-2xl font-semibold text-text-primary">{sector.label}</h2>
          <p className="mt-2 max-w-4xl text-sm leading-6 text-text-secondary">{detail.agent_prompt}</p>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:min-w-[280px]">
          <StatTile label="Health" value={fmt(sector.health_score, 1)} tone={scoreTone(sector.health_score)} />
          <StatTile label="Risk" value={fmt(sector.risk_score, 1)} tone={sector.risk_score > 65 ? "text-accent-red" : "text-text-primary"} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-accent-green">
            <TrendingUp size={15} />
            Drivers
          </div>
          <div className="space-y-2">
            {detail.drivers.map((item) => (
              <div key={item} className="rounded-lg border border-bg-border bg-bg-secondary/25 p-3 text-sm leading-6 text-text-secondary">
                {item}
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-accent-red">
            <TrendingDown size={15} />
            Draggers
          </div>
          <div className="space-y-2">
            {detail.draggers.map((item) => (
              <div key={item} className="rounded-lg border border-bg-border bg-bg-secondary/25 p-3 text-sm leading-6 text-text-secondary">
                {item}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
        <h3 className="text-sm font-semibold uppercase tracking-[0.16em] text-text-primary">Research data points</h3>
        <div className="mt-3 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="text-[10px] uppercase tracking-[0.16em] text-text-muted">
              <tr className="border-b border-bg-border">
                <th className="py-2 pr-4">Metric</th>
                <th className="py-2 pr-4">Cadence</th>
                <th className="py-2 pr-4">Signal read</th>
              </tr>
            </thead>
            <tbody>
              {detail.research_matrix.map((item) => (
                <tr key={item.metric} className="border-b border-bg-border/70">
                  <td className="py-3 pr-4 font-semibold text-text-primary">{item.metric}</td>
                  <td className="py-3 pr-4 font-mono text-xs uppercase text-accent-amber">{item.cadence}</td>
                  <td className="py-3 pr-4 text-text-secondary">{item.signal}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
          <div className="text-[10px] uppercase tracking-[0.16em] text-text-muted">Leaders</div>
          <div className="mt-3 flex flex-wrap gap-2">
            {detail.leaders.map((symbol) => (
              <span key={symbol} className="rounded-lg bg-accent-blue/10 px-2 py-1 font-mono text-xs text-accent-blue">{symbol}</span>
            ))}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
          <div className="text-[10px] uppercase tracking-[0.16em] text-text-muted">Agent use cases</div>
          <div className="mt-3 space-y-2">
            {detail.agent_uses.map((item) => (
              <div key={item} className="text-xs text-text-secondary">{item}</div>
            ))}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/35 p-4">
          <div className="text-[10px] uppercase tracking-[0.16em] text-text-muted">Live query seeds</div>
          <div className="mt-3 space-y-2">
            {[...detail.source_queries.news_social_proxy, ...detail.source_queries.frontier_research].slice(0, 6).map((item) => (
              <div key={item} className="font-mono text-[11px] text-text-secondary">{item}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
