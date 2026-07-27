/**
 * book-rows — pure adapters from each lane's RAW book payload to the one row
 * shape the four views render.
 *
 * Two rules govern every function here:
 *
 *   1. A field the payload does not carry stays NULL. It is never coerced to
 *      zero, never defaulted to a plausible value, and never back-filled from a
 *      sibling field that means something else. `Number(null)` is 0 and
 *      `Number("")` is 0, so every read goes through `num()`.
 *
 *   2. Nothing is synthesised across layers. An order row may only come from a
 *      real order/fill-event log; no adapter here turns a trade into an order.
 *
 * Dependency-free (imports only types + helpers from lane-books) so the whole
 * module is assertable under bare `node --test`.
 */
import { type PlanBasis, type Quantity, quantityOf } from "./lane-books.ts";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Raw = any;

export function num(v: unknown): number | null {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function str(v: unknown): string | null {
  if (v == null) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
}

// ─── Row shapes ─────────────────────────────────────────────────────────────

/** A real executed fill event. Only ever built from a real order/fill log. */
export type BookOrderRow = {
  id: string;
  time: string | null;
  symbol: string;
  /** open | close | partial_close — the event that actually executed. */
  action: string;
  direction: string | null;
  price: number | null;
  qty: Quantity;
  /** The lane's own reason string (signal_entry, hard_stop, target1_partial…). */
  reason: string | null;
  /** Realized P&L booked by this event, when the event books one. */
  pnl: number | null;
  lotsRemaining: number | null;
  positionId: string | null;
};

/** A per-cycle accept/decline decision. NOT an order. */
export type BookDecisionRow = {
  id: string;
  time: string | null;
  symbol: string;
  approved: boolean | null;
  reason: string | null;
  executionReady: boolean | null;
  degradedReason: string | null;
  readinessMode: string | null;
  confidence: number | null;
  direction: string | null;
  spotAgeSeconds: number | null;
  watchlistAgeSeconds: number | null;
  /** True when the reason string is a raw stacktrace rather than a decision. */
  reasonIsFault: boolean;
};

/** A logged trading INTENT that cannot be reconciled to a position. */
export type BookIntentRow = {
  id: string;
  time: string | null;
  symbol: string;
  action: string | null;
  agent: string | null;
  executionStyle: string | null;
  confidence: number | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  regime: string | null;
};

export type BookTradeRow = {
  id: string;
  symbol: string;
  underlying: string | null;
  contract: string | null;
  side: string;
  openedAt: string | null;
  closedAt: string | null;
  entry: number | null;
  exit: number | null;
  entrySpot: number | null;
  exitSpot: number | null;
  qty: Quantity;
  /** NET of recorded cost where the book records one; otherwise the book's own figure. */
  realized: number | null;
  /** Present only when the book separately records a gross figure. */
  realizedGross: number | null;
  /** Recorded transaction cost. NULL means the book records none — not zero. */
  cost: number | null;
  slippage: number | null;
  rMultiple: number | null;
  exitReason: string | null;
  stop: number | null;
  target: number | null;
  /** Which scale `stop` / `target` are quoted on. See PlanBasis. */
  planBasis: PlanBasis;
  /**
   * True when the position was closed in MORE THAN ONE exit, so
   * `entry → exit × quantity` deliberately does NOT reconcile with `realized`.
   * Null when the book records nothing either way.
   */
  partialExit: boolean | null;
  /** Lots still open at the final exit, when the book records it. */
  lotsAtFinalExit: number | null;
  sessionDate: string | null;
  regime: string | null;
  notes: string[];
};

export type BookPositionRow = {
  id: string;
  symbol: string;
  underlying: string | null;
  contract: string | null;
  side: string;
  openedAt: string | null;
  entry: number | null;
  entrySpot: number | null;
  qty: Quantity;
  mark: number | null;
  /** The timestamp the mark is as-of, on the clock named by `markClock`. */
  markAsOf: string | null;
  markClock: "mark" | "book_sync";
  markSource: string | null;
  /** The book's own unrealised figure. Suppressed by the view when the mark is stale. */
  unrealized: number | null;
  stop: number | null;
  target: number | null;
  /** Which scale `stop` / `target` are quoted on. See PlanBasis. */
  planBasis: PlanBasis;
  /**
   * The entry price on the SAME scale as `stop` / `target` — the only entry an
   * R/R may be computed against. For an underlying-level plan on an option row
   * that is the entry SPOT, never the premium.
   */
  planEntry: number | null;
  expiry: string | null;
  regime: string | null;
  reason: string | null;
  notes: string[];
};

// ─── directional_options (PG directional_paper_positions) ───────────────────

const dirSymbol = (r: Raw): string => str(r?.trading_symbol) ?? str(r?.instrument_key) ?? str(r?.underlying) ?? "—";

export function directionalTrade(r: Raw): BookTradeRow {
  const gross = num(r?.realized_pnl_gross);
  const net = num(r?.realized_pnl);
  return {
    id: str(r?.position_id) ?? `${dirSymbol(r)}-${str(r?.closed_at) ?? ""}`,
    symbol: dirSymbol(r),
    underlying: str(r?.underlying),
    contract: [str(r?.option_type), num(r?.strike), str(r?.expiry)].filter(Boolean).join(" "),
    side: str(r?.direction) ?? str(r?.option_type) ?? "—",
    openedAt: str(r?.opened_at),
    closedAt: str(r?.closed_at),
    entry: num(r?.entry_premium),
    exit: num(r?.exit_premium),
    entrySpot: num(r?.entry_spot),
    exitSpot: num(r?.exit_spot),
    qty: quantityOf({ lots: num(r?.quantity_lots), units: num(r?.quantity_units) }),
    realized: net,
    realizedGross: gross,
    cost: num(r?.transaction_cost),
    slippage: num(r?.slippage_cost),
    rMultiple: num(r?.policy_r_multiple),
    exitReason: str(r?.close_reason),
    // `stop_underlying` is an UNDERLYING level, not a premium level.
    stop: num(r?.stop_underlying),
    target: null,
    planBasis: "underlying",
    partialExit: null,
    lotsAtFinalExit: null,
    sessionDate: null,
    regime: str(r?.regime),
    notes: [str(r?.selection_reason)].filter((x): x is string => Boolean(x)),
  };
}

export function directionalPosition(r: Raw): BookPositionRow {
  return {
    id: str(r?.position_id) ?? dirSymbol(r),
    symbol: dirSymbol(r),
    underlying: str(r?.underlying),
    contract: [str(r?.option_type), num(r?.strike), str(r?.expiry)].filter(Boolean).join(" "),
    side: str(r?.direction) ?? str(r?.option_type) ?? "—",
    openedAt: str(r?.opened_at),
    entry: num(r?.entry_premium),
    entrySpot: num(r?.entry_spot),
    qty: quantityOf({ lots: num(r?.quantity_lots), units: num(r?.quantity_units) }),
    mark: num(r?.latest_premium),
    // A REAL per-position mark clock: mark_time, not the row's updated_at.
    markAsOf: str(r?.mark_time),
    markClock: "mark",
    markSource: str(r?.price_source),
    unrealized: num(r?.unrealized_pnl),
    stop: num(r?.stop_underlying),
    target: null,
    // The stop this lane records is an UNDERLYING level, so the only entry it
    // may be measured against is the entry SPOT — not the option premium.
    planBasis: "underlying",
    planEntry: num(r?.entry_spot),
    expiry: str(r?.expiry),
    regime: str(r?.regime),
    reason: str(r?.selection_reason),
    notes: [],
  };
}

/** A raw asyncpg traceback in `selection_reason` is a FAULT, not a decision. */
function looksLikeStacktrace(reason: string | null): boolean {
  if (!reason) return false;
  return /Traceback|asyncpg|\bError\b.*\n|at [\w.]+\(/.test(reason) || reason.length > 400;
}

export function directionalDecision(r: Raw, index: number): BookDecisionRow {
  const ds = r?.data_status ?? {};
  const reason = str(r?.selection_reason);
  return {
    id: `${str(r?.recorded_at) ?? "t"}-${str(r?.underlying) ?? "s"}-${index}`,
    time: str(r?.recorded_at),
    symbol: str(r?.trading_symbol) ?? str(r?.underlying) ?? "—",
    approved: typeof r?.approved === "boolean" ? r.approved : null,
    reason,
    executionReady:
      typeof r?.execution_ready === "boolean"
        ? r.execution_ready
        : typeof ds?.execution_ready === "boolean"
          ? ds.execution_ready
          : null,
    degradedReason: str(ds?.degraded_reason),
    readinessMode: str(ds?.readiness_mode),
    confidence: num(r?.confidence),
    direction: str(r?.direction),
    spotAgeSeconds: num(ds?.spot_age_seconds),
    watchlistAgeSeconds: num(ds?.watchlist_age_seconds),
    reasonIsFault: looksLikeStacktrace(reason),
  };
}

// ─── auction_intelligence NSE (runtime paper_positions.json) ────────────────

export function auctionTrade(r: Raw): BookTradeRow {
  return {
    id: str(r?.position_id) ?? `${str(r?.symbol)}-${str(r?.closed_at) ?? ""}`,
    symbol: str(r?.trading_symbol) ?? str(r?.symbol) ?? "—",
    underlying: str(r?.underlying_symbol),
    contract: [str(r?.option_type), num(r?.strike), str(r?.expiry)].filter(Boolean).join(" "),
    side: str(r?.signal_action) ?? str(r?.instrument_type) ?? "—",
    openedAt: str(r?.opened_at),
    closedAt: str(r?.closed_at),
    entry: num(r?.entry_premium),
    exit: num(r?.exit_premium),
    entrySpot: num(r?.entry_spot_price),
    exitSpot: num(r?.exit_spot_price),
    qty: quantityOf({ units: num(r?.quantity), lotSize: num(r?.lot_size) }),
    realized: num(r?.realized_pnl),
    // This book records no gross/net split because it records no cost at all.
    realizedGross: null,
    cost: null,
    slippage: null,
    rMultiple: null,
    exitReason: str(r?.close_reason),
    // stop_price / target_price on this book are UNDERLYING index levels while
    // entry_premium is the option price. They are not on the same scale.
    stop: num(r?.stop_price),
    target: num(r?.target_price),
    planBasis: "underlying",
    partialExit: null,
    lotsAtFinalExit: null,
    sessionDate: null,
    regime: str(r?.regime_entry),
    notes: Array.isArray(r?.notes) ? r.notes.map(String) : [],
  };
}

export function auctionPosition(r: Raw, bookSyncedAt: string | null): BookPositionRow {
  return {
    id: str(r?.position_id) ?? `${str(r?.symbol)}`,
    symbol: str(r?.trading_symbol) ?? str(r?.symbol) ?? "—",
    underlying: str(r?.underlying_symbol),
    contract: [str(r?.option_type), num(r?.strike), str(r?.expiry)].filter(Boolean).join(" "),
    side: str(r?.signal_action) ?? str(r?.instrument_type) ?? "—",
    openedAt: str(r?.opened_at),
    entry: num(r?.entry_premium),
    entrySpot: num(r?.entry_spot_price),
    qty: quantityOf({ units: num(r?.quantity), lotSize: num(r?.lot_size) }),
    mark: num(r?.latest_premium),
    // There is NO mark_time on this book. The honest clock is the row's own
    // updated_at, falling back to the book's last_synced_at, and it is labelled
    // book-sync age rather than mark age so nobody reads it as a tick clock.
    markAsOf: str(r?.updated_at) ?? bookSyncedAt,
    markClock: "book_sync",
    markSource: null,
    unrealized: num(r?.unrealized_pnl),
    stop: num(r?.stop_price),
    target: num(r?.target_price),
    // stop_price / target_price are UNDERLYING index levels. Measuring them
    // against entry_premium (29.65 vs a 23,794 stop) yields 0.99R for a plan
    // that is really 2.19R — so the R/R entry here is the entry SPOT.
    planBasis: "underlying",
    planEntry: num(r?.entry_spot_price),
    expiry: str(r?.expiry),
    regime: str(r?.regime_last) ?? str(r?.regime_entry),
    reason: str(r?.selection_reason),
    notes: Array.isArray(r?.notes) ? r.notes.map(String) : [],
  };
}

export function auctionIntent(r: Raw, index: number): BookIntentRow {
  const plan = r?.execution_plan ?? r?.plan ?? r ?? {};
  return {
    id: `${str(r?.recorded_at) ?? "t"}-${index}`,
    time: str(r?.recorded_at) ?? str(r?.timestamp),
    symbol: str(r?.symbol) ?? str(r?.underlying) ?? "—",
    action: str(r?.action) ?? str(r?.signal_action),
    agent: str(r?.agent_name) ?? str(r?.agent),
    executionStyle: str(r?.execution_style) ?? str(plan?.execution_style),
    confidence: num(r?.confidence),
    entry: num(r?.entry ?? plan?.entry ?? r?.entry_price),
    stop: num(r?.stop ?? plan?.stop ?? r?.stop_price),
    target: num(r?.target ?? plan?.target ?? r?.target_price),
    regime: str(r?.regime),
  };
}

// ─── MCX / futures fill-event books (auction MCX + both IC books) ───────────

export function futuresOrder(r: Raw, index: number): BookOrderRow {
  return {
    id: `${str(r?.position_id) ?? "p"}-${str(r?.time) ?? index}-${index}`,
    time: str(r?.time),
    symbol: str(r?.symbol) ?? "—",
    action: str(r?.action) ?? "—",
    direction: str(r?.direction),
    price: num(r?.price),
    qty: quantityOf({ lots: num(r?.lots), lotSize: num(r?.lot_size) }),
    reason: str(r?.reason),
    pnl: num(r?.pnl),
    lotsRemaining: num(r?.lots_remaining),
    positionId: str(r?.position_id),
  };
}

export function futuresTrade(r: Raw): BookTradeRow {
  // A partial close books P&L at more than one exit price, so the row's single
  // `entry → exit` pair times its quantity will NOT equal `realized`. Silently
  // showing both and letting a reader assume they reconcile is a small lie; the
  // fact is recorded, so it is carried and flagged instead.
  const initialLots = num(r?.initial_lots);
  const finalLots = num(r?.lots);
  const partialExit =
    initialLots != null && finalLots != null
      ? initialLots !== finalLots || r?.target1_done === true
      : r?.target1_done === true
        ? true
        : null;
  return {
    id: str(r?.position_id) ?? `${str(r?.symbol)}-${str(r?.closed_at) ?? ""}`,
    symbol: str(r?.symbol) ?? "—",
    underlying: str(r?.symbol),
    contract: str(r?.futures_contract),
    side: str(r?.direction) ?? "—",
    openedAt: str(r?.opened_at),
    closedAt: str(r?.closed_at),
    entry: num(r?.entry_price),
    exit: num(r?.exit_price),
    entrySpot: null,
    exitSpot: null,
    qty: quantityOf({ lots: initialLots ?? finalLots, lotSize: num(r?.lot_size) }),
    realized: num(r?.realized_pnl) ?? num(r?.pnl),
    realizedGross: null,
    cost: null,
    slippage: null,
    rMultiple: num(r?.r_multiple),
    exitReason: str(r?.exit_reason),
    stop: num(r?.initial_stop) ?? num(r?.stop),
    target: num(r?.target1),
    // A futures book quotes entry, stop and target on the one instrument.
    planBasis: "instrument",
    partialExit,
    lotsAtFinalExit: partialExit ? finalLots : null,
    sessionDate: str(r?.session_date),
    regime: null,
    notes: [],
  };
}

export function futuresPosition(r: Raw): BookPositionRow {
  return {
    id: str(r?.position_id) ?? `${str(r?.symbol)}`,
    symbol: str(r?.symbol) ?? "—",
    underlying: str(r?.symbol),
    contract: str(r?.futures_contract),
    side: str(r?.direction) ?? "—",
    openedAt: str(r?.opened_at),
    entry: num(r?.entry_price),
    entrySpot: null,
    qty: quantityOf({ lots: num(r?.lots), lotSize: num(r?.lot_size) }),
    mark: num(r?.current_price),
    markAsOf: str(r?.updated_at),
    markClock: "book_sync",
    markSource: null,
    unrealized: num(r?.unrealized_pnl),
    stop: num(r?.stop),
    target: num(r?.target1_done ? r?.target2 : r?.target1),
    planBasis: "instrument",
    planEntry: num(r?.entry_price),
    // A futures contract LABEL is not a date, so there is no expiry to carry.
    expiry: null,
    regime: null,
    reason: str(r?.setup_id),
    notes: [],
  };
}

// ─── Aggregations used by the portfolio view ────────────────────────────────

/**
 * Notional exposure of the open rows. Returns null when NOTHING can be
 * measured, and reports how many rows were skipped for want of a price or a
 * unit count — a partial sum that pretends to be complete is the same lie in a
 * smaller font.
 */
export function notionalExposure(rows: BookPositionRow[]): { value: number | null; counted: number; skipped: number } {
  let value = 0;
  let counted = 0;
  let skipped = 0;
  for (const r of rows) {
    const price = r.mark ?? r.entry;
    const units = r.qty.units;
    if (price == null || units == null) {
      skipped += 1;
      continue;
    }
    value += Math.abs(price * units);
    counted += 1;
  }
  return { value: counted ? value : null, counted, skipped };
}

/** Sum a nullable field, reporting how many rows carried it. Never sums nulls. */
export function sumPresent(values: (number | null)[]): { total: number | null; present: number; absent: number } {
  let total = 0;
  let present = 0;
  let absent = 0;
  for (const v of values) {
    if (v == null || !Number.isFinite(v)) absent += 1;
    else {
      total += v;
      present += 1;
    }
  }
  return { total: present ? total : null, present, absent };
}
