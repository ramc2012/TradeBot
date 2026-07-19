"use client";

/**
 * Fractal Market Profile desk — native v2.
 *
 * The fractal thesis: a trading day is a self-similar stack of hourly
 * profiles, and value migrates through the session. This desk renders the
 * daily TPO distribution as the centrepiece, the hour-by-hour mini-profile
 * stack beneath it, live order flow, the current setup signal with its
 * value-migration read, paper performance, and the historical replay report.
 *
 * Tabs:
 *   profile     → KPI strip + daily Market-Profile chart + hourly strip + order flow
 *   signal      → current_signal card + value-migration line across hourly profiles
 *   performance → PaperPerformance from paper-positions
 *   replay      → /replay-report (metrics gate, equity curve, setup breakdown, trades)
 */
import { useMemo, useState, useTransition } from "react";
import { Radio as TerminalRadioIcon } from "lucide-react";
import { LaneTerminal } from "@/components/terminal/LaneTerminal";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Crosshair,
  Layers,
  ListTree,
  TrendingUp,
} from "lucide-react";

import {
  DeskShell,
  TransportBadge,
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
  PaperPerformance,
  type ChartPriceLine,
  type OrderFlow,
} from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import { useStrategyPositionsStream, selectStrategySlice } from "@/hooks/useStrategyPositionsStream";
import type { PositionsPayload, PaperSummary, PaperPosition } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { HourlyStrip, type HourlyProfile } from "./HourlyStrip";
import { ValueMigrationChart } from "./ValueMigrationChart";
import { SignalCard, type CurrentSignal } from "./SignalCard";
import { ReplayPanel, type ReplayReport } from "./ReplayPanel";

const TABS = [
  { key: "terminal", label: "Terminal", icon: TerminalRadioIcon },
  { key: "profile", label: "Profile", icon: Layers },
  { key: "signal", label: "Signal", icon: Crosshair },
  { key: "performance", label: "Performance", icon: TrendingUp },
  { key: "replay", label: "Replay", icon: Activity },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
  { key: "live-stream", label: "Live stream", icon: Activity },
];

type ProfileBlock = {
  scope?: string;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  initial_balance_high?: number | null;
  initial_balance_low?: number | null;
  shape?: string | null;
  direction_bias?: string | null;
  day_type?: string | null;
  value_migration?: number | null;
  value_area_overlap?: number | null;
  open_price?: number | null;
  high_price?: number | null;
  low_price?: number | null;
  close_price?: number | null;
  single_prints?: number[] | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

type Snapshot = {
  session?: {
    symbol?: string;
    session_date?: string;
    last_price?: number | null;
    current_hour?: number | null;
    minutes_to_close?: number | null;
  };
  daily_profile?: ProfileBlock | null;
  prior_daily_profile?: ProfileBlock | null;
  hourly_profiles?: HourlyProfile[];
  current_hour_profile?: HourlyProfile | null;
  order_flow?: OrderFlow | null;
  current_signal?: CurrentSignal | null;
  data_status?: { status?: string; detail?: string; [k: string]: unknown } | null;
  supported_symbols?: string[];
  generated_at?: string;
  history_source?: string;
  paper_positions?: PositionsPayload | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

type PositionsResponse = PositionsPayload & {
  summary?: PaperSummary;
};

const SYMBOLS_FALLBACK = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"];

function shapeTone(bias?: string | null): string | undefined {
  if (bias === "bullish") return "text-accent-green";
  if (bias === "bearish") return "text-accent-red";
  return undefined;
}

function dayTypeVariant(dayType?: string | null): "success" | "warn" | "error" | "info" | "neutral" {
  const t = String(dayType || "").toLowerCase();
  if (t.includes("trend")) return "success";
  if (t.includes("balanced") || t.includes("normal")) return "info";
  if (t.includes("neutral") || t.includes("non")) return "warn";
  return "neutral";
}

export default function FractalDesk() {
  // Open positions / paper book is the headline view when the desk opens.
  const [activeTab, setActiveTab] = useUrlTab("performance");
  const [, startTransition] = useTransition();
  const [symbol, setSymbol] = useState("NIFTY");

  const liveQuery = useQuery({
    queryKey: ["fractal", "live", symbol],
    queryFn: async () =>
      (await apiClient.get("/api/fractal-market-profile/live-snapshot", { params: { symbol } })).data as Snapshot,
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const positionsQuery = useQuery({
    queryKey: ["fractal", "positions"],
    queryFn: async () =>
      (await apiClient.get("/api/fractal-market-profile/paper-positions", { params: { status: "all", limit: 200 } }))
        .data as PositionsResponse,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    enabled: activeTab === "performance",
  });

  const replayQuery = useQuery({
    queryKey: ["fractal", "replay", symbol],
    queryFn: async () =>
      (await apiClient.get("/api/fractal-market-profile/replay-report", { params: { symbol } })).data as ReplayReport,
    enabled: activeTab === "replay",
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
  });

  const snap = liveQuery.data;
  const session = snap?.session;
  const daily = snap?.daily_profile || undefined;
  const hourly = snap?.hourly_profiles || [];
  const of = snap?.order_flow || undefined;
  const sig = snap?.current_signal || undefined;
  const lastPrice = session?.last_price ?? sig?.latest_close ?? null;
  const universe = snap?.supported_symbols?.length ? snap.supported_symbols : SYMBOLS_FALLBACK;
  const dataStatus = snap?.data_status;

  // Live open-positions stream (shared /ws/positions-overview channel); active
  // on the performance tab, falls back to the dedicated paper endpoint.
  const posStream = useStrategyPositionsStream({ enabled: activeTab === "performance" });
  const streamSlice = selectStrategySlice(posStream.data, "fractal");
  const streamLive = posStream.isStreamConnected && Boolean(streamSlice);

  // Performance tab: prefer the dedicated /paper-positions endpoint, fall
  // back to the embedded paper_positions block on the live snapshot.
  const positions = useMemo<PositionsPayload>(() => {
    const src = streamLive
      ? streamSlice
      : positionsQuery.data ?? (snap?.paper_positions as PositionsResponse | null) ?? undefined;
    return {
      open_positions: (src?.open_positions as PaperPosition[]) || [],
      closed_positions: (src?.closed_positions as PaperPosition[]) || [],
      summary: src?.summary as PaperSummary | undefined,
    };
  }, [streamLive, streamSlice, positionsQuery.data, snap?.paper_positions]);

  const cvd = of?.cumulative_delta;

  return (
    <DeskShell
      title="Fractal Market Profile"
      description="Self-similar hourly profile stack — value migration, day structure and quote-derived order flow (sides inferred) across the session."
      asOf={snap?.generated_at}
      isFetching={liveQuery.isFetching}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/fractal-market-profile"
      rightSlot={
        <div className="flex items-center gap-2">
          {activeTab === "performance" ? (
            <TransportBadge connected={streamLive} />
          ) : null}
          {dataStatus?.status ? (
            <StatusBadge
              label={String(dataStatus.status)}
              variant={dataStatus.status === "ok" || dataStatus.status === "live" ? "success" : "warn"}
            />
          ) : null}
          <Picker label="Symbol" value={symbol} options={universe} onChange={(v) => startTransition(() => setSymbol(v))} />
        </div>
      }
    >
      {liveQuery.isError ? (
        <Section title="Live snapshot" icon={<AlertTriangle size={16} />}>
          <div className="py-8 text-center text-sm text-accent-red">
            Failed to load fractal snapshot for {symbol}. Retrying…
          </div>
        </Section>
      ) : null}

      {activeTab === "profile" ? (
        <div className="space-y-4">
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
            <MetricTile label="Spot" value={formatNumber(lastPrice, 1)} detail={session?.session_date} />
            <MetricTile
              label="Daily shape"
              value={daily?.shape || "—"}
              detail={`H${session?.current_hour ?? "—"} · ${session?.minutes_to_close ?? 0}m left`}
              color={shapeTone(daily?.direction_bias)}
            />
            <MetricTile label="Day type" value={daily?.day_type || "—"} detail={`bias ${daily?.direction_bias || "—"}`} color={shapeTone(daily?.direction_bias)} />
            <MetricTile label="Direction bias" value={daily?.direction_bias || "—"} detail={`VA overlap ${formatPct(daily?.value_area_overlap, 0)}`} color={shapeTone(daily?.direction_bias)} />
            <MetricTile label="POC" value={formatNumber(daily?.poc, 1)} detail={`migration ${formatSignedNumber(daily?.value_migration, 1)}`} color={tone(daily?.value_migration)} />
            <MetricTile label="VAH" value={formatNumber(daily?.vah, 1)} detail={`IB hi ${formatNumber(daily?.initial_balance_high, 0)}`} />
            <MetricTile label="VAL" value={formatNumber(daily?.val, 1)} detail={`IB lo ${formatNumber(daily?.initial_balance_low, 0)}`} />
            <MetricTile label="CVD" value={formatSignedNumber(cvd, 0)} detail={`delta ${formatSignedNumber(of?.delta, 0)}`} color={tone(cvd)} />
          </section>

          <Section title="Price action" icon={<Activity size={16} />} description="3-min candles with market-profile levels (POC / value area) overlaid">
            <CandleChart
              bars={
                (snap as unknown as { intraday_bars_3m?: { time: string; open: number; high: number; low: number; close: number; volume?: number }[] })
                  ?.intraday_bars_3m || []
              }
              height={300}
              priceLines={[
                daily?.poc != null ? { price: daily.poc, color: CHART.amber, title: "POC" } : null,
                daily?.vah != null ? { price: daily.vah, color: CHART.blue, title: "VAH", dashed: true } : null,
                daily?.val != null ? { price: daily.val, color: CHART.blue, title: "VAL", dashed: true } : null,
              ].filter(Boolean) as ChartPriceLine[]}
            />
          </Section>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
            <Section
              title="Daily market profile"
              icon={<Layers size={16} />}
              description="TPO distribution — value area (VAH–VAL), POC, initial balance, single prints"
              rightSlot={
                <div className="flex gap-1.5">
                  {daily?.shape ? <StatusBadge label={daily.shape} variant="info" /> : null}
                  {daily?.day_type ? <StatusBadge label={daily.day_type} variant={dayTypeVariant(daily.day_type)} /> : null}
                </div>
              }
            >
              <MarketProfileChart profile={daily} lastPrice={lastPrice} height={420} />
              {snap?.prior_daily_profile ? (
                <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-bg-border/40 pt-3 text-[11px] text-text-muted">
                  <span className="uppercase tracking-[0.14em]">Prior day</span>
                  <span className="font-mono">POC {formatNumber(snap.prior_daily_profile.poc, 1)}</span>
                  <span className="font-mono">VA {formatNumber(snap.prior_daily_profile.val, 1)}–{formatNumber(snap.prior_daily_profile.vah, 1)}</span>
                  <span>{snap.prior_daily_profile.shape}</span>
                  <span className={shapeTone(snap.prior_daily_profile.direction_bias)}>{snap.prior_daily_profile.direction_bias}</span>
                </div>
              ) : null}
            </Section>

            <div className="space-y-4">
              <Section title="Hourly profile stack" icon={<ListTree size={16} />} description="Self-similar hour-by-hour mini-profiles — POC migration step shown per cell">
                <HourlyStrip profiles={hourly} />
              </Section>
              {snap?.current_hour_profile ? (
                <Section title="Current hour" icon={<Activity size={16} />}>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <MiniTile label={`H${snap.current_hour_profile.hour_number} shape`} value={snap.current_hour_profile.shape || "—"} />
                    <MiniTile label="POC" value={formatNumber(snap.current_hour_profile.poc, 1)} />
                    <MiniTile label="VAH" value={formatNumber(snap.current_hour_profile.vah, 1)} />
                    <MiniTile label="VAL" value={formatNumber(snap.current_hour_profile.val, 1)} />
                  </div>
                </Section>
              ) : null}
            </div>
          </div>

          <OrderFlowPanel
            of={of}
            source={(dataStatus?.order_flow_source as string | undefined) ?? (of?.source as string | undefined)}
            asOf={snap?.generated_at ?? null}
          />
        </div>
      ) : null}

      {activeTab === "signal" ? (
        <div className="space-y-4">
          <SignalCard signal={sig} lastPrice={lastPrice} />
          <Section title="Value migration" icon={<TrendingUp size={16} />} description="POC / value area drift and the close path across completed hours">
            <ValueMigrationChart profiles={hourly} />
          </Section>
        </div>
      ) : null}

      {activeTab === "performance" ? (
        <PaperPerformance summary={positions.summary} positions={positions} />
      ) : null}

      {activeTab === "replay" ? (
        <ReplayPanel report={replayQuery.data} loading={replayQuery.isFetching} error={replayQuery.isError} />
      ) : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["fractal_market_profile"]} title="Fractal signal validation" />
      ) : null}
      {activeTab === "terminal" ? <LaneTerminal underlyings={universe as unknown as string[]} positions={positions.open_positions} /> : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="Fractal Market Profile"
          watchlist={universe.map((item) => ({ symbol: String(item) }))}
          positionSources={["fractal"]}
        />
      ) : null}
    </DeskShell>
  );
}

function MiniTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className="mt-0.5 font-mono text-[13px] text-text-primary">{value}</div>
    </div>
  );
}

function Picker({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
      <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      <select className="bg-transparent outline-none" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o} className="bg-bg-card text-text-primary">{o}</option>
        ))}
      </select>
    </label>
  );
}
