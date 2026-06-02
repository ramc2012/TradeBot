"use client";

/**
 * Reports — full lifetime trade history across every desk.
 *
 * The live desk dashboards are scoped to "today"; this module holds the
 * complete closed-trade ledger. Filter by desk / date range / search,
 * see aggregate KPIs for the current filter, and export to CSV.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Download, FileText, Filter, RefreshCw, Search } from "lucide-react";

import {
  REPORT_DESKS,
  fetchReportsLedger,
  rowsToCsv,
  type ReportRow,
} from "@/lib/reports-ledger";

function fmt(n?: number | null, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtSigned(n?: number | null, digits = 0, suffix = ""): string {
  if (n == null || Number.isNaN(n)) return "—";
  const prefix = n > 0 ? "+" : "";
  return `${prefix}${Number(n).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${suffix}`;
}
function pnlTone(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "text-text-muted";
  if (n > 0) return "text-accent-green";
  if (n < 0) return "text-accent-red";
  return "text-text-secondary";
}
function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}
function dateOnly(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function KpiCard({ label, value, tone, detail }: { label: string; value: string; tone?: string; detail?: string }) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/25 p-3">
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={clsx("mt-1.5 font-mono text-base font-semibold", tone || "text-text-primary")}>{value}</div>
      {detail ? <div className="mt-0.5 text-[10px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

const deskTone: Record<string, string> = {
  nse: "border-accent-green/30 bg-accent-green/10 text-accent-green",
  commodity: "border-accent-amber/30 bg-accent-amber/10 text-accent-amber",
  directional: "border-accent-blue/30 bg-accent-blue/10 text-accent-blue",
  cbe: "border-accent-purple/30 bg-accent-purple/10 text-accent-purple",
  auction: "border-rose-500/30 bg-rose-500/10 text-rose-300",
  fractal: "border-cyan-500/30 bg-cyan-500/10 text-cyan-300",
};

export default function ReportsPage() {
  const [deskFilter, setDeskFilter] = useState<Set<string>>(new Set());
  const [from, setFrom] = useState<string>("");
  const [to, setTo] = useState<string>("");
  const [search, setSearch] = useState<string>("");

  const query = useQuery({
    queryKey: ["reports-ledger"],
    queryFn: fetchReportsLedger,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const rows = query.data?.rows ?? [];
  const errors = query.data?.errors ?? {};

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (deskFilter.size && !deskFilter.has(r.deskKey)) return false;
      const d = dateOnly(r.exitTime) || dateOnly(r.entryTime);
      if (from && d && d < from) return false;
      if (to && d && d > to) return false;
      if (q) {
        const hay = `${r.desk} ${r.symbol} ${r.contract} ${r.side} ${r.reason}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [rows, deskFilter, from, to, search]);

  const kpis = useMemo(() => {
    const withPnl = filtered.filter((r) => r.pnl != null);
    const gross = withPnl.reduce((s, r) => s + (r.pnl || 0), 0);
    const wins = withPnl.filter((r) => (r.pnl || 0) > 0);
    const losses = withPnl.filter((r) => (r.pnl || 0) < 0);
    const grossWin = wins.reduce((s, r) => s + (r.pnl || 0), 0);
    const grossLoss = Math.abs(losses.reduce((s, r) => s + (r.pnl || 0), 0));
    return {
      trades: filtered.length,
      gross,
      winRate: withPnl.length ? (wins.length / withPnl.length) * 100 : null,
      avgWin: wins.length ? grossWin / wins.length : null,
      avgLoss: losses.length ? -grossLoss / losses.length : null,
      profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
    };
  }, [filtered]);

  const toggleDesk = (key: string) => {
    setDeskFilter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const exportCsv = () => {
    const csv = rowsToCsv(filtered);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `nomad-reports-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const errorKeys = Object.keys(errors);

  return (
    <div className="max-w-screen-2xl space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 font-mono text-lg font-bold text-text-primary">
            <FileText size={18} className="text-accent-blue" />
            Reports · Trade History
          </h1>
          <div className="mt-1 text-xs text-text-muted">
            Full lifetime closed-trade ledger across every desk. Live dashboards show today only — this is the archive.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => query.refetch()}
            className="flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-secondary/30 px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
          >
            <RefreshCw size={13} className={clsx(query.isFetching && "animate-spin")} /> Refresh
          </button>
          <button
            type="button"
            onClick={exportCsv}
            disabled={!filtered.length}
            className="flex items-center gap-1.5 rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-3 py-1.5 text-xs font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-40"
          >
            <Download size={13} /> Export CSV
          </button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        <KpiCard label="Trades" value={String(kpis.trades)} />
        <KpiCard label="Gross P&L" value={fmtSigned(kpis.gross, 0)} tone={pnlTone(kpis.gross)} />
        <KpiCard label="Win rate" value={kpis.winRate != null ? `${kpis.winRate.toFixed(1)}%` : "—"} />
        <KpiCard label="Profit factor" value={kpis.profitFactor != null ? fmt(kpis.profitFactor, 2) : "—"} />
        <KpiCard label="Avg win" value={fmtSigned(kpis.avgWin, 0)} tone="text-accent-green" />
        <KpiCard label="Avg loss" value={fmtSigned(kpis.avgLoss, 0)} tone="text-accent-red" />
      </div>

      {/* Filters */}
      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs text-text-muted">
            <Filter size={13} /> Desks:
          </div>
          {REPORT_DESKS.map((d) => {
            const active = deskFilter.has(d.key);
            return (
              <button
                key={d.key}
                type="button"
                onClick={() => toggleDesk(d.key)}
                className={clsx(
                  "rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-colors",
                  active ? deskTone[d.key] || "border-accent-blue/40 bg-accent-blue/10 text-accent-blue" : "border-bg-border bg-bg-secondary/25 text-text-muted hover:text-text-secondary",
                )}
              >
                {d.label}
              </button>
            );
          })}
          {deskFilter.size ? (
            <button type="button" onClick={() => setDeskFilter(new Set())} className="text-[11px] text-text-muted underline hover:text-text-secondary">
              clear
            </button>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-text-muted">
            From
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="rounded-md border border-bg-border bg-bg-primary/40 px-2 py-1 text-xs text-text-primary" />
          </label>
          <label className="flex items-center gap-1.5 text-xs text-text-muted">
            To
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="rounded-md border border-bg-border bg-bg-primary/40 px-2 py-1 text-xs text-text-primary" />
          </label>
          <div className="flex items-center gap-1.5 rounded-md border border-bg-border bg-bg-primary/40 px-2 py-1">
            <Search size={13} className="text-text-muted" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="symbol / reason / side"
              className="w-44 bg-transparent text-xs text-text-primary outline-none placeholder:text-text-muted"
            />
          </div>
          <span className="text-[11px] text-text-muted">{filtered.length} of {rows.length} rows</span>
        </div>
      </div>

      {errorKeys.length ? (
        <div className="rounded-lg border border-accent-amber/30 bg-accent-amber/10 px-3 py-2 text-[11px] text-accent-amber">
          Some desks did not respond: {errorKeys.join(", ")}. Their trades are omitted from this report.
        </div>
      ) : null}

      {/* Table */}
      <div className="rounded-[18px] border border-bg-border bg-bg-secondary/15 p-4">
        {query.isLoading ? (
          <div className="flex min-h-[200px] items-center justify-center text-sm text-text-muted">Loading ledger…</div>
        ) : filtered.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] text-left text-xs">
              <thead className="border-b border-bg-border text-text-muted">
                <tr>
                  <th className="pb-2 pr-3">Desk</th>
                  <th className="pb-2 pr-3">Symbol</th>
                  <th className="pb-2 pr-3">Contract</th>
                  <th className="pb-2 pr-3">Side</th>
                  <th className="pb-2 pr-3 text-right">Qty</th>
                  <th className="pb-2 pr-3 text-right">Entry</th>
                  <th className="pb-2 pr-3 text-right">Exit</th>
                  <th className="pb-2 pr-3 text-right">P&amp;L</th>
                  <th className="pb-2 pr-3 text-right">Ret%</th>
                  <th className="pb-2 pr-3">Exited</th>
                  <th className="pb-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {filtered.slice(0, 1000).map((r, idx) => (
                  <tr key={r.id} className={clsx("border-b border-bg-border/30", idx % 2 === 1 && "bg-bg-secondary/20")}>
                    <td className="py-2.5 pr-3">
                      <span className={clsx("inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold", deskTone[r.deskKey] || "border-bg-border bg-bg-secondary/40 text-text-secondary")}>
                        {r.desk}
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 font-semibold text-text-primary">{r.symbol}</td>
                    <td className="py-2.5 pr-3 font-mono text-[11px] text-text-muted">{r.contract}</td>
                    <td className="py-2.5 pr-3 font-mono text-text-secondary">{r.side}</td>
                    <td className="py-2.5 pr-3 text-right font-mono text-text-secondary">{r.qty ?? "—"}</td>
                    <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(r.entryPrice)}</td>
                    <td className="py-2.5 pr-3 text-right font-mono text-text-primary">{fmt(r.exitPrice)}</td>
                    <td className={clsx("py-2.5 pr-3 text-right font-mono font-semibold", pnlTone(r.pnl))}>{fmtSigned(r.pnl, 0)}</td>
                    <td className={clsx("py-2.5 pr-3 text-right font-mono", pnlTone(r.returnPct))}>{r.returnPct != null ? fmtSigned(r.returnPct, 1, "%") : "—"}</td>
                    <td className="py-2.5 pr-3 text-[11px] text-text-muted">{fmtTime(r.exitTime)}</td>
                    <td className="max-w-[220px] py-2.5 text-[11px] text-text-secondary" title={r.reason}>{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {filtered.length > 1000 ? (
              <div className="mt-3 text-center text-[11px] text-text-muted">
                Showing first 1000 of {filtered.length} rows — narrow the filter or export CSV for the full set.
              </div>
            ) : null}
          </div>
        ) : (
          <div className="flex min-h-[200px] items-center justify-center text-sm text-text-muted">
            No closed trades match the current filter.
          </div>
        )}
      </div>
    </div>
  );
}
