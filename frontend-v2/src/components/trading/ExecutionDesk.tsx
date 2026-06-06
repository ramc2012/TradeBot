"use client";

/**
 * Execution desk — native v2.
 *
 * Replaces the v1 StrategyDashboard embed with a live execution / monitoring
 * surface built on the canonical strategy-agent control plane. This is the
 * operator's control desk: equity + day P&L at a glance, the open-position
 * blotter, the order blotter, and the risk panel — plus the two levers that
 * actually change behaviour (the kill-switch and the auto-run loop), wired as
 * confirm-gated, read-then-act mutations.
 *
 * Tabs:
 *   orders     → recent order/fill blotter
 *   positions  → live open positions + operator close
 *   risk       → risk-status panel (limits, daily loss, circuit breakers)
 *
 * Endpoints:
 *   /api/trading/strategy-agent/status            → lanes[] (summary, positions, trade_history), loop/auto-run flags
 *   /api/trading/orders                            → order blotter
 *   /api/trading/risk-status                       → trading_allowed, limits, circuit breakers
 *   /api/trading/kill-switch          (GET/PUT)    → kill-switch + auto-run state
 *   /api/trading/strategy-agent/run-once  (POST)   → force one scan cycle
 *   /api/trading/strategy-agent/positions/close (POST) → operator close
 */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  ListOrdered,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Wallet,
} from "lucide-react";

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
import { api } from "@/lib/api";

const TABS = [
  { key: "orders", label: "Orders", icon: ListOrdered },
  { key: "positions", label: "Positions", icon: Wallet },
  { key: "risk", label: "Risk", icon: ShieldCheck },
];

// ── Backend shapes (tolerant of nulls) ─────────────────────────────────────
type StrategyPosition = {
  symbol: string;
  underlying?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  phase?: string | null;
  regime?: string | null;
  signal_reason?: string | null;
  entered_at?: string | null;
  price_updated_at?: string | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
};

type TradeRecord = {
  symbol: string;
  action?: string | null;
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

type StrategySummary = {
  initial_capital?: number | null;
  available_capital?: number | null;
  total_equity?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  day_pnl?: number | null;
  total_trades?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  sharpe_ratio?: number | null;
  open_positions?: number | null;
};

type StrategyLane = {
  key: string;
  label?: string | null;
  agent?: {
    timeframe?: string | null;
    instrument_scope?: string | null;
    scope?: string | null;
    position_cap?: number | null;
  } | null;
  summary?: StrategySummary | null;
  positions?: StrategyPosition[] | null;
  trade_history?: TradeRecord[] | null;
  last_scan_at?: string | null;
  last_message?: string | null;
};

type AgentStatus = {
  enabled?: boolean;
  running?: boolean;
  loop_active?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  manual_restart_required?: boolean;
  scan_interval_seconds?: number | null;
  last_run_at?: string | null;
  next_scan_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  target_expiry?: string | null;
  active_windows?: number | null;
  strategies?: StrategyLane[] | null;
};

type KillSwitchState = {
  market?: string;
  kill_switch_active?: boolean;
  auto_run_enabled?: boolean;
  loop_active?: boolean;
  manual_restart_required?: boolean;
};

type OrderRow = {
  order_id?: string;
  symbol?: string;
  action?: string | null;
  qty?: number | null;
  price?: number | null;
  status?: string | null;
  order_time?: string | null;
  trading_symbol?: string | null;
};

type RiskStatus = {
  trading_allowed?: boolean;
  daily_loss?: number | null;
  max_daily_loss?: number | null;
  open_positions?: number | null;
  max_positions?: number | null;
  sizing_mode?: string | null;
  circuit_breakers?: {
    consecutive_stops?: number | null;
    paused_until?: string | null;
    drawdown_pct?: number | null;
    peak_equity?: number | null;
  } | null;
  config?: {
    max_loss_per_trade?: number | null;
    max_daily_loss?: number | null;
    max_open_positions?: number | null;
    concentration_limit?: number | null;
    max_sector_positions?: number | null;
  } | null;
};

const orderStatusVariant = (status?: string | null) => {
  const s = String(status || "").toUpperCase();
  if (s === "COMPLETE" || s === "FILLED" || s === "EXECUTED") return "success" as const;
  if (s === "OPEN" || s === "PENDING" || s === "TRIGGER_PENDING") return "info" as const;
  if (s === "CANCELLED" || s === "REJECTED" || s === "FAILED") return "error" as const;
  return "neutral" as const;
};

export default function ExecutionDesk() {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useUrlTab("orders");
  const [busy, setBusy] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["execution", "status"],
    queryFn: async () => (await api.get("/api/trading/strategy-agent/status")).data as AgentStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const killQuery = useQuery({
    queryKey: ["execution", "kill-switch"],
    queryFn: async () => (await api.get("/api/trading/kill-switch")).data as KillSwitchState,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const ordersQuery = useQuery({
    queryKey: ["execution", "orders"],
    queryFn: async () => (await api.get("/api/trading/orders")).data as OrderRow[],
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const riskQuery = useQuery({
    queryKey: ["execution", "risk"],
    queryFn: async () => (await api.get("/api/trading/risk-status")).data as RiskStatus,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const status = statusQuery.data;
  const kill = killQuery.data;
  const orders = useMemo(() => (Array.isArray(ordersQuery.data) ? ordersQuery.data : []), [ordersQuery.data]);
  const risk = riskQuery.data;

  const lanes = useMemo(() => status?.strategies ?? [], [status?.strategies]);

  // Desk-level aggregates across every execution lane.
  const desk = useMemo(() => {
    return lanes.reduce(
      (acc, lane) => {
        const s = lane.summary ?? {};
        acc.equity += s.total_equity ?? 0;
        acc.dayPnl += s.day_pnl ?? 0;
        acc.unrealized += s.unrealized_pnl ?? 0;
        acc.realized += s.realized_pnl ?? 0;
        acc.openPositions += s.open_positions ?? 0;
        acc.trades += s.total_trades ?? 0;
        return acc;
      },
      { equity: 0, dayPnl: 0, unrealized: 0, realized: 0, openPositions: 0, trades: 0 },
    );
  }, [lanes]);

  // Flatten every lane's open positions / trade history into one blotter view.
  const openPositions = useMemo(
    () =>
      lanes.flatMap((lane) =>
        (lane.positions ?? []).map((p) => ({ ...p, _laneKey: lane.key, _laneLabel: lane.label ?? lane.key })),
      ),
    [lanes],
  );
  const tradeHistory = useMemo(
    () =>
      lanes
        .flatMap((lane) => lane.trade_history ?? [])
        .slice()
        .sort((a, b) => String(b.exit_time ?? b.entry_time).localeCompare(String(a.exit_time ?? a.entry_time))),
    [lanes],
  );

  // Kill-switch / auto-run state — prefer the dedicated endpoint, fall back to status.
  const killActive = kill?.kill_switch_active ?? status?.kill_switch_active ?? false;
  const autoRun = kill?.auto_run_enabled ?? status?.auto_run_enabled ?? false;
  const loopActive = kill?.loop_active ?? status?.loop_active ?? false;

  const invalidateAll = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["execution", "status"] }),
      qc.invalidateQueries({ queryKey: ["execution", "kill-switch"] }),
      qc.invalidateQueries({ queryKey: ["execution", "orders"] }),
      qc.invalidateQueries({ queryKey: ["execution", "risk"] }),
    ]);
  };

  // ── Read-then-act mutations (confirm-gated) ──────────────────────────────
  const toggleKillSwitch = async () => {
    const next = !killActive;
    const verb = next ? "ENGAGE the kill switch (block all new entries + cancel open orders)" : "RELEASE the kill switch and resume execution";
    if (typeof window !== "undefined" && !window.confirm(`${verb}?`)) return;
    setBusy("kill");
    try {
      await api.put("/api/trading/kill-switch", { active: next });
      await invalidateAll();
    } catch {
      /* surfaced via banner / staleness */
    } finally {
      setBusy(null);
    }
  };

  const toggleAutoRun = async () => {
    const next = !autoRun;
    // The control plane exposes auto-run via the kill-switch PUT: releasing the
    // switch re-arms auto-run; engaging it disables the loop. We map the
    // operator's auto-run intent onto that lever and confirm explicitly.
    const verb = next ? "RE-ARM the auto-run loop (release kill switch, resume scans)" : "DISABLE the auto-run loop (engage kill switch)";
    if (typeof window !== "undefined" && !window.confirm(`${verb}?`)) return;
    setBusy("autorun");
    try {
      await api.put("/api/trading/kill-switch", { active: !next });
      await invalidateAll();
    } catch {
      /* surfaced via banner / staleness */
    } finally {
      setBusy(null);
    }
  };

  const runOnce = async () => {
    setBusy("run");
    try {
      await api.post("/api/trading/strategy-agent/run-once", null, { params: { force: true } });
      await invalidateAll();
    } catch {
      /* surfaced via banner / staleness */
    } finally {
      setBusy(null);
    }
  };

  const closePosition = async (laneKey: string, symbol: string) => {
    if (typeof window !== "undefined" && !window.confirm(`Operator-close ${symbol} at the latest agent mark?`)) return;
    setBusy(`close:${symbol}`);
    try {
      await api.post("/api/trading/strategy-agent/positions/close", {
        strategy_key: laneKey,
        symbol,
        reason: "operator_override",
      });
      await invalidateAll();
    } catch {
      /* surfaced via banner / staleness */
    } finally {
      setBusy(null);
    }
  };

  const anyError = statusQuery.isError || killQuery.isError || ordersQuery.isError || riskQuery.isError;
  const isFetching =
    statusQuery.isFetching || killQuery.isFetching || ordersQuery.isFetching || riskQuery.isFetching;

  return (
    <DeskShell
      title="Execution Desk"
      description="Live NSE paper-execution control plane — equity, P&L, order + position blotter, risk limits, and the kill-switch / auto-run levers."
      asOf={status?.last_run_at ?? undefined}
      isFetching={isFetching}
      isLive={loopActive}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href="http://localhost:3000/strategy"
      rightSlot={
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={runOnce}
            disabled={busy != null}
            className="inline-flex items-center gap-1.5 rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-2.5 py-1.5 text-[11.5px] font-semibold text-accent-blue transition-colors hover:bg-accent-blue/20 disabled:opacity-50"
          >
            {busy === "run" ? <RefreshCw size={12} className="animate-spin" /> : <Play size={12} />}
            Run scan
          </button>
          <button
            type="button"
            onClick={toggleAutoRun}
            disabled={busy != null}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11.5px] font-semibold transition-colors disabled:opacity-50",
              autoRun
                ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                : "border-bg-border bg-bg-primary/30 text-text-secondary hover:border-bg-active hover:text-text-primary",
            )}
            title="Toggle the auto-run loop"
          >
            <Activity size={12} className={loopActive ? "animate-pulse" : undefined} />
            {busy === "autorun" ? "…" : autoRun ? "Auto-run on" : "Auto-run off"}
          </button>
          <button
            type="button"
            onClick={toggleKillSwitch}
            disabled={busy != null}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11.5px] font-semibold transition-colors disabled:opacity-50",
              killActive
                ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
            )}
            title={killActive ? "Release the kill switch" : "Engage the kill switch"}
          >
            {killActive ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />}
            {busy === "kill" ? "…" : killActive ? "Release kill" : "Kill switch"}
          </button>
        </div>
      }
    >
      {/* status / error banner */}
      {anyError ? (
        <div className="flex items-center gap-2 rounded-xl border border-accent-red/30 bg-accent-red/10 px-3 py-2 text-[12.5px] text-accent-red">
          <AlertTriangle size={13} />
          Lost contact with the execution control plane. Retrying…
        </div>
      ) : status?.last_error ? (
        <div className="flex items-center gap-2 rounded-xl border border-accent-red/30 bg-accent-red/10 px-3 py-2 text-[12.5px] text-accent-red">
          <AlertTriangle size={13} />
          {status.last_error}
        </div>
      ) : status?.last_message ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-secondary/20 px-3 py-2 text-[12px] text-text-muted">
          <span className="truncate">{status.last_message}</span>
          <span className="shrink-0">scan {formatNumber(status?.scan_interval_seconds, 0)}s · windows {status?.active_windows ?? 0}</span>
        </div>
      ) : null}

      {/* KPI strip */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile
          label="Equity"
          value={formatMoney(desk.equity)}
          detail={`realized ${formatSignedMoney(desk.realized)}`}
          color={tone(desk.realized + desk.unrealized)}
        />
        <MetricTile
          label="Day P&L"
          value={formatSignedMoney(desk.dayPnl)}
          detail={`open ${formatSignedMoney(desk.unrealized)}`}
          color={tone(desk.dayPnl)}
        />
        <MetricTile
          label="Open positions"
          value={String(desk.openPositions)}
          detail={`${risk?.open_positions ?? desk.openPositions} / ${risk?.max_positions ?? "—"} cap`}
        />
        <MetricTile
          label="Auto-run loop"
          value={loopActive ? "active" : autoRun ? "armed" : "manual"}
          detail={status?.next_scan_at ? `next ${formatIST(status.next_scan_at)}` : "—"}
          color={loopActive ? "text-accent-green" : autoRun ? "text-accent-blue" : undefined}
        />
        <MetricTile
          label="Kill switch"
          value={killActive ? "engaged" : "released"}
          detail={status?.manual_restart_required || kill?.manual_restart_required ? "manual restart req." : "ready"}
          color={killActive ? "text-accent-red" : "text-accent-green"}
        />
        <MetricTile
          label="Risk state"
          value={risk?.trading_allowed ? "allowed" : "blocked"}
          detail={`loss ${formatMoney(risk?.daily_loss)} / ${formatMoney(risk?.max_daily_loss)}`}
          color={risk?.trading_allowed ? "text-accent-green" : "text-accent-red"}
        />
      </section>

      {activeTab === "orders" ? (
        <OrdersTab orders={orders} trades={tradeHistory} loading={ordersQuery.isLoading} />
      ) : null}

      {activeTab === "positions" ? (
        <PositionsTab
          positions={openPositions}
          busySymbol={busy?.startsWith("close:") ? busy.slice("close:".length) : null}
          disabled={busy != null}
          onClose={closePosition}
        />
      ) : null}

      {activeTab === "risk" ? (
        <RiskTab risk={risk} killActive={killActive} autoRun={autoRun} loopActive={loopActive} />
      ) : null}
    </DeskShell>
  );
}

// ── Orders tab ─────────────────────────────────────────────────────────────
function OrdersTab({
  orders,
  trades,
  loading,
}: {
  orders: OrderRow[];
  trades: TradeRecord[];
  loading: boolean;
}) {
  return (
    <div className="space-y-4">
      <Section
        title="Order blotter"
        icon={<ListOrdered size={16} />}
        description="Live paper order queue from the execution lane."
        rightSlot={<StatusBadge label={`${orders.length} live`} variant={orders.length ? "info" : "neutral"} />}
      >
        <BlotterTable
          head={["Order", "Action", "Qty", "Price", "Status"]}
          empty={loading ? "Loading orders…" : "No live orders in the blotter."}
          rows={orders.map((o, i) => (
            <tr key={o.order_id ?? i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              <td className={`${TD} text-left`}>
                <div className="font-medium text-text-primary">{o.trading_symbol ?? o.symbol ?? "—"}</div>
                <div className="text-[10.5px] text-text-muted">{o.order_id ?? "—"}</div>
              </td>
              <td className={`${TD} text-left`}>
                <span className={clsx("font-semibold", o.action === "BUY" ? "text-accent-green" : o.action === "SELL" ? "text-accent-red" : "text-text-secondary")}>
                  {o.action ?? "—"}
                </span>
              </td>
              <td className={`${TD} text-right font-mono text-text-primary`}>{o.qty ?? "—"}</td>
              <td className={`${TD} text-right font-mono text-text-primary`}>{formatNumber(o.price, 2)}</td>
              <td className={`${TD} text-right`}>
                <StatusBadge label={String(o.status ?? "—")} variant={orderStatusVariant(o.status)} />
              </td>
            </tr>
          ))}
        />
      </Section>

      <Section
        title="Recent fills"
        icon={<Activity size={16} />}
        description="Latest closed round-trips across every execution lane."
        rightSlot={<StatusBadge label={`${trades.length} closed`} variant="neutral" />}
      >
        <BlotterTable
          head={["Contract", "Side", "Qty", "Entry → Exit", "P&L", "Exited"]}
          empty="No closed trades yet."
          rows={trades.slice(0, 60).map((t, i) => {
            const long = (t.option_type ?? t.action) === "CE" || t.action === "BUY";
            return (
              <tr key={`${t.symbol}-${t.exit_time ?? t.entry_time ?? i}`} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                <td className={`${TD} text-left`}>
                  <div className="font-medium text-text-primary">{t.symbol?.split(":").slice(1).join(" ") || t.symbol}</div>
                  <div className="text-[10.5px] text-text-muted">
                    {(t.option_type ?? "—")} {t.strike ?? ""} {t.expiry ? `· ${t.expiry}` : ""}
                  </div>
                </td>
                <td className={`${TD} text-left`}>
                  <StatusBadge label={long ? "LONG" : "SHORT"} variant={long ? "success" : "error"} />
                </td>
                <td className={`${TD} text-right font-mono text-text-primary`}>{t.qty ?? "—"}</td>
                <td className={`${TD} text-right font-mono`}>
                  {formatNumber(t.entry_price, 1)} → {formatNumber(t.exit_price, 1)}
                </td>
                <td className={`${TD} text-right font-mono ${tone(t.pnl)}`}>{formatSignedMoney(t.pnl)}</td>
                <td className={`${TD} text-right text-text-muted`}>{formatIST(t.exit_time)}</td>
              </tr>
            );
          })}
        />
      </Section>
    </div>
  );
}

// ── Positions tab ──────────────────────────────────────────────────────────
type DeskPosition = StrategyPosition & { _laneKey: string; _laneLabel: string };

function PositionsTab({
  positions,
  busySymbol,
  disabled,
  onClose,
}: {
  positions: DeskPosition[];
  busySymbol: string | null;
  disabled: boolean;
  onClose: (laneKey: string, symbol: string) => void;
}) {
  return (
    <Section
      title="Open positions"
      icon={<Wallet size={16} />}
      description="Live marks across every execution lane. Operator close exits at the latest agent mark."
      rightSlot={<StatusBadge label={`${positions.length} open`} variant={positions.length ? "info" : "neutral"} />}
    >
      <BlotterTable
        head={["Contract", "Dir", "Qty", "Entry → LTP", "Open P&L", "Ret%", "Regime", "Updated", "Operator"]}
        empty="No open positions."
        rows={positions.map((p, i) => {
          const long = p.option_type === "CE";
          const closing = busySymbol === p.symbol;
          return (
            <tr key={`${p.symbol}-${p.entered_at ?? i}`} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              <td className={`${TD} text-left`}>
                <div className="font-medium text-text-primary">{p.underlying ?? p.symbol}</div>
                <div className="text-[10.5px] text-text-muted">
                  {p.option_type ?? "—"} {p.strike ?? ""} {p.expiry ? `· ${p.expiry}` : ""}
                </div>
              </td>
              <td className={`${TD} text-left`}>
                <StatusBadge label={long ? "CE" : p.option_type ?? "—"} variant={long ? "success" : "error"} />
              </td>
              <td className={`${TD} text-right font-mono text-text-primary`}>{p.qty ?? "—"}</td>
              <td className={`${TD} text-right font-mono`}>
                {formatNumber(p.entry_price, 2)} → {formatNumber(p.current_price, 2)}
              </td>
              <td className={`${TD} text-right font-mono ${tone(p.unrealized_pnl)}`}>{formatSignedMoney(p.unrealized_pnl)}</td>
              <td className={`${TD} text-right font-mono ${tone(p.return_pct)}`}>
                {p.return_pct != null ? formatPct(p.return_pct, 1, { asPercent: true }) : "—"}
              </td>
              <td className={`${TD} text-left text-text-secondary`}>{p.regime ?? p.phase ?? "—"}</td>
              <td className={`${TD} text-right text-text-muted`}>{formatIST(p.price_updated_at ?? p.entered_at)}</td>
              <td className={`${TD} text-right`}>
                <button
                  type="button"
                  onClick={() => onClose(p._laneKey, p.symbol)}
                  disabled={disabled}
                  title="Operator close at the latest agent mark"
                  className="rounded-lg border border-accent-red/35 bg-accent-red/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-accent-red transition-colors hover:bg-accent-red/20 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {closing ? "Closing" : "Close"}
                </button>
              </td>
            </tr>
          );
        })}
      />
    </Section>
  );
}

// ── Risk tab ───────────────────────────────────────────────────────────────
function RiskTab({
  risk,
  killActive,
  autoRun,
  loopActive,
}: {
  risk?: RiskStatus;
  killActive: boolean;
  autoRun: boolean;
  loopActive: boolean;
}) {
  const cb = risk?.circuit_breakers ?? {};
  const cfg = risk?.config ?? {};
  const lossPct =
    risk?.max_daily_loss && risk.max_daily_loss > 0 ? Math.min(100, ((risk.daily_loss ?? 0) / risk.max_daily_loss) * 100) : 0;
  const posPct =
    risk?.max_positions && risk.max_positions > 0 ? Math.min(100, ((risk.open_positions ?? 0) / risk.max_positions) * 100) : 0;

  return (
    <div className="space-y-4">
      <Section
        title="Risk status"
        icon={<ShieldCheck size={16} />}
        rightSlot={
          <StatusBadge
            label={risk?.trading_allowed ? "trading allowed" : "trading blocked"}
            variant={risk?.trading_allowed ? "success" : "error"}
          />
        }
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-3">
            <Gauge
              label="Daily loss"
              value={formatMoney(risk?.daily_loss)}
              limit={`limit ${formatMoney(risk?.max_daily_loss)}`}
              pct={lossPct}
              danger
            />
            <Gauge
              label="Open positions"
              value={String(risk?.open_positions ?? 0)}
              limit={`cap ${risk?.max_positions ?? "—"}`}
              pct={posPct}
            />
          </div>
          <div className="grid grid-cols-2 gap-2 self-start text-center">
            <RiskCell label="Sizing mode" value={String(risk?.sizing_mode ?? "—")} />
            <RiskCell
              label="Consecutive stops"
              value={String(cb.consecutive_stops ?? 0)}
              valueClass={(cb.consecutive_stops ?? 0) > 0 ? "text-accent-amber" : undefined}
            />
            <RiskCell label="Drawdown" value={formatPct(cb.drawdown_pct, 2, { asPercent: true })} />
            <RiskCell label="Paused until" value={cb.paused_until ? formatIST(cb.paused_until) : "—"} />
          </div>
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Risk limits" icon={<ShieldAlert size={16} />}>
          <div className="grid grid-cols-2 gap-2 text-center">
            <RiskCell label="Max loss / trade" value={formatMoney(cfg.max_loss_per_trade)} />
            <RiskCell label="Max daily loss" value={formatMoney(cfg.max_daily_loss)} />
            <RiskCell label="Max open positions" value={String(cfg.max_open_positions ?? "—")} />
            <RiskCell label="Max sector positions" value={String(cfg.max_sector_positions ?? "—")} />
            <RiskCell
              label="Concentration limit"
              value={cfg.concentration_limit != null ? formatPct(cfg.concentration_limit) : "—"}
            />
            <RiskCell label="Peak equity" value={formatMoney(cb.peak_equity)} />
          </div>
        </Section>

        <Section title="Execution levers" icon={<Activity size={16} />}>
          <div className="space-y-2.5">
            <LeverRow
              label="Kill switch"
              state={killActive ? "engaged" : "released"}
              variant={killActive ? "error" : "success"}
              detail={killActive ? "New entries blocked; open orders cancelled." : "Execution enabled."}
            />
            <LeverRow
              label="Auto-run loop"
              state={loopActive ? "active" : autoRun ? "armed" : "manual"}
              variant={loopActive ? "success" : autoRun ? "info" : "neutral"}
              detail={loopActive ? "Scanning on the configured cadence." : autoRun ? "Armed; waiting for the next market window." : "Manual scan only."}
            />
            <LeverRow
              label="Trading window"
              state={risk?.trading_allowed ? "open" : "closed"}
              variant={risk?.trading_allowed ? "success" : "warn"}
              detail={risk?.trading_allowed ? "Risk checks passing." : "Blocked by a risk check or the kill switch."}
            />
          </div>
        </Section>
      </div>
    </div>
  );
}

// ── Local primitives ───────────────────────────────────────────────────────
const TH = "px-2.5 py-2 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold";
const TD = "px-2.5 py-2 text-[12px] text-text-secondary whitespace-nowrap";

function BlotterTable({ head, rows, empty }: { head: string[]; rows: React.ReactNode[]; empty: string }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      {rows.length === 0 ? (
        <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-sm text-text-muted">
          {empty}
        </div>
      ) : (
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-bg-border/60">
              {head.map((h, i) => (
                <th key={i} className={clsx(TH, i === 0 ? "text-left" : "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      )}
    </div>
  );
}

function Gauge({
  label,
  value,
  limit,
  pct,
  danger,
}: {
  label: string;
  value: string;
  limit: string;
  pct: number;
  danger?: boolean;
}) {
  const bar = danger
    ? pct > 80
      ? "rgb(var(--accent-red))"
      : pct > 50
        ? "rgb(var(--accent-amber))"
        : "rgb(var(--accent-green))"
    : "rgb(var(--accent-blue))";
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2.5">
      <div className="flex items-end justify-between">
        <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">{label}</span>
        <span className="font-mono text-sm font-semibold text-text-primary">{value}</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-bg-primary/40">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: bar }} />
      </div>
      <div className="mt-1 text-[10.5px] text-text-muted">{limit}</div>
    </div>
  );
}

function RiskCell({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={clsx("mt-0.5 font-mono text-[13px]", valueClass ?? "text-text-primary")}>{value}</div>
    </div>
  );
}

function LeverRow({
  label,
  state,
  variant,
  detail,
}: {
  label: string;
  state: string;
  variant: "neutral" | "success" | "warn" | "error" | "info";
  detail: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2.5">
      <div className="min-w-0">
        <div className="text-[12.5px] font-semibold text-text-primary">{label}</div>
        <div className="mt-0.5 text-[11px] text-text-muted">{detail}</div>
      </div>
      <StatusBadge label={state} variant={variant} />
    </div>
  );
}
