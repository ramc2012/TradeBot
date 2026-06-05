"use client";

/**
 * VAR / Granger causal-network diagram. Sectors are laid out on a ring;
 * directed edges (source leads target) are drawn as curved arrows whose
 * thickness encodes the Granger weight and whose opacity encodes
 * significance (lower p-value = bolder). Node radius + colour encode net
 * influence (leader = green, follower = red). Pure SVG.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type NetNode = {
  id: string;
  label: string;
  net_influence?: number;
  outgoing_edges?: number;
  incoming_edges?: number;
};
export type NetEdge = {
  source: string;
  target: string;
  p_value?: number | null;
  weight?: number;
  lag?: number;
  relationship?: string;
};

const VB = 620;
const C = VB / 2;
const R = 232;

export function SectorNetwork({ nodes, edges }: { nodes: NetNode[]; edges: NetEdge[] }) {
  const [hover, setHover] = useState<NetEdge | null>(null);

  const model = useMemo(() => {
    if (!nodes.length) return null;
    const pos = new Map<string, { x: number; y: number; a: number }>();
    nodes.forEach((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2 - Math.PI / 2;
      pos.set(n.id, { x: C + R * Math.cos(a), y: C + R * Math.sin(a), a });
    });
    const wMax = Math.max(1e-6, ...edges.map((e) => Math.abs(e.weight ?? 0)));
    const infMax = Math.max(1e-6, ...nodes.map((n) => Math.abs(n.net_influence ?? 0)));
    return { pos, wMax, infMax };
  }, [nodes, edges]);

  if (!model || !nodes.length) {
    return (
      <div className="flex h-[320px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        No causal edges available — VAR/Granger requires a connected sector-index history feed.
      </div>
    );
  }
  const { pos, wMax, infMax } = model;

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB} ${VB}`} className="mx-auto block w-full max-w-[620px]" style={{ aspectRatio: "1 / 1" }}>
        <defs>
          <marker id="sec-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill={CHART.violet} />
          </marker>
        </defs>

        {/* edges */}
        {edges.map((e, i) => {
          const s = pos.get(e.source);
          const t = pos.get(e.target);
          if (!s || !t) return null;
          const w = 0.8 + (Math.abs(e.weight ?? 0) / wMax) * 5;
          const sig = e.p_value == null ? 0.55 : Math.max(0.25, 1 - Math.min(1, e.p_value / 0.05) * 0.7);
          // curve via the centre for a clean arc
          const mx = (s.x + t.x) / 2 + (C - (s.x + t.x) / 2) * 0.35;
          const my = (s.y + t.y) / 2 + (C - (s.y + t.y) / 2) * 0.35;
          const isHover = hover === e;
          return (
            <path
              key={`e-${i}`}
              d={`M${s.x.toFixed(1)},${s.y.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${t.x.toFixed(1)},${t.y.toFixed(1)}`}
              fill="none"
              stroke={CHART.violet}
              strokeWidth={isHover ? w + 1.5 : w}
              strokeOpacity={isHover ? 0.95 : sig}
              markerEnd="url(#sec-arrow)"
              onMouseEnter={() => setHover(e)}
              onMouseLeave={() => setHover(null)}
              style={{ cursor: "pointer" }}
            />
          );
        })}

        {/* nodes */}
        {nodes.map((n) => {
          const p = pos.get(n.id)!;
          const inf = n.net_influence ?? 0;
          const r = 6 + (Math.abs(inf) / infMax) * 9;
          const col = inf > 0.0001 ? CHART.green : inf < -0.0001 ? CHART.red : CHART.muted;
          const right = Math.cos(p.a) >= 0;
          const lx = p.x + Math.cos(p.a) * (r + 6);
          const ly = p.y + Math.sin(p.a) * (r + 6);
          return (
            <g key={n.id}>
              <circle cx={p.x} cy={p.y} r={r} fill={col} fillOpacity={0.25} stroke={col} strokeWidth={1.4} />
              <text
                x={lx}
                y={ly + 3}
                fill="#c9d4e0"
                fontSize={10.5}
                textAnchor={right ? "start" : "end"}
              >
                {n.label}
              </text>
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div
          className="pointer-events-none absolute left-1/2 top-2 -translate-x-1/2 rounded-lg border px-3 py-1.5 text-[11px]"
          style={{ background: CHART.surface, borderColor: CHART.border }}
        >
          <span className="font-semibold text-text-primary">{hover.relationship || `${hover.source} → ${hover.target}`}</span>
          <span className="ml-2 font-mono text-text-muted">
            w {Number(hover.weight ?? 0).toFixed(2)} · lag {hover.lag ?? "—"}
            {hover.p_value != null ? ` · p ${Number(hover.p_value).toFixed(3)}` : ""}
          </span>
        </div>
      ) : null}

      <div className="mt-1 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="net leader" />
        <Legend color={CHART.red} label="net follower" />
        <span className="text-text-muted/70">arrow = leads · thickness = weight · opacity = significance</span>
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
