"use client";

/**
 * Relative-Rotation-Graph (RRG) scatter — THE shared quadrant-scatter
 * renderer. One implementation behind the two domain adapters that used
 * to be near-copies (strategies/sector/RrgChart and strategies/cbe/
 * RrgScatter):
 *
 *   x-axis = relative strength     y-axis = momentum
 *   origin = 0,0 crosshair → 4 quadrants:
 *     top-right    Leading    top-left     Improving
 *     bottom-left  Lagging    bottom-right Weakening
 *
 * Pure SVG (viewBox-scaled) so it stays crisp at any width without a
 * charting dependency that can't draw labelled quadrants. Bubbles are
 * sized by |size| (sqrt-scaled), coloured by quadrant; optional rotation
 * tails, ring emphasis, selection, and hover read-out.
 */
import { useMemo, useState } from "react";

import { CHART } from "./chartTheme";

export const QUADRANT_COLOR: Record<string, string> = {
  leading: CHART.green,
  improving: CHART.blue,
  weakening: CHART.amber,
  lagging: CHART.red,
};

export function quadrantColor(q?: string): string {
  return QUADRANT_COLOR[String(q || "").toLowerCase()] || CHART.muted;
}

export type RrgScatterPoint = {
  key: string;
  /** Bubble label. */
  label: string;
  /** Tooltip headline; defaults to `label`. */
  title?: string;
  x: number;
  y: number;
  /** Bubble magnitude (abs, sqrt-scaled). */
  size: number;
  quadrant: string;
  /** Emphasized stroke ring (e.g. on-watchlist names). */
  ring?: boolean;
  /** Rotation path history, oldest first. */
  tail?: Array<{ x: number; y: number }>;
  /** Extra tooltip lines under the default read-out. */
  hoverLines?: string[];
};

const VB_W = 1000;
const PAD = { t: 22, r: 26, b: 30, l: 34 };

export function RrgScatter({
  points,
  selected,
  onSelect,
  xCaption = "relative strength →",
  yCaption = "↑ momentum",
  quadrantSubs = false,
  labelPolicy = "all",
  sizeCaption = "bubble size = magnitude",
  vbHeight = 620,
  minDomain,
  emptyText = "Awaiting rotation data…",
}: {
  points: RrgScatterPoint[];
  selected?: string | null;
  onSelect?: (key: string) => void;
  xCaption?: string;
  yCaption?: string;
  /** Show the explanatory sub-line under each quadrant title. */
  quadrantSubs?: boolean;
  /** "all" labels every bubble; "highlight" labels only ring/hover/selected. */
  labelPolicy?: "all" | "highlight";
  sizeCaption?: string;
  vbHeight?: number;
  /** Floor for the symmetric axis domains (data units). */
  minDomain?: { x?: number; y?: number };
  emptyText?: string;
}) {
  const [hover, setHover] = useState<RrgScatterPoint | null>(null);
  const VB_H = vbHeight;

  const model = useMemo(() => {
    if (!points.length) return null;
    const xs = points.flatMap((p) => [p.x, ...(p.tail || []).map((t) => t.x)]);
    const ys = points.flatMap((p) => [p.y, ...(p.tail || []).map((t) => t.y)]);
    // Symmetric bounds around 0 so the crosshair sits at the true centre.
    const xAbs = Math.max(minDomain?.x ?? 1e-6, ...xs.map((v) => Math.abs(v))) * 1.18;
    const yAbs = Math.max(minDomain?.y ?? 1e-6, ...ys.map((v) => Math.abs(v))) * 1.18;
    const xOf = (v: number) => PAD.l + ((v + xAbs) / (2 * xAbs)) * (VB_W - PAD.l - PAD.r);
    const yOf = (v: number) => PAD.t + (1 - (v + yAbs) / (2 * yAbs)) * (VB_H - PAD.t - PAD.b);
    const sMax = Math.max(1e-6, ...points.map((p) => Math.abs(p.size) || 0));
    const rOf = (s: number) => 6 + (Math.sqrt(Math.abs(s) / sMax) || 0) * 16;
    return { xOf, yOf, rOf };
  }, [points, minDomain?.x, minDomain?.y, VB_H]);

  if (!model || !points.length) {
    return (
      <div className="flex h-[340px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        {emptyText}
      </div>
    );
  }

  const { xOf, yOf, rOf } = model;
  const cx = xOf(0);
  const cy = yOf(0);

  // Sort so big/selected bubbles draw last (on top).
  const drawOrder = [...points].sort((a, b) => {
    if (a.key === selected) return 1;
    if (b.key === selected) return -1;
    return Math.abs(a.size) - Math.abs(b.size);
  });

  const showLabel = (p: RrgScatterPoint) =>
    labelPolicy === "all" || p.ring || p.key === selected || hover?.key === p.key;

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* quadrant fills */}
        <rect x={cx} y={PAD.t} width={VB_W - PAD.r - cx} height={cy - PAD.t} fill={CHART.green} opacity={0.05} />
        <rect x={PAD.l} y={PAD.t} width={cx - PAD.l} height={cy - PAD.t} fill={CHART.blue} opacity={0.05} />
        <rect x={PAD.l} y={cy} width={cx - PAD.l} height={VB_H - PAD.b - cy} fill={CHART.red} opacity={0.05} />
        <rect x={cx} y={cy} width={VB_W - PAD.r - cx} height={VB_H - PAD.b - cy} fill={CHART.amber} opacity={0.05} />

        {/* crosshair axes */}
        <line x1={cx} y1={PAD.t} x2={cx} y2={VB_H - PAD.b} stroke={CHART.axis} strokeWidth={0.8} strokeDasharray="4 4" />
        <line x1={PAD.l} y1={cy} x2={VB_W - PAD.r} y2={cy} stroke={CHART.axis} strokeWidth={0.8} strokeDasharray="4 4" />

        {/* quadrant labels */}
        <QuadLabel x={VB_W - PAD.r - 8} y={PAD.t + 16} anchor="end" color={CHART.green} title="LEADING" sub={quadrantSubs ? "strong · accelerating" : undefined} />
        <QuadLabel x={PAD.l + 8} y={PAD.t + 16} anchor="start" color={CHART.blue} title="IMPROVING" sub={quadrantSubs ? "weak · accelerating" : undefined} />
        <QuadLabel x={PAD.l + 8} y={VB_H - PAD.b - 18} anchor="start" color={CHART.red} title="LAGGING" sub={quadrantSubs ? "weak · decelerating" : undefined} />
        <QuadLabel x={VB_W - PAD.r - 8} y={VB_H - PAD.b - 18} anchor="end" color={CHART.amber} title="WEAKENING" sub={quadrantSubs ? "strong · decelerating" : undefined} />

        {/* axis captions */}
        <text x={VB_W - PAD.r} y={cy - 6} fill={CHART.muted} fontSize={10} textAnchor="end">{xCaption}</text>
        <text x={cx + 6} y={PAD.t + 10} fill={CHART.muted} fontSize={10} textAnchor="start">{yCaption}</text>

        {/* rotation tails */}
        {drawOrder.map((p) => {
          if (!p.tail?.length) return null;
          const pts = [...p.tail, { x: p.x, y: p.y }];
          const d = pts.map((t, i) => `${i === 0 ? "M" : "L"}${xOf(t.x).toFixed(1)},${yOf(t.y).toFixed(1)}`).join(" ");
          return <path key={`tail-${p.key}`} d={d} fill="none" stroke={quadrantColor(p.quadrant)} strokeWidth={1} opacity={0.35} strokeDasharray="2 3" />;
        })}

        {/* bubbles */}
        {drawOrder.map((p) => {
          const x = xOf(p.x);
          const y = yOf(p.y);
          const r = rOf(p.size);
          const col = quadrantColor(p.quadrant);
          const isSel = p.key === selected;
          const isHover = hover?.key === p.key;
          return (
            <g
              key={p.key}
              onMouseEnter={() => setHover(p)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onSelect?.(p.key)}
              style={{ cursor: onSelect ? "pointer" : "default" }}
            >
              <circle
                cx={x}
                cy={y}
                r={r}
                fill={col}
                fillOpacity={isSel || isHover ? 0.4 : 0.22}
                stroke={col}
                strokeWidth={isSel || isHover ? 2.2 : p.ring ? 1.8 : 1.1}
              />
              <circle cx={x} cy={y} r={2} fill={col} />
              {showLabel(p) ? (
                <text
                  x={x}
                  y={y - r - 4}
                  fill={isSel || isHover ? "#e6edf3" : CHART.muted}
                  fontSize={isSel ? 11 : 9.5}
                  textAnchor="middle"
                  fontWeight={isSel || p.ring ? 700 : 500}
                >
                  {p.label}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute right-3 top-3 rounded-lg border px-3 py-2 text-[11px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <div className="mb-1 font-sans text-[12px] font-semibold" style={{ color: quadrantColor(hover.quadrant) }}>
            {hover.title || hover.label}
          </div>
          <div className="text-text-muted">{hover.quadrant.toUpperCase()}</div>
          {(hover.hoverLines?.length
            ? hover.hoverLines
            : [`x ${hover.x.toFixed(2)} · y ${hover.y.toFixed(2)}`, `size ${hover.size.toFixed(2)}`]
          ).map((line) => (
            <div key={line}>{line}</div>
          ))}
        </div>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="leading" />
        <Legend color={CHART.blue} label="improving" />
        <Legend color={CHART.amber} label="weakening" />
        <Legend color={CHART.red} label="lagging" />
        <span className="text-text-muted/70">{sizeCaption}</span>
      </div>
    </div>
  );
}

function QuadLabel({ x, y, anchor, color, title, sub }: { x: number; y: number; anchor: "start" | "end"; color: string; title: string; sub?: string }) {
  return (
    <g>
      <text x={x} y={y} fill={color} fontSize={12} fontWeight={700} textAnchor={anchor} opacity={0.85}>{title}</text>
      {sub ? <text x={x} y={y + 12} fill={CHART.muted} fontSize={8.5} textAnchor={anchor}>{sub}</text> : null}
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
