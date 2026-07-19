"use client";

/**
 * Institutional Order-flow Workbench — native v2.
 *
 * One instrument at a time (NIFTY / BANKNIFTY / SENSEX / CRUDEOIL) from the
 * shared `/api/orderflow/snapshot` endpoint (the same one the v1 desk used).
 *
 * Tabs:
 *   microstructure → KPI strip + OrderFlowPanel core + intraday footprint
 *                    candles (POC/VAH/VAL/VWAP overlays) + a recharts CVD
 *                    (cumulative-delta) line below.
 *   tape           → time-and-sales tape + DOM ladder + block/whale prints.
 *   profile        → Market-Profile TPO histogram + heatmap reference levels.
 *
 * HONESTY (2026-07-19): the QUOTE stream and the OHLCV bars behind this desk
 * are observed; every buy/sell ATTRIBUTION on top of them (CVD, footprint
 * delta, aggressive buy/sell, tape "side") is INFERRED. No wired Indian retail
 * broker pushes public aggressor-tagged trade prints — see
 * `backend/analytics/orderflow.py` — and `market_ticks` stores no per-trade
 * size or side. Labels here must never claim trade-print provenance.
 */
import { useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Gauge,
  Layers3,
  ListOrdered,
  Waves,
} from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatNumber,
  formatPct,
  formatSignedNumber,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import {
  CandleChart,
  CHART,
  MarketProfileChart,
  OrderFlowPanel,
  type CandleBar,
  type ChartPriceLine,
  type OrderFlow,
} from "@/components/strategies/shared";
import { api as apiClient } from "@/lib/api";
import { classifyDataMode, classifySourceGrade, dataModeLabel, sourceGradeLabel } from "@/lib/market-semantics";

// ─── Types (built from the live /api/orderflow/snapshot shape) ──────────────

type FootprintBar = {
  symbol?: string;
  timestamp?: string;
  label?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  total_volume?: number;
  buy_volume?: number;
  sell_volume?: number;
  delta?: number;
  cumulative_delta?: number;
  imbalance?: number;
};

type Metrics = {
  spread?: number;
  mid_price?: number;
  micro_price?: number;
  top_imbalance?: number;
  depth_imbalance?: number;
  delta?: number;
  cumulative_delta?: number;
  vwap?: number;
  vwap_drift?: number;
  queue_pressure?: number;
  trade_imbalance?: number;
  order_flow_imbalance?: number;
  book_pressure?: number;
  toxicity_score?: number;
  timing_confidence?: number;
  execution_aggression?: string;
};

type MarketProfile = {
  poc?: number;
  vah?: number;
  val?: number;
  initial_balance_high?: number;
  initial_balance_low?: number;
  day_type?: string | null;
  trend?: string | null;
};

type DomLevel = { price: number; quantity: number; cumulative_quantity: number };
type DomLadder = {
  bids?: DomLevel[];
  asks?: DomLevel[];
  mid_price?: number | null;
  spread?: number;
  level_count?: number;
};

type TapeRow = {
  timestamp?: string | null;
  label?: string;
  price?: number;
  quantity?: number;
  side?: string;
  tone?: "up" | "down" | "neutral";
  is_block?: boolean;
};

type WhaleMarker = {
  id?: string;
  timestamp?: string | null;
  label?: string;
  side?: string;
  direction?: "BULLISH" | "BEARISH" | "NEUTRAL";
  price?: number;
  notional?: number;
  volume?: number;
  score?: number;
  source?: string;
};

type HeatmapLevel = {
  price?: number;
  side?: "bid" | "ask" | "reference";
  label?: string;
  kind?: string;
  quantity?: number | null;
  intensity?: number;
};

type DataQuality = {
  live_mode?: boolean;
  snapshot_mode?: string;
  execution_ready?: boolean;
  degraded_reason?: string | null;
  order_flow_source?: string;
  quote_source?: string;
  tick_history_count?: number;
  trade_print_count?: number;
  stale_data_seconds?: number;
};

type Instrument = {
  symbol?: string;
  display?: string;
  market?: string;
  instrument_proxy?: string;
  price?: number;
  change?: number;
  change_pct?: number;
  timestamp?: string | null;
  age_seconds?: number | null;
  data_quality?: DataQuality;
  source?: Record<string, string | undefined>;
  session?: { date?: string; mode?: string; lot_size?: number; tick_size?: number };
  metrics?: Metrics;
  market_profile?: MarketProfile;
  footprint?: FootprintBar[];
  heatmap?: HeatmapLevel[];
  whales?: WhaleMarker[];
  dom?: DomLadder;
  tape?: TapeRow[];
  synthetic_quote?: boolean;
  raw_bar_count?: number;
  raw_trade_count?: number;
  error?: string | null;
};

type Snapshot = {
  as_of?: string;
  symbols?: string[];
  intervals?: number[] | string[];
  history_sessions?: number;
  instruments?: Instrument[];
};

const SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"];

const TABS = [
  { key: "microstructure", label: "Microstructure", icon: Waves },
  { key: "tape", label: "Tape & DOM", icon: ListOrdered },
  { key: "profile", label: "Profile", icon: Layers3 },
];

// metrics → OrderFlow (shared panel) adapter ──────────────────────────────
function toOrderFlow(m?: Metrics): OrderFlow {
  const x = m || {};
  return {
    spread: x.spread,
    mid_price: x.mid_price,
    micro_price: x.micro_price,
    top_imbalance: x.top_imbalance,
    depth_imbalance: x.depth_imbalance,
    trade_imbalance: x.trade_imbalance,
    order_flow_imbalance: x.order_flow_imbalance,
    book_pressure: x.book_pressure,
    delta: x.delta,
    cumulative_delta: x.cumulative_delta,
    vwap: x.vwap,
    vwap_drift: x.vwap_drift,
    queue_pressure: x.queue_pressure,
    toxicity_score: x.toxicity_score,
    timing_confidence: x.timing_confidence,
  };
}

export default function OrderflowWorkbench() {
  const [activeTab, setActiveTab] = useUrlTab("microstructure");
  const [, startTransition] = useTransition();
  const [symbol, setSymbol] = useState("NIFTY");

  const snapQuery = useQuery({
    queryKey: ["orderflow", "snapshot", symbol],
    queryFn: async () =>
      (
        await apiClient.get("/api/orderflow/snapshot", {
          // Slim the request to only what this desk renders. The `timeframes`
          // multi-TF history was 99% of a ~2MB payload and is never read here —
          // footprint (the rendered series) is built independently of intervals.
          params: {
            symbols: symbol,
            intervals: "30",
            history_sessions: 1,
            include_timeframes: false,
          },
        })
      ).data as Snapshot,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const snap = snapQuery.data;
  const inst = snap?.instruments?.[0];
  const m = inst?.metrics || {};
  const dq = inst?.data_quality || {};
  const bars = inst?.footprint || [];

  const chgUp = Number(inst?.change ?? 0) >= 0;

  return (
    <DeskShell
      title="Order-flow Workbench"
      description="Quote-derived microstructure: footprint delta, CVD, DOM ladder, time-and-sales, market-profile levels. Buy/sell sides are inferred — no aggressor trade tape exists on this feed."
      // Freshness reflects the DATA timestamp, not the server render time
      // (snap.as_of is "now" on every poll and would always read "1s ago").
      asOf={inst?.timestamp ?? snap?.as_of}
      asOfLabel="Data"
      asOfStaleSeconds={90}
      asOfCriticalSeconds={300}
      isFetching={snapQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/orderflow"
      rightSlot={
        <div className="flex items-center gap-2">
          <Picker
            label="Symbol"
            value={symbol}
            options={SYMBOLS}
            onChange={(v) => startTransition(() => setSymbol(v))}
          />
        </div>
      }
    >
      {inst?.error ? (
        <Section title="Snapshot error" icon={<AlertTriangle size={16} />}>
          <div className="text-sm text-accent-red">{inst.error}</div>
        </Section>
      ) : null}

      {/* KPI strip — shared across tabs */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <MetricTile
          label="Last"
          value={formatNumber(inst?.price, 1)}
          detail={`${inst?.market || ""} · ${inst?.instrument_proxy || ""}`}
        />
        <MetricTile
          label="Change"
          value={formatSignedNumber(inst?.change, 1)}
          detail={formatPct((inst?.change_pct ?? 0) / 100)}
          color={tone(inst?.change)}
        />
        <MetricTile
          label="Cum. delta"
          value={formatSignedNumber(m.cumulative_delta, 0)}
          detail={`Δ ${formatSignedNumber(m.delta, 0)}`}
          color={tone(m.cumulative_delta)}
        />
        <MetricTile
          label="VWAP"
          value={formatNumber(m.vwap, 1)}
          detail={`drift ${formatSignedNumber(m.vwap_drift, 1)}`}
          color={tone(m.vwap_drift)}
        />
        <MetricTile
          label="Spread"
          value={formatNumber(m.spread, 2)}
          detail={`mid ${formatNumber(m.mid_price, 1)}`}
        />
        <MetricTile
          label="Toxicity"
          value={formatPct(m.toxicity_score, 0)}
          detail={`exec ${m.execution_aggression || "—"}`}
          color={Number(m.toxicity_score ?? 0) > 0.5 ? "text-accent-red" : undefined}
        />
        <MetricTile
          label="Bars · prints"
          value={`${inst?.raw_bar_count ?? 0} · ${inst?.raw_trade_count ?? 0}`}
          detail={dq.snapshot_mode || dataModeLabel(classifyDataMode(dq))}
        />
      </section>

      <div className="mt-3">
        <DataQualityRow dq={dq} inst={inst} chgUp={chgUp} />
      </div>

      {activeTab === "microstructure" ? (
        <div className="mt-4 space-y-4">
          <OrderFlowPanel of={toOrderFlow(m)} />

          <Section
            title="Footprint candles"
            icon={<Activity size={16} />}
            description="Intraday session bars with POC / VAH / VAL / VWAP overlays"
            rightSlot={
              <div className="flex gap-1.5">
                <StatusBadge label={`${bars.length} bars`} variant="neutral" />
                {inst?.market_profile?.day_type ? (
                  <StatusBadge label={inst.market_profile.day_type} variant="info" />
                ) : null}
              </div>
            }
          >
            <CandleChart
              bars={toCandleBars(bars)}
              priceLines={profilePriceLines(inst?.market_profile, m.vwap)}
              height={400}
              showVolume
              // Re-fit the viewport only on symbol change — never on a periodic
              // data refresh, so trader pan/zoom is preserved across polls.
              fitKey={symbol}
            />
          </Section>

          <Section
            title="Cumulative delta (CVD)"
            icon={<Waves size={16} />}
            description="Net signed volume accumulated across the session, with the sign INFERRED FROM QUOTES (no aggressor tape) — divergence vs price flags absorption."
          >
            <CvdChart bars={bars} />
          </Section>
        </div>
      ) : null}

      {activeTab === "tape" ? (
        <div className="mt-4 grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2 space-y-4">
            <TapePanel rows={inst?.tape || []} />
            {(inst?.whales || []).length ? <WhalePanel whales={inst!.whales!} /> : null}
          </div>
          <div>
            <DomPanel dom={inst?.dom} mid={m.mid_price} />
          </div>
        </div>
      ) : null}

      {activeTab === "profile" ? (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <Section
            title="Market profile"
            icon={<Layers3 size={16} />}
            description="TPO distribution with value area, POC, and initial balance"
          >
            <MarketProfileChart
              profile={buildProfile(bars, inst?.market_profile)}
              lastPrice={inst?.price ?? null}
              height={380}
            />
          </Section>
          <div className="space-y-4">
            <ProfileLevels mp={inst?.market_profile} />
            <HeatmapPanel levels={inst?.heatmap || []} />
          </div>
        </div>
      ) : null}
    </DeskShell>
  );
}

// ─── Data-quality / source row ──────────────────────────────────────────────

// The order-flow stream names this workbench accepts as a genuinely live
// QUOTE feed. Deliberately STRICTER than the shared contract's `observed`
// grade — a TimescaleDB history read grades as observed for provenance but is
// not a live stream, and this surface gates execution readiness.
//
// UNCHANGED 2026-07-19: this predicate keeps calling `classifySourceGrade`
// bare (source grading), so the readiness gate behaves exactly as before. Only
// the DISPLAYED grade below is corrected to the flow grade.
const LIVE_OF_SOURCES = new Set(["market_ticks"]);
// Max data age (seconds) for intraday order flow to count as live.
const MAX_LIVE_AGE_SEC = 90;

/** Strict readiness: live quote source AND fresh data AND flow coverage. */
export function evaluateReadiness(dq: DataQuality, inst?: Instrument): {
  ready: boolean;
  notLiveReason: string | null;
} {
  const ofSource = String(dq.order_flow_source || inst?.source?.order_flow || "");
  const ageSec = Number(inst?.age_seconds ?? Infinity);
  const hasMicro =
    Number(dq.tick_history_count ?? 0) > 0 &&
    Number(dq.trade_print_count ?? 0) > 0 &&
    inst?.synthetic_quote !== true;

  // Grade + data mode come from the SHARED contract so this workbench, the
  // Auction desk, MP and Convergence answer "is this real flow?" identically.
  const grade = classifySourceGrade(ofSource);
  const dataMode = classifyDataMode(dq);
  const isLiveSource =
    dq.live_mode === true && grade === "observed" && LIVE_OF_SOURCES.has(ofSource);
  const isFresh = Number.isFinite(ageSec) && ageSec <= MAX_LIVE_AGE_SEC;
  const executionReady = dq.execution_ready !== false && !dq.degraded_reason;

  const ready = isLiveSource && isFresh && hasMicro && executionReady;

  const notLiveReason = ready
    ? null
    : !isLiveSource
      ? dataMode === "historical_replay"
        ? "REPLAY"
        : `SOURCE: ${ofSource || "—"} (${sourceGradeLabel(classifySourceGrade(ofSource, "flow_attribution"))})`
      : !isFresh
        ? `STALE ${formatAge(ageSec)}`
        : !hasMicro
          ? "NO FLOW COVERAGE"
          : dq.degraded_reason || "DEGRADED";

  return { ready, notLiveReason };
}

function DataQualityRow({
  dq,
  inst,
  chgUp,
}: {
  dq: DataQuality;
  inst?: Instrument;
  chgUp: boolean;
}) {
  const { ready, notLiveReason } = evaluateReadiness(dq, inst);
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
      <StatusBadge
        label={ready ? "LIVE · EXECUTION READY" : `NOT LIVE · ${notLiveReason}`}
        variant={ready ? "success" : "error"}
      />
      <StatusBadge label={chgUp ? "up bar" : "down bar"} variant={chgUp ? "success" : "error"} />
      {dq.degraded_reason ? (
        <StatusBadge label={dq.degraded_reason} variant="warn" />
      ) : null}
      <span>flow: {dq.order_flow_source || inst?.source?.order_flow || "—"}</span>
      <span className="text-bg-border">·</span>
      <span>quote: {dq.quote_source || inst?.source?.quote || "—"}</span>
      <span className="text-bg-border">·</span>
      <span>history: {inst?.source?.history || "—"}</span>
      {inst?.age_seconds != null ? (
        <>
          <span className="text-bg-border">·</span>
          <span>age {formatAge(inst.age_seconds)}</span>
        </>
      ) : null}
      {inst?.timestamp ? (
        <>
          <span className="text-bg-border">·</span>
          <span>{formatIST(inst.timestamp)}</span>
        </>
      ) : null}
    </div>
  );
}

// ─── Footprint → CandleChart / overlays ─────────────────────────────────────

function toCandleBars(bars: FootprintBar[]): CandleBar[] {
  return bars
    .filter((b) => Number.isFinite(Number(b.open)) && Number.isFinite(Number(b.close)))
    .map((b) => ({
      time: b.timestamp || "",
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
      volume: Number(b.total_volume ?? 0),
    }));
}

function profilePriceLines(mp?: MarketProfile, vwap?: number): ChartPriceLine[] {
  const out: ChartPriceLine[] = [];
  const push = (price: unknown, color: string, title: string, dashed = false) => {
    const p = Number(price);
    if (Number.isFinite(p) && p > 0) out.push({ price: p, color, title, dashed });
  };
  push(mp?.poc, CHART.amber, "POC");
  push(mp?.vah, CHART.blue, "VAH", true);
  push(mp?.val, CHART.blue, "VAL", true);
  push(mp?.initial_balance_high, CHART.violet, "IBH", true);
  push(mp?.initial_balance_low, CHART.violet, "IBL", true);
  push(vwap, CHART.muted, "VWAP", true);
  return out;
}

// ─── CVD recharts line ──────────────────────────────────────────────────────

function CvdChart({ bars }: { bars: FootprintBar[] }) {
  const data = useMemo(() => {
    let run = 0;
    return bars
      .filter((b) => b.timestamp || b.label)
      .map((b) => {
        // prefer the API-provided cumulative_delta; fall back to a running sum.
        const cd = Number.isFinite(Number(b.cumulative_delta))
          ? Number(b.cumulative_delta)
          : (run += Number(b.delta ?? 0));
        run = cd;
        return {
          label: b.label || formatIST(b.timestamp),
          cvd: Math.round(cd * 100) / 100,
          delta: Math.round(Number(b.delta ?? 0) * 100) / 100,
          close: Number(b.close ?? 0),
        };
      });
  }, [bars]);

  if (!data.length) {
    return (
      <div className="flex h-[200px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        No delta series available.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
        <CartesianGrid stroke={CHART.grid} vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fill: CHART.axis, fontSize: 10 }}
          tickLine={false}
          axisLine={{ stroke: CHART.border }}
          minTickGap={24}
        />
        <YAxis
          tick={{ fill: CHART.axis, fontSize: 10 }}
          tickLine={false}
          axisLine={{ stroke: CHART.border }}
          width={48}
        />
        <Tooltip
          contentStyle={{
            background: CHART.surface,
            border: `1px solid ${CHART.border}`,
            borderRadius: 8,
            fontSize: 11,
          }}
          labelStyle={{ color: CHART.muted }}
        />
        <ReferenceLine y={0} stroke={CHART.border} />
        <Line
          type="monotone"
          dataKey="cvd"
          name="Cum. delta"
          stroke={CHART.green}
          strokeWidth={1.8}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ─── Tape (time-and-sales) ──────────────────────────────────────────────────

function TapePanel({ rows }: { rows: TapeRow[] }) {
  return (
    <Section
      title="Time & sales"
      icon={<ListOrdered size={16} />}
      description="Most-recent rows first — 'side' is inferred from quotes, not a broker aggressor flag; blocks highlighted"
      rightSlot={<StatusBadge label={`${rows.length} rows`} variant="neutral" />}
    >
      <div className="-mx-2 max-h-[420px] overflow-y-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-bg-secondary/90">
            <tr className="border-b border-bg-border/60">
              {["Time", "Price", "Qty", "Side"].map((h, i) => (
                <th
                  key={h}
                  className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${
                    i === 0 ? "text-left" : "text-right"
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
                const sideUp = r.tone === "up" || /buy/i.test(r.side || "");
                const sideDown = r.tone === "down" || /sell/i.test(r.side || "");
                const c = sideUp ? "text-accent-green" : sideDown ? "text-accent-red" : "text-text-secondary";
                return (
                  <tr
                    key={i}
                    className={`border-b border-bg-border/25 hover:bg-bg-primary/20 ${
                      r.is_block ? "bg-accent-amber/5" : ""
                    }`}
                  >
                    <td className="px-2.5 py-1 text-left font-mono text-[11.5px] text-text-secondary whitespace-nowrap">
                      {r.label || formatIST(r.timestamp)}
                      {r.is_block ? (
                        <span className="ml-1.5 rounded bg-accent-amber/15 px-1 text-[9px] uppercase tracking-wide text-accent-amber">
                          blk
                        </span>
                      ) : null}
                    </td>
                    <td className={`px-2.5 py-1 text-right font-mono text-[12px] ${c}`}>
                      {formatNumber(r.price, 1)}
                    </td>
                    <td className="px-2.5 py-1 text-right font-mono text-[12px] text-text-primary">
                      {formatNumber(r.quantity, 0)}
                    </td>
                    <td className={`px-2.5 py-1 text-right font-mono text-[11px] uppercase ${c}`}>
                      {r.side || "—"}
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={4} className="px-2.5 py-8 text-center text-sm text-text-muted">
                  No time-and-sales rows in this snapshot.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ─── DOM ladder ─────────────────────────────────────────────────────────────

function DomPanel({ dom, mid }: { dom?: DomLadder; mid?: number }) {
  const bids = (dom?.bids || []).slice(0, 8);
  const asks = (dom?.asks || []).slice(0, 8);
  const maxQty = Math.max(
    1,
    ...bids.map((b) => Number(b.quantity ?? 0)),
    ...asks.map((a) => Number(a.quantity ?? 0)),
  );

  return (
    <Section
      title="Depth (DOM)"
      icon={<Gauge size={16} />}
      description="Top-of-book ladder"
      rightSlot={
        dom?.spread != null ? (
          <StatusBadge label={`spread ${formatNumber(dom.spread, 2)}`} variant="neutral" />
        ) : null
      }
    >
      {bids.length || asks.length ? (
        <div className="space-y-2">
          {/* asks (descending) */}
          <div className="space-y-1">
            {[...asks].reverse().map((a, i) => (
              <DomRow key={`a${i}`} level={a} side="ask" maxQty={maxQty} />
            ))}
          </div>
          <div className="flex items-center justify-between rounded-lg border border-bg-border bg-bg-primary/25 px-2.5 py-1">
            <span className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Mid</span>
            <span className="font-mono text-sm text-text-primary">
              {formatNumber(dom?.mid_price ?? mid, 1)}
            </span>
          </div>
          {/* bids (descending) */}
          <div className="space-y-1">
            {bids.map((b, i) => (
              <DomRow key={`b${i}`} level={b} side="bid" maxQty={maxQty} />
            ))}
          </div>
        </div>
      ) : (
        <div className="py-8 text-center text-sm text-text-muted">No depth ladder available.</div>
      )}
    </Section>
  );
}

function DomRow({
  level,
  side,
  maxQty,
}: {
  level: DomLevel;
  side: "bid" | "ask";
  maxQty: number;
}) {
  const qty = Number(level.quantity ?? 0);
  const pct = (qty / maxQty) * 100;
  const isBid = side === "bid";
  const barColor = isBid ? "rgba(0,212,163,0.18)" : "rgba(255,71,87,0.18)";
  const txt = isBid ? "text-accent-green" : "text-accent-red";
  return (
    <div className="relative flex items-center justify-between overflow-hidden rounded px-2.5 py-1">
      <div
        className="absolute inset-y-0"
        style={{
          background: barColor,
          width: `${pct}%`,
          [isBid ? "right" : "left"]: 0,
        }}
      />
      <span className={`relative z-10 font-mono text-[12px] ${txt}`}>
        {formatNumber(level.price, 1)}
      </span>
      <span className="relative z-10 font-mono text-[12px] text-text-secondary">
        {formatNumber(qty, 0)}
        <span className="ml-1.5 text-[10px] text-text-muted">
          {formatNumber(level.cumulative_quantity, 0)}
        </span>
      </span>
    </div>
  );
}

// ─── Whale / block prints ───────────────────────────────────────────────────

function WhalePanel({ whales }: { whales: WhaleMarker[] }) {
  return (
    <Section title="Block / whale prints" icon={<AlertTriangle size={16} />}>
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-bg-border/60">
              {["Time", "Label", "Dir", "Price", "Notional", "Score"].map((h, i) => (
                <th
                  key={h}
                  className={`px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted ${
                    i <= 1 ? "text-left" : "text-right"
                  }`}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {whales.map((w, i) => {
              const dirTone =
                w.direction === "BULLISH"
                  ? "success"
                  : w.direction === "BEARISH"
                    ? "error"
                    : "neutral";
              return (
                <tr key={w.id || i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-1.5 text-left font-mono text-[11.5px] text-text-secondary whitespace-nowrap">
                    {w.label || formatIST(w.timestamp)}
                  </td>
                  <td className="px-2.5 py-1.5 text-left text-[12px] text-text-primary">
                    {w.source || w.side || "—"}
                  </td>
                  <td className="px-2.5 py-1.5 text-right">
                    <StatusBadge label={w.direction || "—"} variant={dirTone} />
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">
                    {formatNumber(w.price, 1)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">
                    {formatCompact(w.notional)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-primary">
                    {formatNumber(w.score, 2)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ─── Profile levels + heatmap ───────────────────────────────────────────────

function ProfileLevels({ mp }: { mp?: MarketProfile }) {
  const rows: Array<[string, unknown]> = [
    ["POC", mp?.poc],
    ["VAH", mp?.vah],
    ["VAL", mp?.val],
    ["IB high", mp?.initial_balance_high],
    ["IB low", mp?.initial_balance_low],
  ];
  return (
    <Section title="Reference levels" icon={<Layers3 size={16} />}>
      <div className="grid grid-cols-2 gap-2">
        {rows.map(([k, v]) => (
          <div key={k} className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2">
            <div className="text-[10px] uppercase tracking-[0.12em] text-text-muted">{k}</div>
            <div className="mt-0.5 font-mono text-text-primary">{formatNumber(Number(v), 1)}</div>
          </div>
        ))}
        {mp?.day_type || mp?.trend ? (
          <div className="col-span-2 flex gap-1.5">
            {mp?.day_type ? <StatusBadge label={mp.day_type} variant="info" /> : null}
            {mp?.trend ? <StatusBadge label={mp.trend} variant="neutral" /> : null}
          </div>
        ) : null}
      </div>
    </Section>
  );
}

function HeatmapPanel({ levels }: { levels: HeatmapLevel[] }) {
  const sorted = [...levels]
    .filter((l) => Number.isFinite(Number(l.price)))
    .sort((a, b) => Number(b.price) - Number(a.price));
  return (
    <Section title="Liquidity heatmap" icon={<Waves size={16} />}>
      {sorted.length ? (
        <div className="space-y-1">
          {sorted.map((l, i) => {
            const intensity = Math.max(0, Math.min(1, Number(l.intensity ?? 0)));
            const color =
              l.side === "bid" ? CHART.green : l.side === "ask" ? CHART.red : CHART.blue;
            return (
              <div
                key={i}
                className="relative flex items-center justify-between overflow-hidden rounded px-2.5 py-1"
              >
                <div
                  className="absolute inset-y-0 left-0"
                  style={{ background: color, opacity: 0.16, width: `${intensity * 100}%` }}
                />
                <span className="relative z-10 font-mono text-[12px] text-text-primary">
                  {formatNumber(l.price, 1)}
                </span>
                <span className="relative z-10 flex items-center gap-2 text-[11px] text-text-secondary">
                  <span className="uppercase tracking-wide text-text-muted">{l.label || l.kind}</span>
                  {l.quantity != null ? (
                    <span className="font-mono">{formatCompact(l.quantity)}</span>
                  ) : null}
                  <span className="font-mono text-text-muted">{formatPct(intensity, 0)}</span>
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="py-6 text-center text-sm text-text-muted">No heatmap levels.</div>
      )}
    </Section>
  );
}

// build a TPO-style profile object from footprint volume-at-price for MarketProfileChart
function buildProfile(bars: FootprintBar[], mp?: MarketProfile) {
  const counts: Record<string, number> = {};
  for (const b of bars) {
    const px = Number(b.close);
    const vol = Number(b.total_volume ?? 0);
    if (!Number.isFinite(px) || px <= 0) continue;
    const key = String(Math.round(px));
    counts[key] = (counts[key] || 0) + (vol > 0 ? vol : 1);
  }
  return {
    tpo_counts: counts,
    poc: mp?.poc,
    vah: mp?.vah,
    val: mp?.val,
    ib: { high: mp?.initial_balance_high, low: mp?.initial_balance_low },
  };
}

// ─── small helpers ──────────────────────────────────────────────────────────

function formatCompact(value?: number | null): string {
  const v = Number(value ?? 0);
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(1)}Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(1)}L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
  return `${sign}${abs.toFixed(0)}`;
}

function formatAge(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function Picker({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
      <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <select
        className="bg-transparent outline-none"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o} value={o} className="bg-bg-card text-text-primary">
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}
