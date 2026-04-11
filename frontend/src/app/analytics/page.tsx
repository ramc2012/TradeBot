"use client";

import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  CandlestickChart,
  Layers3,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { StrategyAgentStatus } from "@/components/trading/StrategyAgentMonitor";
import {
  getCalendarHeatmap,
  getCommodityStrategyStatus,
  getEquityCurve,
  getPerformance,
  getPortfolioGreeks,
  getStrategyAgentStatus,
  getStrategyEquityHistory,
  getStrategyPortfolio,
  getTrades,
} from "@/lib/api";

function fmtMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

function fmtSigned(value?: number | null, digits = 0, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function tone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function MetricCard({
  label,
  value,
  detail,
  color = "text-text-primary",
}: {
  label: string;
  value: string | number;
  detail?: string;
  color?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/30 p-4">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold", color)}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function StrategyCard({
  label,
  summary,
}: {
  label: string;
  summary: StrategyAgentStatus["strategies"][number]["summary"];
}) {
  return (
    <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text-primary">{label}</div>
          <div className="mt-1 text-[11px] text-text-muted">
            {summary.open_positions || 0} open · {summary.total_trades || 0} closed
          </div>
        </div>
        <div className={clsx("font-mono text-sm font-semibold", tone(summary.unrealized_pnl))}>
          {fmtSigned(summary.unrealized_pnl, 0)}
        </div>
      </div>
      <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2">
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Equity</div>
          <div className="mt-1 font-mono text-text-primary">{fmtMoney(summary.total_equity)}</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Realized</div>
          <div className={clsx("mt-1 font-mono", tone(summary.realized_pnl))}>{fmtSigned(summary.realized_pnl, 0)}</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Win Rate</div>
          <div className="mt-1 font-mono text-text-primary">
            {summary.win_rate != null ? `${((summary.win_rate || 0) * 100).toFixed(1)}%` : "--"}
          </div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2">
          <div className="text-text-muted">Entries / Exits</div>
          <div className="mt-1 font-mono text-text-primary">
            {summary.entries || 0} / {summary.exits || 0}
          </div>
        </div>
      </div>
    </div>
  );
}

type AutomatedPortfolioRow = {
  id: string;
  book: "NSE Options" | "Commodity";
  sleeve: string;
  underlying: string;
  contract: string;
  side: string;
  qty: number;
  entryTime?: string | null;
  entryPrice?: number | null;
  lastTime?: string | null;
  lastPrice?: number | null;
  pnl?: number | null;
  returnPct?: number | null;
  status: "open" | "closed";
  statusLabel: string;
  signalReason?: string | null;
};

type CommodityAnalyticsStatus = {
  summary?: {
    total_equity?: number | null;
    realized_pnl?: number | null;
    unrealized_pnl?: number | null;
    open_positions?: number | null;
  };
  last_run_at?: string | null;
  positions?: Array<{
    position_key: string;
    underlying: string;
    symbol: string;
    display_name?: string | null;
    strategy_key: string;
    qty: number;
    action: "BUY" | "SELL";
    entry_price: number;
    current_price: number;
    entered_at?: string | null;
    instrument_type?: string | null;
    expiry?: string | null;
    strike?: number | null;
    option_type?: "CE" | "PE" | null;
    signal_reason?: string | null;
    unrealized_pnl?: number | null;
    return_pct?: number | null;
  }>;
  trade_history?: Array<{
    symbol: string;
    action: string;
    qty: number;
    entry_price: number;
    exit_price: number;
    pnl: number;
    entry_time: string;
    exit_time: string;
    instrument_type?: string | null;
    expiry?: string | null;
    strike?: number | null;
    option_type?: "CE" | "PE" | null;
  }>;
  reports?: Array<{
    time: string;
    total_equity: number;
    realized_pnl: number;
    unrealized_pnl: number;
  }>;
};

function toEpoch(value?: string | null) {
  if (!value) return 0;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
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

function strategyContractLabel(optionType?: string | null, strike?: number | null, expiry?: string | null) {
  return `${optionType || "--"} ${strike != null ? String(strike) : "--"} · ${expiry || "--"}`;
}

function strategyUnderlyingFromSymbol(symbol?: string | null) {
  const parts = String(symbol || "").split(":");
  return parts.length > 1 ? parts[1] : String(symbol || "--");
}

function commodityContractLabel(
  symbol?: string | null,
  instrumentType?: string | null,
  optionType?: string | null,
  strike?: number | null,
  expiry?: string | null,
) {
  if (instrumentType === "OPTION") {
    return `${optionType || "--"} ${strike != null ? String(strike) : "--"} · ${expiry || "--"}`;
  }
  return symbol || "--";
}

function buildStrategyPortfolioRows(
  strategies: StrategyAgentStatus["strategies"],
  lastRunAt?: string | null,
): AutomatedPortfolioRow[] {
  const rows: AutomatedPortfolioRow[] = [];

  for (const strategy of strategies || []) {
    for (const position of strategy.positions || []) {
      rows.push({
        id: `${strategy.key}:open:${position.symbol}:${position.entered_at || position.price_updated_at || "na"}`,
        book: "NSE Options",
        sleeve: strategy.label,
        underlying: position.underlying,
        contract: strategyContractLabel(position.option_type, position.strike, position.expiry),
        side: `BUY ${position.option_type || ""}`.trim(),
        qty: position.qty,
        entryTime: position.entered_at,
        entryPrice: position.entry_price,
        lastTime: position.price_updated_at || lastRunAt || position.entered_at,
        lastPrice: position.current_price,
        pnl: position.unrealized_pnl,
        returnPct: position.return_pct,
        status: "open",
        statusLabel: position.phase ? `open · ${position.phase.replaceAll("_", " ")}` : "open",
        signalReason: position.signal_reason,
      });
    }

    for (const trade of strategy.trade_history || []) {
      const grossCost = (trade.entry_price || 0) * Math.max(trade.qty || 0, 1);
      rows.push({
        id: `${strategy.key}:closed:${trade.symbol}:${trade.exit_time || trade.entry_time || "na"}`,
        book: "NSE Options",
        sleeve: strategy.label,
        underlying: strategyUnderlyingFromSymbol(trade.symbol),
        contract: strategyContractLabel(trade.option_type, trade.strike, trade.expiry),
        side: trade.action || `BUY ${trade.option_type || ""}`.trim(),
        qty: trade.qty,
        entryTime: trade.entry_time,
        entryPrice: trade.entry_price,
        lastTime: trade.exit_time,
        lastPrice: trade.exit_price,
        pnl: trade.pnl,
        returnPct: grossCost > 0 ? (trade.pnl / grossCost) * 100 : null,
        status: "closed",
        statusLabel: "closed",
        signalReason: trade.option_type ? `${trade.option_type} exit` : trade.action,
      });
    }
  }

  return rows;
}

function buildCommodityPortfolioRows(status?: CommodityAnalyticsStatus | null): AutomatedPortfolioRow[] {
  const rows: AutomatedPortfolioRow[] = [];

  for (const position of status?.positions || []) {
    rows.push({
      id: `${position.position_key}:open`,
      book: "Commodity",
      sleeve: position.strategy_key === "commodity_options" ? "Strategy 1 · Options" : "Strategy 2 · Futures",
      underlying: position.underlying,
      contract: commodityContractLabel(
        position.display_name || position.symbol,
        position.instrument_type,
        position.option_type,
        position.strike,
        position.expiry,
      ),
      side: position.option_type ? `BUY ${position.option_type}` : position.action,
      qty: position.qty,
      entryTime: position.entered_at,
      entryPrice: position.entry_price,
      lastTime: status?.last_run_at || position.entered_at,
      lastPrice: position.current_price,
      pnl: position.unrealized_pnl,
      returnPct: position.return_pct,
      status: "open",
      statusLabel: "open",
      signalReason: position.signal_reason,
    });
  }

  for (const trade of status?.trade_history || []) {
    const grossCost = (trade.entry_price || 0) * Math.max(trade.qty || 0, 1);
    rows.push({
      id: `commodity:closed:${trade.symbol}:${trade.exit_time || trade.entry_time || "na"}`,
      book: "Commodity",
      sleeve: trade.instrument_type === "OPTION" ? "Strategy 1 · Options" : "Strategy 2 · Futures",
      underlying: String(trade.symbol || "--").split(" ")[0] || String(trade.symbol || "--"),
      contract: commodityContractLabel(
        trade.symbol,
        trade.instrument_type,
        trade.option_type,
        trade.strike,
        trade.expiry,
      ),
      side: trade.instrument_type === "OPTION" && trade.option_type ? `BUY ${trade.option_type}` : trade.action,
      qty: trade.qty,
      entryTime: trade.entry_time,
      entryPrice: trade.entry_price,
      lastTime: trade.exit_time,
      lastPrice: trade.exit_price,
      pnl: trade.pnl,
      returnPct: grossCost > 0 ? (trade.pnl / grossCost) * 100 : null,
      status: "closed",
      statusLabel: "closed",
      signalReason: trade.action,
    });
  }

  return rows;
}

export default function AnalyticsPage() {
  const { data: perf } = useQuery({
    queryKey: ["performance", "all"],
    queryFn: () => getPerformance("all").then((response) => response.data),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const { data: curve } = useQuery({
    queryKey: ["equityCurve"],
    queryFn: () => getEquityCurve().then((response) => response.data),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const { data: heatmap } = useQuery({
    queryKey: ["heatmap"],
    queryFn: () => getCalendarHeatmap().then((response) => response.data),
  });

  const { data: greeks } = useQuery({
    queryKey: ["portfolioGreeks"],
    queryFn: () => getPortfolioGreeks().then((response) => response.data),
    refetchInterval: 10_000,
    staleTime: 10_000,
  });

  const { data: trades } = useQuery({
    queryKey: ["trades"],
    queryFn: () => getTrades().then((response) => response.data),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const { data: strategyStatus } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((response) => response.data as StrategyAgentStatus),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const { data: strategyPortfolio } = useQuery({
    queryKey: ["strategyPortfolio"],
    queryFn: () => getStrategyPortfolio().then((response) => response.data as any),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const { data: strategyEquity } = useQuery({
    queryKey: ["strategyEquityHistory"],
    queryFn: () => getStrategyEquityHistory().then((response) => response.data as any[]),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const { data: commodityStatus } = useQuery({
    queryKey: ["commodityStrategyStatus"],
    queryFn: () => getCommodityStrategyStatus().then((response) => response.data as CommodityAnalyticsStatus),
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const strategyRows = strategyStatus?.strategies || [];
  const commoditySummary = commodityStatus?.summary;
  const strategyTradeCount = strategyRows.reduce((sum, strategy) => sum + (strategy.summary.total_trades || 0), 0);
  const strategyWins = strategyRows.reduce(
    (sum, strategy) => sum + Math.round((strategy.summary.win_rate || 0) * (strategy.summary.total_trades || 0)),
    0,
  );
  const strategyOpenPnl = strategyRows.reduce((sum, strategy) => sum + (strategy.summary.unrealized_pnl || 0), 0);
  const strategyRealized = strategyRows.reduce((sum, strategy) => sum + (strategy.summary.realized_pnl || 0), 0);
  const strategyEquityValue = strategyRows.reduce((sum, strategy) => sum + (strategy.summary.total_equity || 0), 0);
  const strategyWinRate = strategyTradeCount ? (strategyWins / strategyTradeCount) * 100 : 0;
  const strategyCurve = strategyPortfolio?.equity_curve || [];
  const strategyMonthly = strategyPortfolio?.monthly || [];
  const commodityCurve = commodityStatus?.reports || [];
  const manualCurve = curve || [];
  const tradeList = trades || [];
  const automatedPortfolioRows = [
    ...buildStrategyPortfolioRows(strategyRows, strategyStatus?.last_run_at),
    ...buildCommodityPortfolioRows(commodityStatus),
  ].sort((left, right) => {
    const rightTime = Math.max(toEpoch(right.lastTime), toEpoch(right.entryTime));
    const leftTime = Math.max(toEpoch(left.lastTime), toEpoch(left.entryTime));
    return rightTime - leftTime;
  });
  const automatedClosedTrades = automatedPortfolioRows.filter((row) => row.status === "closed").length;
  const automatedOpenPositions = automatedPortfolioRows.filter((row) => row.status === "open").length;
  const commodityTradeCount = commodityStatus?.trade_history?.length || 0;
  const commodityEquityValue = commoditySummary?.total_equity || 0;
  const commodityRealized = commoditySummary?.realized_pnl || 0;
  const commodityOpenPnl = commoditySummary?.unrealized_pnl || 0;
  const automatedEquityValue = strategyEquityValue + commodityEquityValue;
  const automatedRealized = strategyRealized + commodityRealized;
  const automatedOpenPnl = strategyOpenPnl + commodityOpenPnl;

  const strategyLineData = (() => {
    const rows = strategyEquity || [];
    const maxLen = Math.max(0, ...rows.map((row) => row.equity_curve?.length || 0));
    return Array.from({ length: maxLen }, (_, index) => {
      const point: Record<string, string | number | null> = { step: index + 1 };
      for (const row of rows) {
        point[row.label] = row.equity_curve?.[index]?.equity ?? null;
      }
      return point;
    });
  })();

  const heatmapRows = Object.entries((heatmap || {}) as Record<string, number>).slice(-10);

  return (
    <div className="mx-auto max-w-[1680px] space-y-6 pb-10">
      <section className="rounded-[28px] border border-bg-active/60 bg-bg-secondary/30 px-5 py-5">
        <div className="max-w-4xl">
          <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
            <BarChart3 size={18} className="text-accent-blue" />
            Analytics
          </div>
          <p className="mt-2 text-sm leading-6 text-text-secondary">
            Strategy runtime performance is now first-class here. The strategy desk stays separate from the manual book so live option-system results are visible without being diluted by the generic execution surface.
          </p>
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Layers3 size={16} className="text-accent-green" />
              Automated Strategy Books
            </div>
            <div className="mt-1 text-xs text-text-muted">
              Contract-level portfolio history from the live NSE and commodity strategy runtimes. Open marks and closed-trade P&amp;L now come from the same payloads that drive the strategy pages.
            </div>
          </div>
          <div className="text-xs text-text-muted">{automatedClosedTrades} closed trades</div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <MetricCard label="Live Equity" value={fmtMoney(automatedEquityValue)} color={tone(automatedOpenPnl + automatedRealized)} />
          <MetricCard label="Realized" value={fmtSigned(automatedRealized, 0)} color={tone(automatedRealized)} />
          <MetricCard label="Open P&L" value={fmtSigned(automatedOpenPnl, 0)} color={tone(automatedOpenPnl)} />
          <MetricCard label="Closed Trades" value={String(automatedClosedTrades)} />
          <MetricCard label="Open Positions" value={String(automatedOpenPositions)} />
        </div>

        <div className="mt-5 grid gap-4 xl:grid-cols-[1.05fr,0.95fr]">
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">NSE Strategy Equity</div>
                <div className="mt-1 text-xs text-text-muted">Strategy 1 and Strategy 2 options runtime.</div>
              </div>
              <div className="text-xs text-text-muted">{strategyTradeCount} closed</div>
            </div>
            {strategyCurve.length > 1 ? (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={strategyCurve}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                  <XAxis dataKey="trade" tick={{ fontSize: 9, fill: "#4a5568" }} />
                  <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                  <Tooltip
                    contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                    formatter={(value: number) => [`${(value / 1e5).toFixed(2)}L`, "Equity"]}
                  />
                  <ReferenceLine y={strategyPortfolio?.start_capital || 100000} stroke="#4a5568" strokeDasharray="3 3" />
                  <Line type="monotone" dataKey="equity" stroke="#00d4a3" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[220px] items-center justify-center text-sm text-text-muted">No live strategy trade history yet.</div>
            )}
          </div>

          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Commodity Equity</div>
                <div className="mt-1 text-xs text-text-muted">Strategy 2 futures and Strategy 1 options on the commodity desk.</div>
              </div>
              <div className="text-xs text-text-muted">{commodityTradeCount} closed</div>
            </div>
            {commodityCurve.length > 1 ? (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={commodityCurve}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                  <XAxis dataKey="time" tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: string) => formatTimestamp(value)} />
                  <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                  <Tooltip
                    contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                    formatter={(value: number) => [`${(value / 1e5).toFixed(2)}L`, "Equity"]}
                    labelFormatter={(value: string) => formatTimestamp(value)}
                  />
                  <ReferenceLine y={commodityCurve[0]?.total_equity || 0} stroke="#4a5568" strokeDasharray="3 3" />
                  <Line type="monotone" dataKey="total_equity" stroke="#f59e0b" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="realized_pnl" stroke="#3b82f6" strokeWidth={1.5} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[220px] items-center justify-center text-sm text-text-muted">Commodity equity history will appear after the first report points.</div>
            )}
          </div>
        </div>

        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">NSE Strategy Summary</div>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {strategyRows.map((strategy) => (
                <StrategyCard key={strategy.key} label={strategy.label} summary={strategy.summary} />
              ))}
            </div>
          </div>

          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Commodity Summary</div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <MetricCard label="Live Equity" value={fmtMoney(commodityEquityValue)} color={tone(commodityOpenPnl + commodityRealized)} />
              <MetricCard label="Open Positions" value={String(commoditySummary?.open_positions || 0)} />
              <MetricCard label="Realized" value={fmtSigned(commodityRealized, 0)} color={tone(commodityRealized)} />
              <MetricCard label="Open P&L" value={fmtSigned(commodityOpenPnl, 0)} color={tone(commodityOpenPnl)} />
            </div>
          </div>
        </div>

        <div className="mt-5 rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Automated Contract Portfolio Ledger</div>
              <div className="mt-1 text-xs text-text-muted">
                Every open mark and closed trade from NSE and commodity books, with entry/exit timestamps and incurred P&amp;L.
              </div>
            </div>
            <div className="text-xs text-text-muted">{automatedPortfolioRows.length} rows</div>
          </div>

          <div className="mt-4 max-h-[420px] overflow-auto">
            <table className="w-full min-w-[1440px] text-left text-xs font-mono">
              <thead className="sticky top-0 bg-bg-card">
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="py-2 pr-3">Book</th>
                  <th className="py-2 pr-3">Sleeve</th>
                  <th className="py-2 pr-3">Underlying</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Side</th>
                  <th className="py-2 pr-3">Qty</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit / Mark</th>
                  <th className="py-2 pr-3">Signal</th>
                  <th className="py-2">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {automatedPortfolioRows.length ? (
                  automatedPortfolioRows.map((row) => (
                    <tr key={row.id} className="border-b border-bg-border/40 align-top">
                      <td className="py-2 pr-3 text-text-secondary">{row.book}</td>
                      <td className="py-2 pr-3 text-text-primary">{row.sleeve}</td>
                      <td className="py-2 pr-3 text-text-primary">{row.underlying}</td>
                      <td className="py-2 pr-3 text-text-secondary">{row.contract}</td>
                      <td className="py-2 pr-3">
                        <span className={clsx("inline-flex rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]",
                          row.status === "open"
                            ? "border-accent-green/30 bg-accent-green/10 text-accent-green"
                            : "border-bg-active bg-bg-secondary/60 text-text-secondary",
                        )}>
                          {row.statusLabel}
                        </span>
                      </td>
                      <td className={clsx("py-2 pr-3 font-semibold", row.side.includes("BUY") ? "text-accent-green" : "text-accent-red")}>{row.side}</td>
                      <td className="py-2 pr-3 text-text-primary">{row.qty}</td>
                      <td className="py-2 pr-3 text-text-secondary">
                        <div>{row.entryPrice != null ? row.entryPrice.toFixed(2) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.entryTime)}</div>
                      </td>
                      <td className="py-2 pr-3 text-text-secondary">
                        <div>{row.lastPrice != null ? row.lastPrice.toFixed(2) : "--"}</div>
                        <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(row.lastTime)}</div>
                      </td>
                      <td className="py-2 pr-3 text-text-muted">{row.signalReason || "--"}</td>
                      <td className={clsx("py-2 font-semibold", tone(row.pnl))}>
                        {fmtSigned(row.pnl, 0)}
                        <div className="mt-1 text-[11px] text-text-muted">{fmtSigned(row.returnPct, 1, "%")}</div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={11} className="py-10 text-center text-sm text-text-muted">
                      No automated portfolio rows yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {strategyMonthly.length ? (
          <div className="mt-5 rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">NSE Monthly Strategy Change</div>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={strategyMonthly}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                <XAxis dataKey="month" tick={{ fill: "#4a5568", fontSize: 10 }} />
                <YAxis tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(value: number) => `${value > 0 ? "+" : ""}${value.toFixed(0)}%`} />
                <Tooltip
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: "6px" }}
                  formatter={(value: number) => [`${value > 0 ? "+" : ""}${value.toFixed(1)}%`, "Equity Δ"]}
                />
                <ReferenceLine y={0} stroke="#4a5568" />
                <Area type="monotone" dataKey="eq_change_pct" stroke="#3b82f6" fill="#3b82f633" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : null}

        {strategyLineData.length ? (
          <div className="mt-5 rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Per-Strategy Curves</div>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={strategyLineData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                <XAxis dataKey="step" tick={{ fontSize: 9, fill: "#4a5568" }} />
                <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                <Tooltip
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                  formatter={(value: number) => [`${(value / 1e5).toFixed(2)}L`, "Equity"]}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {strategyRows.map((strategy, index) => (
                  <Line
                    key={strategy.key}
                    type="monotone"
                    dataKey={strategy.label}
                    stroke={index === 0 ? "#00d4a3" : "#3b82f6"}
                    strokeWidth={2}
                    dot={false}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : null}
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <CandlestickChart size={16} className="text-accent-amber" />
              Manual Book
            </div>
            <div className="mt-1 text-xs text-text-muted">
              Existing generic trading analytics remain here as a separate book.
            </div>
          </div>
          <div className="text-xs text-text-muted">{tradeList.length} manual trades</div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <MetricCard label="Total P&L" value={perf ? fmtMoney(perf.total_pnl) : "--"} color={perf?.total_pnl >= 0 ? "text-accent-green" : "text-accent-red"} />
          <MetricCard label="Win Rate" value={perf ? `${(perf.win_rate * 100).toFixed(1)}%` : "--"} />
          <MetricCard label="Profit Factor" value={perf ? perf.profit_factor.toFixed(2) : "--"} />
          <MetricCard label="Sharpe" value={perf ? perf.sharpe_ratio.toFixed(2) : "--"} />
          <MetricCard label="Max DD" value={perf ? `${(perf.max_drawdown * 100).toFixed(2)}%` : "--"} color="text-accent-red" />
        </div>

        <div className="mt-5 grid gap-4 xl:grid-cols-[1.05fr,0.95fr]">
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Manual Equity Curve</div>
            {manualCurve.length > 0 ? (
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={manualCurve}>
                  <defs>
                    <linearGradient id="manual-equity" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#00d4a3" stopOpacity={0.22} />
                      <stop offset="95%" stopColor="#00d4a3" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                  <XAxis dataKey="timestamp" tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(value: string) => value.slice(0, 10)} />
                  <YAxis tick={{ fill: "#4a5568", fontSize: 10 }} tickFormatter={(value: number) => `₹${(value / 1000).toFixed(0)}k`} />
                  <Tooltip
                    contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", borderRadius: "6px" }}
                    labelStyle={{ color: "#94a3b8" }}
                    itemStyle={{ color: "#00d4a3" }}
                  />
                  <Area type="monotone" dataKey="equity" stroke="#00d4a3" fill="url(#manual-equity)" strokeWidth={2} dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[220px] items-center justify-center text-sm text-text-muted">No manual trade history yet.</div>
            )}
          </div>

          <div className="space-y-4">
            <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Portfolio Greeks</div>
              {greeks ? (
                <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <MetricCard label="Delta" value={greeks.delta.toFixed(4)} color="text-accent-blue" />
                  <MetricCard label="Gamma" value={greeks.gamma.toFixed(4)} color="text-accent-purple" />
                  <MetricCard label="Theta" value={greeks.theta.toFixed(4)} color="text-accent-red" />
                  <MetricCard label="Vega" value={greeks.vega.toFixed(4)} color="text-accent-amber" />
                </div>
              ) : (
                <div className="mt-4 text-sm text-text-muted">No option positions in the manual book.</div>
              )}
            </div>

            <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Daily P&amp;L Snapshot</div>
              <div className="mt-4 space-y-2">
                {heatmapRows.length ? (
                  heatmapRows.map(([day, pnl]) => (
                    <div key={day} className="flex items-center justify-between gap-3 rounded-xl border border-bg-border bg-bg-primary/30 px-3 py-2 text-xs">
                      <span className="text-text-muted">{day}</span>
                      <span className={clsx("font-mono font-semibold", tone(pnl))}>{fmtSigned(pnl, 0)}</span>
                    </div>
                  ))
                ) : (
                  <div className="text-sm text-text-muted">No daily P&amp;L rows yet.</div>
                )}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Activity size={16} className="text-accent-blue" />
              Manual Trade History
            </div>
            <div className="mt-1 text-xs text-text-muted">
              Manual execution history remains separate from the strategy desk trade book.
            </div>
          </div>
        </div>

        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[980px] text-left text-xs font-mono">
            <thead>
              <tr className="border-b border-bg-border text-text-muted">
                <th className="pb-2 pr-3">Symbol</th>
                <th className="pb-2 pr-3">Side</th>
                <th className="pb-2 pr-3">Qty</th>
                <th className="pb-2 pr-3">Entry</th>
                <th className="pb-2 pr-3">Exit</th>
                <th className="pb-2 pr-3">P&amp;L</th>
                <th className="pb-2">Exit Time</th>
              </tr>
            </thead>
            <tbody>
              {tradeList.length ? tradeList.map((trade: any, index: number) => (
                <tr key={`${trade.symbol}-${trade.exit_time}-${index}`} className="border-b border-bg-border/40">
                  <td className="py-2 pr-3 text-text-primary">{trade.symbol?.split(":")[1] || trade.symbol}</td>
                  <td className={clsx("py-2 pr-3", trade.action === "BUY" ? "text-accent-green" : "text-accent-red")}>{trade.action}</td>
                  <td className="py-2 pr-3 text-text-primary">{trade.qty}</td>
                  <td className="py-2 pr-3 text-text-primary">{trade.entry_price?.toFixed(2)}</td>
                  <td className="py-2 pr-3 text-text-primary">{trade.exit_price?.toFixed(2)}</td>
                  <td className={clsx("py-2 pr-3 font-semibold", (trade.pnl ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                    {(trade.pnl ?? 0) >= 0 ? "+" : ""}₹{Math.abs(trade.pnl ?? 0).toFixed(0)}
                  </td>
                  <td className="py-2 text-text-muted">{trade.exit_time?.slice(11, 16)}</td>
                </tr>
              )) : (
                <tr>
                  <td colSpan={7} className="py-10 text-center text-sm text-text-muted">No manual trades yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
