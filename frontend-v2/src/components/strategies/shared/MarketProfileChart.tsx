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
  prior = null,
  showLegend = false,
}: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  profile: any;
  lastPrice?: number | null;
  height?: number;
  prior?: { vah?: number | null; val?: number | null; poc?: number | null } | null;
  showLegend?: boolean;
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
  const inDomain = (p?: number | null): p is number =>
    p != null && Number.isFinite(p) && p >= model.pLow && p <= model.pHigh;
  const priorLevels: { p: number; label: string }[] = [
    { p: prior?.vah, label: "pVAH" },
    { p: prior?.poc, label: "pPOC" },
    { p: prior?.val, label: "pVAL" },
  ].flatMap((g) => (inDomain(g.p) ? [{ p: g.p, label: g.label }] : []));
  const poorLevels = [
    profile.poor_high ? { p: model.pHigh, label: "poor high" } : null,
    profile.poor_low ? { p: model.pLow, label: "poor low" } : null,
  ].filter((x): x is { p: number; label: string } => x != null);

  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${VB_W} ${height}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${height}` }}>
        {/* IB band */}
        {ibH != null && ibL != null ? (
          <rect x={PAD.l} y={yOf(Math.max(ibH, ibL))} width={VB_W - PAD.l - PAD.r}
            height={Math.abs(yOf(ibL) - yOf(ibH))} fill={CHART.blueFaint} />
        ) : null}

        {/* TPO rows */}
        {levels.map((l, i) => {
          const w = Math.max(1, wOf(l.count));
          const y = yOf(l.price) - rowH / 2;
          const isPoc = Math.abs(l.price - poc) < 1e-6;
          const isSingle = single.includes(l.price);
          const fill = isPoc ? CHART.amber : inVA(l.price) ? CHART.blueSoft : CHART.barMuted;
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

        {/* prior-session ghost levels */}
        {priorLevels.map((g) => (
          <g key={g.label} opacity={0.75}>
            <line x1={PAD.l} x2={PAD.l + (VB_W - PAD.l - PAD.r) * 0.35} y1={yOf(g.p)} y2={yOf(g.p)} stroke={CHART.violet} strokeWidth={0.9} strokeDasharray="2 3" />
            <text x={PAD.l + 1} y={yOf(g.p) - 2} fill={CHART.violet} fontSize={7.5}>{g.label} {g.p.toFixed(1)}</text>
          </g>
        ))}

        {/* poor (unfinished) high / low */}
        {poorLevels.map((x) => (
          <g key={x.label}>
            <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(x.p)} y2={yOf(x.p)} stroke={CHART.pink} strokeWidth={1} strokeDasharray="1 2" />
            <text x={PAD.l + 1} y={x.label === "poor high" ? yOf(x.p) + 8 : yOf(x.p) - 3} fill={CHART.pink} fontSize={7}>{x.label}</text>
          </g>
        ))}

        {/* last price */}
        {lastPrice != null && lastPrice >= model.pLow && lastPrice <= model.pHigh ? (
          <g>
            <line x1={PAD.l} x2={VB_W - PAD.r} y1={yOf(lastPrice)} y2={yOf(lastPrice)} stroke={CHART.text} strokeWidth={0.8} strokeDasharray="4 2" />
            <text x={PAD.l + 2} y={yOf(lastPrice) - 2} fill={CHART.text} fontSize={8.5}>{lastPrice.toFixed(1)}</text>
          </g>
        ) : null}
      </svg>

      {showLegend ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[9px] uppercase tracking-[0.1em] text-text-muted">
          <Swatch color={CHART.amber} label="POC" />
          <Swatch color={CHART.blueSoft} label="VA" />
          {ibH != null && ibL != null ? <Swatch color={CHART.blueFaint} label="IB" /> : null}
          {single.length ? <Swatch color={CHART.red} label="single prints" /> : null}
          {priorLevels.length ? <Swatch color={CHART.violet} label="prior" /> : null}
          {poorLevels.length ? <Swatch color={CHART.pink} label="poor hi/lo" /> : null}
          {lastPrice != null ? <Swatch color={CHART.text} label="last" /> : null}
        </div>
      ) : null}

      {hover ? (
        <div className="pointer-events-none absolute left-2 top-2 rounded-lg border px-2.5 py-1 text-[10.5px] font-mono"
          style={{ background: CHART.surface, borderColor: CHART.border }}>
          {hover.price.toFixed(1)} · {hover.count} TPO{hover.letters ? ` · ${hover.letters}` : ""}
        </div>
      ) : null}
    </div>
  );
}

function Swatch({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="inline-block h-[3px] w-3 rounded-sm" style={{ backgroundColor: color }} aria-hidden />
      {label}
    </span>
  );
}
