"use client";

/**
 * Paper trading tab for the directional desk.
 *
 * Consumes the canonical `usePaperDeskQueries` hook (shared with every
 * other desk) instead of re-rolling the summary/positions/journal
 * triple. Three sub-tabs: open / closed / journal.
 */
import { clsx } from "clsx";
import { useState } from "react";
import { Banknote, BookOpen, History, Layers3 } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  decisionTone,
  formatIST,
  formatNumber,
  formatSignedMoney,
  tone,
} from "@/components/desk-ui";
import type { usePaperDeskQueries } from "@/hooks/usePaperDeskQueries";
import { LastUpdated, newestTimestamp } from "@/components/common/LastUpdated";
import { LiveMarkCell } from "@/components/terminal/LiveMarkCell";
import { legTapeSymbol } from "@/lib/marketSymbols";

type Paper = ReturnType<typeof usePaperDeskQueries>;

type OpenPos = {
  position_id: string;
  underlying?: string;
  regime?: string;
  option_type?: string;
  strike?: number;
  expiry?: string;
  expiry_kind?: string;
  quantity_lots?: number;
  quantity_units?: number;
  entry_premium?: number;
  latest_premium?: number;
  unrealized_pnl?: number;
  opened_at?: string;
  updated_at?: string;
  policy_size_multiplier?: number;
};

type ClosedPos = OpenPos & {
  closed_at?: string;
  close_reason?: string;
  exit_premium?: number;
  realized_pnl?: number;
  policy_r_multiple?: number;
};

type JournalEntry = {
  recorded_at?: string;
  underlying?: string;
  regime?: string;
  direction?: string;
  confidence?: number;
  approved?: boolean;
  selection_reason?: string;
};

export default function PaperTradingTab({ symbol, paper }: { symbol?: string; paper: Paper }) {
  const [view, setView] = useState<"open" | "closed" | "journal">("open");

  const cap = (paper.summary.data as Record<string, number> | undefined) || {};
  const pos = paper.positions.data as { open_positions?: OpenPos[]; closed_positions?: ClosedPos[] } | undefined;
  const opens = pos?.open_positions ?? [];
  const closes = pos?.closed_positions ?? [];
  const records = (paper.journal.data as { records?: JournalEntry[] } | undefined)?.records ?? [];

  const onReset = async () => {
    if (!paper.resetAccount) return;
    if (typeof window !== "undefined" && window.confirm("Reset the directional paper book?")) {
      await paper.resetAccount("user");
    }
  };

  return (
    <div className="space-y-4">
      <Section
        title={`Paper book${symbol ? ` · ${symbol}` : " · all symbols"}`}
        icon={<Banknote size={16} />}
        rightSlot={
          paper.resetAccount ? (
            <button
              type="button"
              onClick={onReset}
              className="rounded-lg border border-accent-red/30 bg-accent-red/8 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-accent-red hover:bg-accent-red/15"
            >
              Reset book
            </button>
          ) : null
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile label="Equity" value={`₹${(cap.total_equity ?? 0).toLocaleString("en-IN")}`} detail={`Init ₹${(cap.initial_capital ?? 0).toLocaleString("en-IN")}`} />
          <MetricTile label="Available" value={`₹${(cap.available_capital ?? 0).toLocaleString("en-IN")}`} detail={`Reserved ₹${(cap.reserved_margin ?? 0).toLocaleString("en-IN")}`} />
          <MetricTile label="Realized" value={formatSignedMoney(cap.realized_pnl)} color={tone(cap.realized_pnl)} detail={`${cap.total_trades ?? 0} closed`} />
          <MetricTile label="Unrealized" value={formatSignedMoney(cap.unrealized_pnl)} color={tone(cap.unrealized_pnl)} detail={`${cap.open_positions ?? 0} open`} />
          <MetricTile label="Return %" value={cap.total_return_pct != null ? `${cap.total_return_pct.toFixed(3)}%` : "—"} color={tone(cap.total_pnl)} detail={`Sharpe ${formatNumber(cap.sharpe_ratio, 2)}`} />
          <MetricTile label="Win rate" value={cap.win_rate != null ? `${(cap.win_rate * 100).toFixed(1)}%` : "—"} detail={`Max DD ${cap.max_drawdown != null ? (cap.max_drawdown * 100).toFixed(2) + "%" : "—"}`} />
        </div>
      </Section>

      <Section
        title={`Trades${symbol ? " · " + symbol : ""}`}
        rightSlot={
          <LastUpdated
            timestamp={newestTimestamp(
              view === "open"
                ? opens.map((p) => p.updated_at ?? p.opened_at)
                : view === "closed"
                  ? closes.map((p) => p.closed_at ?? p.updated_at ?? p.opened_at)
                  : records.map((r) => r.recorded_at),
            )}
          />
        }
      >
        <div className="mb-3 flex items-center gap-1.5">
          {([
            { k: "open", label: `Open (${opens.length})`, icon: Layers3 },
            { k: "closed", label: `Closed (${closes.length})`, icon: History },
            { k: "journal", label: `Journal (${records.length})`, icon: BookOpen },
          ] as const).map(({ k, label, icon: Icon }) => (
            <button
              key={k}
              type="button"
              onClick={() => setView(k)}
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[11.5px] font-semibold uppercase tracking-[0.12em] transition-colors",
                view === k
                  ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                  : "border-bg-border bg-bg-primary/12 text-text-secondary hover:text-text-primary",
              )}
            >
              <Icon size={13} />
              {label}
            </button>
          ))}
        </div>

        {view === "open" ? (
          <Table
            rows={opens}
            empty="No open positions."
            columns={[
              { th: "Symbol", render: (p: OpenPos) => p.underlying },
              { th: "Contract", render: (p: OpenPos) => `${p.option_type ?? ""} ${p.strike ?? ""} · ${p.expiry ?? ""}` },
              { th: "Regime", render: (p: OpenPos) => p.regime ?? "—" },
              { th: "Lots", render: (p: OpenPos) => p.quantity_lots ?? 0, align: "right" },
              { th: "Entry → Mark", render: (p: OpenPos) => (<span className="font-mono">{formatNumber(p.entry_premium, 2)} → <LiveMarkCell symbol={legTapeSymbol(p)} fallback={p.latest_premium} fallbackAt={p.updated_at ?? p.opened_at} decimals={2} /></span>), align: "right" },
              { th: "Unrealized", render: (p: OpenPos) => formatSignedMoney(p.unrealized_pnl), align: "right", tone: (p: OpenPos) => tone(p.unrealized_pnl) },
              { th: "Size mult", render: (p: OpenPos) => p.policy_size_multiplier != null ? `${p.policy_size_multiplier.toFixed(2)}×` : "—", align: "right" },
              { th: "Opened", render: (p: OpenPos) => formatIST(p.opened_at), align: "right" },
            ]}
          />
        ) : null}

        {view === "closed" ? (
          <Table
            rows={closes}
            empty="No closed positions yet."
            columns={[
              { th: "Symbol", render: (p: ClosedPos) => p.underlying },
              { th: "Contract", render: (p: ClosedPos) => `${p.option_type ?? ""} ${p.strike ?? ""}` },
              { th: "Entry → Exit", render: (p: ClosedPos) => `${formatNumber(p.entry_premium, 2)} → ${formatNumber(p.exit_premium, 2)}`, align: "right" },
              { th: "Realized", render: (p: ClosedPos) => formatSignedMoney(p.realized_pnl), align: "right", tone: (p: ClosedPos) => tone(p.realized_pnl) },
              { th: "R-multiple", render: (p: ClosedPos) => p.policy_r_multiple != null ? `${p.policy_r_multiple >= 0 ? "+" : ""}${p.policy_r_multiple.toFixed(2)}R` : "—", align: "right", tone: (p: ClosedPos) => tone(p.policy_r_multiple) },
              { th: "Reason", render: (p: ClosedPos) => p.close_reason ?? "—" },
              { th: "Closed", render: (p: ClosedPos) => formatIST(p.closed_at), align: "right" },
            ]}
          />
        ) : null}

        {view === "journal" ? (
          <Table
            rows={records.slice(0, 30)}
            empty="No journal entries yet."
            columns={[
              { th: "Time", render: (r: JournalEntry) => formatIST(r.recorded_at) },
              { th: "Symbol", render: (r: JournalEntry) => r.underlying ?? "—" },
              { th: "Regime", render: (r: JournalEntry) => r.regime ?? "—" },
              { th: "Direction", render: (r: JournalEntry) => r.direction ?? "—", tone: (r: JournalEntry) => r.direction === "CE" ? "text-accent-green" : r.direction === "PE" ? "text-accent-red" : "text-text-muted" },
              { th: "Conf", render: (r: JournalEntry) => formatNumber(r.confidence, 3), align: "right" },
              { th: "Action", render: (r: JournalEntry) => <StatusBadge label={r.approved ? "ACT" : "SKIP"} variant={r.approved ? "success" : "warn"} tone={decisionTone(r.approved)} /> },
              { th: "Reason", render: (r: JournalEntry) => <span className="text-[10.5px] line-clamp-1">{r.selection_reason || "—"}</span> },
            ]}
          />
        ) : null}
      </Section>
    </div>
  );
}

type Column<T> = {
  th: string;
  render: (row: T) => React.ReactNode;
  align?: "left" | "right";
  tone?: (row: T) => string;
};

function Table<T>({ rows, columns, empty }: { rows: T[]; columns: Column<T>[]; empty: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
          <tr className="border-b border-bg-border/60">
            {columns.map((c) => (
              <th key={c.th} className={clsx("px-2 py-2", c.align === "right" ? "text-right" : "text-left")}>{c.th}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr><td colSpan={columns.length} className="py-6 text-center text-text-muted">{empty}</td></tr>
          ) : (
            rows.map((row, i) => (
              <tr key={i} className="border-b border-bg-border/30">
                {columns.map((c) => (
                  <td key={c.th} className={clsx("px-2 py-2 font-mono text-[11.5px]", c.align === "right" ? "text-right" : "text-left", c.tone?.(row))}>
                    {c.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
