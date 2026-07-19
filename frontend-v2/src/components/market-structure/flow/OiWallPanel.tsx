"use client";

/**
 * OiWallPanel — open-interest walls from the option chain.
 *
 * The one flow-view panel whose numbers are genuinely OBSERVED: open interest
 * is reported by the exchange per strike, it is not a buy/sell attribution and
 * it needs no aggressor tape. So it is graded with `feature: "quote"` rather
 * than `"flow_attribution"` — the only panel in this view that may.
 *
 * What it is NOT: a directional read. A wall is resting open interest at a
 * strike; who is long or short it is not in the payload, so the panel states
 * the levels and the sizes and stops there.
 */
import { Layers3 } from "lucide-react";

import { formatNumber } from "@/components/desk-ui";

import { ProvenancePanel } from "./ProvenancePanel";

export type OiWalls = {
  expiry: string | null;
  callWall: number | null;
  putWall: number | null;
  topCallWalls: Array<{ strike: number; oi: number }>;
  topPutWalls: Array<{ strike: number; oi: number }>;
};

export function OiWallPanel({
  walls,
  spot,
  source,
  asOf,
  unavailable,
}: {
  walls: OiWalls | null;
  spot?: number | null;
  source?: string | null;
  asOf?: string | null;
  unavailable?: string | null;
}) {
  const reason =
    unavailable ??
    (!walls
      ? "no option-chain block for this instrument in the served payload — call/put walls come from the convergence lane's `options` block, whose scan universe does not include it."
      : walls.callWall == null && walls.putWall == null && !walls.topCallWalls.length && !walls.topPutWalls.length
        ? "the lane served an options block with no wall levels this cycle."
        : null);

  const rows = walls
    ? [
        ...walls.topCallWalls.slice(0, 4).map((w) => ({ ...w, side: "call" as const })),
        ...walls.topPutWalls.slice(0, 4).map((w) => ({ ...w, side: "put" as const })),
      ].sort((a, b) => b.strike - a.strike)
    : [];
  const maxOi = rows.reduce((m, r) => Math.max(m, Number(r.oi) || 0), 0) || 1;

  return (
    <ProvenancePanel
      title="Option OI walls"
      icon={<Layers3 size={14} />}
      description="Resting open interest per strike. Reported by the exchange — not an attribution, and not a directional read on its own."
      // OI is observed. This is the ONE flow panel that may say so.
      feature="quote"
      source={source}
      asOf={asOf}
      timeframe={walls?.expiry ? `expiry ${walls.expiry}` : null}
      showOfBadge={false}
      unavailable={reason}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <WallTile label="Call wall" value={walls?.callWall ?? null} spot={spot} />
        <WallTile label="Put wall" value={walls?.putWall ?? null} spot={spot} />
      </div>

      {rows.length ? (
        <div className="mt-3 space-y-1">
          {rows.map((r) => (
            <div
              key={`${r.side}-${r.strike}`}
              className="relative flex items-center justify-between overflow-hidden rounded px-2 py-1 text-[11px]"
            >
              <span
                className={
                  "absolute inset-y-0 left-0 " +
                  (r.side === "call" ? "bg-accent-red/12" : "bg-accent-green/12")
                }
                style={{ width: `${((Number(r.oi) || 0) / maxOi) * 100}%` }}
                aria-hidden
              />
              <span className="relative font-mono text-text-secondary">
                {formatNumber(r.strike, 0)}
              </span>
              <span className="relative flex items-center gap-2">
                <span className="uppercase tracking-[0.1em] text-text-muted">{r.side}</span>
                <span className="font-mono text-text-secondary">{formatNumber(r.oi, 0)}</span>
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </ProvenancePanel>
  );
}

function WallTile({
  label,
  value,
  spot,
}: {
  label: string;
  value: number | null;
  spot?: number | null;
}) {
  const distance =
    value != null && spot != null && Number.isFinite(spot) ? value - spot : null;
  return (
    <div className="rounded-lg border border-bg-border/70 px-2.5 py-2">
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div
        className={
          "mt-0.5 font-mono text-[15px] " + (value == null ? "text-text-muted" : "text-text-primary")
        }
      >
        {value == null ? "UNAVAILABLE" : formatNumber(value, 0)}
      </div>
      {distance != null ? (
        <div className="text-[10.5px] text-text-muted">
          {distance >= 0 ? "+" : ""}
          {formatNumber(distance, 1)} from spot
        </div>
      ) : null}
    </div>
  );
}
