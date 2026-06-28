import axios from "axios";
import { resolveApiBaseUrl, resolveApiBaseUrlCandidates } from "./runtime-url";

export const API_URL = resolveApiBaseUrl();

let reachableApiBaseUrl: string | null = null;
let reachableApiBaseUrlPromise: Promise<string> | null = null;
const API_PROBE_TIMEOUT_MS = 2500;
const BROKER_STATUS_TIMEOUT_MS = 6_000;
const BROKER_STATUS_FORCE_TIMEOUT_MS = 15_000;
const LATEST_TICKS_TIMEOUT_MS = 60_000;

async function probeApiBaseUrl(candidate: string): Promise<boolean> {
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timeoutId = controller
    ? window.setTimeout(() => controller.abort(), API_PROBE_TIMEOUT_MS)
    : null;
  try {
    const response = await fetch(`${candidate}/health`, {
      method: "GET",
      cache: "no-store",
      signal: controller?.signal,
    });
    return response.ok || response.status === 429;
  } catch {
    return false;
  } finally {
    if (timeoutId != null) {
      window.clearTimeout(timeoutId);
    }
  }
}

function isCurrentOrigin(candidate: string): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  try {
    return new URL(candidate).origin === window.location.origin;
  } catch {
    return false;
  }
}

async function getReachableApiBaseUrl(): Promise<string> {
  if (reachableApiBaseUrl) {
    return reachableApiBaseUrl;
  }
  if (typeof window === "undefined") {
    reachableApiBaseUrl = API_URL;
    return reachableApiBaseUrl;
  }
  if (reachableApiBaseUrlPromise) {
    return reachableApiBaseUrlPromise;
  }
  const candidates = resolveApiBaseUrlCandidates();
  reachableApiBaseUrlPromise = (async () => {
    for (const candidate of candidates) {
      if (candidates.length === 1 || isCurrentOrigin(candidate) || await probeApiBaseUrl(candidate)) {
        reachableApiBaseUrl = candidate;
        return candidate;
      }
    }
    reachableApiBaseUrl = candidates[0] || API_URL;
    return reachableApiBaseUrl;
  })();
  return reachableApiBaseUrlPromise;
}

function currentApiTarget(): string {
  return reachableApiBaseUrl || API_URL;
}

function readCookie(name: string): string {
  if (typeof document === "undefined") {
    return "";
  }
  const prefix = `${name}=`;
  const match = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : "";
}

function readWriteToken(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return (
    window.localStorage.getItem("nomad_write_token")?.trim()
    || readCookie("nomad_write_token").trim()
    || ""
  );
}

export function describeApiError(error: any, fallback = "Request failed"): string {
  const rawDetail = error?.response?.data?.detail;
  if (typeof rawDetail === "string" && rawDetail.trim()) {
    return rawDetail;
  }
  if (Array.isArray(rawDetail) && rawDetail.length > 0) {
    return JSON.stringify(rawDetail);
  }
  if (error?.response?.data && typeof error.response.data === "object") {
    const serialized = JSON.stringify(error.response.data);
    if (serialized && serialized !== "{}") {
      return serialized;
    }
  }
  if (!error?.response) {
    return `Network error reaching ${currentApiTarget()}. Refresh the page and confirm the backend is reachable.`;
  }
  return error?.message || fallback;
}

export const api = axios.create({
  baseURL: API_URL,
  headers: { "Content-Type": "application/json" },
  timeout: 30000,
});

api.interceptors.request.use(async (config) => {
  config.baseURL = await getReachableApiBaseUrl();
  const writeToken = readWriteToken();
  if (writeToken) {
    config.headers.set("x-nomad-write-token", writeToken);
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (typeof window !== "undefined" && !error?.response) {
      reachableApiBaseUrl = null;
      reachableApiBaseUrlPromise = null;
    }
    // Auth gate: a 403 on /api/* means the write token is missing/invalid. Emit a
    // global event so the in-app unlock modal can prompt for it (no devtools needed).
    if (typeof window !== "undefined" && error?.response?.status === 403) {
      if (String(error?.config?.url || "").includes("/api/")) {
        window.dispatchEvent(new CustomEvent("nomad:auth-required"));
      }
    }
    return Promise.reject(error);
  },
);

// ── Auth & Credentials ────────────────────────────────────────────────────
export const connectBroker = (broker: string, credentials: Record<string, string>) =>
  api.post("/api/auth/connect-broker", { broker, credentials });

export const saveCredentials = (broker: string, credentials: Record<string, string>) =>
  api.post("/api/auth/save-credentials", { broker, credentials });

export const getCredentialsStatus = (broker: string) =>
  api.get(`/api/auth/credentials/${broker}`);

export const getBrokerStatus = (options?: { forceValidate?: boolean }) =>
  api.get("/api/auth/broker-status", {
    params: options?.forceValidate ? { force_validate: true } : undefined,
    timeout: options?.forceValidate ? BROKER_STATUS_FORCE_TIMEOUT_MS : BROKER_STATUS_TIMEOUT_MS,
  });
export const getSystemHealth = () => api.get("/api/system/health");
export const getSystemOverview = () => api.get("/api/system/overview");
export const getTradingCalendar = () => api.get("/api/system/trading-calendar");
export const updateTradingCalendar = (payload: object) => api.put("/api/system/trading-calendar", payload);

export const getFyersAuthUrl = () => api.get("/api/auth/fyers/auth-url");
export const getUpstoxAuthUrl = () => api.get("/api/auth/upstox/auth-url");
export const connectUpstox = (code: string) =>
  api.post("/api/auth/upstox/connect", { code });
export const getIciciLoginUrl = () => api.get("/api/auth/icici-breeze/login-url");
export const connectIciciBreeze = (session_token: string) =>
  api.post("/api/auth/icici-breeze/connect", { session_token });
export const connectFivepaisa = (totp: string) =>
  api.post("/api/auth/fivepaisa/connect", { totp });
export const disconnectBroker = (broker: string) =>
  api.post("/api/auth/disconnect-broker", null, { params: { broker } });
export const getTelegramSettings = () => api.get("/api/auth/telegram-settings");
export const saveTelegramSettings = (payload: object) => api.post("/api/auth/telegram-settings", payload);
export const discoverTelegramChats = (bot_token = "") =>
  api.post("/api/auth/telegram-discover-chats", { bot_token });
export const sendTelegramTest = (message = "") =>
  api.post("/api/auth/telegram-test", { message });

// ── Trading ───────────────────────────────────────────────────────────────
export const placeOrder = (order: object) => api.post("/api/trading/orders", order);
export const getOrders = () => api.get("/api/trading/orders");
export const cancelOrder = (id: string) => api.delete(`/api/trading/orders/${id}`);
export const getPositions = () => api.get("/api/trading/positions");
export const getTrades = () => api.get("/api/trading/trades");
export const getTradingMode = () => api.get("/api/trading/mode");
export const setMode = (mode: string, broker?: string) =>
  api.post("/api/trading/mode", { mode, broker });
export const killSwitch = () => api.post("/api/trading/kill-switch");
export const getTradingKillSwitchStatus = () => api.get("/api/trading/kill-switch");
export const updateTradingKillSwitch = (active: boolean) =>
  api.put("/api/trading/kill-switch", { active });
export const getPortfolioSummary = () => api.get("/api/trading/portfolio-summary");
export const getStrategyAgentStatus = () => api.get("/api/trading/strategy-agent/status");
export const getStrategyEquityHistory = () => api.get("/api/trading/strategy-agent/equity-history");
export const runStrategyAgentOnce = (force = true) =>
  api.post("/api/trading/strategy-agent/run-once", null, { params: { force } });
export const closeStrategyAgentPosition = (strategyKey: string, symbol: string, reason = "operator_override") =>
  api.post("/api/trading/strategy-agent/positions/close", {
    strategy_key: strategyKey,
    symbol,
    reason,
  });
export const getRiskStatus = () => api.get("/api/trading/risk-status");
export const updateRiskConfig = (config: object) => api.put("/api/trading/risk-config", config);

// ── Commodity ─────────────────────────────────────────────────────────────
export const getCommodityStrategyStatus = () => api.get("/api/commodity/strategy-agent/status");
export const getCommodityOverview = () => api.get("/api/commodity/overview");
// Read-only NIFTY/BANKNIFTY MP+OF rows for the desk watchlist (monitor only —
// this lane never trades them).
export const getCommodityIndexMonitor = () => api.get("/api/commodity/index-monitor");
export const startCommodityStrategyAgent = () => api.post("/api/commodity/strategy-agent/start");
export const runCommodityStrategyOnce = (force = true) =>
  api.post("/api/commodity/strategy-agent/run-once", null, { params: { force } });
export const updateCommodityStrategyConfig = (symbols: string[]) =>
  api.put("/api/commodity/strategy-agent/config", { symbols });
export const getCommodityStrategyContracts = () =>
  api.get("/api/commodity/strategy-agent/contracts");
// Options sleeve deprecated — kept as no-op for backwards compat with any
// caller that still imports it. Resolves immediately without hitting the API.
export const updateCommodityStrategyContracts = async (
  _selectedOptionExpiries: Record<string, string>,
) => ({ data: { status: "noop", detail: "Commodity options sleeve deprecated." } });
export const getCommodityKillSwitchStatus = () => api.get("/api/commodity/kill-switch");
export const updateCommodityKillSwitch = (active: boolean) =>
  api.put("/api/commodity/kill-switch", { active });
export const getCommodityOrders = (limit?: number) =>
  api.get("/api/commodity/orders", { params: { limit } });
export const getCommodityPositions = () => api.get("/api/commodity/positions");
export const getCommodityReports = (limit?: number) =>
  api.get("/api/commodity/reports", { params: { limit } });
export const getCommodityWatchlistSnapshot = (expiry?: string) =>
  api.get("/api/commodity/watchlist-snapshot", { params: { expiry } });
export const getCommodityProfileHistory = (root: string) =>
  api.get(`/api/commodity/profile-history/${encodeURIComponent(root)}`);
// MP+OF for any instrument — Market Profile (+ order flow where volume exists)
// from existing spot candles. Works for indices, commodities, and F&O stocks.
export const getCommodityIndexMpof = (
  symbol: string,
  timeframe: "5minute" | "15minute" | "30minute" = "30minute",
  sessions = 5,
) => api.get("/api/commodity/index-mpof", { params: { symbol, timeframe, sessions } });
// Legacy options-watchlist stubs — deprecated. The endpoints were removed
// from the backend; these resolve immediately with an empty payload so any
// page that still imports them keeps compiling.
export const getCommodityATMWatchlist = async (_expiry?: string) => ({
  data: {
    rows: [] as unknown[],
    source: "deprecated" as string,
    detail: "Commodity options sleeve removed." as string,
  } as Record<string, unknown>,
});
export const getCommodityATMWatchlistExpiries = async () => ({
  data: { expiries: [] as string[] } as Record<string, unknown>,
});

// ── Market ────────────────────────────────────────────────────────────────
export const getOptionChain = (symbol: string, expiry?: string) =>
  api.get(`/api/market/option-chain/${encodeURIComponent(symbol)}`, { params: { expiry } });
export const getOptionExpiries = (symbol: string) =>
  api.get(`/api/market/expiries/${encodeURIComponent(symbol)}`);
export const getATMWatchlistExpiries = (expiry?: string, liveRefresh = false) =>
  api.get("/api/market/atm-watchlist/expiries", { params: { expiry, live_refresh: liveRefresh } });
export const getATMWatchlist = (expiry?: string, liveRefresh = false) =>
  api.get("/api/market/atm-watchlist", { params: { expiry, live_refresh: liveRefresh } });
export const getMarketProfile = (symbol: string, timeframe = "daily") =>
  api.get(`/api/market/market-profile/${encodeURIComponent(symbol)}`, { params: { timeframe } });
export const getIVRank = (symbol: string) => api.get(`/api/market/iv-rank/${encodeURIComponent(symbol)}`);
export const getPCR = (symbol: string, expiry?: string) =>
  api.get(`/api/market/pcr/${encodeURIComponent(symbol)}`, { params: { expiry } });
export const getLTP = (symbols: string[]) => api.post("/api/market/ltp", { symbols });
export const getLatestTicks = (symbols: string[]) =>
  api.post("/api/market/latest-ticks", { symbols }, { timeout: LATEST_TICKS_TIMEOUT_MS });
export const getMarketIntelligenceContext = () => api.get("/api/market/intelligence-context");
export const getFnoAnalytics = (limit = 20) => api.get("/api/market/fno-analytics", { params: { limit } });
export const getGreeks = (symbol: string, strike: number, expiry: string, optionType: string, spot: number, iv = 0.2) =>
  api.get(`/api/market/greeks/${encodeURIComponent(symbol)}/${strike}/${expiry}/${optionType}`, {
    params: { spot, iv },
  });

// ── Analytics ─────────────────────────────────────────────────────────────
export const getPerformance = (period = "today") =>
  api.get("/api/analytics/performance", { params: { period } });
export const getEquityCurve = () => api.get("/api/analytics/equity-curve");
export const getCalendarHeatmap = () => api.get("/api/analytics/calendar-heatmap");
export const getPortfolioGreeks = () => api.get("/api/analytics/portfolio-greeks");
export const getSectorRotation = (timeframe = "daily") =>
  api.get("/api/analytics/sector-rotation", { params: { timeframe } });
export const getSectorRotationComponents = (sectorCode: string, timeframe = "daily") =>
  api.get(`/api/analytics/sector-rotation/${encodeURIComponent(sectorCode)}/components`, { params: { timeframe } });
export const getMacroDashboard = () => api.get("/api/analytics/macro-dashboard");

// ── Macro Research / Sector Discovery ─────────────────────────────────────
export const getMacroResearchOverview = (refresh = false) =>
  api.get("/api/macro-research/overview", { params: { refresh } });
export const getMacroResearchSectors = (refresh = false) =>
  api.get("/api/macro-research/sectors", { params: { refresh } });
export const getMacroResearchSector = (sectorCode: string, refresh = false) =>
  api.get(`/api/macro-research/sectors/${encodeURIComponent(sectorCode)}`, { params: { refresh } });
export const getMacroResearchBuddingSectors = (refresh = false) =>
  api.get("/api/macro-research/budding-sectors", { params: { refresh } });
export const getMacroResearchSources = () => api.get("/api/macro-research/sources");
export const searchMacroResearch = (q: string, sector?: string, limit = 12, refresh = false) =>
  api.get("/api/macro-research/search", { params: { q, sector, limit, refresh } });

// ── Sector Interaction / Alternative Data ─────────────────────────────────
export const getSectorInteractionOverview = () => api.get("/api/sector-interaction/overview");
export const getSectorInteractionIndiaOverview = () => api.get("/api/sector-interaction/india/overview");
export const getSectorInteractionIndiaRealModel = (periods = 160, maxLag = 2, alpha = 0.05, timeframe = "daily") =>
  api.get("/api/sector-interaction/india/real-model", {
    params: { periods, max_lag: maxLag, alpha, timeframe },
  });
export const getSectorInteractionIndiaSector = (sectorKey: string) =>
  api.get(`/api/sector-interaction/sectors/${encodeURIComponent(sectorKey)}`);
export const getSectorInteractionMarketIntelligence = () => api.get("/api/sector-interaction/market-intelligence");
export const getSectorInteractionNSEConstituentStatus = () =>
  api.get("/api/sector-interaction/nse-constituents/status");
export const syncSectorInteractionNSEConstituents = (timeoutSeconds = 8) =>
  api.post("/api/sector-interaction/nse-constituents/sync", null, {
    params: { timeout_seconds: timeoutSeconds },
  });
export const getSectorInteractionModel = (country = "US", periods = 160, maxLag = 2, alpha = 0.05) =>
  api.get("/api/sector-interaction/model", {
    params: { country, periods, max_lag: maxLag, alpha },
  });
export const getSectorInteractionSourceMap = (country = "US") =>
  api.get("/api/sector-interaction/source-map", { params: { country } });
export const getSectorInteractionSignals = (country = "US", periods = 160) =>
  api.get("/api/sector-interaction/signals", { params: { country, periods } });
export const getSectorInteractionExtendedNetwork = (country = "US", periods = 160, maxLag = 2, alpha = 0.05) =>
  api.get("/api/sector-interaction/extended-network", {
    params: { country, periods, max_lag: maxLag, alpha },
  });
export const getSectorInteractionValidationBacktest = (country = "US", periods = 160) =>
  api.get("/api/sector-interaction/validation-backtest", { params: { country, periods } });
export const getSectorInteractionPipelineStatus = (country = "US") =>
  api.get("/api/sector-interaction/pipeline-status", { params: { country } });
export const getSectorInteractionIngestionStatus = (country = "US") =>
  api.get("/api/sector-interaction/ingestion-status", { params: { country } });
export const runSectorInteractionIngestion = (country = "US", dryRun = true, includePrototype = false) =>
  api.post("/api/sector-interaction/run-ingestion", null, {
    params: { country, dry_run: dryRun, include_prototype: includePrototype },
  });
export const runSectorInteractionIndiaLiveMarketIngestion = (dryRun = true) =>
  api.post("/api/sector-interaction/india/run-live-market-ingestion", null, {
    params: { dry_run: dryRun },
  });
export const getSectorInteractionReport = (country = "US", periods = 160) =>
  api.get("/api/sector-interaction/report", { params: { country, periods } });
export const getSectorInteractionAcquisitionPlan = () => api.get("/api/sector-interaction/acquisition-plan");
export const seedSectorInteractionRAG = () => api.post("/api/sector-interaction/seed-rag");

// ── Agent ─────────────────────────────────────────────────────────────────
export const getProposals = () => api.get("/api/agent/proposals");
export const approveProposal = (id: string) => api.post(`/api/agent/proposals/${id}/approve`);
export const rejectProposal = (id: string) => api.post(`/api/agent/proposals/${id}/reject`);
export const runScan = (symbols?: string[]) => api.post("/api/agent/run-scan", { symbols });
export const getAgentLog = (limit = 50) => api.get("/api/agent/agent-log", { params: { limit } });
export const chatWithAgent = (message: string) => api.post("/api/agent/chat", { message });
export const getRulesStatus = () => api.get("/api/agent/rules-status");

// ── Shared RAG / Agent Memory ─────────────────────────────────────────────
export const getRAGHealth = () => api.get("/api/rag/health");
export const searchRAG = (payload: object) => api.post("/api/rag/search", payload);
export const runRAGContextGate = (payload: object) => api.post("/api/rag/context-gate", payload);
export const addRAGDocument = (payload: object) => api.post("/api/rag/documents", payload);
export const addRAGTradeCase = (payload: object) => api.post("/api/rag/trade-cases", payload);

// ── MACD Analysis ─────────────────────────────────────────────────────────
export const startMacdBacktest = (payload: object) =>
  api.post("/api/analysis/macd-backtest/start", payload);
export const getMacdBacktestStatus = (taskId: string) =>
  api.get(`/api/analysis/macd-backtest/status/${taskId}`);
export const getMacdBacktestResults = (taskId: string) =>
  api.get(`/api/analysis/macd-backtest/results/${taskId}`);
export const listMacdBacktestTasks = () =>
  api.get("/api/analysis/macd-backtest/tasks");
export const getAnalysisBrokerStatus = () => api.get("/api/analysis/broker-status");
export const getFoUnderlyings = () => api.get("/api/analysis/fo-underlyings");
export const getResearchCacheStatus = () => api.get("/api/analysis/research-cache-status");
export const getLatestValidationReport = () => api.get("/api/analysis/validation-report/latest");
export const getLatestGreeksSyncReport = () => api.get("/api/analysis/greeks-sync-report/latest");
export const getAllCredsStatus = () => api.get("/api/auth/all-credentials-status");

// ── Strategy Dashboard ───────────────────────────────────────────────────
export const getStrategyDataStatus = () => api.get("/api/strategy/data-status");
export const getStrategySignals = (underlying = "SENSEX", limit = 30) =>
  api.get("/api/strategy/signals", { params: { underlying, limit } });
export const getStrategyAgentComments = (limit = 20) =>
  api.get("/api/strategy/agent-comments", { params: { limit } });
export const getStrategyTrades = (underlying = "SENSEX", limit = 50) =>
  api.get("/api/strategy/trades", { params: { underlying, limit } });
export const getStrategyPortfolio = (underlying = "SENSEX") =>
  api.get("/api/strategy/portfolio", { params: { underlying } });
export const getStrategyOpenSignals = (underlying = "SENSEX") =>
  api.get("/api/strategy/open-signals", { params: { underlying } });
// MACD diffusion — hourly CE/PE-above-zero breadth (market sentiment).
export const getMacdDiffusion = (days = 30, market = "NSE") =>
  api.get("/api/strategy/diffusion", { params: { days, market } });

// ── CBE Scanner ───────────────────────────────────────────────────────────
export const getCBEConfig = () => api.get("/api/cbe/config");
export const getCBEUniverse = (limit = 500) => api.get("/api/cbe/universe", { params: { limit } });
export const getCBELatestScan = (source?: string) => api.get("/api/cbe/latest", { params: { source } });
export const runCBEScan = (payload: object) => api.post("/api/cbe/scan", payload, { timeout: 90_000 });
export const getCBEInstrumentAnalytics = (symbol: string, lookbackDays = 300) =>
  api.get(`/api/cbe/instruments/${encodeURIComponent(symbol)}/analytics`, {
    params: { lookback_days: lookbackDays },
    timeout: 90_000,
  });

// CBE paper-trading book (alpha engine v1+)
export const getCBEPaperSummary = () => api.get("/api/cbe/paper-summary");
export const getCBEPaperPositions = (status: "all" | "open" | "closed" = "all", limit = 100) =>
  api.get("/api/cbe/paper-positions", { params: { status, limit } });
export const getCBEPaperJournal = (instrument?: string, limit = 100) =>
  api.get("/api/cbe/paper-journal", { params: { instrument, limit } });
export const resetCBEPaper = (actor?: string) =>
  api.post("/api/cbe/reset-paper", { confirm: "RESET", actor });

// ── Directional Long Options ───────────────────────────────────────────────
export const getDirectionalOptionsSummary = () =>
  api.get("/api/directional-options/summary");
export const getDirectionalOptionsWorkspace = (
  underlying = "NIFTY",
  timeframe = "5minute",
  lookbackSessions = 16,
) =>
  api.get("/api/directional-options/workspace", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions },
  });
export const getDirectionalOptionsBacktest = (
  underlying = "NIFTY",
  timeframe = "5minute",
  lookbackSessions = 16,
) =>
  api.get("/api/directional-options/backtest", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions },
  });
export const getDirectionalOptionsLiveSnapshot = (
  underlying = "NIFTY",
  timeframe = "5minute",
  lookbackSessions = 16,
) =>
  api.get("/api/directional-options/live-snapshot", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions },
  });
export const runDirectionalOptionsPaperProposal = (
  underlying = "NIFTY",
  timeframe = "5minute",
  lookbackSessions = 16,
) =>
  api.post("/api/directional-options/paper-proposal", null, {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions },
  });
export const getDirectionalOptionsPaperJournal = (symbol?: string, limit = 50) =>
  api.get("/api/directional-options/paper-journal", { params: { symbol, limit } });
export const getDirectionalOptionsPaperPositions = (symbol?: string, status = "all", limit = 50) =>
  api.get("/api/directional-options/paper-positions", { params: { symbol, status, limit } });
export const getDirectionalOptionsPolicy = () =>
  api.get("/api/directional-options/policy");
export const getDirectionalOptionsPaperSummary = () =>
  api.get("/api/directional-options/paper-summary");

// ── MACD Refined ──────────────────────────────────────────────────────────
export const getMacdRefinedSummary = () => api.get("/api/macd-refined/summary");
export const getMacdRefinedBacktest = (source = "research", underlyings?: string, expiryCount = 8) =>
  api.get("/api/macd-refined/backtest", { params: { source, underlyings, expiry_count: expiryCount } });
export const getMacdRefinedBacktestCompare = (underlyings?: string, expiryCount = 8) =>
  api.get("/api/macd-refined/backtest-compare", { params: { underlyings, expiry_count: expiryCount } });
export const getMacdRefinedPositioning = () => api.get("/api/macd-refined/positioning");
export const runMacdRefinedLiveCycle = (allowEntries = true) =>
  api.post("/api/macd-refined/run-live-cycle", null, { params: { allow_entries: allowEntries } });
export const getMacdRefinedPaperPositions = (symbol?: string, status = "all", limit = 50) =>
  api.get("/api/macd-refined/paper-positions", { params: { symbol, status, limit } });
export const getMacdRefinedPaperJournal = (symbol?: string, limit = 50) =>
  api.get("/api/macd-refined/paper-journal", { params: { symbol, limit } });
export const getMacdRefinedPaperSummary = () => api.get("/api/macd-refined/paper-summary");
export const getMacdRefinedSignals = (underlying?: string, limit = 100) =>
  api.get("/api/macd-refined/signals", { params: { underlying, limit } });

// ── US MACD Refined (Alpaca, paper) ───────────────────────────────────────
export const getUsMacdSummary = () => api.get("/api/us/macd-refined/summary");
export const getUsMacdDataHealth = () => api.get("/api/us/macd-refined/data-source-health");
export const getUsMacdPositioning = () => api.get("/api/us/macd-refined/positioning");
export const getUsMacdSignals = (underlying?: string, limit = 100) =>
  api.get("/api/us/macd-refined/signals", { params: { underlying, limit } });
export const runUsMacdLiveCycle = (allowEntries = true) =>
  api.post("/api/us/macd-refined/run-live-cycle", null, { params: { allow_entries: allowEntries } });
export const getUsMacdPaperPositions = (symbol?: string, status = "all", limit = 50) =>
  api.get("/api/us/macd-refined/paper-positions", { params: { symbol, status, limit } });
export const getUsMacdPaperSummary = () => api.get("/api/us/macd-refined/paper-summary");

// ── Gann TP Delta Harmonic ────────────────────────────────────────────────
export const getGannTPDeltaSummary = () => api.get("/api/gann-tp-delta/summary");
export const getGannTPDeltaWorkspace = (
  underlying = "NIFTY",
  timeframe = "15minute",
  lookbackSessions = 60,
  anchorMode = "auto_pivot",
  hMode = "median_tpd",
  manualH?: number,
) =>
  api.get("/api/gann-tp-delta/workspace", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions, anchor_mode: anchorMode, h_mode: hMode, manual_h: manualH },
  });
export const getGannTPDeltaLiveSnapshot = (
  underlying = "NIFTY",
  timeframe = "15minute",
  lookbackSessions = 60,
  anchorMode = "auto_pivot",
  hMode = "median_tpd",
  manualH?: number,
) =>
  api.get("/api/gann-tp-delta/live-snapshot", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions, anchor_mode: anchorMode, h_mode: hMode, manual_h: manualH },
  });
export const getGannTPDeltaBacktest = (
  underlying = "NIFTY",
  timeframe = "15minute",
  lookbackSessions = 60,
  anchorMode = "auto_pivot",
  hMode = "median_tpd",
) =>
  api.get("/api/gann-tp-delta/backtest", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions, anchor_mode: anchorMode, h_mode: hMode },
  });
export const runGannTPDeltaPaperProposal = (
  underlying = "NIFTY",
  timeframe = "15minute",
  lookbackSessions = 60,
  anchorMode = "auto_pivot",
  hMode = "median_tpd",
) =>
  api.post("/api/gann-tp-delta/paper-proposal", null, {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions, anchor_mode: anchorMode, h_mode: hMode },
  });
export const getGannTPDeltaPaperJournal = (symbol?: string, limit = 50) =>
  api.get("/api/gann-tp-delta/paper-journal", { params: { symbol, limit } });
export const getGannTPDeltaPaperAgentStatus = (limit = 50) =>
  api.get("/api/gann-tp-delta/paper-agent/status", { params: { limit } });
export const runGannTPDeltaPaperAgentOnce = (
  timeframe = "15minute",
  lookbackSessions = 60,
  anchorMode = "auto_pivot",
  hMode = "median_tpd",
  liveRefresh = false,
  maxUnderlyings = 0,
) =>
  api.post("/api/gann-tp-delta/paper-agent/run-once", null, {
    params: { timeframe, lookback_sessions: lookbackSessions, anchor_mode: anchorMode, h_mode: hMode, live_refresh: liveRefresh, max_underlyings: maxUnderlyings },
    timeout: 120_000,
  });

// ── Auction Intelligence ─────────────────────────────────────────────────
export const getAuctionIntelligenceSummary = () =>
  api.get("/api/auction-intelligence/summary");
export const getAuctionIntelligenceDefaultConfig = () =>
  api.get("/api/auction-intelligence/default-config");
export const getAuctionIntelligenceDemoScenario = (
  symbol = "NIFTY",
  scenario = "acceptance_up",
) =>
  api.get("/api/auction-intelligence/demo-scenario", { params: { symbol, scenario } });
export const getAuctionIntelligenceLiveSnapshot = (symbol = "NIFTY") =>
  api.get("/api/auction-intelligence/live-snapshot", { params: { symbol } });
export const runAuctionIntelligenceAnalysis = (payload: object) =>
  api.post("/api/auction-intelligence/analyze", payload);
export const runAuctionIntelligencePaperProposal = (payload: object) =>
  api.post("/api/auction-intelligence/paper-proposal", payload);
export const getAuctionIntelligencePaperStatus = () =>
  api.get("/api/auction-intelligence/paper-status");
export const runAuctionIntelligencePaperRunOnce = (symbol?: string) =>
  api.post("/api/auction-intelligence/paper-run-once", null, { params: { symbol } });
export const runAuctionIntelligenceGateAValidation = (payload: object) =>
  api.post("/api/auction-intelligence/validate-gate-a", payload);
export const getAuctionIntelligenceGateBValidation = (
  symbol = "BANKNIFTY",
  mode: "live" | "demo" = "live",
  scenario = "acceptance_up",
  session_limit = 8,
  lookback_days = 45,
) =>
  api.get("/api/auction-intelligence/validate-gate-b", {
    params: { symbol, mode, scenario, session_limit, lookback_days },
  });
export const runAuctionIntelligenceShadowBackfill = (
  symbol = "BANKNIFTY",
  session_limit = 20,
  lookback_days = 45,
  observation_bars = 4,
  snapshot_cutoff = "11:15",
  shadow_net_liquidation = 1_000_000,
  payload: object = {},
) =>
  api.post("/api/auction-intelligence/shadow-backfill", payload, {
    params: {
      symbol,
      session_limit,
      lookback_days,
      observation_bars,
      snapshot_cutoff,
      shadow_net_liquidation,
    },
  });
export const getAuctionIntelligenceGateCValidation = (
  symbol = "BANKNIFTY",
  session_limit = 30,
  record_limit = 500,
) =>
  api.get("/api/auction-intelligence/validate-gate-c", {
    params: { symbol, session_limit, record_limit },
  });
export const getAuctionIntelligenceCanaryReadiness = (symbol = "BANKNIFTY") =>
  api.get("/api/auction-intelligence/canary-readiness", { params: { symbol } });
export const getAuctionIntelligencePaperJournal = (symbol?: string, limit = 50) =>
  api.get("/api/auction-intelligence/paper-journal", { params: { symbol, limit } });
export const getAuctionIntelligencePaperPositions = (symbol?: string, status = "all", limit = 50) =>
  api.get("/api/auction-intelligence/paper-positions", { params: { symbol, status, limit } });
export const getAuctionIntelligenceShadowRecords = (symbol = "BANKNIFTY", limit = 50) =>
  api.get("/api/auction-intelligence/shadow-records", { params: { symbol, limit } });

// ── Auction Intelligence — MP signal layer ────────────────────────────────
export const getAuctionIntelligenceMPDataStatus = () =>
  api.get("/api/auction-intelligence/mp-data-status");
export const getAuctionIntelligenceMPSignals = (underlying = "NIFTY", limit = 20) =>
  api.get("/api/auction-intelligence/mp-signals", { params: { underlying, limit } });
export const getAuctionIntelligenceMPOpenSignal = (underlying = "NIFTY") =>
  api.get("/api/auction-intelligence/mp-open-signal", { params: { underlying } });
export const getAuctionIntelligenceMPAgentContext = (underlying = "NIFTY", limit = 10) =>
  api.get("/api/auction-intelligence/mp-agent-context", { params: { underlying, limit } });
export const getAuctionIntelligenceMPDashboard = (underlying = "NIFTY", lookback = 30) =>
  api.get("/api/auction-intelligence/mp-dashboard", { params: { underlying, lookback } });
export const getMPAnalytics = (underlying = "NIFTY", lookback = 60) =>
  api.get("/api/auction-intelligence/mp-analytics", { params: { underlying, lookback } });
export const getMPMultiTFProfile = (underlying = "NIFTY") =>
  api.get("/api/auction-intelligence/mp-multi-tf-profile", { params: { underlying } });
export const getMPRegimeHistory = (underlying = "NIFTY", lookback = 60) =>
  api.get("/api/auction-intelligence/mp-regime-history", { params: { underlying, lookback } });
export const getMPSetupPerformance = (underlying = "NIFTY") =>
  api.get("/api/auction-intelligence/mp-setup-performance", { params: { underlying } });
export const getMPConceptDrift = (underlying = "NIFTY", window = 20) =>
  api.get("/api/auction-intelligence/mp-concept-drift", { params: { underlying, window } });
export const getMPOrderflowProxy = (underlying = "NIFTY", lookback = 60) =>
  api.get("/api/auction-intelligence/mp-orderflow-proxy", { params: { underlying, lookback } });

// ── Institutional Orderflow ───────────────────────────────────────────────
export const getOrderflowSnapshot = (
  symbols = "NIFTY",
  intervals = "3,5,15,30",
  historySessions = 5,
) =>
  api.get("/api/orderflow/snapshot", {
    params: { symbols, intervals, history_sessions: historySessions },
    timeout: 60_000,
  });

// ── Fractal Market Profile ────────────────────────────────────────────────
export const getFractalMarketProfileSummary = () =>
  api.get("/api/fractal-market-profile/summary");
export const getFractalMarketProfileLiveSnapshot = (symbol = "NIFTY") =>
  api.get("/api/fractal-market-profile/live-snapshot", { params: { symbol } });
export const runFractalMarketProfilePaperProposal = (symbol = "NIFTY") =>
  api.post("/api/fractal-market-profile/paper-proposal", null, { params: { symbol } });
export const getFractalMarketProfilePaperJournal = (symbol?: string, limit = 50) =>
  api.get("/api/fractal-market-profile/paper-journal", { params: { symbol, limit } });
export const getFractalMarketProfilePaperPositions = (symbol?: string, status = "all", limit = 50) =>
  api.get("/api/fractal-market-profile/paper-positions", { params: { symbol, status, limit } });
export const getFractalMarketProfileReplayReport = (symbol = "NIFTY", force = false) =>
  api.get("/api/fractal-market-profile/replay-report", { params: { symbol, force } });
export const getFractalMarketProfileReplaySuite = (force = false) =>
  api.get("/api/fractal-market-profile/replay-suite", { params: { force } });

// ── OHLC Charts (verification module) ─────────────────────────────────────
export const getChartUniverse = () => api.get("/api/charts/universe");
export const getChartOHLC = (
  underlying: string,
  timeframe: "15minute" | "30minute" | "60minute" = "30minute",
  lookbackSessions = 5,
) =>
  api.get("/api/charts/ohlc", {
    params: { underlying, timeframe, lookback_sessions: lookbackSessions },
    timeout: 30_000,
  });

// Per-ATM-strike option-premium OHLC + indicators (MACD, RSI, BB, KAMA) —
// powers the pop-up chart on the NSE signal desk.
export const getOptionOHLC = (params: {
  underlying: string;
  expiry: string;
  strike: number;
  optionType: string;
  interval?: "5minute" | "15minute" | "30minute";
  limit?: number;
  instrumentKey?: string | null;
}) =>
  api.get("/api/charts/option-ohlc", {
    params: {
      underlying: params.underlying,
      expiry: params.expiry,
      strike: params.strike,
      option_type: params.optionType,
      interval: params.interval ?? "30minute",
      limit: params.limit ?? 200,
      ...(params.instrumentKey ? { instrument_key: params.instrumentKey } : {}),
    },
    timeout: 30_000,
  });

// ── Backtester ────────────────────────────────────────────────────────────
export const getBacktesterDefaultConfig = () => api.get("/api/backtester/default-config");
export const runBacktestJson = (payload: object) => api.post("/api/backtester/run-json", payload);
export const runBacktestBreeze = (payload: object) => api.post("/api/backtester/run-breeze", payload);
export const runWalkForward = (payload: object) => api.post("/api/backtester/walk-forward", payload);
export const runSensitivity = (payload: object) => api.post("/api/backtester/sensitivity", payload);
export const uploadBacktestCsv = (formData: FormData) =>
  api.post("/api/backtester/run-csv", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
