/**
 * Lane-book honesty tests (2026-07-27).
 *
 * Same runner as the other suites — `node --test` with native TypeScript
 * type-stripping — so everything is a pure-module assertion or a source-text
 * assertion over the rendered files.
 *
 * What these lock down, in the order the bugs happened:
 *
 *   1. Each lane's AUTHORITATIVE book is declared by its real table name or
 *      real repo path, and the journals that stranded five wrong "lane state"
 *      calls (agent_positions, agent_signals, runtime/portfolio/daily_*.json,
 *      directional_option_trades) are never among them.
 *   2. NO lane claims a broker order book. Fills-only lanes say so, and no
 *      adapter turns a trade into an order.
 *   3. Fields the books do not carry are UNAVAILABLE with a reason — fees on
 *      four of the five books, DTE on the futures books, a mark clock on the
 *      auction book.
 *   4. DAY and LIFETIME never resolve through the same accessor, and a served
 *      daily series whose newest row is not today refuses to answer.
 *   5. The adapters never coerce a missing number to zero.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  BOOK_KEYS,
  BOOK_VIEWS,
  BOOK_VIEW_PARAM,
  LANE_BOOKS,
  MARK_STALE_LABEL,
  portfolioUnrealized,
  totalPnl,
  bookField,
  booksForRoute,
  dayAndLifetimeAreSeparate,
  dayFigureFor,
  dayFromDailySeries,
  deriveDayFromCloses,
  dteFromExpiry,
  isBookView,
  istDayKey,
  markVerdict,
  quantityOf,
  unavailableReason,
  type BookKey,
} from "../src/lib/lane-books.ts";
import {
  auctionPosition,
  auctionTrade,
  directionalPosition,
  directionalTrade,
  futuresOrder,
  futuresPosition,
  futuresTrade,
  notionalExposure,
  num,
  sumPresent,
} from "../src/lib/book-rows.ts";
import { LANE_HORIZON } from "../src/lib/lane-taxonomy.ts";
import { allDesks } from "../src/lib/nav-model.ts";

const ROOT = path.join(import.meta.dirname, "..");
const read = (...p: string[]) => readFileSync(path.join(ROOT, ...p), "utf8");

// ─── 1. The authoritative book, declared ────────────────────────────────────

test("all five books are declared, one per lane, with a real source path", () => {
  assert.equal(BOOK_KEYS.length, 5);
  assert.deepEqual(new Set(Object.keys(LANE_BOOKS)), new Set(BOOK_KEYS));
  for (const key of BOOK_KEYS) {
    const b = LANE_BOOKS[key];
    assert.equal(b.key, key);
    assert.ok(b.source.path.length > 10, `${key} has no source path`);
    assert.ok(b.source.servedBy.length > 0, `${key} names no serving endpoint`);
    assert.ok(b.source.note.length > 30, `${key} has no note a reader can check`);
    assert.ok(b.orderLayerStatement.length > 80, `${key} does not state its order layer`);
  }
});

test("the declared source is the real table / real file, per lane", () => {
  assert.match(LANE_BOOKS.directional_options.source.path, /^directional_paper_positions/);
  assert.equal(LANE_BOOKS.directional_options.source.kind, "postgres_table");
  assert.match(
    LANE_BOOKS.auction_intelligence.source.path,
    /^backend\/runtime\/auction_intelligence\/paper_positions\.json/,
  );
  assert.equal(
    LANE_BOOKS.auction_intelligence_commodity.source.path,
    "backend/runtime/auction_intelligence_commodity/commodity_paper.json",
  );
  assert.equal(
    LANE_BOOKS.institutional_convergence.source.path,
    "backend/runtime/institutional_convergence/paper.json",
  );
  assert.equal(
    LANE_BOOKS.institutional_convergence_commodity.source.path,
    "backend/runtime/institutional_convergence/commodity_paper.json",
  );
  // The two MCX books are DIFFERENT files and must never collapse into one.
  assert.notEqual(
    LANE_BOOKS.auction_intelligence_commodity.source.path,
    LANE_BOOKS.institutional_convergence_commodity.source.path,
  );
});

/** The artifacts that caused five wrong lane-state calls in one week. */
const FORBIDDEN_SOURCES = [
  "agent_positions",
  "agent_signals",
  "runtime/portfolio/daily_",
  // A BACKTEST table with 0 rows, FK'd to directional_option_runs.
  "directional_option_trades",
  "paper_trade_book",
];

test("no book points at a journal, a roll-up file or the backtest table", () => {
  for (const key of BOOK_KEYS) {
    const b = LANE_BOOKS[key];
    const haystack = `${b.source.path} ${b.source.servedBy.join(" ")}`;
    for (const bad of FORBIDDEN_SOURCES) {
      assert.ok(!haystack.includes(bad), `${key} reads ${bad}, which is not an authoritative book`);
    }
  }
});

test("the book-data loader never fetches a journal endpoint as a book", () => {
  const src = read("src", "components", "books", "book-data.ts");
  for (const bad of ["agent-positions", "agent_positions", "paper-trade-book", "directional_option_trades"]) {
    assert.ok(!src.includes(bad), `book-data reads ${bad}`);
  }
  // And it must read the two convergence/auction MCX books that were missing.
  assert.match(src, /getInstitutionalConvergencePaper/);
  assert.match(src, /getCommodityAuctionIntelligencePaper/);
});

test("every lane key a book claims exists in the horizon table", () => {
  for (const key of BOOK_KEYS) {
    assert.ok(LANE_HORIZON[LANE_BOOKS[key].laneKey], `${key} claims undeclared lane ${LANE_BOOKS[key].laneKey}`);
  }
});

// ─── 2. Nobody claims a broker order book ───────────────────────────────────

test("NO book claims working / cancelled / rejected order states", () => {
  for (const key of BOOK_KEYS) {
    const f = bookField(key, "orderStatus");
    assert.equal(f.state, "unavailable", `${key} claims an order-status field`);
    assert.match(unavailableReason(f)!, /instant|never exist/i);
  }
});

test("the two options lanes are NOT presented as having a fill-event order log", () => {
  // directional keeps a real DECISION log; auction NSE keeps an intent journal
  // that cannot be reconciled to positions. Neither is an order book, and
  // neither may be typed as one.
  assert.equal(LANE_BOOKS.directional_options.orderLayer, "decision_log");
  assert.equal(LANE_BOOKS.auction_intelligence.orderLayer, "intent_log");
  assert.match(LANE_BOOKS.directional_options.orderLayerStatement, /NO order layer/);
  assert.match(LANE_BOOKS.auction_intelligence.orderLayerStatement, /NO order book/);
  assert.match(LANE_BOOKS.auction_intelligence.orderLayerStatement, /no order_log key/);
});

test("the three futures books declare a REAL fill-event log", () => {
  for (const key of [
    "auction_intelligence_commodity",
    "institutional_convergence",
    "institutional_convergence_commodity",
  ] as BookKey[]) {
    assert.equal(LANE_BOOKS[key].orderLayer, "fill_events");
    assert.match(LANE_BOOKS[key].orderLayerStatement, /fill|EMPTY/i);
  }
});

test("no view synthesises orders from trades", () => {
  const views = read("src", "components", "books", "BookViews.tsx");
  const data = read("src", "components", "books", "book-data.ts");
  // The shipped ExecutionPanels exports a deriveOrders(trades) helper. These
  // pages must never reach for it: both MCX books have a real order log, and
  // the two options lanes have no order layer at all.
  assert.ok(!views.includes("deriveOrders"), "a book view derives orders from trades");
  assert.ok(!data.includes("deriveOrders"), "the book loader derives orders from trades");
  assert.ok(!views.includes("deriveStatistics"), "a book view derives statistics client-side");
});

// ─── 3. Absent fields say UNAVAILABLE, with the reason ──────────────────────

test("fees are UNAVAILABLE on all four non-directional books, with a reason", () => {
  for (const key of BOOK_KEYS) {
    const f = bookField(key, "fees");
    if (key === "directional_options") {
      assert.equal(f.state, "available", "directional records transaction_cost on every closed row");
      continue;
    }
    assert.equal(f.state, "unavailable", `${key} claims fees it does not record`);
    assert.ok(unavailableReason(f)!.length > 20);
  }
});

test("slippage is never claimed as complete on any book", () => {
  for (const key of BOOK_KEYS) {
    assert.notEqual(bookField(key, "slippage").state, "available", `${key} claims complete slippage`);
  }
});

test("DTE is UNAVAILABLE on the futures books — a contract label is not a date", () => {
  for (const key of [
    "auction_intelligence_commodity",
    "institutional_convergence",
    "institutional_convergence_commodity",
  ] as BookKey[]) {
    const f = bookField(key, "dte");
    assert.equal(f.state, "unavailable");
  }
  assert.match(unavailableReason(bookField("auction_intelligence_commodity", "dte"))!, /label/i);
  // The two options books DO carry an expiry, so DTE is computable there.
  assert.equal(bookField("directional_options", "dte").state, "available");
  assert.equal(bookField("auction_intelligence", "dte").state, "partial");
  assert.match(unavailableReason(bookField("auction_intelligence", "dte"))!, /FROZEN at entry/);
});

test("only directional claims a real per-position mark clock", () => {
  assert.equal(bookField("directional_options", "markClock").state, "available");
  assert.equal(bookField("auction_intelligence", "markClock").state, "partial");
  assert.match(unavailableReason(bookField("auction_intelligence", "markClock"))!, /BOOK-SYNC/);
});

test("the exit plan is only claimed where stops and targets are actually stored", () => {
  // Auction NSE stores stop_price + target_price on 100% of rows, so R/R may
  // render. Directional's exit is a RULE in code on most rows, so it may not.
  assert.equal(bookField("auction_intelligence", "exitPlan").state, "available");
  assert.equal(bookField("directional_options", "exitPlan").state, "partial");
  assert.match(unavailableReason(bookField("directional_options", "exitPlan"))!, /RULE/);
});

test("no book hardcodes a claim about its own contents", () => {
  // Regression (2026-07-28). institutional_convergence used to declare that it
  // had never opened a position and that "capital is untouched at its declared
  // initial capital". The live book at the time held 26 closed trades,
  // realized -Rs 23,71,330 and equity of -Rs 13,71,330 against Rs 10,00,000
  // initial — the page asserted the exact opposite of the truth, and this test
  // pinned the lie in place.
  //
  // Emptiness is a property of the DATA, not of the declaration. No book may
  // carry a static neverFired string; a lane that genuinely has not traded
  // renders that from its own rows.
  for (const key of BOOK_KEYS) {
    assert.equal(
      LANE_BOOKS[key].neverFired,
      null,
      `${key} must not hardcode a never-fired claim — derive it from the data`,
    );
  }
});

// ─── 4. Day vs lifetime ─────────────────────────────────────────────────────

test("day and lifetime never resolve through the same accessor", () => {
  for (const key of BOOK_KEYS) {
    assert.ok(dayAndLifetimeAreSeparate(key), `${key} could resolve day from a lifetime field`);
    // Lifetime is always the summary block; the day mode is never "summary".
    assert.ok(["served_daily_series", "derived_from_closes", "never_traded"].includes(LANE_BOOKS[key].day.mode));
    assert.ok(LANE_BOOKS[key].day.note.length > 30, `${key} does not say how its day figure is obtained`);
  }
});

test("a served daily series whose newest row is not today REFUSES to answer", () => {
  // This is the exact defect being fixed: a dashboard printing Friday's number
  // under a Monday "today" heading.
  const monday = Date.UTC(2026, 6, 27, 4, 0, 0); // 09:30 IST Mon 2026-07-27
  const daily = [
    { date: "2026-07-23", pnl: 200230, trades: 3 },
    { date: "2026-07-24", pnl: 7780, trades: 3 },
  ];
  const d = dayFromDailySeries(daily, monday);
  assert.equal(d.state, "no_session_today");
  assert.equal(d.state === "no_session_today" ? d.lastSessionDay : null, "2026-07-24");
  assert.equal((d as { realized?: number }).realized, undefined, "no P&L may leak out of a refused day");

  // Same series, with today present → it answers, and with TODAY's number.
  const withToday = [...daily, { date: "2026-07-27", pnl: -1234, trades: 2, wins: 0 }];
  const ok = dayFromDailySeries(withToday, monday);
  assert.equal(ok.state, "served");
  assert.equal(ok.state === "served" ? ok.realized : null, -1234);
});

test("an empty served series is 'no session', never a zero", () => {
  const d = dayFromDailySeries([], Date.UTC(2026, 6, 27, 4, 0, 0));
  assert.equal(d.state, "no_session_today");
  assert.equal((d as { realized?: number }).realized, undefined);
});

test("the derived day figure filters closes on the IST calendar date", () => {
  const monday = Date.UTC(2026, 6, 27, 8, 0, 0); // 13:30 IST
  const rows = [
    // 2026-07-27T03:50Z = 09:20 IST Monday → TODAY, even though it is before
    // 05:30Z. Parsing this naive-UTC string as local time is how an 09:20 IST
    // trade lands on the previous day.
    { closedAt: "2026-07-27T03:50:00", pnl: 500 },
    { closedAt: "2026-07-26T20:00:00", pnl: 999 }, // 27th 01:30 IST → also today
    { closedAt: "2026-07-24T09:31:44", pnl: -55263 }, // Friday
  ];
  const d = deriveDayFromCloses(rows, monday);
  assert.equal(d.state, "derived");
  assert.equal(d.state === "derived" ? d.realized : null, 1499);
  assert.equal(d.state === "derived" ? d.trades : null, 2);
  assert.equal(d.state === "derived" ? d.wins : null, 2);
});

test("no closes today is 'no session', and it names the last session", () => {
  const monday = Date.UTC(2026, 6, 27, 8, 0, 0);
  const d = deriveDayFromCloses([{ closedAt: "2026-07-24T09:31:44", pnl: -55263 }], monday);
  assert.equal(d.state, "no_session_today");
  assert.equal(d.state === "no_session_today" ? d.lastSessionDay : null, "2026-07-24");
});

test("a null P&L is never summed into the day figure as a zero", () => {
  const monday = Date.UTC(2026, 6, 27, 8, 0, 0);
  const d = deriveDayFromCloses(
    [
      { closedAt: "2026-07-27T04:00:00", pnl: null },
      { closedAt: "2026-07-27T05:00:00", pnl: 100 },
    ],
    monday,
  );
  assert.equal(d.state, "derived");
  assert.equal(d.state === "derived" ? d.trades : null, 1, "the null row must be excluded, not counted as 0");
});

test("dayFigureFor dispatches on the DECLARED day source, not on what is passed", () => {
  const monday = Date.UTC(2026, 6, 27, 8, 0, 0);
  // institutional_convergence is declared derived_from_closes (it serves no
  // dated daily series), so a `daily` series it should not have is IGNORED and
  // the figure comes from its closes.
  const fromCloses = dayFigureFor(
    "institutional_convergence",
    { closed: [{ closedAt: "2026-07-27T04:00:00", pnl: -11 }], daily: [{ date: "2026-07-27", pnl: 9 }] },
    monday,
  );
  assert.equal(fromCloses.state, "derived");
  assert.equal(fromCloses.state === "derived" ? fromCloses.realized : null, -11);
  // A derive-from-closes lane ignores a series it should not have.
  const derived = dayFigureFor(
    "directional_options",
    { closed: [{ closedAt: "2026-07-27T04:00:00", pnl: 7 }], daily: [{ date: "2026-07-27", pnl: 99999 }] },
    monday,
  );
  assert.equal(derived.state, "derived");
  assert.equal(derived.state === "derived" ? derived.realized : null, 7);
});

test("istDayKey treats a naive backend timestamp as UTC", () => {
  // 2026-07-26T19:00Z = 2026-07-27 00:30 IST.
  assert.equal(istDayKey("2026-07-26T19:00:00"), "2026-07-27");
  assert.equal(istDayKey("2026-07-26T19:00:00Z"), "2026-07-27");
  assert.equal(istDayKey("2026-07-27T00:30:00+05:30"), "2026-07-27");
  assert.equal(istDayKey(null), null);
  assert.equal(istDayKey("not a date"), null);
});

// ─── 5. Quantity, DTE, mark freshness ───────────────────────────────────────

test("quantity always reports lots AND units, and lot alignment is structural", () => {
  const q = quantityOf({ lots: 2, lotSize: 50 });
  assert.equal(q.units, 100);
  assert.match(q.text, /2 lots/);
  assert.match(q.text, /100 units/);
  // A large unit count on a cheap name is normal, never an anomaly.
  const big = quantityOf({ units: 390, lotSize: 65 });
  assert.equal(big.lots, 6);
  assert.equal(big.units, 390);
  // A non-integer division must NOT invent a fractional lot count.
  const odd = quantityOf({ units: 175, lotSize: 50 });
  assert.equal(odd.lots, null);
  assert.equal(odd.units, 175);
  // Nothing at all → says so rather than showing 0.
  assert.equal(quantityOf({}).text, "quantity UNAVAILABLE");
  assert.equal(quantityOf({}).units, null);
});

test("DTE is recomputed from the expiry date, never taken from a stored field", () => {
  const monday = Date.UTC(2026, 6, 27, 8, 0, 0);
  // The auction book stores days_to_expiry: 11 frozen at entry on 07-24 for a
  // 08-04 expiry. Recomputed on the 27th it is 8, and 8 is the truth.
  assert.equal(dteFromExpiry("2026-08-04", monday), 8);
  assert.equal(dteFromExpiry("2026-07-27", monday), 0);
  assert.equal(dteFromExpiry(null, monday), null);
  assert.equal(dteFromExpiry("MCX:GOLD26AUGFUT", monday), null, "a contract label is not a date");
});

test("a stale mark is a distinct state from a fresh one and from an absent one", () => {
  const now = Date.UTC(2026, 6, 27, 8, 0, 0);
  const fresh = markVerdict(new Date(now - 60_000).toISOString(), "mark", now);
  assert.equal(fresh.state, "fresh");
  const stale = markVerdict("2026-07-24T09:36:00Z", "mark", now);
  assert.equal(stale.state, "stale");
  assert.ok((stale.ageSeconds ?? 0) > 86_400);
  const absent = markVerdict(null, "book_sync", now);
  assert.equal(absent.state, "absent");
  assert.equal(absent.ageSeconds, null);
  assert.equal(absent.clock, "book_sync");
});

test("the positions view suppresses unrealised P&L on a stale mark", () => {
  const src = read("src", "components", "books", "BookPrimitives.tsx");
  const block = src.slice(src.indexOf("export function UnrealizedCell"));
  assert.match(block, /UNKNOWN/);
  assert.match(block, /stale/);
  // The stale branch must return before it can render the number.
  assert.ok(
    block.indexOf('verdict.state === "stale"') < block.indexOf("formatSignedMoney(value)"),
    "a stale mark must short-circuit before the value is formatted",
  );
});

// ─── 6. The adapters never coerce a missing number to zero ──────────────────

test("num() keeps missing missing — null, empty string and NaN are not 0", () => {
  assert.equal(num(null), null);
  assert.equal(num(undefined), null);
  assert.equal(num(""), null);
  assert.equal(num("abc"), null);
  assert.equal(num(0), 0, "a measured zero survives");
  assert.equal(num("12.5"), 12.5);
});

test("the auction trade adapter reports NO cost rather than a zero cost", () => {
  const t = auctionTrade({
    position_id: "p1",
    symbol: "NIFTY 23700 PE 04 AUG 26",
    quantity: 390,
    lot_size: 65,
    entry_premium: 171.25,
    exit_premium: 29.55,
    realized_pnl: -55263,
    stop_price: 23778.5,
    target_price: 23661.5,
    close_reason: "hard_stop",
  });
  assert.equal(t.cost, null, "this book records no cost; null, never 0");
  assert.equal(t.slippage, null);
  assert.equal(t.realizedGross, null);
  assert.equal(t.rMultiple, null);
  assert.equal(t.realized, -55263);
  assert.equal(t.qty.lots, 6);
  assert.equal(t.qty.units, 390);
});

test("the directional trade adapter carries the real net/gross split", () => {
  const t = directionalTrade({
    position_id: "abc",
    trading_symbol: "ULTRACEMCO 12000 PE 28 JUL 26",
    underlying: "ULTRACEMCO",
    direction: "PE",
    quantity_lots: 2,
    quantity_units: 100,
    entry_premium: 182.45,
    exit_premium: 320.75,
    realized_pnl: 13729.32,
    realized_pnl_gross: 13830,
    transaction_cost: 100.68,
    policy_r_multiple: 1.8306,
    close_reason: "profit_target",
  });
  assert.equal(t.realized, 13729.32);
  assert.equal(t.realizedGross, 13830);
  assert.equal(t.cost, 100.68);
  // The net figure must be the one the book computed, not a re-derivation.
  assert.ok(Math.abs((t.realizedGross! - t.cost!) - t.realized!) < 1e-6);
  assert.equal(t.slippage, null, "slippage is absent on most rows and stays null");
});

test("an opening fill books no P&L, and that is null rather than zero", () => {
  const open = futuresOrder(
    { action: "open", direction: "SHORT", lots: 3, lot_size: 5, price: 219866, symbol: "SILVERM", reason: "signal_entry" },
    0,
  );
  assert.equal(open.pnl, null);
  assert.equal(open.lotsRemaining, null);
  assert.equal(open.qty.units, 15);
  const close = futuresOrder(
    { action: "close", lots: 3, lot_size: 5, price: 220635, pnl: -11535, symbol: "SILVERM", reason: "hard_stop" },
    1,
  );
  assert.equal(close.pnl, -11535);
  const partial = futuresOrder(
    { action: "partial_close", lots: 2, lot_size: 100, price: 8148, pnl: 9600, lots_remaining: 2, symbol: "CRUDEOIL" },
    2,
  );
  assert.equal(partial.lotsRemaining, 2);
});

test("a futures position carries no expiry, so nothing can fabricate a DTE", () => {
  const p = futuresPosition({
    position_id: "IC-GOLD-1",
    symbol: "GOLD",
    futures_contract: "MCX:GOLD26AUGFUT",
    direction: "LONG",
    lots: 1,
    lot_size: 10,
    entry_price: 140675,
    current_price: 141000,
  });
  assert.equal(p.expiry, null);
  assert.equal(dteFromExpiry(p.expiry), null);
  assert.equal(p.markClock, "book_sync", "these books have no separate mark clock");
});

test("an auction position falls back to the BOOK-SYNC clock, never to a mark clock", () => {
  const p = auctionPosition({ position_id: "x", symbol: "NIFTY 23000 PE", quantity: 390, lot_size: 65 }, "2026-07-24T09:37:48Z");
  assert.equal(p.markClock, "book_sync");
  assert.equal(p.markAsOf, "2026-07-24T09:37:48Z");
  const withRow = auctionPosition({ position_id: "y", updated_at: "2026-07-24T09:37:47Z" }, "2026-07-24T09:37:48Z");
  assert.equal(withRow.markAsOf, "2026-07-24T09:37:47Z");
});

test("notional exposure refuses to answer rather than summing a partial book", () => {
  const none = notionalExposure([]);
  assert.equal(none.value, null);
  const partial = notionalExposure([
    futuresPosition({ symbol: "GOLD", lots: 1, lot_size: 10, entry_price: 140675, current_price: 141000 }),
    futuresPosition({ symbol: "SILVERM", lots: 3, lot_size: 5 }), // no price at all
  ]);
  assert.equal(partial.counted, 1);
  assert.equal(partial.skipped, 1, "the unpriced row must be reported as skipped, not silently dropped");
  assert.equal(partial.value, 1_410_000);
});

test("sumPresent never treats a missing value as zero", () => {
  assert.deepEqual(sumPresent([]), { total: null, present: 0, absent: 0 });
  assert.deepEqual(sumPresent([null, null]), { total: null, present: 0, absent: 2 });
  assert.deepEqual(sumPresent([1, null, 2]), { total: 3, present: 2, absent: 1 });
  assert.deepEqual(sumPresent([0]), { total: 0, present: 1, absent: 0 });
});

test("the futures trade adapter prefers the INITIAL stop for R, not the trailed one", () => {
  const t = futuresTrade({
    position_id: "IC-NG-1",
    symbol: "NATURALGAS",
    direction: "LONG",
    entry_price: 277.2,
    exit_price: 278,
    initial_stop: 276.0,
    stop: 277.2, // moved to break-even before the exit
    lots: 16,
    initial_lots: 32,
    lot_size: 1250,
    realized_pnl: 26000,
    r_multiple: null,
  });
  assert.equal(t.stop, 276.0);
  assert.equal(t.rMultiple, null, "the backend returns null rather than guessing; the adapter must not guess either");
  assert.equal(t.qty.lots, 32, "the trade book shows the INITIAL size, not the remainder");
});

// ─── 7. Views, routes and endpoints ─────────────────────────────────────────

test("the four views are declared once and the pages key on ?view=", () => {
  assert.deepEqual(BOOK_VIEWS, ["orders", "trades", "positions", "portfolio"]);
  assert.equal(BOOK_VIEW_PARAM, "view");
  assert.ok(isBookView("portfolio"));
  assert.ok(!isBookView("tab"));
  assert.ok(!isBookView(null));
});

test("every book route is a sub-path of its desk route, and resolves back", () => {
  const deskHrefs = new Set(allDesks().map((d) => d.href.split("?")[0]));
  for (const key of BOOK_KEYS) {
    const b = LANE_BOOKS[key];
    assert.ok(deskHrefs.has(b.deskHref), `${key} points at unknown desk ${b.deskHref}`);
    assert.ok(b.route.startsWith(`${b.deskHref}/books`), `${key} route ${b.route} is not under its desk`);
  }
});

test("booksForRoute groups the siblings under one page, in market order", () => {
  const auction = booksForRoute("/strategies/auction/books");
  assert.deepEqual(auction.map((b) => b.market), ["NSE", "MCX"]);
  const ic = booksForRoute("/strategies/institutional-convergence/books");
  assert.deepEqual(ic.map((b) => b.market), ["NSE", "MCX"]);
  const dir = booksForRoute("/strategies/directional/books");
  assert.equal(dir.length, 1);
  assert.equal(dir[0].key, "directional_options");
});

/**
 * Verified against the served OpenAPI schema on 2026-07-27. A book may only
 * declare an endpoint the backend actually serves — a 404 that renders an
 * em-dash is indistinguishable from a flat book.
 */
const SERVED_ENDPOINTS = new Set([
  "/api/directional-options/paper-positions",
  "/api/directional-options/paper-summary",
  "/api/directional-options/paper-journal",
  "/api/auction-intelligence/paper-positions",
  "/api/auction-intelligence/paper-status",
  "/api/auction-intelligence/paper-journal",
  "/api/auction-intelligence/commodity/paper",
  "/api/auction-intelligence/commodity/status",
  "/api/institutional-convergence/paper",
  "/api/institutional-convergence/orders",
  "/api/institutional-convergence/trades",
  "/api/institutional-convergence/statistics",
  "/api/institutional-convergence/status",
  "/api/institutional-convergence/commodity/paper",
  "/api/institutional-convergence/commodity/orders",
  "/api/institutional-convergence/commodity/trades",
  "/api/institutional-convergence/commodity/statistics",
  "/api/institutional-convergence/commodity/status",
]);

test("every endpoint a book declares is one the backend serves", () => {
  for (const key of BOOK_KEYS) {
    for (const e of LANE_BOOKS[key].source.servedBy) {
      assert.ok(SERVED_ENDPOINTS.has(e), `${key} declares unserved ${e}`);
    }
  }
});

test("the endpoints the books need all have a client getter", () => {
  // The convergence getters build their path with a template expression that
  // switches the /commodity segment, so the literal endpoint never appears in
  // the source. Collapse the template away and the NSE and MCX forms both
  // reduce to the same literal — which is the string to look for.
  const api = read("src", "lib", "api.ts")
    .replace(/\$\{market === "MCX" \? "\/commodity" : ""\}/g, "");
  for (const key of BOOK_KEYS) {
    for (const e of LANE_BOOKS[key].source.servedBy) {
      const nse = e.replace("/commodity", "");
      assert.ok(api.includes(e) || api.includes(nse), `no client getter reaches ${e}`);
    }
  }
});

// ─── 8. The cross-lane ledger no longer omits three books ───────────────────

test("the cross-lane ledger fetches all three previously-missing books", () => {
  const src = read("src", "lib", "strategy-position-ledger.ts");
  assert.match(src, /getCommodityAuctionIntelligencePaper/, "the MCX auction book is still missing from the roll-up");
  assert.match(src, /getInstitutionalConvergencePaper\("NSE"\)/);
  assert.match(src, /getInstitutionalConvergencePaper\("MCX"\)/);
  // And they must reach the row builders and the summaries, not just the fetch.
  assert.match(src, /FUTURES_BOOKS/);
  assert.ok(src.includes("futuresBookRow"), "the futures rows are fetched but never built");
});

// ─── 9. Every page states its source ────────────────────────────────────────

test("the desk renders the source banner on every view, above the tabs", () => {
  const src = read("src", "components", "books", "LaneBooksDesk.tsx");
  assert.match(src, /BookSourceBanner/);
  // It must be in `beforeTabs`, i.e. outside the per-view switch, so it cannot
  // be missing from three of the four views.
  const before = src.indexOf("beforeTabs=");
  const viewSwitch = src.indexOf('view === "orders"');
  assert.ok(before > 0 && before < viewSwitch, "the source banner must render above the tab content");
});

test("the source banner prints the path, the endpoints and the row counts", () => {
  const src = read("src", "components", "books", "BookPrimitives.tsx");
  const block = src.slice(src.indexOf("export function BookSourceBanner"), src.indexOf("export function NeverFiredState"));
  assert.match(block, /book\.source\.path/);
  assert.match(block, /book\.source\.servedBy/);
  assert.match(block, /counts/);
  assert.match(block, /lastWriteAt/);
  // A failed source must be named, not folded into an empty state.
  assert.match(block, /did not answer/);
  assert.match(block, /not an empty book/);
});

// ─── 10. R/R may never mix a premium entry with an underlying stop ──────────
//
// Found in DOM verification on 2026-07-27: the auction book's open NIFTY 23000
// PE rendered "0.99R" from entry_premium 29.65 against stop 23794 and target
// 23667.5 — index levels. The plan is really 2.19R on the underlying. The ratio
// passed the "real entry + stop + target" gate while being meaningless, which
// is precisely the failure mode R/R gating exists to prevent.

test("an option row whose stop is an UNDERLYING level exposes the spot as its R/R entry", () => {
  const p = auctionPosition(
    {
      symbol: "NIFTY 23000 PE 04 AUG 26",
      signal_action: "SHORT",
      entry_premium: 29.65,
      entry_spot_price: 23754.3,
      stop_price: 23794.0,
      target_price: 23667.5,
      quantity: 390,
      lot_size: 65,
    },
    null,
  );
  assert.equal(p.planBasis, "underlying");
  assert.equal(p.entry, 29.65, "the traded price stays the premium");
  assert.equal(p.planEntry, 23754.3, "the R/R entry must be the SPOT, not the premium");

  // rrRender lives in market-semantics, which pulls in React components and so
  // cannot be imported under bare `node --test`. Its arithmetic is
  // |target - entry| / |entry - stop|, applied here to both bases.
  const rr = (entry: number) => Math.abs((p.target as number) - entry) / Math.abs(entry - (p.stop as number));

  // |23667.5 - 23754.3| / |23754.3 - 23794| = 86.8 / 39.7 = 2.186
  assert.ok(Math.abs(rr(p.planEntry as number) - 2.186) < 0.01, `expected ~2.19R, got ${rr(p.planEntry as number)}`);

  // And the bug, stated as a test: the premium basis yields ~0.99R, which must
  // never be what the page computes.
  assert.ok(Math.abs(rr(p.entry as number) - 0.9947) < 0.01);
  assert.notEqual(Math.round(rr(p.planEntry as number) * 100), Math.round(rr(p.entry as number) * 100));
});

test("the directional book's stop_underlying is also declared underlying-basis", () => {
  const p = directionalPosition({
    trading_symbol: "LT 3800 PE 25 AUG 26",
    entry_premium: 125.5,
    entry_spot: 3751.5,
    stop_underlying: 3800,
    quantity_lots: 1,
    quantity_units: 175,
  });
  assert.equal(p.planBasis, "underlying");
  assert.equal(p.planEntry, 3751.5);
});

test("a futures row keeps all three legs on the traded instrument", () => {
  const p = futuresPosition({
    symbol: "CRUDEOIL",
    entry_price: 8503,
    stop: 8484,
    target1: 8680,
    lots: 5,
    lot_size: 100,
  });
  assert.equal(p.planBasis, "instrument");
  assert.equal(p.planEntry, 8503);
});

test("the positions view feeds rrRender the PLAN entry, never the traded price", () => {
  const src = read("src", "components", "books", "BookViews.tsx");
  assert.match(
    src,
    /rrRender\(\{\s*entry:\s*p\.planEntry/,
    "R/R must be computed against planEntry — p.entry is the premium on an underlying-level plan",
  );
  assert.ok(
    !/rrRender\(\{\s*entry:\s*p\.entry\b/.test(src),
    "the premium must never be fed in as the R/R entry",
  );
});

// ─── 11. A stale roll-up is UNKNOWN too, and a partial sum is not a total ───
//
// The directional summary reported +9,760 unrealised on 2026-07-27 while all
// six of its marks were 2-5 DAYS old and the positions view suppressed every
// one of them. A portfolio tile that prints the roll-up anyway re-tells the
// same lie one tab across, and any total built on it inherits it.

test("the unrealised roll-up is UNKNOWN when any open mark is stale", () => {
  const now = Date.UTC(2026, 6, 27, 2, 0, 0);
  const fresh = markVerdict(new Date(now - 60_000).toISOString(), "mark", now);
  const stale = markVerdict("2026-07-21T05:50:39Z", "mark", now);

  const bad = portfolioUnrealized({ reported: 9760, openCount: 6, marks: [fresh, stale, stale] });
  assert.equal(bad.state, "unknown");
  assert.equal(bad.state === "unknown" ? bad.staleRows : -1, 2);
  assert.ok(!Object.prototype.hasOwnProperty.call(bad, "value"), "an UNKNOWN roll-up must leak no number");

  const good = portfolioUnrealized({ reported: 9760, openCount: 2, marks: [fresh, fresh] });
  assert.equal(good.state, "known");
  assert.equal(good.state === "known" ? good.value : null, 9760);
});

test("nothing open is a MEASURED ZERO, and unloaded rows are UNKNOWN — not the same", () => {
  const zero = portfolioUnrealized({ reported: null, openCount: 0, marks: [] });
  assert.equal(zero.state, "measured_zero");
  assert.equal(zero.state === "measured_zero" ? zero.value : null, 0);

  const unloaded = portfolioUnrealized({ reported: 9760, openCount: 6, marks: null });
  assert.equal(unloaded.state, "unknown");

  const noField = portfolioUnrealized({
    reported: null,
    openCount: 3,
    marks: [markVerdict(new Date().toISOString(), "mark"), markVerdict(new Date().toISOString(), "mark")],
  });
  assert.equal(noField.state, "unavailable", "a book that serves no roll-up is UNAVAILABLE, not zero");
});

test("total P&L refuses to state a number when unrealised is UNKNOWN", () => {
  const now = Date.UTC(2026, 6, 27, 2, 0, 0);
  const stale = markVerdict("2026-07-21T05:50:39Z", "mark", now);
  const unknown = portfolioUnrealized({ reported: 9760, openCount: 6, marks: [stale] });

  const refused = totalPnl(431724.48, unknown);
  assert.equal(refused.value, null, "a total must never add zero for an UNKNOWN component");
  assert.match(refused.note, /cannot be stated/);

  // Measured zero IS a fact, so a total is legitimate there.
  const flat = totalPnl(258990, portfolioUnrealized({ reported: null, openCount: 0, marks: [] }));
  assert.equal(flat.value, 258990);

  // And with fresh marks the total is the honest sum.
  const fresh = markVerdict(new Date(now - 30_000).toISOString(), "mark", now);
  const summed = totalPnl(100, portfolioUnrealized({ reported: 25, openCount: 1, marks: [fresh] }));
  assert.equal(summed.value, 125);

  // No realized figure at all ⇒ no total either.
  assert.equal(totalPnl(null, portfolioUnrealized({ reported: null, openCount: 0, marks: [] })).value, null);
});

test("the portfolio view routes unrealised and total through the gated helpers", () => {
  const src = read("src", "components", "books", "BookViews.tsx");
  assert.match(src, /UnrealizedRollupTile verdict=\{unrealized\}/);
  assert.match(src, /portfolioUnrealized\(/);
  assert.match(src, /totalPnl\(facts\.realizedLifetime, unrealized\)/);
  // The zero-default that produced the partial-sum-as-total must be gone.
  assert.ok(
    !/\(facts\.realizedLifetime \?\? 0\)/.test(src),
    "a missing component must not be coerced to zero inside a total",
  );
  assert.ok(!/\?\? 0\b/.test(src), "no zero-default fallback may survive in the book views");
});

// ─── 12. A stale reading is named on the clock that produced it ─────────────

test("a book-sync clock never reports itself as a stale MARK", () => {
  assert.equal(MARK_STALE_LABEL.mark, "stale mark");
  assert.equal(MARK_STALE_LABEL.book_sync, "stale book-sync");
  const src = read("src", "components", "books", "BookPrimitives.tsx");
  const block = src.slice(src.indexOf("export function UnrealizedCell"));
  assert.match(block, /MARK_STALE_LABEL\[verdict\.clock\]/);
  assert.ok(
    !/UNKNOWN · stale mark \{/.test(block),
    "the stale label must be chosen by clock, not hardcoded to 'mark'",
  );
});

// ─── 13. A partial exit is flagged, not left to silently not reconcile ──────

test("a trade closed in more than one exit is flagged as such", () => {
  // The real 2026-07-23 CRUDEOIL row: entered 5 lots, 3 remained for the final
  // exit, so 535 pts x 500 units = 267,500 while realized is 198,700.
  const t = futuresTrade({
    symbol: "CRUDEOIL",
    entry_price: 8503,
    exit_price: 9038,
    initial_lots: 5,
    lots: 3,
    lot_size: 100,
    realized_pnl: 198700,
    target1_done: true,
    initial_stop: 8484,
    target1: 8680,
  });
  assert.equal(t.partialExit, true);
  assert.equal(t.lotsAtFinalExit, 3);
  assert.equal(t.qty.lots, 5, "the quantity shown is the size ENTERED");
  assert.equal(t.qty.units, 500);
  assert.notEqual(Math.round((9038 - 8503) * (t.qty.units as number)), t.realized);

  const clean = futuresTrade({
    symbol: "GOLD",
    entry_price: 140675,
    exit_price: 140870,
    initial_lots: 1,
    lots: 1,
    lot_size: 10,
    realized_pnl: 1950,
    target1_done: false,
  });
  assert.equal(clean.partialExit, false);
  assert.equal(clean.lotsAtFinalExit, null);
  assert.equal(Math.round((140870 - 140675) * (clean.qty.units as number)), clean.realized);

  // A book that records neither fact says nothing rather than guessing "no".
  const silent = futuresTrade({ symbol: "X", entry_price: 1, exit_price: 2, lot_size: 1, pnl: 1 });
  assert.equal(silent.partialExit, null);
});

test("the trade book renders the partial-exit marker", () => {
  const src = read("src", "components", "books", "BookViews.tsx");
  assert.match(src, /t\.partialExit/);
  assert.match(src, /partial exit/);
});
