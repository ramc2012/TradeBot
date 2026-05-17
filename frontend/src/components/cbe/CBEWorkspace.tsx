"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Database,
  LineChart as LineChartIcon,
  Play,
  Radar,
  RefreshCw,
  Search,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  describeApiError,
  getCBEInstrumentAnalytics,
  getCBELatestScan,
  getCBEUniverse,
  runCBEScan,
} from "@/lib/api";

type CBESource = "project_timescale";
type DetailTab = "overview" | "price" | "options" | "evidence";

type CBEScanRow = {
  instrument: string;
  composite_score: number;
  directional_bias: "bullish" | "bearish" | "neutral" | string;
  bias_conviction: number;
  f1_vc_score: number;
  f2_omp_score: number;
  f3_csmd_score: number;
  f4_cp_score: number;
  f5_mp_score: number;
  details?: {
    f1_vc?: Record<string, unknown>;
    f2_omp?: Record<string, unknown>;
    f3_csmd?: Record<string, unknown>;
    f4_cp?: Record<string, unknown>;
    f5_mp?: Record<string, unknown>;
  };
};

type CBEScanPayload = {
  source: CBESource;
  source_status?: Record<string, unknown>;
  scan_date: string | null;
  universe_size: number;
  scored_count: number;
  watchlist_count: number;
  results: CBEScanRow[];
  watchlist: CBEScanRow[];
};

type CBEInstrumentAnalytics = {
  symbol: string;
  available: boolean;
  scan_date: string;
  source_status?: Record<string, unknown>;
  score?: CBEScanRow | null;
  ohlc: Array<Record<string, unknown>>;
  option_chain: Array<Record<string, unknown>>;
  iv_history: Array<Record<string, unknown>>;
  pcr_history: Array<Record<string, unknown>>;
  sector_returns: Array<Record<string, unknown>>;
};

const FEATURE_COLUMNS = [
  { key: "f1_vc_score", label: "VC", name: "Vol compression" },
  { key: "f2_omp_score", label: "OMP", name: "Options" },
  { key: "f3_csmd_score", label: "CSMD", name: "Cross-section" },
  { key: "f4_cp_score", label: "CP", name: "Catalyst" },
  { key: "f5_mp_score", label: "MP", name: "Microstructure" },
] as const;

const TABS: Array<{ key: DetailTab; label: string; icon: LucideIcon }> = [
  { key: "overview", label: "Overview", icon: Radar },
  { key: "price", label: "Price", icon: LineChartIcon },
  { key: "options", label: "Options", icon: BarChart3 },
  { key: "evidence", label: "Evidence", icon: SlidersHorizontal },
];

function formatNumber(value: unknown, digits = 2) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  return numeric.toFixed(digits);
}

function formatCompact(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0, notation: "compact" }).format(numeric);
}

function toneForBias(bias: string) {
  if (bias === "bullish") return "border-accent-green/35 bg-accent-green/10 text-accent-green";
  if (bias === "bearish") return "border-accent-red/35 bg-accent-red/10 text-accent-red";
  return "border-bg-border bg-bg-secondary/35 text-text-secondary";
}

function scoreTone(score: number) {
  if (score >= 7) return "text-accent-green";
  if (score >= 5.5) return "text-accent-amber";
  return "text-text-secondary";
}

function Metric({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string;
  value: string;
  detail: string;
  icon: LucideIcon;
}) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/24 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">{label}</div>
        <Icon size={16} className="text-accent-blue" />
      </div>
      <div className="mt-2 font-mono text-lg font-semibold text-text-primary">{value}</div>
      <div className="mt-1 text-[11px] text-text-muted">{detail}</div>
    </div>
  );
}

function FeatureBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value * 10));
  return (
    <div className="h-1.5 w-full rounded-full bg-bg-primary/70">
      <div
        className={clsx(
          "h-full rounded-full",
          value >= 7 ? "bg-accent-green" : value >= 5 ? "bg-accent-amber" : "bg-accent-blue",
        )}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex h-[190px] items-center justify-center rounded-lg border border-bg-border bg-bg-primary/24 text-sm text-text-muted">
      {text}
    </div>
  );
}

function chartTooltipStyle() {
  return {
    backgroundColor: "#101522",
    border: "1px solid rgba(95, 109, 135, 0.45)",
    borderRadius: 8,
    color: "#dbe4f0",
  };
}

function scoreToScanRow(score?: Record<string, any> | null): CBEScanRow | undefined {
  if (!score) return undefined;
  return {
    instrument: String(score.instrument || ""),
    composite_score: Number(score.composite_score || 0),
    directional_bias: String(score.directional_bias || "neutral"),
    bias_conviction: Number(score.bias_conviction || 0),
    f1_vc_score: Number(score.f1_vc?.score || 0),
    f2_omp_score: Number(score.f2_omp?.score || 0),
    f3_csmd_score: Number(score.f3_csmd?.score || 0),
    f4_cp_score: Number(score.f4_cp?.score || 0),
    f5_mp_score: Number(score.f5_mp?.score || 0),
    details: {
      f1_vc: score.f1_vc,
      f2_omp: score.f2_omp,
      f3_csmd: score.f3_csmd,
      f4_cp: score.f4_cp,
      f5_mp: score.f5_mp,
    },
  };
}

function ScanTable({
  rows,
  selected,
  onSelect,
}: {
  rows: CBEScanRow[];
  selected?: string;
  onSelect: (symbol: string) => void;
}) {
  if (!rows.length) {
    return <EmptyState text="No scan rows available." />;
  }

  return (
    <div className="overflow-hidden rounded-lg border border-bg-border">
      <div className="max-h-[560px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-sm">
          <thead className="sticky top-0 z-10 bg-bg-card text-[11px] uppercase tracking-[0.12em] text-text-muted">
            <tr>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Rank</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Symbol</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Score</th>
              <th className="border-b border-bg-border px-3 py-2 text-left font-semibold">Bias</th>
              <th className="border-b border-bg-border px-3 py-2 text-right font-semibold">Conv</th>
              {FEATURE_COLUMNS.map((column) => (
                <th key={column.key} className="border-b border-bg-border px-3 py-2 text-right font-semibold">
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const active = selected === row.instrument;
              return (
                <tr
                  key={row.instrument}
                  onClick={() => onSelect(row.instrument)}
                  className={clsx(
                    "cursor-pointer border-b border-bg-border/70 transition-colors",
                    active ? "bg-accent-blue/12" : "bg-bg-secondary/10 hover:bg-bg-hover/60",
                  )}
                >
                  <td className="border-b border-bg-border/60 px-3 py-2 font-mono text-xs text-text-muted">{index + 1}</td>
                  <td className="border-b border-bg-border/60 px-3 py-2 font-semibold text-text-primary">{row.instrument}</td>
                  <td className={clsx("border-b border-bg-border/60 px-3 py-2 text-right font-mono", scoreTone(row.composite_score))}>
                    {formatNumber(row.composite_score)}
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2">
                    <span className={clsx("rounded-md border px-2 py-1 text-[11px] uppercase", toneForBias(row.directional_bias))}>
                      {row.directional_bias}
                    </span>
                  </td>
                  <td className="border-b border-bg-border/60 px-3 py-2 text-right font-mono text-text-secondary">
                    {formatNumber(row.bias_conviction)}
                  </td>
                  {FEATURE_COLUMNS.map((column) => (
                    <td key={column.key} className="border-b border-bg-border/60 px-3 py-2 text-right">
                      <div className="flex min-w-[54px] flex-col items-end gap-1">
                        <span className="font-mono text-xs text-text-secondary">{formatNumber(row[column.key], 1)}</span>
                        <FeatureBar value={Number(row[column.key] || 0)} />
                      </div>
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FeatureScoreChart({ row }: { row?: CBEScanRow }) {
  if (!row) return <EmptyState text="Run a scan or select an instrument." />;
  const data = FEATURE_COLUMNS.map((column) => ({
    name: column.label,
    score: Number(row[column.key] || 0),
  }));
  return (
    <div className="h-[210px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 12, right: 8, left: -28, bottom: 0 }}>
          <CartesianGrid stroke="rgba(95,109,135,0.22)" vertical={false} />
          <XAxis dataKey="name" tick={{ fill: "#7f8ba3", fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis domain={[0, 10]} tick={{ fill: "#7f8ba3", fontSize: 11 }} axisLine={false} tickLine={false} />
          <Tooltip contentStyle={chartTooltipStyle()} formatter={(value) => formatNumber(value, 2)} />
          <Bar dataKey="score" fill="#60a5fa" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function PriceChart({ analytics }: { analytics?: CBEInstrumentAnalytics }) {
  const data = analytics?.ohlc || [];
  if (!data.length) return <EmptyState text="No price history for the selected instrument." />;
  return (
    <div className="h-[260px]">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 16, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(95,109,135,0.22)" vertical={false} />
          <XAxis dataKey="date" tick={{ fill: "#7f8ba3", fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={24} />
          <YAxis yAxisId="price" tick={{ fill: "#7f8ba3", fontSize: 11 }} tickLine={false} axisLine={false} width={58} />
          <YAxis yAxisId="volume" orientation="right" tick={{ fill: "#7f8ba3", fontSize: 11 }} tickLine={false} axisLine={false} width={44} tickFormatter={formatCompact} />
          <Tooltip contentStyle={chartTooltipStyle()} formatter={(value, name) => [formatNumber(value, name === "volume" ? 0 : 2), name]} />
          <Bar yAxisId="volume" dataKey="volume" fill="rgba(96,165,250,0.22)" radius={[3, 3, 0, 0]} />
          <Line yAxisId="price" type="monotone" dataKey="close" stroke="#34d399" strokeWidth={2} dot={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function OptionOiChart({ analytics }: { analytics?: CBEInstrumentAnalytics }) {
  const data = (analytics?.option_chain || []).slice(0, 70);
  if (!data.length) return <EmptyState text="No option chain snapshot for the selected instrument." />;
  return (
    <div className="h-[260px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 12, right: 16, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(95,109,135,0.22)" vertical={false} />
          <XAxis dataKey="strike" tick={{ fill: "#7f8ba3", fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={18} />
          <YAxis tick={{ fill: "#7f8ba3", fontSize: 11 }} tickLine={false} axisLine={false} tickFormatter={formatCompact} />
          <Tooltip contentStyle={chartTooltipStyle()} formatter={(value) => formatCompact(value)} />
          <Bar dataKey="call_oi" name="Call OI" fill="#60a5fa" radius={[3, 3, 0, 0]} />
          <Bar dataKey="put_oi" name="Put OI" fill="#34d399" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function IvPcrChart({ analytics }: { analytics?: CBEInstrumentAnalytics }) {
  const data = useMemo(() => {
    const byDate = new Map<string, Record<string, unknown>>();
    for (const row of analytics?.iv_history || []) {
      const key = String(row.date);
      byDate.set(key, { ...(byDate.get(key) || { date: key }), iv: row.iv });
    }
    for (const row of analytics?.pcr_history || []) {
      const key = String(row.date);
      byDate.set(key, { ...(byDate.get(key) || { date: key }), pcr: row.pcr });
    }
    return Array.from(byDate.values());
  }, [analytics?.iv_history, analytics?.pcr_history]);
  if (!data.length) return <EmptyState text="No IV/PCR history for the selected instrument." />;
  return (
    <div className="h-[220px]">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 16, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(95,109,135,0.22)" vertical={false} />
          <XAxis dataKey="date" tick={{ fill: "#7f8ba3", fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={24} />
          <YAxis yAxisId="left" tick={{ fill: "#7f8ba3", fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis yAxisId="right" orientation="right" tick={{ fill: "#7f8ba3", fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip contentStyle={chartTooltipStyle()} formatter={(value) => formatNumber(value, 2)} />
          <Line yAxisId="left" type="monotone" dataKey="iv" name="IV" stroke="#f59e0b" strokeWidth={2} dot={false} />
          <Line yAxisId="right" type="monotone" dataKey="pcr" name="PCR" stroke="#a78bfa" strokeWidth={2} dot={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function DetailTabs({
  row,
  analytics,
  isLoading,
  activeTab,
  onTab,
}: {
  row?: CBEScanRow;
  analytics?: CBEInstrumentAnalytics;
  isLoading: boolean;
  activeTab: DetailTab;
  onTab: (tab: DetailTab) => void;
}) {
  const symbol = analytics?.symbol || row?.instrument;
  const scoreRow = row || scoreToScanRow(analytics?.score as Record<string, any> | null | undefined);
  return (
    <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Instrument</div>
          <div className="mt-1 text-lg font-semibold text-text-primary">{symbol || "--"}</div>
        </div>
        {scoreRow ? (
          <div className={clsx("rounded-md border px-2 py-1 text-xs uppercase", toneForBias(scoreRow.directional_bias))}>
            {scoreRow.directional_bias}
          </div>
        ) : null}
      </div>
      <div className="mb-3 grid grid-cols-4 gap-1 rounded-lg border border-bg-border bg-bg-primary/20 p-1">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              type="button"
              onClick={() => onTab(tab.key)}
              className={clsx(
                "inline-flex min-h-9 items-center justify-center gap-1 rounded-md px-2 text-xs font-semibold transition-colors",
                activeTab === tab.key ? "bg-accent-blue/18 text-accent-blue" : "text-text-muted hover:bg-bg-hover/60",
              )}
            >
              <Icon size={13} />
              <span>{tab.label}</span>
            </button>
          );
        })}
      </div>
      {isLoading ? (
        <EmptyState text="Loading instrument analytics..." />
      ) : activeTab === "overview" ? (
        <div className="space-y-3">
          <FeatureScoreChart row={scoreRow} />
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-2">
              <div className="text-text-muted">Score</div>
              <div className={clsx("mt-1 font-mono text-base font-semibold", scoreTone(Number(scoreRow?.composite_score || 0)))}>
                {formatNumber(scoreRow?.composite_score)}
              </div>
            </div>
            <div className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-2">
              <div className="text-text-muted">OHLC</div>
              <div className="mt-1 font-mono text-base font-semibold text-text-primary">{analytics?.ohlc?.length ?? "--"}</div>
            </div>
            <div className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-2">
              <div className="text-text-muted">Strikes</div>
              <div className="mt-1 font-mono text-base font-semibold text-text-primary">{analytics?.option_chain?.length ?? "--"}</div>
            </div>
          </div>
        </div>
      ) : activeTab === "price" ? (
        <div className="space-y-3">
          <PriceChart analytics={analytics} />
          <IvPcrChart analytics={analytics} />
        </div>
      ) : activeTab === "options" ? (
        <OptionOiChart analytics={analytics} />
      ) : (
        <div className="space-y-2">
          {FEATURE_COLUMNS.map((column) => {
            const detailKey = column.key.replace("_score", "").replace("f1_vc", "f1_vc").replace("f2_omp", "f2_omp") as keyof NonNullable<CBEScanRow["details"]>;
            const evidence = scoreRow?.details?.[detailKey];
            return (
              <div key={column.key} className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs font-semibold text-text-secondary">{column.name}</span>
                  <span className="font-mono text-sm text-text-primary">{formatNumber(scoreRow?.[column.key], 2)}</span>
                </div>
                <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-text-muted">
                  {evidence ? JSON.stringify(evidence, null, 2) : "No detailed evidence available."}
                </pre>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function CBEWorkspace() {
  const source: CBESource = "project_timescale";
  const [watchlistMinScore, setWatchlistMinScore] = useState(7);
  const [watchlistMaxSize, setWatchlistMaxSize] = useState(15);
  const [selectedSymbol, setSelectedSymbol] = useState<string | undefined>();
  const [instrumentSearch, setInstrumentSearch] = useState("");
  const [activeTab, setActiveTab] = useState<DetailTab>("overview");

  const universeQuery = useQuery({
    queryKey: ["cbeUniverse"],
    queryFn: () => getCBEUniverse().then((response) => response.data as { count: number; symbols: string[] }),
    staleTime: 5 * 60_000,
  });
  const latestQuery = useQuery({
    queryKey: ["cbeLatest", source],
    queryFn: () => getCBELatestScan(source).then((response) => response.data as CBEScanPayload),
    staleTime: 30_000,
  });

  const scanMutation = useMutation({
    mutationFn: () =>
      runCBEScan({
        source,
        watchlist_min_score: watchlistMinScore,
        watchlist_max_size: watchlistMaxSize,
      }).then((response) => response.data as CBEScanPayload),
    onSuccess: (payload) => {
      setSelectedSymbol(payload.watchlist[0]?.instrument || payload.results[0]?.instrument);
    },
  });

  const payload = scanMutation.data?.source === source ? scanMutation.data : latestQuery.data;
  const rankedRows = payload?.results || [];
  const watchlistRows = payload?.watchlist || [];
  const allSymbols = useMemo(() => {
    const set = new Set<string>();
    for (const symbol of universeQuery.data?.symbols || []) set.add(symbol);
    for (const row of rankedRows) set.add(row.instrument);
    return Array.from(set).sort();
  }, [rankedRows, universeQuery.data?.symbols]);
  const filteredSymbols = useMemo(() => {
    const query = instrumentSearch.trim().toUpperCase();
    if (!query) return allSymbols;
    return allSymbols.filter((symbol) => symbol.includes(query));
  }, [allSymbols, instrumentSearch]);
  const hasScanPayload = Boolean(payload?.scan_date && rankedRows.length);
  const selectedRow = useMemo(
    () => rankedRows.find((row) => row.instrument === selectedSymbol) || watchlistRows[0] || rankedRows[0],
    [rankedRows, selectedSymbol, watchlistRows],
  );
  const activeSymbol = selectedSymbol || selectedRow?.instrument || allSymbols[0];
  const analyticsQuery = useQuery({
    queryKey: ["cbeInstrumentAnalytics", activeSymbol],
    queryFn: () => getCBEInstrumentAnalytics(activeSymbol || "").then((response) => response.data as CBEInstrumentAnalytics),
    enabled: Boolean(activeSymbol),
    staleTime: 60_000,
  });
  const error = scanMutation.error ? describeApiError(scanMutation.error, "CBE scan failed") : "";

  useEffect(() => {
    if (!selectedSymbol && activeSymbol) {
      setSelectedSymbol(activeSymbol);
    }
  }, [activeSymbol, selectedSymbol]);

  return (
    <div className="mx-auto flex w-full max-w-[1540px] flex-col gap-3">
      <header className="rounded-lg border border-bg-border bg-bg-secondary/28 px-4 py-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <Radar size={18} className="text-accent-blue" />
              <h1 className="text-lg font-semibold text-text-primary">CBE Scanner</h1>
            </div>
            <div className="mt-1 text-xs text-text-muted">
              {hasScanPayload ? `${payload?.scan_date} · ${payload?.source}` : "Compression-Before-Expansion watchlist"}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex min-w-[220px] items-center gap-2 rounded-lg border border-bg-border bg-bg-primary/30 px-2 py-1.5 text-xs text-text-secondary">
              <Search size={14} />
              <input
                value={instrumentSearch}
                onChange={(event) => setInstrumentSearch(event.target.value)}
                placeholder="Find F&O symbol"
                className="min-w-0 flex-1 bg-transparent text-text-primary outline-none placeholder:text-text-muted"
              />
            </label>
            <select
              value={activeSymbol || ""}
              onChange={(event) => {
                setSelectedSymbol(event.target.value);
                setActiveTab("overview");
              }}
              className="min-h-9 min-w-[160px] rounded-lg border border-bg-border bg-bg-primary/70 px-3 text-sm font-semibold text-text-primary outline-none focus:border-accent-blue"
            >
              {filteredSymbols.map((symbol) => (
                <option key={symbol} value={symbol}>
                  {symbol}
                </option>
              ))}
            </select>
            <div className="inline-flex items-center gap-2 rounded-lg border border-accent-green/30 bg-accent-green/10 px-3 py-2 text-xs font-semibold text-accent-green">
              <Database size={14} />
              Project Data
            </div>
            <label className="flex items-center gap-2 rounded-lg border border-bg-border bg-bg-primary/30 px-2 py-1.5 text-xs text-text-secondary">
              <SlidersHorizontal size={14} />
              Min
              <input
                type="number"
                min={0}
                max={10}
                step={0.1}
                value={watchlistMinScore}
                onChange={(event) => setWatchlistMinScore(Number(event.target.value))}
                className="w-16 rounded-md border border-bg-border bg-bg-secondary px-2 py-1 font-mono text-text-primary outline-none focus:border-accent-blue"
              />
            </label>
            <label className="flex items-center gap-2 rounded-lg border border-bg-border bg-bg-primary/30 px-2 py-1.5 text-xs text-text-secondary">
              Max
              <input
                type="number"
                min={1}
                max={100}
                step={1}
                value={watchlistMaxSize}
                onChange={(event) => setWatchlistMaxSize(Number(event.target.value))}
                className="w-16 rounded-md border border-bg-border bg-bg-secondary px-2 py-1 font-mono text-text-primary outline-none focus:border-accent-blue"
              />
            </label>
            <button
              type="button"
              onClick={() => scanMutation.mutate()}
              disabled={scanMutation.isPending}
              className="inline-flex items-center gap-2 rounded-lg border border-accent-blue/40 bg-accent-blue/16 px-3 py-2 text-sm font-semibold text-accent-blue transition-colors hover:bg-accent-blue/24 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {scanMutation.isPending ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}
              Run
            </button>
          </div>
        </div>
      </header>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Metric label="Universe" value={String(payload?.universe_size ?? universeQuery.data?.count ?? "--")} detail="F&O underlyings loaded" icon={Database} />
        <Metric label="Scored" value={String(payload?.scored_count ?? "--")} detail="Rows with enough OHLC history" icon={CheckCircle2} />
        <Metric label="Watchlist" value={String(payload?.watchlist_count ?? "--")} detail={`Score >= ${formatNumber(watchlistMinScore, 1)}`} icon={Radar} />
        <Metric label="Data" value={String(payload?.source_status?.ohlc_symbols ?? analyticsQuery.data?.source_status?.ohlc_symbols ?? "--")} detail="OHLC symbols loaded" icon={SlidersHorizontal} />
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-accent-red/35 bg-accent-red/10 px-4 py-3 text-sm text-accent-red">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_430px]">
        <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-text-primary">Ranked Scan</div>
              <div className="text-xs text-text-muted">
                {rankedRows.length ? `${rankedRows.length} ranked rows · ${watchlistRows.length} cleared watchlist` : "No rankings yet"}
              </div>
            </div>
            {scanMutation.isPending ? (
              <span className="inline-flex items-center gap-2 rounded-md border border-accent-amber/30 bg-accent-amber/10 px-2 py-1 text-xs text-accent-amber">
                <RefreshCw size={13} className="animate-spin" />
                Running
              </span>
            ) : null}
          </div>
          <ScanTable rows={rankedRows} selected={activeSymbol} onSelect={setSelectedSymbol} />
        </section>
        <div className="space-y-3">
          <section className="rounded-lg border border-bg-border bg-bg-secondary/16 p-3">
            <div className="mb-3 text-sm font-semibold text-text-primary">Watchlist</div>
            <div className="max-h-[230px] space-y-2 overflow-auto pr-1">
              {watchlistRows.length ? (
                watchlistRows.map((row) => (
                  <button
                    key={row.instrument}
                    type="button"
                    onClick={() => setSelectedSymbol(row.instrument)}
                    className={clsx(
                      "flex w-full items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left transition-colors",
                      activeSymbol === row.instrument
                        ? "border-accent-blue/40 bg-accent-blue/12"
                        : "border-bg-border bg-bg-primary/24 hover:border-accent-blue/35 hover:bg-accent-blue/8",
                    )}
                  >
                    <div>
                      <div className="font-semibold text-text-primary">{row.instrument}</div>
                      <div className="mt-1 text-[11px] text-text-muted">{row.directional_bias}</div>
                    </div>
                    <div className={clsx("font-mono text-sm font-semibold", scoreTone(row.composite_score))}>
                      {formatNumber(row.composite_score)}
                    </div>
                  </button>
                ))
              ) : (
                <div className="rounded-lg border border-bg-border bg-bg-primary/24 px-3 py-5 text-sm text-text-muted">
                  No watchlist rows.
                </div>
              )}
            </div>
          </section>
          <DetailTabs
            row={selectedRow?.instrument === activeSymbol ? selectedRow : undefined}
            analytics={analyticsQuery.data}
            isLoading={analyticsQuery.isFetching}
            activeTab={activeTab}
            onTab={setActiveTab}
          />
        </div>
      </div>
    </div>
  );
}
