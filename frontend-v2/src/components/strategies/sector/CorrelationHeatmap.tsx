"use client";

/**
 * Sector-index correlation heatmap — pure SVG (viewBox-scaled). Diverging
 * red→neutral→green scale on Pearson correlation [-1, 1]. Hover surfaces the
 * exact pair + ρ. Used in the VAR/Granger model tab as the second native viz.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type CorrMatrix = { labels: string[]; values: number[][] };

const CELL = 46;
const PAD = { top: 96, left: 116, right: 8, bottom: 8 };

function corrColor(v: number): string {
  // v in [-1,1]; green for positive, red for negative, fade through surface.
  const a = Math.min(1, Math.abs(v));
  if (v >= 0) return `rgba(0,212,163,${(0.12 + a * 0.72).toFixed(3)})`;
  return `rgba(255,71,87,${(0.12 + a * 0.72).toFixed(3)})`;
}

export function CorrelationHeatmap({ matrix }: { matrix?: CorrMatrix }) {
  const [hover, setHover] = useState<{ i: number; j: number } | null>(null);

  const model = useMemo(() => {
    if (!matrix?.labels?.length || !matrix.values?.length) return null;
    const n = matrix.labels.length;
    const w = PAD.left + n * CELL + PAD.right;
    const h = PAD.top + n * CELL + PAD.bottom;
    return { n, w, h };
  }, [matrix]);

  if (!model || !matrix) {
    return (
      <div className="flex h-[280px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        Correlation matrix requires a connected sector-index history feed.
      </div>
    );
  }

  const { n, w, h } = model;
  const short = (s: string) => s.replace(/\s*&\s*/g, "&").replace("Realty&Infra", "Realty").slice(0, 9);

  return (
    <div className="relative w-full overflow-x-auto">
      <svg viewBox={`0 0 ${w} ${h}`} className="mx-auto block" style={{ maxWidth: w, width: "100%" }}>
        {/* column headers (rotated) */}
        {matrix.labels.map((lab, j) => (
          <text
            key={`col-${j}`}
            x={PAD.left + j * CELL + CELL / 2}
            y={PAD.top - 8}
            fill={hover?.j === j ? "#e6edf3" : CHART.muted}
            fontSize={10}
            textAnchor="start"
            transform={`rotate(-55 ${PAD.left + j * CELL + CELL / 2} ${PAD.top - 8})`}
          >
            {short(lab)}
          </text>
        ))}
        {/* row headers */}
        {matrix.labels.map((lab, i) => (
          <text
            key={`row-${i}`}
            x={PAD.left - 8}
            y={PAD.top + i * CELL + CELL / 2 + 3.5}
            fill={hover?.i === i ? "#e6edf3" : CHART.muted}
            fontSize={10}
            textAnchor="end"
          >
            {short(lab)}
          </text>
        ))}
        {/* cells */}
        {matrix.values.map((row, i) =>
          row.map((v, j) => {
            const isDiag = i === j;
            const isHover = hover?.i === i && hover?.j === j;
            return (
              <g
                key={`${i}-${j}`}
                onMouseEnter={() => setHover({ i, j })}
                onMouseLeave={() => setHover(null)}
                style={{ cursor: "default" }}
              >
                <rect
                  x={PAD.left + j * CELL + 1.5}
                  y={PAD.top + i * CELL + 1.5}
                  width={CELL - 3}
                  height={CELL - 3}
                  rx={4}
                  fill={isDiag ? "rgba(255,255,255,0.06)" : corrColor(v)}
                  stroke={isHover ? "#e6edf3" : "transparent"}
                  strokeWidth={isHover ? 1.4 : 0}
                />
                <text
                  x={PAD.left + j * CELL + CELL / 2}
                  y={PAD.top + i * CELL + CELL / 2 + 3.5}
                  fill={Math.abs(v) > 0.55 ? "#0d1117" : "#c9d4e0"}
                  fontSize={9.5}
                  fontWeight={Math.abs(v) > 0.7 ? 700 : 400}
                  textAnchor="middle"
                >
                  {v.toFixed(2)}
                </text>
              </g>
            );
          }),
        )}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute left-3 top-1 rounded-lg border px-2.5 py-1.5 text-[11px]"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <span className="font-semibold text-text-primary">
            {matrix.labels[hover.i]} × {matrix.labels[hover.j]}
          </span>
          <span className="ml-2 font-mono text-text-muted">ρ {matrix.values[hover.i]?.[hover.j]?.toFixed(3)}</span>
        </div>
      ) : null}

      <div className="mt-2 flex items-center justify-center gap-3 text-[10px] text-text-muted">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-4 rounded" style={{ background: "rgba(255,71,87,0.8)" }} /> −1
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-4 rounded" style={{ background: "rgba(255,255,255,0.1)" }} /> 0
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-4 rounded" style={{ background: "rgba(0,212,163,0.8)" }} /> +1
        </span>
        <span className="text-text-muted/70">Pearson ρ over the model window</span>
      </div>
    </div>
  );
}
