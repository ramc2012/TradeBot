"use client";

import { memo, startTransition, useDeferredValue, useMemo, useState } from "react";
import { clsx } from "clsx";
import {
  Activity,
  Boxes,
  Filter,
  RefreshCw,
  Search,
  TrendingUp,
} from "lucide-react";

import { StreamStatus } from "@/components/live/StreamStatus";
import type { StrategyAgentStatus } from "@/components/trading/StrategyAgentMonitor";
import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import {
  getCommodityStrategyStatus,
  getPositions,
  getStrategyAgentStatus,
} from "@/lib/api";
import { createPositionsOverviewSocket } from "@/lib/websocket";

type PositionScope = "all" | "options" | "futures";

type CommodityStatus = {
  positions?: Array<{
    position_key: string;
    symbol: string;
    live_symbol: string;
    underlying: string;
    strategy_key: string;
    strategy_title: string;
    instrument_type: string;
    action: "BUY" | "SELL";
    qty: number;
    lots: number;
    lot_size: number;
    entry_price: number;
    current_price: number;
    unrealized_pnl?: number | null;
    return_pct?: number | null;
    expiry?: string | null;
    strike?: number | null;
    option_type?: "CE" | "PE" | null;
    entered_at: string;
  }>;
};

type ManualPosition = {
  symbol: string;
  action: "BUY" | "SELL";
  qty: number;
  avg_price: number;
  ltp: number;
  unrealized_pnl?: number | null;
  instrument_type?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
};

type GlobalPositionRow = {
  id: string;
  desk: string;
  strategy: string;
  source: string;
  venue: string;
  underlying: string;
  symbol: string;
  contract: string;
  instrumentGroup: "options" | "futures" | "other";
  action: string;
  qty: number;
  lots?: number | null;
  lotSize?: number | null;
  entryPrice: number;
  currentPrice: number;
  unrealizedPnl: number;
  returnPct?: number | null;
  updatedAt?: string | null;
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  if (Math.abs(value) >= 10_00_000) return `₹${(value / 10_00_000).toFixed(2)}L`;
  if (Math.abs(value) >= 1_000) return `₹${(value / 1_000).toFixed(1)}K`;
  return `₹${value.toFixed(0)}`;
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

function pnlTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function scopeLabel(scope: PositionScope) {
  if (scope === "options") return "Options";
  if (scope === "futures") return "Futures";
  return "All";
}

function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/35 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold text-text-primary", tone)}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function ScopeButton({
  active,
  scope,
  onClick,
}: {
  active: boolean;
  scope: PositionScope;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-full border px-3 py-1.5 text-xs font-semibold uppercase tracking-[0.14em] transition-colors",
        active
          ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
          : "border-bg-border bg-bg-secondary/25 text-text-secondary hover:border-bg-active hover:text-text-primary",
      )}
    >
      {scopeLabel(scope)}
    </button>
  );
}

const PositionsLedgerTable = memo(function PositionsLedgerTable({ rows }: { rows: GlobalPositionRow[] }) {
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[1520px] text-left text-xs">
        <thead>
          <tr className="border-b border-bg-border text-text-muted">
            <th className="pb-2 pr-3">Desk</th>
            <th className="pb-2 pr-3">Strategy</th>
            <th className="pb-2 pr-3">Venue</th>
            <th className="pb-2 pr-3">Underlying</th>
            <th className="pb-2 pr-3">Contract</th>
            <th className="pb-2 pr-3">Side</th>
            <th className="pb-2 pr-3">Qty / Lots</th>
            <th className="pb-2 pr-3">Entry</th>
            <th className="pb-2 pr-3">Last</th>
            <th className="pb-2 pr-3">Open P&amp;L</th>
            <th className="pb-2">Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((row) => (
              <tr key={row.id} className="border-b border-bg-border/40 align-top">
                <td className="py-3 pr-3">
                  <div className="font-medium text-text-primary">{row.desk}</div>
                  <div className="mt-1 text-[11px] text-text-muted">{row.source}</div>
                </td>
                <td className="py-3 pr-3 text-text-secondary">{row.strategy}</td>
                <td className="py-3 pr-3">
                  <span className={clsx(
                    "inline-flex rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
                    row.venue === "MCX"
                      ? "border-accent-amber/30 bg-accent-amber/10 text-accent-amber"
                      : "border-accent-blue/30 bg-accent-blue/10 text-accent-blue",
                  )}>
                    {row.venue}
                  </span>
                </td>
                <td className="py-3 pr-3 font-medium text-text-primary">{row.underlying}</td>
                <td className="py-3 pr-3">
                  <div className="font-mono text-text-primary">{row.contract}</div>
                  <div className="mt-1 text-[11px] text-text-muted">{row.symbol}</div>
                </td>
                <td className={clsx("py-3 pr-3 font-semibold", row.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                  {row.action}
                </td>
                <td className="py-3 pr-3 font-mono text-text-secondary">
                  <div>{row.qty}</div>
                  {row.lots ? <div className="mt-1 text-[11px] text-text-muted">{row.lots} lot · {row.lotSize || "--"} size</div> : null}
                </td>
                <td className="py-3 pr-3 font-mono text-text-primary">{formatNumber(row.entryPrice)}</td>
                <td className="py-3 pr-3 font-mono text-text-primary">{formatNumber(row.currentPrice)}</td>
                <td className={clsx("py-3 pr-3 font-mono font-semibold", pnlTone(row.unrealizedPnl))}>
                  {formatSigned(row.unrealizedPnl, 0)}
                  {row.returnPct != null ? <div className="mt-1 text-[11px] text-text-muted">{formatSigned(row.returnPct, 1, "%")}</div> : null}
                </td>
                <td className="py-3 text-text-muted">{formatTimestamp(row.updatedAt)}</td>
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={11} className="py-10 text-center text-sm text-text-muted">
                No positions match the current filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
});

export default function PositionsPage() {
  const [scope, setScope] = useState<PositionScope>("all");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim().toLowerCase());

  const positionsQuery = useLiveSnapshotQuery<{
    manual: ManualPosition[];
    strategy: StrategyAgentStatus;
    commodity: CommodityStatus;
  }>({
    queryKey: ["globalPositionsSnapshot"],
    queryFn: async () => {
      const [manual, strategy, commodity] = await Promise.all([
        getPositions().then((response) => response.data as ManualPosition[]),
        getStrategyAgentStatus().then((response) => response.data as StrategyAgentStatus),
        getCommodityStrategyStatus().then((response) => response.data as CommodityStatus),
      ]);
      return { manual, strategy, commodity };
    },
    streamFactory: (onData, onStatusChange) =>
      createPositionsOverviewSocket(
        (data) =>
          onData(data as {
            manual: ManualPosition[];
            strategy: StrategyAgentStatus;
            commodity: CommodityStatus;
          }),
        onStatusChange,
      ),
    storageKey: "globalPositionsSnapshot",
    staleTime: 5_000,
  });
  const data = positionsQuery.data;
  const isLoading = positionsQuery.isLoading;

  const rows = useMemo<GlobalPositionRow[]>(() => {
    const manualRows: GlobalPositionRow[] = (data?.manual || []).map((position) => {
      const symbolToken = position.symbol?.split(":")[1] || position.symbol;
      const underlying = symbolToken?.split("-")[0] || symbolToken || "Manual";
      const isOption = (position.option_type || position.instrument_type) ? ["CE", "PE", "OPT"].includes(String(position.option_type || position.instrument_type).toUpperCase()) : false;
      const isFuture = !isOption && String(position.instrument_type || "").toUpperCase() === "FUT";
      const instrumentGroup: GlobalPositionRow["instrumentGroup"] = isOption ? "options" : isFuture ? "futures" : "other";
      return {
        id: `manual-${position.symbol}`,
        desk: "Manual Book",
        strategy: "Execution",
        source: "manual",
        venue: "NSE",
        underlying,
        symbol: position.symbol,
        contract: isOption
          ? `${position.option_type || position.instrument_type} ${position.strike ?? "--"} · ${position.expiry || "--"}`
          : position.symbol,
        instrumentGroup,
        action: position.action,
        qty: position.qty,
        lots: null,
        lotSize: null,
        entryPrice: position.avg_price,
        currentPrice: position.ltp,
        unrealizedPnl: position.unrealized_pnl || 0,
        updatedAt: null,
      };
    });

    const strategyRows: GlobalPositionRow[] = (data?.strategy?.strategies || []).flatMap((strategy) =>
      (strategy.positions || []).map((position) => ({
        id: `strategy-${strategy.key}-${position.symbol}-${position.entered_at}`,
        desk: "Strategy Desk",
        strategy: strategy.label,
        source: strategy.key,
        venue: "NSE",
        underlying: position.underlying,
        symbol: position.symbol,
        contract: `${position.option_type} ${position.strike} · ${position.expiry || "--"}`,
        instrumentGroup: "options" as const,
        action: "BUY",
        qty: position.qty,
        lots: null,
        lotSize: null,
        entryPrice: position.entry_price,
        currentPrice: position.current_price,
        unrealizedPnl: position.unrealized_pnl || 0,
        returnPct: position.return_pct,
        updatedAt: position.price_updated_at || position.entered_at,
      })),
    );

    const commodityRows: GlobalPositionRow[] = (data?.commodity?.positions || []).map((position) => {
      const isOption = position.instrument_type === "OPT" || Boolean(position.option_type);
      const instrumentGroup: GlobalPositionRow["instrumentGroup"] = isOption ? "options" : "futures";
      return {
        id: `commodity-${position.position_key}`,
        desk: "Commodity Desk",
        strategy: position.strategy_title,
        source: position.strategy_key,
        venue: "MCX",
        underlying: position.underlying,
        symbol: position.live_symbol || position.symbol,
        contract: isOption
          ? `${position.option_type || "--"} ${position.strike ?? "--"} · ${position.expiry || "--"}`
          : position.live_symbol || position.symbol,
        instrumentGroup,
        action: position.action,
        qty: position.qty,
        lots: position.lots,
        lotSize: position.lot_size,
        entryPrice: position.entry_price,
        currentPrice: position.current_price,
        unrealizedPnl: position.unrealized_pnl || 0,
        returnPct: position.return_pct,
        updatedAt: position.entered_at,
      };
    });

    return [...strategyRows, ...commodityRows, ...manualRows].sort(
      (left, right) => Math.abs(right.unrealizedPnl || 0) - Math.abs(left.unrealizedPnl || 0),
    );
  }, [data]);

  const { filteredRows, totalOpenPnl, optionsCount, futuresCount, desksActive, grossNotional } = useMemo(() => {
    const nextFilteredRows = rows.filter((row) => {
      if (scope !== "all" && row.instrumentGroup !== scope) {
        return false;
      }
      if (!deferredSearch) {
        return true;
      }
      const haystack = `${row.desk} ${row.strategy} ${row.underlying} ${row.symbol} ${row.contract}`.toLowerCase();
      return haystack.includes(deferredSearch);
    });

    let nextOpenPnl = 0;
    let nextOptionsCount = 0;
    let nextFuturesCount = 0;
    let nextGrossNotional = 0;
    const activeDesks = new Set<string>();

    for (const row of nextFilteredRows) {
      nextOpenPnl += row.unrealizedPnl || 0;
      nextGrossNotional += (row.currentPrice || 0) * (row.qty || 0);
      if (row.instrumentGroup === "options") nextOptionsCount += 1;
      if (row.instrumentGroup === "futures") nextFuturesCount += 1;
      activeDesks.add(row.desk);
    }

    return {
      filteredRows: nextFilteredRows,
      totalOpenPnl: nextOpenPnl,
      optionsCount: nextOptionsCount,
      futuresCount: nextFuturesCount,
      desksActive: activeDesks.size,
      grossNotional: nextGrossNotional,
    };
  }, [deferredSearch, rows, scope]);

  return (
    <div className="mx-auto max-w-[1760px] space-y-6 pb-10">
      <section className="rounded-[28px] border border-bg-active/60 bg-bg-secondary/30 px-5 py-5">
        <div className="max-w-4xl">
          <div className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
            <TrendingUp size={18} className="text-accent-blue" />
            Global Positions
          </div>
          <p className="mt-2 text-sm leading-6 text-text-secondary">
            One live book for manual trading, the NSE strategy desk, and the commodity desk. This page normalizes option and futures exposure so the portfolio can be tracked without switching surfaces.
          </p>
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-sm font-semibold text-text-primary">Live Book</div>
            <div className="mt-1 text-xs text-text-muted">
              The second menu slot now leads directly to the normalized positions ledger.
            </div>
            <StreamStatus
              className="mt-3"
              title="Positions"
              isStreamConnected={positionsQuery.isStreamConnected}
              isShowingSnapshot={positionsQuery.isShowingSnapshot}
              snapshotSavedAt={positionsQuery.snapshotSavedAt}
              liveText="manual, NSE strategy, and commodity books are streaming"
              bootstrapText="loading the combined ledger before the live socket takes over"
            />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void positionsQuery.refetch()}
              className="inline-flex items-center gap-2 rounded-full border border-bg-border bg-bg-secondary/25 px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
            >
              <RefreshCw size={13} />
              Refresh
            </button>
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search desk, strategy, or symbol"
                className="terminal-input min-w-[260px] pl-9 text-sm"
              />
            </div>
            <div className="flex items-center gap-2 rounded-full border border-bg-border bg-bg-secondary/25 px-2 py-2">
              <Filter size={13} className="text-text-muted" />
              {(["all", "options", "futures"] as PositionScope[]).map((item) => (
                <ScopeButton
                  key={item}
                  active={scope === item}
                  scope={item}
                  onClick={() => startTransition(() => setScope(item))}
                />
              ))}
            </div>
          </div>
        </div>

        <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <MetricTile label="Open Positions" value={String(filteredRows.length)} detail={isLoading ? "Refreshing…" : `${rows.length} total rows`} />
          <MetricTile label="Open P&L" value={formatSigned(totalOpenPnl, 0)} tone={pnlTone(totalOpenPnl)} />
          <MetricTile label="Gross Notional" value={formatCompact(grossNotional)} />
          <MetricTile label="Options / Futures" value={`${optionsCount} / ${futuresCount}`} />
          <MetricTile label="Active Desks" value={String(desksActive)} detail="Manual, strategy, commodity" />
        </div>
      </section>

      <section className="rounded-[24px] border border-bg-border bg-bg-secondary/20 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-text-primary">Combined Positions Table</div>
            <div className="mt-1 text-xs text-text-muted">
              Options and futures are normalized into one ledger with desk and strategy labels.
            </div>
          </div>
          <div className="text-xs text-text-muted">{filteredRows.length} rows</div>
        </div>

        <PositionsLedgerTable rows={filteredRows} />

        <div className="mt-5 grid gap-4 xl:grid-cols-3">
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <TrendingUp size={14} className="text-accent-blue" />
              Strategy Desk
            </div>
            <div className="mt-3 text-xs text-text-secondary">
              {(data?.strategy?.strategies || []).reduce((sum, strategy) => sum + (strategy.summary.open_positions || 0), 0)} open option positions across Strategy 1 and Strategy 2.
            </div>
          </div>
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Boxes size={14} className="text-accent-amber" />
              Commodity Desk
            </div>
            <div className="mt-3 text-xs text-text-secondary">
              {(data?.commodity?.positions || []).length} open commodity futures and options positions.
            </div>
          </div>
          <div className="rounded-[22px] border border-bg-border bg-bg-secondary/20 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              <Activity size={14} className="text-accent-green" />
              Manual Book
            </div>
            <div className="mt-3 text-xs text-text-secondary">
              {(data?.manual || []).length} open manual execution positions from the primary trading surface.
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
