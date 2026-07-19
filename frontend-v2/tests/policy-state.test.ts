/**
 * Policy-state + decision-waterfall honesty tests (2026-07-19).
 *
 * The Strategies view is the screen most able to lie, because it puts four
 * lanes that measure different things side by side. These tests lock the four
 * ways it could:
 *
 *   1. Inventing a state a lane never emits (an ARMED tier for Auction or
 *      MP+OF; an EXITING tier where no exit source exists).
 *   2. Turning a missing gate into a FAILED gate — three of the four policies
 *      emit no flow-confirmation gate and three emit no anti-chase test.
 *   3. Averaging the policies into a consensus score, which is the thing that
 *      destroys the information the screen exists to show.
 *   4. Letting a lone opinion read as agreement.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  AUCTION_NO_ARMED_STAGE,
  MPOF_NO_ARMED_STAGE,
  POLICY_COLUMNS,
  POLICY_COLUMN_MEMBERS,
  POLICY_HORIZONS,
  STAGE_KEYS,
  auctionCell,
  auctionWaterfall,
  convergenceCell,
  convergenceWaterfall,
  directionalCell,
  directionalWaterfall,
  findDisagreements,
  mpofCell,
  mpofWaterfall,
  opinionCount,
  policyOperatesAt,
  policyStateVariant,
  waterfallCoverage,
  type PolicyCellData,
} from "../src/lib/policy-state.ts";

const SRC = path.join(import.meta.dirname, "..", "src");
const read = (...p: string[]) => readFileSync(path.join(SRC, ...p), "utf8");

const convergenceBase = {
  available: true,
  reason: null,
  setupState: "WATCHING",
  action: "FLAT",
  direction: "LONG",
  score: 40,
  confirmations: 1,
  required: 2,
  blocked: [],
};

// ─── 1. Green semantics ─────────────────────────────────────────────────────

test("only ACTIONABLE is green; ARMED is blue and UNAVAILABLE is neutral", () => {
  assert.equal(policyStateVariant("ACTIONABLE"), "success");
  assert.equal(policyStateVariant("ARMED"), "info");
  assert.equal(policyStateVariant("WATCHING"), "neutral");
  assert.equal(policyStateVariant("UNAVAILABLE"), "neutral");
  assert.equal(policyStateVariant("BLOCKED"), "error");
});

// ─── 2. No invented states ──────────────────────────────────────────────────

test("Auction never reports ARMED, and says why", () => {
  const c = auctionCell({
    loaded: true,
    reason: null,
    regime: "balanced",
    confidence: 0.9, // high confidence must NOT be promoted into an armed tier
    allowed: true,
    reasons: [],
    agentActions: ["FLAT", "FLAT"],
    executionPlanCount: 0,
  });
  assert.equal(c.state, "WATCHING");
  assert.equal(c.note, AUCTION_NO_ARMED_STAGE);
});

test("MP+OF never reports ARMED, and a candidate without a signal is BLOCKED", () => {
  const watching = mpofCell("mpof_index", {
    available: true,
    reason: null,
    mpStatus: "ready",
    dataReason: null,
    signal: null,
    candidate: null,
    candidateReason: null,
    validationDetail: null,
    confidence: null,
  });
  assert.equal(watching.state, "WATCHING");
  assert.equal(watching.note, MPOF_NO_ARMED_STAGE);

  const blocked = mpofCell("mpof_index", {
    available: true,
    reason: null,
    mpStatus: "ready",
    dataReason: null,
    signal: null,
    candidate: "BUY",
    candidateReason: "value migration not confirmed",
    validationDetail: "candidate held: developing value has not accepted",
    confidence: 0.4,
  });
  assert.equal(blocked.state, "BLOCKED");
  assert.deepEqual(blocked.blockers, ["value migration not confirmed"]);
});

test("a warming-up profile is UNAVAILABLE, not WATCHING", () => {
  const c = mpofCell("mpof_index", {
    available: true,
    reason: null,
    mpStatus: "warming_up",
    dataReason: null,
    signal: null,
    candidate: null,
    candidateReason: null,
    validationDetail: null,
    confidence: null,
  });
  assert.equal(c.state, "UNAVAILABLE");
  assert.match(c.unavailableReason!, /warming up/);
});

test("an unloaded heavy snapshot is UNAVAILABLE with the reason, never a guess", () => {
  const a = auctionCell({ loaded: false, reason: null, regime: null, allowed: null, reasons: [] });
  assert.equal(a.state, "UNAVAILABLE");
  assert.match(a.unavailableReason!, /per-symbol snapshot/);
  assert.equal(a.direction, null);

  const d = directionalCell({ loaded: false, reason: null });
  assert.equal(d.state, "UNAVAILABLE");
  assert.match(d.unavailableReason!, /universe only/);
  assert.equal(d.confidence, null);
});

test("convergence maps its native lifecycle directly, without reinterpretation", () => {
  assert.equal(convergenceCell({ ...convergenceBase, setupState: "ARMED" }).state, "ARMED");
  assert.equal(
    convergenceCell({ ...convergenceBase, setupState: "CONFIRMED", action: "LONG" }).state,
    "ACTIONABLE",
  );
  assert.equal(convergenceCell({ ...convergenceBase, setupState: "CONFLICT" }).state, "BLOCKED");
  assert.equal(
    convergenceCell({ ...convergenceBase, setupState: "MISSED_NO_CHASE" }).state,
    "BLOCKED",
  );
  // EXPIRED is a lapsed window, not a block.
  const expired = convergenceCell({ ...convergenceBase, setupState: "EXPIRED" });
  assert.equal(expired.state, "WATCHING");
  assert.match(expired.note!, /lapsed/);
  // In the universe but not evaluated this cycle is UNAVAILABLE, not WATCHING.
  const none = convergenceCell({ ...convergenceBase, setupState: null });
  assert.equal(none.state, "UNAVAILABLE");
});

test("a convergence STOCK row is blocked on hardcoded gates, and says so", () => {
  const c = convergenceCell({ ...convergenceBase, kind: "stock", setupState: "WATCHING" });
  assert.equal(c.state, "BLOCKED");
  assert.match(c.note!, /hardcoded false/);
});

test("a directional signal with no contract is ARMED, with one selected it is ACTIONABLE", () => {
  const base = {
    loaded: true,
    reason: null,
    regimeLabel: "trend_up",
    tradeAllowed: true,
    signalDirection: "CE",
    signalConfidence: 0.62,
    executionReady: true,
  };
  assert.equal(directionalCell({ ...base, hasSelectedContract: false }).state, "ARMED");
  assert.equal(directionalCell({ ...base, hasSelectedContract: true }).state, "ACTIONABLE");
});

// ─── 3. Missing gates are UNAVAILABLE, never FAILED ─────────────────────────

test("only Convergence emits a flow-confirmation gate; the rest say they do not", () => {
  const conv = convergenceWaterfall({
    ...convergenceBase,
    gates: {
      structural_setup_armed: true,
      confirmation_2_of_3: false,
      not_chasing: true,
      reward_risk_1_5: true,
    },
    readinessGates: { tick_fresh: true, real_tick_cvd: true },
  });
  assert.equal(conv.find((s) => s.key === "flow")!.verdict, "failed");
  assert.equal(conv.find((s) => s.key === "anti_chase")!.verdict, "passed");

  const auc = auctionWaterfall({
    loaded: true,
    reason: null,
    regime: "balanced",
    allowed: true,
    reasons: [],
    allowedDirections: ["LONG"],
    executionPlanCount: 1,
    staleSeconds: 10,
  });
  const aucFlow = auc.find((s) => s.key === "flow")!;
  assert.equal(aucFlow.verdict, "unavailable");
  assert.match(aucFlow.reason!, /no discrete flow-confirmation gate/);
  assert.equal(auc.find((s) => s.key === "anti_chase")!.verdict, "unavailable");

  const dir = directionalWaterfall({
    loaded: true,
    reason: null,
    regimeLabel: "trend_up",
    tradeAllowed: true,
    signalDirection: "CE",
    hasSelectedContract: true,
    executionReady: true,
  });
  assert.equal(dir.find((s) => s.key === "flow")!.verdict, "unavailable");
  assert.match(dir.find((s) => s.key === "flow")!.reason!, /consumes no order flow/);
});

test("the index MP+OF risk stage is unavailable because the HTF gate is commodity-only", () => {
  const idx = mpofWaterfall({
    available: true,
    reason: null,
    mpStatus: "ready",
    dataReason: null,
    signal: "BUY",
    candidate: null,
    candidateReason: null,
    validationDetail: null,
    confidence: 0.5,
    mpDirection: "up",
    isCommodity: false,
  });
  const risk = idx.find((s) => s.key === "risk")!;
  assert.equal(risk.verdict, "unavailable");
  assert.match(risk.reason!, /commodity-only/);
});

test("an unavailable stage always carries a reason, and a failed one never does", () => {
  for (const stages of [
    auctionWaterfall({ loaded: false, reason: null, regime: null, allowed: null, reasons: [] }),
    directionalWaterfall({ loaded: false, reason: null }),
  ]) {
    assert.equal(stages.length, STAGE_KEYS.length);
    for (const s of stages) {
      assert.equal(s.verdict, "unavailable");
      assert.ok(s.reason && s.reason.length > 10, `${s.key} has no reason`);
    }
  }
});

test("the honest picture is that most stage cells are not emitted at all", () => {
  const all = [
    ...auctionWaterfall({
      loaded: true,
      reason: null,
      regime: "balanced",
      allowed: true,
      reasons: [],
      allowedDirections: ["LONG"],
      executionPlanCount: 1,
      staleSeconds: 5,
    }),
    ...directionalWaterfall({
      loaded: true,
      reason: null,
      regimeLabel: "trend_up",
      tradeAllowed: true,
      signalDirection: "CE",
      hasSelectedContract: true,
      executionReady: true,
    }),
    ...mpofWaterfall({
      available: true,
      reason: null,
      mpStatus: "ready",
      dataReason: null,
      signal: "BUY",
      candidate: null,
      candidateReason: null,
      validationDetail: null,
      confidence: 0.5,
      mpDirection: "up",
      isCommodity: false,
    }),
  ];
  // Auction and Directional emit nothing at flow OR anti-chase; the index
  // MP+OF monitor emits nothing at anti-chase OR risk. That is six genuinely
  // unavailable cells out of eighteen even on a fully-loaded, fully-healthy
  // instrument. If this count drops, a stage was invented rather than wired.
  assert.ok(waterfallCoverage(all).unavailable >= 6, "stages became suspiciously well-covered");
});

// ─── 4. Disagreement, never consensus ───────────────────────────────────────

const cellOf = (p: PolicyCellData["policyId"], over: Partial<PolicyCellData>): PolicyCellData => ({
  policyId: p,
  state: "WATCHING",
  direction: null,
  confidence: null,
  nativeState: null,
  validity: null,
  blockers: [],
  note: null,
  unavailableReason: null,
  ...over,
});

test("opposite sides are surfaced as a pair, not averaged", () => {
  const d = findDisagreements([
    cellOf("convergence", { state: "ACTIONABLE", direction: "LONG" }),
    cellOf("directional", { state: "ACTIONABLE", direction: "PE" }),
  ]);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "opposite_direction");
  assert.match(d[0].detail, /LONG/);
  assert.match(d[0].detail, /SHORT/);
});

test("actionable-vs-blocked on the same side is still a disagreement", () => {
  const d = findDisagreements([
    cellOf("convergence", { state: "ACTIONABLE", direction: "LONG" }),
    cellOf("mpof_index", { state: "BLOCKED", direction: "BUY", blockers: ["value not accepted"] }),
  ]);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "actionable_vs_blocked");
});

test("an UNAVAILABLE policy holds no opinion and never disagrees", () => {
  const cells = [
    cellOf("convergence", { state: "ACTIONABLE", direction: "LONG" }),
    cellOf("auction", { state: "UNAVAILABLE", direction: "SHORT" }),
  ];
  assert.deepEqual(findDisagreements(cells), []);
  assert.equal(opinionCount(cells), 1);
});

test("no consensus score, average or agreement tally exists anywhere", () => {
  const src = read("lib", "policy-state.ts").replace(/\/\*[\s\S]*?\*\//g, "");
  for (const banned of ["consensus", "average", "agreementScore", "netScore"]) {
    assert.ok(!src.includes(banned), `policy-state must not compute ${banned}`);
  }
  const strip = read("components", "market-structure", "strategies", "DisagreementStrip.tsx");
  assert.match(strip, /no second opinion/i);
});

// ─── 5. Horizon scope is declared, and empty intersections are stated ───────

test("each policy declares the horizons it operates at, with evidence", () => {
  for (const col of POLICY_COLUMNS) {
    for (const pid of POLICY_COLUMN_MEMBERS[col]) {
      const scope = POLICY_HORIZONS[pid];
      assert.ok(scope.horizons.length > 0);
      assert.ok(scope.evidence.length > 10, `${pid} has no evidence for its horizon scope`);
      // Nothing claims the scalp horizon — it is structurally unsatisfiable.
      assert.ok(!scope.horizons.includes("scalp"), `${pid} may not claim scalp`);
    }
  }
  assert.equal(policyOperatesAt("directional", "positional"), true);
  assert.equal(policyOperatesAt("auction", "positional"), false);
});

test("a policy that does not operate at a horizon states it in the cell", () => {
  const cellSrc = read("components", "market-structure", "strategies", "PolicyCell.tsx");
  assert.match(cellSrc, /does not operate at this horizon/);
  // And it must print the evidence, not just the claim.
  assert.match(cellSrc, /scope\.evidence/);
});

// ─── 6. Query discipline ────────────────────────────────────────────────────

test("the strategy matrix shares the drawer's convergence key and never auto-polls the heavy ones", () => {
  const hook = read("components", "market-structure", "strategies", "useStrategyMatrix.ts");
  assert.match(hook, /\["ms-detail", "convergence", ctx\.market, symbol\]/);
  // Both heavy queries are request-gated and non-polling.
  const auto = [...hook.matchAll(/refetchInterval: false/g)];
  assert.ok(auto.length >= 3, "a heavy per-symbol query is polling");
  assert.match(hook, /enabled: enabled && wantAuction/);
  assert.match(hook, /enabled: enabled && wantDirectional/);
  // The whole view is gated on being the active view.
  const view = read("components", "market-structure", "strategies", "StrategiesView.tsx");
  assert.match(view, /ctx\.view === "strategies"/);
});
