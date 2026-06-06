"use client";

/**
 * Relative-Rotation-Graph (RRG) scatter — the CBE desk's signature viz.
 *
 * The alpha engine classifies every name into one of four RRG quadrants
 * (leading / improving / weakening / lagging) from its relative-strength
 * percentage and momentum. We plot each candidate as a dot positioned by
 *   x = relative-strength %  (RS vs Nifty50, → right = stronger)
 *   y = momentum proxy       (MACD histogram, → up = accelerating)
 * over the canonical 2×2 quadrant backdrop. Bubble size encodes the
 * composite alpha score; colour encodes the quadrant. Pure inline SVG
 * (viewBox-scaled) so it stays crisp at any width — the GannChart idiom.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type RrgPoint = {
  symbol: string;
  rs: number;        // x — relative strength %
  momentum: number;  // y — momentum proxy (MACD hist)
  score: number;     // bubble size — composite alpha 0..100
  quadrant: string;  // colour
  watchlist?: boolean;
};

const VB_W = 1000;
const VB_H = 520;
const PAD = { t: 18, r: 18, b: 34, l: 44 };

export const QUADRANT_COLOR: Record<string, string> = {
  leading: CHART.green,
  improving: CHART.blue,
  weakening: CHART.amber,
  lagging: CHART.red,
};

const QUADRANT_LABEL: Record<string, { text: string; corner: "tr" | "tl" | "br" | "bl" }> = {
  leading: { text: "LEADING", corner: "tr" },
  improving: { text: "IMPROVING", corner: "tl" },
  weakening: { text: "WEAKENING", corner: "br" },
  lagging: { text: "LAGGING", corner: "bl" },
};

export function RrgScatter({ points }: { points: RrgPoint[] }) {
  const [hover, setHover] = useState<RrgPoint | null>(null);

  const model = useMemo(() => {
    if (!points.length) return null;
    // Symmetric domains so the zero-cross lines sit dead-centre — that's
    // what makes the four quadrants read cleanly.
    const maxRs = Math.max(4, ...points.map((p) => Math.abs(p.rs)));
    const maxMom = Math.max(0.4, ...points.map((p) => Math.abs(p.momentum)));
    const rsDom = maxRs * 1.15;
    const momDom = maxMom * 1.15;
    const maxScore = Math.max(1, ...points.map((p) => p.score));

    const xOf = (rs: number) => PAD.l + ((rs + rsDom) / (2 * rsDom)) * (VB_W - PAD.l - PAD.r);
    const yOf = (m: number) => PAD.t + (1 - (m + momDom) / (2 * momDom)) * (VB_H - PAD.t - PAD.b);
    const rOf = (score: number) => 5 + (score / maxScore) * 13;
    return { rsDom, momDom, xOf, yOf, rOf };
  }, [points]);

  if (!model || !points.length) {
    return (
      <div className="flex h-[320px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        Awaiting RRG candidates…
      </div>
    );
  }

  const { xOf, yOf, rOf } = model;
  const cx = xOf(0);
  const cy = yOf(0);
  const left = PAD.l;
  const right = VB_W - PAD.r;
  const top = PAD.t;
  const bottom = VB_H - PAD.b;

  const cornerXY = (corner: "tr" | "tl" | "br" | "bl") => {
    const m = 8;
    if (corner === "tr") return { x: right - m, y: top + 14, anchor: "end" as const };
    if (corner === "tl") return { x: left + m, y: top + 14, anchor: "start" as const };
    if (corner === "br") return { x: right - m, y: bottom - 6, anchor: "end" as const };
    return { x: left + m, y: bottom - 6, anchor: "start" as const };
  };

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* quadrant fills */}
        <rect x={cx} y={top} width={right - cx} height={cy - top} fill={CHART.green} opacity={0.05} />
        <rect x={left} y={top} width={cx - left} height={cy - top} fill={CHART.blue} opacity={0.05} />
        <rect x={cx} y={cy} width={right - cx} height={bottom - cy} fill={CHART.amber} opacity={0.05} />
        <rect x={left} y={cy} width={cx - left} height={bottom - cy} fill={CHART.red} opacity={0.05} />

        {/* zero-cross axes */}
        <line x1={cx} x2={cx} y1={top} y2={bottom} stroke={CHART.border} strokeWidth={1} strokeDasharray="3 4" />
        <line x1={left} x2={right} y1={cy} y2={cy} stroke={CHART.border} strokeWidth={1} strokeDasharray="3 4" />

        {/* frame */}
        <rect x={left} y={top} width={right - left} height={bottom - top} fill="none" stroke={CHART.grid} strokeWidth={1} />

        {/* quadrant labels */}
        {Object.entries(QUADRANT_LABEL).map(([q, { text, corner }]) => {
          const { x, y, anchor } = cornerXY(corner);
          return (
            <text key={q} x={x} y={y} fill={QUADRANT_COLOR[q]} fontSize={11} fontWeight={700} textAnchor={anchor} opacity={0.65} letterSpacing={1.5}>
              {text}
            </text>
          );
        })}

        {/* axis titles */}
        <text x={(left + right) / 2} y={VB_H - 6} fill={CHART.axis} fontSize={10} textAnchor="middle">
          Relative strength vs Nifty50 (%) →
        </text>
        <text x={12} y={(top + bottom) / 2} fill={CHART.axis} fontSize={10} textAnchor="middle" transform={`rotate(-90 12 ${(top + bottom) / 2})`}>
          ↑ Momentum (MACD histogram)
        </text>

        {/* bubbles */}
        {points.map((p, i) => {
          const x = xOf(p.rs);
          const y = yOf(p.momentum);
          const r = rOf(p.score);
          const col = QUADRANT_COLOR[p.quadrant] || CHART.muted;
          const isHover = hover?.symbol === p.symbol;
          return (
            <g key={`${p.symbol}-${i}`} onMouseEnter={() => setHover(p)} onMouseLeave={() => setHover(null)} style={{ cursor: "pointer" }}>
              <circle cx={x} cy={y} r={r} fill={col} opacity={isHover ? 0.55 : 0.28} stroke={col} strokeWidth={p.watchlist ? 1.8 : 0.9} />
              {p.watchlist || isHover ? (
                <text x={x} y={y - r - 3} fill={isHover ? "#e6edf3" : col} fontSize={9} textAnchor="middle" fontWeight={p.watchlist ? 600 : 400}>
                  {p.symbol}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute left-2 top-2 rounded-lg border px-2.5 py-1.5 text-[10.5px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <div className="text-text-primary font-semibold">{hover.symbol}</div>
          <div className="text-text-muted" style={{ color: QUADRANT_COLOR[hover.quadrant] }}>{hover.quadrant}</div>
          <div>RS {hover.rs.toFixed(2)}% · mom {hover.momentum.toFixed(3)}</div>
          <div>alpha {hover.score.toFixed(1)}</div>
        </div>
      ) : null}

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={QUADRANT_COLOR.leading} label="leading" />
        <Legend color={QUADRANT_COLOR.improving} label="improving" />
        <Legend color={QUADRANT_COLOR.weakening} label="weakening" />
        <Legend color={QUADRANT_COLOR.lagging} label="lagging" />
        <span className="text-text-muted">· bubble size = composite alpha · ring = on watchlist</span>
      </div>
    </div>
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
