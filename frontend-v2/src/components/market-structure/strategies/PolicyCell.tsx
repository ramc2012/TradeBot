"use client";

/**
 * PolicyCell — one policy's read on the pinned instrument, at one horizon.
 *
 * Four visually distinct outcomes, and the distinctions are the whole point:
 *
 *   · a real state          → the badge, the side, the confidence, the blockers
 *   · nothing measured yet  → UNAVAILABLE with the reason (neutral, locked)
 *   · not this horizon      → "does not operate at this horizon" + evidence
 *   · a heavy fetch pending → an explicit load affordance, never an auto-poll
 *
 * Colour comes from `policyStateVariant`, which delegates to the terminal's one
 * green-semantics contract: green ONLY for actionable-confirmed, blue for
 * armed, neutral for unavailable (nothing is wrong, so nothing is amber).
 */
import { Lock } from "lucide-react";

import { StatusBadge } from "@/components/desk-ui";
import {
  POLICY_HORIZONS,
  POLICY_LABEL,
  policyStateLabel,
  policyStateVariant,
  type PolicyCellData,
  type PolicyId,
} from "@/lib/policy-state";
import type { TradingHorizon } from "@/lib/lane-taxonomy";

export function PolicyCell({
  data,
  horizon,
  loader,
  selected,
  onSelect,
}: {
  data: PolicyCellData;
  horizon: TradingHorizon;
  loader: { label: string; loaded: boolean; loading: boolean; onLoad: () => void } | null;
  selected: boolean;
  onSelect: () => void;
}) {
  const scope = POLICY_HORIZONS[data.policyId];
  const operates = scope.horizons.includes(horizon);

  if (!operates) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 bg-bg-primary/10 p-2.5">
        <div className="text-[11px] font-semibold text-text-muted">
          {POLICY_LABEL[data.policyId]}
        </div>
        <div className="mt-1 text-[10.5px] leading-4 text-text-muted">
          does not operate at this horizon
        </div>
        <div className="mt-1 text-[10px] leading-4 text-text-muted/80">{scope.evidence}</div>
      </div>
    );
  }

  const unavailable = data.state === "UNAVAILABLE";

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={
        "w-full rounded-xl border p-2.5 text-left transition-colors " +
        (selected
          ? "border-accent-blue/70 bg-bg-secondary/40"
          : "border-bg-border bg-bg-secondary/20 hover:border-accent-blue/40")
      }
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-semibold text-text-secondary">
          {POLICY_LABEL[data.policyId]}
        </span>
        {unavailable ? (
          <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.12em] text-text-muted">
            <Lock size={10} /> unavailable
          </span>
        ) : (
          <StatusBadge label={policyStateLabel(data.state)} variant={policyStateVariant(data.state)} />
        )}
      </div>

      {unavailable ? (
        <p className="mt-1.5 text-[10.5px] leading-4 text-text-muted">{data.unavailableReason}</p>
      ) : (
        <>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-mono text-[12px] text-text-primary">
              {data.direction ?? "—"}
            </span>
            <span className="font-mono text-[10.5px] text-text-muted">
              {data.confidence == null
                ? "confidence not emitted"
                : `${(data.confidence * 100).toFixed(0)}% conf`}
            </span>
          </div>
          {data.nativeState ? (
            <div
              className="mt-0.5 font-mono text-[10px] text-text-muted"
              title="the lane's own state word, verbatim"
            >
              {data.nativeState}
            </div>
          ) : null}
          <div className="mt-1 text-[10px] text-text-muted">
            {data.validity ?? "validity window not emitted by this lane"}
          </div>
          {data.blockers.length ? (
            <ul className="mt-1 space-y-0.5">
              {data.blockers.slice(0, 3).map((b) => (
                <li key={b} className="truncate text-[10.5px] text-accent-red/90" title={b}>
                  {b}
                </li>
              ))}
              {data.blockers.length > 3 ? (
                <li className="text-[10px] text-text-muted">
                  +{data.blockers.length - 3} more
                </li>
              ) : null}
            </ul>
          ) : null}
          {data.note ? (
            <p className="mt-1 line-clamp-3 text-[10px] leading-4 text-text-muted">{data.note}</p>
          ) : null}
        </>
      )}

      {loader && !loader.loaded ? (
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation();
            loader.onLoad();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.stopPropagation();
              loader.onLoad();
            }
          }}
          className="mt-2 inline-flex rounded-lg border border-bg-border px-2 py-0.5 text-[10.5px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary"
        >
          {loader.loading ? "loading…" : loader.label}
        </span>
      ) : null}
    </button>
  );
}
