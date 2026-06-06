"use client";

/**
 * Historical replay report for the Fractal desk.
 *
 * The /replay-report endpoint re-runs the strategy over stored history and
 * returns aggregate metrics (win rate, expectancy, profit factor, drawdown,
 * R:R), a per-metric gate status against acceptance thresholds, an equity
 * curve, a per-setup breakdown, and the full trade list. This panel renders
 * the metric gate grid, the equity curve, the setup breakdown, and the
 * trade book in the desk's design language.
 */
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, BarChart3, CheckCircle2, ListTree, XCircle } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedNumber,
  tone,
} from "@/components/desk-ui";
import { CHART } from "@/components/strategies/shared";

export type ReplayTrade = {
  trade_id?: string;
  underlying?: string;
  setup_name?: string;
  action?: string;
  horizon?: string;
  entry_time?: string;
  exit_time?: string;
  entry_underlying?: number | null;
  exit_underlying?: number | null;
  entry_premium?: number | null;
  exit_premium?: number | null;
  strike?: number | null;
  option_type?: string | null;
  pnl?: number | null;
  return_pct?: number | null;
  exit_reason?: string | null;
  confidence?: number | null;
  daily_shape?: string | null;
  hourly_shape?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

export type ReplayReport = {
  symbol?: string;
  generated_at?: string;
  metrics?: {
    trade_count?: number;
    win_rate?: number;
    expectancy?: number;
    profit_factor?: number;
    max_drawdown?: number;
    avg_risk_reward?: number;
    trades_per_week?: number;
    net_pnl?: number;
  } | null;
  thresholds?: Record<string, number> | null;
  gate_status?: Record<string, boolean> | null;
  equity_curve?: Array<{ time?: string; equity?: number }> | null;
  setup_breakdown?: Array<{ setup_name?: string; count?: number; pnl?: number; win_rate?: number }> | null;
  trades?: ReplayTrade[] | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

const AXIS = { stroke: CHART.axis, fontSize: 10, tickLine: false } as const;
const TH = "px-2.5 py-2 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold";
const TD = "px-2.5 py-2 text-[12px] font-mono whitespace-nowrap";

function setupLabel(name?: string): string {
  return String(name || "—").replace(/_/g, " ");
}

export function ReplayPanel({ report, loading, error }: { report?: ReplayReport; loading: boolean; error?: boolean }) {
  if (loading && !report) {
    return (
      <Section title="Replay report" icon={<Activity size={16} />}>
        <div className="py-10 text-center text-sm text-text-muted">Running replay over stored history…</div>
      </Section>
    );
  }
  if (error) {
    return (
      <Section title="Replay report" icon={<Activity size={16} />}>
        <div className="py-10 text-center text-sm text-accent-red">Replay report failed to load.</div>
      </Section>
    );
  }
  const m = report?.metrics || {};
  const gates = report?.gate_status || {};
  const thresholds = report?.thresholds || {};
  const equity = (report?.equity_curve || [])
    .map((p, i) => ({ i, equity: Number(p.equity ?? 0), time: p.time }))
    .filter((p) => Number.isFinite(p.equity));
  const breakdown = report?.setup_breakdown || [];
  const trades = report?.trades || [];

  const gatePass = Object.values(gates).filter(Boolean).length;
  const gateTotal = Object.keys(gates).length;
  const allPass = gateTotal > 0 && gatePass === gateTotal;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <MetricTile label="Trades" value={String(m.trade_count ?? 0)} detail={`${formatNumber(m.trades_per_week, 2)}/wk`} />
        <MetricTile label="Win rate" value={formatPct((m.win_rate ?? 0) / 100, 1)} color={tone((m.win_rate ?? 0) - 50)} />
        <MetricTile label="Net P&L" value={formatSignedNumber(m.net_pnl, 0)} color={tone(m.net_pnl)} />
        <MetricTile label="Expectancy" value={formatSignedNumber(m.expectancy, 1)} detail="per trade" color={tone(m.expectancy)} />
        <MetricTile label="Profit factor" value={formatNumber(m.profit_factor, 2)} color={tone((m.profit_factor ?? 0) - 1)} />
        <MetricTile label="Avg R:R" value={formatNumber(m.avg_risk_reward, 2)} color={tone((m.avg_risk_reward ?? 0) - 1)} />
        <MetricTile label="Max DD" value={formatPct((m.max_drawdown ?? 0) / 100, 1)} color={tone(m.max_drawdown)} />
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <Section
          title="Acceptance gate"
          icon={allPass ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
          rightSlot={<StatusBadge label={allPass ? "PASS" : `${gatePass}/${gateTotal}`} variant={allPass ? "success" : "warn"} />}
        >
          <div className="space-y-2">
            {Object.entries(gates).map(([k, pass]) => (
              <div key={k} className="flex items-center justify-between rounded-lg border border-bg-border bg-bg-primary/12 px-3 py-2">
                <div className="flex items-center gap-2">
                  {pass ? <CheckCircle2 size={14} className="text-accent-green" /> : <XCircle size={14} className="text-accent-red" />}
                  <span className="text-[12px] text-text-secondary">{k.replace(/_/g, " ")}</span>
                </div>
                <span className="font-mono text-[11px] text-text-muted">
                  min {formatNumber(thresholds[`${k}_min`] ?? thresholds[`${k}_max`] ?? thresholds[k], 1)}
                </span>
              </div>
            ))}
            {!gateTotal ? <div className="text-sm text-text-muted">No gate evaluation available.</div> : null}
          </div>
        </Section>

        <Section title="Equity curve" icon={<BarChart3 size={16} />} description="Cumulative replay equity over closed trades">
          {equity.length >= 2 ? (
            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equity} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
                  <defs>
                    <linearGradient id="replayEq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={CHART.green} stopOpacity={0.4} />
                      <stop offset="100%" stopColor={CHART.green} stopOpacity={0.04} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke={CHART.grid} vertical={false} />
                  <XAxis dataKey="i" {...AXIS} tickFormatter={(v) => `#${v + 1}`} minTickGap={24} />
                  <YAxis {...AXIS} width={52} tickFormatter={(v) => formatNumber(v, 0)} />
                  <ReferenceLine y={0} stroke={CHART.axis} strokeDasharray="3 3" />
                  <Tooltip
                    cursor={{ stroke: CHART.axis, strokeWidth: 1 }}
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload as (typeof equity)[number];
                      return (
                        <div className="rounded-lg border px-3 py-2 text-[11px]" style={{ background: CHART.surface, borderColor: CHART.border }}>
                          <div className="flex justify-between gap-4"><span className="text-text-muted">Trade</span><span className="font-mono">#{d.i + 1}</span></div>
                          <div className="flex justify-between gap-4"><span className="text-text-muted">Equity</span><span className="font-mono" style={{ color: CHART.green }}>{formatSignedNumber(d.equity, 1)}</span></div>
                        </div>
                      );
                    }}
                  />
                  <Area type="monotone" dataKey="equity" stroke={CHART.green} strokeWidth={2} fill="url(#replayEq)" isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center text-sm text-text-muted">Not enough replay trades to plot an equity curve.</div>
          )}
        </Section>
      </div>

      <Section title="Setup breakdown" icon={<ListTree size={16} />}>
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-bg-border/60">
                <th className={`${TH} text-left`}>Setup</th>
                <th className={`${TH} text-right`}>Trades</th>
                <th className={`${TH} text-right`}>Win rate</th>
                <th className={`${TH} text-right`}>P&L</th>
              </tr>
            </thead>
            <tbody>
              {breakdown.length ? (
                breakdown.map((b, i) => (
                  <tr key={i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                    <td className={`${TD} text-left text-text-primary`}>{setupLabel(b.setup_name)}</td>
                    <td className={`${TD} text-right text-text-secondary`}>{b.count ?? 0}</td>
                    <td className={`${TD} text-right text-text-secondary`}>{formatPct((b.win_rate ?? 0) / 100, 0)}</td>
                    <td className={`${TD} text-right ${tone(b.pnl)}`}>{formatSignedNumber(b.pnl, 0)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={4} className="px-2.5 py-6 text-center text-sm text-text-muted">No setup breakdown.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Replay trades" icon={<Activity size={16} />} rightSlot={<span className="text-[11px] text-text-muted">{trades.length} trades</span>}>
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-bg-border/60">
                <th className={`${TH} text-left`}>Setup</th>
                <th className={`${TH} text-left`}>Dir</th>
                <th className={`${TH} text-right`}>Entry → Exit</th>
                <th className={`${TH} text-right`}>Prem</th>
                <th className={`${TH} text-right`}>Ret %</th>
                <th className={`${TH} text-right`}>P&L</th>
                <th className={`${TH} text-left`}>Exit</th>
                <th className={`${TH} text-right`}>When</th>
              </tr>
            </thead>
            <tbody>
              {trades.length ? (
                trades.slice(0, 120).map((t, i) => (
                  <tr key={t.trade_id ?? i} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                    <td className={`${TD} text-left text-text-primary`}>{setupLabel(t.setup_name)}</td>
                    <td className="px-2.5 py-2 text-left">
                      <StatusBadge label={t.option_type || t.action || "—"} variant={t.option_type === "CE" || t.action === "LONG" ? "success" : t.option_type === "PE" || t.action === "SHORT" ? "error" : "neutral"} />
                    </td>
                    <td className={`${TD} text-right text-text-secondary`}>{formatNumber(t.entry_underlying, 0)} → {formatNumber(t.exit_underlying, 0)}</td>
                    <td className={`${TD} text-right text-text-secondary`}>{formatNumber(t.entry_premium, 1)} → {formatNumber(t.exit_premium, 1)}</td>
                    <td className={`${TD} text-right ${tone(t.return_pct)}`}>{formatSignedNumber(t.return_pct, 2)}</td>
                    <td className={`${TD} text-right ${tone(t.pnl)}`}>{formatSignedNumber(t.pnl, 0)}</td>
                    <td className={`${TD} text-left text-text-muted`}>{t.exit_reason ?? "—"}</td>
                    <td className={`${TD} text-right text-text-muted`}>{t.entry_time ? new Date(t.entry_time).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" }) : "—"}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={8} className="px-2.5 py-6 text-center text-sm text-text-muted">No replay trades.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
