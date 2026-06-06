// Loose shape for the NSE strategy-agent status payload (consumed opaquely here,
// only `.strategies[]` is iterated). Kept local so v1 can be fully retired.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type StrategyAgentStatus = { strategies?: any[]; [k: string]: any };
import {
  getAuctionIntelligencePaperPositions,
  getCommodityStrategyStatus,
  getDirectionalOptionsPaperPositions,
  getFractalMarketProfilePaperPositions,
  getGannTPDeltaPaperAgentStatus,
  getStrategyAgentStatus,
} from "@/lib/api";

export type InstrumentGroup = "options" | "futures" | "other";
export type PositionStatus = "open" | "closed";

export type AppStrategyPositionRow = {
  id: string;
  desk: string;
  strategy: string;
  source: string;
  venue: string;
  underlying: string;
  symbol: string;
  contract: string;
  instrumentGroup: InstrumentGroup;
  action: string;
  qty: number;
  lots?: number | null;
  lotSize?: number | null;
  entryPrice: number;
  currentPrice: number;
  unrealizedPnl: number;
  realizedPnl?: number | null;
  returnPct?: number | null;
  updatedAt?: string | null;
  enteredAt?: string | null;
  closedAt?: string | null;
  expiry?: string | null;
  dte?: number | null;
  phase?: string | null;
  trailingStop?: number | null;
  stopPrice?: number | null;
  targetPrice?: number | null;
  targetReached?: boolean | null;
  peakPrice?: number | null;
  entryIvPct?: number | null;
  signalReason?: string | null;
  status: PositionStatus;
};

export type StrategyBookSummary = {
  key: string;
  label: string;
  desk: string;
  venue: string;
  openPositions: number;
  closedPositions: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
};

export type CommodityStrategyStatus = {
  summary?: {
    initial_capital?: number | null;
    available_capital?: number | null;
    total_equity?: number | null;
    realized_pnl?: number | null;
    unrealized_pnl?: number | null;
    day_pnl?: number | null;
    open_positions?: number | null;
    total_trades?: number | null;
  };
  last_run_at?: string | null;
  positions?: Array<Record<string, any>>;
  trade_history?: Array<Record<string, any>>;
  reports?: Array<Record<string, any>>;
};

type PaperPositionsPayload = {
  summary?: Record<string, any>;
  open_positions?: Array<Record<string, any>>;
  closed_positions?: Array<Record<string, any>>;
};

type GannAgentStatus = PaperPositionsPayload & {
  last_scan_at?: string | null;
};

export type AppStrategyPortfolioSnapshot = {
  nse: StrategyAgentStatus | null;
  commodity: CommodityStrategyStatus | null;
  directional: PaperPositionsPayload | null;
  gann: GannAgentStatus | null;
  auction: PaperPositionsPayload | null;
  fractal: PaperPositionsPayload | null;
  errors: Record<string, string>;
  fetchedAt: string;
};

export function computeDTE(expiry?: string | null): number | null {
  if (!expiry) return null;
  const parsed = new Date(expiry);
  if (Number.isNaN(parsed.getTime())) return null;
  return Math.ceil((parsed.getTime() - Date.now()) / 86_400_000);
}

export function toEpoch(value?: string | null) {
  if (!value) return 0;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}

function asNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function apiErrorMessage(error: unknown): string {
  const maybe = error as { response?: { data?: { detail?: string } }; message?: string };
  return maybe?.response?.data?.detail || maybe?.message || "Request failed";
}

async function settle<T>(key: string, task: Promise<T>, errors: Record<string, string>): Promise<T | null> {
  try {
    return await task;
  } catch (error) {
    errors[key] = apiErrorMessage(error);
    return null;
  }
}

export async function fetchAppStrategyPortfolioSnapshot(): Promise<AppStrategyPortfolioSnapshot> {
  const errors: Record<string, string> = {};
  const [nse, commodity, directional, gann, auction, fractal] = await Promise.all([
    settle("nse", getStrategyAgentStatus().then((response) => response.data as StrategyAgentStatus), errors),
    settle("commodity", getCommodityStrategyStatus().then((response) => response.data as CommodityStrategyStatus), errors),
    settle("directional", getDirectionalOptionsPaperPositions(undefined, "all", 100).then((response) => response.data as PaperPositionsPayload), errors),
    settle("gann", getGannTPDeltaPaperAgentStatus(100).then((response) => response.data as GannAgentStatus), errors),
    settle("auction", getAuctionIntelligencePaperPositions(undefined, "all", 100).then((response) => response.data as PaperPositionsPayload), errors),
    settle("fractal", getFractalMarketProfilePaperPositions(undefined, "all", 100).then((response) => response.data as PaperPositionsPayload), errors),
  ]);

  return {
    nse,
    commodity,
    directional,
    gann,
    auction,
    fractal,
    errors,
    fetchedAt: new Date().toISOString(),
  };
}

function optionContract(optionType?: unknown, strike?: unknown, expiry?: unknown) {
  return `${String(optionType || "--")} ${strike != null ? String(strike) : "--"} · ${String(expiry || "--")}`;
}

function inferVenue(symbol?: unknown, fallback = "NSE") {
  const token = String(symbol || "").toUpperCase();
  if (token.startsWith("MCX:") || token.includes(" MCX ")) return "MCX";
  if (token.startsWith("BSE:") || token.includes("SENSEX")) return "BSE";
  if (token.startsWith("NSE:") || token.startsWith("NSE_FO|")) return "NSE";
  return fallback;
}

function genericOptionRow(
  sourceKey: string,
  desk: string,
  strategy: string,
  venue: string,
  position: Record<string, any>,
  status: PositionStatus,
): AppStrategyPositionRow {
  const qty = asNumber(position.quantity_units ?? position.qty_units ?? position.quantity ?? position.qty, 0);
  const entry = asNumber(position.entry_premium ?? position.entry_price, 0);
  const current = asNumber(position.exit_premium ?? position.latest_premium ?? position.current_price ?? entry, entry);
  const openedAt = position.opened_at ?? position.entry_time ?? null;
  const updatedAt = position.closed_at ?? position.updated_at ?? null;
  const action =
    position.action === "SHORT"
      ? "SELL"
      : position.action === "LONG"
        ? "BUY"
        : String(position.direction || position.option_type || "BUY").includes("PE") ||
            String(position.direction || "").toLowerCase().includes("put")
          ? "BUY PE"
          : String(position.direction || position.option_type || "BUY").includes("CE") ||
              String(position.direction || "").toLowerCase().includes("call")
            ? "BUY CE"
            : "BUY";
  const pnl = asNumber(status === "open" ? position.unrealized_pnl : position.realized_pnl, 0);
  const grossCost = entry * Math.max(qty, 1);

  return {
    id: `${sourceKey}-${status}-${position.position_id || position.instrument_key || position.trading_symbol || `${position.underlying || "na"}-${position.strike || "na"}-${openedAt || updatedAt || "na"}`}`,
    desk,
    strategy,
    source: sourceKey,
    venue: inferVenue(position.trading_symbol || position.instrument_key, venue),
    underlying: String(position.underlying || "--"),
    symbol: String(position.trading_symbol || position.instrument_key || "--"),
    contract: optionContract(position.option_type, position.strike, position.expiry),
    instrumentGroup: "options",
    action,
    qty,
    lots: position.quantity_lots ?? position.qty_lots ?? (position.lot_size ? qty / asNumber(position.lot_size, 1) : null),
    lotSize: position.lot_size ?? null,
    entryPrice: entry,
    currentPrice: current,
    unrealizedPnl: status === "open" ? pnl : 0,
    realizedPnl: status === "closed" ? pnl : null,
    returnPct: grossCost > 0 ? (pnl / grossCost) * 100 : null,
    updatedAt,
    enteredAt: openedAt,
    closedAt: position.closed_at ?? null,
    expiry: position.expiry ?? null,
    dte: computeDTE(position.expiry),
    phase: position.regime ?? position.signal_state ?? position.setup_name ?? null,
    stopPrice: position.stop_price ?? position.stop_level ?? null,
    targetPrice: position.target_price ?? position.target_level ?? null,
    signalReason:
      position.selection_reason ||
      position.close_reason ||
      (Array.isArray(position.signal_reasons) ? position.signal_reasons.join(", ") : position.signal_reasons) ||
      position.setup_name ||
      null,
    status,
  };
}

export function buildOpenPositionRows(snapshot?: AppStrategyPortfolioSnapshot | null): AppStrategyPositionRow[] {
  if (!snapshot) return [];
  const rows: AppStrategyPositionRow[] = [];

  for (const strategy of snapshot.nse?.strategies || []) {
    for (const position of strategy.positions || []) {
      rows.push({
        id: `nse-${strategy.key}-${position.symbol}-${position.entered_at}`,
        desk: "NSE Options",
        strategy: strategy.label,
        source: strategy.key,
        venue: "NSE",
        underlying: position.underlying,
        symbol: position.symbol,
        contract: optionContract(position.option_type, position.strike, position.expiry),
        instrumentGroup: "options",
        action: `BUY ${position.option_type || ""}`.trim(),
        qty: position.qty,
        lots: null,
        lotSize: null,
        entryPrice: position.entry_price,
        currentPrice: position.current_price,
        unrealizedPnl: position.unrealized_pnl || 0,
        returnPct: position.return_pct,
        updatedAt: position.price_updated_at || position.entered_at,
        enteredAt: position.entered_at || null,
        expiry: position.expiry || null,
        dte: computeDTE(position.expiry),
        phase: position.phase || null,
        trailingStop: position.trailing_stop ?? null,
        peakPrice: position.peak_price ?? null,
        entryIvPct: position.entry_iv_pct ?? null,
        signalReason: position.signal_reason || null,
        status: "open",
      });
    }
  }

  for (const position of snapshot.commodity?.positions || []) {
    const isOption = position.instrument_type === "OPT" || position.instrument_type === "OPTION" || Boolean(position.option_type);
    rows.push({
      id: `commodity-${position.position_key}`,
      desk: "Commodity",
      strategy: String(position.strategy_title || position.strategy_key || "Commodity Strategy"),
      source: String(position.strategy_key || "commodity"),
      venue: "MCX",
      underlying: String(position.underlying || "--"),
      symbol: String(position.live_symbol || position.symbol || "--"),
      contract: isOption
        ? optionContract(position.option_type, position.strike, position.expiry)
        : String(position.display_name || position.live_symbol || position.symbol || "--"),
      instrumentGroup: isOption ? "options" : "futures",
      action: String(position.option_type ? `BUY ${position.option_type}` : position.action || "BUY"),
      qty: asNumber(position.qty, 0),
      lots: position.lots ?? null,
      lotSize: position.lot_size ?? null,
      entryPrice: asNumber(position.entry_price, 0),
      currentPrice: asNumber(position.current_price, 0),
      unrealizedPnl: asNumber(position.unrealized_pnl, 0),
      returnPct: position.return_pct ?? null,
      updatedAt: snapshot.commodity?.last_run_at || position.last_reviewed_bar_time || position.entered_at || null,
      enteredAt: position.entered_at || null,
      expiry: position.expiry || null,
      dte: computeDTE(position.expiry),
      phase: position.regime || null,
      stopPrice: position.stop_price ?? null,
      targetPrice: position.target_price ?? null,
      targetReached: position.target_reached ?? null,
      peakPrice: position.peak_price ?? null,
      entryIvPct: position.entry_iv_pct ?? null,
      signalReason: position.signal_reason || null,
      status: "open",
    });
  }

  for (const position of snapshot.directional?.open_positions || []) {
    rows.push(genericOptionRow("directional", "Long Premium", "Directional Options", "NSE", position, "open"));
  }
  for (const position of snapshot.gann?.open_positions || []) {
    rows.push(genericOptionRow("gann", "Gann TP Delta", "Gann TP Delta", "NSE", position, "open"));
  }
  for (const position of snapshot.auction?.open_positions || []) {
    rows.push(genericOptionRow("auction", "Auction Intelligence", "Auction Intelligence", "NSE", position, "open"));
  }
  for (const position of snapshot.fractal?.open_positions || []) {
    rows.push(genericOptionRow("fractal", "Fractal Profile", "Fractal Market Profile", "NSE", position, "open"));
  }

  return rows.sort((left, right) => Math.abs(right.unrealizedPnl || 0) - Math.abs(left.unrealizedPnl || 0));
}

export function buildClosedTradeRows(snapshot?: AppStrategyPortfolioSnapshot | null): AppStrategyPositionRow[] {
  if (!snapshot) return [];
  const rows: AppStrategyPositionRow[] = [];

  for (const strategy of snapshot.nse?.strategies || []) {
    for (const trade of strategy.trade_history || []) {
      rows.push(genericOptionRow(strategy.key, "NSE Options", strategy.label, "NSE", {
        ...trade,
        position_id: `${trade.symbol}-${trade.exit_time}`,
        trading_symbol: trade.symbol,
        opened_at: trade.entry_time,
        updated_at: trade.exit_time,
        closed_at: trade.exit_time,
        entry_premium: trade.entry_price,
        exit_premium: trade.exit_price,
        quantity: trade.qty,
        direction: trade.option_type,
      }, "closed"));
    }
  }

  for (const trade of snapshot.commodity?.trade_history || []) {
    const qty = asNumber(trade.qty, 0);
    const entry = asNumber(trade.entry_price, 0);
    const exit = asNumber(trade.exit_price, entry);
    const pnl = asNumber(trade.pnl, 0);
    rows.push({
      id: `commodity-closed-${trade.symbol}-${trade.exit_time}`,
      desk: "Commodity",
      strategy: trade.instrument_type === "OPTION" ? "Commodity · Options" : "Commodity · Futures",
      source: "commodity",
      venue: "MCX",
      underlying: String(trade.symbol || "--").split(/[ :]/)[1] || String(trade.symbol || "--"),
      symbol: String(trade.symbol || "--"),
      contract: trade.instrument_type === "OPTION"
        ? optionContract(trade.option_type, trade.strike, trade.expiry)
        : String(trade.symbol || "--"),
      instrumentGroup: trade.instrument_type === "OPTION" ? "options" : "futures",
      action: String(trade.option_type ? `BUY ${trade.option_type}` : trade.action || "BUY"),
      qty,
      entryPrice: entry,
      currentPrice: exit,
      unrealizedPnl: 0,
      realizedPnl: pnl,
      returnPct: entry * Math.max(qty, 1) > 0 ? (pnl / (entry * Math.max(qty, 1))) * 100 : null,
      updatedAt: trade.exit_time || null,
      enteredAt: trade.entry_time || null,
      closedAt: trade.exit_time || null,
      expiry: trade.expiry || null,
      dte: computeDTE(trade.expiry),
      signalReason: trade.action || null,
      status: "closed",
    });
  }

  for (const position of snapshot.directional?.closed_positions || []) {
    rows.push(genericOptionRow("directional", "Long Premium", "Directional Options", "NSE", position, "closed"));
  }
  for (const position of snapshot.gann?.closed_positions || []) {
    rows.push(genericOptionRow("gann", "Gann TP Delta", "Gann TP Delta", "NSE", position, "closed"));
  }
  for (const position of snapshot.auction?.closed_positions || []) {
    rows.push(genericOptionRow("auction", "Auction Intelligence", "Auction Intelligence", "NSE", position, "closed"));
  }
  for (const position of snapshot.fractal?.closed_positions || []) {
    rows.push(genericOptionRow("fractal", "Fractal Profile", "Fractal Market Profile", "NSE", position, "closed"));
  }

  return rows.sort((left, right) => toEpoch(right.closedAt || right.updatedAt) - toEpoch(left.closedAt || left.updatedAt));
}

function summaryNumber(summary: Record<string, any> | undefined, ...keys: string[]) {
  for (const key of keys) {
    if (summary?.[key] != null) return asNumber(summary[key], 0);
  }
  return 0;
}

export function buildStrategyBookSummaries(snapshot?: AppStrategyPortfolioSnapshot | null): StrategyBookSummary[] {
  if (!snapshot) return [];
  const summaries: StrategyBookSummary[] = [];

  for (const strategy of snapshot.nse?.strategies || []) {
    const summary = strategy.summary || {};
    summaries.push({
      key: strategy.key,
      label: strategy.label,
      desk: "NSE Options",
      venue: "NSE",
      openPositions: asNumber(summary.open_positions, 0),
      closedPositions: asNumber(summary.total_trades, 0),
      realizedPnl: asNumber(summary.realized_pnl, 0),
      unrealizedPnl: asNumber(summary.unrealized_pnl, 0),
      totalPnl: asNumber(summary.realized_pnl, 0) + asNumber(summary.unrealized_pnl, 0),
    });
  }

  const commoditySummary = snapshot.commodity?.summary;
  summaries.push({
    key: "commodity",
    label: "Commodity Strategy Desk",
    desk: "Commodity",
    venue: "MCX",
    openPositions: asNumber(commoditySummary?.open_positions, 0),
    closedPositions: asNumber(commoditySummary?.total_trades, 0),
    realizedPnl: asNumber(commoditySummary?.realized_pnl, 0),
    unrealizedPnl: asNumber(commoditySummary?.unrealized_pnl, 0),
    totalPnl: asNumber(commoditySummary?.realized_pnl, 0) + asNumber(commoditySummary?.unrealized_pnl, 0),
  });

  const generic = [
    ["directional", "Directional Options", "Long Premium", "NSE", snapshot.directional] as const,
    ["gann", "Gann TP Delta", "Gann TP Delta", "NSE", snapshot.gann] as const,
    ["auction", "Auction Intelligence", "Auction Intelligence", "NSE", snapshot.auction] as const,
    ["fractal", "Fractal Market Profile", "Fractal Profile", "NSE", snapshot.fractal] as const,
  ];

  for (const [key, label, desk, venue, payload] of generic) {
    const summary = payload?.summary;
    summaries.push({
      key,
      label,
      desk,
      venue,
      openPositions: summaryNumber(summary, "open_positions", "open_count"),
      closedPositions: summaryNumber(summary, "closed_positions", "closed_count"),
      realizedPnl: summaryNumber(summary, "realized_pnl"),
      unrealizedPnl: summaryNumber(summary, "unrealized_pnl"),
      totalPnl: summaryNumber(summary, "total_pnl", "realized_pnl") + (summary?.total_pnl == null ? summaryNumber(summary, "unrealized_pnl") : 0),
    });
  }

  return summaries;
}
