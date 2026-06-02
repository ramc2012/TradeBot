/**
 * Reports ledger — cross-desk closed-trade aggregation.
 *
 * The live desk dashboards are now scoped to "today" (2026-06-02). The
 * Reports module is where the FULL lifetime trade history across every
 * desk lives. This lib fetches each desk's closed trades in parallel,
 * normalizes their differing schemas into one common row shape, and
 * returns the combined ledger sorted by exit time (newest first).
 *
 * Every desk fetch is independent + failure-tolerant: one desk being
 * down never blanks the whole report.
 */
import {
  getStrategyAgentStatus,
  getCommodityStrategyStatus,
  getDirectionalOptionsPaperPositions,
  getCBEPaperPositions,
  getAuctionIntelligencePaperPositions,
  getFractalMarketProfilePaperPositions,
} from "@/lib/api";

export type ReportRow = {
  id: string;
  desk: string;
  deskKey: string;
  symbol: string;
  side: string;
  qty: number | null;
  entryPrice: number | null;
  exitPrice: number | null;
  pnl: number | null;
  returnPct: number | null;
  entryTime: string | null;
  exitTime: string | null;
  reason: string;
  contract: string;
};

const num = (v: unknown): number | null => {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

const str = (...vals: unknown[]): string => {
  for (const v of vals) {
    if (v != null && v !== "") return String(v);
  }
  return "";
};

const firstNum = (row: Record<string, unknown>, keys: string[]): number | null => {
  for (const k of keys) {
    const n = num(row[k]);
    if (n != null) return n;
  }
  return null;
};

/** Pull the closed-trade array out of whatever wrapper a desk returns. */
function extractRows(payload: unknown, deskKey: string): Record<string, unknown>[] {
  if (!payload) return [];
  const p = payload as Record<string, unknown>;
  // NSE / commodity status endpoints expose split trade history.
  if (deskKey === "nse" || deskKey === "commodity") {
    const lane = deskKey === "nse"
      ? ((p.strategies as Record<string, unknown>[] | undefined) || []).find(
          (s) => (s as Record<string, unknown>).key === "macd_strategy",
        ) || (p.strategies as Record<string, unknown>[] | undefined)?.[0]
      : p;
    const src = (lane as Record<string, unknown>) || {};
    const hist = (src.trade_history || src.historical_trades || src.today_trades) as unknown[] | undefined;
    return Array.isArray(hist) ? (hist as Record<string, unknown>[]) : [];
  }
  // Paper-position desks expose closed_positions.
  const closed = (p.closed_positions || p.closed || p.positions) as unknown[] | undefined;
  if (Array.isArray(closed)) return closed as Record<string, unknown>[];
  if (Array.isArray(payload)) return payload as Record<string, unknown>[];
  return [];
}

function normalize(row: Record<string, unknown>, desk: string, deskKey: string, idx: number): ReportRow | null {
  const pnl = firstNum(row, ["pnl", "realized_pnl", "net_pnl", "realised_pnl"]);
  const exitTime = str(row.exit_time, row.closed_at, row.recorded_at, row.updated_at) || null;
  const entryTime = str(row.entry_time, row.opened_at, row.entered_at) || null;
  // A row with neither a P&L nor an exit timestamp isn't a closed trade.
  if (pnl == null && !exitTime) return null;

  const symbolRaw = str(row.underlying, row.symbol, row.instrument, row.trading_symbol) || "—";
  const symbol = symbolRaw.includes(":") ? symbolRaw.split(":").pop() || symbolRaw : symbolRaw;
  const side = str(row.option_type, row.action, row.side, row.direction).toUpperCase() || "—";
  const entryPrice = firstNum(row, ["entry_price", "entry_premium", "avg_entry", "avg_price"]);
  const exitPrice = firstNum(row, ["exit_price", "exit_premium", "mark_price", "latest_close", "close_price"]);
  const qty = firstNum(row, ["qty", "quantity", "lots"]);
  let returnPct = firstNum(row, ["return_pct", "return_percent", "pnl_pct"]);
  if (returnPct == null && pnl != null && entryPrice != null && qty != null && entryPrice * qty !== 0) {
    returnPct = (pnl / Math.abs(entryPrice * qty)) * 100;
  }
  const reason = str(row.close_reason, row.exit_reason, row.reason, row.signal_reason, row.pending_close_reason) || "—";

  const strike = str(row.strike);
  const optType = str(row.option_type);
  const expiry = str(row.expiry);
  const contract = [optType, strike, expiry].filter(Boolean).join(" ") || symbol;

  return {
    id: `${deskKey}-${symbol}-${exitTime || entryTime || idx}-${idx}`,
    desk,
    deskKey,
    symbol,
    side,
    qty,
    entryPrice,
    exitPrice,
    pnl,
    returnPct,
    entryTime,
    exitTime,
    reason: reason.replaceAll("_", " "),
    contract: contract.replaceAll("_", " "),
  };
}

type DeskAdapter = {
  key: string;
  label: string;
  fetch: () => Promise<{ data: unknown }>;
};

const DESKS: DeskAdapter[] = [
  { key: "nse", label: "NSE S1", fetch: () => getStrategyAgentStatus() },
  { key: "commodity", label: "Commodity", fetch: () => getCommodityStrategyStatus() },
  { key: "directional", label: "Directional", fetch: () => getDirectionalOptionsPaperPositions(undefined, "closed", 500) },
  { key: "cbe", label: "CBE", fetch: () => getCBEPaperPositions("closed", 500) },
  { key: "auction", label: "Auction IQ", fetch: () => getAuctionIntelligencePaperPositions(undefined, "closed", 500) },
  { key: "fractal", label: "Fractal MP", fetch: () => getFractalMarketProfilePaperPositions(undefined, "closed", 500) },
];

export const REPORT_DESKS = DESKS.map((d) => ({ key: d.key, label: d.label }));

export async function fetchReportsLedger(): Promise<{ rows: ReportRow[]; errors: Record<string, string> }> {
  const errors: Record<string, string> = {};
  const settled = await Promise.allSettled(
    DESKS.map(async (desk) => {
      const res = await desk.fetch();
      const raw = extractRows(res.data, desk.key);
      return raw
        .map((row, idx) => normalize(row, desk.label, desk.key, idx))
        .filter((r): r is ReportRow => r !== null);
    }),
  );

  const rows: ReportRow[] = [];
  settled.forEach((result, i) => {
    if (result.status === "fulfilled") {
      rows.push(...result.value);
    } else {
      errors[DESKS[i].label] = String(result.reason?.message || result.reason || "fetch failed");
    }
  });

  rows.sort((a, b) => String(b.exitTime || "").localeCompare(String(a.exitTime || "")));
  return { rows, errors };
}

export function rowsToCsv(rows: ReportRow[]): string {
  const header = ["Desk", "Symbol", "Contract", "Side", "Qty", "Entry", "Exit", "P&L", "Return%", "Entered", "Exited", "Reason"];
  const escape = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  const lines = rows.map((r) =>
    [r.desk, r.symbol, r.contract, r.side, r.qty, r.entryPrice, r.exitPrice, r.pnl, r.returnPct, r.entryTime, r.exitTime, r.reason]
      .map(escape)
      .join(","),
  );
  return [header.join(","), ...lines].join("\n");
}
