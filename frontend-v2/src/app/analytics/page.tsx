"use client";

import { useMemo } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  Boxes,
  Layers3,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
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
  type AppStrategyPortfolioSnapshot,
  type AppStrategyPositionRow,
  buildClosedTradeRows,
  buildOpenPositionRows,
  buildStrategyBookSummaries,
  fetchAppStrategyPortfolioSnapshot,
  toEpoch,
} from "@/lib/strategy-position-ledger";
import { getStrategyEquityHistory, getStrategyPortfolio } from "@/lib/api";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import { createPositionsOverviewSocket } from "@/lib/websocket";

type PositionsOverviewPayload = AppStrategyPortfolioSnapshot & {
  strategy?: AppStrategyPortfolioSnapshot["nse"];
};

function normalizePositionsOverview(payload: PositionsOverviewPayload): AppStrategyPortfolioSnapshot {
  return {
    nse: payload.nse ?? payload.strategy ?? null,
    commodity: payload.commodity ?? null,
    directional: payload.directional ?? null,
    gann: payload.gann ?? null,
    auction: payload.auction ?? null,
    fractal: payload.fractal ?? null,
    cbe: payload.cbe ?? null,
    macd: payload.macd ?? null,
    errors: payload.errors ?? {},
    fetchedAt: payload.fetchedAt ?? new Date().toISOString(),
  };
}

function fmtMoney(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

function fmtSigned(value?: number | null, digits = 0, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toLocaleString("en-IN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  })}${suffix}`;
}

function tone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
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

function shortReason(value?: string | null) {
  if (!value) return "--";
  return value.replaceAll("_", " ");
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
    <div className="rounded-lg border border-bg-border bg-bg-secondary/35 p-4">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold", color)}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function SectionTitle({
  icon,
  title,
  detail,
  right,
}: {
  icon: ReactNode;
  title: string;
  detail?: string;
  right?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          {icon}
          {title}
        </div>
        {detail ? <div className="mt-1 max-w-4xl text-xs leading-5 text-text-muted">{detail}</div> : null}
      </div>
      {right}
    </div>
  );
}

function OpenPositionsTable({ rows }: { rows: AppStrategyPositionRow[] }) {
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[1320px] text-left text-xs">
        <thead>
          <tr className="border-b border-bg-border text-text-muted">
            <th className="pb-2 pr-3">Desk</th>
            <th className="pb-2 pr-3">Strategy</th>
            <th className="pb-2 pr-3">Contract</th>
            <th className="pb-2 pr-3">Side</th>
            <th className="pb-2 pr-3">Qty</th>
            <th className="pb-2 pr-3">Entry</th>
            <th className="pb-2 pr-3">Mark</th>
            <th className="pb-2 pr-3">Open P&amp;L</th>
            <th className="pb-2 pr-3">Updated</th>
            <th className="pb-2">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((row) => (
            <tr key={row.id} className="border-b border-bg-border/40 align-top">
              <td className="py-3 pr-3">
                <div className="font-medium text-text-primary">{row.desk}</div>
                <div className="mt-1 text-[11px] text-text-muted">{row.venue} · {row.source}</div>
              </td>
              <td className="py-3 pr-3 text-text-secondary">{row.strategy}</td>
              <td className="py-3 pr-3">
                <div className="font-mono text-text-primary">{row.underlying} · {row.contract}</div>
                <div className="mt-1 text-[11px] text-text-muted">{row.symbol}</div>
              </td>
              <td className={clsx("py-3 pr-3 font-semibold", row.action.includes("SELL") ? "text-accent-red" : "text-accent-green")}>
                {row.action}
              </td>
              <td className="py-3 pr-3 font-mono text-text-secondary">
                <div>{row.qty}</div>
                {row.lots ? <div className="mt-1 text-[11px] text-text-muted">{row.lots} lot · {row.lotSize || "--"}</div> : null}
              </td>
              <td className="py-3 pr-3 font-mono text-text-primary">{row.entryPrice.toFixed(2)}</td>
              <td className="py-3 pr-3 font-mono text-text-primary">{row.currentPrice.toFixed(2)}</td>
              <td className={clsx("py-3 pr-3 font-mono font-semibold", tone(row.unrealizedPnl))}>
                {fmtSigned(row.unrealizedPnl, 0)}
                {row.returnPct != null ? <div className="mt-1 text-[11px] text-text-muted">{fmtSigned(row.returnPct, 1, "%")}</div> : null}
              </td>
              <td className="py-3 pr-3 text-[11px] text-text-muted">{formatTimestamp(row.updatedAt || row.enteredAt)}</td>
              <td className="max-w-[240px] py-3 text-[11px] text-text-secondary" title={shortReason(row.signalReason)}>
                {shortReason(row.signalReason)}
              </td>
            </tr>
          )) : (
            <tr>
              <td colSpan={10} className="py-10 text-center text-sm text-text-muted">
                No open strategy positions right now.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function AnalyticsPage() {
  const portfolioQuery = useLiveSnapshotQuery<AppStrategyPortfolioSnapshot>({
    queryKey: ["appStrategyPortfolioSnapshot"],
    queryFn: fetchAppStrategyPortfolioSnapshot,
    streamFactory: (onData, onStatusChange) =>
      createPositionsOverviewSocket((payload) => {
        onData(normalizePositionsOverview(payload as PositionsOverviewPayload));
      }, onStatusChange),
    streamWhenHidden: true,
    storageKey: "analytics:app-strategy-portfolio",
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

  const snapshot = portfolioQuery.data;
  const openRows = useMemo(() => buildOpenPositionRows(snapshot), [snapshot]);
  const closedRows = useMemo(() => buildClosedTradeRows(snapshot), [snapshot]);
  const bookSummaries = useMemo(() => buildStrategyBookSummaries(snapshot), [snapshot]);
  const commoditySummary = snapshot?.commodity?.summary;
  const commodityOpenRows = openRows.filter((row) => row.source === "commodity");
  const commodityClosedRows = closedRows
    .filter((row) => row.source === "commodity")
    .sort((left, right) => toEpoch(right.closedAt || right.updatedAt) - toEpoch(left.closedAt || left.updatedAt))
    .slice(0, 6);

  const openPnl = openRows.reduce((sum, row) => sum + (row.unrealizedPnl || 0), 0);
  const realizedPnl = bookSummaries.reduce((sum, row) => sum + (row.realizedPnl || 0), 0);
  const totalPnl = openPnl + realizedPnl;
  const closedCount = bookSummaries.reduce((sum, row) => sum + row.closedPositions, 0);
  const activeBooks = bookSummaries.filter((row) => row.openPositions > 0).length;
  const commodityInitial = Number(commoditySummary?.initial_capital || 0);
  const commodityRealized = Number(commoditySummary?.realized_pnl || 0);
  const commodityUnrealized = Number(commoditySummary?.unrealized_pnl || 0);
  const commodityEquity = Number(commoditySummary?.total_equity || 0);
  const commodityExpectedEquity = commodityInitial + commodityRealized + commodityUnrealized;
  const commodityVariance = commodityEquity - commodityExpectedEquity;
  const sourceErrors = Object.entries(snapshot?.errors || {});
  const strategyCurve = strategyPortfolio?.equity_curve || [];
  const commodityCurve = snapshot?.commodity?.reports || [];

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

  return (
    <div className="mx-auto max-w-[1680px] space-y-6 pb-10">
      <section className="rounded-lg border border-bg-active/60 bg-bg-secondary/30 px-5 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-4xl">
            <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
              <BarChart3 size={18} className="text-accent-blue" />
              Portfolio & Analytics
            </div>
            <p className="mt-2 max-w-4xl text-sm leading-6 text-text-secondary">
              Strategy-owned portfolio view with open positions first, followed by P&amp;L verification and strategy book summaries.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void portfolioQuery.refetch()}
            className="inline-flex items-center gap-2 rounded border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
          >
            <RefreshCw size={13} />
            Refresh
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
        <SectionTitle
          icon={<Layers3 size={16} className="text-accent-green" />}
          title="Open Positions"
          detail="Positions from every app strategy are shown before charts or history. Non-strategy execution rows are excluded from this portfolio design."
          right={<div className="text-xs text-text-muted">{openRows.length} open · {portfolioQuery.isFetching ? "refreshing" : "live snapshot"}</div>}
        />

        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <MetricCard label="Open P&L" value={fmtSigned(openPnl, 0)} color={tone(openPnl)} />
          <MetricCard label="Realized P&L" value={fmtSigned(realizedPnl, 0)} color={tone(realizedPnl)} />
          <MetricCard label="Total P&L" value={fmtSigned(totalPnl, 0)} color={tone(totalPnl)} />
          <MetricCard label="Open Positions" value={String(openRows.length)} detail={`${activeBooks} books active`} />
          <MetricCard label="Closed Trades" value={String(closedCount)} detail="strategy books only" />
        </div>

        {sourceErrors.length ? (
          <div className="mt-4 rounded-lg border border-accent-amber/30 bg-accent-amber/10 p-3 text-xs text-accent-amber">
            {sourceErrors.map(([key, value]) => `${key}: ${value}`).join(" · ")}
          </div>
        ) : null}

        <OpenPositionsTable rows={openRows} />
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.92fr,1.08fr]">
        <div className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
          <SectionTitle
            icon={<ShieldCheck size={16} className="text-accent-amber" />}
            title="Commodity P&L Verification"
            detail="Commodity equity is reconciled from capital, closed-trade realized P&L, and current open mark-to-market."
          />

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <MetricCard label="Initial Capital" value={fmtMoney(commodityInitial)} />
            <MetricCard label="Realized Closed" value={fmtSigned(commodityRealized, 0)} color={tone(commodityRealized)} />
            <MetricCard label="Open Mark" value={fmtSigned(commodityUnrealized, 0)} color={tone(commodityUnrealized)} />
            <MetricCard label="Reported Equity" value={fmtMoney(commodityEquity)} color={tone(commodityRealized + commodityUnrealized)} />
          </div>

          <div className="mt-4 rounded-lg border border-bg-border bg-bg-primary/30 p-4">
            <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Formula Check</div>
            <div className="mt-2 font-mono text-sm text-text-primary">
              {fmtMoney(commodityInitial)} {fmtSigned(commodityRealized, 0)} {fmtSigned(commodityUnrealized, 0)} = {fmtMoney(commodityExpectedEquity)}
            </div>
            <div className={clsx("mt-2 text-xs", Math.abs(commodityVariance) < 1 ? "text-accent-green" : "text-accent-amber")}>
              Difference vs reported equity: {fmtSigned(commodityVariance, 2)}
            </div>
            <div className="mt-3 text-xs leading-5 text-text-muted">
              Realized Closed is closed commodity trade P&amp;L. Open Mark is the sum of open positions using (current - entry) x qty with BUY/SELL direction. Day P&amp;L is the portfolio daily realized bucket and can differ from lifetime realized P&amp;L.
            </div>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <MetricCard label="Day P&L Bucket" value={fmtSigned(commoditySummary?.day_pnl, 0)} color={tone(commoditySummary?.day_pnl)} />
            <MetricCard label="Open Commodity Rows" value={String(commodityOpenRows.length)} />
          </div>
        </div>

        <div className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
          <SectionTitle
            icon={<TrendingUp size={16} className="text-accent-blue" />}
            title="Equity Curves"
            detail="NSE strategy equity and commodity report snapshots remain separated so a commodity swing does not hide option strategy performance."
          />
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">NSE Options</div>
              {strategyCurve.length > 1 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={strategyCurve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                    <XAxis dataKey="trade" tick={{ fontSize: 9, fill: "#4a5568" }} />
                    <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                    <Tooltip contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }} />
                    <ReferenceLine y={strategyPortfolio?.start_capital || 100000} stroke="#4a5568" strokeDasharray="3 3" />
                    <Line type="monotone" dataKey="equity" stroke="#00d4a3" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-[220px] items-center justify-center text-sm text-text-muted">No NSE strategy curve yet.</div>
              )}
            </div>

            <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-3">
              <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">Commodity</div>
              {commodityCurve.length > 1 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={commodityCurve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                    <XAxis dataKey="time" tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: string) => formatTimestamp(value)} />
                    <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                    <Tooltip
                      contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }}
                      labelFormatter={(value: string) => formatTimestamp(value)}
                    />
                    <Line type="monotone" dataKey="total_equity" stroke="#ffa502" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="realized_pnl" stroke="#3b82f6" strokeWidth={1.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-[220px] items-center justify-center text-sm text-text-muted">Commodity reports will appear after snapshots.</div>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
        <SectionTitle
          icon={<Boxes size={16} className="text-accent-amber" />}
          title="Strategy Books"
          detail="Each app strategy is summarized independently. Totals are not mixed with the old manual execution book."
        />
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {bookSummaries.map((book) => (
            <div key={book.key} className="rounded-lg border border-bg-border bg-bg-primary/25 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-text-primary">{book.label}</div>
                  <div className="mt-1 text-[11px] text-text-muted">{book.desk} · {book.venue}</div>
                </div>
                <div className={clsx("font-mono text-sm font-semibold", tone(book.totalPnl))}>{fmtSigned(book.totalPnl, 0)}</div>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
                <MetricCard label="Open" value={String(book.openPositions)} />
                <MetricCard label="Closed" value={String(book.closedPositions)} />
                <MetricCard label="Realized" value={fmtSigned(book.realizedPnl, 0)} color={tone(book.realizedPnl)} />
                <MetricCard label="Open P&L" value={fmtSigned(book.unrealizedPnl, 0)} color={tone(book.unrealizedPnl)} />
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr,0.85fr]">
        <div className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
          <SectionTitle
            icon={<Activity size={16} className="text-accent-blue" />}
            title="Recent Strategy Exits"
            detail="Closed strategy rows are retained for audit, but the page stays open-position first."
            right={<div className="text-xs text-text-muted">{closedRows.length} closed rows</div>}
          />
          <div className="mt-4 max-h-[360px] overflow-auto">
            <table className="w-full min-w-[960px] text-left text-xs">
              <thead className="sticky top-0 bg-bg-card">
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="py-2 pr-3">Desk</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit</th>
                  <th className="py-2 pr-3">P&L</th>
                  <th className="py-2">Closed</th>
                </tr>
              </thead>
              <tbody>
                {closedRows.slice(0, 40).map((row) => (
                  <tr key={row.id} className="border-b border-bg-border/40">
                    <td className="py-2 pr-3 text-text-secondary">{row.desk}</td>
                    <td className="py-2 pr-3 font-mono text-text-primary">{row.underlying} · {row.contract}</td>
                    <td className="py-2 pr-3 font-mono text-text-secondary">{row.entryPrice.toFixed(2)}</td>
                    <td className="py-2 pr-3 font-mono text-text-secondary">{row.currentPrice.toFixed(2)}</td>
                    <td className={clsx("py-2 pr-3 font-mono font-semibold", tone(row.realizedPnl))}>{fmtSigned(row.realizedPnl, 0)}</td>
                    <td className="py-2 text-text-muted">{formatTimestamp(row.closedAt || row.updatedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
          <SectionTitle
            icon={<ShieldCheck size={16} className="text-accent-green" />}
            title="Commodity Audit Rows"
            detail="Latest closed commodity rows used by the realized P&L total."
          />
          <div className="mt-4 space-y-2">
            {commodityClosedRows.length ? commodityClosedRows.map((row) => (
              <div key={row.id} className="rounded-lg border border-bg-border bg-bg-primary/25 px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-mono text-text-primary">{row.symbol}</span>
                  <span className={clsx("font-mono font-semibold", tone(row.realizedPnl))}>{fmtSigned(row.realizedPnl, 0)}</span>
                </div>
                <div className="mt-1 text-[11px] text-text-muted">
                  {row.qty} qty · {row.entryPrice.toFixed(2)} to {row.currentPrice.toFixed(2)} · {formatTimestamp(row.closedAt)}
                </div>
              </div>
            )) : (
              <div className="rounded-lg border border-bg-border bg-bg-primary/25 p-4 text-sm text-text-muted">
                No closed commodity rows in the current snapshot.
              </div>
            )}
          </div>
        </div>
      </section>

      {strategyLineData.length ? (
        <section className="rounded-lg border border-bg-border bg-bg-secondary/20 p-4">
          <SectionTitle
            icon={<TrendingUp size={16} className="text-accent-green" />}
            title="Per-Strategy Curves"
            detail="NSE Strategy 1 equity path, shown alongside the other live desks for cross-desk comparison."
          />
          <div className="mt-4">
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={strategyLineData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2d45" />
                <XAxis dataKey="step" tick={{ fontSize: 9, fill: "#4a5568" }} />
                <YAxis tick={{ fontSize: 9, fill: "#4a5568" }} tickFormatter={(value: number) => `${(value / 1e5).toFixed(0)}L`} />
                <Tooltip contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45", fontSize: 11 }} />
                {(snapshot?.nse?.strategies || []).map((strategy, index) => (
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
        </section>
      ) : null}
    </div>
  );
}
