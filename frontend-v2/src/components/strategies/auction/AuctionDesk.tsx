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
import { Activity, BarChart3, Brain, Compass, Gauge, ListChecks, Map as MapIcon, Radio, ShieldAlert, TrendingUp } from "lucide-react";

import {
  DeskShell,
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
import { MarketProfileChart, OrderFlowPanel, PaperPerformance } from "@/components/strategies/shared";
import { useStrategyPositionsStream, selectStrategySlice } from "@/hooks/useStrategyPositionsStream";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { TerminalPanel } from "@/components/terminal/TerminalPanel";
import { createStrategySnapshotSocket } from "@/lib/websocket";
import type { PaperPosition, PositionsPayload } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { AgentDecisions } from "./AgentDecisions";
import { GatesPanel } from "./GatesPanel";
import { RagMemory } from "./RagMemory";
import type { ExecutionStep, NtmVolx, Regime, Risk, Snapshot, TickProfile } from "./types";

const TABS = [
  { key: "auction", label: "Auction", icon: MapIcon },
  { key: "terminal", label: "Terminal", icon: Radio },
  { key: "gates", label: "Gates", icon: ListChecks },
  { key: "performance", label: "Performance", icon: TrendingUp },
  { key: "memory", label: "Memory", icon: Brain },
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

  return (
    <DeskShell
      title="Auction Intelligence"
      description="Market-profile auction theory · regime classification · agent sleeves · live microstructure"
      asOf={snap?.request?.quote?.timestamp as string | undefined}
      isFetching={liveQuery.isFetching}
      isLive={ds?.live_mode}
      paperMode={paperMode}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/auction-intelligence"
      rightSlot={
        <div className="flex items-center gap-2">
          {activeTab === "performance" ? (
            <StatusBadge label={streamLive ? "● live" : "polling"} variant={streamLive ? "success" : "info"} />
          ) : null}
          {ds?.snapshot_mode ? <StatusBadge label={ds.snapshot_mode.replace(/_/g, " ")} variant="info" /> : null}
          <Picker label="Symbol" value={symbol} options={universe} onChange={(v) => startTransition(() => setSymbol(v))} />
        </div>
      }
    >
      {activeTab === "auction" ? (
        <AuctionTab snap={snap} spot={spot} regime={regime} mp={mp} of={of} />
      ) : null}

      {activeTab === "terminal" ? <TerminalPanel /> : null}

      {activeTab === "gates" ? <GatesPanel symbol={symbol} snapshot={snap} /> : null}

      {activeTab === "performance" ? (
        <PaperPerformance summary={positions.summary} positions={positions} />
      ) : null}

      {activeTab === "memory" ? <RagMemory rag={snap?.rag_context} /> : null}
    </DeskShell>
  );
}

function AuctionTab({
  snap,
  spot,
  regime,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mp,
  of,
}: {
  snap?: Snapshot;
  spot: number | null;
  regime?: Regime;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mp?: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  of?: any;
}) {
  const analysis = snap?.analysis;
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
        <Section title="Market profile (TPO)" icon={<Compass size={16} />} description={`${mp?.session_date || ""} · POC ${formatNumber(mp?.poc, 1)} · VA ${formatNumber(mp?.val, 0)}–${formatNumber(mp?.vah, 0)}`} rightSlot={mp?.bracket_state ? <StatusBadge label={mp.bracket_state} variant="info" /> : null}>
          <MarketProfileChart profile={mp} lastPrice={spot} height={420} />
          <div className="mt-3 grid grid-cols-2 gap-2.5 md:grid-cols-4">
            <MetricTile size="sm" label="IB range" value={formatNumber(mp?.initial_balance_range, 1)} detail="initial balance" />
            <MetricTile size="sm" label="Day range" value={formatNumber(mp?.day_range, 1)} />
            <MetricTile size="sm" label="Value migration" value={formatNumber(mp?.value_migration, 1)} color={tone(mp?.value_migration)} />
            <MetricTile size="sm" label="POC shift" value={formatNumber(mp?.poc_shift, 1)} />
          </div>
        </Section>

        <RegimeCard regime={regime} risk={analysis?.risk} />
      </div>

      <TickProfileCard tick={snap?.tick_market_profile} spot={spot} />

      <OrderFlowPanel of={of} />

      <AgentDecisions decisions={analysis?.agent_decisions} />

      <div className="grid gap-4 lg:grid-cols-2">
        <ExecutionPlanCard steps={analysis?.execution_plan} />
        <NtmVolxCard ntm={analysis?.ntm_volx} />
      </div>
    </div>
  );
}

// Tick-based Market Profile — POC / value area + a price histogram built from
// the index LTP tape (server-side `tick_market_profile`). Complements the
// 30-minute bar TPO with a finer, continuously-developing auction read.
function TickProfileCard({ tick, spot }: { tick?: TickProfile | null; spot: number | null }) {
  if (!tick || !tick.histogram || !tick.total_ticks) {
    return (
      <Section
        title="Market profile (tick)"
        icon={<BarChart3 size={16} />}
        description="Live tick / volume profile from the index tape"
      >
        <div className="py-6 text-center text-sm text-text-muted">
          No tick profile yet — it accrues from the live index tape during the session.
        </div>
      </Section>
    );
  }

  const lo = Number(tick.low_price ?? 0);
  const hi = Number(tick.high_price ?? 0);
  const poc = Number(tick.poc ?? 0);
  const vah = Number(tick.vah ?? 0);
  const val = Number(tick.val ?? 0);
  const last = Number(tick.last_price ?? spot ?? 0);
  const tickSize = Number(tick.tick_size ?? 0.5) || 0.5;

  // Downsample the (often ~600) tick-ladder levels into ~48 display bins.
  const BINS = 48;
  const span = hi - lo;
  const step = span > 0 ? span / BINS : tickSize;
  const bins = Array.from({ length: BINS }, (_, i) => ({ lo: lo + i * step, hi: lo + (i + 1) * step, ticks: 0 }));
  for (const [priceStr, cell] of Object.entries(tick.histogram)) {
    const price = Number(priceStr);
    let idx = step > 0 ? Math.floor((price - lo) / step) : 0;
    if (idx < 0) idx = 0;
    if (idx >= BINS) idx = BINS - 1;
    bins[idx].ticks += Number(cell?.ticks ?? 0);
  }
  const maxTicks = Math.max(1, ...bins.map((b) => b.ticks));

  return (
    <Section
      title="Market profile (tick)"
      icon={<BarChart3 size={16} />}
      description={`${tick.symbol || ""} · ${formatNumber(tick.total_ticks, 0)} ticks · POC ${formatNumber(poc, 1)} · VA ${formatNumber(val, 0)}–${formatNumber(vah, 0)}`}
      rightSlot={<StatusBadge label="live tape" variant="info" />}
    >
      <div className="mb-3 grid grid-cols-2 gap-2.5 md:grid-cols-5">
        <MetricTile size="sm" label="POC" value={formatNumber(poc, 1)} />
        <MetricTile size="sm" label="VAH" value={formatNumber(vah, 1)} color="text-accent-blue" />
        <MetricTile size="sm" label="VAL" value={formatNumber(val, 1)} color="text-accent-blue" />
        <MetricTile size="sm" label="Last" value={formatNumber(last, 1)} color={tone(last - poc)} />
        <MetricTile size="sm" label="Range" value={formatNumber(span, 0)} detail={`${formatNumber(lo, 0)}–${formatNumber(hi, 0)}`} />
      </div>
      {/* Histogram, high → low. Amber = POC, blue = value area, green ring = last. */}
      <div className="space-y-[2px]">
        {bins
          .slice()
          .reverse()
          .map((b, i) => {
            const mid = (b.lo + b.hi) / 2;
            const pct = (b.ticks / maxTicks) * 100;
            const isPoc = poc >= b.lo && poc < b.hi;
            const inVA = mid >= val && mid <= vah;
            const hasLast = last >= b.lo && last < b.hi;
            const barColor = isPoc ? "bg-accent-amber" : inVA ? "bg-accent-blue/70" : "bg-bg-active/60";
            return (
              <div
                key={i}
                className={`flex items-center gap-2 ${hasLast ? "rounded-sm ring-1 ring-accent-green/50" : ""}`}
              >
                <span
                  className={`w-14 shrink-0 text-right font-mono text-[10px] ${
                    isPoc ? "text-accent-amber" : hasLast ? "text-accent-green" : "text-text-muted"
                  }`}
                >
                  {formatNumber(mid, 0)}
                </span>
                <div className="h-3 flex-1 overflow-hidden rounded-sm bg-bg-secondary/20">
                  <div className={`h-full rounded-sm ${barColor}`} style={{ width: `${pct}%` }} />
                </div>
                <span className="w-10 shrink-0 text-right font-mono text-[10px] text-text-muted">
                  {b.ticks ? formatNumber(b.ticks, 0) : ""}
                </span>
              </div>
            );
          })}
      </div>
    </Section>
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
