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
import { Banknote, BookOpen, History, Layers3, RefreshCw, ShieldCheck, Target, XCircle } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  decisionTone,
  formatIST,
  formatMoney,
  formatNumber,
  formatPct,
  formatSignedMoney,
  tone,
} from "@/components/desk-ui";
import type { usePaperDeskQueries } from "@/hooks/usePaperDeskQueries";
import { api as apiClient, describeApiError } from "@/lib/api";

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
  risk_budget?: number;
  max_loss?: number;
  premium_at_risk?: number;
  entry_spot?: number;
  entry_premium?: number;
  latest_premium?: number;
  latest_spot?: number;
  mark_time?: string;
  unrealized_pnl?: number;
  opened_at?: string;
  policy_size_multiplier?: number;
  policy_sampled_value?: number;
  ai_rule_score?: number;
  ai_rule_setup?: string;
  ai_rule_blockers?: string[];
  price_source?: string;
  selection_reason?: string;
};

type ClosedPos = OpenPos & {
  closed_at?: string;
  close_reason?: string;
  exit_premium?: number;
  exit_spot?: number;
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
  const [busyCloseId, setBusyCloseId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

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

  const closePosition = async (position: OpenPos) => {
    if (!position.position_id || busyCloseId) return;
    const label = `${position.underlying ?? "position"} ${position.option_type ?? ""} ${position.strike ?? ""}`;
    if (typeof window !== "undefined" && !window.confirm(`Close ${label} in the directional paper book?`)) {
      return;
    }
    setBusyCloseId(position.position_id);
    setActionError(null);
    try {
      await apiClient.post("/api/directional-options/paper-positions/close", {
        position_id: position.position_id,
        reason: "operator_close",
        actor: "ui",
      });
      await paper.refreshAll();
      setView("closed");
    } catch (error) {
      setActionError(describeApiError(error, "Could not close directional paper position."));
    } finally {
      setBusyCloseId(null);
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
          <MetricTile label="Equity" value={formatMoney(cap.total_equity)} detail={`Init ${formatMoney(cap.initial_capital)}`} />
          <MetricTile label="Available" value={formatMoney(cap.available_capital)} detail={`Reserved ${formatMoney(cap.reserved_margin)}`} />
          <MetricTile label="Realized" value={formatSignedMoney(cap.realized_pnl)} color={tone(cap.realized_pnl)} detail={`${cap.total_trades ?? 0} closed`} />
          <MetricTile label="Unrealized" value={formatSignedMoney(cap.unrealized_pnl)} color={tone(cap.unrealized_pnl)} detail={`${cap.open_positions ?? 0} open`} />
          <MetricTile label="Return %" value={cap.total_return_pct != null ? `${cap.total_return_pct.toFixed(3)}%` : "—"} color={tone(cap.total_pnl)} detail={`Sharpe ${formatNumber(cap.sharpe_ratio, 2)}`} />
          <MetricTile label="Win rate" value={cap.win_rate != null ? `${(cap.win_rate * 100).toFixed(1)}%` : "—"} detail={`Max DD ${cap.max_drawdown != null ? (cap.max_drawdown * 100).toFixed(2) + "%" : "—"}`} />
        </div>
      </Section>

      <Section title="RL paper controls" icon={<ShieldCheck size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile size="sm" label="Open value" value={formatMoney(cap.open_premium_value)} detail={`${formatPct(cap.open_exposure_pct, 2, { asPercent: true })} of equity`} />
          <MetricTile size="sm" label="Risk budget" value={formatMoney(cap.open_risk_budget)} detail={`${formatNumber(cap.open_risk_R, 2)}R open`} color={tone(cap.open_risk_R)} />
          <MetricTile size="sm" label="Deployed" value={formatPct(cap.capital_deployed_pct, 2, { asPercent: true })} detail={`Entry ${formatMoney(cap.entry_premium_value)}`} />
          <MetricTile size="sm" label="Largest line" value={formatMoney(cap.largest_position_value)} detail={`${formatPct(cap.largest_position_pct, 2, { asPercent: true })} concentration`} />
          <MetricTile size="sm" label="Avg learned R" value={formatNumber(cap.avg_r_multiple, 3)} detail={`${cap.policy_trades ?? 0} RL closes`} color={tone(cap.avg_r_multiple)} />
          <MetricTile size="sm" label="Profit factor" value={formatNumber(cap.profit_factor, 2)} detail={`Best ${formatSignedMoney(cap.best_trade)} · worst ${formatSignedMoney(cap.worst_trade)}`} />
        </div>
      </Section>

      <Section title={`Trades${symbol ? " · " + symbol : ""}`}>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1.5">
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
          <button
            type="button"
            onClick={() => paper.refreshAll()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/12 px-3 py-1.5 text-[11.5px] font-semibold uppercase tracking-[0.12em] text-text-secondary hover:text-text-primary"
          >
            <RefreshCw size={13} />
            Refresh
          </button>
        </div>
        {actionError ? (
          <div className="mb-3 rounded-lg border border-accent-red/30 bg-accent-red/8 px-3 py-2 text-[11.5px] text-accent-red">
            {actionError}
          </div>
        ) : null}

        {view === "open" ? (
          <Table
            rows={opens}
            empty="No open positions."
            columns={[
              { th: "Symbol", render: (p: OpenPos) => p.underlying },
              { th: "Contract", render: (p: OpenPos) => `${p.option_type ?? ""} ${p.strike ?? ""} · ${p.expiry ?? ""}` },
              { th: "Regime", render: (p: OpenPos) => p.regime ?? "—" },
              { th: "Lots", render: (p: OpenPos) => p.quantity_lots ?? 0, align: "right" },
              { th: "Entry → Mark", render: (p: OpenPos) => `${formatNumber(p.entry_premium, 2)} → ${formatNumber(p.latest_premium, 2)}`, align: "right" },
              { th: "Spot", render: (p: OpenPos) => <SpotCell p={p} />, align: "right", tone: (p: OpenPos) => tone(spotMovePct(p)) },
              { th: "Value", render: (p: OpenPos) => formatMoney(positionValue(p)), align: "right" },
              { th: "Unrealized / R", render: (p: OpenPos) => <div><div>{formatSignedMoney(p.unrealized_pnl)}</div><div className="text-[10px] text-text-muted">{formatNumber(openR(p), 2)}R</div></div>, align: "right", tone: (p: OpenPos) => tone(p.unrealized_pnl) },
              { th: "Policy", render: (p: OpenPos) => <PolicyCell p={p} />, align: "right" },
              { th: "Mark", render: (p: OpenPos) => <MarkSourceCell p={p} /> },
              { th: "Opened", render: (p: OpenPos) => formatIST(p.opened_at), align: "right" },
              { th: "", render: (p: OpenPos) => (
                <button
                  type="button"
                  disabled={busyCloseId === p.position_id}
                  onClick={() => closePosition(p)}
                  className="inline-flex items-center gap-1 rounded-lg border border-accent-red/30 bg-accent-red/8 px-2 py-1 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-accent-red disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <XCircle size={12} />
                  {busyCloseId === p.position_id ? "Closing" : "Close"}
                </button>
              ) },
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
              { th: "Policy", render: (p: ClosedPos) => <PolicyCell p={p} />, align: "right" },
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

function positionValue(p: OpenPos): number {
  return Number(p.latest_premium ?? p.entry_premium ?? 0) * Number(p.quantity_units ?? 0);
}

function openR(p: OpenPos): number | null {
  const denom = Number(p.max_loss || p.risk_budget || 0);
  if (!denom) return null;
  return Number(p.unrealized_pnl || 0) / denom;
}

function spotMovePct(p: OpenPos): number | null {
  const entry = Number(p.entry_spot || 0);
  const latest = Number(p.latest_spot || p.entry_spot || 0);
  if (!entry || !latest) return null;
  return ((latest - entry) / entry) * 100;
}

function SpotCell({ p }: { p: OpenPos }) {
  const latest = p.latest_spot ?? p.entry_spot;
  const move = spotMovePct(p);
  return (
    <div>
      <div>{formatNumber(latest, 2)}</div>
      <div className="text-[10px] text-text-muted">
        {move != null ? `${move >= 0 ? "+" : ""}${move.toFixed(2)}%` : "from fetched spot"}
      </div>
    </div>
  );
}

function MarkSourceCell({ p }: { p: OpenPos }) {
  return (
    <span title={[p.selection_reason, p.mark_time ? `Marked ${formatIST(p.mark_time)}` : ""].filter(Boolean).join(" · ")}>
      {p.price_source || "—"}
    </span>
  );
}

function PolicyCell({ p }: { p: OpenPos }) {
  const blockers = p.ai_rule_blockers || [];
  return (
    <div className="space-y-0.5">
      <div className="inline-flex items-center gap-1 text-text-primary">
        <Target size={11} />
        {p.policy_size_multiplier != null ? `${p.policy_size_multiplier.toFixed(2)}×` : "—"}
      </div>
      <div className="text-[10px] text-text-muted">
        {p.ai_rule_setup || (p.ai_rule_score != null ? `rule ${formatNumber(p.ai_rule_score, 0)}` : "bandit")}
      </div>
      {blockers.length ? (
        <div className="text-[10px] text-accent-amber">{blockers.slice(0, 2).join(", ")}</div>
      ) : null}
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
