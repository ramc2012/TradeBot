"use client";

/**
 * Gann TP-Delta Harmonic desk — native v2.
 *
 * Tabs:
 *   geometry   → candlestick chart with Gann fans / SQ9 levels / cycles + KPI strip
 *   confluence → signal score, reasons, angles / SQ9 / cycle tables, alerts
 *   paper      → native performance (equity curve, monthly, R-dist, trade book)
 *   backtest   → R-multiple backtest summary + event list
 */
import { useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, Compass, Gauge, Radio, Sparkles, TrendingUp } from "lucide-react";

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
import { PaperPerformance } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import { useStrategyPositionsStream, selectStrategySlice } from "@/hooks/useStrategyPositionsStream";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createStrategySnapshotSocket } from "@/lib/websocket";
import type { PositionsPayload } from "@/lib/strategy-stats";
import { api as apiClient } from "@/lib/api";

import { GannChart, type GannAngle, type GannBar, type Sq9Level, type TimeCycle, type GannAnchor } from "./GannChart";

const TABS = [
  { key: "paper", label: "Performance", icon: TrendingUp },
  { key: "geometry", label: "Geometry", icon: Compass },
  { key: "confluence", label: "Confluence", icon: Sparkles },
  { key: "backtest", label: "Backtest", icon: Activity },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
  { key: "live-stream", label: "Live stream", icon: Radio },
];

type Signal = {
  score?: number;
  threshold?: number;
  bias?: string;
  state?: string;
  reasons?: string[];
  trigger?: number | null;
  stop?: number | null;
  targets?: number[] | null;
};
type Snapshot = {
  as_of?: string;
  underlying?: string;
  timeframe?: string;
  spot_price?: number;
  bars?: GannBar[];
  anchor?: GannAnchor;
  h?: { value?: number; unit?: string; sample_count?: number };
  gann_angles?: GannAngle[];
  sq9_levels?: Sq9Level[];
  time_cycles?: TimeCycle[];
  nearest_angle?: GannAngle & { distance_pct?: number };
  nearest_sq9_level?: Sq9Level;
  active_time_cycle?: TimeCycle & { distance_bars?: number };
  price_time_square?: { active?: boolean; ratio?: number };
  signal?: Signal;
  alerts?: Array<{ key: string; severity: string; message: string }>;
};

const biasVariant = (b?: string) => (b === "bullish" ? "success" : b === "bearish" ? "error" : "neutral");

export default function GannDesk() {
  // Open positions / paper book is the headline view when the desk opens.
  const [activeTab, setActiveTab] = useUrlTab("paper");
  const [, startTransition] = useTransition();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [timeframe, setTimeframe] = useState("15minute");

  const summaryQuery = useQuery({
    queryKey: ["gann", "summary"],
    queryFn: async () => (await apiClient.get("/api/gann-tp-delta/summary")).data as { label?: string; description?: string; underlyings?: string[]; timeframes?: string[] },
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const liveQuery = useLiveSnapshotQuery<Snapshot>({
    queryKey: ["gann", "live", underlying, timeframe],
    queryFn: async () => (await apiClient.get("/api/gann-tp-delta/live-snapshot", { params: { underlying, timeframe } })).data as Snapshot,
    storageKey: `gann-live-${underlying}-${timeframe}`,
    streamFactory: (onData, onStatusChange) =>
      createStrategySnapshotSocket("gann", underlying, timeframe, (d) => onData(d as Snapshot), onStatusChange),
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const statusQuery = useQuery({
    queryKey: ["gann", "paper-status"],
    queryFn: async () => (await apiClient.get("/api/gann-tp-delta/paper-agent/status")).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const backtestQuery = useQuery({
    queryKey: ["gann", "backtest", underlying],
    queryFn: async () => (await apiClient.get("/api/gann-tp-delta/backtest", { params: { underlying } })).data,
    enabled: activeTab === "backtest",
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
  });

  const snap = liveQuery.data;
  const summary = summaryQuery.data;
  const universe = summary?.underlyings || ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"];
  const timeframes = summary?.timeframes || ["5minute", "15minute", "1hour", "1day"];
  const sig = snap?.signal || {};
  const status = statusQuery.data as
    | { summary?: Record<string, number>; open_positions?: unknown[]; closed_positions?: unknown[]; recent_signals?: unknown[] }
    | undefined;

  // Live open-positions stream (shared /ws/positions-overview channel); active
  // on the paper tab, falls back to status polling when the socket is down.
  const posStream = useStrategyPositionsStream({ enabled: activeTab === "paper" });
  const streamSlice = selectStrategySlice(posStream.data, "gann");
  const streamLive = posStream.isStreamConnected && Boolean(streamSlice);

  const positions = useMemo<PositionsPayload>(() => {
    const src = streamLive ? streamSlice : status;
    return {
      open_positions: (src?.open_positions as PositionsPayload["open_positions"]) || [],
      closed_positions: (src?.closed_positions as PositionsPayload["closed_positions"]) || [],
      summary: (src?.summary as PositionsPayload["summary"]) ?? status?.summary,
    };
  }, [streamLive, streamSlice, status]);

  return (
    <DeskShell
      title={summary?.label || "Gann TP-Delta Harmonic"}
      description={summary?.description}
      asOf={snap?.as_of}
      isFetching={liveQuery.isFetching}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/gann-tp-delta"
      rightSlot={
        <div className="flex items-center gap-2">
          {activeTab === "paper" ? (
            <TransportBadge connected={streamLive} />
          ) : null}
          <Picker label="Symbol" value={underlying} options={universe} onChange={(v) => startTransition(() => setUnderlying(v))} />
          <Picker label="TF" value={timeframe} options={timeframes} onChange={(v) => startTransition(() => setTimeframe(v))} />
        </div>
      }
    >
      {activeTab === "geometry" ? (
        <div className="space-y-4">
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
            <MetricTile label="Spot" value={formatNumber(snap?.spot_price, 1)} detail={snap?.timeframe} />
            <MetricTile label="Signal" value={sig.state || "—"} detail={`score ${sig.score ?? 0}/${sig.threshold ?? 0}`} color={sig.bias === "bullish" ? "text-accent-green" : sig.bias === "bearish" ? "text-accent-red" : undefined} />
            <MetricTile label="Bias" value={sig.bias || "neutral"} />
            <MetricTile label="Harmonic h" value={formatNumber(snap?.h?.value, 2)} detail={`${snap?.h?.unit || ""} · n${snap?.h?.sample_count ?? 0}`} />
            <MetricTile label="Anchor" value={snap?.anchor?.kind || "—"} detail={`${formatNumber(snap?.anchor?.price, 1)} · ${snap?.anchor?.strength || ""}`} />
            <MetricTile label="Near angle" value={snap?.nearest_angle?.name || "—"} detail={`${formatPct(snap?.nearest_angle?.distance_pct, 3)}`} color={tone(snap?.nearest_angle?.direction === "bullish" ? 1 : -1)} />
            <MetricTile label="Active cycle" value={snap?.active_time_cycle ? `#${snap.active_time_cycle.cycle}` : "—"} detail={snap?.active_time_cycle?.distance_bars != null ? `Δ${snap.active_time_cycle.distance_bars}b` : ""} />
          </section>

          <Section title="Price-time geometry" icon={<Compass size={16} />} rightSlot={
            <div className="flex gap-1.5">
              {snap?.price_time_square?.active ? <StatusBadge label="price-time squared" variant="info" /> : null}
              <StatusBadge label={sig.state || "loading"} variant={biasVariant(sig.bias)} />
            </div>
          }>
            <GannChart
              bars={snap?.bars || []}
              angles={snap?.gann_angles || []}
              sq9={snap?.sq9_levels || []}
              cycles={snap?.time_cycles || []}
              anchor={snap?.anchor || null}
              spot={snap?.spot_price ?? null}
            />
          </Section>
        </div>
      ) : null}

      {activeTab === "confluence" ? <ConfluencePanel snap={snap} /> : null}


      {activeTab === "paper" ? (
        <PaperPerformance summary={status?.summary as Record<string, number> | undefined} positions={positions} />
      ) : null}

      {activeTab === "backtest" ? <BacktestTab data={backtestQuery.data} loading={backtestQuery.isFetching} /> : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["gann_tp_delta"]} title="Gann signal validation" />
      ) : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="Gann TP Delta"
          watchlist={universe.map((symbol) => ({ symbol }))}
          positionSources={["gann"]}
        />
      ) : null}
    </DeskShell>
  );
}

function ConfluencePanel({ snap }: { snap?: Snapshot }) {
  const sig = snap?.signal || {};
  const score = sig.score ?? 0;
  const threshold = sig.threshold ?? 5;
  const pct = threshold > 0 ? Math.min(100, (score / threshold) * 100) : 0;
  const triggered = score >= threshold;
  return (
    <div className="space-y-4">
      <Section title="Confluence signal" icon={<Sparkles size={16} />} rightSlot={<StatusBadge label={sig.bias || "neutral"} variant={biasVariant(sig.bias)} />}>
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-1 space-y-3">
            <div>
              <div className="flex items-end justify-between">
                <span className="text-2xl font-semibold text-text-primary">{score}<span className="text-sm text-text-muted">/{threshold}</span></span>
                <StatusBadge label={triggered ? sig.state || "triggered" : sig.state || "below threshold"} variant={triggered ? "success" : "warn"} />
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-bg-primary/40">
                <div className="h-full rounded-full" style={{ width: `${pct}%`, background: triggered ? "rgb(var(--accent-green))" : "rgb(var(--accent-amber))" }} />
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center text-[12px]">
              <Tile label="Trigger" value={formatNumber(sig.trigger, 1)} />
              <Tile label="Stop" value={formatNumber(sig.stop, 1)} />
              <Tile label="Targets" value={sig.targets?.length ? String(sig.targets.length) : "—"} />
            </div>
          </div>
          <div className="lg:col-span-2">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Reasons</div>
            <ul className="mt-2 space-y-1.5">
              {(sig.reasons || []).map((r, i) => (
                <li key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />
                  {r}
                </li>
              ))}
              {!sig.reasons?.length ? <li className="text-sm text-text-muted">No confluence reasons.</li> : null}
            </ul>
          </div>
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Gann angles" icon={<TrendingUp size={16} />}>
          <MiniTable
            head={["Angle", "Dir", "Projected", "Dist %"]}
            rows={(snap?.gann_angles || []).map((a) => [
              a.name,
              <StatusBadge key="d" label={a.direction === "bullish" ? "bull" : "bear"} variant={a.direction === "bullish" ? "success" : "error"} />,
              formatNumber(a.projected_price, 1),
              formatPct(a.distance_pct, 3),
            ])}
          />
        </Section>
        <Section title="Square of 9" icon={<Compass size={16} />}>
          <MiniTable
            head={["Degree", "Type", "Price", "Dist %"]}
            rows={(snap?.sq9_levels || []).slice(0, 12).map((l) => [
              `${l.degree}°`,
              l.level_type,
              formatNumber(l.price, 1),
              formatPct(l.distance_pct, 3),
            ])}
          />
        </Section>
      </div>

      <Section title="Alerts" icon={<AlertTriangle size={16} />}>
        {(snap?.alerts || []).length ? (
          <ul className="space-y-1.5">
            {snap!.alerts!.map((a, i) => (
              <li key={i} className="flex items-center gap-2 text-[12.5px] text-text-secondary">
                <StatusBadge label={a.severity} variant={a.severity === "warning" ? "warn" : a.severity === "critical" ? "error" : "info"} />
                {a.message}
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-text-muted">No active alerts.</div>
        )}
      </Section>
    </div>
  );
}

function BacktestTab({ data, loading }: { data?: { summary?: Record<string, number>; events?: Array<Record<string, unknown>> }; loading: boolean }) {
  const s = data?.summary || {};
  const events = data?.events || [];
  if (loading && !data) return <Section title="Backtest"><div className="py-8 text-center text-sm text-text-muted">Running backtest…</div></Section>;
  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <MetricTile label="Trades" value={String(s.trades ?? s.event_count ?? 0)} />
        <MetricTile label="Win rate" value={formatPct((s.win_rate_pct ?? 0) / 100)} />
        <MetricTile label="Total R" value={formatSignedNumber(s.total_r, 2)} color={tone(s.total_r)} />
        <MetricTile label="Expectancy" value={`${formatSignedNumber(s.expectancy_r, 2)}R`} color={tone(s.expectancy_r)} />
        <MetricTile label="Profit factor" value={formatNumber(s.profit_factor, 2)} color={tone((s.profit_factor ?? 0) - 1)} />
        <MetricTile label="Max DD" value={`${formatNumber(s.max_drawdown_r, 1)}R`} />
      </section>
      <Section title="Backtest events" icon={<Activity size={16} />}>
        <MiniTable
          head={["Entry", "Exit", "Side", "R"]}
          rows={events.slice(0, 60).map((e) => [
            formatIST(e.entry_time as string),
            formatIST(e.exit_time as string),
            String((e.side ?? e.direction ?? e.archetype ?? "—") as string),
            <span key="r" className={tone(Number(e.r ?? e.r_multiple))}>{formatSignedNumber(Number(e.r ?? e.r_multiple), 2)}</span>,
          ])}
        />
      </Section>
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
