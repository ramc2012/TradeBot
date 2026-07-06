"use client";

/**
 * NSE Index desk — native v2.
 *
 * Replaces the v1 NseStrategyDesk embed. NSE Strategy 1 (30m ATM option
 * premium MACD zero-cross) is the live lane; the desk is built around the
 * canonical strategy-agent status payload plus the open-signals watchlist
 * and the trading kill-switch.
 *
 * Tabs:
 *   overview     → KPI strip + per-strategy status card + regime/equity chart + recent signals
 *   signals      → full ATM MACD watchlist (sortable, trend + value)
 *   positions    → live open positions
 *   performance  → native PaperPerformance (equity curve, monthly, R-dist, trade book)
 *   activity     → agent commentary + NSE audit feed
 *
 * Endpoints:
 *   /api/trading/strategy-agent/status          → strategies[] lanes, summary, positions, trade_history, commentary
 *   /api/strategy/open-signals?underlying=…      → strategy1_watchlist[]
 *   /api/trading/strategy-agent/equity-history   → [{ key, label, equity_curve[] }]
 *   /api/trading/kill-switch                      → kill-switch / auto-run status (header, read-only)
 *   /api/audit/events?market=nse                  → audit feed
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BarChart3,
  CandlestickChart,
  Gauge,
  LayoutPanelLeft,
  ListChecks,
  Radio,
  TrendingUp,
  Wallet,
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
  formatMoney,
  formatNumber,
  formatPct,
  formatSignedMoney,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { PaperPerformance } from "@/components/strategies/shared";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { OptionChartModal, type OptionChartContract } from "@/components/strategies/nse/OptionChartModal";
import { MacdCockpit } from "@/components/strategies/nse/MacdCockpit";
import { useStrategyPositionsStream } from "@/hooks/useStrategyPositionsStream";
import { TerminalPanel } from "@/components/terminal/TerminalPanel";
import type { PaperPosition, PaperSummary, PositionsPayload } from "@/lib/strategy-stats";
import {
  api as apiClient,
  getStrategyAgentComments,
  getStrategyAgentStatus,
  getStrategyEquityHistory,
  getStrategyOpenSignals,
  getMacdDiffusion,
  getTradingKillSwitchStatus,
} from "@/lib/api";

// ── Types (shaped from the live prod payloads) ──────────────────────────────

type WatchRow = {
  underlying?: string | null;
  symbol?: string | null;
  direction?: string | null;
  status?: string | null;
  reason?: string | null;
  strength?: string | null;
  instruction?: string | null;
  expiry?: string | null;
  atm_strike?: number | null;
  strike?: number | null;
  ltp?: number | null;
  iv_pct?: number | null;
  macd?: number | null;
  previous_macd?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
  priority_score?: number | null;
  bucket?: string | null;
  instrument_key?: string | null;
  trading_symbol?: string | null;
};

type PositionRow = {
  underlying?: string | null;
  trading_symbol?: string | null;
  symbol?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  phase?: string | null;
  trailing_stop?: number | null;
  latest_rsi?: number | null;
  signal_reason?: string | null;
  regime?: string | null;
  entered_at?: string | null;
};

type TradeRow = {
  symbol?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
};

type StrategyEventRow = {
  time?: string | null;
  event?: string | null;
  underlying?: string | null;
  option_type?: string | null;
};

type LaneSummary = {
  initial_capital?: number | null;
  available_capital?: number | null;
  total_equity?: number | null;
  realized_pnl?: number | null;
  realized_pnl_lifetime?: number | null;
  unrealized_pnl?: number | null;
  day_pnl?: number | null;
  day_realized_pnl?: number | null;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  sharpe_ratio?: number | null;
  total_trades?: number | null;
  open_positions?: number | null;
  entries?: number | null;
  exits?: number | null;
};

type StrategyLane = {
  key?: string;
  label?: string;
  timeframe?: string | null;
  instrument_scope?: string | null;
  execution_mode?: string | null;
  position_cap?: number | null;
  mode?: string | null;
  last_scan_at?: string | null;
  last_message?: string | null;
  open_positions?: number | null;
  summary?: LaneSummary;
  positions?: PositionRow[];
  signals?: WatchRow[];
  trade_history?: TradeRow[];
  today_trades?: TradeRow[];
  recent_events?: StrategyEventRow[];
};

type Commentary = { time?: string; scope?: string; tone?: string; level?: string; message?: string };

type AgentStatus = {
  enabled?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  loop_active?: boolean;
  running?: boolean;
  last_run_at?: string | null;
  last_message?: string | null;
  target_expiry?: string | null;
  commentary?: Commentary[];
  strategies?: StrategyLane[];
};

type KillSwitch = {
  market?: string;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  manual_restart_required?: boolean;
  loop_active?: boolean;
};

type EquityLane = { key?: string; label?: string; equity_curve?: Array<{ time?: string; equity?: number }> };

type AuditEvent = { id?: string; time?: string; severity?: string; level?: string; scope?: string; message?: string; market?: string };

type DiffusionPoint = {
  time: string;
  ce_total: number;
  ce_above_zero: number;
  pe_total: number;
  pe_above_zero: number;
  ce_pct?: number | null;
  pe_pct?: number | null;
  net_diffusion?: number | null;
  source?: string;
};

function mergeAgentStatusWithPositionStream(
  polled: AgentStatus | undefined,
  streamed: AgentStatus | undefined,
): AgentStatus | undefined {
  if (!streamed) return polled;
  if (!polled) return streamed;

  // The positions-overview socket intentionally strips bulky lane fields such
  // as recent_events, signals and meta. Overlay its live positions/summary on
  // the complete polled lane instead of replacing the whole status payload.
  const streamedByKey = new Map(
    (streamed.strategies ?? []).map((lane) => [lane.key, lane] as const),
  );
  const mergedStrategies = (polled.strategies ?? []).map((lane) => {
    const liveLane = streamedByKey.get(lane.key);
    return liveLane ? { ...lane, ...liveLane } : lane;
  });
  const knownKeys = new Set(mergedStrategies.map((lane) => lane.key));
  for (const liveLane of streamed.strategies ?? []) {
    if (!knownKeys.has(liveLane.key)) mergedStrategies.push(liveLane);
  }

  return { ...polled, ...streamed, strategies: mergedStrategies };
}

type DiffusionPayload = { market?: string; days?: number; count?: number; series?: DiffusionPoint[]; latest?: DiffusionPoint | null };

const TABS = [
  { key: "cockpit", label: "Cockpit", icon: LayoutPanelLeft },
  { key: "positions", label: "Positions", icon: Wallet },
  { key: "terminal", label: "Terminal", icon: Radio },
  { key: "overview", label: "Overview", icon: TrendingUp },
  { key: "signals", label: "Signals", icon: ListChecks },
  { key: "sentiment", label: "Sentiment", icon: Gauge },
  { key: "performance", label: "Performance", icon: BarChart3 },
  { key: "activity", label: "Activity", icon: Activity },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
  { key: "live-stream", label: "Live stream", icon: Radio },
];

type SortDir = "asc" | "desc";

const prettify = (v?: string | null) => (v ? v.replaceAll("_", " ") : "—");

// ── Trade → PaperPosition adapter ───────────────────────────────────────────
// The shared PaperPerformance surface consumes the canonical PaperPosition
// shape; the strategy lane exposes a flatter trade-history row, so we map.

function tradeToPaperPosition(t: TradeRow): PaperPosition {
  return {
    trading_symbol: t.symbol ?? undefined,
    underlying: (t.symbol || "").split(":")[1] || undefined,
    option_type: t.option_type ?? undefined,
    strike: t.strike ?? null,
    expiry: t.expiry ?? null,
    status: "closed",
    opened_at: t.entry_time ?? null,
    closed_at: t.exit_time ?? null,
    entry_premium: t.entry_price ?? null,
    exit_premium: t.exit_price ?? null,
    quantity_units: t.qty ?? null,
    qty: t.qty ?? null,
    realized_pnl: t.pnl ?? null,
  };
}

function positionToPaperPosition(p: PositionRow): PaperPosition {
  return {
    trading_symbol: p.trading_symbol ?? p.symbol ?? undefined,
    underlying: p.underlying ?? undefined,
    option_type: p.option_type ?? undefined,
    direction: p.option_type ?? undefined,
    strike: p.strike ?? null,
    expiry: p.expiry ?? null,
    regime: p.regime ?? null,
    status: "open",
    opened_at: p.entered_at ?? null,
    entry_premium: p.entry_price ?? null,
    latest_premium: p.current_price ?? null,
    quantity_units: p.qty ?? null,
    qty: p.qty ?? null,
    unrealized_pnl: p.unrealized_pnl ?? null,
  };
}

// ── Main ────────────────────────────────────────────────────────────────────

export default function NseDesk() {
  // Open positions is the headline view when the desk opens.
  const [activeTab, setActiveTab] = useUrlTab("cockpit");

  const statusQuery = useQuery({
    queryKey: ["nse", "status"],
    queryFn: async () => (await getStrategyAgentStatus()).data as AgentStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
  });

  const signalsQuery = useQuery({
    queryKey: ["nse", "open-signals"],
    queryFn: async () => (await getStrategyOpenSignals("SENSEX")).data as { strategy1_watchlist?: WatchRow[] },
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const killQuery = useQuery({
    queryKey: ["nse", "kill-switch"],
    queryFn: async () => (await getTradingKillSwitchStatus()).data as KillSwitch,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const equityQuery = useQuery({
    queryKey: ["nse", "equity-history"],
    queryFn: async () => (await getStrategyEquityHistory()).data as EquityLane[],
    enabled: activeTab === "overview",
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const commentsQuery = useQuery({
    queryKey: ["nse", "comments"],
    queryFn: async () => (await getStrategyAgentComments(40)).data as Commentary[] | { comments?: Commentary[] },
    enabled: activeTab === "activity",
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const diffusionQuery = useQuery({
    queryKey: ["nse", "diffusion"],
    queryFn: async () => (await getMacdDiffusion(30)).data as DiffusionPayload,
    enabled: activeTab === "sentiment",
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const auditQuery = useQuery({
    queryKey: ["nse", "audit"],
    queryFn: async () => (await apiClient.get("/api/audit/events", { params: { market: "nse", limit: 40 } })).data as { events?: AuditEvent[] },
    enabled: activeTab === "activity",
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  // Live positions: stream the NSE agent-status slice off /ws/positions-overview
  // (carries overlay_nse_agent_status per-tick marks on held legs → more
  // accurate than the 30s poll). Falls back to the polled status when the
  // socket is down. The S1 signals watchlist stays on poll — those rows only
  // change at the 60s strategy scan, so streaming would add cadence not accuracy.
  const posStream = useStrategyPositionsStream({
    enabled: activeTab === "cockpit" || activeTab === "positions" || activeTab === "performance" || activeTab === "overview",
  });
  const streamLive = posStream.isStreamConnected && Boolean(posStream.data?.nse);
  const streamedStatus = posStream.data?.nse as unknown as AgentStatus | undefined;
  const status = useMemo(
    () => streamLive
      ? mergeAgentStatusWithPositionStream(statusQuery.data, streamedStatus)
      : statusQuery.data,
    [statusQuery.data, streamLive, streamedStatus],
  );
  const lane = useMemo<StrategyLane | undefined>(
    () => (status?.strategies || []).find((s) => s.key === "macd_strategy") || (status?.strategies || [])[0],
    [status?.strategies],
  );
  const summary = lane?.summary ?? {};
  const watchlist = useMemo(() => signalsQuery.data?.strategy1_watchlist ?? [], [signalsQuery.data]);
  const positions = useMemo(() => lane?.positions ?? [], [lane?.positions]);
  const closedTrades = useMemo(() => lane?.trade_history ?? [], [lane?.trade_history]);
  const todayActivity = useMemo(() => {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    const today = formatter.format(new Date());
    const events = (lane?.recent_events ?? []).filter((event) => {
      if (!event.time) return false;
      const parsed = new Date(event.time);
      return !Number.isNaN(parsed.getTime()) && formatter.format(parsed) === today;
    });
    return {
      entries: events.filter((event) => event.event === "entry").length,
      exits: events.filter((event) => event.event === "exit").length,
    };
  }, [lane?.recent_events]);

  const kill = killQuery.data;
  const killActive = kill?.kill_switch_active ?? status?.kill_switch_active ?? false;
  const autoRun = kill?.auto_run_enabled ?? status?.auto_run_enabled ?? false;
  const loopActive = kill?.loop_active ?? status?.loop_active ?? false;
  const running = (status?.running || loopActive) && !killActive;

  // Lane-recent signals (prepared watchlist) for the overview rail.
  const laneSignals = useMemo(() => lane?.signals ?? [], [lane?.signals]);

  // Canonical positions payload for the Performance tab — open lane positions
  // + closed trade history, both normalised to the shared PaperPosition shape.
  const paperPositions = useMemo<PositionsPayload>(
    () => ({
      open_positions: positions.map(positionToPaperPosition),
      closed_positions: closedTrades.map(tradeToPaperPosition),
      summary: summary as PaperSummary,
    }),
    [positions, closedTrades, summary],
  );

  const headerStatus = killActive ? "kill switch" : running ? "running" : "idle";
  const headerVariant = killActive ? "error" : running ? "success" : "warn";

  return (
    <DeskShell
      title="MACD Strategy · 30m ATM"
      description="30m ATM option-premium MACD zero-cross across NSE F&O. Hard stop −25%, exit on opposite 30m cross, CE↔PE flips allowed."
      asOf={status?.last_run_at}
      isFetching={statusQuery.isFetching}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/strategy"
      rightSlot={
        <div className="flex items-center gap-1.5">
          <StatusBadge label={headerStatus} variant={headerVariant} />
          <StatusBadge label={autoRun ? "auto-run" : "manual"} variant={autoRun ? "info" : "neutral"} />
        </div>
      }
    >
      {/* KPI strip — present on every tab so the trader never loses the book. */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Equity" value={formatMoney(summary.total_equity)} detail={`init ${formatMoney(summary.initial_capital)}`} />
        <MetricTile label="Day P&L" value={formatSignedMoney(summary.day_pnl)} color={tone(summary.day_pnl)} detail={`realized ${formatSignedMoney(summary.day_realized_pnl)}`} />
        <MetricTile label="Realized (life)" value={formatSignedMoney(summary.realized_pnl_lifetime ?? summary.realized_pnl)} color={tone(summary.realized_pnl_lifetime ?? summary.realized_pnl)} />
        <MetricTile label="Open P&L" value={formatSignedMoney(summary.unrealized_pnl)} color={tone(summary.unrealized_pnl)} />
        <MetricTile
          label="Open / Trades"
          value={`${summary.open_positions ?? positions.length} / ${summary.total_trades ?? 0}`}
          detail={`today ${todayActivity.entries} entries · ${todayActivity.exits} exits · cap ${lane?.position_cap ?? "—"}`}
        />
        <MetricTile label="Win rate" value={summary.win_rate != null ? formatPct(summary.win_rate) : "—"} detail={`PF ${formatNumber(summary.profit_factor, 2)}`} color={tone((summary.profit_factor ?? 0) - 1)} />
      </section>

      {activeTab === "overview" ? (
        <OverviewTab
          lane={lane}
          summary={summary}
          laneSignals={laneSignals}
          equity={equityQuery.data}
          equityLoading={equityQuery.isFetching}
          autoRun={autoRun}
          killActive={killActive}
          loopActive={loopActive}
          targetExpiry={status?.target_expiry}
        />
      ) : null}

      {activeTab === "cockpit" ? <MacdCockpit positions={positions} watchlist={watchlist} /> : null}
      {activeTab === "signals" ? <WatchlistTab rows={watchlist} /> : null}
      {activeTab === "sentiment" ? <SentimentTab data={diffusionQuery.data} loading={diffusionQuery.isFetching} /> : null}
      {activeTab === "terminal" ? <TerminalPanel /> : null}
      {activeTab === "positions" ? <PositionsTab rows={positions} /> : null}
      {activeTab === "performance" ? (
        <PaperPerformance summary={summary as PaperSummary} positions={paperPositions} />
      ) : null}
      {activeTab === "activity" ? (
        <ActivityTab
          commentary={normalizeComments(commentsQuery.data, status?.commentary)}
          audit={auditQuery.data?.events ?? []}
        />
      ) : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["macd_strategy", "market_intelligence"]} title="MACD signal validation" />
      ) : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="MACD Strategy"
          watchlist={["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"].map((symbol) => ({ symbol }))}
          positionSources={["macd_strategy"]}
        />
      ) : null}
    </DeskShell>
  );
}

function normalizeComments(
  data: Commentary[] | { comments?: Commentary[] } | undefined,
  fallback?: Commentary[],
): Commentary[] {
  if (Array.isArray(data)) return data;
  if (data?.comments) return data.comments;
  return fallback ?? [];
}

// ── Overview tab ────────────────────────────────────────────────────────────

function OverviewTab({
  lane,
  summary,
  laneSignals,
  equity,
  equityLoading,
  autoRun,
  killActive,
  loopActive,
  targetExpiry,
}: {
  lane?: StrategyLane;
  summary: LaneSummary;
  laneSignals: WatchRow[];
  equity?: EquityLane[];
  equityLoading: boolean;
  autoRun: boolean;
  killActive: boolean;
  loopActive: boolean;
  targetExpiry?: string | null;
}) {
  const curve = useMemo(() => {
    const l = (equity || []).find((e) => e.key === "macd_strategy") || (equity || [])[0];
    return (l?.equity_curve || [])
      .filter((p) => p.equity != null)
      .map((p, i) => ({ i, t: p.time || "", equity: Number(p.equity) }));
  }, [equity]);

  const initial = summary.initial_capital ?? curve[0]?.equity ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-3">
        {/* Strategy status card */}
        <Section
          title="Strategy 1 · 30m ATM MACD"
          icon={<CandlestickChart size={16} className="text-accent-green" />}
          className="xl:col-span-1"
          rightSlot={<StatusBadge label={prettify(lane?.mode) || "idle"} variant={lane?.mode?.includes("market_closed") ? "neutral" : "info"} />}
        >
          <div className="space-y-2.5 text-[12.5px]">
            <KV label="Timeframe" value={lane?.timeframe || "30minute"} />
            <KV label="Scope" value={lane?.instrument_scope || "NSE F&O ATM options"} />
            <KV label="Execution" value={prettify(lane?.execution_mode)} />
            <KV label="Target expiry" value={targetExpiry || "—"} />
            <KV label="Position cap" value={String(lane?.position_cap ?? "—")} />
            <KV label="Last scan" value={formatIST(lane?.last_scan_at)} />
            <div className="flex flex-wrap gap-1.5 pt-1">
              <StatusBadge label={killActive ? "kill switch on" : "kill switch off"} variant={killActive ? "error" : "success"} />
              <StatusBadge label={autoRun ? "auto-run" : "manual"} variant={autoRun ? "info" : "neutral"} />
              <StatusBadge label={loopActive ? "loop active" : "loop idle"} variant={loopActive ? "success" : "warn"} />
            </div>
            {lane?.last_message ? (
              <p className="pt-1 text-[11.5px] leading-5 text-text-muted">{lane.last_message}</p>
            ) : null}
          </div>
        </Section>

        {/* Regime / equity chart */}
        <Section title="Equity curve" icon={<BarChart3 size={16} className="text-accent-blue" />} className="xl:col-span-2">
          {curve.length >= 2 ? (
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={curve} margin={{ top: 6, right: 8, left: 4, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.12)" />
                <XAxis dataKey="i" tick={{ fontSize: 10, fill: "rgb(var(--text-muted))" }} tickFormatter={(v: number) => String(v)} />
                <YAxis
                  tick={{ fontSize: 10, fill: "rgb(var(--text-muted))" }}
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) => `${(v / 100000).toFixed(2)}L`}
                  width={48}
                />
                <Tooltip
                  contentStyle={{ background: "rgb(var(--bg-card))", border: "1px solid rgb(var(--bg-border))", borderRadius: 8, fontSize: 11 }}
                  labelFormatter={(_l, payload) => formatIST((payload?.[0]?.payload as { t?: string })?.t)}
                  formatter={(v: number) => [formatMoney(v), "Equity"]}
                />
                <ReferenceLine y={initial} stroke="rgb(var(--text-muted))" strokeDasharray="3 3" />
                <Line type="monotone" dataKey="equity" stroke="rgb(var(--accent-green))" strokeWidth={1.6} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState message={equityLoading ? "Loading equity history…" : "Equity curve needs at least two marks."} />
          )}
          <div className="mt-3 grid grid-cols-3 gap-2 text-center">
            <MiniStat label="Max DD" value={summary.max_drawdown != null ? formatPct(summary.max_drawdown) : "—"} tone="text-accent-red" />
            <MiniStat label="Sharpe" value={formatNumber(summary.sharpe_ratio, 2)} />
            <MiniStat label="Avg win / loss" value={`${formatSignedMoney(summary.avg_win)} / ${formatSignedMoney(summary.avg_loss)}`} />
          </div>
        </Section>
      </div>

      {/* Recent prepared signals */}
      <Section title="Recent signals" icon={<Radio size={16} className="text-accent-green" />} rightSlot={<span className="text-[11px] text-text-muted">{laneSignals.length} rows</span>}>
        <MiniTable
          head={["Underlying", "Dir", "Strike", "LTP", "Status", "Score", "Reason"]}
          rows={laneSignals.slice(0, 12).map((s) => [
            s.underlying || "—",
            <DirBadge key="d" direction={s.direction} />,
            formatNumber(s.strike ?? s.atm_strike, 0),
            formatNumber(s.ltp, 1),
            <StatusBadge key="s" label={prettify(s.status)} variant={statusVariant(s.status)} />,
            formatNumber(s.priority_score, 1),
            <span key="r" className="text-text-secondary">{s.reason ? prettify(s.reason) : "—"}</span>,
          ])}
        />
      </Section>
    </div>
  );
}

// ── Signals tab — sortable watchlist ────────────────────────────────────────

// Map a watchlist row to a chartable option contract. Returns null when the
// row is a regime-only placeholder with no resolved strike/expiry (the chart
// needs a concrete contract to pull premium candles).
function rowToContract(r: WatchRow): OptionChartContract | null {
  const strike = r.strike ?? r.atm_strike;
  const expiry = (r.expiry || "").slice(0, 10);
  const direction = (r.direction || "").toUpperCase();
  if (strike == null || !expiry || (direction !== "CE" && direction !== "PE")) return null;
  return {
    underlying: r.underlying || "",
    direction,
    strike,
    expiry,
    instrumentKey: r.instrument_key ?? null,
    ltp: r.ltp ?? null,
  };
}

// Sort fields surfaced in the toolbar — indicators first, then name. Clicking
// the active field flips its direction (handled by onSort).
const WATCH_SORT_FIELDS: { key: string; label: string }[] = [
  { key: "macd", label: "MACD" },
  { key: "rsi", label: "RSI" },
  { key: "priority_score", label: "Score" },
  { key: "ltp", label: "LTP" },
  { key: "iv", label: "IV" },
  { key: "underlying", label: "Name" },
];

// A row is "insufficient data" when the desk could not compute its primary
// indicator (no MACD yet) — these are parked in a separate group at the bottom
// so they never dilute the actionable CE/PE lists.
function isInsufficientRow(r: WatchRow): boolean {
  return r.macd == null || String(r.status || "").toLowerCase().includes("missing");
}

// Build a comparator for the watchlist. Indicator/numeric fields compare
// numerically (nulls sink to the bottom); name/status compare lexically.
// Indicator ties fall back to RSI then priority score so the order is stable
// and still meaningful when, say, two legs share a MACD value.
function makeWatchSorter(sortKey: string, sortDir: SortDir): (a: WatchRow, b: WatchRow) => number {
  const valueOf = (r: WatchRow): number | string => {
    switch (sortKey) {
      case "underlying": return (r.underlying || "").toUpperCase();
      case "direction": return r.direction || "";
      case "strike": return r.strike ?? r.atm_strike ?? -Infinity;
      case "ltp": return r.ltp ?? -Infinity;
      case "iv": return r.iv_pct ?? -Infinity;
      case "rsi": return r.rsi ?? -Infinity;
      case "macd": return r.macd ?? -Infinity;
      case "status": return r.status || "";
      default: return r.priority_score ?? -Infinity;
    }
  };
  return (a, b) => {
    const va = valueOf(a);
    const vb = valueOf(b);
    let cmp =
      typeof va === "string" || typeof vb === "string"
        ? String(va).localeCompare(String(vb))
        : (va as number) - (vb as number);
    if (cmp === 0 && sortKey !== "rsi") cmp = (a.rsi ?? -Infinity) - (b.rsi ?? -Infinity);
    if (cmp === 0) cmp = (a.priority_score ?? -Infinity) - (b.priority_score ?? -Infinity);
    return sortDir === "asc" ? cmp : -cmp;
  };
}

type WatchSide = "CE" | "PE" | "INS";

function WatchlistTab({ rows }: { rows: WatchRow[] }) {
  // Default to MACD descending — the strongest-momentum legs first.
  const [sortKey, setSortKey] = useState<string>("macd");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  // CE / PE live in tabs (not stacked) so the trader isn't scrolling 200+ rows
  // to reach the other side.
  const [side, setSide] = useState<WatchSide>("CE");
  // Open chart holds the FROZEN ordered list (so live refetches don't shuffle
  // it under the user) + the current index — prev/next steps through it.
  const [chart, setChart] = useState<{ list: OptionChartContract[]; index: number } | null>(null);

  const onSort = (key: string) => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const groups = useMemo(() => {
    const sorter = makeWatchSorter(sortKey, sortDir);
    const side = (r: WatchRow) => (r.direction || "").toUpperCase();
    const ce = rows.filter((r) => side(r) === "CE" && !isInsufficientRow(r)).sort(sorter);
    const pe = rows.filter((r) => side(r) === "PE" && !isInsufficientRow(r)).sort(sorter);
    // Bottom bucket: anything with no computable indicator, name-sorted.
    const insufficient = rows.filter(isInsufficientRow).sort(makeWatchSorter("underlying", "asc"));
    return { ce, pe, insufficient };
  }, [rows, sortKey, sortDir]);

  if (!rows.length) {
    return <EmptyState message="No watchlist rows. The desk publishes one row per index+side once 30m ATM premium history is available." />;
  }

  const activeSide: WatchSide = side === "INS" && !groups.insufficient.length ? "CE" : side;
  const activeRows = activeSide === "CE" ? groups.ce : activeSide === "PE" ? groups.pe : groups.insufficient;
  const isPending = activeSide === "INS";

  // Open the chart on the clicked row, freezing the current side's chartable
  // legs (in sorted order) so prev/next can step through them.
  const open = (r: WatchRow) => {
    const target = rowToContract(r);
    if (!target) return;
    const list = activeRows.map(rowToContract).filter((c): c is OptionChartContract => c !== null);
    const idx = list.findIndex((c) => c.underlying === target.underlying && c.strike === target.strike && c.direction === target.direction);
    setChart({ list, index: idx >= 0 ? idx : 0 });
  };

  return (
    <Section
      title="ATM MACD watchlist"
      icon={<ListChecks size={16} className="text-accent-blue" />}
      description="CE / PE in tabs · click a row for its OHLC chart (BB · KAMA · MACD · RSI)"
      rightSlot={<WatchSortToolbar sortKey={sortKey} sortDir={sortDir} onSort={onSort} />}
    >
      {/* CE / PE / Pending side tabs */}
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        <SideTab label="CE" tone="ce" count={groups.ce.length} active={activeSide === "CE"} onClick={() => setSide("CE")} />
        <SideTab label="PE" tone="pe" count={groups.pe.length} active={activeSide === "PE"} onClick={() => setSide("PE")} />
        {groups.insufficient.length ? (
          <SideTab label="Pending" tone="muted" count={groups.insufficient.length} active={activeSide === "INS"} onClick={() => setSide("INS")} />
        ) : null}
        {isPending ? (
          <span className="ml-1 text-[11px] text-text-muted">— awaiting enough 30m premium history for MACD</span>
        ) : null}
      </div>

      <div className="-mx-2 overflow-x-auto">
        <table className="w-full min-w-[1040px] border-collapse text-left">
          <thead>
            <tr className="border-b border-bg-border/60">
              <th className="w-8 px-2.5 py-1.5" aria-label="Chart" />
              <SortHead label="Underlying" k="underlying" sk={sortKey} dir={sortDir} onSort={onSort} />
              <th className="px-2.5 py-1.5 text-left text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">Dir</th>
              <SortHead label="Strike" k="strike" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <SortHead label="LTP" k="ltp" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <SortHead label="IV%" k="iv" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <SortHead label="RSI" k="rsi" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <SortHead label="MACD · trend" k="macd" sk={sortKey} dir={sortDir} onSort={onSort} />
              <SortHead label="Status" k="status" sk={sortKey} dir={sortDir} onSort={onSort} />
              <SortHead label="Score" k="priority_score" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <th className="px-2.5 py-1.5 text-left text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">Instruction</th>
            </tr>
          </thead>
          <tbody>
            {activeRows.length ? (
              activeRows.map((r, idx) => (
                <WatchRowItem key={`${activeSide}-${r.underlying}-${r.strike ?? r.atm_strike}-${idx}`} r={r} onOpen={open} dim={isPending} />
              ))
            ) : (
              <tr>
                <td colSpan={11} className="px-2.5 py-8 text-center text-sm text-text-muted">No {activeSide === "CE" ? "CE" : activeSide === "PE" ? "PE" : "pending"} legs right now.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {chart && chart.list[chart.index] ? (
        <OptionChartModal
          contracts={chart.list}
          index={chart.index}
          onIndexChange={(i) => setChart((c) => (c ? { ...c, index: i } : c))}
          onClose={() => setChart(null)}
        />
      ) : null}
    </Section>
  );
}

// CE / PE / Pending side tab pill.
function SideTab({ label, tone: t, count, active, onClick }: { label: string; tone: "ce" | "pe" | "muted"; count: number; active: boolean; onClick: () => void }) {
  const accent = t === "ce" ? "text-accent-green" : t === "pe" ? "text-accent-red" : "text-text-muted";
  const dot = t === "ce" ? "bg-accent-green" : t === "pe" ? "bg-accent-red" : "bg-text-muted";
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "inline-flex items-center gap-2 rounded-lg border px-3 py-1 text-[12px] font-semibold transition-colors",
        active ? "border-accent-blue/50 bg-accent-blue/10" : "border-bg-border bg-bg-primary/20 hover:border-bg-active",
      )}
    >
      <span className={clsx("inline-block h-2 w-2 rounded-sm", dot)} aria-hidden />
      <span className={active ? accent : "text-text-secondary"}>{label}</span>
      <span className="rounded bg-bg-primary/50 px-1.5 py-0.5 text-[10px] font-mono text-text-muted">{count}</span>
    </button>
  );
}

// Sort toolbar — indicator + name shortcuts. Clicking the active field flips
// direction; the per-column header sorts (SortHead) stay in sync via the same
// onSort/sortKey/sortDir state.
function WatchSortToolbar({ sortKey, sortDir, onSort }: { sortKey: string; sortDir: SortDir; onSort: (k: string) => void }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-[0.12em] text-text-muted">Sort</span>
      <div className="flex rounded-lg border border-bg-border bg-bg-primary/30 p-0.5">
        {WATCH_SORT_FIELDS.map((f) => {
          const active = sortKey === f.key;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => onSort(f.key)}
              className={clsx(
                "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium transition-colors",
                active ? "bg-accent-blue/20 text-accent-blue" : "text-text-muted hover:text-text-secondary",
              )}
            >
              {f.label}
              {active ? (sortDir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function WatchRowItem({ r, onOpen, dim }: { r: WatchRow; onOpen: (r: WatchRow) => void; dim?: boolean }) {
  const contract = rowToContract(r);
  return (
    <tr
      className={clsx(
        "border-b border-bg-border/25 align-top",
        contract ? "cursor-pointer hover:bg-bg-primary/30" : "hover:bg-bg-primary/20",
        dim && "opacity-75",
      )}
      onClick={() => onOpen(r)}
      title={contract ? "Open OHLC chart (BB · KAMA · MACD · RSI)" : "No resolved contract for this row yet"}
    >
      <td className="px-2.5 py-2">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen(r);
          }}
          disabled={!contract}
          aria-label="Open OHLC chart"
          className={clsx(
            "rounded-md border p-1 transition-colors",
            contract
              ? "border-bg-border text-text-muted hover:border-accent-blue/60 hover:text-accent-blue"
              : "cursor-not-allowed border-bg-border/40 text-text-muted/30",
          )}
        >
          <CandlestickChart size={14} />
        </button>
      </td>
      <td className="px-2.5 py-2 text-text-primary">
        <div className="font-semibold">{r.underlying || "—"}</div>
        <div className="text-[10px] text-text-muted">{r.expiry || "—"}</div>
      </td>
      <td className="px-2.5 py-2"><DirBadge direction={r.direction} /></td>
      <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(r.strike ?? r.atm_strike, 0)}</td>
      <td className="px-2.5 py-2 text-right font-mono text-text-primary">{formatNumber(r.ltp, 1)}</td>
      <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(r.iv_pct, 1)}</td>
      <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(r.rsi, 1)}</td>
      <td className="px-2.5 py-2"><MacdTrend macd={r.macd} prev={r.previous_macd} hist={r.macd_histogram} /></td>
      <td className="px-2.5 py-2"><StatusBadge label={prettify(r.status)} variant={statusVariant(r.status)} /></td>
      <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(r.priority_score, 2)}</td>
      <td className="max-w-[320px] px-2.5 py-2 text-[11px] text-text-secondary" title={r.instruction || ""}>{r.instruction || "—"}</td>
    </tr>
  );
}

// ── Sentiment tab — MACD diffusion (CE/PE above zero over time) ──────────────

function SentimentTab({ data, loading }: { data?: DiffusionPayload; loading: boolean }) {
  const series = useMemo(
    () =>
      (data?.series ?? []).map((p) => ({
        t: p.time,
        ce: p.ce_above_zero,
        pe: p.pe_above_zero,
        net: p.net_diffusion != null ? p.net_diffusion * 100 : null, // percent, -100..100
      })),
    [data?.series],
  );
  const latest = data?.latest ?? null;
  const net = latest?.net_diffusion ?? null;
  const sentiment = net == null ? "—" : net > 0.05 ? "Bullish" : net < -0.05 ? "Bearish" : "Neutral";

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="CE > 0" value={latest ? `${latest.ce_above_zero} / ${latest.ce_total}` : "—"} detail={latest?.ce_pct != null ? `${formatNumber(latest.ce_pct * 100, 0)}% breadth` : ""} color="text-accent-green" />
        <MetricTile label="PE > 0" value={latest ? `${latest.pe_above_zero} / ${latest.pe_total}` : "—"} detail={latest?.pe_pct != null ? `${formatNumber(latest.pe_pct * 100, 0)}% breadth` : ""} color="text-accent-red" />
        <MetricTile label="Net diffusion" value={net != null ? `${net > 0 ? "+" : ""}${formatNumber(net * 100, 1)}%` : "—"} color={tone(net)} detail="CE% − PE%" />
        <MetricTile label="Sentiment" value={sentiment} color={net == null ? undefined : net > 0.05 ? "text-accent-green" : net < -0.05 ? "text-accent-red" : "text-accent-amber"} detail={latest ? formatIST(latest.time) : ""} />
      </section>

      <Section
        title="MACD diffusion · CE vs PE above zero"
        icon={<Gauge size={16} className="text-accent-blue" />}
        description="Hourly breadth across the tracked ATM F&O universe — how many CE / PE legs have 30m premium MACD above zero. Net = CE% − PE% (＞0 bullish tape)."
      >
        {series.length >= 2 ? (
          <ResponsiveContainer width="100%" height={340}>
            <LineChart data={series} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.12)" />
              <XAxis dataKey="t" tick={{ fontSize: 10, fill: "rgb(var(--text-muted))" }} tickFormatter={(v: string) => formatIST(v)} minTickGap={56} />
              <YAxis yAxisId="cnt" tick={{ fontSize: 10, fill: "rgb(var(--text-muted))" }} width={36} allowDecimals={false} />
              <YAxis yAxisId="net" orientation="right" domain={[-100, 100]} ticks={[-100, -50, 0, 50, 100]} tick={{ fontSize: 10, fill: "rgb(var(--text-muted))" }} width={40} tickFormatter={(v: number) => `${v}%`} />
              <Tooltip
                contentStyle={{ background: "rgb(var(--bg-card))", border: "1px solid rgb(var(--bg-border))", borderRadius: 8, fontSize: 11 }}
                labelFormatter={(l) => formatIST(String(l))}
                formatter={(v: number, name: string) => [name === "Net %" ? `${formatNumber(v, 1)}%` : String(v), name]}
              />
              <ReferenceLine yAxisId="net" y={0} stroke="rgb(var(--text-muted))" strokeDasharray="3 3" />
              <Line yAxisId="cnt" type="monotone" dataKey="ce" name="CE > 0" stroke="rgb(var(--accent-green))" strokeWidth={1.6} dot={false} isAnimationActive={false} connectNulls />
              <Line yAxisId="cnt" type="monotone" dataKey="pe" name="PE > 0" stroke="rgb(var(--accent-red))" strokeWidth={1.6} dot={false} isAnimationActive={false} connectNulls />
              <Line yAxisId="net" type="monotone" dataKey="net" name="Net %" stroke="rgb(var(--accent-blue))" strokeWidth={1.2} strokeDasharray="4 2" dot={false} isAnimationActive={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <EmptyState message={loading ? "Loading diffusion history…" : "Diffusion needs at least two hourly points — the snapshot accrues each hour during market hours."} />
        )}
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">
          <SentLegend color="rgb(var(--accent-green))" label="CE > 0 (count)" />
          <SentLegend color="rgb(var(--accent-red))" label="PE > 0 (count)" />
          <SentLegend color="rgb(var(--accent-blue))" label="Net diffusion %" />
          <span>{series.length} hourly points</span>
        </div>
      </Section>
    </div>
  );
}

function SentLegend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-2 w-2.5 rounded-sm" style={{ backgroundColor: color }} aria-hidden />
      {label}
    </span>
  );
}

// ── Positions tab ───────────────────────────────────────────────────────────

function PositionsTab({ rows }: { rows: PositionRow[] }) {
  if (!rows.length) return <EmptyState message="No open positions right now." />;
  return (
    <Section title="Open positions" icon={<Wallet size={16} className="text-accent-green" />} rightSlot={<span className="text-[11px] text-text-muted">{rows.length} open</span>}>
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full min-w-[1080px] border-collapse text-left">
          <thead>
            <tr className="border-b border-bg-border/60">
              {["Underlying", "Contract", "Side", "Qty", "Entry", "Mark", "Open P&L", "Phase", "Trail / RSI", "Signal", "Age"].map((h, i) => (
                <th key={h} className={clsx("px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted", i >= 3 && i <= 6 ? "text-right" : "text-left")}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((p, idx) => (
              <tr key={`${p.underlying}-${p.option_type}-${p.strike}-${idx}`} className="border-b border-bg-border/25 align-top hover:bg-bg-primary/20">
                <td className="px-2.5 py-2 font-semibold text-text-primary">{p.underlying || "—"}</td>
                <td className="px-2.5 py-2 font-mono text-text-secondary">
                  {p.option_type} {formatNumber(p.strike, 0)}
                  <div className="text-[10px] text-text-muted">{p.expiry || "—"}</div>
                </td>
                <td className="px-2.5 py-2"><DirBadge direction={p.option_type} /></td>
                <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{p.qty ?? "—"}</td>
                <td className="px-2.5 py-2 text-right font-mono text-text-primary">{formatNumber(p.entry_price, 2)}</td>
                <td className="px-2.5 py-2 text-right font-mono text-text-primary">{formatNumber(p.current_price, 2)}</td>
                <td className={clsx("px-2.5 py-2 text-right font-mono font-semibold", tone(p.unrealized_pnl))}>
                  {formatSignedMoney(p.unrealized_pnl)}
                  {p.return_pct != null ? <div className="text-[10px] font-normal text-text-muted">{p.return_pct > 0 ? "+" : ""}{formatNumber(p.return_pct, 1)}%</div> : null}
                </td>
                <td className="px-2.5 py-2"><StatusBadge label={prettify(p.phase)} variant="info" /></td>
                <td className="px-2.5 py-2 text-right font-mono text-text-secondary">
                  {p.trailing_stop != null ? formatNumber(p.trailing_stop, 2) : "—"}
                  <div className="text-[10px] text-text-muted">RSI {formatNumber(p.latest_rsi, 1)}</div>
                </td>
                <td className="max-w-[180px] px-2.5 py-2 text-[11px] text-text-secondary">{prettify(p.signal_reason)}</td>
                <td className="px-2.5 py-2 text-[11px] text-text-muted">{heldFor(p.entered_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ── Activity tab ────────────────────────────────────────────────────────────

function ActivityTab({ commentary, audit }: { commentary: Commentary[]; audit: AuditEvent[] }) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Section title="Agent commentary" icon={<Activity size={16} className="text-accent-blue" />}>
        {commentary.length ? (
          <div className="max-h-[560px] space-y-2 overflow-y-auto pr-1">
            {commentary.slice().reverse().slice(0, 40).map((c, idx) => (
              <div key={`${c.time}-${idx}`} className="rounded-xl border border-bg-border bg-bg-primary/15 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">{c.scope || c.tone || c.level || "info"}</span>
                  <span className="text-[10px] text-text-muted">{formatIST(c.time)}</span>
                </div>
                <div className="mt-1 text-xs leading-5 text-text-secondary">{c.message}</div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState message="No commentary in this runtime yet." />
        )}
      </Section>

      <Section title="NSE audit feed" icon={<Radio size={16} className="text-accent-green" />}>
        {audit.length ? (
          <div className="max-h-[560px] space-y-2 overflow-y-auto pr-1">
            {audit.slice(0, 40).map((a, idx) => (
              <div key={a.id || `${a.time}-${idx}`} className="rounded-xl border border-bg-border bg-bg-primary/15 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <StatusBadge label={a.scope || a.level || "event"} variant={severityVariant(a.severity || a.level)} />
                  <span className="text-[10px] text-text-muted">{formatIST(a.time)}</span>
                </div>
                <div className="mt-1 text-xs leading-5 text-text-secondary">{a.message}</div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState message="No audit events." />
        )}
      </Section>
    </div>
  );
}

// ── Atoms ───────────────────────────────────────────────────────────────────

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-bg-border/30 pb-1.5">
      <span className="text-[11px] uppercase tracking-[0.1em] text-text-muted">{label}</span>
      <span className="font-mono text-text-secondary">{value}</span>
    </div>
  );
}

function MiniStat({ label, value, tone: t }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={clsx("mt-0.5 font-mono text-[12px] font-semibold", t || "text-text-primary")}>{value}</div>
    </div>
  );
}

function DirBadge({ direction }: { direction?: string | null }) {
  if (!direction) return <span className="text-text-muted">—</span>;
  const isCE = direction.toUpperCase() === "CE";
  return (
    <span className={clsx("inline-flex rounded-md border px-1.5 py-0.5 text-[10px] font-bold", isCE ? "border-accent-green/40 bg-accent-green/10 text-accent-green" : "border-accent-red/40 bg-accent-red/10 text-accent-red")}>
      {direction.toUpperCase()}
    </span>
  );
}

function MacdTrend({ macd, prev, hist }: { macd?: number | null; prev?: number | null; hist?: number | null }) {
  if (macd == null) return <span className="text-text-muted">—</span>;
  const delta = prev == null ? null : macd - prev;
  const rising = delta != null && delta > 0;
  const falling = delta != null && delta < 0;
  const aboveZero = macd >= 0;
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx("font-mono text-xs font-semibold", aboveZero ? "text-accent-green" : "text-accent-red")}>
        {macd >= 0 ? "+" : ""}{formatNumber(macd, 4)}
      </span>
      {rising ? <ArrowUp size={12} className="text-accent-green" /> : falling ? <ArrowDown size={12} className="text-accent-red" /> : <span className="text-text-muted">·</span>}
      {hist != null ? (
        <span className={clsx("text-[10px]", hist >= 0 ? "text-accent-green/70" : "text-accent-red/70")} title="MACD histogram (momentum)">
          h{hist >= 0 ? "+" : ""}{formatNumber(hist, 3)}
        </span>
      ) : null}
    </div>
  );
}

function SortHead({ label, k, sk, dir, onSort, align = "left" }: { label: string; k: string; sk: string; dir: SortDir; onSort: (k: string) => void; align?: "left" | "right" }) {
  const active = sk === k;
  return (
    <th className={clsx("px-2.5 py-1.5 select-none", align === "right" && "text-right")}>
      <button
        type="button"
        onClick={() => onSort(k)}
        className={clsx(
          "inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-[0.12em] transition-colors hover:text-text-primary",
          align === "right" && "flex-row-reverse",
          active ? "text-text-primary" : "text-text-muted",
        )}
      >
        {label}
        {active ? (dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />) : <ArrowUpDown size={11} className="opacity-40" />}
      </button>
    </th>
  );
}

function MiniTable({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {head.map((h, i) => (
              <th key={i} className={clsx("px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted", i === 0 ? "text-left" : i >= 2 && i <= 5 ? "text-right" : "text-left")}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((r, i) => (
            <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              {r.map((c, j) => (
                <td key={j} className={clsx("px-2.5 py-1.5 text-[12px] font-mono whitespace-nowrap", j === 0 ? "text-left text-text-primary" : j >= 2 && j <= 5 ? "text-right text-text-secondary" : "text-left text-text-secondary")}>{c}</td>
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

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex min-h-[160px] items-center justify-center rounded-xl border border-dashed border-bg-border text-sm text-text-muted">
      {message}
    </div>
  );
}

// ── Variant mappers ─────────────────────────────────────────────────────────

function statusVariant(status?: string | null): "neutral" | "success" | "warn" | "error" | "info" {
  const s = (status || "").toLowerCase();
  if (s.includes("entry-ready") || s.includes("ready") || s.includes("active")) return "success";
  if (s.includes("trend-aligned") || s.includes("monitor") || s.includes("watching")) return "info";
  if (s.includes("waiting") || s.includes("standby") || s.includes("drift")) return "warn";
  if (s.includes("missing") || s.includes("avoid") || s.includes("stale")) return "error";
  return "neutral";
}

function severityVariant(sev?: string | null): "neutral" | "success" | "warn" | "error" | "info" {
  const s = (sev || "").toLowerCase();
  if (s === "critical" || s === "error") return "error";
  if (s === "warning" || s === "warn") return "warn";
  if (s === "success" || s === "ok") return "success";
  return "info";
}

function heldFor(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const mins = Math.floor((Date.now() - t) / 60_000);
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const rem = mins % 60;
  if (h < 24) return rem ? `${h}h ${rem}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}
