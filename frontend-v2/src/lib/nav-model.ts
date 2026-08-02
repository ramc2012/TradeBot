/**
 * nav-model — the ONE navigation + landing-card grouping model.
 *
 * ─── Why this module exists (2026-07-20) ────────────────────────────────────
 *
 * The sidebar used to be a hand-kept flat list of thirteen "strategy desks"
 * and the landing page a hand-kept list of seven cards. Neither expressed the
 * lane logic the rest of the terminal is built on, the two lists disagreed
 * with each other, and the cross-lane workspace (/strategies/market-structure)
 * was buried inside a group that is COLLAPSED on the landing route — so the
 * owner opened the terminal and saw none of it.
 *
 * This module is the single grouping model both surfaces read. It is PURE
 * (no React, no network, no `@/` imports) so `node --test` can assert the
 * whole model, exactly like lane-taxonomy and policy-state.
 *
 * ─── The two axes, neither invented here ────────────────────────────────────
 *
 *   HORIZON — the declared axis from lib/lane-taxonomy.ts. Sections ARE
 *   horizons: intraday / swing / positional, plus `scalp`, which is present,
 *   empty and PERMANENTLY unavailable with the capability reason attached
 *   (SCALP_UNAVAILABLE). It is never rendered as "nothing here yet".
 *
 *   KIND — strategy-engine / scheduler-runner / product-lane / monitor. This
 *   axis is SERVED by /api/system/lanes and must never be re-declared. So a
 *   desk here declares only the LANE KEYS it is the terminal for, and
 *   `deskKinds()` resolves the kinds from the live registry map. With no
 *   registry loaded a desk reports NO kind rather than a guessed one.
 *
 * ─── Policies ───────────────────────────────────────────────────────────────
 *
 * Inside a horizon, the four policy columns of the Strategies matrix
 * (POLICY_COLUMNS) sort first, so the nav and the workspace agree on what the
 * primary decision surfaces are. MP+OF is TWO policy ids over one column
 * (index long-premium and commodity futures) and therefore two desks — the
 * split is real, not cosmetic.
 *
 * Desks that are the only home of their function but are NOT one of the four
 * policies (Gann, CBE, S1) keep their route and sit in their horizon with
 * `policy: null`. Nothing is deleted; every previously-linked route is still
 * reachable, and the parked ones are LINKED and labelled PARKED rather than
 * left as unreachable pages on disk.
 *
 * ─── Books ──────────────────────────────────────────────────────────────────
 *
 * Each desk declares where its paper book actually comes from, verified
 * against the live OpenAPI schema on 2026-07-20. Five of the seven landing
 * cards were pointed at endpoints that 404 (`/api/strategy/paper-summary`,
 * `/api/gann-tp-delta/paper-summary`, `/api/commodity-strategy/paper-summary`,
 * `/api/auction-intelligence/paper-summary`, `/api/mp-intelligence/paper-summary`)
 * and rendered an em-dash for it — an em-dash that was indistinguishable from
 * a flat book. Those are different states and they now say which one they are.
 */
import {
  HORIZON_BLURB,
  HORIZON_LABEL,
  KIND_ORDER,
  SCALP_UNAVAILABLE,
  type HorizonUnavailable,
  type LaneHorizon,
} from "./lane-taxonomy.ts";
import { POLICY_COLUMNS, POLICY_COLUMN_LABEL, type PolicyColumnId } from "./policy-state.ts";

// ─── The workspace: the primary destination ─────────────────────────────────

export const WORKSPACE_ROUTE = "/strategies/market-structure";

/**
 * The workspace views that are BUILT. Kept in lockstep with the `BUILT` map in
 * components/market-structure/ViewNav.tsx — tests/nav-model.test.ts asserts the
 * two agree by reading that file, so a view cannot be advertised here before it
 * exists. Risk & Execution and Research are deliberately absent: they are
 * scaffolds that deep-link back to /trading, /positions, /research, /analytics,
 * and those legacy routes remain the only home of those functions.
 */
export const WORKSPACE_VIEWS: { view: string; label: string; href: string; blurb: string }[] = [
  {
    view: "command",
    label: "Command",
    href: `${WORKSPACE_ROUTE}`,
    blurb: "Every instrument, every lane's read on it, in one matrix.",
  },
  {
    view: "structure",
    label: "Structure",
    href: `${WORKSPACE_ROUTE}?view=structure`,
    blurb: "Linked price + flow panes and the profile workbench.",
  },
  {
    view: "flow",
    label: "Flow",
    href: `${WORKSPACE_ROUTE}?view=flow`,
    blurb: "CVD, footprint, absorption — all graded as inferred from quotes.",
  },
  {
    view: "strategies",
    label: "Strategies",
    href: `${WORKSPACE_ROUTE}?view=strategies`,
    blurb: "Horizon rows × the four policy columns, with the disagreement strip.",
  },
];

// ─── Desks ──────────────────────────────────────────────────────────────────

export type DeskStatus = "active" | "parked";

/**
 * The currency a book is denominated in. NOT cosmetic: the US lane's paper
 * capital is USD (macd_refined/config.py MACD_REFINED_US_INITIAL_CAPITAL =
 * 100_000.0 "USD paper capital"), so its equity must never be added to the INR
 * cross-lane roll-up. Doing so silently asserted $1 = ₹1.
 */
export type BookCurrency = "INR" | "USD";

export const CURRENCY_SYMBOL: Record<BookCurrency, string> = { INR: "₹", USD: "$" };

/** Where a desk's paper book comes from, or why it has none. */
export type BookSource = {
  /** Verified to exist in the served OpenAPI schema. */
  endpoint: string;
  /** Keys to walk into the payload before reading the book fields. */
  path: string[];
  /** Fields the payload genuinely does NOT carry, so the card can say so. */
  absent?: BookField[];
  /** Denomination of this book. Defaults to INR; declare it when it is not. */
  currency?: BookCurrency;
};

/** The declared denomination of a desk's book (INR unless stated otherwise). */
export function deskCurrency(desk: NavDesk): BookCurrency {
  return desk.book?.currency ?? "INR";
}

/**
 * A desk's BOOKS page — order / trade / position / portfolio over the lane's
 * authoritative paper book.
 *
 * Declared here rather than left as a bare route because the last books-style
 * UI shipped INVISIBLE: it existed on disk and was reachable only through a
 * collapsed group. A desk that declares `books` gets an indented child line in
 * the rail, inside its (already open) section, and a link on its landing card.
 */
export type NavBooks = {
  href: string;
  label: string;
  /** The four views the page carries, matching lib/lane-books BOOK_VIEWS. */
  views: string[];
  blurb: string;
};

export type NavDesk = {
  href: string;
  label: string;
  /** Additional pathnames this entry owns, so no bookmark loses its highlight. */
  matchers?: string[];
  /** One of the four policy columns, when this desk is a terminal for one. */
  policy: PolicyColumnId | null;
  /** Sub-label when two desks share a policy column (MP+OF index vs commodity). */
  policyScope?: string;
  /**
   * Lane keys this desk is the home of. The KIND axis is resolved from these
   * against the SERVED registry — never declared here.
   */
  laneKeys: string[];
  status: DeskStatus;
  /** Required when status === "parked". Rendered, not hidden. */
  parkedReason?: string;
  /** Why this desk still exists alongside the workspace. */
  note: string;
  book: BookSource | null;
  /** Required when `book` is null: why there is no book to show. */
  noBookReason?: string;
  /** The four-view books page for this lane's authoritative book, if it has one. */
  books?: NavBooks;
};

/** The views every books page carries. Kept in lockstep with lib/lane-books. */
export const BOOKS_VIEWS = ["orders", "trades", "positions", "portfolio"];

function booksFor(base: string, label: string, blurb: string): NavBooks {
  return { href: `${base}/books`, label, views: [...BOOKS_VIEWS], blurb };
}

export type NavSection = {
  id: string;
  title: string;
  blurb: string;
  /** The horizon this section expresses, when it expresses one. */
  horizon: LaneHorizon | null;
  desks: NavDesk[];
  /** Non-null ⇒ this section can NEVER be populated on today's feeds. */
  unavailable: HorizonUnavailable | null;
  /** Open on first paint, before any route match or stored override. */
  defaultOpen: boolean;
};

const INTRADAY_DESKS: NavDesk[] = [
  {
    href: "/strategies/auction",
    label: "Auction IQ",
    policy: "auction",
    laneKeys: ["auction_intelligence", "auction_intelligence_commodity", "rl_auto_trainer"],
    status: "active",
    note: "Sole home of the auction policy bundle and the RL auto-trainer; the workspace only summarizes its regime + decision.",
    book: { endpoint: "/api/auction-intelligence/paper-status", path: ["summary"] },
    books: booksFor(
      "/strategies/auction",
      "Books",
      "Order / trade / position / portfolio over BOTH auction books: the NSE index runtime JSON and the MCX commodity book that holds the CRUDEOIL trade the roll-up was missing.",
    ),
  },
  {
    href: "/strategies/mp",
    label: "MP + OF · index",
    policy: "mpof",
    policyScope: "index long premium",
    // s2_index_mp_macd RETIRED from the lane registry 2026-07-20 (owner: only
    // MACD and MACD-refined survive). The desk stays as a read-only board; it
    // simply no longer claims a registry lane.
    laneKeys: [],
    status: "active",
    note: "Index market-profile + order-flow board. Read-only: it has no registry lane and no paper book.",
    book: null,
    noBookReason:
      "The s2_index_mp_macd lane was retired 2026-07-20; the backend serves no paper-book endpoint for the index MP lane (/api/mp-intelligence/paper-summary 404s).",
  },
  {
    href: "/strategies/commodity",
    label: "MP + OF · commodity",
    policy: "mpof",
    policyScope: "MCX futures",
    laneKeys: ["commodity_mp_orderflow", "commodity_mp_history", "commodity_mark_refresh"],
    status: "active",
    note: "Sole home of the MCX MP+OF book, its HTF value-area gate and the durable TPO history.",
    book: { endpoint: "/api/commodity/strategy-agent/status", path: ["summary"] },
  },
  {
    href: "/strategies/institutional-convergence",
    label: "Convergence",
    policy: "convergence",
    laneKeys: ["institutional_convergence", "institutional_convergence_commodity"],
    status: "active",
    note: "Per-symbol gate ladder and setup lifecycle in full depth; the workspace shows only the resulting cell.",
    book: {
      endpoint: "/api/institutional-convergence/paper",
      path: [],
      absent: ["unrealizedPnl", "totalEquity"],
    },
    books: booksFor(
      "/strategies/institutional-convergence",
      "Books",
      "Order / trade / position / portfolio over both convergence books. The NSE book is enabled, running and has NEVER fired — a measured zero, shown as one, with the gate ladder that explains it.",
    ),
  },
  {
    href: "/strategies/directional",
    label: "Long Premium · intraday",
    policy: "directional",
    policyScope: "weekly DTE window",
    laneKeys: ["directional_options"],
    status: "active",
    note: "Sole home of the directional options lane. Dual-horizon by design — it also appears under Positional.",
    book: { endpoint: "/api/directional-options/paper-summary", path: [] },
    books: booksFor(
      "/strategies/directional",
      "Books",
      "Order / trade / position / portfolio over directional_paper_positions. The order view is a DECISION log (approved/declined + reason) because this lane has no order layer.",
    ),
  },
  {
    href: "/strategies/nse/live",
    label: "MACD Strategy (S1)",
    matchers: ["/strategies/nse"],
    policy: null,
    laneKeys: ["s1_atm_30m_macd"],
    status: "active",
    note: "Signal engine, not one of the four policies: no policy column covers S1, so this desk is its only surface.",
    book: {
      endpoint: "/api/strategy/portfolio",
      path: [],
      absent: ["openPositions", "realizedPnl", "unrealizedPnl"],
    },
  },
];

const SWING_DESKS: NavDesk[] = [
  {
    href: "/strategies/macd-refined",
    label: "MACD Refined",
    policy: null,
    laneKeys: ["macd_refined", "macd_refined_marks"],
    status: "active",
    note: "Sole home of the weekly-expiry long-premium swing lane and its 45s exit monitor.",
    book: { endpoint: "/api/macd-refined/paper-summary", path: [] },
  },
  {
    href: "/strategies/gann",
    label: "Gann TP Delta",
    policy: null,
    laneKeys: ["gann_tp_delta"],
    status: "active",
    note: "No policy column covers Gann and no workspace view renders it — this desk is its only surface.",
    book: { endpoint: "/api/gann-tp-delta/paper-agent/status", path: ["summary"] },
  },
];

const POSITIONAL_DESKS: NavDesk[] = [
  {
    href: "/strategies/directional?horizon=positional",
    label: "Long Premium · positional",
    matchers: ["/strategies/directional"],
    policy: "directional",
    policyScope: "monthly DTE window",
    laneKeys: ["directional_positioning"],
    status: "active",
    note: "Same desk, the other declared horizon: DirectionalSignal.positional selects the MONTHLY DTE window. Listed twice because the lane genuinely answers twice.",
    book: { endpoint: "/api/directional-options/paper-summary", path: [] },
  },
  {
    href: "/strategies/cbe",
    label: "CBE Scanner",
    policy: null,
    laneKeys: ["cbe_scanner", "cbe_marks"],
    status: "active",
    note: "Positional cash-equity book. No policy column and no workspace view covers it.",
    book: { endpoint: "/api/cbe/paper-summary", path: [] },
  },
];

const PARKED_DESKS: NavDesk[] = [
  // US MACD Refined desk REMOVED 2026-07-20 with the us_macd_refined lane.
  {
    href: "/strategies/fractal",
    label: "Fractal MP",
    policy: null,
    laneKeys: ["fractal_market_profile"],
    status: "parked",
    parkedReason:
      "PARKED 2026-07-07 out of production; the registry reports execution_mode=\"parked\". The nav link was commented out, which made a live page unreachable.",
    note: "Session-profile lane, preserved on disk and now linked again under this section.",
    book: null,
    noBookReason:
      "the backend serves /api/fractal-market-profile/paper-positions and /paper-journal but no paper-summary, so there is no roll-up to show.",
  },
  {
    href: "/strategies/sniper",
    label: "Sniper",
    policy: null,
    laneKeys: [],
    status: "parked",
    parkedReason:
      "PARKED 2026-07-07 out of production. It has NO key in /api/system/lanes at all, so it has no served kind and no runtime status.",
    note: "Page preserved on disk and linked here rather than left dangling.",
    book: null,
    noBookReason: "no lane, no runner and no paper endpoint — there is nothing to report.",
  },
];

const BOOK_DESKS: NavDesk[] = [
  {
    href: "/strategies/overview",
    label: "Strategy overview",
    policy: null,
    laneKeys: [],
    status: "active",
    note: "Cross-lane P&L and the lane inventory table. It is a BOOK roll-up, not a decision surface — the workspace is the decision surface.",
    book: null,
    noBookReason: "this desk composes every other desk's book; it has no book of its own.",
  },
];

/** The horizon-organised sections. The nav and the landing page share these. */
export const LANE_SECTIONS: NavSection[] = [
  {
    id: "intraday",
    title: `Policy desks · ${HORIZON_LABEL.intraday}`,
    blurb: HORIZON_BLURB.intraday,
    horizon: "intraday",
    desks: INTRADAY_DESKS,
    unavailable: null,
    defaultOpen: true,
  },
  {
    id: "swing",
    title: `Desks · ${HORIZON_LABEL.swing}`,
    blurb: HORIZON_BLURB.swing,
    horizon: "swing",
    desks: SWING_DESKS,
    unavailable: null,
    defaultOpen: false,
  },
  {
    id: "positional",
    title: `Desks · ${HORIZON_LABEL.positional}`,
    blurb: HORIZON_BLURB.positional,
    horizon: "positional",
    desks: POSITIONAL_DESKS,
    unavailable: null,
    defaultOpen: false,
  },
  {
    // Present, empty, and PERMANENTLY so — with the capability reason attached.
    // Rendered as a labelled unavailable row, never as "no lanes yet", because
    // a scalp lane needs aggressor prints and/or real L2 depth and neither
    // exists on any wired feed.
    id: "scalp",
    title: `${HORIZON_LABEL.scalp} — unavailable`,
    blurb: HORIZON_BLURB.scalp,
    horizon: "scalp",
    desks: [],
    unavailable: SCALP_UNAVAILABLE,
    // Open by default ON PURPOSE: a collapsed empty group reads as a backlog
    // item. The reason must be reachable without hunting for it.
    defaultOpen: true,
  },
  {
    id: "book",
    title: "Book & P&L",
    blurb: "Roll-ups over every desk's paper book. No decisions are made here.",
    horizon: null,
    desks: BOOK_DESKS,
    unavailable: null,
    defaultOpen: false,
  },
  {
    id: "parked",
    title: "Parked lanes",
    blurb:
      "Deliberately out of production. They will not trade on the next session. Linked so their pages stay reachable and their state stays inspectable.",
    horizon: null,
    desks: PARKED_DESKS,
    unavailable: null,
    defaultOpen: false,
  },
];

// ─── Sidebar grouping (2026-08-02, owner-requested) ─────────────────────────
//
// The SIDEBAR renders these seven functional groups instead of the horizon
// sections — the owner found eleven headers too many. The LANDING page keeps
// LANE_SECTIONS (the horizon × policy taxonomy) untouched; this is a VIEW over
// the same desks, so every desk href below must exist in LANE_SECTIONS
// (tests/nav-model.test.ts asserts it).
//
// "Future lanes" is the owner's name for lanes that are linked but not part
// of today's production loop (Gann, Fractal, Sniper) — it does NOT change any
// desk's status/horizon in the model above.

export type SidebarEntry =
  | { kind: "desk"; href: string }
  | { kind: "workspace" }
  | { kind: "link"; href: string; label: string; matchers?: string[] };

export type SidebarGroup = {
  id: string;
  title: string;
  entries: SidebarEntry[];
  defaultOpen?: boolean;
};

export const SIDEBAR_GROUPS: SidebarGroup[] = [
  {
    id: "overview",
    title: "Overview",
    defaultOpen: true,
    entries: [
      { kind: "link", href: "/", label: "Landing" },
      { kind: "desk", href: "/strategies/overview" },
      { kind: "link", href: "/positions", label: "Positions", matchers: ["/positions", "/reports"] },
      { kind: "link", href: "/analytics", label: "Portfolio" },
      { kind: "link", href: "/trading", label: "Execution" },
      { kind: "link", href: "/proposals", label: "Proposals" },
      { kind: "link", href: "/agent", label: "AI agent" },
    ],
  },
  {
    id: "market-data",
    title: "Market data",
    defaultOpen: true,
    entries: [
      { kind: "workspace" },
      { kind: "link", href: "/market", label: "Option chain" },
      { kind: "link", href: "/charts", label: "Charts" },
      { kind: "link", href: "/orderflow", label: "Orderflow" },
    ],
  },
  {
    id: "technical",
    title: "Technical lanes",
    defaultOpen: true,
    entries: [
      { kind: "desk", href: "/strategies/nse/live" },
      { kind: "desk", href: "/strategies/macd-refined" },
      { kind: "desk", href: "/strategies/directional" },
      { kind: "desk", href: "/strategies/directional?horizon=positional" },
      { kind: "desk", href: "/strategies/cbe" },
    ],
  },
  {
    id: "auction-mp",
    title: "Auction / MP lanes",
    defaultOpen: true,
    entries: [
      { kind: "desk", href: "/strategies/auction" },
      { kind: "desk", href: "/strategies/mp" },
      { kind: "desk", href: "/strategies/commodity" },
      { kind: "desk", href: "/strategies/institutional-convergence" },
    ],
  },
  {
    id: "research",
    title: "Research",
    entries: [
      { kind: "link", href: "/research", label: "Research lab", matchers: ["/research", "/analysis", "/backtester", "/data"] },
      { kind: "link", href: "/macro-research", label: "Macro" },
      { kind: "link", href: "/sector-interaction", label: "Sector network" },
    ],
  },
  {
    id: "future",
    title: "Future lanes",
    entries: [
      { kind: "desk", href: "/strategies/gann" },
      { kind: "desk", href: "/strategies/fractal" },
      { kind: "desk", href: "/strategies/sniper" },
    ],
  },
  {
    id: "settings",
    title: "Settings",
    entries: [
      { kind: "link", href: "/system", label: "System hub", matchers: ["/system", "/health", "/lane-health"] },
      { kind: "link", href: "/settings", label: "Settings" },
    ],
  },
];

/** Every desk in the model, section order preserved. */
export function allDesks(): NavDesk[] {
  return LANE_SECTIONS.flatMap((s) => s.desks);
}

/** Every route the model links to, including the workspace and its views. */
export function allNavRoutes(): string[] {
  return [WORKSPACE_ROUTE, ...allDesks().map((d) => d.href)];
}

// ─── The KIND axis — resolved from the SERVED registry, never declared ──────

export type DeskKinds = {
  /** Served kinds for this desk's lanes, in KIND_ORDER. */
  kinds: string[];
  /** Lane keys this desk claims that the registry did not serve. */
  unresolved: string[];
  /** True when the registry has not been read at all — say so, do not guess. */
  registryUnavailable: boolean;
};

/**
 * Resolve a desk's KIND axis against a `laneKey → kind` map taken straight from
 * /api/system/lanes. An empty map means the registry has not loaded: the result
 * says `registryUnavailable`, and the caller must render that rather than a
 * plausible kind.
 */
export function deskKinds(desk: NavDesk, kindByLaneKey: Record<string, string>): DeskKinds {
  const registryUnavailable = Object.keys(kindByLaneKey).length === 0;
  const kinds = new Set<string>();
  const unresolved: string[] = [];
  for (const key of desk.laneKeys) {
    const kind = kindByLaneKey[key];
    if (kind) kinds.add(kind);
    else unresolved.push(key);
  }
  const ordered = [
    ...KIND_ORDER.filter((k) => kinds.has(k)),
    ...Array.from(kinds)
      .filter((k) => !KIND_ORDER.includes(k))
      .sort(),
  ];
  return { kinds: ordered, unresolved, registryUnavailable };
}

/** Compact rail labels for the served kinds. Unknown kinds pass through. */
export const KIND_SHORT: Record<string, string> = {
  "strategy-engine": "engine",
  "scheduler-runner": "runner",
  "product-lane": "product",
  monitor: "monitor",
};

export function kindShort(kind: string): string {
  return KIND_SHORT[kind] ?? kind;
}

/** The policy chip text for a desk, or null when it is not a policy terminal. */
export function deskPolicyLabel(desk: NavDesk): string | null {
  if (!desk.policy) return null;
  const base = POLICY_COLUMN_LABEL[desk.policy];
  return desk.policyScope ? `${base} · ${desk.policyScope}` : base;
}

/** Policy terminals sort before non-policy desks, in POLICY_COLUMNS order. */
export function policyRank(desk: NavDesk): number {
  if (!desk.policy) return POLICY_COLUMNS.length;
  const i = POLICY_COLUMNS.indexOf(desk.policy);
  return i < 0 ? POLICY_COLUMNS.length : i;
}

// ─── Desk book state ────────────────────────────────────────────────────────

export type BookField = "openPositions" | "realizedPnl" | "unrealizedPnl" | "totalTrades" | "totalEquity";

export const BOOK_FIELDS: BookField[] = [
  "openPositions",
  "realizedPnl",
  "unrealizedPnl",
  "totalTrades",
  "totalEquity",
];

/**
 * Aliases per field, in priority order. Every alias below was read off a live
 * payload on 2026-07-20; nothing is guessed. A field with no matching key on
 * the payload stays NULL — it is never coerced to 0, because a missing number
 * and a measured zero are different facts.
 */
const FIELD_ALIASES: Record<BookField, string[]> = {
  openPositions: ["open_positions", "open_count"],
  realizedPnl: ["realized_pnl"],
  unrealizedPnl: ["unrealized_pnl"],
  totalTrades: ["total_trades", "closed_positions", "closed_count"],
  totalEquity: ["total_equity", "equity", "final_equity"],
};

export type BookFields = Record<BookField, number | null>;

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Walk `path` into the payload. Returns null when the path does not exist. */
export function walkPath(raw: unknown, path: string[]): Record<string, unknown> | null {
  let cur: unknown = raw;
  for (const k of path) {
    if (!cur || typeof cur !== "object") return null;
    cur = (cur as Record<string, unknown>)[k];
  }
  return cur && typeof cur === "object" ? (cur as Record<string, unknown>) : null;
}

/** Normalize a paper payload into the shared book fields. Missing stays null. */
export function normalizeBook(raw: unknown, path: string[] = []): BookFields {
  const obj = walkPath(raw, path);
  const out = {} as BookFields;
  for (const f of BOOK_FIELDS) {
    let v: number | null = null;
    if (obj) {
      for (const alias of FIELD_ALIASES[f]) {
        v = num(obj[alias]);
        if (v !== null) break;
      }
    }
    out[f] = v;
  }
  return out;
}

/**
 * What a desk card is actually saying. The em-dash the old cards rendered
 * collapsed the first four of these into one glyph.
 */
export type DeskCardState =
  | "PARKED" // the lane is deliberately out of production
  | "NO_BOOK" // no paper-book endpoint exists for this desk
  | "LOADING" // the fetch has not resolved yet
  | "NOT_REPORTING" // the endpoint was called and did not answer usably
  | "NO_POSITIONS" // it answered: the book is measured-flat
  | "BOOK_PARTIAL" // it answered, but carries no P&L and no open count
  | "REPORTING"; // it answered with a live book

export const DESK_CARD_STATE_LABEL: Record<DeskCardState, string> = {
  PARKED: "Parked",
  NO_BOOK: "No book endpoint",
  LOADING: "Fetching",
  NOT_REPORTING: "Not reporting",
  NO_POSITIONS: "No positions",
  BOOK_PARTIAL: "Partial book",
  REPORTING: "Reporting",
};

/**
 * GREEN is reserved for reporting-live. Everything else is info/warn/neutral.
 *
 * BOOK_PARTIAL exists because REPORTING was being painted green on a card whose
 * every visible number read UNAVAILABLE (S1: /api/strategy/portfolio carries
 * final_equity + total_trades but no realized/unrealized P&L and no open count).
 * A green badge above two UNAVAILABLEs asserts a health the payload does not
 * support, so that case is now blue and says what the payload DOES carry.
 */
export const DESK_CARD_STATE_VARIANT: Record<DeskCardState, "neutral" | "success" | "warn" | "error" | "info"> = {
  PARKED: "neutral",
  NO_BOOK: "neutral",
  LOADING: "info",
  NOT_REPORTING: "warn",
  NO_POSITIONS: "info",
  BOOK_PARTIAL: "info",
  REPORTING: "success",
};

/** What the fetch layer hands back. Errors are reported, never swallowed. */
export type DeskFetch =
  | { status: "pending" }
  | { status: "error"; detail: string }
  | { status: "ok"; payload: unknown };

export type DeskCard = {
  desk: NavDesk;
  state: DeskCardState;
  /** Populated only when the endpoint answered. */
  fields: BookFields | null;
  /** Fields the payload structurally does not carry — declared on the desk. */
  absent: BookField[];
  /** One sentence a trader can act on. Never a plausible-sounding default. */
  reason: string;
};

/**
 * Derive the card. Order matters: a parked lane says PARKED even when its
 * (flat) book resolves, because "0 positions" is not the interesting fact
 * about a lane that will not trade tomorrow.
 */
export function deskCardState(desk: NavDesk, fetch: DeskFetch | null): DeskCard {
  const absent = desk.book?.absent ?? [];
  if (desk.status === "parked") {
    const fields = fetch && fetch.status === "ok" ? normalizeBook(fetch.payload, desk.book?.path ?? []) : null;
    return {
      desk,
      state: "PARKED",
      fields,
      absent,
      reason: desk.parkedReason ?? "Parked; no reason was declared for this desk.",
    };
  }
  if (!desk.book) {
    return {
      desk,
      state: "NO_BOOK",
      fields: null,
      absent,
      reason: desk.noBookReason
        ? `No paper-book endpoint — ${desk.noBookReason}`
        : "No paper-book endpoint exists for this desk.",
    };
  }
  if (!fetch || fetch.status === "pending") {
    return { desk, state: "LOADING", fields: null, absent, reason: `Fetching ${desk.book.endpoint}…` };
  }
  if (fetch.status === "error") {
    return {
      desk,
      state: "NOT_REPORTING",
      fields: null,
      absent,
      reason: `${desk.book.endpoint} did not answer (${fetch.detail}). The book is UNAVAILABLE — this is not a flat book.`,
    };
  }
  const fields = normalizeBook(fetch.payload, desk.book.path);
  const answered = BOOK_FIELDS.some((f) => fields[f] !== null);
  if (!answered) {
    return {
      desk,
      state: "NOT_REPORTING",
      fields,
      absent,
      reason: `${desk.book.endpoint} answered, but carries none of the book fields. Nothing measurable to show.`,
    };
  }
  // MEASURED-flat needs a measured zero, not an absent field: `openPositions`
  // must actually be 0, and nothing may have been closed either. A payload that
  // simply does not carry the field stays REPORTING with that field UNAVAILABLE.
  const flat = fields.openPositions === 0 && (fields.totalTrades === null || fields.totalTrades === 0);
  if (flat) {
    return {
      desk,
      state: "NO_POSITIONS",
      fields,
      absent,
      reason: `${desk.book.endpoint} reported a MEASURED-flat book: 0 open, 0 closed.`,
    };
  }
  // The endpoint answered with SOME field, but none of the ones this card
  // displays (P&L and open count). Green would assert a health the payload does
  // not carry, so this is its own blue state that names what it DOES carry.
  const displayable =
    fields.realizedPnl !== null || fields.unrealizedPnl !== null || fields.openPositions !== null;
  if (!displayable) {
    const carried = BOOK_FIELDS.filter((f) => fields[f] !== null);
    return {
      desk,
      state: "BOOK_PARTIAL",
      fields,
      absent,
      reason:
        `${desk.book.endpoint} answered but carries no P&L and no open-position count. ` +
        `It carries only: ${carried.join(", ")}. Those fields are UNAVAILABLE, not zero.`,
    };
  }
  return {
    desk,
    state: "REPORTING",
    fields,
    absent,
    reason: `Book read from ${desk.book.endpoint}.`,
  };
}

/**
 * The P&L a card may display. Returns `complete: false` when a component is
 * missing, so the surface can show what it has and name what it cannot —
 * rather than summing a null as zero.
 */
export function deskTotalPnl(card: DeskCard): {
  value: number | null;
  complete: boolean;
  missing: BookField[];
} {
  const f = card.fields;
  if (!f) return { value: null, complete: false, missing: ["realizedPnl", "unrealizedPnl"] };
  const missing = (["realizedPnl", "unrealizedPnl"] as BookField[]).filter((k) => f[k] === null);
  if (f.realizedPnl === null && f.unrealizedPnl === null) return { value: null, complete: false, missing };
  const value = (f.realizedPnl ?? 0) + (f.unrealizedPnl ?? 0);
  return { value, complete: missing.length === 0, missing };
}

/** How many desks actually reported, and how many were even askable. */
export function reportingTally(cards: DeskCard[]): {
  reporting: number;
  askable: number;
  notReporting: number;
  partial: number;
  parked: number;
  noBook: number;
} {
  let reporting = 0;
  let notReporting = 0;
  let partial = 0;
  let parked = 0;
  let noBook = 0;
  for (const c of cards) {
    if (c.state === "PARKED") parked++;
    else if (c.state === "NO_BOOK") noBook++;
    else if (c.state === "NOT_REPORTING") notReporting++;
    else if (c.state === "BOOK_PARTIAL") partial++;
    else if (c.state === "REPORTING" || c.state === "NO_POSITIONS") reporting++;
  }
  return {
    reporting,
    askable: reporting + notReporting + partial,
    notReporting,
    partial,
    parked,
    noBook,
  };
}
