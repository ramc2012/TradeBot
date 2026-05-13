"use client";

/**
 * Shared helpers for the data-focused desk live views (commodity, NSE,
 * Auction IQ, FMP, Directional Options). Centralises:
 *   - bucket / trajectory rendering
 *   - currency/percent formatting
 *   - relative age + IST timestamp helpers
 *   - audit-event severity colors
 *   - shared TS types for cross-desk reuse
 *
 * Each desk page imports these so the visual language stays consistent.
 */
import type { ReactNode } from "react";

export type Bucket =
  | "active"
  | "ready"
  | "favourable"
  | "drifting"
  | "neutral"
  | null
  | undefined;
export type Trajectory =
  | "improving"
  | "stalled"
  | "deteriorating"
  | null
  | undefined;

export type BucketedRow = {
  symbol?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  bucket?: Bucket;
  trajectory?: Trajectory;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  status?: string | null;
};

export type AuditEvent = {
  created_at?: string;
  event_type?: string;
  severity?: string;
  message?: string;
  symbol?: string;
  underlying?: string;
  market?: string;
  payload?: Record<string, unknown>;
};

export const BUCKET_COLOR: Record<string, string> = {
  active: "bg-emerald-500/20 text-emerald-300 border-emerald-500/50",
  ready: "bg-emerald-500/30 text-emerald-200 border-emerald-500/60",
  favourable: "bg-amber-500/20 text-amber-200 border-amber-500/50",
  drifting: "bg-rose-500/20 text-rose-200 border-rose-500/50",
  neutral: "bg-slate-500/20 text-slate-300 border-slate-500/40",
};

export function formatINR(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `₹${Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

export function formatNumber(
  n: number | null | undefined,
  decimals = 2,
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatPct(
  n: number | null | undefined,
  decimals = 2,
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(decimals)}%`;
}

export function trajectoryGlyph(t: Trajectory): string {
  if (t === "improving") return "▲";
  if (t === "deteriorating") return "▼";
  if (t === "stalled") return "▬";
  return "·";
}

export function trajectoryColor(t: Trajectory): string {
  if (t === "improving") return "text-emerald-400";
  if (t === "deteriorating") return "text-rose-400";
  return "text-slate-400";
}

export function relativeAge(
  seconds: number | null | undefined,
): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m${rs ? ` ${rs}s` : ""}`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h${rm ? ` ${rm}m` : ""}`;
}

export function formatIST(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

export function severityColor(sev: string | undefined): string {
  switch ((sev || "").toLowerCase()) {
    case "error":
      return "text-rose-400";
    case "warning":
      return "text-amber-300";
    case "success":
      return "text-emerald-300";
    case "trade":
      return "text-sky-300";
    default:
      return "text-slate-400";
  }
}

export function BucketBadge({
  bucket,
  trajectory,
  proximity,
}: {
  bucket: Bucket;
  trajectory?: Trajectory;
  proximity?: number | null;
}): ReactNode {
  return (
    <>
      <span
        className={`rounded border px-1.5 py-0.5 text-[10px] ${
          BUCKET_COLOR[bucket || "neutral"] || BUCKET_COLOR.neutral
        }`}
      >
        {bucket || "—"}
      </span>{" "}
      <span className={trajectoryColor(trajectory)}>
        {trajectoryGlyph(trajectory)}
      </span>{" "}
      <span className="text-[10px] text-text-muted">
        {proximity !== null && proximity !== undefined
          ? `${Math.round(proximity)}%`
          : ""}
      </span>
    </>
  );
}

/**
 * Three-list view (Met / Favourable / Drifting). Filters the input rows by
 * `bucket` and renders three side-by-side cards. Shared across every desk.
 */
export function ThreeListView({ rows }: { rows: BucketedRow[] }): ReactNode {
  const met = rows.filter(
    (r) => r.bucket === "active" || r.bucket === "ready",
  );
  const favourable = rows.filter((r) => r.bucket === "favourable");
  const drifting = rows.filter((r) => r.bucket === "drifting");

  const columns: Array<{ label: string; tone: string; items: BucketedRow[] }> =
    [
      {
        label: "Met / Traded",
        tone: "border-emerald-500/40 bg-emerald-500/5",
        items: met,
      },
      {
        label: "Favourable",
        tone: "border-amber-500/40 bg-amber-500/5",
        items: favourable,
      },
      {
        label: "Drifting",
        tone: "border-rose-500/40 bg-rose-500/5",
        items: drifting,
      },
    ];

  return (
    <div className="grid grid-cols-3 gap-2 text-xs">
      {columns.map(({ label, tone, items }) => (
        <div key={label} className={`rounded border px-2 py-1.5 ${tone}`}>
          <div className="flex items-baseline justify-between">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide">
              {label}
            </span>
            <span className="text-[10px] text-text-muted">{items.length}</span>
          </div>
          <ul className="mt-1 flex flex-col gap-0.5">
            {items.length === 0 ? (
              <li className="italic text-text-muted">none</li>
            ) : (
              items.map((r, idx) => (
                <li
                  key={`${r.underlying || r.symbol || "row"}-${idx}`}
                  className="truncate"
                >
                  <span className="font-medium">
                    {r.underlying || r.symbol}
                  </span>
                  <span className="text-[10px] text-text-muted">
                    {" · "}
                    {r.proximity_pct != null
                      ? `${Math.round(r.proximity_pct)}%`
                      : ""}
                  </span>
                </li>
              ))
            )}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function AuditFeed({
  events,
  emptyText = "no events yet",
}: {
  events: AuditEvent[];
  emptyText?: string;
}): ReactNode {
  if (events.length === 0) {
    return (
      <div className="px-2 py-3 text-xs text-text-muted">{emptyText}</div>
    );
  }
  return (
    <ul className="space-y-0.5 text-[11.5px]">
      {events.map((e, idx) => (
        <li
          key={`${e.created_at}-${idx}`}
          className="flex items-baseline gap-2 border-b border-bg-active/15 py-1"
        >
          <span className="w-[60px] font-mono text-[10.5px] text-text-muted">
            {formatIST(e.created_at)}
          </span>
          <span
            className={`w-[80px] text-[10.5px] uppercase ${severityColor(e.severity)}`}
          >
            {e.event_type || "—"}
          </span>
          {e.symbol ? (
            <span className="w-[140px] truncate font-mono text-[10.5px] text-text-muted">
              {e.symbol}
            </span>
          ) : (
            <span className="w-[140px] text-text-muted">
              {e.underlying || "—"}
            </span>
          )}
          <span className="flex-1 truncate text-text-secondary">
            {e.message || JSON.stringify(e.payload || {})}
          </span>
        </li>
      ))}
    </ul>
  );
}
