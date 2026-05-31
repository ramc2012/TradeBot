"use client";

/**
 * Paper Trading tab — full visibility into the directional book.
 *
 * Three sections:
 *   1. Capital summary tile-strip (initial / realized / unrealized / equity / DD)
 *   2. Open positions table — quantity, premium, unrealized P&L, policy multiplier
 *   3. Closed positions + recent journal entries (interleavable by tab)
 *
 * Each row in open/closed surfaces the policy attribution fields so it's
 * easy to see WHY a position is the size it is and WHAT R-multiple it
 * delivered. The min-hold guard and one-position-per-symbol are visible
 * in the strategy params panel; here we just render outcomes.
 */
import { clsx } from "clsx";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Banknote, BookOpen, History, Layers3 } from "lucide-react";

import {
  api as apiClient,
  getDirectionalOptionsPaperJournal,
  getDirectionalOptionsPaperPositions,
  getDirectionalOptionsPaperSummary,
} from "@/lib/api";

type CapitalSummary = {
  initial_capital?: number;
  available_capital?: number;
  reserved_margin?: number;
  total_equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  total_pnl?: number;
  total_return_pct?: number;
  max_drawdown?: number;
  sharpe_ratio?: number;
  total_trades?: number;
  win_rate?: number;
  open_positions?: number;
  closed_positions?: number;
};

type OpenPosition = {
  position_id: string;
  underlying?: string;
  direction?: string;
  regime?: string;
  trading_symbol?: string;
  option_type?: string;
  strike?: number;
  expiry?: string;
  expiry_kind?: string;
  quantity_lots?: number;
  quantity_units?: number;
  entry_premium?: number;
  latest_premium?: number;
  unrealized_pnl?: number;
  confidence?: number;
  opened_at?: string;
  updated_at?: string;
  policy_size_multiplier?: number;
  policy_sampled_value?: number;
  policy_n_seen_at_open?: number;
};

type ClosedPosition = OpenPosition & {
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
  execution_ready?: boolean;
  trading_symbol?: string | null;
  strike?: number | null;
  option_type?: string | null;
  latest_premium?: number | null;
  selection_reason?: string;
};

function fmtMoney(v?: number | null, signed = false, digits = 0): string {
  if (v == null || Number.isNaN(v)) return "—";
  const prefix = signed && v >= 0 ? "+" : "";
  return `${prefix}₹${v.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function fmtNum(v?: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

function fmtTime(v?: string | null): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function pnlTone(v?: number | null): string {
  if (v == null) return "text-text-muted";
  if (v > 0) return "text-emerald-300";
  if (v < 0) return "text-rose-300";
  return "text-text-secondary";
}

function Tile({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
      <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-lg font-semibold", accent || "text-text-primary")}>
        {value}
      </div>
      {sub ? <div className="mt-0.5 text-[11px] text-text-muted">{sub}</div> : null}
    </div>
  );
}

const REFRESH_MS = 15_000;

export default function PaperTradingTab({ symbol }: { symbol?: string | null }) {
  const [activeView, setActiveView] = useState<"open" | "closed" | "journal">("open");

  const summaryQuery = useQuery({
    queryKey: ["do-paper-summary"],
    queryFn: async () => (await getDirectionalOptionsPaperSummary()).data as CapitalSummary,
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: false,
  });

  const positionsQuery = useQuery({
    queryKey: ["do-paper-positions", symbol || "all"],
    queryFn: async () =>
      (
        await getDirectionalOptionsPaperPositions(symbol || undefined, "all", 50)
      ).data as {
        open_positions?: OpenPosition[];
        closed_positions?: ClosedPosition[];
      },
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: false,
  });

  const journalQuery = useQuery({
    queryKey: ["do-paper-journal", symbol || "all"],
    queryFn: async () =>
      (
        await getDirectionalOptionsPaperJournal(symbol || undefined, 50)
      ).data as { records?: JournalEntry[] },
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: false,
  });

  const cap = summaryQuery.data || {};
  const openPositions = positionsQuery.data?.open_positions || [];
  const closedPositions = positionsQuery.data?.closed_positions || [];
  const journalRecords = journalQuery.data?.records || [];

  const resetMutation = async () => {
    if (typeof window === "undefined" || !window.confirm("Reset the directional paper book? This archives current state and restores the funded baseline.")) {
      return;
    }
    try {
      await apiClient.post("/api/directional-options/reset-paper", { confirm: "RESET", actor: "user" });
      await Promise.all([summaryQuery.refetch(), positionsQuery.refetch(), journalQuery.refetch()]);
    } catch (e) {
      window.alert(`Reset failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="space-y-5">
      {/* Capital tiles */}
      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Banknote size={16} />
            Paper Book {symbol ? `· ${symbol}` : "· all symbols"}
          </div>
          <button
            type="button"
            onClick={() => void resetMutation()}
            className="rounded-xl border border-rose-500/30 bg-rose-500/8 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-rose-300 hover:bg-rose-500/15"
          >
            Reset book
          </button>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <Tile label="Equity" value={fmtMoney(cap.total_equity)} sub={`Init ${fmtMoney(cap.initial_capital)}`} />
          <Tile
            label="Available"
            value={fmtMoney(cap.available_capital)}
            sub={`Reserved ${fmtMoney(cap.reserved_margin)}`}
          />
          <Tile
            label="Realized"
            value={fmtMoney(cap.realized_pnl, true)}
            accent={pnlTone(cap.realized_pnl)}
            sub={`${cap.total_trades ?? 0} closed`}
          />
          <Tile
            label="Unrealized"
            value={fmtMoney(cap.unrealized_pnl, true)}
            accent={pnlTone(cap.unrealized_pnl)}
            sub={`${cap.open_positions ?? 0} open`}
          />
          <Tile
            label="Total return"
            value={cap.total_return_pct != null ? `${cap.total_return_pct.toFixed(3)}%` : "—"}
            accent={pnlTone(cap.total_pnl)}
            sub={`PF Sharpe ${fmtNum(cap.sharpe_ratio, 2)}`}
          />
          <Tile
            label="Win rate"
            value={cap.win_rate != null ? `${(cap.win_rate * 100).toFixed(1)}%` : "—"}
            sub={`Max DD ${cap.max_drawdown != null ? (cap.max_drawdown * 100).toFixed(2) + "%" : "—"}`}
          />
        </div>
      </section>

      {/* Sub-tab selector */}
      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-3 flex items-center gap-1.5">
          {([
            { k: "open", label: `Open (${openPositions.length})`, icon: Layers3 },
            { k: "closed", label: `Closed (${closedPositions.length})`, icon: History },
            { k: "journal", label: `Journal (${journalRecords.length})`, icon: BookOpen },
          ] as const).map(({ k, label, icon: Icon }) => (
            <button
              key={k}
              type="button"
              onClick={() => setActiveView(k)}
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-[11.5px] font-semibold uppercase tracking-[0.12em] transition-colors",
                activeView === k
                  ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                  : "border-bg-border bg-bg-primary/12 text-text-secondary hover:text-text-primary",
              )}
            >
              <Icon size={13} />
              {label}
            </button>
          ))}
        </div>

        {activeView === "open" ? (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/60">
                  <th className="px-2 py-2 text-left">Symbol</th>
                  <th className="px-2 py-2 text-left">Contract</th>
                  <th className="px-2 py-2 text-left">Regime</th>
                  <th className="px-2 py-2 text-right">Qty</th>
                  <th className="px-2 py-2 text-right">Entry / Mark</th>
                  <th className="px-2 py-2 text-right">Unreal. PnL</th>
                  <th className="px-2 py-2 text-right">Size mult</th>
                  <th className="px-2 py-2 text-right">Opened</th>
                </tr>
              </thead>
              <tbody>
                {openPositions.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="py-6 text-center text-text-muted">
                      No open positions. The engine will fire as soon as a signal clears the policy.
                    </td>
                  </tr>
                ) : (
                  openPositions.map((p) => (
                    <tr key={p.position_id} className="border-b border-bg-border/30">
                      <td className="px-2 py-2 font-semibold text-text-primary">{p.underlying || "—"}</td>
                      <td className="px-2 py-2 font-mono text-[11.5px]">
                        {p.option_type || ""} {p.strike ?? "—"}
                        <div className="text-[10px] text-text-muted">
                          {p.expiry || "—"} {p.expiry_kind ? `· ${p.expiry_kind}` : ""}
                        </div>
                      </td>
                      <td className="px-2 py-2 text-text-secondary">{p.regime || "—"}</td>
                      <td className="px-2 py-2 text-right font-mono">
                        {p.quantity_lots ?? 0}L × {(p.quantity_units ?? 0) / Math.max(p.quantity_lots ?? 1, 1)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono">
                        {fmtMoney(p.entry_premium, false, 2)} → {fmtMoney(p.latest_premium, false, 2)}
                      </td>
                      <td className={clsx("px-2 py-2 text-right font-mono", pnlTone(p.unrealized_pnl))}>
                        {fmtMoney(p.unrealized_pnl, true)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono">
                        {p.policy_size_multiplier != null ? `${p.policy_size_multiplier.toFixed(2)}×` : "—"}
                      </td>
                      <td className="px-2 py-2 text-right text-[11px] text-text-muted">
                        {fmtTime(p.opened_at)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        ) : null}

        {activeView === "closed" ? (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/60">
                  <th className="px-2 py-2 text-left">Symbol</th>
                  <th className="px-2 py-2 text-left">Contract</th>
                  <th className="px-2 py-2 text-left">Regime</th>
                  <th className="px-2 py-2 text-right">Entry / Exit</th>
                  <th className="px-2 py-2 text-right">Realized PnL</th>
                  <th className="px-2 py-2 text-right">Size mult</th>
                  <th className="px-2 py-2 text-right">R-multiple</th>
                  <th className="px-2 py-2 text-left">Close reason</th>
                  <th className="px-2 py-2 text-right">Closed</th>
                </tr>
              </thead>
              <tbody>
                {closedPositions.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-6 text-center text-text-muted">
                      No closed positions yet.
                    </td>
                  </tr>
                ) : (
                  closedPositions.map((p) => (
                    <tr key={p.position_id} className="border-b border-bg-border/30">
                      <td className="px-2 py-2 font-semibold text-text-primary">{p.underlying || "—"}</td>
                      <td className="px-2 py-2 font-mono text-[11.5px]">
                        {p.option_type || ""} {p.strike ?? "—"}
                        <div className="text-[10px] text-text-muted">
                          {p.expiry || "—"}
                        </div>
                      </td>
                      <td className="px-2 py-2 text-text-secondary">{p.regime || "—"}</td>
                      <td className="px-2 py-2 text-right font-mono">
                        {fmtMoney(p.entry_premium, false, 2)} → {fmtMoney(p.exit_premium, false, 2)}
                      </td>
                      <td className={clsx("px-2 py-2 text-right font-mono", pnlTone(p.realized_pnl))}>
                        {fmtMoney(p.realized_pnl, true)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono">
                        {p.policy_size_multiplier != null ? `${p.policy_size_multiplier.toFixed(2)}×` : "—"}
                      </td>
                      <td className={clsx("px-2 py-2 text-right font-mono font-semibold", pnlTone(p.policy_r_multiple))}>
                        {p.policy_r_multiple != null ? `${p.policy_r_multiple >= 0 ? "+" : ""}${p.policy_r_multiple.toFixed(2)}R` : "—"}
                      </td>
                      <td className="px-2 py-2 text-[11px] text-text-secondary">{p.close_reason || "—"}</td>
                      <td className="px-2 py-2 text-right text-[11px] text-text-muted">
                        {fmtTime(p.closed_at)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        ) : null}

        {activeView === "journal" ? (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/60">
                  <th className="px-2 py-2 text-left">Time</th>
                  <th className="px-2 py-2 text-left">Symbol</th>
                  <th className="px-2 py-2 text-left">Regime</th>
                  <th className="px-2 py-2 text-left">Direction</th>
                  <th className="px-2 py-2 text-right">Conf</th>
                  <th className="px-2 py-2 text-left">Approved</th>
                  <th className="px-2 py-2 text-left">Reason</th>
                </tr>
              </thead>
              <tbody>
                {journalRecords.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="py-6 text-center text-text-muted">
                      No journal entries yet.
                    </td>
                  </tr>
                ) : (
                  journalRecords.slice(0, 30).map((r, idx) => (
                    <tr key={`${r.recorded_at}-${idx}`} className="border-b border-bg-border/30">
                      <td className="px-2 py-2 text-[10.5px] text-text-muted">{fmtTime(r.recorded_at)}</td>
                      <td className="px-2 py-2 font-semibold text-text-primary">{r.underlying || "—"}</td>
                      <td className="px-2 py-2 text-text-secondary">{r.regime || "—"}</td>
                      <td className={clsx("px-2 py-2 font-mono", r.direction === "CE" ? "text-emerald-300" : r.direction === "PE" ? "text-rose-300" : "text-text-muted")}>
                        {r.direction || "—"}
                      </td>
                      <td className="px-2 py-2 text-right font-mono">{fmtNum(r.confidence, 3)}</td>
                      <td className="px-2 py-2">
                        <span className={clsx(
                          "rounded-full border px-2 py-0.5 text-[10.5px] font-semibold",
                          r.approved
                            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                            : "border-amber-500/40 bg-amber-500/10 text-amber-300",
                        )}>
                          {r.approved ? "ACT" : "SKIP"}
                        </span>
                      </td>
                      <td className="px-2 py-2 text-[10.5px] text-text-secondary line-clamp-1 max-w-[420px]">
                        {r.selection_reason || "—"}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
