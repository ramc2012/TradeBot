"use client";

/**
 * Charts workbench — native v2 surface.
 *
 * Institutional OHLC verification chart built on the shared TradingView
 * CandleChart (canvas candles + volume pane) with an overlaid Bollinger /
 * EMA50 price-line band, a KPI strip (last / chg% / day range / VWAP), and
 * recharts MACD + RSI sub-panels rendered from the parallel indicator
 * arrays the backend returns. Trade markers from agent_signals are surfaced
 * in a strategy-filterable book beside the chart.
 *
 * Replaces the v1 SVG ChartsWorkbench (kept reachable via the DeskShell
 * v1 link). Data:
 *   GET /api/charts/universe                                         → instruments + strategy colours + timeframes
 *   GET /api/charts/ohlc?underlying&timeframe&lookback_sessions      → bars + aligned indicators + trade markers
 */
import { useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, BarChart3, CandlestickChart, LineChart, Search, TrendingUp } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatNumber,
  formatPct,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { CandleChart, CHART, type CandleBar, type ChartPriceLine } from "@/components/strategies/shared";
import { useTickStream } from "@/hooks/useTickStream";
import { api as apiClient } from "@/lib/api";

type Kind = "INDEX" | "COMMODITY" | "STOCK";
type Strategy = "s1" | "s2" | "commodity" | "cbe" | "directional";

type UniverseInstrument = { underlying: string; kind: Kind; traded_today: boolean };
type UniverseResponse = {
  instruments?: UniverseInstrument[];
  kinds?: Kind[];
  strategy_colors?: Record<string, string>;
  supported_timeframes?: string[];
};

type Bar1 = { time: string; open: number; high: number; low: number; close: number; volume: number };
type IndicatorArrays = {
  macd?: (number | null)[];
  macd_signal?: (number | null)[];
  macd_histogram?: (number | null)[];
  rsi?: (number | null)[];
  bb_upper?: (number | null)[];
  bb_middle?: (number | null)[];
  bb_lower?: (number | null)[];
  ema50?: (number | null)[];
};
type TradeMarker = {
  strategy: Strategy;
  type: "entry" | "exit";
  time: string;
  option_type?: string | null;
  strike?: number | null;
  premium?: number | null;
  pnl?: number | null;
  reason?: string | null;
  label?: string | null;
};
type OHLCResponse = {
  underlying?: string;
  timeframe?: string;
  bar_count?: number;
  bars?: Bar1[];
  indicators?: IndicatorArrays;
  trades?: TradeMarker[];
  strategy_colors?: Record<string, string>;
  detail?: string | null;
};

const TABS = [
  { key: "chart", label: "Chart", icon: CandlestickChart },
  { key: "oscillators", label: "Oscillators", icon: LineChart },
  { key: "trades", label: "Trade markers", icon: TrendingUp },
];

const TIMEFRAMES = ["15minute", "30minute", "60minute"];
const KIND_LABELS: Record<Kind, string> = { INDEX: "Indices", COMMODITY: "Commodities", STOCK: "Stocks" };
const STRATEGY_LABELS: Record<Strategy, string> = {
  s1: "S1 · 30m ATM MACD",
  s2: "S2 · 5m Index MP",
  commodity: "Commodity",
  cbe: "CBE",
  directional: "Directional",
};
const DEFAULT_STRATEGY_COLORS: Record<string, string> = {
  s1: "#22d3ee",
  s2: "#3b82f6",
  commodity: "#f59e0b",
  cbe: "#a855f7",
  directional: "#10b981",
};
const AXIS = { stroke: CHART.axis, fontSize: 10, tickLine: false } as const;

const tfLabel = (tf?: string) => (tf || "").replace("minute", "m");

export default function ChartsWorkbench() {
  const [activeTab, setActiveTab] = useUrlTab("chart");
  const [, startTransition] = useTransition();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [timeframe, setTimeframe] = useState("30minute");
  const [lookback, setLookback] = useState(5);
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState<Kind | "ALL" | "TRADED">("ALL");
  const [showBands, setShowBands] = useState(true);
  const [enabledStrategies, setEnabledStrategies] = useState<Set<Strategy>>(
    new Set<Strategy>(["s1", "s2", "commodity", "cbe", "directional"]),
  );

  const universeQuery = useQuery({
    queryKey: ["charts", "universe"],
    queryFn: async () => (await apiClient.get("/api/charts/universe")).data as UniverseResponse,
    staleTime: REFRESH_MS.summary * 5,
    refetchOnWindowFocus: false,
  });

  const ohlcQuery = useQuery({
    queryKey: ["charts", "ohlc", underlying, timeframe, lookback],
    queryFn: async () =>
      (
        await apiClient.get("/api/charts/ohlc", {
          params: { underlying, timeframe, lookback_sessions: lookback },
          timeout: 30_000,
        })
      ).data as OHLCResponse,
    refetchInterval: REFRESH_MS.live * 8, // ~2m REST cadence; tick stream breathes between
    staleTime: REFRESH_MS.live * 4,
    refetchOnWindowFocus: false,
    enabled: Boolean(underlying),
  });

  // Live broker tick (indices only) splices into the trailing bar so the
  // chart breathes between REST refreshes. Indicators stay on closed bars.
  const liveTick = useTickStream(underlying);

  const data = ohlcQuery.data;
  const rawBars = useMemo(() => data?.bars ?? [], [data?.bars]);
  const indicators = data?.indicators ?? {};
  const strategyColors = { ...DEFAULT_STRATEGY_COLORS, ...(universeQuery.data?.strategy_colors || {}) };
  const universe = universeQuery.data?.instruments ?? [];
  const timeframes = universeQuery.data?.supported_timeframes ?? TIMEFRAMES;

  const bars = useMemo<Bar1[]>(() => {
    if (!rawBars.length || !liveTick || liveTick.ltp == null) return rawBars;
    const ltp = Number(liveTick.ltp);
    if (!Number.isFinite(ltp) || ltp <= 0) return rawBars;
    const last = rawBars[rawBars.length - 1];
    return [...rawBars.slice(0, -1), { ...last, close: ltp, high: Math.max(last.high, ltp), low: Math.min(last.low, ltp) }];
  }, [rawBars, liveTick]);

  const candleBars = useMemo<CandleBar[]>(
    () => bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume })),
    [bars],
  );

  const filteredUniverse = useMemo(() => {
    const q = search.trim().toUpperCase();
    return universe.filter((item) => {
      if (kindFilter === "TRADED" && !item.traded_today) return false;
      if (kindFilter !== "ALL" && kindFilter !== "TRADED" && item.kind !== kindFilter) return false;
      if (q && !item.underlying.includes(q)) return false;
      return true;
    });
  }, [universe, search, kindFilter]);

  const trades = useMemo(
    () => (data?.trades ?? []).filter((t) => enabledStrategies.has(t.strategy)),
    [data?.trades, enabledStrategies],
  );

  // ── KPI math from the (tick-spliced) bars ──────────────────────────────
  const kpis = useMemo(() => {
    if (!bars.length) return null;
    const last = bars[bars.length - 1];
    const lastClose = liveTick?.ltp != null && Number.isFinite(Number(liveTick.ltp)) ? Number(liveTick.ltp) : last.close;
    // session = bars sharing the last bar's IST calendar date
    const dayKey = istDay(last.time);
    const session = bars.filter((b) => istDay(b.time) === dayKey);
    const sessionBars = session.length ? session : bars;
    const dayOpen = sessionBars[0].open;
    const dayHigh = Math.max(...sessionBars.map((b) => b.high));
    const dayLow = Math.min(...sessionBars.map((b) => b.low));
    const chg = lastClose - dayOpen;
    const chgPct = dayOpen ? chg / dayOpen : 0;
    let pv = 0;
    let vol = 0;
    for (const b of sessionBars) {
      const tp = (b.high + b.low + b.close) / 3;
      pv += tp * (b.volume || 0);
      vol += b.volume || 0;
    }
    const vwap = vol > 0 ? pv / vol : null;
    return { last: lastClose, chg, chgPct, dayOpen, dayHigh, dayLow, vwap, sessionCount: sessionBars.length };
  }, [bars, liveTick]);

  // ── Bollinger / EMA50 overlay as CandleChart price-lines (last value) ──
  const priceLines = useMemo<ChartPriceLine[]>(() => {
    if (!showBands || !bars.length) return [];
    const lastOf = (arr?: (number | null)[]) => {
      if (!arr) return null;
      for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i] as number;
      return null;
    };
    const out: ChartPriceLine[] = [];
    const bu = lastOf(indicators.bb_upper);
    const bm = lastOf(indicators.bb_middle);
    const bl = lastOf(indicators.bb_lower);
    const ema = lastOf(indicators.ema50);
    if (bu != null) out.push({ price: bu, color: CHART.blue, title: "BB↑", dashed: true });
    if (bm != null) out.push({ price: bm, color: CHART.muted, title: "BB·", dashed: true });
    if (bl != null) out.push({ price: bl, color: CHART.blue, title: "BB↓", dashed: true });
    if (ema != null) out.push({ price: ema, color: CHART.amber, title: "EMA50" });
    if (kpis?.vwap != null) out.push({ price: kpis.vwap, color: CHART.violet, title: "VWAP", dashed: true });
    return out;
  }, [showBands, bars.length, indicators, kpis?.vwap]);

  const toggleStrategy = (s: Strategy) =>
    setEnabledStrategies((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });

  const emptyDetail = data?.detail && rawBars.length === 0 ? data.detail : null;

  return (
    <DeskShell
      title="Charts workbench"
      description="Institutional OHLC verification — candles, Bollinger/EMA/VWAP overlays, MACD/RSI oscillators, and agent trade markers."
      asOf={bars.length ? bars[bars.length - 1].time : undefined}
      isFetching={ohlcQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/charts"
      rightSlot={
        <div className="flex flex-wrap items-center gap-2">
          <Picker label="Symbol" value={underlying} options={universe.map((u) => u.underlying)} fallback={[underlying]} onChange={(v) => startTransition(() => setUnderlying(v))} />
          <Picker label="TF" value={timeframe} options={timeframes} onChange={(v) => startTransition(() => setTimeframe(v))} display={tfLabel} />
          <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
            <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">Sessions</span>
            <input
              type="number"
              min={1}
              max={30}
              value={lookback}
              onChange={(e) => setLookback(Math.max(1, Math.min(30, Number(e.target.value) || 5)))}
              className="w-12 bg-transparent text-right font-mono outline-none"
            />
          </label>
        </div>
      }
    >
      {/* KPI strip — always on, all tabs */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <MetricTile label="Last" value={formatNumber(kpis?.last, 1)} detail={`${data?.underlying || underlying} · ${tfLabel(data?.timeframe || timeframe)}`} />
        <MetricTile label="Change" value={kpis ? formatNumber(kpis.chg, 1) : "—"} detail={kpis ? formatPct(kpis.chgPct) : ""} color={tone(kpis?.chg)} />
        <MetricTile label="Day open" value={formatNumber(kpis?.dayOpen, 1)} />
        <MetricTile label="Day range" value={kpis ? `${formatNumber(kpis.dayLow, 1)} – ${formatNumber(kpis.dayHigh, 1)}` : "—"} detail={kpis ? formatNumber(kpis.dayHigh - kpis.dayLow, 1) : ""} />
        <MetricTile label="VWAP" value={formatNumber(kpis?.vwap, 1)} detail={kpis?.vwap != null && kpis ? `${kpis.last >= kpis.vwap ? "above" : "below"}` : ""} color={kpis?.vwap != null && kpis ? tone(kpis.last - kpis.vwap) : undefined} />
        <MetricTile label="Bars" value={String(rawBars.length)} detail={`${kpis?.sessionCount ?? 0} this session`} />
        <MetricTile label="Markers" value={String(data?.trades?.length ?? 0)} detail={`${trades.length} shown`} />
      </section>

      <div className="mt-4 grid gap-4 xl:grid-cols-[240px_minmax(0,1fr)]">
        {/* ── Left rail: instrument picker ── */}
        <aside className="space-y-3">
          <Section title="Instrument" icon={<Search size={15} />}>
            <div className="relative">
              <Search size={13} className="pointer-events-none absolute left-2 top-2.5 text-text-muted" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="NIFTY, RELIANCE, GOLD…"
                className="w-full rounded-lg border border-bg-border bg-bg-primary/40 px-7 py-1.5 font-mono text-[11.5px] text-text-primary placeholder:text-text-muted focus:border-accent-blue/50 focus:outline-none"
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {(["ALL", "INDEX", "COMMODITY", "STOCK", "TRADED"] as const).map((k) => {
                const active = kindFilter === k;
                const label = k === "ALL" ? "All" : k === "TRADED" ? "Traded" : KIND_LABELS[k as Kind];
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setKindFilter(k)}
                    className={`rounded-md border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em] transition-colors ${
                      active
                        ? "border-accent-blue/60 bg-accent-blue/10 text-accent-blue"
                        : "border-bg-border bg-bg-primary/20 text-text-secondary hover:border-bg-active"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            <div className="mt-2 max-h-[420px] overflow-y-auto rounded-lg border border-bg-border/60 bg-bg-primary/20">
              {filteredUniverse.length === 0 ? (
                <div className="px-3 py-4 text-center text-[11px] text-text-muted">{universeQuery.isFetching ? "Loading…" : "No matches"}</div>
              ) : (
                filteredUniverse.map((item) => {
                  const active = item.underlying === underlying;
                  return (
                    <button
                      key={item.underlying}
                      type="button"
                      onClick={() => startTransition(() => setUnderlying(item.underlying))}
                      className={`flex w-full items-center justify-between border-b border-bg-border/30 px-3 py-1.5 text-left font-mono text-[11px] transition-colors ${
                        active ? "bg-accent-blue/15 text-accent-blue" : "text-text-secondary hover:bg-bg-primary/30"
                      }`}
                    >
                      <span>{item.underlying}</span>
                      <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.12em] text-text-muted">
                        {item.traded_today ? <span className="rounded-sm bg-accent-amber/20 px-1 py-0.5 text-accent-amber">live</span> : null}
                        <span>{item.kind === "INDEX" ? "IDX" : item.kind === "COMMODITY" ? "MCX" : "STK"}</span>
                      </span>
                    </button>
                  );
                })
              )}
            </div>
            <div className="mt-1.5 text-[10px] text-text-muted">
              {filteredUniverse.length} / {universe.length} instruments
            </div>
          </Section>

          <Section title="Strategies" icon={<TrendingUp size={15} />} description="Toggle trade markers by lane.">
            <div className="space-y-1">
              {(Object.keys(STRATEGY_LABELS) as Strategy[]).map((s) => {
                const enabled = enabledStrategies.has(s);
                const count = (data?.trades ?? []).filter((t) => t.strategy === s).length;
                return (
                  <label key={s} className="flex cursor-pointer items-center gap-2 py-0.5 text-[11px]">
                    <input type="checkbox" checked={enabled} onChange={() => toggleStrategy(s)} className="h-3 w-3 accent-accent-blue" />
                    <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: strategyColors[s] }} aria-hidden />
                    <span className="flex-1 text-text-secondary">{STRATEGY_LABELS[s]}</span>
                    <span className="text-text-muted">{count}</span>
                  </label>
                );
              })}
            </div>
          </Section>
        </aside>

        {/* ── Main pane ── */}
        <main className="min-w-0 space-y-4">
          {ohlcQuery.isError ? (
            <div className="rounded-xl border border-accent-red/40 bg-accent-red/10 px-4 py-3 text-sm text-accent-red">
              Failed to load chart data for {underlying}.
            </div>
          ) : null}
          {emptyDetail ? (
            <div className="rounded-xl border border-accent-amber/40 bg-accent-amber/10 px-4 py-3 text-sm text-accent-amber">{emptyDetail}</div>
          ) : null}

          {activeTab === "chart" ? (
            <Section
              title={`${data?.underlying || underlying} · ${tfLabel(data?.timeframe || timeframe)}`}
              icon={<CandlestickChart size={16} />}
              rightSlot={
                <div className="flex items-center gap-2">
                  {liveTick?.ltp != null ? <StatusBadge label="live tick" variant="success" icon={<span className="h-1.5 w-1.5 rounded-full bg-accent-green" />} /> : null}
                  <button
                    type="button"
                    onClick={() => setShowBands((v) => !v)}
                    className={`rounded-md border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em] transition-colors ${
                      showBands ? "border-accent-blue/60 bg-accent-blue/10 text-accent-blue" : "border-bg-border bg-bg-primary/20 text-text-muted hover:border-bg-active"
                    }`}
                  >
                    BB · EMA · VWAP
                  </button>
                </div>
              }
            >
              <CandleChart bars={candleBars} priceLines={priceLines} height={460} showVolume />
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">
                <Legend color={CHART.blue} label="BB ±2σ(20)" />
                <Legend color={CHART.amber} label="EMA(50)" />
                <Legend color={CHART.violet} label="VWAP" />
                <span>{rawBars.length} bars · last {formatIST(rawBars[rawBars.length - 1]?.time)}</span>
              </div>
            </Section>
          ) : null}

          {activeTab === "oscillators" ? (
            <OscillatorPanels bars={bars} indicators={indicators} />
          ) : null}

          {activeTab === "trades" ? (
            <TradeMarkerBook trades={trades} strategyColors={strategyColors} total={data?.trades?.length ?? 0} />
          ) : null}
        </main>
      </div>
    </DeskShell>
  );
}

// ──────────────────────────────────────────────────────────────────────────
// Oscillator sub-panels (recharts) — MACD + RSI aligned to bar index.
// ──────────────────────────────────────────────────────────────────────────
function OscillatorPanels({ bars, indicators }: { bars: Bar1[]; indicators: IndicatorArrays }) {
  const rows = useMemo(
    () =>
      bars.map((b, i) => ({
        i,
        t: b.time,
        macd: indicators.macd?.[i] ?? null,
        signal: indicators.macd_signal?.[i] ?? null,
        hist: indicators.macd_histogram?.[i] ?? null,
        rsi: indicators.rsi?.[i] ?? null,
      })),
    [bars, indicators],
  );

  if (!bars.length) {
    return (
      <Section title="Oscillators" icon={<Activity size={16} />}>
        <div className="py-10 text-center text-sm text-text-muted">No bars to compute oscillators.</div>
      </Section>
    );
  }

  const fmtX = (t: string) => formatIST(t);
  const lastRsi = [...rows].reverse().find((r) => r.rsi != null)?.rsi ?? null;
  const lastMacd = [...rows].reverse().find((r) => r.macd != null)?.macd ?? null;
  const lastHist = [...rows].reverse().find((r) => r.hist != null)?.hist ?? null;

  return (
    <div className="space-y-4">
      <Section
        title="MACD (12, 26, 9)"
        icon={<BarChart3 size={16} />}
        rightSlot={
          <div className="flex gap-3 text-[11px]">
            <span className="text-text-muted">macd <span className={tone(lastMacd)}>{formatNumber(lastMacd, 2)}</span></span>
            <span className="text-text-muted">hist <span className={tone(lastHist)}>{formatNumber(lastHist, 2)}</span></span>
          </div>
        }
      >
        <ResponsiveContainer width="100%" height={200}>
          <ComposedChart data={rows} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
            <XAxis dataKey="t" tickFormatter={fmtX} {...AXIS} minTickGap={48} />
            <YAxis {...AXIS} width={48} tickFormatter={(v) => formatNumber(v, 0)} />
            <Tooltip content={<OscTooltip mode="macd" />} />
            <ReferenceLine y={0} stroke={CHART.axis} />
            <Bar dataKey="hist" isAnimationActive={false} barSize={3}>
              {rows.map((r, i) => (
                <Cell key={i} fill={(r.hist ?? 0) >= 0 ? CHART.green : CHART.red} fillOpacity={0.6} />
              ))}
            </Bar>
            <Line type="monotone" dataKey="macd" stroke={CHART.blue} strokeWidth={1.4} dot={false} isAnimationActive={false} connectNulls />
            <Line type="monotone" dataKey="signal" stroke={CHART.amber} strokeWidth={1.2} dot={false} isAnimationActive={false} connectNulls />
          </ComposedChart>
        </ResponsiveContainer>
      </Section>

      <Section
        title="RSI (14)"
        icon={<LineChart size={16} />}
        rightSlot={<span className="text-[11px] text-text-muted">rsi <span className={lastRsi != null && lastRsi >= 70 ? "text-accent-red" : lastRsi != null && lastRsi <= 30 ? "text-accent-green" : "text-text-secondary"}>{formatNumber(lastRsi, 1)}</span></span>}
      >
        <ResponsiveContainer width="100%" height={170}>
          <ComposedChart data={rows} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
            <XAxis dataKey="t" tickFormatter={fmtX} {...AXIS} minTickGap={48} />
            <YAxis {...AXIS} width={48} domain={[0, 100]} ticks={[0, 30, 50, 70, 100]} />
            <Tooltip content={<OscTooltip mode="rsi" />} />
            <ReferenceLine y={70} stroke={CHART.red} strokeDasharray="3 4" strokeOpacity={0.5} />
            <ReferenceLine y={50} stroke={CHART.axis} strokeOpacity={0.5} />
            <ReferenceLine y={30} stroke={CHART.green} strokeDasharray="3 4" strokeOpacity={0.5} />
            <Line type="monotone" dataKey="rsi" stroke={CHART.violet} strokeWidth={1.4} dot={false} isAnimationActive={false} connectNulls />
          </ComposedChart>
        </ResponsiveContainer>
      </Section>
    </div>
  );
}

type OscRow = { t: string; macd: number | null; signal: number | null; hist: number | null; rsi: number | null };
function OscTooltip({ active, payload, mode }: { active?: boolean; payload?: { payload: OscRow }[]; mode: "macd" | "rsi" }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="rounded-lg border border-bg-border bg-bg-card/95 px-2.5 py-1.5 font-mono text-[11px] shadow-lg">
      <div className="text-text-muted">{formatIST(r.t)}</div>
      {mode === "macd" ? (
        <div className="mt-0.5 grid grid-cols-2 gap-x-3">
          <span className="text-text-muted">macd</span><span className={`text-right ${tone(r.macd)}`}>{formatNumber(r.macd, 2)}</span>
          <span className="text-text-muted">signal</span><span className="text-right text-text-secondary">{formatNumber(r.signal, 2)}</span>
          <span className="text-text-muted">hist</span><span className={`text-right ${tone(r.hist)}`}>{formatNumber(r.hist, 2)}</span>
        </div>
      ) : (
        <div className="mt-0.5"><span className="text-text-secondary">{formatNumber(r.rsi, 1)}</span></div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
// Trade-marker book.
// ──────────────────────────────────────────────────────────────────────────
function TradeMarkerBook({ trades, strategyColors, total }: { trades: TradeMarker[]; strategyColors: Record<string, string>; total: number }) {
  const sorted = useMemo(() => [...trades].sort((a, b) => Date.parse(b.time) - Date.parse(a.time)), [trades]);
  return (
    <Section
      title="Trade markers"
      icon={<TrendingUp size={16} />}
      description="Entries (filled) and exits from agent_signals on the visible window."
      rightSlot={<StatusBadge label={`${trades.length} / ${total}`} variant="info" />}
    >
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-bg-border/60">
              {["Time", "Lane", "Type", "Side", "Strike", "Premium", "P&L", "Reason"].map((h, i) => (
                <th key={i} className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${i === 0 || i === 7 ? "text-left" : "text-right"}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length ? (
              sorted.map((t, i) => (
                <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-1.5 text-left font-mono text-[12px] text-text-primary whitespace-nowrap">{formatIST(t.time)}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] whitespace-nowrap">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: strategyColors[t.strategy] }} />
                      <span className="text-text-secondary">{t.strategy}</span>
                    </span>
                  </td>
                  <td className="px-2.5 py-1.5 text-right">
                    <StatusBadge label={t.type} variant={t.type === "entry" ? "success" : "neutral"} />
                  </td>
                  <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${t.option_type === "CE" ? "text-accent-green" : t.option_type === "PE" ? "text-accent-red" : "text-text-secondary"}`}>{t.option_type || "—"}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{t.strike != null ? formatNumber(t.strike, 0) : "—"}</td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{t.premium != null ? formatNumber(t.premium, 2) : "—"}</td>
                  <td className={`px-2.5 py-1.5 text-right font-mono text-[12px] ${tone(t.pnl)}`}>{t.pnl != null ? formatNumber(t.pnl, 0) : "—"}</td>
                  <td className="px-2.5 py-1.5 text-left text-[11.5px] text-text-muted whitespace-nowrap">{t.reason || "—"}</td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={8} className="px-2.5 py-8 text-center text-sm text-text-muted">No trade markers for the selected lanes / window.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ── small helpers ──────────────────────────────────────────────────────────
function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-2 w-2.5 rounded-sm" style={{ backgroundColor: color }} aria-hidden />
      {label}
    </span>
  );
}

function Picker({
  label,
  value,
  options,
  onChange,
  display,
  fallback,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  display?: (v: string) => string;
  fallback?: string[];
}) {
  const opts = options.length ? options : fallback || [value];
  return (
    <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
      <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <select className="bg-transparent outline-none" value={value} onChange={(e) => onChange(e.target.value)}>
        {opts.map((o) => (
          <option key={o} value={o} className="bg-bg-card text-text-primary">
            {display ? display(o) : o}
          </option>
        ))}
      </select>
    </label>
  );
}

/** IST calendar date key (YYYY-MM-DD) for session grouping. */
function istDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // en-CA gives ISO-style YYYY-MM-DD; pin to IST.
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}
