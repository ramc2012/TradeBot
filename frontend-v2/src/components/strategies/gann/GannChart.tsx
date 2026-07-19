"use client";

/**
 * Gann geometry chart — candlesticks with Gann angle-fans projected from
 * the anchor pivot, Square-of-9 price levels, and time-cycle bands. Pure
 * SVG (viewBox-scaled) so it stays crisp at any width without a charting
 * dependency that can't draw Gann fans.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";

export type GannBar = { index: number; time: string; open: number; high: number; low: number; close: number };
export type GannAngle = {
  name: string;
  direction: string;
  anchor_price: number;
  anchor_bar_index: number;
  slope: number;
  projected_price?: number;
  distance_pct?: number;
};
export type Sq9Level = { degree: number; direction: string; price: number; level_type: string; distance_pct?: number };
export type TimeCycle = { cycle: number; start_bar_index: number; center_bar_index: number; end_bar_index: number; active?: boolean };
export type GannAnchor = { bar_index: number; price: number; kind?: string; strength?: string };
export type GannTradePlan = {
  trigger?: number | null;
  stop?: number | null;
  targets?: number[] | null;
};

const VB_W = 1000;
const VB_H = 460;
const PAD = { t: 16, r: 64, b: 24, l: 8 };

export function GannChart({
  bars,
  angles = [],
  sq9 = [],
  cycles = [],
  anchor,
  spot,
  tradePlan,
}: {
  bars: GannBar[];
  angles?: GannAngle[];
  sq9?: Sq9Level[];
  cycles?: TimeCycle[];
  anchor?: GannAnchor | null;
  spot?: number | null;
  tradePlan?: GannTradePlan | null;
}) {
  const [hover, setHover] = useState<GannBar | null>(null);

  const model = useMemo(() => {
    if (!bars.length) return null;
    const idxMin = bars[0].index;
    const idxMax = bars[bars.length - 1].index;
    // Extend right so projected angle lines + active cycles have room.
    const projBars = Math.max(12, Math.round((idxMax - idxMin) * 0.12));
    const xMin = idxMin;
    const xMax = idxMax + projBars;

    const planPrices = [
      tradePlan?.trigger,
      tradePlan?.stop,
      ...(tradePlan?.targets || []),
    ].filter((value): value is number => typeof value === "number" && Number.isFinite(value));
    let pLow = Math.min(...bars.map((b) => b.low), ...planPrices);
    let pHigh = Math.max(...bars.map((b) => b.high), ...planPrices);
    const padP = (pHigh - pLow) * 0.08 || 1;
    pLow -= padP;
    pHigh += padP;

    const xOf = (i: number) => PAD.l + ((i - xMin) / (xMax - xMin || 1)) * (VB_W - PAD.l - PAD.r);
    const yOf = (p: number) => PAD.t + (1 - (p - pLow) / (pHigh - pLow || 1)) * (VB_H - PAD.t - PAD.b);
    const inRange = (p: number) => p >= pLow && p <= pHigh;
    const candleW = Math.max(1.2, ((VB_W - PAD.l - PAD.r) / (xMax - xMin)) * 0.62);

    return { idxMin, idxMax, xMin, xMax, pLow, pHigh, xOf, yOf, inRange, candleW };
  }, [bars, tradePlan]);

  if (!model || !bars.length) {
    return (
      <div className="flex h-[320px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        Awaiting Gann geometry…
      </div>
    );
  }
  const { xOf, yOf, inRange, candleW, xMax } = model;

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* time-cycle vertical bands */}
        {cycles.map((c, i) => {
          const x0 = xOf(c.start_bar_index);
          const x1 = xOf(c.end_bar_index);
          if (x1 < PAD.l || x0 > VB_W - PAD.r) return null;
          return (
            <g key={`cyc-${i}`}>
              <rect
                x={Math.max(PAD.l, x0)}
                y={PAD.t}
                width={Math.max(1.5, Math.min(VB_W - PAD.r, x1) - Math.max(PAD.l, x0))}
                height={VB_H - PAD.t - PAD.b}
                fill={c.active ? "rgba(167,139,250,0.12)" : "rgba(255,255,255,0.03)"}
              />
              <line x1={xOf(c.center_bar_index)} x2={xOf(c.center_bar_index)} y1={PAD.t} y2={VB_H - PAD.b}
                stroke={c.active ? CHART.violet : "rgba(255,255,255,0.10)"} strokeDasharray="2 4" strokeWidth={c.active ? 1.2 : 0.8} />
              {c.active ? (
                <text x={xOf(c.center_bar_index)} y={PAD.t + 10} fill={CHART.violet} fontSize={9} textAnchor="middle">
                  cycle {c.cycle}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* SQ9 horizontal levels */}
        {sq9.filter((l) => inRange(l.price)).map((l, i) => {
          const y = yOf(l.price);
          const cardinal = l.level_type === "cardinal";
          return (
            <g key={`sq9-${i}`}>
              <line x1={PAD.l} x2={VB_W - PAD.r} y1={y} y2={y}
                stroke={cardinal ? "rgba(255,165,2,0.45)" : "rgba(255,165,2,0.22)"}
                strokeWidth={cardinal ? 1 : 0.7} strokeDasharray={cardinal ? undefined : "3 4"} />
              <text x={VB_W - PAD.r + 3} y={y + 3} fill={CHART.amber} fontSize={8.5}>
                {l.degree}°
              </text>
            </g>
          );
        })}

        {/* Gann angle fans from the anchor */}
        {angles.map((a, i) => {
          const x0 = xOf(a.anchor_bar_index);
          const y0 = yOf(a.anchor_price);
          const xEnd = xMax;
          const pEnd = a.anchor_price + a.slope * (xEnd - a.anchor_bar_index);
          const yEnd = yOf(pEnd);
          const bull = a.direction === "bullish";
          return (
            <g key={`ang-${i}`}>
              <line x1={x0} y1={y0} x2={xOf(xEnd)} y2={yEnd}
                stroke={bull ? "rgba(0,212,163,0.42)" : "rgba(255,71,87,0.42)"} strokeWidth={0.9} />
              <text x={xOf(xEnd) - 2} y={Math.max(PAD.t + 8, Math.min(VB_H - PAD.b, yEnd)) - 2}
                fill={bull ? CHART.green : CHART.red} fontSize={8} textAnchor="end">
                {a.name}
              </text>
            </g>
          );
        })}

        {/* candlesticks */}
        {bars.map((b) => {
          const x = xOf(b.index);
          const up = b.close >= b.open;
          const col = up ? CHART.green : CHART.red;
          const yO = yOf(b.open);
          const yC = yOf(b.close);
          return (
            <g key={b.index} onMouseEnter={() => setHover(b)} onMouseLeave={() => setHover(null)}>
              <line x1={x} x2={x} y1={yOf(b.high)} y2={yOf(b.low)} stroke={col} strokeWidth={0.7} />
              <rect x={x - candleW / 2} y={Math.min(yO, yC)} width={candleW} height={Math.max(0.8, Math.abs(yC - yO))}
                fill={col} opacity={0.92} />
            </g>
          );
        })}

        {/* anchor marker */}
        {anchor ? (
          <g>
            <circle cx={xOf(anchor.bar_index)} cy={yOf(anchor.price)} r={3.4} fill={CHART.blue} stroke="#0d1117" strokeWidth={1} />
            <text x={xOf(anchor.bar_index) + 5} y={yOf(anchor.price) - 4} fill={CHART.blue} fontSize={8.5}>
              {anchor.kind || "anchor"}
            </text>
          </g>
        ) : null}

        {/* current spot line */}
        {spot != null && inRange(spot) ? (
          <g>
            <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(spot)} y2={yOf(spot)} stroke="rgba(255,255,255,0.55)" strokeWidth={0.7} strokeDasharray="4 3" />
            <text x={PAD.l + 3} y={yOf(spot) - 3} fill="#e6edf3" fontSize={9}>
              {spot.toFixed(1)}
            </text>
          </g>
        ) : null}

        {/* Actionable entry, invalidation and structural objectives. */}
        {tradePlan?.trigger != null && inRange(tradePlan.trigger) ? (
          <PlanLine y={yOf(tradePlan.trigger)} label={`ENTRY ${tradePlan.trigger.toFixed(1)}`} color={CHART.blue} />
        ) : null}
        {tradePlan?.stop != null && inRange(tradePlan.stop) ? (
          <PlanLine y={yOf(tradePlan.stop)} label={`STOP ${tradePlan.stop.toFixed(1)}`} color={CHART.red} />
        ) : null}
        {(tradePlan?.targets || []).filter(inRange).map((target, index) => (
          <PlanLine key={`target-${target}`} y={yOf(target)} label={`T${index + 1} ${target.toFixed(1)}`} color={CHART.green} />
        ))}
      </svg>

      {hover ? (
        <div className="pointer-events-none absolute right-2 top-2 rounded-lg border px-2.5 py-1.5 text-[10.5px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}>
          <div className="text-text-muted">{hover.time.replace("T", " ").slice(5, 16)}</div>
          <div>O {hover.open.toFixed(1)} H {hover.high.toFixed(1)}</div>
          <div>L {hover.low.toFixed(1)} C {hover.close.toFixed(1)}</div>
        </div>
      ) : null}

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="bullish fan" />
        <Legend color={CHART.red} label="bearish fan" />
        <Legend color={CHART.amber} label="SQ9 level" />
        <Legend color={CHART.violet} label="active cycle" />
        <Legend color={CHART.blue} label="anchor pivot" />
        {tradePlan?.trigger != null ? <Legend color={CHART.blue} label="entry" /> : null}
        {tradePlan?.stop != null ? <Legend color={CHART.red} label="invalidation" /> : null}
        {tradePlan?.targets?.length ? <Legend color={CHART.green} label="targets" /> : null}
      </div>
    </div>
  );
}

function PlanLine({ y, label, color }: { y: number; label: string; color: string }) {
  return (
    <g>
      <line x1={PAD.l} x2={VB_W - PAD.r} y1={y} y2={y} stroke={color} strokeWidth={1.35} strokeDasharray="7 3" />
      <rect x={VB_W - PAD.r - 66} y={y - 8} width={66} height={13} rx={2} fill="#0d1117" opacity={0.9} />
      <text x={VB_W - PAD.r - 3} y={y + 2} fill={color} fontSize={8.5} textAnchor="end">
        {label}
      </text>
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
