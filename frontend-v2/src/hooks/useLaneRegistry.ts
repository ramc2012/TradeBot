"use client";

/**
 * useLaneRegistry — the ONE lane inventory for the whole terminal.
 *
 * Polls GET /api/system/lanes, the single backend source of truth assembled in
 * backend/core/lane_registry.py (every supervisor runner, own-loop strategy
 * agent, daemon and parked product lane, each joined with its CURRENT runtime
 * state). This replaces the frontend's fragmented hardcoded lane lists.
 *
 * "One UI over the split": the frontend talks ONLY to the core plane. When the
 * backend is split into two processes (core + strategies), core merges the
 * strategy plane's runner status via Redis into these snapshots and tags any
 * foreign plane whose snapshot has gone stale (`snapshot_stale` / `foreign_plane`).
 * Nothing here needs to know whether the backend is one process or two — it
 * consumes core's aggregated /api/system/lanes only.
 */
import { useQuery } from "@tanstack/react-query";

import { REFRESH_MS } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

/** Declarative + live shape of one lane, straight off /api/system/lanes. */
export type LaneSnapshot = {
  // Declarative (from the registry spec)
  key: string;
  label: string;
  kind: "strategy-engine" | "scheduler-runner" | "product-lane" | "monitor" | string;
  execution_mode: "paper" | "live" | "parked" | "none" | string;
  cadence_seconds?: number | null;
  broker_profile?: string | null;
  exchange_session?: string | null;
  enabled_flag_name?: string | null;
  runner_keys?: string[];
  paper_book_source?: string | null;
  status_endpoint?: string | null;
  audit_coverage?: boolean;
  audit_lane_key?: string | null;
  notes?: string | null;
  // Live (from the state source)
  status:
    | "running"
    | "ready"
    | "enabled"
    | "configured"
    | "disabled"
    | "parked"
    | "stale"
    | "error"
    | "unknown"
    | string;
  enabled?: boolean | null;
  running?: boolean | null;
  stale?: boolean | null;
  last_error?: string | null;
  last_success_at?: string | null;
  last_message?: string | null;
  next_run_at?: string | null;
  loop_active?: boolean | null;
  plane?: string | null;
  foreign_plane?: boolean | null;
  snapshot_stale?: boolean | null;
  snapshot_age_seconds?: number | null;
  probed?: boolean | null;
  // Risk VISIBILITY (never enforcement)
  risk_breach?: boolean | null;
  risk_breach_reason?: string | null;
  error?: string | null;
};

export type LaneRegistrySummary = {
  generated_at?: string;
  total?: number;
  by_kind?: Record<string, number>;
  by_status?: Record<string, number>;
  by_execution_mode?: Record<string, number>;
  audit_covered?: number;
  audit_uncovered?: number;
  risk_breached?: number;
  risk_breached_keys?: string[];
  risk_unknown?: number;
};

export type LaneRegistryResponse = {
  lanes: LaneSnapshot[];
  summary: LaneRegistrySummary;
};

/**
 * Registry key → existing v2 desk route. Lanes NOT in this map (monitors,
 * daemons, data-plane runners) have no per-desk terminal and render as
 * status-only rows. Every route here already exists under src/app/strategies.
 */
export const LANE_ROUTE_BY_KEY: Record<string, string> = {
  // strategy engines / paper cycles with a desk
  s1_atm_30m_macd: "/strategies/nse/live",
  s2_index_mp_macd: "/strategies/mp",
  macd_refined: "/strategies/macd-refined",
  macd_refined_marks: "/strategies/macd-refined",
  us_macd_refined: "/strategies/us-macd-refined",
  directional_options: "/strategies/directional",
  directional_positioning: "/strategies/directional",
  auction_intelligence: "/strategies/auction",
  auction_intelligence_commodity: "/strategies/auction",
  rl_auto_trainer: "/strategies/auction",
  fractal_market_profile: "/strategies/fractal",
  gann_tp_delta: "/strategies/gann",
  cbe_scanner: "/strategies/cbe",
  cbe_marks: "/strategies/cbe",
  institutional_convergence: "/strategies/institutional-convergence",
  institutional_convergence_commodity: "/strategies/institutional-convergence",
  commodity_mp_orderflow: "/strategies/commodity",
  commodity_mp_history: "/strategies/commodity",
  commodity_mark_refresh: "/strategies/commodity",
  // audit lane deep-links to the lane-health board
  lane_audit: "/lane-health",
};

export function laneRoute(key: string): string | null {
  return LANE_ROUTE_BY_KEY[key] ?? null;
}

/** Honest status → desk-ui StatusBadge variant. No lane shows a false green. */
export function laneStatusVariant(
  status: string,
): "success" | "warn" | "error" | "info" | "neutral" {
  switch (status) {
    case "running":
    case "ready":
      return "success";
    case "enabled":
    case "configured":
      return "info";
    case "stale":
      return "warn";
    case "error":
    case "unknown":
      return "error";
    case "parked":
    case "disabled":
    default:
      return "neutral";
  }
}

/** True when the lane belongs to a foreign plane whose snapshot has gone stale. */
export function isPlaneStale(lane: LaneSnapshot): boolean {
  return Boolean(lane.snapshot_stale);
}

// ── Honest derivation layer ─────────────────────────────────────────────────
// The registry's `summary.by_status` block has NO `running` key and NO `stale`
// key (running lives on each lane's `running` flag; staleness on
// `snapshot_stale` / status==="stale"). Folding running+ready into one number,
// or reading `by_status.running`, is exactly the "one misleading green number"
// this sprint removes. So derive every operational tally from the lanes[] array.

/** Statuses that mean "armed for the next session" — configured but not looping. */
const ARMED_STATUSES = new Set(["ready", "enabled", "configured"]);
/** Statuses that mean the lane is intentionally not participating. */
const PARKED_STATUSES = new Set(["parked", "disabled"]);
/** Statuses that are actionable problems (distinct from unknown-risk). */
const ATTENTION_STATUSES = new Set(["stale", "error", "unknown"]);

/** Actionable attention: a real breach, a stale plane/snapshot, or an error. */
export function isLaneAttention(l: LaneSnapshot): boolean {
  return (
    l.risk_breach === true ||
    Boolean(l.snapshot_stale) ||
    ATTENTION_STATUSES.has(String(l.status))
  );
}

/** Armed = not running, but configured/ready to run on the next session. */
export function isLaneArmed(l: LaneSnapshot): boolean {
  return l.running !== true && ARMED_STATUSES.has(String(l.status));
}

/** Parked = intentionally out (parked flag or disabled). */
export function isLaneParked(l: LaneSnapshot): boolean {
  return PARKED_STATUSES.has(String(l.status));
}

/** An execution-capable lane (paper/live) with NO audit coverage. */
export function isExecUncovered(l: LaneSnapshot): boolean {
  return (
    (l.execution_mode === "paper" || l.execution_mode === "live") &&
    !l.audit_coverage
  );
}

export type LaneStats = {
  total: number;
  running: number; // l.running === true — actually looping right now
  armed: number; // not running, but ready/enabled/configured for next session
  parked: number; // parked | disabled
  attention: number; // actionable: breach | stale | error (NOT unknown-risk)
  // Risk coverage — surfaced separately so "3 breaches" isn't read as all-clear.
  riskEvaluated: number; // risk_breach is a real boolean
  riskBreached: number; // risk_breach === true
  riskUnknown: number; // risk_breach == null (never evaluated)
  execUncovered: number; // execution-capable but unaudited
};

export function deriveLaneStats(lanes: LaneSnapshot[]): LaneStats {
  const s: LaneStats = {
    total: lanes.length,
    running: 0,
    armed: 0,
    parked: 0,
    attention: 0,
    riskEvaluated: 0,
    riskBreached: 0,
    riskUnknown: 0,
    execUncovered: 0,
  };
  for (const l of lanes) {
    if (l.running === true) s.running++;
    else if (ARMED_STATUSES.has(String(l.status))) s.armed++;
    if (PARKED_STATUSES.has(String(l.status))) s.parked++;
    if (isLaneAttention(l)) s.attention++;
    if (l.risk_breach == null) {
      s.riskUnknown++;
    } else {
      s.riskEvaluated++;
      if (l.risk_breach) s.riskBreached++;
    }
    if (isExecUncovered(l)) s.execUncovered++;
  }
  return s;
}

/**
 * Honest display status. A green "ready" lane with the market closed is ARMED
 * for the next session, not running — never render a false "live"/"ready" green.
 */
export function laneDisplayStatus(l: LaneSnapshot): string {
  if (l.running === true) return "Running";
  switch (String(l.status)) {
    case "ready":
      return "Armed";
    case "enabled":
      return "Enabled";
    case "configured":
      return "Configured";
    case "stale":
      return "Stale";
    case "error":
      return "Error";
    case "parked":
      return "Parked";
    case "disabled":
      return "Off";
    case "unknown":
      return "Unknown";
    default:
      return String(l.status);
  }
}

/** StatusBadge variant for the honest display status. */
export function laneDisplayVariant(
  l: LaneSnapshot,
): "success" | "warn" | "error" | "info" | "neutral" {
  if (l.running === true) return "success";
  switch (String(l.status)) {
    case "ready":
      return "info"; // armed, not a false green
    case "enabled":
    case "configured":
      return "info";
    case "stale":
      return "warn";
    case "error":
    case "unknown":
      return "error";
    case "parked":
    case "disabled":
    default:
      return "neutral";
  }
}

export function useLaneRegistry() {
  return useQuery<LaneRegistryResponse>({
    queryKey: ["system", "lane-registry"],
    queryFn: async () =>
      (await apiClient.get("/api/system/lanes")).data as LaneRegistryResponse,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });
}
