"use client";

/**
 * Strike × time canvas heatmap for the GEX progression (gamma density / OI).
 * Rows = strikes (high at top), cols = 30-min buckets. Handles many columns.
 */
import { useEffect, useRef } from "react";

type Props = {
  title: string;
  strikes: number[];
  times: string[];
  matrix: Array<Array<number | null>>; // [strikeRow][timeCol]
  atm?: number | null;
  diverging?: boolean; // true → red/blue around 0; false → sequential intensity
};

function lerp(a: number, b: number, t: number) {
  return Math.round(a + (b - a) * t);
}

function seqColor(t: number): string {
  // dark teal → green → amber (low→high)
  const stops = [
    [15, 23, 36],
    [0, 120, 110],
    [0, 212, 163],
    [245, 200, 66],
  ];
  const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = stops[i];
  const b = stops[Math.min(i + 1, stops.length - 1)];
  return `rgb(${lerp(a[0], b[0], f)},${lerp(a[1], b[1], f)},${lerp(a[2], b[2], f)})`;
}

function divColor(t: number): string {
  // t in [-1,1]: red (neg) → dark → green (pos)
  if (t >= 0) return `rgb(${lerp(15, 0, t)},${lerp(23, 212, t)},${lerp(36, 163, t)})`;
  const u = -t;
  return `rgb(${lerp(15, 255, u)},${lerp(23, 71, u)},${lerp(36, 87, u)})`;
}

export default function GexHeatmap({ title, strikes, times, matrix, atm, diverging }: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || !strikes.length || !times.length) return;
    const dpr = window.devicePixelRatio || 1;
    const padL = 52;
    const padB = 38;
    const padT = 6;
    const cssW = canvas.clientWidth || 640;
    const cellW = Math.max(3, Math.floor((cssW - padL - 4) / times.length));
    const cellH = Math.max(8, Math.min(22, Math.floor(220 / strikes.length)));
    const cssH = padT + strikes.length * cellH + padB;

    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.height = `${cssH}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    // value range (ignore nulls)
    let lo = Infinity;
    let hi = -Infinity;
    for (const row of matrix)
      for (const v of row) {
        if (v == null || Number.isNaN(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    if (!Number.isFinite(lo)) {
      lo = 0;
      hi = 1;
    }
    const absMax = Math.max(Math.abs(lo), Math.abs(hi)) || 1;

    // strikes high→low top→bottom
    const rows = strikes.map((s, i) => ({ s, i })).sort((a, b) => b.s - a.s);
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "middle";

    rows.forEach((r, rowIdx) => {
      const y = padT + rowIdx * cellH;
      const seriesRow = matrix[r.i] || [];
      for (let c = 0; c < times.length; c++) {
        const v = seriesRow[c];
        const x = padL + c * cellW;
        if (v == null || Number.isNaN(v)) {
          ctx.fillStyle = "rgba(148,163,184,0.06)";
        } else if (diverging) {
          ctx.fillStyle = divColor(v / absMax);
        } else {
          ctx.fillStyle = seqColor(hi === lo ? 0.5 : (v - lo) / (hi - lo));
        }
        ctx.fillRect(x, y, cellW - 1, cellH - 1);
      }
      // strike label + ATM marker
      ctx.fillStyle = atm != null && r.s === atm ? "#3b82f6" : "#94a3b8";
      ctx.textAlign = "right";
      ctx.fillText(String(r.s), padL - 6, y + cellH / 2);
    });

    // time ticks (~6)
    ctx.fillStyle = "#94a3b8";
    ctx.textAlign = "center";
    const step = Math.max(1, Math.ceil(times.length / 6));
    for (let c = 0; c < times.length; c += step) {
      const x = padL + c * cellW + cellW / 2;
      ctx.save();
      ctx.translate(x, padT + rows.length * cellH + 4);
      ctx.rotate(-Math.PI / 4);
      ctx.textAlign = "right";
      ctx.fillText(times[c], 0, 0);
      ctx.restore();
    }
  }, [strikes, times, matrix, atm, diverging]);

  return (
    <div>
      <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{title}</div>
      <canvas ref={ref} className="w-full" />
    </div>
  );
}
