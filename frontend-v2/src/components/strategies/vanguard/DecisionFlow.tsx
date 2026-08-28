"use client";

/**
 * The decision flow: where a bar's candidates actually die.
 *
 * WHY A RIBBON RATHER THAN A BAR CHART. M6's filter is a hard AND-chain of six
 * legs. A bar chart of "survivors after each leg" is readable but hides the
 * thing a trader needs: the SHAPE of the attrition — whether the universe is
 * lost in one cliff at one leg (a data problem) or bled evenly across all six
 * (a tuning problem). Those two look nearly identical as bars and completely
 * different as a narrowing ribbon with wedges peeling off it.
 *
 * WHY "BIGGEST KILLER" AND NOT "FIRST ZERO". The previous funnel named the
 * first stage whose survivor count hit zero. In a hard AND-chain that is
 * usually just the last stage, which is never the interesting one — the leg
 * that killed 180 of 210 names is. The API computes the biggest killer; this
 * component only draws it.
 *
 * The counts come from `candidate_evaluations`, the journal M6 writes as it
 * decides. When a bar predates that journal the API says so (`source:
 * "rederived"`) and this component renders the warning rather than quietly
 * showing numbers that cannot see the freshness legs.
 */
import { useMemo } from "react";
import { AlertTriangle, Filter, Info, Layers } from "lucide-react";

import { MetricTile, Section, StatusBadge, formatIST, formatMoney } from "@/components/desk-ui";
import { LEG_LABELS, ScoreBar, Unmeasured, num } from "./vanguard-vocab";

type Stage = {
  leg: string;
  stage: string;
  surviving: number;
  lost_here: number;
  gate: string;
  examples?: string[];
};

const RIBBON_W = 860;
const RIBBON_H = 190;
const TOP = 16;

/**
 * Survivors are drawn on a SQRT scale, not a linear one.
 *
 * Linearly, a chain that goes 210 → 4 → 4 → 0 is a single vertical cliff
 * followed by a hairline nobody can see or click. Sqrt keeps the small tail
 * legible while preserving the ordering, and the exact count is printed on
 * every stage so the scale is never what a reader takes the number from.
 */
function scaleHeight(count: number, entered: number): number {
  if (!entered) return 0;
  const usable = RIBBON_H - TOP - 26;
  return Math.max(count > 0 ? 3 : 0, Math.sqrt(count / entered) * usable);
}

export function AttritionRibbon({ stages, binding }: { stages: Stage[]; binding?: string | null }) {
  const entered = stages[0]?.surviving ?? 0;
  const geometry = useMemo(() => {
    if (!stages.length || !entered) return null;
    const slot = RIBBON_W / stages.length;
    return stages.map((stage, i) => {
      const h = scaleHeight(stage.surviving, entered);
      const prevH = i === 0 ? h : scaleHeight(stages[i - 1].surviving, entered);
      return {
        ...stage,
        x: i * slot,
        w: slot,
        h,
        prevH,
        cy: TOP + (RIBBON_H - TOP - 26) / 2,
      };
    });
  }, [stages, entered]);

  if (!geometry) {
    return (
      <p className="text-sm text-text-secondary">
        No symbols were evaluated at this bar. That is a missing FEED, not a no-trade decision —
        see the Pipeline tab.
      </p>
    );
  }

  return (
    <svg
      viewBox={`0 0 ${RIBBON_W} ${RIBBON_H}`}
      className="w-full"
      role="img"
      aria-label={`Candidate attrition across ${stages.length} filter legs, from ${entered} symbols to ${stages[stages.length - 1].surviving}`}
    >
      <defs>
        <linearGradient id="vg-alive" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="rgb(var(--accent-blue))" stopOpacity="0.55" />
          <stop offset="100%" stopColor="rgb(var(--accent-green))" stopOpacity="0.55" />
        </linearGradient>
      </defs>

      {geometry.map((g, i) => {
        const isBinding = binding === g.leg;
        const nextH = i < geometry.length - 1 ? geometry[i + 1].h : g.h;
        // The surviving cohort: a trapezium from this stage's height to the next.
        const y0 = g.cy - g.h / 2;
        const y1 = g.cy + g.h / 2;
        const ny0 = g.cy - nextH / 2;
        const ny1 = g.cy + nextH / 2;
        return (
          <g key={g.leg}>
            <path
              d={`M${g.x},${y0} L${g.x + g.w},${ny0} L${g.x + g.w},${ny1} L${g.x},${y1} Z`}
              fill="url(#vg-alive)"
              stroke="rgb(var(--bg-border))"
              strokeWidth={0.5}
            />
            {/* the wedge that peels away: candidates that died AT the next leg */}
            {i < geometry.length - 1 && geometry[i + 1].lost_here > 0 && (
              <path
                d={`M${g.x + g.w},${ny1} L${g.x + g.w},${y1} L${g.x + g.w * 0.55},${RIBBON_H - 24} Z`}
                fill="rgb(var(--accent-red))"
                fillOpacity={isBinding || binding === geometry[i + 1].leg ? 0.5 : 0.25}
              />
            )}
            {i > 0 && (
              <line
                x1={g.x}
                y1={TOP - 6}
                x2={g.x}
                y2={RIBBON_H - 22}
                stroke="rgb(var(--bg-border))"
                strokeWidth={0.75}
                strokeDasharray="2 3"
              />
            )}
            <text
              x={g.x + 4}
              y={TOP - 8}
              className="fill-current text-[10px]"
              style={{ fill: isBinding ? "rgb(var(--accent-amber))" : "rgb(var(--text-muted))" }}
            >
              {LEG_LABELS[g.leg] ?? g.stage}
            </text>
            <text
              x={g.x + 4}
              y={g.cy + 4}
              className="text-[13px] font-semibold"
              style={{ fill: "rgb(var(--text-primary))" }}
            >
              {g.surviving}
            </text>
            {g.lost_here > 0 && (
              <text
                x={g.x + 4}
                y={RIBBON_H - 8}
                className="text-[10px]"
                style={{ fill: "rgb(var(--accent-red))" }}
              >
                −{g.lost_here}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function DecisionFlowTab({
  funnel,
  selection,
  market,
  onPickSymbol,
}: {
  funnel?: any;
  selection?: any;
  market?: any;
  onPickSymbol?: (symbol: string) => void;
}) {
  const stages: Stage[] = funnel?.stages ?? [];
  const survivors = funnel?.survivors ?? 0;
  const binding = funnel?.binding_constraint;
  const rederived = funnel?.source === "rederived";
  const bindingStage = stages.find((s) => s.leg === binding);

  // Conviction across the WHOLE evaluated universe, not just survivors. The
  // gate sits at 85 and the lane's own history never reached it; a histogram
  // shows how far away the universe actually is, which a "0 tickets" line
  // cannot.
  const convictionHistogram = useMemo(() => {
    const rows: any[] = market?.symbols ?? [];
    const values = rows.map((r) => num(r.conviction)).filter((v): v is number => v != null);
    if (!values.length) return null;
    const buckets = Array.from({ length: 10 }, (_, i) => ({ lo: i * 10, hi: i * 10 + 10, n: 0 }));
    values.forEach((v) => {
      const idx = Math.max(0, Math.min(9, Math.floor(v / 10)));
      buckets[idx].n += 1;
    });
    return { buckets, max: Math.max(...buckets.map((b) => b.n)), n: values.length,
             best: Math.max(...values) };
  }, [market]);

  const convictionMin = num(market?.thresholds?.conviction_min) ?? 85;

  return (
    <div className="space-y-4">
      <Section
        title="Where this bar's candidates died"
        icon={<Filter size={16} />}
        description={
          funnel?.ts
            ? `${formatIST(funnel.ts)} · each leg is a hard AND. A candidate dies at exactly one of them; the rest are never asked.`
            : "No timing bar available yet."
        }
        rightSlot={
          funnel?.config_hash ? (
            <span className="font-mono text-[10px] text-text-muted" title="Hash of every threshold and weight M6 applied at this bar. A retune produces visibly different rows rather than quietly reinterpreting old ones.">
              config {funnel.config_hash}
            </span>
          ) : null
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <StatusBadge
            label={survivors > 0 ? `${survivors} candidate${survivors === 1 ? "" : "s"}` : "no trade"}
            variant={survivors > 0 ? "success" : "neutral"}
          />
          {bindingStage && (
            <StatusBadge
              label={`biggest killer: ${LEG_LABELS[binding] ?? binding} (−${bindingStage.lost_here})`}
              variant="warn"
              icon={<Info size={12} />}
            />
          )}
          <span className="text-xs text-text-muted">
            A reasoned no-trade is the designed default (doctrine #2), not a fault.
          </span>
        </div>

        {rederived && (
          <div className="mb-3 flex items-start gap-2 rounded-xl border border-accent-amber/35 bg-accent-amber/8 px-3 py-2">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-accent-amber" />
            <p className="text-[11px] leading-relaxed text-text-secondary">
              {funnel?.note ??
                "This bar predates the evaluation journal, so these counts were re-derived in SQL and cannot see the freshness legs. Treat them as an upper bound."}
            </p>
          </div>
        )}

        <AttritionRibbon stages={stages} binding={binding} />

        <div className="mt-3 grid gap-1.5">
          {stages.slice(1).map((stage) => {
            const isBinding = binding === stage.leg;
            return (
              <div
                key={stage.leg}
                className={
                  "rounded-xl border px-3 py-2 " +
                  (isBinding
                    ? "border-accent-amber/45 bg-accent-amber/8"
                    : "border-bg-border bg-bg-secondary/25")
                }
              >
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-sm font-medium text-text-primary">
                    {LEG_LABELS[stage.leg] ?? stage.stage}
                  </span>
                  <span className="font-mono text-sm text-text-primary">
                    {stage.surviving}
                    {stage.lost_here > 0 && (
                      <span className="ml-2 text-xs text-accent-red">−{stage.lost_here}</span>
                    )}
                  </span>
                </div>
                <div className="mt-1 text-[11px] text-text-muted">{stage.gate}</div>
                {!!stage.examples?.length && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {stage.examples.map((symbol) => (
                      <button
                        key={symbol}
                        type="button"
                        onClick={() => onPickSymbol?.(symbol)}
                        className="rounded border border-bg-border bg-bg-primary/30 px-1.5 py-0.5 font-mono text-[10px] text-text-secondary transition-colors hover:border-accent-blue/40 hover:text-text-primary"
                        title={`open ${symbol} — see exactly which input failed this leg`}
                      >
                        {symbol}
                      </button>
                    ))}
                    {stage.lost_here > (stage.examples.length || 0) && (
                      <span className="px-1 text-[10px] text-text-muted">
                        +{stage.lost_here - stage.examples.length} more
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Section>

      <Section
        title="How far the universe is from the conviction gate"
        icon={<Layers size={16} />}
        description={
          convictionHistogram
            ? `Conviction across all ${convictionHistogram.n} evaluated symbols at this bar — not just the ones that cleared the filter. The gate is ${convictionMin}.`
            : "No evaluated symbols at this bar."
        }
      >
        {!convictionHistogram ? (
          <Unmeasured why="candidate_evaluations has no rows at this bar" />
        ) : (
          <>
            <div className="flex h-28 items-end gap-1">
              {convictionHistogram.buckets.map((b) => {
                const past = b.lo >= convictionMin;
                return (
                  <div key={b.lo} className="flex flex-1 flex-col items-center gap-1">
                    <span className="font-mono text-[10px] text-text-muted">{b.n || ""}</span>
                    <div
                      className={
                        "w-full rounded-t " +
                        (past ? "bg-accent-green/70" : "bg-accent-blue/35")
                      }
                      style={{
                        height: `${convictionHistogram.max ? (b.n / convictionHistogram.max) * 84 : 0}px`,
                        minHeight: b.n ? "2px" : "0px",
                      }}
                      title={`${b.n} symbols with conviction ${b.lo}–${b.hi}`}
                    />
                    <span className="font-mono text-[9px] text-text-muted">{b.lo}</span>
                  </div>
                );
              })}
            </div>
            <p className="mt-2 text-[11px] text-text-muted">
              Best conviction at this bar:{" "}
              <span className="font-mono text-text-secondary">
                {convictionHistogram.best.toFixed(1)}
              </span>
              {convictionHistogram.best < convictionMin && (
                <> — {(convictionMin - convictionHistogram.best).toFixed(1)} short of the gate.
                  Lowering the gate to meet it would be tuning a threshold against the sample
                  it is supposed to judge.</>
              )}
            </p>
          </>
        )}
      </Section>

      {!!market?.thresholds && (
        <Section
          title="Thresholds in force"
          description="Served by the API and guarded against M6's own source by backend/tests/test_vanguard_router.py — the desk always shows the numbers the selector actually applied, never its own copy."
        >
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
            <MetricTile size="sm" label="|flow| ≥" value={String(market.thresholds.flow_min_abs)} />
            <MetricTile size="sm" label="flow age ≤" value={`${market.thresholds.flow_max_age_sessions}s`}
                        detail="sessions, not calendar days" />
            <MetricTile size="sm" label="ingredients ≥" value={String(market.thresholds.flow_min_ingredients)}
                        detail="of 5, or the score is uncorroborated" />
            <MetricTile size="sm" label="|RS z| ≥" value={String(market.thresholds.sector_rs_min_abs_z)}
                        detail={`≤ ${market.thresholds.rs_max_age_sessions} sessions old`} />
            <MetricTile size="sm" label="regime age ≤" value={`${market.thresholds.regime_max_age_bars}b`}
                        detail="30-minute bars" />
            <MetricTile size="sm" label="timing ≥" value={String(market.thresholds.timing_min_score)} />
            <MetricTile size="sm" label="conviction ≥" value={String(market.thresholds.conviction_min)} />
            <MetricTile
              size="sm"
              label="regime permits"
              value={String((market.thresholds.regime_permits ?? []).length)}
              detail={(market.thresholds.regime_permits ?? []).join(", ")}
            />
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-text-muted">
            The three age limits did not exist before 2026-08-27. Until then the joins behind these
            legs had no maximum age at all, so a live bar could take a month-old flow score as
            though it were yesterday&apos;s reading — and nothing in the code objected.
          </p>
        </Section>
      )}

      <Section
        title="Tickets & near-misses"
        icon={<Layers size={16} />}
        description={
          selection?.ts
            ? `Every candidate that cleared the filter at ${formatIST(selection.ts)} — emitted AND gated. Gated rows are kept on purpose (doctrine #5).`
            : "No ticket has ever been generated."
        }
      >
        {!selection?.tickets?.length ? (
          <p className="text-sm text-text-secondary">
            No candidate has ever cleared all six legs at the same bar. The funnel above names the
            leg responsible; the Market tab shows the inputs behind it.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[840px] text-left text-xs">
              <thead className="text-[10px] uppercase tracking-[0.14em] text-text-muted">
                <tr>
                  <th className="py-1.5 pr-3">symbol</th>
                  <th className="py-1.5 pr-3">instrument</th>
                  <th className="py-1.5 pr-3">dir</th>
                  <th className="py-1.5 pr-3">conviction</th>
                  <th className="py-1.5 pr-3">entry</th>
                  <th className="py-1.5 pr-3">stop</th>
                  <th className="py-1.5 pr-3">risk @ stop</th>
                  <th className="py-1.5 pr-3">premium</th>
                  <th className="py-1.5">outcome</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {selection.tickets.map((t: any) => (
                  <tr key={t.id} className="border-t border-bg-border/60">
                    <td className="py-1.5 pr-3">
                      <button type="button" onClick={() => onPickSymbol?.(t.symbol)}
                              className="text-text-primary hover:text-accent-blue">
                        {t.symbol}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3 text-text-secondary">{t.instrument ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-text-secondary">{t.direction}</td>
                    <td className="py-1.5 pr-3">
                      <ScoreBar value={num(t.conviction)} threshold={convictionMin} color="violet" />
                    </td>
                    <td className="py-1.5 pr-3 text-text-secondary">
                      {t.entry_zone_low ? `${t.entry_zone_low}–${t.entry_zone_high}` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-text-secondary">{t.stop ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-text-secondary"
                        title="Capital at risk if the stop fills as intended — the number heat and the loss stops are denominated in.">
                      {t.sizing_risk_rupees ? formatMoney(num(t.sizing_risk_rupees)) : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-text-muted"
                        title="Total premium paid — the most a gap to zero can take. Capped separately.">
                      {t.sizing_premium_rupees ? formatMoney(num(t.sizing_premium_rupees)) : "—"}
                    </td>
                    <td className="py-1.5">
                      {t.emitted ? (
                        <span className="text-accent-green">emitted</span>
                      ) : (
                        <span className="text-text-muted" title={t.gated_reason ?? ""}>
                          {t.gated_reason ?? "gated"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
