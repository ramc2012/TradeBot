"use client";

/**
 * FlowView — institutional order flow for the pinned instrument, honestly
 * labelled.
 *
 * Every panel is wrapped in `ProvenancePanel`, so source · derivation ·
 * aggregation · completeness · age · data mode is derived ONCE by the shared
 * contract and rendered the same way everywhere. No panel here carries a
 * bespoke honesty badge.
 *
 * The standing claim this view makes on its face: CVD, the footprint split,
 * aggression, absorption and delta are ATTRIBUTIONS. Their inputs — quotes and
 * cumulative volume — are observed; the split into buy and sell is inferred,
 * because no wired broker sends an aggressor-tagged print. The contract forces
 * that grade (`feature: "flow_attribution"`), so it cannot be forgotten on a
 * new panel.
 *
 * Two panels a real terminal would have are rendered as CAPABILITY-ABSENT
 * cards instead of proxies:
 *   · Order-book depth (DOM)  — the backend serves a broker depth PROXY; a
 *                               reconstruction inside a slot labelled "depth"
 *                               would function as the real thing on screen.
 *   · Aggressor prints        — there is no such feed at all.
 * Those cards read `MISSING_CAPABILITIES` from the shared contract, so what
 * they say cannot drift from what the grading does.
 *
 * The quote tape IS rendered, because it is honest about itself: it is built
 * from the shared /ws/quotes stream and its rows are reconstructed from quote
 * and volume deltas, which its own header states.
 */
import { Activity, BookOpen, Gauge, Radio, Waves } from "lucide-react";
import { useMemo } from "react";

import { Section, StatusBadge, formatNumber } from "@/components/desk-ui";
import { CvdPanel, FootprintGrid, LiveOrderFlowTape, OrderFlowPulse } from "@/components/mpof";
import { underlyingToTapeSymbol } from "@/lib/marketSymbols";

import type { MatrixRow } from "../command/useUniverseMatrix";
import type { WorkspaceContext } from "../context/schema";
import { useMarketCanvas } from "../structure/useMarketCanvas";

import { CapabilityAbsentCard } from "./CapabilityAbsentCard";
import { OiWallPanel } from "./OiWallPanel";
import { ProvenancePanel } from "./ProvenancePanel";

/** Metrics the payload reports as bounded scores / signed shares. */
const METRIC_ROWS: Array<{ key: string; label: string; hint: string }> = [
  { key: "execution_aggression", label: "Execution aggression", hint: "how far prints sit from the mid — inferred" },
  { key: "book_pressure", label: "Book pressure", hint: "L1 size skew (observed sizes, inferred intent)" },
  { key: "trade_imbalance", label: "Trade imbalance", hint: "signed share of attributed volume" },
  { key: "order_flow_imbalance", label: "Order-flow imbalance", hint: "signed share, quote-derived" },
  { key: "queue_pressure", label: "Queue pressure", hint: "L1 queue asymmetry" },
  { key: "toxicity_score", label: "Toxicity / adverse selection", hint: "modelled score" },
];

export function FlowView({ ctx, row }: { ctx: WorkspaceContext; row: MatrixRow | null }) {
  const canvas = useMarketCanvas(ctx.symbol, ctx.market, true);

  const cvdSeries = useMemo(
    () =>
      canvas.flow.points.map((p) => ({ time: p.time, cvd: p.cvd, close: p.close })),
    [canvas.flow.points],
  );

  const tapeSymbol =
    ctx.contract && (ctx.contract.includes(":") || ctx.contract.includes("|"))
      ? ctx.contract
      : underlyingToTapeSymbol(ctx.symbol) ?? ctx.symbol;

  const digits = ctx.market === "MCX" ? 2 : 1;

  return (
    <div className="space-y-4">
      <header className="rounded-xl border border-bg-border/70 bg-bg-secondary/20 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold text-text-primary">
            {ctx.market} · {ctx.symbol}
          </span>
          <StatusBadge label="every side below is inferred" variant="info" />
          {canvas.isFetching ? <StatusBadge label="refreshing" variant="neutral" /> : null}
        </div>
        <p className="mt-1 max-w-prose text-[10.5px] leading-4 text-text-muted">
          Inputs are observed quotes plus cumulative volume. The attribution of
          that volume to buyers and sellers is a heuristic with no
          aggressor-tagged feed to check it against, so no panel here can grade
          better than inferred-from-quotes — including when the underlying quote
          stream is perfectly good.
        </p>
      </header>

      <div className="grid gap-4 xl:grid-cols-2">
        <ProvenancePanel
          title="Cumulative delta (CVD)"
          icon={<Waves size={14} />}
          description="Net attributed volume accumulated across the session, against price."
          source={canvas.flow.source}
          asOf={canvas.asOf}
          timeframe={canvas.flow.timeframe}
          have={cvdSeries.length}
          completenessLabel={`${cvdSeries.length} bars`}
          unavailable={cvdSeries.length ? null : canvas.flow.unavailable ?? "no CVD series in the served payload this cycle."}
        >
          <CvdPanel
            series={cvdSeries}
            source={canvas.flow.source}
            asOf={canvas.asOf}
            timeframe={canvas.flow.timeframe}
            height={280}
            hideHeader
          />
        </ProvenancePanel>

        <ProvenancePanel
          title="Aggression, initiative and absorption"
          icon={<Activity size={14} />}
          description="Signed volume per bar. Beyond ±60% of a bar's volume reads as initiative; high volume with a flat split reads as absorption."
          source={canvas.footprint.source ?? canvas.flow.source}
          asOf={canvas.asOf}
          timeframe={canvas.footprint.timeframe}
          have={canvas.footprint.bars.length}
          completenessLabel={`${canvas.footprint.bars.length} bars`}
          unavailable={canvas.footprint.bars.length ? null : canvas.footprint.unavailable}
        >
          <OrderFlowPulse
            bars={canvas.footprint.bars}
            source={canvas.footprint.source ?? canvas.flow.source}
            asOf={canvas.asOf}
            timeframe={canvas.footprint.timeframe}
            height={260}
          />
        </ProvenancePanel>
      </div>

      <ProvenancePanel
        title="Footprint"
        icon={<BookOpen size={14} />}
        description="Volume bucketed at price with the two sides inferred. Imbalance is a BOUNDED 0-100% share plus the raw volumes — never the backend's unbounded side ratio, which is a divide-by-zero artefact when one side is empty."
        source={canvas.footprint.source}
        asOf={canvas.asOf}
        timeframe={canvas.footprint.timeframe}
        have={canvas.footprint.bars.length}
        completenessLabel={`${canvas.footprint.bars.length} bars`}
        unavailable={canvas.footprint.bars.length ? null : canvas.footprint.unavailable}
      >
        <FootprintGrid
          bars={canvas.footprint.bars}
          source={canvas.footprint.source}
          timeframe={canvas.footprint.timeframe}
          maxBars={5}
          digits={digits}
          hideHeader
        />
      </ProvenancePanel>

      <div className="grid gap-4 xl:grid-cols-2">
        <ProvenancePanel
          title="Microstructure metrics"
          icon={<Gauge size={14} />}
          description="L1-derived scores from the order-flow snapshot. Sizes are observed; every intent read on top of them is inferred."
          source={canvas.metricsSource}
          asOf={canvas.asOf}
          timeframe={canvas.barTimeframe}
          unavailable={
            canvas.metrics
              ? null
              : "microstructure metrics come from /api/orderflow/snapshot, which supports NIFTY, BANKNIFTY, SENSEX and CRUDEOIL only. This instrument is outside that set."
          }
        >
          <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
            {METRIC_ROWS.map((m) => {
              const raw = canvas.metrics?.[m.key];
              const value =
                raw == null || raw === ""
                  ? null
                  : typeof raw === "number"
                    ? formatNumber(raw, 3)
                    : String(raw);
              return (
                <div key={m.key} className="flex items-baseline justify-between gap-2" title={m.hint}>
                  <dt className="text-[10.5px] text-text-muted">{m.label}</dt>
                  <dd
                    className={
                      "font-mono text-[11.5px] " +
                      (value == null ? "text-text-muted" : "text-text-secondary")
                    }
                  >
                    {value ?? "UNAVAILABLE"}
                  </dd>
                </div>
              );
            })}
          </dl>
        </ProvenancePanel>

        <OiWallPanel
          walls={canvas.options}
          spot={canvas.spot}
          source={canvas.options ? "option_chain" : null}
          asOf={canvas.asOf}
          unavailable={
            canvas.options
              ? null
              : row?.options.available === false
                ? row.options.reason
                : undefined
          }
        />
      </div>

      <Section
        title="Quote tape"
        icon={<Radio size={16} />}
        description="The shared /ws/quotes stream. Rows are reconstructed from quote and volume deltas — this is not an exchange trade tape, and each row's side is inferred from where the print sat against the bid and ask."
      >
        <LiveOrderFlowTape
          symbol={tapeSymbol}
          title={`${ctx.symbol} · quote pulse (sides inferred)`}
        />
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <CapabilityAbsentCard capability="DEPTH_L2" slotTitle="Order-book depth (DOM)" />
        <CapabilityAbsentCard
          capability="BROKER_AGGRESSOR_PRINTS"
          slotTitle="Aggressor-tagged trade tape"
        />
      </div>
    </div>
  );
}
