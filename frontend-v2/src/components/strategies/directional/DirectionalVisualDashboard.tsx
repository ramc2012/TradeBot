"use client";

import { useMemo } from "react";
import { clsx } from "clsx";
import { useQueries } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { Activity, BarChart3, Crosshair, Layers3, RefreshCw } from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  tone,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

const MAX_STOCKS_TO_SCAN = 64;

type ChainLevel = {
  strike?: number | null;
  distance_pct?: number | null;
  net_gamma_exposure?: number | null;
};

type ChainVisualPayload = {
  available?: boolean;
  underlying?: string;
  expiry?: string | null;
  spot?: number | null;
  atm_iv?: number | null;
  pcr_oi?: number | null;
  pcr_volume?: number | null;
  max_pain?: number | null;
  dealer_gex_total?: number | null;
  gex_total?: number | null;
  total_ce_oi_change?: number | null;
  total_pe_oi_change?: number | null;
  oi_build_ce?: Record<string, number>;
  oi_build_pe?: Record<string, number>;
  key_levels?: {
    call_wall?: ChainLevel | null;
    put_wall?: ChainLevel | null;
    abs_gamma?: ChainLevel | null;
    zero_gamma?: number | null;
    vol_trigger?: number | null;
    dealer_gex_total?: number | null;
    gamma_regime?: string | null;
  };
  trace_exposures?: Array<{
    strike: number;
    moneyness_pct: number;
    net_gamma_exposure: number;
    net_delta_exposure?: number | null;
  }>;
  unusual_activity?: Array<{
    strike: number;
    option_type: string;
    volume_to_oi: number;
    oi_change: number;
    score: number;
    flags: string[];
  }>;
  straddle?: {
    expected_move_pct?: number | null;
  };
  cache_status?: {
    default_expiry?: string | null;
    poll_running?: boolean;
  };
};

type VisualRow = {
  symbol: string;
  kind: "index" | "stock";
  available: boolean;
  spot: number | null;
  atmIv: number | null;
  pcr: number | null;
  maxPain: number | null;
  expectedMovePct: number | null;
  zeroGamma: number | null;
  flipDistancePct: number | null;
  absFlipDistancePct: number | null;
  callWall: number | null;
  putWall: number | null;
  netGex: number | null;
  gammaRegime: string;
  oiChange: number | null;
  longBuildup: number;
  shortBuildup: number;
  shortCover: number;
  longUnwind: number;
  positiveGammaStrike: number | null;
  positiveGamma: number | null;
  negativeGammaStrike: number | null;
  negativeGamma: number | null;
  unusualScore: number | null;
  payload?: ChainVisualPayload;
};

function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "-";
  const abs = Math.abs(n);
  if (abs >= 1e10) return `${(n / 1e10).toFixed(2)} kCr`;
  if (abs >= 1e7) return `${(n / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${(n / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)} K`;
  return n.toFixed(0);
}

function safeNumber(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function stateLabel(value?: string | null): string {
  return String(value || "unknown").replaceAll("_", " ");
}

function gammaFill(regime: string, value?: number | null): string {
  const label = regime.toLowerCase();
  if (label.includes("positive") || (value ?? 0) > 0) return "#00d4a3";
  if (label.includes("negative") || (value ?? 0) < 0) return "#f59e0b";
  return "#64748b";
}

function rowFromPayload(symbol: string, kind: "index" | "stock", payload?: ChainVisualPayload): VisualRow {
  const spot = safeNumber(payload?.spot);
  const key = payload?.key_levels || {};
  const zeroGamma = safeNumber(key.zero_gamma);
  const flipDistancePct = spot && zeroGamma ? ((zeroGamma - spot) / spot) * 100 : null;
  const trace = payload?.trace_exposures || [];
  const positive = trace.length ? trace.reduce((best, row) => (
    (row.net_gamma_exposure || 0) > (best?.net_gamma_exposure ?? Number.NEGATIVE_INFINITY) ? row : best
  ), trace[0]) : null;
  const negative = trace.length ? trace.reduce((best, row) => (
    (row.net_gamma_exposure || 0) < (best?.net_gamma_exposure ?? Number.POSITIVE_INFINITY) ? row : best
  ), trace[0]) : null;
  const ceBuild = payload?.oi_build_ce || {};
  const peBuild = payload?.oi_build_pe || {};
  const unusual = payload?.unusual_activity || [];

  return {
    symbol,
    kind,
    available: Boolean(payload?.available),
    spot,
    atmIv: safeNumber(payload?.atm_iv),
    pcr: safeNumber(payload?.pcr_oi),
    maxPain: safeNumber(payload?.max_pain),
    expectedMovePct: safeNumber(payload?.straddle?.expected_move_pct),
    zeroGamma,
    flipDistancePct,
    absFlipDistancePct: flipDistancePct == null ? null : Math.abs(flipDistancePct),
    callWall: safeNumber(key.call_wall?.strike),
    putWall: safeNumber(key.put_wall?.strike),
    netGex: safeNumber(payload?.dealer_gex_total ?? key.dealer_gex_total ?? payload?.gex_total),
    gammaRegime: stateLabel(key.gamma_regime),
    oiChange: safeNumber((payload?.total_ce_oi_change || 0) + (payload?.total_pe_oi_change || 0)),
    longBuildup: Number(ceBuild.long_buildup || 0) + Number(peBuild.long_buildup || 0),
    shortBuildup: Number(ceBuild.short_buildup || 0) + Number(peBuild.short_buildup || 0),
    shortCover: Number(ceBuild.short_cover || 0) + Number(peBuild.short_cover || 0),
    longUnwind: Number(ceBuild.long_unwind || 0) + Number(peBuild.long_unwind || 0),
    positiveGammaStrike: safeNumber(positive?.strike),
    positiveGamma: safeNumber(positive?.net_gamma_exposure),
    negativeGammaStrike: safeNumber(negative?.strike),
    negativeGamma: safeNumber(negative?.net_gamma_exposure),
    unusualScore: unusual.length ? Math.max(...unusual.map((row) => Number(row.score || 0))) : null,
    payload,
  };
}

function selectScanSymbols(indices: string[], stocks: string[], selected?: string): Array<{ symbol: string; kind: "index" | "stock" }> {
  const stockSet = new Set(stocks);
  const rows: Array<{ symbol: string; kind: "index" | "stock" }> = [];
  for (const symbol of indices) rows.push({ symbol, kind: "index" });
  for (const symbol of stocks.slice(0, MAX_STOCKS_TO_SCAN)) rows.push({ symbol, kind: "stock" });
  if (selected && !rows.some((row) => row.symbol === selected)) {
    rows.unshift({ symbol: selected, kind: stockSet.has(selected) ? "stock" : "index" });
  }
  return Array.from(new Map(rows.map((row) => [row.symbol, row])).values());
}

export default function DirectionalVisualDashboard({
  indices,
  stocks,
  selected,
  onSelect,
}: {
  indices: string[];
  stocks: string[];
  selected?: string;
  onSelect?: (symbol: string) => void;
}) {
  const scanSymbols = useMemo(() => selectScanSymbols(indices, stocks, selected), [indices, stocks, selected]);
  const queries = useQueries({
    queries: scanSymbols.map(({ symbol }) => ({
      queryKey: ["directional", "visual-chain", symbol],
      queryFn: async () => (
        await apiClient.get("/api/directional-options/chain-analytics", {
          params: { underlying: symbol },
        })
      ).data as ChainVisualPayload,
      refetchInterval: REFRESH_MS.summary,
      staleTime: 45_000,
      refetchOnWindowFocus: false,
    })),
  });

  const rows = scanSymbols.map((item, idx) => rowFromPayload(item.symbol, item.kind, queries[idx].data));
  const availableRows = rows.filter((row) => row.available);
  const indexRows = availableRows.filter((row) => row.kind === "index");
  const stockRows = availableRows.filter((row) => row.kind === "stock");
  const loading = queries.some((query) => query.isFetching);
  const cacheHitRate = scanSymbols.length ? availableRows.length / scanSymbols.length : 0;

  const gammaFlipRows = [...availableRows]
    .filter((row) => row.flipDistancePct != null)
    .sort((a, b) => (a.absFlipDistancePct ?? 999) - (b.absFlipDistancePct ?? 999))
    .slice(0, 24);
  const gexIndexRows = [...indexRows].sort((a, b) => Math.abs(b.netGex || 0) - Math.abs(a.netGex || 0));
  const stockOiLeaders = [...stockRows]
    .filter((row) => row.oiChange != null)
    .sort((a, b) => Math.abs(b.oiChange || 0) - Math.abs(a.oiChange || 0))
    .slice(0, 14);
  const compassIndex = indexRows.filter((row) => row.pcr != null && row.atmIv != null).map((row) => ({
    ...row,
    atmIvPct: (row.atmIv || 0) * 100,
    gexAbs: Math.max(Math.abs(row.netGex || 0), 1),
  }));
  const compassStocks = stockRows.filter((row) => row.pcr != null && row.atmIv != null).slice(0, 80).map((row) => ({
    ...row,
    atmIvPct: (row.atmIv || 0) * 100,
    gexAbs: Math.max(Math.abs(row.netGex || 0), 1),
  }));
  const keyScannerRows = [...availableRows]
    .filter((row) => row.zeroGamma != null || row.callWall != null || row.putWall != null)
    .sort((a, b) => (a.absFlipDistancePct ?? 999) - (b.absFlipDistancePct ?? 999))
    .slice(0, 40);
  const positiveGammaRows = [...availableRows]
    .filter((row) => row.positiveGamma != null)
    .sort((a, b) => (b.positiveGamma || 0) - (a.positiveGamma || 0))
    .slice(0, 8);
  const negativeGammaRows = [...availableRows]
    .filter((row) => row.negativeGamma != null)
    .sort((a, b) => (a.negativeGamma || 0) - (b.negativeGamma || 0))
    .slice(0, 8);
  const unusualFlowLeader = [...availableRows]
    .sort((a, b) => (b.unusualScore || 0) - (a.unusualScore || 0))[0];

  return (
    <div className="space-y-4">
      <Section
        title="Universe visual dashboard"
        icon={<BarChart3 size={16} />}
        rightSlot={
          <div className="flex flex-wrap items-center justify-end gap-2 text-[11px] text-text-muted">
            <span className="inline-flex items-center gap-1.5">
              <RefreshCw size={12} className={clsx(loading && "animate-spin text-accent-blue")} />
              {availableRows.length}/{scanSymbols.length} cached
            </span>
            <StatusBadge label={`${formatPct(cacheHitRate, 0)} cache hit`} variant={cacheHitRate > 0.6 ? "success" : "warn"} />
          </div>
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile label="Cached chains" value={`${availableRows.length}`} detail={`${indexRows.length} indices · ${stockRows.length} stocks`} />
          <MetricTile label="Nearest gamma flip" value={gammaFlipRows[0]?.symbol || "-"} detail={gammaFlipRows[0]?.flipDistancePct != null ? formatPct(gammaFlipRows[0].flipDistancePct / 100, 2) : ""} color={tone(gammaFlipRows[0]?.flipDistancePct)} />
          <MetricTile label="Positive gamma" value={String(availableRows.filter((row) => row.gammaRegime.includes("positive")).length)} detail="pinning names" color="text-accent-green" />
          <MetricTile label="Negative gamma" value={String(availableRows.filter((row) => row.gammaRegime.includes("negative")).length)} detail="trend-amplifying names" color="text-accent-amber" />
          <MetricTile label="Stock OI leader" value={stockOiLeaders[0]?.symbol || "-"} detail={compact(stockOiLeaders[0]?.oiChange)} color={tone(stockOiLeaders[0]?.oiChange)} />
          <MetricTile label="Unusual flow" value={unusualFlowLeader?.symbol || "-"} detail="highest activity score" />
        </div>
      </Section>

      <Section title="Index snapshot" icon={<Activity size={16} />}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {indices.map((symbol) => {
            const row = rows.find((item) => item.symbol === symbol);
            return (
              <button
                key={symbol}
                type="button"
                onClick={() => onSelect?.(symbol)}
                className={clsx(
                  "rounded-2xl border border-bg-border bg-bg-secondary/24 p-3 text-left transition hover:border-accent-blue/40",
                  selected === symbol && "border-accent-blue/50 bg-accent-blue/8",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-text-primary">{symbol}</span>
                  <StatusBadge label={row?.available ? row.gammaRegime : "cache miss"} variant={row?.available ? "info" : "warn"} />
                </div>
                <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
                  <MiniStat label="Spot" value={formatNumber(row?.spot, 2)} />
                  <MiniStat label="PCR" value={formatNumber(row?.pcr, 2)} className={row?.pcr != null && row.pcr > 1.2 ? "text-accent-green" : row?.pcr != null && row.pcr < 0.8 ? "text-accent-red" : ""} />
                  <MiniStat label="ATM IV" value={row?.atmIv != null ? formatPct(row.atmIv, 1) : "-"} />
                  <MiniStat label="Zero G" value={formatNumber(row?.zeroGamma, 0)} />
                  <MiniStat label="Net GEX" value={compact(row?.netGex)} className={tone(row?.netGex)} />
                  <MiniStat label="1 sigma" value={row?.expectedMovePct != null ? formatPct(row.expectedMovePct, 2) : "-"} />
                </div>
              </button>
            );
          })}
        </div>
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Gamma Flip Map" icon={<Crosshair size={16} />}>
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={gammaFlipRows} layout="vertical" margin={{ left: 18, right: 16 }}>
                <CartesianGrid stroke="#1e2d45" horizontal={false} />
                <XAxis type="number" tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => `${Number(v).toFixed(1)}%`} />
                <YAxis type="category" dataKey="symbol" width={74} tick={{ fill: "#cbd5e1", fontSize: 10 }} />
                <Tooltip
                  formatter={(value: number) => [`${value.toFixed(2)}%`, "distance to flip"]}
                  labelFormatter={(label) => String(label)}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                <ReferenceLine x={0} stroke="#64748b" strokeDasharray="3 3" />
                <Bar dataKey="flipDistancePct" radius={[3, 3, 3, 3]}>
                  {gammaFlipRows.map((row) => <Cell key={row.symbol} fill={gammaFill(row.gammaRegime, row.netGex)} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>

        <Section title="Compass" icon={<Activity size={16} />}>
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ left: 4, right: 16, top: 8, bottom: 10 }}>
                <CartesianGrid stroke="#1e2d45" />
                <XAxis type="number" dataKey="pcr" name="PCR" domain={["auto", "auto"]} tick={{ fill: "#94a3b8", fontSize: 10 }} />
                <YAxis type="number" dataKey="atmIvPct" name="ATM IV" tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => `${Number(v).toFixed(0)}%`} />
                <ZAxis type="number" dataKey="gexAbs" range={[60, 420]} />
                <Tooltip
                  cursor={{ strokeDasharray: "3 3" }}
                  formatter={(value: number, name: string) => [name === "atmIvPct" ? `${value.toFixed(2)}%` : formatNumber(value, 2), name === "atmIvPct" ? "ATM IV" : name.toUpperCase()]}
                  labelFormatter={(_, payload) => payload?.[0]?.payload?.symbol || ""}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                <ReferenceLine x={1} stroke="#64748b" strokeDasharray="3 3" />
                <Scatter name="Indices" data={compassIndex} fill="#4f8cff" shape="diamond" />
                <Scatter name="Stocks" data={compassStocks} fill="#00d4a3" opacity={0.72} />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </Section>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="Net GEX by index" icon={<BarChart3 size={16} />}>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={gexIndexRows}>
                <CartesianGrid stroke="#1e2d45" vertical={false} />
                <XAxis dataKey="symbol" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => compact(v)} />
                <Tooltip formatter={(value: number) => [compact(value), "Net GEX"]} contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }} />
                <ReferenceLine y={0} stroke="#64748b" />
                <Bar dataKey="netGex">
                  {gexIndexRows.map((row) => <Cell key={row.symbol} fill={gammaFill(row.gammaRegime, row.netGex)} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>

        <Section title="Stock OI-change leaders" icon={<BarChart3 size={16} />}>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={stockOiLeaders}>
                <CartesianGrid stroke="#1e2d45" vertical={false} />
                <XAxis dataKey="symbol" tick={{ fill: "#94a3b8", fontSize: 10 }} interval={0} angle={-20} textAnchor="end" height={54} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => compact(v)} />
                <Tooltip formatter={(value: number) => [compact(value), "Net OI change"]} contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }} />
                <ReferenceLine y={0} stroke="#64748b" />
                <Bar dataKey="oiChange">
                  {stockOiLeaders.map((row) => <Cell key={row.symbol} fill={(row.oiChange || 0) >= 0 ? "#00d4a3" : "#ff4757"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <GammaExtremes title="Top positive gamma" rows={positiveGammaRows} positive onSelect={onSelect} />
        <GammaExtremes title="Top negative gamma" rows={negativeGammaRows} onSelect={onSelect} />
      </div>

      <Section title="Key Levels Scanner" icon={<Crosshair size={16} />}>
        <div className="overflow-x-auto">
          <table className="w-full text-[11.5px]">
            <thead className="text-[10px] uppercase tracking-wide text-text-muted">
              <tr className="border-b border-bg-border/50">
                <th className="px-2 py-2 text-left">Symbol</th>
                <th className="px-2 py-2 text-right">Spot</th>
                <th className="px-2 py-2 text-right">Put wall</th>
                <th className="px-2 py-2 text-right">Zero gamma</th>
                <th className="px-2 py-2 text-right">Call wall</th>
                <th className="px-2 py-2 text-right">Flip dist</th>
                <th className="px-2 py-2 text-right">Net GEX</th>
                <th className="px-2 py-2 text-left">Regime</th>
              </tr>
            </thead>
            <tbody>
              {keyScannerRows.map((row) => (
                <tr
                  key={`scanner-${row.symbol}`}
                  onClick={() => onSelect?.(row.symbol)}
                  className="cursor-pointer border-b border-bg-border/25 hover:bg-bg-primary/15"
                >
                  <td className="px-2 py-2 font-semibold text-text-primary">{row.symbol}</td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(row.spot, 2)}</td>
                  <td className="px-2 py-2 text-right font-mono text-accent-red">{formatNumber(row.putWall, 0)}</td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(row.zeroGamma, 0)}</td>
                  <td className="px-2 py-2 text-right font-mono text-accent-green">{formatNumber(row.callWall, 0)}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", tone(row.flipDistancePct))}>{row.flipDistancePct != null ? `${row.flipDistancePct.toFixed(2)}%` : "-"}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", tone(row.netGex))}>{compact(row.netGex)}</td>
                  <td className="px-2 py-2"><StatusBadge label={row.gammaRegime} variant={row.gammaRegime.includes("positive") ? "success" : "warn"} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Full cached universe scan" icon={<Layers3 size={16} />}>
        <div className="overflow-x-auto">
          <table className="w-full text-[11.5px]">
            <thead className="text-[10px] uppercase tracking-wide text-text-muted">
              <tr className="border-b border-bg-border/50">
                <th className="px-2 py-2 text-left">Symbol</th>
                <th className="px-2 py-2 text-left">Type</th>
                <th className="px-2 py-2 text-right">Spot</th>
                <th className="px-2 py-2 text-right">PCR</th>
                <th className="px-2 py-2 text-right">ATM IV</th>
                <th className="px-2 py-2 text-right">Max pain</th>
                <th className="px-2 py-2 text-right">1 sigma</th>
                <th className="px-2 py-2 text-right">OI change</th>
                <th className="px-2 py-2 text-left">Build</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={`scan-${row.symbol}`}
                  onClick={() => onSelect?.(row.symbol)}
                  className={clsx("cursor-pointer border-b border-bg-border/25 hover:bg-bg-primary/15", selected === row.symbol && "bg-accent-blue/8")}
                >
                  <td className="px-2 py-2 font-semibold text-text-primary">{row.symbol}</td>
                  <td className="px-2 py-2 text-text-muted">{row.kind}</td>
                  <td className="px-2 py-2 text-right font-mono">{row.available ? formatNumber(row.spot, 2) : "cache miss"}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", row.pcr != null && row.pcr > 1.2 ? "text-accent-green" : row.pcr != null && row.pcr < 0.8 ? "text-accent-red" : "")}>{formatNumber(row.pcr, 2)}</td>
                  <td className="px-2 py-2 text-right font-mono">{row.atmIv != null ? formatPct(row.atmIv, 1) : "-"}</td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(row.maxPain, 0)}</td>
                  <td className="px-2 py-2 text-right font-mono">{row.expectedMovePct != null ? formatPct(row.expectedMovePct, 2) : "-"}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", tone(row.oiChange))}>{compact(row.oiChange)}</td>
                  <td className="px-2 py-2 text-text-muted">
                    L {row.longBuildup} · S {row.shortBuildup} · C {row.shortCover} · U {row.longUnwind}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

function MiniStat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={clsx("mt-0.5 font-mono text-text-secondary", className)}>{value}</div>
    </div>
  );
}

function GammaExtremes({
  title,
  rows,
  positive,
  onSelect,
}: {
  title: string;
  rows: VisualRow[];
  positive?: boolean;
  onSelect?: (symbol: string) => void;
}) {
  return (
    <Section title={title} icon={<Activity size={16} />}>
      <div className="space-y-2">
        {rows.length === 0 ? (
          <div className="rounded-xl border border-bg-border bg-bg-primary/12 p-3 text-sm text-text-muted">No cached exposure rows yet.</div>
        ) : rows.map((row) => {
          const strike = positive ? row.positiveGammaStrike : row.negativeGammaStrike;
          const gamma = positive ? row.positiveGamma : row.negativeGamma;
          return (
            <button
              key={`${title}-${row.symbol}`}
              type="button"
              onClick={() => onSelect?.(row.symbol)}
              className="flex w-full items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-secondary/20 px-3 py-2 text-left hover:border-accent-blue/40"
            >
              <div>
                <div className="font-semibold text-text-primary">{row.symbol}</div>
                <div className="text-[10.5px] text-text-muted">strike {formatNumber(strike, 0)}</div>
              </div>
              <div className={clsx("font-mono text-sm", tone(gamma))}>{compact(gamma)}</div>
            </button>
          );
        })}
      </div>
    </Section>
  );
}
