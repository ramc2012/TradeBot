"use client";

/**
 * book-data — one fetch layer per authoritative book.
 *
 * Every lane reads a DIFFERENT artifact (a Postgres table for one, a runtime
 * JSON file for the next), so there is one loader per book and each one names
 * the endpoints it called. Errors are recorded, never swallowed: a source that
 * did not answer makes its fields read UNAVAILABLE, which is a different state
 * from an empty book and must never look the same.
 *
 * `Promise.allSettled` throughout — one 404 must not sink the other three
 * panels, and a lane whose statistics endpoint is down still has a real trade
 * book to show.
 */
import { useQuery } from "@tanstack/react-query";

import { REFRESH_MS } from "@/components/desk-ui";
import {
  getAuctionIntelligencePaperJournal,
  getAuctionIntelligencePaperPositions,
  getAuctionIntelligencePaperStatus,
  getCommodityAuctionIntelligencePaper,
  getCommodityAuctionIntelligenceStatus,
  getCommodityInstitutionalConvergenceStatus,
  getDirectionalOptionsPaperJournal,
  getDirectionalOptionsPaperPositions,
  getDirectionalOptionsPaperSummary,
  getInstitutionalConvergenceOrders,
  getInstitutionalConvergencePaper,
  getInstitutionalConvergenceStatistics,
  getInstitutionalConvergenceStatus,
  getInstitutionalConvergenceTrades,
} from "@/lib/api";
import {
  auctionIntent,
  auctionPosition,
  auctionTrade,
  directionalDecision,
  directionalPosition,
  directionalTrade,
  futuresOrder,
  futuresPosition,
  futuresTrade,
  num,
  type BookDecisionRow,
  type BookIntentRow,
  type BookOrderRow,
  type BookPositionRow,
  type BookTradeRow,
} from "@/lib/book-rows";
import {
  EMPTY_PORTFOLIO_FACTS,
  LANE_BOOKS,
  dayFigureFor,
  type BookKey,
  type DayFigure,
  type LaneBook,
  type PortfolioFacts,
} from "@/lib/lane-books";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Raw = any;

export type GateBlock = {
  symbol: string;
  action: string | null;
  quality: string | null;
  reasons: string[];
  risk: { entry?: number | null; stop?: number | null; target1?: number | null; reward_risk?: number | null };
};

export type ExitReasonStat = {
  trades?: number | null;
  wins?: number | null;
  win_rate?: number | null;
  pnl?: number | null;
  avg_r?: number | null;
};

export type BookData = {
  book: LaneBook;
  /** Sources that did not answer, described so the UI can say which. */
  errors: string[];
  lastWriteAt: string | null;

  /** The order layer, whichever kind this lane actually has. null = no source. */
  orders: BookOrderRow[] | null;
  decisions: BookDecisionRow[] | null;
  decisionsTotal: number | null;
  intents: BookIntentRow[] | null;
  intentsTotal: number | null;
  gateLadder: { generatedAt: string | null; breakdown: Record<string, number> | null; blocked: GateBlock[] } | null;

  trades: BookTradeRow[] | null;
  tradesTruncated: { shown: number; total: number | null } | null;
  positions: BookPositionRow[] | null;

  facts: PortfolioFacts;
  day: DayFigure;
  dayPnlLive: number | null;
  opensToday: number | null;
  closesToday: number | null;
  cooldownSkipsToday: number | null;
  perExitReason: Record<string, ExitReasonStat> | null;
};

// ─── helpers ────────────────────────────────────────────────────────────────

function fail(errors: string[], endpoint: string, e: unknown): null {
  const detail =
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (e as any)?.response?.status
      ? // eslint-disable-next-line @typescript-eslint/no-explicit-any
        `HTTP ${(e as any).response.status}`
      : // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (e as any)?.message || "no response";
  errors.push(`${endpoint} (${detail})`);
  return null;
}

async function settled<T>(errors: string[], endpoint: string, p: Promise<{ data: T }>): Promise<T | null> {
  try {
    return (await p).data;
  } catch (e) {
    return fail(errors, endpoint, e);
  }
}

/** Newest ISO string in a list, ignoring nulls. Never invents a timestamp. */
function newest(values: (string | null | undefined)[]): string | null {
  let best: string | null = null;
  let bestMs = -Infinity;
  for (const v of values) {
    if (!v) continue;
    const s = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(v) || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v) ? v : `${v.replace(" ", "T")}Z`;
    const ms = new Date(s).getTime();
    if (Number.isFinite(ms) && ms > bestMs) {
      bestMs = ms;
      best = v;
    }
  }
  return best;
}

const arr = (v: unknown): Raw[] => (Array.isArray(v) ? v : []);

function statsFacts(stats: Raw, paper: Raw): PortfolioFacts {
  const openCount = num(paper?.open_count);
  return {
    ...EMPTY_PORTFOLIO_FACTS,
    initialCapital: num(paper?.initial_capital) ?? num(stats?.initial_capital),
    equity: num(paper?.equity),
    realizedLifetime: num(paper?.realized_pnl) ?? num(stats?.net_pnl),
    // These books carry no unrealised roll-up FIELD. With zero open rows the
    // unrealised is nonetheless a measured zero — nothing is open, so nothing
    // is unrealised — and that is a fact, not a default. With rows open and no
    // field, it stays UNAVAILABLE rather than being summed here behind the
    // positions view's stale-mark rule.
    unrealized: openCount === 0 ? 0 : null,
    openCount,
    closedCount: num(paper?.closed_count) ?? num(stats?.trade_count),
    winRate: num(stats?.win_rate),
    profitFactor: num(stats?.profit_factor),
    maxDrawdown: num(stats?.max_drawdown),
    maxDrawdownPct: num(stats?.max_drawdown_pct),
    reservedMargin: null,
    notionalExposure: null,
  };
}

function gateLadderFrom(status: Raw): BookData["gateLadder"] {
  if (!status) return null;
  const latest = status?.latest ?? {};
  const results = arr(latest?.results);
  const blocked: GateBlock[] = results
    .map((r: Raw) => ({
      symbol: String(r?.symbol ?? r?.root ?? "—"),
      action: r?.action == null ? null : String(r.action),
      quality: r?.quality == null ? null : String(r.quality),
      reasons: arr(r?.blocked_reasons).map(String),
      risk: {
        entry: num(r?.risk?.entry),
        stop: num(r?.risk?.stop),
        target1: num(r?.risk?.target1),
        reward_risk: num(r?.risk?.reward_risk),
      },
    }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
  const breakdown = latest?.gate_breakdown && typeof latest.gate_breakdown === "object" ? latest.gate_breakdown : null;
  if (!blocked.length && !breakdown) return null;
  return { generatedAt: latest?.generated_at ?? null, breakdown, blocked };
}

// ─── directional_options — Postgres directional_paper_positions ─────────────

async function loadDirectional(nowMs: number): Promise<BookData> {
  const book = LANE_BOOKS.directional_options;
  const errors: string[] = [];
  const [summary, open, closed, journal] = await Promise.all([
    settled<Raw>(errors, "/api/directional-options/paper-summary", getDirectionalOptionsPaperSummary()),
    settled<Raw>(errors, "/api/directional-options/paper-positions?status=open", getDirectionalOptionsPaperPositions(undefined, "open", 200)),
    settled<Raw>(errors, "/api/directional-options/paper-positions?status=closed", getDirectionalOptionsPaperPositions(undefined, "closed", 200)),
    settled<Raw>(errors, "/api/directional-options/paper-journal", getDirectionalOptionsPaperJournal(undefined, 200)),
  ]);

  const positions = open ? arr(open.open_positions).map(directionalPosition) : null;
  const trades = closed ? arr(closed.closed_positions).map(directionalTrade) : null;
  const decisionRows = journal ? arr(journal.records).map(directionalDecision) : null;

  const closedTotal = num(summary?.closed_positions);
  const tradesTruncated =
    trades && closedTotal != null && closedTotal > trades.length ? { shown: trades.length, total: closedTotal } : null;

  const facts: PortfolioFacts = {
    ...EMPTY_PORTFOLIO_FACTS,
    initialCapital: num(summary?.initial_capital),
    equity: num(summary?.total_equity),
    realizedLifetime: num(summary?.realized_pnl),
    unrealized: num(summary?.unrealized_pnl),
    openCount: num(summary?.open_positions),
    closedCount: closedTotal,
    winRate: num(summary?.win_rate),
    profitFactor: null,
    maxDrawdown: null,
    maxDrawdownPct: num(summary?.max_drawdown) == null ? null : (num(summary?.max_drawdown) as number) * 100,
    reservedMargin: num(summary?.reserved_margin),
    notionalExposure: null,
  };

  return {
    book,
    errors,
    lastWriteAt: newest([
      ...(positions ?? []).map((p) => p.markAsOf),
      ...(trades ?? []).slice(0, 5).map((t) => t.closedAt),
    ]),
    orders: null,
    decisions: decisionRows,
    decisionsTotal: num(journal?.count),
    intents: null,
    intentsTotal: null,
    gateLadder: null,
    trades,
    tradesTruncated,
    positions,
    facts,
    day: dayFigureFor(
      "directional_options",
      { closed: (trades ?? []).map((t) => ({ closedAt: t.closedAt, pnl: t.realized })) },
      nowMs,
    ),
    dayPnlLive: null,
    opensToday: num(summary?.opens_today),
    closesToday: num(summary?.closes_today),
    cooldownSkipsToday: num(summary?.cooldown_skips_today),
    perExitReason: null,
  };
}

// ─── auction_intelligence NSE — runtime paper_positions.json ────────────────

async function loadAuctionNse(nowMs: number): Promise<BookData> {
  const book = LANE_BOOKS.auction_intelligence;
  const errors: string[] = [];
  const [status, positionsPayload, journal] = await Promise.all([
    settled<Raw>(errors, "/api/auction-intelligence/paper-status", getAuctionIntelligencePaperStatus()),
    settled<Raw>(errors, "/api/auction-intelligence/paper-positions", getAuctionIntelligencePaperPositions(undefined, "all", 200)),
    settled<Raw>(errors, "/api/auction-intelligence/paper-journal", getAuctionIntelligencePaperJournal(undefined, 200)),
  ]);

  const summary = positionsPayload?.summary ?? status?.summary ?? null;
  const bookSyncedAt = summary?.last_synced_at ?? null;
  const positions = positionsPayload ? arr(positionsPayload.open_positions).map((r) => auctionPosition(r, bookSyncedAt)) : null;
  const trades = positionsPayload ? arr(positionsPayload.closed_positions).map(auctionTrade) : null;
  const intents = journal ? arr(journal.records).map(auctionIntent) : null;

  const facts: PortfolioFacts = {
    ...EMPTY_PORTFOLIO_FACTS,
    initialCapital: num(summary?.initial_capital),
    equity: num(summary?.total_equity),
    realizedLifetime: num(summary?.realized_pnl),
    unrealized: num(summary?.unrealized_pnl),
    openCount: num(summary?.open_count),
    closedCount: num(summary?.closed_count),
    winRate: num(summary?.win_rate),
    profitFactor: null,
    maxDrawdown: null,
    maxDrawdownPct: num(summary?.max_drawdown) == null ? null : (num(summary?.max_drawdown) as number) * 100,
    reservedMargin: num(summary?.reserved_margin),
    notionalExposure: null,
  };

  return {
    book,
    errors,
    lastWriteAt: bookSyncedAt ?? newest([summary?.latest_closed_at, summary?.latest_opened_at]),
    orders: null,
    decisions: null,
    decisionsTotal: null,
    intents,
    intentsTotal: num(journal?.total_records),
    gateLadder: null,
    trades,
    // The whole closed list fits inside one page for this book, so nothing is
    // truncated and no truncation warning is shown.
    tradesTruncated:
      trades && facts.closedCount != null && facts.closedCount > trades.length
        ? { shown: trades.length, total: facts.closedCount }
        : null,
    positions,
    facts,
    day: dayFigureFor(
      "auction_intelligence",
      { closed: (trades ?? []).map((t) => ({ closedAt: t.closedAt, pnl: t.realized })) },
      nowMs,
    ),
    dayPnlLive: null,
    opensToday: null,
    closesToday: null,
    cooldownSkipsToday: null,
    perExitReason: null,
  };
}

// ─── auction MCX — runtime commodity_paper.json (the missing book) ──────────

async function loadAuctionMcx(nowMs: number): Promise<BookData> {
  const book = LANE_BOOKS.auction_intelligence_commodity;
  const errors: string[] = [];
  const [paper, status] = await Promise.all([
    settled<Raw>(errors, "/api/auction-intelligence/commodity/paper", getCommodityAuctionIntelligencePaper(200)),
    settled<Raw>(errors, "/api/auction-intelligence/commodity/status", getCommodityAuctionIntelligenceStatus()),
  ]);
  const stats = paper?.statistics ?? status?.paper_statistics ?? null;
  const src = paper ?? status?.paper ?? null;

  const orders = paper ? arr(paper.orders).map(futuresOrder).reverse() : null;
  const trades = src ? arr(src.closed_positions).map(futuresTrade) : null;
  const positions = src ? arr(src.open_positions).map(futuresPosition) : null;

  return {
    book,
    errors,
    lastWriteAt: stats?.updated_at ?? newest((orders ?? []).slice(0, 5).map((o) => o.time)),
    orders,
    decisions: null,
    decisionsTotal: null,
    intents: null,
    intentsTotal: null,
    gateLadder: null,
    trades,
    tradesTruncated: null,
    positions,
    facts: statsFacts(stats, src),
    day: dayFigureFor("auction_intelligence_commodity", { daily: arr(stats?.daily_pnl) }, nowMs),
    dayPnlLive: num(src?.circuit_breaker?.day_pnl),
    opensToday: null,
    closesToday: null,
    cooldownSkipsToday: null,
    perExitReason: stats?.per_exit_reason ?? null,
  };
}

// ─── institutional_convergence NSE + MCX ────────────────────────────────────

async function loadConvergence(market: "NSE" | "MCX", nowMs: number): Promise<BookData> {
  const key: BookKey = market === "MCX" ? "institutional_convergence_commodity" : "institutional_convergence";
  const book = LANE_BOOKS[key];
  const base = `/api/institutional-convergence${market === "MCX" ? "/commodity" : ""}`;
  const errors: string[] = [];
  const [paper, ordersPayload, tradesPayload, stats, status] = await Promise.all([
    settled<Raw>(errors, `${base}/paper`, getInstitutionalConvergencePaper(market)),
    settled<Raw>(errors, `${base}/orders`, getInstitutionalConvergenceOrders(market)),
    settled<Raw>(errors, `${base}/trades`, getInstitutionalConvergenceTrades(market)),
    settled<Raw>(errors, `${base}/statistics`, getInstitutionalConvergenceStatistics(market)),
    settled<Raw>(
      errors,
      `${base}/status`,
      market === "MCX" ? getCommodityInstitutionalConvergenceStatus() : getInstitutionalConvergenceStatus(),
    ),
  ]);

  const orders = ordersPayload ? arr(ordersPayload.orders).map(futuresOrder).reverse() : null;
  const trades = tradesPayload
    ? arr(tradesPayload.trades).map(futuresTrade)
    : paper
      ? arr(paper.closed_positions).map(futuresTrade)
      : null;
  const positions = paper ? arr(paper.open_positions).map(futuresPosition) : null;

  return {
    book,
    errors,
    lastWriteAt: ordersPayload?.updated_at ?? stats?.updated_at ?? null,
    orders,
    decisions: null,
    decisionsTotal: null,
    intents: null,
    intentsTotal: null,
    gateLadder: gateLadderFrom(status),
    trades,
    tradesTruncated: null,
    positions,
    facts: statsFacts(stats, paper),
    day: dayFigureFor(key, { daily: arr(stats?.daily_pnl) }, nowMs),
    dayPnlLive: num(paper?.circuit_breaker?.day_pnl),
    opensToday: null,
    closesToday: null,
    cooldownSkipsToday: null,
    perExitReason: stats?.per_exit_reason ?? null,
  };
}

// ─── The hook ───────────────────────────────────────────────────────────────

export function loadBook(key: BookKey, nowMs: number): Promise<BookData> {
  switch (key) {
    case "directional_options":
      return loadDirectional(nowMs);
    case "auction_intelligence":
      return loadAuctionNse(nowMs);
    case "auction_intelligence_commodity":
      return loadAuctionMcx(nowMs);
    case "institutional_convergence":
      return loadConvergence("NSE", nowMs);
    case "institutional_convergence_commodity":
      return loadConvergence("MCX", nowMs);
  }
}

export function useBookData(key: BookKey) {
  return useQuery<BookData>({
    queryKey: ["lane-book", key],
    queryFn: () => loadBook(key, Date.now()),
    refetchInterval: REFRESH_MS.snapshot,
    retry: 1,
  });
}
