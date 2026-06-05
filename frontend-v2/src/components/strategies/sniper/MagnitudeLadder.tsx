"use client";

/**
 * Excursion ladder — a horizontal diverging bar per underlying showing
 * the sniper's predicted DOWN excursion (left, red) vs UP excursion
 * (right, green), both in ATR units, with the net directional call
 * marked. Pure SVG so the geometry (a shared centre axis, per-row
 * symmetric bars) renders crisply.
 *
 * up_atr / down_atr come straight from the estimator heads; when the
 * sidecar only carried a reduced magnitude we fall back to projecting
 * the signed magnitude onto the called side.
 */
import { CHART } from "../shared/chartTheme";
import type { SniperRow } from "./types";

function sideValues(r: SniperRow): { up: number; down: number } {
  const up = r.up_atr != null ? Math.abs(Number(r.up_atr)) : NaN;
  const down = r.down_atr != null ? Math.abs(Number(r.down_atr)) : NaN;
  if (Number.isFinite(up) || Number.isFinite(down)) {
    return { up: Number.isFinite(up) ? up : 0, down: Number.isFinite(down) ? down : 0 };
  }
  // Fallback: project reduced magnitude onto the called direction.
  const mag = Math.abs(r.magnitude_atr || 0);
  const s = String(r.direction || "").toUpperCase();
  if (s === "LONG") return { up: mag, down: 0 };
  if (s === "SHORT") return { up: 0, down: mag };
  return { up: 0, down: 0 };
}

export function MagnitudeLadder({ rows }: { rows: SniperRow[] }) {
  if (!rows.length) {
    return (
      <div className="flex h-[180px] items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted">
        No live excursion estimates.
      </div>
    );
  }

  const vals = rows.map((r) => ({ r, ...sideValues(r) }));
  const maxV = Math.max(0.5, ...vals.map((v) => Math.max(v.up, v.down)));
  const rowH = 30;
  const labelW = 96;
  const VB_W = 1000;
  const VB_H = rows.length * rowH + 26;
  const axisX = labelW + (VB_W - labelW - 70) / 2 + 10;
  const half = (VB_W - labelW - 80) / 2;
  const scale = (v: number) => (v / maxV) * half;

  return (
    <div className="w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="w-full" style={{ height: "auto" }}>
        {/* axis labels */}
        <text x={axisX - half} y={14} fill={CHART.red} fontSize={10} fontWeight={600}>↓ down excursion (ATR)</text>
        <text x={axisX + half} y={14} fill={CHART.green} fontSize={10} fontWeight={600} textAnchor="end">up excursion (ATR) ↑</text>
        <line x1={axisX} x2={axisX} y1={20} y2={VB_H - 4} stroke="rgba(255,255,255,0.22)" strokeWidth={1} />

        {vals.map((v, i) => {
          const y = 26 + i * rowH;
          const cy = y + rowH / 2 - 4;
          const up = scale(v.up);
          const down = scale(v.down);
          const s = String(v.r.direction || "").toUpperCase();
          const stale = (v.r.age_sec ?? 0) > 1800;
          const op = stale ? 0.45 : 1;
          return (
            <g key={v.r.symbol} opacity={op}>
              <text x={4} y={cy + 4} fill="#e6edf3" fontSize={11} fontWeight={s === "FLAT" ? 400 : 600}>
                {v.r.symbol}
              </text>
              {/* down bar (left) */}
              <rect x={axisX - down} y={cy - 7} width={down} height={14} rx={2}
                fill={CHART.red} opacity={s === "SHORT" ? 0.85 : 0.4} />
              {/* up bar (right) */}
              <rect x={axisX} y={cy - 7} width={up} height={14} rx={2}
                fill={CHART.green} opacity={s === "LONG" ? 0.85 : 0.4} />
              {/* value labels */}
              {v.down > 0 ? (
                <text x={axisX - down - 4} y={cy + 4} fill={CHART.red} fontSize={9} textAnchor="end" fontFamily="monospace">
                  {v.down.toFixed(2)}
                </text>
              ) : null}
              {v.up > 0 ? (
                <text x={axisX + up + 4} y={cy + 4} fill={CHART.green} fontSize={9} fontFamily="monospace">
                  {v.up.toFixed(2)}
                </text>
              ) : null}
              {/* called-side marker */}
              {s !== "FLAT" ? (
                <text x={VB_W - 6} y={cy + 4} fill={s === "LONG" ? CHART.green : CHART.red} fontSize={9} textAnchor="end" fontWeight={600}>
                  {s} · {v.r.horizon || "—"}
                </text>
              ) : (
                <text x={VB_W - 6} y={cy + 4} fill={CHART.muted} fontSize={9} textAnchor="end">FLAT</text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
