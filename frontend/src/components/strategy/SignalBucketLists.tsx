"use client";

import { useMemo } from "react";

/**
 * Three-list rendering powered by the cross-strategy signal bucket classifier.
 *
 * Every strategy agent now decorates its lane rows with:
 *   - bucket: active | ready | favourable | drifting | neutral
 *   - trajectory: improving | stalled | deteriorating | null
 *   - proximity_pct: 0-100
 *   - bucket_rationale: human-readable string
 *
 * This component renders three columns:
 *   1. Already met / traded  (bucket in {active, ready})
 *   2. Favourable - tracked  (bucket = favourable)
 *   3. Drifting away          (bucket = drifting)
 *
 * Used by /commodity, /nse-desk, /fractal-market-profile, /directional-options,
 * and /auction-intelligence to provide the same audit-friendly surface.
 */

export type BucketRow = {
  symbol?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  bucket?: "active" | "ready" | "favourable" | "drifting" | "neutral" | null;
  trajectory?: "improving" | "stalled" | "deteriorating" | null;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  status?: string | null;
};

type Props = {
  rows: BucketRow[];
  title?: string;
  emptyHint?: string;
};

function trajectoryGlyph(t: BucketRow["trajectory"]): string {
  if (t === "improving") return "▲";
  if (t === "deteriorating") return "▼";
  if (t === "stalled") return "▬";
  return "·";
}

function trajectoryColor(t: BucketRow["trajectory"]): string {
  if (t === "improving") return "text-emerald-400";
  if (t === "deteriorating") return "text-rose-400";
  return "text-text-muted";
}

function rowLabel(row: BucketRow): string {
  return (
    row.display_name ||
    row.symbol ||
    row.underlying ||
    "—"
  );
}

function BucketColumn({
  heading,
  hint,
  tone,
  rows,
}: {
  heading: string;
  hint: string;
  tone: "emerald" | "amber" | "rose";
  rows: BucketRow[];
}) {
  const toneClass =
    tone === "emerald"
      ? "border-emerald-500/40 bg-emerald-500/5"
      : tone === "amber"
        ? "border-amber-500/40 bg-amber-500/5"
        : "border-rose-500/40 bg-rose-500/5";
  return (
    <div className={`flex min-h-[140px] flex-col rounded-2xl border ${toneClass} px-3 py-3`}>
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-primary">
          {heading}
        </div>
        <div className="text-[11px] text-text-muted">{rows.length}</div>
      </div>
      <div className="mt-1 text-[11px] text-text-muted">{hint}</div>
      <ul className="mt-2 flex flex-1 flex-col gap-1.5 text-xs">
        {rows.length === 0 ? (
          <li className="text-text-muted italic">No symbols in this bucket.</li>
        ) : (
          rows.map((row, idx) => (
            <li
              key={`${row.symbol || row.underlying || "row"}-${idx}`}
              className="rounded-lg bg-bg-secondary/40 px-2 py-1.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-text-primary">{rowLabel(row)}</span>
                <span className={`text-[10px] ${trajectoryColor(row.trajectory)}`}>
                  {trajectoryGlyph(row.trajectory)}{" "}
                  {row.proximity_pct != null ? `${Math.round(row.proximity_pct)}%` : ""}
                </span>
              </div>
              <div className="mt-0.5 text-[10.5px] leading-snug text-text-muted">
                {row.bucket_rationale || row.signal_validation_detail || row.status || "—"}
              </div>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

export default function SignalBucketLists({ rows, title, emptyHint }: Props) {
  const met = useMemo(
    () => rows.filter((r) => r.bucket === "active" || r.bucket === "ready"),
    [rows],
  );
  const favourable = useMemo(
    () => rows.filter((r) => r.bucket === "favourable"),
    [rows],
  );
  const drifting = useMemo(() => rows.filter((r) => r.bucket === "drifting"), [rows]);

  return (
    <section className="rounded-2xl border border-bg-active/50 bg-bg-secondary/20 px-4 py-4">
      {title ? (
        <h3 className="text-xs font-semibold uppercase tracking-[0.18em] text-text-primary">
          {title}
        </h3>
      ) : null}
      {emptyHint && rows.length === 0 ? (
        <div className="mt-2 text-xs text-text-muted">{emptyHint}</div>
      ) : (
        <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-3">
          <BucketColumn
            heading="Met / Traded"
            hint="Already triggered or currently held."
            tone="emerald"
            rows={met}
          />
          <BucketColumn
            heading="Favourable · Tracked"
            hint="Close to firing — watch closely."
            tone="amber"
            rows={favourable}
          />
          <BucketColumn
            heading="Drifting Away"
            hint="Moving out of favourable zone."
            tone="rose"
            rows={drifting}
          />
        </div>
      )}
    </section>
  );
}
