/**
 * Navigation / landing grouping honesty tests (2026-07-20).
 *
 * Same runner as the other suites — `node --test` with native TypeScript
 * type-stripping, no framework — so everything is either a pure module
 * assertion or a source-text assertion over the rendered files.
 *
 * What these lock down:
 *   1. The workspace is a PRIMARY destination: linked at the top of the rail,
 *      outside any collapsible group, and on the landing page.
 *   2. Both axes are expressed and neither is invented: HORIZON comes from
 *      lane-taxonomy, KIND is resolved from the SERVED registry and reports
 *      `registryUnavailable` rather than guessing.
 *   3. Scalp is present, empty, permanent, and carries the capability reason.
 *   4. Every route the old flat nav linked is still linked (no bookmark loses
 *      its entry), and the parked ones are LINKED and labelled PARKED.
 *   5. The four desk-card states are distinct: a 404 endpoint, a failed fetch,
 *      a measured-flat book and a parked lane can never collapse into one
 *      em-dash again.
 *   6. Every declared book endpoint is one the backend actually serves.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  BOOK_FIELDS,
  CURRENCY_SYMBOL,
  DESK_CARD_STATE_VARIANT,
  LANE_SECTIONS,
  deskCurrency,
  WORKSPACE_ROUTE,
  WORKSPACE_VIEWS,
  allDesks,
  deskCardState,
  deskKinds,
  deskPolicyLabel,
  deskTotalPnl,
  normalizeBook,
  policyRank,
  reportingTally,
  type NavDesk,
} from "../src/lib/nav-model.ts";
import {
  HORIZON_LABEL,
  KIND_ORDER,
  LANE_HORIZON,
  SCALP_UNAVAILABLE,
  TRADING_HORIZONS,
  laneHorizons,
} from "../src/lib/lane-taxonomy.ts";
import { POLICY_COLUMNS, POLICY_COLUMN_LABEL } from "../src/lib/policy-state.ts";
import { MISSING_CAPABILITY_KEYS } from "../src/lib/flow-provenance.ts";

const ROOT = path.join(import.meta.dirname, "..");
const read = (...p: string[]) => readFileSync(path.join(ROOT, ...p), "utf8");
const sidebar = () => read("src", "components", "layout", "Sidebar.tsx");
const landing = () => read("src", "app", "page.tsx");

const desk = (href: string): NavDesk => {
  const d = allDesks().find((x) => x.href === href);
  assert.ok(d, `no desk declared for ${href}`);
  return d;
};

// ─── 1. The workspace is primary ────────────────────────────────────────────

test("workspace route is the declared primary destination", () => {
  assert.equal(WORKSPACE_ROUTE, "/strategies/market-structure");
  // It is NOT one of the collapsible desk sections — it must not be hideable.
  for (const s of LANE_SECTIONS) {
    assert.ok(
      !s.desks.some((d) => d.href.startsWith(WORKSPACE_ROUTE)),
      `workspace must not live inside collapsible section "${s.id}"`,
    );
  }
});

test("sidebar renders the workspace outside any collapsible group", () => {
  const src = sidebar();
  assert.match(src, /WORKSPACE_ROUTE/);
  // The workspace block must not be gated on an `isOpen(...)` check.
  const block = src.slice(src.indexOf("Workspace: the primary destination"), src.indexOf("LANE_SECTIONS.map"));
  assert.ok(block.length > 200, "workspace block not found in the sidebar");
  assert.ok(!/isOpen\(/.test(block), "the workspace section must never be collapsible");
});

test("landing page leads with the workspace and its built views", () => {
  const src = landing();
  assert.match(src, /WORKSPACE_ROUTE/);
  assert.match(src, /WORKSPACE_VIEWS/);
  // The workspace hero must appear before the desk sections.
  assert.ok(
    src.indexOf("WORKSPACE_ROUTE") < src.indexOf("LANE_SECTIONS.map"),
    "the workspace must be rendered before the desk sections",
  );
});

test("advertised workspace views are exactly the ones ViewNav marks BUILT", () => {
  const nav = read("src", "components", "market-structure", "ViewNav.tsx");
  const map = nav.slice(nav.indexOf("const BUILT"), nav.indexOf("export function ViewNav"));
  const built = Array.from(map.matchAll(/(\w+):\s*(true|false)/g))
    .filter(([, , v]) => v === "true")
    .map(([, k]) => k);
  assert.deepEqual(WORKSPACE_VIEWS.map((v) => v.view).sort(), built.sort());
});

// ─── 2. Both axes, neither invented ─────────────────────────────────────────

test("every section that declares a horizon uses a horizon lane-taxonomy knows", () => {
  for (const s of LANE_SECTIONS) {
    if (s.horizon === null) continue;
    assert.ok(HORIZON_LABEL[s.horizon], `unknown horizon "${s.horizon}" on section ${s.id}`);
  }
});

test("every trading horizon has a section — including the unsatisfiable one", () => {
  const covered = new Set(LANE_SECTIONS.map((s) => s.horizon).filter(Boolean));
  for (const h of TRADING_HORIZONS) {
    assert.ok(covered.has(h), `no nav section expresses the "${h}" horizon`);
  }
});

test("a desk sits in a section whose horizon its lanes actually declare", () => {
  for (const s of LANE_SECTIONS) {
    if (!s.horizon) continue;
    for (const d of s.desks) {
      if (d.laneKeys.length === 0) continue;
      const declared = new Set(d.laneKeys.flatMap((k) => laneHorizons(k)));
      assert.ok(
        declared.has(s.horizon),
        `${d.href} is under "${s.horizon}" but its lanes declare ${Array.from(declared).join(", ")}`,
      );
    }
  }
});

test("every declared lane key exists in the horizon table", () => {
  for (const d of allDesks()) {
    for (const k of d.laneKeys) {
      assert.ok(LANE_HORIZON[k], `${d.href} claims undeclared lane key "${k}"`);
    }
  }
});

test("KIND is resolved from the served registry and admits when it cannot be", () => {
  const d = desk("/strategies/auction");
  // No registry at all → say so, never guess a kind.
  const blind = deskKinds(d, {});
  assert.equal(blind.registryUnavailable, true);
  assert.deepEqual(blind.kinds, []);
  assert.deepEqual(blind.unresolved, d.laneKeys);

  // Registry present but this lane absent → unresolved, still no guess.
  const partial = deskKinds(d, { some_other_lane: "monitor" });
  assert.equal(partial.registryUnavailable, false);
  assert.deepEqual(partial.kinds, []);
  assert.deepEqual(partial.unresolved, d.laneKeys);

  // Served kinds come back in KIND_ORDER, deduplicated.
  const served = deskKinds(d, {
    auction_intelligence: "scheduler-runner",
    auction_intelligence_commodity: "scheduler-runner",
    rl_auto_trainer: "monitor",
  });
  assert.deepEqual(served.kinds, ["scheduler-runner", "monitor"]);
  assert.deepEqual(served.unresolved, []);
  for (const k of served.kinds) assert.ok(KIND_ORDER.includes(k));
});

test("the sidebar renders the kind axis from the registry, not a local table", () => {
  const src = sidebar();
  assert.match(src, /useLaneRegistry/);
  assert.match(src, /deskKinds\(/);
  assert.match(src, /kind unavailable/);
});

// ─── 3. Policies ────────────────────────────────────────────────────────────

test("all four policy columns have a desk, and MP+OF has two", () => {
  const byPolicy = new Map<string, NavDesk[]>();
  for (const d of allDesks()) {
    if (!d.policy) continue;
    byPolicy.set(d.policy, [...(byPolicy.get(d.policy) ?? []), d]);
  }
  for (const p of POLICY_COLUMNS) {
    assert.ok((byPolicy.get(p) ?? []).length > 0, `policy column "${p}" has no desk`);
  }
  assert.equal(byPolicy.get("mpof")!.length, 2, "MP+OF is two policy ids and must be two desks");
});

test("policy terminals sort before non-policy desks", () => {
  const intraday = LANE_SECTIONS.find((s) => s.id === "intraday")!;
  const ranks = [...intraday.desks].sort((a, b) => policyRank(a) - policyRank(b)).map(policyRank);
  assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b));
  // The S1 signal engine is not a policy and must sort last.
  assert.equal(policyRank(desk("/strategies/nse/live")), POLICY_COLUMNS.length);
});

test("policy chips reuse POLICY_COLUMN_LABEL rather than a bespoke string", () => {
  const d = desk("/strategies/commodity");
  assert.ok(deskPolicyLabel(d)!.startsWith(POLICY_COLUMN_LABEL.mpof));
  assert.equal(deskPolicyLabel(desk("/strategies/cbe")), null);
});

test("the dual-horizon directional lane is listed at BOTH of its horizons", () => {
  const hrefs = allDesks()
    .filter((d) => d.policy === "directional")
    .map((d) => d.href);
  assert.equal(hrefs.length, 2);
  const intraday = LANE_SECTIONS.find((s) => s.id === "intraday")!.desks.some((d) => d.policy === "directional");
  const positional = LANE_SECTIONS.find((s) => s.id === "positional")!.desks.some((d) => d.policy === "directional");
  assert.ok(intraday && positional);
});

// ─── 4. Scalp: permanently unavailable, with the reason ─────────────────────

test("scalp is a section, is empty, and is never merely empty", () => {
  const scalp = LANE_SECTIONS.find((s) => s.id === "scalp");
  assert.ok(scalp, "there is no scalp section at all");
  assert.equal(scalp.horizon, "scalp");
  assert.deepEqual(scalp.desks, [], "scalp must have no desks");
  assert.ok(scalp.unavailable, "an empty scalp section without a reason reads as a backlog item");
  assert.equal(scalp.unavailable, SCALP_UNAVAILABLE);
  assert.equal(scalp.unavailable.permanent, true);
});

test("scalp's reason cites real capability records", () => {
  assert.ok(SCALP_UNAVAILABLE.missingCapabilities.length > 0);
  for (const c of SCALP_UNAVAILABLE.missingCapabilities) {
    assert.ok(MISSING_CAPABILITY_KEYS.includes(c), `${c} is not a declared capability key`);
  }
});

test("no scalp desk may ever be added without removing the unavailability record", () => {
  for (const s of LANE_SECTIONS) {
    if (s.unavailable) assert.equal(s.desks.length, 0, `section ${s.id} is unavailable but lists desks`);
  }
});

test("both surfaces render the scalp reason, not just the word 'scalp'", () => {
  for (const [name, src] of [["sidebar", sidebar()], ["landing", landing()]] as const) {
    assert.match(src, /section\.unavailable/, `${name} does not branch on the unavailability record`);
    assert.match(src, /missingCapabilities/, `${name} does not render the missing capabilities`);
  }
  assert.match(landing(), /u\.reason/);
  assert.match(landing(), /u\.citation/);
});

// ─── 5. Nothing lost, parked labelled ───────────────────────────────────────

/** Every strategy route the pre-2026-07-20 flat nav linked or had parked. */
const LEGACY_ROUTES = [
  "/strategies/market-structure",
  "/strategies/overview",
  "/strategies/nse/live",
  "/strategies/macd-refined",
  "/strategies/us-macd-refined",
  "/strategies/directional",
  "/strategies/auction",
  "/strategies/gann",
  "/strategies/mp",
  "/strategies/cbe",
  "/strategies/institutional-convergence",
  "/strategies/commodity",
  "/strategies/fractal",
  "/strategies/sniper",
];

test("every legacy strategy route is still linked somewhere in the model", () => {
  const linked = [WORKSPACE_ROUTE, ...allDesks().map((d) => d.href.split("?")[0])];
  for (const r of LEGACY_ROUTES) {
    assert.ok(linked.includes(r), `${r} lost its nav entry`);
  }
});

test("parked desks read PARKED and say why", () => {
  const parked = allDesks().filter((d) => d.status === "parked");
  assert.ok(parked.length >= 3, "US MACD Refined, Fractal MP and Sniper are all parked");
  for (const d of parked) {
    assert.ok(d.parkedReason && d.parkedReason.length > 40, `${d.href} is parked without a stated reason`);
    assert.match(d.parkedReason, /PARKED/);
  }
  const us = desk("/strategies/us-macd-refined");
  assert.equal(us.status, "parked");
  // A parked lane says PARKED even when its (flat) book resolves — "0 open" is
  // not the interesting fact about a lane that will not trade tomorrow.
  const card = deskCardState(us, { status: "ok", payload: { open_positions: 0, realized_pnl: 0, total_trades: 0 } });
  assert.equal(card.state, "PARKED");
  assert.match(card.reason, /PARKED/);
});

test("the two previously commented-out routes are linked again", () => {
  const hrefs = allDesks().map((d) => d.href);
  assert.ok(hrefs.includes("/strategies/fractal"));
  assert.ok(hrefs.includes("/strategies/sniper"));
});

test("desks that overlap a workspace view justify why they are kept", () => {
  for (const d of allDesks()) {
    assert.ok(d.note && d.note.length > 30, `${d.href} has no stated reason to exist`);
  }
});

// ─── 6. Card states are distinct ────────────────────────────────────────────

test("a desk with no book endpoint is NO_BOOK, not a flat book", () => {
  const mp = desk("/strategies/mp");
  assert.equal(mp.book, null);
  const card = deskCardState(mp, null);
  assert.equal(card.state, "NO_BOOK");
  assert.match(card.reason, /No paper-book endpoint/);
  assert.equal(card.fields, null);
  assert.equal(deskTotalPnl(card).value, null);
});

test("a failed fetch is NOT_REPORTING and says it is not a flat book", () => {
  const card = deskCardState(desk("/strategies/cbe"), { status: "error", detail: "HTTP 404" });
  assert.equal(card.state, "NOT_REPORTING");
  assert.match(card.reason, /HTTP 404/);
  assert.match(card.reason, /not a flat book/);
  assert.equal(deskTotalPnl(card).value, null);
});

test("a measured-flat book is NO_POSITIONS and is NOT the same as not reporting", () => {
  const card = deskCardState(desk("/strategies/cbe"), {
    status: "ok",
    payload: { open_positions: 0, closed_positions: 0, realized_pnl: 0, unrealized_pnl: 0 },
  });
  assert.equal(card.state, "NO_POSITIONS");
  assert.match(card.reason, /MEASURED-flat/);
  assert.equal(deskTotalPnl(card).value, 0);
  assert.equal(deskTotalPnl(card).complete, true);
});

test("a live book is REPORTING", () => {
  const card = deskCardState(desk("/strategies/cbe"), {
    status: "ok",
    payload: { open_positions: 10, closed_positions: 51, realized_pnl: -100, unrealized_pnl: 25 },
  });
  assert.equal(card.state, "REPORTING");
  assert.equal(card.fields!.openPositions, 10);
  assert.equal(deskTotalPnl(card).value, -75);
});

test("the card states are distinct labels and only REPORTING is green", () => {
  const states = [
    "PARKED",
    "NO_BOOK",
    "LOADING",
    "NOT_REPORTING",
    "NO_POSITIONS",
    "BOOK_PARTIAL",
    "REPORTING",
  ] as const;
  const labels = new Set(states.map((s) => s));
  assert.equal(labels.size, states.length);
  // Every declared state must be covered by the variant map — no state may
  // reach the UI without a colour decision having been made for it.
  assert.deepEqual(new Set(Object.keys(DESK_CARD_STATE_VARIANT)), new Set(states));
  // GREEN is reserved for reporting-live only.
  assert.equal(DESK_CARD_STATE_VARIANT.REPORTING, "success");
  for (const s of states) {
    if (s !== "REPORTING") assert.notEqual(DESK_CARD_STATE_VARIANT[s], "success", `${s} must not be green`);
  }
});

// ─── Green is never painted over an all-UNAVAILABLE card ────────────────────

test("a book that carries no P&L and no open count is BOOK_PARTIAL, not green", () => {
  // This is exactly /api/strategy/portfolio (S1): it carries final_equity and
  // total_trades but no realized/unrealized P&L and no open-position count.
  // Before this state existed the card rendered a GREEN "Reporting" badge above
  // "P&L UNAVAILABLE" and "open UNAVAILABLE" — health the payload cannot support.
  const card = deskCardState(desk("/strategies/nse/live"), {
    status: "ok",
    payload: { final_equity: 908205, total_trades: 33 },
  });
  assert.equal(card.state, "BOOK_PARTIAL");
  assert.notEqual(DESK_CARD_STATE_VARIANT[card.state], "success");
  assert.equal(deskTotalPnl(card).value, null, "no P&L may be synthesised from equity");
  assert.equal(card.fields!.openPositions, null, "open count stays UNAVAILABLE, never 0");
  assert.match(card.reason, /carries no P&L and no open-position count/);
  assert.match(card.reason, /totalEquity/, "the reason must name what the payload DOES carry");
});

test("a book carrying only an open count still REPORTS", () => {
  const card = deskCardState(desk("/strategies/cbe"), {
    status: "ok",
    payload: { open_positions: 4 },
  });
  assert.equal(card.state, "REPORTING");
});

// ─── Currency: a USD book is never summed into the ₹ roll-up ────────────────

test("the US lane declares USD and is the only non-INR book", () => {
  const us = desk("/strategies/us-macd-refined");
  assert.equal(deskCurrency(us), "USD", "US MACD Refined paper capital is USD, not INR");
  const nonInr = allDesks().filter((d) => d.book && deskCurrency(d) !== "INR");
  assert.deepEqual(
    nonInr.map((d) => d.href),
    ["/strategies/us-macd-refined"],
  );
  // Everything else must be INR by declaration or by default.
  for (const d of allDesks()) {
    if (d.href === us.href) continue;
    assert.equal(deskCurrency(d), "INR", `${d.href} must be an INR book`);
  }
  assert.equal(CURRENCY_SYMBOL.USD, "$");
  assert.equal(CURRENCY_SYMBOL.INR, "₹");
});

test("the INR roll-up excludes every non-INR endpoint", () => {
  // Mirrors the landing page's INR_ENDPOINTS derivation exactly.
  const all = Array.from(new Set(allDesks().filter((d) => d.book).map((d) => d.book!.endpoint)));
  const inr = all.filter((e) =>
    allDesks().some((d) => d.book?.endpoint === e && deskCurrency(d) === "INR"),
  );
  assert.ok(all.includes("/api/us/macd-refined/paper-summary"));
  assert.ok(
    !inr.includes("/api/us/macd-refined/paper-summary"),
    "a USD book must never enter the ₹ total — that asserts $1 = ₹1",
  );
  assert.equal(inr.length, all.length - 1);
});

test("a missing number is NEVER coerced to zero", () => {
  // /api/institutional-convergence/paper carries no unrealized_pnl at all.
  const b = normalizeBook({ realized_pnl: 0, open_count: 0 }, []);
  assert.equal(b.realizedPnl, 0, "a measured zero survives as zero");
  assert.equal(b.openPositions, 0);
  assert.equal(b.unrealizedPnl, null, "an absent field must stay null, not become 0");
  assert.equal(b.totalEquity, null);
  // A nested path that does not exist yields all-null, not zeros.
  const missing = normalizeBook({ nothing: true }, ["summary"]);
  for (const f of BOOK_FIELDS) assert.equal(missing[f], null);
});

test("a partial P&L is flagged partial rather than silently summed", () => {
  const card = deskCardState(desk("/strategies/institutional-convergence"), {
    status: "ok",
    payload: { realized_pnl: 1234, open_count: 0, closed_count: 3 },
  });
  const pnl = deskTotalPnl(card);
  assert.equal(pnl.value, 1234);
  assert.equal(pnl.complete, false);
  assert.deepEqual(pnl.missing, ["unrealizedPnl"]);
});

test("the tally separates reporting from askable, parked and no-book", () => {
  const cards = [
    deskCardState(desk("/strategies/cbe"), { status: "ok", payload: { open_positions: 1, realized_pnl: 5 } }),
    deskCardState(desk("/strategies/gann"), { status: "error", detail: "HTTP 500" }),
    deskCardState(desk("/strategies/mp"), null),
    deskCardState(desk("/strategies/us-macd-refined"), { status: "pending" }),
  ];
  assert.deepEqual(reportingTally(cards), {
    reporting: 1,
    askable: 2,
    notReporting: 1,
    partial: 0,
    parked: 1,
    noBook: 1,
  });
});

// ─── 7. Declared endpoints must be ones the backend serves ──────────────────

/**
 * Verified against the live OpenAPI schema on 2026-07-20. The five endpoints
 * the old landing page polled are listed as KNOWN-404 so nobody re-adds them.
 */
const SERVED_ENDPOINTS = new Set([
  "/api/auction-intelligence/paper-status",
  "/api/commodity/strategy-agent/status",
  "/api/institutional-convergence/paper",
  "/api/directional-options/paper-summary",
  "/api/strategy/portfolio",
  "/api/macd-refined/paper-summary",
  "/api/gann-tp-delta/paper-agent/status",
  "/api/cbe/paper-summary",
  "/api/us/macd-refined/paper-summary",
]);

const KNOWN_404 = [
  "/api/strategy/paper-summary",
  "/api/gann-tp-delta/paper-summary",
  "/api/commodity-strategy/paper-summary",
  "/api/auction-intelligence/paper-summary",
  "/api/mp-intelligence/paper-summary",
];

test("every declared book endpoint is one the backend actually serves", () => {
  for (const d of allDesks()) {
    if (!d.book) {
      assert.ok(d.noBookReason && d.noBookReason.length > 20, `${d.href} has no book and no reason`);
      continue;
    }
    assert.ok(SERVED_ENDPOINTS.has(d.book.endpoint), `${d.href} points at unserved ${d.book.endpoint}`);
  }
});

test("the endpoints that 404 are not referenced by either surface", () => {
  const src = `${landing()}\n${sidebar()}\n${read("src", "lib", "nav-model.ts")}`;
  for (const bad of KNOWN_404) {
    // They may appear in a comment explaining the fix, but never as a value in
    // a declared BookSource.
    assert.ok(!new RegExp(`endpoint:\\s*"${bad}"`).test(src), `${bad} is still declared as a book endpoint`);
  }
});
