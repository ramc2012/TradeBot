"use client";

/**
 * Macro Research desk — native v2.
 *
 * Tabs:
 *   overview     → macro-indicator tape (GDP/inflation/rates/unemployment with
 *                  z-scores + tailwind/headwind badges) + market-read summary
 *   commodities  → commodity-pressure board (price, %chg, trend, sectors)
 *   sectors      → health-vs-risk bubble scatter + sector playbook panel
 *   discovery    → budding-sector search list (theme, score, stage, why_now)
 *
 * Pure research lane — no paper positions, so no Performance tab.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Compass, Flame, Gauge, Layers, Search, Sparkles, TrendingDown, TrendingUp } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

import { CommodityBoard, type Commodity } from "./CommodityBoard";
import { SectorScatter, type ScatterSector } from "./SectorScatter";
import { Sparkline } from "@/components/desk-ui";

const TABS = [
  { key: "overview", label: "Overview", icon: Gauge },
  { key: "commodities", label: "Commodities", icon: Flame },
  { key: "sectors", label: "Sectors", icon: Layers },
  { key: "discovery", label: "Discovery", icon: Search },
];

/* ---------- data shapes (from /api/macro-research/overview) ---------- */
type HistPoint = { date: string; value: number };
type MacroIndicator = {
  id: string;
  label: string;
  country?: string;
  latest_value?: number;
  latest_year?: string;
  unit?: string;
  change?: number;
  signal?: string; // tailwind | headwind
  good_direction?: string; // higher | lower
  influences?: string[];
  history?: HistPoint[];
  source?: string;
};
type Sector = ScatterSector & {
  drivers?: string[];
  draggers?: string[];
  leaders?: string[];
  agent_uses?: string[];
  commodity_notes?: string[];
  research_points?: { metric: string; cadence: string; signal: string }[];
};
type BuddingTheme = {
  code: string;
  label: string;
  sector_code?: string;
  budding_score?: number;
  stage?: string;
  why_now?: string;
  watchlist?: string[];
  news_social_proxy_score?: number;
  frontier_research_score?: number;
  agent_action?: string;
};
type MarketRead = {
  headline?: string;
  tailwind_count?: number;
  headwind_count?: number;
  cost_pressure_count?: number;
  leading_sectors?: string[];
  risk_sectors?: string[];
  agent_instruction?: string;
};
type Overview = {
  macro_indicators?: MacroIndicator[];
  commodities?: Commodity[];
  sectors?: Sector[];
  sector_leaders?: Sector[];
  sector_risks?: Sector[];
  budding_themes?: BuddingTheme[];
  market_read?: MarketRead;
  sources?: string[];
  timestamp?: string;
};
type SearchResult = {
  score?: number;
  scope?: string; // sector | budding_theme
  sector_code?: string;
  title?: string;
  summary?: string;
  tags?: string[];
};

const signalVariant = (s?: string): "success" | "error" | "neutral" =>
  s === "tailwind" ? "success" : s === "headwind" ? "error" : "neutral";

/* z-score of the latest value vs its own history — a relative-position read. */
function zScore(ind: MacroIndicator): number | null {
  const hist = (ind.history || []).map((h) => h.value).filter((v) => Number.isFinite(v));
  if (hist.length < 3 || ind.latest_value == null) return null;
  const mean = hist.reduce((a, b) => a + b, 0) / hist.length;
  const variance = hist.reduce((a, b) => a + (b - mean) ** 2, 0) / hist.length;
  const sd = Math.sqrt(variance);
  if (sd < 1e-9) return 0;
  return (ind.latest_value - mean) / sd;
}

export default function MacroDesk() {
  const [activeTab, setActiveTab] = useUrlTab("overview");
  const [selectedSector, setSelectedSector] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");

  const overviewQuery = useQuery({
    queryKey: ["macro", "overview"],
    queryFn: async () => (await apiClient.get("/api/macro-research/overview")).data as Overview,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const searchQuery = useQuery({
    queryKey: ["macro", "search", submitted],
    queryFn: async () =>
      (await apiClient.get("/api/macro-research/search", { params: { q: submitted, limit: 14 } })).data as {
        results?: SearchResult[];
        result_count?: number;
      },
    enabled: activeTab === "discovery" && submitted.trim().length > 0,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const data = overviewQuery.data;
  const indicators = data?.macro_indicators || [];
  const commodities = data?.commodities || [];
  const sectors = (data?.sectors || []) as Sector[];
  const themes = data?.budding_themes || [];
  const read = data?.market_read || {};

  const activeSector = useMemo(
    () => sectors.find((s) => s.code === (selectedSector ?? sectors[0]?.code)) || null,
    [sectors, selectedSector],
  );

  return (
    <DeskShell
      title="Macro Research"
      description="Top-down macro, commodity-pressure, and sector-rotation intelligence for the trading agents."
      asOf={data?.timestamp}
      isFetching={overviewQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/macro-research"
      rightSlot={
        <div className="hidden items-center gap-1.5 md:flex">
          <StatusBadge label={`${read.tailwind_count ?? 0} tailwinds`} variant="success" />
          <StatusBadge label={`${read.headwind_count ?? 0} headwinds`} variant="error" />
        </div>
      }
    >
      {/* KPI strip — present on every tab */}
      <section className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Tailwinds" value={String(read.tailwind_count ?? 0)} detail="macro indicators" color="text-accent-green" />
        <MetricTile label="Headwinds" value={String(read.headwind_count ?? 0)} detail="macro indicators" color="text-accent-red" />
        <MetricTile label="Cost pressure" value={String(read.cost_pressure_count ?? 0)} detail="rising commodities" color={read.cost_pressure_count ? "text-accent-amber" : undefined} />
        <MetricTile label="Sectors" value={String(sectors.length)} detail={`${sectors.filter((s) => s.stage === "leading").length} leading`} />
        <MetricTile label="Budding themes" value={String(themes.length)} detail="emerging frontiers" color="text-accent-blue" />
        <MetricTile label="Sources" value={String((data?.sources || []).length)} detail={(data?.sources || []).slice(0, 2).join(", ")} />
      </section>

      {activeTab === "overview" ? (
        <OverviewTab indicators={indicators} read={read} loading={overviewQuery.isLoading} />
      ) : null}

      {activeTab === "commodities" ? (
        <Section title="Commodity-pressure board" icon={<Flame size={16} />} description="Input-cost trend and second-order sector impact. Rising = inflationary headwind.">
          <CommodityBoard commodities={commodities} />
        </Section>
      ) : null}

      {activeTab === "sectors" ? (
        <SectorsTab sectors={sectors} active={activeSector} onSelect={(c) => setSelectedSector(c)} />
      ) : null}

      {activeTab === "discovery" ? (
        <DiscoveryTab
          themes={themes}
          query={query}
          onQuery={setQuery}
          onSubmit={() => setSubmitted(query)}
          results={searchQuery.data?.results || []}
          searching={searchQuery.isFetching}
          submitted={submitted}
        />
      ) : null}
    </DeskShell>
  );
}

/* ============================== OVERVIEW ============================== */
function OverviewTab({ indicators, read, loading }: { indicators: MacroIndicator[]; read: MarketRead; loading: boolean }) {
  if (loading && !indicators.length) {
    return <Section title="Macro indicators"><div className="py-10 text-center text-sm text-text-muted">Loading macro tape…</div></Section>;
  }
  return (
    <div className="space-y-4">
      {read.headline ? (
        <Section title="Market read" icon={<Sparkles size={16} />}>
          <div className="text-[13.5px] leading-relaxed text-text-primary">{read.headline}</div>
          {read.agent_instruction ? (
            <div className="mt-2 rounded-lg border border-bg-border bg-bg-primary/20 px-3 py-2 text-[11.5px] text-text-secondary">
              <span className="text-accent-blue">Agent gate · </span>{read.agent_instruction}
            </div>
          ) : null}
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <ChipBlock title="Leading sectors" tone="green" items={read.leading_sectors} />
            <ChipBlock title="Risk sectors" tone="red" items={read.risk_sectors} />
          </div>
        </Section>
      ) : null}

      <Section title="Macro-indicator tape" icon={<Activity size={16} />} description="Latest value, year-over-year change, z-score vs own history, and tailwind/headwind for equities.">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {indicators.map((ind) => {
            const z = zScore(ind);
            const up = (ind.change ?? 0) > 0;
            const good = ind.signal === "tailwind";
            return (
              <div key={ind.id} className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3.5">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-[12.5px] font-semibold text-text-primary">{ind.label}</div>
                    <div className="text-[10px] uppercase tracking-[0.12em] text-text-muted">{ind.country} · {ind.latest_year} · {ind.source}</div>
                  </div>
                  <StatusBadge label={ind.signal || "—"} variant={signalVariant(ind.signal)} />
                </div>
                <div className="mt-3 flex items-end justify-between gap-3">
                  <div>
                    <div className="font-mono text-xl font-semibold text-text-primary">
                      {formatNumber(ind.latest_value, 2)}<span className="ml-0.5 text-[11px] text-text-muted">{ind.unit}</span>
                    </div>
                    <div className={`font-mono text-[11.5px] ${up ? "text-accent-green" : "text-accent-red"}`}>
                      {up ? "▲" : "▼"} {formatSigned(ind.change)} YoY
                    </div>
                  </div>
                  <Sparkline values={(ind.history || []).map((h) => h.value)} width={104} height={38} color={good ? "rgb(var(--accent-green))" : "rgb(var(--accent-red))"} />
                </div>
                <div className="mt-2.5 flex items-center justify-between border-t border-bg-border/40 pt-2 text-[11px]">
                  <span className="text-text-muted">z-score</span>
                  <span className={`font-mono ${z == null ? "text-text-muted" : tone(z)}`}>{z == null ? "—" : `${z > 0 ? "+" : ""}${z.toFixed(2)}σ`}</span>
                </div>
                {ind.influences?.length ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {ind.influences.slice(0, 6).map((x) => (
                      <span key={x} className="rounded bg-bg-primary/30 px-1.5 py-0.5 text-[9.5px] text-text-secondary">{x}</span>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

/* ============================== SECTORS ============================== */
function SectorsTab({ sectors, active, onSelect }: { sectors: Sector[]; active: Sector | null; onSelect: (code: string) => void }) {
  return (
    <div className="space-y-4">
      <Section title="Sector health-vs-risk map" icon={<Compass size={16} />} description="x = risk, y = health, bubble size = trend momentum, colour = net macro tailwind. Click a bubble for its playbook.">
        <SectorScatter sectors={sectors} selected={active?.code} onSelect={onSelect} />
      </Section>

      {active ? (
        <Section
          title={`Playbook · ${active.label}`}
          icon={<Layers size={16} />}
          rightSlot={
            <div className="flex items-center gap-1.5">
              <StatusBadge label={active.stage || "—"} variant={active.stage === "leading" ? "success" : "info"} />
              <StatusBadge label={`net ${(active.macro_tailwinds ?? 0) - (active.macro_headwinds ?? 0) >= 0 ? "+" : ""}${(active.macro_tailwinds ?? 0) - (active.macro_headwinds ?? 0)}`} variant={(active.macro_tailwinds ?? 0) - (active.macro_headwinds ?? 0) >= 0 ? "success" : "error"} />
            </div>
          }
        >
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <ScoreTile label="Health" value={active.health_score} good />
            <ScoreTile label="Risk" value={active.risk_score} good={false} />
            <ScoreTile label="Trend" value={active.trend_score} good />
            <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-2">
              <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Tail / Head</div>
              <div className="mt-0.5 font-mono text-text-primary">
                <span className="text-accent-green">{active.macro_tailwinds ?? 0}</span>
                <span className="text-text-muted"> / </span>
                <span className="text-accent-red">{active.macro_headwinds ?? 0}</span>
              </div>
            </div>
          </div>

          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            <BulletList title="Drivers" icon={<TrendingUp size={13} className="text-accent-green" />} items={active.drivers} dot="bg-accent-green/70" />
            <BulletList title="Draggers" icon={<TrendingDown size={13} className="text-accent-red" />} items={active.draggers} dot="bg-accent-red/70" />
          </div>

          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <ChipBlock title="Leaders" tone="blue" items={active.leaders} />
            <ChipBlock title="Agent uses" tone="purple" items={active.agent_uses} />
          </div>

          {active.commodity_notes?.length ? (
            <div className="mt-3 rounded-lg border border-bg-border bg-bg-primary/20 px-3 py-2">
              <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Commodity notes</div>
              <ul className="mt-1 space-y-0.5">
                {active.commodity_notes.map((n, i) => (
                  <li key={i} className="text-[11.5px] text-text-secondary">· {n}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {active.research_points?.length ? (
            <div className="mt-3">
              <div className="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-text-muted">Research matrix</div>
              <MiniTable
                head={["Metric", "Cadence", "Signal"]}
                rows={active.research_points.map((r) => [r.metric, r.cadence, r.signal])}
              />
            </div>
          ) : null}
        </Section>
      ) : null}
    </div>
  );
}

/* ============================== DISCOVERY ============================== */
function DiscoveryTab({
  themes,
  query,
  onQuery,
  onSubmit,
  results,
  searching,
  submitted,
}: {
  themes: BuddingTheme[];
  query: string;
  onQuery: (v: string) => void;
  onSubmit: () => void;
  results: SearchResult[];
  searching: boolean;
  submitted: string;
}) {
  return (
    <div className="space-y-4">
      <Section title="Research search" icon={<Search size={16} />} description="Full-text search across sector profiles and budding themes.">
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit();
          }}
        >
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="e.g. hydrogen, defence, semiconductors, capex…"
            className="flex-1 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-2 text-[13px] text-text-primary outline-none placeholder:text-text-muted focus:border-accent-blue/50"
          />
          <button type="submit" className="rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-4 py-2 text-[12.5px] font-medium text-accent-blue hover:bg-accent-blue/20">
            Search
          </button>
        </form>
        {submitted ? (
          <div className="mt-3">
            {searching ? (
              <div className="py-4 text-center text-sm text-text-muted">Searching…</div>
            ) : results.length ? (
              <ul className="space-y-2">
                {results.map((r, i) => (
                  <li key={i} className="rounded-lg border border-bg-border bg-bg-secondary/40 px-3 py-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-[12.5px] font-semibold text-text-primary">{r.title}</div>
                      <div className="flex items-center gap-1.5">
                        <StatusBadge label={r.scope === "budding_theme" ? "theme" : "sector"} variant={r.scope === "budding_theme" ? "info" : "neutral"} />
                        <span className="font-mono text-[11px] text-accent-blue">{formatNumber(r.score, 0)}</span>
                      </div>
                    </div>
                    {r.summary ? <div className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-text-secondary">{r.summary}</div> : null}
                    {r.tags?.length ? (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {r.tags.slice(0, 7).map((t) => (
                          <span key={t} className="rounded bg-bg-primary/30 px-1.5 py-0.5 text-[9.5px] text-text-muted">{t}</span>
                        ))}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="py-4 text-center text-sm text-text-muted">No matches for “{submitted}”.</div>
            )}
          </div>
        ) : null}
      </Section>

      <Section title="Budding sectors" icon={<Sparkles size={16} />} description="Emerging themes ranked by budding score (news/social + frontier-research proxies).">
        <div className="grid gap-3 md:grid-cols-2">
          {themes.map((t) => {
            const score = t.budding_score ?? 0;
            const pct = Math.min(100, Math.max(0, score));
            return (
              <div key={t.code} className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3.5">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-[12.5px] font-semibold text-text-primary">{t.label}</div>
                    <div className="text-[10px] uppercase tracking-[0.12em] text-text-muted">{t.sector_code} · {t.stage}</div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-lg font-semibold text-accent-blue">{formatNumber(score, 1)}</div>
                    <div className="text-[9px] uppercase tracking-[0.12em] text-text-muted">budding</div>
                  </div>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-bg-primary/40">
                  <div className="h-full rounded-full" style={{ width: `${pct}%`, background: "rgb(var(--accent-blue))" }} />
                </div>
                {t.why_now ? <div className="mt-2 text-[11.5px] leading-snug text-text-secondary">{t.why_now}</div> : null}
                <div className="mt-2 flex items-center gap-3 text-[10px] text-text-muted">
                  <span>news/social <span className="font-mono text-text-secondary">{formatNumber(t.news_social_proxy_score, 0)}</span></span>
                  <span>frontier <span className="font-mono text-text-secondary">{formatNumber(t.frontier_research_score, 0)}</span></span>
                </div>
                {t.watchlist?.length ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {t.watchlist.slice(0, 8).map((w) => (
                      <span key={w} className="rounded border border-accent-purple/30 bg-accent-purple/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-purple">{w}</span>
                    ))}
                  </div>
                ) : null}
                {t.agent_action ? <div className="mt-2 text-[10.5px] text-accent-blue/80">Agent · {t.agent_action}</div> : null}
              </div>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

/* ============================== shared bits ============================== */
function ScoreTile({ label, value, good }: { label: string; value?: number; good: boolean }) {
  const v = value ?? 0;
  const color = good ? (v >= 55 ? "text-accent-green" : v <= 45 ? "text-accent-red" : "text-text-primary") : v >= 55 ? "text-accent-red" : v <= 45 ? "text-accent-green" : "text-text-primary";
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-2">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-semibold ${color}`}>{formatNumber(value, 1)}</div>
    </div>
  );
}

function BulletList({ title, icon, items, dot }: { title: string; icon: React.ReactNode; items?: string[]; dot: string }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[10.5px] uppercase tracking-[0.14em] text-text-muted">{icon}{title}</div>
      <ul className="space-y-1">
        {(items || []).map((x, i) => (
          <li key={i} className="flex items-start gap-2 text-[11.5px] text-text-secondary">
            <span className={`mt-1.5 h-1 w-1 shrink-0 rounded-full ${dot}`} />
            {x}
          </li>
        ))}
        {!items?.length ? <li className="text-[11px] text-text-muted">—</li> : null}
      </ul>
    </div>
  );
}

function ChipBlock({ title, items, tone }: { title: string; items?: string[]; tone: "green" | "red" | "blue" | "purple" }) {
  const cls = {
    green: "border-accent-green/30 bg-accent-green/10 text-accent-green",
    red: "border-accent-red/30 bg-accent-red/10 text-accent-red",
    blue: "border-accent-blue/30 bg-accent-blue/10 text-accent-blue",
    purple: "border-accent-purple/30 bg-accent-purple/10 text-accent-purple",
  }[tone];
  return (
    <div>
      <div className="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-text-muted">{title}</div>
      <div className="flex flex-wrap gap-1">
        {(items || []).map((s) => (
          <span key={s} className={`rounded border px-1.5 py-0.5 text-[10.5px] font-medium ${cls}`}>{s}</span>
        ))}
        {!items?.length ? <span className="text-[11px] text-text-muted">—</span> : null}
      </div>
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
              <th key={i} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i === 0 ? "text-left" : "text-left"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((r, i) => (
            <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              {r.map((c, j) => (
                <td key={j} className={`px-2.5 py-1.5 text-[11.5px] ${j === 0 ? "text-text-primary" : "text-text-secondary"}`}>{c}</td>
              ))}
            </tr>
          )) : (
            <tr><td colSpan={head.length} className="px-2.5 py-6 text-center text-sm text-text-muted">No data</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function formatSigned(n?: number): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}`;
}
