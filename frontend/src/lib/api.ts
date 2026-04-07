import axios from "axios";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const api = axios.create({
  baseURL: API_URL,
  headers: { "Content-Type": "application/json" },
  timeout: 30000,
});

// ── Auth & Credentials ────────────────────────────────────────────────────
export const connectBroker = (broker: string, credentials: Record<string, string>) =>
  api.post("/api/auth/connect-broker", { broker, credentials });

export const saveCredentials = (broker: string, credentials: Record<string, string>) =>
  api.post("/api/auth/save-credentials", { broker, credentials });

export const getCredentialsStatus = (broker: string) =>
  api.get(`/api/auth/credentials/${broker}`);

export const getBrokerStatus = () => api.get("/api/auth/broker-status");

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
export const getRiskStatus = () => api.get("/api/trading/risk-status");
export const updateRiskConfig = (config: object) => api.put("/api/trading/risk-config", config);

// ── Commodity ─────────────────────────────────────────────────────────────
export const getCommodityStrategyStatus = () => api.get("/api/commodity/strategy-agent/status");
export const startCommodityStrategyAgent = () => api.post("/api/commodity/strategy-agent/start");
export const runCommodityStrategyOnce = (force = true) =>
  api.post("/api/commodity/strategy-agent/run-once", null, { params: { force } });
export const updateCommodityStrategyConfig = (symbols: string[]) =>
  api.put("/api/commodity/strategy-agent/config", { symbols });
export const getCommodityStrategyContracts = () =>
  api.get("/api/commodity/strategy-agent/contracts");
export const updateCommodityStrategyContracts = (selectedOptionExpiries: Record<string, string>) =>
  api.put("/api/commodity/strategy-agent/contracts", { selected_option_expiries: selectedOptionExpiries });
export const getCommodityKillSwitchStatus = () => api.get("/api/commodity/kill-switch");
export const updateCommodityKillSwitch = (active: boolean) =>
  api.put("/api/commodity/kill-switch", { active });
export const getCommodityOrders = (limit?: number) =>
  api.get("/api/commodity/orders", { params: { limit } });
export const getCommodityPositions = () => api.get("/api/commodity/positions");
export const getCommodityReports = (limit?: number) =>
  api.get("/api/commodity/reports", { params: { limit } });
export const getCommodityATMWatchlistExpiries = () =>
  api.get("/api/commodity/atm-watchlist/expiries");
export const getCommodityATMWatchlist = (expiry?: string) =>
  api.get("/api/commodity/atm-watchlist", { params: { expiry } });

// ── Market ────────────────────────────────────────────────────────────────
export const getOptionChain = (symbol: string, expiry?: string) =>
  api.get(`/api/market/option-chain/${encodeURIComponent(symbol)}`, { params: { expiry } });
export const getOptionExpiries = (symbol: string) =>
  api.get(`/api/market/expiries/${encodeURIComponent(symbol)}`);
export const getATMWatchlistExpiries = () =>
  api.get("/api/market/atm-watchlist/expiries");
export const getATMWatchlist = (expiry?: string) =>
  api.get("/api/market/atm-watchlist", { params: { expiry } });
export const getMarketProfile = (symbol: string, timeframe = "daily") =>
  api.get(`/api/market/market-profile/${encodeURIComponent(symbol)}`, { params: { timeframe } });
export const getIVRank = (symbol: string) => api.get(`/api/market/iv-rank/${encodeURIComponent(symbol)}`);
export const getPCR = (symbol: string, expiry?: string) =>
  api.get(`/api/market/pcr/${encodeURIComponent(symbol)}`, { params: { expiry } });
export const getLTP = (symbols: string[]) => api.post("/api/market/ltp", { symbols });
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
export const getMacroDashboard = () => api.get("/api/analytics/macro-dashboard");

// ── Agent ─────────────────────────────────────────────────────────────────
export const getProposals = () => api.get("/api/agent/proposals");
export const approveProposal = (id: string) => api.post(`/api/agent/proposals/${id}/approve`);
export const rejectProposal = (id: string) => api.post(`/api/agent/proposals/${id}/reject`);
export const runScan = (symbols?: string[]) => api.post("/api/agent/run-scan", { symbols });
export const getAgentLog = (limit = 50) => api.get("/api/agent/agent-log", { params: { limit } });
export const chatWithAgent = (message: string) => api.post("/api/agent/chat", { message });
export const getRulesStatus = () => api.get("/api/agent/rules-status");

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

// ── Auction Intelligence — MP signal layer ────────────────────────────────
export const getAuctionIntelligenceMPDataStatus = () =>
  api.get("/api/auction-intelligence/mp-data-status");
export const getAuctionIntelligenceMPSignals = (underlying = "NIFTY", limit = 20) =>
  api.get("/api/auction-intelligence/mp-signals", { params: { underlying, limit } });
export const getAuctionIntelligenceMPOpenSignal = (underlying = "NIFTY") =>
  api.get("/api/auction-intelligence/mp-open-signal", { params: { underlying } });
export const getAuctionIntelligenceMPAgentContext = (underlying = "NIFTY", limit = 10) =>
  api.get("/api/auction-intelligence/mp-agent-context", { params: { underlying, limit } });

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
