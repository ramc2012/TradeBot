"use client";

/**
 * Auction Intelligence desk — native v2.
 *
 * Tabs:
 *   auction     → KPI strip + Market Profile (TPO) + Order flow + agent
 *                 decisions + regime / execution-plan / risk / NTM-VolX cards
 *   gates       → A/B/C validation results + canary readiness (tab-gated fetch)
 *   performance → native paper performance (stats, equity, R-dist, trade book)
 *   memory      → RAG case-memory gate (matched cases, win-rate, expectancy)
 */
import { useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Brain, Compass, Gauge, ListChecks, Map as MapIcon, Radio, ShieldAlert, TrendingUp, Waves } from "lucide-react";

import {
  DeskShell,
  TransportBadge,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatMoney,
  formatNumber,
  formatPct,
  regimeTone,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { LastUpdated } from "@/components/common/LastUpdated";
import { OfSourceBadge, OrderFlowPulse, ProfileLadder, type FlowTrade } from "@/components/mpof";
import { MarketProfileChart, OrderFlowPanel, PaperPerformance } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import { useStrategyPositionsStream, selectStrategySlice } from "@/hooks/useStrategyPositionsStream";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { useSystemState } from "@/hooks/useSystemState";
import { classifyDataMode, deriveFreshness, liveVerdict, type DataMode } from "@/lib/market-semantics";
import { createStrategySnapshotSocket } from "@/lib/websocket";
import type { PaperPosition, PositionsPayload } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { AgentDecisions } from "./AgentDecisions";
import { GatesPanel } from "./GatesPanel";
import { RagMemory } from "./RagMemory";
import { MotionTab } from "./motion/MotionTab";
import type { ExecutionStep, NtmVolx, Regime, Risk, Snapshot } from "./types";

const TABS = [
  { key: "performance", label: "Performance", icon: TrendingUp },
  { key: "auction", label: "Auction", icon: MapIcon },
  { key: "motion", label: "In Motion", icon: Waves },
  { key: "gates", label: "Gates", icon: ListChecks },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
  { key: "live-stream", label: "Live stream", icon: Radio },
];

const DEFAULT_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "CRUDEOIL"];

/** Map a paper position into the canonical TradeBook/StrategyStats shape. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function normalizePosition(p: any): PaperPosition {
  return {
    ...p,
    direction: p.direction ?? p.option_type ?? p.signal_action,
    regime: p.regime ?? p.regime_entry ?? p.regime_last,
    confidence: p.confidence ?? p.entry_confidence ?? p.latest_confidence,
    underlying: p.underlying ?? p.underlying_symbol,
  };
}

export default function AuctionDesk() {
  // Open positions (PaperPerformance shows the open book) is the headline view on open.
  const [activeTab, setActiveTab] = useUrlTab("performance");
  const [, startTransition] = useTransition();
  const [symbol, setSymbol] = useState("NIFTY");

  // Live auction snapshot (market profile + order flow + regime): 8s WS push
  // with polling fallback, via the generic /ws/strategy-snapshot channel.
  const liveQuery = useLiveSnapshotQuery<Snapshot>({
    queryKey: ["auction", "live", symbol],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/live-snapshot", { params: { symbol } })).data as Snapshot,
    storageKey: `auction-live-${symbol}`,
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket("auction", symbol, null, (d) => onData(d as Snapshot), onStatusChange),
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const statusQuery = useQuery({
    queryKey: ["auction", "paper-status"],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/paper-status")).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    enabled: activeTab === "performance",
  });

  const positionsQuery = useQuery({
    queryKey: ["auction", "paper-positions"],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/paper-positions", { params: { status: "all" } })).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    enabled: activeTab === "performance",
  });

  const snap = liveQuery.data;
  const analysis = snap?.analysis;
  const mp = analysis?.market_profile;
  const of = analysis?.order_flow;
  const regime = analysis?.regime;
  const spot = snap?.request?.session?.last_price ?? mp?.close_price ?? null;
  const universe = snap?.available_symbols?.length ? snap.available_symbols : DEFAULT_SYMBOLS;
  const status = statusQuery.data as
    | { summary?: Record<string, number>; open_positions?: unknown[]; closed_positions?: unknown[] }
    | undefined;
  const posData = positionsQuery.data as
    | { summary?: Record<string, number>; open_positions?: unknown[]; closed_positions?: unknown[] }
    | undefined;

  // Live open-positions stream (shared /ws/positions-overview channel); active
  // on the performance tab, falls back to the polled paper-positions otherwise.
  const posStream = useStrategyPositionsStream({ enabled: activeTab === "performance" });
  const streamSlice = selectStrategySlice(posStream.data, "auction");
  const streamLive = posStream.isStreamConnected && Boolean(streamSlice);

  const positions = useMemo<PositionsPayload>(() => {
    const src = streamLive ? streamSlice : (posData ?? status);
    return {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      open_positions: ((src?.open_positions as any[]) || []).map(normalizePosition),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      closed_positions: ((src?.closed_positions as any[]) || []).map(normalizePosition),
      summary: (src?.summary as PositionsPayload["summary"]) || undefined,
    };
  }, [streamLive, streamSlice, posData, status]);

  const ds = snap?.data_status;
  const paperMode = (snap?.mode ?? "paper") !== "live";

  // Four separated facts (do NOT collapse into one green light):
  //  · execution_mode → paperMode pill (paper/live)
  //  · scheduler_state → the green "armed" dot = auto-run armed (NOT data live)
  //  · data_mode      → live / historical_replay / bar_inference (data_status)
  //  · freshness      → usable quote age within threshold + market session
  // The ladder below used to be inline here. It now delegates to the SHARED
  // contract (lib/market-semantics) so this desk, Convergence, MP and the
  // Orderflow workbench cannot drift apart. The desk's own precedence
  // (replay → live → closed → stale) is preserved; the only change is that
  // `live data` additionally requires the global feed to be online and the
  // data mode to actually be `live` — a bar-inferred quote source can no
  // longer read green just because `live_mode` was true.
  const { autoRunArmed, nseOpen, mcxOpen, feedOnline } = useSystemState();
  const asOf = (snap?.request?.quote?.timestamp as string | undefined) ?? undefined;
  const sessionOpen = nseOpen || mcxOpen;
  const dataMode = classifyDataMode(ds);
  const { freshness } = deriveFreshness(asOf, { staleAfterSeconds: 90 });
  const verdict = liveVerdict({
    sessionOpen,
    feedOnline,
    dataMode,
    freshness,
    hasSymbolObservation: freshness !== "absent",
  });
  const dataBadge: { label: string; variant: "success" | "warn" | "neutral" | "info" } =
    dataMode === "historical_replay"
      ? { label: "replay", variant: "warn" }
      : verdict.live
        ? { label: "live data", variant: "success" }
        : !sessionOpen
          ? { label: "closed", variant: "neutral" }
          : { label: "stale", variant: "warn" };

  return (
    <DeskShell
      title="Auction Intelligence"
      description="Market-profile auction theory · regime classification · agent sleeves · live microstructure"
      asOf={snap?.request?.quote?.timestamp as string | undefined}
      isFetching={liveQuery.isFetching}
      isLive={autoRunArmed}
      paperMode={paperMode}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/auction-intelligence"
      rightSlot={
        <div className="flex items-center gap-2">
          {activeTab === "performance" ? (
            <TransportBadge connected={streamLive} />
          ) : null}
          <StatusBadge label={dataBadge.label} variant={dataBadge.variant} />
          {ds?.snapshot_mode ? <StatusBadge label={ds.snapshot_mode.replace(/_/g, " ")} variant="info" /> : null}
          <OfSourceBadge source={ds?.order_flow_source} size="sm" />
          <Picker label="Symbol" value={symbol} options={universe} onChange={(v) => startTransition(() => setSymbol(v))} />
        </div>
      }
    >
      {activeTab === "auction" ? (
        <AuctionTab snap={snap} spot={spot} regime={regime} dataMode={dataMode} mp={mp} of={of} />
      ) : null}

      {activeTab === "motion" ? <MotionTab snap={snap} /> : null}


      {activeTab === "gates" ? <GatesPanel symbol={symbol} snapshot={snap} /> : null}

      {activeTab === "performance" ? (
        <PaperPerformance summary={positions.summary} positions={positions} />
      ) : null}

      {activeTab === "memory" ? <RagMemory rag={snap?.rag_context} /> : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["auction_intelligence"]} title="Auction signal validation" />
      ) : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="Auction Intelligence"
          watchlist={universe.map((item) => ({ symbol: String(item) }))}
          positionSources={["auction"]}
        />
      ) : null}
    </DeskShell>
  );
}

function AuctionTab({
  snap,
  spot,
  regime,
  dataMode,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mp,
  of,
}: {
  snap?: Snapshot;
  spot: number | null;
  regime?: Regime;
  /** Derived once by the desk from the shared contract; never re-derived here. */
  dataMode?: DataMode;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mp?: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  of?: any;
}) {
  const analysis = snap?.analysis;
  const prior = analysis?.prior_market_profile;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const meta = (snap?.request as any)?.metadata as Record<string, any> | undefined;
  const ofSource = (snap?.data_status?.order_flow_source as string | undefined) ?? meta?.order_flow_source;
  const asOf = (snap?.request?.quote?.timestamp as string | undefined) ?? meta?.snapshot_time ?? null;
  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <MetricTile label="Spot" value={formatNumber(spot, 1)} detail={snap?.symbol_code} />
        <MetricTile label="Regime" value={regime?.label?.replace(/_/g, " ") || "—"} detail={`conf ${formatPct(regime?.confidence, 0)}`} color={regime?.label?.includes("breakout") || regime?.label?.includes("trend") ? "text-accent-green" : regime?.label?.includes("failed") ? "text-accent-red" : undefined} />
        <MetricTile label="POC" value={formatNumber(mp?.poc, 1)} />
        <MetricTile label="VAH" value={formatNumber(mp?.vah, 1)} color="text-accent-blue" />
        <MetricTile label="VAL" value={formatNumber(mp?.val, 1)} color="text-accent-blue" />
        <MetricTile label="CVD" value={formatNumber(of?.cumulative_delta, 0)} detail={`Δ ${formatNumber(of?.delta, 0)}`} color={tone(of?.cumulative_delta)} />
        <MetricTile label="Toxicity" value={formatPct(of?.toxicity_score, 0)} color={Number(of?.toxicity_score ?? 0) > 0.5 ? "text-accent-red" : "text-text-primary"} />
        <MetricTile label="Bias" value={(regime?.allowed_directions || []).join(" / ") || "—"} />
      </section>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <Section title="Market profile (TPO)" icon={<Compass size={16} />} description={`${mp?.session_date || ""} · POC ${formatNumber(mp?.poc, 1)} · VA ${formatNumber(mp?.val, 0)}–${formatNumber(mp?.vah, 0)}`} rightSlot={<div className="flex flex-wrap items-center gap-2">{mp?.bracket_state ? <StatusBadge label={mp.bracket_state} variant="info" /> : null}<LastUpdated timestamp={asOf} label="snapshot" /></div>}>
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px]">
            <MarketProfileChart profile={mp} lastPrice={spot} height={420} />
            <ProfileLadder
              spot={spot}
              vah={mp?.vah} val={mp?.val} poc={mp?.poc}
              ibHigh={mp?.initial_balance_high} ibLow={mp?.initial_balance_low}
              dayHigh={mp?.high_price} dayLow={mp?.low_price}
              prior={prior ? { vah: prior.vah, val: prior.val, poc: prior.poc } : null}
              singlePrints={mp?.single_prints}
              height={400}
            />
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2.5 md:grid-cols-4">
            <MetricTile size="sm" label="IB range" value={formatNumber(mp?.initial_balance_range, 1)} detail="initial balance" />
            <MetricTile size="sm" label="Day range" value={formatNumber(mp?.day_range, 1)} />
            <MetricTile size="sm" label="Value migration" value={formatNumber(mp?.value_migration, 1)} color={tone(mp?.value_migration)} />
            <MetricTile size="sm" label="POC shift" value={formatNumber(mp?.poc_shift, 1)} />
          </div>
        </Section>

        <RegimeCard regime={regime} risk={analysis?.risk} />
      </div>

      <OrderFlowPanel of={of} source={ofSource} asOf={asOf} />
      <Section title="Tape aggression & absorption" icon={<Waves size={16} />} description="Three-minute aggressive buy/sell pulses from the same clean tape used by the auction decision; low-efficiency high-volume bars are marked as absorption.">
        <OrderFlowPulse trades={(snap?.request?.trades ?? []) as FlowTrade[]} source={ofSource} asOf={asOf} dataMode={dataMode} />
      </Section>

      <AgentDecisions decisions={analysis?.agent_decisions} />

      <div className="grid gap-4 lg:grid-cols-2">
        <ExecutionPlanCard steps={analysis?.execution_plan} />
        <NtmVolxCard ntm={analysis?.ntm_volx} />
      </div>
    </div>
  );
}

function RegimeCard({ regime, risk }: { regime?: Regime; risk?: Risk }) {
  const scorecard = regime?.scorecard || {};
  return (
    <div className="space-y-4">
      <Section title="Regime" icon={<Gauge size={16} />} rightSlot={<StatusBadge label={regime?.label?.replace(/_/g, " ") || "—"} tone={regimeTone(regime?.label)} />}>
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-2xl font-semibold text-text-primary">{formatPct(regime?.confidence, 0)}</span>
            <span className="text-[11.5px] text-text-muted">confidence · allows {(regime?.allowed_directions || []).join(", ") || "—"}</span>
          </div>
          {(regime?.reasons || []).map((r, i) => (
            <div key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />{r}
            </div>
          ))}
          {Object.keys(scorecard).length ? (
            <div className="grid grid-cols-2 gap-2 pt-1">
              {Object.entries(scorecard).map(([k, v]) => (
                <div key={k} className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
                  <div className="text-[9.5px] uppercase tracking-[0.1em] text-text-muted">{k.replace(/_/g, " ")}</div>
                  <div className="font-mono text-[12.5px] text-text-primary">{formatNumber(v, 3)}</div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </Section>

      <Section title="Risk gate" icon={<ShieldAlert size={16} />} rightSlot={<StatusBadge label={risk?.kill_switch ? "kill switch" : risk?.allowed ? "allowed" : "blocked"} variant={risk?.kill_switch ? "error" : risk?.allowed ? "success" : "warn"} />}>
        <div className="space-y-2">
          <div className="text-[12.5px] text-text-secondary">Max size multiplier <span className="font-mono text-text-primary">{formatNumber(risk?.max_size_multiplier, 2)}×</span></div>
          {(risk?.reasons || []).map((r, i) => (
            <div key={i} className="text-[12px] text-text-muted">{r}</div>
          ))}
        </div>
      </Section>
    </div>
  );
}

function ExecutionPlanCard({ steps }: { steps?: ExecutionStep[] }) {
  const rows = steps || [];
  return (
    <Section title="Execution plan" icon={<Activity size={16} />} description="How a triggered decision would route to the broker">
      {rows.length ? (
        <div className="space-y-2.5">
          {rows.map((s, i) => (
            <div key={i} className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2.5">
              <div className="flex items-center justify-between">
                <span className="text-[12.5px] font-semibold text-text-primary">{s.trading_symbol || s.symbol || s.agent_name}</span>
                <div className="flex gap-1.5">
                  {s.action ? <StatusBadge label={s.action} variant={s.action.toUpperCase().includes("BUY") || s.action.toUpperCase() === "LONG" ? "success" : "error"} /> : null}
                  {s.style ? <StatusBadge label={s.style} variant="info" /> : null}
                </div>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11.5px] text-text-muted">
                <span>{s.order_type || "—"} · limit {formatNumber(s.limit_price, 1)}</span>
                <span>qty {formatNumber(s.quantity, 0)}</span>
                <span>slices {s.slices ?? "—"}</span>
                <span>cancel {s.cancel_after_seconds ?? "—"}s</span>
              </div>
              {s.selection_reason ? <div className="mt-1 text-[11px] text-text-secondary">{s.selection_reason}</div> : null}
            </div>
          ))}
        </div>
      ) : (
        <div className="py-6 text-center text-sm text-text-muted">No execution plan — no agent crossed its trigger this snapshot.</div>
      )}
    </Section>
  );
}

function NtmVolxCard({ ntm }: { ntm?: NtmVolx }) {
  if (!ntm) {
    return (
      <Section title="NTM VolX" icon={<TrendingUp size={16} />} description="Near-the-money option-chain pressure">
        <div className="py-6 text-center text-sm text-text-muted">No option-chain snapshot — VolX waits for an ingested NTM chain.</div>
      </Section>
    );
  }
  return (
    <Section title="NTM VolX" icon={<TrendingUp size={16} />} description={`${ntm.underlying || ""} ${ntm.expiry || ""} · ATM ${formatNumber(ntm.atm_strike, 0)}`} rightSlot={<StatusBadge label={ntm.regime || "—"} variant="info" />}>
      <div className="space-y-3">
        <section className="grid grid-cols-2 gap-2.5 md:grid-cols-3">
          <MetricTile size="sm" label="Dominant" value={`${ntm.dominant_side || "—"}`} detail={ntm.directional_bias} />
          <MetricTile size="sm" label="VXR" value={formatNumber(ntm.vxr, 2)} />
          <MetricTile size="sm" label="Net pressure" value={formatPct(ntm.net_pressure, 0)} color={tone(ntm.net_pressure)} />
          <MetricTile size="sm" label="Call / Put notl" value={`${formatMoney(ntm.call_notional, 0)} / ${formatMoney(ntm.put_notional, 0)}`} />
          <MetricTile size="sm" label="Call / Put wall" value={`${formatNumber(ntm.call_wall_strike, 0)} / ${formatNumber(ntm.put_wall_strike, 0)}`} />
          <MetricTile size="sm" label="Pairs" value={String(ntm.pair_count ?? "—")} />
        </section>
        {(ntm.notes || []).map((n, i) => <div key={i} className="text-[11.5px] text-text-muted">{n}</div>)}
      </div>
    </Section>
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
