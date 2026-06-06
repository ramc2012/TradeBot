"use client";

/**
 * Sniper conviction map — a bespoke direction × confidence scatter.
 *
 * Pure SVG (viewBox-scaled, like GannChart) so it stays crisp at any
 * width. Each underlying is plotted as a bubble:
 *   x  → signed conviction = sign(direction) × confidence  (-1 left .. +1 right)
 *   y  → magnitude (favorable excursion, ATR units)         (0 bottom .. yMax top)
 *   r  → magnitude (bigger = more conviction in size terms)
 *   colour → direction (green long / red short / muted flat)
 *
 * The plot is split into a LONG half (right) and SHORT half (left); a
 * neutral dead-zone band straddles the centre for FLAT calls. Rows fade
 * with staleness so a trader can see at a glance which calls are fresh.
 */
import { useMemo, useState } from "react";

import { CHART } from "../shared/chartTheme";
import type { SniperRow } from "./types";

const VB_W = 1000;
const VB_H = 460;
const PAD = { t: 26, r: 22, b: 34, l: 46 };

function dirSign(d?: string): number {
  const s = String(d || "").toUpperCase();
  if (s === "LONG" || s === "UP" || s === "BULLISH") return 1;
  if (s === "SHORT" || s === "DOWN" || s === "BEARISH") return -1;
  return 0;
}

export function SniperQuadrant({ rows }: { rows: SniperRow[] }) {
  const [hover, setHover] = useState<SniperRow | null>(null);

  const model = useMemo(() => {
    const yMax = Math.max(1, ...rows.map((r) => Math.abs(r.magnitude_atr || 0))) * 1.15;
    const xOf = (signedConf: number) =>
      PAD.l + ((signedConf + 1) / 2) * (VB_W - PAD.l - PAD.r);
    const yOf = (mag: number) =>
      PAD.t + (1 - Math.min(1, mag / yMax)) * (VB_H - PAD.t - PAD.b);
    return { yMax, xOf, yOf };
  }, [rows]);

  const { xOf, yOf, yMax } = model;
  const xMid = xOf(0);
  const plotTop = PAD.t;
  const plotBot = VB_H - PAD.b;

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${VB_H}` }}>
        {/* half-plane fills */}
        <rect x={PAD.l} y={plotTop} width={xMid - PAD.l} height={plotBot - plotTop} fill="rgba(255,71,87,0.05)" />
        <rect x={xMid} y={plotTop} width={VB_W - PAD.r - xMid} height={plotBot - plotTop} fill="rgba(0,212,163,0.05)" />

        {/* neutral dead-zone band around centre */}
        <rect x={xOf(-0.18)} y={plotTop} width={xOf(0.18) - xOf(-0.18)} height={plotBot - plotTop} fill="rgba(255,255,255,0.025)" />

        {/* horizontal magnitude gridlines */}
        {[0.25, 0.5, 0.75, 1].map((f) => {
          const y = yOf(yMax * f);
          return (
            <g key={`g-${f}`}>
              <line x1={PAD.l} x2={VB_W - PAD.r} y1={y} y2={y} stroke={CHART.grid} strokeWidth={0.7} />
              <text x={PAD.l - 5} y={y + 3} fill={CHART.axis} fontSize={9} textAnchor="end">
                {(yMax * f).toFixed(1)}
              </text>
            </g>
          );
        })}

        {/* centre divider + axis labels */}
        <line x1={xMid} x2={xMid} y1={plotTop} y2={plotBot} stroke="rgba(255,255,255,0.22)" strokeWidth={1} strokeDasharray="4 4" />
        <text x={PAD.l + 6} y={plotTop + 13} fill={CHART.red} fontSize={10} fontWeight={600}>SHORT</text>
        <text x={VB_W - PAD.r - 6} y={plotTop + 13} fill={CHART.green} fontSize={10} fontWeight={600} textAnchor="end">LONG</text>
        <text x={xMid} y={plotBot + 22} fill={CHART.axis} fontSize={9} textAnchor="middle">← conviction (confidence × direction) →</text>
        <text x={PAD.l - 30} y={(plotTop + plotBot) / 2} fill={CHART.axis} fontSize={9} textAnchor="middle" transform={`rotate(-90 ${PAD.l - 30} ${(plotTop + plotBot) / 2})`}>
          magnitude (ATR)
        </text>

        {/* bubbles */}
        {rows.map((r) => {
          const sgn = dirSign(r.direction);
          const conf = Math.max(0, Math.min(1, r.confidence || 0));
          const signedConf = sgn * conf;
          const mag = Math.abs(r.magnitude_atr || 0);
          const cx = xOf(signedConf);
          const cy = yOf(mag);
          const col = sgn > 0 ? CHART.green : sgn < 0 ? CHART.red : CHART.muted;
          const rad = 6 + Math.min(22, (mag / yMax) * 22);
          const stale = (r.age_sec ?? 0) > 1800; // >30m
          const op = stale ? 0.4 : 0.9;
          const active = hover?.symbol === r.symbol;
          return (
            <g key={r.symbol} onMouseEnter={() => setHover(r)} onMouseLeave={() => setHover(null)} style={{ cursor: "pointer" }}>
              <circle cx={cx} cy={cy} r={rad} fill={col} opacity={op * 0.22} />
              <circle cx={cx} cy={cy} r={rad} fill="none" stroke={col} strokeWidth={active ? 2 : 1.2} opacity={op} />
              <circle cx={cx} cy={cy} r={2.2} fill={col} opacity={op} />
              <text x={cx} y={cy - rad - 4} fill="#e6edf3" fontSize={9.5} textAnchor="middle" opacity={op}>
                {r.symbol}
              </text>
            </g>
          );
        })}
      </svg>

      {hover ? (
        <div className="pointer-events-none absolute right-2 top-2 rounded-lg border px-2.5 py-1.5 text-[10.5px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}>
          <div className="text-text-primary">{hover.symbol} · {hover.direction}</div>
          <div className="text-text-muted">mag {Math.abs(hover.magnitude_atr || 0).toFixed(2)} ATR · conf {(hover.confidence || 0).toFixed(2)}</div>
          <div className="text-text-muted">horizon {hover.horizon || "—"} · {(hover.age_sec ?? 0) > 1800 ? "stale" : "fresh"}</div>
        </div>
      ) : null}

      {!rows.length ? (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-text-muted">
          No live sniper signals — the sidecar posts during market hours (09:30–15:30 IST).
        </div>
      ) : null}

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.green} label="long" />
        <Legend color={CHART.red} label="short" />
        <Legend color={CHART.muted} label="flat / stale" />
        <span>bubble size = magnitude (ATR)</span>
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
