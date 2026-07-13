"use client";

/**
 * CvdPanel — cumulative-volume-delta sparkline vs close overlay.
 *
 * Dual-axis recharts line (price left / CVD right) with:
 *   · a PROMINENT order-flow-source badge (REAL TICKS vs BAR PROXY) — the
 *     honest-data distinction is the headline, not a footnote
 *   · a divergence annotation when the lane detected one
 *   · a LastUpdated freshness badge on the last series point
 */
import { useMemo } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { LastUpdated } from "@/components/common/LastUpdated";
import { StatusBadge, formatNumber } from "@/components/desk-ui";

import { OfSourceBadge } from "./OfSourceBadge";

export type CvdPoint = { time: string; cvd: number; close: number };
export type CvdDivergence = { kind?: string | null; strength?: number | null } | null;

export function CvdPanel({
  series,
  source,
  divergence,
  asOf,
  height = 280,
  hideHeader = false,
}: {
  series?: CvdPoint[] | null;
  source?: string | null;
  divergence?: CvdDivergence;
  /** Overrides the last series point as the freshness timestamp. */
  asOf?: string | null;
  height?: number;
  hideHeader?: boolean;
}) {
  const rows = useMemo(() => (series ?? []).filter((r) => r && Number.isFinite(Number(r.cvd)) && Number.isFinite(Number(r.close))), [series]);
  const lastTime = asOf ?? rows.at(-1)?.time ?? null;
  const divKind = divergence?.kind ? String(divergence.kind) : null;

  return (
    <div className="w-full">
      {!hideHeader ? (
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <OfSourceBadge source={source} />
            {divKind ? (
              <StatusBadge
                label={`divergence · ${divKind}${divergence?.strength != null ? ` ${formatNumber(Number(divergence.strength) * 100, 0)}%` : ""}`}
                variant={divKind.toLowerCase().includes("bull") ? "success" : divKind.toLowerCase().includes("bear") ? "error" : "info"}
              />
            ) : (
              <span className="text-[10.5px] text-text-muted">no CVD divergence</span>
            )}
          </div>
          <LastUpdated timestamp={lastTime} label="last bar" />
        </div>
      ) : null}

      {rows.length ? (
        <div style={{ height }}>
          <ResponsiveContainer>
            <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="time" tickFormatter={(v) => String(v).slice(11, 16)} minTickGap={30} tick={{ fontSize: 10, fill: "rgba(255,255,255,0.4)" }} stroke="rgba(255,255,255,0.12)" />
              <YAxis yAxisId="price" domain={["auto", "auto"]} width={58} tick={{ fontSize: 10, fill: "rgba(255,255,255,0.4)" }} stroke="rgba(255,255,255,0.12)" />
              <YAxis yAxisId="cvd" orientation="right" width={52} tick={{ fontSize: 10, fill: "#ffa502" }} stroke="rgba(255,255,255,0.12)" />
              <Tooltip
                contentStyle={{ background: "#0d1117", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 8, fontSize: 11 }}
                labelFormatter={(v) => String(v).slice(11, 19)}
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                formatter={(value: any, name: any) => [formatNumber(Number(value), name === "CVD" ? 0 : 2), name]}
              />
              <Line yAxisId="price" dataKey="close" name="Price" stroke="#3b82f6" dot={false} strokeWidth={1.5} isAnimationActive={false} />
              <Line yAxisId="cvd" dataKey="cvd" name="CVD" stroke="#ffa502" dot={false} strokeWidth={1.5} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-xs text-text-muted" style={{ height }}>
          No CVD series in this snapshot.
        </div>
      )}
    </div>
  );
}
