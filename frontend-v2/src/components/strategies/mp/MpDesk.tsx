"use client";

/**
 * Market-Profile Intelligence desk — native v2.
 *
 * Tabs:
 *   live      → live TPO profile (MarketProfileChart) + order-flow board +
 *               current setup/signal card + KPI strip
 *   structure → composite 20D/50D + weekly TPO profiles, regime distribution
 *   migration → POC / value-area migration trend (recharts) + setup matrix
 *   drift     → concept-drift Page-Hinkley diagnostics + orderflow-proxy CVD
 *
 * Endpoints (all GET, open on prod 15.206.56.206:8000):
 *   /api/auction-intelligence/mp-analytics        ?underlying&lookback
 *   /api/auction-intelligence/live-snapshot       ?symbol   (market_profile + order_flow + regime + agent_decisions)
 *   /api/auction-intelligence/mp-open-signal       ?underlying (current pending setup)
 *
 * No MP-specific paper endpoints exist, so there is no Performance tab —
 * the MP lane exposes a daily open-signal, not a paper-position book.
 */
import { useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  CheckCircle2,
  Compass,
  Layers3,
  Target,
  TrendingUp,
  Waves,
} from "lucide-react";
import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  DataModeBadge,
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  directionTone,
  formatNumber,
  formatPct,
  regimeTone,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { CandleChart, CHART, MarketProfileChart, OrderFlowPanel, type ChartPriceLine, type OrderFlow } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import { api as apiClient } from "@/lib/api";
import { AuctionInsights, type AuctionInsightsData } from "../auction/AuctionInsights";
import { UniversePicker } from "../auction/UniversePicker";
import { useAuctionUniverse } from "@/hooks/useAuctionUniverse";
import { useSystemState } from "@/hooks/useSystemState";
import { classifySourceGrade, deriveFreshness, liveVerdict, sourceGradeLabel, sourceGradeVariant } from "@/lib/market-semantics";

// ── types (built from the curled prod shapes; everything is nullable) ────────

type TpoRow = { price: number; count: number; letters?: string };

type CompositeProfile = {
  scope?: string;
  lookback_sessions?: number;
  available_sessions?: number;
  integrity_status?: string;
  session_start?: string;
  session_end?: string;
  high_price?: number;
  low_price?: number;
  poc?: number;
  vah?: number;
  val?: number;
  va_width?: number;
  tpo_rows?: TpoRow[];
};

type WeeklyProfile = {
  scope?: string;
  week?: string;
  sessions?: number;
  start_date?: string;
  end_date?: string;
  poc?: number;
  vah?: number;
  val?: number;
  total_tpos?: number;
};

type MigrationSession = {
  date: string;
  poc?: number;
  vah?: number;
  val?: number;
  va_center?: number;
  va_width?: number;
  close?: number;
  close_location?: number;
  poc_shift?: number;
  day_type?: string;
};

type SetupCell = {
  day_type?: string;
  direction?: string;
  strength?: string;
  count?: number;
  win_rate_1d?: number;
  win_rate_3d?: number;
  avg_next_day_move?: number;
  expectancy_1d?: number;
  sharpe_proxy?: number;
};

type DriftSeries = { date: string; rolling_win_rate?: number; ph_stat?: number; n?: number };

type CvdSeries = { date: string; cvd?: number; daily_delta?: number; close?: number };

type MpAnalytics = {
  underlying?: string;
  lookback?: number;
  total_sessions?: number;
  error?: string;
  profiles?: { composite_20d?: CompositeProfile; composite_50d?: CompositeProfile };
  weekly_profiles?: WeeklyProfile[];
  value_migration?: {
    sessions?: MigrationSession[];
    summary?: {
      avg_poc_shift?: number;
      upward_migration_pct?: number;
      downward_migration_pct?: number;
      cumulative_poc_shift?: number;
      avg_va_width?: number;
    };
  };
  regime_history?: {
    distribution?: Array<{ day_type: string; count: number; pct: number }>;
    streaks?: Array<{ day_type: string; length: number; start_date: string; end_date: string }>;
  };
  setup_performance?: {
    total_signals?: number;
    overall_next_day_win_rate?: number;
    cells?: SetupCell[];
    calibration?: Array<{ strength: string; total_signals: number; avg_win_rate_1d: number; avg_win_rate_3d: number }>;
  };
  concept_drift?: {
    drift_detected?: boolean;
    current_rolling_win_rate?: number;
    historical_mean_win_rate?: number;
    drift_magnitude?: number;
    current_state?: string;
    ph_threshold?: number;
    series?: DriftSeries[];
  };
  orderflow_proxy?: {
    series?: CvdSeries[];
    current_cvd?: number;
    summary?: { total_bull_days?: number; total_bear_days?: number; net_cvd?: number; divergences_count?: number };
  };
  data_status?: { source?: string; latest_date?: string; stale_days?: number; live_appended?: boolean };
};

type LiveProfile = {
  symbol?: string;
  session_date?: string;
  open_price?: number;
  high_price?: number;
  low_price?: number;
  close_price?: number;
  total_volume?: number;
  tick_size?: number;
  tpo_counts?: Record<string, number>;
  tpo_letters?: Record<string, string>;
  poc?: number;
  vah?: number;
  val?: number;
  initial_balance_high?: number;
  initial_balance_low?: number;
  single_prints?: number[];
  poor_high?: boolean;
  poor_low?: boolean;
};

type AgentDecision = {
  agent_name?: string;
  action?: string;
  confidence?: number;
  rationale?: string[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  metadata?: Record<string, any>;
};

type LiveSnapshot = {
  auction_insights?: AuctionInsightsData;
  symbol_code?: string;
  session_date?: string;
  request?: { metadata?: { order_flow_source?: string; snapshot_time?: string; snapshot_mode?: string } };
  analysis?: {
    market_profile?: LiveProfile;
    order_flow?: OrderFlow;
    regime?: { label?: string; confidence?: number; allowed_directions?: string[]; reasons?: string[] };
    agent_decisions?: AgentDecision[];
  };
};

type OpenSignal = {
  signal_date?: string;
  trade_date?: string;
  direction?: string;
  reason?: string;
  strength?: string;
  alloc?: number;
  day_type?: string;
  status?: string;
  instruction?: string;
};

type OpenSignalPayload = {
  as_of?: string;
  underlying?: string;
  signals?: OpenSignal[];
  skip_reason?: string | null;
  rag_context?: {
    decision?: string;
    confidence?: number;
    summary?: string;
    reason_codes?: string[];
    case_stats?: { matched_cases?: number; win_rate?: number; expectancy?: number };
  };
};

// ── small palette for day-type chips (matches v1 semantics) ──────────────────

const DAY_TYPE_TONE: Record<string, string> = {
  TREND_UP: CHART.green,
  TREND_DN: CHART.red,
  NORMAL_VAR_UP: "#7ee0c0",
  NORMAL_VAR_DN: "#f7a8b0",
  FAILED_AUCTION: CHART.amber,
  DOUBLE_DIST: CHART.violet,
  NORMAL: "#94a3b8",
  UNKNOWN: "#64748b",
};

const dayTypeColor = (dt?: string) => (dt ? DAY_TYPE_TONE[dt] ?? "#94a3b8" : "#94a3b8");

/**
 * Order-flow source presentation. The GRADE now comes from the shared semantic
 * contract (`classifySourceGrade`) rather than a private map that only knew
 * three strings and silently fell back to "synthetic" for everything else —
 * the desk-local notes stay because they explain what each source means HERE.
 */
// 2026-07-19: notes describe the DERIVATION, not a trade tape. No wired
// broker sends aggressor-tagged prints, so a buy/sell side is never measured
// here — only the underlying quote/book stream differs in granularity.
const OF_SOURCE_NOTE: Record<string, string> = {
  tick_reconstruction_book: "Futures/option L2 book snapshots — real sizes, sides still inferred (no aggressor tape).",
  tick_reconstruction: "Rebuilt from index quote ticks; L2 sizes floored, sides inferred.",
  bar_inference: "Inferred from candle shape — no quote stream behind it at all.",
};

function ofBadgeOf(source: string): { label: string; variant: "success" | "warn" | "error" | "info" | "neutral"; note: string } {
  // Order flow is a buy/sell-ATTRIBUTED feature: grade it as such, so the
  // badge can never read OBSERVED off a quote-only stream. The readiness
  // predicates elsewhere still use the bare source grade — unchanged.
  const grade = classifySourceGrade(source, "flow_attribution");
  return {
    label: sourceGradeLabel(grade).toLowerCase(),
    variant: sourceGradeVariant(grade),
    note: OF_SOURCE_NOTE[source] ?? `Source reported as "${source || "not reported"}".`,
  };
}

const TABS = [
  { key: "auction", label: "Auction lens", icon: Compass },
  { key: "live-stream", label: "Live stream", icon: Activity },
  { key: "live", label: "Live Profile", icon: Compass },
  { key: "structure", label: "Structure", icon: Layers3 },
  { key: "migration", label: "Value Migration", icon: TrendingUp },
  { key: "drift", label: "Drift & CVD", icon: Brain },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
];

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "CRUDEOIL"];
const LOOKBACKS = [30, 45, 60, 90, 120, 180];

const shortDate = (s?: string) => (s ? s.slice(5) : "");
const directionVariant = (d?: string) => (d === "CE" ? "success" : d === "PE" ? "error" : "neutral");

export default function MpDesk() {
  const [activeTab, setActiveTab] = useUrlTab("auction");
  const [, startTransition] = useTransition();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [lookback, setLookback] = useState(60);
  const universeQuery = useAuctionUniverse();
  const symbols = universeQuery.data?.symbols ?? UNDERLYINGS;

  const analyticsQuery = useQuery({
    queryKey: ["mp", "analytics", underlying, lookback],
    enabled: ["structure", "migration", "drift"].includes(activeTab),
    queryFn: async () =>
      (await apiClient.get("/api/auction-intelligence/mp-analytics", { params: { underlying, lookback } })).data as MpAnalytics,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const liveQuery = useQuery({
    queryKey: ["auction", "live", underlying],
    queryFn: async () =>
      (await apiClient.get("/api/auction-intelligence/live-snapshot", { params: { symbol: underlying } })).data as LiveSnapshot,
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const signalQuery = useQuery({
    queryKey: ["mp", "open-signal", underlying],
    enabled: activeTab === "live",
    queryFn: async () =>
      (await apiClient.get("/api/auction-intelligence/mp-open-signal", { params: { underlying } })).data as OpenSignalPayload,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const analytics = analyticsQuery.data;
  // guard against showing stale tape while switching instruments
  const liveSnap = liveQuery.data?.symbol_code && liveQuery.data.symbol_code !== underlying ? undefined : liveQuery.data;
  const live = liveSnap?.analysis;
  const liveProfile = live?.market_profile;
  const orderFlow = live?.order_flow;
  const regime = live?.regime;
  const ofSource = liveSnap?.request?.metadata?.order_flow_source ?? "bar_inference";
  const ofBadge = ofBadgeOf(ofSource);

  const signal = signalQuery.data?.signals?.[0];
  const rag = signalQuery.data?.rag_context;
  const drift = analytics?.concept_drift;
  const setup = analytics?.setup_performance;
  const migration = analytics?.value_migration;

  const driftVariant: "error" | "warn" | "success" =
    drift?.current_state === "drift" ? "error" : drift?.current_state === "recovering" ? "warn" : "success";

  // price-lines for the intraday candle (POC/VAH/VAL/IB from the live profile)
  const priceLines = useMemo<ChartPriceLine[]>(() => {
    if (!liveProfile) return [];
    const lines: ChartPriceLine[] = [];
    if (liveProfile.poc != null) lines.push({ price: liveProfile.poc, color: CHART.amber, title: "POC" });
    if (liveProfile.vah != null) lines.push({ price: liveProfile.vah, color: CHART.green, title: "VAH", dashed: true });
    if (liveProfile.val != null) lines.push({ price: liveProfile.val, color: CHART.blue, title: "VAL", dashed: true });
    if (liveProfile.initial_balance_high != null) lines.push({ price: liveProfile.initial_balance_high, color: CHART.violet, title: "IBH", dashed: true });
    if (liveProfile.initial_balance_low != null) lines.push({ price: liveProfile.initial_balance_low, color: CHART.violet, title: "IBL", dashed: true });
    return lines;
  }, [liveProfile]);

  const asOf = liveSnap?.request?.metadata?.snapshot_time ?? signalQuery.data?.as_of ?? analytics?.data_status?.latest_date;
  const positional = live?.agent_decisions?.find((d) => d.agent_name === "positional") ?? live?.agent_decisions?.[0];
  // What the MP numbers describe: a live auction, a replayed session, or a
  // fabrication from bars. Session state comes from the shared clock, never
  // from a backfill flag.
  const { nseOpen, mcxOpen, feedOnline } = useSystemState();
  const mpSessionOpen = underlying === "CRUDEOIL" ? mcxOpen : nseOpen;
  const mpVerdict = liveVerdict({
    sessionOpen: mpSessionOpen,
    feedOnline,
    dataMode: classifySourceGrade(ofSource) === "bar_inferred" ? "bar_inference" : "live",
    freshness: deriveFreshness(asOf).freshness,
    hasSymbolObservation: !!liveSnap,
  });
  const mpDataMode = mpVerdict.live
    ? "live"
    : classifySourceGrade(ofSource) === "bar_inferred"
      ? "bar_inference"
      : mpSessionOpen
        ? "unknown"
        : "historical_replay";

  return (
    <DeskShell
      title="MP Intelligence"
      description="Live market-profile structure — TPO distribution, value migration, regime history, drift & setup diagnostics. Order-flow sides are inferred from quotes."
      asOf={asOf}
      /* `live_appended` is a BACKFILL-APPEND flag, not liveness — it used to
         light the shell's "armed" dot. Data honesty now lives in the
         DataModeBadge below; the shell no longer claims anything. */
      isFetching={liveQuery.isFetching || analyticsQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/mp-intelligence"
      rightSlot={
        <div className="flex items-center gap-2">
          <DataModeBadge mode={mpDataMode} title={`order-flow source: ${ofSource} · ${ofBadge.note}`} />
          <UniversePicker value={underlying} symbols={symbols} onChange={(v) => startTransition(() => setUnderlying(v))} />
          <Picker label="Lookback" value={String(lookback)} options={LOOKBACKS.map((l) => `${l}`)} suffix="d" onChange={(v) => startTransition(() => setLookback(Number(v)))} />
        </div>
      }
    >
      {activeTab === "auction" ? <AuctionInsights data={liveSnap?.auction_insights} decisions={live?.agent_decisions} /> : null}
      {liveQuery.error ? <Section title="Snapshot unavailable"><p className="text-sm text-accent-amber">Shared auction history is unavailable for {underlying}. Data readiness is required before paper entry.</p></Section> : null}
      {/* Historical research metrics are separate from the current auction. */}
      {activeTab !== "auction" ? <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <MetricTile label="Live POC" value={formatNumber(liveProfile?.poc, 1)} detail={`VA ${formatNumber(liveProfile?.val, 0)}–${formatNumber(liveProfile?.vah, 0)}`} color="text-accent-amber" />
        <MetricTile label="Regime" value={regime?.label ? regime.label.replace(/_/g, " ") : "—"} detail={regime?.confidence != null ? `conf ${formatPct(regime.confidence)}` : undefined} />
        <MetricTile label="Open signal" value={signal?.direction ?? "—"} detail={signal?.day_type?.replace(/_/g, " ") ?? signalQuery.data?.skip_reason ?? "no setup"} color={directionTone(signal?.direction)} />
        <MetricTile label="1d win rate" value={formatPct(setup?.overall_next_day_win_rate, 1, { asPercent: true })} detail={`n${setup?.total_signals ?? 0}`} color={tone((setup?.overall_next_day_win_rate ?? 50) - 50)} />
        <MetricTile label="Cum POC shift" value={formatNumber(migration?.summary?.cumulative_poc_shift, 0)} detail={`up ${formatPct(migration?.summary?.upward_migration_pct, 0, { asPercent: true })}`} color={tone(migration?.summary?.cumulative_poc_shift)} />
        <MetricTile label="Net CVD" value={formatNumber(analytics?.orderflow_proxy?.summary?.net_cvd, 2)} detail={`${analytics?.orderflow_proxy?.summary?.total_bull_days ?? 0}↑ / ${analytics?.orderflow_proxy?.summary?.total_bear_days ?? 0}↓`} color={tone(analytics?.orderflow_proxy?.summary?.net_cvd)} />
        <MetricTile label="Drift state" value={drift?.current_state ? drift.current_state.toUpperCase() : "—"} detail={`roll ${formatPct(drift?.current_rolling_win_rate, 0, { asPercent: true })}`} color={drift?.current_state === "drift" ? "text-accent-red" : drift?.current_state === "stable" ? "text-accent-green" : "text-accent-amber"} />
      </section> : null}

      <div className="mt-4">
        {analytics?.error ? (
          <Section>
            <div className="py-8 text-center text-sm text-text-muted">{analytics.error}. Ensure MP data exists for {underlying}.</div>
          </Section>
        ) : null}

        {activeTab === "live" ? (
          <LiveTab
            liveProfile={liveProfile}
            orderFlow={orderFlow}
            priceLines={priceLines}
            ofBadge={ofBadge}
            regime={regime}
            signal={signal}
            skipReason={signalQuery.data?.skip_reason}
            rag={rag}
            positional={positional}
          />
        ) : null}

        {activeTab === "structure" ? (
          <StructureTab
            composite20={analytics?.profiles?.composite_20d}
            composite50={analytics?.profiles?.composite_50d}
            weekly={analytics?.weekly_profiles}
            distribution={analytics?.regime_history?.distribution}
            streaks={analytics?.regime_history?.streaks}
            lastPrice={liveProfile?.close_price ?? liveProfile?.poc}
          />
        ) : null}

        {activeTab === "migration" ? <MigrationTab migration={migration} setup={setup} /> : null}

        {activeTab === "drift" ? <DriftTab drift={drift} orderflow={analytics?.orderflow_proxy} /> : null}
        {activeTab === "signal-quality" ? (
          <SignalQualityTab laneKeys={["market_intelligence", "auction_intelligence"]} title="Market-profile signal validation" />
        ) : null}
      {activeTab === "live-stream" ? (
          <StrategyLiveStream
            title="Market Profile"
            watchlist={symbols.map((symbol) => ({ symbol }))}
            positionSources={["mp"]}
          />
        ) : null}
      </div>
    </DeskShell>
  );
}

// ── Live tab ─────────────────────────────────────────────────────────────────

function LiveTab({
  liveProfile,
  orderFlow,
  priceLines,
  ofBadge,
  regime,
  signal,
  skipReason,
  rag,
  positional,
}: {
  liveProfile?: LiveProfile;
  orderFlow?: OrderFlow;
  priceLines: ChartPriceLine[];
  ofBadge: { label: string; variant: "success" | "warn" | "error" | "info" | "neutral"; note: string };
  regime?: NonNullable<LiveSnapshot["analysis"]>["regime"];
  signal?: OpenSignal;
  skipReason?: string | null;
  rag?: OpenSignalPayload["rag_context"];
  positional?: AgentDecision;
}) {
  // The live profile exposes a daily OHLC + TPO histogram (no intraday bars
  // are returned). Synthesise a single session candle so the CandleChart shows
  // the day's range with the POC/VAH/VAL/IB price-lines overlaid.
  const sessionBar = useMemo(() => {
    if (!liveProfile || liveProfile.open_price == null) return [];
    return [
      {
        time: liveProfile.session_date || new Date().toISOString().slice(0, 10),
        open: liveProfile.open_price,
        high: liveProfile.high_price ?? liveProfile.open_price,
        low: liveProfile.low_price ?? liveProfile.open_price,
        close: liveProfile.close_price ?? liveProfile.open_price,
        volume: liveProfile.total_volume,
      },
    ];
  }, [liveProfile]);

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <Section title="Live TPO profile" icon={<BarChart3 size={16} />} rightSlot={liveProfile?.session_date ? <StatusBadge label={liveProfile.session_date} variant="neutral" /> : null}>
          {liveProfile?.tpo_counts && Object.keys(liveProfile.tpo_counts).length ? (
            <MarketProfileChart profile={liveProfile} lastPrice={liveProfile.close_price ?? liveProfile.poc} height={360} />
          ) : (
            <div className="py-10 text-center text-sm text-text-muted">No live TPO profile (market closed or desk idle).</div>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5">
            {liveProfile?.poor_high ? <StatusBadge label="poor high" variant="warn" /> : null}
            {liveProfile?.poor_low ? <StatusBadge label="poor low" variant="warn" /> : null}
            {liveProfile?.single_prints?.length ? <StatusBadge label={`${liveProfile.single_prints.length} single prints`} variant="info" /> : null}
          </div>
        </Section>

        <Section title="Session range & value" icon={<Activity size={16} />}>
          {sessionBar.length ? (
            <CandleChart bars={sessionBar} priceLines={priceLines} height={300} showVolume={false} />
          ) : (
            <div className="py-10 text-center text-sm text-text-muted">No session OHLC available.</div>
          )}
          <div className="mt-3 grid grid-cols-3 gap-2 text-center text-[12px] md:grid-cols-6">
            <Tile label="Open" value={formatNumber(liveProfile?.open_price, 1)} />
            <Tile label="High" value={formatNumber(liveProfile?.high_price, 1)} />
            <Tile label="Low" value={formatNumber(liveProfile?.low_price, 1)} />
            <Tile label="POC" value={formatNumber(liveProfile?.poc, 1)} />
            <Tile label="IBH" value={formatNumber(liveProfile?.initial_balance_high, 1)} />
            <Tile label="IBL" value={formatNumber(liveProfile?.initial_balance_low, 1)} />
          </div>
        </Section>
      </div>

      <SetupCard signal={signal} skipReason={skipReason} rag={rag} regime={regime} positional={positional} />

      <Section title="Order flow (sides inferred from quotes)" icon={<Waves size={16} />} rightSlot={<StatusBadge label={ofBadge.label} variant={ofBadge.variant} />} description={ofBadge.note}>
        {orderFlow ? <OrderFlowPanel of={orderFlow} /> : <div className="py-8 text-center text-sm text-text-muted">No live order-flow snapshot (market closed or desk idle).</div>}
      </Section>
    </div>
  );
}

function SetupCard({
  signal,
  skipReason,
  rag,
  regime,
  positional,
}: {
  signal?: OpenSignal;
  skipReason?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  regime?: any;
  rag?: OpenSignalPayload["rag_context"];
  positional?: AgentDecision;
}) {
  const ragDecision = rag?.decision ?? "—";
  const ragVariant: "success" | "warn" | "error" | "neutral" =
    ragDecision === "approve" || ragDecision === "act" ? "success" : ragDecision === "warn" ? "warn" : ragDecision === "block" || ragDecision === "reject" ? "error" : "neutral";

  return (
    <Section title="Current setup" icon={<Target size={16} />} rightSlot={
      <div className="flex gap-1.5">
        {regime?.label ? <span className={`rounded-full border px-2 py-0.5 text-[11px] ${regimeTone(regime.label)}`}>{String(regime.label).replace(/_/g, " ")}</span> : null}
        <StatusBadge label={signal?.direction ? `${signal.direction} setup` : skipReason ? "skip" : "no setup"} variant={directionVariant(signal?.direction)} />
      </div>
    }>
      {signal ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-2 lg:col-span-1">
            <div className="flex items-end justify-between">
              <span className={`text-2xl font-semibold ${directionTone(signal.direction)}`}>{signal.direction ?? "—"}</span>
              <StatusBadge label={signal.strength ?? "base"} variant="info" />
            </div>
            <div className="grid grid-cols-3 gap-2 text-center text-[12px]">
              <Tile label="Day type" value={signal.day_type?.replace(/_/g, " ") ?? "—"} />
              <Tile label="Alloc" value={signal.alloc != null ? formatPct(signal.alloc) : "—"} />
              <Tile label="Trade" value={signal.trade_date ?? "—"} />
            </div>
            {signal.status ? <StatusBadge label={signal.status.replace(/_/g, " ")} variant="warn" /> : null}
          </div>
          <div className="lg:col-span-2 space-y-3">
            {signal.instruction ? (
              <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-[12.5px] leading-6 text-text-secondary">{signal.instruction}</div>
            ) : null}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">RAG gate</span>
              <StatusBadge label={ragDecision} variant={ragVariant} />
              {rag?.confidence != null ? <span className="text-[12px] text-text-muted">conf {formatPct(rag.confidence)}</span> : null}
              {rag?.case_stats?.matched_cases != null ? (
                <span className="text-[12px] text-text-muted">{rag.case_stats.matched_cases} cases · wr {formatPct(rag.case_stats.win_rate)}</span>
              ) : null}
            </div>
            {rag?.summary ? <div className="text-[12px] text-text-muted">{rag.summary}</div> : null}
          </div>
        </div>
      ) : (
        <div className="text-sm text-text-muted">{skipReason ? `No setup — ${skipReason}.` : "No pending MP setup for the next session."}</div>
      )}

      {positional?.rationale?.length ? (
        <div className="mt-4 border-t border-bg-border/40 pt-3">
          <div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            <span>{positional.agent_name ?? "agent"} · {positional.action ?? "—"}</span>
            {positional.confidence != null ? <span className="text-text-secondary">conf {formatPct(positional.confidence)}</span> : null}
          </div>
          <ul className="mt-2 space-y-1.5">
            {positional.rationale.slice(0, 4).map((r, i) => (
              <li key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
                <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />
                {r}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

// ── Structure tab ────────────────────────────────────────────────────────────

function StructureTab({
  composite20,
  composite50,
  weekly,
  distribution,
  streaks,
  lastPrice,
}: {
  composite20?: CompositeProfile;
  composite50?: CompositeProfile;
  weekly?: WeeklyProfile[];
  distribution?: Array<{ day_type: string; count: number; pct: number }>;
  streaks?: Array<{ day_type: string; length: number; start_date: string; end_date: string }>;
  lastPrice?: number;
}) {
  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <ProfileColumn label="Composite 20D" profile={composite20} lastPrice={lastPrice} />
        <ProfileColumn label="Composite 50D" profile={composite50} lastPrice={lastPrice} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Day-type distribution" icon={<Layers3 size={16} />}>
          {distribution?.length ? (
            <div className="space-y-1.5">
              {distribution.map((d) => (
                <div key={d.day_type} className="flex items-center gap-2">
                  <div className="h-3 rounded" style={{ width: `${Math.max(d.pct, 3)}%`, minWidth: 4, background: dayTypeColor(d.day_type) }} />
                  <span className="whitespace-nowrap text-[11px] text-text-secondary">
                    {d.day_type.replace(/_/g, " ")} <span className="text-text-muted">{d.pct}% · {d.count}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-text-muted">No distribution data.</div>
          )}
          {streaks?.length ? (
            <div className="mt-4 border-t border-bg-border/40 pt-3">
              <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Notable streaks</div>
              <div className="mt-2 space-y-1">
                {streaks.slice(0, 5).map((s, i) => (
                  <div key={i} className="flex items-center gap-2 text-[12px]">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: dayTypeColor(s.day_type) }} />
                    <span className="font-medium text-text-secondary">{s.length}× {s.day_type.replace(/_/g, " ")}</span>
                    <span className="text-text-muted">{shortDate(s.start_date)}→{shortDate(s.end_date)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </Section>

        <Section title="Weekly profiles" icon={<BarChart3 size={16} />}>
          <MiniTable
            head={["Week", "Sessions", "POC", "VAH", "VAL", "TPOs"]}
            rows={(weekly ?? []).slice().reverse().map((w) => [
              w.week ?? `${shortDate(w.start_date)}–${shortDate(w.end_date)}`,
              String(w.sessions ?? "—"),
              formatNumber(w.poc, 0),
              formatNumber(w.vah, 0),
              formatNumber(w.val, 0),
              String(w.total_tpos ?? "—"),
            ])}
          />
        </Section>
      </div>
    </div>
  );
}

function ProfileColumn({ label, profile, lastPrice }: { label: string; profile?: CompositeProfile; lastPrice?: number }) {
  return (
    <Section
      title={label}
      icon={<Compass size={16} />}
      rightSlot={
        <StatusBadge
          label={profile?.integrity_status ?? "—"}
          variant={profile?.integrity_status === "complete" ? "success" : "warn"}
        />
      }
      description={profile?.session_start ? `${profile.session_start} → ${profile.session_end} · ${profile.available_sessions ?? "?"} sessions` : undefined}
    >
      {profile?.tpo_rows?.length ? (
        <MarketProfileChart profile={profile} lastPrice={lastPrice ?? profile.poc} height={340} />
      ) : (
        <div className="py-10 text-center text-sm text-text-muted">No composite profile.</div>
      )}
      <div className="mt-2 grid grid-cols-4 gap-2 text-center text-[12px]">
        <Tile label="POC" value={formatNumber(profile?.poc, 0)} />
        <Tile label="VAH" value={formatNumber(profile?.vah, 0)} />
        <Tile label="VAL" value={formatNumber(profile?.val, 0)} />
        <Tile label="VA width" value={formatNumber(profile?.va_width, 0)} />
      </div>
    </Section>
  );
}

// ── Migration tab ────────────────────────────────────────────────────────────

function MigrationTab({
  migration,
  setup,
}: {
  migration?: MpAnalytics["value_migration"];
  setup?: MpAnalytics["setup_performance"];
}) {
  const sessions = migration?.sessions ?? [];
  const summary = migration?.summary;
  const calibration = setup?.calibration ?? [];
  const cells = setup?.cells ?? [];

  const priceDomain = useMemo<[number, number]>(() => {
    const all = sessions.flatMap((s) => [s.vah, s.val, s.poc]).filter((v): v is number => v != null);
    if (!all.length) return [0, 1];
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const pad = Math.max((hi - lo) * 0.08, 25);
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  }, [sessions]);

  const shiftDomain = useMemo<[number, number]>(() => {
    const all = sessions.map((s) => s.poc_shift).filter((v): v is number => v != null && v !== 0);
    const abs = all.length ? Math.max(...all.map(Math.abs), 10) : 50;
    return [-(abs + 10), abs + 10];
  }, [sessions]);

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="Cum POC shift" value={formatNumber(summary?.cumulative_poc_shift, 0)} color={tone(summary?.cumulative_poc_shift)} />
        <MetricTile label="Upward migration" value={formatPct(summary?.upward_migration_pct, 1, { asPercent: true })} color="text-accent-green" />
        <MetricTile label="Avg VA width" value={formatNumber(summary?.avg_va_width, 0)} />
        <MetricTile label="Avg POC shift" value={formatNumber(summary?.avg_poc_shift, 2)} color={tone(summary?.avg_poc_shift)} />
      </section>

      <Section title="POC & value area migration" icon={<TrendingUp size={16} />} description={`Last ${sessions.length} sessions · Y zoomed to data range`}>
        {sessions.length ? (
          <ResponsiveContainer width="100%" height={240}>
            <ComposedChart data={sessions} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
              <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: CHART.axis }} interval="preserveStartEnd" />
              <YAxis domain={priceDomain} tick={{ fontSize: 9, fill: CHART.axis }} width={52} tickFormatter={(v) => v.toLocaleString()} />
              <Tooltip contentStyle={{ background: CHART.surface, border: `1px solid ${CHART.border}`, fontSize: 11, borderRadius: 8 }} />
              <Area dataKey="vah" name="VAH" stroke={CHART.green} fill={`${CHART.green}1f`} strokeWidth={1.4} dot={false} />
              <Area dataKey="val" name="VAL" stroke={CHART.blue} fill={`${CHART.blue}1f`} strokeWidth={1.4} dot={false} />
              <Line dataKey="poc" name="POC" stroke={CHART.amber} strokeWidth={2.4} dot={false} />
              <Line dataKey="va_center" name="VA centre" stroke={CHART.violet} strokeWidth={1.3} strokeDasharray="3 2" dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="py-8 text-center text-sm text-text-muted">No value-migration data.</div>
        )}
      </Section>

      <Section title="Session-on-session POC shift" icon={<Activity size={16} />}>
        {sessions.length ? (
          <ResponsiveContainer width="100%" height={140}>
            <ComposedChart data={sessions} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
              <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: CHART.axis }} interval="preserveStartEnd" />
              <YAxis domain={shiftDomain} tick={{ fontSize: 9, fill: CHART.axis }} width={44} tickFormatter={(v) => (v > 0 ? "+" : "") + v} />
              <Tooltip contentStyle={{ background: CHART.surface, border: `1px solid ${CHART.border}`, fontSize: 11, borderRadius: 8 }} formatter={(v: number) => [(v > 0 ? "+" : "") + v.toFixed(0), "POC shift"]} />
              <ReferenceLine y={0} stroke={CHART.axis} />
              <Bar dataKey="poc_shift" name="POC shift" maxBarSize={10}>
                {sessions.map((s, i) => (
                  <Cell key={i} fill={(s.poc_shift ?? 0) >= 0 ? CHART.green : CHART.red} />
                ))}
              </Bar>
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="py-6 text-center text-sm text-text-muted">No data.</div>
        )}
      </Section>

      <div className="grid gap-4 lg:grid-cols-[0.8fr_1.2fr]">
        <Section title="Conviction calibration" icon={<Target size={16} />} description="signal strength vs realised win rate">
          {calibration.length ? (
            <div className="grid grid-cols-2 gap-2">
              {calibration.map((c) => (
                <div key={c.strength} className="rounded-xl border border-bg-border bg-bg-primary/15 p-2.5 text-center">
                  <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{c.strength}</div>
                  <div className={`text-lg font-semibold font-mono ${tone((c.avg_win_rate_1d ?? 50) - 50)}`}>{formatPct(c.avg_win_rate_1d, 0, { asPercent: true })}</div>
                  <div className="text-[9.5px] text-text-muted">3d {formatPct(c.avg_win_rate_3d, 0, { asPercent: true })} · n{c.total_signals}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-text-muted">No calibration data.</div>
          )}
        </Section>

        <Section title="Setup matrix" icon={<BarChart3 size={16} />} description="day-type × direction × strength → empirical edge">
          <MiniTable
            head={["Day type", "Dir", "Str", "N", "WR 1d", "WR 3d", "Avg move", "Exp"]}
            rows={[...cells]
              .sort((a, b) => (b.win_rate_1d ?? 0) - (a.win_rate_1d ?? 0))
              .map((c) => [
                <span key="dt" style={{ color: dayTypeColor(c.day_type) }}>{c.day_type?.replace(/_/g, " ") ?? "—"}</span>,
                <StatusBadge key="d" label={c.direction ?? "—"} variant={directionVariant(c.direction)} />,
                c.strength ?? "—",
                String(c.count ?? 0),
                <span key="w1" className={tone((c.win_rate_1d ?? 50) - 50)}>{formatPct(c.win_rate_1d, 0, { asPercent: true })}</span>,
                formatPct(c.win_rate_3d, 0, { asPercent: true }),
                <span key="m" className={tone(c.avg_next_day_move)}>{formatNumber(c.avg_next_day_move, 0)}</span>,
                <span key="e" className={tone(c.expectancy_1d)}>{formatNumber(c.expectancy_1d, 0)}</span>,
              ])}
          />
        </Section>
      </div>
    </div>
  );
}

// ── Drift & CVD tab ──────────────────────────────────────────────────────────

function DriftTab({
  drift,
  orderflow,
}: {
  drift?: MpAnalytics["concept_drift"];
  orderflow?: MpAnalytics["orderflow_proxy"];
}) {
  const series = drift?.series ?? [];
  const state = drift?.current_state ?? "stable";
  const stateColor = state === "drift" ? CHART.amber : state === "recovering" ? CHART.amber : CHART.green;
  const cvd = orderflow?.series ?? [];

  const wrDomain = useMemo<[number, number]>(() => {
    const vals = series.map((s) => s.rolling_win_rate).filter((v): v is number => v != null);
    if (!vals.length) return [40, 60];
    const lo = Math.min(...vals, 45);
    const hi = Math.max(...vals, 55);
    const pad = Math.max((hi - lo) * 0.15, 3);
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  }, [series]);

  const cvdDomain = useMemo<[number, number]>(() => {
    const vals = cvd.map((s) => s.cvd).filter((v): v is number => v != null);
    if (!vals.length) return [-1, 1];
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.1, 0.5);
    return [lo - pad, hi + pad];
  }, [cvd]);

  return (
    <div className="space-y-4">
      <Section>
        <div className="flex items-center gap-3 rounded-xl p-3" style={{ background: `${stateColor}14`, border: `1px solid ${stateColor}44` }}>
          {state === "drift" ? <AlertTriangle size={20} style={{ color: stateColor }} /> : <CheckCircle2 size={20} style={{ color: stateColor }} />}
          <div>
            <div className="text-sm font-semibold" style={{ color: stateColor }}>
              {state === "drift" ? "Concept drift detected" : state === "recovering" ? "Signal performance below mean" : "Signal performance stable"}
            </div>
            <div className="mt-0.5 text-xs text-text-muted">
              Rolling win rate <span className="font-mono text-text-secondary">{formatPct(drift?.current_rolling_win_rate, 1, { asPercent: true })}</span> vs historical{" "}
              <span className="font-mono text-text-secondary">{formatPct(drift?.historical_mean_win_rate, 1, { asPercent: true })}</span> · deviation{" "}
              <span className={`font-mono ${tone(drift?.drift_magnitude)}`}>{drift?.drift_magnitude != null ? (drift.drift_magnitude > 0 ? "+" : "") + drift.drift_magnitude.toFixed(1) : "—"}</span>
            </div>
          </div>
        </div>
      </Section>

      <Section title="Rolling win rate & Page-Hinkley statistic" icon={<Brain size={16} />} description={`PH threshold ${formatNumber(drift?.ph_threshold, 0)} · ${series.length} points`}>
        {series.length ? (
          <ResponsiveContainer width="100%" height={220}>
            <ComposedChart data={series} margin={{ top: 6, right: 48, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
              <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: CHART.axis }} interval="preserveStartEnd" />
              <YAxis yAxisId="wr" domain={wrDomain} tick={{ fontSize: 9, fill: CHART.violet }} width={40} tickFormatter={(v) => v.toFixed(0)} />
              <YAxis yAxisId="ph" orientation="right" tick={{ fontSize: 9, fill: CHART.amber }} width={36} />
              <Tooltip contentStyle={{ background: CHART.surface, border: `1px solid ${CHART.border}`, fontSize: 11, borderRadius: 8 }} />
              <ReferenceLine yAxisId="wr" y={50} stroke={CHART.axis} strokeDasharray="3 3" />
              {drift?.ph_threshold != null ? <ReferenceLine yAxisId="ph" y={drift.ph_threshold} stroke={`${CHART.amber}88`} strokeDasharray="4 2" /> : null}
              <Area yAxisId="wr" dataKey="rolling_win_rate" name="Win rate" stroke={CHART.violet} fill={`${CHART.violet}22`} strokeWidth={2} dot={false} />
              <Line yAxisId="ph" dataKey="ph_stat" name="PH stat" stroke={CHART.amber} strokeWidth={1.5} dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="py-8 text-center text-sm text-text-muted">Need more sessions with directional signals for drift analysis.</div>
        )}
      </Section>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="Current CVD" value={formatNumber(orderflow?.current_cvd ?? orderflow?.summary?.net_cvd, 2)} color={tone(orderflow?.current_cvd ?? orderflow?.summary?.net_cvd)} />
        <MetricTile label="Bull days" value={String(orderflow?.summary?.total_bull_days ?? 0)} color="text-accent-green" />
        <MetricTile label="Bear days" value={String(orderflow?.summary?.total_bear_days ?? 0)} color="text-accent-red" />
        <MetricTile label="Divergences" value={String(orderflow?.summary?.divergences_count ?? 0)} color="text-accent-amber" />
      </section>

      <Section title="Cumulative volume delta vs close" icon={<Waves size={16} />} description="CVD approximation from daily auction structure (NSE MBO not available)">
        {cvd.length ? (
          <ResponsiveContainer width="100%" height={210}>
            <ComposedChart data={cvd} margin={{ top: 6, right: 52, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
              <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: CHART.axis }} interval="preserveStartEnd" />
              <YAxis yAxisId="cvd" domain={cvdDomain} tick={{ fontSize: 9, fill: CHART.violet }} width={44} tickFormatter={(v) => v.toFixed(1)} />
              <YAxis yAxisId="close" orientation="right" domain={["auto", "auto"]} tick={{ fontSize: 9, fill: CHART.blue }} width={52} tickFormatter={(v) => v.toLocaleString()} />
              <Tooltip contentStyle={{ background: CHART.surface, border: `1px solid ${CHART.border}`, fontSize: 11, borderRadius: 8 }} />
              <ReferenceLine yAxisId="cvd" y={0} stroke={CHART.axis} strokeDasharray="2 2" />
              <Area yAxisId="cvd" dataKey="cvd" name="CVD" stroke={CHART.violet} fill={`${CHART.violet}22`} strokeWidth={2} dot={false} />
              <Line yAxisId="close" dataKey="close" name="Close" stroke={CHART.blue} strokeWidth={1.4} dot={false} opacity={0.75} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="py-8 text-center text-sm text-text-muted">No orderflow-proxy data.</div>
        )}
      </Section>
    </div>
  );
}

// ── shared primitives (mirrors GannDesk idioms) ──────────────────────────────

function Picker({ label, value, options, onChange, suffix }: { label: string; value: string; options: string[]; onChange: (v: string) => void; suffix?: string }) {
  return (
    <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
      <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <select className="bg-transparent outline-none" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o} className="bg-bg-card text-text-primary">{o}{suffix ?? ""}</option>
        ))}
      </select>
    </label>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className="font-mono text-text-primary">{value}</div>
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
              <th key={i} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i === 0 ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((r, i) => (
            <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              {r.map((c, j) => (
                <td key={j} className={`px-2.5 py-1.5 text-[12px] ${j === 0 ? "text-left text-text-primary" : "text-right text-text-secondary"} font-mono whitespace-nowrap`}>{c}</td>
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
