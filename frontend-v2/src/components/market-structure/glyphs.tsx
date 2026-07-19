"use client";

/**
 * Matrix glyphs — the dense, scannable cell vocabulary.
 *
 * Every glyph here is a pure function of already-derived semantics. None of
 * them fetches, subscribes, or holds a timer: a matrix row must cost nothing
 * but a render. Colour meaning is inherited from the shared contract
 * (`lib/market-semantics`) so a green here means exactly what a green means on
 * every desk.
 *
 * THE THREE-STATE RULE, applied to every cell:
 *   missing → "—", muted, dotted underline, title says WHY there is no source
 *   zero    → the numeral, normal weight (a measured zero is information)
 *   stale   → dimmed amber with its age
 */
import { clsx } from "clsx";

import { setupStageVariant, type BadgeVariant, type Freshness, type Sufficiency } from "@/lib/market-semantics";

/**
 * The ONE place a semantic variant becomes a text colour in this workspace.
 * Green is reserved for healthy-live / actionable-confirmed; everything that
 * merely means "armed and waiting" is blue. The variant itself is decided by
 * the shared contract (`lib/status-variants`), never re-decided per glyph.
 */
const VARIANT_TEXT: Record<BadgeVariant, string> = {
  success: "text-accent-green",
  info: "text-accent-blue",
  warn: "text-accent-amber",
  error: "text-accent-red",
  neutral: "text-text-muted",
};

/** The one way this workspace renders "there is no source for this cell". */
export function Unavailable({ reason, className }: { reason?: string | null; className?: string }) {
  return (
    <span
      className={clsx(
        "cursor-help border-b border-dotted border-text-muted/60 font-mono text-text-muted",
        className,
      )}
      title={reason ? `unavailable — ${reason}` : "not reported"}
    >
      —
    </span>
  );
}

const FRESHNESS_DOT: Record<Freshness, string> = {
  fresh: "bg-accent-green",
  stale: "bg-accent-amber",
  absent: "bg-text-muted",
};

const SUFFICIENCY_TEXT: Record<Sufficiency, string> = {
  ok: "text-accent-green",
  degraded: "text-accent-amber",
  insufficient: "text-text-muted",
};

/** Readiness = freshness dot + age + sufficiency tint. No live claim is made. */
export function ReadinessGlyph({
  freshness,
  sufficiency,
  age,
  reasons,
}: {
  freshness: Freshness;
  sufficiency: Sufficiency;
  age: string;
  reasons: string[];
}) {
  return (
    <span
      className={clsx("inline-flex items-center gap-1.5 font-mono", SUFFICIENCY_TEXT[sufficiency])}
      title={[
        `freshness: ${freshness}`,
        `sufficiency: ${sufficiency}`,
        ...(reasons.length ? [`reasons: ${reasons.join("; ")}`] : []),
      ].join("\n")}
    >
      <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", FRESHNESS_DOT[freshness])} />
      {freshness === "absent" ? "no ts" : age}
    </span>
  );
}

/**
 * Convergence lifecycle stage. ARMED is not a trade; MISSED is not a signal.
 *
 * ARMED used to render GREEN here, which put it in the same visual class as a
 * TRIGGERED setup and as healthy-live data. It is now blue, via the shared
 * `setupStageVariant` contract — so the matrix cell, the drawer badge and any
 * future surface all move together.
 */

export function StageGlyph({
  stage,
  confirmations,
  required,
  blocked,
}: {
  stage: string | null;
  confirmations: number | null;
  required: number | null;
  blocked: string[];
}) {
  if (!stage) return <Unavailable reason="no setup state in this cycle" />;
  const conf =
    confirmations != null && required != null ? `${confirmations}/${required}` : null;
  return (
    <span
      className={clsx("inline-flex items-center gap-1.5 font-mono", VARIANT_TEXT[setupStageVariant(stage)])}
      title={blocked.length ? `blocked by: ${blocked.join(", ")}` : "no blockers reported"}
    >
      <span className="truncate">{stage.toLowerCase().replace(/_/g, " ")}</span>
      {conf ? <span className="text-text-muted">{conf}</span> : null}
      {blocked.length ? <span className="text-accent-amber">⊘{blocked.length}</span> : null}
    </span>
  );
}

/** A setup/signal with its strength. A null signal with a reason is NOT blank. */
export function SignalGlyph({
  signal,
  candidate,
  reason,
  confidence,
  detail,
}: {
  signal: string | null;
  candidate: string | null;
  reason: string | null;
  confidence: number | null;
  detail: string | null;
}) {
  const title = [detail, reason ? `reason: ${reason}` : null].filter(Boolean).join("\n") || undefined;
  if (signal) {
    return (
      <span className="inline-flex items-center gap-1.5 font-mono text-accent-green" title={title}>
        {signal.toLowerCase()}
        {confidence != null ? <span className="text-text-muted">{confidence.toFixed(2)}</span> : null}
      </span>
    );
  }
  if (candidate) {
    return (
      <span className="inline-flex items-center gap-1.5 font-mono text-accent-amber" title={title}>
        cand {candidate.toLowerCase()}
      </span>
    );
  }
  if (reason) {
    return (
      <span className="cursor-help font-mono text-text-muted" title={title}>
        no signal · {reason.replace(/_/g, " ")}
      </span>
    );
  }
  return <Unavailable reason="lane reported neither a signal nor a reason" />;
}

/** Portfolio intent: net direction + leg count. Zero legs is a real "flat". */
export function IntentGlyph({
  side,
  legs,
  lanes,
}: {
  side: "LONG" | "SHORT" | "MIXED" | null;
  legs: number;
  lanes: string[];
}) {
  if (legs === 0) {
    // NOT the same claim as "this instrument is flat". Only the real broker
    // book and the auction paper book are polled at universe scale; a lane
    // whose only position endpoint is megabyte-scale (directional-options
    // paper positions, ~1.4 MB) is not read here. So this says exactly what
    // was checked — hence the dotted underline that marks every partial cell.
    return (
      <span
        className="cursor-help border-b border-dotted border-text-muted/60 font-mono text-text-muted"
        title="flat in the polled books (real broker book + auction paper book). Lanes with megabyte-scale position endpoints are not polled at universe scale — check their desk before concluding no exposure."
      >
        flat*
      </span>
    );
  }
  const cls =
    side === "LONG" ? "text-accent-green" : side === "SHORT" ? "text-accent-red" : "text-accent-amber";
  return (
    <span className={clsx("font-mono", cls)} title={`lanes: ${lanes.join(", ") || "unknown"}`}>
      {String(side ?? "mixed").toLowerCase()} · {legs}
    </span>
  );
}

/** Risk-plan availability, straight off the shared rrRender verdict. */
export function RiskGlyph({
  available,
  reason,
  planComplete,
  missing,
  rrText,
}: {
  available: boolean;
  reason: string | null;
  planComplete: boolean | null;
  missing: string[];
  rrText: string;
}) {
  if (!available) return <Unavailable reason={reason} />;
  if (planComplete) {
    return (
      <span className="font-mono text-accent-green" title="entry, stop and target-1 all present">
        {rrText}
      </span>
    );
  }
  return (
    <span
      className="cursor-help font-mono text-text-muted"
      title={`R/R unavailable — missing ${missing.join(", ") || "plan fields"}`}
    >
      no {missing.join("/") || "plan"}
    </span>
  );
}
