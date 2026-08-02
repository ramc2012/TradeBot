"use client";

/**
 * MACD Refined desk (v2).
 *
 * Premium-MACD entry, IV-mapped, volume-led single-leg long options
 * (separate CE / PE books). Surfaces:
 *   backtest    → research-validated edge vs the causal forward engine
 *   positioning → current + next monthly expiry + volume-tracking coverage
 *   paper       → CE/PE paper book capital + open/closed positions
 *
 * Same backend (port 8000) as every other desk; endpoints under
 * /api/macd-refined. The "Run live cycle" action fetches current+next expiry
 * chains, persists per-contract volume/turnover, and syncs the paper book.
 */
import { Fragment, useMemo, useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, Banknote, CalendarClock, CandlestickChart, RefreshCw, ShieldCheck } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedMoney,
  formatIST,
  formatMoney,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import {
  getMacdRefinedSummary,
  getMacdRefinedBacktestCompare,
  getMacdRefinedPositioning,
  getMacdRefinedPaperPositions,
  runMacdRefinedLiveCycle,
} from "@/lib/api";
import { LiveMarkCell } from "@/components/terminal/LiveMarkCell";
import { legTapeSymbol } from "@/lib/marketSymbols";
import { LastUpdated, newestTimestamp } from "@/components/common/LastUpdated";
import { OptionChartModal, type OptionChartContract } from "@/components/strategies/shared/OptionChartModal";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { selectStrategySlice, useStrategyPositionsStream } from "@/hooks/useStrategyPositionsStream";

type BookMetrics = {
  trades: number;
  win_rate: number;
  median_return_pct: number;
  mean_return_pct: number;
  profit_factor: number;
  pct_below_minus_50: number;
};

type MacdPaperPosition = {
  position_id?: string | null;
  status?: string | null;
  underlying?: string | null;
  book?: string | null;
  option_type?: string | null;
  direction?: string | null;
  instrument_key?: string | null;
  strike?: number | string | null;
  expiry?: string | null;
  quantity_units?: number | null;
  initial_qty?: number | null;
  entry_premium?: number | null;
  latest_premium?: number | null;
  current_price?: number | null;
  exit_premium?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  latest_spot?: number | string | null;
  exit_spot?: number | string | null;
  entry_spot?: number | string | null;
  opened_at?: string | null;
  closed_at?: string | null;
  updated_at?: string | null;
  mark_source?: string | null;
  close_reason?: string | null;
};

const TABS = [
  { key: "paper", label: "Paper book", icon: Banknote },
  { key: "backtest", label: "Backtest", icon: BarChart3 },
  { key: "positioning", label: "Positioning", icon: CalendarClock },
  { key: "signal-quality", label: "Signal quality", icon: ShieldCheck },
];

function pctTone(winRate?: number): string | undefined {
  if (winRate == null) return undefined;
  return winRate >= 0.6 ? "text-accent-green" : winRate >= 0.45 ? "text-accent-amber" : "text-accent-red";
}

function sideOfPosition(position: MacdPaperPosition): "CE" | "PE" {
  return String(position.book || position.option_type || position.direction || "CE").toUpperCase() === "PE" ? "PE" : "CE";
}

function positionToContract(position: MacdPaperPosition): OptionChartContract | null {
  const strike = Number(position.strike);
  const expiry = String(position.expiry || "").slice(0, 10);
  const underlying = String(position.underlying || "").trim();
  if (!underlying || !Number.isFinite(strike) || !expiry) return null;
  return {
    underlying,
    direction: sideOfPosition(position),
    strike,
    expiry,
    instrumentKey: position.instrument_key ?? null,
    ltp: Number(position.latest_premium ?? position.exit_premium ?? NaN),
  };
}

function sameContract(left: OptionChartContract, right: OptionChartContract): boolean {
  return (
    left.underlying === right.underlying
    && left.direction === right.direction
    && left.expiry === right.expiry
    && Number(left.strike) === Number(right.strike)
  );
}

function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positionId(position: MacdPaperPosition, index: number): string {
  return String(
    position.position_id
    || position.instrument_key
    || `${position.underlying ?? "row"}-${position.book ?? position.option_type ?? "book"}-${position.strike ?? "strike"}-${position.opened_at ?? position.closed_at ?? index}`,
  );
}

function markPremium(position: MacdPaperPosition, isOpen: boolean): number {
  return finiteNumber(
    isOpen
      ? position.current_price ?? position.latest_premium ?? position.entry_premium
      : position.exit_premium ?? position.latest_premium,
  );
}

function spotLtp(position: MacdPaperPosition, isOpen: boolean): number | null {
  const value = finiteNumber(
    isOpen
      ? position.latest_spot ?? position.entry_spot
      : position.exit_spot ?? position.latest_spot ?? position.entry_spot,
    NaN,
  );
  return Number.isFinite(value) && value > 0 ? value : null;
}

function positionPnl(position: MacdPaperPosition, isOpen: boolean): number {
  return finiteNumber(isOpen ? position.unrealized_pnl : position.realized_pnl);
}

function displayQty(position: MacdPaperPosition, isOpen: boolean): number {
  return finiteNumber(isOpen ? position.quantity_units : position.initial_qty ?? position.quantity_units);
}

function zebra(index: number): string {
  return index % 2 === 0 ? "bg-bg-primary/10" : "bg-bg-secondary/20";
}

type OpenBookGroup = {
  book: string;
  rows: MacdPaperPosition[];
  qty: number;
  entryValue: number;
  markValue: number;
  pnl: number;
};

function BooksTable({ books, label }: { books?: Record<string, BookMetrics>; label: string }) {
  if (!books) return null;
  const order = ["CE", "PE", "ALL"];
  return (
    <div className="overflow-x-auto rounded-xl border border-bg-border">
      <table className="w-full text-sm">
        <caption className="px-3 py-2 text-left text-[11px] uppercase tracking-[0.14em] text-text-muted">{label}</caption>
        <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
          <tr>
            {["Book", "Trades", "Win %", "Median ret", "Mean ret", "PF", "% < −50%"].map((h) => (
              <th key={h} className="px-3 py-2 text-right font-medium first:text-left">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {order.map((k) => {
            const b = books[k];
            if (!b) return null;
            return (
              <tr key={k} className="border-t border-bg-border/60">
                <td className="px-3 py-2 font-mono font-semibold text-text-primary">{k}</td>
                <td className="px-3 py-2 text-right font-mono">{b.trades}</td>
                <td className={`px-3 py-2 text-right font-mono ${pctTone(b.win_rate)}`}>{formatPct(b.win_rate)}</td>
                <td className="px-3 py-2 text-right font-mono">{formatPct(b.median_return_pct, 1, { asPercent: true })}</td>
                <td className="px-3 py-2 text-right font-mono">{formatPct(b.mean_return_pct, 1, { asPercent: true })}</td>
                <td className="px-3 py-2 text-right font-mono">{formatNumber(b.profit_factor, 2)}</td>
                <td className="px-3 py-2 text-right font-mono">{formatPct(b.pct_below_minus_50)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function MacdRefinedDesk() {
  const [activeTab, setActiveTab] = useUrlTab("paper");
  const [isPending, startTransition] = useTransition();
  const [cycleResult, setCycleResult] = useState<string | null>(null);

  const summaryQuery = useQuery({
    queryKey: ["macd-refined", "summary"],
    queryFn: () => getMacdRefinedSummary().then((r) => r.data),
    // Live-market page → keep the header freshness poll at ≤30s.
    refetchInterval: REFRESH_MS.snapshot,
  });
  const backtestQuery = useQuery({
    queryKey: ["macd-refined", "backtest-compare"],
    queryFn: () => getMacdRefinedBacktestCompare(undefined, 8).then((r) => r.data),
    enabled: activeTab === "backtest",
    staleTime: 5 * 60_000,
  });
  const positioningQuery = useQuery({
    queryKey: ["macd-refined", "positioning"],
    queryFn: () => getMacdRefinedPositioning().then((r) => r.data),
    enabled: activeTab === "positioning",
    refetchInterval: REFRESH_MS.snapshot,
  });
  const paperQuery = useQuery({
    queryKey: ["macd-refined", "paper"],
    queryFn: () => getMacdRefinedPaperPositions(undefined, "all", 100).then((r) => r.data),
    enabled: activeTab === "paper",
    refetchInterval: REFRESH_MS.snapshot,
  });

  const summary = summaryQuery.data as any;
  const params = summary?.params ?? {};
  const automation = summary?.automation ?? {};
  const automationFailureCount = Number(automation?.last_result_meta?.failure_count ?? 0);
  const automationStatus = String(automation?.last_result_meta?.status ?? "").toLowerCase();
  const automationFailed = Boolean(
    automation?.last_error
    || automationFailureCount > 0
    || ["error", "failed", "timeout", "broker_not_ready"].includes(automationStatus),
  );
  const automationMessage = automation?.last_error
    || automation?.last_result_meta?.message
    || automation?.last_message;

  // Header freshness: the automation runner's last completed cycle is the real
  // "data as of" time. Fall back to the fetch completion time (labelled
  // "Fetched") when the runner has never reported.
  const automationAsOf: string | null =
    automation?.last_success_at ?? automation?.last_finished_at ?? automation?.last_started_at ?? null;
  const runnerInterval = Number(automation?.interval_seconds ?? 0);
  const staleAfter = Math.max(120, runnerInterval * 2 || 0);

  const runCycle = () => {
    startTransition(async () => {
      try {
        const res = await runMacdRefinedLiveCycle(true);
        const d = res.data as any;
        const failures = Object.entries(d?.failures ?? {});
        setCycleResult(
          !d?.broker_ready
            ? `Broker not connected — ${d?.note ?? "nothing fetched"}`
            : failures.length > 0
              ? `Cycle failed for ${failures.length} target(s) — ${failures[0]?.[0]}: ${String(failures[0]?.[1] ?? "unknown error")}`
              : d?.broker_ready
            ? `Persisted ${d.snapshots_persisted} snapshots · ${d.proposals} proposals`
            : "Cycle did not run",
        );
      } catch (e: any) {
        setCycleResult(e?.message ?? "cycle failed");
      }
    });
  };

  return (
    <DeskShell
      title="MACD Refined"
      description="Premium-MACD entry with IV-regime mapping and liquidity gates — separate capped CE/PE books, ATM, held to expiry−7d."
      paperMode
      asOf={automationAsOf ?? (summaryQuery.dataUpdatedAt ? new Date(summaryQuery.dataUpdatedAt) : null)}
      asOfLabel={automationAsOf ? "Updated" : "Fetched"}
      asOfStaleSeconds={staleAfter}
      asOfCriticalSeconds={Math.max(600, staleAfter * 3)}
      isFetching={summaryQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      rightSlot={
        <div className="flex items-center gap-2">
          <StatusBadge
            label={automationFailed ? "automation failed" : automation?.running ? "automation running" : "automation ready"}
            variant={automationFailed ? "error" : automation?.running ? "info" : "success"}
          />
          <button
            type="button"
            onClick={runCycle}
            disabled={isPending}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-1.5 text-xs text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary disabled:opacity-50"
          >
            <RefreshCw size={13} className={isPending ? "animate-spin" : undefined} /> Run live cycle
          </button>
        </div>
      }
    >
      {automationFailed ? (
        <div className="rounded-xl border border-accent-red/35 bg-accent-red/8 px-3 py-2 text-xs text-accent-red">
          {automationMessage || `Last automated cycle reported ${automationFailureCount} failure(s).`}
        </div>
      ) : null}
      {/* Param strip */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <MetricTile label="MACD" value={(params.macd ?? [12, 26, 9]).join(", ")} size="sm" />
        <MetricTile label="Timeframe" value={summary?.timeframe ?? "30minute"} size="sm" />
        <MetricTile label="IV cheap label" value={`< ${params.iv_rank_max ?? 0.3}`} size="sm" />
        <MetricTile label="Stop" value={`−${Math.round((params.catastrophe_stop_pct ?? 0.5) * 100)}% (${params.catastrophe_stop_basis ?? "bar_close"})`} size="sm" />
        <MetricTile label="Books" value={`${params.ce_slots ?? 10} CE / ${params.pe_slots ?? 10} PE`} size="sm" />
        <MetricTile label="Slippage" value={formatPct(params.round_trip_slippage_pct ?? 0.05)} size="sm" />
      </div>
      {cycleResult ? (
        <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2 text-xs text-text-secondary">{cycleResult}</div>
      ) : null}

      {activeTab === "backtest" ? (
        <Section title="Backtest — documented edge vs causal forward engine">
          {backtestQuery.isLoading ? (
            <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">Running backtest over existing data…</div>
          ) : backtestQuery.data ? (
            (() => {
              const d = backtestQuery.data as any;
              const r = d.research, e = d.engine;
              return (
                <div className="space-y-4">
                  <div className="grid gap-4 lg:grid-cols-2">
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <StatusBadge label="research-validated" variant="success" />
                        <span className="text-[11px] text-text-muted">data/signals/macd_signals.parquet · pure hold, gross</span>
                      </div>
                      <BooksTable books={r?.portfolio?.books} label="Portfolio books (research)" />
                      <div className="grid grid-cols-2 gap-2">
                        <MetricTile label="Signal win-rate" value={formatPct(r?.signals?.signal_level_metrics?.win_rate)} size="sm" color={pctTone(r?.signals?.signal_level_metrics?.win_rate)} />
                        <MetricTile label="Signal median" value={formatPct(r?.signals?.signal_level_metrics?.median_return_pct, 1, { asPercent: true })} size="sm" />
                      </div>
                    </div>
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <StatusBadge label="causal engine" variant="warn" />
                        <span className="text-[11px] text-text-muted">no hindsight · −50% stop + slippage</span>
                      </div>
                      <BooksTable books={e?.portfolio?.books} label="Portfolio books (engine)" />
                      <div className="grid grid-cols-2 gap-2">
                        <MetricTile label="Signal win-rate" value={formatPct(e?.signals?.signal_level_metrics?.win_rate)} size="sm" color={pctTone(e?.signals?.signal_level_metrics?.win_rate)} />
                        <MetricTile label="Signal median" value={formatPct(e?.signals?.signal_level_metrics?.median_return_pct, 1, { asPercent: true })} size="sm" />
                      </div>
                    </div>
                  </div>
                  <ul className="space-y-1 rounded-xl border border-bg-border bg-bg-primary/10 p-3 text-[11.5px] text-text-muted">
                    {(d.caveats ?? []).map((c: string, i: number) => (
                      <li key={i}>• {c}</li>
                    ))}
                  </ul>
                </div>
              );
            })()
          ) : (
            <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-accent-amber">Backtest unavailable.</div>
          )}
        </Section>
      ) : null}

      {activeTab === "positioning" ? (
        <Section title="Next-month positioning — current + next monthly expiry">
          {positioningQuery.data ? (
            <div className="overflow-x-auto rounded-xl border border-bg-border">
              <table className="w-full text-sm">
                <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
                  <tr>
                    {["Symbol", "Type", "Current expiry", "Next expiry", "Tracked rows", "Latest capture"].map((h) => (
                      <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {((positioningQuery.data as any).symbols ?? []).map((s: any) => (
                    <tr key={s.underlying} className="border-t border-bg-border/60">
                      <td className="px-3 py-2 font-mono font-semibold text-text-primary">{s.underlying}</td>
                      <td className="px-3 py-2 text-text-muted">{s.is_index ? "Index" : "Stock"}</td>
                      <td className="px-3 py-2 font-mono">{s.current_expiry ?? "—"}</td>
                      <td className="px-3 py-2 font-mono">{s.next_expiry ?? "—"}</td>
                      <td className="px-3 py-2 font-mono text-right">{formatNumber(s.tracked_rows, 0)}</td>
                      <td className="px-3 py-2 text-text-muted">{s.latest_capture ? formatIST(s.latest_capture) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">Loading positioning…</div>
          )}
        </Section>
      ) : null}

      {activeTab === "paper" ? (
        <PaperBook data={paperQuery.data as any} />
      ) : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["macd_refined"]} title="MACD Refined signal validation" />
      ) : null}
    </DeskShell>
  );
}

function PaperBook({ data }: { data: any }) {
  const [bookTab, setBookTab] = useState<"open" | "closed">("open");
  const positionsStream = useStrategyPositionsStream();
  const streamSlice = selectStrategySlice(positionsStream.data, "macd");
  const restOpen = (data?.open_positions ?? []) as MacdPaperPosition[];
  const streamedOpen = streamSlice?.open_positions;
  const open = (Array.isArray(streamedOpen) ? streamedOpen : restOpen) as MacdPaperPosition[];
  const closed = (data?.closed_positions ?? []) as MacdPaperPosition[];
  const summary = (streamSlice?.summary ?? data?.summary ?? {}) as Record<string, any>;
  const openGroups = useMemo(() => {
    const groups = ["CE", "PE"].map((book) => ({
      book,
      rows: open.filter((position) => sideOfPosition(position) === book),
    }));
    const extras = open.filter((position) => !["CE", "PE"].includes(sideOfPosition(position)));
    if (extras.length) groups.push({ book: "OTHER", rows: extras });
    return groups
      .map((group) => {
        const qty = group.rows.reduce((sum, position) => sum + displayQty(position, true), 0);
        const entryValue = group.rows.reduce((sum, position) => sum + finiteNumber(position.entry_premium) * displayQty(position, true), 0);
        const markValue = group.rows.reduce((sum, position) => sum + markPremium(position, true) * displayQty(position, true), 0);
        const pnl = group.rows.reduce((sum, position) => sum + positionPnl(position, true), 0);
        return { ...group, qty, entryValue, markValue, pnl };
      })
      .filter((group) => group.rows.length > 0);
  }, [open]);
  const chartContracts = useMemo(
    () => open.map(positionToContract).filter((contract): contract is OptionChartContract => contract !== null),
    [open],
  );
  const [chart, setChart] = useState<{ list: OptionChartContract[]; index: number } | null>(null);
  const streamUsingOpenRows = Array.isArray(streamedOpen);

  const openChart = (position: MacdPaperPosition) => {
    const target = positionToContract(position);
    if (!target || !chartContracts.length) return;
    const index = chartContracts.findIndex((contract) => sameContract(contract, target));
    setChart({ list: chartContracts, index: index >= 0 ? index : 0 });
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <MetricTile label="Total equity" value={formatSignedMoney(summary.total_equity)} />
        <MetricTile label="Realized" value={formatSignedMoney(summary.realized_pnl)} color={tone(summary.realized_pnl)} />
        <MetricTile label="Unrealized" value={formatSignedMoney(summary.unrealized_pnl)} color={tone(summary.unrealized_pnl)} />
        <MetricTile label="Open" value={`${summary.open_positions ?? 0} (${summary.ce_open ?? 0}CE/${summary.pe_open ?? 0}PE)`} />
        <MetricTile label="Win rate" value={formatPct(summary.win_rate)} color={pctTone(summary.win_rate)} />
        <MetricTile label="Max DD" value={formatPct(summary.max_drawdown)} />
      </div>
      <Section
        title="Paper book"
        description={bookTab === "open" ? "Open positions stream from the positions-overview socket when available." : "Closed trades are kept on the stable REST snapshot."}
        rightSlot={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <LastUpdated
              timestamp={newestTimestamp(
                (bookTab === "open" ? open : closed).map((p) => p.updated_at ?? p.closed_at ?? p.opened_at),
              )}
            />
            {bookTab === "open" ? (
              <StatusBadge
                label={positionsStream.isStreamConnected && streamUsingOpenRows ? "stream live" : "poll fallback"}
                variant={positionsStream.isStreamConnected && streamUsingOpenRows ? "success" : "warn"}
              />
            ) : null}
            <div className="inline-flex rounded-lg border border-bg-border bg-bg-primary/20 p-1 text-xs">
              <button
                type="button"
                onClick={() => setBookTab("open")}
                className={`rounded-md px-3 py-1.5 transition-colors ${bookTab === "open" ? "bg-accent-blue/18 text-accent-blue" : "text-text-muted hover:text-text-primary"}`}
              >
                Open book
              </button>
              <button
                type="button"
                onClick={() => setBookTab("closed")}
                className={`rounded-md px-3 py-1.5 transition-colors ${bookTab === "closed" ? "bg-accent-blue/18 text-accent-blue" : "text-text-muted hover:text-text-primary"}`}
              >
                Closed trades
              </button>
            </div>
          </div>
        }
      >
        {bookTab === "open" ? (
          <OpenBookTable
            groups={openGroups}
            openCount={open.length}
            onOpenChart={openChart}
          />
        ) : (
          <ClosedTradesTable closed={closed} />
        )}
        {chart && chart.list[chart.index] ? (
          <OptionChartModal
            contracts={chart.list}
            index={chart.index}
            onIndexChange={(index) => setChart((current) => (current ? { ...current, index } : current))}
            onClose={() => setChart(null)}
          />
        ) : null}
      </Section>
    </div>
  );
}

function OpenBookTable({
  groups,
  openCount,
  onOpenChart,
}: {
  groups: OpenBookGroup[];
  openCount: number;
  onOpenChart: (position: MacdPaperPosition) => void;
}) {
  if (openCount === 0) {
    return (
      <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">
        No open paper positions. Entries open during the NSE session once a live broker is connected and a fresh premium-MACD cross clears the IV / liquidity gates.
      </div>
    );
  }

  let rowIndex = 0;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {groups.map((group) => (
          <div key={group.book} className="rounded-xl border border-bg-border bg-bg-primary/12 px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <StatusBadge label={`${group.book} subtotal`} variant={group.book === "CE" ? "success" : group.book === "PE" ? "info" : "neutral"} />
              <span className={`font-mono text-sm ${tone(group.pnl)}`}>{formatSignedMoney(group.pnl)}</span>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px] text-text-muted">
              <div>
                <div className="uppercase tracking-wide">Positions</div>
                <div className="font-mono text-text-primary">{group.rows.length}</div>
              </div>
              <div>
                <div className="uppercase tracking-wide">Qty</div>
                <div className="font-mono text-text-primary">{formatNumber(group.qty, 0)}</div>
              </div>
              <div>
                <div className="uppercase tracking-wide">Live value</div>
                <div className="font-mono text-text-primary">{formatMoney(group.markValue)}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="overflow-x-auto rounded-xl border border-bg-border">
        <table className="w-full min-w-[1320px] table-fixed text-sm">
          <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
            <tr>
              <th className="w-10 px-3 py-2 text-left font-medium" aria-label="Chart" />
              <th className="w-32 px-3 py-2 text-left font-medium">Symbol</th>
              <th className="w-16 px-3 py-2 text-left font-medium">Book</th>
              <th className="w-24 px-3 py-2 text-right font-medium">Strike</th>
              <th className="w-28 px-3 py-2 text-left font-medium">Expiry</th>
              <th className="w-24 px-3 py-2 text-right font-medium">Spot LTP</th>
              <th className="w-24 px-3 py-2 text-right font-medium">Qty</th>
              <th className="w-24 px-3 py-2 text-right font-medium">Entry</th>
              <th className="w-28 px-3 py-2 text-right font-medium">Live mark</th>
              <th className="w-28 px-3 py-2 text-right font-medium">P&L</th>
              <th className="w-28 px-3 py-2 text-left font-medium">Opened</th>
              <th className="w-28 px-3 py-2 text-left font-medium">Updated</th>
              <th className="w-28 px-3 py-2 text-left font-medium">State</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <Fragment key={group.book}>
                <tr key={`${group.book}-heading`} className="border-t border-bg-border/70 bg-bg-secondary/35">
                  <td colSpan={13} className="px-3 py-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-xs font-semibold uppercase tracking-wide text-text-primary">{group.book} book</span>
                      <span className="text-[11px] text-text-muted">
                        {group.rows.length} positions · qty {formatNumber(group.qty, 0)} · entry value {formatMoney(group.entryValue)} · live value {formatMoney(group.markValue)}
                      </span>
                    </div>
                  </td>
                </tr>
                {group.rows.map((p) => {
                  const pnl = positionPnl(p, true);
                  const canChart = positionToContract(p) !== null;
                  const rowClass = zebra(rowIndex++);
                  const liveMark = markPremium(p, true);
                  const markSource = String(p.mark_source || "").toLowerCase();
                  return (
                    <tr key={positionId(p, rowIndex)} className={`border-t border-bg-border/45 ${rowClass} hover:bg-bg-primary/25`}>
                      <td className="px-3 py-2">
                        {canChart ? (
                          <button
                            type="button"
                            onClick={() => onOpenChart(p)}
                            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-bg-border bg-bg-primary/30 text-text-muted transition-colors hover:border-accent-blue/60 hover:text-accent-blue"
                            aria-label={`Open chart for ${p.underlying}`}
                            title="Open premium chart · KAMA · MACD"
                          >
                            <CandlestickChart size={14} />
                          </button>
                        ) : (
                          <span className="block h-7 w-7" />
                        )}
                      </td>
                      <td className="truncate px-3 py-2 font-mono text-text-primary">{p.underlying}</td>
                      <td className="px-3 py-2 font-mono">{p.book ?? p.option_type}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatNumber(Number(p.strike), 0)}</td>
                      <td className="px-3 py-2 font-mono text-text-muted">{p.expiry}</td>
                      <td className="px-3 py-2 text-right font-mono text-text-secondary">{formatNumber(spotLtp(p, true), 2)}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatNumber(displayQty(p, true), 0)}</td>
                      <td className="px-3 py-2 text-right font-mono">{formatNumber(finiteNumber(p.entry_premium), 2)}</td>
                      <td className="px-3 py-2 text-right text-accent-blue">
                        <LiveMarkCell symbol={legTapeSymbol(p)} fallback={liveMark} decimals={2} />
                      </td>
                      <td className={`px-3 py-2 text-right font-mono ${tone(pnl)}`}>{formatSignedMoney(pnl)}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-text-muted">{formatIST(p.opened_at)}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-text-muted">{formatIST(p.updated_at ?? p.opened_at)}</td>
                      <td className="px-3 py-2">
                        <StatusBadge
                          label={markSource === "live_tick" ? "live" : markSource === "scan_guarded" ? "guarded" : "scan"}
                          variant={markSource === "live_tick" ? "success" : markSource === "scan_guarded" ? "warn" : "info"}
                        />
                      </td>
                    </tr>
                  );
                })}
                <tr key={`${group.book}-subtotal`} className="border-t border-bg-border/70 bg-bg-primary/25">
                  <td colSpan={6} className="px-3 py-2 text-right text-[11px] font-semibold uppercase tracking-wide text-text-muted">{group.book} subtotal</td>
                  <td className="px-3 py-2 text-right font-mono font-semibold text-text-primary">{formatNumber(group.qty, 0)}</td>
                  <td className="px-3 py-2 text-right font-mono text-text-muted">{formatMoney(group.entryValue)}</td>
                  <td className="px-3 py-2 text-right font-mono text-text-muted">{formatMoney(group.markValue)}</td>
                  <td className={`px-3 py-2 text-right font-mono font-semibold ${tone(group.pnl)}`}>{formatSignedMoney(group.pnl)}</td>
                  <td colSpan={3} />
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ClosedTradesTable({ closed }: { closed: MacdPaperPosition[] }) {
  if (closed.length === 0) {
    return (
      <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">
        No closed MACD Refined paper trades yet.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-bg-border">
      <table className="w-full min-w-[1240px] table-fixed text-sm">
        <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="w-32 px-3 py-2 text-left font-medium">Symbol</th>
            <th className="w-16 px-3 py-2 text-left font-medium">Book</th>
            <th className="w-24 px-3 py-2 text-right font-medium">Strike</th>
            <th className="w-28 px-3 py-2 text-left font-medium">Expiry</th>
            <th className="w-24 px-3 py-2 text-right font-medium">Spot LTP</th>
            <th className="w-24 px-3 py-2 text-right font-medium">Qty</th>
            <th className="w-24 px-3 py-2 text-right font-medium">Entry</th>
            <th className="w-24 px-3 py-2 text-right font-medium">Exit</th>
            <th className="w-28 px-3 py-2 text-right font-medium">P&L</th>
            <th className="w-28 px-3 py-2 text-left font-medium">Opened</th>
            <th className="w-28 px-3 py-2 text-left font-medium">Closed</th>
            <th className="w-32 px-3 py-2 text-left font-medium">Reason</th>
          </tr>
        </thead>
        <tbody>
          {closed.map((p, index) => {
            const pnl = positionPnl(p, false);
            return (
              <tr key={positionId(p, index)} className={`border-t border-bg-border/45 ${zebra(index)} hover:bg-bg-primary/25`}>
                <td className="truncate px-3 py-2 font-mono text-text-primary">{p.underlying}</td>
                <td className="px-3 py-2 font-mono">{p.book ?? p.option_type}</td>
                <td className="px-3 py-2 text-right font-mono">{formatNumber(Number(p.strike), 0)}</td>
                <td className="px-3 py-2 font-mono text-text-muted">{p.expiry}</td>
                <td className="px-3 py-2 text-right font-mono text-text-secondary">{formatNumber(spotLtp(p, false), 2)}</td>
                <td className="px-3 py-2 text-right font-mono">{formatNumber(displayQty(p, false), 0)}</td>
                <td className="px-3 py-2 text-right font-mono">{formatNumber(finiteNumber(p.entry_premium), 2)}</td>
                <td className="px-3 py-2 text-right font-mono">{formatNumber(markPremium(p, false), 2)}</td>
                <td className={`px-3 py-2 text-right font-mono ${tone(pnl)}`}>{formatSignedMoney(pnl)}</td>
                <td className="px-3 py-2 font-mono text-[12px] text-text-muted">{formatIST(p.opened_at)}</td>
                <td className="px-3 py-2 font-mono text-[12px] text-text-muted">{formatIST(p.closed_at)}</td>
                <td className="px-3 py-2"><StatusBadge label={p.close_reason ?? "closed"} variant={pnl >= 0 ? "success" : "warn"} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
