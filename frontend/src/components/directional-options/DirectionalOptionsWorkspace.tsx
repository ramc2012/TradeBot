"use client";

import { useDeferredValue, useState, useTransition } from "react";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  CandlestickChart,
  Gauge,
  Layers3,
  ShieldCheck,
  Target,
  TrendingUp,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";
import { API_URL, getDirectionalOptionsWorkspace } from "@/lib/api";

type CoverageRow = {
  underlying: string;
  spot_rows: number;
  contracts: number;
  weekly_contracts: number;
  monthly_contracts: number;
  option_rows: number;
  spot_end?: string | null;
  option_end?: string | null;
};

type DashboardStatus = {
  mounted: boolean;
  url?: string | null;
  reason: string;
};

type ModuleSummary = {
  key: string;
  label: string;
  description: string;
  underlyings: string[];
  timeframes: string[];
  dashboard: DashboardStatus;
  coverage: CoverageRow[];
};

type FeatureSnapshot = {
  timestamp: string;
  close: number;
  ema_spread_pct: number;
  adx: number;
  atr: number;
  rv_annualized: number;
  range_expansion: number;
};

type RegimeSnapshot = {
  label: string;
  trade_allowed: boolean;
  confidence: number;
  reasons: string[];
  preferred_expiry_kind: string;
  delta_target_min: number;
  delta_target_max: number;
};

type DirectionalSignal = {
  direction: string;
  confidence: number;
  expected_move: number;
  expected_horizon_bars: number;
  expected_horizon_hours: number;
  sleeve: string;
  thesis: string;
};

type ContractCandidate = {
  trading_symbol: string;
  option_type: string;
  expiry: string;
  expiry_kind: string;
  strike: number;
  option_price: number;
  days_to_expiry: number;
  delta: number;
  delta_bucket: string;
  liquidity_score: number;
  spread_pct: number;
  expected_pnl: number;
  contract_score: number;
  selection_reason: string;
  selected: boolean;
};

type RiskSnapshot = {
  approved: boolean;
  quantity_lots: number;
  quantity_units: number;
  premium_at_risk: number;
  max_loss: number;
  reasons: string[];
};

type BacktestSummary = {
  trade_count: number;
  total_pnl: number;
  ending_equity: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  profit_factor: number;
  win_rate_pct: number;
  expectancy: number;
  percent_profitable_months: number;
  engine_score: number;
};

type BacktestPayload = {
  summary: BacktestSummary;
  stability: {
    rolling_20_trade_expectancy: number;
    rolling_50_trade_profit_factor: number;
    walkforward_expectancy: number;
    walkforward_profit_factor: number;
  };
  equity_curve: Array<{ time: string; equity: number }>;
  monthly: Array<{ month: string; pnl: number }>;
  regime_breakdown: Array<{ regime: string; trades: number; expectancy: number; win_rate_pct: number }>;
  exit_breakdown: Array<{ exit_reason: string; trades: number; avg_pnl: number }>;
  recent_trades: Array<{
    trading_symbol: string;
    option_type: string;
    pnl: number;
    return_pct: number;
    exit_reason: string;
    regime: string;
    entry_time: string;
    exit_time: string;
  }>;
};

type WorkspaceResponse = {
  module: ModuleSummary;
  selection: {
    underlying: string;
    timeframe: string;
    lookback_sessions: number;
  };
  snapshot: {
    as_of?: string | null;
    spot_price?: number | null;
    feature_snapshot?: FeatureSnapshot | null;
    regime?: RegimeSnapshot | null;
    signal?: DirectionalSignal | null;
    selected_contract?: ContractCandidate | null;
    contract_candidates: ContractCandidate[];
    risk?: RiskSnapshot | null;
    selection_reason: string;
  };
  backtest: BacktestPayload;
};

function formatMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatSignedMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatPct(value?: number | null, digits = 1) {
  if (value == null || Number.isNaN(value)) return "--";
  return `${value.toFixed(digits)}%`;
}

function formatNumber(value?: number | null, digits = 1) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function tone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function badgeTone(value?: string | null) {
  if (value === "breakout" || value === "trend" || value === "CE" || value === "approved") {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (value === "risk_off" || value === "chop" || value === "rejected" || value === "PE") {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-active bg-bg-secondary/60 text-text-secondary";
}

function MetricTile({
  label,
  value,
  detail,
  color,
}: {
  label: string;
  value: string;
  detail: string;
  color?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-1.5 font-mono text-lg font-semibold text-text-primary", color)}>{value}</div>
      <div className="mt-1 text-[11px] text-text-muted">{detail}</div>
    </div>
  );
}

function StatusBadge({ label }: { label: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
        badgeTone(label),
      )}
    >
      {label}
    </span>
  );
}

export default function DirectionalOptionsWorkspace() {
  const [isPending, startTransition] = useTransition();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [timeframe, setTimeframe] = useState("5minute");
  const [lookbackSessions, setLookbackSessions] = useState("16");
  const deferredUnderlying = useDeferredValue(underlying);
  const deferredTimeframe = useDeferredValue(timeframe);
  const deferredLookbackSessions = useDeferredValue(lookbackSessions);

  const workspace = usePersistentSnapshotQuery<WorkspaceResponse>({
    storageKey: `nomad-curie.directional-options.${deferredUnderlying}.${deferredTimeframe}.${deferredLookbackSessions}`,
    queryKey: ["directional-options-workspace", deferredUnderlying, deferredTimeframe, deferredLookbackSessions],
    queryFn: async () => {
      const response = await getDirectionalOptionsWorkspace(
        deferredUnderlying,
        deferredTimeframe,
        Number(deferredLookbackSessions),
      );
      return response.data as WorkspaceResponse;
    },
    refetchInterval: 60_000,
    staleTime: 45_000,
  });

  const data = workspace.data;
  const summary = data?.backtest.summary;
  const snapshot = data?.snapshot;
  const module = data?.module;
  const dashboardUrl = module?.dashboard?.mounted && module.dashboard.url ? `${API_URL}${module.dashboard.url}` : null;
  const candidateBars = snapshot?.contract_candidates?.slice(0, 6) ?? [];

  return (
    <div className="space-y-5">
      <section className="rounded-[28px] border border-bg-border bg-bg-secondary/22 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-2xl">
            <div className="text-[11px] uppercase tracking-[0.2em] text-text-muted">New Module</div>
            <div className="mt-2 flex items-center gap-2">
              <div className="rounded-2xl border border-accent-blue/25 bg-accent-blue/10 p-2 text-accent-blue">
                <Target size={18} />
              </div>
              <div>
                <h1 className="text-2xl font-semibold text-text-primary">{module?.label || "Directional Long Options"}</h1>
                <p className="mt-1 text-sm text-text-muted">
                  {module?.description || "Directional long-premium desk for selective CE/PE entries."}
                </p>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <label className="rounded-2xl border border-bg-border bg-bg-primary/20 px-3 py-2 text-sm text-text-secondary">
              <span className="mr-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">Underlying</span>
              <select
                className="bg-transparent outline-none"
                value={underlying}
                onChange={(event) => startTransition(() => setUnderlying(event.target.value))}
              >
                {(module?.underlyings || ["NIFTY", "BANKNIFTY", "SENSEX"]).map((item) => (
                  <option key={item} value={item} className="bg-bg-card text-text-primary">
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="rounded-2xl border border-bg-border bg-bg-primary/20 px-3 py-2 text-sm text-text-secondary">
              <span className="mr-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">Timeframe</span>
              <select
                className="bg-transparent outline-none"
                value={timeframe}
                onChange={(event) => startTransition(() => setTimeframe(event.target.value))}
              >
                {(module?.timeframes || ["5minute", "15minute"]).map((item) => (
                  <option key={item} value={item} className="bg-bg-card text-text-primary">
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="rounded-2xl border border-bg-border bg-bg-primary/20 px-3 py-2 text-sm text-text-secondary">
              <span className="mr-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">Sessions</span>
              <select
                className="bg-transparent outline-none"
                value={lookbackSessions}
                onChange={(event) => startTransition(() => setLookbackSessions(event.target.value))}
              >
                {["8", "12", "16", "24", "32"].map((item) => (
                  <option key={item} value={item} className="bg-bg-card text-text-primary">
                    {item}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-text-muted">
          <span className="inline-flex items-center gap-2">
            <Activity size={14} />
            {workspace.isFetching || isPending ? "Refreshing engine view" : "Snapshot ready"}
          </span>
          <span>As of {formatTimestamp(snapshot?.as_of)}</span>
          <span>{workspace.isShowingSnapshot ? "Showing cached snapshot" : "Live API response"}</span>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricTile label="Engine Score" value={formatNumber(summary?.engine_score, 1)} detail="Composite walk-forward quality score." color={tone(summary?.engine_score)} />
        <MetricTile label="Expectancy" value={formatSignedMoney(summary?.expectancy)} detail="Average rupee PnL per simulated trade." color={tone(summary?.expectancy)} />
        <MetricTile label="Max DD" value={formatPct(summary?.max_drawdown_pct)} detail="Largest peak-to-trough drawdown." color="text-accent-red" />
        <MetricTile label="Win Rate" value={formatPct(summary?.win_rate_pct)} detail={`PF ${summary?.profit_factor?.toFixed(2) || "--"}`} color={tone((summary?.win_rate_pct || 0) - 50)} />
        <MetricTile label="Profitable Months" value={formatPct(summary?.percent_profitable_months)} detail={`${summary?.trade_count || 0} trades across ${deferredLookbackSessions} sessions.`} color={tone((summary?.percent_profitable_months || 0) - 50)} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[1.1fr,0.9fr]">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
                <Gauge size={16} />
                Current Setup
              </div>
              <div className="mt-1 text-xs text-text-muted">Regime, signal, contract selection, and sizing gate.</div>
            </div>
            <div className="flex gap-2">
              <StatusBadge label={snapshot?.regime?.label || "loading"} />
              <StatusBadge label={snapshot?.signal?.direction || "flat"} />
              <StatusBadge label={snapshot?.risk?.approved ? "approved" : "rejected"} />
            </div>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Underlying View</div>
              <div className="mt-3 space-y-2 text-sm text-text-secondary">
                <div className="flex items-center justify-between">
                  <span>Spot Price</span>
                  <span className="font-mono text-text-primary">{formatMoney(snapshot?.spot_price, 2)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>ADX</span>
                  <span className="font-mono text-text-primary">{snapshot?.feature_snapshot?.adx?.toFixed(1) || "--"}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>EMA Spread</span>
                  <span className="font-mono text-text-primary">{formatPct((snapshot?.feature_snapshot?.ema_spread_pct || 0) * 100, 2)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Expected Move</span>
                  <span className="font-mono text-text-primary">{snapshot?.signal?.expected_move?.toFixed(1) || "--"} pts</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Horizon</span>
                  <span className="font-mono text-text-primary">
                    {snapshot?.signal ? `${snapshot.signal.expected_horizon_bars} bars / ${snapshot.signal.expected_horizon_hours}h` : "--"}
                  </span>
                </div>
              </div>
              <div className="mt-4 rounded-2xl border border-bg-border bg-bg-secondary/35 p-3 text-sm text-text-secondary">
                {snapshot?.selection_reason || "Waiting for the engine snapshot."}
              </div>
            </div>

            <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Selected Contract</div>
              <div className="mt-3 space-y-2 text-sm text-text-secondary">
                <div className="font-semibold text-text-primary">
                  {snapshot?.selected_contract?.trading_symbol || "No contract cleared the hurdle"}
                </div>
                <div className="flex items-center justify-between">
                  <span>Expiry / Kind</span>
                  <span className="font-mono text-text-primary">
                    {snapshot?.selected_contract ? `${snapshot.selected_contract.expiry} · ${snapshot.selected_contract.expiry_kind}` : "--"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Price / Delta</span>
                  <span className="font-mono text-text-primary">
                    {snapshot?.selected_contract ? `${formatMoney(snapshot.selected_contract.option_price, 2)} · ${snapshot.selected_contract.delta.toFixed(2)}` : "--"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Expected PnL</span>
                  <span className={clsx("font-mono", tone(snapshot?.selected_contract?.expected_pnl))}>
                    {formatSignedMoney(snapshot?.selected_contract?.expected_pnl, 2)}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Premium at Risk</span>
                  <span className="font-mono text-text-primary">{formatMoney(snapshot?.risk?.premium_at_risk, 0)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Max Loss</span>
                  <span className="font-mono text-accent-red">{formatMoney(snapshot?.risk?.max_loss, 0)}</span>
                </div>
              </div>
              <div className="mt-4 rounded-2xl border border-bg-border bg-bg-secondary/35 p-3 text-sm text-text-secondary">
                {snapshot?.selected_contract?.selection_reason || (snapshot?.risk?.reasons?.[0] || "No approval or candidate yet.")}
              </div>
            </div>
          </div>

          <div className="mt-5">
            <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Layers3 size={16} />
              Top Contract Candidates
            </div>
            <div className="h-64 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={candidateBars}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.12)" />
                  <XAxis dataKey="strike" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip
                    formatter={(value: number, name: string) =>
                      name === "contract_score" ? value.toFixed(1) : `₹${value.toFixed(2)}`
                    }
                    labelFormatter={(label) => `Strike ${label}`}
                  />
                  <Bar dataKey="expected_pnl" name="expected_pnl" radius={[6, 6, 0, 0]}>
                    {candidateBars.map((entry) => (
                      <Cell
                        key={entry.trading_symbol}
                        fill={entry.selected ? "#22c55e" : entry.expected_pnl >= 0 ? "#38bdf8" : "#ef4444"}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        <div className="space-y-5">
          <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
                  <TrendingUp size={16} />
                  Backtest Equity
                </div>
                <div className="mt-1 text-xs text-text-muted">Bounded event-driven simulation over the selected lookback.</div>
              </div>
              <div className={clsx("font-mono text-sm font-semibold", tone(summary?.total_pnl))}>
                {formatSignedMoney(summary?.total_pnl)}
              </div>
            </div>
            <div className="mt-4 h-64 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={data?.backtest.equity_curve || []}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.12)" />
                  <XAxis dataKey="time" hide />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip formatter={(value: number) => formatMoney(value, 0)} labelFormatter={(value) => formatTimestamp(String(value))} />
                  <Line type="monotone" dataKey="equity" stroke="#22c55e" strokeWidth={2.4} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <MetricTile label="Walk-forward Expectancy" value={formatSignedMoney(data?.backtest.stability.walkforward_expectancy)} detail="Average expectancy across sequential windows." color={tone(data?.backtest.stability.walkforward_expectancy)} />
              <MetricTile label="Rolling PF" value={data?.backtest.stability.rolling_50_trade_profit_factor?.toFixed(2) || "--"} detail="Latest 50-trade profit factor proxy." color={tone((data?.backtest.stability.rolling_50_trade_profit_factor || 0) - 1)} />
            </div>
          </div>

          <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <CandlestickChart size={16} />
              Monthly and Regime Diagnostics
            </div>
            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <div className="h-56 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data?.backtest.monthly || []}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.12)" />
                    <XAxis dataKey="month" stroke="#94a3b8" />
                    <YAxis stroke="#94a3b8" />
                    <Tooltip formatter={(value: number) => formatMoney(value, 0)} />
                    <Bar dataKey="pnl" radius={[6, 6, 0, 0]}>
                      {(data?.backtest.monthly || []).map((item) => (
                        <Cell key={item.month} fill={item.pnl >= 0 ? "#22c55e" : "#ef4444"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="h-56 rounded-2xl border border-bg-border bg-bg-primary/12 p-3">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data?.backtest.regime_breakdown || []} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.12)" />
                    <XAxis type="number" stroke="#94a3b8" />
                    <YAxis type="category" dataKey="regime" stroke="#94a3b8" width={76} />
                    <Tooltip formatter={(value: number) => value.toFixed(1)} />
                    <Bar dataKey="expectancy" radius={[0, 6, 6, 0]}>
                      {(data?.backtest.regime_breakdown || []).map((item) => (
                        <Cell key={item.regime} fill={item.expectancy >= 0 ? "#38bdf8" : "#f97316"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-[1fr,0.92fr]">
        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <ShieldCheck size={16} />
            Recent Trades
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full divide-y divide-bg-border text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">
                  <th className="pb-2 pr-4">Contract</th>
                  <th className="pb-2 pr-4">P&amp;L</th>
                  <th className="pb-2 pr-4">Return</th>
                  <th className="pb-2 pr-4">Regime</th>
                  <th className="pb-2 pr-4">Exit</th>
                  <th className="pb-2">Closed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-border/70 text-text-secondary">
                {(data?.backtest.recent_trades || []).length > 0 ? (
                  (data?.backtest.recent_trades || []).map((trade) => (
                    <tr key={`${trade.trading_symbol}-${trade.exit_time}`}>
                      <td className="py-2 pr-4 font-medium text-text-primary">{trade.trading_symbol}</td>
                      <td className={clsx("py-2 pr-4 font-mono", tone(trade.pnl))}>{formatSignedMoney(trade.pnl, 2)}</td>
                      <td className={clsx("py-2 pr-4 font-mono", tone(trade.return_pct))}>{formatPct(trade.return_pct, 2)}</td>
                      <td className="py-2 pr-4">{trade.regime}</td>
                      <td className="py-2 pr-4">{trade.exit_reason}</td>
                      <td className="py-2">{formatTimestamp(trade.exit_time)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="py-6 text-center text-text-muted">
                      No simulated trades yet for the selected filter set.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
                <ArrowUpRight size={16} />
                Dash Hand-off
              </div>
              <div className="mt-1 text-xs text-text-muted">
                Optional embedded Dash board mounted from the backend when the dependency is installed.
              </div>
            </div>
            <StatusBadge label={module?.dashboard?.mounted ? "approved" : "rejected"} />
          </div>
          <div className="mt-4 rounded-2xl border border-bg-border bg-bg-primary/16 p-4 text-sm text-text-secondary">
            {module?.dashboard?.reason || "Dashboard mount state unavailable."}
          </div>
          {dashboardUrl ? (
            <div className="mt-4 space-y-3">
              <a
                href={dashboardUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-2xl border border-accent-blue/30 bg-accent-blue/10 px-4 py-2 text-sm font-semibold text-accent-blue transition-colors hover:border-accent-blue/45"
              >
                Open Dash Workspace
                <ArrowUpRight size={15} />
              </a>
              <div className="overflow-hidden rounded-2xl border border-bg-border bg-bg-primary/12">
                <iframe title="Directional options Dash" src={dashboardUrl} className="h-[480px] w-full bg-white" />
              </div>
            </div>
          ) : (
            <div className="mt-4 rounded-2xl border border-accent-amber/30 bg-accent-amber/10 p-4 text-sm text-accent-amber">
              <div className="flex items-start gap-3">
                <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                <div>
                  Dash is code-wired but not mounted in this environment yet. The backend will expose the embedded board automatically after the `dash` dependency is installed from the updated backend requirements.
                </div>
              </div>
            </div>
          )}
        </div>
      </section>

      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Layers3 size={16} />
          Dataset Coverage
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          {(module?.coverage || []).map((coverage) => (
            <div key={coverage.underlying} className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4">
              <div className="text-sm font-semibold text-text-primary">{coverage.underlying}</div>
              <div className="mt-3 space-y-2 text-sm text-text-secondary">
                <div className="flex items-center justify-between">
                  <span>Spot Rows</span>
                  <span className="font-mono">{coverage.spot_rows.toLocaleString("en-IN")}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Contracts</span>
                  <span className="font-mono">{coverage.contracts.toLocaleString("en-IN")}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Weekly / Monthly</span>
                  <span className="font-mono">{coverage.weekly_contracts} / {coverage.monthly_contracts}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Option Rows</span>
                  <span className="font-mono">{coverage.option_rows.toLocaleString("en-IN")}</span>
                </div>
                <div className="text-[11px] text-text-muted">
                  Spot {formatTimestamp(coverage.spot_end)} · Options {formatTimestamp(coverage.option_end)}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
