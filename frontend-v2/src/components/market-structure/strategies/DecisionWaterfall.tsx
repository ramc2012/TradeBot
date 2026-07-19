"use client";

/**
 * DecisionWaterfall — the auditable path from data to execution, per policy.
 *
 * This replaces a long badge list with something a trader can actually follow:
 * six ordered stages, each PASSED / FAILED / UNAVAILABLE, and clicking a stage
 * reveals the payload field the verdict was read from and the value it held.
 *
 * The three verdicts are visually distinct on purpose. UNAVAILABLE is the one
 * that earns this component its keep: ten of the twenty-four stage cells across
 * the four policies are genuinely unavailable — three of the four lanes emit no
 * flow-confirmation gate and none but Convergence emits an anti-chase test — and
 * an interface that showed those as "failed", or hid them, would be asserting
 * something the lanes never computed.
 */
import { Check, Minus, X } from "lucide-react";
import { useState } from "react";

import {
  STAGE_LABEL,
  waterfallCoverage,
  type StageVerdict,
  type WaterfallStage,
} from "@/lib/policy-state";

const VERDICT_CLASS: Record<StageVerdict, string> = {
  passed: "border-accent-green/40 bg-accent-green/[0.08] text-accent-green",
  failed: "border-accent-red/40 bg-accent-red/[0.08] text-accent-red",
  unavailable: "border-bg-border/70 bg-bg-primary/10 text-text-muted",
};

const VERDICT_ICON: Record<StageVerdict, React.ReactNode> = {
  passed: <Check size={11} />,
  failed: <X size={11} />,
  unavailable: <Minus size={11} />,
};

export function DecisionWaterfall({
  title,
  stages,
}: {
  title: string;
  stages: WaterfallStage[];
}) {
  const [open, setOpen] = useState<string | null>(null);
  const cov = waterfallCoverage(stages);
  const shown = stages.find((s) => s.key === open) ?? null;

  return (
    <section className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-text-primary">{title}</h3>
        <span className="font-mono text-[10.5px] text-text-muted">
          {cov.passed} passed · {cov.failed} failed · {cov.unavailable} not emitted
        </span>
      </div>

      <ol className="mt-2.5 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
        {stages.map((s, i) => (
          <li key={s.key}>
            <button
              type="button"
              onClick={() => setOpen(open === s.key ? null : s.key)}
              aria-expanded={open === s.key}
              className={
                "flex w-full items-center gap-1.5 rounded-lg border px-2 py-1.5 text-left transition-opacity hover:opacity-90 " +
                VERDICT_CLASS[s.verdict]
              }
            >
              <span className="font-mono text-[9.5px] opacity-70">{i + 1}</span>
              {VERDICT_ICON[s.verdict]}
              <span className="truncate text-[11px] font-semibold">{STAGE_LABEL[s.key]}</span>
            </button>
          </li>
        ))}
      </ol>

      {shown ? (
        <div className="mt-2.5 rounded-xl border border-bg-border/70 bg-bg-primary/20 px-3 py-2">
          <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">
            {STAGE_LABEL[shown.key]} · {shown.verdict === "unavailable" ? "not emitted" : shown.verdict}
          </div>
          {shown.observationField ? (
            <div className="mt-1 font-mono text-[11px] text-text-secondary">
              {shown.observationField}
            </div>
          ) : null}
          {shown.observationValue ? (
            <div className="mt-0.5 font-mono text-[11px] text-text-primary">
              {shown.observationValue}
            </div>
          ) : null}
          {shown.reason ? (
            <p className="mt-1 max-w-prose text-[11px] leading-4 text-text-muted">{shown.reason}</p>
          ) : null}
          {!shown.observationField && !shown.observationValue && !shown.reason ? (
            <p className="mt-1 text-[11px] text-text-muted">
              No observation was recorded for this stage.
            </p>
          ) : null}
        </div>
      ) : (
        <p className="mt-2 text-[10.5px] text-text-muted">
          Click a stage to see the payload field it was read from.
        </p>
      )}
    </section>
  );
}
