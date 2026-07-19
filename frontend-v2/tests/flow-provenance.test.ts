/**
 * Honesty tests for the source-grading contract (2026-07-19).
 *
 * Runs on Node's built-in test runner with native TypeScript type-stripping:
 *
 *   cd frontend-v2 && npm test
 *
 * No test framework is installed in this app and none is added here — the
 * module under test is deliberately dependency-free so it needs none.
 *
 * What these lock down:
 *   1. A quote/bar SOURCE still grades `observed` (no gate can move).
 *   2. A buy/sell-ATTRIBUTED feature NEVER grades `observed`.
 *   3. The aggressor trade-print capability surfaces as unavailable.
 *   4. No order-flow surface in the app prints an observed/REAL trade-print
 *      claim (source scan over the rendered strings).
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  AGGRESSOR_TAPE_AVAILABLE,
  FLOW_ATTRIBUTION_FEATURES,
  classifyFlowGrade,
  classifyOfSource,
  classifySourceGrade,
  describeFlowDerivation,
  isInferredSideGrade,
  sourceGradeLabel,
  sourceGradeVariant,
} from "../src/lib/flow-provenance.ts";

const QUOTE_SOURCES = ["market_ticks", "tick_reconstruction", "ticks", "live_tick"];
const BOOK_SOURCES = ["tick_reconstruction_book", "depth_reconstruction"];
const BAR_SOURCES = ["bar_inference", "bar_proxy", "bar_fallback", "insufficient_ticks"];

// ─── 1. The stream itself is still observed — gates cannot have moved ───────

test("quote/bar SOURCES keep their original grade when no feature is given", () => {
  for (const s of QUOTE_SOURCES) assert.equal(classifySourceGrade(s), "observed");
  for (const s of BOOK_SOURCES) assert.equal(classifySourceGrade(s), "reconstructed");
  for (const s of BAR_SOURCES) assert.equal(classifySourceGrade(s), "bar_inferred");
  assert.equal(classifySourceGrade("timescaledb"), "observed");
  assert.equal(classifySourceGrade("black_scholes"), "modelled");
  assert.equal(classifySourceGrade(""), "unavailable");
  assert.equal(classifySourceGrade(null), "unavailable");
  // Unrecognised strings are still graded DOWN, never promoted.
  assert.equal(classifySourceGrade("something_new"), "bar_inferred");
});

test('an explicit "quote" / "bar" feature is identical to the bare call', () => {
  for (const s of [...QUOTE_SOURCES, ...BOOK_SOURCES, ...BAR_SOURCES, "", "timescaledb"]) {
    assert.equal(classifySourceGrade(s, "quote"), classifySourceGrade(s));
    assert.equal(classifySourceGrade(s, "bar"), classifySourceGrade(s));
  }
});

// ─── 2. Buy/sell attribution is never observed ─────────────────────────────

test("flow-attribution features NEVER grade observed, whatever the source", () => {
  const everySource = [...QUOTE_SOURCES, ...BOOK_SOURCES, ...BAR_SOURCES, "timescaledb", "", "weird"];
  for (const s of everySource) {
    const grade = classifySourceGrade(s, "flow_attribution");
    assert.notEqual(grade, "observed", `flow grade for "${s}" must not be observed`);
    assert.notEqual(grade, "reconstructed", `flow grade for "${s}" must not be reconstructed`);
  }
  // The tick/book streams specifically land on the new grade.
  for (const s of [...QUOTE_SOURCES, ...BOOK_SOURCES, "timescaledb"]) {
    assert.equal(classifyFlowGrade(s), "modelled_from_quotes");
    assert.equal(isInferredSideGrade(classifyFlowGrade(s)), true);
  }
  // Bar-fabricated flow keeps its (worse) grade rather than being promoted.
  for (const s of BAR_SOURCES) assert.equal(classifyFlowGrade(s), "bar_inferred");
  assert.equal(classifyFlowGrade(""), "unavailable");
});

test("the modelled_from_quotes badge is not a green light", () => {
  assert.equal(sourceGradeLabel("modelled_from_quotes"), "INFERRED FROM QUOTES");
  assert.notEqual(sourceGradeVariant("modelled_from_quotes"), "success");
  assert.equal(sourceGradeVariant("observed"), "success");
});

// ─── 3. The capability we do NOT have surfaces as unavailable ──────────────

test("an aggressor trade-print feature is unavailable on every source", () => {
  assert.equal(AGGRESSOR_TAPE_AVAILABLE, false);
  for (const s of [...QUOTE_SOURCES, ...BOOK_SOURCES, ...BAR_SOURCES, "timescaledb", ""]) {
    assert.equal(classifySourceGrade(s, "trade_print"), "unavailable");
  }
  assert.equal(sourceGradeLabel("unavailable"), "SOURCE UNKNOWN");
});

test("every named flow-attribution feature is covered by the flow grade", () => {
  assert.ok(FLOW_ATTRIBUTION_FEATURES.length > 0);
  for (const feature of FLOW_ATTRIBUTION_FEATURES) {
    assert.match(describeFlowDerivation(feature, "market_ticks"), /inferred from quotes/);
    assert.match(describeFlowDerivation(feature, "market_ticks"), /no aggressor tape/);
    assert.match(describeFlowDerivation(feature, "bar_proxy"), /inferred from bars/);
  }
});

// ─── 4. Badge labels never assert a trade print ────────────────────────────

test("OfSourceBadge labels state the derivation and never say REAL", () => {
  for (const s of [...QUOTE_SOURCES, ...BOOK_SOURCES, ...BAR_SOURCES, "timescaledb", "", "weird_src"]) {
    const { kind, label, note, grade } = classifyOfSource(s);
    assert.doesNotMatch(label, /\bREAL\b/i, `"${s}" badge must not claim REAL`);
    assert.doesNotMatch(label, /trade print/i);
    assert.notEqual(kind as string, "real");
    assert.notEqual(grade, "observed");
    if (kind !== "unknown") assert.match(note, /inferred/i);
  }
  assert.equal(classifyOfSource("market_ticks").label, "TICK QUOTES · SIDES INFERRED");
  assert.equal(classifyOfSource("tick_reconstruction_book").label, "BOOK QUOTES · SIDES INFERRED");
  assert.equal(classifyOfSource("bar_proxy").label, "BAR PROXY · SIDES INFERRED");
  assert.equal(classifyOfSource("").label, "SOURCE UNKNOWN");
});

// ─── 5. Render assertion: no OF surface prints a trade-print claim ─────────

const SRC = path.join(import.meta.dirname, "..", "src");

/** Files that render an order-flow / microstructure surface. */
const OF_SURFACE_DIRS = [
  path.join(SRC, "components", "mpof"),
  path.join(SRC, "components", "orderflow"),
  path.join(SRC, "components", "strategies"),
  path.join(SRC, "components", "market-structure"),
  path.join(SRC, "components", "v1-orderflow"),
  path.join(SRC, "components", "v1-mp-intelligence"),
  path.join(SRC, "components", "v1-auction-intelligence"),
  path.join(SRC, "components", "v1-fractal-market-profile"),
];

function walk(dir: string): string[] {
  let out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out = out.concat(walk(full));
    else if (full.endsWith(".ts") || full.endsWith(".tsx")) out.push(full);
  }
  return out;
}

/**
 * Strings that would claim trade-print / observed provenance for flow. These
 * are matched against RENDERED text only — comments and docstrings explaining
 * the correction are stripped first.
 */
const FORBIDDEN: Array<[RegExp, string]> = [
  [/REAL TICKS/i, 'a badge saying "REAL TICKS"'],
  [/real trade tape/i, '"real trade tape"'],
  [/real tick cvd/i, '"real tick CVD"'],
  [/genuine tick tape/i, '"genuine tick tape"'],
  [/live microstructure/i, '"live microstructure" (implies an observed tape)'],
  [/real microstructure/i, '"real microstructure"'],
  // Added after the adversarial verification pass — each of these was a LIVE
  // surface the first sweep missed, so the pattern set is widened, not just
  // the files.
  [/\btick tape\b/i, '"tick tape" (asserts a trade tape)'],
  [/\bclean tape\b/i, '"clean tape"'],
  [/tape behind the/i, '"the tape behind …" (asserts a tape read)'],
  [/aggressive (buys|sells)\b/i, 'prose "aggressive buys/sells" without the inferred qualifier'],
  [/\bno trade prints\b/i, 'an empty state implying trade prints normally arrive'],
];

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

test("no order-flow surface renders a trade-print / observed-flow claim", () => {
  const offences: string[] = [];
  for (const dir of OF_SURFACE_DIRS) {
    for (const file of walk(dir)) {
      const body = stripComments(readFileSync(file, "utf8"));
      for (const [re, what] of FORBIDDEN) {
        if (re.test(body)) offences.push(`${path.relative(SRC, file)} renders ${what}`);
      }
    }
  }
  assert.deepEqual(offences, [], `order-flow surfaces overstate provenance:\n${offences.join("\n")}`);
});

/**
 * The grep above can only see LITERAL strings. `GateChips` renders raw backend
 * gate keys, so the loudest overstatement in the terminal — a green chip
 * reading "real tick cvd" on the Convergence desk — was invisible to it. That
 * chip is now display-overridden, and this locks the override in place.
 */
test("GateChips display-overrides the real_tick_cvd key", () => {
  const src = readFileSync(path.join(SRC, "components", "mpof", "GateChips.tsx"), "utf8");
  assert.match(src, /GATE_LABEL_OVERRIDE/, "GateChips must carry a display-override map");
  assert.match(src, /real_tick_cvd:\s*\{/, "the real_tick_cvd key must be overridden");
  assert.match(src, /cvd from quote ticks/, "the override must name the derivation");
  // The chip label and the blocked-reasons banner must BOTH go through it.
  const uses = src.match(/GATE_LABEL_OVERRIDE\[/g) ?? [];
  assert.ok(uses.length >= 2, "both the chip label and the blocked banner must use the override");
  // The raw key must still be visible in the tooltip — relabel, never launder.
  assert.match(src, /\$\{key\}: \$\{pass \? "PASS" : "BLOCK"\}/);
});

test("the OF badge component itself never emits an observed-grade style", () => {
  const badge = readFileSync(path.join(SRC, "components", "mpof", "OfSourceBadge.tsx"), "utf8");
  // The green (success) treatment was the visual half of the REAL claim.
  assert.ok(!/accent-green/.test(stripComments(badge)), "OF badge must not paint flow green");
});
