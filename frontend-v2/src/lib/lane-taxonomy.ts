/**
 * lane-taxonomy — the TWO axes a lane is grouped by, in one declared table.
 *
 * ─── Why this module exists ─────────────────────────────────────────────────
 *
 * `/api/system/lanes` (backend/core/lane_registry.py) carries a lane's KIND —
 * strategy-engine / scheduler-runner / product-lane / monitor. That axis is
 * real, served, and must never be re-derived here.
 *
 * It does NOT carry a HORIZON. There is no `horizon` field anywhere in the
 * registry, and the only horizon-shaped thing on the payload is
 * `cadence_seconds`, which is a POLL INTERVAL, not a holding period:
 * `s1_atm_30m_macd` polls every 60 s and decides on 30-minute bars. Inferring
 * horizon from cadence would therefore manufacture a number the data does not
 * support — the exact failure class this terminal exists to remove.
 *
 * So horizon is a DECLARED, CITED frontend table. Every entry carries the
 * evidence that justifies it (the config field, bar size or flag in the repo),
 * and the UI renders that evidence in the tooltip. When the backend one day
 * emits a horizon of its own, this table is deleted, not "reconciled".
 *
 * A lane key that is NOT in the table renders `unclassified` with a reason —
 * never silently bucketed into intraday. A new backend lane must therefore be
 * declared here to appear on the horizon axis, and until it is, the UI says so.
 *
 * ─── SCALP ──────────────────────────────────────────────────────────────────
 *
 * `scalp` is a first-class horizon with ZERO members and a PERMANENT
 * unavailability record. It is not an empty-but-possible row: a scalp lane
 * needs aggressor-tagged trade prints and/or real L2 depth, and both are
 * structurally absent from every wired feed (see MISSING_CAPABILITIES in
 * flow-provenance.ts). Rendering it as "no lanes yet" would imply the gap is
 * a backlog item rather than a data-capability gap.
 *
 * DEPENDENCY-FREE AT RUNTIME. The only import is a TYPE, so this module can be
 * loaded by bare `node --test` with nothing but type-stripping. The capability
 * records themselves are resolved by the CONSUMER via `missingCapability()` —
 * this module names the gap, the shared contract describes it, and neither
 * duplicates the other.
 */
import type { CapabilityKey } from "./flow-provenance";

// ─── Horizon axis ───────────────────────────────────────────────────────────

/** The four trading horizons the design groups by. */
export type TradingHorizon = "scalp" | "intraday" | "swing" | "positional";

/**
 * Every bucket a lane can land in. `not_a_trading_lane` is deliberate and
 * distinct from `unclassified`: the former is a decision (this lane has no
 * holding period because it never takes a position), the latter is an admission
 * (nobody has declared one).
 */
export type LaneHorizon = TradingHorizon | "not_a_trading_lane" | "unclassified";

export const TRADING_HORIZONS: TradingHorizon[] = ["scalp", "intraday", "swing", "positional"];

export const HORIZON_ORDER: LaneHorizon[] = [
  "scalp",
  "intraday",
  "swing",
  "positional",
  "not_a_trading_lane",
  "unclassified",
];

export const HORIZON_LABEL: Record<LaneHorizon, string> = {
  scalp: "Scalp",
  intraday: "Intraday",
  swing: "Swing",
  positional: "Positional",
  not_a_trading_lane: "Not a trading lane",
  unclassified: "Horizon not declared",
};

export const HORIZON_BLURB: Record<LaneHorizon, string> = {
  scalp: "Seconds to minutes; exits inside the same auction leg.",
  intraday: "Decided and closed within one session.",
  swing: "Held across sessions, inside the current expiry cycle.",
  positional: "Held across expiries; a multi-day or monthly view.",
  not_a_trading_lane:
    "Data-plane, monitor or audit lanes. They take no position, so they have no holding period — they are grouped by KIND only.",
  unclassified:
    "This lane key is served by /api/system/lanes but has no declared horizon in lib/lane-taxonomy.ts. It is shown here rather than guessed into a bucket.",
};

// ─── Scalp: permanently unavailable, with its reason ────────────────────────

export type HorizonUnavailable = {
  horizon: TradingHorizon;
  /** No runtime probe can flip this on today's wired feeds. */
  permanent: true;
  missingCapabilities: CapabilityKey[];
  reason: string;
  citation: string;
};

export const SCALP_UNAVAILABLE: HorizonUnavailable = {
  horizon: "scalp",
  permanent: true,
  missingCapabilities: ["BROKER_AGGRESSOR_PRINTS", "DEPTH_L2"],
  reason:
    "A scalp lane decides on the tape: which side is lifting, what is resting, what got absorbed. Neither input exists here. No wired broker pushes aggressor-tagged trade prints (market_ticks stores quotes and CUMULATIVE volume only), and the DOM/heatmap the backend serves is a broker depth PROXY, not the exchange book. Every buy/sell attribution in this terminal is therefore INFERRED, which is adequate for a 3-minute confirmation and not adequate for a 30-second decision. This is a data-capability gap, not an unbuilt lane.",
  citation:
    "backend/analytics/orderflow.py (module docstring); backend/api/routers/orderflow.py reference_model; flow-provenance AGGRESSOR_TAPE_AVAILABLE = false",
};

/** True when a horizon can never be satisfied on today's feeds. */
export function isHorizonPermanentlyUnavailable(h: LaneHorizon): boolean {
  return h === "scalp";
}

// ─── The declared table ─────────────────────────────────────────────────────

export type LaneHorizonEntry = {
  horizon: LaneHorizon;
  /** The repo fact that justifies the classification — rendered, not hidden. */
  evidence: string;
  /**
   * A lane that genuinely answers at TWO horizons. `directional_options` is the
   * only one: its `positional` signal flag selects the monthly DTE window,
   * which is a different holding period from the same lane's weekly view.
   */
  alsoHorizon?: LaneHorizon;
};

/**
 * Lane key → horizon. Keys are exactly those emitted by
 * backend/core/lane_registry.py `get_registry()` (34 as of 2026-08-27; preopen_spot_snapshot is still absent).
 */
export const LANE_HORIZON: Record<string, LaneHorizonEntry> = {
  // ── strategy engines ──
  s1_atm_30m_macd: {
    horizon: "intraday",
    evidence: "decides on the 30-minute ATM premium bar; squares off within the session (audit_lane_key \"s1\")",
  },
  // s2_index_mp_macd RETIRED 2026-07-20 (owner: MACD + MACD-refined only).
  commodity_mp_orderflow: {
    horizon: "intraday",
    evidence: "3-minute signal bars (commodity_mp_signal.bar_minutes) inside the MCX session",
  },
  // ── scheduler runners with a trading view ──
  institutional_convergence: {
    horizon: "intraday",
    evidence: "3-minute bars with an initial-balance and noon-quarantine gate (institutional_convergence/engine.py)",
  },
  institutional_convergence_commodity: {
    horizon: "intraday",
    evidence: "same engine, MCX roots; 3-minute bars, session-scoped",
  },
  auction_intelligence: {
    horizon: "intraday",
    evidence: "session-scoped auction analysis; regime + execution plan rebuilt per session (auction_intelligence/live.py)",
  },
  auction_intelligence_commodity: {
    horizon: "intraday",
    evidence: "same bundle, MCX session scope",
  },
  directional_options: {
    horizon: "intraday",
    alsoHorizon: "positional",
    evidence:
      "dual-horizon by design: DirectionalSignal.positional selects the MONTHLY DTE window, otherwise the regime's weekly preference (directional_options/schemas.py DirectionalSignal.positional)",
  },
  directional_positioning: {
    horizon: "positional",
    evidence: "daily option-positioning feed (PCR / OI build / HTF) that only the positional view consumes",
  },
  macd_refined: {
    horizon: "swing",
    evidence: "weekly-expiry long-premium lane, held across sessions",
  },
  macd_refined_marks: {
    horizon: "swing",
    evidence: "45-second exit monitor for the swing lane's open premium legs",
  },
  cbe_scanner: {
    horizon: "positional",
    evidence: "daily end-of-day cash-equity pass",
  },
  cbe_marks: {
    horizon: "positional",
    evidence: "marks the CBE positional book",
  },
  gann_tp_delta: {
    horizon: "swing",
    evidence: "15-minute default timeframe with multi-session targets",
  },
  fractal_market_profile: {
    horizon: "intraday",
    evidence: "session profile lane; execution_mode=\"parked\"",
  },
  // us_macd_refined RETIRED 2026-07-20 (owner: MACD + MACD-refined only).
  // ── everything that never takes a position ──
  candidate_capture: {
    horizon: "not_a_trading_lane",
    evidence: "research observer: records every evaluated contract to candidate_snapshots; execution_mode=\"none\", takes no position and makes no broker call",
  },
  candidate_labelling: {
    horizon: "not_a_trading_lane",
    evidence: "post-close outcome resolution for candidate_capture; reads committed rows only and writes candidate_outcomes",
  },
  market_intelligence: {
    horizon: "not_a_trading_lane",
    evidence: "data plane: builds the ATM watchlist every other lane reads",
  },
  stock_spot_sweep: {
    horizon: "not_a_trading_lane",
    evidence: "data plane: kind=\"data\" in the served registry; sweeps stock spot bars and takes no position",
  },
  macd_preopen_watchlist: {
    horizon: "not_a_trading_lane",
    evidence: "data plane: builds the pre-open watchlist the MACD lanes read; execution_mode=\"none\" in the served registry",
  },
  commodity_mp_history: {
    horizon: "not_a_trading_lane",
    evidence: "durable TPO history writer",
  },
  research_sync: {
    horizon: "not_a_trading_lane",
    evidence: "research dataset sync",
  },
  rl_auto_trainer: {
    horizon: "not_a_trading_lane",
    evidence: "trains the auction policy; emits no signal itself",
  },
  chain_candle_builder: {
    horizon: "not_a_trading_lane",
    evidence: "option-chain candle writer",
  },
  greeks_enrichment: {
    horizon: "not_a_trading_lane",
    evidence: "copies broker greeks onto chain snapshots",
  },
  macd_diffusion: {
    horizon: "not_a_trading_lane",
    evidence: "breadth daemon, execution_mode=\"none\"",
  },
  option_ws_subscription_manager: {
    horizon: "not_a_trading_lane",
    evidence: "websocket subscription plumbing",
  },
  held_position_marks_refresh: {
    horizon: "not_a_trading_lane",
    evidence: "refreshes marks on open books across lanes",
  },
  commodity_mark_refresh: {
    horizon: "not_a_trading_lane",
    evidence: "refreshes MCX marks",
  },
  option_flow_watchdog: {
    horizon: "not_a_trading_lane",
    evidence: "watchdog over the option-flow writer",
  },
  token_readiness: {
    horizon: "not_a_trading_lane",
    evidence: "broker-token readiness probe",
  },
  event_loop_lag_monitor: {
    horizon: "not_a_trading_lane",
    evidence: "runtime health monitor",
  },
  live_candle_store: {
    horizon: "not_a_trading_lane",
    evidence: "in-memory candle store",
  },
  quote_bus: {
    horizon: "not_a_trading_lane",
    evidence: "quote fan-out bus",
  },
  lane_audit: {
    horizon: "not_a_trading_lane",
    evidence: "signal-correctness audit over other lanes",
  },
};

const UNDECLARED: LaneHorizonEntry = {
  horizon: "unclassified",
  evidence:
    "no horizon is declared for this lane key in lib/lane-taxonomy.ts, and /api/system/lanes does not carry one — so none is shown",
};

/** Horizon for a lane key. Unknown keys are ADMITTED, never bucketed. */
export function laneHorizon(key: string): LaneHorizonEntry {
  return LANE_HORIZON[key] ?? UNDECLARED;
}

/** Every horizon a lane answers at (one, or two for the dual-horizon lane). */
export function laneHorizons(key: string): LaneHorizon[] {
  const e = laneHorizon(key);
  return e.alsoHorizon ? [e.horizon, e.alsoHorizon] : [e.horizon];
}

// ─── Kind axis (served, not derived) ────────────────────────────────────────

export const KIND_ORDER: string[] = [
  "strategy-engine",
  "scheduler-runner",
  "product-lane",
  "monitor",
];

export const KIND_LABEL: Record<string, string> = {
  "strategy-engine": "Strategy engines",
  "scheduler-runner": "Scheduler runners",
  "product-lane": "Product lanes",
  monitor: "Monitors & data plane",
};

export const KIND_BLURB: Record<string, string> = {
  "strategy-engine": "Own-loop agents that decide and place paper orders themselves.",
  "scheduler-runner": "Supervisor-driven paper cycles run on a cadence.",
  "product-lane": "Packaged lanes with their own book, currently parked.",
  monitor: "Watchdogs, daemons and data-plane writers. They take no position.",
};

// ─── Grouping (pure) ────────────────────────────────────────────────────────

/** The minimum a lane must look like to be grouped. Structural, not nominal. */
export type GroupableLane = { key: string; kind: string };

export type LaneGroup<T> = {
  id: string;
  label: string;
  blurb: string;
  lanes: T[];
  /** Non-null ⇒ the group can never be populated; render it as such. */
  unavailable: HorizonUnavailable | null;
};

/** Group by the registry's own `kind`. Unknown kinds get their own group. */
export function groupLanesByKind<T extends GroupableLane>(lanes: T[]): LaneGroup<T>[] {
  const seen = new Map<string, T[]>();
  for (const l of lanes) {
    const k = String(l.kind || "unknown");
    const bucket = seen.get(k);
    if (bucket) bucket.push(l);
    else seen.set(k, [l]);
  }
  const ordered = [
    ...KIND_ORDER.filter((k) => seen.has(k)),
    ...Array.from(seen.keys())
      .filter((k) => !KIND_ORDER.includes(k))
      .sort(),
  ];
  return ordered.map((k) => ({
    id: k,
    label: KIND_LABEL[k] ?? k,
    blurb: KIND_BLURB[k] ?? "Kind reported by the registry with no local description.",
    lanes: seen.get(k) ?? [],
    unavailable: null,
  }));
}

/**
 * Group by DECLARED horizon.
 *
 * Two deliberate properties:
 *   · every horizon in HORIZON_ORDER is returned even when empty, so `scalp`
 *     is always present and always carries its unavailability record;
 *   · a dual-horizon lane appears in BOTH of its groups (it genuinely answers
 *     at both), rather than being forced into one.
 */
export function groupLanesByHorizon<T extends GroupableLane>(lanes: T[]): LaneGroup<T>[] {
  const buckets = new Map<LaneHorizon, T[]>();
  for (const h of HORIZON_ORDER) buckets.set(h, []);
  for (const l of lanes) {
    for (const h of laneHorizons(l.key)) {
      const bucket = buckets.get(h);
      if (bucket) bucket.push(l);
      else buckets.set(h, [l]);
    }
  }
  return HORIZON_ORDER.filter((h) => h !== "unclassified" || (buckets.get(h) ?? []).length > 0).map(
    (h) => ({
      id: h,
      label: HORIZON_LABEL[h],
      blurb: HORIZON_BLURB[h],
      lanes: buckets.get(h) ?? [],
      unavailable: h === "scalp" ? SCALP_UNAVAILABLE : null,
    }),
  );
}
