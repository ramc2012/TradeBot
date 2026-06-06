"use client";

/**
 * Market Profile (TPO) histogram — shared by the Auction and Fractal desks.
 *
 * Horizontal distribution of TPO counts per price level with the value area
 * (VAH–VAL) shaded, POC highlighted, initial-balance band, single-print and
 * poor-high/low markers, and the last price. Pure SVG, viewBox-scaled.
 */
import { useMemo, useState } from "react";

import { CHART } from "./chartTheme";

export type TpoLevel = { price: number; count: number; letters?: string };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeTpo(profile: any): TpoLevel[] {
  if (!profile) return [];
  const rows = profile.tpo_rows ?? profile.tpoRows;
  if (Array.isArray(rows) && rows.length) {
    return rows
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .map((r: any) => ({
        price: Number(r.price ?? r.level ?? r[0]),
        count: Number(r.count ?? r.tpo_count ?? r.tpos ?? (typeof r.letters === "string" ? r.letters.length : 0) ?? r[1] ?? 0),
        letters: typeof r.letters === "string" ? r.letters : undefined,
      }))
      .filter((r: TpoLevel) => Number.isFinite(r.price))
      .sort((a: TpoLevel, b: TpoLevel) => b.price - a.price);
  }
  const counts = profile.tpo_counts ?? profile.tpoCounts;
  if (counts && typeof counts === "object") {
    const letters = profile.tpo_letters ?? {};
    return Object.entries(counts)
      .map(([p, c]) => ({ price: Number(p), count: Number(c), letters: letters[p] }))
      .filter((r) => Number.isFinite(r.price))
      .sort((a, b) => b.price - a.price);
  }
  return [];
}

const VB_W = 480;
const PAD = { t: 10, r: 56, b: 10, l: 8 };

export function MarketProfileChart({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  profile,
  lastPrice,
  height = 360,
}: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  profile: any;
  lastPrice?: number | null;
  height?: number;
}) {
  const [hover, setHover] = useState<TpoLevel | null>(null);
  const levels = useMemo(() => normalizeTpo(profile), [profile]);

  const model = useMemo(() => {
    if (!levels.length) return null;
    const prices = levels.map((l) => l.price);
    const pHigh = Math.max(...prices);
    const pLow = Math.min(...prices);
    const maxCount = Math.max(...levels.map((l) => l.count), 1);
    const rowH = (height - PAD.t - PAD.b) / levels.length;
    const yOf = (p: number) => PAD.t + (1 - (p - pLow) / (pHigh - pLow || 1)) * (height - PAD.t - PAD.b);
    const barMax = VB_W - PAD.l - PAD.r;
    const wOf = (c: number) => (c / maxCount) * barMax;
    return { pHigh, pLow, maxCount, rowH, yOf, wOf };
  }, [levels, height]);

  if (!model || !levels.length) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted" style={{ height }}>
        No profile distribution yet.
      </div>
    );
  }

  const poc = Number(profile.poc);
  const vah = Number(profile.vah);
  const val = Number(profile.val);
  const ibH = profile.initial_balance_high ?? profile.ib_high;
  const ibL = profile.initial_balance_low ?? profile.ib_low;
  const single: number[] = (profile.single_prints ?? []).map(Number);
  const { yOf, wOf, rowH } = model;
  const inVA = (p: number) => val && vah && p >= Math.min(val, vah) && p <= Math.max(val, vah);

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${height}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${height}` }}>
        {/* IB band */}
        {ibH != null && ibL != null ? (
          <rect x={PAD.l} y={yOf(Math.max(ibH, ibL))} width={VB_W - PAD.l - PAD.r}
            height={Math.abs(yOf(ibL) - yOf(ibH))} fill="rgba(59,130,246,0.07)" />
        ) : null}

        {/* TPO rows */}
        {levels.map((l, i) => {
          const w = Math.max(1, wOf(l.count));
          const y = yOf(l.price) - rowH / 2;
          const isPoc = Math.abs(l.price - poc) < 1e-6;
          const isSingle = single.includes(l.price);
          const fill = isPoc ? CHART.amber : inVA(l.price) ? "rgba(59,130,246,0.55)" : "rgba(255,255,255,0.18)";
          return (
            <g key={i} onMouseEnter={() => setHover(l)} onMouseLeave={() => setHover(null)}>
              <rect x={PAD.l} y={y} width={w} height={Math.max(1, rowH - 0.6)} fill={fill} rx={1} />
              {isSingle ? <rect x={PAD.l + w} y={y} width={3} height={Math.max(1, rowH - 0.6)} fill={CHART.red} /> : null}
            </g>
          );
        })}

        {/* VAH / VAL / POC level lines + labels */}
        {[
          { p: vah, c: CHART.blue, label: "VAH" },
          { p: val, c: CHART.blue, label: "VAL" },
          { p: poc, c: CHART.amber, label: "POC" },
        ]
          .filter((x) => Number.isFinite(x.p))
          .map((x, i) => (
            <g key={`lvl-${i}`}>
              <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(x.p)} y2={yOf(x.p)} stroke={x.c} strokeWidth={0.8} strokeDasharray={x.label === "POC" ? undefined : "3 3"} opacity={0.8} />
              <text x={VB_W - PAD.r + 3} y={yOf(x.p) + 3} fill={x.c} fontSize={8.5}>{x.label} {x.p.toFixed(1)}</text>
            </g>
          ))}

        {/* last price */}
        {lastPrice != null && lastPrice >= model.pLow && lastPrice <= model.pHigh ? (
          <g>
            <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(lastPrice)} y2={yOf(lastPrice)} stroke="#e6edf3" strokeWidth={0.8} strokeDasharray="4 2" />
            <text x={PAD.l + 2} y={yOf(lastPrice) - 2} fill="#e6edf3" fontSize={8.5}>{lastPrice.toFixed(1)}</text>
          </g>
        ) : null}
      </svg>

      {hover ? (
        <div className="pointer-events-none absolute left-2 top-2 rounded-lg border px-2.5 py-1 text-[10.5px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}>
          {hover.price.toFixed(1)} · {hover.count} TPO{hover.letters ? ` · ${hover.letters}` : ""}
        </div>
      ) : null}
    </div>
  );
}
