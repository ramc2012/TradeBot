"use client";

/**
 * ExecutionPanels — Institutional Convergence trade-book / open-positions /
 * statistics / order-log building blocks.
 *
 * Data contract: fed by the NEW `/api/institutional-convergence{,/commodity}/
 * {trades,orders,statistics}` endpoints. Those routes are deployed separately
 * from this desk, so EVERY panel here must degrade gracefully:
 *   · endpoint 404s        → skeleton state + "endpoint pending deployment"
 *   · endpoint missing but paper snapshot has closed positions →
 *     client-side derived stats/trades, clearly labelled DERIVED
 *   · unknown field spellings → the normalizers below accept several aliases
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ArrowUpDown, BarChart3, ListChecks, ScrollText, Target } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatMoney,
  formatNumber,
  formatSignedMoney,
  toDate,
} from "@/components/desk-ui";
import { LastUpdated } from "@/components/common/LastUpdated";
import {
  getInstitutionalConvergenceOrders,
  getInstitutionalConvergenceStatistics,
  getInstitutionalConvergenceTrades,
} from "@/lib/api";

// ── Types ────────────────────────────────────────────────────────────────

export type ConvergenceTrade = {
  position_id?: string;
  symbol: string;
  direction: string;
  entry_price?: number | null;
  exit_price?: number | null;
  stop?: number | null;
  initial_stop?: number | null;
  target1?: number | null;
  target2?: number | null;
  lots?: number | null;
  initial_lots?: number | null;
  lot_size?: number | null;
  opened_at?: string | null;
  closed_at?: string | null;
  exit_reason?: string | null;
  realized_pnl?: number | null;
  r_multiple?: number | null;
  risk_fraction?: number | null;
  session_date?: string | null;
  futures_contract?: string | null;
};

export type ConvergenceOrder = {
  time?: string | null;
  symbol?: string | null;
  event?: string | null;
  direction?: string | null;
  price?: number | null;
  lots?: number | null;
  note?: string | null;
  position_id?: string | null;
  derived?: boolean;
};

export type ConvergenceStatistics = {
  total_trades?: number | null;
  wins?: number | null;
  losses?: number | null;
  win_rate?: number | null; // ratio 0..1
  profit_factor?: number | null;
  expectancy?: number | null; // ₹ per trade
  avg_win?: number | null;
  avg_loss?: number | null;
  max_drawdown?: number | null; // ₹ (positive magnitude)
  realized_pnl?: number | null;
  daily_pnl?: Array<{ date: string; pnl: number }>;
  derived?: boolean;
};

type OpenPosition = {
  position_id: string;
  symbol: string;
  direction: string;
  entry_price: number;
  current_price: number;
  stop: number;
  target1: number;
  target2?: number;
  lots: number;
  initial_lots: number;
  lot_size: number;
  target1_done: boolean;
  opened_at?: string;
};

// ── Normalizers (tolerate several backend spellings + bare arrays) ──────

const num = (v: unknown): number | null => {
  const n = Number(v);
  return v == null || Number.isNaN(n) ? null : n;
};

const pickArray = (payload: unknown, keys: string[]): unknown[] => {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    for (const key of keys) if (Array.isArray(obj[key])) return obj[key] as unknown[];
    // one level of nesting ({data: {trades: [...]}})
    for (const value of Object.values(obj)) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        for (const key of keys) {
          const nested = (value as Record<string, unknown>)[key];
          if (Array.isArray(nested)) return nested;
        }
      }
    }
  }
  return [];
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeTrade(row: any): ConvergenceTrade {
  return {
    position_id: row?.position_id ?? row?.id ?? row?.trade_id,
    symbol: String(row?.symbol ?? row?.root ?? "—"),
    direction: String(row?.direction ?? row?.side ?? row?.action ?? "—").toUpperCase(),
    entry_price: num(row?.entry_price ?? row?.entry),
    exit_price: num(row?.exit_price ?? row?.exit ?? row?.current_price),
    stop: num(row?.stop ?? row?.stop_price),
    initial_stop: num(row?.initial_stop ?? row?.stop_initial ?? row?.original_stop),
    target1: num(row?.target1 ?? row?.t1),
    target2: num(row?.target2 ?? row?.t2),
    lots: num(row?.lots ?? row?.quantity),
    initial_lots: num(row?.initial_lots ?? row?.lots ?? row?.quantity),
    lot_size: num(row?.lot_size),
    opened_at: row?.opened_at ?? row?.entry_time ?? row?.created_at ?? null,
    closed_at: row?.closed_at ?? row?.exit_time ?? row?.updated_at ?? null,
    exit_reason: row?.exit_reason ?? row?.reason ?? null,
    realized_pnl: num(row?.realized_pnl ?? row?.pnl ?? row?.net_pnl),
    r_multiple: num(row?.r_multiple ?? row?.r ?? row?.rr ?? row?.r_mult),
    risk_fraction: num(row?.risk_fraction),
    session_date: row?.session_date ?? null,
    futures_contract: row?.futures_contract ?? row?.contract ?? null,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function normalizeOrder(row: any): ConvergenceOrder {
  return {
    time: row?.time ?? row?.timestamp ?? row?.created_at ?? row?.ts ?? null,
    symbol: row?.symbol ?? row?.root ?? null,
    event: String(row?.event ?? row?.type ?? row?.kind ?? row?.action ?? "order").toLowerCase(),
    direction: row?.direction ?? row?.side ?? null,
    price: num(row?.price ?? row?.fill_price ?? row?.entry_price),
    lots: num(row?.lots ?? row?.quantity ?? row?.qty),
    note: row?.note ?? row?.reason ?? row?.detail ?? null,
    position_id: row?.position_id ?? null,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function normalizeStatistics(payload: any): ConvergenceStatistics | null {
  const root = payload?.statistics ?? payload?.stats ?? payload?.data ?? payload;
  if (!root || typeof root !== "object" || Array.isArray(root)) return null;
  const winRateRaw = num(root.win_rate ?? root.winRate ?? root.hit_rate ?? root.hit_ratio);
  const daily = (() => {
    const arr = pickArray(root.daily_pnl ?? root.daily ?? root.per_day ?? [], ["daily_pnl", "days", "rows"]);
    if (arr.length) {
      return arr
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .map((r: any) => ({ date: String(r?.date ?? r?.day ?? r?.session_date ?? ""), pnl: num(r?.pnl ?? r?.realized_pnl ?? r?.value) ?? 0 }))
        .filter((r) => r.date);
    }
    const obj = root.daily_pnl ?? root.daily;
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      return Object.entries(obj as Record<string, unknown>).map(([date, pnl]) => ({ date, pnl: num(pnl) ?? 0 }));
    }
    return [];
  })().sort((a, b) => a.date.localeCompare(b.date));
  const dd = num(root.max_drawdown ?? root.max_dd ?? root.drawdown);
  return {
    total_trades: num(root.total_trades ?? root.trade_count ?? root.count ?? root.trades),
    wins: num(root.wins ?? root.win_count),
    losses: num(root.losses ?? root.loss_count),
    win_rate: winRateRaw == null ? null : winRateRaw > 1 ? winRateRaw / 100 : winRateRaw,
    profit_factor: num(root.profit_factor ?? root.pf),
    expectancy: num(root.expectancy ?? root.avg_pnl ?? root.expectancy_per_trade),
    avg_win: num(root.avg_win ?? root.average_win),
    avg_loss: num(root.avg_loss ?? root.average_loss),
    max_drawdown: dd == null ? null : Math.abs(dd),
    realized_pnl: num(root.realized_pnl ?? root.net_pnl ?? root.total_pnl),
    daily_pnl: daily,
  };
}

// ── Client-side derivations (fallback until endpoints deploy) ────────────

export function computeRMultiple(trade: ConvergenceTrade, equityBase?: number | null): { r: number | null; approx: boolean } {
  if (trade.r_multiple != null) return { r: trade.r_multiple, approx: false };
  const pnl = trade.realized_pnl;
  if (pnl == null) return { r: null, approx: false };
  const stop = trade.initial_stop ?? trade.stop;
  const entry = trade.entry_price;
  const perUnit = entry != null && stop != null ? Math.abs(entry - stop) : 0;
  const units = (trade.initial_lots ?? trade.lots ?? 0) * (trade.lot_size ?? 1);
  const riskFromStop = perUnit * units;
  if (riskFromStop > 1e-9) return { r: pnl / riskFromStop, approx: trade.initial_stop == null };
  // Stop already moved to break-even on closed trades — approximate with the
  // risk fraction against the lane's capital base.
  if (trade.risk_fraction && equityBase) {
    const risked = trade.risk_fraction * equityBase;
    if (risked > 1e-9) return { r: pnl / risked, approx: true };
  }
  return { r: null, approx: false };
}

function istDay(value?: string | null): string {
  if (!value) return "";
  const d = toDate(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }); // YYYY-MM-DD
}

export function deriveStatistics(trades: ConvergenceTrade[], equityBase?: number | null): ConvergenceStatistics | null {
  const closed = trades.filter((t) => t.realized_pnl != null);
  if (!closed.length) return null;
  const pnls = closed.map((t) => t.realized_pnl as number);
  const wins = pnls.filter((p) => p > 0);
  const losses = pnls.filter((p) => p < 0);
  const grossWin = wins.reduce((a, b) => a + b, 0);
  const grossLoss = Math.abs(losses.reduce((a, b) => a + b, 0));
  const ordered = [...closed].sort((a, b) => String(a.closed_at ?? "").localeCompare(String(b.closed_at ?? "")));
  let equity = 0;
  let peak = 0;
  let maxDd = 0;
  for (const t of ordered) {
    equity += t.realized_pnl as number;
    peak = Math.max(peak, equity);
    maxDd = Math.max(maxDd, peak - equity);
  }
  const byDay = new Map<string, number>();
  for (const t of ordered) {
    const day = t.session_date || istDay(t.closed_at) || "unknown";
    byDay.set(day, (byDay.get(day) ?? 0) + (t.realized_pnl as number));
  }
  void equityBase;
  return {
    total_trades: closed.length,
    wins: wins.length,
    losses: losses.length,
    win_rate: wins.length / closed.length,
    profit_factor: grossLoss > 1e-9 ? grossWin / grossLoss : wins.length ? Infinity : null,
    expectancy: pnls.reduce((a, b) => a + b, 0) / closed.length,
    avg_win: wins.length ? grossWin / wins.length : null,
    avg_loss: losses.length ? -grossLoss / losses.length : null,
    max_drawdown: maxDd,
    realized_pnl: pnls.reduce((a, b) => a + b, 0),
    daily_pnl: Array.from(byDay.entries()).map(([date, pnl]) => ({ date, pnl })).sort((a, b) => a.date.localeCompare(b.date)),
    derived: true,
  };
}

export function deriveOrders(trades: ConvergenceTrade[]): ConvergenceOrder[] {
  const events: ConvergenceOrder[] = [];
  for (const t of trades) {
    if (t.opened_at) {
      events.push({ time: t.opened_at, symbol: t.symbol, event: "entry", direction: t.direction, price: t.entry_price, lots: t.initial_lots ?? t.lots, position_id: t.position_id, derived: true });
    }
    if (t.closed_at) {
      events.push({ time: t.closed_at, symbol: t.symbol, event: "exit", direction: t.direction, price: t.exit_price, lots: t.initial_lots ?? t.lots, note: t.exit_reason, position_id: t.position_id, derived: true });
    }
  }
  return events.sort((a, b) => String(b.time ?? "").localeCompare(String(a.time ?? "")));
}

// ── Data hook — Promise.allSettled so one 404 never sinks the rest ──────

export type ExecutionData = {
  trades: ConvergenceTrade[] | null; // null = endpoint unavailable
  orders: ConvergenceOrder[] | null;
  statistics: ConvergenceStatistics | null;
  missing: string[]; // which endpoints 404'd / failed
};

export function useConvergenceExecution(market: "NSE" | "MCX") {
  return useQuery<ExecutionData>({
    queryKey: ["institutional-convergence", market, "execution"],
    queryFn: async () => {
      const [tradesRes, ordersRes, statsRes] = await Promise.allSettled([
        getInstitutionalConvergenceTrades(market),
        getInstitutionalConvergenceOrders(market),
        getInstitutionalConvergenceStatistics(market),
      ]);
      const missing: string[] = [];
      const trades = tradesRes.status === "fulfilled"
        ? pickArray(tradesRes.value.data, ["trades", "closed_positions", "rows", "data", "items"]).map(normalizeTrade)
        : (missing.push("trades"), null);
      const orders = ordersRes.status === "fulfilled"
        ? pickArray(ordersRes.value.data, ["orders", "order_log", "events", "rows", "data", "items"]).map(normalizeOrder)
        : (missing.push("orders"), null);
      const statistics = statsRes.status === "fulfilled" ? normalizeStatistics(statsRes.value.data) : (missing.push("statistics"), null);
      return { trades, orders, statistics, missing };
    },
    refetchInterval: REFRESH_MS.snapshot,
    // Endpoints may 404 until the backend deploy lands — don't retry-storm.
    retry: 1,
  });
}

// ── Shared bits ──────────────────────────────────────────────────────────

function PendingNote({ endpoint }: { endpoint: string }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center">
      <div className="h-2 w-40 animate-pulse rounded bg-bg-secondary/60" />
      <div className="h-2 w-28 animate-pulse rounded bg-bg-secondary/50" />
      <div className="mt-2 text-[11px] text-text-muted">
        <span className="font-mono">/{endpoint}</span> endpoint not deployed yet — this panel activates automatically once the backend route lands.
      </div>
    </div>
  );
}

const EXIT_REASON_VARIANT: Record<string, "success" | "error" | "warn" | "info" | "neutral"> = {
  target: "success", target1: "success", target2: "success", t2: "success", trailing: "success",
  stop: "error", stop_loss: "error", stopped: "error", hard_stop: "error",
  cvd_reversal: "warn", reversal: "warn", anti_chase: "warn",
  eod: "info", session_close: "info", time: "info", time_stop: "info", market_close: "info", force_close: "info",
};

export function ExitReasonChip({ reason }: { reason?: string | null }) {
  if (!reason) return <span className="text-text-muted">—</span>;
  const key = String(reason).toLowerCase();
  const variant = EXIT_REASON_VARIANT[key] ?? (key.includes("stop") ? "error" : key.includes("target") ? "success" : key.includes("eod") || key.includes("close") ? "info" : "warn");
  return <StatusBadge label={key.replace(/_/g, " ")} variant={variant} className="normal-case tracking-normal" />;
}

function holdDuration(opened?: string | null, closed?: string | null): string {
  if (!opened || !closed) return "—";
  const ms = toDate(closed).getTime() - toDate(opened).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const m = Math.round(ms / 60_000);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// ── Statistics cards + daily P&L chart ───────────────────────────────────

export function StatisticsCards({ stats, endpointMissing }: { stats: ConvergenceStatistics | null; endpointMissing: boolean }) {
  if (!stats) {
    return (
      <Section title="Performance statistics" icon={<BarChart3 size={16} />}>
        {endpointMissing ? <PendingNote endpoint="statistics" /> : <div className="py-8 text-center text-sm text-text-muted">No closed trades yet.</div>}
      </Section>
    );
  }
  const pf = stats.profit_factor;
  const daily = stats.daily_pnl ?? [];
  return (
    <Section
      title="Performance statistics"
      icon={<BarChart3 size={16} />}
      description={stats.derived ? "Derived client-side from the closed-trade journal (statistics endpoint pending deployment)." : undefined}
      rightSlot={stats.derived ? <StatusBadge label="derived" variant="warn" /> : undefined}
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <MetricTile size="sm" label="Trades" value={String(stats.total_trades ?? "—")} detail={`${stats.wins ?? 0}W / ${stats.losses ?? 0}L`} />
        <MetricTile size="sm" label="Win rate" value={stats.win_rate != null ? `${formatNumber(stats.win_rate * 100, 1)}%` : "—"} color={stats.win_rate != null ? (stats.win_rate >= 0.5 ? "text-accent-green" : "text-accent-amber") : undefined} />
        <MetricTile size="sm" label="Profit factor" value={pf == null ? "—" : Number.isFinite(pf) ? formatNumber(pf, 2) : "∞"} color={pf != null && pf >= 1 ? "text-accent-green" : "text-accent-red"} detail="gross win / gross loss" />
        <MetricTile size="sm" label="Expectancy" value={formatSignedMoney(stats.expectancy)} detail="per trade" color={stats.expectancy != null ? (stats.expectancy >= 0 ? "text-accent-green" : "text-accent-red") : undefined} />
        <MetricTile size="sm" label="Avg win" value={formatMoney(stats.avg_win)} />
        <MetricTile size="sm" label="Avg loss" value={formatMoney(stats.avg_loss)} />
        <MetricTile size="sm" label="Max drawdown" value={stats.max_drawdown != null ? formatMoney(-stats.max_drawdown) : "—"} color="text-accent-red" detail="closed-equity curve" />
        <MetricTile size="sm" label="Net P&L" value={formatSignedMoney(stats.realized_pnl)} color={stats.realized_pnl != null ? (stats.realized_pnl >= 0 ? "text-accent-green" : "text-accent-red") : undefined} />
      </div>
      {daily.length ? (
        <div className="mt-4">
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.14em] text-text-muted">Daily realized P&L</div>
          <div className="h-[170px]">
            <ResponsiveContainer>
              <BarChart data={daily} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: "rgba(255,255,255,0.4)" }} stroke="rgba(255,255,255,0.12)" minTickGap={20} tickFormatter={(v: string) => v.slice(5)} />
                <YAxis width={64} tick={{ fontSize: 10, fill: "rgba(255,255,255,0.4)" }} stroke="rgba(255,255,255,0.12)" tickFormatter={(v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : String(v))} />
                <Tooltip
                  contentStyle={{ background: "#0d1117", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 8, fontSize: 11 }}
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  formatter={(value: any) => [formatSignedMoney(Number(value)), "P&L"]}
                />
                <Bar dataKey="pnl" isAnimationActive={false} radius={[3, 3, 0, 0]}>
                  {daily.map((row) => (
                    <Cell key={row.date} fill={row.pnl >= 0 ? "#00d4a3" : "#ff4757"} fillOpacity={0.85} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      ) : null}
    </Section>
  );
}

// ── Open positions panel with stop→target progress bars ─────────────────

function ProgressToTarget({ position }: { position: OpenPosition }) {
  const long = position.direction !== "SHORT";
  const target = position.target1_done && position.target2 != null ? position.target2 : position.target1;
  const span = target - position.stop;
  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  const frac = Math.abs(span) > 1e-9 ? clamp((position.current_price - position.stop) / span) : 0;
  const entryFrac = Math.abs(span) > 1e-9 ? clamp((position.entry_price - position.stop) / span) : 0;
  const inProfit = long ? position.current_price >= position.entry_price : position.current_price <= position.entry_price;
  return (
    <div className="min-w-[190px]">
      <div className="relative h-2.5 overflow-hidden rounded-full bg-bg-secondary/70">
        <div className={`absolute inset-y-0 left-0 rounded-full ${inProfit ? "bg-accent-green/70" : "bg-accent-red/70"}`} style={{ width: `${(frac * 100).toFixed(1)}%` }} />
        <div className="absolute inset-y-0 w-[2px] bg-white/70" style={{ left: `${(entryFrac * 100).toFixed(1)}%` }} title={`entry ${formatNumber(position.entry_price, 2)}`} />
      </div>
      <div className="mt-0.5 flex justify-between font-mono text-[9px] text-text-muted">
        <span title="stop">S {formatNumber(position.stop, 2)}</span>
        <span title={position.target1_done ? "target 2" : "target 1"}>{position.target1_done ? "T2" : "T1"} {formatNumber(target, 2)}</span>
      </div>
    </div>
  );
}

export function OpenPositionsPanel({ rows, asOf }: { rows: OpenPosition[]; asOf?: string | null }) {
  const totalUnrealized = rows.reduce((acc, p) => {
    const dir = p.direction === "SHORT" ? -1 : 1;
    return acc + dir * (p.current_price - p.entry_price) * p.lots * p.lot_size;
  }, 0);
  return (
    <Section
      title="Open paper positions"
      icon={<Target size={16} />}
      rightSlot={
        <div className="flex items-center gap-2">
          {rows.length ? <StatusBadge label={`unrealized ${formatSignedMoney(totalUnrealized)}`} variant={totalUnrealized >= 0 ? "success" : "error"} /> : null}
          <LastUpdated timestamp={asOf} label="scan" />
        </div>
      }
    >
      {!rows.length ? (
        <div className="py-8 text-center text-sm text-text-muted">No open positions.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] text-xs">
            <thead className="text-left text-text-muted">
              <tr>
                <th className="py-1">Symbol</th><th>Side</th><th>Entry</th><th>Mark</th><th>Lots</th>
                <th>Stop → target progress</th><th>Unrealized</th><th>Stage</th><th>Opened</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((p) => {
                const dir = p.direction === "SHORT" ? -1 : 1;
                const unrealized = dir * (p.current_price - p.entry_price) * p.lots * p.lot_size;
                return (
                  <tr key={p.position_id}>
                    <td className="py-2.5 font-semibold">{p.symbol}</td>
                    <td><StatusBadge label={p.direction} variant={p.direction === "SHORT" ? "error" : "success"} /></td>
                    <td className="font-mono">{formatNumber(p.entry_price, 2)}</td>
                    <td className="font-mono">{formatNumber(p.current_price, 2)}</td>
                    <td className="font-mono">{p.lots}/{p.initial_lots}</td>
                    <td className="pr-3"><ProgressToTarget position={p} /></td>
                    <td className={`font-mono font-semibold ${unrealized >= 0 ? "text-accent-green" : "text-accent-red"}`}>{formatSignedMoney(unrealized)}</td>
                    <td>{p.target1_done ? <StatusBadge label="T1 done · BE stop" variant="info" /> : <StatusBadge label="initial risk" variant="neutral" />}</td>
                    <td className="text-text-muted">{formatIST(p.opened_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ── Trade book (sortable) ────────────────────────────────────────────────

type SortKey = "closed_at" | "symbol" | "direction" | "entry_price" | "exit_price" | "realized_pnl" | "r" | "hold";

export function TradeBook({
  trades,
  endpointMissing,
  fallbackUsed,
  equityBase,
}: {
  trades: ConvergenceTrade[] | null;
  endpointMissing: boolean;
  /** true when rows came from the paper snapshot instead of /trades */
  fallbackUsed: boolean;
  equityBase?: number | null;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "closed_at", dir: -1 });
  const rows = useMemo(() => {
    const list = (trades ?? []).map((t) => {
      const { r, approx } = computeRMultiple(t, equityBase);
      return { ...t, _r: r, _rApprox: approx, _holdMs: t.opened_at && t.closed_at ? toDate(t.closed_at).getTime() - toDate(t.opened_at).getTime() : null };
    });
    const val = (row: (typeof list)[number]): string | number => {
      switch (sort.key) {
        case "symbol": return row.symbol ?? "";
        case "direction": return row.direction ?? "";
        case "entry_price": return row.entry_price ?? -Infinity;
        case "exit_price": return row.exit_price ?? -Infinity;
        case "realized_pnl": return row.realized_pnl ?? -Infinity;
        case "r": return row._r ?? -Infinity;
        case "hold": return row._holdMs ?? -Infinity;
        default: return String(row.closed_at ?? "");
      }
    };
    return list.sort((a, b) => {
      const av = val(a); const bv = val(b);
      const cmp = typeof av === "string" || typeof bv === "string" ? String(av).localeCompare(String(bv)) : (av as number) - (bv as number);
      return cmp * sort.dir;
    });
  }, [trades, sort, equityBase]);

  const header = (label: string, key: SortKey, align = "text-left") => {
    const active = sort.key === key;
    return (
      <th className={`${align} select-none`}>
        <button
          type="button"
          onClick={() => setSort((cur) => (cur.key === key ? { key, dir: cur.dir === 1 ? -1 : 1 } : { key, dir: -1 }))}
          className={`inline-flex items-center gap-1 py-1 ${active ? "text-text-primary" : "text-text-muted hover:text-text-secondary"}`}
        >
          {label}
          {active ? (sort.dir === 1 ? <ArrowUp size={11} /> : <ArrowDown size={11} />) : <ArrowUpDown size={11} className="opacity-40" />}
        </button>
      </th>
    );
  };

  return (
    <Section
      title="Trade book"
      icon={<ListChecks size={16} />}
      description="Closed paper trades with exit attribution and R-multiples. Click a column to sort."
      rightSlot={fallbackUsed ? <StatusBadge label="from paper snapshot" variant="warn" /> : undefined}
    >
      {trades == null && endpointMissing && !rows.length ? (
        <PendingNote endpoint="trades" />
      ) : !rows.length ? (
        <div className="py-8 text-center text-sm text-text-muted">No closed trades yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1050px] text-xs">
            <thead className="text-text-muted">
              <tr>
                {header("Symbol", "symbol")}
                {header("Side", "direction")}
                {header("Entry", "entry_price")}
                {header("Exit", "exit_price")}
                <th className="text-left py-1">Lots</th>
                {header("Opened / closed", "closed_at")}
                {header("Hold", "hold")}
                <th className="text-left py-1">Exit reason</th>
                {header("P&L", "realized_pnl")}
                {header("R", "r")}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((t, i) => (
                <tr key={t.position_id ?? `${t.symbol}-${i}`}>
                  <td className="py-2.5 font-semibold">{t.symbol}<div className="font-mono text-[9px] font-normal text-text-muted">{t.futures_contract ?? ""}</div></td>
                  <td><StatusBadge label={t.direction} variant={t.direction === "SHORT" ? "error" : "success"} /></td>
                  <td className="font-mono">{formatNumber(t.entry_price, 2)}</td>
                  <td className="font-mono">{formatNumber(t.exit_price, 2)}</td>
                  <td className="font-mono">{t.initial_lots ?? t.lots ?? "—"}{t.lot_size ? <span className="text-text-muted"> ×{t.lot_size}</span> : null}</td>
                  <td className="text-text-muted">{formatIST(t.opened_at)}<br />{formatIST(t.closed_at)}</td>
                  <td className="font-mono">{holdDuration(t.opened_at, t.closed_at)}</td>
                  <td><ExitReasonChip reason={t.exit_reason} /></td>
                  <td className={`font-mono font-semibold ${(t.realized_pnl ?? 0) >= 0 ? "text-accent-green" : "text-accent-red"}`}>{formatSignedMoney(t.realized_pnl)}</td>
                  <td className={`font-mono ${t._r == null ? "text-text-muted" : t._r >= 0 ? "text-accent-green" : "text-accent-red"}`} title={t._rApprox ? "approximated — initial stop unavailable, uses risk fraction × capital" : undefined}>
                    {t._r == null ? "—" : `${t._rApprox ? "≈" : ""}${formatNumber(t._r, 2)}R`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ── Order log timeline ───────────────────────────────────────────────────

const EVENT_COLOR: Record<string, string> = {
  entry: "bg-accent-green", buy: "bg-accent-green", long: "bg-accent-green",
  exit: "bg-accent-red", sell: "bg-accent-red", stop: "bg-accent-red", short: "bg-accent-red",
  partial: "bg-accent-amber", partial_exit: "bg-accent-amber", target1: "bg-accent-amber", trim: "bg-accent-amber",
  stop_move: "bg-accent-blue", break_even: "bg-accent-blue", modify: "bg-accent-blue",
};

export function OrderLogTimeline({
  orders,
  endpointMissing,
  derivedFromTrades,
  maxItems = 40,
}: {
  orders: ConvergenceOrder[] | null;
  endpointMissing: boolean;
  derivedFromTrades: boolean;
  maxItems?: number;
}) {
  const rows = (orders ?? []).slice(0, maxItems);
  return (
    <Section
      title="Order log"
      icon={<ScrollText size={16} />}
      description="Chronological execution events — entries, partials, stop moves and exits."
      rightSlot={derivedFromTrades ? <StatusBadge label="derived from trade book" variant="warn" /> : undefined}
    >
      {orders == null && endpointMissing ? (
        <PendingNote endpoint="orders" />
      ) : !rows.length ? (
        <div className="py-8 text-center text-sm text-text-muted">No order events yet.</div>
      ) : (
        <ol className="relative ml-2 space-y-0 border-l border-bg-border pl-4">
          {rows.map((o, i) => {
            const key = String(o.event ?? "order").toLowerCase();
            const dot = EVENT_COLOR[key] ?? (key.includes("exit") || key.includes("close") ? "bg-accent-red" : key.includes("entry") || key.includes("open") ? "bg-accent-green" : "bg-text-muted");
            return (
              <li key={`${o.position_id ?? o.symbol}-${o.time}-${i}`} className="relative py-2">
                <span className={`absolute -left-[21px] top-3.5 h-2.5 w-2.5 rounded-full ring-2 ring-bg-primary ${dot}`} aria-hidden />
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                  <span className="font-mono text-[10px] text-text-muted">{formatIST(o.time)}</span>
                  <span className="font-semibold">{o.symbol ?? "—"}</span>
                  <StatusBadge label={key.replace(/_/g, " ")} variant={dot === "bg-accent-green" ? "success" : dot === "bg-accent-red" ? "error" : dot === "bg-accent-amber" ? "warn" : dot === "bg-accent-blue" ? "info" : "neutral"} className="normal-case tracking-normal" />
                  {o.direction ? <span className="text-text-muted">{o.direction}</span> : null}
                  {o.price != null ? <span className="font-mono">@ {formatNumber(o.price, 2)}</span> : null}
                  {o.lots != null ? <span className="font-mono text-text-muted">{o.lots} lots</span> : null}
                  {o.note ? <span className="text-text-muted">· {o.note}</span> : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </Section>
  );
}
