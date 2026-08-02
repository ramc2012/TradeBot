"use client";

/**
 * Macro-indicator HISTORY charts (owner-requested 2026-08-02) — the
 * elaborate companion to the compact indicator tape: one full recharts
 * line chart per indicator with axes, tooltip, a dashed mean reference
 * (the visual anchor for the tape's z-score), and a min/mean/max strip.
 *
 * Renders whatever history the API serves — the World Bank connector
 * currently returns annual observations; longer windows plug straight in.
 */
import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Section, StatusBadge, formatNumber } from "@/components/desk-ui";
import { CHART } from "@/components/strategies/shared";

type HistPoint = { date: string; value: number };

export type HistoryIndicator = {
  id: string;
  label: string;
  country?: string;
  latest_value?: number;
  latest_year?: string;
  unit?: string;
  change?: number;
  signal?: string; // tailwind | headwind
  history?: HistPoint[];
  source?: string;
};

const signalVariant = (s?: string): "success" | "error" | "neutral" =>
  s === "tailwind" ? "success" : s === "headwind" ? "error" : "neutral";

function IndicatorHistoryCard({ ind }: { ind: HistoryIndicator }) {
  const points = useMemo(
    () => (ind.history || []).filter((h) => Number.isFinite(h.value)),
    [ind.history],
  );

  const stats = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    return { min, max, mean, first: points[0].date, last: points[points.length - 1].date };
  }, [points]);

  const color = ind.signal === "tailwind" ? CHART.green : ind.signal === "headwind" ? CHART.red : CHART.blue;

  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[13px] font-semibold text-text-primary">{ind.label}</div>
          <div className="text-[10px] uppercase tracking-[0.12em] text-text-muted">
            {ind.country} · {ind.source || "source unknown"}
            {stats ? ` · ${stats.first} → ${stats.last} · ${points.length} obs` : ""}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm font-semibold text-text-primary">
            {formatNumber(ind.latest_value, 2)}
            <span className="ml-0.5 text-[10.5px] text-text-muted">{ind.unit}</span>
          </span>
          <StatusBadge label={ind.signal || "—"} variant={signalVariant(ind.signal)} />
        </div>
      </div>

      {stats ? (
        <>
          <div className="mt-3">
            <ResponsiveContainer width="100%" height={210}>
              <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: CHART.axis }} tickMargin={6} />
                <YAxis
                  domain={["auto", "auto"]}
                  width={52}
                  tick={{ fontSize: 10, fill: CHART.axis }}
                  tickFormatter={(v: number) => formatNumber(v, 1)}
                />
                <Tooltip
                  contentStyle={{ background: CHART.surface, border: `1px solid ${CHART.border}`, fontSize: 11, borderRadius: 8 }}
                  labelStyle={{ color: CHART.muted }}
                  formatter={(v: number) => [`${formatNumber(v, 2)} ${ind.unit || ""}`, ind.label]}
                />
                <ReferenceLine
                  y={stats.mean}
                  stroke={CHART.muted}
                  strokeDasharray="4 4"
                  label={{ value: "mean", fontSize: 9, fill: CHART.muted, position: "insideTopRight" }}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke={color}
                  strokeWidth={2}
                  dot={{ r: 2.5, fill: color, strokeWidth: 0 }}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-bg-border/40 pt-2 font-mono text-[10.5px] text-text-muted">
            <span>min {formatNumber(stats.min, 2)}</span>
            <span>mean {formatNumber(stats.mean, 2)}</span>
            <span>max {formatNumber(stats.max, 2)}</span>
            {ind.change != null ? (
              <span className={ind.change > 0 ? "text-accent-green" : ind.change < 0 ? "text-accent-red" : ""}>
                Δ latest {ind.change > 0 ? "+" : ""}{formatNumber(ind.change, 2)}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <div className="mt-3 rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-[12px] text-text-muted">
          Only a single observation served for this indicator — no history to chart. The connector
          reports source “{ind.source || "unknown"}”.
        </div>
      )}
    </div>
  );
}

export function MacroHistoryCharts({ indicators }: { indicators: HistoryIndicator[] }) {
  if (!indicators.length) {
    return (
      <Section title="Macro history">
        <div className="py-10 text-center text-sm text-text-muted">No macro indicators served yet.</div>
      </Section>
    );
  }
  const charted = indicators.filter((i) => (i.history || []).length >= 2).length;
  return (
    <Section
      title="Macro history"
      description="Full observation history per indicator, straight from the connector. Dashed line = the mean the tape's z-score is measured against."
      rightSlot={<span className="text-[11px] text-text-muted">{charted} of {indicators.length} with chartable history</span>}
    >
      <div className="grid gap-4 xl:grid-cols-2">
        {indicators.map((ind) => (
          <IndicatorHistoryCard key={ind.id} ind={ind} />
        ))}
      </div>
    </Section>
  );
}
