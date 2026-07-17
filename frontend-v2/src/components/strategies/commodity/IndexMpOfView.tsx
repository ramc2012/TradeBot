"use client";

/**
 * Index Market-Profile view (NIFTY / BANKNIFTY) for the MP+Order Flow desk.
 *
 * Reuses existing index spot candles via /api/commodity/index-mpof to render a
 * full per-instrument Market Profile: candles with POC/VAH/VAL/IB level lines
 * plus a TPO histogram. Order Flow (CVD/VWAP) needs index-futures volume
 * (spot has none), so it's flagged "coming soon" until futures candles exist.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, BarChart3, CandlestickChart } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, StatusBadge, formatIST, formatNumber, tone } from "@/components/desk-ui";
import { LiveOrderFlowTape } from "@/components/mpof";
import { CandleChart, CHART, type CandleBar, type ChartPriceLine } from "@/components/strategies/shared";
import { getCommodityIndexMpof } from "@/lib/api";
import { underlyingToTapeSymbol } from "@/lib/marketSymbols";

type Bar = { time: string; open: number; high: number; low: number; close: number; volume: number };
type TpoRow = { price: number; count: number };
type IndexMpof = {
  symbol?: string;
  timeframe?: string;
  tick_size?: number;
  session_date?: string;
  last_price?: number;
  profile?: {
    poc: number; vah: number; val: number;
    ib_high: number; ib_low: number;
    day_high: number; day_low: number;
    single_prints: number[]; poor_high: boolean; poor_low: boolean;
    tpo: TpoRow[];
  };
  orderflow?: { available: boolean; cvd: number | null; vwap: number | null };
  bars?: Bar[];
  session_bars?: Bar[];
  detail?: string | null;
};

const TFS = ["5minute", "15minute", "30minute"] as const;
type Tf = (typeof TFS)[number];
const tfLabel = (t: string) => t.replace("minute", "m");

export function IndexMpOfView({ symbol }: { symbol: string }) {
  const [tf, setTf] = useState<Tf>("30minute");
  const q = useQuery({
    queryKey: ["index-mpof", symbol, tf],
    queryFn: async () => (await getCommodityIndexMpof(symbol, tf)).data as IndexMpof,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const d = q.data;
  const prof = d?.profile;
  const bars = useMemo<CandleBar[]>(
    () => (d?.bars ?? []).map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })),
    [d?.bars],
  );

  const priceLines = useMemo<ChartPriceLine[]>(() => {
    if (!prof) return [];
    const out: ChartPriceLine[] = [];
    if (prof.poc) out.push({ price: prof.poc, color: CHART.amber, title: "POC" });
    if (prof.vah) out.push({ price: prof.vah, color: CHART.blue, title: "VAH", dashed: true });
    if (prof.val) out.push({ price: prof.val, color: CHART.blue, title: "VAL", dashed: true });
    if (prof.ib_high) out.push({ price: prof.ib_high, color: CHART.muted, title: "IBH", dashed: true });
    if (prof.ib_low) out.push({ price: prof.ib_low, color: CHART.muted, title: "IBL", dashed: true });
    return out;
  }, [prof]);

  const detail = d?.detail && !bars.length ? d.detail : null;
  const ibRange = prof ? prof.ib_high - prof.ib_low : null;
  const dayRange = prof ? prof.day_high - prof.day_low : null;

  return (
    <div className="space-y-4 p-4">
      {/* KPI strip */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Last" value={formatNumber(d?.last_price, 1)} detail={d?.session_date ? `session ${d.session_date}` : ""} />
        <MetricTile label="POC" value={formatNumber(prof?.poc, 1)} color="text-accent-amber" />
        <MetricTile label="Value area" value={prof ? `${formatNumber(prof.val, 0)}–${formatNumber(prof.vah, 0)}` : "—"} detail="VAL – VAH" />
        <MetricTile label="Initial balance" value={prof ? `${formatNumber(prof.ib_low, 0)}–${formatNumber(prof.ib_high, 0)}` : "—"} detail={ibRange != null ? `${formatNumber(ibRange, 0)} pts` : ""} />
        <MetricTile label="Day range" value={prof ? `${formatNumber(prof.day_low, 0)}–${formatNumber(prof.day_high, 0)}` : "—"} detail={dayRange != null ? `${formatNumber(dayRange, 0)} pts` : ""} />
        <MetricTile label="vs POC" value={d?.last_price != null && prof?.poc != null ? (d.last_price >= prof.poc ? "above" : "below") : "—"} color={d?.last_price != null && prof?.poc != null ? tone(d.last_price - prof.poc) : undefined} detail={prof ? `${prof.poor_high ? "poor-high " : ""}${prof.poor_low ? "poor-low" : ""}` || "balanced" : ""} />
      </section>

      {detail ? (
        <div className="rounded-xl border border-accent-amber/40 bg-accent-amber/10 px-4 py-3 text-sm text-accent-amber">{detail}</div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Section
          title={`${symbol} · candles + MP levels`}
          icon={<CandlestickChart size={16} />}
          rightSlot={
            <div className="flex items-center gap-2">
              <div className="flex rounded-lg border border-bg-border bg-bg-primary/30 p-0.5">
                {TFS.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTf(t)}
                    className={`rounded-md px-2 py-0.5 text-[11px] font-medium transition-colors ${tf === t ? "bg-accent-blue/20 text-accent-blue" : "text-text-muted hover:text-text-secondary"}`}
                  >
                    {tfLabel(t)}
                  </button>
                ))}
              </div>
              {q.isFetching ? <StatusBadge label="loading" variant="info" /> : null}
            </div>
          }
        >
          <CandleChart bars={bars} priceLines={priceLines} height={420} showVolume={false} />
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">
            <Legend color={CHART.amber} label="POC" />
            <Legend color={CHART.blue} label="Value area (VAH/VAL)" />
            <Legend color={CHART.muted} label="Initial balance" />
            <span>{bars.length} bars · {tfLabel(d?.timeframe || tf)}</span>
          </div>
        </Section>

        <Section title="Market Profile · TPO" icon={<BarChart3 size={16} />} description={d?.session_date ? `Session ${d.session_date}` : undefined}>
          <TpoHistogram rows={prof?.tpo ?? []} poc={prof?.poc} vah={prof?.vah} val={prof?.val} />
        </Section>
      </div>

      <Section
        title={`${symbol} · streaming order flow`}
        icon={<Activity size={16} />}
        description="Live bid/ask and signed quote-volume changes between completed profile bars."
      >
        <LiveOrderFlowTape symbol={underlyingToTapeSymbol(symbol) ?? symbol} />
      </Section>

      {!d?.orderflow?.available ? (
        <div className="flex items-center gap-2 rounded-xl border border-accent-amber/30 bg-accent-amber/5 px-4 py-2.5 text-[12px] text-text-muted">
          <Activity size={14} className="text-accent-amber" />
          Historical CVD/VWAP remains unavailable when {symbol} spot candles carry no volume. The live panel above is explicitly a quote-tape proxy, not a fabricated footprint.
        </div>
      ) : null}
    </div>
  );
}

// Horizontal TPO histogram — price rows with bar width ∝ TPO count; POC amber,
// value-area rows blue-tinted, rest muted.
function TpoHistogram({ rows, poc, vah, val }: { rows: TpoRow[]; poc?: number; vah?: number; val?: number }) {
  if (!rows.length) {
    return <div className="flex h-[420px] items-center justify-center text-sm text-text-muted">No profile for this session.</div>;
  }
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <div className="max-h-[440px] space-y-[2px] overflow-y-auto pr-1">
      {rows.map((r) => {
        const inVa = val != null && vah != null && r.price >= val && r.price <= vah;
        const isPoc = poc != null && Math.abs(r.price - poc) < 1e-6;
        const w = (r.count / max) * 100;
        return (
          <div key={r.price} className="flex items-center gap-2 font-mono text-[10px]">
            <span className={`w-14 text-right ${isPoc ? "text-accent-amber" : "text-text-muted"}`}>{formatNumber(r.price, 0)}</span>
            <div className="relative h-3 flex-1 overflow-hidden rounded-sm bg-bg-primary/20">
              <div
                className="absolute inset-y-0 left-0 rounded-sm"
                style={{ width: `${Math.max(w, 3)}%`, backgroundColor: isPoc ? CHART.amber : inVa ? "rgba(59,130,246,0.55)" : "rgba(148,163,184,0.3)" }}
              />
            </div>
            <span className="w-5 text-right text-text-muted">{r.count}</span>
          </div>
        );
      })}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-2 w-2.5 rounded-sm" style={{ backgroundColor: color }} aria-hidden />
      {label}
    </span>
  );
}
