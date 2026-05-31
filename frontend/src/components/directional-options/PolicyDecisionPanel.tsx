"use client";

/**
 * Per-symbol RL policy decision panel.
 *
 * Replaces the old "Risk approved? Yes / No" boolean with a window
 * into WHY the policy chose to fire or skip:
 *
 *   - Sampled R-multiple (the Thompson draw) vs. posterior mean
 *   - Size multiplier picked (0.5×/1.0×/1.5×/2.0×) + the per-bucket samples
 *   - Per-candidate samples — which strike won the ranking and by how much
 *   - n_seen counter so you can tell whether the posterior has tightened
 *
 * Reads `snapshot.policy` from the live-snapshot payload (see
 * service.py::_policy_pick).
 */
import { clsx } from "clsx";
import { Sparkles, Target, TrendingDown, TrendingUp } from "lucide-react";

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

export type CandidateRow = {
  trading_symbol: string;
  strike: number;
  option_type: string;
  delta?: number;
  delta_bucket?: string;
  contract_score?: number;
  option_price?: number;
};

function fmt(v: number | undefined | null, digits = 3, withSign = false): string {
  if (v == null || Number.isNaN(v)) return "—";
  const formatted = v.toFixed(digits);
  if (withSign && v >= 0) return `+${formatted}`;
  return formatted;
}

function bucketColor(v: number | undefined): string {
  if (v == null) return "text-text-muted";
  if (v > 0.2) return "text-emerald-300";
  if (v > 0) return "text-emerald-200";
  if (v > -0.2) return "text-amber-300";
  return "text-rose-300";
}

export default function PolicyDecisionPanel({
  policy,
  candidates,
}: {
  policy: PolicyBlock | null | undefined;
  candidates?: CandidateRow[];
}) {
  if (!policy) {
    return (
      <div className="rounded-2xl border border-bg-border bg-bg-secondary/20 p-4 text-sm text-text-muted">
        Policy block unavailable. Either the RL policy is disabled in
        config or no signal fired on the current bar.
      </div>
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
    <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Sparkles size={16} />
            RL Policy Decision
          </div>
          <div className="mt-1 text-xs text-text-muted">
            Thompson sample drives act/skip; per-bucket posteriors pick size.
          </div>
        </div>
        <span
          className={clsx(
            "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
            act
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
              : "border-amber-500/40 bg-amber-500/10 text-amber-200",
          )}
        >
          <ActIcon size={12} />
          {act ? "ACT" : "SKIP"}
        </span>
      </div>

      {/* Value posterior */}
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Sampled R</div>
          <div className={clsx("mt-1 font-mono text-xl font-semibold", sampled >= 0 ? "text-emerald-300" : "text-rose-300")}>
            {fmt(sampled, 3, true)}
          </div>
          <div className="mt-0.5 text-[11px] text-text-muted">Decision threshold = 0</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Posterior μ</div>
          <div className={clsx("mt-1 font-mono text-xl font-semibold", mean >= 0 ? "text-emerald-300" : "text-rose-300")}>
            {fmt(mean, 3, true)}
          </div>
          <div className="mt-0.5 text-[11px] text-text-muted">± σ {fmt(stdev, 2)}</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Size Multiplier</div>
          <div className="mt-1 font-mono text-xl font-semibold text-text-primary">
            {fmt(sizeMult, 2)}×
          </div>
          <div className="mt-0.5 text-[11px] text-text-muted">Of base risk budget</div>
        </div>
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
          <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Trades Seen</div>
          <div className="mt-1 font-mono text-xl font-semibold text-text-primary">
            {policy.n_seen ?? 0}
          </div>
          <div className="mt-0.5 text-[11px] text-text-muted">{policy.feature_dim ?? 0}-D features</div>
        </div>
      </div>

      {/* Size bucket samples */}
      {Object.keys(sizeSamples).length > 0 ? (
        <div className="mt-5">
          <div className="mb-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Size-bucket Thompson samples (this cycle)
          </div>
          <div className="grid grid-cols-4 gap-2">
            {["0.50", "1.00", "1.50", "2.00"].map((key) => {
              const v = sizeSamples[key];
              const isChosen = Math.abs(parseFloat(key) - sizeMult) < 1e-6;
              return (
                <div
                  key={key}
                  className={clsx(
                    "rounded-xl border px-2 py-2 text-center",
                    isChosen
                      ? "border-accent-blue/50 bg-accent-blue/10"
                      : "border-bg-border bg-bg-primary/10",
                  )}
                >
                  <div className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">
                    {parseFloat(key).toFixed(1)}×
                  </div>
                  <div className={clsx("mt-1 font-mono text-sm", bucketColor(v))}>
                    {fmt(v, 3, true)}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* Per-candidate ranking */}
      {candidates && candidates.length > 0 && candidateSamples.length === candidates.length ? (
        <div className="mt-5">
          <div className="mb-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Candidate ranking · Thompson samples per strike
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th className="text-left pb-1.5"></th>
                  <th className="text-left pb-1.5">Strike</th>
                  <th className="text-left pb-1.5">Δ-bucket</th>
                  <th className="text-right pb-1.5">Sampled R</th>
                  <th className="text-right pb-1.5">Selector score</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((c, i) => {
                  const sample = candidateSamples[i];
                  const isPicked = chosenIdx === i;
                  return (
                    <tr
                      key={`${c.trading_symbol}-${i}`}
                      className={clsx(
                        "border-t border-bg-border/40",
                        isPicked && "bg-accent-blue/8",
                      )}
                    >
                      <td className="py-1.5">
                        {isPicked ? <Target size={12} className="text-accent-blue" /> : null}
                      </td>
                      <td className="font-mono text-text-primary">
                        {c.strike} {c.option_type}
                      </td>
                      <td className="text-text-muted">{c.delta_bucket ?? "—"}</td>
                      <td className={clsx("text-right font-mono", sample >= 0 ? "text-emerald-300" : "text-rose-300")}>
                        {fmt(sample, 3, true)}
                      </td>
                      <td className="text-right font-mono text-text-secondary">
                        {fmt(c.contract_score, 1)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {/* Reason */}
      {policy.reason ? (
        <div className="mt-4 rounded-xl border border-bg-border bg-bg-secondary/30 p-3 text-[11.5px] text-text-secondary">
          {policy.reason}
        </div>
      ) : null}
    </div>
  );
}
