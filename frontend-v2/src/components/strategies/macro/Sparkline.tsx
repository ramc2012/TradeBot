"use client";

/**
 * Tiny inline-SVG sparkline — no charting dependency, viewBox-scaled so it
 * stays crisp at any width. Used by the macro-indicator tape (multi-year
 * history) and the commodity-pressure board (price trend).
 */
import { useMemo } from "react";

export function Sparkline({
  values,
  width = 120,
  height = 32,
  color = "var(--accent-blue)",
  fill = true,
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
  fill?: boolean;
}) {
  const { line, area } = useMemo(() => {
    const pts = values.filter((v) => Number.isFinite(v));
    if (pts.length < 2) return { line: "", area: "" };
    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const span = max - min || 1;
    const pad = 2;
    const innerW = width - pad * 2;
    const innerH = height - pad * 2;
    const step = innerW / (pts.length - 1);
    const xy = pts.map((v, i) => {
      const x = pad + i * step;
      const y = pad + (1 - (v - min) / span) * innerH;
      return [x, y] as const;
    });
    const line = xy.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const area =
      `${line} L${xy[xy.length - 1][0].toFixed(1)},${(height - pad).toFixed(1)} L${xy[0][0].toFixed(1)},${(height - pad).toFixed(1)} Z`;
    return { line, area };
  }, [values, width, height]);

  if (!line) {
    return <div style={{ width, height }} className="rounded bg-bg-primary/20" />;
  }

  const gid = `spark-${color.replace(/[^a-z0-9]/gi, "")}`;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.28} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {fill ? <path d={area} fill={`url(#${gid})`} /> : null}
      <path d={line} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
