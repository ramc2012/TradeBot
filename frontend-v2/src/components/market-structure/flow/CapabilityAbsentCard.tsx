"use client";

/**
 * CapabilityAbsentCard — the panel a terminal renders where a capability the
 * stack does NOT have would otherwise go.
 *
 * This is a distinct visual class from stale and from measured-zero, on purpose:
 *
 *   · neutral border, lock glyph, struck-through title
 *   · NO timestamp — there is no observation to be old
 *   · NO refresh affordance — nothing would arrive
 *   · the reason in prose, plus the repo file that establishes it
 *
 * It exists because the alternative is what the design brief calls out
 * specifically: rendering the broker DEPTH PROXY inside a slot labelled
 * "Depth", or a reconstructed quote tape inside a slot labelled "Trade tape".
 * Both would function as the real thing on screen while being reconstructions.
 *
 * The content is not written here — it comes from `MISSING_CAPABILITIES` in the
 * shared contract, so this card, the flow view and any future horizon gating
 * all read one table.
 */
import { Lock } from "lucide-react";

import { missingCapability, type CapabilityKey } from "@/lib/market-semantics";

export function CapabilityAbsentCard({
  capability,
  slotTitle,
  className,
}: {
  capability: CapabilityKey;
  /** The panel this card stands in for, e.g. "Order-book depth (DOM)". */
  slotTitle: string;
  className?: string;
}) {
  const cap = missingCapability(capability);
  return (
    <section
      className={
        "rounded-2xl border border-bg-border/70 bg-bg-primary/10 p-4 " + (className ?? "")
      }
      aria-label={`${slotTitle} — capability not available`}
    >
      <div className="flex items-start gap-2">
        <Lock size={14} className="mt-0.5 shrink-0 text-text-muted" />
        <div className="min-w-0">
          <div className="text-[13px] font-semibold text-text-muted line-through decoration-text-muted/50">
            {slotTitle}
          </div>
          <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted">
            capability not available · {cap.key}
          </div>
        </div>
      </div>

      <p className="mt-2 max-w-prose text-[11.5px] leading-5 text-text-secondary/80">
        <span className="font-semibold text-text-secondary">{cap.label}</span> — {cap.reason}
      </p>
      <p className="mt-1.5 max-w-prose text-[11px] leading-4 text-text-muted">
        Needed for: {cap.wants}.
      </p>
      <p className="mt-1.5 max-w-prose text-[11px] leading-4 text-text-muted">
        Use instead: {cap.insteadUse}.
      </p>
      <p className="mt-2 font-mono text-[10px] leading-4 text-text-muted/80">{cap.citation}</p>
    </section>
  );
}
