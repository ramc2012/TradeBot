"use client";

/**
 * Sector-desk RRG — thin domain adapter over the shared quadrant scatter
 * (strategies/shared/RrgScatter). Keeps the sector point type and the
 * label shortening; all rendering lives in the shared component.
 */
import { RrgScatter, type RrgScatterPoint } from "../shared/RrgScatter";

export type RrgPoint = {
  sector_key: string;
  sector: string;
  x: number; // relative strength
  y: number; // momentum
  quadrant: string;
  leadership_score: number;
  tail?: Array<{ x: number; y: number }>;
};

export function RrgChart({
  points,
  selected,
  onSelect,
}: {
  points: RrgPoint[];
  selected?: string | null;
  onSelect?: (key: string) => void;
}) {
  const mapped: RrgScatterPoint[] = points.map((p) => ({
    key: p.sector_key,
    label: shortName(p.sector),
    title: p.sector,
    x: p.x,
    y: p.y,
    size: p.leadership_score,
    quadrant: p.quadrant,
    tail: p.tail,
    hoverLines: [
      `RS ${p.x.toFixed(2)} · Mom ${p.y.toFixed(2)}`,
      `Lead ${p.leadership_score.toFixed(2)}`,
    ],
  }));
  return (
    <RrgScatter
      points={mapped}
      selected={selected}
      onSelect={onSelect}
      quadrantSubs
      sizeCaption="bubble size = leadership magnitude"
      emptyText="Awaiting sector rotation data…"
    />
  );
}

/** Trim "Nifty " prefix + verbose suffixes so labels fit in the scatter. */
function shortName(s: string): string {
  return s
    .replace(/^Nifty\s+/i, "")
    .replace(/\s+Ex-Bank$/i, " ex-Bk")
    .replace(/Financial Services/i, "Fin Svc")
    .replace(/Capital Markets/i, "Cap Mkt")
    .replace(/Consumer Durables/i, "Cons Dur");
}
