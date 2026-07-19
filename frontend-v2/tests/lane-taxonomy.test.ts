/**
 * Lane-grouping honesty tests (2026-07-19).
 *
 * Same runner as the other two suites — `node --test`, native TypeScript
 * type-stripping, no framework — so everything here is either a pure module
 * assertion or a source-text assertion over the rendered files.
 *
 * What these lock down:
 *   1. Horizon is a DECLARED table, never inferred from cadence.
 *   2. Every lane key served by the backend registry has an entry, and an
 *      unknown key is ADMITTED rather than bucketed into intraday.
 *   3. Scalp exists, is empty, is permanent, and cites the capability records.
 *   4. Lanes that take no position are excluded from the horizon axis rather
 *      than dumped into a trading bucket.
 *   5. The grid reuses the shipped lane status ladder instead of defining a
 *      second one.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  HORIZON_ORDER,
  KIND_ORDER,
  LANE_HORIZON,
  SCALP_UNAVAILABLE,
  TRADING_HORIZONS,
  groupLanesByHorizon,
  groupLanesByKind,
  laneHorizon,
  laneHorizons,
} from "../src/lib/lane-taxonomy.ts";
import { MISSING_CAPABILITY_KEYS, missingCapability } from "../src/lib/flow-provenance.ts";

const SRC = path.join(import.meta.dirname, "..", "src");
const read = (...p: string[]) => readFileSync(path.join(SRC, ...p), "utf8");

/**
 * The 32 lane keys emitted by backend/core/lane_registry.py get_registry(),
 * transcribed from the `key="…"` declarations. If the backend adds a lane, this
 * list and the table must both move — which is the point of asserting it.
 */
const REGISTRY_KEYS = [
  "option_flow_watchdog",
  "token_readiness",
  "market_intelligence",
  "auction_intelligence",
  "auction_intelligence_commodity",
  "institutional_convergence",
  "institutional_convergence_commodity",
  "fractal_market_profile",
  "directional_options",
  "directional_positioning",
  "commodity_mp_history",
  "macd_refined",
  "macd_refined_marks",
  "cbe_scanner",
  "cbe_marks",
  "gann_tp_delta",
  "lane_audit",
  "s1_atm_30m_macd",
  "s2_index_mp_macd",
  "commodity_mp_orderflow",
  "us_macd_refined",
  "research_sync",
  "macd_diffusion",
  "greeks_enrichment",
  "chain_candle_builder",
  "option_ws_subscription_manager",
  "held_position_marks_refresh",
  "commodity_mark_refresh",
  "rl_auto_trainer",
  "event_loop_lag_monitor",
  "live_candle_store",
  "quote_bus",
];

// ─── 1. Every served lane is declared, with its evidence ────────────────────

test("every registry lane key has a declared horizon and a cited reason", () => {
  assert.equal(REGISTRY_KEYS.length, 32);
  for (const key of REGISTRY_KEYS) {
    const entry = LANE_HORIZON[key];
    assert.ok(entry, `${key} has no declared horizon`);
    assert.notEqual(entry.horizon, "unclassified");
    assert.ok(entry.evidence.length > 10, `${key} has no evidence for its horizon`);
  }
});

test("an unknown lane key is admitted, never bucketed into a trading horizon", () => {
  const e = laneHorizon("some_lane_the_backend_added_yesterday");
  assert.equal(e.horizon, "unclassified");
  assert.match(e.evidence, /no horizon is declared/);
  // The critical negative: it must not silently become intraday.
  assert.notEqual(e.horizon, "intraday");
});

test("horizon is never derived from cadence_seconds", () => {
  const src = read("lib", "lane-taxonomy.ts");
  assert.doesNotMatch(src.replace(/\/\*[\s\S]*?\*\//g, ""), /cadence_seconds/);
});

test("lane-taxonomy is runtime dependency-free, so it stays unit-testable", () => {
  const src = read("lib", "lane-taxonomy.ts").replace(/\/\*[\s\S]*?\*\//g, "");
  const imports = [...src.matchAll(/^import .*$/gm)].map((m) => m[0]);
  assert.equal(imports.length, 1);
  assert.match(imports[0], /^import type /);
});

// ─── 2. Scalp is permanently unavailable, not empty-but-possible ────────────

test("scalp is a first-class horizon with zero members and a permanent reason", () => {
  assert.ok(TRADING_HORIZONS.includes("scalp"));
  assert.equal(SCALP_UNAVAILABLE.permanent, true);
  assert.ok(SCALP_UNAVAILABLE.reason.length > 100);
  assert.deepEqual(SCALP_UNAVAILABLE.missingCapabilities.slice().sort(), MISSING_CAPABILITY_KEYS.slice().sort());

  const declaredScalp = Object.entries(LANE_HORIZON).filter(([, v]) => v.horizon === "scalp");
  assert.equal(declaredScalp.length, 0, "no lane may claim the scalp horizon");
});

test("the scalp reason resolves to the shared capability records", () => {
  const caps = SCALP_UNAVAILABLE.missingCapabilities.map(missingCapability);
  assert.equal(caps.length, 2);
  for (const c of caps) {
    assert.equal(c.permanent, true);
    assert.ok(c.citation.includes("backend/"));
  }
});

test("grouping by horizon always returns the scalp group carrying its record", () => {
  const groups = groupLanesByHorizon([{ key: "s1_atm_30m_macd", kind: "strategy-engine" }]);
  const scalp = groups.find((g) => g.id === "scalp");
  assert.ok(scalp);
  assert.equal(scalp!.lanes.length, 0);
  assert.equal(scalp!.unavailable?.permanent, true);
  // Every other populated group must NOT carry an unavailability record.
  const intraday = groups.find((g) => g.id === "intraday");
  assert.equal(intraday!.unavailable, null);
  assert.equal(intraday!.lanes.length, 1);
});

// ─── 3. Non-trading lanes are excluded, not mis-bucketed ────────────────────

test("monitors and data-plane lanes land in not_a_trading_lane, not intraday", () => {
  for (const key of ["quote_bus", "greeks_enrichment", "market_intelligence", "lane_audit"]) {
    assert.equal(laneHorizon(key).horizon, "not_a_trading_lane", key);
  }
});

test("the dual-horizon lane appears in BOTH of its groups", () => {
  assert.deepEqual(laneHorizons("directional_options"), ["intraday", "positional"]);
  const groups = groupLanesByHorizon([{ key: "directional_options", kind: "scheduler-runner" }]);
  assert.equal(groups.find((g) => g.id === "intraday")!.lanes.length, 1);
  assert.equal(groups.find((g) => g.id === "positional")!.lanes.length, 1);
});

test("scalp is ordered first so the gap is seen, not scrolled past", () => {
  assert.equal(HORIZON_ORDER[0], "scalp");
});

// ─── 4. The kind axis is the served one ─────────────────────────────────────

test("kind grouping preserves the registry's own kinds and tolerates new ones", () => {
  const groups = groupLanesByKind([
    { key: "a", kind: "monitor" },
    { key: "b", kind: "strategy-engine" },
    { key: "c", kind: "a-kind-nobody-declared" },
  ]);
  assert.deepEqual(groups.map((g) => g.id), [
    "strategy-engine",
    "monitor",
    "a-kind-nobody-declared",
  ]);
  assert.ok(KIND_ORDER.includes("strategy-engine"));
});

// ─── 5. The grid reuses the shipped status ladder ───────────────────────────

test("LaneGroupGrid derives no status ladder of its own", () => {
  const grid = read("components", "market-structure", "lanes", "LaneGroupGrid.tsx");
  assert.match(grid, /laneDisplayStatus/);
  assert.match(grid, /laneDisplayVariant/);
  // A bespoke ladder would show up as a local mapping over the raw status word.
  assert.doesNotMatch(grid, /case "ready":/);
  assert.doesNotMatch(grid, /"success"/);
});

test("the scalp group renders as struck-through and locked, never as loading", () => {
  const grid = read("components", "market-structure", "lanes", "LaneGroupGrid.tsx");
  assert.match(grid, /line-through/);
  assert.match(grid, /permanently unavailable/);
  const view = read("components", "market-structure", "strategies", "StrategiesView.tsx");
  assert.match(view, /permanently unavailable · not an empty row/);
});
