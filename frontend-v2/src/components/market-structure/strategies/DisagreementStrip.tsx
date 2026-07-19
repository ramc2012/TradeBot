"use client";

/**
 * DisagreementStrip — where the policies CONFLICT on the pinned instrument.
 *
 * Disagreement is the product. There is deliberately no consensus score, no
 * average and no "3 of 4 agree" tally anywhere in this component or the module
 * behind it: four lanes built on different evidence are not commensurable, and
 * collapsing them into one number destroys exactly the information a trader
 * came here for.
 *
 * Two honest empty states, and they are NOT the same:
 *   · several policies hold opinions and none conflict → "no conflict"
 *   · fewer than two policies hold an opinion at all   → "no second opinion",
 *     because a lone read is not agreement
 */
import { GitCompareArrows } from "lucide-react";

import { NO_SECOND_OPINION, type Disagreement } from "@/lib/policy-state";

export function DisagreementStrip({
  disagreements,
  opinions,
  symbol,
}: {
  disagreements: Disagreement[];
  opinions: number;
  symbol: string;
}) {
  const conflicted = disagreements.length > 0;
  return (
    <section
      className={
        "rounded-2xl border p-3 " +
        (conflicted
          ? "border-amber-400/40 bg-amber-400/[0.06]"
          : "border-bg-border bg-bg-secondary/20")
      }
      aria-label="Policy disagreement"
    >
      <div className="flex items-center gap-2">
        <GitCompareArrows size={14} className={conflicted ? "text-amber-300" : "text-text-muted"} />
        <h3 className="text-[12.5px] font-semibold text-text-primary">
          Disagreement on {symbol}
        </h3>
        <span className="font-mono text-[10.5px] text-text-muted">
          {opinions} of 5 policies hold an opinion
        </span>
      </div>

      {conflicted ? (
        <ul className="mt-2 space-y-1">
          {disagreements.map((d) => (
            <li key={`${d.a}-${d.b}-${d.kind}`} className="text-[11.5px] leading-5 text-text-secondary">
              <span className="mr-1.5 rounded px-1 py-0 font-mono text-[9.5px] uppercase tracking-[0.1em] text-text-muted">
                {d.kind === "opposite_direction" ? "opposite side" : "actionable vs blocked"}
              </span>
              {d.detail}
            </li>
          ))}
        </ul>
      ) : opinions < 2 ? (
        <p className="mt-2 text-[11.5px] leading-5 text-text-muted">{NO_SECOND_OPINION}</p>
      ) : (
        <p className="mt-2 text-[11.5px] leading-5 text-text-muted">
          No policy currently contradicts another on this instrument. That is an absence of
          conflict among {opinions} reads — it is not a combined score, and none is computed.
        </p>
      )}
    </section>
  );
}
