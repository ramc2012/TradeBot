/**
 * Honesty + wiring tests for the Structure and Flow views (2026-07-19).
 *
 * Same runner as `flow-provenance.test.ts` — `node --test`, native TypeScript
 * type-stripping, no framework — so everything asserted here is either a pure
 * module or a source-text assertion over the rendered files.
 *
 * What these lock down:
 *   1. The structurally-absent capabilities are declared as DATA, are permanent,
 *      and cite the repo file that establishes them.
 *   2. The Flow view renders those gaps as capability-absent cards and does NOT
 *      render the broker depth PROXY in a slot labelled "depth".
 *   3. A flow pane may not draw on a bar clock that differs from price — the
 *      shared crosshair would otherwise point at different bars per pane.
 *   4. Every pane shares ONE time transform, and one fitKey, so the viewport
 *      survives a refresh and re-fits only on a real context change.
 *   5. The Profile Workbench declares naked POC / LVN unavailable instead of
 *      deriving them client-side.
 *   6. `ViewNav.BUILT` agrees with which view components actually exist.
 *   7. `CandleChart`'s new linking props are OPT-IN, so its three other call
 *      sites cannot inherit behaviour they never asked for.
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  MISSING_CAPABILITIES,
  MISSING_CAPABILITY_KEYS,
  isCapabilityMissing,
  missingCapability,
} from "../src/lib/flow-provenance.ts";
import {
  DESK_TZ_OFFSET_MINUTES,
  alignFlowToPrice,
  toChartTime,
  toUnixSeconds,
} from "../src/components/market-structure/structure/chart-time.ts";

const SRC = path.join(import.meta.dirname, "..", "src");
const MS = path.join(SRC, "components", "market-structure");
const read = (...p: string[]) => readFileSync(path.join(...p), "utf8");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

// ─── 1. Missing capabilities are declared data, not prose in a component ────

test("both structurally-absent capabilities are declared, permanent and cited", () => {
  assert.deepEqual(MISSING_CAPABILITY_KEYS.sort(), ["BROKER_AGGRESSOR_PRINTS", "DEPTH_L2"]);
  for (const key of MISSING_CAPABILITY_KEYS) {
    const cap = missingCapability(key);
    assert.equal(cap.key, key);
    assert.equal(cap.permanent, true, `${key} must be a permanent capability gap, not an outage`);
    assert.ok(cap.reason.length > 40, `${key} must state WHY in prose`);
    // The citation must name a real repo path so the claim is checkable.
    assert.match(cap.citation, /backend\/|flow-provenance/, `${key} must cite its source`);
    assert.ok(cap.insteadUse.length > 10, `${key} must say what IS available instead`);
  }
  assert.equal(isCapabilityMissing("DEPTH_L2"), true);
  assert.equal(isCapabilityMissing("SOMETHING_WE_HAVE"), false);
});

test("the depth gap names the proxy rather than implying an outage", () => {
  const depth = MISSING_CAPABILITIES.DEPTH_L2;
  assert.match(depth.reason, /proxy/i);
  assert.doesNotMatch(depth.reason, /temporar|currently unavailable|try again/i);
});

// ─── 2. The Flow view renders the gaps, and renders no proxy ladder ────────

test("FlowView renders a capability-absent card for BOTH gaps", () => {
  const view = read(MS, "flow", "FlowView.tsx");
  assert.match(view, /CapabilityAbsentCard/);
  assert.match(view, /capability="DEPTH_L2"/);
  assert.match(view, /capability="BROKER_AGGRESSOR_PRINTS"/);
});

test("FlowView does not render the broker depth proxy in a depth slot", () => {
  const view = stripComments(read(MS, "flow", "FlowView.tsx"));
  // The proxy ladder and the heatmap are the two surfaces that would function
  // as an observed book if placed in a "Depth" panel.
  assert.doesNotMatch(view, /DepthLadder/, "the proxy ladder must not appear in the Flow view");
  assert.doesNotMatch(view, /heatmap/i, "the depth-proxy heatmap must not appear in the Flow view");
});

test("the capability card carries no timestamp or refresh affordance", () => {
  const card = stripComments(read(MS, "flow", "CapabilityAbsentCard.tsx"));
  assert.doesNotMatch(card, /LastUpdated|FreshnessBadge|formatIST|refetch|Refresh/);
  // It must read the shared table, never restate the reason itself.
  assert.match(card, /missingCapability/);
});

test("every Flow panel goes through the shared provenance wrapper", () => {
  const view = stripComments(read(MS, "flow", "FlowView.tsx"));
  assert.match(view, /ProvenancePanel/);
  const wrapper = stripComments(read(MS, "flow", "ProvenancePanel.tsx"));
  // Derivation is composed once by the contract, not hand-rolled per panel.
  assert.match(wrapper, /provenanceOf\(/);
  assert.match(wrapper, /feature = "flow_attribution"/, "flow attribution must be the DEFAULT grade");
});

// ─── 3. A flow pane may not draw on a foreign bar clock ────────────────────

test("aligned series are accepted; mismatched clocks are refused with a reason", () => {
  const price = [100, 280, 460, 640, 820];
  assert.equal(alignFlowToPrice(price, price).aligned, true);
  assert.equal(alignFlowToPrice(price, price).overlap, price.length);

  // A 30-minute flow series against 3-minute price bars: no shared timestamps.
  const foreign = [7, 1807, 3607];
  const verdict = alignFlowToPrice(price, foreign);
  assert.equal(verdict.aligned, false);
  assert.match(verdict.reason ?? "", /do not align/i);
  assert.match(verdict.reason ?? "", /crosshair/i, "the reason must say what breaks");

  // Empty on either side is a refusal, never a silent empty chart.
  assert.equal(alignFlowToPrice([], price).aligned, false);
  assert.equal(alignFlowToPrice(price, []).aligned, false);
});

test("a partial overlap below the threshold is still refused", () => {
  const price = [0, 100, 200, 300, 400, 500, 600, 700, 800, 900];
  const half = [0, 100, 111, 222, 333, 444, 555, 666, 777, 888];
  assert.equal(alignFlowToPrice(price, half).aligned, false);
});

test("the flow pane refuses to draw rather than resampling", () => {
  const pane = stripComments(read(MS, "structure", "panes", "FlowPane.tsx"));
  assert.match(pane, /alignFlowToPrice/);
  assert.match(pane, /alignment\.aligned/);
  // No interpolation / nearest-bar snapping anywhere in the pane.
  assert.doesNotMatch(pane, /interpolat|nearestBar|resample/i);
});

// ─── 4. One time transform, one fitKey ─────────────────────────────────────

test("the chart time transform is the desk offset applied to epoch seconds", () => {
  assert.equal(DESK_TZ_OFFSET_MINUTES, 330);
  const iso = "2026-07-17T09:15:00Z";
  assert.equal(toChartTime(iso) - toUnixSeconds(iso), 330 * 60);
  // Millisecond and second epochs both normalise.
  assert.equal(toUnixSeconds(1_700_000_000_000), 1_700_000_000);
  assert.equal(toUnixSeconds(1_700_000_000), 1_700_000_000);
});

test("both panes are driven by ONE fitKey, and nothing else calls fitContent", () => {
  const view = read(MS, "structure", "StructureView.tsx");
  // A single fitKey expression, handed to both panes.
  assert.match(view, /const fitKey = /);
  assert.equal((view.match(/fitKey=\{fitKey\}/g) ?? []).length, 2, "price and flow must share it");
  // Neither the provider nor the fan-out may fit — that would yank the
  // viewport on a poll. Only the panes fit, and only on a fitKey change.
  const provider = stripComments(read(MS, "structure", "LinkedChartProvider.tsx"));
  assert.doesNotMatch(provider, /fitContent/);
  assert.doesNotMatch(stripComments(read(MS, "structure", "pane-registry.ts")), /fitContent/);
  // The flow pane's fit is gated, not unconditional.
  const flow = stripComments(read(MS, "structure", "panes", "FlowPane.tsx"));
  assert.match(flow, /fitRef\.current !== fitKey/);
});

// ─── 4b. The fan-out itself, driven by fake charts ─────────────────────────
//
// A shared crosshair and a shared zoom are the two features on this canvas that
// LOOK like they work while being wrong, so they are exercised, not grepped.

type Call = [string, unknown[]];

function fakeChart(values: Record<number, number>, calls: Call[], name: string) {
  const subs: Record<string, ((arg: unknown) => void)[]> = {
    range: [],
    crosshair: [],
    click: [],
  };
  let visibleRange: unknown = null;
  const chart = {
    timeScale: () => ({
      subscribeVisibleLogicalRangeChange: (f: (a: unknown) => void) => subs.range.push(f),
      unsubscribeVisibleLogicalRangeChange: (f: (a: unknown) => void) => {
        subs.range = subs.range.filter((x) => x !== f);
      },
      setVisibleLogicalRange: (r: unknown) => {
        visibleRange = r;
        calls.push([`${name}.setVisibleLogicalRange`, [r]]);
        // A real chart fires its own range callback when set externally. This
        // is the echo that must NOT ping-pong back.
        subs.range.forEach((f) => f(r));
      },
      getVisibleLogicalRange: () => visibleRange,
    }),
    subscribeCrosshairMove: (f: (a: unknown) => void) => subs.crosshair.push(f),
    unsubscribeCrosshairMove: (f: (a: unknown) => void) => {
      subs.crosshair = subs.crosshair.filter((x) => x !== f);
    },
    subscribeClick: (f: (a: unknown) => void) => subs.click.push(f),
    unsubscribeClick: (f: (a: unknown) => void) => {
      subs.click = subs.click.filter((x) => x !== f);
    },
    setCrosshairPosition: (v: number, t: number) =>
      calls.push([`${name}.setCrosshairPosition`, [v, t]]),
    clearCrosshairPosition: () => calls.push([`${name}.clearCrosshairPosition`, []]),
  };
  return {
    chart,
    series: { id: name },
    priceAt: (t: number) => (t in values ? values[t] : null),
    emitRange: (r: unknown) => subs.range.forEach((f) => f(r)),
    emitCrosshair: (p: unknown) => subs.crosshair.forEach((f) => f(p)),
    emitClick: (p: unknown) => subs.click.forEach((f) => f(p)),
    subs,
  };
}

test("panning one pane moves its peer, and the peer's echo does not bounce back", async () => {
  const { createPaneRegistry } = await import(
    "../src/components/market-structure/structure/pane-registry.ts"
  );
  const calls: Call[] = [];
  const price = fakeChart({ 100: 24_000 }, calls, "price");
  const flow = fakeChart({ 100: 1.5 }, calls, "flow");
  const reg = createPaneRegistry({ onSelect: () => {} });
  reg.register("price", price);
  reg.register("flow", flow);

  price.emitRange({ from: 3, to: 9 });

  const applied = calls.filter((c) => c[0].endsWith("setVisibleLogicalRange"));
  assert.deepEqual(applied, [["flow.setVisibleLogicalRange", [{ from: 3, to: 9 }]]]);
});

test("hovering one pane places the peer's crosshair at the PEER's own value", async () => {
  const { createPaneRegistry } = await import(
    "../src/components/market-structure/structure/pane-registry.ts"
  );
  const calls: Call[] = [];
  const price = fakeChart({ 100: 24_000 }, calls, "price");
  const flow = fakeChart({ 100: 1.5 }, calls, "flow");
  const reg = createPaneRegistry({ onSelect: () => {} });
  reg.register("price", price);
  reg.register("flow", flow);

  price.emitCrosshair({ time: 100 });
  // The peer is placed at 1.5 — ITS value at that time — not at the price.
  assert.deepEqual(calls, [["flow.setCrosshairPosition", [1.5, 100]]]);
});

test("a peer with no value at the hovered time CLEARS rather than inventing one", async () => {
  const { createPaneRegistry } = await import(
    "../src/components/market-structure/structure/pane-registry.ts"
  );
  const calls: Call[] = [];
  const price = fakeChart({ 100: 24_000, 200: 24_010 }, calls, "price");
  const flow = fakeChart({ 100: 1.5 }, calls, "flow"); // nothing at 200
  const reg = createPaneRegistry({ onSelect: () => {} });
  reg.register("price", price);
  reg.register("flow", flow);

  price.emitCrosshair({ time: 200 });
  assert.deepEqual(calls, [["flow.clearCrosshairPosition", []]]);
  // Leaving the chart clears too — no last-known crosshair lingering.
  calls.length = 0;
  price.emitCrosshair({ time: undefined });
  assert.deepEqual(calls, [["flow.clearCrosshairPosition", []]]);
});

test("a pane that mounts second adopts the peer's viewport instead of its own", async () => {
  const { createPaneRegistry } = await import(
    "../src/components/market-structure/structure/pane-registry.ts"
  );
  const calls: Call[] = [];
  const price = fakeChart({}, calls, "price");
  const flow = fakeChart({}, calls, "flow");
  const reg = createPaneRegistry({ onSelect: () => {} });
  reg.register("price", price);
  price.chart.timeScale().setVisibleLogicalRange({ from: 10, to: 20 });
  calls.length = 0;

  reg.register("flow", flow);
  assert.deepEqual(calls, [["flow.setVisibleLogicalRange", [{ from: 10, to: 20 }]]]);
});

test("a click pins the bar time, and unregistering detaches every subscription", async () => {
  const { createPaneRegistry } = await import(
    "../src/components/market-structure/structure/pane-registry.ts"
  );
  const calls: Call[] = [];
  const picked: (number | null)[] = [];
  const price = fakeChart({ 100: 1 }, calls, "price");
  const flow = fakeChart({ 100: 2 }, calls, "flow");
  const reg = createPaneRegistry({ onSelect: (t) => picked.push(t) });
  reg.register("price", price);
  reg.register("flow", flow);

  flow.emitClick({ time: 100 });
  price.emitClick({ time: undefined });
  assert.deepEqual(picked, [100, null]);

  reg.unregister("flow");
  assert.equal(reg.size(), 1);
  assert.equal(flow.subs.range.length, 0);
  assert.equal(flow.subs.crosshair.length, 0);
  assert.equal(flow.subs.click.length, 0);
  // With the peer gone, a hover fans out to nobody.
  calls.length = 0;
  price.emitCrosshair({ time: 100 });
  assert.deepEqual(calls, []);
});

// ─── 5. The workbench declares what no lane emits ──────────────────────────

test("the Profile Workbench declares naked POC and LVN unavailable", () => {
  const wb = read(MS, "structure", "ProfileWorkbench.tsx");
  assert.match(wb, /Naked POC/);
  assert.match(wb, /LVN/);
  assert.match(wb, /UNAVAILABLE_OVERLAYS/, "the gaps must be declared as data");
  const body = stripComments(wb);
  // It must not compute either one client-side.
  assert.doesNotMatch(body, /nakedPocFrom|deriveNakedPoc|computeLvn|findLvn/);
});

test("the workbench reuses the shipped renderers rather than forking them", () => {
  const wb = read(MS, "structure", "ProfileWorkbench.tsx");
  assert.match(wb, /from "@\/components\/mpof"/);
  assert.match(wb, /normalizeTpo/, "it must reuse the histogram's payload adapter");
});

test("the nine existing profile call sites are untouched by this pass", () => {
  // ProfileLadder's new props must be OPTIONAL with the shipped defaults, so a
  // desk that passes none renders exactly as before.
  const ladder = read(SRC, "components", "mpof", "ProfileLadder.tsx");
  assert.match(ladder, /defaultShowTpo\?: boolean/);
  assert.match(ladder, /defaultShowTpo = true/);
  assert.match(ladder, /defaultShowVol = false/);
  assert.match(ladder, /poorHigh\?: number \| null/);
});

// ─── 6. The nav's BUILT map matches reality ────────────────────────────────

test("ViewNav.BUILT agrees with which view components exist", () => {
  const nav = read(MS, "ViewNav.tsx");
  const built: Record<string, boolean> = {};
  for (const [, key, value] of nav.matchAll(/^\s{2}(\w+): (true|false),$/gm)) {
    built[key] = value === "true";
  }
  assert.equal(built.command, true);
  assert.equal(built.structure, true);
  assert.equal(built.flow, true);
  assert.equal(built.strategies, true);
  // Not built in this pass — and they must still say so.
  assert.equal(built.risk, false);
  assert.equal(built.research, false);

  assert.ok(existsSync(path.join(MS, "structure", "StructureView.tsx")));
  assert.ok(existsSync(path.join(MS, "flow", "FlowView.tsx")));
  assert.ok(existsSync(path.join(MS, "strategies", "StrategiesView.tsx")));

  // The workspace must actually route to them, or the flag would overstate.
  const workspace = read(MS, "MarketStructureWorkspace.tsx");
  assert.match(workspace, /<StructureView/);
  assert.match(workspace, /<FlowView/);
  assert.match(workspace, /<StrategiesView/);
});

// ─── 7. CandleChart linking is opt-in ──────────────────────────────────────

test("CandleChart's linking props are optional and default-undefined", () => {
  const chart = read(SRC, "components", "strategies", "shared", "CandleChart.tsx");
  assert.match(chart, /onChartReady\?:/);
  assert.match(chart, /onChartDispose\?:/);
  // Optional-call syntax only — never an unconditional invocation.
  assert.match(chart, /readyRef\.current\?\.\(/);
  assert.doesNotMatch(stripComments(chart), /onChartReady\(chart/);
});

test("the structure panes gate every query on the view being open", () => {
  const canvas = stripComments(read(MS, "structure", "useMarketCanvas.ts"));
  // Three queries, three `enabled` gates, every one of them requiring BOTH the
  // caller's view gate and a pinned symbol.
  assert.equal((canvas.match(/useQuery\(\{/g) ?? []).length, 3);
  assert.match(canvas, /enabled: enabled && !!ofSymbol/);
  assert.match(canvas, /enabled: enabled && !!symbol/);
  assert.match(canvas, /const needsOhlc = enabled && !!symbol && !ofSymbol/);
  assert.match(canvas, /enabled: needsOhlc/);
  // ...and the convergence key must be the drawer's, so the two dedupe.
  assert.match(canvas, /\["ms-detail", "convergence", market, symbol\]/);
});
