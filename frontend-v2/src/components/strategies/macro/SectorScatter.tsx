"use client";

/**
 * Sector health-vs-risk map — a bespoke pure-SVG bubble scatter.
 *
 *   x = risk_score (0..100, left = safe, right = risky)
 *   y = health_score (0..100, bottom = weak, top = strong)
 *   bubble radius ~ |trend_score - 50| (momentum magnitude)
 *   bubble colour = net macro tailwind (tailwinds - headwinds): green = tailwind,
 *                   amber = neutral, red = headwind
 *
 * The "sweet spot" (high health, low risk) is the top-left quadrant, shaded
 * green. The danger quadrant (low health, high risk) is bottom-right, shaded
 * red. This is the innovative-viz idiom borrowed from GannChart — viewBox
 * scaled so it stays crisp at any width, no charting dependency.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type ScatterSector = {
  code: string;
  label: string;
  health_score?: number;
  risk_score?: number;
  trend_score?: number;
  stage?: string;
  macro_tailwinds?: number;
  macro_headwinds?: number;
};

const VB_W = 1000;
const VB_H = 560;
const PAD = { t: 24, r: 24, b: 44, l: 52 };

const netTone = (net: number) => (net > 0 ? CHART.green : net < 0 ? CHART.red : CHART.amber);

export function SectorScatter({
  sectors,
  selected,
  onSelect,
}: {
  sectors: ScatterSector[];
  selected?: string | null;
  onSelect?: (code: string) => void;
}) {
  const [hover, setHover] = useState<ScatterSector | null>(null);

  const xOf = (v: number) => PAD.l + (clamp01(v) / 100) * (VB_W - PAD.l - PAD.r);
  const yOf = (v: number) => PAD.t + (1 - clamp01(v) / 100) * (VB_H - PAD.t - PAD.b);

  const nodes = useMemo(
    () =>
      sectors.map((s) => {
        const risk = s.risk_score ?? 50;
        const health = s.health_score ?? 50;
        const trend = s.trend_score ?? 50;
        const net = (s.macro_tailwinds ?? 0) - (s.macro_headwinds ?? 0);
        const r = 9 + Math.min(22, Math.abs(trend - 50) * 0.6);
        return { s, x: xOf(risk), y: yOf(health), r, net, trend };
      }),
    [sectors],
  );

  if (!sectors.length) {
    return (
      <div className="flex h-[320px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        Awaiting sector map…
      </div>
    );
  }

  const midX = xOf(50);
  const midY = yOf(50);

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* quadrant shading: top-left = sweet spot, bottom-right = danger */}
        <rect x={PAD.l} y={PAD.t} width={midX - PAD.l} height={midY - PAD.t} fill="rgba(0,212,163,0.06)" />
        <rect x={midX} y={midY} width={VB_W - PAD.r - midX} height={VB_H - PAD.b - midY} fill="rgba(255,71,87,0.06)" />

        {/* grid lines at 25/50/75 */}
        {[25, 50, 75].map((g) => (
          <g key={`gx-${g}`}>
            <line x1={xOf(g)} x2={xOf(g)} y1={PAD.t} y2={VB_H - PAD.b} stroke={CHART.grid} strokeWidth={g === 50 ? 1.1 : 0.6} strokeDasharray={g === 50 ? undefined : "3 5"} />
            <text x={xOf(g)} y={VB_H - PAD.b + 16} fill={CHART.axis} fontSize={9} textAnchor="middle">{g}</text>
          </g>
        ))}
        {[25, 50, 75].map((g) => (
          <g key={`gy-${g}`}>
            <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(g)} y2={yOf(g)} stroke={CHART.grid} strokeWidth={g === 50 ? 1.1 : 0.6} strokeDasharray={g === 50 ? undefined : "3 5"} />
            <text x={PAD.l - 8} y={yOf(g) + 3} fill={CHART.axis} fontSize={9} textAnchor="end">{g}</text>
          </g>
        ))}

        {/* axis labels */}
        <text x={(PAD.l + VB_W - PAD.r) / 2} y={VB_H - 6} fill={CHART.muted} fontSize={11} textAnchor="middle">Risk score  →  riskier</text>
        <text x={14} y={(PAD.t + VB_H - PAD.b) / 2} fill={CHART.muted} fontSize={11} textAnchor="middle" transform={`rotate(-90 14 ${(PAD.t + VB_H - PAD.b) / 2})`}>Health score  →  stronger</text>

        {/* quadrant captions */}
        <text x={PAD.l + 8} y={PAD.t + 14} fill="rgba(0,212,163,0.55)" fontSize={10} fontWeight={600}>SWEET SPOT</text>
        <text x={VB_W - PAD.r - 8} y={VB_H - PAD.b - 8} fill="rgba(255,71,87,0.55)" fontSize={10} fontWeight={600} textAnchor="end">DANGER</text>

        {/* bubbles */}
        {nodes.map((n) => {
          const col = netTone(n.net);
          const isSel = selected === n.s.code;
          return (
            <g
              key={n.s.code}
              onMouseEnter={() => setHover(n.s)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onSelect?.(n.s.code)}
              style={{ cursor: onSelect ? "pointer" : "default" }}
            >
              <circle cx={n.x} cy={n.y} r={n.r} fill={col} fillOpacity={isSel ? 0.42 : 0.2} stroke={col} strokeWidth={isSel ? 2.2 : 1.2} />
              <text x={n.x} y={n.y - n.r - 4} fill="#e6edf3" fontSize={9.5} textAnchor="middle" opacity={isSel || hover?.code === n.s.code ? 1 : 0.78}>
                {n.s.code}
              </text>
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute left-2 top-2 rounded-lg border px-3 py-2 text-[11px]"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <div className="font-semibold text-text-primary">{hover.label}</div>
          <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-text-secondary">
            <span>Health</span><span className="text-right">{(hover.health_score ?? 0).toFixed(1)}</span>
            <span>Risk</span><span className="text-right">{(hover.risk_score ?? 0).toFixed(1)}</span>
            <span>Trend</span><span className="text-right">{(hover.trend_score ?? 0).toFixed(1)}</span>
            <span>Stage</span><span className="text-right">{hover.stage || "—"}</span>
            <span>Tail/Head</span><span className="text-right">{hover.macro_tailwinds ?? 0}/{hover.macro_headwinds ?? 0}</span>
          </div>
        </div>
      ) : null}

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="net macro tailwind" />
        <Legend color={CHART.amber} label="macro neutral" />
        <Legend color={CHART.red} label="net macro headwind" />
        <span className="text-text-muted/70">bubble size = trend momentum · click to load playbook</span>
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

function clamp01(v: number) {
  if (Number.isNaN(v)) return 0;
  return Math.max(0, Math.min(100, v));
}
