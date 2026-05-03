import { create } from "zustand";

// ── Types ─────────────────────────────────────────────────────────────────

export type TradingMode = "paper" | "live";
export type BrokerName = "fyers" | "upstox" | "fivepaisa" | "icici_breeze";

interface Position {
  symbol: string;
  action: string;
  qty: number;
  avg_price: number;
  ltp: number;
  unrealized_pnl: number;
  instrument_type: string;
  expiry?: string;
  strike?: number;
  option_type?: string;
}

interface Proposal {
  id: string;
  symbol: string;
  strategy: string;
  entry: number;
  sl: number;
  target: number;
  qty: number;
  rationale: string;
  confidence: "HIGH" | "MED" | "LOW";
  status: string;
  created_at: string;
}

interface PortfolioSummary {
  total_equity: number;
  available_capital: number;
  unrealized_pnl: number;
  realized_pnl: number;
  day_pnl: number;
  win_rate: number;
  sharpe_ratio: number;
  max_drawdown: number;
}

export interface Tick {
  symbol: string;
  ltp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  oi: number;
  timestamp: string;
  source?: string;
  stale?: boolean;
  stale_seconds?: number | null;
}

interface BrokerStatus {
  broker: BrokerName;
  connected: boolean;
  ready?: boolean;
  session_active?: boolean;
  state?: string | null;
  detail?: string | null;
  source?: string | null;
  needs_reconnect?: boolean;
  user_id?: string | null;
  name?: string | null;
  connected_at?: string | null;
}

// ── Tick store (isolated to prevent full re-renders on every tick) ─────────
// Using a separate store for high-frequency tick data so components that
// only need portfolio/positions don't re-render on each tick.

interface TickStore {
  ticks: Map<string, Tick>;
  updateTick: (tick: Tick) => void;
  getTick: (symbol: string) => Tick | undefined;
}

export const useTickStore = create<TickStore>((set, get) => ({
  ticks: new Map(),
  updateTick: (tick) =>
    set((s) => {
      const next = new Map(s.ticks);
      next.set(tick.symbol, tick);
      return { ticks: next };
    }),
  getTick: (symbol) => get().ticks.get(symbol),
}));

// Convenience selector hook for a single tick — only re-renders when THAT symbol changes
export function useTickSymbol(symbol: string): Tick | undefined {
  return useTickStore((s) => s.ticks.get(symbol));
}

// ── Main App Store ─────────────────────────────────────────────────────────

interface AppStore {
  // Mode
  mode: TradingMode;
  activeBroker: BrokerName;
  setMode: (mode: TradingMode) => void;
  setActiveBroker: (broker: BrokerName) => void;

  // Broker status
  brokerStatuses: BrokerStatus[];
  setBrokerStatuses: (statuses: BrokerStatus[]) => void;

  // Positions
  positions: Position[];
  setPositions: (positions: Position[]) => void;

  // Portfolio
  portfolio: PortfolioSummary | null;
  setPortfolio: (summary: PortfolioSummary) => void;

  // Agent proposals
  proposals: Proposal[];
  setProposals: (proposals: Proposal[]) => void;
  addProposal: (proposal: Proposal) => void;
  removeProposal: (id: string) => void;

  // India VIX
  indiaVix: number;
  setIndiaVix: (v: number) => void;

  // Kill switch state
  killSwitchActive: boolean;
  setKillSwitchActive: (v: boolean) => void;
}

export const useStore = create<AppStore>()((set) => ({
    mode: "paper",
    activeBroker: "fyers",
    setMode: (mode) => set({ mode }),
    setActiveBroker: (activeBroker) => set({ activeBroker }),

    brokerStatuses: [],
    setBrokerStatuses: (brokerStatuses) => set({ brokerStatuses }),

    positions: [],
    setPositions: (positions) => set({ positions }),

    portfolio: null,
    setPortfolio: (portfolio) => set({ portfolio }),

    proposals: [],
    setProposals: (proposals) => set({ proposals }),
    addProposal: (proposal) =>
      set((s) => ({ proposals: [proposal, ...s.proposals] })),
    removeProposal: (id) =>
      set((s) => ({ proposals: s.proposals.filter((p) => p.id !== id) })),

    indiaVix: 0,
    setIndiaVix: (indiaVix) => set({ indiaVix }),

    killSwitchActive: false,
    setKillSwitchActive: (killSwitchActive) => set({ killSwitchActive }),
  }));
