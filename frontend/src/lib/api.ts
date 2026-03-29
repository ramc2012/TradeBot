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
export const getPortfolioSummary = () => api.get("/api/trading/portfolio-summary");
export const getStrategyAgentStatus = () => api.get("/api/trading/strategy-agent/status");
export const runStrategyAgentOnce = (force = true) =>
  api.post("/api/trading/strategy-agent/run-once", null, { params: { force } });
export const getRiskStatus = () => api.get("/api/trading/risk-status");
export const updateRiskConfig = (config: object) => api.put("/api/trading/risk-config", config);

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
