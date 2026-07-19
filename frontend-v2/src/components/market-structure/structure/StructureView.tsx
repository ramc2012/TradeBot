"use client";

/**
 * StructureView — the linked market canvas.
 *
 * ONE synchronized timeline: candles + structural levels + the plan on top,
 * cumulative delta and per-bar signed volume BELOW on the same x-axis, with a
 * shared crosshair, shared zoom/pan, and a click that pins one bar for the
 * footprint inspector. The sync mechanism itself is `LinkedChartProvider`; this
 * file only composes panes into it.
 *
 * WHAT IT READS. `useMarketCanvas`, which resolves a source tier and says which
 * one it used. Nothing here re-derives freshness: the `row` handed down is the
 * SAME decorated object the header and the matrix read, so the three surfaces
 * cannot drift (that drift is exactly what the one-decoration-pass rule in
 * `MarketStructureWorkspace` exists to prevent).
 *
 * WHAT IT REFUSES TO DRAW.
 *   · a VWAP curve — `metrics.vwap` is one scalar, so it is drawn as a level
 *     labelled "(last)".
 *   · a flow pane on a different bar clock from price — it states the mismatch.
 *   · naked POC / LVN — no lane emits them (see `ProfileWorkbench`).
 *   · order / fill / exit markers — those live in the positions payloads, which
 *     this tier deliberately does not poll. The absence is stated, not filled.
 */
import { Activity, BarChart3, CandlestickChart, Layers3 } from "lucide-react";
import { useMemo, useState } from "react";

import { ProvenanceChip, Section, StatusBadge, formatISTTime } from "@/components/desk-ui";
import { provenanceOf } from "@/lib/market-semantics";

import type { MatrixRow } from "../command/useUniverseMatrix";
import type { WorkspaceContext } from "../context/schema";

import { LinkedChartProvider } from "./LinkedChartProvider";
import { FlowPane } from "./panes/FlowPane";
import { PricePane, type OverlayToggles } from "./panes/PricePane";
import { ProfileWorkbench, ProfileLevelReadout } from "./ProfileWorkbench";
import { SelectedBarInspector } from "./SelectedBarInspector";
import { useMarketCanvas } from "./useMarketCanvas";

const TIER_LABEL: Record<string, string> = {
  orderflow: "order-flow snapshot · 3m",
  convergence: "convergence detail · 3m",
  ohlc: "charts OHLC · 30m",
  none: "no source",
};

export function StructureView({ ctx, row }: { ctx: WorkspaceContext; row: MatrixRow | null }) {
  const canvas = useMarketCanvas(ctx.symbol, ctx.market, true);
  const [overlays, setOverlays] = useState<OverlayToggles>({
    valueArea: true,
    initialBalance: true,
    prior: true,
    vwap: true,
    plan: true,
  });

  // ONE fitKey for every pane in the group: the viewport survives a poll and
  // re-fits together on a genuine instrument / tier change.
  const fitKey = `${ctx.market}:${ctx.symbol}:${canvas.barTimeframe ?? "-"}`;

  const provenance = useMemo(
    () =>
      provenanceOf({
        source: canvas.barSource,
        feature: "bar",
        asOf: canvas.asOf,
        timeframe: canvas.barTimeframe,
        have: canvas.bars.length,
        completenessLabel: `${canvas.bars.length} bars`,
      }),
    [canvas.barSource, canvas.asOf, canvas.barTimeframe, canvas.bars.length],
  );

  const digits = ctx.market === "MCX" ? 2 : 1;

  return (
    <div className="space-y-4">
      <TierStrip canvas={canvas} ctx={ctx} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(300px,0.95fr)]">
        <div className="min-w-0 space-y-4">
          <Section
            title="Linked market canvas"
            icon={<CandlestickChart size={16} />}
            description="Price, structural levels and flow share ONE x-axis: hover either pane for a shared crosshair, pan or zoom either to move both, click any bar to inspect it."
            rightSlot={
              <div className="flex flex-wrap items-center gap-1.5">
                <StatusBadge label={TIER_LABEL[canvas.tier] ?? canvas.tier} variant="neutral" />
                {canvas.isFetching ? <StatusBadge label="refreshing" variant="info" /> : null}
              </div>
            }
            provenance={provenance}
          >
            <OverlayBar
              overlays={overlays}
              setOverlays={setOverlays}
              hasPrior={
                canvas.levels.prior.vah != null ||
                canvas.levels.prior.val != null ||
                canvas.levels.prior.poc != null
              }
              hasVwap={canvas.levels.vwapLast != null}
              hasPlan={!!canvas.plan}
            />

            {canvas.isLoading ? (
              <div className="flex h-[380px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
                Loading the canvas for {ctx.symbol}…
              </div>
            ) : canvas.barsUnavailable ? (
              <div className="flex h-[380px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 px-6 text-center text-[12px] leading-5 text-text-muted">
                {canvas.barsUnavailable}
              </div>
            ) : (
              <LinkedChartProvider>
                <div className="space-y-1">
                  <PricePane
                    bars={canvas.bars}
                    levels={canvas.levels}
                    plan={canvas.plan}
                    overlays={overlays}
                    fitKey={fitKey}
                    height={380}
                  />
                  <div className="flex items-center gap-2 pt-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">
                    <Activity size={11} />
                    cumulative delta · sides inferred from quotes
                  </div>
                  <FlowPane
                    points={canvas.flow.points}
                    priceBars={canvas.bars}
                    fitKey={fitKey}
                    height={180}
                  />
                </div>

                <div className="mt-3 rounded-xl border border-bg-border/70 bg-bg-primary/10 p-3">
                  <div className="mb-1.5 text-[9.5px] uppercase tracking-[0.14em] text-text-muted">
                    Selected bar · price-by-price
                  </div>
                  <SelectedBarInspector bars={canvas.footprint.bars} digits={digits} />
                </div>
              </LinkedChartProvider>
            )}

            <MarkerAvailabilityNote plan={canvas.plan} />
          </Section>
        </div>

        <div className="min-w-0 space-y-4">
          <Section
            title="Profile workbench"
            icon={<BarChart3 size={16} />}
            description="One configurable profile — TPO, volume or both; ladder or histogram; prior-session overlay when the lane emits one."
          >
            <ProfileWorkbench
              profile={canvas.profile}
              spot={canvas.spot}
              digits={digits}
              height={340}
              migration={
                row?.mp.available
                  ? { state: row.mp.migrationState, direction: row.mp.migrationDirection }
                  : null
              }
              unavailableReason={canvas.profileUnavailable}
              title={`${ctx.symbol} · profile`}
              sourceNote={canvas.profileSource}
            />
          </Section>

          <Section title="Levels" icon={<Layers3 size={16} />}>
            <ProfileLevelReadout
              levels={{
                POC: canvas.levels.poc,
                VAH: canvas.levels.vah,
                VAL: canvas.levels.val,
                IBH: canvas.levels.ibHigh,
                IBL: canvas.levels.ibLow,
                "VWAP (last)": canvas.levels.vwapLast,
                "prior VAH": canvas.levels.prior.vah,
                "prior POC": canvas.levels.prior.poc,
                "prior VAL": canvas.levels.prior.val,
              }}
              digits={digits}
            />
            <p className="mt-2 font-mono text-[10px] leading-4 text-text-muted">
              current-session levels drawn on the price pane come from{" "}
              {canvas.levelsSource ?? "no wired payload"}
              {canvas.profileSource && canvas.profileSource !== canvas.levelsSource
                ? ` · prior-session levels and the profile workbench come from ${canvas.profileSource}`
                : ""}
            </p>
            <p className="mt-1 text-[10.5px] leading-4 text-text-muted">
              {canvas.profileSource && canvas.profileSource !== canvas.levelsSource
                ? "Two payloads compute a POC/VAH/VAL over different inputs, so the same level name can carry two different numbers on this screen. They are attributed rather than reconciled — the numbers here are the ones drawn on the chart. "
                : ""}
              UNAVAILABLE means no wired payload carried that level for this
              instrument — it is not a zero and not a pending value.
            </p>
          </Section>
        </div>
      </div>
    </div>
  );
}

function TierStrip({
  canvas,
  ctx,
}: {
  canvas: ReturnType<typeof useMarketCanvas>;
  ctx: WorkspaceContext;
}) {
  return (
    <div className="rounded-xl border border-bg-border/70 bg-bg-secondary/20 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold text-text-primary">
          {ctx.market} · {ctx.symbol}
        </span>
        <StatusBadge label={TIER_LABEL[canvas.tier] ?? canvas.tier} variant="neutral" />
        {canvas.asOf ? (
          <span className="font-mono text-[10.5px] text-text-muted">
            last bar {formatISTTime(canvas.asOf)}
          </span>
        ) : (
          <span className="text-[10.5px] text-text-muted">no bar timestamp reported</span>
        )}
        {canvas.syntheticQuote ? (
          <StatusBadge label="synthetic quote path" variant="warn" />
        ) : null}
      </div>
      {canvas.tierNote ? (
        <p className="mt-1 max-w-prose text-[10.5px] leading-4 text-text-muted">{canvas.tierNote}</p>
      ) : null}
      {canvas.errors.length ? (
        <p className="mt-1 max-w-prose text-[10.5px] leading-4 text-accent-amber">
          {canvas.errors.join(" · ")}
        </p>
      ) : null}
      <p className="mt-1 text-[10px] leading-4 text-text-muted">
        Pane timeframe is set by the resolved source tier and is a PANE-LOCAL
        fact; the URL&apos;s timeframe dimension is still not applied to any query.
      </p>
    </div>
  );
}

function OverlayBar({
  overlays,
  setOverlays,
  hasPrior,
  hasVwap,
  hasPlan,
}: {
  overlays: OverlayToggles;
  setOverlays: (next: OverlayToggles) => void;
  hasPrior: boolean;
  hasVwap: boolean;
  hasPlan: boolean;
}) {
  const items: Array<{ key: keyof OverlayToggles; label: string; enabled: boolean; why: string }> = [
    { key: "valueArea", label: "VA", enabled: true, why: "value area high / low" },
    { key: "initialBalance", label: "IB", enabled: true, why: "initial balance high / low" },
    {
      key: "prior",
      label: "prior",
      enabled: hasPrior,
      why: hasPrior ? "prior-session VAH / VAL / POC" : "no prior-session levels in this payload",
    },
    {
      key: "vwap",
      label: "VWAP",
      enabled: hasVwap,
      why: hasVwap
        ? "the LAST vwap value, drawn as a level — no payload carries a vwap series"
        : "no vwap value in this payload",
    },
    {
      key: "plan",
      label: "plan",
      enabled: hasPlan,
      why: hasPlan ? "entry / stop / targets from the lane's risk block" : "no lane emitted a plan for this instrument",
    },
  ];
  return (
    <div className="mb-2 flex flex-wrap items-center gap-1.5">
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          disabled={!it.enabled}
          title={it.why}
          onClick={() => setOverlays({ ...overlays, [it.key]: !overlays[it.key] })}
          className={
            "rounded-lg border px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] transition-colors " +
            (!it.enabled
              ? "cursor-not-allowed border-bg-border/50 text-text-muted/50 line-through"
              : overlays[it.key]
                ? "border-accent-blue/60 bg-accent-blue/15 text-accent-blue"
                : "border-bg-border text-text-muted hover:text-text-primary")
          }
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

function MarkerAvailabilityNote({ plan }: { plan: { entry: number | null } | null }) {
  return (
    <p className="mt-2 max-w-prose text-[10.5px] leading-4 text-text-muted">
      {plan?.entry != null
        ? "Entry, stop and target levels come from the lane's own risk block. "
        : "No lane emitted an entry/stop/target plan for this instrument, so no plan levels are drawn. "}
      Order, fill and exit markers are not drawn at all: they live in the
      positions and paper-journal payloads, which this view deliberately does
      not poll. Their absence here means &quot;not fetched&quot;, never &quot;none happened&quot;.
    </p>
  );
}
