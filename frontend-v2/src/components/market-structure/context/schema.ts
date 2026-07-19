/**
 * Workspace context schema — the ONE pinned context of the market-structure
 * terminal, and the only thing the URL carries.
 *
 * Everything the workspace renders derives from this object. Changing the
 * instrument is a SINGLE mutation of this object (one `router.replace`), which
 * is what makes "one instrument change updates every panel atomically" true by
 * construction rather than by convention: there is no second place to store a
 * symbol, so no panel can lag behind.
 *
 * Pure module — no React, no hooks — so it can be unit-tested and imported by
 * server components.
 */

export type MarketKey = "NSE" | "MCX";
export type Horizon = "intraday" | "swing" | "positional";
export type Timeframe = "1m" | "3m" | "30m" | "1d";
export type WorkspaceView =
  | "command"
  | "structure"
  | "flow"
  | "strategies"
  | "risk"
  | "research";
export type SortDir = "asc" | "desc";

export const MARKETS: MarketKey[] = ["NSE", "MCX"];
export const HORIZONS: Horizon[] = ["intraday", "swing", "positional"];
export const TIMEFRAMES: Timeframe[] = ["1m", "3m", "30m", "1d"];
export const VIEWS: WorkspaceView[] = [
  "command",
  "structure",
  "flow",
  "strategies",
  "risk",
  "research",
];

export const VIEW_LABEL: Record<WorkspaceView, string> = {
  command: "Command",
  structure: "Structure",
  flow: "Flow",
  strategies: "Strategies",
  risk: "Risk & Execution",
  research: "Research",
};

export type WorkspaceContext = {
  market: MarketKey;
  /** Underlying / root — the pin. Everything else is scoped by it. */
  symbol: string;
  /** Resolved derivative contract, when the lane reports one. */
  contract: string | null;
  horizon: Horizon;
  /** Aggregation for detail panels. */
  timeframe: Timeframe;
  /** Time frontier: "now" or an ISO instant. Non-"now" implies replay. */
  asOf: string;
  /** When true NOTHING in the workspace may render a live verdict. */
  replay: boolean;
  view: WorkspaceView;
  sortKey: string;
  sortDir: SortDir;
  /** Symbol search filter for the matrix. */
  query: string;
};

export const DEFAULT_CONTEXT: WorkspaceContext = {
  market: "NSE",
  symbol: "NIFTY",
  contract: null,
  horizon: "intraday",
  timeframe: "3m",
  asOf: "now",
  replay: false,
  view: "command",
  sortKey: "readiness",
  sortDir: "desc",
  query: "",
};

/** Default symbol per market — MCX has no NIFTY. */
export const DEFAULT_SYMBOL: Record<MarketKey, string> = {
  NSE: "NIFTY",
  MCX: "GOLD",
};

function pick<T extends string>(raw: string | null, allowed: readonly T[], fallback: T): T {
  const v = String(raw ?? "").trim().toLowerCase();
  const hit = allowed.find((a) => a.toLowerCase() === v);
  return hit ?? fallback;
}

/** Parse the URL into a fully-populated context. Unknown values fall back. */
export function parseContext(params: URLSearchParams | null): WorkspaceContext {
  const p = params ?? new URLSearchParams();
  const market = pick(p.get("market"), MARKETS, DEFAULT_CONTEXT.market);
  const symbolRaw = (p.get("symbol") ?? "").trim().toUpperCase();
  const asOfRaw = (p.get("asof") ?? "now").trim();
  const asOf = asOfRaw === "" ? "now" : asOfRaw;
  // A time frontier in the past is a replay by definition — the flag cannot be
  // turned off while `asof` names a historical instant.
  const replayParam = p.get("replay") === "1";
  const replay = replayParam || asOf !== "now";
  return {
    market,
    symbol: symbolRaw || DEFAULT_SYMBOL[market],
    contract: (p.get("contract") ?? "").trim() || null,
    horizon: pick(p.get("horizon"), HORIZONS, DEFAULT_CONTEXT.horizon),
    timeframe: pick(p.get("tf"), TIMEFRAMES, DEFAULT_CONTEXT.timeframe),
    asOf,
    replay,
    view: pick(p.get("view"), VIEWS, DEFAULT_CONTEXT.view),
    sortKey: (p.get("sort") ?? "").split(":")[0] || DEFAULT_CONTEXT.sortKey,
    sortDir: pick((p.get("sort") ?? "").split(":")[1] ?? null, ["asc", "desc"] as const, DEFAULT_CONTEXT.sortDir),
    query: p.get("q") ?? "",
  };
}

/**
 * Serialize back to the URL, eliding defaults so a shared link stays short and
 * a fresh visit and a round-trip produce the same context.
 */
export function serializeContext(ctx: WorkspaceContext): URLSearchParams {
  const p = new URLSearchParams();
  if (ctx.market !== DEFAULT_CONTEXT.market) p.set("market", ctx.market);
  if (ctx.symbol && ctx.symbol !== DEFAULT_SYMBOL[ctx.market]) p.set("symbol", ctx.symbol);
  if (ctx.contract) p.set("contract", ctx.contract);
  if (ctx.horizon !== DEFAULT_CONTEXT.horizon) p.set("horizon", ctx.horizon);
  if (ctx.timeframe !== DEFAULT_CONTEXT.timeframe) p.set("tf", ctx.timeframe);
  if (ctx.asOf && ctx.asOf !== "now") p.set("asof", ctx.asOf);
  if (ctx.replay && ctx.asOf === "now") p.set("replay", "1");
  if (ctx.view !== DEFAULT_CONTEXT.view) p.set("view", ctx.view);
  if (ctx.sortKey !== DEFAULT_CONTEXT.sortKey || ctx.sortDir !== DEFAULT_CONTEXT.sortDir) {
    p.set("sort", `${ctx.sortKey}:${ctx.sortDir}`);
  }
  if (ctx.query) p.set("q", ctx.query);
  return p;
}

/** `/strategies/market-structure?…` for the given context. */
export function contextHref(ctx: WorkspaceContext, base = "/strategies/market-structure"): string {
  const qs = serializeContext(ctx).toString();
  return qs ? `${base}?${qs}` : base;
}

/**
 * Deep link into an EXISTING desk carrying the pinned symbol. Old routes are
 * untouched; those that read `?symbol=` seed their picker from it, the rest
 * simply ignore the param and open on their own default.
 */
export function deskHref(path: string, ctx: WorkspaceContext): string {
  const p = new URLSearchParams();
  if (ctx.symbol) p.set("symbol", ctx.symbol);
  if (ctx.market !== "NSE") p.set("market", ctx.market);
  return `${path}?${p.toString()}`;
}

/** Human summary of the pin, echoed by every view including the placeholders. */
export function describeContext(ctx: WorkspaceContext): string {
  return [
    `${ctx.market} · ${ctx.symbol}`,
    ctx.contract ?? null,
    ctx.timeframe,
    ctx.horizon,
    ctx.asOf === "now" ? "as of now" : `as of ${ctx.asOf}`,
    ctx.replay ? "replay" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
