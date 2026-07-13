"use client";

/**
 * ProfileLadder — compact vertical market-profile level ladder.
 *
 * One shared price axis carrying:
 *   · value-area band (VAL→VAH, blue tint) + VAH/VAL dashed lines
 *   · POC (solid amber)
 *   · initial-balance band (IB low→high, amber tint) + IBH/IBL dashed
 *   · prior-session VAH/VAL/POC ghost lines (violet, left half)
 *   · HVN price dots (violet, left rail)
 *   · single-print ticks (red, left edge)
 *   · current spot marker (white line + arrow), colored by position vs POC
 *
 * Pure SVG — no chart deps. Field names vary per lane, so callers map their
 * payload onto these flat props.
 */
import { useMemo } from "react";

import { formatNumber } from "@/components/desk-ui";

const VB_W = 250;
const PAD = { t: 12, b: 12, l: 14, r: 66 };
const GHOST_X = VB_W - PAD.r - 100; // prior ghost lines span the left portion

export type ProfileLadderProps = {
  spot?: number | null;
  vah?: number | null;
  val?: number | null;
  poc?: number | null;
  ibHigh?: number | null;
  ibLow?: number | null;
  dayHigh?: number | null;
  dayLow?: number | null;
  prior?: { vah?: number | null; val?: number | null; poc?: number | null } | null;
  hvnPrices?: Array<number | null | undefined> | null;
  singlePrints?: Array<number | null | undefined> | null;
  height?: number;
  /** Decimal places for level labels (indices 0-1, commodities up to 2). */
  digits?: number;
  /** Hide the mini legend row underneath. */
  hideLegend?: boolean;
};

const num = (v: number | null | undefined): number | null =>
  v == null || !Number.isFinite(Number(v)) || Number(v) === 0 ? null : Number(v);

export function ProfileLadder({
  spot,
  vah,
  val,
  poc,
  ibHigh,
  ibLow,
  dayHigh,
  dayLow,
  prior,
  hvnPrices,
  singlePrints,
  height = 320,
  digits = 1,
  hideLegend = false,
}: ProfileLadderProps) {
  const levels = useMemo(() => {
    const cur = { spot: num(spot), vah: num(vah), val: num(val), poc: num(poc), ibHigh: num(ibHigh), ibLow: num(ibLow), dayHigh: num(dayHigh), dayLow: num(dayLow) };
    const ghost = { vah: num(prior?.vah), val: num(prior?.val), poc: num(prior?.poc) };
    const hvns = (hvnPrices ?? []).map(num).filter((p): p is number => p != null);
    const singles = (singlePrints ?? []).map(num).filter((p): p is number => p != null);
    return { cur, ghost, hvns, singles };
  }, [spot, vah, val, poc, ibHigh, ibLow, dayHigh, dayLow, prior, hvnPrices, singlePrints]);

  const domain = useMemo(() => {
    const all = [
      ...Object.values(levels.cur),
      ...Object.values(levels.ghost),
      ...levels.hvns,
      ...levels.singles,
    ].filter((p): p is number => p != null);
    if (all.length < 2) return null;
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.001 || 1;
    return { lo: lo - pad, hi: hi + pad };
  }, [levels]);

  if (!domain) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-xs text-text-muted" style={{ height }}>
        No profile levels yet.
      </div>
    );
  }

  const y = (p: number) => PAD.t + (1 - (p - domain.lo) / (domain.hi - domain.lo || 1)) * (height - PAD.t - PAD.b);
  const { cur, ghost, hvns, singles } = levels;
  const fmt = (p: number) => formatNumber(p, digits);
  const lineEnd = VB_W - PAD.r;

  const spotAbovePoc = cur.spot != null && cur.poc != null ? cur.spot >= cur.poc : null;
  const spotColor = spotAbovePoc == null ? "#e6edf3" : spotAbovePoc ? "#00d4a3" : "#ff4757";

  return (
    <div className="w-full">
      <svg viewBox={`0 0 ${VB_W} ${height}`} className="w-full" style={{ height: "auto", aspectRatio: `${VB_W} / ${height}` }} role="img" aria-label="Market profile level ladder">
        {/* day-range rail */}
        {cur.dayHigh != null && cur.dayLow != null ? (
          <line x1={PAD.l - 5} x2={PAD.l - 5} y1={y(cur.dayHigh)} y2={y(cur.dayLow)} stroke="rgba(255,255,255,0.18)" strokeWidth={2} strokeLinecap="round" />
        ) : null}

        {/* value-area band */}
        {cur.vah != null && cur.val != null ? (
          <rect x={PAD.l} y={y(Math.max(cur.vah, cur.val))} width={lineEnd - PAD.l} height={Math.abs(y(cur.val) - y(cur.vah))} fill="rgba(59,130,246,0.10)" />
        ) : null}

        {/* IB band */}
        {cur.ibHigh != null && cur.ibLow != null ? (
          <rect x={PAD.l} y={y(Math.max(cur.ibHigh, cur.ibLow))} width={lineEnd - PAD.l} height={Math.abs(y(cur.ibLow) - y(cur.ibHigh))} fill="rgba(255,165,2,0.07)" />
        ) : null}

        {/* prior-session ghost lines */}
        {(
          [
            { p: ghost.vah, label: "pVAH" },
            { p: ghost.poc, label: "pPOC" },
            { p: ghost.val, label: "pVAL" },
          ] as const
        )
          .filter((g) => g.p != null)
          .map((g) => (
            <g key={g.label} opacity={0.75}>
              <line x1={PAD.l} x2={GHOST_X} y1={y(g.p as number)} y2={y(g.p as number)} stroke="#a78bfa" strokeWidth={0.9} strokeDasharray="2 3" />
              <text x={PAD.l + 1} y={y(g.p as number) - 2} fill="#a78bfa" fontSize={7.5}>{g.label} {fmt(g.p as number)}</text>
            </g>
          ))}

        {/* current session reference lines */}
        {(
          [
            { p: cur.ibHigh, c: "rgba(255,165,2,0.75)", label: "IBH", dash: "4 3", w: 0.8 },
            { p: cur.ibLow, c: "rgba(255,165,2,0.75)", label: "IBL", dash: "4 3", w: 0.8 },
            { p: cur.vah, c: "#3b82f6", label: "VAH", dash: "5 3", w: 1 },
            { p: cur.val, c: "#3b82f6", label: "VAL", dash: "5 3", w: 1 },
            { p: cur.poc, c: "#ffa502", label: "POC", dash: undefined, w: 1.4 },
          ] as const
        )
          .filter((l) => l.p != null)
          .map((l) => (
            <g key={l.label}>
              <line x1={PAD.l} x2={lineEnd} y1={y(l.p as number)} y2={y(l.p as number)} stroke={l.c} strokeWidth={l.w} strokeDasharray={l.dash} />
              <text x={lineEnd + 4} y={y(l.p as number) + 2.6} fill={l.c} fontSize={8}>{l.label} {fmt(l.p as number)}</text>
            </g>
          ))}

        {/* HVN dots */}
        {hvns.map((p, i) => (
          <g key={`hvn-${i}`}>
            <circle cx={PAD.l + 7} cy={y(p)} r={2.6} fill="#a78bfa" opacity={0.9}>
              <title>HVN {fmt(p)}</title>
            </circle>
          </g>
        ))}

        {/* single-print ticks */}
        {singles.map((p, i) => (
          <rect key={`sp-${i}`} x={PAD.l - 2} y={y(p) - 1} width={5} height={2} fill="#ff4757" opacity={0.9}>
            <title>single print {fmt(p)}</title>
          </rect>
        ))}

        {/* spot marker */}
        {cur.spot != null ? (
          <g>
            <line x1={PAD.l} x2={lineEnd} y1={y(cur.spot)} y2={y(cur.spot)} stroke={spotColor} strokeWidth={1.1} strokeDasharray="6 2" />
            <polygon points={`${lineEnd},${y(cur.spot)} ${lineEnd + 6},${y(cur.spot) - 3.5} ${lineEnd + 6},${y(cur.spot) + 3.5}`} fill={spotColor} />
            <text x={lineEnd + 8} y={y(cur.spot) + 2.6} fill={spotColor} fontSize={8.5} fontWeight={700}>{fmt(cur.spot)}</text>
          </g>
        ) : null}
      </svg>

      {!hideLegend ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[9px] uppercase tracking-[0.1em] text-text-muted">
          <LegendSwatch color="#ffa502" label="POC" />
          <LegendSwatch color="#3b82f6" label="VA" />
          <LegendSwatch color="rgba(255,165,2,0.75)" label="IB" />
          {ghost.poc != null || ghost.vah != null || ghost.val != null ? <LegendSwatch color="#a78bfa" label="prior" /> : null}
          {hvns.length ? <LegendSwatch color="#a78bfa" label="HVN ·" round /> : null}
          {cur.spot != null ? <LegendSwatch color={spotColor} label="spot" /> : null}
        </div>
      ) : null}
    </div>
  );
}

function LegendSwatch({ color, label, round = false }: { color: string; label: string; round?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={round ? "inline-block h-1.5 w-1.5 rounded-full" : "inline-block h-[3px] w-3 rounded-sm"} style={{ backgroundColor: color }} aria-hidden />
      {label}
    </span>
  );
}
