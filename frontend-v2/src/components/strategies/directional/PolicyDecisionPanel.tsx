"use client";

/**
 * RL policy decision panel — slimmer rebuild of v1's PolicyDecisionPanel.
 * Uses desk-ui primitives (StatusBadge, Section, tones) instead of
 * inline className strings.
 */
import { clsx } from "clsx";
import { Sparkles, Target, TrendingDown, TrendingUp } from "lucide-react";

import { Section, StatusBadge, formatNumber, formatSignedNumber, tone } from "@/components/desk-ui";

export type PolicyBlock = {
  act?: boolean;
  size_multiplier?: number;
  sampled_value?: number;
  posterior_mean?: number;
  posterior_var?: number;
  reason?: string;
  n_seen?: number;
  feature_dim?: number;
  candidate_index?: number;
  candidate_samples?: number[];
  size_samples?: Record<string, number>;
};

type CandidateRow = {
  trading_symbol?: string;
  strike?: number;
  option_type?: string;
  delta?: number;
  delta_bucket?: string;
  contract_score?: number;
};

export default function PolicyDecisionPanel({
  policy,
  candidates,
}: {
  policy: PolicyBlock | null;
  candidates?: CandidateRow[];
}) {
  if (!policy) {
    return (
      <Section title="RL Policy decision" icon={<Sparkles size={16} />}>
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-sm text-text-muted">
          No signal on this bar — the policy was not called. The Engine
          calculations panel below shows what regime / features the engine
          is computing while it waits.
        </div>
      </Section>
    );
  }

  const act = !!policy.act;
  const sampled = policy.sampled_value ?? 0;
  const mean = policy.posterior_mean ?? 0;
  const stdev = Math.sqrt(Math.max(policy.posterior_var ?? 0, 0));
  const sizeMult = policy.size_multiplier ?? 1.0;
  const sizeSamples = policy.size_samples ?? {};
  const chosenIdx = policy.candidate_index ?? null;
  const candidateSamples = policy.candidate_samples ?? [];
  const ActIcon = act ? TrendingUp : TrendingDown;

  return (
    <Section
      title="RL Policy decision"
      icon={<Sparkles size={16} />}
      description="Thompson sample drives act/skip; per-bucket posteriors pick size."
      rightSlot={
        <StatusBadge label={act ? "ACT" : "SKIP"} variant={act ? "success" : "warn"} icon={<ActIcon size={11} />} />
      }
    >
      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Sampled R</div>
          <div className={clsx("mt-1 font-mono text-xl font-semibold", tone(sampled))}>{formatSignedNumber(sampled, 3)}</div>
          <div className="mt-0.5 text-[11px] text-text-muted">Threshold = 0</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Posterior μ</div>
          <div className={clsx("mt-1 font-mono text-xl font-semibold", tone(mean))}>{formatSignedNumber(mean, 3)}</div>
          <div className="mt-0.5 text-[11px] text-text-muted">± σ {formatNumber(stdev, 2)}</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Size multiplier</div>
          <div className="mt-1 font-mono text-xl font-semibold text-text-primary">{formatNumber(sizeMult, 2)}×</div>
          <div className="mt-0.5 text-[11px] text-text-muted">Of base risk budget</div>
        </div>
        <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Trades seen</div>
          <div className="mt-1 font-mono text-xl font-semibold text-text-primary">{policy.n_seen ?? 0}</div>
          <div className="mt-0.5 text-[11px] text-text-muted">{policy.feature_dim ?? 0}-D features</div>
        </div>
      </div>

      {Object.keys(sizeSamples).length > 0 ? (
        <div className="mt-4">
          <div className="mb-1.5 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Size-bucket samples (this cycle)
          </div>
          <div className="grid grid-cols-4 gap-2">
            {["0.50", "1.00", "1.50", "2.00"].map((k) => {
              const v = sizeSamples[k];
              const isChosen = Math.abs(parseFloat(k) - sizeMult) < 1e-6;
              return (
                <div
                  key={k}
                  className={clsx(
                    "rounded-lg border px-2 py-2 text-center",
                    isChosen ? "border-accent-blue/45 bg-accent-blue/10" : "border-bg-border bg-bg-primary/10",
                  )}
                >
                  <div className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">{parseFloat(k).toFixed(1)}×</div>
                  <div className={clsx("mt-1 font-mono text-sm", tone(v))}>{formatSignedNumber(v, 3)}</div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {candidates && candidates.length > 0 && candidateSamples.length === candidates.length ? (
        <div className="mt-4">
          <div className="mb-1.5 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Candidate ranking · Thompson samples per strike
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th></th>
                  <th className="text-left pb-1.5">Strike</th>
                  <th className="text-left pb-1.5">Δ-bucket</th>
                  <th className="text-right pb-1.5">Sampled R</th>
                  <th className="text-right pb-1.5">Selector score</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((c, i) => {
                  const sample = candidateSamples[i];
                  const picked = chosenIdx === i;
                  return (
                    <tr key={`${c.trading_symbol}-${i}`} className={clsx("border-t border-bg-border/40", picked && "bg-accent-blue/8")}>
                      <td className="py-1.5">{picked ? <Target size={12} className="text-accent-blue" /> : null}</td>
                      <td className="font-mono text-text-primary">{c.strike} {c.option_type}</td>
                      <td className="text-text-muted">{c.delta_bucket ?? "—"}</td>
                      <td className={clsx("text-right font-mono", tone(sample))}>{formatSignedNumber(sample, 3)}</td>
                      <td className="text-right font-mono text-text-secondary">{formatNumber(c.contract_score, 1)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {policy.reason ? (
        <div className="mt-4 rounded-lg border border-bg-border bg-bg-secondary/30 p-2.5 text-[11.5px] text-text-secondary">
          {policy.reason}
        </div>
      ) : null}
    </Section>
  );
}
