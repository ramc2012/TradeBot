"use client";

/**
 * Tiny inline-SVG price sparkline. Used in the alpha-candidate table to
 * show each name's recent_closes_30d at a glance. Coloured by net move,
 * with an end-dot marking the latest close. No charting dependency —
 * stays crisp at any size and renders dozens per page cheaply.
 */
import { CHART } from "../shared/chartTheme";

export function Sparkline({
  values,
  width = 96,
  height = 26,
  strokeColor,
}: {
  values?: number[] | null;
  width?: number;
  height?: number;
  strokeColor?: string;
}) {
  const series = (values || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (series.length < 2) {
    return <div className="text-[10px] text-text-muted">—</div>;
  }

  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const stepX = width / (series.length - 1);
  const up = series[series.length - 1] >= series[0];
  const color = strokeColor || (up ? CHART.green : CHART.red);

  const pad = 2;
  const usableH = height - pad * 2;
  const points = series.map((v, i) => {
    const x = i * stepX;
    const y = pad + (1 - (v - min) / span) * usableH;
    return [x, y] as const;
  });
  const path = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = `${path} L${width} ${height} L0 ${height} Z`;
  const [lastX, lastY] = points[points.length - 1];

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="overflow-visible">
      <path d={area} fill={color} opacity={0.09} />
      <path d={path} fill="none" stroke={color} strokeWidth={1.2} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r={1.7} fill={color} />
    </svg>
  );
}
