"use client";

/**
 * US MACD Refined desk (v2).
 *
 * The MACD Refined engine (pure premium-MACD zero-cross, IV-mapping, hard SL +
 * partial booking + trailing) run on US equity/ETF options via Alpaca. Paper
 * only. Tabs: Data health (Alpaca), Signals (recorded), Positioning (current +
 * next monthly 3rd-Friday expiry), Paper book. API under /api/us/macd-refined.
 */
import { useState, useTransition } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Banknote, CalendarClock, ListChecks, RefreshCw } from "lucide-react";

import {
  DeskShell, MetricTile, REFRESH_MS, Section, StatusBadge,
  formatNumber, formatPct, formatIST, tone, useUrlTab,
} from "@/components/desk-ui";
import {
  getUsMacdSummary, getUsMacdDataHealth, getUsMacdPositioning,
  getUsMacdSignals, getUsMacdPaperPositions, runUsMacdLiveCycle,
} from "@/lib/api";

const usd = (n: unknown) => {
  const v = Number(n);
  if (!Number.isFinite(v)) return "$0";
  return `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
};

const TABS = [
  { key: "health", label: "Data health", icon: Activity },
  { key: "signals", label: "Signals", icon: ListChecks },
  { key: "positioning", label: "Positioning", icon: CalendarClock },
  { key: "paper", label: "Paper book", icon: Banknote },
];

export default function UsMacdDesk() {
  const [activeTab, setActiveTab] = useUrlTab("health");
  const [isPending, startTransition] = useTransition();
  const [cycleMsg, setCycleMsg] = useState<string | null>(null);

  const summaryQuery = useQuery({
    queryKey: ["us-macd", "summary"],
    queryFn: () => getUsMacdSummary().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
  });
  const healthQuery = useQuery({
    queryKey: ["us-macd", "health"],
    queryFn: () => getUsMacdDataHealth().then((r) => r.data),
    enabled: activeTab === "health",
    refetchInterval: REFRESH_MS.snapshot,
  });
  const signalsQuery = useQuery({
    queryKey: ["us-macd", "signals"],
    queryFn: () => getUsMacdSignals(undefined, 100).then((r) => r.data),
    enabled: activeTab === "signals",
    refetchInterval: REFRESH_MS.snapshot,
  });
  const positioningQuery = useQuery({
    queryKey: ["us-macd", "positioning"],
    queryFn: () => getUsMacdPositioning().then((r) => r.data),
    enabled: activeTab === "positioning",
    refetchInterval: REFRESH_MS.snapshot,
  });
  const paperQuery = useQuery({
    queryKey: ["us-macd", "paper"],
    queryFn: () => getUsMacdPaperPositions(undefined, "all", 100).then((r) => r.data),
    enabled: activeTab === "paper",
    refetchInterval: REFRESH_MS.snapshot,
  });

  const summary = summaryQuery.data as any;
  const params = summary?.params ?? {};

  const runCycle = () => startTransition(async () => {
    try {
      const d = (await runUsMacdLiveCycle(false)).data as any;
      setCycleMsg(d?.broker_ready
        ? `Persisted ${d.snapshots_persisted} snapshots · funnel ${JSON.stringify(d.funnel)}`
        : `Alpaca not connected — ${d?.note ?? "no data"}`);
    } catch (e: any) { setCycleMsg(e?.message ?? "cycle failed"); }
  });

  return (
    <DeskShell
      title="US MACD Refined"
      description="Premium-MACD zero-cross on US equity/ETF options via Alpaca — pure MACD, IV as mapping, hard SL + partial booking + trailing. Paper, USD."
      paperMode
      isFetching={summaryQuery.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      rightSlot={
        <button type="button" onClick={runCycle} disabled={isPending}
          className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-1.5 text-xs text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary disabled:opacity-50">
          <RefreshCw size={13} className={isPending ? "animate-spin" : undefined} /> Run cycle
        </button>
      }
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <MetricTile label="Market" value="US (Alpaca)" size="sm" />
        <MetricTile label="MACD" value={(params.macd ?? [12, 26, 9]).join(", ")} size="sm" />
        <MetricTile label="Timeframe" value={summary?.timeframe ?? "30minute"} size="sm" />
        <MetricTile label="Universe" value={`${(summary?.live_universe ?? []).length} names`} size="sm" />
        <MetricTile label="Stop" value={`-${Math.round((params.catastrophe_stop_pct ?? 0.3) * 100)}%`} size="sm" />
        <MetricTile label="Capital" value={usd(params.starting_equity ?? 100000)} size="sm" />
      </div>
      {cycleMsg ? <div className="rounded-xl border border-bg-border bg-bg-primary/14 px-3 py-2 text-xs text-text-secondary">{cycleMsg}</div> : null}

      {activeTab === "health" ? (
        <Section title="Alpaca data source">
          {(() => {
            const d = healthQuery.data as any;
            if (!d) return <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">Checking Alpaca…</div>;
            const ok = d.configured && d.ok;
            return (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <StatusBadge label={d.configured ? (d.ok ? "connected" : "configured, not reachable") : "not configured"} variant={ok ? "success" : (d.configured ? "warn" : "error")} />
                  <span className="text-[11px] text-text-muted">provider: {d.provider ?? "alpaca"}</span>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  <MetricTile label="Keys present" value={d.configured ? "yes" : "no"} size="sm" />
                  <MetricTile label="SPY quote" value={d.spy != null ? usd(d.spy) : "—"} size="sm" />
                  <MetricTile label="Checked" value={d.checked_at ? formatIST(d.checked_at) : "—"} size="sm" />
                </div>
                {!d.configured ? (
                  <div className="rounded-xl border border-accent-amber/30 bg-accent-amber/5 p-3 text-[12px] text-accent-amber">
                    {d.note ?? "Add ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY to TradeBot/.env, then restart the backend."}
                  </div>
                ) : null}
                {d.error ? <div className="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3 text-[12px] text-accent-red">{d.error}</div> : null}
              </div>
            );
          })()}
        </Section>
      ) : null}

      {activeTab === "signals" ? (
        <Section title="Recorded premium-MACD signals">
          {(() => {
            const d = signalsQuery.data as any;
            const rows = d?.signals ?? [];
            if (!rows.length) return <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">No signals yet. They record once Alpaca is connected and a fresh premium-MACD cross forms during US RTH.</div>;
            return (
              <div className="overflow-x-auto rounded-xl border border-bg-border">
                <table className="w-full text-sm">
                  <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
                    <tr>{["Time", "Symbol", "Leg", "Strike", "Premium", "IV", "IV-rank", "Zone", "Accepted", "Reason"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.map((s: any, i: number) => (
                      <tr key={i} className="border-t border-bg-border/60">
                        <td className="px-3 py-2 text-text-muted">{String(s.signal_time ?? "").slice(0, 16)}</td>
                        <td className="px-3 py-2 font-mono text-text-primary">{s.underlying}</td>
                        <td className="px-3 py-2 font-mono">{s.option_type}</td>
                        <td className="px-3 py-2 font-mono">{formatNumber(s.strike, 1)}</td>
                        <td className="px-3 py-2 font-mono text-right">{usd(s.premium)}</td>
                        <td className="px-3 py-2 font-mono text-right">{formatPct(s.iv)}</td>
                        <td className="px-3 py-2 font-mono text-right">{s.iv_rank == null ? "—" : formatPct(s.iv_rank)}</td>
                        <td className="px-3 py-2">{s.iv_zone}</td>
                        <td className="px-3 py-2"><StatusBadge label={s.accepted ? "accepted" : "skipped"} variant={s.accepted ? "success" : "neutral"} /></td>
                        <td className="px-3 py-2 text-[11px] text-text-muted">{s.skip_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })()}
        </Section>
      ) : null}

      {activeTab === "positioning" ? (
        <Section title="Current + next monthly expiry (3rd Friday)">
          {positioningQuery.data ? (
            <div className="overflow-x-auto rounded-xl border border-bg-border">
              <table className="w-full text-sm">
                <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
                  <tr>{["Symbol", "Current expiry", "Next expiry", "Tracked rows", "Latest capture"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {((positioningQuery.data as any).symbols ?? []).map((s: any) => (
                    <tr key={s.underlying} className="border-t border-bg-border/60">
                      <td className="px-3 py-2 font-mono font-semibold text-text-primary">{s.underlying}</td>
                      <td className="px-3 py-2 font-mono">{s.current_expiry ?? "—"}</td>
                      <td className="px-3 py-2 font-mono">{s.next_expiry ?? "—"}</td>
                      <td className="px-3 py-2 font-mono text-right">{formatNumber(s.tracked_rows, 0)}</td>
                      <td className="px-3 py-2 text-text-muted">{s.latest_capture ? formatIST(s.latest_capture) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">Loading…</div>}
        </Section>
      ) : null}

      {activeTab === "paper" ? (
        (() => {
          const d = paperQuery.data as any;
          const sum = d?.summary ?? {};
          const open = d?.open_positions ?? [];
          const closed = d?.closed_positions ?? [];
          const rows = [...open, ...closed];
          return (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
                <MetricTile label="Equity" value={usd(sum.total_equity)} />
                <MetricTile label="Realized" value={usd(sum.realized_pnl)} color={tone(sum.realized_pnl)} />
                <MetricTile label="Unrealized" value={usd(sum.unrealized_pnl)} color={tone(sum.unrealized_pnl)} />
                <MetricTile label="Open" value={`${sum.open_positions ?? 0} (${sum.ce_open ?? 0}C/${sum.pe_open ?? 0}P)`} />
                <MetricTile label="Win rate" value={formatPct(sum.win_rate)} />
                <MetricTile label="Max DD" value={formatPct(sum.max_drawdown)} />
              </div>
              <Section title={`Positions (${open.length} open · ${closed.length} closed)`}>
                {rows.length === 0 ? (
                  <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-4 text-sm text-text-muted">No US paper positions yet.</div>
                ) : (
                  <div className="overflow-x-auto rounded-xl border border-bg-border">
                    <table className="w-full text-sm">
                      <thead className="bg-bg-secondary/40 text-[11px] uppercase tracking-wide text-text-muted">
                        <tr>{["Symbol", "Leg", "Strike", "Expiry", "Qty", "Entry", "Now/Exit", "P&L", "Status"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
                      </thead>
                      <tbody>
                        {rows.map((p: any) => {
                          const isOpen = p.status === "open";
                          const pnl = isOpen ? p.unrealized_pnl : p.realized_pnl;
                          return (
                            <tr key={p.position_id} className="border-t border-bg-border/60">
                              <td className="px-3 py-2 font-mono text-text-primary">{p.underlying}</td>
                              <td className="px-3 py-2 font-mono">{p.book ?? p.option_type}</td>
                              <td className="px-3 py-2 font-mono">{formatNumber(p.strike, 1)}</td>
                              <td className="px-3 py-2 font-mono text-text-muted">{p.expiry}</td>
                              <td className="px-3 py-2 font-mono text-right">{p.quantity_units}</td>
                              <td className="px-3 py-2 font-mono text-right">{usd(p.entry_premium)}</td>
                              <td className="px-3 py-2 font-mono text-right">{usd(isOpen ? p.latest_premium : p.exit_premium)}</td>
                              <td className={`px-3 py-2 font-mono text-right ${tone(pnl)}`}>{usd(pnl)}</td>
                              <td className="px-3 py-2"><StatusBadge label={isOpen ? "open" : (p.close_reason ?? "closed")} variant={isOpen ? "info" : (pnl >= 0 ? "success" : "warn")} /></td>
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
        })()
      ) : null}
    </DeskShell>
  );
}
