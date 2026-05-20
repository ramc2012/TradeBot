"use client";

import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, RefreshCw } from "lucide-react";
import { getChartUniverse, getChartOHLC } from "@/lib/api";

type Kind = "INDEX" | "COMMODITY" | "STOCK";
type Timeframe = "15minute" | "30minute" | "60minute";
type Strategy = "s1" | "s2" | "commodity" | "cbe" | "directional";

interface UniverseInstrument {
  underlying: string;
  kind: Kind;
  traded_today: boolean;
}

interface UniverseResponse {
  instruments: UniverseInstrument[];
  kinds: Kind[];
  strategy_colors: Record<Strategy, string>;
  supported_timeframes: Timeframe[];
}

interface Bar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface TradeMarker {
  strategy: Strategy;
  type: "entry" | "exit";
  time: string;
  option_type?: string | null;
  strike?: number | null;
  premium?: number | null;
  pnl?: number | null;
  reason?: string | null;
  label?: string | null;
}

interface OHLCResponse {
  underlying: string;
  timeframe: Timeframe;
  bar_count?: number;
  bars: Bar[];
  indicators: {
    macd?: (number | null)[];
    macd_signal?: (number | null)[];
    macd_histogram?: (number | null)[];
    rsi?: (number | null)[];
    bb_upper?: (number | null)[];
    bb_middle?: (number | null)[];
    bb_lower?: (number | null)[];
    ema50?: (number | null)[];
  };
  trades: TradeMarker[];
  strategy_colors?: Record<Strategy, string>;
  detail?: string;
}

const STRATEGY_LABELS: Record<Strategy, string> = {
  s1: "S1 · 30m ATM MACD",
  s2: "S2 · 5m Index MP",
  commodity: "Commodity",
  cbe: "CBE",
  directional: "Directional",
};

const KIND_LABELS: Record<Kind, string> = {
  INDEX: "Indices",
  COMMODITY: "Commodities",
  STOCK: "Stocks",
};

const TIMEFRAMES: { value: Timeframe; label: string }[] = [
  { value: "15minute", label: "15m" },
  { value: "30minute", label: "30m" },
  { value: "60minute", label: "1h" },
];

// Chart pane heights tuned so candle bodies render at ≥4px even on a
// busy 5-day × 30m view (~70 bars). Total vertical viewBox ~960 keeps
// 212-bar views readable when the SVG scales down to ~1100px display
// width. Increase the price pane the most — that's where granular
// price action lives.
const CHART_HEIGHT = 560;
const MACD_HEIGHT = 150;
const RSI_HEIGHT = 120;
const VOLUME_HEIGHT = 80;
const TOP_PAD = 20;
const RIGHT_PAD = 70;
const LEFT_PAD = 64;

function fmt(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtTime(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Kolkata",
    });
  } catch {
    return iso;
  }
}

function ChartsWorkbench() {
  const [selected, setSelected] = useState<string>("NIFTY");
  const [timeframe, setTimeframe] = useState<Timeframe>("30minute");
  const [lookback, setLookback] = useState<number>(5);
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState<Kind | "ALL" | "TRADED">("ALL");
  const [enabledStrategies, setEnabledStrategies] = useState<Set<Strategy>>(
    new Set<Strategy>(["s1", "s2", "commodity", "cbe", "directional"]),
  );

  const universeQuery = useQuery<UniverseResponse>({
    queryKey: ["chart-universe"],
    queryFn: async () => (await getChartUniverse()).data,
    staleTime: 5 * 60_000,
  });

  const ohlcQuery = useQuery<OHLCResponse>({
    queryKey: ["chart-ohlc", selected, timeframe, lookback],
    queryFn: async () => (await getChartOHLC(selected, timeframe, lookback)).data,
    refetchInterval: 30_000,
    enabled: Boolean(selected),
  });

  const universe = universeQuery.data?.instruments ?? [];
  const strategyColors = universeQuery.data?.strategy_colors ?? {
    s1: "#22d3ee",
    s2: "#3b82f6",
    commodity: "#f59e0b",
    cbe: "#a855f7",
    directional: "#10b981",
  };

  const filteredUniverse = useMemo(() => {
    const q = search.trim().toUpperCase();
    return universe.filter((item) => {
      if (kindFilter === "TRADED" && !item.traded_today) return false;
      if (kindFilter !== "ALL" && kindFilter !== "TRADED" && item.kind !== kindFilter) return false;
      if (q && !item.underlying.includes(q)) return false;
      return true;
    });
  }, [universe, search, kindFilter]);

  const data = ohlcQuery.data;
  const bars = data?.bars ?? [];
  const trades = useMemo(
    () => (data?.trades ?? []).filter((t) => enabledStrategies.has(t.strategy)),
    [data?.trades, enabledStrategies],
  );

  const toggleStrategy = (s: Strategy) => {
    setEnabledStrategies((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  return (
    <div className="min-h-full bg-bg-primary text-text-primary">
      <header className="border-b border-bg-border bg-black/40 px-4 py-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-baseline gap-3">
            <h1 className="font-mono text-base font-semibold uppercase tracking-[0.12em] text-text-primary">
              Verification chart
            </h1>
            <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-text-muted">
              OHLC · MACD · RSI · BB · EMA50 · trade markers
            </span>
          </div>
          <button
            type="button"
            onClick={() => ohlcQuery.refetch()}
            className="inline-flex items-center gap-2 border border-accent-cyan/40 bg-accent-cyan/10 px-3 py-1 font-mono text-xs font-semibold uppercase tracking-[0.12em] text-accent-cyan transition-colors hover:bg-accent-cyan/15"
          >
            <RefreshCw size={14} className={ohlcQuery.isFetching ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      <div className="grid gap-3 p-3 xl:grid-cols-[240px_minmax(0,1fr)]">
        {/* ── Left rail: instrument picker + filters + strategy legend ── */}
        <aside className="space-y-3">
          <div className="border border-bg-border bg-black/30 p-3">
            <div className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Instrument
            </div>
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-2 top-2.5 text-text-muted" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search (e.g. NIFTY, RELIANCE, GOLD)"
                className="w-full border border-bg-border bg-black px-7 py-1.5 font-mono text-xs text-text-primary placeholder:text-text-muted focus:border-accent-cyan/50 focus:outline-none"
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {(["ALL", "INDEX", "COMMODITY", "STOCK", "TRADED"] as const).map((k) => {
                const active = kindFilter === k;
                const label = k === "ALL" ? "All" : k === "TRADED" ? "Traded today" : KIND_LABELS[k as Kind];
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setKindFilter(k)}
                    className={`border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] ${
                      active
                        ? "border-accent-cyan/60 bg-accent-cyan/10 text-accent-cyan"
                        : "border-bg-border bg-black/30 text-text-secondary hover:border-bg-active"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            <div className="mt-2 max-h-[440px] overflow-y-auto border border-bg-border bg-black/20">
              {filteredUniverse.length === 0 ? (
                <div className="px-3 py-4 text-center text-xs text-text-muted">No matches</div>
              ) : (
                filteredUniverse.map((item) => {
                  const active = item.underlying === selected;
                  return (
                    <button
                      key={item.underlying}
                      type="button"
                      onClick={() => setSelected(item.underlying)}
                      className={`flex w-full items-center justify-between border-b border-bg-border/40 px-3 py-1.5 text-left font-mono text-[11px] transition-colors ${
                        active ? "bg-accent-cyan/15 text-accent-cyan" : "hover:bg-bg-hover/30"
                      }`}
                    >
                      <span>{item.underlying}</span>
                      <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.14em] text-text-muted">
                        {item.traded_today ? (
                          <span className="rounded-sm bg-accent-amber/20 px-1 py-0.5 text-accent-amber">live</span>
                        ) : null}
                        <span>{item.kind === "INDEX" ? "IDX" : item.kind === "COMMODITY" ? "MCX" : "STK"}</span>
                      </span>
                    </button>
                  );
                })
              )}
            </div>
            <div className="mt-1 text-[10px] text-text-muted">
              {filteredUniverse.length} / {universe.length} instruments
            </div>
          </div>

          <div className="border border-bg-border bg-black/30 p-3">
            <div className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Timeframe
            </div>
            <div className="flex gap-1">
              {TIMEFRAMES.map((tf) => (
                <button
                  key={tf.value}
                  type="button"
                  onClick={() => setTimeframe(tf.value)}
                  className={`flex-1 border px-2 py-1.5 font-mono text-xs ${
                    timeframe === tf.value
                      ? "border-accent-amber/70 bg-accent-amber/12 text-accent-amber"
                      : "border-bg-border bg-black/30 text-text-secondary hover:border-bg-active"
                  }`}
                >
                  {tf.label}
                </button>
              ))}
            </div>
            <div className="mt-2 flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Sessions</span>
              <input
                type="number"
                min={1}
                max={30}
                value={lookback}
                onChange={(e) => setLookback(Math.max(1, Math.min(30, Number(e.target.value) || 5)))}
                className="w-16 border border-bg-border bg-black px-2 py-1 text-right font-mono text-xs"
              />
            </div>
          </div>

          <div className="border border-bg-border bg-black/30 p-3">
            <div className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Strategies on chart
            </div>
            <div className="space-y-1">
              {(Object.keys(STRATEGY_LABELS) as Strategy[]).map((s) => {
                const enabled = enabledStrategies.has(s);
                const color = strategyColors[s];
                const tradeCount = (data?.trades ?? []).filter((t) => t.strategy === s).length;
                return (
                  <label
                    key={s}
                    className="flex cursor-pointer items-center gap-2 py-1 font-mono text-[11px]"
                  >
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={() => toggleStrategy(s)}
                      className="h-3 w-3 accent-accent-cyan"
                    />
                    <span
                      className="inline-block h-2.5 w-2.5"
                      style={{ backgroundColor: color }}
                      aria-hidden
                    />
                    <span className="flex-1 text-text-secondary">{STRATEGY_LABELS[s]}</span>
                    <span className="text-text-muted">{tradeCount}</span>
                  </label>
                );
              })}
            </div>
            <div className="mt-2 border-t border-bg-border pt-2 text-[10px] leading-4 text-text-muted">
              ▲ filled = entry · △ hollow = exit · hover for strike/premium/P&L
            </div>
          </div>
        </aside>

        {/* ── Main chart pane ────────────────────────────────────────── */}
        <main className="space-y-3">
          {ohlcQuery.isError ? (
            <div className="border border-accent-red/50 bg-accent-red/10 px-4 py-3 text-sm text-accent-red">
              Failed to load chart data for {selected}.
            </div>
          ) : null}

          {data?.detail && bars.length === 0 ? (
            <div className="border border-accent-amber/50 bg-accent-amber/10 px-4 py-3 text-sm text-accent-amber">
              {data.detail}
            </div>
          ) : null}

          {bars.length > 0 ? (
            <ChartPanel
              data={data!}
              trades={trades}
              strategyColors={strategyColors}
            />
          ) : (
            <div className="border border-bg-border bg-black px-4 py-12 text-center text-sm text-text-muted">
              {ohlcQuery.isFetching ? "Loading…" : "No chart data."}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function ChartPanel({
  data,
  trades,
  strategyColors,
}: {
  data: OHLCResponse;
  trades: TradeMarker[];
  strategyColors: Record<Strategy, string>;
}) {
  const bars = data.bars;
  const ind = data.indicators ?? {};
  const [hover, setHover] = useState<number | null>(null);

  // Layout constants — main panel stacks price / MACD / RSI / Volume vertically
  const totalHeight = TOP_PAD + CHART_HEIGHT + 8 + MACD_HEIGHT + 8 + RSI_HEIGHT + 8 + VOLUME_HEIGHT + 28;
  const chartWidth = 1640; // SVG internal width — scales via viewBox
  const plotWidth = chartWidth - LEFT_PAD - RIGHT_PAD;
  const slot = plotWidth / Math.max(bars.length, 1);
  // Candle bodies fill 70% of their slot. No hard cap — when the user
  // pulls a 1-day lookback the candles should render thick, not pinned
  // to a 14px ceiling that wasted screen space.
  const candleWidth = Math.max(2, Math.min(slot * 0.72, 24));

  const xFor = (idx: number) => LEFT_PAD + slot * idx + slot / 2;

  // Price domain (incl. BB)
  const priceVals: number[] = [];
  for (const b of bars) {
    priceVals.push(b.high, b.low);
  }
  for (const v of ind.bb_upper ?? []) if (v != null) priceVals.push(v);
  for (const v of ind.bb_lower ?? []) if (v != null) priceVals.push(v);
  const priceMin = priceVals.length ? Math.min(...priceVals) : 0;
  const priceMax = priceVals.length ? Math.max(...priceVals) : 1;
  const pricePad = (priceMax - priceMin) * 0.05 || 1;
  const yPrice = (p: number) =>
    TOP_PAD + ((priceMax + pricePad - p) / (priceMax - priceMin + pricePad * 2)) * CHART_HEIGHT;

  // MACD domain
  const macdY0 = TOP_PAD + CHART_HEIGHT + 8;
  const macdVals = [
    ...(ind.macd ?? []).filter((v) => v != null),
    ...(ind.macd_signal ?? []).filter((v) => v != null),
    ...(ind.macd_histogram ?? []).filter((v) => v != null),
  ] as number[];
  const macdMax = macdVals.length ? Math.max(...macdVals, 0) : 1;
  const macdMin = macdVals.length ? Math.min(...macdVals, 0) : -1;
  const macdRange = macdMax - macdMin || 1;
  const yMacd = (v: number) => macdY0 + ((macdMax - v) / macdRange) * MACD_HEIGHT;

  // RSI domain (0-100)
  const rsiY0 = macdY0 + MACD_HEIGHT + 8;
  const yRsi = (v: number) => rsiY0 + ((100 - v) / 100) * RSI_HEIGHT;

  // Volume
  const volY0 = rsiY0 + RSI_HEIGHT + 8;
  const volMax = Math.max(1, ...bars.map((b) => b.volume));
  const yVol = (v: number) => volY0 + ((volMax - v) / volMax) * VOLUME_HEIGHT;

  // Find nearest bar index for a trade time
  const tradeToIndex = (iso: string): number => {
    if (!iso || !bars.length) return -1;
    const t = new Date(iso).getTime();
    let best = -1;
    let bestDiff = Infinity;
    for (let i = 0; i < bars.length; i++) {
      const bt = new Date(bars[i].time).getTime();
      const d = Math.abs(bt - t);
      if (d < bestDiff) {
        bestDiff = d;
        best = i;
      }
    }
    // Within ~2× bar interval
    const maxBars = bars.length;
    if (maxBars >= 2) {
      const t0 = new Date(bars[0].time).getTime();
      const t1 = new Date(bars[1].time).getTime();
      const interval = Math.abs(t1 - t0);
      if (bestDiff > interval * 3) return -1;
    }
    return best;
  };

  // Build paths for the line indicators
  const linePath = (values: (number | null)[] | undefined, yFn: (v: number) => number) => {
    if (!values) return "";
    let path = "";
    let started = false;
    values.forEach((v, i) => {
      if (v == null || i >= bars.length) {
        started = false;
        return;
      }
      const x = xFor(i);
      const y = yFn(v);
      if (!started) {
        path += `M${x.toFixed(1)},${y.toFixed(1)}`;
        started = true;
      } else {
        path += ` L${x.toFixed(1)},${y.toFixed(1)}`;
      }
    });
    return path;
  };

  // More gridlines now that the price pane is ~50% taller — denser
  // y-axis ticks make small price movements legible.
  const priceTicks = 10;
  const priceTickValues = Array.from({ length: priceTicks + 1 }, (_, i) =>
    priceMax - (i * (priceMax - priceMin)) / priceTicks,
  );

  return (
    <div className="border border-bg-border bg-black">
      <div className="flex flex-col gap-2 border-b border-bg-border bg-bg-secondary/70 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="font-mono text-xs font-semibold uppercase tracking-[0.22em] text-accent-amber">
            {data.underlying} · {data.timeframe.replace("minute", "m")}
          </div>
          <div className="mt-0.5 text-xs text-text-secondary">
            {bars.length} bars · {trades.length} trade markers · last bar {fmtTime(bars[bars.length - 1]?.time)}
          </div>
        </div>
        <div className="flex flex-wrap gap-3 font-mono text-[10px] uppercase tracking-[0.14em] text-text-muted">
          <span className="text-accent-cyan">BB ±2σ(20)</span>
          <span className="text-accent-amber">EMA(50)</span>
          <span>MACD(12,26,9)</span>
          <span>RSI(14)</span>
        </div>
      </div>

      <div className="relative overflow-x-auto">
        <svg
          className="block h-auto w-full min-w-0 sm:min-w-[700px]"
          viewBox={`0 0 ${chartWidth} ${totalHeight}`}
          preserveAspectRatio="xMidYMid meet"
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const xPct = (e.clientX - rect.left) / rect.width;
            const localX = xPct * chartWidth - LEFT_PAD;
            if (localX < 0 || localX > plotWidth) {
              setHover(null);
              return;
            }
            const idx = Math.min(bars.length - 1, Math.max(0, Math.round(localX / slot - 0.5)));
            setHover(idx);
          }}
        >
          <rect width={chartWidth} height={totalHeight} fill="#030712" />

          {/* Price pane grid + ticks */}
          <rect x={LEFT_PAD} y={TOP_PAD} width={plotWidth} height={CHART_HEIGHT} fill="#050816" stroke="#1e2d45" />
          {priceTickValues.map((tick, i) => (
            <g key={`pt-${i}`}>
              <line x1={LEFT_PAD} x2={chartWidth - RIGHT_PAD} y1={yPrice(tick)} y2={yPrice(tick)} stroke="#1e2d45" strokeDasharray="3 6" opacity="0.5" />
              <text x={LEFT_PAD - 6} y={yPrice(tick) + 4} fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="13" textAnchor="end">
                {fmt(tick, 2)}
              </text>
            </g>
          ))}

          {/* Bollinger Bands */}
          {ind.bb_upper ? (
            <>
              <path d={linePath(ind.bb_upper, yPrice)} stroke="#22d3ee" strokeWidth="1" fill="none" opacity="0.65" />
              <path d={linePath(ind.bb_lower, yPrice)} stroke="#22d3ee" strokeWidth="1" fill="none" opacity="0.65" />
              <path d={linePath(ind.bb_middle, yPrice)} stroke="#22d3ee" strokeWidth="0.6" fill="none" opacity="0.45" strokeDasharray="4 3" />
            </>
          ) : null}

          {/* EMA(50) */}
          {ind.ema50 ? (
            <path d={linePath(ind.ema50, yPrice)} stroke="#fbbf24" strokeWidth="1.4" fill="none" opacity="0.85" />
          ) : null}

          {/* Candlesticks */}
          {bars.map((bar, i) => {
            const x = xFor(i);
            const up = bar.close >= bar.open;
            const color = up ? "#10b981" : "#ef4444";
            const yHigh = yPrice(bar.high);
            const yLow = yPrice(bar.low);
            const yOpen = yPrice(bar.open);
            const yClose = yPrice(bar.close);
            const bodyTop = Math.min(yOpen, yClose);
            const bodyHeight = Math.max(1, Math.abs(yOpen - yClose));
            return (
              <g key={`c-${i}`} opacity={hover != null && hover !== i ? 0.55 : 1}>
                <line x1={x} x2={x} y1={yHigh} y2={yLow} stroke={color} strokeWidth="1" />
                <rect
                  x={x - candleWidth / 2}
                  y={bodyTop}
                  width={candleWidth}
                  height={bodyHeight}
                  fill={up ? color : color}
                  opacity={up ? 0.85 : 1}
                />
              </g>
            );
          })}

          {/* Hover crosshair */}
          {hover != null && bars[hover] ? (
            <line
              x1={xFor(hover)}
              x2={xFor(hover)}
              y1={TOP_PAD}
              y2={volY0 + VOLUME_HEIGHT}
              stroke="#94a3b8"
              strokeDasharray="2 3"
              opacity="0.5"
            />
          ) : null}

          {/* Trade markers — entry filled, exit hollow */}
          {trades.map((trade, idx) => {
            const i = tradeToIndex(trade.time);
            if (i < 0 || !bars[i]) return null;
            const color = strategyColors[trade.strategy] || "#94a3b8";
            const x = xFor(i);
            // Place entries below the bar's low (pointing up), exits above the high (pointing down).
            const yRef = trade.type === "entry" ? yPrice(bars[i].low) + 12 : yPrice(bars[i].high) - 12;
            const size = 6;
            const pts =
              trade.type === "entry"
                ? `${x},${yRef - size} ${x - size},${yRef + size} ${x + size},${yRef + size}`
                : `${x},${yRef + size} ${x - size},${yRef - size} ${x + size},${yRef - size}`;
            return (
              <Fragment key={`tr-${idx}`}>
                <polygon
                  points={pts}
                  fill={trade.type === "entry" ? color : "none"}
                  stroke={color}
                  strokeWidth="1.4"
                >
                  <title>
                    {`${STRATEGY_LABELS[trade.strategy]} · ${trade.type.toUpperCase()}\n` +
                      `${fmtTime(trade.time)}\n` +
                      (trade.option_type ? `${trade.option_type} ${trade.strike ?? ""} @ ${fmt(trade.premium ?? 0)}\n` : "") +
                      (trade.pnl != null ? `P&L ₹${fmt(trade.pnl, 0)}\n` : "") +
                      (trade.reason ? `Reason: ${trade.reason}` : "")}
                  </title>
                </polygon>
              </Fragment>
            );
          })}

          {/* MACD pane */}
          <rect x={LEFT_PAD} y={macdY0} width={plotWidth} height={MACD_HEIGHT} fill="#050816" stroke="#1e2d45" />
          <text x={LEFT_PAD - 6} y={macdY0 + 14} fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="12" textAnchor="end">
            MACD
          </text>
          {/* Zero line */}
          <line x1={LEFT_PAD} x2={chartWidth - RIGHT_PAD} y1={yMacd(0)} y2={yMacd(0)} stroke="#1e2d45" />
          {/* Histogram */}
          {(ind.macd_histogram ?? []).map((v, i) => {
            if (v == null || i >= bars.length) return null;
            const x = xFor(i);
            const y0 = yMacd(0);
            const y1 = yMacd(v);
            const top = Math.min(y0, y1);
            const h = Math.max(1, Math.abs(y1 - y0));
            return (
              <rect
                key={`mh-${i}`}
                x={x - candleWidth / 2}
                y={top}
                width={candleWidth}
                height={h}
                fill={v >= 0 ? "#10b981" : "#ef4444"}
                opacity="0.55"
              />
            );
          })}
          <path d={linePath(ind.macd, yMacd)} stroke="#22d3ee" strokeWidth="1.4" fill="none" />
          <path d={linePath(ind.macd_signal, yMacd)} stroke="#fbbf24" strokeWidth="1.2" fill="none" />

          {/* RSI pane */}
          <rect x={LEFT_PAD} y={rsiY0} width={plotWidth} height={RSI_HEIGHT} fill="#050816" stroke="#1e2d45" />
          <text x={LEFT_PAD - 6} y={rsiY0 + 14} fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="12" textAnchor="end">
            RSI
          </text>
          {[30, 50, 70].map((lvl) => (
            <g key={`rl-${lvl}`}>
              <line
                x1={LEFT_PAD}
                x2={chartWidth - RIGHT_PAD}
                y1={yRsi(lvl)}
                y2={yRsi(lvl)}
                stroke={lvl === 50 ? "#1e2d45" : "#3b82f6"}
                strokeDasharray={lvl === 50 ? "" : "2 4"}
                opacity={lvl === 50 ? 0.6 : 0.35}
              />
              <text x={chartWidth - RIGHT_PAD + 4} y={yRsi(lvl) + 4} fill="#64748b" fontFamily="JetBrains Mono" fontSize="11">
                {lvl}
              </text>
            </g>
          ))}
          <path d={linePath(ind.rsi, yRsi)} stroke="#a855f7" strokeWidth="1.3" fill="none" />

          {/* Volume pane */}
          <rect x={LEFT_PAD} y={volY0} width={plotWidth} height={VOLUME_HEIGHT} fill="#050816" stroke="#1e2d45" />
          <text x={LEFT_PAD - 6} y={volY0 + 14} fill="#94a3b8" fontFamily="JetBrains Mono" fontSize="12" textAnchor="end">
            Vol
          </text>
          {bars.map((bar, i) => {
            const x = xFor(i);
            const y = yVol(bar.volume);
            const h = Math.max(1, volY0 + VOLUME_HEIGHT - y);
            const up = bar.close >= bar.open;
            return (
              <rect
                key={`v-${i}`}
                x={x - candleWidth / 2}
                y={y}
                width={candleWidth}
                height={h}
                fill={up ? "#10b981" : "#ef4444"}
                opacity="0.5"
              />
            );
          })}

          {/* X-axis labels — sparse */}
          {bars.length > 0
            ? Array.from({ length: 6 }, (_, k) => Math.floor((bars.length - 1) * (k / 5))).map((i, k) => {
                const x = xFor(i);
                return (
                  <text
                    key={`xt-${k}`}
                    x={x}
                    y={volY0 + VOLUME_HEIGHT + 16}
                    fill="#94a3b8"
                    fontFamily="JetBrains Mono"
                    fontSize="12"
                    textAnchor="middle"
                  >
                    {new Date(bars[i].time).toLocaleString("en-IN", {
                      day: "2-digit",
                      month: "short",
                      hour: "2-digit",
                      minute: "2-digit",
                      hour12: false,
                      timeZone: "Asia/Kolkata",
                    })}
                  </text>
                );
              })
            : null}
        </svg>

        {/* Hover tooltip */}
        {hover != null && bars[hover] ? (
          <div className="pointer-events-none absolute left-4 top-3 border border-bg-border bg-black/85 px-3 py-2 font-mono text-[11px]">
            <div className="text-text-muted">{fmtTime(bars[hover].time)}</div>
            <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5">
              <span className="text-text-muted">O</span><span className="text-right">{fmt(bars[hover].open)}</span>
              <span className="text-text-muted">H</span><span className="text-right">{fmt(bars[hover].high)}</span>
              <span className="text-text-muted">L</span><span className="text-right">{fmt(bars[hover].low)}</span>
              <span className="text-text-muted">C</span>
              <span className={`text-right ${bars[hover].close >= bars[hover].open ? "text-accent-green" : "text-accent-red"}`}>
                {fmt(bars[hover].close)}
              </span>
              <span className="text-text-muted">Vol</span><span className="text-right">{fmt(bars[hover].volume, 0)}</span>
              {ind.macd?.[hover] != null ? (
                <>
                  <span className="text-text-muted">MACD</span><span className="text-right">{fmt(ind.macd[hover]!)}</span>
                </>
              ) : null}
              {ind.rsi?.[hover] != null ? (
                <>
                  <span className="text-text-muted">RSI</span><span className="text-right">{fmt(ind.rsi[hover]!, 1)}</span>
                </>
              ) : null}
              {ind.ema50?.[hover] != null ? (
                <>
                  <span className="text-text-muted">EMA50</span><span className="text-right">{fmt(ind.ema50[hover]!)}</span>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default ChartsWorkbench;
