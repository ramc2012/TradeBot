/**
 * lane-books — the AUTHORITATIVE-BOOK registry for the order / trade /
 * position / portfolio pages.
 *
 * ─── Why this module exists (2026-07-27) ────────────────────────────────────
 *
 * Five wrong "lane state" calls were made in one week by reading the wrong
 * artifact. `agent_positions` and `agent_signals` are write-only JOURNALS that
 * strand rows at status=open; `backend/runtime/portfolio/daily_*.json` only
 * registers three lanes. Neither is a book. The real book differs per lane —
 * a Postgres table for one, a runtime JSON file for the next — and the only
 * defence against reading the wrong one again is to DECLARE the right one,
 * render the declaration on the page, and unit-test the declaration.
 *
 * So every page states its source. Not "paper book" — the actual table name or
 * the actual repo path, plus the endpoint that serves it.
 *
 * ─── The order-layer question, answered per lane ────────────────────────────
 *
 * These are PAPER lanes. A broker order book — working / cancelled / rejected,
 * with an order id and a venue — does not exist for ANY of them, and the page
 * must never synthesise one out of fills. What DOES exist varies:
 *
 *   · directional_options  → a real DECISION log (approved / declined + reason)
 *   · auction (NSE)        → an intent journal that cannot be reconciled to
 *                            positions, so it is NOT presented as an order book
 *   · auction MCX, IC MCX  → a real FILL-EVENT log (open / close / partial)
 *   · IC NSE               → an order log that exists and is EMPTY: measured
 *                            zero since inception, which is not "missing"
 *
 * ─── Day vs lifetime ────────────────────────────────────────────────────────
 *
 * A dashboard once showed LIFETIME realized P&L under a "today" heading. Every
 * day figure here therefore declares HOW it was obtained, and a served daily
 * series whose newest entry is not today resolves to "no session today" rather
 * than silently presenting a stale day.
 *
 * DEPENDENCY-FREE AT RUNTIME so `node --test` can assert the whole model, like
 * lane-taxonomy, policy-state and nav-model.
 */

// ─── Views ──────────────────────────────────────────────────────────────────

export type BookView = "orders" | "trades" | "positions" | "portfolio";

/** The four views every lane's books page carries, in tab order. */
export const BOOK_VIEWS: BookView[] = ["orders", "trades", "positions", "portfolio"];

export const BOOK_VIEW_LABEL: Record<BookView, string> = {
  orders: "Order book",
  trades: "Trade book",
  positions: "Positions",
  portfolio: "Portfolio",
};

/** The URL search param the books pages key their view on. */
export const BOOK_VIEW_PARAM = "view";

export function isBookView(value: string | null | undefined): value is BookView {
  return BOOK_VIEWS.includes(value as BookView);
}

// ─── Books ──────────────────────────────────────────────────────────────────

export type BookKey =
  | "directional_options"
  | "auction_intelligence"
  | "auction_intelligence_commodity"
  | "institutional_convergence"
  | "institutional_convergence_commodity";

/**
 * What the lane records at order level.
 *
 *   none         — nothing order-shaped exists. The page says so and shows
 *                  fills only. It NEVER fabricates orders from trades.
 *   decision_log — per-cycle accept/decline with a reason. A real decision
 *                  layer, but no order id, no status, no venue.
 *   intent_log   — decisions were logged but cannot be reconciled to positions
 *                  (n intents collapse into one position), so the log is shown
 *                  as context and explicitly NOT as an order book.
 *   fill_events  — a genuine append-only log of executed events (open / close /
 *                  partial_close). Paper fills are instant, so working /
 *                  pending / rejected states never exist.
 */
export type OrderLayer = "none" | "decision_log" | "intent_log" | "fill_events";

export const ORDER_LAYER_LABEL: Record<OrderLayer, string> = {
  none: "No order layer",
  decision_log: "Decision log",
  intent_log: "Intent journal",
  fill_events: "Fill-event log",
};

/** GREEN is reserved for running / healthy-live / actionable-confirmed. */
export const ORDER_LAYER_VARIANT: Record<OrderLayer, "neutral" | "info" | "warn"> = {
  none: "neutral",
  decision_log: "info",
  intent_log: "warn",
  fill_events: "info",
};

export type BookSourceRef = {
  kind: "postgres_table" | "runtime_json";
  /** The table name or the repo-relative path, verbatim. */
  path: string;
  /** Endpoints this page actually calls to read it. */
  servedBy: string[];
  /** One sentence a future reader can check the number against. */
  note: string;
};

/** The optional facts a book may or may not carry. Never guessed. */
export type FieldKey =
  | "fees"
  | "slippage"
  | "exitPlan"
  | "markClock"
  | "dte"
  | "exposure"
  | "rMultiple"
  | "orderStatus";

export type FieldAvailability =
  | { state: "available"; note: string }
  | { state: "partial"; note: string }
  | { state: "unavailable"; reason: string };

/** How a lane's DAY figure is obtained, if it can be obtained at all. */
export type DaySource =
  /** The backend serves a dated daily series; today's row is used or refused. */
  | { mode: "served_daily_series"; note: string }
  /** No day figure is served — derive it by filtering closes on the IST date. */
  | { mode: "derived_from_closes"; note: string }
  /** The lane has never traded, so there is no day figure to derive either. */
  | { mode: "never_traded"; note: string };

export type LaneBook = {
  key: BookKey;
  /** Key in /api/system/lanes — the RUNNING-vs-ARMED source. */
  laneKey: string;
  label: string;
  market: "NSE" | "MCX";
  /** The books route + market query this book is reachable at. */
  route: string;
  /** The parent desk this book belongs to. */
  deskHref: string;
  source: BookSourceRef;
  orderLayer: OrderLayer;
  /** Rendered verbatim on the Order book view. Must name what DOES exist. */
  orderLayerStatement: string;
  fields: Record<FieldKey, FieldAvailability>;
  day: DaySource;
  /** Non-null ⇒ the lane has never fired; that is measured zero, not missing. */
  neverFired: string | null;
};

const NO_BROKER_ORDER_STATUS: FieldAvailability = {
  state: "unavailable",
  reason:
    "Paper fills are instant, so working / pending / cancelled / rejected states never exist. There is no order id and no venue.",
};

export const LANE_BOOKS: Record<BookKey, LaneBook> = {
  directional_options: {
    key: "directional_options",
    laneKey: "directional_options",
    label: "Long Premium · directional options",
    market: "NSE",
    route: "/strategies/directional/books",
    deskHref: "/strategies/directional",
    source: {
      kind: "postgres_table",
      path: "directional_paper_positions (+ directional_paper_journal)",
      servedBy: [
        "/api/directional-options/paper-positions",
        "/api/directional-options/paper-summary",
        "/api/directional-options/paper-journal",
      ],
      note:
        "The authoritative book is the Postgres table directional_paper_positions. `directional_option_trades` is a BACKTEST table with 0 rows and is deliberately not read here; agent_positions is a journal and is never read.",
    },
    orderLayer: "decision_log",
    orderLayerStatement:
      "This lane has NO order layer — only fills. What it does keep is a genuine per-cycle DECISION log (directional_paper_journal): every scan writes approved=true/false with the reason it declined. That is shown below as a decision log. It carries no order id, no order status and no venue, and no order is ever synthesised from a trade.",
    fields: {
      fees: {
        state: "available",
        note: "transaction_cost is recorded on every closed row; realized_pnl is NET of it and realized_pnl_gross is carried alongside.",
      },
      slippage: {
        state: "partial",
        note: "slippage_cost / slippage_pct exist on a minority of rows only. Rows without them render UNAVAILABLE, never 0.",
      },
      // The exits here are a RULE in the lane's own code (a profit-target
      // percentage and a stop threshold in directional_options/paper.py), not
      // stored levels. Only a minority of rows carry stop_underlying / max_loss
      // / premium_at_risk. Rendering R/R for the rest would invent a plan.
      exitPlan: {
        state: "partial",
        note:
          "Most rows carry NO stop and NO target: the exit is a RULE in the lane's own code (a profit-target percentage and a stop threshold), not a stored level. A minority carry stop_underlying / max_loss / premium_at_risk. R/R renders only for those rows and reads UNAVAILABLE for the rest.",
      },
      markClock: {
        state: "available",
        note: "mark_time is a real per-position mark clock, with price_source naming where the mark came from.",
      },
      dte: { state: "available", note: "expiry is carried, so DTE is recomputed from it at render time." },
      exposure: { state: "available", note: "reserved_margin on /paper-summary is a real capital reservation." },
      rMultiple: {
        state: "partial",
        note: "policy_r_multiple is present on most closed rows and absent on a few; absent renders UNAVAILABLE.",
      },
      orderStatus: NO_BROKER_ORDER_STATUS,
    },
    day: {
      mode: "derived_from_closes",
      note:
        "The backend serves opens_today / closes_today / cooldown_skips_today but no day P&L. The day figure below is DERIVED by filtering the closed list on the IST close date, and the closed list is capped by the API at 200 rows per page.",
    },
    neverFired: null,
  },

  auction_intelligence: {
    key: "auction_intelligence",
    laneKey: "auction_intelligence",
    label: "Auction IQ · NSE index",
    market: "NSE",
    route: "/strategies/auction/books?market=NSE",
    deskHref: "/strategies/auction",
    source: {
      kind: "runtime_json",
      path: "backend/runtime/auction_intelligence/paper_positions.json (+ *.jsonl decision logs)",
      servedBy: [
        "/api/auction-intelligence/paper-positions",
        "/api/auction-intelligence/paper-status",
        "/api/auction-intelligence/paper-journal",
      ],
      note:
        "The authoritative book is the runtime JSON position book. The .jsonl files beside it are decision logs, not a book.",
    },
    orderLayer: "intent_log",
    orderLayerStatement:
      "There is NO order book for this lane, and none can be reconstructed. PaperPositionBook carries only open_positions and closed_positions — it has no order_log key at all. The .jsonl decision journal records LONG/SHORT intents with an entry, stop and target, but it logs no FLAT decisions, no rejections and no approved flag, and one symbol's many intents collapse into a single position — so the journal count and the position count are not comparable. It is shown below as intent context, explicitly NOT as an order book.",
    fields: {
      fees: {
        state: "unavailable",
        reason:
          "This book records no brokerage, no taxes and no transaction cost of any kind. realized_pnl is therefore GROSS.",
      },
      slippage: {
        state: "unavailable",
        reason: "No slippage is modelled or recorded on this book.",
      },
      exitPlan: {
        state: "available",
        note: "stop_price and target_price are present on every row, so R/R renders legitimately.",
      },
      markClock: {
        state: "partial",
        note:
          "There is no per-position mark_time. Age is taken from the book's own updated_at / last_synced_at and is labelled BOOK-SYNC age, not mark age.",
      },
      dte: {
        state: "partial",
        note:
          "days_to_expiry on the row is FROZEN at entry and would lie if rendered. DTE is recomputed from `expiry` at render time.",
      },
      exposure: { state: "available", note: "reserved_margin and available_capital are served on the summary." },
      rMultiple: {
        state: "unavailable",
        reason: "This book stores no R-multiple field, and no closed row carries the risk it was sized against.",
      },
      orderStatus: NO_BROKER_ORDER_STATUS,
    },
    day: {
      mode: "derived_from_closes",
      note:
        "No day figure is served. The day figure below is DERIVED by filtering the closed list on the IST close date; the whole closed list fits inside one page, so the derivation is complete.",
    },
    neverFired: null,
  },

  auction_intelligence_commodity: {
    key: "auction_intelligence_commodity",
    laneKey: "auction_intelligence_commodity",
    label: "Auction IQ · MCX commodity",
    market: "MCX",
    route: "/strategies/auction/books?market=MCX",
    deskHref: "/strategies/auction",
    source: {
      kind: "runtime_json",
      path: "backend/runtime/auction_intelligence_commodity/commodity_paper.json",
      servedBy: [
        "/api/auction-intelligence/commodity/paper",
        "/api/auction-intelligence/commodity/status",
      ],
      note:
        "This is the book that held the CRUDEOIL long. It is absent from backend/runtime/portfolio/daily_*.json, which registers only three lanes — that roll-up is not the desk's day P&L and is not read here.",
    },
    orderLayer: "fill_events",
    orderLayerStatement:
      "This lane keeps a real append-only FILL-EVENT log: open, close and partial_close records with price, lots, lot size, reason and position id. It is not a broker order book — paper fills are instant, so no working, pending or rejected state ever exists — and every row below is an event that actually executed.",
    fields: {
      fees: {
        state: "unavailable",
        reason: "This book records no brokerage, taxes or transaction cost. Realized P&L is GROSS.",
      },
      slippage: { state: "unavailable", reason: "No slippage is modelled or recorded on this book." },
      exitPlan: {
        state: "available",
        note: "initial_stop, stop, target1 and target2 are recorded, with target1_done and break_even_at.",
      },
      markClock: {
        state: "partial",
        note:
          "Open rows carry current_price and updated_at from the lane's own mark refresh; there is no separate mark clock, so age is the row's updated_at.",
      },
      dte: {
        state: "unavailable",
        reason:
          "futures_contract (e.g. MCX:GOLD26AUGFUT) is a contract LABEL, not a date. Parsing it into an expiry would manufacture a number, so DTE is not shown.",
      },
      exposure: {
        state: "partial",
        note:
          "There is no reserved-margin field on this book. Only NOTIONAL exposure is derivable from open rows, and it is labelled notional.",
      },
      rMultiple: {
        state: "partial",
        note: "R is served where initial_stop was recorded and returns null otherwise — the backend already refuses to guess.",
      },
      orderStatus: NO_BROKER_ORDER_STATUS,
    },
    day: {
      mode: "served_daily_series",
      note:
        "statistics.daily_pnl[] is a dated series and circuit_breaker.day_pnl is the live day figure. The day tile resolves only when the newest dated row IS today; otherwise it reads 'no session today' rather than showing a stale day.",
    },
    neverFired: null,
  },

  institutional_convergence: {
    key: "institutional_convergence",
    laneKey: "institutional_convergence",
    label: "Convergence · NSE",
    market: "NSE",
    route: "/strategies/institutional-convergence/books?market=NSE",
    deskHref: "/strategies/institutional-convergence",
    source: {
      kind: "runtime_json",
      path: "backend/runtime/institutional_convergence/paper.json",
      servedBy: [
        "/api/institutional-convergence/paper",
        "/api/institutional-convergence/orders",
        "/api/institutional-convergence/trades",
        "/api/institutional-convergence/statistics",
        "/api/institutional-convergence/status",
      ],
      note:
        "A separate file from the MCX book — the two never share capital and are never summed here.",
    },
    orderLayer: "fill_events",
    orderLayerStatement:
      "This lane keeps a real append-only FILL-EVENT log with the same shape as its MCX sibling: open, close and partial_close records with price, lots, lot size, reason and position id. It is not a broker order book — paper fills are instant, so no working, pending or rejected state ever exists.",
    fields: {
      fees: { state: "unavailable", reason: "This book records no brokerage, taxes or transaction cost." },
      slippage: { state: "unavailable", reason: "No slippage is modelled or recorded on this book." },
      exitPlan: {
        state: "available",
        note: "The engine records initial_stop / stop / target1 / target2 on any position it opens.",
      },
      markClock: { state: "partial", note: "Open rows carry current_price and updated_at; there is no separate mark clock." },
      dte: {
        state: "unavailable",
        reason: "Index futures positions carry a contract label, not an expiry date, so DTE cannot be computed.",
      },
      exposure: { state: "partial", note: "No reserved-margin field; only notional exposure is derivable." },
      rMultiple: { state: "partial", note: "R resolves only where initial_stop was recorded." },
      orderStatus: NO_BROKER_ORDER_STATUS,
    },
    day: {
      mode: "derived_from_closes",
      note: "This book serves no dated daily series, so the day figure is derived by bucketing closed_positions on closed_at in IST. Lifetime realized comes from the book's own realized_pnl.",
    },
    // NOT a never-fired lane. This previously asserted the lane had never
    // opened a position and that "capital is untouched at its declared initial
    // capital" — while the live book carried 26 closed trades, realized
    // -Rs 23,71,330 and equity of -Rs 13,71,330 against Rs 10,00,000 initial.
    // A books page must never hardcode a claim about its own contents; the
    // emptiness state has to come from the data. Left null so the page renders
    // whatever the book actually holds.
    neverFired: null,
  },

  institutional_convergence_commodity: {
    key: "institutional_convergence_commodity",
    laneKey: "institutional_convergence_commodity",
    label: "Convergence · MCX",
    market: "MCX",
    route: "/strategies/institutional-convergence/books?market=MCX",
    deskHref: "/strategies/institutional-convergence",
    source: {
      kind: "runtime_json",
      path: "backend/runtime/institutional_convergence/commodity_paper.json",
      servedBy: [
        "/api/institutional-convergence/commodity/paper",
        "/api/institutional-convergence/commodity/orders",
        "/api/institutional-convergence/commodity/trades",
        "/api/institutional-convergence/commodity/statistics",
        "/api/institutional-convergence/commodity/status",
      ],
      note: "A separate file from the NSE book — the two never share capital and are never summed here.",
    },
    orderLayer: "fill_events",
    orderLayerStatement:
      "This lane keeps a real append-only FILL-EVENT log: open, close and partial_close records with price, lots, lot size, reason and position id. It is not a broker order book — paper fills are instant, so no working, pending or rejected state ever exists.",
    fields: {
      fees: { state: "unavailable", reason: "This book records no brokerage, taxes or transaction cost. Realized P&L is GROSS." },
      slippage: { state: "unavailable", reason: "No slippage is modelled or recorded on this book." },
      exitPlan: { state: "available", note: "initial_stop, stop, target1 and target2 are recorded where the engine set them." },
      markClock: { state: "partial", note: "Open rows carry current_price and updated_at; there is no separate mark clock." },
      dte: {
        state: "unavailable",
        reason: "futures_contract is a contract LABEL, not a date. Parsing it into an expiry would manufacture a number.",
      },
      exposure: { state: "partial", note: "No reserved-margin field; only notional exposure is derivable from open rows." },
      rMultiple: {
        state: "partial",
        note: "R is served for the subset of trades that recorded an initial_stop and is null for the rest — the backend refuses to guess.",
      },
      orderStatus: NO_BROKER_ORDER_STATUS,
    },
    day: {
      mode: "served_daily_series",
      note:
        "statistics.daily_pnl[] is a dated series and circuit_breaker.day_pnl is the live day figure. The day tile resolves only when the newest dated row IS today.",
    },
    neverFired: null,
  },
};

export const BOOK_KEYS: BookKey[] = [
  "directional_options",
  "auction_intelligence",
  "auction_intelligence_commodity",
  "institutional_convergence",
  "institutional_convergence_commodity",
];

export function laneBook(key: BookKey): LaneBook {
  return LANE_BOOKS[key];
}

/** The books for one page, in market order. */
export function booksForRoute(routeBase: string): LaneBook[] {
  return BOOK_KEYS.map((k) => LANE_BOOKS[k]).filter((b) => b.route.split("?")[0] === routeBase);
}

export function bookField(key: BookKey, field: FieldKey): FieldAvailability {
  return LANE_BOOKS[key].fields[field];
}

export function isAvailable(f: FieldAvailability): boolean {
  return f.state === "available";
}

/** The one-line reason to render when a field is not fully available. */
export function unavailableReason(f: FieldAvailability): string | null {
  if (f.state === "available") return null;
  return f.state === "unavailable" ? f.reason : f.note;
}

// ─── IST day keys ───────────────────────────────────────────────────────────

/**
 * The IST calendar day of a timestamp, as YYYY-MM-DD.
 *
 * Repo convention: a TZ-NAIVE backend timestamp is UTC. Parsing it as local
 * time shifts an IST desk by +5:30 and puts an 09:20 IST trade on the previous
 * day, which is exactly how a "today" figure silently becomes wrong.
 */
export function istDayKey(value: string | number | Date | null | undefined): string | null {
  if (value == null || value === "") return null;
  let d: Date;
  if (value instanceof Date) d = value;
  else if (typeof value === "number") d = new Date(value);
  else {
    let s = String(value).trim();
    const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    const isDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s);
    if (isDateTime && !hasTz) s = s.replace(" ", "T") + "Z";
    d = new Date(s);
  }
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

export function istToday(nowMs: number = Date.now()): string {
  return new Date(nowMs).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

// ─── Day vs lifetime ────────────────────────────────────────────────────────

export type ClosedRowLike = { closedAt: string | null | undefined; pnl: number | null | undefined };

/**
 * The DAY figure and how it was obtained. `state` is the discriminator the UI
 * paints on — a derived figure, a served figure, a lane that has not traded
 * today and a lane that has never traded are four different facts.
 */
export type DayFigure =
  | { state: "derived"; realized: number; trades: number; wins: number; dayKey: string; note: string }
  | { state: "served"; realized: number; trades: number | null; wins: number | null; dayKey: string; note: string }
  | { state: "no_session_today"; dayKey: string; lastSessionDay: string | null; note: string }
  | { state: "never_traded"; note: string }
  | { state: "unavailable"; note: string };

/** Derive today's realized P&L from a closed list. Never sums a null as zero. */
export function deriveDayFromCloses(
  rows: ClosedRowLike[],
  nowMs: number = Date.now(),
  note = "",
): DayFigure {
  const dayKey = istToday(nowMs);
  const today = rows.filter((r) => istDayKey(r.closedAt) === dayKey && typeof r.pnl === "number" && Number.isFinite(r.pnl));
  if (!today.length) {
    const days = rows
      .map((r) => istDayKey(r.closedAt))
      .filter((d): d is string => Boolean(d))
      .sort();
    return {
      state: "no_session_today",
      dayKey,
      lastSessionDay: days.length ? days[days.length - 1] : null,
      note: note || "No position closed on this book today.",
    };
  }
  const realized = today.reduce((a, r) => a + (r.pnl as number), 0);
  return {
    state: "derived",
    realized,
    trades: today.length,
    wins: today.filter((r) => (r.pnl as number) > 0).length,
    dayKey,
    note,
  };
}

export type DailyPnlRow = { date?: string | null; pnl?: number | null; trades?: number | null; wins?: number | null };

/**
 * Read a SERVED daily series. The critical rule: the newest dated row is used
 * ONLY when its date is today. A dashboard that prints Friday's number under a
 * Monday "today" heading is the exact defect these pages exist to remove.
 */
export function dayFromDailySeries(
  daily: DailyPnlRow[] | null | undefined,
  nowMs: number = Date.now(),
  note = "",
): DayFigure {
  const dayKey = istToday(nowMs);
  const rows = (daily ?? []).filter((r) => typeof r?.date === "string" && r.date);
  if (!rows.length) {
    return { state: "no_session_today", dayKey, lastSessionDay: null, note: note || "The served daily series is empty." };
  }
  const sorted = [...rows].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const newest = sorted[sorted.length - 1];
  if (String(newest.date) !== dayKey) {
    return {
      state: "no_session_today",
      dayKey,
      lastSessionDay: String(newest.date),
      note: note || "The newest dated row on the served series is not today, so no day figure is shown.",
    };
  }
  const pnl = typeof newest.pnl === "number" && Number.isFinite(newest.pnl) ? newest.pnl : null;
  if (pnl == null) {
    return { state: "unavailable", note: "Today's row exists on the served series but carries no P&L value." };
  }
  return {
    state: "served",
    realized: pnl,
    trades: typeof newest.trades === "number" ? newest.trades : null,
    wins: typeof newest.wins === "number" ? newest.wins : null,
    dayKey,
    note,
  };
}

/** The day figure for a book, dispatched on its DECLARED day source. */
export function dayFigureFor(
  key: BookKey,
  input: { closed?: ClosedRowLike[]; daily?: DailyPnlRow[] | null },
  nowMs: number = Date.now(),
): DayFigure {
  const book = LANE_BOOKS[key];
  switch (book.day.mode) {
    case "never_traded":
      return { state: "never_traded", note: book.day.note };
    case "served_daily_series":
      return dayFromDailySeries(input.daily, nowMs, book.day.note);
    case "derived_from_closes":
      return deriveDayFromCloses(input.closed ?? [], nowMs, book.day.note);
  }
}

// ─── Quantity: lots AND units ───────────────────────────────────────────────

export type Quantity = {
  lots: number | null;
  lotSize: number | null;
  units: number | null;
  /** "2 lots · 100 units" — both, always, because a big unit count is normal. */
  text: string;
};

/**
 * Lots and units from whatever the book carries. Lot alignment is STRUCTURAL
 * (units = lots × lot_size), so a four-digit unit count on a cheap name is a
 * fact about the contract, never an error to flag.
 */
export function quantityOf(input: {
  lots?: number | null;
  lotSize?: number | null;
  units?: number | null;
}): Quantity {
  const n = (v: unknown): number | null => {
    if (v == null || v === "") return null;
    const x = Number(v);
    return Number.isFinite(x) ? x : null;
  };
  const lotSize = n(input.lotSize);
  let lots = n(input.lots);
  let units = n(input.units);
  if (units == null && lots != null && lotSize != null) units = lots * lotSize;
  if (lots == null && units != null && lotSize != null && lotSize > 0) {
    const q = units / lotSize;
    lots = Number.isInteger(q) ? q : null;
  }
  const parts: string[] = [];
  if (lots != null) parts.push(`${lots} lot${Math.abs(lots) === 1 ? "" : "s"}`);
  if (units != null) parts.push(`${units.toLocaleString("en-IN")} units`);
  return { lots, lotSize, units, text: parts.length ? parts.join(" · ") : "quantity UNAVAILABLE" };
}

// ─── DTE, recomputed rather than trusted ────────────────────────────────────

/**
 * DTE from an expiry DATE. Returns null when there is no expiry — and callers
 * must never fall back to a stored days_to_expiry, which on the auction book is
 * frozen at entry and drifts further from the truth every session.
 */
export function dteFromExpiry(expiry: string | null | undefined, nowMs: number = Date.now()): number | null {
  if (!expiry) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(expiry));
  if (!m) return null;
  const expiryUtcMidnight = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const todayKey = istToday(nowMs);
  const t = /^(\d{4})-(\d{2})-(\d{2})/.exec(todayKey);
  if (!t) return null;
  const todayUtcMidnight = Date.UTC(Number(t[1]), Number(t[2]) - 1, Number(t[3]));
  return Math.round((expiryUtcMidnight - todayUtcMidnight) / 86_400_000);
}

// ─── Mark freshness ─────────────────────────────────────────────────────────

/**
 * A STALE mark makes unrealised P&L UNKNOWN, not "last known".
 *
 * The rule the pages apply: past `staleAfterSeconds` the P&L cell stops
 * rendering a number entirely. Showing the last-known value with a small amber
 * dot was how a NIFTY 23000 PE kept displaying +55,048 against a premium that
 * belonged to a different strike.
 */
export const MARK_STALE_AFTER_SECONDS = 900;

export type MarkVerdict =
  | { state: "fresh"; ageSeconds: number; clock: "mark" | "book_sync" }
  | { state: "stale"; ageSeconds: number; clock: "mark" | "book_sync" }
  | { state: "absent"; ageSeconds: null; clock: "mark" | "book_sync" };

export function markVerdict(
  asOf: string | number | Date | null | undefined,
  clock: "mark" | "book_sync",
  nowMs: number = Date.now(),
  staleAfterSeconds: number = MARK_STALE_AFTER_SECONDS,
): MarkVerdict {
  if (asOf == null || asOf === "") return { state: "absent", ageSeconds: null, clock };
  let d: Date;
  if (asOf instanceof Date) d = asOf;
  else if (typeof asOf === "number") d = new Date(asOf);
  else {
    let s = String(asOf).trim();
    const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    const isDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s);
    if (isDateTime && !hasTz) s = s.replace(" ", "T") + "Z";
    d = new Date(s);
  }
  if (Number.isNaN(d.getTime())) return { state: "absent", ageSeconds: null, clock };
  const ageSeconds = Math.max(0, (nowMs - d.getTime()) / 1000);
  return { state: ageSeconds <= staleAfterSeconds ? "fresh" : "stale", ageSeconds, clock };
}

/** The label for a mark clock. An auction book has no mark clock at all. */
export const MARK_CLOCK_LABEL: Record<"mark" | "book_sync", string> = {
  mark: "mark age",
  book_sync: "book-sync age",
};

/** What a STALE reading is called, on the clock that actually produced it. */
export const MARK_STALE_LABEL: Record<"mark" | "book_sync", string> = {
  mark: "stale mark",
  book_sync: "stale book-sync",
};

// ─── R/R plan basis ─────────────────────────────────────────────────────────

/**
 * Which price scale a row's stop and target are quoted on.
 *
 * This is not pedantry. The auction book stores `entry_premium` (29.65) next to
 * a `stop_price` and `target_price` that are UNDERLYING index levels (23794 /
 * 23667.5). Feeding the premium in as the R/R entry produces |29.65 − 23794| of
 * risk against |23667.5 − 29.65| of reward — 0.99R, a number with no meaning,
 * where the real plan is 2.19R. An R/R is only a fact when all three legs are
 * quoted on the SAME instrument.
 *
 *   instrument — stop and target are on the same instrument as the entry price
 *   underlying — stop and target are underlying levels; the comparable entry is
 *                the entry SPOT, and the rendered ratio must say so
 */
export type PlanBasis = "instrument" | "underlying";

export const PLAN_BASIS_NOTE: Record<PlanBasis, string> = {
  instrument: "entry, stop and target are all quoted on the traded instrument.",
  underlying:
    "this book's stop and target are UNDERLYING levels, so the R/R is computed against the entry SPOT, not the option premium. Mixing the two would produce a meaningless ratio.",
};

// ─── Portfolio field completeness ───────────────────────────────────────────

export type PortfolioFacts = {
  initialCapital: number | null;
  equity: number | null;
  realizedLifetime: number | null;
  unrealized: number | null;
  openCount: number | null;
  closedCount: number | null;
  winRate: number | null;
  profitFactor: number | null;
  maxDrawdown: number | null;
  maxDrawdownPct: number | null;
  /** Real capital reservation, when the book keeps one. */
  reservedMargin: number | null;
  /** Notional value of open exposure — labelled notional, never "margin". */
  notionalExposure: number | null;
};

export const EMPTY_PORTFOLIO_FACTS: PortfolioFacts = {
  initialCapital: null,
  equity: null,
  realizedLifetime: null,
  unrealized: null,
  openCount: null,
  closedCount: null,
  winRate: null,
  profitFactor: null,
  maxDrawdown: null,
  maxDrawdownPct: null,
  reservedMargin: null,
  notionalExposure: null,
};

/**
 * Day and lifetime must never resolve through the same accessor — that is the
 * mechanism by which lifetime realized P&L got printed under a "today"
 * heading. This asserts the two are structurally separate for a given book.
 */
export function dayAndLifetimeAreSeparate(key: BookKey): boolean {
  const book = LANE_BOOKS[key];
  // Lifetime always comes off the summary/statistics block; the day figure never
  // does — it is either a dated series row or a filter over the closed list.
  return (
    book.day.mode === "served_daily_series" ||
    book.day.mode === "derived_from_closes" ||
    book.day.mode === "never_traded"
  );
}

// ─── Portfolio unrealised, under the stale-mark rule ────────────────────────

/**
 * The unrealised P&L a PORTFOLIO view is allowed to print.
 *
 * A book's summary happily reports `unrealized_pnl` computed against whatever
 * mark it last saw. On 2026-07-27 the directional summary reported +9,760 while
 * every one of its six open marks was 2–5 DAYS old, and the positions view —
 * correctly — refused to show a single one of those numbers. A portfolio tile
 * that prints the roll-up anyway re-tells the same lie one screen away, and the
 * total P&L built on top of it inherits it.
 *
 * So the stale-mark rule applies at the roll-up too: the number is shown only
 * when every open row's mark is fresh.
 */
export type UnrealizedVerdict =
  /** Nothing is open, so nothing is unrealised. Information, not a default. */
  | { state: "measured_zero"; value: 0; note: string }
  | { state: "known"; value: number; note: string }
  | { state: "unknown"; note: string; staleRows: number; totalRows: number }
  | { state: "unavailable"; note: string };

export function portfolioUnrealized(input: {
  /** The book's own unrealised roll-up, or null when it serves none. */
  reported: number | null;
  /** The book's own open count, or null when it serves none. */
  openCount: number | null;
  /** One verdict per OPEN row, or null when the open rows were not loaded. */
  marks: MarkVerdict[] | null;
}): UnrealizedVerdict {
  const rows = input.marks;
  const noneOpen = input.openCount === 0 || (rows != null && rows.length === 0);
  if (noneOpen) {
    return {
      state: "measured_zero",
      value: 0,
      note: "nothing is open, so nothing is unrealised — a measured zero",
    };
  }
  if (rows == null) {
    return {
      state: "unknown",
      note: "the open rows were not loaded, so the age of the marks behind this figure cannot be established",
      staleRows: 0,
      totalRows: 0,
    };
  }
  const stale = rows.filter((m) => m.state !== "fresh");
  if (stale.length) {
    return {
      state: "unknown",
      note: "computed against marks that are too old to act on",
      staleRows: stale.length,
      totalRows: rows.length,
    };
  }
  if (input.reported == null || !Number.isFinite(input.reported)) {
    return { state: "unavailable", note: "this book serves no unrealised roll-up" };
  }
  return { state: "known", value: input.reported, note: "every open mark is fresh" };
}

/**
 * Realized + unrealised, but only when BOTH are facts. A missing or unknown
 * component is not zero, so the total is refused rather than approximated —
 * `(realized ?? 0) + (unrealized ?? 0)` is how a partial sum passes for a total.
 */
export function totalPnl(
  realizedLifetime: number | null,
  unrealized: UnrealizedVerdict,
): { value: number | null; note: string } {
  if (realizedLifetime == null || !Number.isFinite(realizedLifetime)) {
    return { value: null, note: "this book serves no lifetime realized figure, so no total can be stated" };
  }
  if (unrealized.state === "known") {
    return { value: realizedLifetime + unrealized.value, note: "realized + unrealised, both fresh" };
  }
  if (unrealized.state === "measured_zero") {
    return { value: realizedLifetime, note: "realized only — nothing is open, so unrealised is a measured zero" };
  }
  if (unrealized.state === "unknown") {
    return {
      value: null,
      note: `realized is known but unrealised is UNKNOWN (${unrealized.note}), so a total cannot be stated. Adding zero for the unknown part would understate or overstate it.`,
    };
  }
  return {
    value: null,
    note: "realized is known but this book serves no unrealised figure, so a total cannot be stated",
  };
}
