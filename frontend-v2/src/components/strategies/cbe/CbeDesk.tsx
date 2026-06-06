"use client";

/**
 * CBE Scanner desk — native v2.
 *
 * The Compression-Before-Expansion scanner runs a weekly alpha engine
 * (MACD + RSI + Relative-Rotation-Graph) over the NSE F&O universe and
 * paper-trades the top-N ranked names. This desk surfaces the scan output
 * end-to-end: cross-asset rotation, sector rotation, the stock-level RRG
 * scatter, the ranked alpha-candidate book, and the paper portfolio.
 *
 * Tabs:
 *   rotation     → cross-asset winner + sector-rotation ladder + RRG quadrant census
 *   candidates   → RRG scatter (signature viz) + ranked alpha-candidate table (sparklines)
 *   sectors      → sector winners vs Nifty50 (RS%, quadrant, leaders)
 *   performance  → PaperPerformance (equity curve, R-dist, trade book) from CBE paper endpoints
 *
 * Data: /api/cbe/latest (alpha_engine scan), /api/cbe/paper-summary,
 * /api/cbe/paper-positions. Scans are produced by the backend paper-agent
 * cycle (POST /api/cbe/scan) — this desk reads the latest persisted scan.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Compass, Layers3, Radar, TrendingUp } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedMoney,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { PaperPerformance } from "@/components/strategies/shared";
import type { PositionsPayload } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { RrgScatter, QUADRANT_COLOR, type RrgPoint } from "./RrgScatter";
import { SectorRotation, type SectorRow } from "./SectorRotation";
import { Sparkline } from "./Sparkline";

const TABS = [
  { key: "rotation", label: "Rotation", icon: Radar },
  { key: "candidates", label: "Candidates", icon: Compass },
  { key: "sectors", label: "Sectors", icon: Layers3 },
  { key: "performance", label: "Performance", icon: TrendingUp },
];

// ── Scan payload types (from /api/cbe/latest, source=alpha_engine) ──────────
type MacdMeta = { line?: number; signal?: number; cross_today?: boolean; label?: string };
type RsiMeta = { rsi?: number; label?: string };

type ScanResult = {
  instrument: string;
  composite_score?: number | null;
  composite_alpha_score?: number | null;
  directional_bias?: string | null;
  bias_conviction?: number | null;
  is_watchlist?: boolean | null;
  gate_passed?: boolean | null;
  sector_code?: string | null;
  sector_quadrant?: string | null;
  sector_rs_pct?: number | null;
  stock_quadrant?: string | null;
  stock_rs_pct?: number | null;
  stock_rank_in_sector?: number | null;
  macd_line?: number | null;
  macd_signal?: number | null;
  macd_hist?: number | null;
  macd_bullish?: boolean | null;
  macd_score?: number | null;
  macd_meta?: MacdMeta | null;
  rsi_14?: number | null;
  rsi_score?: number | null;
  rsi_meta?: RsiMeta | null;
  weekly_close_vs_ema20?: number | null;
  weekly_trend?: string | null;
  latest_close?: number | null;
  recent_closes_30d?: number[] | null;
  details?: {
    components?: { rsi?: number; macd?: number; asset?: number; stock?: number; sector?: number };
  } | null;
};

type ScanPayload = {
  id?: string;
  source?: string;
  scan_date?: string | null;
  created_at?: string | null;
  universe_size?: number;
  scored_count?: number;
  watchlist_count?: number;
  asset_winner?: string | null;
  config?: {
    timeframe?: string;
    sectors_to_keep?: number;
    top_n_watchlist?: number;
    low_conviction_floor?: number;
  };
  results?: ScanResult[];
  watchlist?: ScanResult[];
};

type PaperSummary = {
  open_positions?: number;
  closed_positions?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  total_pnl?: number;
  initial_capital?: number;
  available_capital?: number;
  reserved_margin?: number;
  total_equity?: number;
  total_return_pct?: number;
  win_rate?: number;
  total_trades?: number;
};

const biasVariant = (b?: string | null) =>
  b === "bullish" ? "success" : b === "bearish" ? "error" : "neutral";
const trendTone = (t?: string | null) =>
  t === "up" ? "text-accent-green" : t === "down" ? "text-accent-red" : undefined;

export default function CbeDesk() {
  const [activeTab, setActiveTab] = useUrlTab("rotation");

  const latestQuery = useQuery({
    queryKey: ["cbe", "latest"],
    queryFn: async () => (await apiClient.get("/api/cbe/latest")).data as ScanPayload,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const paperSummaryQuery = useQuery({
    queryKey: ["cbe", "paper-summary"],
    queryFn: async () => (await apiClient.get("/api/cbe/paper-summary")).data as PaperSummary,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const paperPositionsQuery = useQuery({
    queryKey: ["cbe", "paper-positions"],
    queryFn: async () =>
      (await apiClient.get("/api/cbe/paper-positions", { params: { status: "all", limit: 500 } }))
        .data as PositionsPayload & { last_synced_at?: string },
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const scan = latestQuery.data;
  const results = useMemo(() => scan?.results ?? [], [scan?.results]);
  const watchlist = scan?.watchlist ?? [];
  const paperSum = paperSummaryQuery.data;
  const positions = paperPositionsQuery.data;

  // RRG scatter points — every scored name, x=stock RS%, y=MACD histogram.
  const rrgPoints = useMemo<RrgPoint[]>(
    () =>
      results
        .filter((r) => r.stock_quadrant && r.stock_quadrant !== "unclassified")
        .map((r) => ({
          symbol: r.instrument,
          rs: r.stock_rs_pct ?? 0,
          momentum: r.macd_hist ?? 0,
          score: r.composite_alpha_score ?? 0,
          quadrant: r.stock_quadrant ?? "lagging",
          watchlist: !!r.is_watchlist,
        })),
    [results],
  );

  // Sector rotation ladder — collapse results into the distinct sectors.
  const sectors = useMemo<SectorRow[]>(() => {
    const map = new Map<string, SectorRow>();
    for (const r of results) {
      const code = r.sector_code;
      if (!code) continue;
      const row = map.get(code) ?? {
        code,
        quadrant: r.sector_quadrant ?? "lagging",
        rs: r.sector_rs_pct ?? 0,
        count: 0,
        leaders: 0,
      };
      row.count += 1;
      if (r.stock_quadrant === "leading") row.leaders += 1;
      map.set(code, row);
    }
    return Array.from(map.values());
  }, [results]);

  // Stock RRG quadrant census for the KPI strip.
  const census = useMemo(() => {
    const c = { leading: 0, improving: 0, weakening: 0, lagging: 0 };
    for (const r of results) {
      const q = r.stock_quadrant as keyof typeof c;
      if (q in c) c[q] += 1;
    }
    return c;
  }, [results]);

  const ranked = useMemo(
    () => [...results].sort((a, b) => (b.composite_alpha_score ?? 0) - (a.composite_alpha_score ?? 0)),
    [results],
  );

  const totalReturn = paperSum?.total_return_pct ?? null;

  return (
    <DeskShell
      title="CBE Scanner"
      description={`Compression-Before-Expansion — weekly alpha engine (MACD · RSI · RRG). ${
        scan?.source ?? ""
      }`}
      asOf={scan?.created_at ?? scan?.scan_date ?? undefined}
      isFetching={latestQuery.isFetching}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/cbe"
      rightSlot={
        <div className="flex items-center gap-2">
          <StatusBadge
            label={`scan ${scan?.scan_date ?? "—"}`}
            variant="info"
            icon={<Activity size={12} />}
          />
          <StatusBadge
            label={`${scan?.watchlist_count ?? 0} watchlist`}
            variant={scan?.watchlist_count ? "success" : "neutral"}
          />
        </div>
      }
    >
      {/* ── Rotation ─────────────────────────────────────────────────────── */}
      {activeTab === "rotation" ? (
        <div className="space-y-4">
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
            <MetricTile label="Asset winner" value={scan?.asset_winner ?? "—"} detail="cross-asset rotation" color="text-accent-amber" />
            <MetricTile label="Universe" value={String(scan?.universe_size ?? 0)} detail={`${scan?.scored_count ?? 0} scored`} />
            <MetricTile label="Watchlist" value={String(scan?.watchlist_count ?? 0)} detail={`top-${scan?.config?.top_n_watchlist ?? "N"} gate`} />
            <MetricTile label="Leading" value={String(census.leading)} detail="stocks" color="text-accent-green" />
            <MetricTile label="Improving" value={String(census.improving)} detail="stocks" color="text-accent-blue" />
            <MetricTile label="Lagging" value={String(census.lagging + census.weakening)} detail="weak + lagging" color="text-accent-red" />
          </section>

          <Section
            title="Sector rotation vs Nifty50"
            icon={<Radar size={16} />}
            description="Relative strength ladder — where capital is rotating across sectors this week."
            rightSlot={<StatusBadge label={`timeframe ${scan?.config?.timeframe ?? "weekly"}`} variant="neutral" />}
          >
            <SectorRotation sectors={sectors} />
          </Section>

          <Section
            title="Cross-asset & quadrant census"
            icon={<Compass size={16} />}
            description="Asset-class winner drives the equity tilt; the stock-quadrant census shows breadth."
          >
            <div className="grid gap-3 md:grid-cols-4">
              <QuadrantCard q="leading" n={census.leading} note="strong + accelerating" />
              <QuadrantCard q="improving" n={census.improving} note="weak but accelerating" />
              <QuadrantCard q="weakening" n={census.weakening} note="strong but decelerating" />
              <QuadrantCard q="lagging" n={census.lagging} note="weak + decelerating" />
            </div>
          </Section>
        </div>
      ) : null}

      {/* ── Candidates ───────────────────────────────────────────────────── */}
      {activeTab === "candidates" ? (
        <div className="space-y-4">
          <Section
            title="Relative-Rotation Graph"
            icon={<Radar size={16} />}
            description="Each scored name plotted by relative strength (x) and MACD-histogram momentum (y). Ringed dots are on the watchlist."
          >
            <RrgScatter points={rrgPoints} />
          </Section>

          <Section
            title="Ranked alpha candidates"
            icon={<TrendingUp size={16} />}
            description="Top-of-book by composite alpha score. Gate ✓ = passed the top-N watchlist cut."
            rightSlot={<StatusBadge label={`${ranked.length} scored`} variant="neutral" />}
          >
            <CandidateTable rows={ranked.slice(0, 60)} />
          </Section>

          {watchlist.length ? (
            <Section title="Watchlist — paper entries" icon={<Activity size={16} />}>
              <CandidateTable rows={watchlist} />
            </Section>
          ) : null}
        </div>
      ) : null}

      {/* ── Sectors ──────────────────────────────────────────────────────── */}
      {activeTab === "sectors" ? (
        <div className="space-y-4">
          <Section
            title="Sector winners vs Nifty50"
            icon={<Layers3 size={16} />}
            description="Distinct sectors with RRG quadrant, relative strength, and the count of leading stocks within each."
          >
            <SectorTable sectors={[...sectors].sort((a, b) => b.rs - a.rs)} />
          </Section>
          <Section title="Sector rotation ladder" icon={<Radar size={16} />}>
            <SectorRotation sectors={sectors} />
          </Section>
        </div>
      ) : null}

      {/* ── Performance ──────────────────────────────────────────────────── */}
      {activeTab === "performance" ? (
        <div className="space-y-4">
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
            <MetricTile label="Open book" value={String(paperSum?.open_positions ?? 0)} detail={`${paperSum?.closed_positions ?? 0} closed`} />
            <MetricTile label="Total P&L" value={formatSignedMoney(paperSum?.total_pnl)} detail={`real ${formatSignedMoney(paperSum?.realized_pnl)}`} color={tone(paperSum?.total_pnl)} />
            <MetricTile label="Unrealized" value={formatSignedMoney(paperSum?.unrealized_pnl)} color={tone(paperSum?.unrealized_pnl)} />
            <MetricTile label="Equity" value={formatSignedMoney(paperSum?.total_equity)} detail={`init ${formatSignedMoney(paperSum?.initial_capital)}`} />
            <MetricTile label="Return" value={totalReturn != null ? formatPct(totalReturn / 100, 2) : "—"} color={tone(totalReturn)} />
            <MetricTile label="Available" value={formatSignedMoney(paperSum?.available_capital)} detail={`reserved ${formatSignedMoney(paperSum?.reserved_margin)}`} />
          </section>
          <PaperPerformance summary={paperSum} positions={positions} />
        </div>
      ) : null}
    </DeskShell>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function QuadrantCard({ q, n, note }: { q: string; n: number; note: string }) {
  const col = QUADRANT_COLOR[q] || "rgb(var(--accent-blue))";
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
      <div className="flex items-center justify-between">
        <span className="text-[10.5px] uppercase tracking-[0.16em]" style={{ color: col }}>
          {q}
        </span>
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: col }} />
      </div>
      <div className="mt-1 text-2xl font-semibold text-text-primary">{n}</div>
      <div className="text-[11px] text-text-muted">{note}</div>
    </div>
  );
}

function CandidateTable({ rows }: { rows: ScanResult[] }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {["#", "Symbol", "Sector", "Quadrant", "Alpha", "Bias", "RS%", "MACD", "RSI", "Wk trend", "Close", "Trend", "Gate"].map((h, i) => (
              <th
                key={h}
                className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${
                  i <= 2 ? "text-left" : "text-right"
                }`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((r, i) => {
              const col = QUADRANT_COLOR[r.stock_quadrant ?? ""] || "rgb(var(--accent-blue))";
              return (
                <tr key={r.instrument} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-1.5 text-left font-mono text-[11px] text-text-muted">{i + 1}</td>
                  <td className="px-2.5 py-1.5 text-left font-mono text-[12px] font-semibold text-text-primary">
                    {r.instrument}
                    {r.is_watchlist ? <span className="ml-1.5 text-accent-amber" title="on watchlist">★</span> : null}
                  </td>
                  <td className="px-2.5 py-1.5 text-left text-[11px] text-text-secondary">{(r.sector_code ?? "—").replace(/_/g, " ")}</td>
                  <td className="px-2.5 py-1.5 text-right">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]" style={{ color: col, background: `${col}1f` }}>
                      {r.stock_quadrant ?? "—"}
                    </span>
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-primary">{formatNumber(r.composite_alpha_score, 1)}</td>
                  <td className="px-2.5 py-1.5 text-right">
                    <StatusBadge label={r.directional_bias ?? "neutral"} variant={biasVariant(r.directional_bias)} />
                  </td>
                  <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(r.stock_rs_pct)}`}>{formatNumber(r.stock_rs_pct, 2)}</td>
                  <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${r.macd_bullish ? "text-accent-green" : "text-accent-red"}`} title={r.macd_meta?.label ?? ""}>
                    {formatNumber(r.macd_hist, 3)}
                    {r.macd_meta?.cross_today ? <span className="ml-1 text-[9px] text-accent-amber">×</span> : null}
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary" title={r.rsi_meta?.label ?? ""}>{formatNumber(r.rsi_14, 1)}</td>
                  <td className={`px-2.5 py-1.5 text-right text-[11px] ${trendTone(r.weekly_trend)}`}>{r.weekly_trend ?? "—"}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{formatNumber(r.latest_close, 1)}</td>
                  <td className="px-2.5 py-1.5 text-right">
                    <div className="ml-auto w-[96px]">
                      <Sparkline values={r.recent_closes_30d} />
                    </div>
                  </td>
                  <td className="px-2.5 py-1.5 text-right">
                    {r.gate_passed ? <span className="text-accent-green">✓</span> : <span className="text-text-muted">·</span>}
                  </td>
                </tr>
              );
            })
          ) : (
            <tr>
              <td colSpan={13} className="px-2.5 py-6 text-center text-sm text-text-muted">No candidates</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SectorTable({ sectors }: { sectors: SectorRow[] }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {["Sector", "Quadrant", "RS% vs Nifty50", "Leaders", "Scored", "Breadth"].map((h, i) => (
              <th key={h} className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${i === 0 ? "text-left" : "text-right"}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sectors.length ? (
            sectors.map((s) => {
              const col = QUADRANT_COLOR[s.quadrant] || "rgb(var(--accent-blue))";
              const breadth = s.count ? s.leaders / s.count : 0;
              return (
                <tr key={s.code} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-1.5 text-left font-medium text-text-primary">{s.code.replace(/_/g, " ")}</td>
                  <td className="px-2.5 py-1.5 text-right">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]" style={{ color: col, background: `${col}1f` }}>
                      {s.quadrant}
                    </span>
                  </td>
                  <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(s.rs)}`}>{s.rs >= 0 ? "+" : ""}{s.rs.toFixed(2)}%</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-accent-green">{s.leaders}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{s.count}</td>
                  <td className="px-2.5 py-1.5 text-right">
                    <div className="ml-auto h-2 w-24 overflow-hidden rounded-full bg-bg-primary/40">
                      <div className="h-full rounded-full" style={{ width: `${breadth * 100}%`, background: col, opacity: 0.7 }} />
                    </div>
                  </td>
                </tr>
              );
            })
          ) : (
            <tr>
              <td colSpan={6} className="px-2.5 py-6 text-center text-sm text-text-muted">No sectors</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
