/**
 * Shared TS shapes for the Auction Intelligence desk, derived from the prod
 * `/api/auction-intelligence/live-snapshot` payload. Every numeric field is
 * nullable in practice (historical replay, missing option chain, empty agent
 * lane) so types tolerate `null`/`undefined` everywhere.
 */
import type { OrderFlow } from "@/components/strategies/shared";

export type Bar = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type MarketProfile = Record<string, any> & {
  symbol?: string;
  session_date?: string;
  open_price?: number;
  high_price?: number;
  low_price?: number;
  close_price?: number;
  poc?: number;
  vah?: number;
  val?: number;
  initial_balance_high?: number;
  initial_balance_low?: number;
  initial_balance_range?: number;
  day_range?: number;
  single_prints?: number[];
  bracket_state?: string | null;
  value_area_overlap?: number | null;
  poc_shift?: number | null;
  value_migration?: number | null;
};

export type Regime = {
  label?: string;
  confidence?: number;
  allowed_directions?: string[];
  reasons?: string[];
  scorecard?: Record<string, number>;
};

export type Risk = {
  allowed?: boolean;
  kill_switch?: boolean;
  max_size_multiplier?: number;
  reasons?: string[];
};

export type AgentDecision = {
  agent_name: string;
  action: string;
  confidence?: number | null;
  entry_price?: number | null;
  stop_price?: number | null;
  target_price?: number | null;
  quantity?: number | null;
  sleeve_fraction?: number | null;
  rationale?: string[];
  bucket?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  metadata?: Record<string, any> | null;
};

export type ExecutionStep = {
  agent_name?: string;
  symbol?: string;
  action?: string;
  style?: string;
  order_type?: string;
  limit_price?: number | null;
  slices?: number;
  cancel_after_seconds?: number;
  rationale?: string[];
  quantity?: number;
  trading_symbol?: string | null;
  premium?: number | null;
  spot_price?: number | null;
  moneyness?: string | null;
  selection_reason?: string | null;
};

export type NtmVolx = {
  underlying?: string;
  expiry?: string;
  atm_strike?: number;
  spot_price?: number;
  dominant_side?: "CALLS" | "PUTS" | "BALANCED";
  directional_bias?: string;
  regime?: string;
  vxr?: number;
  net_pressure?: number;
  call_pressure?: number;
  put_pressure?: number;
  call_notional?: number;
  put_notional?: number;
  call_oi_change?: number;
  put_oi_change?: number;
  call_wall_strike?: number | null;
  put_wall_strike?: number | null;
  pair_count?: number;
  notes?: string[];
  pressure_ladder?: Array<{
    strike: number;
    distance_from_spot?: number;
    distance_from_spot_pct?: number;
    call_volume?: number;
    put_volume?: number;
    net_pressure?: number;
  }>;
} | null;

export type Analysis = {
  market_profile?: MarketProfile;
  prior_market_profile?: MarketProfile;
  order_flow?: OrderFlow;
  regime?: Regime;
  risk?: Risk;
  agent_decisions?: AgentDecision[];
  execution_plan?: ExecutionStep[];
  ntm_volx?: NtmVolx;
};

export type DataStatus = {
  live_mode?: boolean;
  snapshot_mode?: string;
  quote_source?: string;
  order_flow_source?: string;
  stale_data_seconds?: number;
  degraded_reason?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

export type RagRetrieval = {
  id?: string;
  title?: string;
  text?: string;
  score?: number;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  metadata?: Record<string, any>;
};

export type RagContext = {
  decision?: string;
  confidence?: number;
  summary?: string;
  reason_codes?: string[];
  case_stats?: {
    matched_cases?: number;
    resolved_cases?: number;
    wins?: number;
    losses?: number;
    win_rate?: number;
    expectancy?: number;
    best_pnl?: number;
    worst_pnl?: number;
  };
  retrievals?: RagRetrieval[];
} | null;

export type Snapshot = {
  mode?: string;
  symbol_code?: string;
  session_date?: string;
  available_symbols?: string[];
  data_status?: DataStatus;
  rag_context?: RagContext;
  request?: {
    session?: { last_price?: number; minutes_to_close?: number; broker_connected?: boolean };
    quote?: Record<string, unknown>;
    bars?: Bar[];
    prior_bars?: Bar[];
    depth?: Record<string, unknown>;
    trades?: unknown[];
  };
  analysis?: Analysis;
};

export type GateCheck = {
  key?: string;
  label?: string;
  passed?: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  observed?: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  threshold?: any;
  severity?: string;
  detail?: string;
};

export type GateResult = {
  gate?: string;
  label?: string;
  passed?: boolean;
  score?: number;
  generated_at?: string;
  checks?: GateCheck[];
  blockers?: string[] | null;
};

export type CanaryReadiness = {
  symbol?: string;
  ready?: boolean;
  stage?: string;
  blockers?: string[];
  requirements?: {
    manual_approval_required?: boolean;
    allowed_agents?: string[];
    max_live_lots?: number;
    daily_loss_limit?: number;
    max_size_multiplier?: number;
  };
  gate_b?: GateResult & { context?: Record<string, unknown> };
  gate_c?: GateResult & { context?: Record<string, unknown> };
};
