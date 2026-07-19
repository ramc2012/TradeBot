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
 *   5. A STORAGE mode ("snapshot") never forces a DERIVATION grade — the
 *      mirror-image over-correction of the trade-print error.
 *   6. The live verdict is derived from the pinned row, so it can be genuinely
 *      LIVE, and is absent only when the observation genuinely is.
 *   7. ARMED never reaches for the green/success treatment.
 *   8. A user-entered as-of can never fabricate a replay label.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  AGGRESSOR_TAPE_AVAILABLE,
  FLOW_ATTRIBUTION_FEATURES,
  classifyAcquisitionSource,
  classifyFlowGrade,
  classifyOfSource,
  classifySourceGrade,
  classifyStorageMode,
  describeFlowDerivation,
  isInferredSideGrade,
  sourceGradeLabel,
  sourceGradeVariant,
} from "../src/lib/flow-provenance.ts";
import {
  liveVerdict,
  liveVerdictInputFor,
  pinnedObservationOf,
} from "../src/lib/live-verdict-input.ts";
import {
  isActionableVariant,
  schedulerStateVariant,
  setupStageVariant,
} from "../src/lib/status-variants.ts";

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

// ─── 6. Storage mode is NOT a derivation grade ─────────────────────────────
//
// The first correction over-shot: `snapshot` was put in the bar-inferred set,
// so the saved ATM watchlist rendered "snapshot · bar inferred · inferred from
// bars, not a tape". Nothing in that payload establishes it. Claiming a WORSE
// grade than the evidence supports is the same defect as claiming a better one.

const SNAPSHOT_SOURCES = ["snapshot", "cached_snapshot", "persisted_snapshot"];
/** Genuinely fabricated-from-bars — these must NOT be relaxed by the fix. */
const TRUE_BAR_SOURCES = [
  "bar_inference",
  "historical_bar_inference",
  "spot_index_proxy",
  "bar_proxy",
  "bar_fallback",
  "insufficient_ticks",
  "bar_proxy_timeout",
];

test("a stored snapshot is NOT graded bar_inferred", () => {
  for (const s of SNAPSHOT_SOURCES) {
    assert.notEqual(classifySourceGrade(s), "bar_inferred", `"${s}" must not be graded bar_inferred`);
    assert.equal(classifySourceGrade(s), "unknown_derivation");
    // ...and it is not promoted to observed either. We do not know.
    assert.notEqual(classifySourceGrade(s), "observed");
    assert.equal(sourceGradeLabel(classifySourceGrade(s)), "DERIVATION UNKNOWN");
    assert.notEqual(sourceGradeVariant(classifySourceGrade(s)), "success");
  }
});

test("snapshot is a STORAGE mode, carried on its own axis", () => {
  for (const s of SNAPSHOT_SOURCES) assert.equal(classifyStorageMode(s), "snapshot");
  // The live quote path is the live read; a history store is a backfilled read.
  assert.equal(classifyStorageMode("market_ticks"), "live");
  assert.equal(classifyStorageMode("timescaledb"), "backfilled");
  // An unrecognised source is never PROMOTED to a live read.
  assert.equal(classifyStorageMode("something_new"), "unknown");
  assert.equal(classifyStorageMode(""), "unknown");
  // The acquisition axis stays honest too: a storage string says nothing about
  // what produced the numbers.
  assert.equal(classifyAcquisitionSource("snapshot"), "unknown");
  assert.equal(classifyAcquisitionSource("market_ticks"), "quote_stream");
  assert.equal(classifyAcquisitionSource("bar_inference"), "bar");
  assert.equal(classifyAcquisitionSource("tick_reconstruction_book"), "book");
  assert.equal(classifyAcquisitionSource("black_scholes"), "model");
});

test("genuinely bar-inferred sources still grade bar_inferred", () => {
  for (const s of TRUE_BAR_SOURCES) {
    assert.equal(classifySourceGrade(s), "bar_inferred", `"${s}" must stay bar_inferred`);
    assert.equal(classifyFlowGrade(s), "bar_inferred");
  }
});

test("the snapshot OF badge names the storage mode, never a bar proxy", () => {
  const { kind, label, note, grade } = classifyOfSource("snapshot");
  assert.equal(grade, "unknown_derivation");
  assert.doesNotMatch(label, /bar/i, "a snapshot read must not be labelled a bar proxy");
  assert.match(label, /SNAPSHOT READ/);
  assert.equal(kind, "unknown");
  assert.match(note, /storage mode/i);
});

// ─── 7. The live verdict is DERIVED from the pinned row ────────────────────
//
// ContextBar.tsx hard-coded `freshness: "absent"` + `hasSymbolObservation:
// false`, so the header could only ever say "no observation" — even on a
// Monday with an open session, a connected feed and fresh rows.

const OPEN_AND_ONLINE = { sessionOpen: true, feedOnline: true };

test("a fresh pinned row yields a genuine LIVE verdict", () => {
  const observation = pinnedObservationOf({
    freshness: "fresh",
    asOf: "2026-07-17T09:30:00",
    provenance: { dataMode: "live" },
  });
  assert.equal(observation.present, true);
  assert.equal(observation.freshness, "fresh");
  const verdict = liveVerdict(liveVerdictInputFor({ ...OPEN_AND_ONLINE, observation }));
  assert.equal(verdict.live, true, "a fresh observed row on an open session IS live");
  assert.equal(verdict.variant, "success");
});

test("an absent observation never yields a live verdict", () => {
  // No row pinned at all.
  const none = pinnedObservationOf(null);
  assert.equal(none.present, false);
  assert.equal(liveVerdict(liveVerdictInputFor({ ...OPEN_AND_ONLINE, observation: none })).live, false);

  // A row that exists but carries no timestamp is NOT an observation.
  const noTimestamp = pinnedObservationOf({ freshness: "fresh", asOf: null });
  assert.equal(noTimestamp.present, false);
  assert.equal(
    liveVerdict(liveVerdictInputFor({ ...OPEN_AND_ONLINE, observation: noTimestamp })).label,
    "no observation",
  );

  // Freshness "absent" collapses regardless of the timestamp string.
  const absent = pinnedObservationOf({ freshness: "absent", asOf: "2026-07-17T09:30:00" });
  assert.equal(absent.present, false);
});

test("the verdict stays pessimistic ahead of the observation", () => {
  const fresh = pinnedObservationOf({
    freshness: "fresh",
    asOf: "2026-07-17T09:30:00",
    provenance: { dataMode: "live" },
  });
  // Session closed and feed offline both outrank a perfectly good row.
  assert.equal(liveVerdict(liveVerdictInputFor({ sessionOpen: false, feedOnline: true, observation: fresh })).live, false);
  assert.equal(liveVerdict(liveVerdictInputFor({ sessionOpen: true, feedOnline: false, observation: fresh })).live, false);
  // A replayed row is not live even with everything else green.
  const replayed = pinnedObservationOf({
    freshness: "fresh",
    asOf: "2026-07-17T09:30:00",
    provenance: { dataMode: "historical_replay" },
  });
  assert.equal(liveVerdict(liveVerdictInputFor({ ...OPEN_AND_ONLINE, observation: replayed })).label, "replay");
  // A stale row is not live either.
  const stale = pinnedObservationOf({
    freshness: "stale",
    asOf: "2026-07-17T09:30:00",
    provenance: { dataMode: "live" },
  });
  assert.equal(liveVerdict(liveVerdictInputFor({ ...OPEN_AND_ONLINE, observation: stale })).label, "stale");
});

// ─── 8. ARMED IS NOT GREEN ─────────────────────────────────────────────────

test("armed never maps to the success (green) variant", () => {
  assert.notEqual(schedulerStateVariant("armed"), "success");
  assert.equal(schedulerStateVariant("armed"), "info");
  assert.equal(isActionableVariant(schedulerStateVariant("armed")), false);
  // A loop that is genuinely turning still earns green.
  assert.equal(schedulerStateVariant("running"), "success");
  // Setup stages obey the same rule.
  assert.equal(setupStageVariant("ARMED"), "info");
  assert.equal(setupStageVariant("armed"), "info", "the mapping is case-insensitive");
  assert.equal(setupStageVariant("WATCHING"), "info");
  assert.equal(setupStageVariant("TRIGGERED"), "success");
  // Anything unrecognised is neutral — never promoted to green.
  assert.equal(setupStageVariant("SOMETHING_NEW"), "neutral");
  assert.equal(setupStageVariant(null), "neutral");
});

test("no market-structure surface paints an ARMED state green", () => {
  const glyphs = stripComments(
    readFileSync(path.join(SRC, "components", "market-structure", "glyphs.tsx"), "utf8"),
  );
  assert.doesNotMatch(glyphs, /ARMED:\s*"text-accent-green"/);
  assert.match(glyphs, /setupStageVariant/, "stage colour must come from the shared contract");

  // The desk shell's armed pill and the truth strip must go through it too.
  const shell = stripComments(readFileSync(path.join(SRC, "components", "desk-ui", "DeskShell.tsx"), "utf8"));
  assert.doesNotMatch(shell, /label="armed"\s+variant="success"/);
  const system = stripComments(readFileSync(path.join(SRC, "hooks", "useSystemState.ts"), "utf8"));
  assert.doesNotMatch(system, /autoRunArmed \? "success"/);
  assert.match(system, /schedulerStateVariant\("armed"\)/);
});

// ─── 9. The as-of control may not fabricate a replay ────────────────────────

test("the workspace context never derives replay from a user-entered as-of", () => {
  const schema = stripComments(
    readFileSync(path.join(SRC, "components", "market-structure", "context", "schema.ts"), "utf8"),
  );
  // The exact shipped derivation that painted REPLAY over live-latest data.
  assert.doesNotMatch(schema, /replay\s*=\s*replayParam \|\| asOf !== "now"/);
  assert.match(schema, /APPLIED_TO_DATA/, "the wiring gap must be declared as data");
  assert.match(schema, /asOf: false/, "as-of must be marked NOT applied");

  const hook = stripComments(
    readFileSync(path.join(SRC, "components", "market-structure", "context", "useWorkspaceContext.ts"), "utf8"),
  );
  assert.doesNotMatch(hook, /next\.replay\s*=\s*true/);

  // And the row decorator must not accept a replay pin at all: the only replay
  // claim available is "the session is closed".
  const matrix = stripComments(
    readFileSync(path.join(SRC, "components", "market-structure", "command", "useUniverseMatrix.ts"), "utf8"),
  );
  assert.doesNotMatch(matrix, /opts\.replay/);
  assert.match(matrix, /opts\.sessionOpen === false/);
});

// ─── 10. A market switch may not show the other market's universe ───────────

/**
 * The loading-state fix originally reached for `placeholderData:
 * keepPreviousData` on the convergence query. That query is the only
 * market-keyed one — and for MCX it IS the universe — so carrying the previous
 * key's payload across a market switch composed NSE indices into the MCX matrix
 * (and MCX roots into the NSE one) for the length of one round trip. Smoothing
 * a blank grid is not worth a false claim about what an instrument is; the
 * distinct loading state covers that window instead.
 */
test("the market-keyed universe query never carries the other market's payload", () => {
  const matrix = stripComments(
    readFileSync(path.join(SRC, "components", "market-structure", "command", "useUniverseMatrix.ts"), "utf8"),
  );
  assert.doesNotMatch(
    matrix,
    /keepPreviousData/,
    "a market switch must fall to the loading state, never to the other market's rows",
  );
  // The loading state is what covers that window, and it must still be gated on
  // an empty matrix so a same-market refetch keeps the last good rows.
  assert.match(matrix, /rows\.length === 0/);
});
