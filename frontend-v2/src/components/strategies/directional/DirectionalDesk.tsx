"use client";

/**
 * Directional desk — v2 prototype.
 *
 * Built natively on the desk-ui primitives so no helpers are
 * re-implemented. Same backend (port 8000), same endpoints, same
 * payloads as v1 — only the UI shell + composition changes.
 *
 * Tabs:
 *   live      → universe watchlist + policy decision + engine calcs
 *   paper     → paper-trading capital tiles + open/closed/journal sub-tabs
 *   policy    → bandit posterior + size buckets + strategy params
 *   backtest  → bounded backtest (deferred to v1 link for now)
 *
 * What's missing from this v2 prototype today (will land incrementally):
 *   - the recharts bar chart of candidate p_trading_edge
 *   - the Dash hand-off iframe
 *   - dataset coverage cards
 * For those, the "v1 view" header link opens the v1 page in a new tab.
 */
import { useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Banknote, Brain, Gauge, Layers3, Radio, ShieldCheck, TrendingUp } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  decisionTone,
  directionTone,
  formatNumber,
  formatSignedMoney,
  formatPct,
  regimeTone,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { usePaperDeskQueries } from "@/hooks/usePaperDeskQueries";
import { useStrategyPositionsStream, selectStrategySlice } from "@/hooks/useStrategyPositionsStream";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createStrategySnapshotSocket } from "@/lib/websocket";
import { PaperPerformance, GammaDensity } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import type { PositionsPayload } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { TerminalPanel } from "@/components/terminal/TerminalPanel";
import UniverseWatchlist from "./UniverseWatchlist";
import EngineCalculations from "./EngineCalculations";
import PolicyDecisionPanel, { type PolicyBlock } from "./PolicyDecisionPanel";
import PaperTradingTab from "./PaperTradingTab";
import PolicyLearningTab from "./PolicyLearningTab";
import OptionAnalyticsPanel from "./OptionAnalyticsPanel";

const DEFAULT_UNDERLYING = "NIFTY";
const DEFAULT_TIMEFRAME = "5minute";
const DEFAULT_LOOKBACK = 16;

const TABS = [
  { key: "live",      label: "Live overview",      icon: Gauge },
  { key: "terminal",  label: "Terminal",           icon: Radio },
  { key: "analytics", label: "Option analytics",   icon: Layers3 },
  { key: "gamma",     label: "Gamma / GEX",         icon: Layers3 },
  { key: "paper",     label: "Paper trading",      icon: Banknote },
  { key: "policy",    label: "Policy & learning",  icon: Brain },
  { key: "performance", label: "Performance",      icon: TrendingUp },
  { key: "signal-quality", label: "Signal quality", icon: ShieldCheck },
  { key: "live-stream", label: "Live stream", icon: Radio },
];

type ModuleSummary = {
  key?: string;
  label?: string;
  description?: string;
  underlyings?: string[];
  timeframes?: string[];
  automation?: { loop_active?: boolean };
};

type Snapshot = {
  as_of?: string | null;
  underlying?: string;
  timeframe?: string;
  spot_price?: number | null;
  feature_snapshot?: Record<string, number> | null;
  regime?: { label?: string; confidence?: number; trade_allowed?: boolean; reasons?: string[]; preferred_expiry_kind?: string; delta_target_min?: number; delta_target_max?: number } | null;
  signal?: { direction?: string; confidence?: number; expected_horizon_bars?: number } | null;
  selected_contract?: { trading_symbol?: string; strike?: number; option_type?: string; delta?: number; expiry?: string } | null;
  contract_candidates?: Array<Record<string, unknown>>;
  risk?: { approved?: boolean; quantity_lots?: number; risk_budget?: number; premium_at_risk?: number; max_loss?: number; reasons?: string[] } | null;
  policy?: PolicyBlock | null;
  selection_reason?: string;
  data_status?: { execution_ready?: boolean; degraded_reason?: string | null };
};

export default function DirectionalDesk() {
  // Open positions / paper book is the headline view when the desk opens.
  const [activeTab, setActiveTab] = useUrlTab("paper");
  const [isPending, startTransition] = useTransition();
  const [underlying, setUnderlying] = useState(DEFAULT_UNDERLYING);
  const timeframe = DEFAULT_TIMEFRAME;
  const lookback = DEFAULT_LOOKBACK;

  const summaryQuery = useQuery({
    queryKey: ["directional", "summary"],
    queryFn: async () => (await apiClient.get("/api/directional-options/summary")).data as ModuleSummary,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  // Live watchlist + analytics: 8s WS push (real index spot) with a polling
  // fallback. Reuses the generic /ws/strategy-snapshot channel.
  const liveQuery = useLiveSnapshotQuery<{ snapshot?: Snapshot }>({
    queryKey: ["directional", "live-snapshot", underlying, timeframe, lookback],
    queryFn: async () =>
      (
        await apiClient.get("/api/directional-options/live-snapshot", {
          params: { underlying, timeframe, lookback_sessions: lookback },
        })
      ).data as { snapshot?: Snapshot },
    storageKey: `directional-live-${underlying}-${timeframe}`,
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket(
        "directional", underlying, timeframe,
        (d) => onData(d as { snapshot?: Snapshot }),
        onStatusChange,
      ),
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const snapshot = liveQuery.data?.snapshot;
  const summary = summaryQuery.data;
  const universe = useMemo(
    () => summary?.underlyings || ["NIFTY", "BANKNIFTY", "SENSEX"],
    [summary?.underlyings],
  );

  // Paper queries shared with the Paper tab via the canonical hook.
  const paper = usePaperDeskQueries({
    deskKey: "directional",
    symbol: underlying,
    endpoints: {
      summary: "/api/directional-options/paper-summary",
      positions: "/api/directional-options/paper-positions",
      journal: "/api/directional-options/paper-journal",
      reset: "/api/directional-options/reset-paper",
    },
  });

  // Live open-positions stream (shared /ws/positions-overview channel). Only
  // active on the tabs that render the book; falls back to paper.positions
  // polling when the socket is down or the slice is absent.
  const posStream = useStrategyPositionsStream({
    enabled: activeTab === "paper" || activeTab === "performance",
  });
  const streamSlice = selectStrategySlice(posStream.data, "directional");
  const streamLive = posStream.isStreamConnected && Boolean(streamSlice);
  // Shadow `paper` with the streamed slice so PaperTradingTab (which reads
  // paper.positions.data internally) and PaperPerformance both go live.
  const livePaper = streamLive
    ? ({ ...paper, positions: { ...paper.positions, data: streamSlice } } as typeof paper)
    : paper;

  const reg = snapshot?.regime || {};
  const sig = snapshot?.signal || {};
  const pol = snapshot?.policy || null;
  const risk = snapshot?.risk || {};
  const paperSum = (paper.summary.data as Record<string, number> | undefined) || {};

  return (
    <DeskShell
      title={summary?.label || "Directional Long Options"}
      description={summary?.description}
      asOf={snapshot?.as_of}
      isFetching={liveQuery.isFetching || isPending}
      isLive={!!summary?.automation?.loop_active}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/directional-options"
      rightSlot={
        <div className="flex items-center gap-2">
          {(activeTab === "paper" || activeTab === "performance") ? (
            <StatusBadge
              label={streamLive ? "● live" : "polling"}
              variant={streamLive ? "success" : "info"}
            />
          ) : null}
          <label className="rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1 text-[11.5px] text-text-secondary">
            <span className="mr-1.5 text-[10.5px] uppercase tracking-[0.12em] text-text-muted">Symbol</span>
            <select
              className="bg-transparent outline-none"
              value={underlying}
              onChange={(e) => startTransition(() => setUnderlying(e.target.value))}
            >
              {universe.map((u) => (
                <option key={u} value={u} className="bg-bg-card text-text-primary">
                  {u}
                </option>
              ))}
            </select>
          </label>
        </div>
      }
    >
      {activeTab === "live" ? (
        <div className="space-y-4">
          {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
          <UniverseWatchlist
            symbols={universe}
            timeframe={timeframe}
            lookback={lookback}
            selected={underlying}
            onSelect={(s) => startTransition(() => setUnderlying(s))}
          />

          <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
            <MetricTile label="Paper book" value={String(paperSum.open_positions ?? 0)} detail={`Realized ${formatSignedMoney(paperSum.realized_pnl)} · open ${formatSignedMoney(paperSum.unrealized_pnl)}`} color={tone((paperSum.realized_pnl || 0) + (paperSum.unrealized_pnl || 0))} />
            <MetricTile
              label="Policy verdict"
              value={pol?.act ? "ACT" : pol ? "SKIP" : "—"}
              detail={pol ? `${pol.size_multiplier?.toFixed(1) ?? "1.0"}× · sampled ${pol.sampled_value?.toFixed(2) ?? "—"}` : "Policy not active"}
              color={pol?.act ? "text-accent-green" : "text-accent-amber"}
            />
            <MetricTile label="Regime" value={reg.label || "—"} detail={`Conf ${formatNumber(reg.confidence, 2)}`} />
            <MetricTile label="Signal" value={sig.direction || "flat"} detail={`Conf ${formatNumber(sig.confidence, 2)} · horizon ${sig.expected_horizon_bars ?? "—"}b`} color={directionTone(sig.direction)} />
            <MetricTile label="Trades learned" value={String(pol?.n_seen ?? 0)} detail={pol?.posterior_var != null ? `σ ${formatNumber(Math.sqrt(pol.posterior_var), 2)}` : ""} />
          </section>

          <PolicyDecisionPanel policy={pol} candidates={(snapshot?.contract_candidates as any[]) || []} />

          {/* The two Snapshot types are structurally similar but the
              candidate shape differs slightly between the two files —
              cast through `any` to bridge them without further coupling. */}
          {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
          <EngineCalculations snapshot={(snapshot as any) ?? null} />

          <Section title="Current setup" icon={<Gauge size={16} />} rightSlot={
            <div className="flex gap-1.5">
              <StatusBadge label={reg.label || "loading"} tone={regimeTone(reg.label)} />
              <StatusBadge label={sig.direction || "flat"} variant="info" />
              <StatusBadge label={risk.approved ? "approved" : "rejected"} variant={risk.approved ? "success" : "warn"} tone={decisionTone(risk.approved)} />
            </div>
          }>
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3 text-sm text-text-secondary">
                <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Underlying</div>
                <div className="mt-2 flex justify-between">
                  <span>Spot</span><span className="font-mono">{formatNumber(snapshot?.spot_price, 2)}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>EMA spread</span><span className="font-mono">{formatPct(snapshot?.feature_snapshot?.ema_spread_pct, 3)}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>ADX</span><span className="font-mono">{formatNumber(snapshot?.feature_snapshot?.adx, 1)}</span>
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3 text-sm text-text-secondary">
                <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Selected contract</div>
                <div className="mt-2 font-semibold text-text-primary">
                  {snapshot?.selected_contract?.trading_symbol || "Policy chose to skip — see Engine calcs"}
                </div>
                <div className="mt-1 flex justify-between">
                  <span>Risk budget</span><span className="font-mono">{formatSignedMoney(risk.risk_budget)}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>Premium at risk</span><span className="font-mono">{formatSignedMoney(risk.premium_at_risk)}</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>Lots</span><span className="font-mono">{risk.quantity_lots ?? "—"}</span>
                </div>
              </div>
            </div>
          </Section>
        </div>
      ) : null}

      {activeTab === "analytics" ? (
        <OptionAnalyticsPanel
          underlying={underlying}
          expiry={snapshot?.selected_contract?.expiry ?? null}
        />
      ) : null}

      {activeTab === "gamma" ? <GammaDensity symbol={underlying} /> : null}

      {activeTab === "terminal" ? <TerminalPanel /> : null}

      {activeTab === "paper" ? <PaperTradingTab symbol={underlying} paper={livePaper} /> : null}

      {activeTab === "policy" ? <PolicyLearningTab /> : null}

      {activeTab === "performance" ? (
        <PaperPerformance
          summary={paper.summary.data as Record<string, number> | undefined}
          positions={livePaper.positions.data as PositionsPayload | undefined}
        />
      ) : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["directional_options", "directional_positioning"]} title="Directional signal validation" />
      ) : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="Directional Options"
          watchlist={universe.map((symbol) => ({ symbol }))}
          positionSources={["directional"]}
        />
      ) : null}
    </DeskShell>
  );
}
