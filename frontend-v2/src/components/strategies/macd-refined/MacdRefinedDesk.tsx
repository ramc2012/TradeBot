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
import { useMemo, useState, useTransition } from "react";
import { Radio as TerminalRadioIcon } from "lucide-react";
import { LaneTerminal } from "@/components/terminal/LaneTerminal";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, Banknote, CalendarClock, RefreshCw, ShieldCheck } from "lucide-react";

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
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";

type BookMetrics = {
  trades: number;
  win_rate: number;
  median_return_pct: number;
  mean_return_pct: number;
  profit_factor: number;
  pct_below_minus_50: number;
};

const TABS = [
  { key: "terminal", label: "Terminal", icon: TerminalRadioIcon },
  { key: "backtest", label: "Backtest", icon: BarChart3 },
  { key: "positioning", label: "Positioning", icon: CalendarClock },
  { key: "paper", label: "Paper book", icon: Banknote },
  { key: "signal-quality", label: "Signal quality", icon: ShieldCheck },
  { key: "live-stream", label: "Live stream", icon: RefreshCw },
];

function pctTone(winRate?: number): string | undefined {
  if (winRate == null) return undefined;
  return winRate >= 0.6 ? "text-accent-green" : winRate >= 0.45 ? "text-accent-amber" : "text-accent-red";
}

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
  const [activeTab, setActiveTab] = useUrlTab("backtest");
  const [isPending, startTransition] = useTransition();
  const [cycleResult, setCycleResult] = useState<string | null>(null);

  const summaryQuery = useQuery({
    queryKey: ["macd-refined", "summary"],
    queryFn: () => getMacdRefinedSummary().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
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
      asOf={summary?.paper_summary ? new Date() : null}
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
        <MetricTile label="Slippage" value={formatPct(params.round_trip_slippage_pct ?? 0.1)} size="sm" />
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
      {activeTab === "terminal" ? <LaneTerminal title="Live Terminal · MACD Refined" /> : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="MACD Refined"
          watchlist={(summary?.live_universe ?? []).map((symbol: string) => ({ symbol }))}
          positionSources={["macd"]}
        />
      ) : null}
    </DeskShell>
  );
}

function PaperBook({ data }: { data: any }) {
  const summary = data?.summary ?? {};
  const open = data?.open_positions ?? [];
  const closed = data?.closed_positions ?? [];
  const rows = useMemo(() => [...open, ...closed], [open, closed]);
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
      <Section title={`Positions (${open.length} open · ${closed.length} closed)`}>
        {rows.length === 0 ? (
          <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">
            No paper positions yet. Entries open during the NSE session once a live broker is connected and a fresh premium-MACD cross clears the IV / liquidity gates.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-bg-border">
            <table className="w-full text-sm">
              <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
                <tr>
                  {["Symbol", "Book", "Strike", "Expiry", "Qty", "Entry", "Latest/Exit", "P&L", "Status"].map((h) => (
                    <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((p: any) => {
                  const isOpen = p.status === "open";
                  const pnl = isOpen ? p.unrealized_pnl : p.realized_pnl;
                  return (
                    <tr key={p.position_id} className="border-t border-bg-border/60">
                      <td className="px-3 py-2 font-mono text-text-primary">{p.underlying}</td>
                      <td className="px-3 py-2 font-mono">{p.book ?? p.option_type}</td>
                      <td className="px-3 py-2 font-mono">{formatNumber(p.strike, 0)}</td>
                      <td className="px-3 py-2 font-mono text-text-muted">{p.expiry}</td>
                      <td className="px-3 py-2 font-mono text-right">{p.quantity_units}</td>
                      <td className="px-3 py-2 font-mono text-right">{formatNumber(p.entry_premium, 2)}</td>
                      <td className="px-3 py-2 font-mono text-right">{formatNumber(isOpen ? p.latest_premium : p.exit_premium, 2)}</td>
                      <td className={`px-3 py-2 font-mono text-right ${tone(pnl)}`}>{formatSignedMoney(pnl)}</td>
                      <td className="px-3 py-2">
                        <StatusBadge label={isOpen ? "open" : (p.close_reason ?? "closed")} variant={isOpen ? "info" : (pnl >= 0 ? "success" : "warn")} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
