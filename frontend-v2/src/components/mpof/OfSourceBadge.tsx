"use client";

/**
 * Order-flow source honesty badge.
 *
 * Every OF visualization must state where its data came from:
 *   REAL TICKS   (green, solid)   — reconstructed from an actual tick tape /
 *                                   futures book (market_ticks,
 *                                   tick_reconstruction, tick_reconstruction_book)
 *   BAR PROXY    (amber, pulsing) — INFERRED from bar OHLCV, not a tape
 *                                   (bar_inference, bar_proxy, bar_fallback, …)
 *   SOURCE UNKNOWN (neutral)      — payload didn't say; treat with suspicion.
 *
 * The pulse on the amber badge is deliberate — fabricated flow must be
 * impossible to mistake for the real thing.
 */

export type OfSourceKind = "real" | "inferred" | "unknown";

const REAL_SOURCES = new Set(["market_ticks", "tick_reconstruction", "tick_reconstruction_book"]);
const INFERRED_SOURCES = new Set([
  "bar_inference",
  "bar_proxy",
  "bar_fallback",
  "bar_proxy_timeout",
  "insufficient_ticks",
  "spot_index_proxy",
]);

export function classifyOfSource(source?: string | null): { kind: OfSourceKind; label: string } {
  const s = String(source || "").trim().toLowerCase();
  if (REAL_SOURCES.has(s)) {
    return { kind: "real", label: s === "tick_reconstruction_book" ? "REAL TICKS · BOOK" : "REAL TICKS" };
  }
  if (INFERRED_SOURCES.has(s)) {
    return { kind: "inferred", label: s === "insufficient_ticks" ? "INSUFFICIENT TICKS · INFERRED" : "BAR PROXY · INFERRED" };
  }
  if (!s || s === "unavailable" || s === "unknown") return { kind: "unknown", label: "SOURCE UNKNOWN" };
  // Unrecognized source string — show it verbatim but flag as inferred-grade.
  return { kind: "inferred", label: `${s.replace(/_/g, " ").toUpperCase()} · UNVERIFIED` };
}

const STYLE: Record<OfSourceKind, string> = {
  real: "border-accent-green/50 bg-accent-green/15 text-accent-green",
  inferred: "border-accent-amber/50 bg-accent-amber/15 text-accent-amber animate-pulse",
  unknown: "border-bg-border bg-bg-secondary/40 text-text-muted",
};

export function OfSourceBadge({
  source,
  size = "md",
  className,
}: {
  source?: string | null;
  size?: "sm" | "md";
  className?: string;
}) {
  const { kind, label } = classifyOfSource(source);
  const pad = size === "sm" ? "px-1.5 py-[1px] text-[9px]" : "px-2.5 py-0.5 text-[10.5px]";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border font-semibold uppercase tracking-[0.12em] ${pad} ${STYLE[kind]} ${className ?? ""}`}
      title={`order-flow source: ${source || "not reported"}`}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          kind === "real" ? "bg-accent-green" : kind === "inferred" ? "bg-accent-amber" : "bg-text-muted"
        }`}
      />
      {label}
    </span>
  );
}
