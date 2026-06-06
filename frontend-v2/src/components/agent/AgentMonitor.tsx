"use client";

/**
 * AI Agent monitor — native v2 surface.
 *
 * Replaces the v1 StrategyAgentMonitor embed. A tool-page (not a strategy
 * desk) layout: header + KPI strip + Sections, matching ProposalsBoard's
 * idioms and the desk-ui design system.
 *
 * Tabs:
 *   overview   → per-strategy runtime cards (status / last run / next scan,
 *                summary KPIs, open positions, signal lane, recent activity)
 *   commentary → agent commentary timeline (live English notes)
 *   brokers    → broker-session status + data-health token health
 *
 * Data:
 *   /api/trading/strategy-agent/status  (strategy runtimes, commentary, health)
 *   /api/auth/broker-status             (per-broker session status)
 *   /api/trading/strategy-agent/run-once (manual scan, write-token gated)
 */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Bot,
  Clock,
  MessageSquare,
  Play,
  Radio,
  RefreshCw,
  Shield,
  TrendingUp,
  Zap,
} from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatNumber,
  formatPct,
  formatSignedMoney,
  formatTimestamp,
  tone,
} from "@/components/desk-ui";
import { api } from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";

// ── Types (mirrors /api/trading/strategy-agent/status) ─────────────────────

type TokenHealth = {
  connected?: boolean;
  valid?: boolean;
  status?: string | null;
  source?: string | null;
  checked_at?: string | null;
  needs_reconnect?: boolean;
  message?: string | null;
  expires_at_ist?: string | null;
};

type CommentaryItem = {
  time: string;
  scope: string;
  tone: string;
  message: string;
};

type StrategyAgentMeta = {
  key: string;
  label: string;
  timeframe?: string | null;
  instrument_scope?: string | null;
  execution_mode?: string | null;
  position_cap?: number | null;
  last_scan_at?: string | null;
  last_message?: string | null;
  open_positions?: number | null;
  signals?: number | null;
  mode?: string | null;
};

type StrategyPosition = {
  symbol: string;
  underlying: string;
  trading_symbol?: string | null;
  expiry?: string | null;
  option_type?: string | null;
  strike?: number | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  phase?: string | null;
  trailing_stop?: number | null;
  regime?: string | null;
  latest_rsi?: number | null;
  signal_reason?: string | null;
  entered_at?: string | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
};

type StrategySignal = {
  underlying: string;
  direction?: string | null;
  status?: string | null;
  reason?: string | null;
  freshness?: string | null;
  instruction?: string | null;
  atm_strike?: number | null;
  ltp?: number | null;
  iv_pct?: number | null;
  priority_score?: number | null;
};

type StrategyEvent = {
  time: string;
  event: string;
  underlying: string;
  option_type?: string | null;
  strike?: number | null;
  qty?: number | null;
  price?: number | null;
  reason?: string | null;
  pnl?: number | null;
};

type StrategyTrade = {
  symbol: string;
  action?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
  option_type?: string | null;
  strike?: number | null;
};

type StrategySummary = {
  total_equity?: number | null;
  initial_capital?: number | null;
  available_capital?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  day_pnl?: number | null;
  total_trades?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  sharpe_ratio?: number | null;
  max_drawdown?: number | null;
  open_positions?: number | null;
  entries?: number | null;
  exits?: number | null;
};

type Strategy = {
  key: string;
  label: string;
  agent?: StrategyAgentMeta | null;
  last_scan_at?: string | null;
  last_message?: string | null;
  summary: StrategySummary;
  positions: StrategyPosition[];
  signals?: StrategySignal[];
  recent_events: StrategyEvent[];
  trade_history?: StrategyTrade[];
};

type AgentStatus = {
  enabled?: boolean;
  auto_run_enabled?: boolean;
  loop_active?: boolean;
  running?: boolean;
  kill_switch_active?: boolean;
  scan_interval_seconds?: number | null;
  last_run_at?: string | null;
  next_scan_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  target_expiry?: string | null;
  candidate_expiries?: string[];
  telegram?: {
    enabled?: boolean;
    configured?: boolean;
    report_interval?: string | null;
    last_sent_at?: string | null;
  };
  data_health?: {
    broker_snapshot?: {
      connected_brokers?: string[];
      broker_ready?: boolean;
      upstox_ready?: boolean;
      fyers_ready?: boolean;
      upstox_token_health?: TokenHealth;
      fyers_token_health?: TokenHealth;
    };
    market_intelligence?: {
      ready?: boolean;
      market_open?: boolean;
      execution_mode?: string | null;
      watchlist_rows_latest?: number | null;
      latest_watchlist_session?: string | null;
    };
    data_quality?: {
      overall?: string | null;
      market_state?: string | null;
      stale_count?: number | null;
      frozen_count?: number | null;
      symbol_count?: number | null;
    };
  };
  strategy_agents?: StrategyAgentMeta[];
  commentary?: CommentaryItem[];
  strategies: Strategy[];
};

const TABS = [
  { key: "overview", label: "Runtime", icon: Activity },
  { key: "commentary", label: "Commentary", icon: MessageSquare },
  { key: "brokers", label: "Brokers", icon: Radio },
] as const;
type TabKey = (typeof TABS)[number]["key"];

// ── Component ──────────────────────────────────────────────────────────────

export default function AgentMonitor() {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<TabKey>("overview");
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["agent-monitor", "status"],
    queryFn: async () => (await api.get("/api/trading/strategy-agent/status")).data as AgentStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const brokerQuery = useQuery({
    queryKey: ["agent-monitor", "brokers"],
    queryFn: async () => (await api.get("/api/auth/broker-status")).data as BrokerStatusEntry[],
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const status = statusQuery.data;
  const strategies = status?.strategies ?? [];
  const brokers = useMemo(() => (Array.isArray(brokerQuery.data) ? brokerQuery.data : []), [brokerQuery.data]);
  const commentary = status?.commentary ?? [];

  const connectedBrokers = brokers.filter((b) => isBrokerReady(b));
  const openPositions = strategies.reduce((sum, s) => sum + (s.summary?.open_positions ?? s.positions?.length ?? 0), 0);
  const realizedTotal = strategies.reduce((sum, s) => sum + (s.summary?.realized_pnl ?? 0), 0);

  const loopActive = status?.loop_active ?? status?.auto_run_enabled ?? false;
  const loopLabel = status?.kill_switch_active
    ? "kill switch"
    : status?.running
      ? "scanning"
      : loopActive
        ? "auto loop"
        : "manual";
  const loopVariant = status?.kill_switch_active ? "error" : status?.running ? "info" : loopActive ? "success" : "neutral";

  const runOnce = async () => {
    setRunning(true);
    setRunMsg(null);
    try {
      const res = await api.post("/api/trading/strategy-agent/run-once", null, {
        params: { force: true },
        timeout: 120_000,
      });
      const msg = (res.data?.message ?? res.data?.last_message ?? "Scan triggered.") as string;
      setRunMsg(msg);
      await qc.invalidateQueries({ queryKey: ["agent-monitor", "status"] });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail;
      setRunMsg(detail || (err as { message?: string })?.message || "Run-once failed (write token may be required).");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Bot size={18} className="text-accent-purple" />
              <h1 className="text-xl font-semibold text-text-primary">AI Strategy Agent</h1>
              <StatusBadge label={loopLabel} variant={loopVariant} />
            </div>
            <p className="mt-1 max-w-2xl text-sm text-text-muted">
              Live monitor for the NSE paper runtime — Strategy 1 on 30-minute ATM option-premium MACD zero-cross.
              Runtime state, open positions, signal lanes and the agent&apos;s commentary feed.
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-3 text-[11.5px] text-text-muted">
              <span className="inline-flex items-center gap-1.5">
                <Activity size={12} className={statusQuery.isFetching ? "animate-pulse text-accent-blue" : undefined} />
                {statusQuery.isFetching ? "Refreshing" : "Idle"}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Clock size={12} /> Last run {formatIST(status?.last_run_at)}
              </span>
              <span className="inline-flex items-center gap-1.5">
                Next scan {formatIST(status?.next_scan_at)}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={runOnce}
              disabled={running}
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-2.5 py-1.5 text-[11.5px] font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50"
              title="Force a scan now (requires a write token)"
            >
              <Play size={13} className={running ? "animate-pulse" : ""} />
              {running ? "Running…" : "Run once"}
            </button>
            <button
              type="button"
              onClick={() => statusQuery.refetch()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
            >
              <RefreshCw size={13} className={statusQuery.isFetching ? "animate-spin" : ""} />
              Refresh
            </button>
            <a
              href="http://localhost:3000"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11px] text-text-secondary hover:border-bg-active hover:text-text-primary"
              title="Open the equivalent v1 page"
            >
              v1 view
            </a>
          </div>
        </div>
        {runMsg ? (
          <div className="mt-3 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-2 text-[12px] text-text-secondary">
            {runMsg}
          </div>
        ) : null}
      </header>

      {/* KPI strip */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Strategies" value={String(strategies.length)} detail="active runtimes" />
        <MetricTile
          label="Open positions"
          value={String(openPositions)}
          detail="under management"
          color={openPositions ? "text-accent-blue" : undefined}
        />
        <MetricTile
          label="Broker links"
          value={String(connectedBrokers.length)}
          detail={
            connectedBrokers.length
              ? connectedBrokers.map((b) => b.broker.toUpperCase()).join(" + ")
              : "none connected"
          }
          color={connectedBrokers.length ? "text-accent-green" : "text-accent-red"}
        />
        <MetricTile
          label="Loop state"
          value={status?.kill_switch_active ? "halted" : loopActive ? "active" : "manual"}
          detail={`scan ${status?.scan_interval_seconds ?? 60}s`}
          color={status?.kill_switch_active ? "text-accent-red" : loopActive ? "text-accent-green" : undefined}
        />
        <MetricTile
          label="Realized P&L"
          value={formatSignedMoney(realizedTotal, 0)}
          detail="lifetime paper"
          color={tone(realizedTotal)}
        />
        <MetricTile
          label="Feed"
          value={statusQuery.isError ? "offline" : "live"}
          detail={statusQuery.dataUpdatedAt ? formatIST(statusQuery.dataUpdatedAt) : ""}
          color={statusQuery.isError ? "text-accent-red" : "text-accent-green"}
        />
      </section>

      {/* Runtime banner */}
      <RuntimeBanner status={status} />

      {/* Tab bar */}
      <nav className="flex flex-wrap items-center gap-1 border-b border-bg-border/40">
        {TABS.map(({ key, label, icon: Icon }) => {
          const count =
            key === "commentary" ? commentary.length : key === "brokers" ? brokers.length : strategies.length;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setActiveTab(key)}
              className={
                "inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12.5px] font-semibold transition-colors " +
                (activeTab === key
                  ? "border-accent-blue text-text-primary"
                  : "border-transparent text-text-muted hover:text-text-secondary")
              }
            >
              <Icon size={14} />
              {label}
              <span className="font-mono text-[11px] text-text-muted">{count}</span>
            </button>
          );
        })}
      </nav>

      {activeTab === "overview" ? (
        <div className="space-y-4">
          {strategies.length ? (
            strategies.map((s) => <StrategyRuntimeCard key={s.key} strategy={s} />)
          ) : (
            <Section title="Strategy runtimes" icon={<Activity size={16} />}>
              <Empty>
                {statusQuery.isLoading ? "Loading strategy runtime…" : "No strategy runtimes are reporting yet."}
              </Empty>
            </Section>
          )}
        </div>
      ) : null}

      {activeTab === "commentary" ? <CommentaryTimeline items={commentary} /> : null}

      {activeTab === "brokers" ? <BrokerPanel brokers={brokers} status={status} /> : null}
    </div>
  );
}

// ── Runtime banner ─────────────────────────────────────────────────────────

function RuntimeBanner({ status }: { status?: AgentStatus }) {
  const mi = status?.data_health?.market_intelligence;
  const expiries = status?.candidate_expiries ?? [];
  const errored = Boolean(status?.last_error);
  return (
    <Section title="Runtime" icon={<Shield size={16} />} padded>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          label={status?.running ? "running" : status?.loop_active ? "loop idle" : "stopped"}
          variant={status?.running ? "info" : status?.loop_active ? "success" : "neutral"}
        />
        <StatusBadge
          label={status?.kill_switch_active ? "kill switch active" : "kill switch released"}
          variant={status?.kill_switch_active ? "error" : "success"}
        />
        <StatusBadge
          label={mi?.market_open ? "market open" : "market closed"}
          variant={mi?.market_open ? "success" : "neutral"}
        />
        {mi?.execution_mode ? <StatusBadge label={mi.execution_mode} variant="info" /> : null}
        <span className="text-[11.5px] text-text-muted">Expiry {status?.target_expiry || "—"}</span>
        {expiries.length ? (
          <span className="text-[11.5px] text-text-muted">{expiries.length} candidate expiries</span>
        ) : null}
        {mi?.watchlist_rows_latest != null ? (
          <span className="text-[11.5px] text-text-muted">{mi.watchlist_rows_latest} ATM rows</span>
        ) : null}
        <span className="text-[11.5px] text-text-muted">
          Telegram {status?.telegram?.enabled ? `on · ${status.telegram.report_interval || "1h"}` : "off"}
        </span>
      </div>
      <div
        className={
          "mt-3 flex items-start gap-2 text-[12.5px] " + (errored ? "text-accent-red" : "text-text-secondary")
        }
      >
        {errored ? <AlertTriangle size={14} className="mt-0.5 shrink-0" /> : null}
        <span>{status?.last_error || status?.last_message || "Waiting for strategy state…"}</span>
      </div>
    </Section>
  );
}

// ── Strategy runtime card ──────────────────────────────────────────────────

function StrategyRuntimeCard({ strategy }: { strategy: Strategy }) {
  const s = strategy.summary || {};
  const agent = strategy.agent;
  const positions = strategy.positions || [];
  const signals = strategy.signals || [];
  const events = strategy.recent_events || [];
  const trades = strategy.trade_history || [];
  const winRate = s.win_rate != null ? s.win_rate : null;

  return (
    <Section
      title={strategy.label}
      icon={<Zap size={16} className="text-accent-purple" />}
      description={agent ? `${agent.timeframe || ""} · ${agent.instrument_scope || ""} · ${agent.execution_mode || ""}` : undefined}
      rightSlot={
        <div className="flex items-center gap-1.5">
          {agent?.mode ? <StatusBadge label={agent.mode.replaceAll("_", " ")} variant="info" /> : null}
          <StatusBadge
            label={formatSignedMoney(s.realized_pnl, 0)}
            tone={
              (s.realized_pnl ?? 0) >= 0
                ? "border-accent-green/35 bg-accent-green/10 text-accent-green"
                : "border-accent-red/35 bg-accent-red/10 text-accent-red"
            }
          />
        </div>
      }
    >
      {/* runtime status row */}
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2 text-[11.5px] text-text-muted">
        <span className="inline-flex items-center gap-1.5">
          <Clock size={12} /> Last scan {formatIST(strategy.last_scan_at ?? agent?.last_scan_at)}
        </span>
        {agent?.position_cap != null ? <span>Cap {agent.position_cap}</span> : null}
        <span>Signals {agent?.signals ?? signals.length}</span>
        <span className="min-w-0 flex-1 truncate text-text-secondary">
          {strategy.last_message || agent?.last_message || "Waiting for first scan."}
        </span>
      </div>

      {/* summary KPIs */}
      <section className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Equity" value={formatSignedMoney(s.total_equity, 0)} size="sm" />
        <MetricTile
          label="Unrealized"
          value={formatSignedMoney(s.unrealized_pnl, 0)}
          size="sm"
          color={tone(s.unrealized_pnl)}
        />
        <MetricTile label="Win rate" value={winRate != null ? formatPct(winRate) : "—"} size="sm" />
        <MetricTile label="Trades" value={String(s.total_trades ?? 0)} size="sm" detail={`${s.entries ?? 0}/${s.exits ?? 0} in/out`} />
        <MetricTile label="Profit factor" value={formatNumber(s.profit_factor, 2)} size="sm" color={tone((s.profit_factor ?? 0) - 1)} />
        <MetricTile label="Open" value={String(s.open_positions ?? positions.length)} size="sm" color={positions.length ? "text-accent-blue" : undefined} />
      </section>

      {/* open positions + signal lane */}
      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <div>
          <SubHead icon={<TrendingUp size={13} />} label="Open positions" count={positions.length} />
          {positions.length ? (
            <div className="space-y-2">
              {positions.slice(0, 6).map((p) => {
                const ce = (p.option_type || "").toUpperCase() === "CE";
                return (
                  <div key={p.symbol} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3 text-[12px]">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="font-semibold text-text-primary">{p.underlying}</span>
                        <StatusBadge label={p.option_type || "—"} variant={ce ? "success" : "error"} />
                        <span className="text-text-muted">{p.strike != null ? formatNumber(p.strike, 0) : "—"}</span>
                        {p.phase ? <span className="text-[10.5px] text-text-muted">{p.phase}</span> : null}
                      </div>
                      <span className={"font-mono font-semibold " + tone(p.unrealized_pnl)}>
                        {formatSignedMoney(p.unrealized_pnl, 0)}
                      </span>
                    </div>
                    <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px] text-text-muted md:grid-cols-4">
                      <span>Entry {formatNumber(p.entry_price, 2)}</span>
                      <span>Last {formatNumber(p.current_price, 2)}</span>
                      <span>Qty {p.qty ?? "—"}</span>
                      <span className={tone(p.return_pct)}>Ret {formatPct((p.return_pct ?? 0) / 100)}</span>
                    </div>
                    {p.signal_reason ? (
                      <div className="mt-1 truncate text-[11px] text-text-muted">{p.signal_reason}</div>
                    ) : null}
                  </div>
                );
              })}
              {positions.length > 6 ? (
                <div className="px-1 text-[11px] text-text-muted">+{positions.length - 6} more open positions</div>
              ) : null}
            </div>
          ) : (
            <Empty small>No open positions.</Empty>
          )}
        </div>

        <div>
          <SubHead icon={<Radio size={13} />} label="Signal lane" count={signals.length} />
          {signals.length ? (
            <div className="space-y-2">
              {signals.slice(0, 6).map((sig, i) => {
                const ce = (sig.direction || "").toUpperCase() === "CE";
                return (
                  <div key={`${sig.underlying}-${i}`} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3 text-[12px]">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-text-primary">{sig.underlying}</span>
                      {sig.direction ? <StatusBadge label={sig.direction} variant={ce ? "success" : "error"} /> : null}
                      <StatusBadge label={sig.status || "waiting"} variant="neutral" />
                      {sig.freshness ? <span className="text-[10.5px] text-text-muted">{sig.freshness}</span> : null}
                      {sig.priority_score != null ? (
                        <span className="ml-auto font-mono text-[11px] text-text-muted">★ {formatNumber(sig.priority_score, 1)}</span>
                      ) : null}
                    </div>
                    <div className="mt-1 truncate text-[11px] text-text-muted">
                      {sig.instruction || sig.reason || "—"}
                    </div>
                  </div>
                );
              })}
              {signals.length > 6 ? (
                <div className="px-1 text-[11px] text-text-muted">+{signals.length - 6} more in the signal lane</div>
              ) : null}
            </div>
          ) : (
            <Empty small>No signals in this lane.</Empty>
          )}
        </div>
      </div>

      {/* trade history + recent activity */}
      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <div>
          <SubHead icon={<Activity size={13} />} label="Trade history" count={trades.length} />
          {trades.length ? (
            <div className="-mx-2 overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-bg-border/60">
                    {["Contract", "Qty", "Entry", "Exit", "P&L", "Closed"].map((h, i) => (
                      <th
                        key={h}
                        className={
                          "px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted " +
                          (i === 0 ? "text-left" : "text-right")
                        }
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.slice(0, 8).map((t, i) => (
                    <tr key={`${t.symbol}-${t.exit_time}-${i}`} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                      <td className="px-2.5 py-1.5 text-left font-mono text-[12px] text-text-primary whitespace-nowrap">
                        {contractLabel(t.symbol)}
                      </td>
                      <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{t.qty ?? "—"}</td>
                      <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{formatNumber(t.entry_price, 2)}</td>
                      <td className="px-2.5 py-1.5 text-right font-mono text-[12px] text-text-secondary">{formatNumber(t.exit_price, 2)}</td>
                      <td className={"px-2.5 py-1.5 text-right font-mono text-[12px] font-semibold " + tone(t.pnl)}>
                        {formatSignedMoney(t.pnl, 0)}
                      </td>
                      <td className="px-2.5 py-1.5 text-right font-mono text-[11px] text-text-muted whitespace-nowrap">
                        {formatIST(t.exit_time)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty small>No closed trades yet.</Empty>
          )}
        </div>

        <div>
          <SubHead icon={<Zap size={13} />} label="Recent activity" count={events.length} />
          {events.length ? (
            <div className="space-y-2">
              {events.slice(0, 7).map((e, i) => {
                const exit = (e.event || "").toLowerCase().includes("exit");
                return (
                  <div
                    key={`${e.time}-${i}`}
                    className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2 text-[12px]"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <StatusBadge label={e.event || "—"} variant={exit ? "warn" : "info"} />
                        <span className="font-semibold text-text-primary">{e.underlying}</span>
                        <span className="text-text-muted">
                          {e.option_type || ""} {e.strike != null ? formatNumber(e.strike, 0) : ""}
                        </span>
                      </div>
                      {e.reason ? <div className="mt-0.5 truncate text-[11px] text-text-muted">{e.reason}</div> : null}
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="font-mono text-[12px] text-text-primary">{formatNumber(e.price, 2)}</div>
                      <div className="font-mono text-[10.5px] text-text-muted">{formatIST(e.time)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty small>No recent activity.</Empty>
          )}
        </div>
      </div>
    </Section>
  );
}

// ── Commentary timeline ────────────────────────────────────────────────────

const commentaryToneVariant = (t: string): "info" | "success" | "warn" | "error" | "neutral" => {
  switch (t) {
    case "trade":
      return "info";
    case "success":
      return "success";
    case "warning":
      return "warn";
    case "error":
      return "error";
    default:
      return "neutral";
  }
};

function CommentaryTimeline({ items }: { items: CommentaryItem[] }) {
  return (
    <Section
      title="Agent commentary"
      icon={<MessageSquare size={16} />}
      description="Live English notes on what the agent observes and why it trades, exits, or stands aside."
      rightSlot={<span className="text-[11.5px] text-text-muted">{items.length} recent notes</span>}
    >
      {items.length ? (
        <ol className="relative space-y-3 border-l border-bg-border/60 pl-4">
          {items.map((item, i) => (
            <li key={`${item.time}-${i}`} className="relative">
              <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-accent-blue/70" />
              <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2.5">
                <div className="flex items-center justify-between gap-3">
                  <StatusBadge label={item.scope || "agent"} variant={commentaryToneVariant(item.tone)} />
                  <span className="font-mono text-[10.5px] text-text-muted">{formatTimestamp(item.time)}</span>
                </div>
                <div className="mt-1.5 text-[12.5px] text-text-secondary">{item.message}</div>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <Empty>No agent commentary yet.</Empty>
      )}
    </Section>
  );
}

// ── Broker panel ───────────────────────────────────────────────────────────

function brokerVariant(b: BrokerStatusEntry): "success" | "warn" | "neutral" {
  if (isBrokerReady(b)) return "success";
  if (b.state && b.state !== "disconnected") return "warn";
  return "neutral";
}

function BrokerPanel({ brokers, status }: { brokers: BrokerStatusEntry[]; status?: AgentStatus }) {
  const snap = status?.data_health?.broker_snapshot;
  const dq = status?.data_health?.data_quality;
  const tokens: Array<{ name: string; health?: TokenHealth }> = [
    { name: "Fyers", health: snap?.fyers_token_health },
    { name: "Upstox", health: snap?.upstox_token_health },
  ];

  return (
    <div className="space-y-4">
      <Section
        title="Broker sessions"
        icon={<Radio size={16} />}
        rightSlot={
          <StatusBadge
            label={snap?.broker_ready ? "broker ready" : "broker blocked"}
            variant={snap?.broker_ready ? "success" : "error"}
          />
        }
      >
        <div className="grid gap-3 md:grid-cols-2">
          {brokers.map((b) => (
            <div key={b.broker} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold uppercase tracking-wide text-text-primary">{b.broker}</span>
                    <StatusBadge label={(b.state || "disconnected").replaceAll("_", " ")} variant={brokerVariant(b)} />
                    {b.needs_reconnect ? <StatusBadge label="reconnect" variant="error" /> : null}
                  </div>
                  {b.name || b.user_id ? (
                    <div className="mt-0.5 text-[11px] text-text-muted">
                      {b.name || "—"} {b.user_id ? `· ${b.user_id}` : ""}
                    </div>
                  ) : null}
                </div>
                <div className="shrink-0 text-right text-[10.5px] text-text-muted">
                  {b.connected_at ? formatTimestamp(b.connected_at) : ""}
                </div>
              </div>
              {b.detail ? <div className="mt-2 text-[11.5px] text-text-secondary">{b.detail}</div> : null}
            </div>
          ))}
          {!brokers.length ? <Empty>No broker status reported.</Empty> : null}
        </div>
      </Section>

      <Section title="Token health" icon={<Shield size={16} />}>
        <div className="grid gap-3 md:grid-cols-2">
          {tokens.map(({ name, health }) =>
            health ? (
              <div key={name} className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-text-primary">{name}</span>
                  <StatusBadge
                    label={(health.status || "unknown").replaceAll("_", " ")}
                    variant={health.valid ? "success" : health.needs_reconnect ? "error" : "warn"}
                  />
                </div>
                {health.message ? <div className="mt-1.5 text-[11.5px] text-text-secondary">{health.message}</div> : null}
                <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[10.5px] text-text-muted">
                  {health.source ? <span>src {health.source.replaceAll("_", " ")}</span> : null}
                  {health.expires_at_ist ? <span>expires {formatTimestamp(health.expires_at_ist)}</span> : null}
                  {health.checked_at ? <span>checked {formatTimestamp(health.checked_at)}</span> : null}
                </div>
              </div>
            ) : null,
          )}
        </div>
      </Section>

      {dq ? (
        <Section title="Data quality" icon={<Activity size={16} />}>
          <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile
              label="Overall"
              value={dq.overall || "—"}
              size="sm"
              color={dq.overall === "healthy" ? "text-accent-green" : "text-accent-amber"}
            />
            <MetricTile label="Symbols" value={String(dq.symbol_count ?? 0)} size="sm" detail={dq.market_state || ""} />
            <MetricTile label="Stale" value={String(dq.stale_count ?? 0)} size="sm" color={(dq.stale_count ?? 0) ? "text-accent-amber" : undefined} />
            <MetricTile label="Frozen" value={String(dq.frozen_count ?? 0)} size="sm" color={(dq.frozen_count ?? 0) ? "text-accent-red" : undefined} />
          </section>
        </Section>
      ) : null}
    </div>
  );
}

// ── Small helpers ──────────────────────────────────────────────────────────

function SubHead({ icon, label, count }: { icon: React.ReactNode; label: string; count: number }) {
  return (
    <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">
      {icon}
      {label}
      <span className="font-mono text-text-muted/80">{count}</span>
    </div>
  );
}

function Empty({ children, small }: { children: React.ReactNode; small?: boolean }) {
  return (
    <div
      className={
        "rounded-xl border border-dashed border-bg-border/60 text-center text-text-muted " +
        (small ? "px-3 py-5 text-[12px]" : "px-4 py-10 text-sm")
      }
    >
      {children}
    </div>
  );
}

/** OPT:LT:2026-06-30:4000:CE → "LT 4000 CE". */
function contractLabel(symbol?: string | null): string {
  if (!symbol) return "—";
  const parts = symbol.split(":");
  if (parts[0] === "OPT" && parts.length >= 5) {
    return `${parts[1]} ${parts[3]} ${parts[4]}`;
  }
  return parts.slice(1).join(" ") || symbol;
}
