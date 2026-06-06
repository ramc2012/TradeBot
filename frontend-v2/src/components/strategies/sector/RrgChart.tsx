"use client";

/**
 * Relative-Rotation-Graph (RRG) scatter — the innovative centerpiece of
 * the Sector desk. Pure SVG (viewBox-scaled) so it stays crisp at any
 * width without a charting dependency that can't draw labelled quadrants.
 *
 *   x-axis = relative strength (JdK RS-Ratio analogue)
 *   y-axis = momentum         (JdK RS-Momentum analogue)
 *   origin = 0,0 crosshair → 4 quadrants:
 *     top-right    Leading    (strong + accelerating)
 *     top-left     Improving  (weak  + accelerating)
 *     bottom-left  Lagging    (weak  + decelerating)
 *     bottom-right Weakening  (strong + decelerating)
 *
 * Each sector is a bubble sized by |leadership_score|, coloured by
 * quadrant. A faint trail (if `tail` history is supplied) shows the
 * rotation path. Hover surfaces the full read-out.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type RrgPoint = {
  sector_key: string;
  sector: string;
  x: number; // relative strength
  y: number; // momentum
  quadrant: string;
  leadership_score: number;
  tail?: Array<{ x: number; y: number }>;
};

const VB_W = 1000;
const VB_H = 620;
const PAD = { t: 22, r: 26, b: 30, l: 34 };

const QUAD_TONE: Record<string, string> = {
  leading: CHART.green,
  improving: CHART.blue,
  lagging: CHART.red,
  weakening: CHART.amber,
};

function quadColor(q?: string): string {
  return QUAD_TONE[String(q || "").toLowerCase()] || CHART.muted;
}

export function RrgChart({
  points,
  selected,
  onSelect,
}: {
  points: RrgPoint[];
  selected?: string | null;
  onSelect?: (key: string) => void;
}) {
  const [hover, setHover] = useState<RrgPoint | null>(null);

  const model = useMemo(() => {
    if (!points.length) return null;
    const xs = points.flatMap((p) => [p.x, ...(p.tail || []).map((t) => t.x)]);
    const ys = points.flatMap((p) => [p.y, ...(p.tail || []).map((t) => t.y)]);
    // Symmetric bounds around 0 so the crosshair sits at the true centre.
    const xAbs = Math.max(1e-6, ...xs.map((v) => Math.abs(v))) * 1.18;
    const yAbs = Math.max(1e-6, ...ys.map((v) => Math.abs(v))) * 1.18;
    const xMin = -xAbs;
    const xMax = xAbs;
    const yMin = -yAbs;
    const yMax = yAbs;
    const xOf = (v: number) => PAD.l + ((v - xMin) / (xMax - xMin)) * (VB_W - PAD.l - PAD.r);
    const yOf = (v: number) => PAD.t + (1 - (v - yMin) / (yMax - yMin)) * (VB_H - PAD.t - PAD.b);
    const scores = points.map((p) => Math.abs(p.leadership_score) || 0);
    const sMax = Math.max(1e-6, ...scores);
    const rOf = (s: number) => 7 + (Math.sqrt(Math.abs(s) / sMax) || 0) * 17;
    return { xMin, xMax, yMin, yMax, xOf, yOf, rOf };
  }, [points]);

  if (!model || !points.length) {
    return (
      <div className="flex h-[360px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        Awaiting sector rotation data…
      </div>
    );
  }

  const { xOf, yOf, rOf } = model;
  const cx = xOf(0);
  const cy = yOf(0);

  // Sort so big/selected bubbles draw last (on top).
  const drawOrder = [...points].sort((a, b) => {
    if (a.sector_key === selected) return 1;
    if (b.sector_key === selected) return -1;
    return Math.abs(a.leadership_score) - Math.abs(b.leadership_score);
  });

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* quadrant fills */}
        <rect x={cx} y={PAD.t} width={VB_W - PAD.r - cx} height={cy - PAD.t} fill="rgba(0,212,163,0.05)" />
        <rect x={PAD.l} y={PAD.t} width={cx - PAD.l} height={cy - PAD.t} fill="rgba(59,130,246,0.05)" />
        <rect x={PAD.l} y={cy} width={cx - PAD.l} height={VB_H - PAD.b - cy} fill="rgba(255,71,87,0.05)" />
        <rect x={cx} y={cy} width={VB_W - PAD.r - cx} height={VB_H - PAD.b - cy} fill="rgba(255,165,2,0.05)" />

        {/* crosshair axes */}
        <line x1={cx} y1={PAD.t} x2={cx} y2={VB_H - PAD.b} stroke={CHART.axis} strokeWidth={0.8} strokeDasharray="4 4" />
        <line x1={PAD.l} y1={cy} x2={VB_W - PAD.r} y2={cy} stroke={CHART.axis} strokeWidth={0.8} strokeDasharray="4 4" />

        {/* quadrant labels */}
        <QuadLabel x={VB_W - PAD.r - 8} y={PAD.t + 16} anchor="end" color={CHART.green} title="LEADING" sub="strong · accelerating" />
        <QuadLabel x={PAD.l + 8} y={PAD.t + 16} anchor="start" color={CHART.blue} title="IMPROVING" sub="weak · accelerating" />
        <QuadLabel x={PAD.l + 8} y={VB_H - PAD.b - 18} anchor="start" color={CHART.red} title="LAGGING" sub="weak · decelerating" />
        <QuadLabel x={VB_W - PAD.r - 8} y={VB_H - PAD.b - 18} anchor="end" color={CHART.amber} title="WEAKENING" sub="strong · decelerating" />

        {/* axis captions */}
        <text x={VB_W - PAD.r} y={cy - 6} fill={CHART.muted} fontSize={10} textAnchor="end">relative strength →</text>
        <text x={cx + 6} y={PAD.t + 10} fill={CHART.muted} fontSize={10} textAnchor="start">↑ momentum</text>

        {/* rotation tails */}
        {drawOrder.map((p) => {
          if (!p.tail?.length) return null;
          const pts = [...p.tail, { x: p.x, y: p.y }];
          const d = pts.map((t, i) => `${i === 0 ? "M" : "L"}${xOf(t.x).toFixed(1)},${yOf(t.y).toFixed(1)}`).join(" ");
          return <path key={`tail-${p.sector_key}`} d={d} fill="none" stroke={quadColor(p.quadrant)} strokeWidth={1} opacity={0.35} strokeDasharray="2 3" />;
        })}

        {/* bubbles */}
        {drawOrder.map((p) => {
          const x = xOf(p.x);
          const y = yOf(p.y);
          const r = rOf(p.leadership_score);
          const col = quadColor(p.quadrant);
          const isSel = p.sector_key === selected;
          const isHover = hover?.sector_key === p.sector_key;
          return (
            <g
              key={p.sector_key}
              onMouseEnter={() => setHover(p)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onSelect?.(p.sector_key)}
              style={{ cursor: onSelect ? "pointer" : "default" }}
            >
              <circle cx={x} cy={y} r={r} fill={col} fillOpacity={isSel ? 0.4 : 0.2} stroke={col} strokeWidth={isSel || isHover ? 2.2 : 1.2} />
              <circle cx={x} cy={y} r={2} fill={col} />
              <text x={x} y={y - r - 4} fill={isSel || isHover ? "#e6edf3" : CHART.muted} fontSize={isSel ? 11 : 9.5} textAnchor="middle" fontWeight={isSel ? 700 : 500}>
                {shortName(p.sector)}
              </text>
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute right-3 top-3 rounded-lg border px-3 py-2 text-[11px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <div className="mb-1 font-sans text-[12px] font-semibold" style={{ color: quadColor(hover.quadrant) }}>
            {hover.sector}
          </div>
          <div className="text-text-muted">{hover.quadrant.toUpperCase()}</div>
          <div>RS {hover.x.toFixed(2)} · Mom {hover.y.toFixed(2)}</div>
          <div>Lead {hover.leadership_score.toFixed(2)}</div>
        </div>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="leading" />
        <Legend color={CHART.blue} label="improving" />
        <Legend color={CHART.amber} label="weakening" />
        <Legend color={CHART.red} label="lagging" />
        <span className="text-text-muted/70">bubble size = leadership magnitude</span>
      </div>
    </div>
  );
}

function QuadLabel({ x, y, anchor, color, title, sub }: { x: number; y: number; anchor: "start" | "end"; color: string; title: string; sub: string }) {
  return (
    <g>
      <text x={x} y={y} fill={color} fontSize={12} fontWeight={700} textAnchor={anchor} opacity={0.85}>{title}</text>
      <text x={x} y={y + 12} fill={CHART.muted} fontSize={8.5} textAnchor={anchor}>{sub}</text>
    </g>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}

/** Trim "Nifty " prefix + verbose suffixes so labels fit in the scatter. */
function shortName(s: string): string {
  return s
    .replace(/^Nifty\s+/i, "")
    .replace(/\s+Ex-Bank$/i, " ex-Bk")
    .replace(/Financial Services/i, "Fin Svc")
    .replace(/Capital Markets/i, "Cap Mkt")
    .replace(/Consumer Durables/i, "Cons Dur");
}
