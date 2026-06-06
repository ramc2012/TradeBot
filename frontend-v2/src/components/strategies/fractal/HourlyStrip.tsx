"use client";

/**
 * Fractal hourly-profile strip — a horizontal row of compact vertical
 * mini-profiles, one per completed hour. Each cell draws a tiny TPO
 * histogram (price ascending top→down) with the VA band shaded and the
 * POC tick highlighted, plus the hour's shape, direction bias and the
 * value-migration step (POC drift vs the prior hour). The fractal thesis
 * is that the day is a self-similar stack of these hours — so showing them
 * side by side surfaces the migration of value through the session.
 */
import { useMemo, useState } from "react";

import { StatusBadge, directionTone, formatNumber } from "@/components/desk-ui";
import { CHART } from "@/components/strategies/shared";
import { normalizeTpo } from "@/components/strategies/shared";

export type HourlyProfile = {
  scope?: string;
  hour_number?: number | null;
  completed?: boolean;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  open_price?: number | null;
  close_price?: number | null;
  high_price?: number | null;
  low_price?: number | null;
  shape?: string | null;
  direction_bias?: string | null;
  value_migration?: number | null;
  day_type?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  tpo_rows?: any[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

const CELL_W = 78;
const CELL_H = 132;

function biasTone(bias?: string | null): string {
  if (bias === "bullish") return "text-accent-green";
  if (bias === "bearish") return "text-accent-red";
  return "text-text-muted";
}

function MiniProfile({ hp, prevPoc }: { hp: HourlyProfile; prevPoc?: number | null }) {
  const levels = useMemo(() => normalizeTpo(hp), [hp]);
  const model = useMemo(() => {
    if (!levels.length) return null;
    const prices = levels.map((l) => l.price);
    const pHigh = Math.max(...prices);
    const pLow = Math.min(...prices);
    const maxCount = Math.max(...levels.map((l) => l.count), 1);
    return { pHigh, pLow, maxCount };
  }, [levels]);

  const poc = Number(hp.poc);
  const vah = Number(hp.vah);
  const val = Number(hp.val);
  const migration =
    prevPoc != null && Number.isFinite(poc) && Number.isFinite(prevPoc)
      ? poc - Number(prevPoc)
      : null;

  if (!model) {
    return (
      <div className="flex items-center justify-center text-[9px] text-text-muted" style={{ width: CELL_W, height: CELL_H }}>
        —
      </div>
    );
  }

  const { pHigh, pLow, maxCount } = model;
  const span = pHigh - pLow || 1;
  const padT = 4;
  const padB = 4;
  const innerH = CELL_H - padT - padB;
  const yOf = (p: number) => padT + (1 - (p - pLow) / span) * innerH;
  const barMax = CELL_W - 12;
  const rowH = Math.max(1, innerH / levels.length);
  const inVA = (p: number) =>
    Number.isFinite(val) && Number.isFinite(vah) && p >= Math.min(val, vah) && p <= Math.max(val, vah);

  return (
    <svg viewBox={`0 0 ${CELL_W} ${CELL_H}`} width={CELL_W} height={CELL_H} className="shrink-0">
      {/* VA band */}
      {Number.isFinite(val) && Number.isFinite(vah) ? (
        <rect
          x={2}
          y={yOf(Math.max(val, vah))}
          width={CELL_W - 4}
          height={Math.abs(yOf(val) - yOf(vah))}
          fill="rgba(59,130,246,0.08)"
        />
      ) : null}
      {levels.map((l, i) => {
        const w = Math.max(1.2, (l.count / maxCount) * barMax);
        const isPoc = Math.abs(l.price - poc) < 1e-6;
        const fill = isPoc ? CHART.amber : inVA(l.price) ? "rgba(59,130,246,0.5)" : "rgba(255,255,255,0.16)";
        return (
          <rect key={i} x={4} y={yOf(l.price) - rowH / 2} width={w} height={Math.max(0.8, rowH - 0.4)} fill={fill} rx={0.6} />
        );
      })}
      {/* POC tick */}
      {Number.isFinite(poc) ? (
        <line x1={2} x2={CELL_W - 2} y1={yOf(poc)} y2={yOf(poc)} stroke={CHART.amber} strokeWidth={0.7} opacity={0.85} />
      ) : null}
      {/* migration arrow vs prior hour POC */}
      {migration != null && Math.abs(migration) > 1e-6 ? (
        <text x={CELL_W - 3} y={CELL_H - 3} textAnchor="end" fontSize={8} fill={migration > 0 ? CHART.green : CHART.red}>
          {migration > 0 ? "▲" : "▼"}
          {Math.abs(migration).toFixed(0)}
        </text>
      ) : null}
    </svg>
  );
}

export function HourlyStrip({ profiles }: { profiles: HourlyProfile[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const completed = profiles.filter((p) => (p.tpo_rows?.length ?? 0) > 0);

  if (!completed.length) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-sm text-text-muted">
        No completed hourly profiles yet.
      </div>
    );
  }

  return (
    <div className="-mx-1 overflow-x-auto pb-1">
      <div className="flex gap-2 px-1">
        {completed.map((hp, i) => {
          const prevPoc = i > 0 ? completed[i - 1].poc : null;
          const active = hover === i;
          return (
            <div
              key={hp.hour_number ?? i}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              className={`shrink-0 rounded-xl border px-2 py-2 transition-colors ${
                active ? "border-accent-blue/50 bg-bg-primary/30" : "border-bg-border bg-bg-primary/12"
              }`}
              style={{ width: CELL_W + 16 }}
            >
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[10px] font-semibold text-text-primary">H{hp.hour_number}</span>
                <span className={`text-[9px] font-mono ${biasTone(hp.direction_bias)}`}>
                  {hp.direction_bias === "bullish" ? "▲" : hp.direction_bias === "bearish" ? "▼" : "•"}
                </span>
              </div>
              <MiniProfile hp={hp} prevPoc={prevPoc} />
              <div className="mt-1.5 space-y-0.5">
                <div className="truncate text-[9px] text-text-secondary" title={hp.shape ?? ""}>
                  {hp.shape ?? "—"}
                </div>
                <div className="flex items-center justify-between text-[8.5px] font-mono text-text-muted">
                  <span>POC {formatNumber(hp.poc, 0)}</span>
                </div>
                <div className="text-[8.5px] font-mono text-text-muted">
                  VA {formatNumber(hp.val, 0)}–{formatNumber(hp.vah, 0)}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
