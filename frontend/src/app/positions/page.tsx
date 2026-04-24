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
    stop_price?: number | null;
    target_price?: number | null;
    target_reached?: boolean | null;
    peak_price?: number | null;
    signal_reason?: string | null;
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
  expiry?: string | null;
  dte?: number | null;
  phase?: string | null;
  trailingStop?: number | null;
  stopPrice?: number | null;
  targetPrice?: number | null;
  targetReached?: boolean | null;
  peakPrice?: number | null;
  entryIvPct?: number | null;
  signalReason?: string | null;
  enteredAt?: string | null;
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

function computeDTE(expiry?: string | null): number | null {
  if (!expiry) return null;
  const parsed = new Date(expiry);
  if (Number.isNaN(parsed.getTime())) return null;
  const diffMs = parsed.getTime() - Date.now();
  return Math.ceil(diffMs / 86_400_000);
}

function formatHeldFor(enteredAt?: string | null): string {
  if (!enteredAt) return "--";
  const parsed = new Date(enteredAt);
  if (Number.isNaN(parsed.getTime())) return "--";
  const diffMs = Date.now() - parsed.getTime();
  if (diffMs < 0) return "--";
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  if (hours < 24) return remMin ? `${hours}h ${remMin}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days}d ${remHours}h` : `${days}d`;
}

function phaseBadge(phase?: string | null): { label: string; tone: string } | null {
  if (!phase) return null;
  const normalized = phase.toLowerCase();
  if (normalized === "phase1") return { label: "P1", tone: "border-accent-blue/30 bg-accent-blue/10 text-accent-blue" };
  if (normalized === "phase2") return { label: "P2", tone: "border-accent-amber/30 bg-accent-amber/10 text-accent-amber" };
  if (normalized === "trailing") return { label: "Trail", tone: "border-accent-green/30 bg-accent-green/10 text-accent-green" };
  if (normalized === "exited") return { label: "Exit", tone: "border-bg-border bg-bg-secondary/30 text-text-muted" };
  return { label: phase.slice(0, 6), tone: "border-bg-border bg-bg-secondary/30 text-text-secondary" };
}

function prettifyReason(reason?: string | null): string {
  if (!reason) return "";
  return reason.replaceAll("_", " ");
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
      <table className="w-full min-w-[1760px] text-left text-xs">
        <thead>
          <tr className="border-b border-bg-border text-text-muted">
            <th className="pb-2 pr-3">Desk</th>
            <th className="pb-2 pr-3">Strategy</th>
            <th className="pb-2 pr-3">Venue</th>
            <th className="pb-2 pr-3">Underlying</th>
            <th className="pb-2 pr-3">Contract</th>
            <th className="pb-2 pr-3">Phase</th>
            <th className="pb-2 pr-3">Side</th>
            <th className="pb-2 pr-3">Qty / Lots</th>
            <th className="pb-2 pr-3">Entry</th>
            <th className="pb-2 pr-3">Last</th>
            <th className="pb-2 pr-3">Risk</th>
            <th className="pb-2 pr-3">Open P&amp;L</th>
            <th className="pb-2 pr-3">Reason</th>
            <th className="pb-2">Age</th>
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((row) => {
              const badge = phaseBadge(row.phase);
              const dteTone =
                row.dte == null
                  ? "text-text-muted"
                  : row.dte <= 2
                    ? "text-accent-red"
                    : row.dte <= 7
                      ? "text-accent-amber"
                      : "text-text-muted";
              const heldFor = formatHeldFor(row.enteredAt || row.updatedAt);
              const reason = prettifyReason(row.signalReason);
              const hasRisk =
                row.trailingStop != null ||
                row.stopPrice != null ||
                row.targetPrice != null ||
                row.entryIvPct != null;
              return (
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
                    <div className="mt-1 flex items-center gap-2 text-[11px] text-text-muted">
                      <span>{row.symbol}</span>
                      {row.dte != null ? (
                        <span className={clsx("font-mono font-semibold", dteTone)}>
                          · {row.dte}d
                        </span>
                      ) : null}
                    </div>
                  </td>
                  <td className="py-3 pr-3">
                    {badge ? (
                      <span className={clsx(
                        "inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em]",
                        badge.tone,
                      )}>
                        {badge.label}
                      </span>
                    ) : (
                      <span className="text-[11px] text-text-muted">--</span>
                    )}
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
                  <td className="py-3 pr-3 font-mono text-[11px] leading-5">
                    {hasRisk ? (
                      <div className="space-y-0.5">
                        {row.trailingStop != null ? (
                          <div>
                            <span className="text-text-muted">Trail </span>
                            <span className="text-accent-red">{formatNumber(row.trailingStop)}</span>
                          </div>
                        ) : null}
                        {row.stopPrice != null ? (
                          <div>
                            <span className="text-text-muted">Stop </span>
                            <span className="text-accent-red">{formatNumber(row.stopPrice)}</span>
                          </div>
                        ) : null}
                        {row.targetPrice != null ? (
                          <div>
                            <span className="text-text-muted">Tgt </span>
                            <span className={clsx(row.targetReached ? "text-accent-green" : "text-accent-blue")}>
                              {formatNumber(row.targetPrice)}
                              {row.targetReached ? " ✓" : ""}
                            </span>
                          </div>
                        ) : null}
                        {row.entryIvPct != null ? (
                          <div>
                            <span className="text-text-muted">IV </span>
                            <span className="text-text-secondary">{formatNumber(row.entryIvPct, 1)}%</span>
                          </div>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-text-muted">--</span>
                    )}
                  </td>
                  <td className={clsx("py-3 pr-3 font-mono font-semibold", pnlTone(row.unrealizedPnl))}>
                    {formatSigned(row.unrealizedPnl, 0)}
                    {row.returnPct != null ? <div className="mt-1 text-[11px] text-text-muted">{formatSigned(row.returnPct, 1, "%")}</div> : null}
                    {row.peakPrice != null && row.currentPrice != null && row.peakPrice > 0 ? (
                      <div
                        className="mt-0.5 text-[10px] font-normal text-text-muted"
                        title={`Peak ${formatNumber(row.peakPrice)}`}
                      >
                        pk {formatNumber(row.peakPrice)}
                      </div>
                    ) : null}
                  </td>
                  <td className="py-3 pr-3 text-[11px] text-text-secondary max-w-[180px]">
                    {reason ? (
                      <span title={reason}>{reason}</span>
                    ) : (
                      <span className="text-text-muted">--</span>
                    )}
                  </td>
                  <td className="py-3 text-[11px] text-text-muted">
                    <div className="font-mono text-text-secondary">{heldFor}</div>
                    <div className="mt-0.5 text-[10px]">{formatTimestamp(row.updatedAt)}</div>
                  </td>
                </tr>
              );
            })
          ) : (
            <tr>
              <td colSpan={14} className="py-10 text-center text-sm text-text-muted">
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
        expiry: position.expiry || null,
        dte: computeDTE(position.expiry),
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
        expiry: position.expiry || null,
        dte: computeDTE(position.expiry),
        phase: position.phase || null,
        trailingStop: position.trailing_stop ?? null,
        peakPrice: position.peak_price ?? null,
        entryIvPct: position.entry_iv_pct ?? null,
        signalReason: position.signal_reason || null,
        enteredAt: position.entered_at || null,
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
        expiry: position.expiry || null,
        dte: computeDTE(position.expiry),
        stopPrice: position.stop_price ?? null,
        targetPrice: position.target_price ?? null,
        targetReached: position.target_reached ?? null,
        peakPrice: position.peak_price ?? null,
        signalReason: position.signal_reason || null,
        enteredAt: position.entered_at || null,
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
            Portfolio Positions
          </div>
          <p className="mt-2 max-w-4xl text-sm leading-6 text-text-secondary line-clamp-2">
            One live book for manual trading, the NSE desk, and the commodity desk. This normalized ledger remains available directly, even though portfolio analytics now carry the primary navigation slot.
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
